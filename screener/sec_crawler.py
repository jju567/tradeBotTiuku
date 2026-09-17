"""
US SEC EDGAR Filings Crawler for Micro-Cap & Small-Cap Equities.

Directly queries the official SEC EDGAR REST API to ingest quarterly (10-Q)
and annual (10-K) filings for US stocks tradable via Nordnet.

Key Features:
1. Universe Ingestion: Reads `data/nordnet_global_universe.csv` or `data/nordnet_universe.csv`.
2. Company CIK Resolution: Queries `https://www.sec.gov/files/company_tickers.json` with in-memory caching.
3. Submissions API: Pulls `https://data.sec.gov/submissions/CIK{cik}.json` to find 10-K and 10-Q filings.
4. Clean File Naming: Saves extracted text to `data/historical_reports/us/{Ticker}_{Year}_Q{Quarter/FY}.txt`.
5. Strict SEC Compliance: Polite User-Agent header (User-Agent: Company name admin@email.com) and rate limiting (max 10 req/sec per SEC rules).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Set
from bs4 import BeautifulSoup
import requests
from dotenv import load_dotenv

load_dotenv()

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

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("screener.sec_crawler")

_current_dir = Path(__file__).resolve().parent
BASE_DIR = _current_dir.parent if _current_dir.name == "screener" else _current_dir
DEFAULT_UNIVERSE_CSV = BASE_DIR / "data" / "nordnet_global_universe.csv"
FALLBACK_UNIVERSE_CSV = BASE_DIR / "data" / "nordnet_universe.csv"
DEFAULT_US_REPORTS_DIR = BASE_DIR / "data" / "historical_reports" / "us"

# SEC EDGAR Endpoints & Headers
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

DEFAULT_SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "TradeBotTiuku Research jarmo.trading@example.com"
)


class SECEdgarCrawler:
    """Manages polite, robust downloading and text extraction of SEC 10-K / 10-Q filings."""

    def __init__(
        self,
        universe_csv_path: Optional[Path | str] = None,
        output_dir: Optional[Path | str] = None,
        user_agent: str = DEFAULT_SEC_USER_AGENT,
        min_year: int = 2023,
        max_year: int = 2026,
        max_filings_per_ticker: int = 4,
        rate_limit_sleep: float = 0.25,  # 4 req/sec (SEC allows max 10 req/sec)
    ):
        self.universe_csv_path = Path(universe_csv_path or (
            DEFAULT_UNIVERSE_CSV if DEFAULT_UNIVERSE_CSV.exists() else FALLBACK_UNIVERSE_CSV
        ))
        self.output_dir = Path(output_dir or DEFAULT_US_REPORTS_DIR)
        self.user_agent = user_agent
        self.min_year = min_year
        self.max_year = max_year
        self.max_filings_per_ticker = max_filings_per_ticker
        self.rate_limit_sleep = rate_limit_sleep

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html, */*",
            "Accept-Encoding": "gzip, deflate",
        })

        self._ticker_cik_map: Optional[Dict[str, int]] = None

    def _throttle(self) -> None:
        """Polite sleep per SEC EDGAR rate limit policy."""
        if self.rate_limit_sleep > 0:
            time.sleep(self.rate_limit_sleep)

    def load_ticker_cik_map(self) -> Dict[str, int]:
        """Fetches the official SEC company ticker -> CIK mapping table."""
        if self._ticker_cik_map is not None:
            return self._ticker_cik_map

        logger.info("Fetching SEC EDGAR company tickers mapping table...")
        self._throttle()
        try:
            resp = self.session.get(SEC_TICKERS_URL, timeout=20.0)
            resp.raise_for_status()
            data = resp.json()

            mapping: Dict[str, int] = {}
            for item in data.values():
                t = str(item.get("ticker", "")).strip().upper()
                c = item.get("cik_str")
                if t and c:
                    mapping[t] = int(c)

            self._ticker_cik_map = mapping
            logger.info(f"Loaded {len(mapping)} ticker-to-CIK mappings from SEC.")
            return mapping
        except Exception as e:
            logger.error(f"Failed to fetch SEC ticker map: {e}")
            self._ticker_cik_map = {}
            return {}

    def load_us_tickers_from_universe(self) -> List[Dict[str, Any]]:
        """Loads US micro-caps from the universe CSV."""
        if not self.universe_csv_path.exists():
            logger.warning(f"Universe file {self.universe_csv_path} does not exist!")
            return []

        us_stocks: List[Dict[str, Any]] = []
        try:
            with open(self.universe_csv_path, mode="r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ticker = row.get("Ticker_YF", "").strip().upper()
                    market = row.get("Market", "").upper()
                    # US tickers don't have suffix (.HE, .ST, .OL, .CO) or have US market
                    is_us = (
                        ("." not in ticker) or
                        ("US" in market) or
                        ("NASDAQ" in market and not any(s in ticker for s in (".HE", ".ST", ".OL", ".CO"))) or
                        ("NYSE" in market)
                    )
                    if is_us and ticker:
                        # Clean ticker for SEC query (e.g., BRK.B -> BRK-B or BRKB)
                        clean_sec_ticker = ticker.split(".")[0].replace("-", "")
                        us_stocks.append({
                            "Ticker_YF": ticker,
                            "SEC_Ticker": clean_sec_ticker,
                            "Name": row.get("Name", ticker),
                            "Market": row.get("Market", "US"),
                            "MarketCap_EUR": row.get("MarketCap_EUR", "0.0"),
                        })
            logger.info(f"Loaded {len(us_stocks)} US tickers from {self.universe_csv_path.name}.")
            return us_stocks
        except Exception as e:
            logger.error(f"Error reading universe CSV {self.universe_csv_path}: {e}")
            return []

    def get_company_filings_list(self, cik: int) -> List[Dict[str, Any]]:
        """Queries SEC EDGAR submissions API for recent 10-K and 10-Q filings."""
        padded_cik = str(cik).zfill(10)
        url = SEC_SUBMISSIONS_URL.format(cik=padded_cik)
        self._throttle()

        try:
            resp = self.session.get(url, timeout=20.0)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()

            recent = data.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accession_numbers = recent.get("accessionNumber", [])
            primary_documents = recent.get("primaryDocument", [])
            filing_dates = recent.get("filingDate", [])
            report_dates = recent.get("reportDate", [])

            filings: List[Dict[str, Any]] = []
            for i in range(len(forms)):
                form = forms[i].upper()
                if form not in ("10-K", "10-Q"):
                    continue

                f_date = filing_dates[i] if i < len(filing_dates) else ""
                r_date = report_dates[i] if i < len(report_dates) else f_date
                year_str = r_date[:4] if len(r_date) >= 4 else (f_date[:4] if len(f_date) >= 4 else "")
                try:
                    year = int(year_str)
                except ValueError:
                    continue

                if not (self.min_year <= year <= self.max_year):
                    continue

                acc_num = accession_numbers[i].replace("-", "")
                prim_doc = primary_documents[i]
                doc_url = f"{SEC_ARCHIVES_BASE}/{cik}/{acc_num}/{prim_doc}"

                # Infer Quarter from form and filing date/report date
                quarter = "FY" if form == "10-K" else "Q"
                if form == "10-Q":
                    # Determine quarter from report month
                    if len(r_date) >= 7:
                        month = int(r_date[5:7])
                        if 1 <= month <= 4:
                            quarter = "Q1"
                        elif 5 <= month <= 7:
                            quarter = "Q2"
                        elif 8 <= month <= 10:
                            quarter = "Q3"
                        else:
                            quarter = "Q4"
                    else:
                        quarter = "Q"

                filings.append({
                    "form": form,
                    "year": year,
                    "quarter": quarter,
                    "filing_date": f_date,
                    "report_date": r_date,
                    "url": doc_url,
                    "accession": accession_numbers[i],
                })

            return filings[:self.max_filings_per_ticker]

        except Exception as e:
            logger.warning(f"Failed to fetch filings for CIK {cik}: {e}")
            return []

    def download_filing_text(self, url: str) -> str:
        """Downloads filing document and strips HTML tags to clean readable text."""
        self._throttle()
        try:
            resp = self.session.get(url, timeout=30.0)
            resp.raise_for_status()
            content = resp.text

            # If HTML, extract text cleanly
            if "<html" in content.lower() or "<body" in content.lower():
                soup = BeautifulSoup(content, "html.parser")
                # Remove scripts, styles
                for s in soup(["script", "style", "header", "footer"]):
                    s.decompose()
                text = soup.get_text(separator="\n")
            else:
                text = content

            # Clean whitespace
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text)
            return text.strip()

        except Exception as e:
            logger.warning(f"Error downloading filing from {url}: {e}")
            return ""

    def crawl_all(self) -> int:
        """Main execution: fetches filings for all US tickers and saves them to disk."""
        cik_map = self.load_ticker_cik_map()
        if not cik_map:
            logger.error("No CIK mapping available. Aborting SEC crawl.")
            return 0

        us_stocks = self.load_us_tickers_from_universe()
        if not us_stocks:
            logger.warning("No US tickers found in universe.")
            return 0

        logger.info(f"Starting SEC EDGAR crawl for {len(us_stocks)} US tickers...")
        total_downloaded = 0

        for idx, stock in enumerate(us_stocks, 1):
            ticker_yf = stock["Ticker_YF"]
            sec_ticker = stock["SEC_Ticker"]
            cik = cik_map.get(sec_ticker) or cik_map.get(ticker_yf)

            if not cik:
                logger.debug(f"[{idx}/{len(us_stocks)}] CIK not found for {ticker_yf} ({stock['Name']}). Skipping.")
                continue

            filings = self.get_company_filings_list(cik)
            if not filings:
                continue

            logger.info(f"[{idx}/{len(us_stocks)}] Found {len(filings)} filing(s) for {ticker_yf} (CIK: {cik}).")

            for f in filings:
                year = f["year"]
                quarter = f["quarter"]
                filename = f"{ticker_yf}_{year}_{quarter}.txt"
                target_file = self.output_dir / filename

                if target_file.exists() and target_file.stat().st_size > 0:
                    logger.debug(f"File {filename} already exists. Skipping download.")
                    continue

                text = self.download_filing_text(f["url"])
                if text:
                    target_file.write_text(text, encoding="utf-8", errors="replace")
                    total_downloaded += 1
                    logger.info(f" -> Saved {filename} ({len(text.split())} words)")

        logger.info("=" * 70)
        logger.info(f"SEC EDGAR Crawl Complete! Downloaded {total_downloaded} filings to {self.output_dir}")
        logger.info("=" * 70)
        return total_downloaded


def main():
    parser = argparse.ArgumentParser(description="US SEC EDGAR Filings Crawler for Micro-Caps")
    parser.add_argument("--universe", type=str, default=str(DEFAULT_UNIVERSE_CSV), help="Path to universe CSV")
    parser.add_argument("--output-dir", type=str, default=str(DEFAULT_US_REPORTS_DIR), help="Path to output directory")
    parser.add_argument("--min-year", type=int, default=2023, help="Earliest filing year (default: 2023)")
    parser.add_argument("--max-year", type=int, default=2026, help="Latest filing year (default: 2026)")
    parser.add_argument("--max-filings", type=int, default=4, help="Max filings per ticker (default: 4)")
    args = parser.parse_args()

    crawler = SECEdgarCrawler(
        universe_csv_path=args.universe,
        output_dir=args.output_dir,
        min_year=args.min_year,
        max_year=args.max_year,
        max_filings_per_ticker=args.max_filings,
    )
    crawler.crawl_all()


if __name__ == "__main__":
    main()
