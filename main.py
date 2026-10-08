import os, re, time, logging, threading
import telebot
import fitz  # PyMuPDF
from translator import Translator
from translate_pdf import translate_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


def clean_token(raw):
    """ينظف التوكن: يشيل الفراغات والاقتباسات والتكرار، ويلتقط الشكل الصحيح 123456789:AAxxxx."""
    m = re.search(r"\d{6,12}:[A-Za-z0-9_-]{35}", re.sub(r"\s+", "", raw or ""))
    return m.group(0) if m else ""


BOT_TOKEN = clean_token(os.environ.get("BOT_TOKEN", ""))
if not BOT_TOKEN:
    _raw = os.environ.get("BOT_TOKEN", "")
    raise SystemExit(
        f"BOT_TOKEN غير صالح (الطول={len(_raw)}، عدد النقطتين={_raw.count(':')}). "
        "انسخ التوكن من BotFather مرة ثانية والصقه بدون أي إضافات.")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
tr = Translator()
work_lock = threading.Lock()          # ملف واحد بكل مرة، حتى ما نضغط على حد الطلبات

FAIL_TEXT = "[تعذرت ترجمة هذه الفقرة]"


# ---------------- فحص وجود نص + نسخة TXT احتياطية ----------------
def extract_paragraphs(pdf_path):
    """returns list of (page_no, text) - نستخدمها للفحص وللنسخة الاحتياطية TXT"""
    res = []
    with fitz.open(pdf_path) as doc:
        for pno, page in enumerate(doc, 1):
            for b in sorted(page.get_text("blocks"), key=lambda b: (round(b[1]), b[0])):
                if len(b) > 6 and b[6] != 0:      # تجاهل الصور
                    continue
                t = re.sub(r"\s+", " ", b[4]).strip()
                if t and re.search(r"[A-Za-z0-9\u0600-\u06FF]", t):
                    res.append((pno, t))
    return res


def build_txt(items, translations, path):
    with open(path, "w", encoding="utf-8") as f:
        for (pno, en), ar in zip(items, translations):
            f.write((ar or FAIL_TEXT) + "\n" + en + "\n\n")


# ---------------- التليجرام ----------------
def say(chat_id, text, msg=None):
    try:
        if msg:
            bot.edit_message_text(text, chat_id, msg.message_id)
            return msg
        return bot.send_message(chat_id, text)
    except Exception as e:
        log.warning("say failed: %s", e)
        return msg


@bot.message_handler(commands=["start"])
def start(m):
    bot.reply_to(m, "أهلاً! أرسل لي ملف PDF بالإنجليزي وأرجعه لك PDF بنفس التصميم والصور، "
                    "وتحت كل فقرة الترجمة العربية بالأحمر.\nاكتب /test لفحص اتصال البوت بالترجمة.")


@bot.message_handler(commands=["test"])
def test(m):
    msg = say(m.chat.id, "⏳ أفحص خدمات الترجمة...")
    report = tr.check()
    say(m.chat.id, "نتيجة الفحص:\n" + report, msg)


@bot.message_handler(content_types=["document"])
def on_doc(m):
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        bot.reply_to(m, "أرسل ملف PDF فقط.")
        return
    threading.Thread(target=process, args=(m,), daemon=True).start()


def process(m):
    chat = m.chat.id
    status = say(chat, "⏳ استلمت الملف، أستخرج النص...")
    with work_lock:
        try:
            info = bot.get_file(m.document.file_id)
            data = bot.download_file(info.file_path)
            base = re.sub(r"[^\w\-. ]", "_", os.path.splitext(m.document.file_name)[0])
            in_path = f"/tmp/in_{m.message_id}.pdf"
            out = f"/tmp/translated_{base}.pdf"
            with open(in_path, "wb") as f:
                f.write(data)

            items = extract_paragraphs(in_path)
            if not items:
                say(chat, "❌ ما لقيت نص بالملف (يمكن مصوّر/Scan). أرسل ملف نصي.", status)
                return

            stats = {"ok": 0, "total": 0}

            def translate_many(texts):
                res = tr.translate_all(texts, lambda *a: None)
                res = list(res)
                stats["total"] += len(res)
                stats["ok"] += sum(1 for r in res if r)
                return [r or FAIL_TEXT for r in res]

            def prog(i, n):
                say(chat, f"⏳ أترجم وأبني الصفحات... {i} من {n}", status)

            try:
                translate_pdf(in_path, out, translate_many=translate_many, progress=prog)
            except Exception:
                log.exception("translate_pdf failed; falling back to TXT")
                texts = [t for _, t in items]
                res = tr.translate_all(texts, lambda *a: None)
                stats["ok"] = sum(1 for r in res if r)
                stats["total"] = len(texts)
                out = out[:-4] + ".txt"
                build_txt(items, res, out)

            if stats["ok"] == 0:
                say(chat, "❌ فشلت الترجمة بالكامل. الأسباب:\n" + tr.all_errors()
                    + "\nانتظر دقيقة وأعد إرسال الملف (الفقرات المترجمة تنحفظ).", status)
                return

            caption = f"✅ تمت ترجمة {stats['ok']} من {stats['total']} فقرة"
            if stats["ok"] < stats["total"]:
                caption += (f"\n⚠️ {stats['total'] - stats['ok']} فقرة فشلت ({tr.last_error})"
                            "\nأعد إرسال الملف وراح يكمل الناقص.")
            if out.endswith(".txt"):
                caption += "\n(ما كدرت أبني PDF فرجّعت ملف نصي)"
            with open(out, "rb") as f:
                bot.send_document(chat, f, caption=caption)
            say(chat, "تم ✅", status)
        except Exception as e:
            log.exception("process failed")
            say(chat, f"❌ صار خطأ: {type(e).__name__}: {e}", status)


if __name__ == "__main__":
    log.info("Bot starting. Backends: %s", tr._backends())
    bot.remove_webhook()
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception:
            log.exception("polling crashed, restarting in 5s")
            time.sleep(5)
