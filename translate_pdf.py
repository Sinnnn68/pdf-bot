#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate_pdf.py  (النسخة المصحّحة v3)
=====================================
يترجم ملف PDF (إنجليزي -> عربي) بوضع الترجمة العربية **أسفل كل فقرة مباشرة**
داخل نفس الصفحة، مع الحفاظ الكامل على النص الأصلي والصور والرسوم.

سبب المشاكل في النسخ السابقة وكيف عولجت
---------------------------------------
* الحروف العربية كانت تُرسم بدون تشكيل في بعض الحالات -> الآن نُشكّل **دائماً**
  (arabic_reshaper + bidi) قبل الرسم.
* خط Amiri لم يكن يُبحث عنه إلا في مسار واحد -> الآن نبحث في عدّة مسارات
  (assets/fonts، مجلد الملف، مجلد العمل، /app/assets/fonts، AR_FONT_PATH).
* **التراكب (التداخل بين الترجمة والنص الإنجليزي)** كان سببه أننا نضع الترجمة
  عند "منتصف المسافة بين خطوط الأساس" وهو موضع أعلى من نهاية النص المرئية.
  الحل: تُوضع الترجمة عند **الحدّ السفلي البصري** لآخر سطر في الفقرة
  (خط الأساس + عمق الخط)، وتُفتح فجوة هناك تماماً.
* **تقطيع الحروف الإنجليزية** كان سببه أن حدود الشرائح كانت تقع داخل النص لأن
  صناديق الأسطر (bbox) في بعض الملفات متداخلة ومنتفخة. الآن حدود الشرائح هي
  نفسها الحدود السفلية البصرية للفقرات -> لا يمرّ أي حدّ عبر حرف مرئي.
* تقسيم الفقرات الآن حسب **الفراغ البصري** بين الأسطر (لا حسب bbox الخام).

الواجهة العامة التي يستخدمها main.py:

    from translate_pdf import translate_pdf
    translate_pdf(input_path, output_path, translator, progress_cb=None) -> dict
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path

try:
    import pymupdf as fitz
except Exception:                                    # pragma: no cover
    import fitz

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:                                    # pragma: no cover
    arabic_reshaper = None
    get_display = None

log = logging.getLogger("translate_pdf")


# ==========================================================================
# 1) البحث عن خط Amiri في عدّة مسارات
# ==========================================================================
def _font_candidates(filename: str) -> list[Path]:
    out: list[Path] = []
    for var in ("AR_FONT_PATH", "AR_FONT_DIR", "FONT_DIR"):
        val = (os.getenv(var) or "").strip()
        if not val:
            continue
        p = Path(val)
        out.append(p / filename if p.is_dir() else p)
    here = Path(__file__).resolve().parent
    for base in (
        here / "assets" / "fonts", here / "assets", here / "fonts", here,
        Path.cwd() / "assets" / "fonts", Path.cwd() / "fonts",
        Path("/app/assets/fonts"), Path("/app/fonts"), Path("/app"),
        Path("/usr/share/fonts/truetype/amiri"),
        Path("/usr/share/fonts/truetype/freefont"),
    ):
        out.append(base / filename)
    return out


def _find_font(*filenames: str) -> Path | None:
    for fn in filenames:
        for cand in _font_candidates(fn):
            try:
                if cand.is_file() and cand.stat().st_size > 1000:
                    return cand
            except Exception:
                continue
    return None


def _download_fonts() -> None:
    """تحميل خطي Amiri تلقائياً إن لم يكونا موجودين (شبكة Railway متاحة)."""
    try:
        import urllib.request
    except Exception:                                    # pragma: no cover
        return
    target_dir = Path(__file__).resolve().parent / "assets" / "fonts"
    sources = {
        "Amiri-Regular.ttf": [
            "https://github.com/aliftype/amiri/raw/main/fonts/Amiri-Regular.ttf",
            "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Regular.ttf",
        ],
        "Amiri-Bold.ttf": [
            "https://github.com/aliftype/amiri/raw/main/fonts/Amiri-Bold.ttf",
            "https://raw.githubusercontent.com/google/fonts/main/ofl/amiri/Amiri-Bold.ttf",
        ],
    }
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:                             # pragma: no cover
        log.error("تعذّر إنشاء مجلد الخطوط %s: %s", target_dir, exc)
        return
    for fname, urls in sources.items():
        dest = target_dir / fname
        if dest.is_file() and dest.stat().st_size > 1000:
            continue
        for url in urls:
            try:
                log.info("جارٍ تحميل الخط %s من %s", fname, url)
                # مهلة 10 ثوانٍ: بدونها قد يتجمّد البوت عند الاستيراد إن كانت الشبكة محجوبة
                with urllib.request.urlopen(url, timeout=10) as resp:
                    dest.write_bytes(resp.read())
                if dest.is_file() and dest.stat().st_size > 1000:
                    log.info("تم تحميل %s بنجاح (%d بايت)", fname, dest.stat().st_size)
                    break
                dest.unlink(missing_ok=True)
            except Exception as exc:
                log.warning("فشل تحميل %s من %s: %s", fname, url, exc)


FONT_AR_REG = _find_font("Amiri-Regular.ttf", "Amiri-Regular.otf",
                         "NotoNaskhArabic-Regular.ttf", "FreeSerif.ttf")
FONT_AR_BOLD = _find_font("Amiri-Bold.ttf", "Amiri-Bold.otf",
                          "NotoNaskhArabic-Bold.ttf", "FreeSerifBold.ttf")

# إن لم يوجد الخط، نحاول تحميله تلقائياً ثم نعيد البحث.
if FONT_AR_REG is None:
    log.warning("لم يُعثر على خط عربي - محاولة تحميله تلقائياً...")
    _download_fonts()
    FONT_AR_REG = _find_font("Amiri-Regular.ttf", "Amiri-Regular.otf",
                             "NotoNaskhArabic-Regular.ttf", "FreeSerif.ttf")
    FONT_AR_BOLD = _find_font("Amiri-Bold.ttf", "Amiri-Bold.otf",
                              "NotoNaskhArabic-Bold.ttf", "FreeSerifBold.ttf")

if FONT_AR_REG is None:
    log.error("❌ لم يُعثر على خط عربي ولم ينجح تحميله تلقائياً. "
              "ارفع مجلد assets/fonts مع Amiri-Regular.ttf، أو اضبط AR_FONT_PATH. "
              "بدون خط عربي ستظهر الترجمة كمربعات فارغة!")
elif FONT_AR_BOLD is None:
    log.warning("لم يُعثر على Amiri-Bold.ttf - سيُستخدم الخط العادي للعناوين.")
else:
    log.info("خط عربي: %s", FONT_AR_REG)

_AR_FONT_NAME = "amiri"
_AR_FONT_REGISTERED = False
_AR_FONT_OBJ = None


def _init_arabic_font():
    global _AR_FONT_REGISTERED, _AR_FONT_OBJ
    if _AR_FONT_REGISTERED:
        return True
    if FONT_AR_REG is None:
        return False
    try:
        _AR_FONT_OBJ = fitz.Font(fontfile=str(FONT_AR_REG))
        _AR_FONT_REGISTERED = True
        log.info("تم تحميل قياسات الخط العربي: %s", FONT_AR_REG)
        return True
    except Exception as exc:                          # pragma: no cover
        log.error("تعذّر تحميل قياسات الخط (%s): %s", FONT_AR_REG, exc)
        return False


# ==========================================================================
# 2) إعدادات عبر متغيّرات البيئة
# ==========================================================================
TRANSLATE_MODE = os.getenv("TRANSLATE_MODE", "below").strip().lower()
if TRANSLATE_MODE in ("keep_source",):
    TRANSLATE_MODE = "below"
if TRANSLATE_MODE not in ("below", "bilingual", "replace"):
    TRANSLATE_MODE = "below"

AR_FONT_SCALE = float(os.getenv("AR_FONT_SCALE", "0.85"))
AR_FONT_MIN = float(os.getenv("AR_FONT_MIN", "7.5"))
AR_FONT_MAX = float(os.getenv("AR_FONT_MAX", "40"))
AR_LEADING = float(os.getenv("AR_LEADING", "1.30"))
AR_GAP = float(os.getenv("AR_GAP", "4"))          # فجوة أمان فوق الترجمة
AR_MARGIN = float(os.getenv("AR_MARGIN", "22"))   # هامش يمين/يسار
AR_COLOR_HEX = os.getenv("AR_COLOR", "#9E1B1B")

# نسب عمق/ارتفاع الخط لحساب الحدّ البصري (بلا اعتماد على bbox المنتفخ)
INK_DESCENT = float(os.getenv("INK_DESCENT", "0.30"))   # كم ينزل الحبر تحت خط الأساس
INK_ASCENT = float(os.getenv("INK_ASCENT", "0.78"))     # كم يصعد الحبر فوقه

ARABIC_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
PRESENTATION = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
LATIN_RANGE = re.compile(r"[A-Za-z]")


# ==========================================================================
# 3) أدوات النص العربي
# ==========================================================================
def has_arabic(text: str) -> bool:
    return bool(ARABIC_RANGE.search(text or ""))


def arabic_ratio(text: str) -> float:
    if not text:
        return 0.0
    ar = len(ARABIC_RANGE.findall(text))
    la = len(LATIN_RANGE.findall(text))
    total = ar + la
    return (ar / total) if total else 0.0


def detect_lang(text: str) -> str:
    """يُرجّح العربي إذا كان فيه 3 كلمات عربية على الأقل ونسبة العربي >= 45%."""
    ratio = arabic_ratio(text)
    words = re.findall(r"[\u0600-\u06FF]{2,}", text or "")
    if len(words) >= 2 and ratio >= 0.45:
        return "ar"
    return "ar" if ratio >= 0.6 else "en"


# رموز شائعة غير موجودة في خط Amiri -> بدائل تُعرض بشكل صحيح (تفادي المربعات)
_SYMBOL_FALLBACK = {
    "\u03b2": "B", "\u03b1": "a", "\u03b3": "g", "\u03b4": "d",
    "\u2022": "-", "\u25cf": "-", "\u25aa": "-", "\u25e6": "-",
    "\u2192": "<-", "\u2190": "<-", "\u2194": "<->",
    "\u2265": ">=", "\u2264": "<=", "\u2260": "!=",
    "\u00b5": "u", "\u00b0": "deg", "\u00d7": "x", "\u2044": "/",
}


def _fix_unsupported(line: str) -> str:
    """يستبدل/يحذف أي رمز لا يدعمه خط Amiri حتى لا يظهر مربع فارغ.

    الفكرة: بعض الرموز (مثل الحرف اليوناني beta في 'β adrenergic') غير موجودة
    في خط Amiri فتظهر كمربع ▯. نستبدلها ببديل مدعوم، وإن لم يوجد نُسقطها.
    """
    if not line or _AR_FONT_OBJ is None:
        return line
    out = []
    for ch in line:
        if ch in ("\n", "\t", " "):
            out.append(ch)
            continue
        try:
            supported = bool(_AR_FONT_OBJ.has_glyph(ord(ch)))
        except Exception:
            supported = True
        if supported:
            out.append(ch)
            continue
        if ch in _SYMBOL_FALLBACK:
            out.append(_SYMBOL_FALLBACK[ch])
        elif ch.isascii():
            out.append(ch)          # لاتيني/رقم: مدعوم في الخط الأساسي
        # غير ذلك: نُسقطه بدل إظهار مربع فارغ
    return "".join(out)


def shape_line(line: str) -> str:
    """تشكيل الحروف العربية + الاتجاه RTL (يُطبَّق دائماً قبل الرسم)."""
    if not line:
        return line
    if PRESENTATION.search(line):
        line = unicodedata.normalize("NFKC", line)
    line = _fix_unsupported(line)
    if not has_arabic(line):
        return line
    if arabic_reshaper is None or get_display is None:
        log.error("arabic-reshaper / python-bidi غير مثبّتين - الحروف ستظهر منفصلة!")
        return line
    try:
        return get_display(arabic_reshaper.reshape(line), base_dir="R")
    except Exception as exc:                          # pragma: no cover
        log.warning("فشل تشكيل الحروف: %s", exc)
        return line


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _width(text: str, size: float) -> float:
    if _AR_FONT_OBJ is not None:
        try:
            return _AR_FONT_OBJ.text_length(text, fontsize=size)
        except Exception:
            pass
    try:
        return fitz.get_text_length(text, fontname="helv", fontsize=size)
    except Exception:
        return len(text) * size * 0.5


def _wrap_logical(text: str, size: float, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        para = para.strip()
        if not para:
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            trial = word if not cur else cur + " " + word
            if not cur or _width(shape_line(trial), size) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def _layout(text: str, size: float, max_w: float) -> tuple[list[str], float]:
    """يُصغّر الخط تدريجياً حتى تتّسع أطول كلمة (يمنع الخروج عن الصفحة)."""
    s = max(AR_FONT_MIN, min(AR_FONT_MAX, size))
    while s >= AR_FONT_MIN:
        lines = _wrap_logical(text, s, max_w)
        widest = max((_width(shape_line(ln), s) for ln in lines if ln), default=0.0)
        if widest <= max_w or s <= AR_FONT_MIN:
            return lines, s
        s = round(s - 0.5, 2)
    return _wrap_logical(text, AR_FONT_MIN, max_w), AR_FONT_MIN


def _rtl_height(text: str, size: float, max_w: float) -> float:
    lines, used = _layout(text, size, max_w)
    n = max(1, len([ln for ln in lines if ln.strip()]))
    return n * used * AR_LEADING


def _draw_rtl_lines(page, x_right: float, top: float, text: str,
                    size: float, color: str, x_left: float | None = None) -> int:
    """يرسم الترجمة العربية محاذيةً لليمين داخل الشريط [x_left, x_right]."""
    if x_left is None:
        x_left = AR_MARGIN
    max_w = max(40.0, x_right - x_left)
    lines, used = _layout(text, size, max_w)
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return 0
    leading = used * AR_LEADING
    baseline = top + used * 0.85
    col = _hex_to_rgb(color)
    page_h = page.rect.height
    drawn = 0
    for ln in lines:
        if baseline > page_h - 4:
            break
        shaped = shape_line(ln)
        w = _width(shaped, used)
        x = max(x_left, x_right - w)
        try:
            page.insert_text(fitz.Point(x, baseline), shaped,
                             fontname=_AR_FONT_NAME, fontfile=str(FONT_AR_REG),
                             fontsize=used, color=col)
            drawn += 1
        except Exception as exc:
            log.warning("فشل إدراج سطر عربي: %s", exc)
        baseline += leading
    return drawn


def _detect_columns(blocks: list[dict], W: float) -> list[tuple[float, float]]:
    """يكتشف الأعمدة من مواضع الكتل أفقياً.

    يرجع قائمة أشرطة (x0, x1) لكل عمود. لملف بعمود واحد يرجع شريطاً واحداً
    يغطي كامل عرض النص. لملف بعمودين (أو جدول) يرجع شريطاً لكل عمود حتى
    تُوضع ترجمة كل كتلة داخل عمودها لا في عمود منفصل.
    """
    infos = []
    for b in blocks:
        bb = b.get("bbox", (0, 0, W, 0))
        x0, x1 = float(bb[0]), float(bb[2])
        if x1 - x0 < 1:
            continue
        infos.append((x0, x1, (x0 + x1) / 2.0))
    if not infos:
        return [(AR_MARGIN, W - AR_MARGIN)]
    centers = sorted(c for _x0, _x1, c in infos)
    gap_thr = 0.12 * W
    clusters: list[list[float]] = [[centers[0]]]
    for c in centers[1:]:
        if c - clusters[-1][-1] > gap_thr:
            clusters.append([c])
        else:
            clusters[-1].append(c)
    bands = []
    for cl in clusters:
        lo, hi = min(cl), max(cl)
        x0 = min(i[0] for i in infos if lo - 0.5 <= i[2] <= hi + 0.5)
        x1 = max(i[1] for i in infos if lo - 0.5 <= i[2] <= hi + 0.5)
        bands.append((x0, x1))
    bands.sort(key=lambda b: b[0])
    return bands


def _band_for(block: dict, bands: list[tuple[float, float]], W: float) -> tuple[float, float]:
    """يختار الشريط (العمود) الأقرب لمركز الكتلة."""
    bb = block.get("bbox", (0, 0, W, 0))
    cx = (float(bb[0]) + float(bb[2])) / 2.0
    best, bestd = bands[0], 1e9
    for (x0, x1) in bands:
        d = abs(cx - (x0 + x1) / 2.0)
        if d < bestd:
            best, bestd = (x0, x1), d
    return best


def _ar_font_size(en_size: float) -> float:
    return min(AR_FONT_MAX, max(AR_FONT_MIN, en_size * AR_FONT_SCALE))


# ==========================================================================
# 4) أدوات الكتل النصية + تقسيم الفقرات بالفراغ البصري
# ==========================================================================
def _line_span(ln: dict) -> dict:
    spans = ln.get("spans", [])
    sp = max(spans, key=lambda s: len(s.get("text", ""))) if spans else {}
    return sp


def _line_text(ln: dict) -> str:
    return "".join(s.get("text", "") for s in ln.get("spans", [])).strip()


def _line_baseline(ln: dict) -> float:
    sp = _line_span(ln)
    o = sp.get("origin")
    if o:
        return float(o[1])
    size = sp.get("size", 11)
    return float(ln.get("bbox", (0, 0, 0, 0))[3]) - 0.25 * size


def _line_size(ln: dict) -> float:
    sp = _line_span(ln)
    return float(sp.get("size", 11))


def _block_text(block: dict) -> str:
    return "\n".join(_line_text(ln) for ln in block.get("lines", []) if _line_text(ln)).strip()


def _block_style(block: dict) -> tuple[float, str, bool]:
    sizes, colors, bold = [], [], False
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            if span.get("text", "").strip():
                sizes.append(span.get("size", 11))
                colors.append(span.get("color", 0))
                if span.get("flags", 0) & 2 ** 4:
                    bold = True
    size = max(sizes) if sizes else 11.0
    color = "#000000"
    if colors:
        c = max(set(colors), key=colors.count)
        try:
            color = f"#{int(c):06x}"
        except Exception:
            color = "#000000"
    return size, color, bold


_BULLET_RE = re.compile(r"^[\u2022\u25aa\u25e6\u25cf\-\u2013*]|^[\(\[]?[A-Za-z0-9]{1,3}[\)\].-]\s")


def _block_segments(block: dict) -> list[dict]:
    """
    تقسيم الكتلة إلى مقاطع (فقرات/نقاط) حسب **الفراغ البصري** بين الأسطر:
    الفراغ = (خط أساس التالي - 0.78*حجمه) - (خط أساس الحالي + 0.30*حجمه).
    الحدّ السفلي للمقطع = خط أساس آخر سطر + 0.30*حجمه (نهاية الحبر المرئية).
    """
    rows = []
    for ln in block.get("lines", []):
        txt = _line_text(ln)
        if txt:
            rows.append((_line_baseline(ln), _line_size(ln), txt))
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])

    vgaps = []
    for i in range(len(rows) - 1):
        b0, s0, _ = rows[i]
        b1, s1, _ = rows[i + 1]
        vgaps.append((b1 - INK_ASCENT * s1) - (b0 + INK_DESCENT * s0))
    if vgaps:
        med = sorted(vgaps)[len(vgaps) // 2]
        thr = max(med * 1.8, 2.0)
    else:
        med, thr = 2.0, 2.0

    segments, current = [], [rows[0]]
    for i in range(1, len(rows)):
        b0, s0, _ = rows[i - 1]
        b1, s1, txt = rows[i]
        vg = (b1 - INK_ASCENT * s1) - (b0 + INK_DESCENT * s0)
        if vg > thr or _BULLET_RE.match(txt):
            segments.append(current)
            current = [rows[i]]
        else:
            current.append(rows[i])
    segments.append(current)

    out = []
    for seg in segments:
        text = "\n".join(r[2] for r in seg)
        if not LATIN_RANGE.search(text):
            continue
        first_b, first_s, _ = min(seg, key=lambda r: r[0])
        last_b, last_s, _ = max(seg, key=lambda r: r[0])
        out.append({"text": text,
                    "ink_top": first_b - INK_ASCENT * first_s,
                    "ink_bottom": last_b + INK_DESCENT * last_s,
                    "en_size": last_s})
    return out


def _translate_all(translator, texts: list[str]) -> list[str]:
    if hasattr(translator, "translate_all"):
        try:
            out = translator.translate_all(texts, None)
            if isinstance(out, list) and len(out) == len(texts):
                return out
        except TypeError:
            try:
                out = translator.translate_all(texts)
                if isinstance(out, list) and len(out) == len(texts):
                    return out
            except Exception as exc:
                log.warning("translate_all فشل: %s", exc)
        except Exception as exc:
            log.warning("translate_all فشل: %s", exc)
    return [translator.translate(t) for t in texts]


# ==========================================================================
# 5) إعادة بناء الصفحة مع إدراج الترجمة أسفل كل فقرة
# ==========================================================================
def _append_below_page(out_doc, src_doc, pno: int, translator) -> dict:
    src_page = src_doc[pno]
    W, H = float(src_page.rect.width), float(src_page.rect.height)
    blocks = [b for b in src_page.get_text("dict")["blocks"]
              if b.get("type") == 0 and _block_text(b)]
    if not blocks:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "inserted": 0, "added_h": 0.0, "blocks": 0}

    latin = [b for b in blocks if detect_lang(_block_text(b)) == "en"]
    if not latin:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "inserted": 0, "added_h": 0.0, "blocks": 0}

    # اكتشاف الأعمدة: تُوضع ترجمة كل كتلة داخل عمودها (يدعم عمودين/جداول)
    bands = _detect_columns(latin, W)

    segs = []
    for b in latin:
        bx0, bx1 = _band_for(b, bands, W)
        for seg in _block_segments(b):
            seg["band"] = (bx0, bx1)
            segs.append(seg)
    if not segs:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "inserted": 0, "added_h": 0.0, "blocks": 0}

    translations = _translate_all(translator, [s["text"] for s in segs])

    items = []
    for seg, tr in zip(segs, translations):
        tr = (tr or "").strip()
        if not tr:
            continue
        bx0, bx1 = seg.get("band", (AR_MARGIN, W - AR_MARGIN))
        # هامش داخلي صغير داخل العمود حتى لا يلامس حدوده
        x_left = max(AR_MARGIN, bx0 + 2)
        x_right = min(W - AR_MARGIN, bx1 - 2)
        if x_right - x_left < 40:
            x_left, x_right = AR_MARGIN, W - AR_MARGIN
        ar_size = _ar_font_size(_line_size_from_seg(seg))
        clear = AR_GAP
        ah = clear + _rtl_height(tr, ar_size, x_right - x_left)
        items.append({"anchor": seg["ink_bottom"], "ar_size": ar_size,
                      "ar": tr, "clear": clear, "ah": ah,
                      "ink_top": seg.get("ink_top", seg["ink_bottom"]),
                      "x_left": x_left, "x_right": x_right})
    if not items:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "inserted": 0, "added_h": 0.0, "blocks": 0}

    # تجميع العناصر حسب المستوى الرأسي (نفس خط الأساس):
    # عناصر نفس المستوى (عمودان / صف جدول) تُرسم **جنباً إلى جنب** في أشرطتها،
    # والمسافة المُدرجة عند هذا المستوى = أطول ترجمة فيه (لا مجموعها).
    groups: dict[float, list[dict]] = {}
    for it in items:
        key = round(it["anchor"], 1)
        groups.setdefault(key, []).append(it)
    anchors = sorted(groups)
    group_h = {a: max(it["ah"] for it in groups[a]) for a in anchors}

    shift: dict[float, float] = {}
    acc = 0.0
    for a in anchors:
        shift[a] = acc
        acc += group_h[a]
    total_h = acc

    page = out_doc.new_page(width=W, height=H + total_h)

    # حدود الشرائح = منتصف الفراغ الأبيض بين الفقرات (لا تمرّ عبر أي حرف)
    # لكل مستوى: أعلى حبر (ink_top) وأسفل حبر (ink_bottom). القطع بين مستوى
    # ومستوى يقع في منتصف المسافة بين أسفل حبر الأول وأعلى حبر التالي.
    top_of = {a: min(it["ink_top"] for it in groups[a]) for a in anchors}
    bot_of = {a: max(it["anchor"] for it in groups[a]) for a in anchors}
    cuts = [0.0]
    for i in range(len(anchors) - 1):
        a, a2 = anchors[i], anchors[i + 1]
        cut = (bot_of[a] + top_of[a2]) / 2.0
        cut = min(max(cut, bot_of[a]), top_of[a2]) if top_of[a2] > bot_of[a] else bot_of[a]
        cuts.append(cut)
    cuts.append(H)
    bounds = sorted({min(max(c, 0.0), H) for c in cuts})

    for k in range(len(bounds) - 1):
        y0, y1 = bounds[k], bounds[k + 1]
        if y1 - y0 <= 0.01:
            continue
        strip_shift = sum(group_h[a] for a in anchors if bot_of[a] <= y0 + 0.01)
        # تراكب صغير (0.75pt) بين الشرائح لإخفاء خطوط الوصل البيضاء
        ov = 0.75
        cy0 = max(0.0, y0 - ov)
        cy1 = min(H, y1 + ov)
        try:
            page.show_pdf_page(
                fitz.Rect(0, cy0 + strip_shift, W, cy1 + strip_shift), src_doc, pno,
                clip=fitz.Rect(0, cy0, W, cy1), keep_proportion=False)
        except Exception as exc:
            log.warning("فشل استيراد شريط الصفحة %d (%.0f-%.0f): %s", pno + 1, y0, y1, exc)

    inserted = 0
    for a in anchors:
        for it in groups[a]:
            top = a + shift[a] + it["clear"]
            if _draw_rtl_lines(page, it["x_right"], top, it["ar"], it["ar_size"],
                               AR_COLOR_HEX, x_left=it["x_left"]):
                inserted += 1

    return {"page": pno + 1, "inserted": inserted,
            "added_h": round(total_h, 1), "blocks": len(items)}


def _line_size_from_seg(seg: dict) -> float:
    """حجم خط تقديري للمقطع (من نصه الخام)."""
    return seg.get("en_size", 11.0)


def _replace_mode_page(page, translator) -> int:
    blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
    jobs = []
    for b in blocks:
        text = _block_text(b)
        if not text:
            continue
        jobs.append((fitz.Rect(b["bbox"]), text, _block_style(b)))
    if not jobs:
        return 0
    translated = _translate_all(translator, [t for _r, t, _s in jobs])
    for (rect, _t, _s) in jobs:
        page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    done = 0
    for (rect, _t, (size, _c, _b)), tr in zip(jobs, translated):
        if _draw_rtl_lines(page, rect.x1, rect.y0, tr, max(6.0, size * 0.85), "#000000"):
            done += 1
    return done


# ==========================================================================
# 6) الواجهة العامة
# ==========================================================================
def translate_pdf(input_path, output_path, translator, progress_cb=None) -> dict:
    if not _init_arabic_font():
        # نوقف العملية بدل إرجاع PDF بلا ترجمة ورسالة "تمت الترجمة" كاذبة
        raise RuntimeError("الخط العربي غير متوفر. ارفع assets/fonts/Amiri-Regular.ttf "
                           "أو اضبط AR_FONT_PATH.")

    src_doc = fitz.open(str(input_path))
    stats = {
        "mode": TRANSLATE_MODE, "pages": src_doc.page_count,
        "blocks": 0, "translated_blocks": 0,
        "images_before": 0, "images_after": 0,
        "drawings_before": 0, "drawings_after": 0,
        "arabic_font": str(FONT_AR_REG) if FONT_AR_REG else None,
        "pages_detail": [],
    }

    for pno in range(src_doc.page_count):
        p = src_doc[pno]
        stats["images_before"] += len(p.get_images(full=True))
        stats["drawings_before"] += len(p.get_drawings())
        for b in p.get_text("dict")["blocks"]:
            if b.get("type") == 0 and _block_text(b):
                stats["blocks"] += 1

    if TRANSLATE_MODE == "replace":
        out_doc = fitz.open(str(input_path))
        for pno in range(out_doc.page_count):
            stats["translated_blocks"] += _replace_mode_page(out_doc[pno], translator)
            if progress_cb:
                progress_cb(pno + 1, out_doc.page_count)
    else:
        out_doc = fitz.open()
        for pno in range(src_doc.page_count):
            info = _append_below_page(out_doc, src_doc, pno, translator)
            stats["translated_blocks"] += info["inserted"]
            stats["pages_detail"].append(info)
            if progress_cb:
                progress_cb(pno + 1, src_doc.page_count)

    for pno in range(out_doc.page_count):
        p = out_doc[pno]
        stats["images_after"] += len(p.get_images(full=True))
        stats["drawings_after"] += len(p.get_drawings())

    out_doc.save(str(output_path), garbage=3, deflate=True)
    out_doc.close()
    src_doc.close()

    stats["ok"] = True
    stats["input"] = str(input_path)
    stats["output"] = str(output_path)
    stats["images_preserved"] = stats["images_after"] >= stats["images_before"]
    stats["drawings_preserved"] = stats["drawings_after"] >= stats["drawings_before"]
    log.info("تمت الترجمة: mode=%s blocks=%d translated=%d imgs %d->%d draws %d->%d",
             TRANSLATE_MODE, stats["blocks"], stats["translated_blocks"],
             stats["images_before"], stats["images_after"],
             stats["drawings_before"], stats["drawings_after"])
    return stats
