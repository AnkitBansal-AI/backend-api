"""
Database helper functions for the backend API.

Wraps all Postgres access needed for the synchronous /search flow:
- checking for a recent cached result
- creating a pending row before calling the scraper
- marking a row done (with result) or failed (with error)
"""

import os

import psycopg2
from psycopg2.extras import RealDictCursor
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
                       created_at, completed_at
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


def get_search_by_id(search_id: int):
    """Return the full row for a given search id, or None if it doesn't exist."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, keyword, status, estimated_monthly_sales_value,
                       num_products_found, error_message, created_at, completed_at
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


def mark_search_done(search_id: int, estimated_monthly_sales_value: float, num_products_found: int) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE searches
                SET status = 'done',
                    estimated_monthly_sales_value = %s,
                    num_products_found = %s,
                    completed_at = now()
                WHERE id = %s;
                """,
                (estimated_monthly_sales_value, num_products_found, search_id),
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
