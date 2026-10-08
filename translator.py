"""محرك الترجمة: دفعات كبيرة + إعادة محاولة ذكية + احتياطي متعدد + كاش.
لا يحتاج مكتبات خارجية غير requests (و deep-translator اختياري كآخر احتياط)."""
import os, re, json, time, hashlib, threading, logging
import requests

log = logging.getLogger("translator")

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash,gemini-2.0-flash-lite"
).split(",") if m.strip()]
GROQ_MODELS = [m.strip() for m in os.getenv(
    "GROQ_MODELS", "llama-3.3-70b-versatile,llama-3.1-8b-instant").split(",") if m.strip()]

BATCH_CHARS = int(os.getenv("BATCH_CHARS", "5000"))   # حجم الدفعة الواحدة
BATCH_ITEMS = int(os.getenv("BATCH_ITEMS", "25"))
MAX_WAIT = int(os.getenv("MAX_WAIT", "65"))           # أقصى انتظار لإعادة المحاولة (ثانية)
CACHE_FILE = os.getenv("CACHE_FILE", "/tmp/translation_cache.json")

PROMPT = (
    "Translate each numbered English item below into clear, simple Modern Standard Arabic "
    "for a pharmacy student. Keep drug names, abbreviations and formulas in English "
    "(you may put the Arabic term first and the English term in parentheses). "
    "Do not summarize or skip anything. Output ONLY the translations, each one starting "
    "with its marker exactly like <<<1>>>, <<<2>>> ... in the same order.\n\n{items}"
)


class Quota(Exception):
    """الحصة خلصت (يومية أو غير قابلة للانتظار)."""


class Transient(Exception):
    """خطأ مؤقت."""


# ---------- طبقة الشبكة (منفصلة حتى نقدر نختبرها بمحاكاة) ----------
def http_post(url, payload, headers, timeout=90):
    r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    try:
        body = r.json()
    except Exception:
        body = {"raw": r.text[:300]}
    return r.status_code, body


def _retry_delay(body):
    try:
        for d in body["error"].get("details", []):
            if "retryDelay" in d:
                return float(str(d["retryDelay"]).rstrip("s"))
    except Exception:
        pass
    m = re.search(r"retry in ([\d.]+)s", json.dumps(body))
    return float(m.group(1)) if m else None


def _is_daily(body):
    return "PerDay" in json.dumps(body) or "per day" in json.dumps(body).lower()


class Translator:
    def __init__(self, post=None, sleep=time.sleep):
        self.post = post or http_post
        self.sleep = sleep
        self.dead = {}                      # backend -> وقت انتهاء الإيقاف
        self.lock = threading.Lock()
        self.cache = self._load()
        self.last_error = ""
        self.errors = {}                    # model -> آخر خطأ
        self.stats = {}

    # ---- كاش ----
    def _load(self):
        try:
            return json.load(open(CACHE_FILE, encoding="utf-8"))
        except Exception:
            return {}

    def _save(self):
        try:
            json.dump(self.cache, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def _key(t):
        return hashlib.sha1(t.encode("utf-8")).hexdigest()

    def all_errors(self):
        """كل الأخطاء لكل خدمة (حتى نعرف السبب الحقيقي مو بس آخر خطأ)."""
        if not self.errors:
            return self.last_error or "غير معروف"
        return "\n".join(f"- {v[:160]}" for v in self.errors.values())

    # ---- استدعاء نموذج واحد مع إعادة المحاولة ----
    def _call(self, backend, prompt):
        try:
            return self._call_inner(backend, prompt)
        except (Quota, Transient) as e:
            if "موقوف مؤقتاً" not in str(e):
                self.errors[backend[1]] = str(e)
            raise

    def _call_inner(self, backend, prompt):
        kind, model = backend
        until, why = self.dead.get(backend, (0, ""))
        if time.time() < until:
            raise Quota(f"{model} موقوف مؤقتاً. السبب الأصلي: {why}")
        for attempt in range(4):
            if kind == "gemini":
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                headers = {"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
                gen = {"temperature": 0.2}
                if re.search(r"2\.5-flash", model):
                    gen["thinkingConfig"] = {"thinkingBudget": 0}
                payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
            else:
                url = "https://api.groq.com/openai/v1/chat/completions"
                headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
                payload = {"model": model, "temperature": 0.2,
                           "messages": [{"role": "user", "content": prompt}]}
            try:
                code, body = self.post(url, payload, headers)
            except requests.RequestException as e:
                self.last_error = f"{model}: شبكة ({type(e).__name__})"
                self.sleep(2 * (attempt + 1))
                continue
            if code == 200:
                try:
                    if kind == "gemini":
                        parts = body["candidates"][0]["content"]["parts"]
                        return "".join(p.get("text", "") for p in parts)
                    return body["choices"][0]["message"]["content"]
                except Exception:
                    self.last_error = f"{model}: رد فارغ/محجوب"
                    raise Transient(self.last_error)
            if code == 429:
                self.last_error = f"{model}: 429 تجاوز حد الطلبات"
                if _is_daily(body):
                    self.dead[backend] = (time.time() + 3600, self.last_error)
                    raise Quota(self.last_error)
                wait = _retry_delay(body) or 20 * (attempt + 1)
                if wait > MAX_WAIT:
                    self.dead[backend] = (time.time() + wait, self.last_error)
                    raise Quota(self.last_error)
                self.sleep(wait + 1)
                continue
            if code in (500, 502, 503, 504):
                self.last_error = f"{model}: خطأ خادم {code}"
                self.sleep(3 * (attempt + 1))
                continue
            if code in (400, 401, 403, 404):
                msg = json.dumps(body, ensure_ascii=False)[:200]
                self.last_error = f"{model}: خطأ {code} {msg}"
                self.dead[backend] = (time.time() + 120, self.last_error)
                raise Quota(self.last_error)
            self.last_error = f"{model}: HTTP {code}"
            raise Transient(self.last_error)
        raise Transient(self.last_error or f"{model}: فشلت المحاولات")

    # ---- ترجمة دفعة ----
    @staticmethod
    def _parse(text, n):
        parts = re.split(r"<<<\s*(\d+)\s*>>>", text)
        out = {}
        for i in range(1, len(parts) - 1, 2):
            idx = int(parts[i])
            val = parts[i + 1].strip()
            if 1 <= idx <= n and val:
                out[idx] = val
        return out

    def _backends(self):
        b = []
        if GEMINI_KEY:
            b += [("gemini", m) for m in GEMINI_MODELS]
        if GROQ_KEY:
            b += [("groq", m) for m in GROQ_MODELS]
        return b

    def _translate_batch(self, texts):
        """يرجع قائمة بنفس الطول (None للي فشلت)."""
        n = len(texts)
        result = [None] * n
        items = "\n\n".join(f"<<<{i+1}>>>\n{t}" for i, t in enumerate(texts))
        prompt = PROMPT.format(items=items)
        for backend in self._backends():
            try:
                raw = self._call(backend, prompt)
            except (Quota, Transient):
                continue
            got = self._parse(raw, n)
            for i, v in got.items():
                result[i - 1] = v
            self.stats[backend[1]] = self.stats.get(backend[1], 0) + len(got)
            missing = [i for i in range(n) if result[i] is None]
            if not missing:
                return result
            # أعد المفقود فقط بطلب صغير للنموذج نفسه
            sub = "\n\n".join(f"<<<{k+1}>>>\n{texts[i]}" for k, i in enumerate(missing))
            try:
                raw2 = self._call(backend, PROMPT.format(items=sub))
                got2 = self._parse(raw2, len(missing))
                for k, v in got2.items():
                    result[missing[k - 1]] = v
            except (Quota, Transient):
                pass
            if all(r is not None for r in result):
                return result
        # آخر احتياط: مترجم جوجل المجاني
        missing = [i for i in range(n) if result[i] is None]
        if missing:
            try:
                from deep_translator import GoogleTranslator
                gt = GoogleTranslator(source="en", target="ar")
                for i in missing:
                    for _ in range(3):
                        try:
                            result[i] = gt.translate(texts[i][:4900])
                            self.stats["google-free"] = self.stats.get("google-free", 0) + 1
                            break
                        except Exception as e:
                            self.last_error = f"google-free: {type(e).__name__}"
                            self.errors["google-free"] = self.last_error
                            self.sleep(2)
                    self.sleep(0.4)
            except ImportError:
                pass
        return result

    # ---- الواجهة الرئيسية ----
    def translate_all(self, paragraphs, progress=None):
        """paragraphs: list[str] -> list[str|None] (يحتفظ بالكاش، فإعادة الإرسال تكمل من حيث وقفت)."""
        self.errors = {}
        res = [None] * len(paragraphs)
        todo = []
        for i, p in enumerate(paragraphs):
            if not re.search(r"[A-Za-z]", p):          # أرقام/رموز فقط: لا تحتاج ترجمة
                res[i] = p
            elif self._key(p) in self.cache:
                res[i] = self.cache[self._key(p)]
            else:
                todo.append(i)
        batches, cur, size = [], [], 0
        for i in todo:
            ln = len(paragraphs[i])
            if cur and (size + ln > BATCH_CHARS or len(cur) >= BATCH_ITEMS):
                batches.append(cur); cur, size = [], 0
            cur.append(i); size += ln
        if cur:
            batches.append(cur)
        for bi, idxs in enumerate(batches, 1):
            if progress:
                progress(bi, len(batches))
            out = self._translate_batch([paragraphs[i] for i in idxs])
            for i, v in zip(idxs, out):
                if v:
                    res[i] = v
                    self.cache[self._key(paragraphs[i])] = v
            self._save()
        return res

    def check(self):
        """للأمر /test: يجرب كل خدمة بطلب صغير ويرجع تقرير."""
        report = []
        for backend in self._backends():
            self.dead.pop(backend, None)          # الفحص دائماً يجرب فعلياً
            try:
                t = self._call(backend, PROMPT.format(items="<<<1>>>\nGood morning"))
                report.append(f"✅ {backend[1]}: {self._parse(t,1).get(1, t)[:30]}")
            except (Quota, Transient) as e:
                report.append(f"❌ {backend[1]}: {e}")
        if not report:
            report.append("❌ لا يوجد GEMINI_API_KEY ولا GROQ_API_KEY")
        return "\n".join(report)
