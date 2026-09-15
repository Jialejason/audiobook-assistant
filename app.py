import asyncio
import io
import os
import re
import jieba
import jieba.analyse
import jieba.posseg as pseg
import requests
import streamlit as st
import edge_tts
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi

# --------------------------------------------------
# 1. 页面基本配置
# --------------------------------------------------
st.set_page_config(
    page_title="随身听书 & 思维助手", page_icon="🎧", layout="centered"
)

st.title("🎧 随身听书 & 思维助手")
st.caption(
    "全领域动态自适应版：YouTube 智能突围引擎 + 跨学科金句引擎 + 全球母语听书"
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
# 3. 网页与 YouTube 突围抓取解析函数
# --------------------------------------------------
@st.cache_data(show_spinner=False, ttl=3600)
def fetch_text_from_url(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_text_from_youtube(url):
    video_id = extract_youtube_id(url)
    if not video_id:
        raise ValueError("无效的 YouTube 链接，请检查网址格式。")
    
    # 策略 1：尝试标准官方 API
    try:
        ytt_api = YouTubeTranscriptApi()
        target_languages = ['zh-CN', 'zh-TW', 'zh', 'en', 'ja', 'ko', 'fr', 'de', 'es']
        transcript_list = ytt_api.fetch(video_id, languages=target_languages)
        full_text = "\n".join([chunk.text for chunk in transcript_list])
        if full_text.strip():
            return full_text
    except Exception:
        pass

    # 策略 2：通过全球公开去中心化 Invidious 节点矩阵突围云端 IP 封锁
    invidious_nodes = [
        "https://yewtu.be",
        "https://vid.puffyan.us",
        "https://invidious.projectsegfault.net",
        "https://iv.ggtyler.dev"
    ]
    
    for node in invidious_nodes:
        try:
            res = requests.get(f"{node}/api/v1/captions/{video_id}", timeout=6)
            if res.status_code == 200:
                data = res.json()
                captions = data.get("captions", [])
                if captions:
                    target_cap = None
                    for cap in captions:
                        if any(l in cap.get("languageCode", "") for l in ["zh", "en", "ja"]):
                            target_cap = cap
                            break
                    if not target_cap:
                        target_cap = captions[0]
                    
                    cap_url = node + target_cap.get("url")
                    vtt_res = requests.get(cap_url, timeout=8)
                    if vtt_res.status_code == 200:
                        vtt_text = vtt_res.text
                        clean_lines = []
                        for line in vtt_text.split("\n"):
                            line = line.strip()
                            if "-->" in line or not line or line.startswith("WEBVTT") or line.isdigit():
                                continue
                            clean_line = re.sub(r'<[^>]+>', '', line)
                            if clean_line not in clean_lines:
                                clean_lines.append(clean_line)
                        full_text = "\n".join(clean_lines)
                        if len(full_text) > 30:
                            return full_text
        except Exception:
            continue

    raise Exception("YouTube 云端防火墙拦截成功，所有突围中转节点均无响应。建议更换视频测试或在本地运行。")

# --------------------------------------------------
# 4. 多功能输入层（支持网页、YouTube、文件）
# --------------------------------------------------
st.subheader("📥 导入阅读内容")
input_mode = st.radio(
    "选择输入方式：",
    ["✍️ 粘贴纯文本或网址(URL)", "🌐 粘贴 YouTube 视频链接", "📁 上传文件 (.txt / .pdf)"],
    horizontal=True,
)

raw_text = ""

if input_mode == "✍️ 粘贴纯文本或网址(URL)":
    user_input = st.text_area(
        "粘贴文本或网页网址（以 http/https 开头）：",
        height=180,
        placeholder="粘贴任意文章纯文本或输入网页网址...\n提示：粘贴后直接点击下方按钮即可动态智能提炼！",
    )
    if user_input.strip():
        text_candidate = user_input.strip()
        if text_candidate.startswith("http://") or text_candidate.startswith("https://"):
            with st.spinner("🔗 正在尝试解析网页正文..."):
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

elif input_mode == "🌐 粘贴 YouTube 视频链接":
    yt_url = st.text_input(
        "请输入 YouTube 视频网址：",
        placeholder="https://youtu.be/..."
    )
    if yt_url.strip():
        with st.spinner("🎬 正在通过突围矩阵提取 YouTube 视频字幕对白..."):
            try:
                raw_text = fetch_text_from_youtube(yt_url.strip())
                st.success(f"🎉 YouTube 视频内容提取成功！共获取到 {len(raw_text)} 个字符的对白。")
            except Exception as e:
                st.error(f"提取失败: {e}")

else:
    uploaded_file = st.file_uploader(
        "支持上传 .txt 或 .pdf 电子书文件", type=["txt", "pdf"]
    )
    if uploaded_file is not None:
        if uploaded_file.name.endswith(".txt"):
            raw_text = uploaded_file.read().decode("utf-8", errors="ignore")
        elif uploaded_file.name.endswith(".pdf"):
            pdf_reader = PdfReader(uploaded_file)
            for page in pdf_reader.pages:
                text_page = page.extract_text()
                if text_page:
                    raw_text += text_page + "\n"
        st.success(f"成功导入文件，共读取到 {len(raw_text)} 个字符！")

# --------------------------------------------------
# 5. 全球多语种音色选择
# --------------------------------------------------
VOICE_MAP = {
    # --- 中文与方言区 ---
    "zh-CN-XiaoxiaoNeural": "💃 Xiaoxiao - 经典御姐 / 知性温婉 (推荐)",
    "zh-CN-YunxiNeural": "🎙️ Yunxi - 磁性男主角 (小说听书推荐)",
    "zh-HK-HiuMaanNeural": "🇭🇰 HiuMaan - 标准粤语 / 港台风情",
    "zh-TW-HsiaoChenNeural": "🍵 HsiaoChen - 台湾腔 / 软萌甜美",
    "zh-CN-XiaoruiNeural": "🌸 Xiaorui - 柔和少女 / 清新可人",
    "zh-CN-XiaoyiNeural": "🎀 Xiaoyi - 娇软萌妹 / 甜美萝莉音",
    "zh-CN-YunjianNeural": "💼 Yunjian - 沉稳解说 / 商务男声",
    "zh-CN-YunyangNeural": "📢 Yunyang - 专业新闻播音 / 正气男声",
    "zh-CN-XiaozhenNeural": "📖 Xiaozhen - 故事绘本 / 亲切女声",
    "zh-CN-YunfengNeural": "🎬 Yunfeng - 影视解说 / 沉稳男声",
    "zh-CN-YunhaoNeural": "👦 Yunhao - 活力少男 / 朝气澎湃",
    "zh-CN-LN-XiaobeiNeural": "🗣️ Xiaobei - 东北方言 / 豪爽风趣",
    "zh-CN-SC-YunxiNeural": "🌶️ Yunxi - 四川方言 / 辣味亲切",
    "zh-CN-SD-YunxiangNeural": "🌾 Yunxiang - 山东方言 / 朴实厚重",
    "zh-HK-WanLungNeural": "🇭🇰 WanLung - 粤语男声 / 稳重成熟",
    "zh-TW-YunJheNeural": "🍵 YunJhe - 台湾腔男声 / 自然流畅",
    # --- 国际大语种区 ---
    "en-US-JennyNeural": "🇺🇸 Jenny (美音) - 自然清晰 / 播客首选",
    "en-US-GuyNeural": "🇺🇸 Guy (美音) - 商务稳重 / 男声解说",
    "en-GB-SoniaNeural": "🇬🇧 Sonia (英音) - 优雅地道 / 英伦风情",
    "ja-JP-NanamiNeural": "🇯🇵 Nanami (日语) - 甜美自然 / 亲切女声",
    "ja-JP-KeitaNeural": "🇯🇵 Keita (日语) - 沉稳男声 / 动漫解说感",
    "ko-KR-SunHiNeural": "🇰🇷 SunHi (韩语) - 温柔细腻 / 韩剧女声",
    "ko-KR-InJoonNeural": "🇰🇷 InJoon (韩语) - 磁性男声 / 沉稳有力",
    "fr-FR-DeniseNeural": "🇫🇷 Denise (法语) - 优雅浪漫 / 标准女声",
    "de-DE-KatjaNeural": "🇩🇪 Katja (德语) - 严谨清晰 / 播音质感",
    "es-ES-ElviraNeural": "🇪🇸 Elvira (西班牙语) - 热情明快",
}
voice_option = st.selectbox(
    "选择朗读音色：",
    options=list(VOICE_MAP.keys()),
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
        fallback = (
            "zh-CN-XiaoxiaoNeural"
            if any(f in voice for f in ["Xiaorui", "Xiaoyi", "HsiaoChen", "Xiaoxiao", "HiuMaan"])
            else "zh-CN-YunxiNeural"
        )
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
# 7. 跨平台自适应字库引擎
# --------------------------------------------------
def get_chinese_font(font_size=20):
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc"
    ]
    for path in font_paths:
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
# 8. 全领域动态自适应提炼引擎【跨学科通用逻辑】
# --------------------------------------------------
def clean_sentence_prefix(sentence):
    cleaned = sentence.strip()
    patterns = [
        r"^(?:[0-9一二三四五六七八九十]+[.\s、]|核心观点|观点|总结|总之|首先|其次|最后)[：:\s]*",
        r"^网上曾有一个广为流传的段子[：:]?",
        r"^我认为[，,]?",
        r"^在我看来[，,]?",
        r"^著名的科学家.*曾经说[，,]?",
        r"^我举一个简单的例子[：:]?",
        r"^一提到.*首先想到的就是",
    ]
    for p in patterns:
        cleaned = re.sub(p, "", cleaned)
    return cleaned.strip()

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
                top_quote = "把握事物的底层逻辑与增长原则。"

            return ai_output, top_quote, ["知识提炼", "核心要领"]
        else:
            raise Exception(f"Ollama 返回错误代码: {response.status_code}")
    except Exception as e:
        raise Exception(f"Ollama 连接异常: {e}")

@st.cache_data(show_spinner=False)
def extract_ultimate_local_insights(text):
    if not text.strip():
        return "", "", []

    char_count = len(text)
    read_minutes = round(char_count / 300, 1)

    auto_discovered_words = jieba.analyse.textrank(
        text, topK=25, withWeight=False, allowPOS=("n", "vn", "nz", "nr", "nt")
    )
    for word in auto_discovered_words:
        if len(word) >= 2:
            jieba.add_word(word)

    keywords = jieba.analyse.textrank(
        text, topK=6, withWeight=False, allowPOS=("n", "vn", "nz", "nr", "nt", "eng")
    )

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    headings = []
    candidates = []
    data_sentences = []
    total_paras = len(paragraphs)

    for p_idx, p in enumerate(paragraphs):
        if len(p) < 22 and not any(p.endswith(x) for x in ["。", "！", "？"]):
            headings.append(p)
            continue

        sentences = re.split(r"[。！!？\?]", p)
        for s_idx, s in enumerate(sentences):
            s_clean = s.strip()
            if len(s_clean) < 12:
                continue

            if re.search(r"\d+(\.\d+)?(%|亿|万|次方|年|层|个)?", s_clean) and len(s_clean) > 15:
                if s_clean not in data_sentences and len(data_sentences) < 3:
                    data_sentences.append(clean_sentence_prefix(s_clean))

            score = 0
            if s_idx == 0:
                score += 2
            
            if any(kw in s_clean for kw in keywords[:5]):
                score += 4
                
            if any(w in s_clean for w in ["底层", "核心", "关键", "本质", "原则", "总结", "规律", "逻辑", "机制", "结构", "核心是", "本质是"]):
                score += 4
                
            if any(w in s_clean for w in ["等于", "意味着", "决定了", "在于", "归根结底", "换句话说", "核心在于", "关键在于", "则是"]):
                score += 6
                
            if p_idx >= total_paras * 0.5 or "总结" in p or "核心观点" in p or "结论" in p:
                score += 3

            if score >= 5:
                clean_s = clean_sentence_prefix(s_clean)
                if clean_s and len(clean_s) > 12:
                    candidates.append((score, clean_s))

    candidates.sort(key=lambda x: x[0], reverse=True)
    
    unique_candidates = []
    seen = set()
    for _, s in candidates:
        if s not in seen:
            seen.add(s)
            unique_candidates.append(s)

    top_one_sentence = unique_candidates[0] if unique_candidates else "把握文章的核心逻辑与主旨概念。"
    top_points = unique_candidates[1:4] if len(unique_candidates) > 1 else unique_candidates[:1]

    summary_md = f"📈 **文本体检**：全文共 **{char_count}** 字  |  ⏱️ 预估阅读约 **{read_minutes}** 分钟\n\n"
    
    kw_badges = " ".join([f"`#{kw}`" for kw in keywords]) if keywords else "暂无"
    summary_md += f"🏷️ **核心主题标签**：\n{kw_badges}\n\n"

    if headings:
        summary_md += "🧩 **文章结构骨架**：\n"
        for h in headings:
            summary_md += f"• **{h}**\n"
        summary_md += "\n"

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
            st.warning("请先粘贴文本或导入内容！")
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
            st.warning("请先粘贴文本或导入内容！")
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
                with st.spinner("⚡ 正在通过全领域动态引擎深度提炼中..."):
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
        file_name="full_audiobook.mp3",
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
