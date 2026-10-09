#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
translator.py
=============
مُترجِم متعدّد الخدمات. تُجرَّب الخدمات بهذا الترتيب وأول واحدة تنجح تُستخدم:

  1. Gemini         (GEMINI_API_KEY)
  2. Groq           (GROQ_API_KEY)
  3. Google         (deep-translator، بلا مفتاح)
  4. LibreTranslate (LIBRETRANSLATE_URL)

إذا فشلت كل الخدمات يظهر خطأ واضح (لا توجد ترجمة وهمية).
"""
from __future__ import annotations

import logging
import os
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


def _prompt(target: str) -> str:
    lang = LANG_NAMES.get(target, target)
    return (
        f"You are a professional medical and pharmacy translator. "
        f"Translate the user's text into clear, natural {lang} (Modern Standard Arabic "
        f"if the target is Arabic). Rules: "
        f"1) Use the correct standard medical Arabic terms. "
        f"2) Write drug names in Arabic and put the English name in parentheses after it, "
        f"for example: النيتروغليسرين (Nitroglycerin). "
        f"3) Keep numbers, doses and units as they are. "
        f"4) Keep the line breaks of the original. "
        f"5) Return ONLY the translation, with no explanations and no quotes."
    )


# ==========================================================================
# المُترجِم
# ==========================================================================
class Translator:
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

    # ------------------------------------------------------------------ #
    # معلومات
    # ------------------------------------------------------------------ #
    def _backends(self) -> list[str]:
        names: list[str] = []
        if self.gemini_key:
            names.append("gemini")
        if self.groq_key:
            names.append("groq")
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
            bad = ("image", "tts", "embed", "live", "audio", "vision",
                   "aqa", "imagen", "veo", "robotics", "computer")
            usable = [
                m for m in models
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and not any(b in m.get("name", "").lower() for b in bad)
            ]
            if not usable:
                self._record("Gemini: لا يوجد موديل نصي مناسب")
                return None

            def rank(m: dict) -> tuple:
                n = m.get("name", "").lower()
                score = 2
                if "flash" in n and "lite" not in n:
                    score = 0
                elif "flash" in n:
                    score = 1
                elif "pro" in n:
                    score = 3
                # نفضّل الموديلات المستقرة على التجريبية
                unstable = 1 if ("preview" in n or "exp" in n) else 0
                return (score, unstable, n)

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
            bad = ("whisper", "guard", "tts", "playai", "distil")
            ids = [i for i in ids if i and not any(b in i.lower() for b in bad)]
            if not ids:
                self._record("Groq: لا توجد موديلات نصية متاحة")
                return None

            def rank(mid: str) -> int:
                m = mid.lower()
                if "llama-3.3-70b" in m:
                    return 0
                if "llama-3.1-70b" in m:
                    return 1
                if "llama-3.3" in m or "llama-3.1" in m:
                    return 2
                if "llama" in m:
                    return 3
                if "gpt-oss" in m or "qwen" in m:
                    return 4
                return 5

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
        r = requests.post(
            GEMINI_GEN_URL.format(model=model),
            params={"key": self.gemini_key},
            json={
                "systemInstruction": {"parts": [{"text": _prompt(self.target)}]},
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {"temperature": 0.0},
            },
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
                    {"role": "system", "content": _prompt(self.target)},
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
        r = requests.post(f"{self.lt_url}/translate", json=payload,
                          timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        for k in ("translatedText", "translation", "result"):
            if isinstance(data, dict) and data.get(k):
                return data[k]
        raise RuntimeError(f"رد غير مفهوم من LibreTranslate: {str(data)[:150]}")

    # ------------------------------------------------------------------ #
    # الواجهة العامة
    # ------------------------------------------------------------------ #
    def translate(self, text: str) -> str:
        if not text or not text.strip():
            return text

        chain = []
        if self.gemini_key:
            chain.append(("gemini", self._gemini))
        if self.groq_key:
            chain.append(("groq", self._groq))
        if GoogleTranslator is not None:
            chain.append(("google", self._google))
        if self.lt_url:
            chain.append(("libretranslate", self._libretranslate))

        if not chain:
            raise RuntimeError("لا توجد أي خدمة ترجمة متاحة (ثبّت deep-translator).")

        for name, fn in chain:
            for _attempt in range(2):
                try:
                    out = fn(text)
                    if out and out.strip():
                        return out
                    self._record(f"{name}: رد فارغ")
                    break
                except Exception as exc:
                    self._record(f"{name}: {exc}")
                    time.sleep(1.5)

        raise RuntimeError(
            "فشلت كل خدمات الترجمة: " + " | ".join(self._errors[-3:]))

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
        """فحص سريع لكل خدمة. يعيد {backend: (ok, message)}."""
        result: dict[str, tuple[bool, str]] = {}
        probe = "Angina pectoris is chest pain."
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
            result["none"] = (False, "لا توجد أي خدمة ترجمة")
        return result

    def status_text(self) -> str:
        lines = [f"الخدمات المتاحة: {', '.join(self._backends()) or 'لا شيء'}",
                 f"الاتجاه: {self.source} -> {self.target}"]
        if self._gemini_model:
            lines.append(f"موديل Gemini: {self._gemini_model}")
        if self._groq_model:
            lines.append(f"موديل Groq: {self._groq_model}")
        if self.last_error:
            lines.append(f"آخر خطأ: {self.last_error[:200]}")
        return "\n".join(lines)
