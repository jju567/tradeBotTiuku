"""
document_fetcher.py - Smart Crawler for SEC and Nordic Market Reports
Fetches ONLY the single most recent financial report to save tokens and API limits.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Optional, Dict, Any
from urllib.parse import urljoin, quote

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("screener.document_fetcher")

# SEC EDGAR requires a specific, declared User-Agent header (format: Sample Company Name AdminContact@<sample company domain>.com)
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "tradeBotTiuku_Quant/1.0 (contact: trader@bot.local)")
SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
    "Host": "data.sec.gov"
}

# Targeted keywords to identify earnings releases and interim reports in Nordic feeds
NORDIC_REPORT_KEYWORDS = [
    "osavuosikatsaus", "tilinpäätös", "tilinpäätöstiedote", "puolivuosikatsaus",
    "delårsrapport", "bokslutskommuniké", "årsredovisning",
    "interim report", "half-year report", "financial statement release",
    "annual report", "10-q", "10-k", "q1", "q2", "q3", "q4", "h1", "h2"
]


def fetch_latest_sec_report(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Fetches the single most recent 10-Q or 10-K report URL and metadata for a US stock from SEC EDGAR API.
    Converts stock ticker to 10-digit zero-padded CIK number.
    """
    clean_ticker = ticker.upper().split(".")[0].strip()
    try:
        # 1. Fetch SEC ticker-to-CIK mapping
        tickers_url = "https://www.sec.gov/files/company_tickers.json"
        tickers_resp = requests.get(tickers_url, headers={"User-Agent": SEC_USER_AGENT}, timeout=15.0)
        tickers_resp.raise_for_status()

        cik_str = None
        for item in tickers_resp.json().values():
            if item.get("ticker", "").upper() == clean_ticker:
                cik_str = str(item["cik_str"]).zfill(10)
                break

        if not cik_str:
            logger.warning(f"CIK not found for US ticker {clean_ticker}")
            return None

        # 2. Query company filings from SEC EDGAR submissions API
        submissions_url = f"https://data.sec.gov/submissions/CIK{cik_str}.json"
        sub_resp = requests.get(submissions_url, headers=SEC_HEADERS, timeout=15.0)
        sub_resp.raise_for_status()
        data = sub_resp.json()

        filings = data.get("filings", {}).get("recent", {})
        if not filings:
            return None

        # 3. Locate the single most recent 10-Q or 10-K filing
        forms = filings.get("form", [])
        accession_numbers = filings.get("accessionNumber", [])
        primary_docs = filings.get("primaryDocument", [])
        filing_dates = filings.get("filingDate", [])

        for i, form in enumerate(forms):
            if form in ["10-Q", "10-K"]:
                accession_clean = accession_numbers[i].replace("-", "")
                primary_doc = primary_docs[i]
                filing_date = filing_dates[i] if i < len(filing_dates) else ""

                report_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_str)}/{accession_clean}/{primary_doc}"

                logger.info(f"[{clean_ticker}] Discovered latest SEC report: {form} ({filing_date}) -> {report_url}")
                return {
                    "ticker": clean_ticker,
                    "market": "US",
                    "report_type": form,
                    "date": filing_date,
                    "url": report_url,
                    "title": f"{clean_ticker} {form} ({filing_date})",
                }

    except Exception as e:
        logger.error(f"Error fetching SEC report for {clean_ticker}: {e}")

    return None


def fetch_latest_nordic_report(ticker: str, market: str, company_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetches the single most recent financial interim or annual report from Nordic PR wires (Cision & MFN).
    Validates company matching to ensure no cross-company report contamination.
    """
    clean_ticker = ticker.upper().split(".")[0].strip()
    market_code = market.upper().strip()

    feeds = []
    if market_code in ["FI", "HE"]:
        feeds.append("https://publish.ne.cision.com/papi/NewsFeed/FinlandsFeed?format=rss")
        feeds.append("https://mfn.se/all/rss")
    elif market_code in ["SE", "ST"]:
        feeds.append("https://publish.ne.cision.com/papi/NewsFeed/SverigesFeed?format=rss")
        feeds.append("https://mfn.se/all/rss")
    else:
        feeds.append("https://publish.ne.cision.com/papi/NewsFeed/FinlandsFeed?format=rss")
        feeds.append("https://publish.ne.cision.com/papi/NewsFeed/SverigesFeed?format=rss")
        feeds.append("https://mfn.se/all/rss")

    match_terms = [clean_ticker.lower()]
    if company_name:
        clean_comp = re.sub(r"\b(oyj|oy|plc|ab|asa|a/s|as|corp|corporation|group)\b", "", company_name, flags=re.IGNORECASE).strip().lower()
        if clean_comp and len(clean_comp) >= 3:
            match_terms.append(clean_comp)

    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get("title", "").strip()
                title_lower = title.lower()

                # Verify company match
                matches_company = any(term in title_lower for term in match_terms)
                if not matches_company:
                    continue

                # Verify financial report keyword
                if any(kw in title_lower for kw in NORDIC_REPORT_KEYWORDS):
                    logger.info(f"[{ticker}] Discovered latest Nordic report: {title} ({entry.get('published')})")
                    return {
                        "ticker": ticker,
                        "market": market_code,
                        "report_type": "Interim/Annual Report",
                        "date": entry.get("published", ""),
                        "url": entry.get("link", ""),
                        "title": title,
                    }
        except Exception as e:
            logger.error(f"Error reading feed {feed_url}: {e}")

    logger.debug(f"[{ticker}] No recent financial report found in Nordic RSS feeds.")
    return None


def get_latest_report(ticker: str, market: Optional[str] = None, company_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Main router: Directs request based on market/ticker to SEC EDGAR (US) or Nordic PR feeds (FI/SE/NO/DK).
    """
    # Infer market if not provided
    if not market:
        if ticker.endswith(".HE"):
            market = "FI"
        elif ticker.endswith(".ST"):
            market = "SE"
        elif ticker.endswith(".OL"):
            market = "NO"
        elif ticker.endswith(".CO"):
            market = "DK"
        elif "." not in ticker:
            market = "US"
        else:
            market = "OTHER"

    market_upper = market.upper()

    if market_upper == "US":
        return fetch_latest_sec_report(ticker)
    elif market_upper in ["FI", "HE", "SE", "ST", "NO", "OL", "DK", "CO"]:
        return fetch_latest_nordic_report(ticker, market_upper, company_name=company_name)
    else:
        logger.warning(f"Unknown market '{market}' for ticker {ticker}")
        return None


if __name__ == "__main__":
    import sys
    test_ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    test_market = sys.argv[2] if len(sys.argv) > 2 else "US"
    print(f"Testing get_latest_report({test_ticker}, {test_market})...")
    res = get_latest_report(test_ticker, test_market)
    print("Result:", res)
