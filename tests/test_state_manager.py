"""
Unit tests for SQLite state manager module.
"""

import pytest
from pathlib import Path
import tempfile
import gc

from screener.state_manager import (
    init_db,
    is_processed,
    mark_as_processed,
    get_unprocessed_batch,
    StateManager,
    get_db_connection
)
from screener.models import FeedItem


@pytest.fixture
def temp_state_db(tmp_path):
    db_file = tmp_path / "test_processed_news.db"
    init_db(db_file)
    yield db_file
    gc.collect()


def test_init_db_creates_table(temp_state_db):
    assert temp_state_db.exists()
    with get_db_connection(temp_state_db) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='processed_articles'")
        assert cursor.fetchone() is not None


def test_is_processed_and_mark_as_processed(temp_state_db):
    art_id = "https://newsclient.omxgroup.com/article/99999"
    title = "Faron Pharmaceuticals Oy: Q3 Report"
    pub_date = "2026-09-15T08:00:00Z"

    # Initially not processed
    assert is_processed(art_id, temp_state_db) is False

    # Mark as processed
    mark_as_processed(art_id, title, pub_date, temp_state_db)

    # Now it should be processed
    assert is_processed(art_id, temp_state_db) is True

    # Re-inserting (update) must not fail
    mark_as_processed(art_id, "Updated Title", pub_date, temp_state_db)
    assert is_processed(art_id, temp_state_db) is True


def test_batch_lookup(temp_state_db):
    mark_as_processed("id-1", "Title 1", db_path=temp_state_db)
    mark_as_processed("id-2", "Title 2", db_path=temp_state_db)

    processed_set = get_unprocessed_batch(["id-1", "id-2", "id-3"], temp_state_db)
    assert processed_set == {"id-1", "id-2"}


def test_state_manager_class_filtering(temp_state_db):
    sm = StateManager(temp_state_db)

    item1 = FeedItem(guid="item-1", title="Release 1", link="https://link1.com")
    item2 = FeedItem(guid="item-2", title="Release 2", link="https://link2.com")

    # Both items should pass initial filter
    new_items = sm.filter_new_items([item1, item2])
    assert len(new_items) == 2

    # Mark item1 as processed
    sm.mark_as_processed(item1.guid, item1.title)

    # Only item2 should pass subsequent filter
    filtered_items = sm.filter_new_items([item1, item2])
    assert len(filtered_items) == 1
    assert filtered_items[0].guid == "item-2"
