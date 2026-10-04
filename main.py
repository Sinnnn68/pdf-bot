import os
import time
import logging
import tempfile

import requests
import telebot
from deep_translator import GoogleTranslator

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

from fpdf import FPDF
import arabic_reshaper

try:
    from bidi import get_display
except ImportError:  # older python-bidi
    from bidi.algorithm import get_display

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pdf-bot")

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN is missing. Add it in Railway > Variables.")

# Free key from https://aistudio.google.com/apikey
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODELS = [
    m for m in [
        os.environ.get("GEMINI_MODEL"),
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-flash-latest",
    ] if m
]

bot = telebot.TeleBot(TOKEN)

MAX_FILE_MB = 20
MAX_PAGES = 300
GOOGLE_CHUNK = 4000
BATCH_CHARS = 6000

state = {"google_ok": True, "last_error": ""}

# Arabic font used to build the PDF (downloaded once, when first needed)
FONT_PATH = os.path.join(tempfile.gettempdir(), "arabic_font.ttf")
FONT_URLS = [
    "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf",
    "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf",
    "https://raw.githubusercontent.com/notofonts/arabic/main/fonts/NotoNaskhArabic/hinted/ttf/NotoNaskhArabic-Regular.ttf",
]


# ---------- translation ----------

def split_text(text, size=GOOGLE_CHUNK):
    chunks, current = [], ""
    for line in text.splitlines():
        while len(line) > size:
            cut = line.rfind(" ", 0, size)
            if cut <= 0:
                cut = size
            piece, line = line[:cut], line[cut:].lstrip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        if len(current) + len(line) + 1 > size:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current.strip():
        chunks.append(current)
    return chunks


def translate_gemini(text):
    prompt = (
        "Translate the following text into Arabic. "
        "Keep the same line breaks, numbering and the page marker lines "
        "(like '--- Page 3 ---'). For medical or scientific terms, write the "
        "Arabic term followed by the English term in parentheses. "
        "Output only the translation, no explanations.\n\n" + text
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(4):
            try:
                r = requests.post(
                    url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=120
                )
            except Exception as e:
                state["last_error"] = f"Gemini network error: {e}"
                log.warning(state["last_error"])
                time.sleep(5)
                continue

            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    out = "".join(p.get("text", "") for p in parts).strip()
                    if out:
                        time.sleep(4)
                        return out
                    state["last_error"] = "Gemini returned empty text"
                except Exception:
                    state["last_error"] = f"Gemini bad response: {r.text[:200]}"
                log.warning(state["last_error"])
                break

            state["last_error"] = f"Gemini {model} error {r.status_code}: {r.text[:200]}"
            log.warning(state["last_error"])
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            if r.status_code in (400, 401, 403, 404):
                break
            time.sleep(5)
    return None


def translate_chunk_google(chunk, retries=3):
    if not chunk.strip():
        return chunk
    for attempt in range(retries):
        try:
            result = GoogleTranslator(source="auto", target="ar").translate(chunk)
            time.sleep(0.7)
            return result
        except Exception as e:
            state["last_error"] = f"Google translate error: {e}"
            log.warning("google translate failed (try %s): %s", attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    return None


def translate_text(text):
    if GEMINI_KEY:
        out = translate_gemini(text)
        if out:
            return out

    if not state["google_ok"]:
        return None
    parts = []
    for c in split_text(text):
        t = translate_chunk_google(c)
        if t is None:
            state["google_ok"] = False
            return None
        parts.append(t)
    return "\n".join(parts)


# ---------- building the Arabic PDF ----------

def ensure_font():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000:
        return
    for url in FONT_URLS:
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and len(r.content) > 50000:
                with open(FONT_PATH, "wb") as f:
                    f.write(r.content)
                return
            log.warning("font download %s -> %s", url, r.status_code)
        except Exception as e:
            log.warning("font download failed %s: %s", url, e)
    raise RuntimeError("could not download an Arabic font")


def shape(text):
    """Connect Arabic letters and put them in right-to-left display order."""
    return get_display(arabic_reshaper.reshape(text))


def wrap_paragraph(pdf, para, max_w):
    lines, current = [], ""
    for word in para.split(" "):
        test = (current + " " + word).strip()
        if not current or pdf.get_string_width(shape(test)) <= max_w:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build_pdf(text, out_path):
    ensure_font()
    pdf = FPDF(format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("Ar", "", FONT_PATH)
    pdf.add_page()
    pdf.set_font("Ar", size=14)
    max_w = pdf.w - pdf.l_margin - pdf.r_margin

    for para in text.split("\n"):
        para = para.strip()
        if not para:
            pdf.ln(3)
            continue
        if para.startswith("---") and para.endswith("---"):
            pdf.ln(4)  # a little space before each page title
        for line in wrap_paragraph(pdf, para, max_w):
            pdf.cell(max_w, 8, shape(line), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.output(out_path)


# ---------- telegram ----------

def safe_edit(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception:
        pass


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "أهلاً! 👋\nأرسل لي ملف PDF وأترجمه لك للعربية، وأرجعه لك كملف PDF.\n\n"
        "ملاحظة: الملف لازم يكون نص (مو صور ممسوحة بالسكانر).",
    )


@bot.message_handler(content_types=["document"])
def handle_pdf(message):
    doc = message.document
    is_pdf = (doc.mime_type == "application/pdf") or (
        doc.file_name and doc.file_name.lower().endswith(".pdf")
    )
    if not is_pdf:
        bot.reply_to(message, "أرسل ملف بصيغة PDF فقط من فضلك.")
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        bot.reply_to(message, f"الملف كبير. الحد الأقصى {MAX_FILE_MB} ميجابايت.")
        return

    status = bot.reply_to(message, "⏳ استلمت الملف، أبدأ القراءة...")
    chat_id, status_id = message.chat.id, status.message_id
    state["google_ok"] = True

    try:
        file_info = bot.get_file(doc.file_id)
        data = bot.download_file(file_info.file_path)

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "input.pdf")
            with open(pdf_path, "wb") as f:
                f.write(data)

            pdf = fitz.open(pdf_path)
            total_pages = len(pdf)
            if total_pages > MAX_PAGES:
                safe_edit(chat_id, status_id,
                          f"الملف طويل ({total_pages} صفحة). الحد الأقصى {MAX_PAGES} صفحة.")
                return

            pages_text = [page.get_text("text") for page in pdf]
            pdf.close()

            if sum(len(t.strip()) for t in pages_text) < 20:
                safe_edit(
                    chat_id, status_id,
                    "❌ ما لقيت نص داخل الملف. غالباً هو صور ممسوحة بالسكانر.\n"
                    "جرّب ملف نصي، أو حوّله بأداة OCR أولاً.",
                )
                return

            batches, current = [], ""
            for i, t in enumerate(pages_text, start=1):
                block = f"--- Page {i} ---\n{t.strip() or '(no text)'}\n\n"
                if current and len(current) + len(block) > BATCH_CHARS:
                    batches.append(current)
                    current = ""
                current += block
            if current:
                batches.append(current)

            results, failed = [], 0
            for n, batch in enumerate(batches, start=1):
                safe_edit(chat_id, status_id, f"🔄 الترجمة: جزء {n} من {len(batches)}")
                translated = translate_text(batch)
                if translated is None:
                    failed += 1
                    results.append("[تعذرت ترجمة هذا الجزء]\n" + batch)
                else:
                    results.append(translated)

            if failed == len(batches):
                err = (state["last_error"] or "unknown")[:300]
                safe_edit(chat_id, status_id,
                          "❌ فشلت الترجمة لكل الأجزاء.\nالسبب:\n" + err)
                return

            full_text = "\n\n".join(results)
            base = (doc.file_name or "file").rsplit(".", 1)[0]
            caption = "✅ تمت الترجمة"
            if failed:
                caption = f"⚠️ تمت الترجمة، لكن {failed} من {len(batches)} أجزاء ما انترجمت."

            safe_edit(chat_id, status_id, "📄 أجهّز ملف PDF...")
            sent = False
            try:
                out_pdf = os.path.join(tmp, base + "_ar.pdf")
                build_pdf(full_text, out_pdf)
                with open(out_pdf, "rb") as f:
                    bot.send_document(chat_id, f, visible_file_name=base + "_ar.pdf",
                                      caption=caption)
                sent = True
            except Exception as e:
                log.exception("pdf build failed")
                caption = f"⚠️ تعذر إنشاء PDF ({str(e)[:120]}). هذا ملف نصي بدلاً منه."

            if not sent:
                out_txt = os.path.join(tmp, base + "_ar.txt")
                with open(out_txt, "w", encoding="utf-8") as f:
                    f.write(full_text)
                with open(out_txt, "rb") as f:
                    bot.send_document(chat_id, f, visible_file_name=base + "_ar.txt",
                                      caption=caption)
            safe_edit(chat_id, status_id, "✅ انتهيت!")

    except Exception as e:
        log.exception("error while processing pdf")
        safe_edit(chat_id, status_id, f"❌ صار خطأ: {e}")


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.reply_to(message, "أرسل لي ملف PDF لأترجمه 📄")


if __name__ == "__main__":
    bot.remove_webhook()
    log.info("Bot started (Gemini: %s)", "on" if GEMINI_KEY else "off")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
