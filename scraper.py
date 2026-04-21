import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from dotenv import load_dotenv

from database import AsyncSessionLocal, Highlight

load_dotenv()

logger = logging.getLogger(__name__)

WP_API_URL = os.getenv("WP_API_URL", "https://dasfootball.com/wp-json/wp/v2/posts")
POSTS_PER_PAGE = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_slug(url: str) -> str:
    path = urlparse(url).path.strip("/")
    return path.split("/")[-1] if path else url


def parse_date(date_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(date_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def extract_streamable_id(html_content: str) -> str:
    """
    Extract a Streamable video ID from WordPress post HTML content.
    Handles CDN URLs and embed URLs:
      - https://cdn-cf-east.streamable.com/video/mp4/9m78qi.mp4?...
      - https://streamable.com/e/9m78qi
      - https://streamable.com/9m78qi
    Returns the embed URL or empty string.
    """
    patterns = [
        # CDN signed URL — handles both / and JSON-escaped \/
        r'streamable\.com(?:\\/|/)video(?:\\/|/)mp4(?:\\/|/)([a-zA-Z0-9]+)\.mp4',
        # Embed URL
        r'streamable\.com(?:\\/|/)e(?:\\/|/)([a-zA-Z0-9]+)',
        # Plain URL
        r'streamable\.com(?:\\/|/)([a-zA-Z0-9]{5,8})[^a-zA-Z0-9]',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_content)
        if match:
            video_id = match.group(1)
            return f"https://streamable.com/e/{video_id}"
    return ""


def extract_og_image(html: str) -> str:
    """Extract og:image URL from raw HTML."""
    match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if match:
        return match.group(1)
    # alternate attribute order
    match = re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        html,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

async def fetch_latest_posts(page: int = 1) -> list[dict]:
    """Fetch posts from WordPress REST API including rendered content."""
    params = {
        "per_page": POSTS_PER_PAGE,
        "page": page,
        "_fields": "id,title,link,date,excerpt,content",
        "orderby": "date",
        "order": "desc",
    }
    async with httpx.AsyncClient(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        try:
            resp = await client.get(WP_API_URL, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch posts: {e}")
            return []


async def fetch_thumbnail(post_url: str) -> str:
    """
    Fetch og:image from the post page HTML (static, no JS needed).
    Much faster than Playwright.
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(post_url)
            resp.raise_for_status()
            # Only need the <head> section, first 8KB is enough
            head_html = resp.text[:8000]
            return extract_og_image(head_html)
        except Exception as e:
            logger.warning(f"Could not fetch thumbnail for {post_url}: {e}")
            return ""


# ---------------------------------------------------------------------------
# Main scrape cycle
# ---------------------------------------------------------------------------

async def scrape_and_store() -> int:
    """
    Fetch new posts via WordPress REST API, extract video + thumbnail,
    store in DB. Returns count of new highlights saved.
    """
    logger.info("Starting scrape cycle...")
    posts = await fetch_latest_posts()
    if not posts:
        logger.info("No posts returned from API.")
        return 0

    new_count = 0

    async with AsyncSessionLocal() as session:
        for post in posts:
            source_id = post.get("id")
            if not source_id:
                continue

            # Skip if already stored
            existing = await session.scalar(
                select(Highlight).where(Highlight.source_id == source_id)
            )
            if existing:
                continue

            # Title
            title_raw = post.get("title", {})
            title = title_raw.get("rendered", "") if isinstance(title_raw, dict) else str(title_raw)
            title = re.sub(r"<[^>]+>", "", title).replace("&#8211;", "-").replace("&amp;", "&").strip()

            # Excerpt
            excerpt_raw = post.get("excerpt", {})
            excerpt = excerpt_raw.get("rendered", "") if isinstance(excerpt_raw, dict) else str(excerpt_raw)
            excerpt = re.sub(r"<[^>]+>", "", excerpt).strip()

            post_url = post.get("link", "")
            slug = extract_slug(post_url)
            published_at = parse_date(post.get("date", ""))

            # Extract video from post content (no browser needed!)
            content_raw = post.get("content", {})
            content_html = content_raw.get("rendered", "") if isinstance(content_raw, dict) else str(content_raw)
            video_url = extract_streamable_id(content_html)

            # Fetch thumbnail from og:image (simple HTTP, no JS)
            thumbnail_url = await fetch_thumbnail(post_url)

            logger.info(
                f"New: '{title}' | video={'yes' if video_url else 'NO'} | thumb={'yes' if thumbnail_url else 'NO'}"
            )

            highlight = Highlight(
                source_id=source_id,
                title=title,
                slug=slug,
                source_url=post_url,
                excerpt=excerpt,
                video_url=video_url,
                thumbnail_url=thumbnail_url,
                published_at=published_at,
                sent_to_telegram=False,
            )
            session.add(highlight)
            await session.commit()
            new_count += 1

    logger.info(f"Scrape done — {new_count} new highlights saved.")
    return new_count
