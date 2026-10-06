"""
Backend API - Phase 3c (async / job-queue version).

POST /search {"keyword": "..."} ->
    - checks the DB for a recent cached result for this keyword
      -> if found, returns it immediately with status="done"
    - if not cached, creates a "pending" row, schedules the actual scrape
      to run in the background, and returns immediately with the job id
      and status="pending" (does NOT wait for the scrape to finish)

GET /search/{job_id} ->
    - returns the current status/result of a previously created job, so
      the frontend can poll until status is "done" or "failed"

Run locally:
    uvicorn main:app --reload --port 9000
"""

import io
import logging
import os
import tempfile
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import db
from label_cropper import LabelCropError, crop_labels

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend-api")

SCRAPER_SERVICE_URL = os.environ["SCRAPER_SERVICE_URL"].rstrip("/")
CACHE_MAX_AGE_HOURS = 24
# This is now a BACKGROUND call, not something a visitor's browser waits
# on directly, so it's fine to be generous here - 112s scrapes shouldn't
# get cut off anymore.
SCRAPER_TIMEOUT_SECONDS = 180
LABEL_PDF_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB

app = FastAPI(title="Keyword Search Backend API")

# Only allow requests from our actual deployed frontend - replace with
# your real Netlify URL (no trailing slash).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-site-name-here.netlify.app"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-IP rate limiting - keeps one visitor from hammering /search and
# getting our scraper's IP flagged/blocked by Amazon.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SearchRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=200)


class TopProduct(BaseModel):
    title: str
    price: str | None = None
    total_sales_value: float
    image_url: str | None = None
    product_url: str | None = None


class SearchStartedResponse(BaseModel):
    job_id: int
    keyword: str
    status: str  # "pending" or "done" (done only on a cache hit)
    estimated_monthly_sales_value: float | None = None
    num_products_found: int | None = None
    top_products: list[TopProduct] | None = None
    suggested_keywords: list[str] | None = None
    cached: bool


class SearchStatusResponse(BaseModel):
    job_id: int
    keyword: str
    status: str  # "pending" | "done" | "failed"
    estimated_monthly_sales_value: float | None = None
    num_products_found: int | None = None
    top_products: list[TopProduct] | None = None
    suggested_keywords: list[str] | None = None
    error_message: str | None = None


class RecentSearchItem(BaseModel):
    keyword: str
    estimated_monthly_sales_value: float


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/recent-searches", response_model=list[RecentSearchItem])
def recent_searches():
    rows = db.get_recent_completed_searches(limit=4)
    return [
        RecentSearchItem(
            keyword=row["keyword"],
            estimated_monthly_sales_value=float(row["estimated_monthly_sales_value"]),
        )
        for row in rows
    ]


def run_scrape_job(search_id: int, keyword: str) -> None:
    """
    Background task: actually calls the scraper and updates the DB row.
    Runs AFTER the HTTP response has already been sent to the caller, so
    it can take as long as it needs without the visitor's browser waiting
    on this specific connection.
    """
    logger.info("Background job starting: search id=%s keyword=%r", search_id, keyword)
    try:
        resp = requests.post(
            f"{SCRAPER_SERVICE_URL}/scrape",
            json={"keyword": keyword},
            timeout=SCRAPER_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.exception("Background scrape failed for search id=%s keyword=%r", search_id, keyword)
        db.mark_search_failed(search_id, str(e))
        return

    db.mark_search_done(
        search_id,
        estimated_monthly_sales_value=data["estimated_monthly_sales_value"],
        num_products_found=data["num_products_found"],
        top_products=data["top_products"],
        suggested_keywords=data["suggested_keywords"],
    )
    logger.info("Background job done: search id=%s keyword=%r", search_id, keyword)


@app.post("/search", response_model=SearchStartedResponse)
@limiter.limit("5/minute")
def search(request: Request, req: SearchRequest, background_tasks: BackgroundTasks):
    keyword = req.keyword.strip().lower()
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword must not be empty")

    # 1. Cache check - return immediately, no job needed.
    cached_row = db.get_recent_search(keyword, max_age_hours=CACHE_MAX_AGE_HOURS)
    if cached_row:
        logger.info("Cache hit for keyword=%r (search id=%s)", keyword, cached_row["id"])
        return SearchStartedResponse(
            job_id=cached_row["id"],
            keyword=keyword,
            status="done",
            estimated_monthly_sales_value=float(cached_row["estimated_monthly_sales_value"]),
            num_products_found=cached_row["num_products_found"],
            top_products=cached_row["top_products"],
            suggested_keywords=cached_row["suggested_keywords"],
            cached=True,
        )

    # 2. No fresh cache - create the pending row, schedule the scrape to
    # run in the background, and return right away with the job id.
    search_id = db.create_pending_search(keyword)
    background_tasks.add_task(run_scrape_job, search_id, keyword)
    logger.info("Created pending search id=%s for keyword=%r (job scheduled)", search_id, keyword)

    return SearchStartedResponse(
        job_id=search_id,
        keyword=keyword,
        status="pending",
        cached=False,
    )


@app.get("/search/{job_id}", response_model=SearchStatusResponse)
def get_search_status(job_id: int):
    row = db.get_search_by_id(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No search found with id {job_id}")

    return SearchStatusResponse(
        job_id=row["id"],
        keyword=row["keyword"],
        status=row["status"],
        estimated_monthly_sales_value=(
            float(row["estimated_monthly_sales_value"])
            if row["estimated_monthly_sales_value"] is not None
            else None
        ),
        num_products_found=row["num_products_found"],
        top_products=row["top_products"],
        suggested_keywords=row["suggested_keywords"],
        error_message=row["error_message"],
    )


@app.post("/crop-labels")
@limiter.limit("10/minute")
async def crop_labels_endpoint(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form(...),
):
    """
    Accepts a single Flipkart order PDF + a mode ("thermal" | "a4" | "both"),
    crops the shipping labels out of it, and returns a ZIP containing the
    cropped label PDF(s) plus the three packing-list CSVs. Nothing is
    stored server-side - everything happens in a temp directory that's
    deleted automatically once the response is built, since these PDFs
    contain customer names/addresses.
    """
    mode = mode.lower().strip()
    if mode not in ("thermal", "a4", "both"):
        raise HTTPException(status_code=400, detail='mode must be "thermal", "a4", or "both"')

    # Validate it's actually a PDF - check both the declared content type
    # and the file's own magic bytes (a renamed .txt could still claim to
    # be a PDF via content_type, but it won't start with %PDF).
    if file.content_type not in ("application/pdf", "application/octet-stream") and not (
        file.filename or ""
    ).lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    contents = await file.read()

    if len(contents) > LABEL_PDF_MAX_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 20 MB)")

    if not contents.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="This doesn't look like a valid PDF file")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        pdf_path = tmp_path / "input.pdf"
        pdf_path.write_bytes(contents)

        try:
            output_files = crop_labels(pdf_path, mode, tmp_path)
        except LabelCropError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            logger.exception("Label cropping failed")
            raise HTTPException(status_code=500, detail=f"Processing failed: {e}")

        # Build the ZIP in memory while the temp dir (and its files) still
        # exist; everything on disk is deleted the moment this `with`
        # block exits, right after this.
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for output_path in output_files:
                zf.write(output_path, arcname=output_path.name)
        zip_buffer.seek(0)

    logger.info("Label cropping done: mode=%r, %d output file(s)", mode, len(output_files))

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=cropped_labels.zip"},
    )