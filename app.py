import asyncio
import io
import json
import os
import re
import hashlib
import subprocess
from urllib.parse import urljoin, urlparse
import jieba
import jieba.analyse
import requests
import streamlit as st
import edge_tts
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

# 尝试导入 pydub 进行影音级 BGM 混音处理
try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError:
    HAS_PYDUB = False

# --------------------------------------------------
# 0. MD5 磁盘缓存与 BGM 根目录初始化
# --------------------------------------------------
CACHE_DIR = ".audio_cache"
BGM_DIR = "."  # 指向根目录，方便手机直接上传 gentle_bgm.mp3
os.makedirs(CACHE_DIR, exist_ok=True)

# 尝试导入 python-docx 与 ebooklib (扩展电子书支持)
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

# 尝试导入 youtube_transcript_api 与 yt_dlp
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

# 尝试导入 trafilatura（网页正文智能提取库）
try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

# --------------------------------------------------
# 1. 页面基本配置
# --------------------------------------------------
st.set_page_config(
    page_title="随身听书 & 思维助手 (全能旗舰版)", page_icon="🎧", layout="centered"
)

st.title("🎧 随身听书 & 思维助手 (Global Ultimate Edition)")
st.caption(
    "全能旗舰版：全源深度清洗引擎 + 300+ 中英双语 AI 音色 + 影音级 BGM 混音 + 倍速调节"
)

# --------------------------------------------------
# 2. 侧边栏：Ollama (Qwen) & 影音 BGM 引擎设置
# --------------------------------------------------
with st.sidebar:
    st.header("⚙️ 引擎与影音设置")
    use_ollama = st.checkbox(
        "🧠 启用 Ollama (Qwen) 本地大模型",
        value=False,
        help="未安装 Ollama 请勿勾选。换新电脑安装 Ollama 后勾选即可开启离线大模型提炼；若连接失败会自动无缝切回自适应算法。",
    )
    ollama_model = st.text_input(
        "Ollama 模型名称:",
        value="qwen2.5:1.5b",
        help="需先在终端运行过: ollama run qwen2.5:1.5b",
    )
    st.divider()
    
    # 🎵 广播剧级 BGM 混音设置
    enable_bgm = st.checkbox(
        "🎵 开启 BGM 沉浸式背景音乐混音",
        value=False,
        help="开启后将为你生成的听书音频自动叠加轻柔背景音乐，打造广播剧级的有声体验！"
    )
    bgm_volume = st.slider("🎚️ BGM 音量比例:", min_value=5, max_value=40, value=15, format="%d%%")
    
    st.divider()
    concurrency_limit = st.slider(
        "⚡ TTS 并发线程数",
        min_value=4,
        max_value=16,
        value=10,
        help="推荐 10 线程并发合成，速度提升 5-8 倍"
    )

# --------------------------------------------------
# 3. 核心：全源通用深度智能清洗引擎 & 伪链接过滤器
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
# 4. 多功能输入层
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

# 大文件超长保护
active_process_text = raw_text
if len(raw_text) > 8000:
    st.info("💡 检测到超长文件/图书，已为你自动激活【章节智能切片器】！")
    auto_chapters = split_text_into_chapters(raw_text)
    chapter_names = [c["title"] for c in auto_chapters]
    selected_ch_idx = st.selectbox("📌 选择当前要合成或提炼的章节：", range(len(chapter_names)), format_func=lambda i: chapter_names[i])
    active_process_text = auto_chapters[selected_ch_idx]["content"]

# --------------------------------------------------
# 5. 音色与倍速设置
# --------------------------------------------------
LOCALE_LANG_MAP = {'zh-CN': ('中文普通话', 'Mandarin'), 'en-US': ('美式英语', 'US English'), 'ja-JP': ('日语', 'Japanese')}
LOCALE_FLAGS = {'zh-CN': '🇨🇳', 'en-US': '🇺🇸', 'ja-JP': '🇯🇵'}

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
            display = f"{flag} [{lang_str}] {voice_name} ({gender})"
            voice_dict[short_name] = display
        return voice_dict
    except Exception:
        return {"zh-CN-XiaoxiaoNeural": "🇨🇳 [中文普通话] Xiaoxiao (👩)", "zh-CN-YunxiNeural": "🎙️ [中文普通话] Yunxi (👨)", "en-US-AvaMultilingualNeural": "🌐 [美式英语] AvaMultilingual (👩)"}

GLOBAL_VOICES = fetch_all_global_voices()
voice_keys = list(GLOBAL_VOICES.keys())

st.markdown("##### 🎛️ 音频播放属性设置")
speech_col1, speech_col2 = st.columns([2, 1])
with speech_col1:
    voice_option = st.selectbox("选择朗读音色：", options=voice_keys, index=0, format_func=lambda x: GLOBAL_VOICES.get(x, x))
with speech_col2:
    speech_rate_val = st.slider("⚡ 播放语速：", min_value=0.5, max_value=2.0, value=1.0, step=0.1, format="%.1fx")

rate_percentage = int(round((speech_rate_val - 1.0) * 100))
rate_str = f"{rate_percentage:+d}%"

# --------------------------------------------------
# 6. TTS 合成 + MD5 缓存 + 修复版 BGM 混音引擎
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

async def synth_single_chunk_cached(chunk, voice, rate_str, sem):
    chunk_hash = hashlib.md5(f"{chunk}_{voice}_{rate_str}".encode('utf-8')).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{chunk_hash}.mp3")
    
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                return f.read()
        except Exception:
            pass

    async with sem:
        audio_data = bytearray()
        try:
            communicate = edge_tts.Communicate(chunk, voice, rate=rate_str)
            async for item in communicate.stream():
                if item["type"] == "audio":
                    audio_data.extend(item["data"])
        except Exception:
            pass
            
        if len(audio_data) == 0:
            fallback = "zh-CN-XiaoxiaoNeural" if re.search(r'[\u4e00-\u9fa5]', chunk) else "en-US-AvaMultilingualNeural"
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
                with open(cache_file, "wb") as f:
                    f.write(res_bytes)
            except Exception:
                pass
        return res_bytes

def mix_bgm_with_audio(speech_bytes, volume_percent=15):
    """广播剧级 BGM 混音器（修复版：自动对齐采样率与声道，确保混音绝对生效）"""
    if not HAS_PYDUB or not speech_bytes:
        return speech_bytes

    try:
        speech = AudioSegment.from_file(io.BytesIO(speech_bytes), format="mp3")
        speech_duration = len(speech)

        bgm_path = os.path.join(BGM_DIR, "gentle_bgm.mp3")
        if not os.path.exists(bgm_path):
            # 生成丰满的 C大调三音和弦（根音261Hz、三音329Hz、五音392Hz），辨识度极高
            from pydub.generators import Sine
            tone1 = Sine(261.63).to_audio_segment(duration=speech_duration + 2000)
            tone2 = Sine(329.63).to_audio_segment(duration=speech_duration + 2000)
            tone3 = Sine(392.00).to_audio_segment(duration=speech_duration + 2000)
            bgm = tone1.overlay(tone2).overlay(tone3)
        else:
            bgm = AudioSegment.from_file(bgm_path, format="mp3")

        # 🔑 关键修复：强制对齐采样率和声道，防止 pydub 混音静默失效
        bgm = bgm.set_frame_rate(speech.frame_rate).set_channels(speech.channels)

        # 让 BGM 循环匹配人声长度
        if len(bgm) < speech_duration:
            loops_needed = (speech_duration // len(bgm)) + 1
            bgm = bgm * loops_needed

        bgm = bgm[:speech_duration].fade_in(1000).fade_out(1000)
        
        # 音量映射：当设置为 40% 时音量非常清晰
        volume_db = -30 + (volume_percent * 0.7)
        bgm = bgm + volume_db

        mixed = speech.overlay(bgm)
        output_io = io.BytesIO()
        mixed.export(output_io, format="mp3")
        
        st.toast("🎵 BGM 沉浸式背景音乐混音成功！", icon="🎧")
        return output_io.getvalue()
    except Exception as e:
        st.error(f"❌ BGM 混音异常: {e}")
        return speech_bytes

async def generate_audio_bytes_parallel(text, voice, rate_str="+0%", max_concurrency=10, apply_bgm=False, volume_pct=15):
    clean_text = clean_markdown_for_speech(text)
    chunks = split_text_chunks_safe(clean_text)
    if not chunks:
        return b""
        
    sem = asyncio.Semaphore(max_concurrency)
    progress_bar = st.progress(0, text=f"⚡ 正在启动 {max_concurrency} 线程并发合成...")
    
    async def worker(idx, chunk):
        data = await synth_single_chunk_cached(chunk, voice, rate_str, sem)
        return idx, data

    tasks = [worker(i, c) for i, c in enumerate(chunks)]
    results = [None] * len(chunks)
    completed = 0
    
    for f in asyncio.as_completed(tasks):
        idx, data = await f
        results[idx] = data
        completed += 1
        progress_bar.progress(completed / len(chunks), text=f"⚡ 合成进度 ({completed}/{len(chunks)} 段)...")

    progress_bar.empty()
    full_audio = bytearray()
    for r in results:
        if r:
            full_audio.extend(r)
            
    final_bytes = bytes(full_audio)
    if apply_bgm and HAS_PYDUB:
        with st.spinner("🎵 正在注入 BGM 沉浸式背景音乐..."):
            final_bytes = mix_bgm_with_audio(final_bytes, volume_pct)

    return final_bytes

# --------------------------------------------------
# 7. 字库与金句卡片
# --------------------------------------------------
@st.cache_resource
def get_chinese_font(font_size=20):
    paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
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
# 8. 知识提炼引擎
# --------------------------------------------------
def clean_sentence_prefix(sentence):
    cleaned = sentence.strip()
    patterns = [r"^(?:[0-9一二三四五六七八九十]+[.\s、]|核心观点|观点|总结|总之|首先|其次|最后)[：:\s]*"]
    for p in patterns:
        cleaned = re.sub(p, "", cleaned)
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
# 9. Session State 管理与操作区
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
    if st.button("🚀 生成完整音频 (10并发/BGM混音)", type="primary", use_container_width=True):
        if not active_process_text.strip():
            st.warning("请先加载章节或粘贴文本！")
        else:
            with st.spinner("正在并发合成高保真音频..."):
                try:
                    audio_bytes = run_async_safe(
                        generate_audio_bytes_parallel(
                            active_process_text, 
                            voice_option, 
                            rate_str, 
                            concurrency_limit,
                            apply_bgm=enable_bgm,
                            volume_pct=bgm_volume
                        )
                    )
                    st.session_state.full_audio_bytes = audio_bytes
                    st.success("🎉 完整音频合成/混音完成！")
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
# 10. 结果展示区
# --------------------------------------------------
if st.session_state.full_audio_bytes:
    st.divider()
    st.subheader("🎧 完整文章听书")
    st.audio(st.session_state.full_audio_bytes, format="audio/mp3")
    st.download_button(
        "📥 下载完整听书 MP3",
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
                        generate_audio_bytes_parallel(
                            st.session_state.local_summary, 
                            voice_option, 
                            rate_str, 
                            concurrency_limit,
                            apply_bgm=enable_bgm,
                            volume_pct=bgm_volume
                        )
                    )
                    st.session_state.summary_audio_bytes = summary_bytes
                    st.success("🎉 总结音频生成成功！")
                except Exception as e:
                    st.error(f"生成失败: {e}")

    with sub_col2:
        st.download_button(
            label="💾 保存每日笔记 (.txt)",
            data=st.session_state.local_summary,
            file_name="daily_knowledge_note.txt",
            mime="text/plain",
            use_container_width=True,
        )

if st.session_state.summary_audio_bytes:
    st.audio(st.session_state.summary_audio_bytes, format="audio/mp3")
    st.download_button(
        "📥 下载总结速读 MP3",
        data=st.session_state.summary_audio_bytes,
        file_name="summary_audio.mp3",
        mime="audio/mp3",
        use_container_width=True,
    )
