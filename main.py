# -*- coding: utf-8 -*-
"""
بوت تليجرام: يستقبل ملف PDF ويرجّعه بنفس الشكل، لكن فوق كل فقرة إنكليزية
تظهر ترجمتها العربية بالأحمر.

المتغيرات في Railway:  BOT_TOKEN  و  GEMINI_API_KEY
الخط العربي: ملف Amiri-Regular.ttf بنفس مجلد main.py
"""
import io
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time

import pdfplumber
import requests
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAVE_AR = True
except Exception:
    HAVE_AR = False

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pdfbot")

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "80"))
FAKE = os.environ.get("FAKE_TRANSLATE") == "1"
AR_COLOR = (0.85, 0.0, 0.0)   # لون الترجمة العربية: أحمر
MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest",
                      "gemini-2.5-flash", "gemini-2.0-flash"] if m]


# ----------------------------------------------------------------- الخطوط
def _find_font():
    for p in [os.environ.get("FONT_PATH"),
              os.path.join(HERE, "Amiri-Regular.ttf"),
              "Amiri-Regular.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if p and os.path.exists(p):
            return p
    raise RuntimeError("ما لقيت الخط العربي Amiri-Regular.ttf بجانب main.py")


pdfmetrics.registerFont(TTFont("AR", _find_font()))
_AR_CMAP = pdfmetrics.getFont("AR").face.charToGlyph

LATIN_FONTS = {(False, False): "Helvetica", (True, False): "Helvetica-Bold",
               (False, True): "Helvetica-Oblique",
               (True, True): "Helvetica-BoldOblique"}


def _cp1252_ok(ch):
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def segments(text, font):
    out = []
    for ch in text.replace("\u00a0", " "):
        if _cp1252_ok(ch):
            f = font
        elif ord(ch) in _AR_CMAP:
            f = "AR"
        else:
            ch, f = "?", font
        if out and out[-1][1] == f:
            out[-1] = (out[-1][0] + ch, f)
        else:
            out.append((ch, f))
    return out


def seg_width(segs, size):
    return sum(stringWidth(t, f, size) for t, f in segs)


# ---------------------------------------------------------------- الألوان
def to_rgb(c):
    if c is None:
        return (0, 0, 0)
    if isinstance(c, (int, float)):
        c = (c,)
    c = tuple(float(x) for x in c)
    if len(c) == 1:
        return (c[0],) * 3
    if len(c) == 3:
        return c
    if len(c) == 4:
        C, M, Y, K = c
        return ((1 - C) * (1 - K), (1 - M) * (1 - K), (1 - Y) * (1 - K))
    return (0, 0, 0)


def is_dark(rgb):
    return max(rgb) < 0.3


# ---------------------------------------------------------- قراءة الـ PDF
class Word:
    __slots__ = ("text", "x0", "x1", "top", "bottom", "size", "bold",
                 "italic", "rgb")


def _style(fontname):
    f = (fontname or "").lower()
    return (("bold" in f or "black" in f or "heavy" in f),
            ("italic" in f or "oblique" in f))


def read_words(page):
    raw = page.extract_words(
        extra_attrs=["fontname", "size", "non_stroking_color"],
        x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    words = []
    for r in raw:
        w = Word()
        w.text = r["text"]
        w.x0, w.x1, w.top, w.bottom = r["x0"], r["x1"], r["top"], r["bottom"]
        w.size = float(r.get("size") or 10)
        w.bold, w.italic = _style(r.get("fontname"))
        w.rgb = to_rgb(r.get("non_stroking_color"))
        words.append(w)
    return words


def group_lines(words):
    words = sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0))
    lines = []
    for w in words:
        cy = (w.top + w.bottom) / 2
        if lines and abs(cy - lines[-1]["cy"]) < w.size * 0.5:
            L = lines[-1]
            L["words"].append(w)
            L["cy"] = statistics.mean((x.top + x.bottom) / 2
                                      for x in L["words"])
        else:
            lines.append({"words": [w], "cy": cy})
    for L in lines:
        L["words"].sort(key=lambda w: w.x0)
        ws = L["words"]
        L["x0"], L["x1"] = ws[0].x0, ws[-1].x1
        L["top"] = min(w.top for w in ws)
        L["bottom"] = max(w.bottom for w in ws)
        L["size"] = statistics.median(w.size for w in ws)
        L["text"] = " ".join(w.text for w in ws)
    return lines


TERMINAL = tuple(".:;!?)")


def split_paragraphs(lines):
    paras, cur = [], []
    for L in lines:
        if cur:
            P = cur[-1]
            gap = L["top"] - P["bottom"]
            ends = P["text"].rstrip().endswith(TERMINAL)
            starts_new = bool(re.match(r"^[A-Z0-9\u2022\-\u2013(]", L["text"]))
            size_change = abs(L["size"] - P["size"]) > 1.2
            color_head = (not is_dark(L["words"][0].rgb)
                          and L["words"][0].rgb != P["words"][0].rgb)
            all_color_prev = all(not is_dark(w.rgb) for w in P["words"])
            if (gap > P["size"] * 0.45 or size_change or all_color_prev
                    or (ends and starts_new) or (ends and color_head)):
                paras.append(cur)
                cur = []
        cur.append(L)
    if cur:
        paras.append(cur)
    return paras


def build_blocks(page, pdfium_page=None, scale=2.0):
    pw = float(page.width)
    blocks = []
    for lines in split_paragraphs(group_lines(read_words(page))):
        words = [w for L in lines for w in L["words"]]
        plain = " ".join(w.text for w in words)
        centers = [abs((L["x0"] + L["x1"]) / 2 - pw / 2) for L in lines]
        centered = statistics.median(centers) < 14 and lines[0]["x0"] > 30
        blocks.append({
            "kind": "text", "top": lines[0]["top"], "plain": plain,
            "size": statistics.median(w.size for w in words),
            "centered": centered,
            "runs": [(w.text, w.bold, w.italic, w.rgb, w.size)
                     for w in words],
        })
    for im in page.images:
        wpt, hpt = im["x1"] - im["x0"], im["bottom"] - im["top"]
        if wpt < 40 or hpt < 40 or pdfium_page is None:
            continue
        try:
            pil = pdfium_page.render(scale=scale).to_pil()
            crop = pil.crop((int(im["x0"] * scale), int(im["top"] * scale),
                             int(im["x1"] * scale), int(im["bottom"] * scale)))
            buf = io.BytesIO()
            crop.convert("RGB").save(buf, "PNG")
            blocks.append({"kind": "image", "top": im["top"], "w": wpt,
                           "h": hpt, "png": buf.getvalue()})
        except Exception as e:
            log.warning("image skipped: %s", e)
    blocks.sort(key=lambda b: b["top"])
    return blocks


# ----------------------------------------------------------------- الترجمة
def has_latin(s):
    return re.search(r"[A-Za-z]{2,}", s) is not None


def _gemini_call(prompt):
    last = None
    for model in MODELS:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        for attempt in range(6):
            try:
                r = requests.post(
                    url, params={"key": GEMINI_KEY}, timeout=150,
                    json={"contents": [{"parts": [{"text": prompt}]}],
                          "generationConfig": {
                              "temperature": 0.2,
                              "responseMimeType": "application/json"}})
            except requests.RequestException as e:
                last = str(e)
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 200:
                data = r.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError):
                    last = "empty response: " + json.dumps(data)[:300]
                    break
            last = f"{model} HTTP {r.status_code}: {r.text[:300]}"
            log.warning(last)
            if r.status_code == 404:
                break
            if r.status_code in (429, 500, 503):
                time.sleep(min(60, 8 * (attempt + 1)))
                continue
            break
    raise RuntimeError(last or "Gemini failed")


PROMPT = (
    "Translate each string in the JSON array below from English to Arabic "
    "(clear Modern Standard Arabic, suitable for university students). "
    "Medical and pharmacology terms: use the standard Arabic term and keep "
    "the English term in parentheses when it helps. Keep drug names, "
    "abbreviations, numbers and symbols as they are. Do not add comments. "
    "Return ONLY a JSON array of strings with exactly the same number of "
    "items and the same order.\n\n")


def _parse_array(txt, n):
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    arr = json.loads(txt)
    if isinstance(arr, list) and len(arr) == n and all(
            isinstance(x, str) for x in arr):
        return arr
    raise ValueError("bad array")


def translate_list(items):
    if FAKE:
        return ["هذه ترجمة تجريبية للنص: " + " ".join(
            ["كلمة"] * max(3, len(t.split()) // 2)) for t in items]
    try:
        return _parse_array(
            _gemini_call(PROMPT + json.dumps(items, ensure_ascii=False)),
            len(items))
    except Exception as e:
        log.warning("batch of %d failed: %s", len(items), e)
        if len(items) == 1:
            return ["[تعذّرت ترجمة هذه الفقرة]"]
        mid = len(items) // 2
        return translate_list(items[:mid]) + translate_list(items[mid:])


def translate_all(texts, progress=None):
    result = [None] * len(texts)
    todo = [i for i, t in enumerate(texts) if t and has_latin(t)]
    batches, cur, n = [], [], 0
    for i in todo:
        if cur and (len(cur) >= 20 or n + len(texts[i]) > 3500):
            batches.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += len(texts[i])
    if cur:
        batches.append(cur)
    for k, b in enumerate(batches, 1):
        tr = translate_list([texts[i] for i in b])
        for i, t in zip(b, tr):
            result[i] = t
        if progress:
            progress(k, len(batches))
        if not FAKE:
            time.sleep(1.5)
    return result


# ------------------------------------------------------------ كتابة الـ PDF
def shape_ar(text):
    if not HAVE_AR:
        return text[::-1]
    return get_display(arabic_reshaper.reshape(text))


def wrap_runs(runs, width):
    lines, cur, cur_w = [], [], 0.0
    for text, bold, italic, rgb, size in runs:
        segs = segments(text, LATIN_FONTS[(bold, italic)])
        w = seg_width(segs, size)
        sp = stringWidth(" ", "Helvetica", size)
        add = w + (sp if cur else 0)
        if cur and cur_w + add > width:
            lines.append((cur, cur_w))
            cur, cur_w, add = [], 0.0, w
        cur.append((segs, rgb, size, False))
        cur_w += add
    if cur:
        lines.append((cur, cur_w))
    return lines


def wrap_arabic(text, size, width):
    words = text.split()
    lines, cur = [], []
    for w in words:
        trial = cur + [w]
        if cur and stringWidth(shape_ar(" ".join(trial)), "AR", size) > width:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur = trial
    if cur:
        lines.append(" ".join(cur))
    return [shape_ar(l) for l in lines]


def write_pdf(src_pdf, pages_blocks, translations, out_path):
    c = None
    M = 36
    for (pw, ph, blocks) in pages_blocks:
        if c is None:
            c = canvas.Canvas(out_path, pagesize=(pw, ph))
        c.setPageSize((pw, ph))
        usable = pw - 2 * M
        y = ph - M

        def need(h):
            nonlocal y
            if y - h < M and y < ph - M - 1:
                c.showPage()
                c.setPageSize((pw, ph))
                y = ph - M

        for b in blocks:
            if b["kind"] == "image":
                k = min(1.0, usable / b["w"], (ph * 0.6) / b["h"])
                w, h = b["w"] * k, b["h"] * k
                need(h + 6)
                c.drawImage(ImageReader(io.BytesIO(b["png"])),
                            (pw - w) / 2, y - h, w, h)
                y -= h + 6
                continue
            size = b["size"]
            lead = size * 1.25
            lines = wrap_runs(b["runs"], usable)

            # 1) الترجمة العربية بالأحمر فوق الفقرة
            ar = translations.get(id(b))
            if ar:
                asize = max(8.0, size * 0.92)
                alead = asize * 1.45
                alines = wrap_arabic(ar, asize, usable)
                need(alead * min(len(alines), 2))
                c.setFillColorRGB(*AR_COLOR)
                c.setFont("AR", asize)
                for al in alines:
                    need(alead)
                    y -= alead
                    if b["centered"] and len(alines) == 1:
                        c.drawCentredString(pw / 2, y, al)
                    else:
                        c.drawRightString(pw - M, y, al)
                y -= 2

            # 2) الفقرة الإنجليزية الأصلية تحتها
            need(lead * min(len(lines), 3))
            for segs_line, lw in lines:
                need(lead)
                y -= lead
                x = M + (usable - lw) / 2 if b["centered"] else M
                for segs, rgb, sz, _ in segs_line:
                    c.setFillColorRGB(*rgb)
                    for t, f in segs:
                        c.setFont(f, sz)
                        c.drawString(x, y, t)
                        x += stringWidth(t, f, sz)
                    x += stringWidth(" ", "Helvetica", sz)
            y -= 1.5
            y -= size * 0.35
        c.showPage()
    c.save()


# ---------------------------------------------------------------- التشغيل
def translate_pdf(in_path, out_path, progress=None):
    import pypdfium2 as pdfium
    pages_blocks = []
    with pdfplumber.open(in_path) as pdf:
        n = len(pdf.pages)
        if n > MAX_PAGES:
            raise ValueError(f"الملف {n} صفحة، الحد الأقصى {MAX_PAGES}")
        doc = pdfium.PdfDocument(in_path)
        for i, page in enumerate(pdf.pages):
            blocks = build_blocks(page, doc[i] if page.images else None)
            pages_blocks.append((float(page.width), float(page.height),
                                 blocks))
    flat = [b for _, _, bl in pages_blocks for b in bl if b["kind"] == "text"]
    if not flat:
        raise ValueError("ما لقيت نص داخل الملف (ممكن يكون صور/ممسوح ضوئياً)")
    tr = translate_all([b["plain"] for b in flat], progress)
    translations = {id(b): t for b, t in zip(flat, tr) if t}
    write_pdf(in_path, pages_blocks, translations, out_path)
    return len(pages_blocks), len(flat)


def run_bot():
    import telebot
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN غير موجود في Variables")
    if not GEMINI_KEY:
        sys.exit("GEMINI_API_KEY غير موجود في Variables")
    bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=4)
    try:
        bot.remove_webhook()
    except Exception:
        pass

    @bot.message_handler(commands=["start", "help"])
    def start(m):
        bot.reply_to(m, "أهلاً! أرسل لي ملف PDF (محاضرة بالإنكليزي) وأرجّعه لك "
                        "بنفس الشكل مع الترجمة العربية فوق كل فقرة.")

    @bot.message_handler(content_types=["document"])
    def on_doc(m):
        d = m.document
        name = d.file_name or "file.pdf"
        if not (name.lower().endswith(".pdf")
                or d.mime_type == "application/pdf"):
            return bot.reply_to(m, "أرسل ملف بصيغة PDF فقط.")
        if d.file_size and d.file_size > 20 * 1024 * 1024:
            return bot.reply_to(m, "الملف أكبر من 20 ميغا (حد تليجرام للبوتات).")
        status = bot.reply_to(m, "⏳ استلمت الملف، جاري القراءة...")
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        last = [0.0]

        def prog(k, n):
            if time.time() - last[0] > 4 or k == n:
                last[0] = time.time()
                try:
                    bot.edit_message_text(f"⏳ الترجمة: {k} من {n}",
                                          status.chat.id, status.message_id)
                except Exception:
                    pass

        try:
            info = bot.get_file(d.file_id)
            with open(src, "wb") as f:
                f.write(bot.download_file(info.file_path))
            pages, paras = translate_pdf(src, dst, prog)
            with open(dst, "rb") as f:
                bot.send_document(
                    m.chat.id, f,
                    visible_file_name=os.path.splitext(name)[0] + "_AR.pdf",
                    caption=f"تمت الترجمة ✅ ({pages} صفحة)")
        except Exception as e:
            log.exception("failed")
            bot.send_message(m.chat.id, f"صار خطأ: {e}")
        finally:
            for p in (src, dst):
                try:
                    os.remove(p)
                except OSError:
                    pass

    log.info("bot started")
    bot.infinity_polling(skip_pending=True, timeout=30,
                         long_polling_timeout=30)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        out = os.path.splitext(sys.argv[1])[0] + "_AR.pdf"
        print(translate_pdf(sys.argv[1], out,
                            lambda k, n: print(f"batch {k}/{n}")), "->", out)
    else:
        run_bot()
