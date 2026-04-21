import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv

from database import init_db, get_db, Highlight

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
PAGE_SIZE = 12

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized.")
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = 1, db: AsyncSession = Depends(get_db)):
    offset = (page - 1) * PAGE_SIZE

    result = await db.execute(
        select(Highlight)
        .order_by(Highlight.published_at.desc())
        .limit(PAGE_SIZE)
        .offset(offset)
    )
    highlights = result.scalars().all()

    total = await db.scalar(select(func.count(Highlight.id)))
    has_next = (offset + PAGE_SIZE) < (total or 0)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "highlights": highlights,
            "page": page,
            "has_next": has_next,
        },
    )


@app.get("/highlight/{slug}", response_class=HTMLResponse)
async def highlight_page(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Highlight).where(Highlight.slug == slug))
    highlight = result.scalar_one_or_none()

    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")

    return templates.TemplateResponse(
        "highlight.html",
        {
            "request": request,
            "highlight": highlight,
            "base_url": BASE_URL,
        },
    )


@app.get("/api/highlights")
async def api_highlights(page: int = 1, db: AsyncSession = Depends(get_db)):
    """Simple JSON API for highlights."""
    offset = (page - 1) * PAGE_SIZE
    result = await db.execute(
        select(Highlight).order_by(Highlight.published_at.desc()).limit(PAGE_SIZE).offset(offset)
    )
    highlights = result.scalars().all()
    return [
        {
            "id": h.id,
            "title": h.title,
            "slug": h.slug,
            "excerpt": h.excerpt,
            "thumbnail_url": h.thumbnail_url,
            "video_url": h.video_url,
            "published_at": h.published_at.isoformat(),
            "url": f"{BASE_URL}/highlight/{h.slug}",
        }
        for h in highlights
    ]
