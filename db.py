"""
Database helper functions for the backend API.

Wraps all Postgres access needed for the synchronous /search flow:
- checking for a recent cached result
- creating a pending row before calling the scraper
- marking a row done (with result) or failed (with error)
"""

import os

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def get_recent_search(keyword: str, max_age_hours: int = 24):
    """
    Return the most recent successfully completed search row for this
    keyword if it finished within the last `max_age_hours`, else None.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, keyword, estimated_monthly_sales_value, num_products_found,
                       top_products, suggested_keywords, created_at, completed_at
                FROM searches
                WHERE keyword = %s
                  AND status = 'done'
                  AND completed_at > now() - (%s || ' hours')::interval
                ORDER BY completed_at DESC
                LIMIT 1;
                """,
                (keyword, max_age_hours),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_recent_completed_searches(limit: int = 4):
    """
    Return the most recent DISTINCT keywords that completed successfully,
    site-wide (across all visitors), most recent first. If the same
    keyword was searched multiple times, only its latest result counts
    once, so the list doesn't get clogged with repeats.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT keyword, estimated_monthly_sales_value, completed_at
                FROM (
                    SELECT DISTINCT ON (keyword)
                        keyword, estimated_monthly_sales_value, completed_at
                    FROM searches
                    WHERE status = 'done'
                    ORDER BY keyword, completed_at DESC
                ) latest_per_keyword
                ORDER BY completed_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_search_by_id(search_id: int):
    """Return the full row for a given search id, or None if it doesn't exist."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, keyword, status, estimated_monthly_sales_value,
                       num_products_found, top_products, suggested_keywords,
                       error_message, created_at, completed_at
                FROM searches
                WHERE id = %s;
                """,
                (search_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def create_pending_search(keyword: str) -> int:
    """Insert a new row with status='pending' and return its id."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO searches (keyword, status) VALUES (%s, 'pending') RETURNING id;",
                (keyword,),
            )
            search_id = cur.fetchone()[0]
            conn.commit()
            return search_id
    finally:
        conn.close()


def mark_search_done(
    search_id: int,
    estimated_monthly_sales_value: float,
    num_products_found: int,
    top_products: list,
    suggested_keywords: list,
) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE searches
                SET status = 'done',
                    estimated_monthly_sales_value = %s,
                    num_products_found = %s,
                    top_products = %s,
                    suggested_keywords = %s,
                    completed_at = now()
                WHERE id = %s;
                """,
                (
                    estimated_monthly_sales_value,
                    num_products_found,
                    Json(top_products),
                    Json(suggested_keywords),
                    search_id,
                ),
            )
            conn.commit()
    finally:
        conn.close()


def mark_search_failed(search_id: int, error_message: str) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE searches
                SET status = 'failed',
                    error_message = %s,
                    completed_at = now()
                WHERE id = %s;
                """,
                (error_message, search_id),
            )
            conn.commit()
    finally:
        conn.close()
