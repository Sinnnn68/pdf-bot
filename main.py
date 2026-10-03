import os
import time
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import fitz  # PyMuPDF
from deep_translator import MyMemoryTranslator, GoogleTranslator
import arabic_reshaper
from bidi.algorithm import get_display

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

def process_arabic_text(text):
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text

def safe_translate(text):
    if not text.strip():
        return ""
    try:
        translated = GoogleTranslator(source='en', target='ar').translate(text)
        time.sleep(0.4)
        return translated
    except Exception:
        pass

    try:
        translated = MyMemoryTranslator(source='en', target='ar').translate(text)
        time.sleep(0.5)
        return translated
    except Exception:
        return text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أهلاً بك! أرسل لي أي ملف PDF وسأقوم بترجمته للغة العربية.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.mime_type or 'pdf' not in document.mime_type.lower():
        await update.message.reply_text("عذراً، يرجى إرسال ملف بصيغة PDF فقط.")
        return

    status_msg = await update.message.reply_text("جاري تحميل الملف ومعالجته، يرجى الانتظار...")

    input_pdf_path = f"input_{document.file_id}.pdf"
    output_pdf_path = f"translated_{document.file_id}.pdf"

    try:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(input_pdf_path)

        await status_msg.edit_text("جاري استخراج النصوص وترجمتها...")

        doc = fitz.open(input_pdf_path)
        new_doc = fitz.open()

        for page in doc:
            new_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
            pix = page.get_pixmap()
            new_page.insert_image(page.rect, stream=pix.tobytes("png"))

            blocks = page.get_text("blocks")
            for b in blocks:
                text = b[4].strip()
                if text:
                    translated = safe_translate(text)
                    arabic_rendered = process_arabic_text(translated)
                    new_page.insert_text((b[0], b[1]), arabic_rendered, fontsize=10, color=(0, 0, 0))

        new_doc.save(output_pdf_path)
        new_doc.close()
        doc.close()

        await status_msg.edit_text("تمت الترجمة بنجاح! جاري إرسال الملف...")
        with open(output_pdf_path, 'rb') as f:
            await update.message.reply_document(document=f, filename=f"Ar_{document.file_name}")

    except Exception as e:
        logging.error(f"Error processing PDF: {e}")
        await status_msg.edit_text("حدث خطأ أثناء ترجمة الملف. يرجى المحاولة لاحقاً.")

    finally:
        if os.path.exists(input_pdf_path):
            os.remove(input_pdf_path)
        if os.path.exists(output_pdf_path):
            os.remove(output_pdf_path)

if __name__ == '__main__':
    if TELEGRAM_TOKEN:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.DOCUMENT, handle_document))
        print("Bot is running...")
        app.run_polling()
