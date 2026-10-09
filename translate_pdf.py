#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate_pdf.py
================
معالجة ملف PDF: استخراج النص -> ترجمته -> إعادة كتابته داخل نفس الملف
مع الحفاظ على التصميم الأصلي (الصور والرسوم والنص الأصلي تبقى كما هي).

الواجهة العامة التي يستخدمها main.py:

    from translate_pdf import translate_pdf
    translate_pdf(input_path, output_path, translator, progress_cb=None) -> dict

`translator` هو كائن Translator من translator.py (يوفّر .translate).
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

BASE_DIR = Path(__file__).resolve().parent

# الخطوط: إذا وُجد المجلد assets/fonts نستخدمه، وإلا نبحث بجانب الملف مباشرة
_FONT_SUBDIR = BASE_DIR / "assets" / "fonts"
FONT_DIR = _FONT_SUBDIR if _FONT_SUBDIR.is_dir() else BASE_DIR
FONT_AR_REG = FONT_DIR / "Amiri-Regular.ttf"
FONT_AR_BOLD = FONT_DIR / "Amiri-Bold.ttf"

# إعدادات قابلة للضبط عبر متغيّرات البيئة
TRANSLATE_MODE = os.getenv("TRANSLATE_MODE", "below").strip().lower()
if TRANSLATE_MODE in ("keep_source",):
    TRANSLATE_MODE = "below"
if TRANSLATE_MODE not in ("below", "bilingual", "replace"):
    TRANSLATE_MODE = "below"

AR_FONT_SCALE = float(os.getenv("AR_FONT_SCALE", "0.72"))
AR_FONT_MIN = float(os.getenv("AR_FONT_MIN", "8.5"))
AR_FONT_MAX = float(os.getenv("AR_FONT_MAX", "26"))
AR_LEADING = float(os.getenv("AR_LEADING", "1.35"))
AR_GAP = float(os.getenv("AR_GAP", "6"))
AR_MARGIN = float(os.getenv("AR_MARGIN", "20"))
AR_COLOR_HEX = os.getenv("AR_COLOR", "#9E1B1B")

ARABIC_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
PRESENTATION = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
LATIN_RANGE = re.compile(r"[A-Za-z]")

_AR_FONT_OBJ = None
if FONT_AR_REG.exists():
    try:
        _AR_FONT_OBJ = fitz.Font(fontfile=str(FONT_AR_REG))
    except Exception as exc:                         # pragma: no cover
        log.warning("تعذّر تحميل قياسات خط Amiri: %s", exc)
else:
    log.warning("ملف الخط غير موجود: %s", FONT_AR_REG)

_AR_FONT_NAME = "amiri"


# ==========================================================================
# أدوات النص
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
    return "ar" if arabic_ratio(text) >= 0.5 else "en"


def shape_arabic(text: str) -> str:
    if not has_arabic(text) or arabic_reshaper is None or get_display is None:
        return text
    return get_display(arabic_reshaper.reshape(text), base_dir="R")


def restore_reading_order(text: str) -> str:
    if not text:
        return text
    if not PRESENTATION.search(text):
        return unicodedata.normalize("NFKC", text)
    return "\n".join(unicodedata.normalize("NFKC", ln[::-1]) for ln in text.split("\n"))


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _text_width(text: str, size: float) -> float:
    if _AR_FONT_OBJ is not None and has_arabic(text):
        try:
            return _AR_FONT_OBJ.text_length(text, fontsize=size)
        except Exception:
            pass
    return fitz.get_text_length(text, fontname="helv", fontsize=size)


def _wrap_text(text: str, size: float, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in text.replace("\r", "").split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split(" "):
            trial = word if not cur else cur + " " + word
            if not cur or _text_width(trial, size) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def _rtl_height(text: str, size: float, max_w: float) -> float:
    lines = [ln for ln in _wrap_text(text, size, max_w) if ln.strip()]
    return max(1, len(lines)) * size * AR_LEADING


def _draw_rtl_lines(page, x_right: float, top: float, text: str,
                    size: float, color: str) -> int:
    lines = [ln for ln in _wrap_text(text, size, x_right - AR_MARGIN - 4) if ln.strip()]
    if not lines:
        return 0
    leading = size * AR_LEADING
    baseline = top + size * 0.95
    col = _hex_to_rgb(color)
    drawn = 0
    for ln in lines:
        shaped = shape_arabic(ln)
        w = _text_width(shaped, size)
        x = max(AR_MARGIN, x_right - w)
        try:
            page.insert_text(fitz.Point(x, baseline), shaped, fontname=_AR_FONT_NAME,
                             fontfile=str(FONT_AR_REG), fontsize=size, color=col)
            drawn += 1
        except Exception as exc:
            log.warning("فشل إدراج سطر عربي: %s", exc)
        baseline += leading
    return drawn


def _ar_font_size(en_size: float) -> float:
    return min(AR_FONT_MAX, max(AR_FONT_MIN, en_size * AR_FONT_SCALE))


# ==========================================================================
# أدوات الكتل
# ==========================================================================
def _block_text(block: dict) -> str:
    return "\n".join(
        "".join(s.get("text", "") for s in ln.get("spans", []))
        for ln in block.get("lines", [])
    ).strip()


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


def _block_segments(block: dict, size: float) -> list[dict]:
    rows = []
    for ln in block.get("lines", []):
        txt = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
        if txt:
            rows.append((ln["bbox"][1], ln["bbox"][3], txt))
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])
    gaps = [rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1)]
    median = sorted(gaps)[len(gaps) // 2] if gaps else size * 1.4
    threshold = max(median * 1.35, size * 1.15)

    segments, current = [], [rows[0]]
    for i in range(1, len(rows)):
        gap = rows[i][0] - rows[i - 1][0]
        if gap > threshold or _BULLET_RE.match(rows[i][2]):
            segments.append(current)
            current = [rows[i]]
        else:
            current.append(rows[i])
    segments.append(current)

    out = []
    for seg in segments:
        text = "\n".join(r[2] for r in seg)
        # نستخدم أسفل صندوق السطر كاملاً (بما فيه أحرف النزول) حتى لا يقطع
        # حدّ الشريط أي حرف عند استيراد المحتوى الأصلي.
        bottom = seg[-1][1] + 0.6
        out.append({"text": text, "bottom": bottom})
    return out


# ==========================================================================
# إعادة بناء الصفحة مع إدراج الترجمة أسفل كل كتلة
# ==========================================================================
def _append_below_page(out_doc, src_doc, pno: int, translator) -> dict:
    src_page = src_doc[pno]
    W, H = float(src_page.rect.width), float(src_page.rect.height)
    blocks = [b for b in src_page.get_text("dict")["blocks"]
              if b.get("type") == 0 and _block_text(b)]
    latin = [b for b in blocks if detect_lang(_block_text(b)) == "en"]

    if not latin:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "inserted": 0, "added_h": 0.0, "blocks": 0}

    latin.sort(key=lambda b: b["bbox"][1])
    col_x1 = W - AR_MARGIN
    text_w = max(40.0, col_x1 - AR_MARGIN - 6)

    items = []
    for b in latin:
        en_size, _color, _bold = _block_style(b)
        ar_size = _ar_font_size(en_size)
        for seg in _block_segments(b, en_size):
            translation = translator.translate(seg["text"])
            clear = 0.30 * en_size + 1.5
            ah = clear + _rtl_height(translation, ar_size, text_w)
            items.append({"tb": seg["bottom"], "ar_size": ar_size,
                          "ar": translation, "clear": clear, "ah": ah})
    items.sort(key=lambda it: it["tb"])

    bounds = sorted({0.0} | {it["tb"] for it in items} | {H})
    total_h = sum(it["ah"] for it in items)
    page = out_doc.new_page(width=W, height=H + total_h)

    for k in range(len(bounds) - 1):
        y0, y1 = bounds[k], bounds[k + 1]
        if y1 - y0 <= 0.01:
            continue
        shift = sum(it["ah"] for it in items if it["tb"] <= y0 + 0.01)
        try:
            page.show_pdf_page(fitz.Rect(0, y0 + shift, W, y1 + shift), src_doc, pno,
                               clip=fitz.Rect(0, y0, W, y1), keep_proportion=False)
        except Exception as exc:
            log.warning("فشل استيراد شريط الصفحة %d: %s", pno + 1, exc)

    inserted = 0
    for it in items:
        shift_before = sum(o["ah"] for o in items if o["tb"] < it["tb"] - 0.01)
        top = it["tb"] + shift_before + it["clear"]
        if _draw_rtl_lines(page, col_x1, top, it["ar"], it["ar_size"], AR_COLOR_HEX):
            inserted += 1

    return {"page": pno + 1, "inserted": inserted,
            "added_h": round(total_h, 1), "blocks": len(items)}


def _replace_mode_page(page, translator) -> int:
    blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
    jobs = []
    for b in blocks:
        text = _block_text(b)
        if not text:
            continue
        jobs.append((fitz.Rect(b["bbox"]), translator.translate(text), _block_style(b)))
    if not jobs:
        return 0
    for rect, _t, _s in jobs:
        page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    done = 0
    for rect, text, (size, _c, _b) in jobs:
        s = max(6.0, size * 0.92)
        if _draw_rtl_lines(page, rect.x1, rect.y0, text, s, "#000000"):
            done += 1
    return done


# ==========================================================================
# الواجهة العامة
# ==========================================================================
def translate_pdf(input_path, output_path, translator, progress_cb=None) -> dict:
    """
    يترجم ملف PDF ويحفظ الناتج. يعيد تقريراً (dict).
    `translator` كائن Translator (يوفّر .translate).
    """
    src_doc = fitz.open(str(input_path))
    stats = {
        "mode": TRANSLATE_MODE, "pages": src_doc.page_count,
        "blocks": 0, "translated_blocks": 0,
        "images_before": 0, "images_after": 0,
        "drawings_before": 0, "drawings_after": 0,
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
    log.info("تمت الترجمة: %s", {k: v for k, v in stats.items() if k != "pages_detail"})
    return stats
