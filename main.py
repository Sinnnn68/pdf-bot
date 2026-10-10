#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py  -  بوت تليجرام لترجمة ملفات المحاضرات إلى العربية  (v9)
==============================================================
* بوت telebot حقيقي (import telebot) - وليس سيرفر Flask.
* يستقبل أي ملف، يكتشف نوعه، يترجمه، ويرجّع ناتجاً بنفس النوع.
* أوامر: /start /help /test /status
* رسائل تقدّم أثناء العمل، ولا يترك المستخدم بدون رد أبداً.

التشغيل على Railway:  Procfile => worker: python main.py  و Service Type = Worker
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
from doc_converter import process_file, SUPPORTED

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_MB = float(os.getenv("MAX_FILE_MB", "20"))
WORK = Path(tempfile.gettempdir()) / "pdf_bot"
WORK.mkdir(parents=True, exist_ok=True)
BUSY: dict[int, bool] = {}


def _spin(pending):
    try:
        import threading
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        while pending["stop"] is False:
            pending["i"] = (pending["i"] + 1) % len(frames)
            time.sleep(0.7)
    except Exception:
        pass


def build_bot(token: str):
    bot = telebot.TeleBot(token, parse_mode=None)

    @bot.message_handler(commands=["start"])
    def cmd_start(m):
        bot.reply_to(m,
            "👋 أهلاً بك في بوت ترجمة ملفات المحاضرات إلى العربية.\n\n"
            "📄 أرسل لي أي ملف (PDF، Word، PowerPoint، Excel، نص، أو صورة) "
            "وسأترجمه إلى العربية مع الحفاظ على التصميم الأصلي، "
            "وتظهر الترجمة باللون الأحمر أسفل كل فقرة.\n\n"
            "الأوامر:\n/start  /help  /test  /status")

    @bot.message_handler(commands=["help"])
    def cmd_help(m):
        bot.reply_to(m,
            "📌 طريقة الاستخدام:\n"
            f"1) أرسل ملفاً (حتى {MAX_MB:g} ميجابايت).\n"
            "2) انتظر قليلاً، وسأرسل لك الناتج المترجم.\n\n"
            "الصيغ المدعومة: PDF, DOCX, PPTX, XLSX, TXT, CSV, PNG/JPG.\n"
            "الأوامر: /start /help /test /status")

    @bot.message_handler(commands=["status"])
    def cmd_status(m):
        t = Translator()
        bot.reply_to(m, "🟢 البوت يعمل.\n\n" + t.status_text())

    @bot.message_handler(commands=["test"])
    def cmd_test(m):
        bot.reply_to(m, "🔎 جارٍ فحص خدمات الترجمة والموديلات المتاحة...")
        t = Translator()
        try:
            res = t.check()
        except Exception as exc:
            bot.reply_to(m, f"❌ فشل الفحص: {str(exc)[:300]}")
            return
        lines = ["نتيجة الفحص:"]
        for name, (ok, msg) in res.items():
            lines.append(f"{'✅' if ok else '❌'} {name}: {msg}")
        gm = t.list_gemini_models()
        if gm:
            lines.append("\nموديلات Gemini المتاحة: " + ", ".join(gm[:6]))
        working = [n for n, (ok, _) in res.items() if ok]
        lines.append("")
        lines.append(("الخدمات العاملة: " + ", ".join(working)) if working else
                     "⚠️ لا توجد خدمة حقيقية - الترجمة ستكون تقريبية. اضبط GEMINI_API_KEY.")
        bot.reply_to(m, "\n".join(lines))

    @bot.message_handler(content_types=["document", "photo"])
    def on_file(message):
        uid = message.from_user.id
        if BUSY.get(uid):
            bot.reply_to(message, "⏳ ما زلت أعالج ملفك السابق، انتظر قليلاً من فضلك.")
            return

        # تحديد الملف والاسم
        if message.content_type == "document":
            doc = message.document
            name = doc.file_name or "file"
            size = doc.file_size or 0
            file_id = doc.file_id
        else:                                    # photo
            ph = message.photo[-1]
            name = "photo.jpg"
            size = ph.file_size or 0
            file_id = ph.file_id

        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED:
            bot.reply_to(message, f"⚠️ صيغة غير مدعومة: {ext or 'غير معروفة'}\n"
                                  f"المدعوم: {', '.join(sorted(SUPPORTED))}")
            return
        if size and size > MAX_MB * 1024 * 1024:
            bot.reply_to(message, f"⚠️ الملف كبير جداً (الحد {MAX_MB:g} ميجابايت).")
            return

        BUSY[uid] = True
        status = bot.reply_to(message, "📥 جارٍ تنزيل الملف...")
        stamp = int(time.time())
        inp = WORK / f"{uid}_{stamp}_{name}"
        outdir = WORK / f"{uid}_{stamp}_out"

        def upd(text):
            try:
                bot.edit_message_text(text, chat_id=status.chat.id,
                                      message_id=status.message_id)
            except Exception:
                pass

        try:
            fi = bot.get_file(file_id)
            inp.write_bytes(bot.download_file(fi.file_path))
            upd("🔤 جارٍ تحليل الملف وترجمته...")

            translator = Translator()
            last = {"t": 0.0}

            def progress(done, total):
                now = time.time()
                if now - last["t"] < 2.5 and done < total:
                    return
                last["t"] = now
                upd(f"🔤 الترجمة... {done}/{total} صفحة")

            report = process_file(str(inp), str(outdir), translator, progress)
            outpath = Path(report["out"])

            upd("📤 جارٍ إرسال الملف المترجم...")
            with open(outpath, "rb") as fh:
                bot.send_document(message.chat.id, fh,
                                  visible_file_name=outpath.name)

            if translator.used_real_service:
                svc = ", ".join(translator.service_counts.keys())
                quality = f"✅ ترجمة حقيقية (الخدمة: {svc})"
            else:
                quality = ("⚠️ ترجمة **تقريبية** لأن خدمات الترجمة الحقيقية غير متاحة.\n"
                           "لتحسين الجودة أضف GEMINI_API_KEY أو GROQ_API_KEY في Railway.")

            lines = ["✅ تمت الترجمة.",
                     f"الصيغة: {report.get('kind')}"]
            if report.get("pages"):
                lines.append(f"الصفحات: {report['pages']}")
            lines.append(f"المقاطع المترجمة: {report.get('translated_blocks', report.get('translated', 0))}")
            if report.get("kind") == "pdf":
                lines.append(f"الصور محفوظة: {'نعم' if report.get('images_preserved') else 'لا'}")
            if report.get("source_pages") is not None and report.get("kind") == "pdf":
                if report["source_pages"] == 0:
                    lines.append("ℹ️ الملف عربي أصلاً - لم تُضف ترجمة.")
            lines.append("")
            lines.append(quality)
            errs = translator.all_errors()
            if errs:
                lines.append("\nآخر ملاحظات:\n" + "\n".join(errs[-2:]))
            bot.send_message(message.chat.id, "\n".join(lines))

        except Exception as exc:
            log.error("فشل المعالجة: %s\n%s", exc, traceback.format_exc())
            bot.reply_to(message, "❌ حدث خطأ أثناء المعالجة:\n" + str(exc)[:300])
        finally:
            BUSY[uid] = False
            try:
                inp.unlink(missing_ok=True)
            except Exception:
                pass

    @bot.message_handler(content_types=["text"])
    def on_text(message):
        bot.reply_to(message, "أرسل لي ملفاً لأترجمه، أو اكتب /help للمساعدة.")

    return bot


def main() -> int:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN غير مضبوط! أضِفه في متغيّرات البيئة على Railway.")
        return 1
    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GROQ_API_KEY"):
        log.warning("لا يوجد GEMINI_API_KEY ولا GROQ_API_KEY - ستُستخدم خدمة google-free المجانية.")
    try:
        Translator().startup_probe()
    except Exception as exc:
        log.warning("تعذّر فحص الخدمات عند البدء: %s", exc)

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
