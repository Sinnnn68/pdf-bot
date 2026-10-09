import os
import io
import re
import json
import time
import hashlib
import threading
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests
import telebot
import fitz  # PyMuPDF
import arabic_reshaper
from fpdf import FPDF

try:
    from bidi import get_display
except ImportError:  # نسخ قديمة من المكتبة
    from bidi.algorithm import get_display

# ======================================================================
# الإعدادات (كلها تتقرأ من Railway Variables)
# ======================================================================
def env(name, default=""):
    return (os.environ.get(name) or default).strip()


BOT_TOKEN = env("BOT_TOKEN") or env("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = env("GEMINI_API_KEY")
GROQ_API_KEY = env("GROQ_API_KEY")


def split_list(value):
    return [x.strip() for x in value.split(",") if x.strip()]


GEMINI_MODELS = split_list(env(
    "GEMINI_MODELS",
    "gemini-flash-latest,gemini-flash-lite-latest,gemini-2.5-flash-lite"))
GROQ_MODELS = split_list(
    env("GROQ_MODELS") or env("GROQ_MODEL") or
    "openai/gpt-oss-120b,openai/gpt-oss-20b,meta-llama/llama-4-scout-17b-16e-instruct")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "Amiri-Regular.ttf")

MAX_PAGES = int(env("MAX_PAGES", "80"))
WORKERS = int(env("WORKERS", "3"))
BATCH_ITEMS = 20
BATCH_CHARS = 3500
FAILED_TEXT = "[تعذرت ترجمة هذه الفقرة]"

# ======================================================================
# الترجمة: Gemini ثم Groq ثم Google (احتياط أخير)
# ======================================================================
class ProviderError(Exception):
    def __init__(self, status, message):
        super().__init__("HTTP %s %s" % (status, message))
        self.status = status


class BadOutput(Exception):
    pass


class TranslationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors[-6:]))
        self.errors = errors


PROMPT = (
    "Translate every item of the following JSON array from English to Arabic.\n"
    "Rules:\n"
    "- Use clear academic Arabic suitable for university students.\n"
    "- Keep drug names, scientific terms, abbreviations, numbers and formulas "
    "in English (you may add the Arabic meaning next to them).\n"
    "- Do not skip, merge or add items.\n"
    "- Return ONLY a JSON array of strings with exactly {n} items, in the same order.\n\n"
    "{data}"
)

DEAD = set()          # موديلات/مزودات ثبت أنها لا تشتغل (حتى ما نضيع وقت)
DEAD_LOCK = threading.Lock()
CACHE = {}            # نص إنجليزي -> ترجمة (يحفظ الفقرات المترجمة)


def parse_json_array(text, n):
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise BadOutput("no JSON array in reply")
    try:
        arr = json.loads(text[start:end + 1])
    except Exception as e:
        raise BadOutput("invalid JSON: %s" % e)
    if not isinstance(arr, list) or len(arr) != n:
        raise BadOutput("expected %d items, got %s" % (
            n, len(arr) if isinstance(arr, list) else "non-list"))
    return [str(x).strip() for x in arr]


def call_gemini(model, items):
    prompt = PROMPT.format(n=len(items), data=json.dumps(items, ensure_ascii=False))
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    r = requests.post(url, json=body, timeout=120, headers={
        "x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise ProviderError(r.status_code, r.text[:200].replace("\n", " "))
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except Exception:
        raise BadOutput("empty Gemini reply")
    return parse_json_array(text, len(items))


def call_groq(model, items):
    prompt = PROMPT.format(n=len(items), data=json.dumps(items, ensure_ascii=False))
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions", timeout=120,
        headers={"Authorization": "Bearer " + GROQ_API_KEY,
                 "Content-Type": "application/json"},
        json={"model": model, "temperature": 0.2,
              "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise ProviderError(r.status_code, r.text[:200].replace("\n", " "))
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        raise BadOutput("empty Groq reply")
    return parse_json_array(text, len(items))


def call_google_free(items):
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="en", target="ar")
    out = []
    for t in items:
        out.append(tr.translate(t[:4500]) or "")
        time.sleep(0.4)
    return out


def candidates():
    if GEMINI_API_KEY:
        for m in GEMINI_MODELS:
            yield "gemini(%s)" % m, (lambda items, m=m: call_gemini(m, items))
    if GROQ_API_KEY:
        for m in GROQ_MODELS:
            yield "groq(%s)" % m, (lambda items, m=m: call_groq(m, items))


def translate_batch(items):
    """يترجم قائمة نصوص. يرجع قائمة بنفس الطول أو يرمي TranslationError."""
    errors = []
    for name, fn in candidates():
        if name in DEAD:
            continue
        for attempt in range(3):
            try:
                return fn(items)
            except BadOutput as e:
                if len(items) > 1:  # قسّم الدفعة نصفين وجرب من جديد
                    mid = len(items) // 2
                    return translate_batch(items[:mid]) + translate_batch(items[mid:])
                errors.append("%s: %s" % (name, e))
                break
            except ProviderError as e:
                if e.status in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                errors.append("%s: %s" % (name, e))
                if e.status in (400, 401, 403, 404):
                    with DEAD_LOCK:
                        DEAD.add(name)
                break
            except Exception as e:
                errors.append("%s: %s: %s" % (name, type(e).__name__, str(e)[:100]))
                break
    try:
        return call_google_free(items)
    except Exception as e:
        errors.append("google: %s: %s" % (type(e).__name__, str(e)[:100]))
    raise TranslationError(errors)


def make_batches(texts):
    batches, cur, size = [], [], 0
    for t in texts:
        if cur and (len(cur) >= BATCH_ITEMS or size + len(t) > BATCH_CHARS):
            batches.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t)
    if cur:
        batches.append(cur)
    return batches


def translate_all(texts, progress=None):
    """يرجع (قاموس الترجمات, قائمة الأخطاء)."""
    pending = [t for t in dict.fromkeys(texts) if t not in CACHE]
    batches = make_batches(pending)
    errors, done = [], [0]

    def work(batch):
        try:
            res = translate_batch(batch)
            for src, dst in zip(batch, res):
                CACHE[src] = dst
        except TranslationError as e:
            errors.extend(e.errors)
        done[0] += 1
        if progress:
            progress(done[0], len(batches))

    with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as ex:
        list(ex.map(work, batches))
    return {t: CACHE.get(t) for t in texts}, errors


# ======================================================================
# قراءة الـ PDF
# ======================================================================
def extract_blocks(page):
    blocks = []
    for b in page.get_text("blocks"):
        if len(b) >= 7 and b[6] != 0:  # نتجاهل الصور
            continue
        text = re.sub(r"\s+", " ", b[4] or "").strip()
        if len(text) < 2 or not re.search(r"[A-Za-z]", text):
            continue
        blocks.append((round(b[1]), round(b[0]), text))
    blocks.sort()
    return [t for _, _, t in blocks]


# ======================================================================
# بناء الـ PDF النهائي
# ======================================================================
def shape(s):
    return get_display(arabic_reshaper.reshape(s))


def wrap(pdf, text, max_w, rtl=False):
    lines, cur = [], ""
    for w in text.split():
        test = (cur + " " + w).strip()
        width = pdf.get_string_width(shape(test) if rtl else test)
        if width <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def build_pdf(src_doc, page_texts, translations):
    """كل شريحة أصلية (بصورها) تبقى كما هي، وبعدها صفحة ترجمة:
    الفقرة الإنجليزية ثم تحتها الترجمة العربية بالأحمر."""
    margin = 15
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=margin)
    pdf.set_margins(margin, margin, margin)
    pdf.add_font("Amiri", "", FONT_PATH)
    width = pdf.w - 2 * margin

    ranges = {}
    for idx, paras in page_texts.items():
        if not paras:
            continue
        pdf.add_page()
        first = pdf.page_no()
        pdf.set_font("Amiri", "", 9)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(width, 6, "Slide %d" % (idx + 1), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        for p in paras:
            pdf.set_font("Amiri", "", 11)
            pdf.set_text_color(0, 0, 0)
            for line in wrap(pdf, p, width):
                pdf.cell(width, 5.8, line, new_x="LMARGIN", new_y="NEXT")
            ar = translations.get(p) or FAILED_TEXT
            pdf.set_font("Amiri", "", 14)
            pdf.set_text_color(200, 0, 0)
            for line in wrap(pdf, ar, width, rtl=True):
                pdf.cell(width, 8, shape(line), align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
        ranges[idx] = (first - 1, pdf.page_no() - 1)

    trans_doc = fitz.open(stream=bytes(pdf.output()), filetype="pdf") if ranges else None
    out = fitz.open()
    for i in range(len(src_doc)):
        out.insert_pdf(src_doc, from_page=i, to_page=i)
        if i in ranges:
            a, b = ranges[i]
            out.insert_pdf(trans_doc, from_page=a, to_page=b)
    return out


def process_pdf(path, progress=None):
    """يرجع (مسار الملف الناتج, قائمة الأخطاء, عدد الفقرات الفاشلة)."""
    src = fitz.open(path)
    if len(src) > MAX_PAGES:
        raise ValueError("الملف كبير (%d صفحة). الحد الأقصى %d." % (len(src), MAX_PAGES))
    page_texts = {i: extract_blocks(src[i]) for i in range(len(src))}
    all_texts = [t for paras in page_texts.values() for t in paras]
    if not all_texts:
        raise ValueError("ما لقيت نص بالملف (يمكن صور ممسوحة ضوئياً).")
    translations, errors = translate_all(all_texts, progress)
    failed = sum(1 for t in all_texts if not translations.get(t))
    if failed == len(all_texts):
        raise TranslationError(errors)
    out = build_pdf(src, page_texts, translations)
    fd, out_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    out.save(out_path, garbage=3, deflate=True)
    return out_path, errors, failed


# ======================================================================
# بوت تيليجرام
# ======================================================================
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN غير موجود بـ Railway Variables")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)


def short(text, limit=3500):
    return text if len(text) <= limit else text[:limit] + "..."


@bot.message_handler(commands=["start", "help"])
def on_start(m):
    bot.reply_to(m, "هلا! ابعثلي ملف PDF (محاضرة بالإنكليزي) وأرجعلك نفس الملف "
                    "بصوره، وبعد كل شريحة صفحة فيها الفقرات الإنجليزية "
                    "والترجمة العربية بالأحمر تحتها.\n\n"
                    "لفحص المفاتيح والموديلات اكتب: /test")


@bot.message_handler(commands=["test"])
def on_test(m):
    lines = ["BOT_TOKEN: موجود",
             "GEMINI_API_KEY: " + ("موجود" if GEMINI_API_KEY else "❌ ناقص"),
             "GROQ_API_KEY: " + ("موجود" if GROQ_API_KEY else "❌ ناقص"),
             "الخط Amiri: " + ("موجود" if os.path.exists(FONT_PATH) else "❌ ملف Amiri-Regular.ttf ناقص"),
             ""]
    bot.reply_to(m, "\n".join(lines) + "جاري فحص الموديلات...")
    results = []
    for name, fn in candidates():
        try:
            out = fn(["Hello, how are you?"])
            results.append("✅ %s → %s" % (name, out[0][:40]))
        except Exception as e:
            results.append("❌ %s → %s" % (name, str(e)[:120]))
    if not results:
        results.append("ما أكو أي مفتاح (Gemini أو Groq) مضبوط.")
    bot.send_message(m.chat.id, short("\n".join(results)))


@bot.message_handler(content_types=["document"])
def on_document(m):
    threading.Thread(target=handle_document, args=(m,), daemon=True).start()


def handle_document(m):
    doc = m.document
    name = doc.file_name or "file.pdf"
    if not name.lower().endswith(".pdf"):
        bot.reply_to(m, "ابعث ملف بصيغة PDF بس.")
        return
    status = bot.reply_to(m, "⏳ استلمت الملف، جاري القراءة...")

    def edit(txt):
        try:
            bot.edit_message_text(txt, status.chat.id, status.message_id)
        except Exception:
            pass

    in_path = out_path = None
    try:
        info = bot.get_file(doc.file_id)
        data = bot.download_file(info.file_path)
        fd, in_path = tempfile.mkstemp(suffix=".pdf")
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        last = [0]

        def progress(done, total):
            if time.time() - last[0] > 4 or done == total:
                last[0] = time.time()
                edit("⏳ الترجمة... %d من %d" % (done, total))

        out_path, errors, failed = process_pdf(in_path, progress)
        edit("✅ خلصت الترجمة، جاري الإرسال...")
        with open(out_path, "rb") as f:
            bot.send_document(m.chat.id, f, visible_file_name="translated_" + name)
        if failed:
            bot.send_message(m.chat.id, "⚠️ %d فقرة ما انترجمت (مكتوب مكانها %s). "
                                        "ابعث الملف مرة ثانية وراح يكمل الناقص."
                             % (failed, FAILED_TEXT))
    except TranslationError as e:
        uniq = list(dict.fromkeys(e.errors))
        edit(short("❌ فشلت الترجمة بالكامل. الأسباب:\n" + "\n".join(uniq)))
    except ValueError as e:
        edit("❌ " + str(e))
    except Exception as e:
        traceback.print_exc()
        edit(short("❌ صار خطأ: %s: %s" % (type(e).__name__, str(e)[:300])))
    finally:
        for p in (in_path, out_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


@bot.message_handler(func=lambda m: True)
def on_other(m):
    bot.reply_to(m, "ابعث ملف PDF حتى أترجمه، أو اكتب /test لفحص الإعدادات.")


if __name__ == "__main__":
    print("Bot starting. Gemini models:", GEMINI_MODELS, "Groq models:", GROQ_MODELS)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
