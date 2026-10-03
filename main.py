import os
import sys
import logging
import telebot
from deep_translator import GoogleTranslator

try:
    import pymupdf as fitz
except ImportError:
    import fitz

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)
telebot.logger.setLevel(logging.DEBUG)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    logger.error("BOT_TOKEN غير موجود!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)


def translate_text(text):
    tr = GoogleTranslator(source="auto", target="ar")
    parts = [text[i:i + 4500] for i in range(0, len(text), 4500)]
    return "\n".join(tr.translate(p) or "" for p in parts)


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    logger.info(f"استقبلت /start من: {message.chat.id}")
    bot.reply_to(message, "أهلاً بك! 🌟 أرسل لي ملف PDF وسأترجمه للعربية.")


@bot.message_handler(content_types=["document"])
def handle_pdf(message):
    temp_pdf = None
    output_txt = None
    try:
        bot.reply_to(message, "⏳ جاري المعالجة...")
        file_info = bot.get_file(message.document.file_id)
        data = bot.download_file(file_info.file_path)

        temp_pdf = f"temp_{message.chat.id}.pdf"
        with open(temp_pdf, "wb") as f:
            f.write(data)

        doc = fitz.open(temp_pdf)
        result = "=== تقرير الترجمة ===\n"
        for i, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                try:
                    result += f"\n\n--- صفحة {i + 1} ---\n" + translate_text(text)
                except Exception as e:
                    logger.warning(f"خطأ في ترجمة الصفحة {i + 1}: {e}")
        doc.close()

        output_txt = f"translated_{message.chat.id}.txt"
        with open(output_txt, "w", encoding="utf-8") as f:
            f.write(result)
        with open(output_txt, "rb") as f:
            bot.send_document(message.chat.id, f, caption="✅ الترجمة جاهزة!")
    except Exception as e:
        logger.error(f"خطأ: {e}")
        bot.reply_to(message, f"❌ حدث خطأ: {e}")
    finally:
        for p in (temp_pdf, output_txt):
            if p and os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    me = bot.get_me()
    logger.info(f"متصل كـ @{me.username}")
    bot.remove_webhook()
    logger.info("بدأ الاستماع للرسائل...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
