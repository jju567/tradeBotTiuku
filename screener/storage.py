import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Set, Generator

from .models import FeedItem, DocumentPayload, ScrapedRelease, ProcessingStatus

logger = logging.getLogger(__name__)


class ScreenerStorage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create database tables and indices if not present."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_releases (
                    id TEXT PRIMARY KEY,
                    feed_url TEXT,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    company_name TEXT,
                    ticker TEXT,
                    category TEXT,
                    published_at TEXT,
                    scraped_at TEXT,
                    status TEXT NOT NULL,
                    has_pdf INTEGER DEFAULT 0,
                    text_length INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_releases_published_at
                ON processed_releases(published_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_releases_status
                ON processed_releases(status)
            """)
            conn.commit()

    def is_processed(self, guid: str) -> bool:
        """Check if a release ID has already been recorded."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM processed_releases WHERE id = ? LIMIT 1", (guid,))
            return cur.fetchone() is not None

    def get_processed_ids(self, guids: List[str]) -> Set[str]:
        """Bulk query to check existing GUIDs."""
        if not guids:
            return set()
        placeholders = ",".join("?" for _ in guids)
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT id FROM processed_releases WHERE id IN ({placeholders})",
                guids,
            )
            return {row["id"] for row in cur.fetchall()}

    def record_scraped_release(self, release: ScrapedRelease) -> None:
        """Insert or update a scraped release record."""
        feed = release.feed_item
        doc = release.document
        
        has_pdf = 1 if (doc.pdf_urls or doc.pdf_text.strip()) else 0
        text_len = len(doc.full_combined_text)
        
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO processed_releases (
                    id, feed_url, title, link, company_name, ticker, category,
                    published_at, scraped_at, status, has_pdf, text_length,
                    error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    error_message = excluded.error_message,
                    text_length = excluded.text_length,
                    has_pdf = excluded.has_pdf
            """, (
                feed.guid,
                feed.source_feed,
                feed.title,
                feed.link,
                feed.company_name,
                feed.ticker,
                feed.category,
                feed.published_at.isoformat() if feed.published_at else None,
                datetime.now(timezone.utc).isoformat(),
                release.status.value,
                has_pdf,
                text_len,
                doc.error_message,
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()

    def update_status(self, release_id: str, status: ProcessingStatus, error_message: Optional[str] = None) -> None:
        """Update processing status for a specific release."""
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE processed_releases
                SET status = ?, error_message = ?
                WHERE id = ?
            """, (status.value, error_message, release_id))
            conn.commit()

    def cleanup_old_records(self, days: int = 90) -> int:
        """Prune records older than specified retention period."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM processed_releases WHERE published_at < ?", (cutoff,))
            deleted = cur.rowcount
            conn.commit()
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old release records older than {days} days.")
            return deleted
