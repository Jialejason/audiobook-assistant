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

# --------------------------------------------------
# 0. 环境与依赖严格诊断 (检查 FFmpeg 是否真正安装成功)
# --------------------------------------------------
HAS_PYDUB = False
FFMPEG_READY = False

try:
    from pydub import AudioSegment
    HAS_PYDUB = True
except ImportError:
    pass

if HAS_PYDUB:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        FFMPEG_READY = True
    except Exception:
        FFMPEG_READY = False

CACHE_DIR = ".audio_cache"
BGM_DIR = "."
os.makedirs(CACHE_DIR, exist_ok=True)

# 尝试导入其他扩展库
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
# 🌟 严格筛选的各国顶级高保真音色库 (严选品质)
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

# --------------------------------------------------
# 1. 页面基本配置
# --------------------------------------------------
st.set_page_config(
    page_title="随身听书 & 思维助手 (AI广播剧全能版)", page_icon="🎧", layout="centered"
)

st.title("🎧 随身听书 & 思维助手 (AI Radio Drama Edition)")
st.caption(
    "全能旗舰版：全自动男女多角色对话 + 各国精选音色 + BGM自由切换与上传 + 知识提炼"
)

# --------------------------------------------------
# 2. 侧边栏：广播剧、音色与 BGM 音轨设置
# --------------------------------------------------
with st.sidebar:
    st.header("⚙️ 引擎与影音设置")
    
    # 🔍 状态诊断面板
    if HAS_PYDUB and FFMPEG_READY:
        st.success("✅ BGM / 多角色混音引擎就绪 (FFmpeg 正常)")
    else:
        st.error("❌ 混音受阻：云端未检测到 FFmpeg！")
        st.info("💡 请检查 packages.txt 文件里是否单独一行写了 `ffmpeg` 并在后台 Reboot App。")

    st.divider()
    
    # 🎭 广播剧多角色模式设置
    enable_multi_role = st.checkbox(
        "🎭 开启全自动 AI 广播剧 (男女多角色对话模式)",
        value=True,
        help="开启后系统将自动识别文中旁白、男主、女主对白，并自动分配不同音色交替朗读！"
    )
    
    st.divider()

    # 🎵 背景音乐管理与自由切换
    enable_bgm = st.checkbox(
        "🎵 开启 BGM 沉浸式背景音乐混音",
        value=False,
        help="开启后将为你生成的听书音频自动叠加背景音乐！"
    )
    
    # 扫描当前项目目录下所有的 MP3 背景音乐文件
    available_bgm_files = [f for f in os.listdir(BGM_DIR) if f.lower().endswith(".mp3") and not f.startswith(".")]
    bgm_options = ["🎹 系统默认柔和和弦"] + available_bgm_files
    
    selected_bgm_name = st.selectbox(
        "🎶 选择背景音乐曲目：",
        options=bgm_options,
        index=0,
        help="你可以从下拉菜单切换已上传的不同 MP3 背景音乐"
    )
    
    bgm_volume = st.slider("🎚️ BGM 音量比例:", min_value=5, max_value=50, value=30, format="%d%%")
    
    # 📤 手机直接上传 BGM
    with st.expander("📤 上传我的背景音乐 (.mp3)", expanded=False):
        uploaded_bgm = st.file_uploader("选择手机里的 MP3 文件上传：", type=["mp3"], key="bgm_uploader")
        if uploaded_bgm is not None:
            save_path = os.path.join(BGM_DIR, uploaded_bgm.name)
            with open(save_path, "wb") as f:
                f.write(uploaded_bgm.read())
            st.success(f"🎉 成功上传 【{uploaded_bgm.name}】！刷新页面即可选择。")

    st.divider()
    show_all_voices = st.checkbox("🌐 显示全球 300+ 完整音色列表 (默认只看精选)", value=False)
    
    use_ollama = st.checkbox("🧠 启用 Ollama (Qwen) 本地大模型", value=False)
    ollama_model = st.text_input("Ollama 模型名称:", value="qwen2.5:1.5b")

    concurrency_limit = st.slider("⚡ TTS 并发线程数", min_value=4, max_value=16, value=10)

# --------------------------------------------------
# 3. 核心：全源通用深度智能清洗引擎
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
        r'^\s*\d+(\s+\d+)*\s*$', r'^\s*-\s*\d+\s*-\s*$',
        r'^\s*第\s*\d+\s*[页頁]\s*$', r'^\s*Page\s*\d+\s*$',
        r'^[A-Z0-9_\-]+/\d+.*$', r'^\s*\d+\s*/\s*\d+\s*$',
    ]
    for line in lines:
        l = line.strip()
        if not l or l in noise_symbols:
            continue
        is_noise = any(re.match(pattern, l, re.IGNORECASE) for pattern in page_patterns)
        if is_noise or any(kw in l for kw in noise_keywords):
            continue
        cleaned_lines.append(l)
    full_text = "\n".join(cleaned_lines)
    return re.sub(r'([^。！？!？…\n])\n([^。！？!？…\n])', r'\1\2', full_text).strip()

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
    return chapters if chapters else [{"title": "全文内容", "content": full_text}]

# --------------------------------------------------
# 4. 🧠 全自动广播剧剧本拆分解析器 (男女角色自动分配)
# --------------------------------------------------
def parse_multi_role_script(text):
    """
    智能剧本拆分器：将任意文本切割为 (role, segment) 列表
    role 类型: "NARRATOR" (旁白), "MALE" (男声), "FEMALE" (女声)
    """
    male_keywords = ["他", "男", "先生", "少爷", "爸爸", "父亲", "爷爷", "哥", "叔", "师父", "队长", "老者", "皇上"]
    female_keywords = ["她", "女", "小姐", "夫人", "妈妈", "母亲", "奶奶", "姐", "妹", "姨", "师姐", "丫头", "皇后"]

    pattern = r'(“.*?”|"[^"]*")'
    raw_segments = re.split(pattern, text)
    
    parsed_script = []
    last_context = ""
    dialogue_counter = 0

    for seg in raw_segments:
        s = seg.strip()
        if not s:
            continue
            
        if (s.startswith("“") and s.endswith("”")) or (s.startswith('"') and s.endswith('"')):
            dialogue_text = s[1:-1].strip()
            if not dialogue_text:
                continue
                
            is_male = any(kw in last_context for kw in male_keywords)
            is_female = any(kw in last_context for kw in female_keywords)
            
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
            last_context = s[-20:]

    return parsed_script

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
# 5. 音色分配界面 (支持单音色 / 多角色独立配置)
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
            voice_dict[short_name] = f"🌐 [{locale}] {short_name} ({gender})"
        return voice_dict
    except Exception:
        return CURATED_VOICES

active_voice_dict = fetch_all_global_voices() if show_all_voices else CURATED_VOICES
voice_keys = list(active_voice_dict.keys())

st.markdown("##### 🎛️ 音频朗读音色与角色分配")

default_narrator = "zh-CN-YunjianNeural" if "zh-CN-YunjianNeural" in voice_keys else voice_keys[0]
default_male = "zh-CN-YunxiNeural" if "zh-CN-YunxiNeural" in voice_keys else voice_keys[0]
default_female = "zh-CN-XiaoxiaoNeural" if "zh-CN-XiaoxiaoNeural" in voice_keys else voice_keys[0]

if enable_multi_role:
    r_col1, r_col2, r_col3 = st.columns(3)
    with r_col1:
        voice_narrator = st.selectbox("📖 旁白音色：", options=voice_keys, index=voice_keys.index(default_narrator) if default_narrator in voice_keys else 0, format_func=lambda x: active_voice_dict.get(x, x))
    with r_col2:
        voice_male = st.selectbox("👨 男主/男声对白：", options=voice_keys, index=voice_keys.index(default_male) if default_male in voice_keys else 0, format_func=lambda x: active_voice_dict.get(x, x))
    with r_col3:
        voice_female = st.selectbox("👩 女主/女声对白：", options=voice_keys, index=voice_keys.index(default_female) if default_female in voice_keys else 0, format_func=lambda x: active_voice_dict.get(x, x))
else:
    voice_narrator = st.selectbox("选择统一朗读音色：", options=voice_keys, index=0, format_func=lambda x: active_voice_dict.get(x, x))
    voice_male, voice_female = voice_narrator, voice_narrator

speech_rate_val = st.slider("⚡ 播放语速：", min_value=0.5, max_value=2.0, value=1.0, step=0.1, format="%.1fx")
rate_str = f"{int(round((speech_rate_val - 1.0) * 100)):+d}%"

# --------------------------------------------------
# 6. 多功能输入层
# --------------------------------------------------
st.subheader("📥 导入阅读内容")
input_mode = st.radio("选择输入方式：", ["✍️ 粘贴纯文本", "📁 上传文件 (.txt / .pdf / .docx)"], horizontal=True)

raw_text = ""
if input_mode == "✍️ 粘贴纯文本":
    user_input = st.text_area("粘贴文章或小说文本：", height=160, placeholder="粘贴文本...系统会自动识别双引号“对白”进行男女声交替朗读！")
    raw_text = clean_extracted_text(user_input)
else:
    uploaded_file = st.file_uploader("支持上传文件", type=["txt", "pdf", "docx"])
    if uploaded_file:
        if uploaded_file.name.endswith(".txt"):
            raw_text = clean_extracted_text(uploaded_file.read().decode("utf-8", errors="ignore"))
        elif uploaded_file.name.endswith(".pdf"):
            reader = PdfReader(uploaded_file)
            raw_text = clean_extracted_text("\n".join([p.extract_text() for p in reader.pages if p.extract_text()]))

active_process_text = raw_text

# --------------------------------------------------
# 7. 🚀 多角色广播剧 TTS 合成与 BGM 混音引擎
# --------------------------------------------------
async def synth_single_chunk_cached(chunk_text, voice, rate_str):
    chunk_hash = hashlib.md5(f"{chunk_text}_{voice}_{rate_str}".encode('utf-8')).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{chunk_hash}.mp3")
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                return f.read()
        except Exception:
            pass

    audio_data = bytearray()
    try:
        communicate = edge_tts.Communicate(chunk_text, voice, rate=rate_str)
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

def mix_bgm_with_audio(speech_bytes, bgm_choice_name, volume_percent=30):
    if not HAS_PYDUB or not FFMPEG_READY or not speech_bytes:
        return speech_bytes

    try:
        speech = AudioSegment.from_file(io.BytesIO(speech_bytes), format="mp3")
        speech_duration = len(speech)

        is_custom_mp3 = bgm_choice_name != "🎹 系统默认柔和和弦"
        bgm_path = os.path.join(BGM_DIR, bgm_choice_name) if is_custom_mp3 else ""

        if is_custom_mp3 and os.path.exists(bgm_path):
            bgm = AudioSegment.from_file(bgm_path, format="mp3")
        else:
            from pydub.generators import Sine
            t1 = Sine(261.63).to_audio_segment(duration=speech_duration + 2000)
            t2 = Sine(329.63).to_audio_segment(duration=speech_duration + 2000)
            t3 = Sine(392.00).to_audio_segment(duration=speech_duration + 2000)
            bgm = t1.overlay(t2).overlay(t3)

        bgm = bgm.set_frame_rate(speech.frame_rate).set_channels(speech.channels)
        if len(bgm) < speech_duration:
            bgm = bgm * ((speech_duration // len(bgm)) + 1)

        bgm = bgm[:speech_duration].fade_in(800).fade_out(800)
        volume_db = -25 + (volume_percent * 0.6)
        mixed = speech.overlay(bgm + volume_db)

        output_io = io.BytesIO()
        mixed.export(output_io, format="mp3")
        st.success(f"🎵 BGM 混音成功：已为您叠加背景音乐 【{bgm_choice_name}】！")
        return output_io.getvalue()
    except Exception as e:
        st.error(f"❌ 混音报错: {e}")
        return speech_bytes

async def generate_radio_drama_audio(text, v_narrator, v_male, v_female, rate_str, use_multi_role=True, apply_bgm=False, bgm_name="🎹 系统默认柔和和弦", vol_pct=30):
    if not text.strip():
        return b""

    if use_multi_role:
        script_segments = parse_multi_role_script(text)
    else:
        script_segments = [("NARRATOR", text)]

    progress_bar = st.progress(0, text=f"🎭 正在合成多角色广播剧音频 (共 {len(script_segments)} 段对白/旁白)...")
    
    role_voice_map = {
        "NARRATOR": v_narrator,
        "MALE": v_male,
        "FEMALE": v_female
    }

    audio_segments_pydub = []
    
    for idx, (role, seg_text) in enumerate(script_segments):
        v = role_voice_map.get(role, v_narrator)
        seg_bytes = await synth_single_chunk_cached(seg_text, v, rate_str)
        
        if seg_bytes:
            if HAS_PYDUB and FFMPEG_READY:
                audio_seg = AudioSegment.from_file(io.BytesIO(seg_bytes), format="mp3")
                pause = AudioSegment.silent(duration=250, frame_rate=audio_seg.frame_rate)
                audio_segments_pydub.append(audio_seg + pause)
            else:
                audio_segments_pydub.append(seg_bytes)
                
        progress_bar.progress((idx + 1) / len(script_segments), text=f"🎭 已完成第 {idx+1}/{len(script_segments)} 段 [{role}] 合成...")

    progress_bar.empty()

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

    if apply_bgm:
        with st.spinner("🎵 正在注入背景音乐..."):
            final_bytes = mix_bgm_with_audio(final_bytes, bgm_name, vol_pct)

    return final_bytes

# --------------------------------------------------
# 8. 操作与结果展示区
# --------------------------------------------------
if "full_audio_bytes" not in st.session_state:
    st.session_state.full_audio_bytes = None

if st.button("🚀 生成完整广播剧/听书音频", type="primary", use_container_width=True):
    if not active_process_text.strip():
        st.warning("请先加载章节或粘贴文本！")
    else:
        with st.spinner("正在并发合成高保真多角色音频..."):
            try:
                audio_bytes = run_async_safe(
                    generate_radio_drama_audio(
                        active_process_text,
                        voice_narrator,
                        voice_male,
                        voice_female,
                        rate_str,
                        use_multi_role=enable_multi_role,
                        apply_bgm=enable_bgm,
                        bgm_name=selected_bgm_name,
                        vol_pct=bgm_volume
                    )
                )
                st.session_state.full_audio_bytes = audio_bytes
                st.success("🎉 多角色广播剧音频合成完成！")
            except Exception as e:
                st.error(f"生成失败: {e}")

if st.session_state.full_audio_bytes:
    st.divider()
    st.subheader("🎧 完整文章/广播剧听书试听")
    st.audio(st.session_state.full_audio_bytes, format="audio/mp3")
    st.download_button(
        "📥 下载完整 MP3 音频",
        data=st.session_state.full_audio_bytes,
        file_name="audiobook_radio_drama.mp3",
        mime="audio/mp3",
        use_container_width=True,
    )
