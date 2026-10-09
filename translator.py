#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translator.py
=============
مُترجِم متعدّد الخدمات مع **اكتشاف تلقائي للموديلات المتاحة**.

تُجرَّب الخدمات بهذا الترتيب، وأول خدمة تنجح هي التي تُستخدم:

  1. Gemini         (متغيّر البيئة GEMINI_API_KEY)   - يكتشف الموديل تلقائياً
  2. Groq           (متغيّر البيئة GROQ_API_KEY)     - يكتشف الموديل تلقائياً
  3. Google         (deep-translator، بلا مفتاح)     - قد يرفض بسبب الضغط
  4. LibreTranslate (متغيّر البيئة LIBRETRANSLATE_URL) - اختياري (fallback)
  5. Mock           (قاموس محلي)                     - عند غياب كل المفاتيح

الواجهة العامة التي يستخدمها main.py:

    t = Translator()
    t.check()                      -> {backend: (ok, message)}
    t.translate_all(texts, prog)   -> [str, ...]
    t.all_errors()                 -> [str, ...]
    t.last_error                   -> str
    t._backends()                  -> [str, ...]
"""
from __future__ import annotations

import logging
import os
import re

try:
    import requests
except Exception:                                    # pragma: no cover
    requests = None

try:
    from deep_translator import GoogleTranslator
except Exception:                                    # pragma: no cover
    GoogleTranslator = None

log = logging.getLogger("translator")

GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_GEN_URL = ("https://generativelanguage.googleapis.com/v1beta/"
                  "{model}:generateContent")
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

LANG_NAMES = {
    "ar": "Arabic", "en": "English", "fr": "French", "de": "German",
    "tr": "Turkish", "es": "Spanish", "ru": "Russian", "fa": "Persian",
    "ur": "Urdu", "it": "Italian", "nl": "Dutch",
}

# ==========================================================================
# قاموس وهمي (إنجليزي -> عربي) + تحويل حرفي احتياطي
# ==========================================================================
EN_AR: dict[str, str] = {
    "hello": "مرحباً", "welcome": "أهلاً", "world": "العالم", "this": "هذا",
    "that": "ذلك", "text": "نص", "sample": "تجريبي", "document": "مستند",
    "file": "ملف", "page": "صفحة", "translation": "ترجمة", "language": "لغة",
    "arabic": "العربية", "english": "الإنجليزية", "application": "تطبيق",
    "service": "خدمة", "system": "نظام", "data": "بيانات", "information": "معلومات",
    "software": "برنامج", "computer": "حاسوب", "network": "شبكة",
    "internet": "الإنترنت", "intelligence": "ذكاء", "artificial": "اصطناعي",
    "learning": "تعلّم", "machine": "الآلة", "analysis": "تحليل",
    "results": "نتائج", "project": "مشروع", "work": "عمل", "team": "فريق",
    "company": "شركة", "client": "عميل", "user": "مستخدم", "technology": "تقنية",
    "development": "تطوير", "report": "تقرير", "study": "دراسة", "research": "بحث",
    "product": "منتج", "market": "سوق", "price": "سعر", "quality": "جودة",
    "security": "أمان", "speed": "سرعة", "accuracy": "دقة", "performance": "أداء",
    "plan": "خطة", "goal": "هدف", "growth": "نمو", "value": "قيمة",
    "cost": "تكلفة", "time": "وقت", "day": "يوم", "month": "شهر", "year": "سنة",
    "today": "اليوم", "tomorrow": "غداً", "yesterday": "أمس", "large": "كبير",
    "small": "صغير", "new": "جديد", "old": "قديم", "fast": "سريع",
    "accurate": "دقيق", "important": "مهم", "best": "أفضل", "first": "الأول",
    "second": "الثاني", "all": "كل", "some": "بعض", "many": "كثير",
    "more": "أكثر", "less": "أقل", "and": "و", "or": "أو", "in": "في",
    "from": "من", "on": "على", "to": "إلى", "about": "حول", "with": "مع",
    "without": "بدون", "is": "هو", "are": "هي", "was": "كان", "can": "يمكن",
    "must": "يجب", "will": "سوف", "no": "لا", "yes": "نعم", "that": "أن",
    "as": "كما", "where": "حيث", "works": "يعمل", "provides": "يوفّر",
    "uses": "يستخدم", "using": "باستخدام", "via": "عبر", "between": "بين",
    "during": "خلال", "after": "بعد", "before": "قبل", "now": "الآن",
    "always": "دائماً", "example": "مثال", "very": "جداً", "also": "أيضاً",
    "only": "فقط", "until": "حتى", "but": "لكن", "because": "لأن", "if": "إذا",
    "when": "عندما", "heart": "القلب", "blood": "الدم", "pressure": "الضغط",
    "drug": "دواء", "drugs": "أدوية", "patient": "المريض", "patients": "المرضى",
    "treatment": "العلاج", "therapy": "العلاج", "dose": "الجرعة",
    "administration": "الإعطاء", "route": "الطريق", "routes": "الطرق",
    "oral": "فموي", "injection": "الحقن", "skin": "الجلد", "tissue": "النسيج",
    "absorption": "الامتصاص", "metabolism": "الأيض", "system": "الجهاز",
    "university": "الجامعة", "college": "الكلية", "department": "القسم",
    "laboratory": "المختبر", "lab": "المختبر", "pharmacy": "الصيدلة",
    "therapeutics": "العلاجيات", "applied": "التطبيقي", "technical": "التقني",
    "northern": "الشمالية", "angina": "الذبحة", "chest": "الصدر", "pain": "ألم",
    "nitrates": "النترات", "organic": "عضوي", "calcium": "الكالسيوم",
    "channel": "قناة", "blockers": "حاصرات", "muscle": "العضلة",
    "vessels": "الأوعية", "flow": "التدفق", "oxygen": "الأكسجين",
    "demand": "الطلب", "coronary": "التاجي", "arteries": "الشرايين",
    "reduce": "يقلل", "increase": "يزيد", "prevent": "يمنع", "used": "يُستخدم",
    "given": "يُعطى", "daily": "يومياً", "effects": "الآثار", "adverse": "جانبية",
    "management": "إدارة", "acute": "حاد", "attack": "نوبة", "rate": "معدل",
    "minutes": "دقائق", "hours": "ساعات", "sublingual": "تحت اللسان",
    "tablets": "الأقراص", "chewable": "قابلة للمضغ", "long": "طويل",
    "short": "قصير", "acting": "المفعول", "onset": "بداية", "action": "المفعول",
    "clinical": "سريري", "syndrome": "متلازمة", "plaque": "لويحة",
    "several": "عدة", "volume": "الحجم", "cells": "الخلايا", "produce": "ينتج",
    "relax": "يرخي", "dilate": "يوسع", "directly": "مباشرة",
    "approximately": "تقريباً", "lasts": "يستمر", "within": "خلال",
    "occurs": "يحدث", "peak": "الذروة", "rapidly": "بسرعة", "usually": "عادة",
    "same": "نفس", "once": "مرة", "twice": "مرتين", "week": "أسبوع",
    "the": "الـ", "a": "", "an": "", "of": "من", "for": "لـ", "which": "التي",
    "these": "هذه", "those": "تلك", "it": "هو", "its": "الخاص به",
    "they": "هم", "their": "الخاص بهم", "not": "لا", "such": "مثل",
    "may": "قد", "should": "ينبغي", "so": "لذا", "most": "معظم", "any": "أي",
    "each": "كل", "both": "كلا", "other": "آخر", "into": "إلى داخل",
    "through": "عبر", "than": "من", "then": "ثم", "there": "هناك",
    "here": "هنا", "up": "أعلى", "down": "أسفل", "out": "خارج", "over": "فوق",
    "under": "تحت", "again": "مرة أخرى", "well": "جيداً", "just": "فقط",
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
_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_LATIN = re.compile(r"[A-Za-z]")


def _romanize_ar(word: str) -> str:
    out = []
    for ch in word:
        if ch in _DIACRITICS:
            continue
        out.append(_AR2LAT.get(ch, ch if not _ARABIC.search(ch) else ""))
    return re.sub(r"(.)\1{2,}", r"\1\1", "".join(out))


def _arabize_en(word: str) -> str:
    out = []
    for ch in word:
        low = ch.lower()
        if low in _LAT2AR:
            out.append(_LAT2AR[low])
        elif ch.isdigit() or ch in "-/":
            out.append(ch)
    return "".join(out)


def _lookup_en(word: str) -> str:
    low = word.lower()
    if low in EN_AR:
        return EN_AR[low]
    for suf, repl in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ed", "")):
        if low.endswith(suf) and len(low) > len(suf) + 1 and low[:-len(suf)] + repl in EN_AR:
            return EN_AR[low[:-len(suf)] + repl]
    return _arabize_en(word)


def _lookup_ar(word: str) -> str:
    if word in EN_AR.values():
        return word
    for suf in ("ها", "هم", "هن", "كم", "نا", "ه", "ك", "ي"):
        if word.endswith(suf) and len(word) > len(suf) + 1:
            stem = word[:-len(suf)]
            if stem in EN_AR.values():
                return stem
    return _romanize_ar(word)


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


def _mock_line_ar2en(line: str) -> str:
    out = []
    for tok in _TOKEN_SPLIT.split(line):
        if _ARABIC.search(tok):
            core = tok.strip(_STRIP)
            pre = tok[: len(tok) - len(tok.lstrip(_STRIP))]
            suf = tok[len(tok.rstrip(_STRIP)):]
            out.append(pre + _lookup_ar(core) + suf)
        else:
            out.append(tok)
    return "".join(out)


# ==========================================================================
# المُترجِم
# ==========================================================================
class Translator:
    """مُترجِم متعدّد الخدمات مع اكتشاف تلقائي للموديلات."""

    def __init__(self) -> None:
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()
        self.lt_url = os.getenv("LIBRETRANSLATE_URL", "").strip().rstrip("/")
        self.lt_key = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
        self.source = os.getenv("SOURCE_LANG", "auto").strip() or "auto"
        self.target = os.getenv("TARGET_LANG", "ar").strip() or "ar"
        self.timeout = float(os.getenv("HTTP_TIMEOUT", "45"))

        self._errors: list[str] = []
        self.last_error: str = ""
        self._gemini_model: str | None = None
        self._groq_model: str | None = None
        # FORCE_MOCK=1 يجبر استخدام المترجم الوهمي (للاختبار بلا مفاتيح)
        force_mock = os.getenv("FORCE_MOCK", "").strip() in ("1", "true", "True", "yes")
        # نستخدم المترجم الوهمي فقط إذا لم تتوفّر أي خدمة إطلاقاً
        self._mock = force_mock or not (
            self.gemini_key or self.groq_key or self.lt_url
            or GoogleTranslator is not None)

    # ------------------------------------------------------------------ #
    # معلومات
    # ------------------------------------------------------------------ #
    def _backends(self) -> list[str]:
        """أسماء الخدمات المتاحة فعلاً (بالترتيب)."""
        names: list[str] = []
        if self.gemini_key:
            names.append("gemini")
        if self.groq_key:
            names.append("groq")
        if GoogleTranslator is not None:
            names.append("google")
        if self.lt_url:
            names.append("libretranslate")
        if not names:
            names.append("mock")
        return names

    def all_errors(self) -> list[str]:
        return list(self._errors)

    def _record(self, msg: str) -> None:
        self.last_error = msg
        self._errors.append(msg)
        log.warning(msg)

    # ------------------------------------------------------------------ #
    # اكتشاف الموديلات
    # ------------------------------------------------------------------ #
    def _discover_gemini_model(self) -> str | None:
        if self._gemini_model:
            return self._gemini_model
        if requests is None or not self.gemini_key:
            return None
        try:
            r = requests.get(GEMINI_LIST_URL, params={"key": self.gemini_key},
                             timeout=self.timeout)
            r.raise_for_status()
            models = r.json().get("models", [])
            usable = [m for m in models
                      if "generateContent" in m.get("supportedGenerationMethods", [])]
            if not usable:
                self._record("Gemini: لا يوجد موديل يدعم generateContent")
                return None
            # نفضّل flash ثم pro ثم أي موديل
            def rank(m: dict) -> int:
                n = m.get("name", "")
                if "flash" in n:
                    return 0
                if "pro" in n:
                    return 1
                return 2
            usable.sort(key=rank)
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
                if "llama-3.3" in mid:
                    return 0
                if "llama-3.1" in mid:
                    return 1
                if "llama" in mid:
                    return 2
                if "mixtral" in mid or "gemma" in mid:
                    return 3
                return 4
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
            raise RuntimeError("Gemini: لا يوجد موديل")
        prompt = (f"Translate the following text into {LANG_NAMES.get(self.target, self.target)}. "
                  f"Return ONLY the translation, no explanations, no quotes.\n\n{text}")
        r = requests.post(
            GEMINI_GEN_URL.format(model=model),
            params={"key": self.gemini_key},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.0}},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _groq(self, text: str) -> str:
        model = self._discover_groq_model()
        if not model:
            raise RuntimeError("Groq: لا يوجد موديل")
        r = requests.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {self.groq_key}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.0,
                "messages": [
                    {"role": "system",
                     "content": (f"You are a translator. Translate the user's text into "
                                 f"{LANG_NAMES.get(self.target, self.target)}. "
                                 f"Return ONLY the translation.")},
                    {"role": "user", "content": text},
                ],
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def _google(self, text: str) -> str:
        if GoogleTranslator is None:
            raise RuntimeError("deep-translator غير مثبّت")
        src = "auto" if self.source in ("", "auto") else self.source
        return GoogleTranslator(source=src, target=self.target).translate(text)

    def _libretranslate(self, text: str) -> str:
        if not self.lt_url:
            raise RuntimeError("LIBRETRANSLATE_URL غير مضبوط")
        payload = {"q": text, "source": self.source, "target": self.target,
                   "format": "text"}
        if self.lt_key:
            payload["api_key"] = self.lt_key
        r = requests.post(f"{self.lt_url}/translate", json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        for k in ("translatedText", "translation", "result"):
            if isinstance(data, dict) and data.get(k):
                return data[k]
        raise RuntimeError(f"رد غير مفهوم من LibreTranslate: {str(data)[:150]}")

    def _mock_translate(self, text: str) -> str:
        if self.target == "ar":
            return "\n".join(_mock_line_en2ar(ln) for ln in text.split("\n"))
        return "\n".join(_mock_line_ar2en(ln) for ln in text.split("\n"))

    # ------------------------------------------------------------------ #
    # الواجهة العامة
    # ------------------------------------------------------------------ #
    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return text
        if self._mock:
            return self._mock_translate(text)

        chain = []
        if self.gemini_key:
            chain.append(("gemini", self._gemini))
        if self.groq_key:
            chain.append(("groq", self._groq))
        if GoogleTranslator is not None:
            chain.append(("google", self._google))
        if self.lt_url:
            chain.append(("libretranslate", self._libretranslate))

        for name, fn in chain:
            try:
                out = fn(text)
                if out and out.strip():
                    return out
                self._record(f"{name}: رد فارغ")
            except Exception as exc:
                self._record(f"{name}: {exc}")
        # كل الخدمات فشلت -> mock حتى لا يتوقف البوت
        self._record("كل الخدمات فشلت - استخدام المترجم الوهمي")
        return self._mock_translate(text)

    def translate_all(self, texts, prog=None) -> list[str]:
        """ترجمة قائمة نصوص، مع استدعاء prog(done, total) للتقدّم."""
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
        """فحص سريع لكل خدمة. يعيد {backend: (ok, message)}."""
        result: dict[str, tuple[bool, str]] = {}
        probe = "hello world"
        if self.gemini_key:
            try:
                self._gemini(probe)
                result["gemini"] = (True, f"OK (model={self._gemini_model})")
            except Exception as exc:
                result["gemini"] = (False, str(exc)[:200])
        if self.groq_key:
            try:
                self._groq(probe)
                result["groq"] = (True, f"OK (model={self._groq_model})")
            except Exception as exc:
                result["groq"] = (False, str(exc)[:200])
        if GoogleTranslator is not None:
            try:
                self._google(probe)
                result["google"] = (True, "OK")
            except Exception as exc:
                result["google"] = (False, str(exc)[:200])
        if self.lt_url:
            try:
                self._libretranslate(probe)
                result["libretranslate"] = (True, "OK")
            except Exception as exc:
                result["libretranslate"] = (False, str(exc)[:200])
        if not result:
            result["mock"] = (True, "لا مفاتيح - المترجم الوهمي يعمل")
        return result

    def status_text(self) -> str:
        lines = [f"الخدمات المتاحة: {', '.join(self._backends())}",
                 f"الاتجاه: {self.source} -> {self.target}"]
        if self._gemini_model:
            lines.append(f"موديل Gemini: {self._gemini_model}")
        if self._groq_model:
            lines.append(f"موديل Groq: {self._groq_model}")
        if self.last_error:
            lines.append(f"آخر خطأ: {self.last_error[:200]}")
        return "\n".join(lines)
