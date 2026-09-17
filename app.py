import asyncio
import base64
import io
import json
import os
import re
import hashlib
import shutil
import subprocess
import sys
from urllib.parse import urljoin, urlparse

import jieba
import jieba.analyse
import requests
import streamlit as st
import edge_tts
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

# --------------------------------------------------
# 0. 环境与依赖诊断 (自动激活原生 FFmpeg / imageio-ffmpeg & Python 3.14 audioop 适配)
# --------------------------------------------------
try:
    import audioop
except ImportError:
    try:
        import audioop_lts as audioop
        sys.modules["audioop"] = audioop
    except ImportError:
        pass

for path_dir in ["/usr/bin", "/usr/local/bin", "/bin"]:
    if path_dir not in os.environ.get("PATH", "").split(os.path.pathsep):
        os.environ["PATH"] = path_dir + os.path.pathsep + os.environ.get("PATH", "")

HAS_PYDUB = False
FFMPEG_READY = False
FFMPEG_SOURCE = "未就绪"
FFMPEG_ERROR_MSG = ""

try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError as e:
    FFMPEG_ERROR_MSG = f"pydub 导入失败: {e}"

ffmpeg_sys_path = shutil.which("ffmpeg")
if ffmpeg_sys_path:
    FFMPEG_READY = True
    FFMPEG_SOURCE = f"原生 FFmpeg ({ffmpeg_sys_path})"
    if HAS_PYDUB:
        AudioSegment.converter = ffmpeg_sys_path
        AudioSegment.ffmpeg = ffmpeg_sys_path
else:
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if ffmpeg_exe and os.path.exists(ffmpeg_exe):
            FFMPEG_READY = True
            FFMPEG_SOURCE = f"imageio-ffmpeg ({ffmpeg_exe})"
            ffmpeg_dir = os.path.dirname(ffmpeg_exe)
            if ffmpeg_dir not in os.environ.get("PATH", "").split(os.path.pathsep):
                os.environ["PATH"] = ffmpeg_dir + os.path.pathsep + os.environ.get("PATH", "")
            if HAS_PYDUB:
                AudioSegment.converter = ffmpeg_exe
                AudioSegment.ffmpeg = ffmpeg_exe
        else:
            FFMPEG_ERROR_MSG = "未检测到有效的 FFmpeg 二进制文件"
    except Exception as ex:
        FFMPEG_ERROR_MSG = f"imageio-ffmpeg 检测失败: {ex}"

CACHE_DIR = ".audio_cache"
BGM_DIR = "."
os.makedirs(CACHE_DIR, exist_ok=True)

# --------------------------------------------------
# 🧹 自动缓存清理（预防磁盘/内存溢出）
# --------------------------------------------------
def cleanup_cache_if_needed(max_size_mb=200):
    if not os.path.exists(CACHE_DIR):
        return
    try:
        files_with_size = [
            (os.path.join(CACHE_DIR, f), os.path.getsize(os.path.join(CACHE_DIR, f)))
            for f in os.listdir(CACHE_DIR)
            if os.path.isfile(os.path.join(CACHE_DIR, f))
        ]
        total_size = sum(size for _, size in files_with_size)
        if total_size > max_size_mb * 1024 * 1024:
            files_sorted = sorted(files_with_size, key=lambda x: os.path.getmtime(x[0]))
            for file_path, _ in files_sorted[: len(files_sorted) // 2]:
                try:
                    os.remove(file_path)
                except Exception:
                    pass
    except Exception:
        pass

cleanup_cache_if_needed(200)

try:
    import docx
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    import ebooklib
    from ebooklib import epub
    HAS_EPUB = True
except ImportError:
    HAS_EPUB = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    HAS_YOUTUBE_API = True
except ImportError:
    HAS_YOUTUBE_API = False

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

# --------------------------------------------------
# 🌟 严选音色库配置
# --------------------------------------------------
CURATED_VOICES = {
    "zh-CN-YunxiNeural": "🎙️ [中文普通话] 云希 (有声书/广播剧热播·男)",
    "zh-CN-XiaoxiaoNeural": "🇨🇳 [中文普通话] 晓晓 (自然温柔·女)",
    "zh-CN-YunjianNeural": "📖 [中文旁白] 云健 (新闻评书/讲故事沉稳·男)",
    "zh-CN-XiaoyiNeural": "👩 [中文普通话] 晓伊 (多情感亲切·女)",
    "zh-CN-YunyangNeural": "📰 [中文播音] 云扬 (专业新闻广播·男)",
    "zh-HK-HiuGaaiNeural": "🇭🇰 [粤语/香港] 晓佳 (标准港剧腔·女)",
    "zh-TW-HsiaoChenNeural": "🇹🇼 [国语/台湾] 晓臻 (甜美台湾腔·女)",
    "en-US-JennyNeural": "🇺🇸 [美式英语] Jenny (标准清晰美音·女)",
    "en-US-GuyNeural": "🎙️ [美式英语] Guy (沉稳专业男音·男)",
    "en-GB-SoniaNeural": "🇬🇧 [英式英语] Sonia (标准优雅英音·女)",
    "ja-JP-NanamiNeural": "🇯🇵 [日语/Japan] 七海 (自然动漫播报·女)",
    "ko-KR-SunHiNeural": "🇰🇷 [韩语/Korea] 善熙 (标准韩剧女音·女)",
    "fr-FR-DeniseNeural": "🇫🇷 [法语/France] Denise (浪漫法音·女)",
    "de-DE-KatjaNeural": "🇩🇪 [德语/Germany] Katja (严谨德音·女)",
    "es-ES-ElviraNeural": "🇪🇸 [西班牙语] Elvira (热情标准西音·女)",
}

LOCALE_LANG_MAP = {'zh-CN': ('中文普通话', 'Mandarin'), 'en-US': ('美式英语', 'US English'), 'ja-JP': ('日语', 'Japanese')}
LOCALE_FLAGS = {'zh-CN': '🇨🇳', 'en-US': '🇺🇸', 'ja-JP': '🇯🇵'}

# --------------------------------------------------
# 1. 页面配置与 PWA 客户端化注入
# --------------------------------------------------
st.set_page_config(
    page_title="随身听书 & 思维助手 (PWA全能版)", page_icon="🎧", layout="centered"
)

# 动态注入 PWA Manifest 和移动端锁屏后台保活 JS
pwa_js = """
<script>
    (function() {
        const manifest = {
            "name": "随身听书 & 思维助手",
            "short_name": "AI听书",
            "start_url": ".",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#38bdf8",
            "icons": [{
                "src": "https://cdn-icons-png.flaticon.com/512/3039/3039387.png",
                "sizes": "512x512",
                "type": "image/png"
            }]
        };
        const stringManifest = JSON.stringify(manifest);
        const blob = new Blob([stringManifest], {type: 'application/json'});
        const manifestURL = URL.createObjectURL(blob);
        let link = document.createElement('link');
        link.rel = 'manifest';
        link.href = manifestURL;
        document.head.appendChild(link);

        let metaCapable = document.createElement('meta');
        metaCapable.name = "mobile-web-app-capable";
        metaCapable.content = "yes";
        document.head.appendChild(metaCapable);

        let metaApple = document.createElement('meta');
        metaApple.name = "apple-mobile-web-app-capable";
        metaApple.content = "yes";
        document.head.appendChild(metaApple);
    })();
</script>
"""
st.components.v1.html(pwa_js, height=0)

st.title("🎧 随身听书 & 思维助手 (PWA Ultimate Edition)")
st.caption(
    "全能旗舰版：支持单人听书/多角色广播剧 + PWA 桌面独立应用 + 动态闪避混音(Audio Ducking) + 锁屏后台播放"
)

# --------------------------------------------------
# 2. 侧边栏：朗读模式、引擎与影音 BGM 设置
# --------------------------------------------------
with st.sidebar:
    st.header("⚙️ 朗读模式与引擎设置")
    
    audio_mode = st.radio(
        "🎙️ 选择音频合成模式：",
        ["🎙️ 单人沉浸朗读 (专注听书)", "🎭 全自动 AI 广播剧 (男女多角色)"],
        index=0,
        help="【单人沉浸朗读】：使用单一精选音色贯穿全文，适合新闻、文章、书籍与记录片，完全支持所有导入内容！\n【全自动 AI 广播剧】：自动识别文中对话并分配男女声交替朗读，适合故事与小说。"
    )
    enable_multi_role = (audio_mode == "🎭 全自动 AI 广播剧 (男女多角色)")

    st.divider()

    if HAS_PYDUB and FFMPEG_READY:
        st.success(f"✅ BGM / 多角色混音引擎就绪\n({FFMPEG_SOURCE})")
    else:
        st.error(f"❌ BGM 混音受阻：未检测到 FFmpeg！\n(详情: {FFMPEG_ERROR_MSG if FFMPEG_ERROR_MSG else '路径检测失败'})")

    enable_bgm = st.checkbox(
        "🎵 开启 BGM 沉浸式背景音乐混音",
        value=False,
        help="开启后将为你生成的听书音频自动叠加背景音乐！"
    )
    
    available_bgm_files = [f for f in os.listdir(BGM_DIR) if f.lower().endswith(".mp3") and not f.startswith(".")]
    bgm_options = ["🎹 系统默认柔频和弦"] + available_bgm_files
    
    selected_bgm_name = st.selectbox(
        "🎶 选择背景音乐曲目：",
        options=bgm_options,
        index=0,
        help="你可以从下拉菜单切换已上传的不同 MP3 背景音乐"
    )
    
    bgm_volume = st.slider("🎚️ BGM 音量比例:", min_value=5, max_value=50, value=15, format="%d%%", help="自动配合动态闪避算法，建议设置在 10%~20% 之间")
    
    with st.expander("📤 上传我的背景音乐 (.mp3)", expanded=False):
        uploaded_bgm = st.file_uploader("选择手机里的 MP3 文件上传：", type=["mp3"], key="bgm_uploader")
        if uploaded_bgm is not None:
            save_path = os.path.join(BGM_DIR, uploaded_bgm.name)
            with open(save_path, "wb") as f:
                f.write(uploaded_bgm.read())
            st.success(f"🎉 成功上传 【{uploaded_bgm.name}】！刷新页面即可选择。")

    st.divider()
    show_all_voices = st.checkbox("🌐 显示全球 300+ 完整音色列表 (默认只看精选)", value=False)
    
    use_ollama = st.checkbox(
        "🧠 启用 Ollama (Qwen) 本地大模型",
        value=False,
        help="未安装 Ollama 请勿勾选。",
    )
    ollama_model = st.text_input(
        "Ollama 模型名称:",
        value="qwen2.5:1.5b",
    )
    st.divider()
    concurrency_limit = st.slider(
        "⚡ TTS 并发线程数",
        min_value=4,
        max_value=16,
        value=10,
    )

# --------------------------------------------------
# 3. 核心清洗引擎 & 网页处理
# --------------------------------------------------
def clean_extracted_text(text):
    if not text:
        return ""
    text = text.replace('﹗', '！').replace('﹖', '？').replace('......', '……')
    lines = text.split("\n")
    cleaned_lines = []
    noise_keywords = [
        "家庭发展基金", "家庭發展基金", "ICAC", "廉政公署", "署政", 
        "编者的话", "編者的話", "智多多大道理小故事", "智多多", 
        "製作", "制作", "贊助", "赞助", "版权所有", "版權所有",
        "All rights reserved", "ISBN", "关注微信公众号", "点击上方蓝字"
    ]
    noise_symbols = {"M", "W", "NNN", "B", "FES", "0", "00", "000"}
    page_patterns = [
        r'^\s*\d+(\s+\d+)*\s*$',
        r'^\s*-\s*\d+\s*-\s*$',
        r'^\s*第\s*\d+\s*[页頁]\s*$',
        r'^\s*Page\s*\d+\s*$',
        r'^[A-Z0-9_\-]+/\d+.*$',
        r'^\s*\d+\s*/\s*\d+\s*$',
    ]
    for line in lines:
        l = line.strip()
        if not l or l in noise_symbols:
            continue
        is_noise = False
        for pattern in page_patterns:
            if re.match(pattern, l, re.IGNORECASE):
                is_noise = True
                break
        if is_noise:
            continue
        if any(kw in l for kw in noise_keywords):
            continue
        cleaned_lines.append(l)
    full_text = "\n".join(cleaned_lines)
    full_text = re.sub(r'([^。！？!？…\n])\n([^。！？!？…\n])', r'\1\2', full_text)
    return full_text.strip()

def split_text_into_chapters(full_text):
    if not full_text:
        return []
    pattern = r'(?=\n\s*(?:第[0-9一二三四五六七八九十百千]+[章卷节部]|Chapter\s+\d+|【第.*章】))'
    parts = re.split(pattern, full_text)
    chapters = []
    for i, p in enumerate(parts):
        p_str = p.strip()
        if not p_str:
            continue
        first_line = p_str.split('\n')[0].strip()
        title = first_line[:30] if len(first_line) <= 35 else f"第 {i+1} 部分 ({first_line[:12]}...)"
        chapters.append({"title": title, "content": p_str})
    if not chapters:
        chapters = [{"title": "全文内容", "content": full_text}]
    return chapters

def parse_youtube_subtitle_text(raw_str):
    try:
        sub_data = json.loads(raw_str)
        if "events" in sub_data:
            lines = []
            for event in sub_data["events"]:
                if "segs" in event:
                    seg_text = "".join([s.get("utf8", "") for s in event["segs"]])
                    seg_text = seg_text.replace("\n", " ").strip()
                    if seg_text:
                        lines.append(seg_text)
            if lines:
                return " ".join(lines)
    except Exception:
        pass
    clean_text = re.sub(r'<[^>]+>', '', raw_str)
    clean_text = re.sub(r'\d{2}:\d{2}:\d{2}\.\d{3}.*?\n', '', clean_text)
    lines = [
        line.strip() 
        for line in clean_text.split('\n') 
        if line.strip() and not line.strip().isdigit() and not line.startswith('{')
    ]
    return " ".join(lines[:500])

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_text_from_url(url):
    if HAS_TRAFILATURA:
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                result = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
                if result and len(result.strip()) > 30:
                    return result.strip()
        except Exception:
            pass
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        response = requests.get(url, headers=headers, timeout=12)
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")
        for element in soup(["script", "style", "header", "footer", "nav", "aside"]):
            element.extract()
        paragraphs = soup.find_all(["p", "article", "h1", "h2", "h3", "section"])
        extracted_text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 10])
        if len(extracted_text) < 50:
            extracted_text = soup.get_text().strip()
        extracted_text = re.sub(r"\n\s*\n", "\n", extracted_text)
        return extracted_text
    except Exception as e:
        raise Exception(f"网页抓取失败: {e}")

def parse_book_catalog(catalog_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"}
    try:
        res = requests.get(catalog_url, headers=headers, timeout=12)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        parsed_url = urlparse(catalog_url)
        base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
        chapters = []
        for a in soup.find_all("a", href=True):
            text = a.get_text().strip()
            href = a['href'].strip()
            if not href or href.startswith("javascript:") or href == "#" or "openapp" in href.lower():
                continue
            if text and (("第" in text and "章" in text) or ("集" in text) or len(text) < 30):
                if any(kw in text for kw in ["首页", "书架", "登录", "目录", "作者", "意见", "关于", "上一页", "下一页", "尾页", "排行榜", "打开APP"]):
                    continue
                full_url = urljoin(base_domain, href) if not href.startswith("http") else href
                if full_url.startswith("http://") or full_url.startswith("https://"):
                    if not any(c['url'] == full_url for c in chapters):
                        chapters.append({"title": text, "url": full_url})
        return chapters
    except Exception as e:
        raise Exception(f"解析书本目录失败: {e}")

def extract_youtube_id(url):
    patterns = [r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})']
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

@st.cache_data(show_spinner=False, ttl=1800)
def fetch_youtube_transcript_backend(video_id, full_url=""):
    preferred_langs = ['zh-Hans', 'zh-Hant', 'zh', 'en', 'ja', 'es', 'de', 'fr', 'ko', 'vi', 'th', 'ru']
    if HAS_YOUTUBE_API:
        try:
            data = YouTubeTranscriptApi.get_transcript(video_id, languages=preferred_langs)
            full_text = " ".join([item['text'] for item in data])
            return True, full_text, "auto"
        except Exception:
            try:
                data = YouTubeTranscriptApi.get_transcript(video_id)
                full_text = " ".join([item['text'] for item in data])
                return True, full_text, "auto"
            except Exception:
                pass
    if HAS_YTDLP and full_url:
        try:
            ydl_opts = {
                'skip_download': True, 'writesubtitles': True, 'writeautomaticsub': True,
                'subtitleslangs': preferred_langs, 'quiet': True, 'no_warnings': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(full_url, download=False)
                subtitles = info.get('subtitles') or info.get('automatic_captions')
                if subtitles:
                    lang_key = next(iter(subtitles))
                    sub_data = subtitles[lang_key]
                    json_sub = [s for s in sub_data if s.get('ext') in ['json3', 'srv1', 'vtt']]
                    if json_sub:
                        res = requests.get(json_sub[0]['url'], timeout=10)
                        if res.status_code == 200:
                            clean_text = parse_youtube_subtitle_text(res.text)
                            if len(clean_text) > 30:
                                return True, clean_text, lang_key
        except Exception:
            pass
    return False, "后端抓取受限", "zh"

def detect_language(text):
    if not text or len(text.strip()) == 0:
        return 'zh'
    if re.search(r'[\u4e00-\u9fa5]', text):
        return 'zh'
    elif re.search(r'[\u3040-\u30ff]', text):
        return 'ja'
    elif re.search(r'[\uac00-\ud7af]', text):
        return 'ko'
    else:
        return 'en'

def run_async_safe(coroutine):
    try:
        return asyncio.run(coroutine)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.close()

# --------------------------------------------------
# 4. 🧠 广播剧剧本拆分器 (启发式算法全面增强)
# --------------------------------------------------
def parse_multi_role_script(text):
    male_keywords = ["他", "男", "先生", "少爷", "爸爸", "父亲", "爷爷", "哥", "叔", "师父", "队长", "老者", "皇上", "兄", "师兄", "老道", "少年", "汉子", "小哥", "老头", "王", "爷", "老兄", "叔叔", "叔父"]
    female_keywords = ["她", "女", "小姐", "夫人", "妈妈", "母亲", "奶奶", "姐", "妹", "姨", "师姐", "丫头", "皇后", "娘", "姑娘", "少女", "大娘", "阿姨", "嫂", "妹妹", "姐姐", "媳妇", "婆婆"]

    pattern = r'(“.*?”|"[^"]*"|「.*?」|『.*?』)'
    raw_segments = re.split(pattern, text)
    
    parsed_script = []
    last_context = ""
    dialogue_counter = 0

    for seg in raw_segments:
        s = seg.strip()
        if not s:
            continue
            
        is_quote = (
            (s.startswith("“") and s.endswith("”")) or 
            (s.startswith('"') and s.endswith('"')) or
            (s.startswith("「") and s.endswith("」")) or
            (s.startswith("『") and s.endswith("』"))
        )

        if is_quote:
            dialogue_text = s[1:-1].strip()
            if not dialogue_text:
                continue
                
            speaker_ctx = last_context[-25:]
            is_male = any(kw in speaker_ctx for kw in male_keywords)
            is_female = any(kw in speaker_ctx for kw in female_keywords)
            
            if is_male and not is_female:
                role = "MALE"
            elif is_female and not is_male:
                role = "FEMALE"
            else:
                role = "MALE" if (dialogue_counter % 2 == 0) else "FEMALE"
                dialogue_counter += 1
                
            parsed_script.append((role, dialogue_text))
            last_context = ""
        else:
            parsed_script.append(("NARRATOR", s))
            last_context = s

    return parsed_script

# --------------------------------------------------
# 5. 音色选择配置
# --------------------------------------------------
@st.cache_resource
def fetch_all_global_voices():
    try:
        voices = run_async_safe(edge_tts.list_voices())
        voice_dict = {}
        for v in voices:
            short_name = v.get("ShortName", "")
            locale = v.get("Locale", "")
            gender = "👩" if v.get("Gender") == "Female" else "👨"
            flag = LOCALE_FLAGS.get(locale, "🌍")
            lang_str = LOCALE_LANG_MAP.get(locale, (locale, locale))[0]
            name_parts = short_name.split("-")
            voice_name = name_parts[-1].replace("Neural", "") if len(name_parts) >= 3 else short_name
            voice_dict[short_name] = f"{flag} [{lang_str}] {voice_name} ({gender})"
        return voice_dict
    except Exception:
        return CURATED_VOICES

active_voice_dict = fetch_all_global_voices() if show_all_voices else CURATED_VOICES
voice_keys = list(active_voice_dict.keys())

st.markdown("##### 🎛️ 音质属性与朗读音色配置")

default_narrator = "zh-CN-YunjianNeural" if "zh-CN-YunjianNeural" in voice_keys else voice_keys[0]
default_male = "zh-CN-YunxiNeural" if "zh-CN-YunxiNeural" in voice_keys else voice_keys[0]
default_female = "zh-CN-XiaoxiaoNeural" if "zh-CN-XiaoxiaoNeural" in voice_keys else voice_keys[0]

if enable_multi_role:
    st.info("🎭 当前已启用 **全自动 AI 广播剧** 模式（对白与旁白分角色朗读）")
    r_col1, r_col2, r_col3 = st.columns(3)
    with r_col1:
        voice_narrator = st.selectbox("📖 旁白音色：", options=voice_keys, index=voice_keys.index(default_narrator) if default_narrator in voice_keys else 0, format_func=lambda x: active_voice_dict.get(x, x))
    with r_col2:
        voice_male = st.selectbox("👨 男主/男声对白：", options=voice_keys, index=voice_keys.index(default_male) if default_male in voice_keys else 0, format_func=lambda x: active_voice_dict.get(x, x))
    with r_col3:
        voice_female = st.selectbox("👩 女主/女声对白：", options=voice_keys, index=voice_keys.index(default_female) if default_female in voice_keys else 0, format_func=lambda x: active_voice_dict.get(x, x))
    voice_option = voice_narrator
else:
    st.info("🎙️ 当前已启用 **单人沉浸朗读** 模式（全篇文章使用单一专业音色）")
    speech_col1, speech_col2 = st.columns([2, 1])
    with speech_col1:
        voice_option = st.selectbox("🎙️ 选择贯穿全文的朗读音色：", options=voice_keys, index=voice_keys.index(default_narrator) if default_narrator in voice_keys else 0, format_func=lambda x: active_voice_dict.get(x, x))
    voice_narrator, voice_male, voice_female = voice_option, voice_option, voice_option

speech_rate_val = st.slider("⚡ 播放语速：", min_value=0.5, max_value=2.0, value=1.0, step=0.1, format="%.1fx")
rate_percentage = int(round((speech_rate_val - 1.0) * 100))
rate_str = f"{rate_percentage:+d}%"

# --------------------------------------------------
# 6. 输入界面
# --------------------------------------------------
st.subheader("📥 导入阅读内容")
input_mode = st.radio(
    "选择输入方式：",
    ["✍️ 粘贴纯文本或单页网址(URL)", "📚 智能分章节整本听书 (目录网址)", "🌐 粘贴 YouTube 视频链接", "📁 上传文件 (.txt / .pdf / .docx / .epub)"],
    horizontal=True,
)

raw_text = ""
detected_lang_code = "zh"

if input_mode == "✍️ 粘贴纯文本或单页网址(URL)":
    user_input = st.text_area(
        "粘贴文本或网页网址（以 http/https 开头）：",
        height=160,
        placeholder="粘贴文章纯文本...",
    )
    if user_input.strip():
        text_candidate = user_input.strip()
        if text_candidate.startswith("http://") or text_candidate.startswith("https://"):
            with st.spinner("🔗 正在通过智能解析引擎提取正文..."):
                try:
                    fetched = fetch_text_from_url(text_candidate)
                    if len(fetched) > 50 and not fetched.startswith("http"):
                        raw_text = clean_extracted_text(fetched)
                        st.success(f"🎉 网页解析成功！共提取到 {len(raw_text)} 个字符。")
                    else:
                        raw_text = clean_extracted_text(user_input)
                except Exception as e:
                    raw_text = clean_extracted_text(user_input)
        else:
            raw_text = clean_extracted_text(user_input)
        detected_lang_code = detect_language(raw_text)

elif input_mode == "📚 智能分章节整本听书 (目录网址)":
    st.caption("🤖 AI 听书助理模式：输入整本书或小说的目录页网址，自动切章节并支持连续播放下一章！")
    if "book_chapters" not in st.session_state:
        st.session_state.book_chapters = []
    if "current_chapter_idx" not in st.session_state:
        st.session_state.current_chapter_idx = 0

    catalog_url = st.text_input("请输入书籍目录页网址：", placeholder="https://www.example.com/book/12345/")
    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("📚 解析全书目录", use_container_width=True):
            if catalog_url.strip():
                with st.spinner("正在解析全书章节目录，请稍候..."):
                    try:
                        chapters = parse_book_catalog(catalog_url.strip())
                        if chapters:
                            st.session_state.book_chapters = chapters
                            st.session_state.current_chapter_idx = 0
                            st.success(f"🎉 目录解析成功！共发现 {len(chapters)} 个有效章节。")
                        else:
                            st.warning("未能在该网址中自动提取到有效的文字章节目录。")
                    except Exception as e:
                        st.error(f"{e}")
            else:
                st.warning("请输入有效的目录网址！")
    with col_btn2:
        if st.button("🗑️ 清空目录重置", use_container_width=True):
            st.session_state.book_chapters = []
            st.session_state.current_chapter_idx = 0
            st.rerun()

    if st.session_state.book_chapters:
        chapters = st.session_state.book_chapters
        idx = st.session_state.current_chapter_idx
        total = len(chapters)
        st.markdown("---")
        st.markdown(f"📖 **当前导读进度**：第 **{idx + 1}** 章 / 共 **{total}** 章")
        col_prev, col_info, col_next = st.columns([1, 2, 1])
        with col_prev:
            if st.button("◀️ 上一章", use_container_width=True, disabled=(idx <= 0)):
                st.session_state.current_chapter_idx -= 1
                st.session_state.full_audio_bytes = None
                st.rerun()
        with col_info:
            st.markdown(f"<div style='text-align:center; font-weight:bold; color:#38bdf8; margin-top:5px;'>{chapters[idx]['title']}</div>", unsafe_allow_html=True)
        with col_next:
            if st.button("▶️ 下一章", use_container_width=True, disabled=(idx >= total - 1)):
                st.session_state.current_chapter_idx += 1
                st.session_state.full_audio_bytes = None
                st.rerun()
        current_ch_url = chapters[idx]['url']
        with st.spinner(f"正在加载【{chapters[idx]['title']}】正文内容..."):
            try:
                ch_fetched = fetch_text_from_url(current_ch_url)
                raw_text = clean_extracted_text(ch_fetched)
                st.info(f"✅ 本章加载成功，共 {len(raw_text)} 个字符。")
                detected_lang_code = detect_language(raw_text)
            except Exception as e:
                raw_text = f"加载章节正文出错: {e}"
                st.error(raw_text)

elif input_mode == "🌐 粘贴 YouTube 视频链接":
    yt_url = st.text_input("请输入 YouTube 视频网址：", placeholder="https://www.youtube.com/watch?v=...")
    if yt_url.strip():
        video_id = extract_youtube_id(yt_url.strip())
        if video_id:
            with st.spinner("🤖 正在自动提取字幕..."):
                success, yt_text, lang_code = fetch_youtube_transcript_backend(video_id, yt_url.strip())
            if success:
                raw_text = clean_extracted_text(yt_text)
                detected_lang_code = detect_language(raw_text)
                st.success(f"🎉 字幕抓取成功！共提取到 {len(raw_text)} 个字符。")
                st.text_area("📹 提取的字幕文本预览", raw_text, height=140)
            else:
                st.warning(f"⚠️ {yt_text}")

else:
    uploaded_file = st.file_uploader("支持上传 .txt / .pdf / .docx / .epub 电子书文件", type=["txt", "pdf", "docx", "epub"])
    if uploaded_file is not None:
        filename = uploaded_file.name.lower()
        extracted_raw = ""
        if filename.endswith(".txt"):
            extracted_raw = uploaded_file.read().decode("utf-8", errors="ignore")
        elif filename.endswith(".pdf"):
            pdf_reader = PdfReader(uploaded_file)
            extracted_pages = [p.extract_text() for p in pdf_reader.pages if p.extract_text()]
            extracted_raw = "\n".join(extracted_pages)
        elif filename.endswith(".docx") and HAS_DOCX:
            doc = docx.Document(uploaded_file)
            extracted_raw = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        elif filename.endswith(".epub") and HAS_EPUB:
            book = epub.read_epub(io.BytesIO(uploaded_file.read()))
            texts = [BeautifulSoup(item.get_content(), 'html.parser').get_text() for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]
            extracted_raw = "\n".join(texts)
        raw_text = clean_extracted_text(extracted_raw)
        if len(raw_text.strip()) > 0:
            st.success(f"🎉 成功导入并深度清洗文件，共提取到 {len(raw_text)} 个有效字符！")
            detected_lang_code = detect_language(raw_text)
            with st.expander("📄 查看 / 编辑提取出的纯净文本", expanded=False):
                raw_text = st.text_area("文本预览：", raw_text, height=180)

active_process_text = raw_text
if len(raw_text) > 8000:
    st.info("💡 检测到超长文件/图书，已为你自动激活【章节智能切片器】！")
    auto_chapters = split_text_into_chapters(raw_text)
    chapter_names = [c["title"] for c in auto_chapters]
    selected_ch_idx = st.selectbox("📌 选择当前要合成或提炼的章节：", range(len(chapter_names)), format_func=lambda i: chapter_names[i])
    active_process_text = auto_chapters[selected_ch_idx]["content"]

# --------------------------------------------------
# 7. TTS 合成 + 高通滤波 + 动态闪避混音引擎
# --------------------------------------------------
def clean_markdown_for_speech(text):
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"^[•\-\*]\s*", "", text, flags=re.MULTILINE)
    emoji_pattern = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF]+", flags=re.UNICODE)
    return emoji_pattern.sub("", text).strip()

def split_text_chunks_safe(text, max_chunk_size=800):
    raw_sentences = re.split(r'([。！!?？\n])', text)
    chunks, current_chunk = [], ""
    i = 0
    while i < len(raw_sentences):
        segment = raw_sentences[i]
        if i + 1 < len(raw_sentences) and raw_sentences[i+1] in "。！!?？\n":
            segment += raw_sentences[i+1]
            i += 2
        else:
            i += 1
        if not segment.strip():
            continue
        if len(current_chunk) + len(segment) <= max_chunk_size:
            current_chunk += segment
        else:
            if current_chunk.strip():
                chunks.append(current_chunk.strip())
            current_chunk = segment
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    return chunks if chunks else [text]

async def synth_single_chunk_cached(chunk, voice, rate_str, sem=None):
    chunk_hash = hashlib.md5(f"{chunk}_{voice}_{rate_str}".encode('utf-8')).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{chunk_hash}.mp3")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                return f.read()
        except Exception:
            pass
            
    async def do_synth():
        audio_data = bytearray()
        try:
            communicate = edge_tts.Communicate(chunk, voice, rate=rate_str)
            async for item in communicate.stream():
                if item["type"] == "audio":
                    audio_data.extend(item["data"])
        except Exception:
            pass
        if len(audio_data) == 0:
            fallback = "zh-CN-XiaoxiaoNeural" if re.search(r'[\u4e00-\u9fa5]', chunk) else "en-US-JennyNeural"
            try:
                communicate = edge_tts.Communicate(chunk, fallback, rate=rate_str)
                async for item in communicate.stream():
                    if item["type"] == "audio":
                        audio_data.extend(item["data"])
            except Exception:
                pass
        res_bytes = bytes(audio_data)
        if len(res_bytes) > 0:
            try:
                cleanup_cache_if_needed(200)
                with open(cache_file, "wb") as f:
                    f.write(res_bytes)
            except Exception:
                pass
        return res_bytes

    if sem:
        async with sem:
            return await do_synth()
    else:
        return await do_synth()

def mix_bgm_with_audio(speech_bytes, bgm_choice_name, volume_percent=15):
    if not HAS_PYDUB or not FFMPEG_READY or not speech_bytes:
        return speech_bytes

    try:
        speech = AudioSegment.from_file(io.BytesIO(speech_bytes), format="mp3")
        
        try:
            speech = speech.high_pass_filter(80)
        except Exception:
            pass

        speech = speech.apply_gain(+3.0)
        speech_duration = len(speech)

        is_custom_mp3 = bgm_choice_name != "🎹 系统默认柔频和弦"
        bgm_path = os.path.join(BGM_DIR, bgm_choice_name) if is_custom_mp3 else ""

        if is_custom_mp3 and os.path.exists(bgm_path):
            bgm = AudioSegment.from_file(bgm_path, format="mp3")
        else:
            from pydub.generators import Sine
            tone1 = Sine(261.63).to_audio_segment(duration=max(speech_duration + 2000, 5000)) - 35
            tone2 = Sine(329.63).to_audio_segment(duration=max(speech_duration + 2000, 5000)) - 35
            tone3 = Sine(392.00).to_audio_segment(duration=max(speech_duration + 2000, 5000)) - 35
            bgm = tone1.overlay(tone2).overlay(tone3)

        bgm = bgm.set_frame_rate(speech.frame_rate).set_channels(speech.channels)

        if len(bgm) < speech_duration:
            loops_needed = (speech_duration // len(bgm)) + 1
            bgm = bgm * loops_needed

        bgm = bgm[:speech_duration].fade_in(1000).fade_out(1000)

        bgm_base_gain = -38.0 + (volume_percent * 0.4)
        chunk_ms = 250
        ducked_chunks = []
        current_duck = 0.0

        for i in range(0, speech_duration, chunk_ms):
            speech_chunk = speech[i:i+chunk_ms]
            bgm_chunk = bgm[i:i+chunk_ms]

            chunk_db = speech_chunk.dBFS
            target_duck = -6.0 if (chunk_db is not None and chunk_db > -42.0) else 0.0
            current_duck = current_duck * 0.7 + target_duck * 0.3

            adjusted_chunk = bgm_chunk.apply_gain(bgm_base_gain + current_duck)
            ducked_chunks.append(adjusted_chunk)

        if ducked_chunks:
            ducked_bgm = ducked_chunks[0]
            for c in ducked_chunks[1:]:
                ducked_bgm = ducked_bgm.append(c, crossfade=15)
        else:
            ducked_bgm = bgm.apply_gain(bgm_base_gain)

        ducked_bgm = ducked_bgm[:speech_duration]
        mixed = speech.overlay(ducked_bgm)
        output_io = io.BytesIO()
        mixed.export(output_io, format="mp3")
        
        st.success("🎵 极致混音完成：已注入 80Hz 高通滤波与平滑动态闪避 (Audio Ducking)！")
        return output_io.getvalue()
    except Exception as e:
        st.error(f"❌ 混音崩溃报错详情: {e}")
        return speech_bytes

async def generate_radio_drama_or_standard_audio(text, v_narrator, v_male, v_female, rate_str, use_multi_role=True, max_concurrency=10, apply_bgm=False, bgm_name="🎹 系统默认柔频和弦", vol_pct=15):
    clean_text = clean_markdown_for_speech(text)
    if not clean_text.strip():
        return b""

    sem = asyncio.Semaphore(max_concurrency)

    if use_multi_role:
        script_segments = parse_multi_role_script(clean_text)
        progress_bar = st.progress(0, text=f"🎭 正在并发合成多角色广播剧 (共 {len(script_segments)} 段对白/旁白)...")
        role_voice_map = {"NARRATOR": v_narrator, "MALE": v_male, "FEMALE": v_female}
        
        async def synth_segment_task(idx, role, seg_text):
            v = role_voice_map.get(role, v_narrator)
            seg_bytes = await synth_single_chunk_cached(seg_text, v, rate_str, sem)
            return idx, role, seg_bytes

        tasks = [synth_segment_task(i, r, t) for i, (r, t) in enumerate(script_segments)]
        results = [None] * len(script_segments)
        completed = 0

        for f in asyncio.as_completed(tasks):
            idx, role, seg_bytes = await f
            results[idx] = (role, seg_bytes)
            completed += 1
            progress_bar.progress(completed / len(script_segments), text=f"🎭 广播剧合成进度 ({completed}/{len(script_segments)} 段)...")

        progress_bar.empty()
        
        audio_segments_pydub = []
        for role, seg_bytes in results:
            if seg_bytes:
                if HAS_PYDUB and FFMPEG_READY:
                    audio_seg = AudioSegment.from_file(io.BytesIO(seg_bytes), format="mp3")
                    pause = AudioSegment.silent(duration=250, frame_rate=audio_seg.frame_rate)
                    audio_segments_pydub.append(audio_seg + pause)
                else:
                    audio_segments_pydub.append(seg_bytes)

        if not audio_segments_pydub:
            return b""

        if HAS_PYDUB and FFMPEG_READY:
            combined_audio = audio_segments_pydub[0]
            for seg in audio_segments_pydub[1:]:
                combined_audio += seg
            out_io = io.BytesIO()
            combined_audio.export(out_io, format="mp3")
            final_bytes = out_io.getvalue()
        else:
            final_bytes = b"".join([b for b in audio_segments_pydub if isinstance(b, bytes)])
    else:
        chunks = split_text_chunks_safe(clean_text)
        progress_bar = st.progress(0, text=f"⚡ 正在启动 {max_concurrency} 线程并发合成单人音频...")
        
        async def worker(idx, chunk):
            data = await synth_single_chunk_cached(chunk, v_narrator, rate_str, sem)
            return idx, data

        tasks = [worker(i, c) for i, c in enumerate(chunks)]
        results = [None] * len(chunks)
        completed = 0
        
        for f in asyncio.as_completed(tasks):
            idx, data = await f
            results[idx] = data
            completed += 1
            progress_bar.progress(completed / len(chunks), text=f"⚡ 单人音频合成进度 ({completed}/{len(chunks)} 段)...")

        progress_bar.empty()
        full_audio = bytearray()
        for r in results:
            if r:
                full_audio.extend(r)
        final_bytes = bytes(full_audio)

    if apply_bgm:
        with st.spinner("🎵 正在注入背景音乐并执行动态闪避 (Ducking)..."):
            final_bytes = mix_bgm_with_audio(final_bytes, bgm_name, vol_pct)

    return final_bytes

# --------------------------------------------------
# 8. 金句卡片生成引擎与 Media Session + WakeLock 锁屏保活组件
# --------------------------------------------------
def render_custom_media_player(audio_bytes, title="完整文章听书 / 广播剧", artist="随身听书 & 思维助手"):
    b64_audio = base64.b64encode(audio_bytes).decode('utf-8')
    audio_data_url = f"data:audio/mp3;base64,{b64_audio}"
    
    current_chapter_title = title
    if "book_chapters" in st.session_state and st.session_state.book_chapters:
        idx = st.session_state.get("current_chapter_idx", 0)
        if idx < len(st.session_state.book_chapters):
            current_chapter_title = st.session_state.book_chapters[idx]['title']

    html_code = f"""
    <div style="width: 100%; text-align: center; margin: 5px 0;">
        <audio id="custom-audio-player" controls autoplay style="width: 100%; max-width: 650px; height: 48px; border-radius: 8px;">
            <source src="{audio_data_url}" type="audio/mp3">
            您的浏览器不支持 HTML5 音频播放。
        </audio>
    </div>
    <script>
        const audio = document.getElementById('custom-audio-player');
        
        // PWA 锁屏保活及 Media Session 控制
        let wakeLock = null;
        async function requestWakeLock() {{
            try {{
                if ('wakeLock' in navigator) {{
                    wakeLock = await navigator.wakeLock.request('screen');
                }}
            }} catch (err) {{}}
        }}

        if ('mediaSession' in navigator) {{
            navigator.mediaSession.metadata = new MediaMetadata({{
                title: {json.dumps(current_chapter_title)},
                artist: {json.dumps(artist)},
                album: "AI 听书全能版",
                artwork: [
                    {{ src: 'https://cdn-icons-png.flaticon.com/512/3039/3039387.png', sizes: '512x512', type: 'image/png' }}
                ]
            }});

            navigator.mediaSession.setActionHandler('play', () => audio.play());
            navigator.mediaSession.setActionHandler('pause', () => audio.pause());
            navigator.mediaSession.setActionHandler('seekbackward', (details) => {{
                audio.currentTime = Math.max(audio.currentTime - (details.seekOffset || 10), 0);
            }});
            navigator.mediaSession.setActionHandler('seekforward', (details) => {{
                audio.currentTime = Math.min(audio.currentTime + (details.seekOffset || 10), audio.duration);
            }});
        }}

        audio.addEventListener('play', () => {{
            requestWakeLock();
            if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
        }});
        audio.addEventListener('pause', () => {{
            if (wakeLock !== null) {{
                wakeLock.release().then(() => {{ wakeLock = null; }});
            }}
            if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
        }});
    </script>
    """
    st.components.v1.html(html_code, height=65)

@st.cache_resource
def get_chinese_font(font_size=20):
    paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc"
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, font_size)
            except Exception:
                continue
    return ImageFont.load_default()

def generate_quote_card(quote_text, bg_style="暖粉水彩", keywords=None):
    quote_len = len(quote_text)
    font_size, chars_per_line, line_height = (22, 20, 40) if quote_len <= 35 else (18, 24, 34)
    font_title, font_quote = get_chinese_font(20), get_chinese_font(font_size)
    font_footer, font_badge, font_big = get_chinese_font(13), get_chinese_font(13), get_chinese_font(70)

    lines, line = [], ""
    for char in quote_text:
        line += char
        if len(line) >= chars_per_line:
            lines.append(line)
            line = ""
    if line:
        lines.append(line)

    width, height = 750, max(480, 200 + len(lines) * line_height)
    styles = {
        "暖粉水彩": {"bg_top": (252, 231, 243), "bg_bot": (254, 249, 195), "card_bg": (255, 255, 255), "text": (88, 28, 135), "accent": (192, 38, 211), "quote_mark": (244, 114, 182), "badge_bg": (250, 232, 255), "badge_text": (168, 85, 247)},
        "莫兰迪绿": {"bg_top": (209, 250, 229), "bg_bot": (236, 253, 245), "card_bg": (255, 255, 255), "text": (6, 78, 59), "accent": (5, 150, 105), "quote_mark": (110, 231, 183), "badge_bg": (209, 250, 229), "badge_text": (4, 120, 87)},
        "天空云蓝": {"bg_top": (224, 242, 254), "bg_bot": (240, 249, 255), "card_bg": (255, 255, 255), "text": (12, 74, 110), "accent": (2, 132, 199), "quote_mark": (125, 211, 252), "badge_bg": (224, 242, 254), "badge_text": (3, 105, 161)},
        "复古奶茶": {"bg_top": (254, 243, 199), "bg_bot": (254, 252, 232), "card_bg": (255, 253, 248), "text": (120, 53, 15), "accent": (217, 119, 6), "quote_mark": (252, 211, 77), "badge_bg": (254, 243, 199), "badge_text": (180, 83, 9)}
    }
    s = styles.get(bg_style, styles["暖粉水彩"])

    img = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        r = int(s["bg_top"][0] + (s["bg_bot"][0] - s["bg_top"][0]) * (y / height))
        g = int(s["bg_top"][1] + (s["bg_bot"][1] - s["bg_top"][1]) * (y / height))
        b = int(s["bg_top"][2] + (s["bg_bot"][2] - s["bg_top"][2]) * (y / height))
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    margin = 35
    draw.rounded_rectangle([margin, margin, width - margin, height - margin], radius=24, fill=s["card_bg"])
    draw.text((margin + 30, margin + 40), "“", fill=s["quote_mark"], font=font_big)
    draw.text((width - margin - 80, height - margin - 110), "”", fill=s["quote_mark"], font=font_big)
    draw.text((margin + 45, margin + 35), "🌿 每日精华金句卡", fill=s["accent"], font=font_title)

    y_offset = margin + 95
    for l in lines:
        bbox = draw.textbbox((0, 0), l, font=font_quote)
        x_center = (width - (bbox[2] - bbox[0])) // 2
        draw.text((x_center, y_offset), l, fill=s["text"], font=font_quote)
        y_offset += line_height

    if keywords:
        x_badge = margin + 45
        y_badge = height - margin - 75
        for kw in keywords[:3]:
            tag_text = f"#{kw}"
            bbox = draw.textbbox((0, 0), tag_text, font=font_badge)
            w = bbox[2] - bbox[0] + 18
            draw.rounded_rectangle([x_badge, y_badge, x_badge + w, y_badge + 28], radius=10, fill=s["badge_bg"])
            draw.text((x_badge + 9, y_badge + 5), tag_text, fill=s["badge_text"], font=font_badge)
            x_badge += w + 12

    draw.line([(margin + 45, height - margin - 35), (width - margin - 45, height - margin - 35)], fill=(241, 245, 249), width=1)
    draw.text((margin + 45, height - margin - 28), "—— 随身听书 & 思维助手 · 深度领读", fill=(148, 163, 184), font=font_footer)

    img_byte_arr = io.BytesIO()
    img.convert("RGB").save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()

# --------------------------------------------------
# 9. 知识提炼引擎 (含前缀清洗与异常符号去除)
# --------------------------------------------------
def clean_sentence_prefix(sentence):
    cleaned = sentence.strip()
    cleaned = re.sub(r'^[”"“\'’`\s]+', '', cleaned)
    patterns = [r"^(?:[0-9一二三四五六七八九十]+[.\s、]|核心观点|观点|总结|总之|首先|其次|最后)[：:\s]*"]
    for p in patterns:
        cleaned = re.sub(p, "", cleaned)
    cleaned = re.sub(r'^[”"“\'’`\s]+', '', cleaned)
    return cleaned.strip()

def is_noise_or_heading(sentence):
    s = sentence.strip()
    if any(kw in s for kw in ["深度领读", "导读", "作者：", "来源："]):
        return True
    if len(s) < 15 or len(s) > 140:
        return True
    return False

def extract_with_ollama(text, model_name):
    url = "http://localhost:11434/api/generate"
    prompt = f"请对以下文本进行深度解析与领读归纳：\n📌 **一句话精髓**：\n🔑 **核心要点**：\n文本：\n{text[:4000]}"
    payload = {"model": model_name, "prompt": prompt, "stream": False}
    try:
        response = requests.post(url, json=payload, timeout=60)
        if response.status_code == 200:
            ai_output = response.json().get("response", "")
            top_quote = "把握事物的底层逻辑与核心原则。"
            quote_match = re.search(r"📌 \*\*一句话精髓\*\*：?\n?>?\s*(.*)", ai_output)
            if quote_match:
                top_quote = quote_match.group(1).split("\n")[0].strip()
            return ai_output, top_quote, ["知识提炼", "核心要领"]
        else:
            raise Exception(f"Ollama 错误代码: {response.status_code}")
    except Exception as e:
        raise Exception(f"Ollama 连接异常: {e}")

@st.cache_data(show_spinner=False, ttl=3600)
def extract_ultimate_local_insights(text):
    if not text.strip():
        return "", "", []
    char_count = len(text)
    read_minutes = round(char_count / 300, 1)

    has_chinese = bool(re.search(r'[\u4e00-\u9fa5]', text))
    if has_chinese:
        keywords = jieba.analyse.textrank(text, topK=6, withWeight=False, allowPOS=("n", "vn", "nz", "nr", "nt", "eng"))
    else:
        words = re.findall(r'\b[A-Za-z]{4,}\b', text)
        keywords = list(set(words))[:6]

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    candidates = []
    
    for p in paragraphs:
        for s in re.split(r"[。！!？\?.]", p):
            s_clean = s.strip()
            if is_noise_or_heading(s_clean):
                continue
            score = sum(4 for kw in keywords if kw in s_clean)
            if score >= 4:
                candidates.append((score, clean_sentence_prefix(s_clean)))

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_points = [c[1] for c in candidates[:3]]
    top_one_sentence = top_points[0] if top_points else "把握文本的核心逻辑与核心观点。"

    summary_md = f"📈 **文本体检**：全文共 **{char_count}** 字  |  ⏱️ 预估阅读约 **{read_minutes}** 分钟\n\n"
    summary_md += f"🏷️ **核心主题标签**： " + " ".join([f"`#{kw}`" for kw in keywords]) + "\n\n"
    summary_md += "🎯 **核心观点深度提炼**：\n"
    for i, pt in enumerate(top_points, 1):
        summary_md += f"**{i}.** {pt}。\n\n"

    return summary_md, top_one_sentence, keywords

# --------------------------------------------------
# 10. Session State 管理与操作区
# --------------------------------------------------
if "full_audio_bytes" not in st.session_state:
    st.session_state.full_audio_bytes = None
if "local_summary" not in st.session_state:
    st.session_state.local_summary = ""
if "top_quote" not in st.session_state:
    st.session_state.top_quote = ""
if "current_keywords" not in st.session_state:
    st.session_state.current_keywords = []
if "summary_audio_bytes" not in st.session_state:
    st.session_state.summary_audio_bytes = None

col1, col2 = st.columns(2)

with col1:
    btn_label = "🚀 生成全自动 AI 广播剧" if enable_multi_role else "🚀 生成单人沉浸听书音频"
    if st.button(btn_label, type="primary", use_container_width=True):
        if not active_process_text.strip():
            st.warning("请先加载章节或粘贴文本！")
        else:
            with st.spinner("正在并发合成高保真音频..."):
                try:
                    audio_bytes = run_async_safe(
                        generate_radio_drama_or_standard_audio(
                            active_process_text, 
                            v_narrator=voice_narrator,
                            v_male=voice_male,
                            v_female=voice_female,
                            rate_str=rate_str, 
                            use_multi_role=enable_multi_role,
                            max_concurrency=concurrency_limit,
                            apply_bgm=enable_bgm,
                            bgm_name=selected_bgm_name,
                            vol_pct=bgm_volume
                        )
                    )
                    st.session_state.full_audio_bytes = audio_bytes
                    st.success("🎉 音频合成/混音完成！")
                except Exception as e:
                    st.error(f"生成失败: {e}")

with col2:
    if st.button("⚡ 开始知识深度提炼", use_container_width=True):
        if not active_process_text.strip():
            st.warning("请先加载章节或粘贴文本！")
        else:
            if use_ollama:
                with st.spinner("🧠 正在调用本地 Ollama (Qwen) AI 大模型思考中..."):
                    try:
                        summary_res, top_quote, kws = extract_with_ollama(active_process_text, ollama_model)
                        st.session_state.local_summary = summary_res
                        st.session_state.top_quote = top_quote
                        st.session_state.current_keywords = kws
                        st.success("🎉 本地 Ollama (Qwen) 大模型提炼完成！")
                    except Exception as e:
                        st.error(f"Ollama 连接失败({e})，已自动无缝切回自适应算法！")
                        summary_res, top_quote, kws = extract_ultimate_local_insights(active_process_text)
                        st.session_state.local_summary = summary_res
                        st.session_state.top_quote = top_quote
                        st.session_state.current_keywords = kws
            else:
                with st.spinner("⚡ 正在通过智能增强引擎深度提炼中..."):
                    summary_res, top_quote, kws = extract_ultimate_local_insights(active_process_text)
                    st.session_state.local_summary = summary_res
                    st.session_state.top_quote = top_quote
                    st.session_state.current_keywords = kws
                    st.success("🎉 深度提炼完成！")

# --------------------------------------------------
# 11. 结果展示区 (集成 Media Session 与 完美文本导出)
# --------------------------------------------------
if st.session_state.full_audio_bytes:
    st.divider()
    st.subheader("🎧 听书 / 广播剧音频播放器")
    render_custom_media_player(st.session_state.full_audio_bytes, title="听书 / 广播剧", artist="随身听书 & 思维助手")
    st.download_button(
        "📥 下载完整 MP3",
        data=st.session_state.full_audio_bytes,
        file_name="audiobook_chapter.mp3",
        mime="audio/mp3",
        use_container_width=True,
    )

if st.session_state.local_summary:
    st.divider()
    st.subheader("💡 深度领读分析报告")

    if st.session_state.top_quote:
        st.info(f"📌 **一句话精髓**\n\n“ {st.session_state.top_quote} ”")

    st.markdown(st.session_state.local_summary)

    if st.session_state.top_quote:
        with st.expander("🖼️ 查看 / 保存自动生成的【每日精华金句卡】", expanded=True):
            edited_quote = st.text_area(
                "✏️ 编辑卡片金句文字（可自由修改润色）：",
                value=st.session_state.top_quote,
                height=70
            )
            card_style = st.selectbox(
                "🎨 选择卡片背景风格：",
                ["暖粉水彩", "莫兰迪绿", "天空云蓝", "复古奶茶"]
            )
            card_bytes = generate_quote_card(
                edited_quote, 
                bg_style=card_style,
                keywords=st.session_state.current_keywords
            )
            st.image(card_bytes, use_container_width=True)
            st.download_button(
                label=f"📥 保存【{card_style}】每日精华金句卡 (.png)",
                data=card_bytes,
                file_name=f"essential_quote_card_{card_style}.png",
                mime="image/png",
                use_container_width=True,
            )

    st.divider()
    sub_col1, sub_col2 = st.columns(2)

    with sub_col1:
        if st.button("🎙️ 将总结转为速读音频", use_container_width=True):
            with st.spinner("正在生成总结音频..."):
                try:
                    summary_bytes = run_async_safe(
                        generate_radio_drama_or_standard_audio(
                            st.session_state.local_summary, 
                            v_narrator=voice_option,
                            v_male=voice_option,
                            v_female=voice_option,
                            rate_str=rate_str, 
                            use_multi_role=False,
                            max_concurrency=concurrency_limit,
                            apply_bgm=enable_bgm,
                            bgm_name=selected_bgm_name,
                            vol_pct=bgm_volume
                        )
                    )
                    st.session_state.summary_audio_bytes = summary_bytes
                    st.success("🎉 总结音频生成成功！")
                except Exception as e:
                    st.error(f"生成失败: {e}")

    with sub_col2:
        export_text = ""
        if st.session_state.top_quote:
            export_text += f"📌 一句话精髓：\n“ {st.session_state.top_quote} ”\n\n"
        export_text += st.session_state.local_summary

        st.download_button(
            label="💾 保存每日笔记 (.txt)",
            data=export_text,
            file_name="daily_knowledge_note.txt",
            mime="text/plain",
            use_container_width=True,
        )

if st.session_state.summary_audio_bytes:
    render_custom_media_player(st.session_state.summary_audio_bytes, title="速读总结音频", artist="随身听书 & 思维助手")
    st.download_button(
        "📥 下载总结速读 MP3",
        data=st.session_state.summary_audio_bytes,
        file_name="summary_audio.mp3",
        mime="audio/mp3",
        use_container_width=True,
    )
