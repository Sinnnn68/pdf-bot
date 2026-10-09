# -*- coding: utf-8 -*-
"""
translator.py - ترجمة نصوص للعربي بأكثر من خدمة (واحدة تفشل تجرب الثانية).

الترتيب:
  1) Gemini  (يحتاج GEMINI_API_KEY بـ Railway Variables)
  2) Groq    (اختياري: GROQ_API_KEY)
  3) Google  (مجاني عبر deep-translator، بدون مفتاح)

الواجهة اللي يستخدمها main.py:
  Translator().translate_all(texts, prog) -> list (نفس الترتيب، None للفاشل)
  .check() -> نص تقرير | .all_errors() -> نص | .last_error | ._backends()
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

CHUNK_ITEMS = 12          # عدد الفقرات بالطلب الواحد
CHUNK_CHARS = 3500        # أو حد الحروف
WORKERS = 3               # طلبات متوازية

PROMPT = (
    "Translate each English string in the JSON array below into Arabic. "
    "The text comes from a university pharmacy/medicine lecture, so keep drug names "
    "and medical terms accurate: write the Arabic term followed by the English term "
    "in parentheses when helpful. Keep numbers, doses and symbols as they are. "
    "Return ONLY a JSON array of Arabic strings, same length and same order, nothing else.\n\n"
)


class Translator:
    def __init__(self):
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.groq_key = os.environ.get("GROQ_API_KEY", "").strip()
        self.gemini_models = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(),
                                          "gemini-2.0-flash", "gemini-2.5-flash",
                                          "gemini-1.5-flash"] if m]
        self.groq_model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.errors = []
        self.last_error = ""
        self.cache = {}

    # ---------------- أدوات ----------------
    def _backends(self):
        b = []
        if self.gemini_key:
            b.append("gemini")
        if self.groq_key:
            b.append("groq")
        b.append("google")
        return b

    def _err(self, where, e):
        msg = f"{where}: {type(e).__name__}: {str(e)[:150]}"
        self.last_error = msg
        if msg not in self.errors:
            self.errors.append(msg)
        return msg

    def all_errors(self):
        return "\n".join(self.errors[-6:]) or "لا توجد تفاصيل"

    @staticmethod
    def _parse_array(text, n):
        text = (text or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = json.loads(text)
        if isinstance(data, dict):                       # أحياناً يرجع {"translations": [...]}
            data = next((v for v in data.values() if isinstance(v, list)), None)
        if not isinstance(data, list) or len(data) != n:
            raise ValueError("عدد النتائج لا يطابق عدد الفقرات")
        return [str(x).strip() for x in data]

    # ---------------- Gemini ----------------
    def _gemini(self, texts):
        body = {
            "contents": [{"parts": [{"text": PROMPT + json.dumps(texts, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        last = None
        for model in self.gemini_models:
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent?key={self.gemini_key}")
            try:
                r = requests.post(url, json=body, timeout=90)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return self._parse_array(txt, len(texts))
            except Exception as e:
                last = e
                self._err(f"gemini({model})", e)
                if "429" in str(e):
                    time.sleep(3)
        raise last

    # ---------------- Groq ----------------
    def _groq(self, texts):
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.groq_key}"},
            json={"model": self.groq_model, "temperature": 0.2,
                  "messages": [{"role": "user",
                                "content": PROMPT + json.dumps(texts, ensure_ascii=False)}]},
            timeout=90)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {r.text[:120]}")
        return self._parse_array(r.json()["choices"][0]["message"]["content"], len(texts))

    # ---------------- Google (مجاني) ----------------
    def _google_one(self, text):
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="ar").translate(text[:4900])

    def _google(self, texts):
        out = []
        for t in texts:
            res = None
            for i in range(3):
                try:
                    res = self._google_one(t)
                    if res:
                        break
                except Exception as e:
                    self._err("google", e)
                time.sleep(1 + i)
            out.append(res)
            time.sleep(0.3)
        return out

    # ---------------- ترجمة دفعة ----------------
    def _translate_chunk(self, texts):
        """يجرّب الخدمات بالترتيب. يرجع قائمة (ممكن فيها None)."""
        todo = [t for t in texts if t not in self.cache]
        if todo:
            done = None
            for name in self._backends():
                try:
                    if name == "gemini":
                        done = self._gemini(todo)
                    elif name == "groq":
                        done = self._groq(todo)
                    else:
                        done = self._google(todo)
                    if done and any(done):
                        break
                except Exception as e:
                    self._err(name, e)
                    done = None
            if done:
                for t, r in zip(todo, done):
                    if r:
                        self.cache[t] = r
        return [self.cache.get(t) for t in texts]

    def translate_all(self, texts, prog=None):
        chunks, cur, chars = [], [], 0
        for i, t in enumerate(texts):
            if cur and (len(cur) >= CHUNK_ITEMS or chars + len(t) > CHUNK_CHARS):
                chunks.append(cur)
                cur, chars = [], 0
            cur.append(i)
            chars += len(t)
        if cur:
            chunks.append(cur)

        results = [None] * len(texts)
        counter = {"n": 0}

        def work(idx_list):
            res = self._translate_chunk([texts[i] for i in idx_list])
            for i, r in zip(idx_list, res):
                results[i] = r
            counter["n"] += 1
            if prog:
                try:
                    prog(counter["n"], len(chunks))
                except Exception:
                    pass

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            list(ex.map(work, chunks))
        return results

    # ---------------- فحص ----------------
    def check(self):
        lines = []
        for name in self._backends():
            try:
                t0 = time.time()
                if name == "gemini":
                    r = self._gemini(["Hello"])[0]
                elif name == "groq":
                    r = self._groq(["Hello"])[0]
                else:
                    r = self._google_one("Hello")
                lines.append(f"✅ {name}: {r} ({time.time() - t0:.1f}s)")
            except Exception as e:
                lines.append(f"❌ {name}: {self._err(name, e)}")
        if not self.gemini_key:
            lines.append("ℹ️ GEMINI_API_KEY غير موجود بـ Variables")
        return "\n".join(lines)
