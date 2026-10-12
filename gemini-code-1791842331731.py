import asyncio
import sqlite3
import logging
import feedparser
import requests
import re
import html
import os
from urllib.parse import urlparse
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from time import mktime

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==================================================
# НАЛАШТУВАННЯ
# ==================================================

BOT_TOKEN = "8751606600:AAEYkwqt2NnJ8K7sif-Ia6kik2jcwinUqOU"
CHANNEL_ID = "@kolomyia_inf"

CHECK_INTERVAL = 900
MAX_NEWS_PER_FEED = 6
MAX_POSTS_PER_CYCLE = 4
MIN_DELAY_BETWEEN_POSTS = 50
MAX_DESCRIPTION = 420
SIMILARITY_THRESHOLD = 0.84
DB_NAME = "kolomyia_news.db"
KEEP_DAYS = 45

RSS_URLS = [
    "https://kurs.if.ua/feed/",
    "https://galka.if.ua/feed/",
    "https://www.pravda.com.ua/rss/atom.xml",
]

SOURCE_NAMES = {
    "kurs.if.ua": "Курс",
    "galka.if.ua": "Галка",
    "www.pravda.com.ua": "Українська правда",
    "pravda.com.ua": "Українська правда",
}

LOCAL_KEYWORDS = [
    "коломи", "коломийськ", "покутт", "івано-франків",
    "прикарпат", "косів", "снятин", "надвірн",
    "гуцульщин", "чернели", "п'яди", "пяди", "верховин"
]

GENERAL_KEYWORDS = [
    "україн", "україні", "верховна рада", "кабмін",
    "війна", "зсу", "ппо", "енергетик", "пенсі",
    "субсид", "погода", "курс валют", "відключенн",
    "тривог", "подат", "мобілізац", "кордон", "блекаут"
]

TOPICS = [
    (r"війна|зсу|ппо|обстріл|ракет|дрон|шахед|тривог", "🚨", "#Війна"),
    (r"енергетик|відключенн|блекаут|світло|електро", "💡", "#Енергетика"),
    (r"погода|дощ|сніг|температур|заморозк|synoptic|синоптик", "🌦", "#Погода"),
    (r"пенсі|субсид|тариф|виплат", "💰", "#Соціальне"),
    (r"мобілізац|тцк|призов", "🪖", "#Мобілізація"),
    (r"курс валют|долар|євро|гривн", "💱", "#Фінанси"),
    (r"дтп|аварі|пожеж|поліці|затрим|злочин", "🚔", "#Події"),
    (r"лікарн|медик|вірус|грип|захворю|епідем|вакцин", "🏥", "#Здоровя"),
    (r"будівництв|девелопер|житл|ремонт доріг|дорог", "🏗", "#Будівництво"),
    (r"концерт|фестивал|виставк|театр|культур|свято", "🎭", "#Культура"),
    (r"школ|універс|освіт|студент|вчител", "🎓", "#Освіта"),
    (r"спорт|футбол|матч|чемпіонат|турнір", "⚽", "#Спорт"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KolomyiaNewsBot/3.0)"}

# ==================================================
# ЛОГУВАННЯ
# ==================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("kolomyia_news.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("kolomyia_news")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

# ==================================================
# БАЗА ДАНИХ
# ==================================================

def init_db():
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS posted_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                posted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posted_at ON posted_news(posted_at)"
        )
    logger.info("Базу даних готово.")

def cleanup_old_records():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        deleted = conn.execute(
            "DELETE FROM posted_news WHERE posted_at < ?", (cutoff,)
        ).rowcount
        if deleted:
            logger.info("Видалено старих записів: %s", deleted)

def is_posted(link: str) -> bool:
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        return conn.execute(
            "SELECT 1 FROM posted_news WHERE link = ?", (link,)
        ).fetchone() is not None

def is_similar_title(new_title: str) -> bool:
    new_lower = new_title.lower().strip()
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        rows = conn.execute(
            "SELECT title FROM posted_news ORDER BY id DESC LIMIT 120"
        ).fetchall()
    for (old_title,) in rows:
        if SequenceMatcher(None, new_lower, old_title.lower()).ratio() >= SIMILARITY_THRESHOLD:
            return True
    return False

def mark_posted(link: str, title: str):
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO posted_news (link, title) VALUES (?, ?)",
            (link, title),
        )

# ==================================================
# ОБРОБКА ТЕКСТУ ТА ЗОБРАЖЕНЬ
# ==================================================

def clean_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split()).strip()

def _is_image_url(url: str) -> bool:
    return bool(url) and re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I) is not None

def extract_image(entry):
    for media in getattr(entry, "media_content", []) or []:
        url = media.get("url")
        if _is_image_url(url) or "image" in media.get("type", ""):
            return url

    for thumb in getattr(entry, "media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]

    for enc in getattr(entry, "enclosures", []) or []:
        if "image" in enc.get("type", "") and enc.get("href"):
            return enc["href"]

    for link in getattr(entry, "links", []) or []:
        if "image" in link.get("type", "") and link.get("href"):
            return link["href"]

    blobs = [getattr(entry, "summary", "") or getattr(entry, "description", "")]
    for c in getattr(entry, "content", []) or []:
        blobs.append(c.get("value", ""))
    for blob in blobs:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', blob or "")
        if m and m.group(1).startswith("http"):
            return m.group(1)
    return None

def fetch_og_image(page_url: str):
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=(6, 12))
        r.raise_for_status()
        m = (
            re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, re.I)
            or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text, re.I)
        )
        if m:
            return html.unescape(m.group(1))
    except Exception as exc:
        logger.debug("og:image не знайдено для %s: %s", page_url, exc)
    return None

JUNK_PATTERNS = [
    r"(?i)читайте також.*",
    r"(?i)підписуйтесь.*",
    r"(?i)читати далі.*",
    r"(?i)допис .{0,200}з[’']явився (першим )?на.*",
    r"(?i)the post .{0,200}appeared first on.*",
    r"\[\s*(…|\.\.\.)\s*\]",
    r"(?i)\bфото:.*",
    r"(?i)\bвідео:.*",
    r"(?i)\bджерело:.*",
]

def get_description(entry) -> str:
    raw = getattr(entry, "summary", None) or getattr(entry, "description", None) or ""
    text = clean_html(raw)
    for pattern in JUNK_PATTERNS:
        text = re.sub(pattern, "", text)
    text = " ".join(text.split())

    sentences = re.split(r"(?<=[.!?…])\s+", text)
    result = ""
    for s in sentences:
        s = s.strip()
        if len(s) < 25:
            continue
        if re.match(r"(?i)^(про це|як повідомляє|нагадаємо|повідомляє|пише)", s):
            continue
        if re.search(r"(?i)(з посиланням на|за даними|повідомляє)\s*[….]*$", s):
            continue
        candidate = (result + " " + s).strip()
        if len(candidate) > MAX_DESCRIPTION:
            break
        result = candidate

    if not result and sentences:
        first = sentences[0].strip()
        if len(first) > MAX_DESCRIPTION:
            first = first[:MAX_DESCRIPTION].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
        result = first

    return result.strip()

def get_date(entry) -> float:
    parsed = getattr(entry, "published_parsed", None)
    if parsed:
        try:
            return mktime(parsed)
        except (OverflowError, ValueError):
            pass
    updated = getattr(entry, "updated", None) or getattr(entry, "published", None)
    if updated:
        try:
            return parsedate_to_datetime(updated).timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    return 0.0

def news_priority(title: str, description: str) -> int:
    text = (title + " " + description).lower()
    if any(w in text for w in LOCAL_KEYWORDS):
        return 2
    if any(w in text for w in GENERAL_KEYWORDS):
        return 1
    return 0

# ==================================================
# ОФОРМЛЕННЯ ПОСТА
# ==================================================

def source_name(feed_url: str) -> str:
    host = urlparse(feed_url).netloc
    return SOURCE_NAMES.get(host, host.replace("www.", ""))

def clean_link(url: str) -> str:
    return re.sub(r"[?&](utm_[^=]+|fbclid|gclid)=[^&]*", "", url).rstrip("?&")

def pick_topics(title: str, description: str):
    text = (title + " " + description).lower()
    emoji = "📰"
    tags = []
    for pattern, em, tag in TOPICS:
        if re.search(pattern, text):
            if not tags:
                emoji = em
            tags.append(tag)
    return emoji, tags[:3]

def geo_info(title: str, description: str, priority: int):
    text = (title + " " + description).lower()
    if "коломи" in text or "покутт" in text:
        return "📍 Коломия та Покуття", "#Коломия"
    if priority == 2:
        return "📍 Прикарпаття", "#Прикарпаття"
    return "🇺🇦 Україна", "#Україна"

def make_message(item: dict, limit: int):
    title = item["title"].strip()
    description = item["description"]
    emoji, topic_tags = pick_topics(title, description)
    geo_line, geo_tag = geo_info(title, description, item["priority"])

    tags = " ".join([geo_tag, *topic_tags, "#Новини"])
    footer = f"{geo_line}  •  <i>{html.escape(item['source'])}</i>"

    def build(desc: str) -> str:
        parts = [f"{emoji} <b>{html.escape(title)}</b>"]
        if desc:
            parts.append(f"<blockquote>{html.escape(desc)}</blockquote>")
        parts.append(footer)
        parts.append(tags)
        return "\n\n".join(parts)

    message = build(description)
    while len(message) > limit and description:
        description = description[: max(0, len(description) - 40)].rsplit(" ", 1)[0].rstrip(",;:—- ")
        if len(description) < 40:
            description = ""
        else:
            description += "…"
        message = build(description)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Читати повністю", url=clean_link(item["link"]))]
    ])
    return message, keyboard

# ==================================================
# ЗАВАНТАЖЕННЯ RSS ТА ПУБЛІКАЦІЯ
# ==================================================

def fetch_feed(url: str):
    response = requests.get(url, headers=HEADERS, timeout=(6, 18))
    response.raise_for_status()
    return feedparser.parse(response.content)

async def collect_candidates():
    candidates = []
    for url in RSS_URLS:
        logger.info("Перевіряю RSS: %s", url)
        try:
            feed = await asyncio.to_thread(fetch_feed, url)
        except Exception as exc:
            logger.warning("Не вдалося завантажити %s: %s", url, exc)
            continue

        if not feed.entries:
            logger.warning("Немає новин у стрічці: %s", url)
            continue

        entries = sorted(feed.entries, key=get_date, reverse=True)[:MAX_NEWS_PER_FEED]
        for entry in entries:
            link = getattr(entry, "link", "").strip()
            title = clean_html(getattr(entry, "title", "")).strip()
            if not link or not title:
                continue
            if is_posted(link) or is_similar_title(title):
                continue

            description = get_description(entry)
            priority = news_priority(title, description)
            if priority == 0:
                continue

            candidates.append({
                "link": link,
                "title": title,
                "description": description,
                "priority": priority,
                "date": get_date(entry),
                "image": extract_image(entry),
                "source": source_name(url),
            })

    candidates.sort(key=lambda x: (-x["priority"], -x["date"]))
    return candidates

async def send_post(item: dict):
    image = item["image"] or await asyncio.to_thread(fetch_og_image, item["link"])

    if image:
        message, keyboard = make_message(item, limit=1024)
        try:
            await bot.send_photo(
                chat_id=CHANNEL_ID, photo=image,
                caption=message, reply_markup=keyboard,
            )
            return
        except TelegramRetryAfter:
            raise
        except Exception as exc:
            logger.warning("Фото не відправилось (%s), публікую текстом", exc)

    message, keyboard = make_message(item, limit=4000)
    await bot.send_message(
        chat_id=CHANNEL_ID, text=message,
        reply_markup=keyboard, disable_web_page_preview=True,
    )

async def process_and_publish():
    candidates = await collect_candidates()
    if not candidates:
        logger.info("Нових релевантних новин немає.")
        return

    to_publish = candidates[:MAX_POSTS_PER_CYCLE]
    logger.info("Кандидатів: %s, публікуємо: %s", len(candidates), len(to_publish))

    for i, item in enumerate(to_publish):
        try:
            try:
                await send_post(item)
            except TelegramRetryAfter as exc:
                logger.warning("Ліміт Telegram. Чекаю %s сек.", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 2)
                await send_post(item)

            mark_posted(item["link"], item["title"])
            logger.info("Опубліковано [%s]: %s", item["priority"], item["title"])

            if i < len(to_publish) - 1:
                await asyncio.sleep(MIN_DELAY_BETWEEN_POSTS)
        except Exception:
            logger.exception("Помилка публікації: %s", item["title"])

# ==================================================
# ОСНОВНИЙ ЦИКЛ
# ==================================================

async def main():
    init_db()
    cleanup_old_records()
    try:
        me = await bot.get_me()
        logger.info("Бот запущено: @%s", me.username)

        while True:
            started = datetime.now(timezone.utc)
            logger.info("===== Початок перевірки новин =====")
            try:
                await process_and_publish()
            except Exception:
                logger.exception("Помилка в циклі обробки")

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            logger.info("Перевірку завершено за %.1f сек.", elapsed)
            await asyncio.sleep(max(15, CHECK_INTERVAL - elapsed))
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот зупинений.")