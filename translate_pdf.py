# -*- coding: utf-8 -*-
"""
translate_pdf.py  -  ترجمة PDF للعربي مع الحفاظ على الصور والتصميم.

الفكرة:
  * ما نبني ملف جديد من الصفر. نقص كل شريحة (صفحة) لشرائح أفقية.
  * بين كل فقرة والفقرة اللي بعدها نحط شريط فيه الترجمة (تحت الفقرة مو فوقها).
  * الصور والخلفية تنسخ مثل ما هي (show_pdf_page) فما تختفي.

الاستخدام:
    from translate_pdf import translate_pdf
    translate_pdf("input.pdf", "output.pdf")

أو من الطرفية:
    python translate_pdf.py input.pdf output.pdf
"""
import asyncio
import html
import inspect
import math
import os
import re
import sys
import time

try:                      # الاسم الجديد للمكتبة
    import pymupdf as fitz
except ImportError:       # الاسم القديم
    try:
        import fitz
    except ImportError:   # نسمح بالاستيراد حتى بدون المكتبة (للاختبار فقط)
        fitz = None

# ----------------------------- إعدادات يمكنك تغييرها -----------------------------
TARGET_LANG = "ar"
ARABIC_COLOR = "#d00000"     # لون الترجمة (أحمر)
MIN_FONT, MAX_FONT = 11, 16  # حجم خط الترجمة (نقطة)
# ملف الخط العربي (نفس اللي رفعته جنب main.py). لو ما موجود يستخدم خط المكتبة.
ARABIC_FONT_FILE = "Amiri-Regular.ttf"
# ----------------------------------------------------------------------------------

BULLET_RE = re.compile(r"^\s*(?:[•·▪●○◦■▶►‣⁃*]|[-–—](?=\s|[A-Za-z]))\s*")


# =====================================================================
# 1) دوال الحساب (ما تحتاج fitz - كلها منطق صافي)
# =====================================================================
def group_paragraphs(lines):
    """
    lines: قائمة أسطر بترتيب القراءة. كل سطر dict فيه:
           x0, y0, x1, y1, size, text, (new_block اختياري)
    ترجع قائمة فقرات. الفقرة الجديدة تبدأ عند: نقطة (•) / بلوك جديد /
    فراغ عمودي كبير / تغيّر حجم الخط.
    """
    paras, cur, prev = [], None, None
    for ln in lines:
        raw = ln["text"].strip()
        if not raw:
            continue
        is_bullet = bool(BULLET_RE.match(raw))
        body = BULLET_RE.sub("", raw, count=1).strip() if is_bullet else raw
        if is_bullet and not body:        # نقطة لوحدها بدون نص
            continue

        start = cur is None or is_bullet or ln.get("new_block", False)
        if not start and prev is not None:
            h = max(prev["y1"] - prev["y0"], 1.0)
            gap = ln["y0"] - prev["y1"]
            # سطر مكمّل لنفس الجملة: السطر اللي قبله ما انتهى بنقطة والحالي يبدأ بحرف صغير
            cont = (not re.search(r"[.!?:;؟]$", prev["text"].strip())) and raw[:1].islower()
            if abs(ln["size"] - prev["size"]) > 1.5:
                start = True
            elif gap > 2.5 * h:
                start = True
            elif gap > 0.6 * h and not cont:
                start = True

        if start:
            cur = {"x0": ln["x0"], "y0": ln["y0"], "x1": ln["x1"], "y1": ln["y1"],
                   "size": ln["size"], "bullet": is_bullet, "parts": [body]}
            paras.append(cur)
        else:
            cur["x0"] = min(cur["x0"], ln["x0"])
            cur["x1"] = max(cur["x1"], ln["x1"])
            cur["y1"] = max(cur["y1"], ln["y1"])
            cur["parts"].append(body)
        prev = ln

    for p in paras:
        p["text"] = " ".join(p["parts"]).strip()
    return paras


def has_letters(text):
    return len(re.findall(r"[^\W\d_]", text)) >= 2


def cluster_rows(paras):
    """نجمع الفقرات اللي بنفس الارتفاع (أعمدة جنب بعض) بصف واحد."""
    rows = []
    for p in sorted(paras, key=lambda q: (q["y0"], q["x0"])):
        if rows and p["y0"] < rows[-1]["y1"] - 2:
            r = rows[-1]
            r["paras"].append(p)
            r["y1"] = max(r["y1"], p["y1"])
        else:
            rows.append({"y0": p["y0"], "y1": p["y1"], "paras": [p]})
    return rows


def choose_cut(y_bottom, y_limit, page_h, is_blank_row, zones=(), max_search=30):
    """
    نختار مكان القص تحت الفقرة:
      1) أول سطر فاضي كلياً (بدون نص/صورة) قريب من أسفل الفقرة.
      2) وإلا مباشرة تحت الفقرة.
    zones = مناطق الصور (y0, y1): ممنوع نقص داخلها. لو الفقرة تتداخل مع صورة
    ننزل لأسفل الصورة (نفضّل الترجمة تحت الصورة على إنها تقص الصورة نصفين).
    """
    def ok(y):
        return not any(z0 < y < z1 for z0, z1 in zones)

    start = int(math.ceil(y_bottom)) + 1
    for y in range(start, int(min(y_limit, page_h, start + max_search))):
        if is_blank_row(y) and ok(y):
            return float(y)
    y = start
    guard = 0
    while y < page_h and not ok(y) and guard < 50:
        y = int(math.ceil(max(z1 for z0, z1 in zones if z0 < y < z1)))
        guard += 1
    return float(min(y, page_h))


def plan_page(lines, page_w, page_h, is_blank_row, zones=()):
    """
    ترجع: groups = قائمة أماكن قص. كل مكان: cut + rows (الصفوف اللي ترجمتها
    تنحط هناك). و content_right = أقصى يمين للنص (لمحاذاة الترجمة).
    """
    paras = group_paragraphs(lines)
    rows = cluster_rows(paras)
    groups = []
    prev_cut = 0.0
    for i, r in enumerate(rows):
        next_top = rows[i + 1]["y0"] if i + 1 < len(rows) else page_h
        cut = choose_cut(r["y1"], next_top, page_h, is_blank_row, zones)
        cut = min(max(cut, prev_cut + 1.0), page_h)
        todo = [p for p in r["paras"] if has_letters(p["text"])]
        row = {"y0": r["y0"], "y1": r["y1"], "paras": todo}
        if groups and abs(cut - groups[-1]["cut"]) < 1.5:   # نفس مكان القص
            groups[-1]["rows"].append(row)
        else:
            groups.append({"cut": cut, "rows": [row]})
        prev_cut = groups[-1]["cut"]
    content_right = max([p["x1"] for p in paras], default=page_w - 20)
    return {"groups": groups, "content_right": content_right}


def box_geometry(row, page_w, content_right):
    """لكل فقرة بالصف: مكان مربع الترجمة (x0,x1) والمحاذاة."""
    boxes = []
    if len(row["paras"]) == 1:
        p = row["paras"][0]
        cx = (p["x0"] + p["x1"]) / 2.0
        if abs(cx - page_w / 2.0) < 0.04 * page_w and (p["x1"] - p["x0"]) < 0.7 * page_w:
            boxes.append((p, 20.0, page_w - 20.0, "center"))   # عنوان بالنص
        else:
            x0 = max(10.0, p["x0"] - 5)
            x1 = min(page_w - 10.0, max(content_right, p["x1"]) + 5)
            boxes.append((p, x0, x1, "right"))
    else:                                                      # أعمدة جنب بعض
        for p in row["paras"]:
            x0 = max(10.0, p["x0"] - 5)
            x1 = min(page_w - 10.0, max(p["x1"] + 5, x0 + 150))
            boxes.append((p, x0, x1, "right"))
    return boxes


def find_graphic_zones(get_row, n, w, h, text_rects, pad=2, min_height=8):
    """
    نلقى مناطق الصور/الرسوم من البكسلات: أي صف فيه شي مو نص (بعد استثناء مستطيلات
    النص) نعتبره صورة. هيج نعرف وين ممنوع نقص الصفحة حتى لو الصورة محفوظة
    بطريقة غريبة (pattern) وما تطلع بـ get_image_info.
    get_row(y) -> bytes لصف البكسلات (w*n بايت)
    """
    bgp = get_row(0)[:n]
    flagged = []
    for y in range(h):
        row = get_row(y)
        cover = sorted((max(0, int(r[0]) - pad), min(w, int(r[2]) + pad + 1))
                       for r in text_rects if r[1] - pad <= y <= r[3] + pad)
        pos, bad = 0, False
        for x0, x1 in cover + [(w, w)]:
            if x0 > pos:
                seg = row[pos * n: x0 * n]
                if seg != bgp * (x0 - pos):
                    bad = True
                    break
            pos = max(pos, x1)
        flagged.append(bad)
    zones, y = [], 0
    while y < h:
        if flagged[y]:
            y0 = y
            while y < h and (flagged[y] or (y + 2 < h and any(flagged[y:y + 3]))):
                y += 1
            if y - y0 >= min_height:
                zones.append((float(y0), float(y)))
        else:
            y += 1
    return zones


def estimate_height(text, width, size):
    cpl = max(1, int(width / (size * 0.48)))
    return math.ceil(len(text) / cpl) * size * 1.45 + 6


# =====================================================================
# 2) الترجمة
# =====================================================================
_cache = {}


def translate_text(text, retries=3):
    """
    الترجمة الافتراضية (مجانية): deep-translator. وإذا ما موجودة يجرّب googletrans.
    إذا عندك دالة ترجمة بالبوت (Gemini مثلاً) مرّرها لـ translate_pdf بدل هاي.
    """
    if text in _cache:
        return _cache[text]
    last = None
    for i in range(retries):
        try:
            try:
                from deep_translator import GoogleTranslator
                out = GoogleTranslator(source="auto", target=TARGET_LANG).translate(text[:4900])
            except ImportError:
                from googletrans import Translator
                res = Translator().translate(text, dest=TARGET_LANG)
                if inspect.isawaitable(res):
                    res = asyncio.run(res)
                out = res.text
            if out:
                _cache[text] = out
                return out
        except Exception as e:          # noqa
            last = e
        time.sleep(1.0 + i)
    print("translation failed:", last)
    return None


# =====================================================================
# 3) الرسم (يحتاج PyMuPDF)
# =====================================================================
def _line_dicts(page):
    """نطلع الأسطر من الصفحة بالشكل اللي يفهمه group_paragraphs."""
    lines = []
    d = page.get_text("dict")
    for b in sorted([b for b in d["blocks"] if b.get("type") == 0],
                    key=lambda b: (b["bbox"][1], b["bbox"][0])):
        first = True
        for l in b["lines"]:
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            x0, y0, x1, y1 = l["bbox"]
            lines.append({
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "size": max(s["size"] for s in spans),
                "text": "".join(s["text"] for s in l["spans"]),
                "new_block": first,
            })
            first = False
    return lines


def _pixel_helpers(page):
    pix = page.get_pixmap(alpha=False)              # 72dpi => 1 بكسل = 1 نقطة
    n, w, h = pix.n, pix.width, pix.height
    stride = getattr(pix, "stride", w * n)
    s = pix.samples
    y_off = page.rect.y0

    def row(y):
        yy = int(y - y_off)
        yy = min(max(yy, 0), h - 1)
        return s[yy * stride: yy * stride + w * n]

    def is_blank(y):
        if y - y_off >= h:
            return True
        r = row(y)
        return r == r[:n] * w

    def bg(y):
        r = row(y)[:n]
        if n >= 3:
            return tuple(c / 255.0 for c in r[:3])
        return (r[0] / 255.0,) * 3

    def zones(text_rects):
        return find_graphic_zones(lambda yy: row(yy + y_off), n, w, h, text_rects)

    return is_blank, bg, zones


def _image_zones(page):
    """مناطق الصور (y0, y1) حتى لا نقصها نصفين."""
    zones = []
    try:
        for info in page.get_image_info():
            y0, y1 = info["bbox"][1], info["bbox"][3]
            if 4 < (y1 - y0) < 0.9 * page.rect.height:
                zones.append((y0, y1))
    except Exception:
        pass
    return zones


def _font_setup():
    """يرجع (css, archive, family) لو فيه ملف خط عربي، وإلا بدون."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, ARABIC_FONT_FILE)
    if os.path.exists(path):
        css = '@font-face {font-family: arfont; src: url("%s");}' % ARABIC_FONT_FILE
        return css, fitz.Archive(here), "arfont"
    return "", None, "sans-serif"


def _ar_html(text, size, align, bullet, family):
    body = html.escape(text)
    if bullet:
        body = "• " + body
    return ('<p dir="rtl" style="margin:0; font-family:%s; font-size:%.1fpt; '
            'line-height:1.35; color:%s; text-align:%s;">%s</p>'
            % (family, size, ARABIC_COLOR, align, body))


def _measure(html_str, width, css, archive, text, size):
    try:
        story = fitz.Story(html=html_str, user_css=css or None, archive=archive)
        more, filled = story.place(fitz.Rect(0, 0, width, 6000))
        return filled.y1 + 6
    except Exception:
        return estimate_height(text, width, size)


def translate_pdf(input_path, output_path, translate=None, translate_many=None,
                  progress=None):
    """
    input_path  : ملف PDF الأصلي
    output_path : الملف الناتج (نفس الشرائح + الترجمة تحت كل فقرة)
    translate   : دالة ترجمة نص واحد (نص -> نص). الافتراضي deep-translator.
    translate_many : (اختياري) دالة تترجم قائمة نصوص مرة وحدة (قائمة -> قائمة بنفس
                  الترتيب). إذا موجودة تنستخدم بدل translate (أسرع مع Gemini).
    progress    : دالة اختيارية progress(page_number, total_pages)
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF غير مثبتة. نفّذ: pip install -U pymupdf")
    if not hasattr(fitz.Page, "insert_htmlbox"):
        raise RuntimeError("نسخة PyMuPDF قديمة. نفّذ: pip install -U pymupdf")

    translate = translate or translate_text
    css, archive, family = _font_setup()

    src = fitz.open(input_path)
    out = fitz.open()
    total = src.page_count

    for pno in range(total):
        page = src[pno]
        W, H = page.rect.width, page.rect.height
        is_blank, bg, graphic_zones = _pixel_helpers(page)
        lines = _line_dicts(page)
        text_rects = [(l["x0"], l["y0"], l["x1"], l["y1"]) for l in lines]
        zones = _image_zones(page) + graphic_zones(text_rects)
        plan = plan_page(lines, W, H, is_blank, zones)

        # 1) نترجم كل فقرات الصفحة، ونحسب ارتفاع كل شريط ترجمة
        all_paras = [p for g in plan["groups"] for r in g["rows"] for p in r["paras"]]
        texts = [p["text"] for p in all_paras]
        if translate_many:
            results = list(translate_many(texts)) if texts else []
        else:
            results = [translate(t) for t in texts]
        tr = {id(p): r for p, r in zip(all_paras, results)}

        strips = []
        for g in plan["groups"]:
            bands = []                      # كل band = صف ترجمات جنب بعض
            for row in g["rows"]:
                items, band_h = [], 0.0
                for p, x0, x1, align in box_geometry(row, W, plan["content_right"]):
                    ar = tr.get(id(p))
                    if not ar:
                        continue
                    size = max(MIN_FONT, min(MAX_FONT, p["size"] * 0.75))
                    h_html = _ar_html(ar, size, align, p["bullet"], family)
                    h = _measure(h_html, x1 - x0, css, archive, ar, size)
                    items.append((x0, x1, h_html))
                    band_h = max(band_h, h)
                if items:
                    bands.append((band_h, items))
            strip_h = sum(b[0] for b in bands) + 4 if bands else 0.0
            strips.append((g["cut"], bands, strip_h))

        # 2) نبني الصفحة الجديدة (أطول من الأصلية)
        extra = sum(s[2] for s in strips)
        newp = out.new_page(width=W, height=H + extra)
        src_y, dst_y = page.rect.y0, 0.0

        def put_slice(y_from, y_to):
            nonlocal dst_y
            if y_to - y_from < 0.5:
                return
            clip = fitz.Rect(page.rect.x0, y_from, page.rect.x1, y_to)
            dest = fitz.Rect(0, dst_y, W, dst_y + (y_to - y_from))
            newp.show_pdf_page(dest, src, pno, clip=clip, keep_proportion=False)
            dst_y += (y_to - y_from)

        for cut, bands, strip_h in strips:
            put_slice(src_y, cut)
            src_y = max(src_y, cut)
            if strip_h:
                fill = bg(min(cut, H - 1))
                newp.draw_rect(fitz.Rect(0, dst_y, W, dst_y + strip_h),
                               color=None, fill=fill)
                y = dst_y + 2
                for band_h, items in bands:
                    for x0, x1, h_html in items:
                        newp.insert_htmlbox(fitz.Rect(x0, y, x1, y + band_h),
                                            h_html, css=css or None,
                                            archive=archive)
                    y += band_h
                dst_y += strip_h
        put_slice(src_y, page.rect.y1)

        if progress:
            progress(pno + 1, total)

    out.save(output_path, garbage=4, deflate=True)
    out.close()
    src.close()
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: python translate_pdf.py input.pdf output.pdf")
        sys.exit(1)
    translate_pdf(sys.argv[1], sys.argv[2],
                  progress=lambda i, n: print("page %d/%d" % (i, n)))
    print("done:", sys.argv[2])
