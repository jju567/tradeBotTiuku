"""
Unit tests for the Nasdaq OMX Helsinki stock screener scraper module and storage.
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import gc

from screener.config import ScreenerConfig
from screener.models import FeedItem, DocumentPayload, ScrapedRelease, ProcessingStatus
from screener.storage import ScreenerStorage
from screener.scraper_module import NasdaqHelsinkiScraper, PDFExtractor


SAMPLE_RSS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Nasdaq OMX Helsinki Releases</title>
    <link>https://newsclient.omxgroup.com</link>
    <description>Company Releases</description>
    <item>
      <title>Faron Pharmaceuticals Oy: Sisäpiiritieto: Positiiviset tulokset faasi II -tutkimuksessa</title>
      <link>https://newsclient.omxgroup.com/cds-public/view/article.action?id=123456</link>
      <guid>https://newsclient.omxgroup.com/cds-public/view/article.action?id=123456</guid>
      <pubDate>Mon, 15 Sep 2026 07:30:00 +0000</pubDate>
      <description>Faron tiedottaa merkittävästä edistysaskeleesta.</description>
      <category>Sisäpiiritieto</category>
    </item>
    <item>
      <title>[NIGHT] Nightingale Health Oyj: Osavuosikatsaus tammi-syyskuu 2026</title>
      <link>https://newsclient.omxgroup.com/cds-public/view/article.action?id=654321</link>
      <guid>https://newsclient.omxgroup.com/cds-public/view/article.action?id=654321</guid>
      <pubDate>Mon, 15 Sep 2026 08:00:00 +0000</pubDate>
      <description>Nightingale julkaisee Q3-raporttinsa.</description>
      <category>Osavuosikatsaus</category>
    </item>
  </channel>
</rss>
"""

SAMPLE_HTML_PAGE = """
<!DOCTYPE html>
<html>
<head><title>Release Page</title></head>
<body>
  <nav><a href="/">Home</a></nav>
  <div class="release-body">
    <h1>Faron Pharmaceuticals Oy: Positiiviset tulokset</h1>
    <p>Yhtiön kassa riittää nykyarvion mukaan seuraavaksi 24 kuukaudeksi ilman lisärahoitusta.</p>
    <p>Katso liitteenä oleva osavuosikatsaus raportti alla olevasta linkistä:</p>
    <a href="/reports/faron_q3_report.pdf">Lataa Q3 Raportti (PDF)</a>
  </div>
  <footer>Copyright 2026</footer>
</body>
</html>
"""


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    gc.collect()
    if db_path.exists():
        try:
            db_path.unlink(missing_ok=True)
        except PermissionError:
            pass


def test_rss_feed_parsing():
    scraper = NasdaqHelsinkiScraper()
    items = scraper.parse_rss_feed_content(SAMPLE_RSS_XML, "https://mockfeed.omxgroup.com")
    
    assert len(items) == 2
    
    item1 = items[0]
    assert item1.guid == "https://newsclient.omxgroup.com/cds-public/view/article.action?id=123456"
    assert "Faron" in item1.title
    assert item1.company_name == "Faron Pharmaceuticals Oy"
    assert item1.category == "Sisäpiiritieto"
    assert item1.published_at is not None
    assert item1.published_at.year == 2026
    
    item2 = items[1]
    assert item2.ticker == "NIGHT"
    assert item2.company_name == "Nightingale Health Oyj"


def test_html_parsing_and_pdf_discovery(monkeypatch):
    scraper = NasdaqHelsinkiScraper()
    
    # Mock requests.get for HTML fetching
    class MockResponse:
        def __init__(self, text, status_code=200):
            self.text = text
            self.status_code = status_code
        def raise_for_status(self):
            pass
            
    monkeypatch.setattr(scraper.session, "get", lambda url, **kwargs: MockResponse(SAMPLE_HTML_PAGE))
    
    payload = scraper.fetch_and_parse_document("https://newsclient.omxgroup.com/article/123")
    
    assert "Yhtiön kassa riittää nykyarvion mukaan" in payload.html_text
    assert "Home" not in payload.html_text  # Nav removed
    assert "Copyright" not in payload.html_text  # Footer removed
    assert len(payload.pdf_urls) == 1
    assert payload.pdf_urls[0] == "https://newsclient.omxgroup.com/reports/faron_q3_report.pdf"


def test_storage_deduplication(temp_db):
    storage = ScreenerStorage(temp_db)
    
    feed_item = FeedItem(
        guid="release-guid-001",
        title="Test Release 1",
        link="https://test.com/rel/1",
        published_at=datetime.now(timezone.utc),
        company_name="Test Oyj",
        ticker="TEST"
    )
    doc = DocumentPayload(
        url="https://test.com/rel/1",
        html_text="Test content text"
    )
    scraped = ScrapedRelease(feed_item=feed_item, document=doc)
    
    assert not storage.is_processed("release-guid-001")
    
    storage.record_scraped_release(scraped)
    
    assert storage.is_processed("release-guid-001")
    assert "release-guid-001" in storage.get_processed_ids(["release-guid-001", "other-id"])
    
    # Update status
    storage.update_status("release-guid-001", ProcessingStatus.QUANT_PASSED)
    
    # Cleanup test
    cleaned = storage.cleanup_old_records(days=1)
    assert cleaned == 0


def test_pdf_extractor_fallback():
    # Empty bytes test
    text, tables = PDFExtractor.extract_text_from_bytes(b"")
    assert text == ""
    assert tables == []
    
    # Non-empty corrupt bytes test (must not raise an unhandled exception)
    text, tables = PDFExtractor.extract_text_from_bytes(b"NOT_A_VALID_PDF_HEADER")
    assert text == ""
    assert tables == []
