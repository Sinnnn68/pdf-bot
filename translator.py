#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translator.py  -  مُترجِم متعدّد الخدمات (نسخة الإنتاج v3)
=========================================================
المبدأ الأساسي: **لا نستخدم المترجم الوهمي إلا كخيار أخير مُعلن**.
نحاول أولاً خدمات ترجمة حقيقية مجانية تعمل من داخل Railway بلا مفاتيح،
ثم خدمات المفاتيح إن توفّرت، ثم الوهمي.

ترتيب الخدمات (أول خدمة تنجح هي المستخدمة):
  1. Gemini         (GEMINI_API_KEY)      - اكتشاف تلقائي للموديل المتاح
  2. Groq           (GROQ_API_KEY)        - اكتشاف تلقائي للموديل المتاح
  3. Google-free    (بلا مفتاح)           - جودة عالية للمصطلحات
  4. MyMemory       (بلا مفتاح)           - حقيقية ومجانية وتعمل بدون API key
  5. Google         (deep-translator)     - تدوير + إعادة محاولة
  6. LibreTranslate (LIBRETRANSLATE_URL)  - اختياري
  7. Mock           (قاموس محلي)          - خيار أخير فقط، ورسالته "تقريبية"
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

# ==========================================================================
# نقاط النهاية
# ==========================================================================
GEMINI_LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_GEN_URL = ("https://generativelanguage.googleapis.com/v1beta/"
                  "{model}:generateContent")
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
    "ur": "Urdu", "it": "Italian", "nl": "Dutch",
}

EN_AR: dict[str, str] = {
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
    "chest pain": "ألم الصدر", "systolic blood pressure": "ضغط الدم الانقباضي",
    "angina": "الذبحة", "heart": "القلب", "blood": "الدم",
    "pressure": "الضغط", "drug": "دواء", "drugs": "أدوية",
    "nitrates": "النترات", "calcium": "الكالسيوم", "muscle": "العضلة",
    "oxygen": "الأكسجين", "coronary": "التاجي", "arteries": "الشرايين",
    "treatment": "العلاج", "patient": "المريض", "dose": "الجرعة",
}

_TOKEN_SPLIT = re.compile(r"(\s+)")
_STRIP = "،.,؛;:!؟?\"'()[]{}«»…"
_LATIN = re.compile(r"[A-Za-z]")
_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def _lookup_en(word: str) -> str:
    low = word.lower()
    if low in EN_AR:
        return EN_AR[low]
    for suf, repl in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""), ("ed", "")):
        if low.endswith(suf) and len(low) > len(suf) + 1 and low[:-len(suf)] + repl in EN_AR:
            return EN_AR[low[:-len(suf)] + repl]
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


TERM_FIXES = {
    "نقطة في البوصة": "مثبطات مضخة البروتون",
    "هضم السرمرض": "حرقة المعدة",
    "المخدرات": "الأدوية",
    "مخدرات": "أدوية",
}
ORDER_FIXES = {
    "الدم إمداد": "إمداد الدم",
    "الانقباضي الضغط": "ضغط الدم الانقباضي",
    "الدم تدفق": "تدفق الدم",
    "المعدة حمض": "حمض المعدة",
}
_AR_LAT = re.compile(r"([\u0600-\u06FF])([A-Za-z])")
_LAT_AR = re.compile(r"([A-Za-z])([\u0600-\u06FF])")


def polish_text(text: str, target: str = "ar") -> str:
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


class Translator:
    def __init__(self) -> None:
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip()
        self.lt_url = os.getenv("LIBRETRANSLATE_URL", "").strip().rstrip("/")
        self.lt_key = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()
        self.mymemory_email = os.getenv("MYMEMORY_EMAIL", "").strip()
        self.source = (os.getenv("SOURCE_LANG", "auto").strip() or "auto")
        self.target = (os.getenv("TARGET_LANG", "ar").strip() or "ar")
        self.timeout = float(os.getenv("HTTP_TIMEOUT", "30"))

        self._errors: list[str] = []
        self.last_error: str = ""
        self._gemini_model: str | None = None
        self._groq_model: str | None = None
        self.force_mock = os.getenv("FORCE_MOCK", "").strip() in ("1", "true", "yes")

        self.used_real_service = False
        self.service_counts: dict[str, int] = {}
        self._dead_until: dict[str, float] = {}
        self._fail_count: dict[str, int] = {}
        self.cooldown = float(os.getenv("SERVICE_COOLDOWN", "300"))

    def _backends(self) -> list[str]:
        names: list[str] = []
        if self.gemini_key:
            names.append("gemini")
        if self.groq_key:
            names.append("groq")
        names.append("google-free")
        names.append("mymemory")
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

    def _count(self, name: str) -> None:
        self.service_counts[name] = self.service_counts.get(name, 0) + 1
        self.used_real_service = True

    def _discover_gemini_model(self) -> str | None:
        if self._gemini_model:
            return self._gemini_model
        if requests is None or not self.gemini_key:
            return None
        try:
            r = requests.get(GEMINI_LIST_URL, params={"key": self.gemini_key}, timeout=self.timeout)
            r.raise_for_status()
            models = r.json().get("models", [])
            usable = [m for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
            if not usable:
                return None
            self._gemini_model = usable[0]["name"].split("/")[-1]
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
            r = requests.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {self.groq_key}"}, timeout=self.timeout)
            r.raise_for_status()
            ids = [m.get("id", "") for m in r.json().get("data", [])]
            if not ids:
                return None
            self._groq_model = ids[0]
            return self._groq_model
        except Exception as exc:
            self._record(f"Groq discovery فشل: {exc}")
            return None

    def _gemini(self, text: str) -> str:
        model = self._discover_gemini_model()
        if not model:
            raise RuntimeError("Gemini: لا يوجد موديل متاح")
        prompt = f"Translate into Arabic. Return ONLY the translation:\n\n{text}"
        r = requests.post(
            GEMINI_GEN_URL.format(model=model),
            params={"key": self.gemini_key},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.0}},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _groq(self, text: str) -> str:
        model = self._discover_groq_model()
        if not model:
            raise RuntimeError("Groq: لا يوجد موديل متاح")
        r = requests.post(
            GROQ_CHAT_URL,
            headers={"Authorization": f"Bearer {self.groq_key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0.0,
                  "messages": [{"role": "system", "content": "Translate into Arabic. Return ONLY translation."},
                               {"role": "user", "content": text}]},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    def _mymemory(self, text: str) -> str:
        parts = _chunk_bytes(text, 450)
        outs: list[str] = []
        for part in parts:
            params = {"q": part, "langpair": f"{'en' if self.source == 'auto' else self.source}|{self.target}"}
            if self.mymemory_email:
                params["de"] = self.mymemory_email
            r = requests.get(MYMEMORY_URL, params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            t = (data.get("responseData") or {}).get("translatedText", "") or ""
            if not t.strip() or "MYMEMORY WARNING" in t.upper():
                raise RuntimeError("MyMemory رد غير صالح")
            outs.append(t.strip())
            time.sleep(0.35)
        return " ".join(outs)

    def _google(self, text: str) -> str:
        if GoogleTranslator is None:
            raise RuntimeError("deep-translator غير مثبّت")
        for attempt in range(3):
            try:
                out = GoogleTranslator(source="auto", target=self.target).translate(text)
                if out and out.strip():
                    return out.strip()
            except Exception:
                time.sleep(1.2 * (attempt + 1))
        raise RuntimeError("Google فشل")

    def _google_free(self, text: str) -> str:
        if requests is None:
            raise RuntimeError("requests غير مثبّت")
        sl = "auto" if self.source in ("", "auto") else self.source
        last = None
        for attempt in range(3):
            for host in GOOGLE_FREE_HOSTS:
                try:
                    if "clients5" in host:
                        r = requests.get(
                            host, params={"client": "dict-chrome-ex", "sl": sl, "tl": self.target, "q": text},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout)
                        r.raise_for_status()
                        data = r.json()
                        out = "".join(seg[0] for seg in data[0] if seg) if isinstance(data, list) and data else ""
                    else:
                        r = requests.get(
                            host, params={"client": "gtx", "sl": sl, "tl": self.target, "dt": "t", "q": text},
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=self.timeout)
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
        raise RuntimeError("رد LibreTranslate غير مفهوم")

    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return text
        if self.force_mock:
            return self._polish(self._mock_translate(text))

        chain = []
        if self.gemini_key:
            chain.append(("gemini", self._gemini))
        if self.groq_key:
            chain.append(("groq", self._groq))
        if requests is not None:
            chain.append(("google-free", self._google_free))
            chain.append(("mymemory", self._mymemory))
        if GoogleTranslator is not None:
            chain.append(("google", self._google))
        if self.lt_url:
            chain.append(("libretranslate", self._libretranslate))

        now = time.time()
        for name, fn in chain:
            if self._dead_until.get(name, 0.0) > now:
                continue
            try:
                out = fn(text)
                if out and out.strip():
                    self._count(name)
                    self._fail_count[name] = 0
                    return self._polish(out)
            except Exception as exc:
                self._record(f"{name}: {exc}")
                self._fail_count[name] = self._fail_count.get(name, 0) + 1
                if self._fail_count[name] >= 2:
                    self._dead_until[name] = now + self.cooldown

        return self._polish(self._mock_translate(text))

    def translate_all(self, texts, prog=None) -> list[str]:
        out: list[str] = []
        total = len(texts)
        for i, t in enumerate(texts):
            out.append(self.translate(t))
            if prog:
                try:
                    prog(i + 1, total)
                except Exception:
                    pass
        return out

    def check(self) -> dict:
        result: dict[str, tuple[bool, str]] = {}
        probe = "chest pain"
        if self.gemini_key:
            try:
                self._gemini(probe)
                result["gemini"] = (True, f"يعمل (model={self._gemini_model})")
            except Exception as exc:
                result["gemini"] = (False, str(exc)[:160])
        if self.groq_key:
            try:
                self._groq(probe)
                result["groq"] = (True, f"يعمل (model={self._groq_model})")
            except Exception as exc:
                result["groq"] = (False, str(exc)[:160])
        if requests is not None:
            try:
                self._google_free(probe)
                result["google-free"] = (True, "يعمل (بلا مفتاح)")
            except Exception as exc:
                result["google-free"] = (False, str(exc)[:160])
            try:
                self._mymemory(probe)
                result["mymemory"] = (True, "يعمل (بلا مفتاح)")
            except Exception as exc:
                result["mymemory"] = (False, str(exc)[:160])
        if GoogleTranslator is not None:
            try:
                self._google(probe)
                result["google"] = (True, "يعمل")
            except Exception as exc:
                result["google"] = (False, str(exc)[:160])
        if self.lt_url:
            try:
                self._libretranslate(probe)
                result["libretranslate"] = (True, "يعمل")
            except Exception as exc:
                result["libretranslate"] = (False, str(exc)[:160])
        return result

    def startup_probe(self) -> dict:
        results = self.check()
        for name, (ok, msg) in results.items():
            log.info("خدمة %-14s : %s %s", name, "✅" if ok else "❌", msg)
        return results

    def status_text(self) -> str:
        lines = [f"الخدمات المجهّزة: {', '.join(self._backends())}",
                 f"الاتجاه: {self.source} -> {self.target}"]
        if self.service_counts:
            used = ", ".join(f"{k}({v})" for k, v in self.service_counts.items())
            lines.append(f"استُخدمت فعلاً: {used}")
        return "\n".join(lines)
