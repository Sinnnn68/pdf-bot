import os, re, io, time, logging, threading
import telebot
import fitz  # PyMuPDF
from fpdf import FPDF
import arabic_reshaper
from bidi.algorithm import get_display
from translator import Translator

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

HERE = os.path.dirname(os.path.abspath(__file__))
AMIRI = os.path.join(HERE, "Amiri-Regular.ttf")
CARLITO = os.path.join(HERE, "Carlito-Regular.ttf")
FAIL_TEXT = "[تعذرت ترجمة هذه الفقرة]"

reshaper = arabic_reshaper.ArabicReshaper()


# ---------------- استخراج الفقرات ----------------
def blocks_to_paragraphs(blocks):
    """blocks: tuples من PyMuPDF (x0,y0,x1,y1,text,no,type) -> list[str]"""
    out = []
    for b in sorted(blocks, key=lambda b: (round(b[1]), b[0])):
        if len(b) > 6 and b[6] != 0:      # تجاهل الصور
            continue
        t = re.sub(r"\s+", " ", b[4]).strip()
        if t and re.search(r"[A-Za-z0-9\u0600-\u06FF]", t):
            out.append(t)
    return out


def extract_paragraphs(pdf_bytes):
    """returns list of (page_no, text)"""
    res = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pno, page in enumerate(doc, 1):
            for t in blocks_to_paragraphs(page.get_text("blocks")):
                res.append((pno, t))
    return res


# ---------------- بناء الـ PDF ----------------
def shape(s):
    return reshaper.reshape(s)


def wrap_rtl(measure, text, width, shape_fn=shape):
    """يلف النص العربي على أسطر حسب العرض، وبعدها يرتب كل سطر للعرض الصحيح."""
    lines, cur = [], []
    for w in text.split():
        trial = " ".join(cur + [w])
        if cur and measure(shape_fn(trial)) > width:
            lines.append(" ".join(cur)); cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    return [get_display(shape_fn(l)) for l in lines]


def clean_en(s):
    return re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]", "", s)


def build_pdf(items, translations, path):
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    pdf.add_font("Amiri", "", AMIRI)
    en_font = "Amiri"
    if os.path.exists(CARLITO):
        pdf.add_font("Carlito", "", CARLITO)
        en_font = "Carlito"
    w = pdf.epw
    last_page = items[0][0] if items else 1
    for (pno, en), ar in zip(items, translations):
        if pno != last_page:
            pdf.add_page(); last_page = pno
        pdf.set_text_color(200, 0, 0)
        pdf.set_font("Amiri", size=13)
        for line in wrap_rtl(pdf.get_string_width, ar or FAIL_TEXT, w):
            pdf.cell(w, 7.5, line, align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_font(en_font, size=11)
        pdf.multi_cell(w, 5.5, clean_en(en), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)
    pdf.output(path)


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
    bot.reply_to(m, "أهلاً! أرسل لي ملف PDF بالإنجليزي وأرجعه لك PDF: الأصل الإنجليزي كما هو، "
                    "وفوق كل فقرة الترجمة العربية بالأحمر.\nاكتب /test لفحص اتصال البوت بالترجمة.")


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
            items = extract_paragraphs(data)
            if not items:
                say(chat, "❌ ما لقيت نص بالملف (يمكن مصوّر/Scan). أرسل ملف نصي.", status)
                return
            texts = [t for _, t in items]

            def prog(i, n):
                say(chat, f"⏳ أترجم... الدفعة {i} من {n}", status)

            res = tr.translate_all(texts, prog)
            ok = sum(1 for r in res if r)
            if ok == 0:
                say(chat, "❌ فشلت الترجمة بالكامل.\nالسبب: " + (tr.last_error or "غير معروف")
                    + "\nانتظر دقيقة وأعد إرسال الملف (الفقرات المترجمة تنحفظ).", status)
                return
            say(chat, "⏳ أبني ملف الـ PDF...", status)
            base = re.sub(r"[^\w\-. ]", "_", os.path.splitext(m.document.file_name)[0])
            out = f"/tmp/translated_{base}.pdf"
            try:
                build_pdf(items, res, out)
            except Exception:
                log.exception("PDF build failed; falling back to TXT")
                out = out[:-4] + ".txt"
                build_txt(items, res, out)
            caption = f"✅ تمت ترجمة {ok} من {len(texts)} فقرة"
            if ok < len(texts):
                caption += f"\n⚠️ {len(texts)-ok} فقرة فشلت ({tr.last_error})\nأعد إرسال الملف وراح يكمل الناقص."
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
