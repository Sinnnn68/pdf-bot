#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ocr.py  -  قراءة النص من الصفحات الممسوحة والصور (بدون أي مفتاح API)
=====================================================================
المشكلة التي يحلّها:
  ملفات المحاضرات (الملازم) غالباً صور ممسوحة (scanned) بلا طبقة نص،
  فكان البوت يعدّها "لا شيء للترجمة" ويرجّع الملف كما هو مع رسالة نجاح.

الحل: Tesseract OCR محلي (مجاني، بلا مفتاح).
  * ocr_pdf_page(page)  -> (text, lines) بإحداثيات PDF points
  * ocr_image_file(path) -> text
  * available()          -> هل Tesseract مثبّت ويعمل؟

متغيّرات البيئة:
  OCR_LANG   لغة Tesseract (افتراضي eng). مثال: "eng+ara"
  OCR_DPI    دقة تحويل الصفحة قبل القراءة (افتراضي 200)
  OCR_MIN_CONF  أقل ثقة مقبولة للكلمة (افتراضي 30)
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path

log = logging.getLogger("ocr")

_AVAILABLE: bool | None = None


def _deps():
    """يرجّع (pytesseract, PIL.Image) أو (None, None)."""
    try:
        import pytesseract
        from PIL import Image
        return pytesseract, Image
    except Exception as exc:                       # pragma: no cover
        log.warning("pytesseract/Pillow غير متاح: %s", exc)
        return None, None


def available() -> bool:
    """هل Tesseract مثبّت ويعمل فعلاً؟ (يُخزَّن بعد أول فحص)"""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    pt, _ = _deps()
    if pt is None:
        _AVAILABLE = False
        return False
    try:
        pt.get_tesseract_version()
        _AVAILABLE = True
    except Exception as exc:
        log.warning("Tesseract غير مثبّت على النظام: %s", exc)
        _AVAILABLE = False
    return _AVAILABLE


def _lang() -> str:
    return (os.getenv("OCR_LANG", "eng").strip() or "eng")


def _dpi() -> int:
    try:
        return max(100, min(400, int(os.getenv("OCR_DPI", "200"))))
    except Exception:
        return 200


def _min_conf() -> float:
    try:
        return float(os.getenv("OCR_MIN_CONF", "30"))
    except Exception:
        return 30.0


def _to_image(data: bytes):
    _, Image = _deps()
    if Image is None:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        return img
    except Exception as exc:
        log.warning("تعذّر فتح الصورة: %s", exc)
        return None


def ocr_image_bytes(data: bytes) -> str:
    """يقرأ النص من بايتات صورة."""
    pt, _ = _deps()
    if pt is None or not available():
        return ""
    img = _to_image(data)
    if img is None:
        return ""
    try:
        return (pt.image_to_string(img, lang=_lang()) or "").strip()
    except Exception as exc:
        log.warning("OCR صورة فشل: %s", exc)
        return ""


def ocr_image_file(path) -> str:
    """يقرأ النص من ملف صورة."""
    try:
        return ocr_image_bytes(Path(path).read_bytes())
    except Exception as exc:
        log.warning("قراءة ملف الصورة فشلت: %s", exc)
        return ""


def ocr_pdf_page(page, dpi: int | None = None):
    """يقرأ صفحة PDF ممسوحة.

    يرجّع (text, lines) حيث lines = [(x0, y0, x1, y1, text)]
    بإحداثيات PDF points (لا بكسل الصورة) حتى تُرسم الترجمة بمكانها الصحيح.
    """
    pt, _ = _deps()
    if pt is None or not available():
        return "", []
    dpi = dpi or _dpi()
    try:
        pix = page.get_pixmap(dpi=dpi)
        img = _to_image(pix.tobytes("png"))
        if img is None:
            return "", []
        data = pt.image_to_data(img, lang=_lang(), output_type=pt.Output.DICT)
    except Exception as exc:
        log.warning("OCR صفحة PDF فشل: %s", exc)
        return "", []

    W = float(page.rect.width) or 1.0
    H = float(page.rect.height) or 1.0
    sx = W / float(pix.width or 1)
    sy = H / float(pix.height or 1)

    # تجميع الكلمات في أسطر حسب (block, par, line)
    buckets: dict[tuple, list] = {}
    texts = data.get("text", []) or []
    for i in range(len(texts)):
        word = (texts[i] or "").strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = 0.0
        if conf < _min_conf():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        buckets.setdefault(key, []).append(
            (data["left"][i], data["top"][i], data["width"][i], data["height"][i], word))

    lines = []
    for key in sorted(buckets):
        words = sorted(buckets[key], key=lambda r: r[0])
        text = " ".join(w[4] for w in words).strip()
        if not text:
            continue
        x0 = min(w[0] for w in words)
        y0 = min(w[1] for w in words)
        x1 = max(w[0] + w[2] for w in words)
        y1 = max(w[1] + w[3] for w in words)
        lines.append((x0 * sx, y0 * sy, x1 * sx, y1 * sy, text))

    lines.sort(key=lambda r: (round(r[1], 1), r[0]))
    return "\n".join(r[4] for r in lines), lines


def page_needs_ocr(page, min_chars: int = 20) -> bool:
    """هل الصفحة بلا طبقة نص كافية (أي ممسوحة)؟"""
    try:
        return len((page.get_text() or "").strip()) < min_chars
    except Exception:
        return False
