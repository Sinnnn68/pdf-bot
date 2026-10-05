import os
import re
import json
import time
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor

import fitz  # PyMuPDF
import requests
import telebot
import arabic_reshaper
from bidi.algorithm import get_display
from fpdf import FPDF
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Amiri-Regular.ttf")

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest", "gemini-2.0-flash-lite"]
RED = (200, 0, 0)
BLACK = (0, 0, 0)
BATCH_CHARS = 3500

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)


# ---------------------------------------------------------------- extraction
def extract_paragraphs(pdf_path):
    paras = []
    doc = fitz.open(pdf_path)
    for page in doc:
        for block in page.get_text("blocks"):
            if block[6] != 0:  # not text
                continue
            text = re.sub(r"\s*\n\s*", " ", block[4]).strip()
            text = re.sub(r"\s{2,}", " ", text)
            if len(text) > 1:
                paras.append(text)
    doc.close()
    return paras


# ---------------------------------------------------------------- translation
def _gemini_call(prompt):
    last_err = None
    for round_ in range(3):
        for model in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
            }
            try:
                r = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=60)
                if r.status_code == 200:
                    data = r.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                last_err = f"{model} HTTP {r.status_code}: {r.text[:300]}"
                log.warning(last_err)
            except Exception as e:
                last_err = f"{model} exception: {e}"
                log.warning(last_err)
        time.sleep(15 * (round_ + 1))  # wait before retrying
    raise RuntimeError(last_err or "Gemini failed")


def gemini_translate_batch(texts):
    items = [{"id": i, "text": t} for i, t in enumerate(texts)]
    prompt = (
        "Translate each English pharmacology lecture paragraph below into clear Arabic. "
        "Keep drug names, formulas and abbreviations (Vd, CL, EC50, Emax, etc.) in English. "
        "Return ONLY a JSON array of strings, same order and same length as the input "
        f"({len(texts)} items).\n\n" + json.dumps(items, ensure_ascii=False)
    )
    raw = _gemini_call(prompt)
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
    out = json.loads(raw)
    if isinstance(out, dict):
        out = list(out.values())
    out = [o["text"] if isinstance(o, dict) and "text" in o else str(o) for o in out]
    if len(out) != len(texts):
        raise ValueError(f"expected {len(texts)} got {len(out)}")
    return out


def fallback_translate(text):
    tr = GoogleTranslator(source="en", target="ar")
    parts = re.findall(r".{1,4000}(?:\s|$)", text, flags=re.S) or [text]
    res = []
    for p in parts:
        for attempt in range(3):
            try:
                res.append(tr.translate(p))
                break
            except Exception as e:
                log.warning("fallback error: %s", e)
                time.sleep(2 * (attempt + 1))
        else:
            return None
    return " ".join(res)


def make_batches(indices, paras):
    batches, cur, size = [], [], 0
    for i in indices:
        p = paras[i]
        if cur and size + len(p) > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += len(p)
    if cur:
        batches.append(cur)
    return batches


def translate_all(paras, translator=None):
    """Return list of Arabic strings (or None where everything failed)."""
    result = [None] * len(paras)

    def work(idxs):
        texts = [paras[i] for i in idxs]
        if translator:  # used only for offline testing
            return [translator(t) for t in texts]
        if GEMINI_API_KEY:
            try:
                return gemini_translate_batch(texts)
            except Exception as e:
                log.warning("Gemini batch failed: %s", e)
        return [None] * len(idxs)

    for round_ in range(3):
        missing = [i for i, v in enumerate(result) if not v]
        if not missing:
            break
        if round_ > 0:
            log.info("retrying %d missing paragraphs (round %d)", len(missing), round_ + 1)
            time.sleep(20)
        batches = make_batches(missing, paras)
        with ThreadPoolExecutor(max_workers=2) as ex:
            for idxs, out in zip(batches, ex.map(work, batches)):
                for i, o in zip(idxs, out):
                    if o:
                        result[i] = o

    # last chance: Google Translate for whatever is still missing
    if not translator:
        for i, v in enumerate(result):
            if not v:
                result[i] = fallback_translate(paras[i])
                time.sleep(0.5)
    return result


# ---------------------------------------------------------------- PDF build
def ar(text):
    return get_display(arabic_reshaper.reshape(text))


def clean_latin(text):
    return text.replace("\u2022", "-").replace("\u2013", "-").replace("\u2014", "-")


def build_pdf(paras, translations, out_path, font_path=FONT_PATH):
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.add_font("F", "", font_path)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    for en, arb in zip(paras, translations):
        if arb:
            pdf.set_font("F", size=13)
            pdf.set_text_color(*RED)
            pdf.multi_cell(width, 7, ar(arb), align="R", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
        pdf.set_font("F", size=11)
        pdf.set_text_color(*BLACK)
        pdf.multi_cell(width, 6, clean_latin(en), align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    pdf.output(out_path)


# ---------------------------------------------------------------- bot
@bot.message_handler(commands=["start"])
def start(m):
    bot.reply_to(m, "أهلاً! أرسل لي ملف PDF بالإنجليزي وأرجعه لك بنفس الشكل مع الترجمة العربية بالأحمر فوق كل فقرة.")


@bot.message_handler(content_types=["document"])
def handle_doc(m):
    doc = m.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        bot.reply_to(m, "أرسل ملف PDF فقط.")
        return
    status = bot.reply_to(m, "⏳ جاري الترجمة، انتظر شوية...")
    try:
        info = bot.get_file(doc.file_id)
        data = bot.download_file(info.file_path)
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.pdf")
            with open(src, "wb") as f:
                f.write(data)
            paras = extract_paragraphs(src)
            if not paras:
                bot.edit_message_text("ما لكيت نص بالملف (يمكن صور).", status.chat.id, status.message_id)
                return
            trans = translate_all(paras)
            failed = sum(1 for t in trans if not t)
            log.info("paragraphs=%d failed=%d", len(paras), failed)
            if failed == len(paras):
                bot.edit_message_text("❌ فشلت الترجمة بالكامل. تأكد من GEMINI_API_KEY وشوف Logs بـ Railway.",
                                      status.chat.id, status.message_id)
                return
            out = os.path.join(d, "translated.pdf")
            build_pdf(paras, trans, out)
            base = os.path.splitext(doc.file_name)[0]
            with open(out, "rb") as f:
                bot.send_document(m.chat.id, f, visible_file_name=f"{base}_AR.pdf",
                                  caption=f"✅ تمت الترجمة ({len(paras) - failed}/{len(paras)} فقرة)")
        bot.delete_message(status.chat.id, status.message_id)
    except Exception as e:
        log.exception("processing failed")
        bot.edit_message_text(f"❌ صار خطأ: {e}", status.chat.id, status.message_id)


if __name__ == "__main__":
    log.info("Bot started")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
