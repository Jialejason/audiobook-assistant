import asyncio
import io
import os
import re
import subprocess
from urllib.parse import urljoin, urlparse
import jieba
import jieba.analyse
import jieba.posseg as pseg
import requests
import streamlit as st
import edge_tts
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

# 尝试导入 youtube_transcript_api
try:
    from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
    HAS_YOUTUBE_API = True
except ImportError:
    HAS_YOUTUBE_API = False

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
    page_title="随身听书 & 思维助手", page_icon="🎧", layout="centered"
)

st.title("🎧 随身听书 & 思维助手")
st.caption(
    "全语种动态自适应版：YouTube多语言字幕抓取 + 智能分章节听书 + 国际化 TTS 引擎"
)

# --------------------------------------------------
# 2. 侧边栏：Ollama (Qwen) 大模型选配设置
# --------------------------------------------------
with st.sidebar:
    st.header("⚙️ 引擎设置")
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

# --------------------------------------------------
# 3. 高级网页与 YouTube 自动解析函数
# --------------------------------------------------
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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
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
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        res = requests.get(catalog_url, headers=headers, timeout=12)
        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, "html.parser")
        
        parsed_url = urlparse(catalog_url)
        base_domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        chapters = []
        for a in soup.find_all("a", href=True):
            text = a.get_text().strip()
            href = a['href']
            if text and (("第" in text and "章" in text) or len(text) < 30):
                if any(kw in text for kw in ["首页", "书架", "登录", "目录", "作者", "意见", "关于", "上一页", "下一页", "尾页", "排行榜"]):
                    continue
                full_url = urljoin(base_domain, href) if not href.startswith("http") else href
                if not any(c['url'] == full_url for c in chapters):
                    chapters.append({"title": text, "url": full_url})
        return chapters
    except Exception as e:
        raise Exception(f"解析书本目录失败: {e}")

def extract_youtube_id(url):
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

@st.cache_data(show_spinner=False, ttl=1800)
def fetch_youtube_transcript_backend(video_id):
    """Python 后端全版本兼容字幕抓取引擎（三重备用机制）"""
    if not HAS_YOUTUBE_API:
        return False, "未安装 youtube_transcript_api 库", "en"
    
    preferred_langs = ['en', 'zh-Hans', 'zh-Hant', 'zh', 'ja', 'es', 'de', 'fr', 'ko']
    
    # 1. 第一重方案：使用 get_transcript 指定语言
    try:
        data = YouTubeTranscriptApi.get_transcript(video_id, languages=preferred_langs)
        full_text = " ".join([item['text'] for item in data])
        return True, full_text, "auto"
    except Exception:
        pass

    # 2. 第二重方案：使用 get_transcript 默认抓取
    try:
        data = YouTubeTranscriptApi.get_transcript(video_id)
        full_text = " ".join([item['text'] for item in data])
        return True, full_text, "auto"
    except Exception:
        pass

    # 3. 第三重方案：尝试使用 list_transcripts（若版本支持）
    try:
        if hasattr(YouTubeTranscriptApi, 'list_transcripts'):
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            try:
                transcript = transcript_list.find_transcript(preferred_langs)
            except Exception:
                transcript = next(iter(transcript_list))
            data = transcript.fetch()
            full_text = " ".join([item['text'] for item in data])
            return True, full_text, getattr(transcript, 'language_code', 'auto')
    except Exception:
        pass

    return False, "后端抓取受限（可能云端 IP 被限制或未开启公开 CC 字幕）", "zh"

def detect_language(text):
    """简易语种识别引擎"""
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

# --------------------------------------------------
# 4. 多功能输入层
# --------------------------------------------------
st.subheader("📥 导入阅读内容")
input_mode = st.radio(
    "选择输入方式：",
    ["✍️ 粘贴纯文本或单页网址(URL)", "📚 智能分章节整本听书 (目录网址)", "🌐 粘贴 YouTube 视频链接", "📁 上传文件 (.txt / .pdf)"],
    horizontal=True,
)

raw_text = ""
detected_lang_code = "zh"

if input_mode == "✍️ 粘贴纯文本或单页网址(URL)":
    user_input = st.text_area(
        "粘贴文本或网页网址（以 http/https 开头）：",
        height=160,
        placeholder="粘贴文章纯文本、单篇知乎/新闻链接、英文 TED 字幕...\n提示：单次建议控制在 2000-15000 字以内，体验最流畅！",
    )
    if user_input.strip():
        text_candidate = user_input.strip()
        if text_candidate.startswith("http://") or text_candidate.startswith("https://"):
            with st.spinner("🔗 正在通过智能解析引擎提取正文..."):
                try:
                    fetched = fetch_text_from_url(text_candidate)
                    if len(fetched) > 50 and not fetched.startswith("http"):
                        raw_text = fetched
                        st.success(f"🎉 网页解析成功！共提取到 {len(raw_text)} 个字符。")
                    else:
                        st.warning("⚠️ 该网页设置了加密防爬，已为你恢复文本模式，请直接复制网页里的文字粘贴进来！")
                        raw_text = user_input
                except Exception as e:
                    st.warning(f"无法读取该网址正文: {e}，请直接复制文本粘贴输入。")
                    raw_text = user_input
        else:
            raw_text = user_input
        detected_lang_code = detect_language(raw_text)

elif input_mode == "📚 智能分章节整本听书 (目录网址)":
    st.caption("🤖 AI 听书助理模式：输入整本书或小说的目录页网址，自动切章节并支持连续播放下一章！")
    
    if "book_chapters" not in st.session_state:
        st.session_state.book_chapters = []
    if "current_chapter_idx" not in st.session_state:
        st.session_state.current_chapter_idx = 0

    catalog_url = st.text_input(
        "请输入书籍目录页网址：",
        placeholder="https://www.example.com/book/12345/"
    )
    
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
                            st.success(f"🎉 目录解析成功！共发现 {len(chapters)} 个章节。")
                        else:
                            st.warning("未能在该网址中自动提取到章节目录，请尝试换一个源或直接使用单页网址。")
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
                raw_text = fetch_text_from_url(current_ch_url)
                st.info(f"✅ 本章加载成功，共 {len(raw_text)} 个字符。点击下方按钮即可一键听书或提炼！")
                detected_lang_code = detect_language(raw_text)
            except Exception as e:
                raw_text = f"加载章节正文出错: {e}"
                st.error(raw_text)

elif input_mode == "🌐 粘贴 YouTube 视频链接":
    yt_url = st.text_input(
        "请输入 YouTube 视频网址：",
        placeholder="https://www.youtube.com/watch?v=... 或 https://youtu.be/..."
    )
    if yt_url.strip():
        video_id = extract_youtube_id(yt_url.strip())
        if video_id:
            with st.spinner("🤖 正在尝试 Python 后端全语种自动提取字幕..."):
                success, yt_text, lang_code = fetch_youtube_transcript_backend(video_id)
            
            if success:
                raw_text = yt_text
                detected_lang_code = detect_language(raw_text)
                st.success(f"🎉 字幕抓取成功！共提取到 {len(raw_text)} 个字符。")
                st.text_area("📹 提取的字幕文本预览", raw_text, height=140)
            else:
                st.warning(f"⚠️ {yt_text}")
                st.markdown("#### 📱 备用方案：启动 CORS 跨域代理抓取")
                st.caption("如果后端云端 IP 被限制，可点击下方按钮使用手机本地网络抓取字幕：")
                
                js_code = f"""
                <div style="font-family: system-ui, -apple-system, sans-serif; padding: 12px; background: #1e293b; border-radius: 10px; color: #fff;">
                    <button id="fetchBtn" style="background: #2563eb; color: white; border: none; padding: 12px 18px; border-radius: 8px; cursor: pointer; font-weight: bold; width: 100%; font-size: 15px;">
                        ⚡ 启动手机 CORS 跨域代理抓取字幕
                    </button>
                    <div id="status" style="margin-top: 10px; font-size: 13px; color: #94a3b8; text-align: center;">准备就绪，点击上方按钮开始抓取</div>
                    <textarea id="resultText" style="width: 100%; height: 110px; margin-top: 10px; background: #0f172a; color: #e2e8f0; border: 1px solid #334155; border-radius: 6px; padding: 10px; font-size: 13px; display: none;" readonly></textarea>
                </div>

                <script>
                document.getElementById('fetchBtn').addEventListener('click', async () => {{
                    const status = document.getElementById('status');
                    const resultText = document.getElementById('resultText');
                    const videoId = "{video_id}";
                    
                    status.innerText = "⏳ 正在连接 CORS 跨域代理抓取字幕...";
                    status.style.color = "#fbbf24";

                    const targetApi = `https://yt.lemnoslife.com/noKey/captions?videoId=${{videoId}}`;
                    const proxies = [
                        `https://api.allorigins.win/raw?url=${{encodeURIComponent(targetApi)}}`,
                        `https://corsproxy.io/?${{encodeURIComponent(targetApi)}}`
                    ];

                    let fetchedText = "";

                    for (let proxyUrl of proxies) {{
                        try {{
                            let response = await fetch(proxyUrl);
                            if (response.ok) {{
                                let data = await response.json();
                                let tracks = data.subtitles || [];
                                if (tracks.length > 0) {{
                                    let trackUrl = tracks[0].baseUrl;
                                    let xmlProxy = `https://api.allorigins.win/raw?url=${{encodeURIComponent(trackUrl)}}`;
                                    let xmlRes = await fetch(xmlProxy);
                                    let xmlText = await xmlRes.text();
                                    
                                    let parser = new DOMParser();
                                    let xmlDoc = parser.parseFromString(xmlText, "text/xml");
                                    let textNodes = xmlDoc.getElementsByTagName("text");
                                    
                                    let lines = [];
                                    for (let i = 0; i < textNodes.length; i++) {{
                                        let txt = textNodes[i].textContent.replace(/<[^>]+>/g, '').trim();
                                        if (txt) lines.push(txt);
                                    }}
                                    fetchedText = lines.join('\\n');
                                    if (fetchedText.length > 30) break;
                                }}
                            }}
                        }} catch (e) {{}}
                    }}

                    if (fetchedText.length > 30) {{
                        status.innerText = "✅ 抓取成功！已自动选中文本，复制后切到【粘贴纯文本】模式即可使用：";
                        status.style.color = "#4ade80";
                        resultText.value = fetchedText;
                        resultText.style.display = "block";
                        resultText.select();
                    }} else {{
                        status.innerText = "⚠️ 抓取失败：该视频作者未开启公开 CC 字幕（或字幕已被限制）。";
                        status.style.color = "#f87171";
                    }}
                }});
                </script>
                """
                st.components.v1.html(js_code, height=220)
        else:
            st.error("无效的 YouTube 链接，请检查网址格式。")

else:
    uploaded_file = st.file_uploader(
        "支持上传 .txt 或 .pdf 电子书文件", type=["txt", "pdf"]
    )
    if uploaded_file is not None:
        if uploaded_file.name.endswith(".txt"):
            raw_text = uploaded_file.read().decode("utf-8", errors="ignore")
        elif uploaded_file.name.endswith(".pdf"):
            pdf_reader = PdfReader(uploaded_file)
            extracted_pages = []
            for page in pdf_reader.pages:
                text_page = page.extract_text()
                if text_page:
                    extracted_pages.append(text_page)
            raw_text = "\n".join(extracted_pages)
        st.success(f"成功导入文件，共读取到 {len(raw_text)} 个字符！")
        detected_lang_code = detect_language(raw_text)

# --------------------------------------------------
# 5. 全球多语种音色映射与自适应匹配
# --------------------------------------------------
VOICE_MAP = {
    "zh-CN-XiaoxiaoNeural": "💃 Xiaoxiao - 经典御姐 / 知性温婉 (推荐)",
    "zh-CN-YunxiNeural": "🎙️ Yunxi - 磁性男主角 (小说听书推荐)",
    "en-US-JennyNeural": "🇺🇸 Jenny (美音) - 自然清晰 / TED 播客推荐",
    "en-US-GuyNeural": "🇺🇸 Guy (美音) - 商务稳重 / 英文解说",
    "en-GB-SoniaNeural": "🇬🇧 Sonia (英音) - 标准优雅 / 商务英音",
    "zh-HK-HiuMaanNeural": "🇭🇰 HiuMaan - 标准粤语 / 港台风情",
    "zh-TW-HsiaoChenNeural": "🍵 HsiaoChen - 台湾腔 / 软萌甜美",
    "zh-CN-YunjianNeural": "💼 Yunjian - 沉稳解说 / 商务男声",
    "zh-CN-YunyangNeural": "📢 Yunyang - 专业新闻播音 / 正气男声",
    "ja-JP-NanamiNeural": "🇯🇵 Nanami (日语) - 甜美自然 / 亲切女声",
    "ko-KR-SunHiNeural": "🇰🇷 SunHi (韩语) - 温柔细腻 / 韩剧女声",
    "es-ES-ElviraNeural": "🇪🇸 Elvira (西班牙语) - 标准通用西语",
}

# 根据语种自动匹配最佳索引
def get_default_voice_index(lang):
    lang_lower = str(lang).lower()
    if "en" in lang_lower:
        return 2  # Jenny (美音)
    elif "ja" in lang_lower:
        return 9  # Nanami (日语)
    elif "ko" in lang_lower:
        return 10 # SunHi (韩语)
    elif "es" in lang_lower:
        return 11 # Elvira (西语)
    elif "hant" in lang_lower or "tw" in lang_lower:
        return 6  # HsiaoChen (台湾腔)
    elif "hk" in lang_lower:
        return 5  # HiuMaan (粤语)
    return 0     # 默认中文 Xiaoxiao

default_idx = get_default_voice_index(detected_lang_code)

voice_option = st.selectbox(
    "选择朗读音色（已根据检测语种为您智能推荐）：",
    options=list(VOICE_MAP.keys()),
    index=default_idx,
    format_func=lambda x: VOICE_MAP[x],
)

# --------------------------------------------------
# 6. 纯净语音与安全切片引擎
# --------------------------------------------------
def clean_markdown_for_speech(text):
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"^[•\-\*]\s*", "", text, flags=re.MULTILINE)

    emoji_pattern = re.compile(
        "["
        "\U0001F000-\U0001FAFF"
        "\U00002600-\U000027BF"
        "\U00002300-\U000023FF"
        "\U00002b00-\U00002bff"
        "\U0000fe00-\U0000fe0f"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    return text.strip()

def split_text_chunks_safe(text, max_chunk_size=800):
    raw_sentences = re.split(r'([。！!?？\n])', text)
    chunks = []
    current_chunk = ""

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

async def synth_single_chunk(chunk, voice):
    audio_data = bytearray()
    try:
        communicate = edge_tts.Communicate(chunk, voice)
        async for item in communicate.stream():
            if item["type"] == "audio":
                audio_data.extend(item["data"])
    except Exception:
        fallback = "zh-CN-XiaoxiaoNeural" if "zh" in voice else "en-US-JennyNeural"
        communicate = edge_tts.Communicate(chunk, fallback)
        async for item in communicate.stream():
            if item["type"] == "audio":
                audio_data.extend(item["data"])
    return audio_data

async def generate_audio_bytes_safe(text, voice):
    clean_text = clean_markdown_for_speech(text)
    chunks = split_text_chunks_safe(clean_text)
    
    full_audio = bytearray()
    for chunk in chunks:
        res = await synth_single_chunk(chunk, voice)
        full_audio.extend(res)
        await asyncio.sleep(0.05)

    return bytes(full_audio)

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
# 7. 跨平台自适应字库引擎与金句卡片生成
# --------------------------------------------------
@st.cache_resource
def get_chinese_font(font_size=20):
    try:
        res = subprocess.run(['fc-list', ':lang=zh', 'file'], capture_output=True, text=True, timeout=3)
        if res.returncode == 0 and res.stdout:
            for line in res.stdout.splitlines():
                font_path = line.split(':')[0].strip()
                if font_path and os.path.exists(font_path):
                    try:
                        return ImageFont.truetype(font_path, font_size)
                    except Exception:
                        continue
    except Exception:
        pass

    linux_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"
    ]
    for path in linux_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, font_size)
            except Exception:
                continue

    fallback_paths = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc"
    ]
    for path in fallback_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, font_size)
            except Exception:
                continue
                
    return ImageFont.load_default()

def generate_quote_card(quote_text, bg_style="暖粉水彩", keywords=None):
    quote_len = len(quote_text)
    if quote_len <= 35:
        font_size, chars_per_line, line_height = 22, 20, 40
    elif quote_len <= 70:
        font_size, chars_per_line, line_height = 18, 24, 34
    elif quote_len <= 120:
        font_size, chars_per_line, line_height = 16, 28, 28
    else:
        font_size, chars_per_line, line_height = 14, 32, 24

    font_title = get_chinese_font(20)
    font_quote = get_chinese_font(font_size)
    font_footer = get_chinese_font(13)
    font_badge = get_chinese_font(13)
    font_big = get_chinese_font(70)

    lines = []
    line = ""
    for char in quote_text:
        line += char
        if len(line) >= chars_per_line:
            lines.append(line)
            line = ""
    if line:
        lines.append(line)

    width = 750
    needed_text_h = len(lines) * line_height
    height = max(480, 200 + needed_text_h)

    styles = {
        "暖粉水彩": {
            "bg_top": (252, 231, 243), "bg_bot": (254, 249, 195),
            "card_bg": (255, 255, 255), "text": (88, 28, 135),
            "accent": (192, 38, 211), "quote_mark": (244, 114, 182),
            "badge_bg": (250, 232, 255), "badge_text": (168, 85, 247)
        },
        "莫兰迪绿": {
            "bg_top": (209, 250, 229), "bg_bot": (236, 253, 245),
            "card_bg": (255, 255, 255), "text": (6, 78, 59),
            "accent": (5, 150, 105), "quote_mark": (110, 231, 183),
            "badge_bg": (209, 250, 229), "badge_text": (4, 120, 87)
        },
        "天空云蓝": {
            "bg_top": (224, 242, 254), "bg_bot": (240, 249, 255),
            "card_bg": (255, 255, 255), "text": (12, 74, 110),
            "accent": (2, 132, 199), "quote_mark": (125, 211, 252),
            "badge_bg": (224, 242, 254), "badge_text": (3, 105, 161)
        },
        "复古奶茶": {
            "bg_top": (254, 243, 199), "bg_bot": (254, 252, 232),
            "card_bg": (255, 253, 248), "text": (120, 53, 15),
            "accent": (217, 119, 6), "quote_mark": (252, 211, 77),
            "badge_bg": (254, 243, 199), "badge_text": (180, 83, 9)
        }
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
    card_rect = [margin, margin, width - margin, height - margin]
    draw.rounded_rectangle(card_rect, radius=24, fill=s["card_bg"])

    draw.text((margin + 30, margin + 40), "“", fill=s["quote_mark"], font=font_big)
    draw.text((width - margin - 80, height - margin - 110), "”", fill=s["quote_mark"], font=font_big)
    draw.text((margin + 45, margin + 35), "🌿 每日精华金句卡", fill=s["accent"], font=font_title)

    y_offset = margin + 95
    for l in lines:
        bbox = draw.textbbox((0, 0), l, font=font_quote)
        w = bbox[2] - bbox[0]
        x_center = (width - w) // 2
        draw.text((x_center, y_offset), l, fill=s["text"], font=font_quote)
        y_offset += line_height

    if keywords:
        x_badge = margin + 45
        y_badge = height - margin - 75
        for kw in keywords[:3]:
            tag_text = f"#{kw}"
            bbox = draw.textbbox((0, 0), tag_text, font=font_badge)
            w = bbox[2] - bbox[0] + 18
            h = 28
            draw.rounded_rectangle([x_badge, y_badge, x_badge + w, y_badge + h], radius=10, fill=s["badge_bg"])
            draw.text((x_badge + 9, y_badge + 5), tag_text, fill=s["badge_text"], font=font_badge)
            x_badge += w + 12

    draw.line([(margin + 45, height - margin - 35), (width - margin - 45, height - margin - 35)], fill=(241, 245, 249), width=1)
    draw.text((margin + 45, height - margin - 28), "—— 随身听书 & 思维助手 · 深度领读", fill=(148, 163, 184), font=font_footer)

    img_byte_arr = io.BytesIO()
    img.convert("RGB").save(img_byte_arr, format="PNG")
    return img_byte_arr.getvalue()

# --------------------------------------------------
# 8. 全语种自适应知识提炼引擎
# --------------------------------------------------
def clean_sentence_prefix(sentence):
    cleaned = sentence.strip()
    patterns = [
        r"^(?:[0-9一二三四五六七八九十]+[.\s、]|核心观点|观点|总结|总之|首先|其次|最后)[：:\s]*",
        r"^我认为[，,]?",
        r"^在我看来[，,]?",
        r"^我举一个简单的例子[：:]?",
    ]
    for p in patterns:
        cleaned = re.sub(p, "", cleaned)
    return cleaned.strip()

def is_noise_or_heading(sentence):
    s = sentence.strip()
    if any(kw in s for kw in ["深度领读", "导读", "作者：", "来源：", "点击上方", "关注我们"]):
        return True
    if re.match(r'^(?:[一二三四五六七八九十]+[、\.\s]|\d+[、\.\s]|结语|总结|引言|前言|摘要)', s):
        return True
    if len(s) < 15 or len(s) > 140:
        return True
    return False

def extract_with_ollama(text, model_name):
    url = "http://localhost:11434/api/generate"
    prompt = f"""
    你是一个资深的知识提炼专家。请对以下文本进行深度解析与领读归纳，格式要求如下：

    📌 **一句话精髓**：
    (用精炼的一句话概括核心逻辑)

    🔑 **核心要点与主要重点**：
    1. 
    2. 
    3. 

    📊 **关键数据与硬核事实**：
    (列出文中提到的核心数据、百分比或关键概念)

    文本内容：
    {text}
    """
    payload = {"model": model_name, "prompt": prompt, "stream": False}

    try:
        response = requests.post(url, json=payload, timeout=60)
        if response.status_code == 200:
            res_json = response.json()
            ai_output = res_json.get("response", "")

            top_quote = ""
            quote_match = re.search(r"📌 \*\*一句话精髓\*\*：?\n?>?\s*(.*)", ai_output)
            if quote_match:
                top_quote = quote_match.group(1).split("\n")[0].strip()
            else:
                top_quote = "把握事物的底层逻辑与核心原则。"

            return ai_output, top_quote, ["知识提炼", "核心要领"]
        else:
            raise Exception(f"Ollama 返回错误代码: {response.status_code}")
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
        keywords = jieba.analyse.textrank(
            text, topK=6, withWeight=False, allowPOS=("n", "vn", "nz", "nr", "nt", "eng")
        )
    else:
        words = re.findall(r'\b[A-Za-z]{4,}\b', text)
        keywords = list(set(words))[:6]

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    candidates = []
    data_sentences = []
    
    for p in paragraphs:
        sentences = re.split(r"[。！!？\?.]", p)
        for s in sentences:
            s_clean = s.strip()
            if is_noise_or_heading(s_clean):
                continue

            if re.search(r"\d+(\.\d+)?(%|亿|万|percent|years|times)?", s_clean) and len(s_clean) > 15:
                if s_clean not in data_sentences and len(data_sentences) < 3:
                    data_sentences.append(clean_sentence_prefix(s_clean))

            score = 0
            if any(kw in s_clean for kw in keywords[:5]):
                score += 4
            if any(w in s_clean.lower() for w in ["important", "key", "essential", "core", "底层", "核心", "关键", "本质"]):
                score += 5

            if score >= 4:
                clean_s = clean_sentence_prefix(s_clean)
                if clean_s and not is_noise_or_heading(clean_s):
                    candidates.append((score, clean_s))

    candidates.sort(key=lambda x: x[0], reverse=True)
    
    unique_candidates = []
    seen = set()
    for _, s in candidates:
        if s not in seen and len(s) > 15:
            seen.add(s)
            unique_candidates.append(s)

    top_one_sentence = unique_candidates[0] if unique_candidates else "把握文本的核心逻辑与核心观点。"
    top_points = unique_candidates[:3] if len(unique_candidates) >= 3 else unique_candidates

    summary_md = f"📈 **文本体检**：全文共 **{char_count}** 字  |  ⏱️ 预估阅读约 **{read_minutes}** 分钟\n\n"
    kw_badges = " ".join([f"`#{kw}`" for kw in keywords]) if keywords else "暂无"
    summary_md += f"🏷️ **核心主题标签**：\n{kw_badges}\n\n"

    summary_md += "🎯 **核心观点深度提炼**：\n"
    if top_points:
        for i, pt in enumerate(top_points, 1):
            summary_md += f"**{i}.** {pt}。\n\n"

    if data_sentences:
        summary_md += "📊 **关键数据与硬核事实**：\n"
        for ds in data_sentences:
            summary_md += f"• {ds}。\n"

    return summary_md, top_one_sentence, keywords

# --------------------------------------------------
# 9. Session State 状态管理与操作区
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
    if st.button("🚀 生成完整音频", type="primary", use_container_width=True):
        if not raw_text.strip():
            st.warning("请先加载章节或粘贴文本！")
        else:
            with st.spinner("正在合成完整音频（长文本请稍候）..."):
                try:
                    audio_bytes = run_async_safe(
                        generate_audio_bytes_safe(raw_text, voice_option)
                    )
                    st.session_state.full_audio_bytes = audio_bytes
                    st.success("🎉 完整音频合成完成！")
                except Exception as e:
                    st.error(f"生成失败: {e}")

with col2:
    if st.button("⚡ 开始知识深度提炼", use_container_width=True):
        if not raw_text.strip():
            st.warning("请先加载章节或粘贴文本！")
        else:
            if use_ollama:
                with st.spinner("🧠 正在调用本地 Ollama (Qwen) AI 大模型思考中..."):
                    try:
                        summary_res, top_quote, kws = extract_with_ollama(raw_text, ollama_model)
                        st.session_state.local_summary = summary_res
                        st.session_state.top_quote = top_quote
                        st.session_state.current_keywords = kws
                        st.success("🎉 本地 Ollama (Qwen) 大模型提炼完成！")
                    except Exception as e:
                        st.error(f"Ollama 连接失败({e})，已自动无缝切回自适应算法！")
                        summary_res, top_quote, kws = extract_ultimate_local_insights(raw_text)
                        st.session_state.local_summary = summary_res
                        st.session_state.top_quote = top_quote
                        st.session_state.current_keywords = kws
            else:
                with st.spinner("⚡ 正在通过智能增强引擎深度提炼中..."):
                    summary_res, top_quote, kws = extract_ultimate_local_insights(raw_text)
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
            card_style = st.selectbox(
                "🎨 选择卡片背景风格：",
                ["暖粉水彩", "莫兰迪绿", "天空云蓝", "复古奶茶"]
            )
            card_bytes = generate_quote_card(
                st.session_state.top_quote, 
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
                        generate_audio_bytes_safe(st.session_state.local_summary, voice_option)
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
