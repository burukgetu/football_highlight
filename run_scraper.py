"""
Standalone scrape cycle — called by GitHub Actions every 30 minutes.
No web server or scheduler needed here.
"""
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    from database import init_db
    from scraper import scrape_and_store
    from telegram_bot import send_pending_highlights

    logger.info("=== Scrape cycle starting ===")
    await init_db()

    new = await scrape_and_store()
    logger.info(f"New highlights found: {new}")

    if new > 0:
        sent = await send_pending_highlights()
        logger.info(f"Sent to Telegram: {sent}")
    else:
        logger.info("Nothing new to send.")

    logger.info("=== Scrape cycle done ===")


if __name__ == "__main__":
    asyncio.run(main())
