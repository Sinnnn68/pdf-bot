#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 IN-PLACE (LAYOUT-PRESERVING) PDF TRANSLATION  -  v4
 ترجمة PDF مع الحفاظ الكامل على التصميم + الترجمة العربية أسفل كل فقرة مباشرة
================================================================================
What v4 does differently
------------------------
v3 either replaced the source text or dumped the translation into a bottom band /
a companion page. v4 does what the reference layout shows:

    English paragraph (unchanged)
    الترجمة العربية مباشرةً أسفله           <-- right aligned, right below

For every text block the engine

    1. reads the block + its bbox, font size, colour and direction;
    2. translates it (real REST API when keys are set, else the offline MOCK);
    3. measures the Arabic translation with the *real* glyph metrics of the
       embedded Amiri font (logical characters, not presentation forms);
    4. opens a vertical GAP of exactly that height immediately below the block,
       shifting everything that follows it downwards;
    5. writes the Arabic translation into that gap, right-aligned (RTL) and in a
       distinct colour, just below the original English text.

Nothing is removed. Every page is rebuilt at its original width and enlarged on
the Y axis to hold the added translations; the whole original page -- text,
raster images AND vector drawings -- is imported verbatim with
``Page.show_pdf_page`` (which performs a Form-XObject import, so the graphics are
copied 1:1 and the text stays vector / searchable). The original English text is
never covered, never overwritten and never altered.

TRANSLATE_MODE (environment variable):
  * below        (DEFAULT) English stays exactly where it is, the Arabic
                 translation is inserted directly BELOW each block.
  * keep_source  Alias of `below` (both preserve the source text).
  * bilingual    Same as `below`, but every page also gets a small header line
                 naming the language pair.
  * replace      Legacy monolingual overlay: cover each block and write the
                 translation into its own bbox (images/drawings still kept).

Interfaces
----------
  GET  /            : upload form
  POST /translate   : upload a PDF -> result PDF (?format=json for a report)
  GET  /health      : JSON status
  GET  /download/<name>
  CLI : python main.py -i in.pdf -o out.pdf --json
================================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# PyMuPDF - the engine that does the layout-preserving editing
# --------------------------------------------------------------------------
try:
    import pymupdf as fitz                       # modern import name
except Exception:                                # pragma: no cover
    import fitz                                  # legacy fallback

try:
    import requests
except Exception:                                # pragma: no cover
    requests = None

import arabic_reshaper
from bidi.algorithm import get_display

from flask import Flask, jsonify, render_template_string, request, send_file


# ==========================================================================
# 1. CONFIGURATION
# ==========================================================================
BASE_DIR = Path(__file__).resolve().parent
FONT_DIR = BASE_DIR / "assets" / "fonts"
FONT_AR_REG = FONT_DIR / "Amiri-Regular.ttf"
FONT_AR_BOLD = FONT_DIR / "Amiri-Bold.ttf"
OUT_DIR = BASE_DIR / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)

APP_NAME = "In-Place PDF Translator (translation below each block)"

# ---- behaviour -----------------------------------------------------------
TRANSLATE_MODE = os.getenv("TRANSLATE_MODE", "below").strip().lower()
if TRANSLATE_MODE in ("keep_source",):
    TRANSLATE_MODE = "below"
if TRANSLATE_MODE not in ("below", "bilingual", "replace"):
    TRANSLATE_MODE = "below"

# ---- Arabic block geometry (below / bilingual) ---------------------------
AR_FONT_SCALE = float(os.getenv("AR_FONT_SCALE", "0.72"))
AR_FONT_MIN = float(os.getenv("AR_FONT_MIN", "8.5"))
AR_FONT_MAX = float(os.getenv("AR_FONT_MAX", "26"))
AR_LEADING = float(os.getenv("AR_LEADING", "1.35"))
AR_GAP = float(os.getenv("AR_GAP", "6"))          # pt between EN block and AR text
AR_MARGIN = float(os.getenv("AR_MARGIN", "20"))
AR_COLOR_HEX = os.getenv("AR_COLOR", "#9E1B1B")   # maroon, as in the reference

# ---- bilingual page header ------------------------------------------------
BILINGUAL_HEADER = os.getenv("BILINGUAL_HEADER", "1") not in ("0", "false", "False")

# ---- legacy `replace` mode ------------------------------------------------
COVER_COLOR = os.getenv("COVER_COLOR", "white").strip().lower()
FONT_SHRINK = float(os.getenv("FONT_SHRINK", "0.92"))
MIN_FONT = float(os.getenv("MIN_FONT", "5.5"))

# ---- translation direction / engine ---------------------------------------
TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL", "").strip()
TRANSLATE_API_KEY = os.getenv("TRANSLATE_API_KEY", "").strip()
TRANSLATE_PROVIDER = os.getenv("TRANSLATE_PROVIDER", "libretranslate").strip().lower()
SOURCE_LANG = os.getenv("SOURCE_LANG", "auto").strip()
TARGET_LANG = os.getenv("TARGET_LANG", "auto").strip()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("inplace-translator")


# ==========================================================================
# 2. LANGUAGE / TEXT UTILITIES
# ==========================================================================
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
    """Return 'ar' or 'en' for a chunk of text."""
    return "ar" if arabic_ratio(text) >= 0.5 else "en"


def shape_arabic(text: str) -> str:
    """Logical Arabic -> visual glyphs (contextual joining + bidi)."""
    if not has_arabic(text):
        return text
    return get_display(arabic_reshaper.reshape(text), base_dir="R")


def restore_reading_order(text: str) -> str:
    """Inverse of shape_arabic for text extracted from a shaped PDF."""
    if not text:
        return text
    if not PRESENTATION.search(text):
        return unicodedata.normalize("NFKC", text)
    return "\n".join(unicodedata.normalize("NFKC", ln[::-1]) for ln in text.split("\n"))


def rgb_to_hex(color) -> str:
    try:
        if isinstance(color, (int, float)):
            return f"#{int(color):06x}"
        r, g, b = (color + (0, 0, 0))[:3]
        return "#{:02x}{:02x}{:02x}".format(
            max(0, min(255, int(round(r * 255)))),
            max(0, min(255, int(round(g * 255)))),
            max(0, min(255, int(round(b * 255)))),
        )
    except Exception:
        return "#000000"


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# ==========================================================================
# 3. TRANSLATION LAYER (real API + automatic offline MOCK, bidirectional)
# ==========================================================================
EN_AR: dict[str, str] = {
    "angina pectoris": "الذبحة الصدرية", "organic nitrates": "النترات العضوية",
    "calcium channel blocking agents": "حاصرات قنوات الكالسيوم",
    "calcium channel blockers": "حاصرات قنوات الكالسيوم",
    "adrenergic blocking agents": "العوامل الحاصرة الأدرينية",
    "beta adrenergic blocking agents": "العوامل الحاصرة الأدرينية بيتا",
    "myocardial contractility": "الانقباضية العضلية القلبية",
    "blood flow": "تدفق الدم", "blood pressure": "ضغط الدم",
    "heart rate": "معدل ضربات القلب", "coronary arteries": "الشرايين التاجية",
    "smooth muscle": "العضلة الملساء", "blood vessel walls": "جدران الأوعية الدموية",
    "blood vessels": "الأوعية الدموية", "systemic circulation": "الدورة الدموية الجهازية",
    "first-pass metabolism": "الأيض الأولي", "adverse effects": "الآثار الجانبية",
    "half-lives": "أنصاف الأعمار", "once daily": "مرة واحدة يومياً",
    "isosorbide dinitrate": "إيزوسوربيد ثنائي النترات",
    "isosorbide mononitrate": "إيزوسوربيد أحادي النترات",
    "applied therapeutics lab": "مختبر العلاجيات التطبيقي",
    "northern technical university": "الجامعة التقنية الشمالية",
    "al-dour college of polytechnique": "كلية الدَّور التقنية",
    "department of pharmacy techniques": "قسم تقنيات الصيدلة",
    "routes of drug administration": "طرق إعطاء الدواء",
    "drug administration": "إعطاء الدواء", "sublingual area": "المنطقة تحت اللسان",
    "buccal cavity": "تجويف الفم", "chest pain": "ألم الصدر",
    "clinical syndrome": "متلازمة سريرية",
    "atherosclerotic plaque": "اللويحة التصلبية العصيدية",
    "oxygen demand": "الطلب على الأكسجين", "cardiac workload": "العبء القلبي",
    "venous pressure": "الضغط الوريدي",
    "peripheral vascular supply": "الإمداد الوعائي المحيطي",
    "systolic blood pressure": "ضغط الدم الانقباضي", "acute angina": "الذبحة الحادة",
    "recurrent angina": "الذبحة المتكررة",
    "exercise-induced angina": "الذبحة المُحدثة بالتمرين",
    "exercise-induced tachycardia": "تسرع القلب المُحدث بالتمرين",
    "atrioventricular heart block": "الإحصار الأذيني البطيني",
    "chewable tablets": "الأقراص القابلة للمضغ", "long-acting": "طويل المفعول",
    "fast acting": "سريع المفعول", "onset of action": "بداية المفعول",
    "peak effects": "ذروة التأثير",
    "antianginal drug regimen": "النظام الدوائي المضاد للذبحة",
    "beta blocker": "حاصر بيتا", "beta blockers": "حاصرات بيتا",
    "angina": "الذبحة الصدرية", "pectoris": "الصدرية", "chest": "الصدر",
    "pain": "ألم", "blood": "الدم", "flow": "التدفق", "heart": "القلب",
    "drug": "دواء", "drugs": "أدوية", "nitrates": "النترات", "nitrate": "نترات",
    "organic": "عضوي", "adrenergic": "أدريني", "blocking": "حاصر",
    "blocker": "حاصر", "blockers": "حاصرات", "agents": "عوامل", "agent": "عامل",
    "calcium": "الكالسيوم", "channel": "قناة", "relieve": "يخفف", "relieves": "يخفف",
    "increase": "يزيد", "increases": "يزيد", "supply": "الإمداد",
    "myocardium": "عضلة القلب", "smooth": "الأملس", "muscle": "العضلة",
    "muscles": "العضلات", "vessel": "الوعاء", "vessels": "الأوعية",
    "walls": "الجدران", "wall": "الجدار", "vasodilatation": "توسع الأوعية",
    "vasodilation": "توسع الأوعية", "veins": "الأوردة", "vein": "الوريد",
    "venous": "وريدي", "pressure": "الضغط", "cardiac": "القلبي",
    "workload": "العبء", "oxygen": "الأكسجين", "demand": "الطلب",
    "coronary": "التاجي", "arteries": "الشرايين", "artery": "الشريان",
    "ischemic": "الإقفاري", "arterioles": "الشرينات", "peripheral": "المحيطي",
    "vascular": "الوعائي", "systolic": "الانقباضي", "dilation": "توسيع",
    "nitroglycerin": "النيتروجليسرين", "sublingually": "تحت اللسان",
    "sublingual": "تحت اللسان", "systemic": "الجهازي",
    "circulation": "الدورة الدموية", "minute": "دقيقة", "minutes": "دقائق",
    "hour": "ساعة", "hours": "ساعات", "isosorbide": "إيزوسوربيد",
    "dinitrate": "ثنائي النترات", "mononitrate": "أحادي النترات",
    "metabolite": "مستقلب", "active": "فعال", "onset": "بداية", "action": "المفعول",
    "prophylaxis": "الوقاية", "acute": "حاد", "attack": "نوبة", "attacks": "نوبات",
    "propranolol": "البروبرانولول", "atenolol": "الأتينولول",
    "metoprolol": "الميتوبرولول", "nadolol": "النادولول", "beta": "بيتا",
    "rate": "معدل", "contractility": "الانقباضية", "discontinued": "إيقافه",
    "prolonged": "المطوّل", "tapered": "تخفيضاً تدريجياً", "dosage": "الجرعة",
    "rebound": "ارتدادي", "tachycardia": "تسرع القلب",
    "atrioventricular": "الأذيني البطيني", "block": "إحصار", "half": "نصف",
    "lives": "العمر", "daily": "يومياً", "adverse": "جانبية", "effects": "الآثار",
    "effect": "الأثر", "collapse": "انهيار", "headache": "صداع", "fall": "انخفاض",
    "used": "يُستخدم", "use": "استخدام", "given": "يُعطى", "give": "يعطي",
    "reduce": "يقلل", "reduces": "يقلل", "reduced": "منخفض", "decrease": "يقلل",
    "decreases": "يقلل", "decreased": "منخفض", "lower": "أخفض", "lowers": "يخفض",
    "prevent": "يمنع", "preventing": "منع", "prevention": "الوقاية",
    "management": "إدارة", "recurrent": "المتكرر", "severity": "شدة",
    "frequency": "تكرار", "exercise": "التمرين", "induced": "المُحدث",
    "chewable": "قابلة للمضغ", "tablets": "الأقراص", "tablet": "قرص",
    "fast": "سريع", "acting": "المفعول", "long": "طويل", "short": "قصير",
    "clinical": "سريري", "syndrome": "متلازمة", "characterized": "يتميز",
    "caused": "ناتج", "atherosclerotic": "تصلبي عصيدي", "plaque": "لويحة",
    "antianginal": "مضاد للذبحة", "mechanisms": "آليات", "mechanism": "آلية",
    "several": "عدة", "volume": "الحجم", "myocardial": "عضلي قلبي",
    "areas": "مناطق", "results": "يؤدي", "result": "النتيجة", "levels": "مستويات",
    "level": "مستوى", "cells": "الخلايا", "cell": "الخلية", "produce": "ينتج",
    "produces": "ينتج", "relax": "يرخي", "relaxes": "يرخي", "dilate": "يوسع",
    "dilates": "يوسع", "directly": "مباشرة", "approximately": "تقريباً",
    "lasts": "يستمر", "last": "يستمر", "acts": "يعمل", "act": "يعمل",
    "within": "خلال", "occurs": "يحدث", "peak": "الذروة", "only": "فقط",
    "rapidly": "بسرعة", "enough": "بما يكفي", "added": "يُضاف", "regimen": "النظام",
    "especially": "خاصة", "useful": "مفيد", "usually": "عادة", "same": "نفس",
    "uses": "الاستخدامات", "longer": "أطول", "once": "مرة", "twice": "مرتين",
    "day": "يوم", "week": "أسبوع", "month": "شهر", "year": "سنة",
    "the": "الـ", "a": "", "an": "", "of": "من", "to": "إلى", "in": "في",
    "on": "على", "by": "بواسطة", "with": "مع", "and": "و", "or": "أو",
    "for": "لـ", "from": "من", "that": "التي", "this": "هذا", "these": "هذه",
    "those": "تلك", "which": "التي", "when": "عندما", "where": "حيث",
    "is": "هو", "are": "هي", "was": "كان", "were": "كانت", "be": "يكون",
    "been": "كان", "being": "كون", "has": "لديه", "have": "لديها", "had": "كان",
    "it": "هو", "its": "الخاص به", "they": "هم", "their": "الخاص بهم",
    "as": "كـ", "at": "عند", "not": "لا", "no": "لا", "also": "أيضاً",
    "such": "مثل", "can": "يمكن", "may": "قد", "will": "سوف", "should": "ينبغي",
    "must": "يجب", "if": "إذا", "but": "لكن", "because": "لأن", "so": "لذا",
    "more": "أكثر", "less": "أقل", "most": "معظم", "all": "كل", "some": "بعض",
    "any": "أي", "each": "كل", "both": "كلا", "other": "آخر", "another": "آخر",
    "into": "إلى داخل", "through": "عبر", "during": "خلال", "after": "بعد",
    "before": "قبل", "between": "بين", "about": "حول", "than": "من",
    "then": "ثم", "there": "هناك", "here": "هنا", "up": "أعلى", "down": "أسفل",
    "out": "خارج", "over": "فوق", "under": "تحت", "again": "مرة أخرى",
    "very": "جداً", "well": "جيداً", "just": "فقط", "now": "الآن",
    "patient": "المريض", "patients": "المرضى", "treatment": "العلاج",
    "therapy": "العلاج", "dose": "الجرعة", "doses": "الجرعات",
    "administration": "الإعطاء", "route": "الطريق", "routes": "الطرق",
    "oral": "فموي", "injection": "الحقن", "inject": "يحقن",
    "skin": "الجلد", "tissue": "النسيج", "mucosa": "الغشاء المخاطي",
    "absorption": "الامتصاص", "absorbed": "يُمتص", "metabolism": "الأيض",
    "first": "الأول", "pass": "المرور", "system": "الجهاز",
    "respiratory": "التنفسي", "tract": "السبيل", "pulmonary": "الرئوي",
    "epithelium": "الظهارة", "surface": "السطح", "area": "المساحة",
    "delivery": "التوصيل", "rapid": "سريع", "rectal": "المستقيمي",
    "rectum": "المستقيم", "vomiting": "القيء", "unconscious": "فاقد الوعي",
    "insulin": "الأنسولين", "ceftriaxone": "سيفترياكسون",
    "paracetamol": "باراسيتامول", "biscodyl": "بيساكوديل",
    "university": "الجامعة", "college": "الكلية", "department": "القسم",
    "techniques": "التقنيات", "technical": "التقني", "northern": "الشمالية",
    "lab": "المختبر", "laboratory": "المختبر", "applied": "التطبيقي",
    "therapeutics": "العلاجيات", "pharmacy": "الصيدلة",
    "ph": "الصيدلاني", "additional": "إضافية",
    "aspirin": "الأسبرين", "become": "أصبح", "standard": "المعيار",
    "care": "الرعاية", "antiplatlete": "مضاد للصفيحات",
}

AR_EN: dict[str, str] = {
    "مرحبا": "hello", "أهلا": "welcome", "العالم": "the world", "هذا": "this",
    "هذه": "this", "نص": "text", "النص": "the text", "تجريبي": "sample",
    "مستند": "document", "المستند": "the document", "ملف": "file",
    "الملف": "the file", "صفحة": "page", "الصفحة": "the page",
    "ترجمة": "translation", "الترجمة": "the translation", "لغة": "language",
    "اللغة": "the language", "العربية": "Arabic", "الإنجليزية": "English",
    "تطبيق": "application", "التطبيق": "the application", "خدمة": "service",
    "الخدمة": "the service", "نظام": "system", "النظام": "the system",
    "بيانات": "data", "معلومات": "information", "برنامج": "software",
    "حاسوب": "computer", "شبكة": "network", "الإنترنت": "the internet",
    "ذكاء": "intelligence", "اصطناعي": "artificial", "تعلم": "learning",
    "الآلة": "the machine", "تحليل": "analysis", "نتائج": "results",
    "مشروع": "project", "عمل": "work", "فريق": "team", "شركة": "company",
    "عميل": "client", "مستخدم": "user", "تقنية": "technology",
    "تطوير": "development", "تقرير": "report", "دراسة": "study",
    "بحث": "research", "منتج": "product", "سوق": "market", "سعر": "price",
    "جودة": "quality", "أمان": "security", "سرعة": "speed", "دقة": "accuracy",
    "أداء": "performance", "خطة": "plan", "هدف": "goal", "نمو": "growth",
    "قيمة": "value", "تكلفة": "cost", "وقت": "time", "يوم": "day",
    "شهر": "month", "سنة": "year", "اليوم": "today", "غدا": "tomorrow",
    "أمس": "yesterday", "كبير": "large", "صغير": "small", "جديد": "new",
    "قديم": "old", "سريع": "fast", "دقيق": "accurate", "مهم": "important",
    "أفضل": "best", "أول": "first", "ثاني": "second", "جميع": "all",
    "كل": "every", "بعض": "some", "كثير": "many", "أكثر": "more",
    "أقل": "less", "و": "and", "أو": "or", "في": "in", "من": "from",
    "على": "on", "إلى": "to", "عن": "about", "مع": "with", "بدون": "without",
    "هو": "is", "هي": "is", "كان": "was", "يكون": "is", "يمكن": "can",
    "يجب": "must", "سوف": "will", "لا": "no", "نعم": "yes", "أن": "that",
    "كما": "as", "حيث": "where", "يعمل": "works", "يقدم": "provides",
    "يوفر": "provides", "يستخدم": "uses", "يتم": "is done", "تم": "was done",
    "باستخدام": "using", "عبر": "via", "بين": "between", "خلال": "during",
    "بعد": "after", "قبل": "before", "الآن": "now", "دائما": "always",
    "مثال": "example", "جدا": "very", "أيضا": "also", "فقط": "only",
    "حتى": "until", "لكن": "but", "لأن": "because", "إذا": "if",
    "عندما": "when", "القلب": "the heart", "الدم": "the blood",
    "الضغط": "the pressure", "الدواء": "the drug", "الأدوية": "the drugs",
    "الذبحة": "the angina", "الصدرية": "pectoris", "الألم": "the pain",
    "العلاج": "the treatment", "المريض": "the patient", "المرضى": "the patients",
    "الجرعة": "the dose", "الشرايين": "the arteries", "الأوردة": "the veins",
}

_AR2LAT = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "aa", "ٱ": "a", "ب": "b", "ت": "t",
    "ث": "th", "ج": "j", "ح": "h", "خ": "kh", "د": "d", "ذ": "dh", "ر": "r",
    "ز": "z", "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
    "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l", "م": "m",
    "ن": "n", "ه": "h", "و": "w", "ي": "y", "ى": "a", "ء": "'", "ئ": "'",
    "ؤ": "'", "ة": "a", "ـ": "",
}
_LAT2AR = {
    "a": "ا", "b": "ب", "c": "ك", "d": "د", "e": "ي", "f": "ف", "g": "ج",
    "h": "ه", "i": "ي", "j": "ج", "k": "ك", "l": "ل", "m": "م", "n": "ن",
    "o": "و", "p": "ب", "q": "ق", "r": "ر", "s": "س", "t": "ت", "u": "و",
    "v": "ف", "w": "و", "x": "كس", "y": "ي", "z": "ز",
}
_DIACRITICS = "\u064b\u064c\u064d\u064e\u064f\u0650\u0651\u0652\u0670\u0640"
_TOKEN_SPLIT = re.compile(r"(\s+)")
_STRIP = "،.,؛;:!؟?\"'()[]{}«»…"


def _romanize_ar(word: str) -> str:
    out = []
    for ch in word:
        if ch in _DIACRITICS:
            continue
        out.append(_AR2LAT.get(ch, ch if not ARABIC_RANGE.search(ch) else ""))
    return re.sub(r"(.)\1{2,}", r"\1\1", "".join(out))


def _arabize_en(word: str) -> str:
    out = []
    for ch in word:
        low = ch.lower()
        if low in _LAT2AR:
            out.append(_LAT2AR[low])
        elif ch.isdigit() or ch in "-/":
            out.append(ch)
        else:
            out.append("")
    return "".join(out)


def _lookup_ar(word: str) -> str:
    if word in AR_EN:
        return AR_EN[word]
    for suf in ("ها", "هم", "هن", "كم", "نا", "ه", "ك", "ي"):
        if word.endswith(suf) and len(word) > len(suf) + 1 and word[:-len(suf)] in AR_EN:
            return AR_EN[word[:-len(suf)]]
    return _romanize_ar(word)


def _lookup_en(word: str) -> str:
    low = word.lower()
    if low in EN_AR:
        return EN_AR[low]
    for suf, repl in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ed", "")):
        if low.endswith(suf) and len(low) > len(suf) + 1 and low[:-len(suf)] + repl in EN_AR:
            return EN_AR[low[:-len(suf)] + repl]
    return _arabize_en(word)


def _mock_line_en2ar(line: str) -> str:
    """English -> Arabic: phrase-level longest-match first, then word level."""
    text = line
    for phrase in sorted((k for k in EN_AR if " " in k), key=len, reverse=True):
        if phrase in text.lower():
            text = re.sub(re.escape(phrase), EN_AR[phrase], text, flags=re.IGNORECASE)
    out = []
    for tok in _TOKEN_SPLIT.split(text):
        if LATIN_RANGE.search(tok):
            core = tok.strip(_STRIP)
            pre = tok[: len(tok) - len(tok.lstrip(_STRIP))]
            suf = tok[len(tok.rstrip(_STRIP)):]
            out.append(pre + _lookup_en(core) + suf)
        else:
            out.append(tok)
    return "".join(out)


def _mock_line_ar2en(line: str) -> str:
    out = []
    for tok in _TOKEN_SPLIT.split(line):
        if ARABIC_RANGE.search(tok):
            core = tok.strip(_STRIP)
            pre = tok[: len(tok) - len(tok.lstrip(_STRIP))]
            suf = tok[len(tok.rstrip(_STRIP)):]
            out.append(pre + _lookup_ar(core) + suf)
        else:
            out.append(tok)
    return "".join(out)


class Translator:
    """Bidirectional translator: real REST API when configured, else offline mock."""

    def __init__(self) -> None:
        self.api_url = TRANSLATE_API_URL
        self.api_key = TRANSLATE_API_KEY
        self.provider = TRANSLATE_PROVIDER
        self.forced_src = SOURCE_LANG
        self.forced_tgt = TARGET_LANG
        self.mock_used = False

    @property
    def mode(self) -> str:
        return "api" if (self.api_url and self.api_key) else "mock"

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "translate_mode": TRANSLATE_MODE,
            "provider": self.provider if self.mode == "api" else "offline-mock",
            "source": self.forced_src, "target": self.forced_tgt,
            "api_url_configured": bool(self.api_url),
            "api_key_configured": bool(self.api_key),
            "en_ar_entries": len(EN_AR), "ar_en_entries": len(AR_EN),
        }

    def _resolve(self, text: str) -> tuple[str, str]:
        src = self.forced_src if self.forced_src in ("ar", "en") else detect_lang(text)
        tgt = self.forced_tgt if self.forced_tgt in ("ar", "en") else ("en" if src == "ar" else "ar")
        if tgt == src:
            tgt = "en" if src == "ar" else "ar"
        return src, tgt

    def translate(self, text: str, target: str | None = None) -> str:
        if not text or not text.strip():
            return text
        src, tgt = self._resolve(text)
        if target in ("ar", "en"):
            tgt = target
        if self.mode == "api":
            try:
                self.mock_used = False
                return self._api_translate(text, src, tgt)
            except Exception as exc:
                log.warning("API translation failed (%s) - using mock.", exc)
        self.mock_used = True
        return self._mock_translate(text, src, tgt)

    def _mock_translate(self, text: str, src: str, tgt: str) -> str:
        if tgt == "ar":
            return "\n".join(_mock_line_en2ar(ln) for ln in text.split("\n"))
        return "\n".join(_mock_line_ar2en(ln) for ln in text.split("\n"))

    def _api_translate(self, text: str, src: str, tgt: str) -> str:
        if requests is None:
            raise RuntimeError("'requests' is not installed")
        if self.provider == "google":
            payload = {"q": text, "source": src, "target": tgt, "format": "text"}
            r = requests.post(self.api_url, params={"key": self.api_key}, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()["data"]["translations"][0]["translatedText"]
        payload = {"q": text, "source": src, "target": tgt, "format": "text", "api_key": self.api_key}
        r = requests.post(self.api_url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        for k in ("translatedText", "translation", "result", "text"):
            if isinstance(data, dict) and data.get(k):
                return data[k]
        raise RuntimeError(f"Unrecognised API response: {str(data)[:200]}")


TRANSLATOR = Translator()


# ==========================================================================
# 4. FONT / MEASUREMENT HELPERS
#    IMPORTANT: widths are always measured on LOGICAL characters. Measuring the
#    *presentation forms* produced by arabic_reshaper yields zero widths in
#    Amiri (those glyphs are not in the font's cmap), which silently collapsed
#    every translation to a single line and made the insert overflow.
# ==========================================================================
_AR_FONT_OBJ = None
if FONT_AR_REG.exists():
    try:
        _AR_FONT_OBJ = fitz.Font(fontfile=str(FONT_AR_REG))
    except Exception as exc:                             # pragma: no cover
        log.warning("Could not load Arabic font metrics (%s).", exc)

_AR_FONT_NAME = "amiri"


def _ensure_arabic_font(page) -> bool:
    """True when an Arabic TTF is available."""
    return FONT_AR_REG.exists()


def _text_width(text: str, size: float) -> float:
    """Width of `text` at `size`, measured on whatever glyphs it holds."""
    if _AR_FONT_OBJ is not None and has_arabic(text):
        try:
            return _AR_FONT_OBJ.text_length(text, fontsize=size)
        except Exception:
            pass
    return fitz.get_text_length(text, fontname="helv", fontsize=size)


def _wrap_text(text: str, size: float, max_w: float) -> list[str]:
    """Greedy word wrap that never produces an empty result for non-empty text."""
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


def _rtl_body(text: str, size: float, max_w: float) -> tuple[str, int]:
    """Return (shaped multi-line body, number of lines) for a logical Arabic text."""
    logical_lines = _wrap_text(text, size, max_w)
    shaped = [shape_arabic(ln) for ln in logical_lines]
    return "\n".join(shaped), len(shaped)


def _rtl_height(text: str, size: float, max_w: float) -> float:
    """Height needed to draw `text` (logical) as wrapped Arabic lines.

    Drawn line-by-line with our own leading, so the height is deterministic and
    does not depend on the (very tall) built-in line metrics of the Amiri font.
    """
    lines = [ln for ln in _wrap_text(text, size, max_w) if ln.strip()]
    return max(1, len(lines)) * size * AR_LEADING


def _draw_rtl_lines(page, x_right: float, top: float, text: str,
                    size: float, color: str) -> int:
    """Draw logical Arabic right-aligned at `x_right`, one line at a time.

    `insert_textbox` uses Amiri's built-in line height (~2.4x the font size) and
    silently drops the content when the box is even slightly short. Drawing each
    line explicitly keeps the leading under our control and never clips.
    """
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
            log.warning("rtl line insert failed: %s", exc)
        baseline += leading
    return drawn


def _ar_font_size(en_size: float) -> float:
    return min(AR_FONT_MAX, max(AR_FONT_MIN, en_size * AR_FONT_SCALE))


# ==========================================================================
# 5. IN-PLACE OVERLAY ENGINE
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
    color = rgb_to_hex(max(set(colors), key=colors.count)) if colors else "#000000"
    return size, color, bold


def _tight_bottom(block: dict, size: float) -> float:
    """Visual bottom of a block: the last line's bbox bottom minus the descent."""
    lines = block.get("lines", [])
    if not lines:
        return block["bbox"][3]
    y1 = lines[-1]["bbox"][3]
    y0 = lines[0]["bbox"][1]
    return max(y1 - 0.22 * size, y0 + 0.55 * size)


_BULLET_RE = re.compile(r"^[\u2022\u25aa\u25e6\u25cf\-\u2013*]|^[\(\[]?[A-Za-z0-9]{1,3}[\)\].-]\s")


def _block_segments(block: dict, size: float) -> list[dict]:
    """Split a text block into paragraph / bullet segments.

    PyMuPDF's `get_text("dict")` often merges a whole page of flowing prose into
    a single block, which would push one large translation to the bottom of that
    block instead of under each paragraph. This groups the block's lines the way
    a reader sees them: a new segment starts at a blank-line-sized gap or at a
    bullet / list marker.
    """
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
        bottom = seg[-1][1] - 0.20 * size
        out.append({"text": text, "bottom": bottom})
    return out


def _insert_rtl(page, rect, logical_text, size, color) -> bool:
    """Draw logical Arabic into `rect` (RTL, right aligned)."""
    if not _ensure_arabic_font(page):
        return False
    body, _n = _rtl_body(logical_text, size, rect.width - 2)
    try:
        rc = page.insert_textbox(rect, body, fontname=_AR_FONT_NAME,
                                 fontfile=str(FONT_AR_REG), fontsize=size,
                                 color=_hex_to_rgb(color), align=fitz.TEXT_ALIGN_RIGHT)
        return rc is not None and rc >= -0.5
    except Exception as exc:
        log.warning("RTL insert failed: %s", exc)
        return False


def _insert_ltr(page, rect, text, size, color) -> bool:
    try:
        rc = page.insert_textbox(rect, text, fontname="helv", fontsize=size,
                                 color=_hex_to_rgb(color), align=fitz.TEXT_ALIGN_LEFT)
        return rc is not None and rc >= -0.5
    except Exception as exc:
        log.warning("LTR insert failed: %s", exc)
        return False


def _insert_any(page, rect, text, lang, size, color, bold=False) -> bool:
    if lang == "ar":
        return _insert_rtl(page, rect, text, size, color)
    return _insert_ltr(page, rect, text, size, color)


def _cover_color_for(page, rect) -> tuple:
    if COVER_COLOR != "auto":
        return (1, 1, 1)
    try:
        pix = page.get_pixmap(clip=rect, dpi=36)
        if pix.n >= 3 and pix.samples:
            n = len(pix.samples) // (pix.width * pix.height)
            cnt = pix.width * pix.height
            r = sum(pix.samples[i] for i in range(0, len(pix.samples), n)) / cnt
            g = sum(pix.samples[i + 1] for i in range(0, len(pix.samples), n)) / cnt
            b = sum(pix.samples[i + 2] for i in range(0, len(pix.samples), n)) / cnt
            return (r / 255, g / 255, b / 255)
    except Exception:
        pass
    return (1, 1, 1)


# --------------------------------------------------------------------------
# 5a. THE CORE: rebuild one page with a translation inserted below each block
# --------------------------------------------------------------------------
def _append_below_page(out_doc, src_doc, pno: int) -> dict:
    """
    Rebuild page `pno` of `src_doc` into `out_doc`, inserting the Arabic
    translation directly BELOW every English text block.

    * the page keeps its original width and grows only on the Y axis;
    * the original content is imported verbatim with `show_pdf_page`
      (Form-XObject copy) so text stays vector and images / drawings are 1:1;
    * the only geometry change is the vertical offset needed to open each gap.
    """
    src_page = src_doc[pno]
    W, H = float(src_page.rect.width), float(src_page.rect.height)
    blocks = [b for b in src_page.get_text("dict")["blocks"]
              if b.get("type") == 0 and _block_text(b)]
    latin = [b for b in blocks if detect_lang(_block_text(b)) == "en"]

    # Nothing to translate on this page -> copy it untouched.
    if not latin:
        out_doc.insert_pdf(src_doc, from_page=pno, to_page=pno)
        return {"page": pno + 1, "inserted": 0, "added_h": 0.0, "blocks": 0}

    latin.sort(key=lambda b: b["bbox"][1])
    col_x0 = max(AR_MARGIN, min(b["bbox"][0] for b in latin) - 2)
    col_x1 = W - AR_MARGIN
    text_w = max(40.0, col_x1 - col_x0 - 6)

    items = []
    for b in latin:
        en_size, _color, _bold = _block_style(b)
        ar_size = _ar_font_size(en_size)
        for seg in _block_segments(b, en_size):
            translation = TRANSLATOR.translate(seg["text"], target="ar")
            clear = 0.30 * en_size + 1.5        # keep clear of the EN descenders
            ah = clear + _rtl_height(translation, ar_size, text_w)
            items.append({
                "tb": seg["bottom"],
                "ar_size": ar_size,
                "ar": translation,
                "clear": clear,
                "ah": ah,
            })
    items.sort(key=lambda it: it["tb"])

    # Boundaries: 0, every block bottom, page bottom. Each item is inserted into
    # the gap that opens at its own block bottom.
    bounds = sorted({0.0} | {it["tb"] for it in items} | {H})
    total_h = sum(it["ah"] for it in items)
    page = out_doc.new_page(width=W, height=H + total_h)

    if TRANSLATE_MODE == "bilingual" and BILINGUAL_HEADER:
        try:
            page.draw_rect(fitz.Rect(0, 0, W, 24), color=None, fill=(0.94, 0.96, 0.99), width=0)
            page.insert_textbox(
                fitz.Rect(AR_MARGIN, 4, W - AR_MARGIN, 21),
                shape_arabic("النص الإنجليزي الأصلي مع الترجمة العربية أسفله"),
                fontname=_AR_FONT_NAME, fontfile=str(FONT_AR_REG), fontsize=9,
                color=_hex_to_rgb("#2e75b6"), align=fitz.TEXT_ALIGN_RIGHT)
        except Exception:
            pass

    # 1) import the original content, strip by strip, each shifted by the amount
    #    of translation added above it.
    for k in range(len(bounds) - 1):
        y0, y1 = bounds[k], bounds[k + 1]
        if y1 - y0 <= 0.01:
            continue
        shift = sum(it["ah"] for it in items if it["tb"] <= y0 + 0.01)
        try:
            page.show_pdf_page(fitz.Rect(0, y0 + shift, W, y1 + shift), src_doc, pno,
                               clip=fitz.Rect(0, y0, W, y1), keep_proportion=False)
        except Exception as exc:
            log.warning("show_pdf_page strip failed (page %d, %.0f-%.0f): %s",
                        pno + 1, y0, y1, exc)

    # 2) write each translation into the gap that was opened right below its block
    inserted = 0
    for it in items:
        shift_before = sum(o["ah"] for o in items if o["tb"] < it["tb"] - 0.01)
        top = it["tb"] + shift_before + it["clear"]
        if _draw_rtl_lines(page, col_x1, top, it["ar"], it["ar_size"], AR_COLOR_HEX):
            inserted += 1

    return {"page": pno + 1, "inserted": inserted,
            "added_h": round(total_h, 1), "blocks": len(items)}


def _replace_mode_page(page, pno: int) -> int:
    """Legacy monolingual overlay: cover each block, write the translation in place."""
    blocks = [b for b in page.get_text("dict")["blocks"] if b.get("type") == 0]
    jobs = []
    for b in blocks:
        text = _block_text(b)
        if not text:
            continue
        tgt = "ar" if detect_lang(text) == "en" else "en"
        jobs.append((fitz.Rect(b["bbox"]), TRANSLATOR.translate(text, target=tgt),
                     tgt, _block_style(b)))
    if not jobs:
        return 0
    for rect, _t, _l, _s in jobs:
        page.add_redact_annot(rect, fill=_cover_color_for(page, rect))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    done = 0
    for rect, text, lang, (size, _c, bold) in jobs:
        s = max(MIN_FONT, size * FONT_SHRINK)
        if _insert_any(page, rect, text, lang, s, "#000000", bold):
            done += 1
    return done


def translate_pdf_inplace(in_path, out_path, translator=None, progress=None) -> dict:
    """
    Core routine. In the default `below` mode every page is rebuilt at its
    original width, enlarged on the Y axis, and the Arabic translation is placed
    directly beneath each English block while the source text and all graphics
    are preserved.
    """
    global TRANSLATOR
    if translator is not None:
        TRANSLATOR = translator
    translator = TRANSLATOR
    started = datetime.now()
    mode = TRANSLATE_MODE

    src_doc = fitz.open(str(in_path))
    stats = {
        "mode": mode, "pages": src_doc.page_count, "blocks": 0,
        "translated_blocks": 0, "redactions": 0, "appended_pages": 0,
        "images_before": 0, "images_after": 0,
        "drawings_before": 0, "drawings_after": 0,
        "source_chars": 0, "translated_chars": 0, "mock_used": False,
        "pages_detail": [],
    }

    # ---- count the source inventory (images / drawings / chars) -----------
    for pno in range(src_doc.page_count):
        p = src_doc[pno]
        stats["images_before"] += len(p.get_images(full=True))
        stats["drawings_before"] += len(p.get_drawings())
        for b in p.get_text("dict")["blocks"]:
            if b.get("type") == 0 and _block_text(b):
                stats["blocks"] += 1
                stats["source_chars"] += len(_block_text(b))

    if mode == "replace":
        out_doc = fitz.open(str(in_path))          # edit in place
        for pno in range(out_doc.page_count):
            stats["translated_blocks"] += _replace_mode_page(out_doc[pno], pno)
            if progress:
                progress(pno + 1, out_doc.page_count)
    else:
        out_doc = fitz.open()                      # rebuild, preserving content
        for pno in range(src_doc.page_count):
            info = _append_below_page(out_doc, src_doc, pno)
            stats["translated_blocks"] += info["inserted"]
            stats["pages_detail"].append(info)
            if progress:
                progress(pno + 1, src_doc.page_count)

    # ---- verify the output inventory -------------------------------------
    for pno in range(out_doc.page_count):
        p = out_doc[pno]
        stats["images_after"] += len(p.get_images(full=True))
        stats["drawings_after"] += len(p.get_drawings())
        stats["translated_chars"] += sum(
            len(_block_text(b)) for b in p.get_text("dict")["blocks"]
            if b.get("type") == 0 and _block_text(b))

    stats["mock_used"] = translator.mock_used
    out_doc.save(str(out_path), garbage=3, deflate=True)
    out_doc.close()
    src_doc.close()
    stats.update({
        "ok": True, "input": str(in_path), "output": str(out_path),
        "translator_mode": translator.mode,
        "images_preserved": stats["images_after"] >= stats["images_before"],
        "drawings_preserved": stats["drawings_after"] >= stats["drawings_before"],
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 3),
    })
    log.info("In-place translation finished: mode=%s blocks=%d translated=%d "
             "imgs %d->%d draws %d->%d", mode, stats["blocks"],
             stats["translated_blocks"], stats["images_before"],
             stats["images_after"], stats["drawings_before"], stats["drawings_after"])
    return stats


# ==========================================================================
# 6. FLASK APPLICATION
# ==========================================================================
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

INDEX_HTML = """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ app }}</title>
<style>
  :root{--brand:#1f3864;--accent:#2e75b6;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:"Segoe UI",Tahoma,system-ui,sans-serif;
       background:linear-gradient(135deg,#eef3fb,#f7f9fc);color:#111827;
       min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}
  .card{background:#fff;width:min(680px,100%);border-radius:18px;padding:38px;
        box-shadow:0 18px 50px rgba(31,56,100,.16);}
  h1{margin:0 0 6px;color:var(--brand);font-size:23px;}
  .sub{color:#6b7280;font-size:14px;margin-bottom:24px;}
  .drop{border:2px dashed #c7d4e8;border-radius:14px;padding:34px;text-align:center;
        background:#f8fafd;transition:.2s;cursor:pointer;}
  .drop:hover{border-color:var(--accent);background:#f1f6fd;}
  input[type=file]{margin-top:12px;font-size:14px;}
  button{margin-top:22px;width:100%;padding:14px;border:0;border-radius:12px;
         background:var(--accent);color:#fff;font-size:16px;font-weight:600;cursor:pointer;}
  button:hover{background:var(--brand);}
  .status{margin-top:18px;font-size:13px;color:#6b7280;line-height:1.8;}
  code{background:#eef3fb;padding:2px 6px;border-radius:6px;}
</style></head>
<body><div class="card">
  <h1>ترجمة PDF مع الحفاظ على التصميم الأصلي</h1>
  <div class="sub">Translation below each block &middot; mode: <b>{{ mode }}</b> &middot; engine: <b>PyMuPDF</b> &middot; translator: <b>{{ tmode }}</b></div>
  <form action="/translate" method="post" enctype="multipart/form-data">
    <label class="drop" for="file">
      <div style="font-size:15px;font-weight:600;color:#1f3864">اختر ملف PDF</div>
      <div style="font-size:13px;color:#6b7280;margin-top:6px">يُحفظ النص الأصلي والصور كما هي وتُضاف الترجمة أسفل كل فقرة</div>
      <input id="file" type="file" name="file" accept="application/pdf" required>
    </label>
    <button type="submit">ترجم ونزّل PDF</button>
  </form>
  <div class="status">
    <code>POST /translate</code> &middot; <code>GET /health</code> &middot; أضف <code>?format=json</code> للتقرير.<br>
    الأوضاع: <code>below</code> (افتراضي) &middot; <code>bilingual</code> &middot; <code>replace</code>
  </div>
</div></body></html>"""


@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, app=APP_NAME,
                                  mode=TRANSLATE_MODE, tmode=TRANSLATOR.mode)


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", app=APP_NAME, engine="PyMuPDF",
                   translate_mode=TRANSLATE_MODE,
                   arabic_font=FONT_AR_REG.exists(),
                   translator=TRANSLATOR.status())


@app.route("/translate", methods=["POST"])
def translate_route():
    if "file" not in request.files or not request.files["file"].filename:
        return jsonify(ok=False, error="No file uploaded (field name must be 'file')."), 400
    upload = request.files["file"]
    tmp_in = OUT_DIR / f"upload_{uuid.uuid4().hex}.pdf"
    upload.save(str(tmp_in))
    out_name = f"translated_{Path(upload.filename).stem}_{uuid.uuid4().hex[:8]}.pdf"
    tmp_out = OUT_DIR / out_name
    try:
        report = translate_pdf_inplace(tmp_in, tmp_out)
    except Exception as exc:
        log.exception("Processing failed")
        return jsonify(ok=False, error=str(exc)), 500
    finally:
        tmp_in.unlink(missing_ok=True)
    if request.args.get("format") == "json":
        report["download_url"] = f"/download/{out_name}"
        return jsonify(report)
    return send_file(str(tmp_out), as_attachment=True, download_name=out_name,
                     mimetype="application/pdf")


@app.route("/download/<path:name>", methods=["GET"])
def download(name: str):
    target = (OUT_DIR / name).resolve()
    if OUT_DIR.resolve() not in target.parents or not target.exists():
        return jsonify(ok=False, error="Not found"), 404
    return send_file(str(target), as_attachment=True, mimetype="application/pdf")


# ==========================================================================
# 7. CLI
# ==========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--input", "-i", help="Source PDF")
    ap.add_argument("--output", "-o", help="Output PDF")
    ap.add_argument("--mode", choices=["below", "bilingual", "replace"],
                    help="Override TRANSLATE_MODE")
    ap.add_argument("--json", action="store_true", help="Print a JSON report")
    ap.add_argument("--serve", action="store_true", help="Run the Flask server")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", 5000)))
    args = ap.parse_args()

    if args.mode:
        global TRANSLATE_MODE
        TRANSLATE_MODE = args.mode

    if args.serve or not args.input:
        log.info("Starting Flask on port %s (mode=%s translator=%s)",
                 args.port, TRANSLATE_MODE, TRANSLATOR.mode)
        app.run(host="0.0.0.0", port=args.port, debug=False)
        return 0

    out = args.output or str(OUT_DIR / (Path(args.input).stem + "_translated.pdf"))
    report = translate_pdf_inplace(args.input, out)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"✔ Translated PDF written to: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
