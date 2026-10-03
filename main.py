import os
import time
import logging
import tempfile
import telebot
import pymupdf
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pdf-bot")

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN is missing in Railway Variables")

MAX_FILE_MB = 20
CHUNK_SIZE = 4500

bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=4)


def split_text(text, limit=CHUNK_SIZE):
    chunks = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


def translate_chunk(translator, chunk, retries=3):
    if not chunk.strip():
        return chunk
    for attempt in range(retries):
        try:
            return translator.translate(chunk) or ""
        except Exception as e:
            log.warning("Translate retry %s: %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1))
    return "[تعذرت ترجمة هذا الجزء]\n" + chunk


def safe_edit(chat_id, msg_id, text):
    try:
        bot.edit_message_text(text, chat_id, msg_id)
    except Exception:
        pass


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "أهلاً بك 👋\nأرسل لي ملف PDF وسأترجمه إلى العربية "
        "وأرجعه لك كملف نصي.",
    )


@bot.message_handler(content_types=["document"])
def handle_pdf(message):
    doc = message.document
    name = doc.file_name or "file.pdf"

    if not name.lower().endswith(".pdf"):
        bot.reply_to(message, "من فضلك أرسل ملف PDF فقط.")
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        bot.reply_to(message, f"الملف كبير. الحد الأقصى {MAX_FILE_MB} ميجا.")
        return

    status = bot.reply_to(message, "⏳ جاري تحميل الملف...")
    chat_id = message.chat.id
    out_path = None

    try:
        info = bot.get_file(doc.file_id)
        data = bot.download_file(info.file_path)

        pdf = pymupdf.open(stream=data, filetype="pdf")
        text = "\n".join(page.get_text() for page in pdf)
        pdf.close()

        if not text.strip():
            safe_edit(
                chat_id,
                status.message_id,
                "لم أجد نصاً في الملف. قد يكون صوراً ممسوحة ضوئياً.",
            )
            return

        chunks = split_text(text)
        total = len(chunks)
        translator = GoogleTranslator(source="auto", target="ar")
        results = []

        for i, chunk in enumerate(chunks, start=1):
            results.append(translate_chunk(translator, chunk))
            if i % 3 == 0 or i == total:
                safe_edit(
                    chat_id,
                    status.message_id,
                    f"⏳ جاري الترجمة... {i}/{total}",
                )

        out_name = os.path.splitext(name)[0] + "_ar.txt"
        out_path = os.path.join(tempfile.gettempdir(), out_name)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(results))

        with open(out_path, "rb") as f:
            bot.send_document(chat_id, f, caption="✅ تمت الترجمة")

        safe_edit(chat_id, status.message_id, "✅ انتهيت.")

    except Exception as e:
        log.exception("Failed to process PDF: %s", e)
        safe_edit(chat_id, status.message_id, "❌ حدث خطأ أثناء المعالجة.")
    finally:
        if out_path and os.path.exists(out_path):
            os.remove(out_path)


@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.reply_to(message, "أرسل لي ملف PDF لأترجمه.")


if __name__ == "__main__":
    me = bot.get_me()
    log.info("Bot started as @%s", me.username)
    bot.remove_webhook()
    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30,
    )
