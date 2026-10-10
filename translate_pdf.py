#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate_pdf.py  -  ترجمة PDF (إنجليزي -> عربي) مع الحفاظ على التصميم  (v9)
==========================================================================
النتيجة المطلوبة:
  * النص الإنجليزي الأصلي يبقى كما هو (لا يُحذف).
  * الترجمة العربية باللون الأحمر أسفل كل فقرة إنجليزية (وليس فوقها).
  * الصور والرسوم والأسهم والإطار الزخرفي تبقى كما هي، وترتيب الصفحات نفسه.

لماذا v9؟
  الطريقة السابقة (`insert_textbox` + arabic_reshaper/bidi) تُخرج النص المختلط
  (عربي + كلمات لاتينية مثل PPI و GERD و HCl) مقلوباً/غير متصل.
  الحل: `page.insert_htmlbox(..., dir="rtl")` الذي يطبّق خوارزمية Unicode Bidi
  القياسية فيخرج العربي متصلاً وبالاتجاه الصحيح، والكلمات اللاتينية في موضعها.
  ولحفظ المحتوى الأصلي نُعيد بناء الصفحة شرائحَ مصوّرة (get_pixmap + insert_image)
  بدل show_pdf_page الذي يُسقط طبقة النص فيظهر إطار بلا محتوى.

الواجهة العامة:
    translate_pdf(input_path, output_path, translator, progress_cb=None) -> dict
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
import unicodedata
from pathlib import Path

import pymupdf as fitz

from translator import has_arabic, arabic_ratio, polish_text

log = logging.getLogger("translate_pdf")


# ==========================================================================
# 1) الخط العربي
# ==========================================================================
def _font_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    dirs = []
    for var in ("AR_FONT_DIR", "FONT_DIR"):
        v = (os.getenv(var) or "").strip()
        if v:
            dirs.append(Path(v))
    p = (os.getenv("AR_FONT_PATH") or "").strip()
    if p:
        dirs.append(Path(p).parent if Path(p).is_file() or p.endswith(".ttf") else Path(p))
    dirs += [here / "assets" / "fonts", here / "assets", here,
             Path.cwd() / "assets" / "fonts", Path.cwd() / "fonts",
             Path("/app/assets/fonts"), Path("/app/fonts"), Path("/app")]
    return dirs


def _find_font(*names: str) -> Path | None:
    for d in _font_dirs():
        for n in names:
            f = d / n
            try:
                if f.is_file() and f.stat().st_size > 1000:
                    return f
            except Exception:
                continue
    return None


FONT_REG = _find_font("Amiri-Regular.ttf", "Amiri-Regular.otf",
                      "NotoNaskhArabic-Regular.ttf", "FreeSerif.ttf")
FONT_BOLD = _find_font("Amiri-Bold.ttf", "Amiri-Bold.otf",
                       "NotoNaskhArabic-Bold.ttf", "FreeSerifBold.ttf")

if FONT_REG is None:
    log.error("❌ لم يُعثر على خط عربي! ارفع assets/fonts/Amiri-Regular.ttf "
              "أو اضبط AR_FONT_DIR. بدون الخط ستظهر الترجمة كمربعات فارغة.")
else:
    log.info("خط عربي: %s", FONT_REG)

_FONT_NAME = "Amiri-Regular.ttf"
_ARCHIVE = fitz.Archive(str(FONT_REG.parent)) if FONT_REG else None


# ==========================================================================
# 2) إعدادات
# ==========================================================================
TRANSLATE_MODE = os.getenv("TRANSLATE_MODE", "below").strip().lower()
if TRANSLATE_MODE in ("bilingual", "keep_source"):
    TRANSLATE_MODE = "below"
if TRANSLATE_MODE not in ("below", "replace"):
    TRANSLATE_MODE = "below"

AR_COLOR = os.getenv("AR_COLOR", "#9E1B1B")          # أحمر
AR_FONT_SCALE = float(os.getenv("AR_FONT_SCALE", "0.92"))
AR_FONT_MIN = float(os.getenv("AR_FONT_MIN", "7.0"))
AR_FONT_MAX = float(os.getenv("AR_FONT_MAX", "26"))
AR_GAP = float(os.getenv("AR_GAP", "4"))            # مسافة بين النص الإنجليزي والترجمة
AR_MARGIN = float(os.getenv("AR_MARGIN", "20"))
LINE_FACTOR = float(os.getenv("AR_LINE_FACTOR", "1.55"))

INK_ASCENT = float(os.getenv("INK_ASCENT", "0.78"))
INK_DESCENT = float(os.getenv("INK_DESCENT", "0.30"))

LATIN_RE = re.compile(r"[A-Za-z]")
# نص حقيقي = كلمة إنجليزية من 3 حروف فأكثر (يستبعد رموز ترقيم Word المكسورة مثل e و ·e و 1e)
_REAL_TEXT = re.compile(r"[A-Za-z]{3,}")


def _has_real_text(t: str) -> bool:
    return bool(_REAL_TEXT.search(t or ""))


# خلية = اختصار طبي بحت (PPI, H2, GERD, NSAIDs...) ⇒ تُترك إنجليزية ولا يُرسم تحتها ترجمة
_CELL_ACRONYM = re.compile(r"^[A-Z][A-Z0-9]{0,5}s?$")
_BULLET_RE = re.compile(r"^\s*(?:[\u2022\u25aa\u25e6\u25cf\-\u2013*]|"
                        r"\(?[A-Za-z0-9]{1,3}[\)\].\-])\s")
PRESENTATION_RE = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")


def detect_lang(text: str) -> str:
    """'ar' إذا كان النص عربي أصلاً، وإلا 'en' (يحتاج ترجمة)."""
    if not text or not text.strip():
        return "en"
    words_ar = re.findall(r"[\u0600-\u06FF]{2,}", text)
    ratio = arabic_ratio(text)
    if has_arabic(text) and len(words_ar) >= 2 and ratio >= 0.45:
        return "ar"
    if ratio >= 0.6:
        return "ar"
    return "en"


# ==========================================================================
# 3) أدوات HTML للترجمة العربية (تضمن الرسم المتصل وRTL الصحيح)
# ==========================================================================
def _css(size: float, color: str) -> str:
    return ("@font-face{font-family:amiri;src:url(" + _FONT_NAME + ");}"
            "*{font-family:amiri;}"
            f"div{{font-size:{size}pt;line-height:{LINE_FACTOR};"
            f"color:{color};text-align:right;direction:rtl;}}")


def _to_html(text: str) -> str:
    esc = _html.escape(text).replace("\n", "<br>")
    return f'<div dir="rtl">{esc}</div>'


def _measure_height(text: str, size: float, width: float, color: str, cap: float = 4000) -> float:
    """يقيس الارتفاع الفعلي المطلوب لرسم الترجمة (باتساع width)."""
    if _ARCHIVE is None or not text.strip():
        return 0.0
    tmp = fitz.open()
    pg = tmp.new_page(width=max(40.0, width), height=cap)
    try:
        spare, scale = pg.insert_htmlbox(
            fitz.Rect(0, 0, width, cap), _to_html(text),
            css=_css(size, color), archive=_ARCHIVE)
        used = cap - spare
        if scale < 1:                       # لم يتّسع كاملاً رغم السقف
            used = cap
    except Exception as exc:                # pragma: no cover
        log.warning("قياس الارتفاع فشل: %s", exc)
        used = size * LINE_FACTOR * 2
    tmp.close()
    return max(used, size * LINE_FACTOR)


def _draw_ar(page, rect: fitz.Rect, text: str, size: float, color: str) -> bool:
    """يرسم الترجمة العربية داخل rect باستخدام محرك HTML (dir=rtl)."""
    if not text.strip():
        return False
    if _ARCHIVE is None:
        return False
    try:
        spare, scale = page.insert_htmlbox(rect, _to_html(text),
                                           css=_css(size, color), archive=_ARCHIVE)
        return scale > 0
    except Exception as exc:
        log.warning("insert_htmlbox فشل: %s", exc)
        return False


# ==========================================================================
# 4) أدوات كتل النص
# ==========================================================================
def _sp(ln: dict) -> dict:
    spans = ln.get("spans", [])
    return max(spans, key=lambda s: len(s.get("text", ""))) if spans else {}


def _line_text(ln: dict) -> str:
    return "".join(s.get("text", "") for s in ln.get("spans", [])).strip()


def _baseline(ln: dict) -> float:
    o = _sp(ln).get("origin")
    if o:
        return float(o[1])
    size = float(_sp(ln).get("size", 11))
    return float(ln.get("bbox", (0, 0, 0, 0))[3]) - 0.25 * size


def _size(ln: dict) -> float:
    return float(_sp(ln).get("size", 11) or 11)


_MIN_CELL_GAP = float(os.getenv("CELL_GAP_PT", "24")) if "os" in dir() else 24.0


def _split_row_cells(row: list) -> list:
    """تقسيم صف (سطر) إلى خلايا حسب الفراغ الأفقي الكبير في خلايا الجداول."""
    spans = []
    for ln in row:
        for s in ln.get("spans", []):
            txt = s.get("text", "")
            if txt.strip() and s.get("bbox"):
                spans.append((float(s["bbox"][0]), float(s["bbox"][2]), txt))
    spans.sort(key=lambda r: r[0])
    if len(spans) <= 1:
        return [row]
    groups, cur = [], [spans[0]]
    for sp in spans[1:]:
        prev_x1 = max(g[1] for g in cur)
        if sp[0] - prev_x1 > _MIN_CELL_GAP:
            groups.append(cur)
            cur = [sp]
        else:
            cur.append(sp)
    groups.append(cur)
    if len(groups) <= 1:
        return [row]
    cells = []
    for g in groups:
        cx0 = min(x[0] for x in g)
        cx1 = max(x[1] for x in g)
        ctext = " ".join(x[2].strip() for x in g).strip()
        if not ctext:
            continue
        cells.append({"x0": cx0, "x1": cx1, "text": ctext,
                      "baseline": _baseline(row[0]), "size": _size(row[0])})
    return cells


def _block_text(b: dict) -> str:
    return "\n".join(_line_text(l) for l in b.get("lines", []) if _line_text(l)).strip()


def _segments(block: dict) -> list[dict]:
    """تقسيم الكتلة إلى فقرات/نقاط حسب الفراغ البصري والمعلامات."""
    rows = []
    for ln in block.get("lines", []):
        t = _line_text(ln)
        if t:
            rows.append((_baseline(ln), _size(ln), t, ln.get("bbox"), ln))
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])

    gaps = []
    for i in range(len(rows) - 1):
        b0, s0, _t, _bb, _ln = rows[i]
        b1, s1, _t2, _bb2, _ln2 = rows[i + 1]
        gaps.append((b1 - INK_ASCENT * s1) - (b0 + INK_DESCENT * s0))
    med = sorted(gaps)[len(gaps) // 2] if gaps else 2.0
    thr = max(med * 1.9, 2.5)

    segs, cur = [], [rows[0]]
    for i in range(1, len(rows)):
        b0, s0, _t, _bb, _ln = rows[i - 1]
        b1, s1, t, _bb2, _ln2 = rows[i]
        vg = (b1 - INK_ASCENT * s1) - (b0 + INK_DESCENT * s0)
        if vg > thr or _BULLET_RE.match(t):
            segs.append(cur)
            cur = [rows[i]]
        else:
            cur.append(rows[i])
    segs.append(cur)

    out = []
    for seg in segs:
        text = "\n".join(r[2] for r in seg)
        if not _has_real_text(text):         # نتجاهل المقاطع بلا نص إنجليزي حقيقي
            continue
        # صفوف الجدول: جمّع الأسطر حسب خط الأساس ثم اقطع أفقيّاً عند الفراغ الكبير
        brow: list[list] = []
        for r in sorted(seg, key=lambda r: r[0]):
            if brow and abs(r[0] - brow[-1][-1][0]) < 0.5 * max(r[1], brow[-1][-1][1]):
                brow[-1].append(r)
            else:
                brow.append([r])
        row_cells: list[list] = []
        for rl in brow:
            cells, cur = [], [rl[0]]
            for r in rl[1:]:
                prev_x1 = max((q[3][2] for q in cur if q[3]), default=0.0)
                if r[3] and (r[3][0] - prev_x1) > _MIN_CELL_GAP:
                    cells.append(cur)
                    cur = [r]
                else:
                    cur.append(r)
            cells.append(cur)
            row_cells.append(cells)
        is_table = any(len(c) > 1 for c in row_cells)
        if is_table:
            for cells in row_cells:
                for c in cells:
                    ct = " ".join(q[2].strip() for q in c).strip()
                    if not _has_real_text(ct):
                        continue
                    if _CELL_ACRONYM.match(ct):     # خلية اختصار طبي (PPI) ⇒ تبقى إنجليزية
                        continue
                    fb = min(q[0] for q in c)
                    lb = max(q[0] for q in c)
                    fs = max(q[1] for q in c)
                    xs = [q[3][0] for q in c if q[3]]
                    xe = [q[3][2] for q in c if q[3]]
                    out.append({"text": ct,
                                "ink_top": fb - INK_ASCENT * fs,
                                "ink_bottom": lb + INK_DESCENT * fs,
                                "en_size": fs,
                                "x0": min(xs) if xs else block["bbox"][0],
                                "x1": max(xe) if xe else block["bbox"][2],
                                "cell": True})
            continue
        fb, fs, _t, _bb, _ln = min(seg, key=lambda r: r[0])
        lb, ls, _t2, bb2, _ln2 = max(seg, key=lambda r: r[0])
        xs = [r[3][0] for r in seg if r[3]]
        xe = [r[3][2] for r in seg if r[3]]
        out.append({"text": text,
                    "ink_top": fb - INK_ASCENT * fs,
                    "ink_bottom": lb + INK_DESCENT * ls,
                    "en_size": ls,
                    "x0": min(xs) if xs else AR_MARGIN,
                    "x1": max(xe) if xe else (block["bbox"][2])})
    return out


def _translate_all(translator, texts):
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
# 5) وضع "الترجمة أسفل النص" (below) - إعادة بناء الصفحة بحفظ كل المحتوى
# ==========================================================================
def _group_segments(segs: list[dict], W: float) -> list[dict]:
    """تجميع المقاطع في صفوف (يدعم عمودين/جداول): ما يتقاطع رأسياً = نفس الصف."""
    groups: list[dict] = []
    for s in sorted(segs, key=lambda x: (x["ink_top"], x["x0"])):
        row_mid = (groups[-1]["top"] + groups[-1]["bottom"]) / 2.0 if groups else None
        cx = (s["x0"] + s["x1"]) / 2.0
        cx_prev = None
        if groups and groups[-1]["items"]:
            p0 = groups[-1]["items"][0]
            cx_prev = (p0["x0"] + p0["x1"]) / 2.0
        v_overlap = (groups and s["ink_top"] < groups[-1]["bottom"] - 1.0)
        h_overlap = (row_mid is not None and abs(cx - row_mid) < 0.55 * W)
        if v_overlap and (h_overlap or (cx_prev is not None and abs(cx - cx_prev) < 0.33 * W)):
            g = groups[-1]
            g["items"].append(s)
            g["bottom"] = max(g["bottom"], s["ink_bottom"])
            g["top"] = min(g["top"], s["ink_top"])
        else:
            groups.append({"top": s["ink_top"], "bottom": s["ink_bottom"], "items": [s]})
    return groups


def _below_page(out_doc, src_doc, pno: int, translator) -> dict:
    src = src_doc[pno]
    W, H = float(src.rect.width), float(src.rect.height)
    blocks = [b for b in src.get_text("dict")["blocks"]
              if b.get("type") == 0 and _block_text(b)]
    to_tr = [b for b in blocks if detect_lang(_block_text(b)) != "ar"]

    if not to_tr:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "translated": 0, "added_h": 0.0, "skipped": "no-source"}

    segs: list[dict] = []
    for b in to_tr:
        segs.extend(_segments(b))
    if not segs:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "translated": 0, "added_h": 0.0, "skipped": "no-latin"}

    trs = _translate_all(translator, [s["text"] for s in segs])
    groups = _group_segments(segs, W)

    # اربط كل مقطع بترجمته واحسب ارتفاعه المطلوب
    tr_map = {id(s): t for s, t in zip(segs, trs)}
    for g in groups:
        heights = []
        for s in g["items"]:
            t = (tr_map.get(id(s)) or "").strip()
            s["tr"] = t
            if not t:
                s["need"] = 0.0
                continue
            x0 = max(AR_MARGIN, float(s["x0"]) - 2)
            x1 = min(W - AR_MARGIN, float(s["x1"]) + 2)
            if (x1 - x0 < 50) and not (s.get("cell") and (x1 - x0) >= 22):
                x0, x1 = AR_MARGIN, W - AR_MARGIN
            s["bx0"], s["bx1"] = x0, x1
            size = min(AR_FONT_MAX, max(AR_FONT_MIN, s["en_size"] * AR_FONT_SCALE))
            s["size"] = size
            used = _measure_height(t, size, x1 - x0, AR_COLOR)
            s["need"] = AR_GAP + used
            heights.append(s["need"])
        g["height"] = max(heights) if heights else 0.0
        g["shift_before"] = 0.0

    total_h = sum(g["height"] for g in groups)
    if total_h <= 0:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "translated": 0, "added_h": 0.0, "skipped": "empty-tr"}

    # إزاحات تراكمية
    acc = 0.0
    for g in groups:
        g["shift_before"] = acc
        acc += g["height"]

    # حدود الشرائح = منتصف الفراغ الأبيض بين الصفوف (لا تمرّ عبر أي حرف)
    bounds = [0.0]
    for i in range(len(groups) - 1):
        a, b = groups[i], groups[i + 1]
        if b["top"] > a["bottom"]:
            cut = (a["bottom"] + b["top"]) / 2.0
        else:
            cut = a["bottom"]
        bounds.append(min(max(cut, a["bottom"]), H))
    bounds.append(H)
    bounds = sorted(set(round(x, 3) for x in bounds))

    page = out_doc.new_page(width=W, height=H + total_h)

    # نرسم كل شريط من الصفحة الأصلية كصورة (get_pixmap) ثم نُلصقه في مكانه.
    # لماذا؟ لأن show_pdf_page لا ينسخ طبقة النص في هذا النوع من الملفات
    # (out-of-the-box فيظهر إطار فقط بلا محتوى). الرسم كصورة يحفظ النص والصور
    # والإطار الزخرفي والأسهم بنسبة 100% وبوضوح عالٍ.
    ov = 0.75
    dpi = int(os.getenv("STRIP_DPI", "150"))
    for k in range(len(bounds) - 1):
        y0, y1 = bounds[k], bounds[k + 1]
        if y1 - y0 <= 0.02:
            continue
        shift = sum(g["height"] for g in groups if g["bottom"] <= y0 + 0.01)
        cy0, cy1 = max(0.0, y0 - ov), min(H, y1 + ov)
        try:
            pix = src.get_pixmap(clip=fitz.Rect(0, cy0, W, cy1), dpi=dpi)
            page.insert_image(fitz.Rect(0, cy0 + shift, W, cy1 + shift), pixmap=pix)
        except Exception as exc:
            log.warning("فشل رسم شريط ص %d (%.0f-%.0f): %s", pno + 1, y0, y1, exc)

    drawn = 0
    for g in groups:
        for s in g["items"]:
            if not s.get("tr"):
                continue
            top = s["ink_bottom"] + g["shift_before"] + AR_GAP
            rect = fitz.Rect(s["bx0"], top, s["bx1"], top + max(s["need"], s["size"] * LINE_FACTOR))
            if _draw_ar(page, rect, s["tr"], s["size"], AR_COLOR):
                drawn += 1

    return {"page": pno + 1, "translated": drawn, "added_h": round(total_h, 1)}


# ==========================================================================
# 6) وضع "الاستبدال" (replace) - تغطية النص الأصلي بترجمة عربية
# ==========================================================================
def _replace_page(page, translator) -> int:
    blocks = [b for b in page.get_text("dict")["blocks"]
              if b.get("type") == 0 and _block_text(b)
              and detect_lang(_block_text(b)) != "ar"]
    if not blocks:
        return 0
    texts = [_block_text(b) for b in blocks]
    trs = _translate_all(translator, texts)
    styles = []
    for b in blocks:
        sizes = [float(s.get("size", 11)) for l in b.get("lines", [])
                 for s in l.get("spans", []) if s.get("text", "").strip()]
        styles.append(max(sizes) if sizes else 11.0)
    for b in blocks:
        page.add_redact_annot(fitz.Rect(b["bbox"]), fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    done = 0
    for b, t, sz in zip(blocks, trs, styles):
        if not t.strip():
            continue
        size = max(AR_FONT_MIN, min(AR_FONT_MAX, sz * AR_FONT_SCALE))
        need = _measure_height(t, size, fitz.Rect(b["bbox"]).width, "#000000")
        rect = fitz.Rect(b["bbox"])
        rect.y1 = rect.y0 + max(need, rect.height)
        if _draw_ar(page, rect, t, size, "#000000"):
            done += 1
    return done


# ==========================================================================
# 7) الواجهة العامة
# ==========================================================================
def translate_pdf(input_path, output_path, translator, progress_cb=None) -> dict:
    if FONT_REG is None:
        log.error("لا يوجد خط عربي صالح - الناتج قد يظهر بمربعات فارغة.")

    src_doc = fitz.open(str(input_path))
    stats = {
        "mode": TRANSLATE_MODE, "pages": src_doc.page_count,
        "translated_blocks": 0,
        "images_before": 0, "images_after": 0,
        "images_preserved": True,
        "source_pages": 0, "skipped_pages": 0,
        "arabic_font": str(FONT_REG) if FONT_REG else None,
        "pages_detail": [],
    }
    for pno in range(src_doc.page_count):
        stats["images_before"] += len(src_doc[pno].get_images(full=True))

    if TRANSLATE_MODE == "replace":
        out_doc = fitz.open(str(input_path))
        for pno in range(out_doc.page_count):
            stats["translated_blocks"] += _replace_page(out_doc[pno], translator)
            if progress_cb:
                progress_cb(pno + 1, out_doc.page_count)
    else:
        out_doc = fitz.open()
        for pno in range(src_doc.page_count):
            info = _below_page(out_doc, src_doc, pno, translator)
            stats["translated_blocks"] += info.get("translated", 0)
            if info.get("skipped"):
                stats["skipped_pages"] += 1
            else:
                stats["source_pages"] += 1
            stats["pages_detail"].append(info)
            if progress_cb:
                progress_cb(pno + 1, src_doc.page_count)

    for pno in range(out_doc.page_count):
        stats["images_after"] += len(out_doc[pno].get_images(full=True))

    out_doc.save(str(output_path), garbage=4, deflate=True)
    out_doc.close()
    src_doc.close()

    stats["images_preserved"] = stats["images_after"] >= stats["images_before"]
    stats["ok"] = True
    stats["input"] = str(input_path)
    stats["output"] = str(output_path)
    log.info("تمت الترجمة: mode=%s pages=%d translated=%d imgs %d->%d",
             TRANSLATE_MODE, stats["pages"], stats["translated_blocks"],
             stats["images_before"], stats["images_after"])
    return stats
