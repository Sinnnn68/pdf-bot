import os
import sys
import logging
import fitz  # PyMuPDF
from googletrans import Translator
import telebot

# إعداد نظام تتبع الأخطاء لنعرف أين المشكلة في سجلات Railway
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

logger.info("--- بدأ تشغيل التطبيق وجاري التحقق من التوكن ---")

# قراءة التوكن من المتغيرات البيئية في Railway
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.error("خطأ كبير: متغير BOT_TOKEN غير موجود! يجب إضافته في إعدادات Variables في منصة Railway.")
    sys.exit(1)
else:
    logger.info("تم العثور على توكن البوت بنجاح.")

# تهيئة البوت والمترجم
bot = telebot.TeleBot(BOT_TOKEN)
translator = Translator()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    logger.info(f"استقبلنا أمر بدء من المستخدم: {message.chat.id}")
    bot.reply_to(message, "أهلاً بك! أرسل لي ملف PDF وسأقوم بترجمته إلى اللغة العربية فوراً.")

@bot.message_handler(content_types=['document'])
def handle_pdf(message):
    try:
        logger.info("تم استلام ملف PDF جديد، جاري التحميل والمعالجة...")
        bot.reply_to(message, "جاري تحميل الملف ومعالجة الترجمة، انتظر قليلاً...")
        
        # تحميل الملف من تليجرام
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        temp_pdf_path = "temp.pdf"
        with open(temp_pdf_path, 'wb') as new_file:
            new_file.write(downloaded_file)
            
        # قراءة النصوص وترجمتها
        doc = fitz.open(temp_pdf_path)
        translated_text = ""
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            if text.strip():
                translation = translator.translate(text, dest='ar')
                translated_text += f"\n--- صفحة {page_num + 1} ---\n" + translation.text

        # حفظ النص المترجم في ملف نصي وإرساله للمستخدم
        output_path = "translated.txt"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(translated_text)
            
        with open(output_path, 'rb') as f:
            bot.send_document(message.chat.id, f, caption="إليك ملف الترجمة جاهزاً!")
            
        logger.info("تم إنجاز الترجمة وإرسال الملف بنجاح.")
        
        doc.close()
        if os.path.exists(temp_pdf_path): os.remove(temp_pdf_path)
        if os.path.exists(output_path): os.remove(output_path)
        
    except Exception as e:
        logger.exception(f"حدث خطأ أثناء معالجة ملف الـ PDF: {e}")
        bot.reply_to(message, f"عذراً، حدث خطأ أثناء المعالجة: {str(e)}")

if __name__ == "__main__":
    logger.info("البوت بدأ الآن العمل في وضع الاستماع المستمر (Polling)...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.exception(f"توقف البوت بسبب خطأ في الشبكة أو الاتصال: {e}")
