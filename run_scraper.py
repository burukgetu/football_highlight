"""
Standalone scrape cycle — called by GitHub Actions every 30 minutes.
No web server or scheduler needed here.
"""
import asyncio
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    from database import init_db
    from scraper import scrape_and_store, fetch_latest_posts
    from telegram_bot import send_pending_highlights

    logger.info("=== Scrape cycle starting ===")

    # Print env check (no secret values, just whether they're set)
    logger.info(f"DB URL set     : {'yes' if os.getenv('DATABASE_URL') else 'NO - MISSING'}")
    logger.info(f"BOT TOKEN set  : {'yes' if os.getenv('TELEGRAM_BOT_TOKEN') else 'NO - MISSING'}")
    logger.info(f"CHANNEL ID set : {'yes' if os.getenv('TELEGRAM_CHANNEL_ID') else 'NO - MISSING'}")
    logger.info(f"BASE URL set   : {os.getenv('BASE_URL', 'NOT SET - using fallback')}")

    logger.info("Initialising database...")
    await init_db()
    logger.info("Database ready.")

    # Quick API check
    logger.info("Fetching posts from WP API...")
    posts = await fetch_latest_posts()
    logger.info(f"API returned {len(posts)} posts")

    new = await scrape_and_store()
    logger.info(f"New highlights saved: {new}")

    if new > 0:
        sent = await send_pending_highlights()
        logger.info(f"Sent to Telegram: {sent}")
    else:
        logger.info("No new highlights to send.")

    logger.info("=== Scrape cycle done ===")


if __name__ == "__main__":
    asyncio.run(main())
