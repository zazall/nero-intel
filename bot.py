
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

# هل التشغيل يدوي من GitHub Actions؟
MANUAL_RUN = os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

# مصادر الأخبار حسب التصنيف
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
                url, headers=HEADERS, timeout=20
            )
            response.raise_for_status()
            parsed = feedparser.parse(response.content)

            for entry in parsed.entries[:30]:
                title = html.unescape(
                    entry.get("title", "")
                ).strip()

                link = entry.get("link", "").strip()
                summary = entry.get("summary", "")
                summary = html.unescape(
                    re.sub(r"<[^>]+>", " ", summary)
                )
                summary = " ".join(summary.split())[:700]

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

    unique = {}
    for item in results:
        unique[item["id"]] = item

    print(f"Fetched {len(unique)} unique stories.")
    return list(unique.values())


def summarize_with_gemini(item):
    if not GEMINI_API_KEY:
        return None

    try:
        endpoint = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/models/gemini-2.5-flash:generateContent"
        )

        prompt = f"""
اكتب ملخصًا عربيًا واضحًا لخبر تقني.
لا تتجاوز 3 جمل.
استعمل المصطلحات الإنجليزية المهمة عند الحاجة.
لا تضف معلومات غير موجودة في النص.
إذا كان الخبر مجرد ادعاء أو تسريب غير مؤكد،
وضّح أنه غير مؤكد ولا تعرضه كحقيقة.

العنوان: {item['title']}
المصدر: {item['source']}
النص: {item['summary']}
الرابط: {item['link']}
"""

        response = requests.post(
            endpoint,
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{
                    "parts": [{"text": prompt}]
                }]
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        return data["candidates"][0]["content"][
            "parts"
        ][0]["text"].strip()

    except Exception as exc:
        print(f"Gemini fallback: {exc}")
        return None


def fallback_summary(item):
    if item["summary"]:
        return item["summary"][:500]

    return (
        "يتناول هذا الخبر تطورًا تقنيًا جديدًا. "
        "افتح المصدر الأصلي للاطلاع على التفاصيل."
    )


def category_name(category):
    return {
        "AI": "الذكاء الاصطناعي وAI Video",
        "CYBER": "الأمن السيبراني وCybersecurity",
        "LEAKS": "التسريبات التقنية",
        "VULN": "الثغرات الأمنية",
        "TOOLS": "الأدوات التقنية"
    }.get(category, "أخبار التقنية")


def make_message(item, summary):
    return (
        "NERO INTEL | INTELLIGENCE REPORT\n\n"
        f"التصنيف: {category_name(item['category'])}\n\n"
        f"{item['title']}\n\n"
        f"الملخص:\n{summary}\n\n"
        f"المصدر: {item['source']}\n"
        f"الرابط الأصلي:\n{item['link']}\n\n"
        "#NERO_INTEL #TechNews"
    )[:4000]


def send_telegram(message):
    url = (
        f"https://api.telegram.org/"
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
        raise RuntimeError(f"Telegram error: {data}")

    return data


def main():
    state = load_state()
    now = now_utc()

    # أول تشغيل: التشغيل اليدوي يحاول النشر فورًا
    if not state.get("initialized"):
        state["initialized"] = True

        if not MANUAL_RUN:
            state["next_post_at"] = iso(
                now + timedelta(
                    hours=random.randint(MIN_HOURS, MAX_HOURS)
                )
            )
            save_state(state)
            print("Initialized. First post scheduled.")
            return

        state["next_post_at"] = iso(now)
        save_state(state)
        print("Initialized by manual run. Trying to post now.")

    # التشغيل التلقائي يحترم الموعد؛ اليدوي يتجاوز الانتظار
    due = state.get("next_post_at")

    if (
        due
        and now < datetime.fromisoformat(due)
        and not MANUAL_RUN
    ):
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
        print("No fresh, unposted stories. Retry next check.")
        state["next_post_at"] = iso(
            now_utc() + timedelta(minutes=30)
        )
        save_state(state)
        return

    # اختيار تصنيف ثم خبر عشوائي منه
    available_categories = list({
        item["category"] for item in candidates
    })
    category = random.choice(available_categories)

    choices = [
        item for item in candidates
        if item["category"] == category
    ]

    item = random.choice(choices)

    summary = summarize_with_gemini(item)
    if not summary:
        summary = fallback_summary(item)

    message = make_message(item, summary)

    # حفظ الخبر في السجل فقط بعد نجاح الإرسال
    send_telegram(message)

    posted.add(item["id"])
    state["posted"] = list(posted)[-MAX_HISTORY:]

    state["next_post_at"] = iso(
        now_utc() + timedelta(
            hours=random.randint(MIN_HOURS, MAX_HOURS)
        )
    )

    save_state(state)

    print(f"Posted: {item['title']}")
    print(f"Next target: {state['next_post_at']}")


if __name__ == "__main__":
    main()
