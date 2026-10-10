#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translator.py  -  مُترجِم ذكي متعدّد الخدمات  (v9)
=================================================
الفكرة: ترجمة من أي لغة إلى العربية بجودة عالية، مع ترتيب خدمات:
  1) Gemini      (GEMINI_API_KEY)   - الموديل من GEMINI_MODEL أو اكتشاف تلقائي
  2) Groq        (GROQ_API_KEY)     - أسماء الموديلات من GROQ_MODELS أو اكتشاف تلقائي
  3) google-free (بلا مفتاح)        - جودة عالية جداً، نقطتان + إعادة محاولة
  4) MyMemory    (بلا مفتاح)        - احتياطي
  5) google      (deep-translator)  - احتياطي
  6) LibreTranslate (LIBRETRANSLATE_URL) - اختياري
  7) mock        - قاموس محلي (يُعلَن صراحة كمترجم تقريبي عند استخدامه)

ميزات مهمة:
  * لا يوجد أي اسم موديل مكتوب ثابتاً: يُقرأ من متغيّر البيئة أو يُكتشف من الـAPI.
  * ترجمة دفعات (batch) لتقليل عدد الطلبات وتسريع العمل وتفادي حدود الاستخدام.
  * إعادة محاولة مع انتظار تصاعدي (exponential backoff).
  * إيقاف مؤقت لأي خدمة تفشل مرتين متتاليتين (cooldown) حتى لا تُبطئ كل مقطع.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time

try:
    import requests
except Exception:                                   # pragma: no cover
    requests = None

try:
    from deep_translator import GoogleTranslator
except Exception:                                   # pragma: no cover
    GoogleTranslator = None

log = logging.getLogger("translator")

# --------------------------------------------------------------------------
# نقاط النهاية
# --------------------------------------------------------------------------
GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_GEN_URL = ("https://generativelanguage.googleapis.com/v1beta/"
                  "models/{model}:generateContent")
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
GOOGLE_FREE_HOSTS = [
    "https://translate.googleapis.com/translate_a/single",
    "https://clients5.google.com/translate_a/t",
]

LANG_NAMES = {
    "ar": "Arabic", "en": "English", "fr": "French", "de": "German",
    "tr": "Turkish", "es": "Spanish", "ru": "Russian", "fa": "Persian",
    "ur": "Urdu", "it": "Italian", "nl": "Dutch", "zh": "Chinese",
}

# الفاصل الذي نستخدمه لتقسيم دفعة واحدة إلى عدّة مقاطع (رمز نادر لا يظهر في النص)
BATCH_SEP = "\n@@@\n"

# ==========================================================================
# أدوات نصية
# ==========================================================================
ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
LATIN_RE = re.compile(r"[A-Za-z]")
_TOKEN_SPLIT = re.compile(r"(\s+)")
_STRIP = "،.,؛;:!؟?\"'()[]{}«»…"


def has_arabic(text: str) -> bool:
    return bool(ARABIC_RE.search(text or ""))


def arabic_ratio(text: str) -> float:
    if not text:
        return 0.0
    ar = len(ARABIC_RE.findall(text))
    la = len(LATIN_RE.findall(text))
    total = ar + la
    return (ar / total) if total else 0.0


def _chunk_bytes(text: str, max_bytes: int = 450) -> list[str]:
    words = str(text).replace("\r", "").split(" ")
    chunks, cur = [], ""
    for w in words:
        trial = w if not cur else cur + " " + w
        if len(trial.encode("utf-8")) > max_bytes and cur:
            chunks.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    return chunks or [str(text)]


# ==========================================================================
# تصحيحات المصطلحات + فصل العربي عن اللاتيني
# ==========================================================================
TERM_FIXES = {
    "نقطة في البوصة": "مثبطات مضخة البروتون",
    "هضم السرمرض": "حرقة المعدة",
    "مثبطات مضخة البروتونات": "مثبطات مضخة البروتون",
    "عسر الهضم الحموضي": "عسر الهضم الحمضي",
    "لوحة تصلب الشرايين": "لويحة تصلب الشرايين",
    "التحميل التالي": "الحمل اللاحق",
    "الأحماض المعدية": "حمض المعدة",
    "المخدرات": "الأدوية",
    "مخدرات": "أدوية",
    "الدموية الدم": "الدم",
    "القلب عضلة": "عضلة القلب",
    "الدم ضغط": "ضغط الدم",
    "المعدة حمض": "حمض المعدة",
    "الدم تدفق": "تدفق الدم",
    "الدم إمداد": "إمداد الدم",
}
_ORD = [
    ("المعدة حمض", "حمض المعدة"),
    ("الدم إمداد", "إمداد الدم"),
    ("الدم ضغط", "ضغط الدم"),
    ("الدم تدفق", "تدفق الدم"),
    ("الانقباضي الضغط", "ضغط الدم الانقباضي"),
    ("المرئي ارتجاع", "ارتجاع المريء"),
]
_AR_LAT = re.compile(r"([\u0600-\u06FF])([A-Za-z])")
_LAT_AR = re.compile(r"([A-Za-z])([\u0600-\u06FF])")


def polish_text(text: str, target: str = "ar") -> str:
    """تصحيح مصطلحات شائعة + فصل الحروف العربية عن اللاتينية."""
    if not text:
        return text
    for bad, good in TERM_FIXES.items():
        if bad in text:
            text = text.replace(bad, good)
    for bad, good in _ORD:
        if bad in text:
            text = text.replace(bad, good)
    if target == "ar":
        t = text
        for _ in range(2):
            t = _AR_LAT.sub(r"\1 \2", t)
            t = _LAT_AR.sub(r"\1 \2", t)
        text = t
    return text


# ==========================================================================
# المترجم التقريبي (قاموس) - يُستخدم فقط عند فشل كل الخدمات الحقيقية
# ==========================================================================
EN_AR: dict[str, str] = {
    "gastrointestinal tract": "الجهاز الهضمي",
    "gastric acid": "حمض المعدة",
    "acid control": "التحكم في الحمض",
    "proton pump inhibitors": "مثبطات مضخة البروتون",
    "learning objectives": "أهداف التعلم",
    "adverse effects": "الآثار الجانبية",
    "mechanism of action": "آلية العمل",
    "drug interactions": "التفاعلات الدوائية",
    "side effects": "الآثار الجانبية",
    "peptic ulcer": "القرحة الهضمية",
    "antimicrobial": "مضاد للميكروبات",
    "gastroesophageal reflux": "ارتجاع المريء",
    "histamine": "الهيستامين", "gastrin": "الجاسترين",
    "acetylcholine": "الأسيتيل كولين",
    "parietal cell": "الخلية الجدارية", "parietal cells": "الخلايا الجدارية",
    "stomach": "المعدة", "digestion": "الهضم", "patient": "المريض",
    "patients": "المرضى", "treatment": "العلاج", "therapy": "العلاج",
    "drug": "دواء", "drugs": "أدوية", "dose": "الجرعة", "doses": "الجرعات",
    "blood": "الدم", "heart": "القلب", "liver": "الكبد", "kidney": "الكلى",
    "cells": "الخلايا", "cell": "الخلية", "acid": "الحمض", "base": "القاعدة",
    "secretion": "الإفراز", "secrete": "تفرز", "increase": "يزيد",
    "increases": "يزيد", "decrease": "يقلل", "decreases": "يقلل",
    "reduce": "يقلل", "reduces": "يقلل", "inhibit": "يثبط", "inhibits": "يثبط",
    "block": "يحجب", "blocks": "يحجب", "used": "يُستخدم", "use": "يستخدم",
    "using": "باستخدام", "effect": "التأثير", "effects": "التأثيرات",
    "symptoms": "الأعراض", "symptom": "العرض", "pain": "الألم",
    "the": "", "a": "", "an": "", "and": "و", "or": "أو", "of": "من",
    "in": "في", "on": "على", "to": "إلى", "for": "لـ", "with": "مع",
    "by": "بواسطة", "from": "من", "is": "هو", "are": "هي", "was": "كان",
    "this": "هذا", "that": "ذلك", "which": "التي", "when": "عندما",
    "can": "يمكن", "may": "قد", "not": "لا", "also": "أيضاً", "more": "أكثر",
    "less": "أقل", "students": "الطلاب", "explain": "اشرح", "classify": "صنّف",
    "describe": "صف", "identify": "حدّد", "recognize": "تعرّف على",
    "give": "أعطِ", "common": "الشائع", "examples": "أمثلة", "example": "مثال",
    "gastric": "المعدي", "ulcer": "القرحة", "reflux": "الارتجاع",
    "secretion of": "إفراز", "caused by": "بسبب", "leading to": "مما يؤدي إلى",
}


def _lookup_en(word: str) -> str:
    low = word.lower()
    if low in EN_AR:
        return EN_AR[low]
    for suf, repl in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ed", "")):
        if low.endswith(suf) and len(low) > len(suf) + 1 and low[:-len(suf)] + repl in EN_AR:
            return EN_AR[low[:-len(suf)] + repl]
    return word                    # مهم: الكلمة غير المعروفة تبقى إنجليزية بلا نقحرة


# ==========================================================================
# حماية الاختصارات الطبية: كلمة كاملة بحروف كبيرة (PPI, PUD, GERD, NSAIDs) أو
# صيغة كيميائية، تُترك بالإنجليزية ولا تُترجم، لأن الترجمة الحرفية تفسدها
# (PPI صارت "مؤشر أسعار المنتجين" و H2 رقماً خطأً).
# ==========================================================================
_PURE_LATIN = re.compile(r"^[A-Za-z0-9\s\-/().,%+]+$")
_HAS_ARABIC = re.compile(r"[\u0600-\u06FF]")


def _is_acronym(text: str) -> bool:
    """يُرجع True فقط إذا كان النص اختصاراً طبياً قصيراً (PPI, PUD, GERD, H2, NSAIDs)
    فيُترك إنجليزياً كما هو. العناوين النصية العادية تُترجم."""
    t = (text or "").strip()
    if not t or _HAS_ARABIC.search(t) or not _PURE_LATIN.match(t):
        return False
    words = [w for w in re.split(r"[\s/]+", t) if w]
    if not words or len(words) > 6:
        return False
    cap_re = re.compile(r"[A-Z0-9]{2,5}s?$")
    norm_re = re.compile(r"[a-z]{2,}")
    cap_count = 0
    norm_count = 0
    for w in words:
        core = w.strip("().,")
        if not core:
            continue
        if cap_re.match(core):                # اختصار قصير (PPI, H2, GERD, NSAIDs...)
            cap_count += 1
            continue
        if re.fullmatch(r"\d+([.,]\d+)?", core):   # رقم / جرعة
            continue
        if norm_re.fullmatch(core):           # كلمة عادية (are, meals, taken...)
            norm_count += 1
            continue
        return False                          # يحتوي رمزاً/حالة غريبة -> ترجمه
    # اختصار فعلاً فقط إن كان قصيراً (≤4 كلمات) وبكلمة عادية واحدة على الأكثر.
    # هكذا لا تُعامَل الجملة الكاملة التي تحتوي اختصاراً (مثل "PPIs are generally taken before meals") كاختصار.
    return cap_count >= 1 and norm_count <= 1 and len(words) <= 4


# ==========================================================================
# تصحيح المصطلحات الطبية بعد الترجمة (بعض الخدمات تترجم الاختصارات خطأً)
# ==========================================================================
TERM_AR = {
    "مؤشر أسعار المنتجين": "PPI",
    "نقطة لكل بوصة": "PPI",
    "نقاط لكل بوصة": "PPI",
    "نقطة في البوصة": "PPI",
}


# تصحيحات دقيقة تخصّ الكلمة كاملةً (لا تُطبَّق داخل جملة، لتفادي إتلاف كلمات صحيحة)
EXACT_AR = {
    "فصل": "فئة",          # Class (في الجداول الطبية) وليس "فصل" بمعنى محاضرة/فصل دراسي
    "الفصل": "الفئة",
    "الصنف": "الفئة",
    "الرتبة": "الدرجة",
}


def _fix_terms_ar(text: str) -> str:
    t = str(text).strip()
    if t in EXACT_AR:                     # المقطع كله = كلمة واحدة ⇒ صحّح المصطلح
        return EXACT_AR[t]
    for bad, good in TERM_AR.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


def _mock_line_en2ar(line: str) -> str:
    text = str(line)
    for phrase in sorted((k for k in EN_AR if " " in k), key=len, reverse=True):
        if phrase in text.lower():
            text = re.sub(re.escape(phrase), EN_AR[phrase], text, flags=re.IGNORECASE)
    out = []
    for tok in _TOKEN_SPLIT.split(text):
        if LATIN_RE.search(tok):
            core = tok.strip(_STRIP)
            pre = tok[: len(tok) - len(tok.lstrip(_STRIP))]
            suf = tok[len(tok.rstrip(_STRIP)):]
            out.append(pre + _lookup_en(core) + suf)
        else:
            out.append(tok)
    return "".join(out)


def _mock_translate(text: str) -> str:
    if not str(text).strip():
        return text
    return "\n".join(_mock_line_en2ar(ln) for ln in str(text).split("\n"))


# ==========================================================================
# المُترجِم
# ==========================================================================
class Translator:
    def __init__(self) -> None:
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.gemini_model_env = os.getenv("GEMINI_MODEL", "").strip()
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()
        # أسماء موديلات Groq من متغيّر GROQ_MODELS (مفصولة بفواصل) - لا اسم ثابت في الكود
        self.groq_models_env = [m.strip() for m in
                                os.getenv("GROQ_MODELS", "").split(",") if m.strip()]
        self.lt_url = os.getenv("LIBRETRANSLATE_URL", "").strip().rstrip("/")
        self.lt_key = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
        self.mymemory_email = os.getenv("MYMEMORY_EMAIL", "").strip()
        self.source = (os.getenv("SOURCE_LANG", "auto").strip() or "auto")
        self.target = (os.getenv("TARGET_LANG", "ar").strip() or "ar")
        self.timeout = float(os.getenv("HTTP_TIMEOUT", "40"))
        self.cooldown = float(os.getenv("SERVICE_COOLDOWN", "240"))
        self.force_mock = os.getenv("FORCE_MOCK", "").strip() in ("1", "true", "yes")

        self._errors: list[str] = []
        self.last_error = ""
        self._gemini_models: list[str] = []      # قائمة الموديلات المتاحة (مرتّبة)
        self._groq_models: list[str] = []        # قائمة موديلات Groq (مرتّبة)
        self.used_real_service = False
        self.service_counts: dict[str, int] = {}
        self._dead_until: dict[str, float] = {}
        self._fail_count: dict[str, int] = {}

    # ---------------------------------------------------------------- #
    def _backends(self) -> list[str]:
        names = []
        if self.gemini_key:
            names.append("gemini")
        if self.groq_key:
            names.append("groq")
        names += ["google-free", "mymemory"]
        if GoogleTranslator is not None:
            names.append("google")
        if self.lt_url:
            names.append("libretranslate")
        return names

    def all_errors(self) -> list[str]:
        return list(self._errors)

    def _record(self, msg: str) -> None:
        self.last_error = msg
        self._errors.append(msg)
        log.warning(msg)

    def _count(self, name: str, n: int = 1) -> None:
        self.service_counts[name] = self.service_counts.get(name, 0) + n
        self.used_real_service = True

    def _sleep(self, base: float) -> None:
        time.sleep(base + random.uniform(0, base * 0.4))

    # ---------------------------------------------------------------- #
    # اختيار الموديلات تلقائياً (لا اسم ثابت في الكود)
    # ---------------------------------------------------------------- #
    def _gemini_model(self) -> str | None:
        if self.gemini_model_env:
            return self.gemini_model_env
        if self._gemini_models:
            return self._gemini_models[0]
        if requests is None or not self.gemini_key:
            return None
        try:
            r = requests.get(GEMINI_LIST_URL, params={"key": self.gemini_key},
                             timeout=self.timeout)
            if r.status_code != 200:
                self._record(f"Gemini models HTTP {r.status_code}: {r.text[:200]}")
                return None
            ms = r.json().get("models", [])
            usable = [m["name"].split("/")[-1] for m in ms
                      if "generateContent" in m.get("supportedGenerationMethods", [])]
            if not usable:
                self._record("Gemini: لا يوجد موديل يدعم generateContent")
                return None
            usable.sort(key=lambda m: (0 if "flash" in m else
                                       1 if "pro" in m else 2))
            self._gemini_models = usable
            log.info("Gemini: الموديلات المتاحة: %s", ", ".join(usable[:5]))
            return usable[0]
        except Exception as exc:
            self._record(f"Gemini discovery فشل: {exc}")
            return None

    def list_gemini_models(self) -> list[str]:
        """يُرجع كل الموديلات القابلة للاستخدام (لاكتشاف الموديل المطلوب)."""
        self._gemini_model()
        return list(self._gemini_models)

    def _groq_model_list(self) -> list[str]:
        if self.groq_models_env:
            return self.groq_models_env            # من متغيّر البيئة كما هي
        if self._groq_models:
            return self._groq_models
        if requests is None or not self.groq_key:
            return []
        try:
            r = requests.get(GROQ_MODELS_URL,
                             headers={"Authorization": f"Bearer {self.groq_key}"},
                             timeout=self.timeout)
            if r.status_code != 200:
                self._record(f"Groq models HTTP {r.status_code}: {r.text[:200]}")
                return []
            ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
            if not ids:
                self._record("Groq: لا توجد موديلات متاحة")
                return []
            ids.sort(key=lambda m: (
                next((i for i, k in enumerate(
                    ("llama-4-scout", "llama-4-maverick", "gpt-oss-120b",
                     "llama-3.3", "llama-3.1", "mixtral", "gemma")) if k in m), 9)))
            self._groq_models = ids
            log.info("Groq: الموديلات المتاحة: %s", ", ".join(ids[:5]))
            return ids
        except Exception as exc:
            self._record(f"Groq discovery فشل: {exc}")
            return []

    # ---------------------------------------------------------------- #
    # خدمات فردية
    # ---------------------------------------------------------------- #
    def _gemini_one(self, text: str, model: str) -> str:
        prompt = (f"Translate the following text into "
                  f"{LANG_NAMES.get(self.target, self.target)} in a clear "
                  f"simplified academic style. Return ONLY the translation.\n\n{text}")
        url = GEMINI_GEN_URL.format(model=model)
        try:
            r = requests.post(
                url, params={"key": self.gemini_key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.0}},
                timeout=self.timeout)
        except Exception as exc:
            log.error("Gemini فشل الاتصال بـ %s -> %s", url, exc)
            raise RuntimeError(f"Gemini اتصال: {exc}") from exc
        if r.status_code != 200:
            log.error("Gemini HTTP %s من %s\nرد Gemini: %s", r.status_code, url, r.text[:400])
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _gemini_batch(self, texts: list[str]) -> list[str]:
        model = self._gemini_model()
        if not model:
            raise RuntimeError("Gemini: لا يوجد موديل متاح")
        prompt = (
            f"Translate the following {len(texts)} text segments into "
            f"{LANG_NAMES.get(self.target, self.target)} in a clear simplified "
            f"academic style. Segments are separated by a line containing exactly "
            f"@@@. Return EXACTLY {len(texts)} translated segments separated by "
            f"the same @@@ line, in the same order, with no extra text.\n\n"
            + BATCH_SEP.join(texts))
        url = GEMINI_GEN_URL.format(model=model)
        r = requests.post(url, params={"key": self.gemini_key},
                          json={"contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"temperature": 0.0}},
                          timeout=self.timeout)
        if r.status_code != 200:
            log.error("Gemini HTTP %s\n%s", r.status_code, r.text[:400])
            raise RuntimeError(f"Gemini HTTP {r.status_code}")
        out = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        parts = [p.strip() for p in re.split(r"\n?@+@+\n?", out)]
        if len(parts) != len(texts):
            raise RuntimeError(f"Gemini أعاد {len(parts)} بدل {len(texts)}")
        return parts

    def _groq_batch(self, texts: list[str]) -> list[str]:
        models = self._groq_model_list()
        if not models:
            raise RuntimeError("Groq: لا يوجد موديل متاح")
        prompt = (
            f"Translate the following {len(texts)} text segments into "
            f"{LANG_NAMES.get(self.target, self.target)}. Segments are separated by "
            f"a line containing exactly @@@. Return EXACTLY {len(texts)} translated "
            f"segments separated by the same @@@ line, same order, no extra text.\n\n"
            + BATCH_SEP.join(texts))
        last = None
        for model in models[:3]:                    # جرّب أول ثلاثة موديلات
            try:
                r = requests.post(
                    GROQ_CHAT_URL,
                    headers={"Authorization": f"Bearer {self.groq_key}",
                             "Content-Type": "application/json"},
                    json={"model": model, "temperature": 0.0,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=self.timeout)
                if r.status_code != 200:
                    last = f"Groq {model} HTTP {r.status_code}: {r.text[:200]}"
                    log.error("%s", last)
                    continue
                out = r.json()["choices"][0]["message"]["content"].strip()
                parts = [p.strip() for p in re.split(r"\n?@+@+\n?", out)]
                if len(parts) == len(texts):
                    return parts
                last = f"Groq {model}: أعاد {len(parts)} بدل {len(texts)}"
            except Exception as exc:
                last = f"Groq {model}: {exc}"
        raise RuntimeError(last or "Groq فشل")

    def _google_free(self, text: str) -> str:
        if requests is None:
            raise RuntimeError("requests غير مثبّت")
        sl = "auto" if self.source in ("", "auto") else self.source
        last = None
        for attempt in range(3):
            for host in GOOGLE_FREE_HOSTS:
                try:
                    if "clients5" in host:
                        r = requests.get(host, params={"client": "dict-chrome-ex",
                                                       "sl": sl, "tl": self.target, "q": text},
                                         headers={"User-Agent": "Mozilla/5.0"},
                                         timeout=self.timeout)
                        r.raise_for_status()
                        d = r.json()
                        out = d[0] if (isinstance(d, list) and d and isinstance(d[0], str)) else ""
                        if not out and isinstance(d, list) and d and isinstance(d[0], list):
                            out = "".join(seg[0] for seg in d[0] if seg)
                    else:
                        r = requests.get(host, params={"client": "gtx", "sl": sl,
                                                       "tl": self.target, "dt": "t", "q": text},
                                         headers={"User-Agent": "Mozilla/5.0"},
                                         timeout=self.timeout)
                        r.raise_for_status()
                        d = r.json()
                        out = "".join(seg[0] for seg in d[0] if seg and seg[0])
                    if out and out.strip():
                        return out.strip()
                except Exception as exc:
                    last = exc
            self._sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"Google-free: {last}")

    def _mymemory(self, text: str) -> str:
        parts = _chunk_bytes(text, 450)
        outs = []
        src = "en" if self.source in ("", "auto") else self.source
        for part in parts:
            params = {"q": part, "langpair": f"{src}|{self.target}"}
            if self.mymemory_email:
                params["de"] = self.mymemory_email
            r = requests.get(MYMEMORY_URL, params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            t = (data.get("responseData") or {}).get("translatedText", "") or ""
            up = t.upper()
            if (not t.strip() or "MYMEMORY WARNING" in up or "QUERY LENGTH" in up
                    or "INVALID" in up or int(data.get("responseStatus", 200) or 200) >= 400):
                raise RuntimeError(f"MyMemory رد غير صالح: {t[:80]}")
            outs.append(t.strip())
            time.sleep(0.3)
        return " ".join(outs)

    def _google(self, text: str) -> str:
        if GoogleTranslator is None:
            raise RuntimeError("deep-translator غير مثبّت")
        last = None
        for attempt in range(3):
            try:
                out = GoogleTranslator(source="auto", target=self.target).translate(text)
                if out and out.strip():
                    return out.strip()
            except Exception as exc:
                last = exc
                self._sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"Google: {last}")

    def _libretranslate(self, text: str) -> str:
        payload = {"q": text, "source": self.source, "target": self.target, "format": "text"}
        if self.lt_key:
            payload["api_key"] = self.lt_key
        r = requests.post(f"{self.lt_url}/translate", json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        for k in ("translatedText", "translation", "result"):
            if isinstance(data, dict) and data.get(k):
                return data[k]
        raise RuntimeError("LibreTranslate: رد غير مفهوم")

    # ---------------------------------------------------------------- #
    # الواجهة العامة
    # ---------------------------------------------------------------- #
    def _chain(self, batched: bool):
        """يُرجع قائمة (name, fn) بالترتيب. batched=True لخدمات تدعم الدفعات."""
        chain = []
        if self.gemini_key:
            chain.append(("gemini", self._gemini_batch if batched else
                          (lambda t: self._gemini_one(t, self._gemini_model() or ""))))
        if self.groq_key:
            chain.append(("groq", self._groq_batch if batched else
                          (lambda t: self._groq_batch([t])[0])))
        if requests is not None:
            chain.append(("google-free", self._google_free))
            chain.append(("mymemory", self._mymemory))
        if GoogleTranslator is not None:
            chain.append(("google", self._google))
        if self.lt_url:
            chain.append(("libretranslate", self._libretranslate))
        return chain

    def _is_dead(self, name: str) -> bool:
        return self._dead_until.get(name, 0.0) > time.time()

    def _mark_fail(self, name: str) -> None:
        self._fail_count[name] = self._fail_count.get(name, 0) + 1
        if self._fail_count[name] >= 2:
            self._dead_until[name] = time.time() + self.cooldown
            log.warning("إيقاف خدمة %s مؤقتاً لـ %.0f ثانية بعد فشلين.", name, self.cooldown)

    def translate_batch(self, texts: list[str]) -> list[str]:
        """ترجمة قائمة نصوص دفعة واحدة (مع تقسيمها لمجموعات مناسبة)."""
        if not texts:
            return []
        if self.force_mock:
            return [polish_text(_mock_translate(t), self.target) for t in texts]

        # نصوص فارغة تُترك كما هي، والمكتوبة تُترجم (ما لم تكن اختصاراً طبياً فتُترك إنجليزية)
        idx = [i for i, t in enumerate(texts) if str(t).strip() and not _is_acronym(str(t))]
        result = list(texts)
        if not idx:
            return result
        pending = [texts[i] for i in idx]

        # جرّب الخدمات بالترتيب على شكل مجموعات (لتفادي حدود الاستخدام)
        for name, fn in self._chain(batched=True):
            if self._is_dead(name):
                continue
            try:
                out = self._translate_with(fn, pending, batched=(name in ("gemini", "groq")))
                if out and len(out) == len(pending):
                    # حماية: بعض الخدمات تُعيد النص الإنجليزي كما هو (لم تُترجمه).
                    # إن كان أكثر من ثلث المقاطع لم يتغيّر ⇒ نعتبرها فاشلة ونجرّب الخدمة التالية.
                    def _norm(x):
                        return " ".join(str(x).split()).casefold()

                    def _untranslated(a, b):
                        """True إن أعادت الخدمة المقطع الإنجليزي كما هو (نسخ بلا ترجمة)."""
                        if not _norm(a):
                            return False
                        if _norm(a) == _norm(b):
                            return True
                        return arabic_ratio(str(a)) < 0.06 and bool(LATIN_RE.search(str(b)))

                    unchanged = sum(1 for a, b in zip(out, pending) if _untranslated(a, b))
                    if unchanged > max(1, len(pending) // 3):
                        self._record(f"{name}: لم يُترجم {unchanged}/{len(pending)} (نص إنجليزي كما هو)")
                        self._mark_fail(name)
                        continue
                    for k, i in enumerate(idx):
                        tr = out[k]
                        if _untranslated(tr, pending[k]):
                            tr = _mock_translate(pending[k])      # ترجمة تقريبية للمقطع المنسوخ
                        result[i] = _fix_terms_ar(polish_text(tr, self.target))
                    self._count(name, len(pending))
                    self._fail_count[name] = 0
                    return result
                self._record(f"{name}: عدد النتائج غير مطابق")
            except Exception as exc:
                self._record(f"{name}: {exc}")
                self._mark_fail(name)

        # فشل الكل -> تقريبي
        self._record("⚠️ لم تنجح أي خدمة ترجمة حقيقية - ترجمة تقريبية")
        for i in idx:
            result[i] = polish_text(_mock_translate(texts[i]), self.target)
        return result

    def _translate_with(self, fn, texts: list[str], batched: bool) -> list[str]:
        """يُنفّذ الترجمة، بتقسيم دفعات لطيفة للخدمات التي تدعم الدفعات."""
        if batched:
            chunk = int(os.getenv("BATCH_SIZE", "12"))
            out: list[str] = []
            for s in range(0, len(texts), chunk):
                part = texts[s:s + chunk]
                out.extend(fn(part))
                if s + chunk < len(texts):
                    time.sleep(0.8)                 # تهدئة بين الدفعات
            return out
        # خدمات بند-بند: استخدم توازي محدود
        return self._run_parallel(fn, texts)

    def _run_parallel(self, fn, texts: list[str]) -> list[str]:
        workers = int(os.getenv("PARALLEL_WORKERS", "4"))
        if workers <= 1 or len(texts) <= 1:
            return [fn(t) for t in texts]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(fn, texts))

    def translate(self, text: str) -> str:
        return self.translate_batch([text])[0]

    def translate_all(self, texts, prog=None) -> list[str]:
        texts = list(texts)
        out: list[str] = []
        total = len(texts)
        # دفعات مناسبة للعرض والتقدّم
        step = int(os.getenv("BATCH_SIZE", "12"))
        for s in range(0, total, step):
            part = texts[s:s + step]
            out.extend(self.translate_batch(part))
            if prog:
                try:
                    prog(min(s + step, total), total)
                except TypeError:
                    try:
                        prog(min(s + step, total))
                    except Exception:
                        pass
                except Exception:
                    pass
        return out

    # ---------------------------------------------------------------- #
    def check(self) -> dict:
        res: dict[str, tuple[bool, str]] = {}
        probe = "Proton pump inhibitors are used to treat gastric acid."
        if self.gemini_key:
            m = self._gemini_model()
            if not m:
                res["gemini"] = (False, "لا يوجد موديل متاح (راجع المفتاح)")
            else:
                try:
                    self._gemini_one("chest pain", m)
                    res["gemini"] = (True, f"يعمل (model={m})")
                except Exception as exc:
                    res["gemini"] = (False, str(exc)[:180])
        if self.groq_key:
            try:
                self._groq_batch(["chest pain"])
                res["groq"] = (True, "يعمل")
            except Exception as exc:
                res["groq"] = (False, str(exc)[:180])
        if requests is not None:
            try:
                self._google_free(probe)
                res["google-free"] = (True, "يعمل (بلا مفتاح)")
            except Exception as exc:
                res["google-free"] = (False, str(exc)[:180])
        if requests is not None:
            try:
                self._mymemory(probe)
                res["mymemory"] = (True, "يعمل (بلا مفتاح)")
            except Exception as exc:
                res["mymemory"] = (False, str(exc)[:180])
        if GoogleTranslator is not None:
            try:
                self._google(probe)
                res["google"] = (True, "يعمل")
            except Exception as exc:
                res["google"] = (False, str(exc)[:180])
        if self.lt_url:
            try:
                self._libretranslate(probe)
                res["libretranslate"] = (True, "يعمل")
            except Exception as exc:
                res["libretranslate"] = (False, str(exc)[:180])
        return res

    def startup_probe(self) -> dict:
        results = self.check()
        for name, (ok, msg) in results.items():
            log.info("خدمة %-14s : %s %s", name, "✅" if ok else "❌", msg)
        working = [n for n, (ok, _) in results.items() if ok]
        if working:
            log.info("الخدمات العاملة عند البدء: %s", ", ".join(working))
        else:
            log.error("⚠️ لا توجد خدمة ترجمة حقيقية! سيُستخدم المترجم التقريبي. "
                      "اضبط GEMINI_API_KEY أو GROQ_API_KEY.")
        return results

    def status_text(self) -> str:
        lines = [f"الاتجاه: {self.source} -> {self.target}",
                 f"الخدمات المجهّزة: {', '.join(self._backends())}"]
        if self.gemini_model_env:
            lines.append(f"موديل Gemini (من GEMINI_MODEL): {self.gemini_model_env}")
        elif self._gemini_models:
            lines.append(f"موديلات Gemini المتاحة: {', '.join(self._gemini_models[:4])}")
        if self.groq_models_env:
            lines.append(f"موديلات Groq (من GROQ_MODELS): {len(self.groq_models_env)} موديل")
        elif self._groq_models:
            lines.append(f"موديلات Groq المتاحة: {', '.join(self._groq_models[:4])}")
        if self.service_counts:
            used = ", ".join(f"{k}({v})" for k, v in self.service_counts.items())
            lines.append(f"استُخدمت فعلاً: {used}")
        if self.last_error:
            lines.append(f"آخر ملاحظة: {self.last_error[:200]}")
        return "\n".join(lines)
