import os
import time
import logging
import tempfile

import telebot
import fitz  # PyMuPDF
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pdf-bot")

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN is missing. Add it in Railway > Variables.")

bot = telebot.TeleBot(TOKEN)

MAX_FILE_MB = 20      # Telegram bots can only download files up to 20 MB
MAX_PAGES = 300       # safety limit
CHUNK_SIZE = 4000     # Google translate limit is ~5000 chars per request


def split_text(text, size=CHUNK_SIZE):
    """Split text into pieces smaller than `size`, keeping lines together."""
    chunks, current = [], ""
    for line in text.splitlines():
        # a single very long line: cut it by words
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


def translate_chunk(chunk, retries=3):
    if not chunk.strip():
        return chunk
    for attempt in range(retries):
        try:
            result = GoogleTranslator(source="auto", target="ar").translate(chunk)
            time.sleep(0.7)  # small pause so Google does not block us
            return result
        except Exception as e:
            log.warning("translate failed (try %s): %s", attempt + 1, e)
            time.sleep(5 * (attempt + 1))  # wait longer after each failure
    return "[تعذرت ترجمة هذا الجزء]\n" + chunk


def safe_edit(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception:
        pass


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "أهلاً! 👋\nأرسل لي ملف PDF وأترجمه لك للعربية، وأرجعه لك كملف نصي.\n\n"
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
                safe_edit(chat_id, status_id, f"الملف طويل ({total_pages} صفحة). الحد الأقصى {MAX_PAGES} صفحة.")
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

            results = []
            for i, text in enumerate(pages_text, start=1):
                if text.strip():
                    translated = "\n".join(translate_chunk(c) for c in split_text(text))
                else:
                    translated = "(صفحة بدون نص)"
                results.append(f"--- صفحة {i} ---\n{translated}\n")

                if i % 3 == 0 or i == total_pages:
                    safe_edit(chat_id, status_id, f"🔄 الترجمة: {i} من {total_pages} صفحة")

            out_name = (doc.file_name or "file").rsplit(".", 1)[0] + "_ar.txt"
            out_path = os.path.join(tmp, out_name)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(results))

            with open(out_path, "rb") as f:
                bot.send_document(chat_id, f, visible_file_name=out_name,
                                  caption="✅ تمت الترجمة")
            safe_edit(chat_id, status_id, "✅ انتهيت!")

    except Exception as e:
        log.exception("error while processing pdf")
        safe_edit(chat_id, status_id, f"❌ صار خطأ: {e}")


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.reply_to(message, "أرسل لي ملف PDF لأترجمه 📄")


if __name__ == "__main__":
    bot.remove_webhook()
    log.info("Bot started")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
