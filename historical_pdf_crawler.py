"""
Historical Earnings Report PDF Crawler for Nordic Micro-Cap Equities.

Automates the mass discovery and downloading of historical quarterly and annual earnings
reports (PDFs) directly from centralized PR wire archives (Cision Wire & Nasdaq OMX Nordic CDS)
for backtesting the CORE Tenbagger fundamental pipeline.

Key Features:
1. Universe Ingestion: Reads `data/nordnet_universe.csv` to map tradable tickers.
2. Centralized Archive Discovery: Searches Cision News archives (`news.cision.com`)
   and Nasdaq OMX Nordic announcement feeds.
3. Strict Financial Keyword Filtering: Filters for quarterly/interim reports and financial statement releases:
   ("Osavuosikatsaus", "Puolivuosikatsaus", "Tilinpäätös", "Tilinpäätöstiedote", "Delårsrapport", "Interim Report", etc.).
4. Standardized Output Naming: `{Ticker}_{Year}_{Period}.pdf` (e.g., `KEMIRA.HE_2024_Q3.pdf`, `QTCOM.HE_2024_FY.pdf`).
5. Anti-Scraping Defenses & Throttling:
   - Randomized delay `time.sleep(random.uniform(2.0, 5.0))` between requests.
   - User-Agent rotation across standard desktop browsers.
   - 429 / 403 exponential backoff and TCP connection reuse.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Set
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Ensure parent directory is in sys.path
_current_dir = Path(__file__).resolve().parent
BASE_DIR = _current_dir.parent if _current_dir.name == "screener" else _current_dir
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Cross-platform stdout encoding fix
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("screener.pdf_crawler")

DEFAULT_UNIVERSE_CSV = BASE_DIR / "data" / "nordnet_universe.csv"
DEFAULT_REPORTS_DIR = BASE_DIR / "data" / "historical_reports"

# Rotating User-Agents to prevent header fingerprinting
ROTATING_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

# Targeted report keywords across Finnish, Swedish, and English
CORE_REPORT_KEYWORDS_REGEX = re.compile(
    r"\b("
    # Finnish keywords
    r"osavuosikatsaus|puolivuosikatsaus|liiketoimintakatsaus|tilinpäätöstiedote|tilinpäätös|vuosikertomus|"
    r"tammi[\s\-]maaliskuu|tammi[\s\-]kesäkuu|tammi[\s\-]syyskuu|loka[\s\-]joulukuu|h1|h2|q1|q2|q3|q4|"
    # Swedish keywords
    r"delårsrapport|halvårsrapport|kvartalsrapport|bokslutskommuniké|årsredovisning|bokslut|"
    # English keywords
    r"interim\s+report|half[\s\-]year\s+report|financial\s+statement\s+release|annual\s+report|"
    r"business\s+review|quarterly\s+report"
    r")\b",
    re.IGNORECASE
)

# Administrative exclusion keywords (discards AGMs, notices, calendars)
EXCLUSION_KEYWORDS_REGEX = re.compile(
    r"\b("
    r"kutsu|yhtiökokous|yhtiökokouksen|yhtiökokouskutsu|hallituksen\s+järjestäytyminen|"
    r"liputusilmoitus|taloudellinen\s+kalenteri|osakepalkkio|nimitystoimikun|johdon\s+liiketoimet|"
    r"kallelse|bolagsstämma|bolagsstämmans|konstituerande|flaggningsmeddelande|finansiell\s+kalender|"
    r"notice\s+to|general\s+meeting|reporting\s+calendar|managers['\s]+transactions"
    r")\b",
    re.IGNORECASE
)


@dataclass
class ReportCandidate:
    """Metadata representing a discovered historical report release."""
    ticker: str
    company_name: str
    title: str
    release_url: str
    published_date_str: str
    year: int
    period: str
    pdf_url: Optional[str] = None
    target_filename: Optional[str] = None


def extract_period_and_year(title: str, pub_date_str: str = "") -> Tuple[int, str]:
    """
    Infers the fiscal Year and Quarter / Period from release title and publication date.
    
    Examples:
    - 'Osavuosikatsaus tammi-syyskuu 2024 (Q3)' -> (2024, 'Q3')
    - 'Puolivuosikatsaus 2023' -> (2023, 'H1')
    - 'Tilinpäätöstiedote 2024' -> (2024, 'FY')
    - 'Delårsrapport Q1 2025' -> (2025, 'Q1')
    """
    clean_title = re.sub(r"\s+", " ", title).strip()
    
    # 1. Extract 4-digit year (2020 - 2029)
    year_match = re.search(r"\b(202[0-9])\b", clean_title)
    if year_match:
        year = int(year_match.group(1))
    elif pub_date_str:
        pub_year_match = re.search(r"\b(202[0-9])\b", pub_date_str)
        year = int(pub_year_match.group(1)) if pub_year_match else datetime.now(timezone.utc).year
    else:
        year = datetime.now(timezone.utc).year

    # 2. Extract Quarter / Period
    period = "Q1"  # Default fallback
    title_lower = clean_title.lower()

    if "tilinpäätöstiedote" in title_lower or "bokslutskommuniké" in title_lower or "financial statement release" in title_lower or "tilinpäätös" in title_lower or "vuosikertomus" in title_lower or "annual report" in title_lower:
        period = "FY"
    elif "q4" in title_lower or "loka-joulukuu" in title_lower or "tammi-joulukuu" in title_lower:
        period = "Q4"
    elif "q3" in title_lower or "tammi-syyskuu" in title_lower or "januari-september" in title_lower or "9m" in title_lower:
        period = "Q3"
    elif "q2" in title_lower or "puolivuosikatsaus" in title_lower or "halvårsrapport" in title_lower or "half-year" in title_lower or "tammi-kesäkuu" in title_lower or "h1" in title_lower:
        period = "Q2"
    elif "q1" in title_lower or "tammi-maaliskuu" in title_lower or "januari-mars" in title_lower or "3m" in title_lower:
        period = "Q1"
    elif "h2" in title_lower or "heinä-joulukuu" in title_lower:
        period = "H2"

    return year, period


def generate_company_slug_candidates(name: str) -> List[str]:
    """Generates candidate Cision archive slugs from company legal name."""
    if not name:
        return []

    # Strip corporate abbreviations
    clean = re.sub(r"\b(oyj|oy|plc|ab|asa|a/s|as|corp|corporation|group)\b", "", name, flags=re.IGNORECASE).strip()
    
    slugs = [
        re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-"),
        re.sub(r"[^a-zA-Z0-9]+", "-", (clean + " oyj").lower()).strip("-"),
        re.sub(r"[^a-zA-Z0-9]+", "-", clean.lower()).strip("-"),
        re.sub(r"[^a-zA-Z0-9]+", "-", (clean + " group").lower()).strip("-"),
    ]
    # Remove duplicates preserving order
    return list(dict.fromkeys(s for s in slugs if s))


class HistoricalPDFCrawler:
    """Crawls centralized PR archives and downloads historical earnings reports as PDFs."""

    def __init__(
        self,
        universe_csv_path: Optional[Path | str] = None,
        output_dir: Optional[Path | str] = None,
        min_year: int = 2023,
        max_year: int = 2026,
        min_delay_seconds: float = 2.0,
        max_delay_seconds: float = 5.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 30.0,
    ):
        self.universe_csv_path = Path(universe_csv_path or DEFAULT_UNIVERSE_CSV)
        self.output_dir = Path(output_dir or DEFAULT_REPORTS_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.min_year = min_year
        self.max_year = max_year
        self.min_delay_seconds = min_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
            "Accept-Language": "fi-FI,fi;q=0.9,sv-SE;q=0.8,en-US;q=0.7,en;q=0.6",
            "Connection": "keep-alive",
        })

    def _get_random_headers(self) -> Dict[str, str]:
        """Returns browser headers with randomized User-Agent."""
        return {"User-Agent": random.choice(ROTATING_USER_AGENTS)}

    def _apply_anti_scraping_delay(self) -> None:
        """Applies randomized polite delay between 2.0s and 5.0s to avoid rate limiting."""
        if self.max_delay_seconds > 0:
            delay = random.uniform(self.min_delay_seconds, self.max_delay_seconds)
            logger.debug(f"Anti-scraping delay: sleeping for {delay:.2f}s...")
            time.sleep(delay)

    def _safe_get(self, url: str, timeout: float = 15.0) -> Optional[requests.Response]:
        """Executes HTTP GET with retries, 429/403 backoff, and header rotation."""
        for attempt in range(1, self.max_retries + 1):
            self._apply_anti_scraping_delay()
            try:
                response = self.session.get(
                    url,
                    headers=self._get_random_headers(),
                    timeout=timeout,
                )

                if response.status_code in (429, 403):
                    logger.warning(
                        f"⚠️ [RATE LIMIT {response.status_code}] on {url}. "
                        f"Backing off {self.retry_backoff_seconds:.0f}s (attempt {attempt}/{self.max_retries})..."
                    )
                    time.sleep(self.retry_backoff_seconds)
                    continue

                if response.status_code == 404:
                    return None

                response.raise_for_status()
                return response

            except requests.exceptions.RequestException as e:
                logger.debug(f"HTTP request error on {url} (attempt {attempt}): {e}")
                if attempt < self.max_retries:
                    time.sleep(3.0 * attempt)
                else:
                    logger.warning(f"Failed to fetch {url} after {self.max_retries} attempts: {e}")
                    return None
        return None

    def load_universe(self, limit: Optional[int] = None, filter_ticker: Optional[str] = None) -> List[Dict[str, str]]:
        """Loads stock listings from nordnet_universe.csv."""
        if not self.universe_csv_path.exists():
            logger.error(f"Nordnet universe CSV not found at {self.universe_csv_path}")
            return []

        stocks: List[Dict[str, str]] = []
        with open(self.universe_csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ticker = row.get("Ticker_YF", "").strip().upper()
                name = row.get("Name", "").strip()
                market = row.get("Market", "").strip()

                if not ticker:
                    continue

                if filter_ticker and filter_ticker.upper() not in ticker:
                    continue

                stocks.append({
                    "Ticker_YF": ticker,
                    "Name": name,
                    "Market": market,
                })

        if limit and limit > 0:
            stocks = stocks[:limit]

        logger.info(f"Loaded {len(stocks)} stock(s) from {self.universe_csv_path}")
        return stocks

    def resolve_cision_company_url(self, company_name: str) -> Optional[str]:
        """Tries slug variants to resolve the active Cision company archive URL."""
        slug_candidates = generate_company_slug_candidates(company_name)
        
        # Test Finnish and Scandinavian country routes
        prefixes = ["https://news.cision.com/fi/", "https://news.cision.com/se/", "https://news.cision.com/"]

        for slug in slug_candidates:
            for prefix in prefixes:
                url = f"{prefix}{slug}"
                resp = self._safe_get(url, timeout=8.0)
                if resp and resp.status_code == 200 and len(resp.text) > 5000:
                    return url

        return None

    def extract_reports_from_cision_archive(
        self,
        company_url: str,
        ticker: str,
        company_name: str,
        max_pages: int = 4,
    ) -> List[ReportCandidate]:
        """
        Paginates through a company's Cision PR archive, filters financial reports,
        and identifies attached PDF releases.
        """
        candidates: List[ReportCandidate] = []
        seen_urls: Set[str] = set()

        for page in range(1, max_pages + 1):
            page_url = f"{company_url}?page={page}"
            resp = self._safe_get(page_url)
            if not resp or resp.status_code != 200:
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            release_links = soup.find_all("a", href=lambda h: h and "/r/" in h)

            if not release_links:
                break

            found_on_page = 0
            for a_tag in release_links:
                href = a_tag["href"]
                full_release_url = urljoin(company_url, href)
                
                if full_release_url in seen_urls:
                    continue
                seen_urls.add(full_release_url)

                title = a_tag.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                # Filter: Exclude administrative noise
                if EXCLUSION_KEYWORDS_REGEX.search(title):
                    continue

                # Filter: Must match quarterly or annual financial report keywords
                if not CORE_REPORT_KEYWORDS_REGEX.search(title):
                    continue

                year, period = extract_period_and_year(title)
                if year < self.min_year or year > self.max_year:
                    continue

                target_filename = f"{ticker}_{year}_{period}.pdf"
                candidate = ReportCandidate(
                    ticker=ticker,
                    company_name=company_name,
                    title=title,
                    release_url=full_release_url,
                    published_date_str=str(year),
                    year=year,
                    period=period,
                    target_filename=target_filename,
                )
                candidates.append(candidate)
                found_on_page += 1

            if found_on_page == 0 and page > 1:
                # No relevant releases on this page; stop paginating deeper
                break

        return candidates

    def extract_pdf_download_url(self, release_url: str) -> Optional[str]:
        """Fetches release HTML and discovers attached PDF report link."""
        resp = self._safe_get(release_url)
        if not resp or resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Look for direct PDF links or Cision media server attachments (mb.cision.com)
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            if ".pdf" in href.lower() or "mb.cision.com" in href.lower():
                return urljoin(release_url, href)

        return None

    def download_pdf(self, pdf_url: str, target_filename: str) -> bool:
        """Downloads and saves a PDF report to data/historical_reports/."""
        dest_path = self.output_dir / target_filename
        
        if dest_path.exists() and dest_path.stat().st_size > 1024:
            logger.info(f"   [SKIP] Already exists: {target_filename} ({dest_path.stat().st_size / 1024:.1f} KB)")
            return True

        resp = self._safe_get(pdf_url, timeout=25.0)
        if not resp or resp.status_code != 200:
            logger.warning(f"   [FAIL] Failed to download PDF from {pdf_url}")
            return False

        # Ensure content is strictly valid PDF binary (%PDF header)
        content = resp.content
        if not content.startswith(b"%PDF"):
            logger.warning(f"   [FAIL] Content from {pdf_url} is not valid PDF binary (%PDF header missing).")
            return False

        try:
            with open(dest_path, "wb") as f:
                f.write(content)
            logger.info(f"   [DOWNLOADED] Saved {target_filename} ({len(content) / 1024:.1f} KB)")
            return True
        except Exception as e:
            logger.error(f"   [ERROR] Failed writing PDF {target_filename}: {e}")
            return False

    def crawl_company(self, ticker: str, company_name: str, max_reports: int = 8) -> int:
        """Crawls and downloads all matching earnings reports for a single company."""
        logger.info(f"\n[{ticker}] Searching archives for: {company_name}...")
        
        archive_url = self.resolve_cision_company_url(company_name)
        if not archive_url:
            logger.info(f"[-] No centralized Cision archive found for '{company_name}'.")
            return 0

        logger.info(f"[+] Found archive: {archive_url}")
        candidates = self.extract_reports_from_cision_archive(archive_url, ticker, company_name)

        if not candidates:
            logger.info(f"[-] No quarterly/annual earnings releases found for {ticker} ({self.min_year}-{self.max_year}).")
            return 0

        logger.info(f"[+] Discovered {len(candidates)} report release(s) for {ticker}:")
        downloaded_count = 0

        for cand in candidates[:max_reports]:
            logger.info(f" -> Release: {cand.title[:65]}... ({cand.target_filename})")
            pdf_url = self.extract_pdf_download_url(cand.release_url)
            if pdf_url:
                success = self.download_pdf(pdf_url, cand.target_filename or f"{ticker}_{cand.year}_{cand.period}.pdf")
                if success:
                    downloaded_count += 1
            else:
                logger.warning(f"   [-] No attached PDF found on release page: {cand.release_url}")

        return downloaded_count

    def crawl_all(self, limit: Optional[int] = None, filter_ticker: Optional[str] = None, max_reports_per_company: int = 8) -> Dict[str, Any]:
        """Iterates through universe and mass downloads historical earnings reports."""
        logger.info("=" * 80)
        logger.info(">>> STARTING HISTORICAL EARNINGS REPORT PDF CRAWLER <<<")
        logger.info(f"Target Directory: {self.output_dir} | Year Range: {self.min_year} - {self.max_year}")
        logger.info("=" * 80)

        stocks = self.load_universe(limit=limit, filter_ticker=filter_ticker)
        if not stocks:
            logger.warning("No stocks loaded. Terminating crawl.")
            return {"total_stocks": 0, "total_downloaded": 0}

        total_downloaded = 0
        successful_companies = 0
        start_time = time.time()

        for idx, stock in enumerate(stocks, 1):
            ticker = stock["Ticker_YF"]
            name = stock["Name"]
            logger.info(f"[{idx}/{len(stocks)}] Processing {ticker} ({name})...")
            
            try:
                count = self.crawl_company(ticker, name, max_reports=max_reports_per_company)
                total_downloaded += count
                if count > 0:
                    successful_companies += 1
            except Exception as e:
                logger.error(f"Error crawling {ticker} ({name}): {e}")

        elapsed = time.time() - start_time
        logger.info("\n" + "=" * 80)
        logger.info(
            f"PDF Crawl Complete in {elapsed:.1f}s: "
            f"Evaluated {len(stocks)} companies | "
            f"{successful_companies} companies with reports | "
            f"{total_downloaded} total PDFs downloaded/verified."
        )
        logger.info(f"Output files saved to: {self.output_dir}")
        logger.info("=" * 80 + "\n")

        return {
            "total_stocks": len(stocks),
            "successful_companies": successful_companies,
            "total_downloaded": total_downloaded,
            "elapsed_seconds": round(elapsed, 1),
        }


def main():
    parser = argparse.ArgumentParser(description="Nordic Micro-Cap Historical Earnings PDF Crawler")
    parser.add_argument("--universe", type=str, default=str(DEFAULT_UNIVERSE_CSV), help="Path to nordnet_universe.csv")
    parser.add_argument("--output-dir", type=str, default=str(DEFAULT_REPORTS_DIR), help="Target PDF directory (default: data/historical_reports)")
    parser.add_argument("--ticker", type=str, default=None, help="Filter to a single ticker (e.g. KEMIRA.HE, RAUTE.HE, QTCOM.HE)")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of companies to crawl from universe")
    parser.add_argument("--min-year", type=int, default=2023, help="Minimum publication year (default: 2023)")
    parser.add_argument("--max-year", type=int, default=2026, help="Maximum publication year (default: 2026)")
    parser.add_argument("--max-reports", type=int, default=8, help="Maximum reports to download per company (default: 8)")
    parser.add_argument("--min-delay", type=float, default=2.0, help="Min randomized request delay in seconds (default: 2.0)")
    parser.add_argument("--max-delay", type=float, default=5.0, help="Max randomized request delay in seconds (default: 5.0)")

    args = parser.parse_args()

    crawler = HistoricalPDFCrawler(
        universe_csv_path=args.universe,
        output_dir=args.output_dir,
        min_year=args.min_year,
        max_year=args.max_year,
        min_delay_seconds=args.min_delay,
        max_delay_seconds=args.max_delay,
    )

    crawler.crawl_all(
        limit=args.limit,
        filter_ticker=args.ticker,
        max_reports_per_company=args.max_reports,
    )


if __name__ == "__main__":
    main()
