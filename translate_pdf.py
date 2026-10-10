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
