import asyncio
import io
import json
import os
import re
import hashlib
import subprocess
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor

import jieba
import jieba.analyse
import requests
import streamlit as st
import edge_tts
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

# --------------------------------------------------
# 0. 基础依赖库检测与磁盘缓存初始化
# --------------------------------------------------
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

# MD5 缓存目录
CACHE_DIR = ".audio_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# --------------------------------------------------
# 1. 页面基本配置 (采用 wide 宽屏渲染工作区)
# --------------------------------------------------
st.set_page_config(
    page_title="AI 全书助手 & 随身听书 (Ultimate Edition)",
    page_icon="🎧",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --------------------------------------------------
# 2. Session State 全局状态初始化
# --------------------------------------------------
if "loaded_text" not in st.session_state:
    st.session_state.loaded_text = ""
if "chapters" not in st.session_state:
    st.session_state.chapters = []  # [{"title": str, "content": str}]
if "selected_chapter_idx" not in st.session_state:
    st.session_state.selected_chapter_idx = 0
if "full_audio_bytes" not in st.session_state:
    st.session_state.full_audio_bytes = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "local_summary" not in st.session_state:
    st.session_state.local_summary = ""
if "top_quote" not in st.session_state:
    st.session_state.top_quote = ""
if "current_keywords" not in st.session_state:
    st.session_state.current_keywords = []

# --------------------------------------------------
# 3. 核心工具函数：深度清洗、自动分章与多线程 TTS 引擎
# --------------------------------------------------
def clean_extracted_text(text):
    """全源通用深度智能清洗引擎：彻底剔除页码、杂音与断句格式问题"""
    if not text:
        return ""
    text = text.replace('﹗', '！').replace('﹖', '？').replace('......', '……')
    lines = text.split("\n")
    cleaned_lines = []
    
    noise_keywords = [
        "家庭发展基金", "家庭發展基金", "ICAC", "廉政公署", "编者的话", "編者的話",
        "智多多", "製作", "制作", "贊助", "赞助", "版权所有", "版權所有",
        "All rights reserved", "ISBN", "关注微信公众号", "点击上方蓝字"
    ]
    noise_symbols = {"M", "W", "NNN", "B", "FES", "0", "00", "000"}
    page_patterns = [
        r'^\s*\d+(\s+\d+)*\s*$',         # 纯数字/双页码
        r'^\s*-\s*\d+\s*-\s*$',         # - 12 -
        r'^\s*第\s*\d+\s*[页頁]\s*$',     # 第 12 页
        r'^\s*Page\s*\d+\s*$',          # Page 12
        r'^[A-Z0-9_\-]+/\d+.*$',        # 印刷编码
        r'^\s*\d+\s*/\s*\d+\s*$',       # 分数页码
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
        if is_noise or any(kw in l for kw in noise_keywords):
            continue
        cleaned_lines.append(l)

    full_text = "\n".join(cleaned_lines)
    full_text = re.sub(r'([^。！？!？…\n])\n([^。！？!？…\n])', r'\1\2', full_text)
    return full_text.strip()

def split_text_into_chapters(full_text):
    """根据大文本中的章节标题进行自动正则拆分"""
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

def clean_markdown_for_speech(text):
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    text = re.sub(r"#+\s*", "", text)
    text = re.sub(r"^[•\-\*]\s*", "", text, flags=re.MULTILINE)
    emoji_pattern = re.compile(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002300-\U000023FF\U00002b00-\U00002bff]+",
        flags=re.UNICODE,
    )
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

# --- 🚀 核心优化：MD5 缓存 + Asyncio 10 线程并发 TTS 合成 ---
async def synth_chunk_cached(chunk, voice, rate_str, sem):
    """带 MD5 磁盘持久化缓存的单段合成"""
    chunk_hash = hashlib.md5(f"{chunk}_{voice}_{rate_str}".encode('utf-8')).hexdigest()
    cache_file = os.path.join(CACHE_DIR, f"{chunk_hash}.mp3")
    
    # 命中缓存直接从磁盘读取
    if os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return f.read()
            
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

async def generate_audio_bytes_parallel(text, voice, rate_str, max_concurrency=10):
    """多线程并发高效合成整个章节"""
    clean_text = clean_markdown_for_speech(text)
    chunks = split_text_chunks_safe(clean_text)
    sem = asyncio.Semaphore(max_concurrency)
    
    progress_bar = st.progress(0, text=f"⚡ 正在启动 {max_concurrency} 线程并发合成 (共 {len(chunks)} 段)...")
    
    async def worker(idx, chunk):
        data = await synth_chunk_cached(chunk, voice, rate_str, sem)
        return idx, data

    tasks = [worker(i, c) for i, c in enumerate(chunks)]
    completed = 0
    results = [None] * len(chunks)
    
    for f in asyncio.as_completed(tasks):
        idx, data = await f
        results[idx] = data
        completed += 1
        progress_bar.progress(completed / len(chunks), text=f"⚡ 已并发完成 {completed}/{len(chunks)} 段 (命中 MD5 缓存即刻秒刷)...")
        
    progress_bar.empty()
    
    full_audio = bytearray()
    for r in results:
        if r:
            full_audio.extend(r)
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
# 4. 网页抓取、YouTube与数据提取辅助函数
# --------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def fetch_text_from_url(url):
    if HAS_TRAFILATURA:
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                res = trafilatura.extract(downloaded, include_comments=False, include_tables=True)
                if res and len(res.strip()) > 30:
                    return res.strip()
        except Exception:
            pass
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36"}
    try:
        response = requests.get(url, headers=headers, timeout=12)
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, "html.parser")
        for el in soup(["script", "style", "header", "footer", "nav", "aside"]):
            el.extract()
        paragraphs = soup.find_all(["p", "article", "h1", "h2", "h3", "section"])
        text = "\n".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 10])
        return text if len(text) >= 50 else soup.get_text().strip()
    except Exception as e:
        raise Exception(f"网页抓取失败: {e}")

def parse_book_catalog(catalog_url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
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
                if any(kw in text for kw in ["首页", "书架", "登录", "目录", "作者", "上一页", "下一页"]):
                    continue
                full_url = urljoin(base_domain, href) if not href.startswith("http") else href
                if not any(c['url'] == full_url for c in chapters):
                    chapters.append({"title": text, "url": full_url})
        return chapters
    except Exception as e:
        raise Exception(f"解析目录失败: {e}")

# --------------------------------------------------
# 5. PIL 金句卡片绘制引擎
# --------------------------------------------------
@st.cache_resource
def get_chinese_font(font_size=20):
    paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttc",
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
    font_size, chars_per_line, line_height = (20, 22, 36) if quote_len <= 50 else (16, 26, 28)
    font_title = get_chinese_font(20)
    font_quote = get_chinese_font(font_size)
    font_footer = get_chinese_font(13)
    font_badge = get_chinese_font(13)
    font_big = get_chinese_font(60)

    lines, line = [], ""
    for char in quote_text:
        line += char
        if len(line) >= chars_per_line:
            lines.append(line)
            line = ""
    if line:
        lines.append(line)

    width, height = 700, max(420, 180 + len(lines) * line_height)
    styles = {
        "暖粉水彩": {"bg_top": (252, 231, 243), "bg_bot": (254, 249, 195), "card_bg": (255, 255, 255), "text": (88, 28, 135), "accent": (192, 38, 211), "quote_mark": (244, 114, 182)},
        "莫兰迪绿": {"bg_top": (209, 250, 229), "bg_bot": (236, 253, 245), "card_bg": (255, 255, 255), "text": (6, 78, 59), "accent": (5, 150, 105), "quote_mark": (110, 231, 183)},
        "天空云蓝": {"bg_top": (224, 242, 254), "bg_bot": (240, 249, 255), "card_bg": (255, 255, 255), "text": (12, 74, 110), "accent": (2, 132, 199), "quote_mark": (125, 211, 252)},
        "复古奶茶": {"bg_top": (254, 243, 199), "bg_bot": (254, 252, 232), "card_bg": (255, 253, 248), "text": (120, 53, 15), "accent": (217, 119, 6), "quote_mark": (252, 211, 77)}
    }
    s = styles.get(bg_style, styles["暖粉水彩"])

    img = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        r = int(s["bg_top"][0] + (s["bg_bot"][0] - s["bg_top"][0]) * (y / height))
        g = int(s["bg_top"][1] + (s["bg_bot"][1] - s["bg_top"][1]) * (y / height))
        b = int(s["bg_top"][2] + (s["bg_bot"][2] - s["bg_top"][2]) * (y / height))
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    margin = 30
    draw.rounded_rectangle([margin, margin, width - margin, height - margin], radius=20, fill=s["card_bg"])
    draw.text((margin + 25, margin + 25), "“", fill=s["quote_mark"], font=font_big)
    draw.text((margin + 40, margin + 30), "🌿 每日读书思维卡", fill=s["accent"], font=font_title)

    y_off = margin + 85
    for l in lines:
        draw.text((margin + 40, y_off), l, fill=s["text"], font=font_quote)
        y_off += line_height

    draw.text((margin + 40, height - margin - 30), "—— AI 全书助手 · 随身听书与思维系统", fill=(148, 163, 184), font=font_footer)
    
    img_byte = io.BytesIO()
    img.convert("RGB").save(img_byte, format="PNG")
    return img_byte.getvalue()

# --------------------------------------------------
# 6. 知识提炼与 30 秒听前导读（Takeaways）生成器
# --------------------------------------------------
def extract_chapter_takeaways(text):
    """自动生成听前 30 秒 3 Key Takeaways 导读卡"""
    if not text:
        return ["暂无前瞻提示"]
    paragraphs = [p.strip() for p in text.split("\n") if len(p.strip()) > 20]
    takeaways = []
    for p in paragraphs:
        if len(takeaways) >= 3:
            break
        s = p.split("。")[0].strip()
        if len(s) > 10 and not any(kw in s for kw in ["目录", "作者"]):
            takeaways.append(s)
    if not takeaways:
        takeaways = [text[:40] + "..."]
    return takeaways

@st.cache_data(show_spinner=False, ttl=3600)
def extract_ultimate_local_insights(text):
    if not text.strip():
        return "", "", []
    char_count = len(text)
    read_minutes = round(char_count / 300, 1)
    
    keywords = jieba.analyse.textrank(text, topK=6, withWeight=False, allowPOS=("n", "vn", "nz", "nr", "nt", "eng"))
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    candidates = []
    
    for p in paragraphs:
        for s in re.split(r"[。！!？]", p):
            s_clean = s.strip()
            if len(s_clean) > 15:
                score = sum(4 for kw in keywords if kw in s_clean)
                if score > 0:
                    candidates.append((score, s_clean))
                    
    candidates.sort(key=lambda x: x[0], reverse=True)
    top_points = [c[1] for c in candidates[:3]]
    top_quote = top_points[0] if top_points else "把握事物本质，逆向思考问题。"

    summary_md = f"📈 **文本体检**：本章共 **{char_count}** 字 | ⏱️ 预估阅读 **{read_minutes}** 分钟\n\n"
    summary_md += f"🏷️ **核心主题**： " + " ".join([f"`#{kw}`" for kw in keywords]) + "\n\n"
    summary_md += "🎯 **核心观点精炼**：\n"
    for i, pt in enumerate(top_points, 1):
        summary_md += f"**{i}.** {pt}。\n"
        
    return summary_md, top_quote, keywords

# --------------------------------------------------
# 7. 侧边栏 (控制中心 + 章节树状选择)
# --------------------------------------------------
with st.sidebar:
    st.title("🎧 控制中心")
    st.caption("AI 全书助手 (Ultimate Edition)")
    
    # Mode 1: 引擎选配
    with st.expander("⚙️ AI 模型与并发配置", expanded=False):
        use_ollama = st.checkbox("🧠 启用 Ollama (Qwen) 本地大模型", value=False)
        ollama_model = st.text_input("Ollama 模型名称:", value="qwen2.5:1.5b")
        concurrency_workers = st.slider("⚡ TTS 并发线程数:", 4, 16, 10, help="越高合成越快，建议 10 线程")
        use_md5_cache = st.checkbox("💾 启用 MD5 磁盘秒刷缓存", value=True)

    st.divider()
    
    # Mode 2: 多源数据导入
    st.subheader("📥 导入阅读内容")
    input_mode = st.radio(
        "选择输入方式：",
        ["📁 上传电子书/文件 (.pdf/epub/txt)", "📚 智能分章节听书 (目录网址)", "✍️ 粘贴纯文本/单页URL"],
        index=0
    )

    if input_mode == "📁 上传电子书/文件 (.pdf/epub/txt)":
        uploaded_file = st.file_uploader("上传文件：", type=["txt", "pdf", "docx", "epub"])
        if uploaded_file is not None:
            filename = uploaded_file.name.lower()
            extracted_raw = ""
            if filename.endswith(".txt"):
                extracted_raw = uploaded_file.read().decode("utf-8", errors="ignore")
            elif filename.endswith(".pdf"):
                reader = PdfReader(uploaded_file)
                extracted_raw = "\n".join([p.extract_text() for p in reader.pages if p.extract_text()])
            elif filename.endswith(".docx") and HAS_DOCX:
                doc = docx.Document(uploaded_file)
                extracted_raw = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
            elif filename.endswith(".epub") and HAS_EPUB:
                book = epub.read_epub(io.BytesIO(uploaded_file.read()))
                texts = [BeautifulSoup(item.get_content(), 'html.parser').get_text() for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)]
                extracted_raw = "\n".join(texts)

            cleaned_full = clean_extracted_text(extracted_raw)
            if cleaned_full != st.session_state.loaded_text:
                st.session_state.loaded_text = cleaned_full
                st.session_state.chapters = split_text_into_chapters(cleaned_full)
                st.session_state.selected_chapter_idx = 0
                st.session_state.full_audio_bytes = None
                st.success(f"🎉 成功解析并分出 {len(st.session_state.chapters)} 个章节！")

    elif input_mode == "📚 智能分章节听书 (目录网址)":
        catalog_url = st.text_input("输入书籍目录 URL:")
        if st.button("📚 解析目录", use_container_width=True):
            if catalog_url:
                try:
                    ch_list = parse_book_catalog(catalog_url)
                    if ch_list:
                        st.session_state.chapters = [{"title": c["title"], "url": c["url"], "content": ""} for c in ch_list]
                        st.session_state.selected_chapter_idx = 0
                        st.success(f"🎉 成功抓取到 {len(ch_list)} 章！")
                except Exception as e:
                    st.error(f"解析失败: {e}")

    else:
        user_text = st.text_area("粘贴文本/网址：", height=150)
        if st.button("🚀 导入文本", use_container_width=True):
            if user_text.startswith("http"):
                fetched = fetch_text_from_url(user_text)
                cleaned = clean_extracted_text(fetched)
            else:
                cleaned = clean_extracted_text(user_text)
            st.session_state.loaded_text = cleaned
            st.session_state.chapters = split_text_into_chapters(cleaned)
            st.session_state.selected_chapter_idx = 0
            st.session_state.full_audio_bytes = None

    # Mode 3: 已解析章节目录树选择
    st.divider()
    if st.session_state.chapters:
        st.subheader("📖 已解析章节目录树")
        chapter_titles = [f"{i+1}. {c['title']}" for i, c in enumerate(st.session_state.chapters)]
        selected_idx = st.selectbox(
            "切换当前听读章节：",
            range(len(chapter_titles)),
            format_func=lambda i: chapter_titles[i],
            index=st.session_state.selected_chapter_idx
        )
        if selected_idx != st.session_state.selected_chapter_idx:
            st.session_state.selected_chapter_idx = selected_idx
            st.session_state.full_audio_bytes = None

    # Mode 4: 朗读音色与倍速
    st.divider()
    st.subheader("🎛️ 语音属性设置")
    voice_option = st.selectbox(
        "选择音色：",
        ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "en-US-AvaMultilingualNeural", "ja-JP-NanamiNeural"],
        format_func=lambda x: {"zh-CN-XiaoxiaoNeural": "🇨🇳 晓晓 (女声)", "zh-CN-YunxiNeural": "🎙️ 云希 (男声)", "en-US-AvaMultilingualNeural": "🌐 Ava (双语)", "ja-JP-NanamiNeural": "🇯🇵 七海 (日语)"}.get(x, x)
    )
    speech_rate = st.slider("⚡ 播放语速：", 0.5, 2.0, 1.0, 0.1, format="%.1fx")
    rate_str = f"{int(round((speech_rate - 1.0) * 100)):+d}%"

# --------------------------------------------------
# 8. 主界面工作区：Tabs 三阶段知识消化系统
# --------------------------------------------------
st.title("📚 AI 全书助手与知识终端")

# 确定当前章节文本
current_chapter_content = ""
if st.session_state.chapters:
    cur_ch = st.session_state.chapters[st.session_state.selected_chapter_idx]
    if "content" in cur_ch and cur_ch["content"]:
        current_chapter_content = cur_ch["content"]
    elif "url" in cur_ch:
        with st.spinner("⏳ 正在动态加载该章节正文..."):
            fetched = fetch_text_from_url(cur_ch["url"])
            current_chapter_content = clean_extracted_text(fetched)
            cur_ch["content"] = current_chapter_content
else:
    current_chapter_content = st.session_state.loaded_text

# 渲染三大主选项卡 (Tab 1, Tab 2, Tab 3)
tab1, tab2, tab3 = st.tabs(["🎧 智能播放与导读卡", "🤔 AI 伴读对话", "📝 知识沉淀与导出"])

# ==================================================
# TAB 1: 智能播放与导读卡
# ==================================================
with tab1:
    if not current_chapter_content:
        st.info("👈 请先在左侧侧边栏导入文件、网址或纯文本。")
    else:
        # 1. 30秒听前前瞻导读卡 (Takeaways)
        takeaways = extract_chapter_takeaways(current_chapter_content)
        st.success("💡 **30 秒听前导读卡 (Key Takeaways)**")
        t_cols = st.columns(len(takeaways))
        for idx, takeaway_text in enumerate(takeaways):
            with t_cols[idx]:
                st.markdown(f"**要点 {idx+1}**")
                st.caption(takeaway_text)

        st.divider()

        # 2. 播放控制与并发合成按钮
        col_synth, col_status = st.columns([1, 2])
        with col_synth:
            if st.button("🚀 并发合成 / 播放本章音频", type="primary", use_container_width=True):
                with st.spinner("并发线程合成中..."):
                    audio_bytes = run_async_safe(
                        generate_audio_bytes_parallel(current_chapter_content, voice_option, rate_str, concurrency_workers)
                    )
                    st.session_state.full_audio_bytes = audio_bytes
                    st.success("🎉 音频生成/缓存读取完成！")

        if st.session_state.full_audio_bytes:
            st.audio(st.session_state.full_audio_bytes, format="audio/mp3")

        # 3. 章节正文折叠查看器
        with st.expander("📄 查看本章高亮与深度清洗文本", expanded=False):
            st.text_area("正文内容：", current_chapter_content, height=250)

        st.divider()

        # 4. 精华金句卡片生成
        st.subheader("🖼️ 自动生成【每日精华金句卡】")
        summary_md, top_quote, kws = extract_ultimate_local_insights(current_chapter_content)
        card_quote = st.text_input("✏️ 确认 / 修改金句文字：", value=top_quote)
        card_style = st.selectbox("🎨 视觉风格：", ["暖粉水彩", "莫兰迪绿", "天空云蓝", "复古奶茶"])
        
        card_bytes = generate_quote_card(card_quote, card_style, kws)
        st.image(card_bytes, width=500)
        st.download_button("📥 保存金句卡片 (.png)", data=card_bytes, file_name="quote_card.png", mime="image/png")

# ==================================================
# TAB 2: AI 伴读对话 (Contextual Chat)
# ==================================================
with tab2:
    st.subheader("💬 随听随问 AI 伴读助手")
    st.caption("AI 已自动绑定当前选定章节上下文。你可以随时提炼公式、解释概念或询问推演步骤。")

    # 快捷发问按钮
    col_q1, col_q2, col_q3 = st.columns(3)
    quick_query = None
    if col_q1.button("💡 提炼本章思维模型", use_container_width=True):
        quick_query = "请帮我提取本章中涉及的所有思维模型、核心决策逻辑或规律算法。"
    if col_q2.button("📊 梳理文中数据与对比", use_container_width=True):
        quick_query = "请帮我整理本章提及的所有硬核数据、百分比与核心事实表格。"
    if col_q3.button("🔄 运用逆向思维推演", use_container_width=True):
        quick_query = "根据本章内容，如果用逆向思维（Inversion）来看，我们应该避免哪些错误？"

    # 显示历史对话
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 处理输入
    user_input = st.chat_input("向 AI 发问本章内容...")
    final_query = quick_query if quick_query else user_input

    if final_query:
        st.session_state.chat_history.append({"role": "user", "content": final_query})
        with st.chat_message("user"):
            st.markdown(final_query)

        with st.chat_message("assistant"):
            with st.spinner("🧠 正在结合当前章节分析中..."):
                if use_ollama:
                    try:
                        prompt = f"上下文内容：\n{current_chapter_content[:3000]}\n\n用户问题：{final_query}"
                        res = requests.post("http://localhost:11434/api/generate", json={"model": ollama_model, "prompt": prompt, "stream": False}, timeout=30)
                        ans = res.json().get("response", "无法获取 Ollama 回复。")
                    except Exception:
                        ans = "Ollama 未开启，已为您切回本地提取。"
                else:
                    # 本地算法提炼
                    summary_md, _, _ = extract_ultimate_local_insights(current_chapter_content)
                    ans = f"**针对问题**：{final_query}\n\n**基于当前章节分析**：\n" + summary_md

                st.markdown(ans)
                st.session_state.chat_history.append({"role": "assistant", "content": ans})

# ==================================================
# TAB 3: 知识沉淀与导出
# ==================================================
with tab3:
    st.subheader("📝 全书/本章知识沉淀报告")
    if current_chapter_content:
        summary_md, top_quote, kws = extract_ultimate_local_insights(current_chapter_content)
        st.markdown(summary_md)

        st.divider()
        st.subheader("📦 多格式一键导出")
        d_col1, d_col2 = st.columns(2)
        with d_col1:
            st.download_button(
                "💾 导出 Markdown 读书笔记 (.md)",
                data=f"# 读书笔记\n\n{summary_md}\n\n## 原文内容\n{current_chapter_content}",
                file_name="book_notes.md",
                mime="text/markdown",
                use_container_width=True
            )
        with d_col2:
            if st.session_state.full_audio_bytes:
                st.download_button(
                    "📥 导出音频文件 (.mp3)",
                    data=st.session_state.full_audio_bytes,
                    file_name="chapter_audio.mp3",
                    mime="audio/mp3",
                    use_container_width=True
                )
