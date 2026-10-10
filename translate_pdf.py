#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translate_pdf.py  (النسخة المصحّحة v3)
=====================================
يترجم ملف PDF (إنجليزي -> عربي) بوضع الترجمة العربية **أسفل كل فقرة مباشرة**
داخل نفس الصفحة، مع الحفاظ الكامل على النص الأصلي والصور والرسوم.
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
                urllib.request.urlretrieve(url, str(dest))
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

if FONT_AR_REG is None:
    log.warning("لم يُعثر على خط عربي - محاولة تحميله تلقائياً...")
    _download_fonts()
    FONT_AR_REG = _find_font("Amiri-Regular.ttf", "Amiri-Regular.otf",
                             "NotoNaskhArabic-Regular.ttf", "FreeSerif.ttf")
    FONT_AR_BOLD = _find_font("Amiri-Bold.ttf", "Amiri-Bold.otf",
                              "NotoNaskhArabic-Bold.ttf", "FreeSerifBold.ttf")

if FONT_AR_REG is None:
    log.error("❌ لم يُعثر على خط عربي ولم ينجح تحميله تلقائياً.")
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
        return True
    except Exception as exc:                          # pragma: no cover
        log.error("تعذّر تحميل قياسات الخط (%s): %s", FONT_AR_REG, exc)
        return False


TRANSLATE_MODE = os.getenv("TRANSLATE_MODE", "below").strip().lower()
if TRANSLATE_MODE in ("keep_source",):
    TRANSLATE_MODE = "below"
if TRANSLATE_MODE not in ("below", "bilingual", "replace"):
    TRANSLATE_MODE = "below"

AR_FONT_SCALE = float(os.getenv("AR_FONT_SCALE", "0.85"))
AR_FONT_MIN = float(os.getenv("AR_FONT_MIN", "7.5"))
AR_FONT_MAX = float(os.getenv("AR_FONT_MAX", "40"))
AR_LEADING = float(os.getenv("AR_LEADING", "1.30"))
AR_GAP = float(os.getenv("AR_GAP", "4"))
AR_MARGIN = float(os.getenv("AR_MARGIN", "22"))
AR_COLOR_HEX = os.getenv("AR_COLOR", "#9E1B1B")

INK_DESCENT = float(os.getenv("INK_DESCENT", "0.30"))
INK_ASCENT = float(os.getenv("INK_ASCENT", "0.78"))

ARABIC_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
PRESENTATION = re.compile(r"[\uFB50-\uFDFF\uFE70-\uFEFF]")
LATIN_RANGE = re.compile(r"[A-Za-z]")


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


_SYMBOL_FALLBACK = {
    "\u03b2": "B", "\u03b1": "a", "\u03b3": "g", "\u03b4": "d",
    "\u2022": "-", "\u25cf": "-", "\u25aa": "-", "\u25e6": "-",
    "\u2192": "<-", "\u2190": "<-", "\u2194": "<->",
    "\u2265": ">=", "\u2264": "<=", "\u2260": "!=",
    "\u00b5": "u", "\u00b0": "deg", "\u00d7": "x", "\u2044": "/",
}


def _fix_unsupported(line: str) -> str:
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
            out.append(ch)
    return "".join(out)


def shape_line(line: str) -> str:
    if not line:
        return line
    if PRESENTATION.search(line):
        line = unicodedata.normalize("NFKC", line)
    line = _fix_unsupported(line)
    if not has_arabic(line):
        return line
    if arabic_reshaper is None or get_display is None:
        return line
    try:
        return get_display(arabic_reshaper.reshape(line), base_dir="R")
    except Exception:
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
                    size: float, color: str) -> int:
    lines, used = _layout(text, size, x_right - AR_MARGIN)
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
        x = max(AR_MARGIN, x_right - w)
        try:
            page.insert_text(fitz.Point(x, baseline), shaped,
                             fontname=_AR_FONT_NAME, fontfile=str(FONT_AR_REG),
                             fontsize=used, color=col)
            drawn += 1
        except Exception:
            pass
        baseline += leading
    return drawn


def _ar_font_size(en_size: float) -> float:
    return min(AR_FONT_MAX, max(AR_FONT_MIN, en_size * AR_FONT_SCALE))


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
        last_b, last_s, _ = max(seg, key=lambda r: r[0])
        out.append({"text": text, "ink_bottom": last_b + INK_DESCENT * last_s,
                    "en_size": last_s})
    return out


def _translate_all(translator, texts: list[str]) -> list[str]:
    if hasattr(translator, "translate_all"):
        try:
            out = translator.translate_all(texts, None)
            if isinstance(out, list) and len(out) == len(texts):
                return out
        except Exception:
            pass
    return [translator.translate(t) for t in texts]


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

    x_right = W - AR_MARGIN
    text_w = max(60.0, x_right - AR_MARGIN)

    segs = []
    for b in latin:
        for seg in _block_segments(b):
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
        ar_size = _ar_font_size(seg.get("en_size", 11.0))
        clear = AR_GAP
        ah = clear + _rtl_height(tr, ar_size, text_w)
        items.append({"anchor": seg["ink_bottom"], "ar_size": ar_size,
                      "ar": tr, "clear": clear, "ah": ah})
    items.sort(key=lambda it: (it["anchor"], -it["ah"]))
    if not items:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "inserted": 0, "added_h": 0.0, "blocks": 0}

    total_h = sum(it["ah"] for it in items)
    page = out_doc.new_page(width=W, height=H + total_h)

    bounds = sorted({0.0} | {min(max(it["anchor"], 0.0), H) for it in items} | {H})

    for k in range(len(bounds) - 1):
        y0, y1 = bounds[k], bounds[k + 1]
        if y1 - y0 <= 0.01:
            continue
        shift = sum(it["ah"] for it in items if it["anchor"] <= y0 + 0.01)
        try:
            page.show_pdf_page(
                fitz.Rect(0, y0 + shift, W, y1 + shift), src_doc, pno,
                clip=fitz.Rect(0, y0, W, y1), keep_proportion=False)
        except Exception:
            pass

    inserted = 0
    used_anchor, stack = None, 0.0
    for it in items:
        if used_anchor is None or abs(it["anchor"] - used_anchor) > 0.01:
            used_anchor, stack = it["anchor"], 0.0
        shift_before = sum(o["ah"] for o in items if o["anchor"] < it["anchor"] - 0.01)
        top = it["anchor"] + shift_before + stack + it["clear"]
        if _draw_rtl_lines(page, x_right, top, it["ar"], it["ar_size"], AR_COLOR_HEX):
            inserted += 1
        stack += it["ah"]

    return {"page": pno + 1, "inserted": inserted,
            "added_h": round(total_h, 1), "blocks": len(items)}


def translate_pdf(input_path, output_path, translator, progress_cb=None) -> dict:
    if not _init_arabic_font():
        log.error("لا يوجد خط عربي صالح.")

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
    return stats
