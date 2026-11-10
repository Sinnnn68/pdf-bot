#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doc_converter.py  -  دعم الصيغ المتعددة  (v9)
===========================================
يستقبل أي نوع ملف ويرجّع ناتجاً مترجماً بنفس النوع (قدر الإمكان):
  * PDF            -> PDF  (خط أنابيب translate_pdf الكامل)
  * DOCX           -> DOCX (كل فقرة يليها ترجمتها باللون الأحمر)
  * PPTX           -> PPTX (كل شريحة: ترجمة حمراء أسفل النص)
  * XLSX           -> XLSX (عمود عربي جديد بجانب كل عمود نصي)
  * TXT / CSV / MD -> TXT  (نص أصلي + ترجمة أسفله)
  * JPG/PNG/WEBP   -> PDF  (قراءة الصورة بـ Gemini ثم ترجمة النص)

الواجهة العامة:
    process_file(inp, out_dir, translator, progress_cb=None) -> dict
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("converter")

SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".csv", ".md",
             ".png", ".jpg", ".jpeg", ".webp", ".bmp"}

_AR_COLOR = (0x9E, 0x1B, 0x1B)          # أحمر للترجمة


def _is_arabic(text: str) -> bool:
    from translate_pdf import detect_lang
    return detect_lang(text) == "ar"


def _bilingual_pairs(texts, translator):
    """يترجم فقط المقاطع غير العربية، ويُبقي العربية كما هي."""
    todo = [t for t in texts if t.strip() and not _is_arabic(t)]
    out = translator.translate_all(todo) if todo else []
    it = iter(out)
    res = []
    for t in texts:
        if not t.strip() or _is_arabic(t):
            res.append(t)
        else:
            res.append(next(it, ""))
    return res


# ==========================================================================
# DOCX
# ==========================================================================
def _docx(inp: Path, out: Path, translator) -> dict:
    from docx import Document
    from docx.shared import RGBColor, Pt

    src = Document(str(inp))
    texts = [p.text for p in src.paragraphs]
    trs = _bilingual_pairs(texts, translator)

    doc = Document()
    n = 0
    for orig, tr in zip(texts, trs):
        p = doc.add_paragraph(orig)
        try:
            for r in p.runs:
                r.font.size = Pt(11)
        except Exception:
            pass
        if tr.strip() and tr != orig:
            q = doc.add_paragraph()
            run = q.add_run(tr)
            run.font.color.rgb = RGBColor(*_AR_COLOR)
            run.font.size = Pt(11)
            try:
                run.font.rtl = True
            except Exception:
                pass
            n += 1
    doc.save(str(out))
    return {"kind": "docx", "out": str(out), "translated": n, "pages": None,
            "images_before": 0, "images_after": 0, "images_preserved": True}


# ==========================================================================
# PPTX
# ==========================================================================
def _pptx(inp: Path, out: Path, translator) -> dict:
    from pptx import Presentation
    from pptx.util import Pt as PPt
    from pptx.dml.color import RGBColor
    from pptx.util import Emu

    prs = Presentation(str(inp))
    n = 0
    for slide in prs.slides:
        for shape in list(slide.shapes):
            if not shape.has_text_frame:
                continue
            txt = shape.text_frame.text
            if not txt.strip() or _is_arabic(txt):
                continue
            tr = translator.translate(txt)
            if not tr.strip():
                continue
            try:
                top = shape.top + shape.height
                box = slide.shapes.add_textbox(shape.left, top,
                                               shape.width, Emu(int(PPt(14).emu * 4)))
                tf = box.text_frame
                tf.word_wrap = True
                p = tf.paragraphs[0]
                r = p.add_run()
                r.text = tr
                r.font.size = PPt(12)
                r.font.color.rgb = RGBColor(*_AR_COLOR)
                n += 1
            except Exception as exc:
                log.warning("ppt: تعذّر إضافة الترجمة: %s", exc)
    prs.save(str(out))
    return {"kind": "pptx", "out": str(out), "translated": n, "pages": None,
            "images_before": 0, "images_after": 0, "images_preserved": True}


# ==========================================================================
# XLSX
# ==========================================================================
def _xlsx(inp: Path, out: Path, translator) -> dict:
    from openpyxl import load_workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    wb = load_workbook(str(inp))
    n = 0
    for ws in wb.worksheets:
        texts = []
        cells = []
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.strip() and not _is_arabic(c.value):
                    texts.append(c.value)
                    cells.append(c)
        if not texts:
            continue
        trs = translator.translate_all(texts)
        # نُنسخ الجدول جانبياً: كل خلية مصدر تُترجم إلى الخلية المقابلة لها
        # في كتلة جديدة على يمين الجدول (نفس الصف ونفس الترتيب) — حتى لا
        # تمسح ترجمات الصف الواحد بعضها.
        base = ws.max_column + 2
        ws.cell(row=1, column=base, value="الترجمة العربية").font = Font(bold=True, color="9E1B1B")
        for c, tr in zip(cells, trs):
            if tr and tr.strip():
                target_col = base + (c.column - 1)
                ws.cell(row=c.row, column=target_col, value=tr).font = Font(color="9E1B1B")
                n += 1
        for off in range(ws.max_column - base + 1):
            try:
                ws.column_dimensions[get_column_letter(base + off)].width = 45
            except Exception:
                pass
    wb.save(str(out))
    return {"kind": "xlsx", "out": str(out), "translated": n, "pages": None,
            "images_before": 0, "images_after": 0, "images_preserved": True}


# ==========================================================================
# TXT / CSV / MD
# ==========================================================================
def _txt(inp: Path, out: Path, translator) -> dict:
    raw = inp.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    n = 0
    buf = []
    for ln in lines:
        buf.append(ln)
        if ln.strip() and not _is_arabic(ln):
            tr = translator.translate(ln)
            if tr.strip() and tr != ln:
                buf.append(tr)
                n += 1
    out.write_text("\n".join(buf), encoding="utf-8")
    return {"kind": "txt", "out": str(out), "translated": n, "pages": None,
            "images_before": 0, "images_after": 0, "images_preserved": True}


# ==========================================================================
# صور (OCR + ترجمة بواسطة Gemini ثم PDF)
# ==========================================================================
def _image(inp: Path, out: Path, translator) -> dict:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    text = ""

    # 1) المسار المفضّل: Gemini (يقرأ الصورة ويفهمها)
    if key:
        try:
            text = _image_via_gemini(inp, translator, key)
        except Exception as exc:
            log.warning("Gemini لقراءة الصورة فشل: %s", exc)
            text = ""

    # 2) بديل بلا مفتاح: Tesseract OCR محلي
    if not text.strip():
        try:
            import ocr as _ocr
            if _ocr.available():
                text = _ocr.ocr_image_file(inp)
                if text.strip():
                    log.info("قُرئت الصورة بـ OCR المحلي (%d حرف)", len(text))
        except Exception as exc:
            log.warning("OCR المحلي للصورة فشل: %s", exc)

    if not text.strip():
        raise RuntimeError(
            "تعذّرت قراءة نص من الصورة. أضف GEMINI_API_KEY لقراءة الصور، "
            "أو تأكد أن الصورة تحتوي نصاً واضحاً (OCR محلي).")

    # 3) ترجمة النص المقروء
    tr = translator.translate(text) or text

    import pymupdf as fitz
    from translate_pdf import _draw_ar, _measure_height

    # نُبقي الصورة الأصلية في أعلى الصفحة، والترجمة العربية تحتها مباشرة
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(inp) as _im:
            iw, ih = _im.size
    except Exception:
        iw, ih = 1000, 1400

    PAGE_W = 595.0
    MARGIN = 30.0
    img_w = PAGE_W - 2 * MARGIN
    img_h = img_w * (ih / iw) if iw else img_w
    # لا نجعل الصورة أطول من صفحة A4 كاملة
    max_img_h = 842.0 - 2 * MARGIN
    if img_h > max_img_h:
        scale = max_img_h / img_h
        img_h *= scale
        img_w *= scale

    ar_size = 12.0
    ar_h = _measure_height(tr, ar_size, PAGE_W - 2 * MARGIN, "#000000", cap=6000)
    page_h = MARGIN + img_h + 18 + ar_h + MARGIN
    page_h = max(page_h, 300.0)

    doc = fitz.open()
    pg = doc.new_page(width=PAGE_W, height=page_h)
    img_rect = fitz.Rect(MARGIN, MARGIN, MARGIN + img_w, MARGIN + img_h)
    # نُدرج الصورة كـ JPEG مضغوط حتى لا يتضخّم حجم الـ PDF
    try:
        import io
        from PIL import Image as _PILImage2
        with _PILImage2.open(inp) as _im2:
            _rgb = _im2.convert("RGB")
            _buf = io.BytesIO()
            _rgb.save(_buf, format="JPEG", quality=88, optimize=True)
        pg.insert_image(img_rect, stream=_buf.getvalue())
    except Exception:
        pg.insert_image(img_rect, filename=str(inp))
    ar_rect = fitz.Rect(MARGIN, MARGIN + img_h + 18,
                        PAGE_W - MARGIN, page_h - MARGIN)
    _draw_ar(pg, ar_rect, tr, ar_size, "#000000")
    doc.save(str(out))
    return {"kind": "image->pdf", "out": str(out), "translated": 1, "pages": 1,
            "images_before": 1, "images_after": 1, "images_preserved": True}


def _image_via_gemini(inp: Path, translator, key: str) -> str:
    """يقرأ نص الصورة عبر Gemini (يحتاج GEMINI_API_KEY)."""
    import base64
    import requests

    b64 = base64.b64encode(inp.read_bytes()).decode()
    ext = inp.suffix.lstrip(".").lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp", "bmp": "image/bmp"}.get(ext, "image/png")
    model = os.getenv("GEMINI_MODEL", "").strip()
    if not model:
        model = (translator.list_gemini_models() or [None])[0]
    if not model:
        raise RuntimeError("تعذّر تحديد موديل Gemini لقراءة الصورة.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = ("Read all the text in this image and return it exactly as plain text, "
              "preserving line breaks. Do not translate and do not add commentary.")
    body = {"contents": [{"parts": [{"text": prompt},
                                    {"inline_data": {"mime_type": mime, "data": b64}}]}],
            "generationConfig": {"temperature": 0.0}}
    r = requests.post(url, params={"key": key}, json=body, timeout=90)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


# ==========================================================================
# الواجهة العامة
# ==========================================================================
def process_file(inp, out_dir, translator, progress_cb=None) -> dict:
    inp = Path(inp)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = inp.suffix.lower()
    stem = inp.stem

    if ext == ".pdf":
        from translate_pdf import translate_pdf
        out = out_dir / f"translated_{stem}.pdf"
        stats = translate_pdf(str(inp), str(out), translator, progress_cb)
        stats["kind"] = "pdf"
        stats["out"] = str(out)
        return stats

    if ext == ".docx":
        return _docx(inp, out_dir / f"translated_{stem}.docx", translator)
    if ext == ".pptx":
        return _pptx(inp, out_dir / f"translated_{stem}.pptx", translator)
    if ext == ".xlsx":
        return _xlsx(inp, out_dir / f"translated_{stem}.xlsx", translator)
    if ext in (".txt", ".csv", ".md"):
        return _txt(inp, out_dir / f"translated_{stem}{ext}", translator)
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return _image(inp, out_dir / f"translated_{stem}.pdf", translator)

    raise ValueError(f"صيغة غير مدعومة: {ext}")
