import os, io, json, re, time, html
import requests
import telebot
import pymupdf

BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
HERE = os.path.dirname(os.path.abspath(__file__))

bot = telebot.TeleBot(BOT_TOKEN)

CSS = (
    "@font-face {font-family: amiri; src: url(Amiri-Regular.ttf);}"
    "body {font-family: amiri; color: #1a1a8c; direction: rtl;"
    " text-align: right; font-size: 9pt; margin: 0;}"
)


def gemini_translate(texts):
    prompt = (
        "Translate each English text below into clear Arabic. "
        "These are pharmacy/medical lecture notes: keep drug names "
        "and scientific terms in English between brackets if needed. "
        "Return ONLY a JSON array of strings, same length and same order.\n\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent?key={GEMINI_KEY}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }
    for attempt in range(5):
        try:
            r = requests.post(url, json=body, timeout=120)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            out = json.loads(text)
            if isinstance(out, list) and len(out) == len(texts):
                return [str(x) for x in out]
        except Exception as e:
            print("translate error:", e)
            time.sleep(3)
    return [""] * len(texts)


def needs_translation(t):
    return len(re.findall(r"[A-Za-z]", t)) >= 8 and not re.search(
        r"[\u0600-\u06FF]", t
    )


def process_pdf(data):
    doc = pymupdf.open(stream=data, filetype="pdf")
    archive = pymupdf.Archive(HERE)
    for page in doc:
        blocks = [
            b for b in page.get_text("blocks", sort=True) if b[6] == 0
        ]
        items = []
        prev_bottom = 0
        for b in blocks:
            text = " ".join(b[4].split())
            if needs_translation(text):
                items.append((b, text, prev_bottom))
            prev_bottom = b[3]
        for i in range(0, len(items), 30):
            chunk = items[i:i + 30]
            arabic = gemini_translate([c[1] for c in chunk])
            for (b, _, prev), ar in zip(chunk, arabic):
                if not ar.strip():
                    continue
                x0, y0, x1, y1 = b[:4]
                top = max(prev, y0 - (y1 - y0), 0)
                if y0 - top < 14:
                    top = max(0, y0 - 14)
                rect = pymupdf.Rect(x0, top, x1, y0 + 1)
                try:
                    page.insert_htmlbox(
                        rect, "<p>" + html.escape(ar) + "</p>",
                        css=CSS, archive=archive,
                    )
                except Exception as e:
                    print("insert error:", e)
            time.sleep(2)
    return doc.tobytes(garbage=3, deflate=True)


@bot.message_handler(commands=["start"])
def start(m):
    bot.reply_to(m, "أهلاً! ارسل لي ملف PDF وأرجعه لك مترجم بالعربي فوق النص الإنجليزي 📄")


@bot.message_handler(content_types=["document"])
def on_doc(m):
    name = m.document.file_name or "file.pdf"
    if not name.lower().endswith(".pdf"):
        bot.reply_to(m, "ارسل ملف PDF فقط.")
        return
    bot.reply_to(m, "⏳ جاري الترجمة... ممكن تاخذ كم دقيقة")
    try:
        info = bot.get_file(m.document.file_id)
        data = bot.download_file(info.file_path)
        out = process_pdf(data)
        f = io.BytesIO(out)
        f.name = name.rsplit(".", 1)[0] + "_AR.pdf"
        bot.send_document(m.chat.id, f)
    except Exception as e:
        print("error:", e)
        bot.reply_to(m, "صار خطأ بالترجمة، جرب مرة ثانية.")


bot.infinity_polling()
