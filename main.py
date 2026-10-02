"""
Backend API - Phase 3, Option 1 (synchronous).

POST /search {"keyword": "..."} ->
    - checks the DB for a recent cached result for this keyword
    - if none found, calls the scraper microservice and waits for it
    - stores the outcome (success or failure) in the `searches` table
    - returns the result to the caller

Run locally:
    uvicorn main:app --reload --port 9000
"""

import logging
import os

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import db

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend-api")

SCRAPER_SERVICE_URL = os.environ["SCRAPER_SERVICE_URL"].rstrip("/")
CACHE_MAX_AGE_HOURS = 24
SCRAPER_TIMEOUT_SECONDS = 60

app = FastAPI(title="Keyword Search Backend API")

# Only allow requests from our actual deployed frontend - replace with
# your real Netlify URL (no trailing slash). This replaces the earlier
# wildcard ("*"), which allowed ANY website to call this API from a
# visitor's browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://oxycommerce.netlify.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-IP rate limiting - protects both our Railway bill and, more
# importantly, keeps one visitor from hammering /search and getting our
# scraper's IP flagged/blocked by Amazon.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SearchRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=200)


class SearchResponse(BaseModel):
    keyword: str
    estimated_monthly_sales_value: float
    num_products_found: int
    cached: bool


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/search", response_model=SearchResponse)
@limiter.limit("5/minute")
def search(request: Request, req: SearchRequest):
    # Normalize so "Wireless Mouse" and "wireless mouse" share one cache entry.
    keyword = req.keyword.strip().lower()
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword must not be empty")

    # 1. Cache check - avoid re-scraping the same keyword too often.
    cached_row = db.get_recent_search(keyword, max_age_hours=CACHE_MAX_AGE_HOURS)
    if cached_row:
        logger.info("Cache hit for keyword=%r (search id=%s)", keyword, cached_row["id"])
        return SearchResponse(
            keyword=keyword,
            estimated_monthly_sales_value=float(cached_row["estimated_monthly_sales_value"]),
            num_products_found=cached_row["num_products_found"],
            cached=True,
        )

    # 2. No fresh cache - log a pending row, then call the scraper and wait.
    search_id = db.create_pending_search(keyword)
    logger.info("Created pending search id=%s for keyword=%r", search_id, keyword)

    try:
        resp = requests.post(
            f"{SCRAPER_SERVICE_URL}/scrape",
            json={"keyword": keyword},
            timeout=SCRAPER_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.exception("Scraper call failed for keyword=%r", keyword)
        db.mark_search_failed(search_id, str(e))
        raise HTTPException(status_code=503, detail=f"Scraper call failed: {e}")

    # 3. Save the result and return it.
    db.mark_search_done(
        search_id,
        estimated_monthly_sales_value=data["estimated_monthly_sales_value"],
        num_products_found=data["num_products_found"],
    )
    logger.info("Search id=%s done for keyword=%r", search_id, keyword)

    return SearchResponse(
        keyword=keyword,
        estimated_monthly_sales_value=data["estimated_monthly_sales_value"],
        num_products_found=data["num_products_found"],
        cached=False,
    )
