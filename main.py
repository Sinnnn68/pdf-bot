import os
import sys
import logging
import fitz  # PyMuPDF
from googletrans import Translator
import telebot

# إعدادات طباعة السجلات لمتابعة حالة البوت مباشرة في Railway
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

logger.info("--- جاري فحص متغيرات البيئة وتشغيل البوت ---")

# قراءة توكن البوت من إعدادات Railway بأمان
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    logger.error("خطأ قاتل: متغير BOT_TOKEN غير موجود في Railway Variables!")
    sys.exit(1)

# تهيئة البوت والمترجم
bot = telebot.TeleBot(BOT_TOKEN)
translator = Translator()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    logger.info(f"المستخدم {message.chat.id} أرسل أمر البدء.")
    bot.reply_to(
        message, 
        "أهلاً بك! 🌟\nأرسل لي أي ملف PDF باللغة الأجنبية، وسأقوم بترجمته كاملاً إلى اللغة العربية وأرسله لك."
    )

@bot.message_handler(content_types=['document'])
def handle_pdf(message):
    temp_pdf_path = None
    output_path = None
    try:
        logger.info("تم استقبال ملف PDF جديد، بدء المعالجة...")
        bot.reply_to(message, "⏳ جاري تحميل الملف ومعالجة صفحاته للترجمة، يرجى الانتظار...")
        
        # تحميل الملف من سيرفرات تيليجرام
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        temp_pdf_path = f"temp_{message.chat.id}.pdf"
        with open(temp_pdf_path, 'wb') as new_file:
            new_file.write(downloaded_file)
            
        # استخراج النصوص وترجمتها صفحة بصفحة
        doc = fitz.open(temp_pdf_path)
        translated_text = "=== ملخص ترجمة ملف الـ PDF ===\n"
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            if text.strip():
                try:
                    translation = translator.translate(text, dest='ar')
                    translated_text += f"\n\n--- [ صفحة {page_num + 1} ] ---\n" + translation.text
                except Exception as trans_err:
                    logger.warning(f"خطأ في ترجمة الصفحة {page_num + 1}: {trans_err}")
                    translated_text += f"\n\n--- [ صفحة {page_num + 1} ] ---\n(تعذر ترجمة هذه الصفحة)"

        doc.close()

        # حفظ النصوص المترجمة في ملف نصي جديد
        output_path = f"translated_{message.chat.id}.txt"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(translated_text)
            
        # إرسال الملف الناتج للمستخدم
        with open(output_path, 'rb') as f:
            bot.send_document(
                message.chat.id, 
                f, 
                caption="✅ تم الانتهاء من ترجمة الملف بنجاح وإعداده كملف نصي!"
            )
            
        logger.info("تم إرسال الملف المترجم بنجاح للمستخدم.")

    except Exception as e:
        logger.exception(f"حدث خطأ أثناء معالجة ملف الـ PDF: {e}")
        bot.reply_to(message, f"❌ عذراً، حدث خطأ أثناء معالجة الملف: {str(e)}")
        
    finally:
        # تنظيف الملفات المؤقتة من السيرفر للحفاظ على الذاكرة
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)
        if output_path and os.path.exists(output_path):
            os.remove(output_path)

if __name__ == "__main__":
    logger.info("البوت يعمل الآن في وضع الاستماع المستمر (Polling)... جاهز لتلقي الرسائل.")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except Exception as e:
        logger.exception(f"توقف البوت بسبب خطأ في الشبكة: {e}")
