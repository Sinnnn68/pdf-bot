import os
import re
import time
import threading
import asyncio
import logging
import tempfile
import shutil
import statistics

import requests
import pypdf
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

try:
    import pdfplumber
except ImportError:  # نكمل بـ pypdf لو المكتبة غير مثبتة
    pdfplumber = None

try:
    from fpdf import FPDF
    import arabic_reshaper
    PDF_LIBS_OK = True
except ImportError:  # نرسل ملف txt بدل PDF لو مكتبات الـ PDF ناقصة
    PDF_LIBS_OK = False

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
# مكتبة httpx تطبع رابط فيه توكن البوت بالـ Logs، نسكتها حتى ما ينكشف التوكن
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pdf-bot")

HERE = os.path.dirname(os.path.abspath(__file__))
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")


def _build_model_list():
    """الموديل اللي تكتبه بمتغير GEMINI_MODEL يجي أول، وبعده بدائل تلقائية."""
    wanted = [m.strip() for m in (os.getenv("GEMINI_MODEL") or "").split(",") if m.strip()]
    wanted += ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
    seen, result = set(), []
    for m in wanted:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


MODELS = _build_model_list()
MODEL_STATE = {"i": 0}  # رقم الموديل الشغال حالياً
MODEL_LOCK = threading.Lock()

MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "6"))       # عدد المحاولات لكل طلب
CONCURRENCY = int(os.getenv("CONCURRENCY", "3"))         # كم دفعة تترجم بنفس الوقت
BATCH_LIMIT = int(os.getenv("BATCH_LIMIT", "6000"))      # أقصى حجم نص بالطلب الواحد (بالحروف)
MAX_FILE_BYTES = 20 * 1024 * 1024                        # حد تليجرام للبوتات بالتحميل
ARABIC_RE = re.compile(r"[؀-ۿ]")
LETTERS_RE = re.compile(r"[A-Za-z]{3,}")                 # فقرة بدون كلمات إنجليزية ما تحتاج ترجمة


# ----------------------------------------------------------------------------
# التعامل مع جيميناي (طلب HTTP مباشر بدون مكتبة جوجل)
# ----------------------------------------------------------------------------
class GeminiError(Exception):
    def __init__(self, user_msg, status=None, retryable=False, fatal=False, wait=None, cap=None):
        super().__init__(user_msg)
        self.user_msg = user_msg
        self.status = status
        self.retryable = retryable
        self.fatal = fatal   # خطأ ما ينفع معه إعادة المحاولة (مثل المفتاح غلط)
        self.wait = wait
        self.cap = cap       # أقصى عدد محاولات لهذا النوع من الأخطاء (مثل الرد الفارغ)


def _call_gemini(model, prompt, timeout=150):
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent"
    try:
        r = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}},
            timeout=timeout,
        )
    except requests.Timeout:
        raise GeminiError("انتهت مهلة الاتصال بجيميناي", retryable=True)
    except requests.RequestException as e:
        raise GeminiError(f"مشكلة اتصال بجيميناي ({type(e).__name__})", retryable=True)

    status = r.status_code
    if status == 200:
        try:
            data = r.json()
        except ValueError:
            raise GeminiError("رد غير مفهوم من جيميناي", status=status, retryable=True)
        cands = data.get("candidates") or []
        text = ""
        if cands:
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
        if not text.strip():
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (cands[0].get("finishReason") if cands else "EMPTY")
            raise GeminiError(f"جيميناي رجّع رد فارغ أو محجوب ({reason})", status=status, retryable=True, cap=2)
        return text

    try:
        detail = (r.json().get("error") or {}).get("message", "")
    except ValueError:
        detail = r.text[:200]

    if status == 404:
        raise GeminiError(f"الموديل {model} غير موجود", status=404)
    if status in (401, 403) or (status == 400 and "api key" in detail.lower()):
        raise GeminiError("مفتاح GEMINI_API_KEY غير صحيح أو ما عنده صلاحية", status=status, fatal=True)
    if status == 429:
        wait = None
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except ValueError:
            pass
        if wait is None:  # جيميناي يكتبها داخل الجسم: "retryDelay": "26s"
            try:
                for d in (r.json().get("error") or {}).get("details") or []:
                    if "retryDelay" in d:
                        wait = float(str(d["retryDelay"]).rstrip("s"))
            except (ValueError, TypeError, AttributeError):
                pass
        raise GeminiError("تجاوزنا حد الطلبات المجاني لجيميناي (429)", status=429, retryable=True, wait=wait)
    if status >= 500:
        raise GeminiError(f"سيرفر جوجل مشغول ({status})", status=status, retryable=True)
    raise GeminiError(f"جيميناي رفض الطلب ({status}): {detail[:120]}", status=status)


def translate_text(prompt):
    """يرسل الطلب مع إعادة محاولة ذكية وتبديل الموديل لو غير موجود. تعمل داخل thread."""
    last = None
    for attempt in range(MAX_ATTEMPTS):
        idx = MODEL_STATE["i"]
        model = MODELS[idx]
        try:
            out = _call_gemini(model, prompt)
            out = re.sub(r"^```\w*\n?|\n?```$", "", out.strip())  # نشيل علامات الكود لو جيميناي حطها
            if not ARABIC_RE.search(out):
                raise GeminiError("جيميناي ما رجّع ترجمة عربية", retryable=True, cap=2)
            return out
        except GeminiError as e:
            last = e
            if e.status == 404 and idx < len(MODELS) - 1:
                # عدة دفعات تشتغل بنفس الوقت: ننقل للموديل التالي مرة وحدة فقط
                with MODEL_LOCK:
                    if MODEL_STATE["i"] == idx:
                        log.warning("الموديل %s غير موجود، ننتقل للتالي", model)
                        MODEL_STATE["i"] = idx + 1
                continue
            if e.fatal or not e.retryable or (e.cap and attempt + 1 >= e.cap):
                raise
            delay = e.wait if e.wait else min(2 ** attempt * 2, 30)
            log.warning("محاولة %d فشلت (%s)، ننتظر %.0f ثانية", attempt + 1, e.user_msg, delay)
            time.sleep(min(delay, 60))
    raise last


# ----------------------------------------------------------------------------
# قراءة الـ PDF وتقسيمه إلى فقرات
# ----------------------------------------------------------------------------
class PdfUnreadable(Exception):
    pass


# بداية فقرة جديدة: A. / 1. / 2- / • / -Word
MARKER_RE = re.compile(r"^(?:[A-Z]\.\s+\S|\d{1,2}[.\-)]\s?[A-Z\[(“\"]|[•●▪]|-\s?[A-Z])")


def clean_text(text):
    return " ".join(text.replace(" ", " ").split())


def lines_to_paragraphs(lines):
    """lines = [(text, top, bottom)] بترتيب الصفحة. يرجع قائمة فقرات."""
    lines = [(clean_text(t), a, b) for t, a, b in lines if clean_text(t)]
    if not lines:
        return []
    heights = [b - a for _, a, b in lines if b > a]
    lh = statistics.median(heights) if heights else 10
    paras, cur, prev_bottom = [], [], None
    for text, top, bottom in lines:
        if cur:
            gap = top - prev_bottom
            if gap > 0.45 * lh or MARKER_RE.match(text) or cur[-1].endswith(":"):
                paras.append(" ".join(cur))
                cur = []
        cur.append(text)
        prev_bottom = bottom
    if cur:
        paras.append(" ".join(cur))
    return paras


SPLIT_AT = int(os.getenv("SPLIT_AT", "450"))         # الفقرة الأطول من هذا تنقسم عند نهاية جملة
SENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[\u201c\"(\u2022])")
ABBREV = {"e.g.", "i.e.", "vs.", "fig.", "ph.", "dr.", "mr.", "no.", "approx.", "etc."}


def split_long(text, limit=SPLIT_AT):
    if len(text) <= limit:
        return [text]
    sentences, start = [], 0
    for m in SENT_END.finditer(text):
        last_word = text[start:m.start()].split()[-1].lower() if text[start:m.start()].split() else ""
        if last_word in ABBREV or (len(last_word) <= 2 and last_word[:1].isupper()):
            continue   # اختصار مثل e.g. أو حرف وحيد A. مو نهاية جملة
        sentences.append(text[start:m.start()])
        start = m.end()
    sentences.append(text[start:])
    chunks, cur = [], ""
    for sent in sentences:
        if cur and len(cur) + len(sent) + 1 > limit:
            chunks.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _merge_across_pages(result):
    """فقرة مقطوعة بنهاية صفحة وتكملتها بأول الصفحة اللي بعدها: ندمجهم."""
    merged = []
    for page_no, text in result:
        if (merged and merged[-1][0] == page_no - 1 and text[:1].islower()
                and not re.search(r"[.!?:)\u201d\"]$", merged[-1][1])):
            merged[-1] = (merged[-1][0], merged[-1][1] + " " + text)
        else:
            merged.append((page_no, text))
    return merged


def _page_lines_pdfplumber(page):
    out = []
    for ln in page.extract_text_lines(return_chars=False):
        out.append((ln["text"], float(ln["top"]), float(ln["bottom"])))
    return out


def _page_lines_pypdf(page):
    out, top = [], 0.0
    for raw in (page.extract_text() or "").split("\n"):
        if raw.strip():
            out.append((raw, top, top + 10))
            top += 12
        else:
            top += 12   # سطر فاضي = فاصل فقرة
    return out


def extract_paragraphs(pdf_path):
    """يرجع (قائمة [(رقم_الصفحة, نص_الفقرة)], عدد_صفحات_الملف)."""
    result, total = [], 0
    try:
        if pdfplumber is not None:
            with pdfplumber.open(pdf_path) as pdf:
                total = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    for p in lines_to_paragraphs(_page_lines_pdfplumber(page)):
                        result.append((i + 1, p))
                result = _merge_across_pages(result)
                result = [(n, c) for n, t in result for c in split_long(t)]
        else:
            reader = pypdf.PdfReader(pdf_path)
            if reader.is_encrypted and int(reader.decrypt("")) == 0:
                raise PdfUnreadable("encrypted")
            total = len(reader.pages)
            for i, page in enumerate(reader.pages):
                for p in lines_to_paragraphs(_page_lines_pypdf(page)):
                    result.append((i + 1, p))
            result = _merge_across_pages(result)
            result = [(n, c) for n, t in result for c in split_long(t)]
    except PdfUnreadable:
        raise
    except Exception as e:
        raise PdfUnreadable(type(e).__name__) from e
    return result, total


# ----------------------------------------------------------------------------
# الترجمة: فقرات مرقّمة، كل دفعة بطلب واحد
# ----------------------------------------------------------------------------
PROMPT = (
    "You are a professional translator of university pharmacy lecture notes (English to Arabic).\n"
    "Below are numbered paragraphs. Translate EACH paragraph into clear, accurate Arabic.\n"
    "Rules:\n"
    "- Output exactly one line per paragraph in this form: [n] <Arabic translation>\n"
    "- Keep the same numbers and the same order. Do not merge, split or skip any paragraph.\n"
    "- Keep drug names, abbreviations and symbols (Vd, CL, IV, EC50, GFR...) in English inside the Arabic sentence.\n"
    "- Output only the numbered lines: no explanations, no markdown, no copy of the English text.\n\n"
    "PARAGRAPHS:\n"
)
NUM_RE = re.compile(r"^\s*\[(\d+)\]\s*(.*?)(?=^\s*\[\d+\]|\Z)", re.S | re.M)


def clean_ar(text):
    text = re.sub(r"[*`#_]+", "", text)
    return " ".join(text.split()).strip()


def parse_numbered(raw):
    return {int(n): clean_ar(t) for n, t in NUM_RE.findall(raw)}


def translate_batch(texts):
    """يترجم قائمة فقرات بطلب واحد. يرجع قائمة بنفس الطول (None لو فقرة ما انترجمت)."""
    out = [None] * len(texts)
    pending = list(range(len(texts)))
    for _ in range(3):
        if not pending:
            break
        prompt = PROMPT + "\n".join(f"[{k + 1}] {texts[i]}" for k, i in enumerate(pending))
        got = parse_numbered(translate_text(prompt))
        still = []
        for k, i in enumerate(pending):
            v = got.get(k + 1)
            if v and ARABIC_RE.search(v):
                out[i] = v
            else:
                still.append(i)
        pending = still
    return out


def make_batches(indices, texts, limit=BATCH_LIMIT):
    batches, cur, size = [], [], 0
    for i in indices:
        n = len(texts[i]) + 10
        if cur and size + n > limit:
            batches.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        batches.append(cur)
    return batches


# ----------------------------------------------------------------------------
# بناء ملف PDF: العربي بالأحمر فوق كل فقرة إنجليزية
# ----------------------------------------------------------------------------
RED = (200, 0, 0)
EN_SIZE, AR_SIZE = 12, 12
LH_EN, LH_AR = 5.8, 7.2

# حروف عربية فعلية (بدون علامات الترقيم والأرقام العربية)
AR_LETTER = re.compile(r"[ء-يٱ-ۓﭐ-﷿ﹰ-﻿]")
LAT_CHAR = re.compile(r"[A-Za-z0-9À-ɏͰ-Ͽ]")
TRAIL_PUNCT = re.compile(r"^(.*?)([.,;:!?،؛]+)$")
MIRROR = str.maketrans("()[]{}<>«»", ")(][}{><»«")


def _first_file(cands):
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_font():
    """الخط العربي (Amiri). إجباري لإنشاء PDF."""
    return _first_file([
        os.getenv("FONT_PATH", ""),
        os.path.join(HERE, "Amiri-Regular.ttf"),
        "Amiri-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ])


def find_en_font():
    """خط الإنجليزي (Carlito). اختياري: لو مفقود نستخدم الخط العربي."""
    return _first_file([
        os.getenv("EN_FONT_PATH", ""),
        os.path.join(HERE, "Carlito-Regular.ttf"),
        "Carlito-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ])


def _rev(word):
    return word[::-1].translate(MIRROR)


def visual_line(words):
    """يحوّل كلمات سطر عربي (بترتيبها المنطقي) إلى نص جاهز للرسم من اليسار لليمين.
    المقاطع الإنجليزية تبقى بترتيبها، والعربية تنعكس."""
    # كل عنصر: [النص، الاتجاه المفروض أو None، يلتصق بما بعده؟]
    toks = []
    for k, w in enumerate(words):
        m = TRAIL_PUNCT.match(w)
        if m and LAT_CHAR.search(m.group(1)) and not AR_LETTER.search(w):
            nxt = words[k + 1] if k + 1 < len(words) else ""
            if not nxt or AR_LETTER.search(nxt):
                # علامة الترقيم بآخر كلمة إنجليزية تنتقل لجهة العربي وتلصق بالكلمة
                toks.append([m.group(1), None, False])
                toks.append([m.group(2), "R", True])
                continue
        toks.append([w, None, False])

    for t in toks:   # أولاً الكلمات اللي فيها حروف
        if t[1] is None:
            if AR_LETTER.search(t[0]):
                t[1] = "R"
            elif LAT_CHAR.search(t[0]):
                t[1] = "L"
    for k, t in enumerate(toks):   # ثم علامات الترقيم: تتبع الجهتين لو متفقتين، وإلا الاتجاه العربي
        if t[1] is None:
            before = next((toks[j][1] for j in range(k - 1, -1, -1) if toks[j][1]), "R")
            after = next((toks[j][1] for j in range(k + 1, len(toks)) if toks[j][1]), "R")
            t[1] = "L" if (before == "L" and after == "L") else "R"

    runs = []
    for t in toks:
        if runs and runs[-1][0] == t[1]:
            runs[-1][1].append(t)
        else:
            runs.append([t[1], [t]])

    parts = []   # (نص، يلتصق بما بعده؟)
    for d, ts in reversed(runs):
        if d == "R":
            parts.extend((_rev(t[0]), t[2]) for t in reversed(ts))
        else:
            parts.extend((t[0], t[2]) for t in ts)
    out = ""
    for i, (txt, glue) in enumerate(parts):
        out += txt
        if i < len(parts) - 1 and not glue:
            out += " "
    return out


def wrap_arabic(pdf, text, width):
    shaped = arabic_reshaper.reshape(text.replace("\n", " "))
    words = shaped.split()
    space = pdf.get_string_width(" ")
    lines, cur, cur_w = [], [], 0.0
    for w in words:
        ww = pdf.get_string_width(w)
        add = ww if not cur else ww + space
        if cur and cur_w + add > width:
            lines.append(cur)
            cur, cur_w = [w], ww
        else:
            cur.append(w)
            cur_w += add
    if cur:
        lines.append(cur)
    return [visual_line(l) for l in lines]


def build_pdf(items, out_path):
    """items = [(english, arabic | '' | None, سبب_الفشل)]"""
    font = find_font()
    if not font:
        raise RuntimeError("ملف الخط Amiri-Regular.ttf غير موجود بجانب main.py")
    pdf = FPDF(unit="mm", format="A4")
    pdf.set_margins(left=18, top=16, right=18)
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_font(family="AR", style="", fname=font)
    pdf.add_font(family="EN", style="", fname=find_en_font() or font)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    for en, ar, reason in items:
        pdf.set_font("AR", size=AR_SIZE)
        if ar is None:
            ar_text = "[تعذرت ترجمة هذه الفقرة]"
        else:
            ar_text = ar
        ar_lines = wrap_arabic(pdf, ar_text, width) if ar_text else []

        need = len(ar_lines) * LH_AR + 2 * LH_EN
        if pdf.get_y() + need > pdf.page_break_trigger:
            pdf.add_page()

        pdf.set_font("AR", size=AR_SIZE)
        pdf.set_text_color(*RED)
        for line in ar_lines:
            pdf.cell(width, LH_AR, line, align="R", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("EN", size=EN_SIZE)
        pdf.set_text_color(0, 0, 0)
        pdf.multi_cell(width, LH_EN, en, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3.5)

    pdf.output(out_path)


def build_txt(items, out_path):
    blocks = []
    for en, ar, reason in items:
        top = ar if ar else ("[تعذرت ترجمة هذه الفقرة]" if ar is None else "")
        blocks.append((top + "\n" if top else "") + en)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(blocks))


# ----------------------------------------------------------------------------
# أوامر البوت
# ----------------------------------------------------------------------------
async def safe_edit(bot, message, text):
    try:
        await bot.edit_message_text(chat_id=message.chat_id, message_id=message.message_id, text=text)
    except Exception as e:  # مثل: الرسالة ما تغيرت
        log.debug("edit skipped: %s", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أرسل لي ملف PDF بالإنجليزي وأرجعه لك PDF: الأصل الإنجليزي كما هو، "
        "وفوق كل فقرة الترجمة العربية بالأحمر.\n"
        "اكتب /test لفحص اتصال البوت بجيميناي."
    )


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 جاري فحص الاتصال بجيميناي...")
    if not GEMINI_API_KEY:
        await safe_edit(context.bot, msg, "❌ المتغير GEMINI_API_KEY غير موجود في Railway → Variables")
        return
    try:
        out = await asyncio.to_thread(translate_text, "Translate to Arabic, output only the translation: Good morning")
        pdf_state = "جاهز ✅" if (PDF_LIBS_OK and find_font()) else "ناقص ❌ (مكتبات أو خط Amiri)"
        await safe_edit(context.bot, msg,
                        f"✅ جيميناي يشتغل\nالموديل: {MODELS[MODEL_STATE['i']]}\nالتجربة: {out[:80]}\nإنشاء PDF: {pdf_state}")
    except GeminiError as e:
        await safe_edit(context.bot, msg, f"❌ فشل الاتصال بجيميناي:\n{e.user_msg}")
    except Exception as e:
        log.exception("test failed")
        await safe_edit(context.bot, msg, f"❌ خطأ غير متوقع: {type(e).__name__}")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    doc = message.document
    name = doc.file_name or "file.pdf"

    if not (name.lower().endswith(".pdf") or doc.mime_type == "application/pdf"):
        await message.reply_text("عذراً، أرسل ملف PDF فقط.")
        return
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.reply_text("الملف أكبر من 20 ميجا، تليجرام ما يسمح للبوت يحمّله. قسّمه لأجزاء أصغر.")
        return
    if not GEMINI_API_KEY:
        await message.reply_text("❌ المتغير GEMINI_API_KEY غير موجود في Railway → Variables")
        return

    status = await message.reply_text("⏳ جاري قراءة الملف...")
    workdir = tempfile.mkdtemp(prefix="pdfbot_")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        pdf_path = os.path.join(workdir, "input.pdf")
        await tg_file.download_to_drive(pdf_path)

        paras, total_pages = await asyncio.to_thread(extract_paragraphs, pdf_path)
        if not paras:
            await safe_edit(context.bot, status,
                            "❌ ما لقيت نص داخل الملف. غالباً صفحاته صور (ممسوحة بالسكانر)، وهذا البوت يقرأ النصوص فقط.")
            return

        texts = [t for _, t in paras]
        arabic = [None] * len(texts)       # الترجمة
        reasons = [None] * len(texts)      # سبب الفشل لو فشلت
        for i, t in enumerate(texts):
            if not LETTERS_RE.search(t):   # أرقام أو رموز فقط
                arabic[i] = ""
        todo = [i for i, a in enumerate(arabic) if a is None]
        batches = make_batches(todo, texts)

        await safe_edit(context.bot, status, f"⏳ جاري الترجمة... 0 من {len(batches)} دفعة ({len(texts)} فقرة)")

        state = {"done": 0, "fatal": None}
        sem = asyncio.Semaphore(CONCURRENCY)

        async def work(batch):
            async with sem:
                if state["fatal"]:
                    for i in batch:
                        reasons[i] = state["fatal"]
                else:
                    try:
                        res = await asyncio.to_thread(translate_batch, [texts[i] for i in batch])
                        for i, r in zip(batch, res):
                            if r:
                                arabic[i] = r
                            else:
                                reasons[i] = "رد جيميناي ناقص"
                    except GeminiError as e:
                        if e.fatal:
                            state["fatal"] = e.user_msg
                        for i in batch:
                            reasons[i] = e.user_msg
                    except Exception as e:
                        log.exception("batch failed")
                        for i in batch:
                            reasons[i] = f"خطأ غير متوقع ({type(e).__name__})"
            state["done"] += 1
            await safe_edit(context.bot, status, f"⏳ جاري الترجمة... {state['done']} من {len(batches)} دفعة")

        await asyncio.gather(*(work(b) for b in batches))

        ok = [i for i in todo if arabic[i]]
        bad = [i for i in todo if not arabic[i]]
        if todo and not ok:
            await safe_edit(context.bot, status, f"❌ فشلت الترجمة بالكامل.\nالسبب: {reasons[bad[0]]}")
            return

        items = [(texts[i], arabic[i], reasons[i]) for i in range(len(texts))]
        base = os.path.splitext(os.path.basename(name))[0]
        caption = f"✅ تمت ترجمة {len(ok)} من {len(todo)} فقرة"
        if bad:
            bad_pages = sorted({paras[i][0] for i in bad})
            caption += f"\n⚠️ فقرات فاشلة بالصفحات: {', '.join(map(str, bad_pages))} (أعد إرسال الملف لإعادة المحاولة)"

        await safe_edit(context.bot, status, "⏳ جاري إنشاء ملف PDF...")
        out_path = os.path.join(workdir, "output.pdf")
        out_name = f"translated_{base}.pdf"
        try:
            if not PDF_LIBS_OK:
                raise RuntimeError("مكتبات fpdf2 / arabic-reshaper غير مثبتة")
            await asyncio.to_thread(build_pdf, items, out_path)
        except Exception as e:
            log.exception("PDF build failed, falling back to TXT")
            out_path = os.path.join(workdir, "output.txt")
            out_name = f"translated_{base}.txt"
            await asyncio.to_thread(build_txt, items, out_path)
            caption += f"\n⚠️ تعذر إنشاء PDF ({str(e)[:80]}) فأرسلت لك ملف نصي بدله"

        with open(out_path, "rb") as f:
            await message.reply_document(document=f, filename=out_name, caption=caption)
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=status.message_id)
        except Exception:
            pass

    except PdfUnreadable as e:
        log.warning("pdf unreadable: %s", e)
        await safe_edit(context.bot, status, "❌ ما قدرت أقرأ الملف. يبدو تالف أو محمي بكلمة سر، جرّب تفتحه وتحفظه من جديد.")
    except Exception as e:
        log.exception("handle_pdf failed")
        await safe_edit(context.bot, status, f"❌ حدث خطأ أثناء المعالجة ({type(e).__name__}): {str(e)[:150]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def on_error(update, context):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


# ----------------------------------------------------------------------------
# التشغيل
# ----------------------------------------------------------------------------
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN is missing!")
        return
    log.info("Gemini key present: %s | models: %s | pdf libs: %s | font: %s",
             bool(GEMINI_API_KEY), MODELS, PDF_LIBS_OK, find_font())

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)   # يخلي /start و /test يردون حتى لو فيه ملف قاعد يترجم
        .read_timeout(60)
        .write_timeout(120)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
    app.add_error_handler(on_error)

    log.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
