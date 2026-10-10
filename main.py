#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py  -  بوت تليجرام لترجمة ملفات PDF إلى العربية  (v3)
=========================================================
هذا ملف **بوت تليجرام** (يستخدم pyTelegramBotAPI / telebot)، وليس سيرفر Flask.
للتأكد: ابحث عن السطر  import telebot  -  إن وُجد فهو بوت.

عند البدء يفحص البوت شيئين ويطبع النتيجة في اللوج:
  1) وجود الخط العربي (Amiri) ومسارُه.
  2) خدمات الترجمة الحقيقية العاملة.

التشغيل على Railway عبر Procfile:  worker: python main.py

متغيّرات البيئة:
    BOT_TOKEN           (إلزامي)  توكن البوت من BotFather
    GEMINI_API_KEY      (مستحسن جداً)  مفتاح Gemini - أفضل جودة
    GROQ_API_KEY        (مستحسن)       مفتاح Groq - بديل ممتاز
    LIBRETRANSLATE_URL  (اختياري)
    AR_FONT_PATH        (اختياري) مسار خط عربي بديل
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import traceback
from pathlib import Path

import telebot

from translator import Translator
import translate_pdf as tpdf
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


def _check_font_or_die() -> bool:
    """فحص صريح للخط العربي عند بدء البوت."""
    if tpdf.FONT_AR_REG is None:
        log.error("❌ لا يوجد خط عربي! ارفع assets/fonts/Amiri-Regular.ttf "
                  "أو اضبط AR_FONT_PATH. بدون الخط ستظهر الترجمة كمربعات فارغة.")
        return False
    log.info("✅ الخط العربي: %s", tpdf.FONT_AR_REG)
    return True


# ==========================================================================
# إنشاء البوت
# ==========================================================================
def build_bot(token: str) -> "telebot.TeleBot":
    # الافتراضي في telebot هو خيطان (threads) فقط: لو انشغل الخيطان بملفين PDF
    # يتوقف البوت عن الرد على الجميع. نرفعها حتى تبقى الأوامر تعمل أثناء الترجمة.
    bot = telebot.TeleBot(token, parse_mode=None,
                          threaded=True, num_threads=int(os.getenv("BOT_THREADS", "8")))

    # ---------------------------------------------------------------- #
    # الأوامر
    # ---------------------------------------------------------------- #
    @bot.message_handler(commands=["start"])
    def cmd_start(message):
        bot.reply_to(
            message,
            "👋 أهلاً بك في بوت ترجمة ملفات PDF إلى العربية.\n\n"
            "📄 أرسل لي ملف PDF وسأترجمه إلى العربية مع الحفاظ على التصميم الأصلي "
            "(الصور والرسوم تبقى كما هي)، وستظهر الترجمة أسفل كل فقرة مباشرة.\n\n"
            "الأوامر المتاحة:\n"
            "/start - رسالة الترحيب\n"
            "/help  - المساعدة\n"
            "/test  - فحص خدمات الترجمة\n"
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
        font_ok = "✅" if tpdf.FONT_AR_REG else "❌ (ارفع assets/fonts)"
        bot.reply_to(message, f"🟢 البوت يعمل.\nالخط العربي: {font_ok}\n\n" + t.status_text())

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
        working = [n for n, (ok, _) in results.items() if ok]
        lines.append("")
        if working:
            lines.append(f"الخدمات العاملة: {', '.join(working)}")
            lines.append("الترجمة ستكون بجودة جيدة.")
        else:
            lines.append("⚠️ لا توجد خدمة حقيقية - الترجمة ستكون تقريبية. "
                         "اضبط GEMINI_API_KEY أو GROQ_API_KEY.")
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

            # ملاحظة الجودة: هل استُخدم مترجم حقيقي أم التقريبي؟
            real_n = sum(translator.service_counts.values())
            mock_n = translator.mock_count
            if mock_n == 0 and real_n > 0:
                svc = ", ".join(translator.service_counts.keys())
                quality = f"✅ ترجمة حقيقية (الخدمة: {svc})"
            elif real_n > 0:
                quality = (f"⚠️ {mock_n} مقطع من أصل {real_n + mock_n} تُرجم بالمترجم "
                           "التقريبي لأن خدمات الترجمة الحقيقية انقطعت أثناء العمل.")
            else:
                quality = ("⚠️ ترجمة تقريبية لأن خدمات الترجمة الحقيقية غير متاحة.\n"
                           "لتحسين الجودة اضبط GEMINI_API_KEY أو GROQ_API_KEY في Railway.")

            summary = (f"✅ تمت الترجمة.\n"
                       f"الصفحات: {report.get('pages')}\n"
                       f"المقاطع المترجمة: {report.get('translated_blocks')}\n"
                       f"الصور محفوظة: {'نعم' if report.get('images_preserved') else 'لا'}\n\n"
                       f"{quality}")
            if mock_n and translator.all_errors():
                summary += "\n\nآخر ملاحظة:\n" + translator.all_errors()[-2:][0][:150]
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

    # فحوصات البدء (تظهر في لوج Railway)
    _check_font_or_die()

    # فحص الخدمات في الخلفية: لو كانت الخدمات بطيئة أو محجوبة قد يستغرق الفحص
    # دقائق، وإذا نُفّذ هنا مباشرة فلن يبدأ البوت بالرد إلا بعد انتهائه.
    def _probe():
        try:
            Translator().startup_probe()
        except Exception as exc:
            log.warning("تعذّر فحص خدمات الترجمة عند البدء: %s", exc)

    threading.Thread(target=_probe, name="startup-probe", daemon=True).start()

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
