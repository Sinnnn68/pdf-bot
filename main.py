import os
import sys
import logging
import fitz
from googletrans import Translator
import telebot

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("خطأ: توكن البوت غير موجود!")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
translator = Translator()

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "أهلاً بك! أرسل لي ملف PDF وسأترجمه لك إلى العربية فوراً.")

@bot.message_handler(content_types=['document'])
def handle_pdf(message):
    try:
        bot.reply_to(message, "جاري معالجة ملف الـ PDF...")
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        with open("temp.pdf", 'wb') as f:
            f.write(downloaded_file)
            
        doc = fitz.open("temp.pdf")
        translated_text = ""
        for i, page in enumerate(doc):
            text = page.get_text()
            if text.strip():
                tr = translator.translate(text, dest='ar')
                translated_text += f"\n--- صفحة {i + 1} ---\n" + tr.text

        with open("translated.txt", 'w', encoding='utf-8') as f:
            f.write(translated_text)
            
        with open("translated.txt", 'rb') as f:
            bot.send_document(message.chat.id, f, caption="إليك ملف الترجمة جاهزاً!")
            
        doc.close()
    except Exception as e:
        bot.reply_to(message, f"حدث خطأ: {str(e)}")

print("البوت يعمل الآن وجاهز للاستماع...")
bot.infinity_polling()
