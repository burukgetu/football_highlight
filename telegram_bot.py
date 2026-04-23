import logging
import os
import re

import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from sqlalchemy import select
from dotenv import load_dotenv

from database import AsyncSessionLocal, Highlight

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

LEAGUE_NAMES = [
    "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
    "Champions League", "Europa League", "Conference League", "FA Cup",
    "Copa del Rey", "DFB Pokal", "Coppa Italia", "Carabao Cup",
    "World Cup", "Euro", "Nations League", "MLS", "Liga MX",
]


def extract_league(title: str, excerpt: str) -> str:
    """Extract league name from title or excerpt."""
    combined = f"{title} {excerpt}"
    for league in LEAGUE_NAMES:
        if league.lower() in combined.lower():
            return league
    return "Football Highlights"


def extract_keywords(title: str) -> list[str]:
    """Extract team names from title like 'Team A vs Team B Highlights...'"""
    # Strip trailing words like "Highlights and Goals", "Highlights", etc.
    clean = re.sub(r'\s+(highlights?|and goals?|goals?|match).*', '', title, flags=re.IGNORECASE).strip()
    # Split on "vs" (case-insensitive)
    parts = re.split(r'\s+vs\.?\s+', clean, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def make_hashtag(name: str) -> str:
    """Turn 'Crystal Palace' into #CrystalPalace"""
    return "#" + re.sub(r'\s+', '', name.title())


def build_message(highlight: Highlight) -> str:
    title = highlight.title
    league_type = extract_league(title, highlight.excerpt)
    keywords = extract_keywords(title)

    kw1 = keywords[0] if len(keywords) > 0 else title
    kw2 = keywords[1] if len(keywords) > 1 else kw1

    date_str = highlight.published_at.strftime("%d %B %Y")

    message = (
        f"⚽ <b>{title}</b>\n\n"
        f"🏆 {league_type}\n\n"
        f"🔴{make_hashtag(kw1)} 🆚 {make_hashtag(kw2)} 🔵\n\n"
        f"📅 {date_str}\n"
        f".\n"
    )
    return message


def build_keyboard(highlight: Highlight) -> InlineKeyboardMarkup:
    page_url = f"{BASE_URL}/highlight/{highlight.slug}"
    # Fallback to source URL if BASE_URL not configured yet
    if not BASE_URL or BASE_URL == "http://localhost:8000":
        page_url = highlight.source_url
    keyboard = [[InlineKeyboardButton("▶ Watch online", url=page_url)]]
    return InlineKeyboardMarkup(keyboard)


async def fetch_streamable_thumbnail(video_url: str) -> str:
    """
    Fetch a fresh signed thumbnail URL from Streamable oEmbed API.
    video_url is like https://streamable.com/e/9m78qi
    """
    # Normalise to https://streamable.com/{id}
    video_id = video_url.rstrip("/").split("/")[-1]
    oembed_url = f"https://api.streamable.com/oembed.json?url=https://streamable.com/{video_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(oembed_url)
            resp.raise_for_status()
            thumb = resp.json().get("thumbnail_url", "")
            # Add https: if protocol-relative
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            return thumb
    except Exception as e:
        logger.warning(f"Could not fetch Streamable thumbnail for {video_id}: {e}")
        return ""


async def send_highlight(highlight: Highlight, thumbnail: str = "") -> bool:
    """Send a single highlight to the Telegram channel."""
    if not BOT_TOKEN or not CHANNEL_ID:
        logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID not set.")
        return False

    bot = Bot(token=BOT_TOKEN)
    message_text = build_message(highlight)
    keyboard = build_keyboard(highlight)

    try:
        if thumbnail:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=thumbnail,
                caption=message_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=message_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        logger.info(f"Sent to Telegram: {highlight.title}")
        return True
    except TelegramError as e:
        logger.error(f"Telegram error for '{highlight.title}': {e}")
        return False


async def send_pending_highlights() -> int:
    """Find all highlights not yet sent and send them. Returns count sent."""
    sent_count = 0

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Highlight)
            .where(Highlight.sent_to_telegram == False)  # noqa: E712
            .order_by(Highlight.published_at.asc())
        )
        pending = result.scalars().all()

        for highlight in pending:
            # Fetch Streamable thumbnail before sending
            thumbnail = ""
            if highlight.video_url:
                thumbnail = await fetch_streamable_thumbnail(highlight.video_url)

            success = await send_highlight(highlight, thumbnail=thumbnail)
            if success:
                highlight.sent_to_telegram = True
                # Save the Streamable thumbnail so the site uses it too
                if thumbnail:
                    highlight.thumbnail_url = thumbnail
                await session.commit()
                sent_count += 1

    logger.info(f"Sent {sent_count} highlights to Telegram.")
    return sent_count
