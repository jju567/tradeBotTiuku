"""
Data Ingestion Scraper Module for Nasdaq OMX Helsinki Small Cap & First North.

Handles:
- Ingestion of Nasdaq CDS and GlobeNewswire RSS feeds.
- Parsing and sanitization of HTML press releases.
- Discovery and text extraction from attached PDF financial reports (pdfplumber/pypdf/PyPDF2).
- Async fetching, rate limiting, connection retries, and comprehensive error containment.
"""

import asyncio
import email.utils
import hashlib
import io
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Dict, Any

import html
import warnings
import requests
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

from .config import ScreenerConfig
from .models import FeedItem, DocumentPayload, ScrapedRelease, ProcessingStatus

logger = logging.getLogger(__name__)


class PDFExtractor:
    """Multi-backend PDF text extractor with graceful fallbacks."""

    @staticmethod
    def extract_text_from_bytes(pdf_bytes: bytes, max_chars: int = 100_000) -> Tuple[str, List[List[List[str]]]]:
        """
        Attempts to extract text and tables from raw PDF bytes.
        Tries pdfplumber first (for rich layout & tables), falling back to pypdf / PyPDF2.
        """
        if not pdf_bytes:
            return "", []

        extracted_text = ""
        extracted_tables = []

        # 1. Try pdfplumber
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages_text = []
                for idx, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text:
                        pages_text.append(f"--- Page {idx + 1} ---\n{text}")
                    tables = page.extract_tables() or []
                    for tbl in tables:
                        # Clean table rows
                        cleaned_tbl = [[str(cell or "").strip() for cell in row] for row in tbl if any(row)]
                        if cleaned_tbl:
                            extracted_tables.append(cleaned_tbl)
                    
                    if sum(len(p) for p in pages_text) >= max_chars:
                        break
                extracted_text = "\n\n".join(pages_text)
                if extracted_text.strip():
                    return extracted_text[:max_chars], extracted_tables
        except ImportError:
            logger.debug("pdfplumber not available, trying pypdf/PyPDF2.")
        except Exception as e:
            logger.warning(f"pdfplumber failed extraction: {e}, falling back to pypdf.")

        # 2. Try pypdf / PyPDF2
        pypdf_module = None
        try:
            import pypdf as pypdf_module  # type: ignore
        except ImportError:
            try:
                import PyPDF2 as pypdf_module  # type: ignore
            except ImportError:
                pass

        if pypdf_module:
            try:
                reader = pypdf_module.PdfReader(io.BytesIO(pdf_bytes))
                pages_text = []
                for idx, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if text:
                        pages_text.append(f"--- Page {idx + 1} ---\n{text}")
                    if sum(len(p) for p in pages_text) >= max_chars:
                        break
                extracted_text = "\n\n".join(pages_text)
                return extracted_text[:max_chars], extracted_tables
            except Exception as e:
                logger.error(f"PyPDF extraction error: {e}")

        if not extracted_text:
            logger.warning("No suitable PDF extraction library available (install pypdf or pdfplumber) or document is image-only.")

        return extracted_text[:max_chars], extracted_tables


class NasdaqHelsinkiScraper:
    """
    Scraper module for ingesting press releases and financial reports from
    Nasdaq OMX Helsinki (Main Market & First North) and relevant disclosure wires.
    """

    def __init__(self, config: Optional[ScreenerConfig] = None):
        self.config = config or ScreenerConfig.from_env()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
            "Accept-Language": "fi-FI,fi;q=0.9,en-US;q=0.8,en;q=0.7",
        })

    # -------------------------------------------------------------------------
    # RSS Feed Processing
    # -------------------------------------------------------------------------

    def parse_rss_feed_content(self, xml_content: str, source_feed_url: str) -> List[FeedItem]:
        """
        Parse raw XML/RSS string into structured FeedItem objects.
        Employs a multi-stage parser: standard xml.etree -> sanitized ET -> BeautifulSoup fallback.
        """
        if not xml_content or not xml_content.strip():
            return []

        items: List[FeedItem] = []

        # 1. First Attempt: Standard ET
        root = None
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            pass

        # 2. Second Attempt: Sanitize unescaped ampersands and invalid control characters
        if root is None:
            try:
                # Strip non-printable XML control characters
                sanitized = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", xml_content)
                # Fix unescaped ampersands
                sanitized = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", sanitized)
                root = ET.fromstring(sanitized)
            except ET.ParseError:
                pass

        if root is not None:
            channel = root.find("channel")
            item_elements = channel.findall("item") if channel is not None else root.findall(".//item")
            if not item_elements and root.tag.endswith("feed"):
                item_elements = root.findall("{http://www.w3.org/2005/Atom}entry")

            for elem in item_elements:
                try:
                    item = self._extract_feed_item(elem, source_feed_url)
                    if item:
                        items.append(item)
                except Exception as e:
                    logger.warning(f"Error parsing single feed item in {source_feed_url}: {e}")

            if items:
                return items

        # 3. Third Attempt: Resilient Regex Tag Extraction fallback for malformed XML feeds
        logger.info(f"Using resilient regex feed parser fallback for {source_feed_url}")
        try:
            def extract_tag(pattern_name: str, block: str) -> str:
                m = re.search(
                    rf"<{pattern_name}[^>]*>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</{pattern_name}>",
                    block,
                    re.DOTALL | re.IGNORECASE,
                )
                if m:
                    val = (m.group(1) or m.group(2) or "").strip()
                    # unescape HTML/XML entities
                    return BeautifulSoup(val, "html.parser").get_text(strip=True)
                return ""

            # Match all <item> or <entry> blocks
            item_blocks = re.findall(r"<(?:item|entry)[\s>](.*?)</(?:item|entry)>", xml_content, re.DOTALL | re.IGNORECASE)
            for block in item_blocks:
                try:
                    title = extract_tag("title", block)
                    link = extract_tag("link", block)
                    if not link:
                        link_match = re.search(r'<link[^>]+href=["\']([^"\']+)["\']', block, re.IGNORECASE)
                        if link_match:
                            link = link_match.group(1).strip()
                    
                    guid = extract_tag("guid", block) or extract_tag("id", block)
                    desc = extract_tag("description", block) or extract_tag("summary", block)
                    cat = extract_tag("category", block)
                    pub_str = extract_tag("pubDate", block) or extract_tag("published", block) or extract_tag("updated", block)
                    
                    if not link and not title:
                        continue
                        
                    if not guid:
                        guid = hashlib.sha256(f"{title}_{link}_{pub_str}".encode("utf-8")).hexdigest()
                        
                    published_at = self._parse_datetime(pub_str)
                    company_name, ticker = self._infer_company_and_ticker(title, cat, desc)
                    
                    items.append(FeedItem(
                        guid=guid,
                        title=title,
                        link=link,
                        published_at=published_at,
                        summary=desc,
                        company_name=company_name,
                        ticker=ticker,
                        category=cat,
                        source_feed=source_feed_url,
                        raw_metadata={"raw_pub_date": pub_str, "parser": "regex_fallback"}
                    ))
                except Exception as sub_e:
                    logger.warning(f"Item parse error in {source_feed_url}: {sub_e}")
        except Exception as e:
            logger.error(f"Regex feed fallback failed on {source_feed_url}: {e}")

        return items

    def _extract_feed_item(self, elem: ET.Element, source_feed_url: str) -> Optional[FeedItem]:
        """Extract fields from an XML item or entry element."""
        # Find tags (handling standard tags and namespaces)
        def get_text(tag_name: str) -> str:
            # direct search
            found = elem.find(tag_name)
            if found is not None and found.text:
                return found.text.strip()
            # search with wildcard namespace
            for child in elem:
                if child.tag.endswith(tag_name) and child.text:
                    return child.text.strip()
            return ""

        title = get_text("title")
        link = get_text("link")
        guid = get_text("guid") or get_text("id")
        description = get_text("description") or get_text("summary")
        pub_date_str = get_text("pubDate") or get_text("published") or get_text("updated")
        category = get_text("category")

        # If link is inside an attribute (e.g. Atom <link href="..."/>)
        if not link:
            link_elem = elem.find("{http://www.w3.org/2005/Atom}link") or elem.find("link")
            if link_elem is not None:
                link = link_elem.attrib.get("href", "")

        if not link and not title:
            return None

        # Generate unique GUID if absent
        if not guid:
            guid_seed = f"{title}_{link}_{pub_date_str}"
            guid = hashlib.sha256(guid_seed.encode("utf-8")).hexdigest()

        # Parse publication datetime
        published_at = self._parse_datetime(pub_date_str)

        # Attempt to infer company name and ticker from title or category
        company_name, ticker = self._infer_company_and_ticker(title, category, description)

        return FeedItem(
            guid=guid,
            title=title,
            link=link,
            published_at=published_at,
            summary=description,
            company_name=company_name,
            ticker=ticker,
            category=category,
            source_feed=source_feed_url,
            raw_metadata={"raw_pub_date": pub_date_str}
        )

    @staticmethod
    def _parse_datetime(date_str: str) -> Optional[datetime]:
        """Parse RFC 2822 / ISO 8601 date strings to UTC datetime."""
        if not date_str:
            return None
        # Try email.utils for RFC 2822 (standard RSS pubDate)
        try:
            parsed_tuple = email.utils.parsedate_to_datetime(date_str)
            if parsed_tuple:
                return parsed_tuple.astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            pass

        # Try ISO 8601
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(date_str[:25].strip(), fmt)
                return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
            except Exception:
                continue

        return None

    @staticmethod
    def _infer_company_and_ticker(title: str, category: str, description: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract company name and ticker if present in formatted press release titles.
        Example formats:
        - "Nightingale Health Oyj: Osavuosikatsaus tammi-maaliskuu 2026"
        - "Faron Pharmaceuticals Oy: Sisäpiiritieto: ..."
        - "[FARON] Johtohenkilöiden liiketoimet"
        - "[NIGHT] Nightingale Health Oyj: Osavuosikatsaus"
        """
        company_name = None
        ticker = None

        # Look for [TICKER] or (TICKER) prefix
        ticker_match = re.search(r"[\[\(]([A-Z0-9]{2,8})[\]\)]", title)
        if ticker_match:
            ticker = ticker_match.group(1)

        # Look for Company Name before colon (standard Nasdaq release pattern)
        if ":" in title:
            prefix = title.split(":", 1)[0].strip()
            # Strip [TICKER] or (TICKER) tag if at start of company name
            prefix = re.sub(r"^[\[\(][A-Z0-9]{2,8}[\]\)]\s*", "", prefix).strip()
            # Exclude generic prefixes
            if prefix and not any(k in prefix.lower() for k in ["sisäpiiritieto", "tiedote", "pörssitiedote", "press release"]):
                company_name = prefix

        return company_name, ticker

    def fetch_feed_sync(self, feed_url: str) -> List[FeedItem]:
        """Synchronously fetch and parse a single RSS feed with retry logic."""
        logger.info(f"Fetching RSS feed: {feed_url}")
        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = self.session.get(feed_url, timeout=self.config.request_timeout_seconds)
                response.raise_for_status()
                return self.parse_rss_feed_content(response.text, feed_url)
            except Exception as e:
                logger.warning(f"Attempt {attempt}/{self.config.max_retries} failed for feed {feed_url}: {e}")
                if attempt < self.config.max_retries:
                    import time
                    time.sleep(self.config.rate_limit_delay_seconds * (self.config.retry_backoff_factor ** attempt))
                else:
                    logger.error(f"Exhausted retries fetching RSS feed {feed_url}")
        return []

    async def fetch_feed_async(self, feed_url: str) -> List[FeedItem]:
        """Asynchronously fetch RSS feed in worker pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.fetch_feed_sync, feed_url)

    async def fetch_all_feeds_async(self, feed_urls: Optional[List[str]] = None) -> List[FeedItem]:
        """Fetch all configured RSS feeds concurrently."""
        urls = feed_urls or self.config.rss_feeds
        tasks = [self.fetch_feed_async(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_items: List[FeedItem] = []
        for res in results:
            if isinstance(res, list):
                all_items.extend(res)
            elif isinstance(res, Exception):
                logger.error(f"Feed fetch error: {res}")
                
        # Deduplicate items by GUID while preserving order
        seen_guids = set()
        deduped_items: List[FeedItem] = []
        for item in all_items:
            if item.guid not in seen_guids:
                seen_guids.add(item.guid)
                deduped_items.append(item)
                
        logger.info(f"Fetched {len(deduped_items)} unique feed items across {len(urls)} feeds.")
        return deduped_items

    # -------------------------------------------------------------------------
    # HTML Document & PDF Extraction
    # -------------------------------------------------------------------------

    def fetch_and_parse_document(self, url: str) -> DocumentPayload:
        """
        Fetch the HTML article content, strip boilerplate, and locate / parse attached PDFs.
        Isolates failures so a corrupt attachment or broken link returns partial payload.
        """
        payload = DocumentPayload(url=url)
        if not url or not url.startswith("http"):
            payload.error_message = f"Invalid URL: {url}"
            return payload

        # 1. Fetch HTML page
        html_content = ""
        try:
            resp = self.session.get(url, timeout=self.config.request_timeout_seconds)
            resp.raise_for_status()
            html_content = resp.text
        except Exception as e:
            msg = f"Failed to fetch HTML document {url}: {e}"
            logger.warning(msg)
            payload.error_message = msg
            return payload

        # 2. Parse HTML text and locate attachments
        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # Remove noise elements (scripts, styles, navigations, footers)
            for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg"]):
                tag.decompose()

            # Find PDF attachments
            pdf_urls = []
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                if ".pdf" in href.lower() or "download" in href.lower() and "pdf" in a_tag.text.lower():
                    full_pdf_url = urllib.parse.urljoin(url, href)
                    if full_pdf_url not in pdf_urls:
                        pdf_urls.append(full_pdf_url)

            payload.pdf_urls = pdf_urls

            # Extract main body text
            # Target common release container classes or body
            main_container = (
                soup.find("div", class_=re.compile(r"release|article|content|news-body|press-release", re.I))
                or soup.find("article")
                or soup.find("main")
                or soup.body
            )

            if main_container:
                raw_text = main_container.get_text(separator="\n")
            else:
                raw_text = soup.get_text(separator="\n")

            # Clean and normalize whitespace
            cleaned_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
            payload.html_text = "\n".join(cleaned_lines)[:self.config.max_text_characters]

        except Exception as e:
            logger.error(f"Error parsing HTML structure for {url}: {e}")
            payload.error_message = f"HTML Parse Error: {e}"

        # 3. Process PDF attachments (if any)
        if payload.pdf_urls:
            self._process_pdf_attachments(payload)

        return payload

    def _process_pdf_attachments(self, payload: DocumentPayload) -> None:
        """Download and extract text from discovered PDF attachments."""
        pdf_texts = []
        for pdf_url in payload.pdf_urls:
            try:
                logger.info(f"Downloading attached PDF report: {pdf_url}")
                head_resp = self.session.head(pdf_url, timeout=10, allow_redirects=True)
                content_len = int(head_resp.headers.get("Content-Length", 0))

                if content_len > self.config.max_pdf_size_bytes:
                    logger.warning(f"Skipping PDF {pdf_url}: size {content_len} bytes exceeds limit {self.config.max_pdf_size_bytes}")
                    continue

                resp = self.session.get(pdf_url, timeout=30)
                resp.raise_for_status()

                text, tables = PDFExtractor.extract_text_from_bytes(
                    resp.content,
                    max_chars=self.config.max_text_characters
                )
                if text:
                    pdf_texts.append(f"[File: {pdf_url}]\n{text}")
                if tables:
                    payload.extracted_tables.extend(tables)

            except Exception as e:
                logger.warning(f"Failed to fetch/parse PDF {pdf_url}: {e}")

        payload.pdf_text = "\n\n".join(pdf_texts)[:self.config.max_text_characters]

    # -------------------------------------------------------------------------
    # Batch Pipeline Orchestration
    # -------------------------------------------------------------------------

    async def scrape_feed_item_async(self, item: FeedItem, semaphore: asyncio.Semaphore) -> ScrapedRelease:
        """Fetch and parse full document for a feed item with concurrency limit."""
        async with semaphore:
            loop = asyncio.get_event_loop()
            doc = await loop.run_in_executor(None, self.fetch_and_parse_document, item.link)
            status = ProcessingStatus.ERROR if doc.error_message and not doc.html_text else ProcessingStatus.PARSED
            return ScrapedRelease(feed_item=item, document=doc, status=status)

    async def process_new_items_async(self, items: List[FeedItem]) -> List[ScrapedRelease]:
        """Concurrently fetch full documents for multiple feed items."""
        semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        tasks = [self.scrape_feed_item_async(item, semaphore) for item in items]
        releases = await asyncio.gather(*tasks, return_exceptions=False)
        return releases


def fetch_latest_releases(config: Optional[ScreenerConfig] = None) -> List[FeedItem]:
    """
    Convenience function to fetch all latest releases synchronously across
    all configured Nasdaq CDS, First North, and GlobeNewswire feeds.
    """
    scraper = NasdaqHelsinkiScraper(config)
    try:
        return asyncio.run(scraper.fetch_all_feeds_async())
    except RuntimeError:
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(scraper.fetch_all_feeds_async())


if __name__ == "__main__":
    # Direct test execution helper
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    print("Testing NasdaqHelsinkiScraper...")
    scraper = NasdaqHelsinkiScraper()
    
    # Run async feed fetch
    feed_items = asyncio.run(scraper.fetch_all_feeds_async())
    print(f"\nFetched {len(feed_items)} feed items.")
    
    if feed_items:
        print("\n--- Sample Latest 3 Items ---")
        for itm in feed_items[:3]:
            print(f"Title: {itm.title}")
            print(f"Company: {itm.company_name} | Ticker: {itm.ticker}")
            print(f"Published: {itm.published_at}")
            print(f"Link: {itm.link}\n")
            
        print("Parsing full document for first item...")
        doc = scraper.fetch_and_parse_document(feed_items[0].link)
        print(f"HTML Text Length: {len(doc.html_text)} chars")
        print(f"PDF URLs Found: {doc.pdf_urls}")
        print(f"PDF Text Length: {len(doc.pdf_text)} chars")
        if doc.html_text:
            print("\nPreview (First 300 chars):")
            print(doc.html_text[:300])
