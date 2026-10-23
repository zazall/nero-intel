import os
import json
import hashlib
import html
import re
import time
import calendar
from pathlib import Path
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
import feedparser

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"].strip()

MAX_HISTORY = 1000
HEADERS = {"User-Agent": "NERO-INTEL-NewsBot/2.0"}

FEEDS = [
    ("AI", "TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("AI", "Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("AI", "Google AI", "https://blog.google/technology/ai/rss/"),
    ("CYBER", "The Hacker News", "https://feeds.feedburner.com/TheHackersNews"),
    ("CYBER", "BleepingComputer", "https://www.bleepingcomputer.com/feed/"),
    ("CYBER", "Krebs on Security", "https://krebsonsecurity.com/feed/"),
    ("VULN", "CISA Advisories", "https://www.cisa.gov/cybersecurity-advisories/all.xml"),
    ("VULN", "TSEC Threat News", "https://tsec.ai/feed/all.xml"),
    ("TOOLS", "Hacker News", "https://news.ycombinator.com/rss"),
]

CATEGORIES_AR = {
    "AI": "الذكاء الاصطناعي وصناعة الفيديو",
    "CYBER": "الأمن السيبراني",
    "LEAKS": "التسريبات التقنية",
    "VULN": "الثغرات الأمنية",
    "TOOLS": "الأدوات التقنية",
}

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

NO_SUMMARY_AR = (
    "لم يوفّر المصدر ملخصًا كافيًا. "
    "يمكنك الاطلاع على التفاصيل من رابط الخبر الأصلي."
)


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
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(
                    calendar.timegm(parsed), timezone.utc
                )
            except Exception:
                pass

    return now_utc()


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
            if isinstance(data, dict):
                data.setdefault("posted", [])
                return data
        except Exception as exc:
            print(f"State read error: {exc}")

    return {"posted": []}


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
                    str(entry.get("title", ""))
                ).strip()

                link = str(entry.get("link", "")).strip()

                raw_summary = str(
                    entry.get("summary", "")
                    or entry.get("description", "")
                )

                summary = html.unescape(
                    re.sub(r"<[^>]+>", " ", raw_summary)
                )
                summary = " ".join(summary.split())[:2500]

                if not title or not link.startswith("https://"):
                    continue

                published = parse_date(entry)
                if published < cutoff:
                    continue

                normalized_link = link.split("#", 1)[0]
                uid = hashlib.sha256(
                    normalized_link.encode("utf-8")
                ).hexdigest()

                results.append({
                    "id": uid,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source,
                    "category": category,
                    "published": iso(published),
                    "published_dt": published,
                })

        except Exception as exc:
            print(f"Feed error [{source}]: {exc}")

    unique = {}

    for item in results:
        old = unique.get(item["id"])
        if old is None or item["published_dt"] > old["published_dt"]:
            unique[item["id"]] = item

    news = list(unique.values())
    news.sort(
        key=lambda item: item["published_dt"],
        reverse=True
    )

    print(f"Fetched {len(news)} unique stories.")
    return news


def translate_to_arabic(text):
    """ترجمة بدون مفتاح Gemini أو API key."""
    text = " ".join(str(text or "").split()).strip()

    if not text:
        return ""

    endpoint = (
        "https://translate.googleapis.com/translate_a/single"
    )
    last_error = None

    for attempt in range(2):
        try:
            response = requests.get(
                endpoint,
                params={
                    "client": "gtx",
                    "sl": "auto",
                    "tl": "ar",
                    "dt": "t",
                    "q": text[:3500],
                },
                headers=HEADERS,
                timeout=25,
            )

            response.raise_for_status()
            data = response.json()
            chunks = data[0] if data and data[0] else []

            translated = "".join(
                str(chunk[0])
                for chunk in chunks
                if isinstance(chunk, list)
                and chunk
                and chunk[0]
            ).strip()

            if translated:
                return translated

            raise ValueError(
                "Translation service returned empty text."
            )

        except Exception as exc:
            last_error = exc
            print(
                f"Translation attempt {attempt + 1} failed: {exc}"
            )

            if attempt == 0:
                time.sleep(2)

    raise RuntimeError(f"Translation failed: {last_error}")


def translate_item(item):
    title_ar = translate_to_arabic(item["title"])

    if item["summary"].strip():
        summary_ar = translate_to_arabic(item["summary"])
    else:
        summary_ar = NO_SUMMARY_AR

    if not title_ar:
        raise RuntimeError("Arabic title is empty.")

    if not summary_ar:
        summary_ar = NO_SUMMARY_AR

    return {
        "title_ar": title_ar,
        "summary_ar": summary_ar,
    }


def make_message(item, translated):
    category = CATEGORIES_AR.get(
        item["category"], "أخبار التقنية"
    )

    source = SOURCES_AR.get(
        item["source"], item["source"]
    )

    message = (
        "🛡️ نيرو إنتل | تقرير استخباراتي تقني\n\n"
        f"📂 التصنيف: {category}\n\n"
        f"📰 {translated['title_ar']}\n\n"
        f"📌 الملخص:\n{translated['summary_ar']}\n\n"
        f"🌐 المصدر: {source}\n"
        f"🔗 رابط الخبر الأصلي:\n{item['link']}\n\n"
        "#نيرو_إنتل #أخبار_التقنية"
    )

    return message[:4000]


def send_telegram(message):
    endpoint = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        endpoint,
        json={
            "chat_id": CHANNEL_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=25,
    )

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")

    return data


def main():
    state = load_state()
    posted_list = state.get("posted", [])

    if not isinstance(posted_list, list):
        posted_list = []

    posted = set(posted_list)

    news = fetch_news()

    candidates = [
        item for item in news
        if item["id"] not in posted
    ]

    if not candidates:
        print(
            "No fresh, unposted stories. "
            "Nothing to publish this run."
        )
        return

    # يجرب أحدث خمسة أخبار إذا فشلت ترجمة أحدها
    for item in candidates[:5]:
        try:
            translated = translate_item(item)
        except Exception as exc:
            print(
                f"Skipping story because translation failed: {exc}"
            )
            continue

        message = make_message(item, translated)

        # لا يسجل الخبر إلا بعد نجاح إرساله إلى Telegram
        send_telegram(message)

        posted_list.append(item["id"])
        state["posted"] = posted_list[-MAX_HISTORY:]
        save_state(state)

        print(f"Posted: {translated['title_ar']}")
        return

    print(
        "No story was posted because translation failed "
        "for the latest candidates."
    )


if __name__ == "__main__":
    main()
