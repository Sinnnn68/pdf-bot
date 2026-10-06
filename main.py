import os
import re
import time
import threading
import asyncio
import logging
import tempfile
import shutil

import requests
import pypdf
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
# مكتبة httpx تطبع رابط فيه توكن البوت بالـ Logs، نسكتها حتى ما ينكشف التوكن
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pdf-bot")

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")


def _build_model_list():
    """الموديل اللي تكتبه بمتغير GEMINI_MODEL يجي أول، وبعده بدائل تلقائية."""
    wanted = [m.strip() for m in (os.getenv("GEMINI_MODEL") or "").split(",") if m.strip()]
    wanted += ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
    seen, result = set(), []
    for m in wanted:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


MODELS = _build_model_list()
MODEL_STATE = {"i": 0}  # رقم الموديل الشغال حالياً
MODEL_LOCK = threading.Lock()

MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "6"))       # عدد المحاولات لكل صفحة
CONCURRENCY = int(os.getenv("CONCURRENCY", "3"))         # كم صفحة تترجم بنفس الوقت
CHUNK_LIMIT = 5000                                       # أقصى طول نص يرسل لجيميناي بطلب واحد
MAX_FILE_BYTES = 20 * 1024 * 1024                        # حد تليجرام للبوتات بالتحميل
ARABIC_RE = re.compile(r"[؀-ۿ]")


# ----------------------------------------------------------------------------
# التعامل مع جيميناي (بدون مكتبة جوجل، طلب HTTP مباشر)
# ----------------------------------------------------------------------------
class GeminiError(Exception):
    def __init__(self, user_msg, status=None, retryable=False, fatal=False, wait=None, cap=None):
        super().__init__(user_msg)
        self.cap = cap       # أقصى عدد محاولات لهذا النوع من الأخطاء (مثل الرد الفارغ)
        self.user_msg = user_msg
        self.status = status
        self.retryable = retryable
        self.fatal = fatal   # خطأ ما ينفع معه إعادة المحاولة (مثل المفتاح غلط)
        self.wait = wait


def _call_gemini(model, prompt, timeout=90):
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent"
    try:
        r = requests.post(
            url,
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}},
            timeout=timeout,
        )
    except requests.Timeout:
        raise GeminiError("انتهت مهلة الاتصال بجيميناي", retryable=True)
    except requests.RequestException as e:
        raise GeminiError(f"مشكلة اتصال بجيميناي ({type(e).__name__})", retryable=True)

    status = r.status_code
    if status == 200:
        try:
            data = r.json()
        except ValueError:
            raise GeminiError("رد غير مفهوم من جيميناي", status=status, retryable=True)
        cands = data.get("candidates") or []
        text = ""
        if cands:
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
        if not text.strip():
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (cands[0].get("finishReason") if cands else "EMPTY")
            raise GeminiError(f"جيميناي رجّع رد فارغ أو محجوب ({reason})", status=status, retryable=True, cap=2)
        return text

    try:
        detail = (r.json().get("error") or {}).get("message", "")
    except ValueError:
        detail = r.text[:200]

    if status == 404:
        raise GeminiError(f"الموديل {model} غير موجود", status=404)
    if status in (401, 403) or (status == 400 and "api key" in detail.lower()):
        raise GeminiError("مفتاح GEMINI_API_KEY غير صحيح أو ما عنده صلاحية", status=status, fatal=True)
    if status == 429:
        wait = None
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except ValueError:
            pass
        if wait is None:  # جيميناي يكتبها داخل الجسم: "retryDelay": "26s"
            try:
                for d in (r.json().get("error") or {}).get("details") or []:
                    if "retryDelay" in d:
                        wait = float(str(d["retryDelay"]).rstrip("s"))
            except (ValueError, TypeError, AttributeError):
                pass
        raise GeminiError("تجاوزنا حد الطلبات المجاني لجيميناي (429)", status=429, retryable=True, wait=wait)
    if status >= 500:
        raise GeminiError(f"سيرفر جوجل مشغول ({status})", status=status, retryable=True)
    raise GeminiError(f"جيميناي رفض الطلب ({status}): {detail[:120]}", status=status)


def translate_text(prompt):
    """يترجم مع إعادة محاولة ذكية وتبديل الموديل لو غير موجود. تعمل داخل thread."""
    last = None
    for attempt in range(MAX_ATTEMPTS):
        idx = MODEL_STATE["i"]
        model = MODELS[idx]
        try:
            out = _call_gemini(model, prompt)
            out = re.sub(r"^```\w*\n?|\n?```$", "", out.strip())  # نشيل علامات الكود لو جيميناي حطها
            if not ARABIC_RE.search(out):
                raise GeminiError("جيميناي ما رجّع ترجمة عربية", retryable=True, cap=2)
            return out
        except GeminiError as e:
            last = e
            if e.status == 404 and idx < len(MODELS) - 1:
                # عدة صفحات تشتغل بنفس الوقت: ننقل للموديل التالي مرة وحدة فقط
                with MODEL_LOCK:
                    if MODEL_STATE["i"] == idx:
                        log.warning("الموديل %s غير موجود، ننتقل للتالي", model)
                        MODEL_STATE["i"] = idx + 1
                continue
            if e.fatal or not e.retryable or (e.cap and attempt + 1 >= e.cap):
                raise
            delay = e.wait if e.wait else min(2 ** attempt * 2, 30)
            log.warning("محاولة %d فشلت (%s)، ننتظر %.0f ثانية", attempt + 1, e.user_msg, delay)
            time.sleep(min(delay, 60))
    raise last


# ----------------------------------------------------------------------------
# قراءة الـ PDF وتجهيز النص
# ----------------------------------------------------------------------------
def clean_text(text):
    """pypdf أحياناً يكتب كل كلمة بسطر وبمسافات مضاعفة، نوحّد المسافات."""
    return " ".join(text.replace(" ", " ").split())


def split_chunks(text, limit=CHUNK_LIMIT):
    chunks = []
    while len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        cut = cut + 1 if cut > limit // 2 else limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def extract_pages(pdf_path):
    reader = pypdf.PdfReader(pdf_path)
    if reader.is_encrypted:
        try:
            opened = int(reader.decrypt(""))   # 0 = فشل الفتح بدون كلمة سر
        except Exception:
            opened = 0
        if opened == 0:
            raise pypdf.errors.PyPdfError("encrypted")
    pages = []
    for i, page in enumerate(reader.pages):
        text = clean_text(page.extract_text() or "")
        if text:
            pages.append((i + 1, text))
    return pages, len(reader.pages)


PROMPT = (
    "You are a professional translator for university pharmacy lectures.\n"
    "Translate the English text below into Arabic.\n"
    "The text was extracted from a PDF page, so lines may be broken or merged: first rebuild the natural "
    "paragraphs (headings and bullet points count as paragraphs).\n"
    "Output format, for EVERY paragraph, exactly like this:\n"
    "<Arabic translation of the paragraph>\n"
    "<the original English paragraph>\n"
    "<one empty line>\n"
    "Rules: keep drug names, abbreviations and symbols (Vd, CL, EC50, GFR...) in English inside the Arabic "
    "sentence; do not skip any text; do not add explanations, titles or markdown.\n\n"
    "TEXT:\n"
)


def translate_page(text):
    parts = [translate_text(PROMPT + chunk) for chunk in split_chunks(text)]
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------
# أوامر البوت
# ----------------------------------------------------------------------------
async def safe_edit(bot, message, text):
    try:
        await bot.edit_message_text(chat_id=message.chat_id, message_id=message.message_id, text=text)
    except Exception as e:  # مثل: الرسالة ما تغيرت
        log.debug("edit skipped: %s", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أرسل لي ملف PDF بالإنجليزي وأرجعه لك ملف نصي، كل فقرة بالعربي وتحتها الأصل الإنجليزي.\n"
        "اكتب /test لفحص اتصال البوت بجيميناي."
    )


async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 جاري فحص الاتصال بجيميناي...")
    if not GEMINI_API_KEY:
        await safe_edit(context.bot, msg, "❌ المتغير GEMINI_API_KEY غير موجود في Railway → Variables")
        return
    try:
        out = await asyncio.to_thread(translate_text, "Translate to Arabic, output only the translation: Good morning")
        await safe_edit(context.bot, msg, f"✅ جيميناي يشتغل\nالموديل: {MODELS[MODEL_STATE['i']]}\nالتجربة: {out[:80]}")
    except GeminiError as e:
        await safe_edit(context.bot, msg, f"❌ فشل الاتصال بجيميناي:\n{e.user_msg}")
    except Exception as e:
        log.exception("test failed")
        await safe_edit(context.bot, msg, f"❌ خطأ غير متوقع: {type(e).__name__}")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    doc = message.document
    name = doc.file_name or "file.pdf"

    if not (name.lower().endswith(".pdf") or doc.mime_type == "application/pdf"):
        await message.reply_text("عذراً، أرسل ملف PDF فقط.")
        return
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await message.reply_text("الملف أكبر من 20 ميجا، تليجرام ما يسمح للبوت يحمّله. قسّمه لأجزاء أصغر.")
        return
    if not GEMINI_API_KEY:
        await message.reply_text("❌ المتغير GEMINI_API_KEY غير موجود في Railway → Variables")
        return

    status = await message.reply_text("⏳ جاري قراءة الملف...")
    workdir = tempfile.mkdtemp(prefix="pdfbot_")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        pdf_path = os.path.join(workdir, "input.pdf")
        await tg_file.download_to_drive(pdf_path)

        pages, total_pages = await asyncio.to_thread(extract_pages, pdf_path)
        if not pages:
            await safe_edit(context.bot, status,
                            "❌ ما لقيت نص داخل الملف. غالباً صفحاته صور (ممسوحة بالسكانر)، وهذا البوت يقرأ النصوص فقط.")
            return

        await safe_edit(context.bot, status, f"⏳ جاري الترجمة... 0 من {len(pages)} صفحة")

        results = {}      # رقم الصفحة -> (نجحت؟، النص أو سبب الفشل)
        state = {"done": 0, "fatal": None}
        sem = asyncio.Semaphore(CONCURRENCY)

        async def work(page_no, text):
            async with sem:
                if state["fatal"]:
                    results[page_no] = (False, state["fatal"], text)
                else:
                    try:
                        results[page_no] = (True, await asyncio.to_thread(translate_page, text), text)
                    except GeminiError as e:
                        if e.fatal:
                            state["fatal"] = e.user_msg
                        results[page_no] = (False, e.user_msg, text)
                    except Exception as e:
                        log.exception("page %s failed", page_no)
                        results[page_no] = (False, f"خطأ غير متوقع ({type(e).__name__})", text)
            state["done"] += 1
            await safe_edit(context.bot, status, f"⏳ جاري الترجمة... {state['done']} من {len(pages)} صفحة")

        await asyncio.gather(*(work(n, t) for n, t in pages))

        ok_pages = [n for n in sorted(results) if results[n][0]]
        bad_pages = [n for n in sorted(results) if not results[n][0]]

        if not ok_pages:
            reason = results[bad_pages[0]][1]
            await safe_edit(context.bot, status, f"❌ فشلت الترجمة بالكامل.\nالسبب: {reason}")
            return

        blocks = []
        for n in sorted(results):
            good, content, original = results[n]
            if good:
                blocks.append(f"--- صفحة {n} ---\n{content}")
            else:
                blocks.append(f"--- صفحة {n} ---\n[تعذرت ترجمة هذه الصفحة: {content}]\n\n{original}")

        base = os.path.splitext(os.path.basename(name))[0]
        out_name = f"translated_{base}.txt"
        out_path = os.path.join(workdir, "output.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n\n".join(blocks))

        caption = f"✅ تمت ترجمة {len(ok_pages)} من {len(pages)} صفحة"
        if bad_pages:
            caption += f"\n⚠️ فشلت صفحات: {', '.join(map(str, bad_pages))} (أعد إرسال الملف لإعادة المحاولة)"
        if total_pages > len(pages):
            caption += f"\nℹ️ {total_pages - len(pages)} صفحة بدون نص (صور) تم تخطيها"

        with open(out_path, "rb") as f:
            await message.reply_document(document=f, filename=out_name, caption=caption)
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=status.message_id)
        except Exception:
            pass

    except pypdf.errors.PyPdfError as e:
        log.warning("pdf unreadable: %s", e)
        await safe_edit(context.bot, status, "❌ ما قدرت أقرأ الملف. يبدو تالف أو محمي بكلمة سر، جرّب تفتحه وتحفظه من جديد.")
    except Exception as e:
        log.exception("handle_pdf failed")
        await safe_edit(context.bot, status, f"❌ حدث خطأ أثناء المعالجة ({type(e).__name__}): {str(e)[:150]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def on_error(update, context):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


# ----------------------------------------------------------------------------
# التشغيل
# ----------------------------------------------------------------------------
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN is missing!")
        return
    log.info("Gemini key present: %s | models: %s", bool(GEMINI_API_KEY), MODELS)

    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)   # يخلي /start و /test يردون حتى لو فيه ملف قاعد يترجم
        .read_timeout(60)
        .write_timeout(120)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
    app.add_error_handler(on_error)

    log.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
