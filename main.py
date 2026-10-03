import os
import sys
import logging
import fitz  # PyMuPDF
from googletrans import Translator
import telebot

# إعدادات السجلات لمراقبة البوت مباشرة في Railway Logs
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("خطأ قاتل: متغير BOT_TOKEN غير موجود في إعدادات البيئة!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
translator = Translator()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    logger.info(f"تم استقبال أمر Start من المستخدم: {message.chat.id}")
    bot.reply_to(message, "أهلاً بك يا غالي! 🌟 أرسل لي ملف PDF وسأترجمه لك إلى العربية فوراً.")

@bot.message_handler(content_types=['document'])
def handle_pdf(message):
    temp_pdf = None
    output_txt = None
    try:
        bot.reply_to(message, "⏳ جاري تحميل الملف ومعالجة صفحاته للترجمة...")
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        temp_pdf = f"temp_{message.chat.id}.pdf"
        with open(temp_pdf, 'wb') as f:
            f.write(downloaded_file)

        doc = fitz.open(temp_pdf)
        translated_text = "=== تقرير الترجمة ===\n"

        for i, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                try:
                    tr = translator.translate(text, dest='ar')
                    translated_text += f"\n\n--- صفحة {i + 1} ---\n" + tr.text
                except Exception as sub_e:
                    logger.warning(f"خطأ في ترجمة الصفحة {i + 1}: {sub_e}")

        doc.close()

        output_txt = f"translated_{message.chat.id}.txt"
        with open(output_txt, 'w', encoding='utf-8') as f:
            f.write(translated_text)

        with open(output_txt, 'rb') as f:
            bot.send_document(message.chat.id, f, caption="✅ إليك ملف الترجمة جاهزاً!")

    except Exception as e:
        logger.error(f"خطأ أثناء المعالجة: {e}")
        bot.reply_to(message, f"❌ حدث خطأ تقني أثناء معالجة الملف: {str(e)}")

    finally:
        if temp_pdf and os.path.exists(temp_pdf):
            os.remove(temp_pdf)
        if output_txt and os.path.exists(output_txt):
            os.remove(output_txt)

if __name__ == "__main__":
    logger.info("البوت يعمل الآن في وضع الاستماع المستمر (Polling)... جاهز لتلقي الرسائل.")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
