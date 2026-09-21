"""
Lightweight SQLite State Management for Nasdaq Helsinki Stock Screener.

Tracks processed press releases and interim reports in `data/processed_news.db`
to avoid duplicate network fetches, LLM API calls, and repeated alerts.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Set, Generator, Any

logger = logging.getLogger(__name__)

# Default SQLite database path
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "processed_news.db"


@contextmanager
def get_db_connection(db_path: Path | str = DEFAULT_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager providing a safe SQLite connection with automatic
    commit and guaranteed closing of database handles.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error on {path}: {e}")
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """
    Initializes the SQLite database and ensures the `processed_articles` and `sent_alerts` tables
    and required indices exist.
    """
    with get_db_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_articles (
                article_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                published_date TEXT,
                processed_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_published_date 
            ON processed_articles(published_date)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed_at 
            ON processed_articles(processed_at)
        """)

        # Table to track dispatched email alerts and prevent spam duplicates
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                title TEXT NOT NULL,
                sent_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_alerts_ticker_type
            ON sent_alerts(ticker, alert_type)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_alerts_sent_at
            ON sent_alerts(sent_at)
        """)
    logger.debug(f"Initialized state database at: {db_path}")


def has_alert_been_sent(
    ticker: str,
    alert_type: str = "SATELLITE",
    cooldown_hours: float = 24.0,
    db_path: Path | str = DEFAULT_DB_PATH
) -> bool:
    """
    Checks whether an email alert of type `alert_type` for `ticker` has already been sent
    within the specified cooldown window (default 24 hours).
    
    :param ticker: Stock ticker (e.g. 'FARON.HE', 'RAUTE.HE').
    :param alert_type: Alert category ('SATELLITE', 'CORE', 'TURNAROUND', 'SELL').
    :param cooldown_hours: Cooldown window in hours (default 24.0).
    :param db_path: Path to SQLite database.
    :return: True if an alert was already dispatched within cooldown, False otherwise.
    """
    if not ticker:
        return False

    clean_ticker = ticker.strip().upper()
    # Normalize base ticker (e.g. 'FARON' and 'FARON.HE' match)
    base_ticker = clean_ticker.split(".")[0]

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sent_at FROM sent_alerts 
            WHERE (ticker = ? OR ticker = ? OR ticker LIKE ?) AND alert_type = ?
            ORDER BY sent_at DESC LIMIT 1
        """, (clean_ticker, base_ticker, f"{base_ticker}.%", alert_type.upper()))
        row = cursor.fetchone()
        if not row:
            return False

        try:
            last_sent_str = row["sent_at"]
            if last_sent_str.endswith("Z"):
                last_sent_str = last_sent_str[:-1] + "+00:00"
            last_sent = datetime.fromisoformat(last_sent_str)
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            elapsed_seconds = (now - last_sent).total_seconds()
            return elapsed_seconds < (cooldown_hours * 3600)
        except Exception as e:
            logger.debug(f"Error parsing sent_at timestamp: {e}")
            return True


def mark_alert_sent(
    ticker: str,
    alert_type: str,
    title: str = "",
    db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """
    Records an outgoing email alert into SQLite `sent_alerts` to prevent duplicate emails.
    """
    if not ticker:
        return
    clean_ticker = ticker.strip().upper()
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO sent_alerts (ticker, alert_type, title, sent_at)
            VALUES (?, ?, ?, ?)
        """, (clean_ticker, alert_type.upper(), title.strip()[:200], now_iso))
    logger.info(f"Recorded sent alert for [{clean_ticker}] ({alert_type.upper()}) at {now_iso}")


def is_processed(article_id: str, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    """
    Check if an article ID (URL or hash) already exists in the database.
    
    :param article_id: Unique identifier of the news release (URL or GUID).
    :param db_path: Path to the SQLite database.
    :return: True if the article has already been processed, False otherwise.
    """
    if not article_id:
        return False

    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM processed_articles WHERE article_id = ? LIMIT 1",
            (str(article_id).strip(),)
        )
        return cursor.fetchone() is not None


def mark_as_processed(
    article_id: str,
    title: str,
    published_date: Optional[str] = None,
    db_path: Path | str = DEFAULT_DB_PATH
) -> None:
    """
    Inserts or updates an article record in the database after successful processing.
    
    :param article_id: Unique identifier of the news release.
    :param title: Headline or title of the release.
    :param published_date: Release publication timestamp (ISO 8601 or raw string).
    :param db_path: Path to the SQLite database.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    pub_iso = published_date or now_iso

    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO processed_articles (article_id, title, published_date, processed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                title = excluded.title,
                published_date = excluded.published_date,
                processed_at = excluded.processed_at
        """, (
            str(article_id).strip(),
            str(title).strip(),
            str(pub_iso),
            now_iso
        ))
    logger.info(f"Marked article as processed: [{article_id}] {title[:60]}")


def get_unprocessed_batch(
    article_ids: List[str],
    db_path: Path | str = DEFAULT_DB_PATH
) -> Set[str]:
    """
    Efficient batch helper to find already processed IDs in a single query.
    
    :return: Set of article IDs that are already present in the database.
    """
    if not article_ids:
        return set()

    placeholders = ",".join("?" for _ in article_ids)
    with get_db_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT article_id FROM processed_articles WHERE article_id IN ({placeholders})",
            article_ids
        )
        return {row["article_id"] for row in cursor.fetchall()}


class StateManager:
    """Object-Oriented wrapper around state database operations."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        init_db(self.db_path)

    def is_processed(self, article_id: str) -> bool:
        return is_processed(article_id, self.db_path)

    def mark_as_processed(self, article_id: str, title: str, published_date: Optional[str] = None) -> None:
        mark_as_processed(article_id, title, published_date, self.db_path)

    def filter_new_items(self, items: List[Any]) -> List[Any]:
        """
        Filter a list of items (FeedItem objects or dicts) and return only those
        not yet processed in the database.
        """
        if not items:
            return []

        # Extract IDs
        def get_id(item):
            if hasattr(item, "guid"):
                return item.guid
            elif hasattr(item, "link"):
                return item.link
            elif isinstance(item, dict):
                return item.get("guid") or item.get("link") or item.get("article_id")
            return str(item)

        ids = [get_id(it) for it in items]
        already_processed = get_unprocessed_batch(ids, self.db_path)

        new_items = [it for it in items if get_id(it) not in already_processed]
        logger.info(f"StateManager filtered {len(items)} items -> {len(new_items)} new, {len(already_processed)} skipped.")
        return new_items


# Auto-initialize database on import
try:
    init_db(DEFAULT_DB_PATH)
except Exception as e:
    logger.warning(f"Could not auto-initialize state database on import: {e}")
