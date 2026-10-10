#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translator.py  -  مُترجِم متعدّد الخدمات (نسخة الإنتاج v3)
=========================================================
المبدأ الأساسي: **لا نستخدم المترجم الوهمي إلا كخيار أخير مُعلن**.
نحاول أولاً خدمات ترجمة حقيقية مجانية تعمل من داخل Railway بلا مفاتيح،
ثم خدمات المفاتيح إن توفّرت، ثم الوهمي.

ترتيب الخدمات (أول خدمة تنجح هي المستخدمة):
  1. Gemini         (GEMINI_API_KEY + GEMINI_MODEL اختياري)
                     - الموديل من GEMINI_MODEL إن وُجد، وإلا اكتشاف تلقائي
  2. Groq           (GROQ_API_KEY)        - اكتشاف تلقائي للموديل المتاح
  3. MyMemory       (بلا مفتاح)  ✅ حقيقية ومجانية وتعمل بدون API key
  4. Google         (deep-translator، بلا مفتاح) - تدوير + إعادة محاولة
  5. LibreTranslate (LIBRETRANSLATE_URL) - اختياري
  6. Mock           (قاموس محلي) - خيار أخير فقط، ورسالته "تقريبية"

الواجهة العامة (يستخدمها main.py و translate_pdf.py):

    t = Translator()
    t.translate(text)                 -> str
    t.translate_all(texts, prog)      -> [str, ...]
    t.check()                         -> {backend: (ok, message)}
    t.startup_probe()                 -> يطبع في اللوج الخدمات العاملة
    t.used_real_service               -> bool (هل استُخدمت خدمة حقيقية؟)
    t.all_errors() / t.last_error     -> سجلّ الأخطاء
    t._backends()                     -> [str, ...]
"""
from __future__ import annotations

import logging
import os
import re
import time

try:
    import requests
except Exception:                                    # pragma: no cover
    requests = None

try:
    from deep_translator import GoogleTranslator
except Exception:                                    # pragma: no cover
    GoogleTranslator = None

log = logging.getLogger("translator")

# حالة الخدمات المعطّلة مؤقتاً (مشتركة بين كل نسخ Translator)
_DEAD_UNTIL: dict[str, float] = {}
_FAIL_COUNT: dict[str, int] = {}

# ==========================================================================
# نقاط النهاية
# ==========================================================================
GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_GEN_URL = ("https://generativelanguage.googleapis.com/v1beta/"
                  "models/{model}:generateContent")
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
# نقطتان عامّتان لخدمة Google Translate المجانية (بلا مفتاح) - جودة عالية للمصطلحات
GOOGLE_FREE_HOSTS = [
    "https://translate.googleapis.com/translate_a/single",
    "https://clients5.google.com/translate_a/t",
]

LANG_NAMES = {
    "ar": "Arabic", "en": "English", "fr": "French", "de": "German",
    "tr": "Turkish", "es": "Spanish", "ru": "Russian", "fa": "Persian",
    "ur": "Urdu", "it": "Italian", "nl": "Dutch",
}

# ==========================================================================
# قاموس احتياطي (إنجليزي -> عربي) - يُستخدم فقط إذا فشلت كل الخدمات الحقيقية
# ملاحظة مهمة: الكلمة غير المعروفة تُترك **بالإنجليزية كما هي** ولا تُنقحر
# صوتياً، حتى لا يخرج نص مشوّه غير مفهوم.
# ==========================================================================
EN_AR: dict[str, str] = {
    # جمل ومصطلحات مركّبة (تُطابق أولاً، الأطول أولاً)
    "angina pectoris": "الذبحة الصدرية",
    "organic nitrates": "النترات العضوية",
    "calcium channel blocking agents": "حاصرات قنوات الكالسيوم",
    "adrenergic blocking agents": "العوامل الحاصرة الأدرينية",
    "myocardial contractility": "الانقباضية العضلية القلبية",
    "blood flow": "تدفق الدم", "blood pressure": "ضغط الدم",
    "heart rate": "معدل ضربات القلب", "coronary arteries": "الشرايين التاجية",
    "smooth muscle": "العضلة الملساء", "blood vessel walls": "جدران الأوعية الدموية",
    "blood vessels": "الأوعية الدموية", "systemic circulation": "الدورة الدموية الجهازية",
    "adverse effects": "الآثار الجانبية", "onset of action": "بداية المفعول",
    "routes of drug administration": "طرق إعطاء الدواء",
    "northern technical university": "الجامعة التقنية الشمالية",
    "al-dour college of polytechnique": "كلية الدَّور التقنية",
    "department of pharmacy techniques": "قسم تقنيات الصيدلة",
    "clinical syndrome": "متلازمة سريرية",
    "atherosclerotic plaque": "اللويحة التصلبية العصيدية",
    "oxygen demand": "الطلب على الأكسجين", "cardiac workload": "العبء القلبي",
    "venous pressure": "الضغط الوريدي", "chest pain": "ألم الصدر",
    "peripheral vascular supply": "الإمداد الوعائي المحيطي",
    "systolic blood pressure": "ضغط الدم الانقباضي",
    # كلمات مفردة - طب وقلب
    "angina": "الذبحة", "pectoris": "الصدرية", "heart": "القلب", "blood": "الدم",
    "pressure": "الضغط", "drug": "دواء", "drugs": "أدوية", "nitrates": "النترات",
    "nitrate": "النترات", "organic": "عضوي", "adrenergic": "أدريني",
    "blocking": "حاصر", "agents": "عوامل", "calcium": "الكالسيوم",
    "channel": "قناة", "muscle": "العضلة", "vessel": "الوعاء",
    "vessels": "الأوعية", "walls": "الجدران", "flow": "التدفق",
    "oxygen": "الأكسجين", "demand": "الطلب", "coronary": "التاجي",
    "arteries": "الشرايين", "artery": "الشريان", "veins": "الأوردة",
    "vein": "الوريد", "venous": "وريدي", "cardiac": "القلبي",
    "workload": "العبء", "vasodilatation": "توسع الأوعية",
    "vasodilation": "توسع الأوعية", "dilation": "توسيع", "dilate": "يوسع",
    "ischemic": "الإقفاري", "myocardium": "عضلة القلب", "systolic": "الانقباضي",
    "relieve": "يخفف", "relieves": "يخفف", "reduce": "يقلل", "reduces": "يقلل",
    "increase": "يزيد", "increases": "يزيد", "supply": "الإمداد",
    "mechanisms": "آليات", "mechanism": "آلية", "arterioles": "الشرينات",
    "peripheral": "المحيطي", "vascular": "الوعائي", "volume": "الحجم",
    "treatment": "العلاج", "therapy": "العلاج", "patient": "المريض",
    "patients": "المرضى", "dose": "الجرعة", "administration": "الإعطاء",
    "routes": "الطرق", "route": "الطريق", "oral": "فموي", "injection": "الحقن",
    "absorption": "الامتصاص", "metabolism": "الأيض", "skin": "الجلد",
    "tissue": "النسيج", "pain": "الألم", "chest": "الصدر",
    "nitroglycerin": "النيتروجليسرين", "sublingual": "تحت اللسان",
    "tablets": "الأقراص", "chewable": "قابلة للمضغ", "attack": "نوبة",
    "acute": "حاد", "clinical": "سريري", "syndrome": "متلازمة",
    "characterized": "يتميز", "plaque": "لويحة", "several": "عدة",
    "cells": "الخلايا", "cell": "الخلية", "produce": "ينتج", "produces": "ينتج",
    "relax": "يرخي", "directly": "مباشرة", "approximately": "تقريباً",
    "occurs": "يحدث", "peak": "الذروة", "usual": "المعتاد", "usually": "عادة",
    "management": "الإدارة", "prophylaxis": "الوقاية", "frequency": "تكرار",
    "severity": "شدة", "exercise": "التمرين", "on": "على", "in": "في",
    "of": "من", "to": "إلى", "and": "و", "or": "أو", "the": "", "a": "",
    "an": "", "is": "هو", "are": "هي", "was": "كان", "by": "بواسطة",
    "with": "مع", "for": "لـ", "from": "من", "this": "هذا", "that": "ذلك",
    "which": "التي", "when": "عندما", "which": "التي", "can": "يمكن",
    "may": "قد", "not": "لا", "also": "أيضاً", "more": "أكثر", "less": "أقل",
    "lower": "أخفض", "lowers": "يخفض", "due": "بسبب", "afterload": "الحمل اللاحق",
}

_TOKEN_SPLIT = re.compile(r"(\s+)")
_STRIP = "،.,؛;:!؟?\"'()[]{}«»…"
_LATIN = re.compile(r"[A-Za-z]")
_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def _lookup_en(word: str) -> str:
    """ترجمة كلمة إنجليزية من القاموس. إن لم تُعرف تُترك كما هي (بلا نقحرة)."""
    low = word.lower()
    if low in EN_AR:
        return EN_AR[low]
    for suf, repl in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ed", "")):
        if low.endswith(suf) and len(low) > len(suf) + 1 and low[:-len(suf)] + repl in EN_AR:
            return EN_AR[low[:-len(suf)] + repl]
    # مهم: لا نقحرة صوتية - نُبقي الكلمة الإنجليزية كما هي
    return word


def _mock_line_en2ar(line: str) -> str:
    text = line
    for phrase in sorted((k for k in EN_AR if " " in k), key=len, reverse=True):
        if phrase in text.lower():
            text = re.sub(re.escape(phrase), EN_AR[phrase], text, flags=re.IGNORECASE)
    out = []
    for tok in _TOKEN_SPLIT.split(text):
        if _LATIN.search(tok):
            core = tok.strip(_STRIP)
            pre = tok[: len(tok) - len(tok.lstrip(_STRIP))]
            suf = tok[len(tok.rstrip(_STRIP)):]
            out.append(pre + _lookup_en(core) + suf)
        else:
            out.append(tok)
    return "".join(out)


def _chunk_bytes(text: str, max_bytes: int = 450) -> list[str]:
    """تقسيم النص إلى أجزاء لا يتجاوز الواحد منها max_bytes عند حدود الكلمات."""
    words = text.replace("\r", "").split(" ")
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
    return chunks or [text]


# ==========================================================================
# تصحيحات المصطلحات + فصل العربي/اللاتيني (تُطبَّق على الإخراج النهائي)
# ==========================================================================
TERM_FIXES = {
    "نقطة في البوصة": "مثبطات مضخة البروتون",
    "هضم السرمرض": "حرقة المعدة",
    "مثبطات مضخة البروتونات": "مثبطات مضخة البروتون",
    "عسر الهضم الحموضي": "عسر الهضم الحمضي",
    "لوحة تصلب الشرايين": "لويحة تصلب الشرايين",
    "التحميل التالي": "الحمل اللاحق",
    "الأحماض المعدية": "حمض المعدة",
    # تصحيحات مصطلحات طبية شائعة الخطأ في الترجمة الآلية
    "المخدرات": "الأدوية",
    "مخدرات": "أدوية",
    "الجهاز الهضمي اضطرابات": "اضطرابات الجهاز الهضمي",
    "للجسم الهضم": "للهضم",
    "الدموية الدم": "الدم",
    "الانقباضي الضغط": "ضغط الدم الانقباضي",
    "غير المؤكسج يعود": "يعود غير المؤكسج",
    "القلب عضلة": "عضلة القلب",
    "الدم ضغط": "ضغط الدم",
    "المعدة حمض": "حمض المعدة",
    "الدم تدفق": "تدفق الدم",
    "الدم إمداد": "إمداد الدم",
}
# تصحيحات ترتيب الكلمات الشائعة (نتيجة الترجمة الحرفية)
ORDER_FIXES = {
    "الدم إمداد": "إمداد الدم",
    "الانقباضي الضغط": "ضغط الدم الانقباضي",
    "الدم تدفق": "تدفق الدم",
    "المعدة حمض": "حمض المعدة",
}
_AR_LAT = re.compile(r"([\u0600-\u06FF])([A-Za-z])")
_LAT_AR = re.compile(r"([A-Za-z])([\u0600-\u06FF])")


def polish_text(text: str, target: str = "ar") -> str:
    """تحسين الإخراج: تصحيح مصطلحات شائعة + فصل الحروف العربية عن اللاتينية
    حتى لا تلتصق مثل (بالفعلHCl) فتظهر (بالفعل HCl)."""
    if not text:
        return text
    for bad, good in TERM_FIXES.items():
        if bad in text:
            text = text.replace(bad, good)
    for bad, good in ORDER_FIXES.items():
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
# المُترجِم
# ==========================================================================
class Translator:
    def __init__(self) -> None:
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        # اسم موديل Gemini: من متغيّر البيئة GEMINI_MODEL إن وُجد، وإلا اكتشاف تلقائي
        self.gemini_model_env = os.getenv("GEMINI_MODEL", "").strip()
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()
        self.lt_url = os.getenv("LIBRETRANSLATE_URL", "").strip().rstrip("/")
        self.lt_key = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
        self.mymemory_email = os.getenv("MYMEMORY_EMAIL", "").strip()
        self.source = (os.getenv("SOURCE_LANG", "auto").strip() or "auto")
        self.target = (os.getenv("TARGET_LANG", "ar").strip() or "ar")
        self.timeout = float(os.getenv("HTTP_TIMEOUT", "12"))

        self._errors: list[str] = []
        self.last_error: str = ""
        self._gemini_model: str | None = None
        self._groq_model: str | None = None

        # هل مُنع الاستخدام الحقيقي صراحةً؟ (للاختبار فقط)
        self.force_mock = os.getenv("FORCE_MOCK", "").strip() in ("1", "true", "yes")

        # إحصاءات الجودة
        self.used_real_service = False
        self.mock_count = 0          # كم مقطعاً تُرجم بالمترجم التقريبي
        self.service_counts: dict[str, int] = {}
        # الخدمات التي فشلت مؤخراً: نوقف محاولتها مؤقتاً حتى لا تُبطئ كل فقرة.
        # الحالة مشتركة بين كل النسخ (كل ملف PDF ينشئ Translator جديداً،
        # فلو كانت لكل نسخة لأعاد البوت تجربة خدمة معطّلة مع كل ملف).
        self._dead_until = _DEAD_UNTIL
        self._fail_count = _FAIL_COUNT
        self.cooldown = float(os.getenv("SERVICE_COOLDOWN", "300"))

    # ------------------------------------------------------------------ #
    # معلومات
    # ------------------------------------------------------------------ #
    def _backends(self) -> list[str]:
        names: list[str] = []
        if self.gemini_key:
            names.append("gemini")
        if self.groq_key:
            names.append("groq")
        names.append("google-free")                    # بلا مفتاح - جودة عالية
        names.append("mymemory")                       # بلا مفتاح - متاح دائماً
        if GoogleTranslator is not None:
            names.append("google")
        if self.lt_url:
            names.append("libretranslate")
        return names

    def all_errors(self) -> list[str]:
        return list(self._errors)

    def _redact(self, msg: str) -> str:
        """يخفي مفاتيح API من أي نص قبل تسجيله أو عرضه."""
        for k in (self.gemini_key, self.groq_key, self.lt_key):
            if k:
                msg = msg.replace(k, "***")
        return msg

    def _record(self, msg: str) -> None:
        msg = self._redact(msg)
        self.last_error = msg
        self._errors.append(msg)
        del self._errors[:-50]                 # لا نحتفظ بأكثر من 50 خطأ
        log.warning(msg)

    def _by_chunks(self, fn, text: str, limit: int) -> str:
        """يقسّم النص الطويل ويترجم كل جزء على حدة (لخدمات لها حد للطول)."""
        if len(text.encode("utf-8")) <= limit:
            return fn(text)
        return " ".join(fn(c) for c in _chunk_bytes(text, limit))

    def _count(self, name: str) -> None:
        self.service_counts[name] = self.service_counts.get(name, 0) + 1
        self.used_real_service = True

    # ------------------------------------------------------------------ #
    # اكتشاف موديلات Gemini / Groq
    # ------------------------------------------------------------------ #
    def _discover_gemini_model(self) -> str | None:
        if self._gemini_model:
            return self._gemini_model
        # 1) إن حُدّد GEMINI_MODEL في البيئة نستخدمه مباشرة (بلا اكتشاف)
        if self.gemini_model_env:
            self._gemini_model = self.gemini_model_env
            log.info("Gemini: استخدام الموديل من GEMINI_MODEL = %s", self._gemini_model)
            return self._gemini_model
        # 2) وإلا نكتشف تلقائياً أفضل موديل متاح للحساب
        if requests is None or not self.gemini_key:
            return None
        try:
            # المفتاح في الهيدر (لا في الرابط) حتى لا يظهر في رسائل الأخطاء واللوج
            r = requests.get(GEMINI_LIST_URL,
                             headers={"x-goog-api-key": self.gemini_key},
                             timeout=self.timeout)
            r.raise_for_status()
            models = r.json().get("models", [])
            # نستبعد موديلات الصوت/الصور/التضمين لأنها لا تصلح للترجمة
            bad = ("tts", "image", "audio", "live", "embed", "aqa",
                   "robotics", "computer-use", "veo", "imagen")
            usable = [m for m in models
                      if "generateContent" in m.get("supportedGenerationMethods", [])
                      and not any(b in m.get("name", "").lower() for b in bad)]
            if not usable:
                self._record("Gemini: لا يوجد موديل يدعم generateContent")
                return None
            usable.sort(key=lambda m: (0 if "flash" in m.get("name", "") else
                                       1 if "pro" in m.get("name", "") else 2))
            self._gemini_model = usable[0]["name"].split("/")[-1]
            log.info("Gemini: تم اختيار الموديل %s", self._gemini_model)
            return self._gemini_model
        except Exception as exc:
            self._record(f"Gemini discovery فشل: {exc}")
            return None

    def _discover_groq_model(self) -> str | None:
        if self._groq_model:
            return self._groq_model
        if requests is None or not self.groq_key:
            return None
        try:
            r = requests.get(GROQ_MODELS_URL,
                             headers={"Authorization": f"Bearer {self.groq_key}"},
                             timeout=self.timeout)
            r.raise_for_status()
            ids = [m.get("id", "") for m in r.json().get("data", [])]
            if not ids:
                self._record("Groq: لا توجد موديلات متاحة")
                return None
            def rank(mid: str) -> int:
                for i, key in enumerate(("llama-3.3", "llama-3.1", "llama", "mixtral", "gemma")):
                    if key in mid:
                        return i
                return 9
            ids.sort(key=rank)
            self._groq_model = ids[0]
            log.info("Groq: تم اختيار الموديل %s", self._groq_model)
            return self._groq_model
        except Exception as exc:
            self._record(f"Groq discovery فشل: {exc}")
            return None

    # ------------------------------------------------------------------ #
    # الخدمات
    # ------------------------------------------------------------------ #
    def _gemini(self, text: str) -> str:
        model = self._discover_gemini_model()
        if not model:
            raise RuntimeError("Gemini: لا يوجد موديل متاح "
                               "(اضبط GEMINI_MODEL أو GEMINI_API_KEY)")
        prompt = (f"Translate the following medical/technical text into "
                  f"{LANG_NAMES.get(self.target, self.target)}. "
                  f"Return ONLY the translation, without notes or quotes.\n\n{text}")
        # الرابط بالضبط: .../v1beta/models/{model}:generateContent?key=GEMINI_API_KEY
        url = GEMINI_GEN_URL.format(model=model)
        try:
            r = requests.post(
                url,
                headers={"x-goog-api-key": self.gemini_key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.0}},
                timeout=self.timeout)
        except Exception as exc:
            log.error("Gemini: فشل الاتصال بالرابط %s -> %s", url, self._redact(str(exc)))
            raise RuntimeError(f"Gemini اتصال فشل: {self._redact(str(exc))}") from exc

        # طباعة واضحة للخطأ: رمز الحالة HTTP + نص رد Gemini
        if r.status_code != 200:
            body = (r.text or "")[:500]
            log.error("Gemini: HTTP %s من %s\nرد Gemini: %s",
                      r.status_code, url, body)
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {body}")

        try:
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as exc:
            body = (r.text or "")[:500]
            log.error("Gemini: رد غير متوقّع من %s -> %s\nالرد: %s", url, exc, body)
            raise RuntimeError(f"Gemini رد غير متوقّع: {body}") from exc

    def _groq(self, text: str) -> str:
        model = self._discover_groq_model()
        if not model:
            raise RuntimeError("Groq: لا يوجد موديل متاح")
        r = requests.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {self.groq_key}",
                     "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.0,
                  "messages": [
                      {"role": "system",
                       "content": (f"You are a professional translator. Translate the "
                                   f"user's text into {LANG_NAMES.get(self.target, self.target)}. "
                                   f"Return ONLY the translation.")},
                      {"role": "user", "content": text}]},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def _mymemory(self, text: str) -> str:
        """خدمة MyMemory: حقيقية، مجانية، بلا مفتاح. حد الطلب 500 بايت."""
        parts = _chunk_bytes(text, 450)
        outs: list[str] = []
        for part in parts:
            params = {"q": part, "langpair": f"{self.source if self.source != 'auto' else 'en'}|{self.target}"}
            if self.mymemory_email:
                params["de"] = self.mymemory_email
            r = requests.get(MYMEMORY_URL, params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            t = (data.get("responseData") or {}).get("translatedText", "") or ""
            up = t.upper()
            if (not t.strip() or "MYMEMORY WARNING" in up
                    or "QUERY LENGTH LIMIT" in up or "INVALID" in up
                    or int(data.get("responseStatus", 200) or 200) >= 400):
                raise RuntimeError(f"MyMemory رد غير صالح: {t[:80]}")
            outs.append(t.strip())
            time.sleep(0.35)                            # نحترم حدود الخدمة
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
                time.sleep(1.2 * (attempt + 1))         # تهدئة عند TooManyRequests
        raise RuntimeError(f"Google فشل: {last}")

    def _google_free(self, text: str) -> str:
        """Google Translate عبر نقطتين عامّتين مجانيتين (بلا مفتاح) بجودة عالية.
        نقطة googleapis أساسية ونقطة clients5 بديلة، مع إعادة محاولة وتدوير."""
        if requests is None:
            raise RuntimeError("requests غير مثبّت")
        sl = "auto" if self.source in ("", "auto") else self.source
        last = None
        for attempt in range(2):
            for host in GOOGLE_FREE_HOSTS:
                try:
                    if "clients5" in host:
                        r = requests.get(
                            host,
                            params={"client": "dict-chrome-ex", "sl": sl,
                                    "tl": self.target, "q": text},
                            headers={"User-Agent": "Mozilla/5.0"},
                            timeout=self.timeout)
                        r.raise_for_status()
                        data = r.json()
                        if isinstance(data, list) and data and isinstance(data[0], str):
                            out = data[0]
                        elif isinstance(data, list) and data and isinstance(data[0], list):
                            out = "".join(seg[0] for seg in data[0] if seg)
                        else:
                            out = ""
                    else:
                        r = requests.get(
                            host,
                            params={"client": "gtx", "sl": sl,
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
            time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"Google-free فشل: {last}")

    def _polish(self, text: str) -> str:
        """تحسين الإخراج: تصحيح مصطلحات شائعة + فصل العربي عن اللاتيني."""
        return polish_text(text, self.target)

    def _libretranslate(self, text: str) -> str:
        if not self.lt_url:
            raise RuntimeError("LIBRETRANSLATE_URL غير مضبوط")
        payload = {"q": text, "source": self.source, "target": self.target, "format": "text"}
        if self.lt_key:
            payload["api_key"] = self.lt_key
        r = requests.post(f"{self.lt_url}/translate", json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        for k in ("translatedText", "translation", "result"):
            if isinstance(data, dict) and data.get(k):
                return data[k]
        raise RuntimeError(f"رد LibreTranslate غير مفهوم: {str(data)[:120]}")

    def _mock_translate(self, text: str) -> str:
        return "\n".join(_mock_line_en2ar(ln) for ln in text.split("\n"))

    # ------------------------------------------------------------------ #
    # الواجهة العامة
    # ------------------------------------------------------------------ #
    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return text
        if self.force_mock:
            self.mock_count += 1
            return self._polish(self._mock_translate(text))

        chain = []
        if self.gemini_key:
            chain.append(("gemini", self._gemini))
        if self.groq_key:
            chain.append(("groq", self._groq))
        if requests is not None:
            # google-free يرسل النص في الرابط (GET) فالنص الطويل يفشل -> نقسّمه
            chain.append(("google-free",
                          lambda t: self._by_chunks(self._google_free, t, 1500)))
            chain.append(("mymemory", self._mymemory))
        if GoogleTranslator is not None:
            # deep-translator يرفض أكثر من 5000 حرف
            chain.append(("google",
                          lambda t: self._by_chunks(self._google, t, 4000)))
        if self.lt_url:
            chain.append(("libretranslate", self._libretranslate))

        now = time.time()
        tried_any = False
        for name, fn in chain:
            if self._dead_until.get(name, 0.0) > now:
                continue                      # خدمة فشلت مؤخراً -> تجاهلها مؤقتاً
            tried_any = True
            try:
                out = fn(text)
                if out and out.strip():
                    self._count(name)
                    self._fail_count[name] = 0
                    return self._polish(out)
                self._record(f"{name}: رد فارغ")
            except Exception as exc:
                self._record(f"{name}: {exc}")
                self._fail_count[name] = self._fail_count.get(name, 0) + 1
                # إيقاف الخدمة مؤقتاً بعد فشلين متتاليين
                if self._fail_count[name] >= 2:
                    self._dead_until[name] = now + self.cooldown
                    log.warning("تم إيقاف خدمة %s مؤقتاً لمدّة %.0f ثانية بعد فشلين.",
                                name, self.cooldown)

        # لا خدمة حقيقية نجحت
        self.mock_count += 1
        self._record("⚠️ لم تنجح أي خدمة ترجمة حقيقية - استخدام المترجم التقريبي")
        return self._polish(self._mock_translate(text))

    def translate_all(self, texts, prog=None) -> list[str]:
        out: list[str] = []
        total = len(texts)
        for i, t in enumerate(texts):
            out.append(self.translate(t))
            if prog:
                try:
                    prog(i + 1, total)
                except TypeError:
                    try:
                        prog(i + 1)
                    except Exception:
                        pass
                except Exception:
                    pass
        return out

    def check(self) -> dict:
        """فحص سريع لكل خدمة -> {backend: (ok, message)}"""
        result: dict[str, tuple[bool, str]] = {}
        probe = "chest pain"
        if self.gemini_key:
            try:
                self._gemini(probe)
                result["gemini"] = (True, f"يعمل (model={self._gemini_model})")
            except Exception as exc:
                result["gemini"] = (False, self._redact(str(exc))[:160])
        if self.groq_key:
            try:
                self._groq(probe)
                result["groq"] = (True, f"يعمل (model={self._groq_model})")
            except Exception as exc:
                result["groq"] = (False, self._redact(str(exc))[:160])
        if requests is not None:
            try:
                self._google_free(probe)
                result["google-free"] = (True, "يعمل (بلا مفتاح)")
            except Exception as exc:
                result["google-free"] = (False, self._redact(str(exc))[:160])
        if requests is not None:
            try:
                self._mymemory(probe)
                result["mymemory"] = (True, "يعمل (بلا مفتاح)")
            except Exception as exc:
                result["mymemory"] = (False, self._redact(str(exc))[:160])
        if GoogleTranslator is not None:
            try:
                self._google(probe)
                result["google"] = (True, "يعمل")
            except Exception as exc:
                result["google"] = (False, self._redact(str(exc))[:160])
        if self.lt_url:
            try:
                self._libretranslate(probe)
                result["libretranslate"] = (True, "يعمل")
            except Exception as exc:
                result["libretranslate"] = (False, self._redact(str(exc))[:160])
        return result

    def startup_probe(self) -> dict:
        """يُستدعى عند بدء البوت: يفحص ويطبع الخدمات العاملة في اللوج."""
        results = self.check()
        working = [n for n, (ok, _m) in results.items() if ok]
        for name, (ok, msg) in results.items():
            log.info("خدمة %-14s : %s %s", name, "✅" if ok else "❌", msg)
        if working:
            log.info("خدمات الترجمة العاملة عند البدء: %s", ", ".join(working))
        else:
            log.error("⚠️ لا توجد أي خدمة ترجمة حقيقية تعمل! "
                      "البوت سيستخدم المترجم التقريبي. اضبط GEMINI_API_KEY أو GROQ_API_KEY.")
        return results

    def status_text(self) -> str:
        lines = [f"الخدمات المجهّزة: {', '.join(self._backends())}",
                 f"الاتجاه: {self.source} -> {self.target}"]
        if self.service_counts:
            used = ", ".join(f"{k}({v})" for k, v in self.service_counts.items())
            lines.append(f"استُخدمت فعلاً: {used}")
        if self.gemini_model_env:
            lines.append(f"موديل Gemini (من GEMINI_MODEL): {self.gemini_model_env}")
        elif self._gemini_model:
            lines.append(f"موديل Gemini (اكتشاف تلقائي): {self._gemini_model}")
        if self._groq_model:
            lines.append(f"موديل Groq: {self._groq_model}")
        if self.last_error:
            lines.append(f"آخر خطأ: {self.last_error[:200]}")
        return "\n".join(lines)
