import os
import json
import random
import hashlib
import html
import time
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
import feedparser

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MIN_HOURS = 1
MAX_HOURS = 30
MAX_HISTORY = 1000
RETRY_MINUTES = 30

MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

# مصادر الأخبار
FEEDS = [
    ("AI", "TechCrunch AI",
     "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("AI", "Hugging Face",
     "https://huggingface.co/blog/feed.xml"),
    ("AI", "Google AI",
     "https://blog.google/technology/ai/rss/"),
    ("CYBER", "The Hacker News",
     "https://feeds.feedburner.com/TheHackersNews"),
    ("CYBER", "BleepingComputer",
     "https://www.bleepingcomputer.com/feed/"),
    ("CYBER", "Krebs on Security",
     "https://krebsonsecurity.com/feed/"),
    ("VULN", "CISA Advisories",
     "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    ("VULN", "TSEC Threat News",
     "https://tsec.ai/feed/all.xml"),
    ("TOOLS", "Hacker News",
     "https://news.ycombinator.com/rss"),
]

HEADERS = {
    "User-Agent": "NERO-INTEL-NewsBot/1.0"
}

# التصنيفات بالعربية
CATEGORIES_AR = {
    "AI": "الذكاء الاصطناعي وصناعة الفيديو",
    "CYBER": "الأمن السيبراني",
    "LEAKS": "التسريبات التقنية",
    "VULN": "الثغرات الأمنية",
    "TOOLS": "الأدوات التقنية",
}

# أسماء المصادر بصياغة مناسبة للمنشور العربي
SOURCES_AR = {
    "TechCrunch AI": "تك كرانش - الذكاء الاصطناعي",
    "Hugging Face": "Hugging Face",
    "Google AI": "Google AI",
    "The Hacker News": "The Hacker News",
    "BleepingComputer": "BleepingComputer",
    "Krebs on Security": "Krebs on Security",
    "CISA Advisories": "وكالة الأمن السيبراني الأمريكية CISA",
    "TSEC Threat News": "TSEC",
    "Hacker News": "Hacker News",
}


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def parse_date(entry):
    value = entry.get("published") or entry.get("updated")

    if value:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(
                    time.mktime(value), timezone.utc
                )
            except Exception:
                pass

    return now_utc()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception as exc:
            print(f"State read error: {exc}")

    return {
        "posted": [],
        "next_post_at": None,
        "initialized": False
    }


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def fetch_news():
    results = []
    cutoff = now_utc() - timedelta(days=14)

    for category, source, url in FEEDS:
        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=20
            )
            response.raise_for_status()

            parsed = feedparser.parse(response.content)

            for entry in parsed.entries[:30]:
                title = html.unescape(
                    entry.get("title", "")
                ).strip()

                link = entry.get("link", "").strip()

                raw_summary = entry.get("summary", "")
                summary = html.unescape(
                    re.sub(r"<[^>]+>", " ", raw_summary)
                )
                summary = " ".join(summary.split())[:1500]

                if not title or not link.startswith("https://"):
                    continue

                published = parse_date(entry)

                if published < cutoff:
                    continue

                key = entry.get("id") or link
                uid = hashlib.sha256(
                    key.encode("utf-8")
                ).hexdigest()

                results.append({
                    "id": uid,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source,
                    "category": category,
                    "published": iso(published)
                })

        except Exception as exc:
            print(f"Feed error [{source}]: {exc}")

    # إزالة الأخبار المكررة القادمة من المصادر المختلفة
    unique = {}
    for item in results:
        unique[item["id"]] = item

    print(f"Fetched {len(unique)} unique stories.")
    return list(unique.values())


def summarize_with_gemini(item):
    """
    ترجمة عنوان الخبر وكتابة ملخص عربي.
    ترجع قاموسًا يحتوي على title_ar و summary_ar،
    أو None عند فشل الترجمة.
    """
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is missing. Arabic translation unavailable.")
        return None

    endpoint = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-2.5-flash:generateContent"
    )

    prompt = f"""
أنت محرر أخبار متخصص لقناة NERO INTEL.

المطلوب كتابة منشور إخباري باللغة العربية الفصحى فقط.

التزم بما يلي:
1. ترجم عنوان الخبر إلى عنوان عربي واضح ودقيق.
2. اكتب ملخصًا عربيًا من جملتين إلى أربع جمل.
3. اكتب بالعربية، ولا تترك العنوان أو الملخص باللغة الإنجليزية.
4. يمكن إبقاء أسماء الشركات والمنتجات والأدوات الرسمية كما هي،
   مثل Google وOpenAI وChatGPT وKali Linux.
5. لا تترجم أسماء المنتجات ترجمة حرفية خاطئة.
6. لا تضف أي معلومة غير موجودة في المصدر.
7. إذا كان الخبر تسريبًا أو ادعاءً غير مؤكد، وضّح ذلك صراحة.
8. لا تدّعِ أن معلومة مؤكدة إذا كان المصدر لا يثبتها.
9. أعد النتيجة بصيغة JSON صحيحة فقط، دون Markdown أو شرح إضافي.

الصيغة المطلوبة:
{{
  "title_ar": "عنوان الخبر بالعربية",
  "summary_ar": "ملخص الخبر باللغة العربية"
}}

عنوان المصدر:
{item['title']}

اسم المصدر:
{item['source']}

نص المصدر:
{item['summary']}

رابط المصدر:
{item['link']}
"""

    try:
        response = requests.post(
            endpoint,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{
                    "parts": [{"text": prompt}]
                }],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json"
                }
            },
            timeout=40
        )

        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates", [])
        if not candidates:
            print("Gemini returned no candidates.")
            return None

        parts = candidates[0].get(
            "content", {}
        ).get("parts", [])

        if not parts or not parts[0].get("text"):
            print("Gemini returned an empty response.")
            return None

        raw_text = parts[0]["text"].strip()

        # إزالة علامات Markdown إذا أضافها النموذج
        raw_text = re.sub(
            r"^```(?:json)?\s*|\s*```$",
            "",
            raw_text,
            flags=re.IGNORECASE
        ).strip()

        result = json.loads(raw_text)

        title_ar = str(
            result.get("title_ar", "")
        ).strip()

        summary_ar = str(
            result.get("summary_ar", "")
        ).strip()

        if not title_ar or not summary_ar:
            print("Arabic title or summary is missing.")
            return None

        return {
            "title_ar": title_ar,
            "summary_ar": summary_ar
        }

    except Exception as exc:
        print(f"Arabic translation failed: {exc}")
        return None


def category_name(category):
    return CATEGORIES_AR.get(
        category,
        "أخبار التقنية"
    )


def make_message(item, translated):
    title = translated["title_ar"]
    summary = translated["summary_ar"]

    category = category_name(item["category"])
    source = SOURCES_AR.get(
        item["source"],
        item["source"]
    )

    message = (
        "🛡️ نيرو إنتل | تقرير استخباراتي تقني\n\n"
        f"📂 التصنيف: {category}\n\n"
        f"📰 {title}\n\n"
        f"📌 الملخص:\n{summary}\n\n"
        f"🌐 المصدر: {source}\n"
        f"🔗 رابط الخبر الأصلي:\n{item['link']}\n\n"
        "#نيرو_إنتل #أخبار_التقنية"
    )

    # حد Telegram الأقصى للرسالة 4096 حرفًا
    return message[:4000]


def send_telegram(message):
    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": CHANNEL_ID,
            "text": message,
            "disable_web_page_preview": False
        },
        timeout=25
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(
            f"Telegram error: {data}"
        )

    return data


def schedule_retry(state, minutes=RETRY_MINUTES):
    state["next_post_at"] = iso(
        now_utc() + timedelta(minutes=minutes)
    )
    save_state(state)


def main():
    state = load_state()
    now = now_utc()

    # التشغيل الأول
    if not state.get("initialized"):
        state["initialized"] = True

        if not MANUAL_RUN:
            state["next_post_at"] = iso(
                now + timedelta(
                    hours=random.randint(
                        MIN_HOURS,
                        MAX_HOURS
                    )
                )
            )
            save_state(state)
            print("Initialized. First post scheduled.")
            return

        state["next_post_at"] = iso(now)
        save_state(state)
        print("Initialized by manual run.")

    # التشغيل اليدوي يتجاوز موعد الانتظار
    due = state.get("next_post_at")

    if due and not MANUAL_RUN:
        due_time = datetime.fromisoformat(due)

        if now < due_time:
            print(f"Waiting until {due}")
            return

    if MANUAL_RUN:
        print("Manual run: schedule wait bypassed.")

    posted = set(state.get("posted", []))
    news = fetch_news()

    candidates = [
        item for item in news
        if item["id"] not in posted
    ]

    if not candidates:
        print("No fresh, unposted stories.")
        schedule_retry(state)
        return

    # اختيار تصنيف ثم خبر غير مكرر
    available_categories = list({
        item["category"] for item in candidates
    })

    category = random.choice(available_categories)

    choices = [
        item for item in candidates
        if item["category"] == category
    ]

    item = random.choice(choices)

    # لا ننشر الخبر إذا تعذرت ترجمته
    translated = summarize_with_gemini(item)

    if not translated:
        print("Skipped: Arabic translation was unsuccessful.")
        schedule_retry(state)
        return

    message = make_message(item, translated)

    # نسجل الخبر بعد نجاح الإرسال فقط
    try:
        send_telegram(message)
    except Exception as exc:
        print(f"Telegram send failed: {exc}")
        schedule_retry(state)
        raise

    posted.add(item["id"])
    state["posted"] = list(posted)[-MAX_HISTORY:]

    state["next_post_at"] = iso(
        now_utc() + timedelta(
            hours=random.randint(
                MIN_HOURS,
                MAX_HOURS
            )
        )
    )

    save_state(state)

    print(f"Posted Arabic news: {translated['title_ar']}")
    print(f"Next target: {state['next_post_at']}")


if __name__ == "__main__":
    main()
