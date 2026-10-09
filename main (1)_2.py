#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py  -  بوت تليجرام لترجمة ملفات PDF إلى العربية
=====================================================
هذا ملف **بوت تليجرام** (يستخدم مكتبة pyTelegramBotAPI / telebot)، وليس سيرفر Flask.

للتأكد بنفسك: ابحث في هذا الملف عن السطر
        import telebot
إن وجدته فهو بوت تليجرام. أما لو وجدت استيراد مكتبة الويب (Flask)
فهذا يعني أنه سيرفر ويب وليس بوتاً.

التشغيل على Railway عبر Procfile:
        worker: python main.py

متغيّرات البيئة:
        BOT_TOKEN            (إلزامي)  توكن البوت من BotFather
        GEMINI_API_KEY       (اختياري) مفتاح Google Gemini
        GROQ_API_KEY         (اختياري) مفتاح Groq
        LIBRETRANSLATE_URL   (اختياري) مثال: https://libretranslate.com
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
import traceback
from pathlib import Path

import telebot

from translator import Translator
from translate_pdf import translate_pdf

# ==========================================================================
# الإعداد
# ==========================================================================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("pdf-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_MB = int(os.getenv("MAX_FILE_MB", "20"))
WORK_DIR = Path(tempfile.gettempdir()) / "pdf_bot"
WORK_DIR.mkdir(parents=True, exist_ok=True)

USER_STATE: dict[int, dict] = {}


def _state(uid: int) -> dict:
    return USER_STATE.setdefault(uid, {"busy": False})


# ==========================================================================
# إنشاء البوت وتسجيل المعالِجات
# ==========================================================================
def build_bot(token: str) -> "telebot.TeleBot":
    bot = telebot.TeleBot(token, parse_mode=None)

    # ---------------------------------------------------------------- #
    # الأوامر
    # ---------------------------------------------------------------- #
    @bot.message_handler(commands=["start"])
    def cmd_start(message):
        bot.reply_to(
            message,
            "👋 أهلاً بك في بوت ترجمة ملفات PDF إلى العربية.\n\n"
            "📄 أرسل لي ملف PDF وسأترجمه إلى العربية مع الحفاظ على التصميم الأصلي "
            "(الصور والرسوم تبقى كما هي).\n\n"
            "الأوامر المتاحة:\n"
            "/start - رسالة الترحيب\n"
            "/help  - المساعدة\n"
            "/test  - فحص حالة خدمات الترجمة\n"
            "/status - حالة البوت",
        )

    @bot.message_handler(commands=["help"])
    def cmd_help(message):
        bot.reply_to(
            message,
            "📌 طريقة الاستخدام:\n"
            f"1) أرسل ملف PDF (حتى {MAX_MB} ميجابايت).\n"
            "2) انتظر قليلاً أثناء الترجمة.\n"
            "3) سأرسل لك ملف PDF المترجم.\n\n"
            "الأوامر: /start /help /test /status",
        )

    @bot.message_handler(commands=["status"])
    def cmd_status(message):
        t = Translator()
        bot.reply_to(message, "🟢 البوت يعمل.\n\n" + t.status_text())

    @bot.message_handler(commands=["test"])
    def cmd_test(message):
        bot.reply_to(message, "🔎 جارٍ فحص خدمات الترجمة...")
        t = Translator()
        try:
            results = t.check()
        except Exception as exc:
            bot.reply_to(message, f"❌ فشل الفحص: {exc}")
            return
        lines = ["نتيجة الفحص:"]
        for name, (ok, msg) in results.items():
            lines.append(f"{'✅' if ok else '❌'} {name}: {msg}")
        lines.append("")
        lines.append("ملاحظة: وجود خدمة واحدة ناجحة على الأقل يكفي لعمل البوت.")
        bot.reply_to(message, "\n".join(lines))

    # ---------------------------------------------------------------- #
    # استقبال ملفات PDF
    # ---------------------------------------------------------------- #
    @bot.message_handler(content_types=["document"])
    def on_document(message):
        uid = message.from_user.id
        st = _state(uid)
        if st.get("busy"):
            bot.reply_to(message, "⏳ ما زلت أعالج ملفك السابق، انتظر قليلاً من فضلك.")
            return

        doc = message.document
        name = doc.file_name or "file.pdf"
        if not name.lower().endswith(".pdf"):
            bot.reply_to(message, "⚠️ أرسل ملف PDF فقط.")
            return
        if doc.file_size and doc.file_size > MAX_MB * 1024 * 1024:
            bot.reply_to(message, f"⚠️ الملف كبير جداً (الحد {MAX_MB} ميجابايت).")
            return

        st["busy"] = True
        status = bot.reply_to(message, "📥 جارٍ تنزيل الملف...")
        stamp = int(time.time())
        in_path = WORK_DIR / f"{uid}_{stamp}_{name}"
        out_path = WORK_DIR / f"{uid}_{stamp}_translated.pdf"

        try:
            file_info = bot.get_file(doc.file_id)
            data = bot.download_file(file_info.file_path)
            in_path.write_bytes(data)
            bot.edit_message_text("🔤 جارٍ استخراج النص وترجمته...",
                                  chat_id=status.chat.id, message_id=status.message_id)

            translator = Translator()
            last = {"t": 0.0}

            def progress(done, total):
                now = time.time()
                if now - last["t"] < 3 and done < total:
                    return
                last["t"] = now
                try:
                    bot.edit_message_text(f"🔤 الترجمة... {done}/{total}",
                                          chat_id=status.chat.id,
                                          message_id=status.message_id)
                except Exception:
                    pass

            report = translate_pdf(str(in_path), str(out_path), translator, progress)

            bot.edit_message_text("📤 جارٍ إرسال الملف المترجم...",
                                  chat_id=status.chat.id, message_id=status.message_id)
            with open(out_path, "rb") as fh:
                bot.send_document(message.chat.id, fh,
                                  visible_file_name=f"translated_{name}")

            summary = (f"✅ تمت الترجمة.\n"
                       f"الصفحات: {report.get('pages')}\n"
                       f"الكتل المترجمة: {report.get('translated_blocks')}\n"
                       f"الصور محفوظة: {'نعم' if report.get('images_preserved') else 'لا'}")
            if translator.all_errors():
                summary += "\n\n⚠️ ملاحظات:\n" + "\n".join(translator.all_errors()[-3:])
            bot.send_message(message.chat.id, summary)

        except Exception as exc:
            log.error("فشل معالجة الملف: %s\n%s", exc, traceback.format_exc())
            bot.reply_to(message, f"❌ حدث خطأ أثناء المعالجة:\n{str(exc)[:300]}")
        finally:
            st["busy"] = False
            for p in (in_path, out_path):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass

    @bot.message_handler(content_types=["text"])
    def on_text(message):
        bot.reply_to(message, "أرسل لي ملف PDF لأترجمه، أو اكتب /help للمساعدة.")

    return bot


# ==========================================================================
# نقطة البداية
# ==========================================================================
def main() -> int:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN غير مضبوط! أضِفه في متغيّرات البيئة على Railway.")
        return 1
    bot = build_bot(BOT_TOKEN)
    log.info("بدء تشغيل البوت (polling)...")
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=25)
        except Exception as exc:
            log.error("انقطع الاتصال: %s - إعادة المحاولة بعد 5 ثوانٍ", exc)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
