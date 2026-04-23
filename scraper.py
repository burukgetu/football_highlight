import logging
import os
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from dotenv import load_dotenv

from database import AsyncSessionLocal, Highlight

load_dotenv()

logger = logging.getLogger(__name__)

WP_API_URL = os.getenv("WP_API_URL", "https://dasfootball.com/wp-json/wp/v2/posts")
POSTS_PER_PAGE = 10

# Generic site-wide fallback image — not a real match thumbnail
GENERIC_THUMBNAILS = {
    "https://dasfootball.com/wp-content/uploads/2025/01/Dasfootball-Social.webp",
}

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


async def fetch_post_content(post_url: str) -> str:
    """Fetch rendered HTML of a post page to re-extract video URL."""
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(post_url)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.warning(f"Could not fetch content for {post_url}: {e}")
            return ""


async def fetch_streamable_thumbnail(video_url: str) -> str:
    """Fetch the actual video thumbnail from Streamable oEmbed API."""
    video_id = video_url.rstrip("/").split("/")[-1]
    oembed_url = f"https://api.streamable.com/oembed.json?url=https://streamable.com/{video_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(oembed_url)
            resp.raise_for_status()
            thumb = resp.json().get("thumbnail_url", "")
            if thumb.startswith("//"):
                thumb = "https:" + thumb
            return thumb
    except Exception as e:
        logger.warning(f"Could not fetch Streamable thumbnail for {video_id}: {e}")
        return ""


async def fetch_thumbnail(post_url: str) -> str:
    """
    Fetch og:image from the post page. Returns empty string if it's
    the generic site fallback image.
    """
    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(post_url)
            resp.raise_for_status()
            head_html = resp.text[:8000]
            url = extract_og_image(head_html)
            if url in GENERIC_THUMBNAILS:
                return ""
            return url
        except Exception as e:
            logger.warning(f"Could not fetch thumbnail for {post_url}: {e}")
            return ""


async def resolve_thumbnail(video_url: str, post_url: str) -> str:
    """
    Get the best available thumbnail:
    1. Streamable oEmbed (actual match image) if video URL exists
    2. og:image from post page as fallback
    """
    if video_url:
        thumb = await fetch_streamable_thumbnail(video_url)
        if thumb:
            return thumb
    return await fetch_thumbnail(post_url)


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

            # Extract video from post content
            content_raw = post.get("content", {})
            content_html = content_raw.get("rendered", "") if isinstance(content_raw, dict) else str(content_raw)
            video_url = extract_streamable_id(content_html)

            # Update existing record if video URL changed or thumbnail missing
            existing = await session.scalar(
                select(Highlight).where(Highlight.source_id == source_id)
            )
            if existing:
                updated = False
                if video_url and existing.video_url != video_url:
                    logger.info(f"Video URL updated for '{existing.title}'")
                    existing.video_url = video_url
                    updated = True
                thumbnail_url = await resolve_thumbnail(video_url, post_url)
                if thumbnail_url and existing.thumbnail_url != thumbnail_url:
                    logger.info(f"Thumbnail updated for '{existing.title}'")
                    existing.thumbnail_url = thumbnail_url
                    updated = True
                if updated:
                    await session.commit()
                continue

            # Fetch thumbnail for new highlight
            thumbnail_url = await resolve_thumbnail(video_url, post_url)

            if not thumbnail_url:
                logger.info(f"Skipping '{title}' — no thumbnail yet, will retry next cycle")
                continue

            logger.info(
                f"New: '{title}' | video={'yes' if video_url else 'NO'} | thumb=yes"
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

    # Re-check recent highlights (last 7 days) that weren't in the API page,
    # to catch video URL changes or late-appearing videos.
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Highlight).where(Highlight.published_at >= cutoff)
        )
        recent = result.scalars().all()
        # Only re-check ones not already handled in the API loop above
        api_ids = {p.get("id") for p in posts}
        for highlight in recent:
            if highlight.source_id in api_ids:
                continue
            try:
                html = await fetch_post_content(highlight.source_url)
                video_url = extract_streamable_id(html)
                updated = False
                if video_url and highlight.video_url != video_url:
                    logger.info(f"Video URL updated for '{highlight.title}'")
                    highlight.video_url = video_url
                    updated = True
                video_url_for_thumb = video_url or highlight.video_url
                og_thumb = extract_og_image(html[:8000])
                if og_thumb in GENERIC_THUMBNAILS:
                    og_thumb = ""
                thumbnail_url = ""
                if video_url_for_thumb:
                    thumbnail_url = await fetch_streamable_thumbnail(video_url_for_thumb)
                if not thumbnail_url:
                    thumbnail_url = og_thumb
                if thumbnail_url and highlight.thumbnail_url != thumbnail_url:
                    logger.info(f"Thumbnail updated for '{highlight.title}'")
                    highlight.thumbnail_url = thumbnail_url
                    updated = True
                if updated:
                    await session.commit()
            except Exception as e:
                logger.warning(f"Could not re-check '{highlight.title}': {e}")

    logger.info(f"Scrape done — {new_count} new highlights saved.")
    return new_count
