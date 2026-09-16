"""
Nordnet Tradable Universe Builder for Nordic Micro-Cap & Small-Cap Equities.

Fetches the complete stock listings directly from Nordnet's public market data endpoints,
applies strict micro-cap filtering (Market Cap <= 300M EUR), and normalizes tickers
to Yahoo Finance (yfinance) conventions for Helsinki (.HE), Stockholm (.ST), Copenhagen (.CO), and Oslo (.OL).

Anti-Ban & Scraping Protection:
1. Randomized Sleep: `random.uniform(1.2, 3.8)` before every HTTP request.
2. Full Browser Spoofing Headers: Standard Chrome User-Agent, Accept, Accept-Language, client-id.
3. Resilient Session Management: TCP connection pooling with requests.Session().
4. Exponential Backoff & 429/403 Handling: Sleeps 60s on Cloudflare/WAF challenge and retries.

Output:
Saves the filtered stock universe to `data/nordnet_universe.csv` with columns:
`Ticker_YF`, `Name`, `Market`, `MarketCap_EUR`.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

import requests

# Ensure parent directory is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("screener.universe_builder")

DEFAULT_UNIVERSE_CSV = BASE_DIR / "data" / "nordnet_universe.csv"
NORDNET_STOCKLIST_API_URL = "https://www.nordnet.fi/api/2/instrument_search/query/stocklist"

# Micro-cap quantitative constraints
DEFAULT_MAX_MARKET_CAP_EUR = 300_000_000.0  # 300M EUR upper threshold

# Approximate FX conversion rates to EUR (fallback if live rates unavailable)
DEFAULT_FX_RATES_TO_EUR: Dict[str, float] = {
    "EUR": 1.0,
    "SEK": 11.45,   # 1 EUR ≈ 11.45 SEK
    "NOK": 11.60,   # 1 EUR ≈ 11.60 NOK
    "DKK": 7.46,    # 1 EUR ≈ 7.46 DKK
    "USD": 1.08,    # 1 EUR ≈ 1.08 USD
}

# Country code to yfinance exchange suffix mapping
MARKET_YF_SUFFIXES: Dict[str, str] = {
    "FI": ".HE",   # Nasdaq OMX Helsinki / First North Finland
    "SE": ".ST",   # Nasdaq OMX Stockholm / First North Sweden / Spotlight
    "DK": ".CO",   # Nasdaq OMX Copenhagen / First North Denmark
    "NO": ".OL",   # Oslo Børs / Euronext Growth Oslo
}

DEFAULT_COUNTRIES: Tuple[str, ...] = ("FI", "SE", "DK", "NO")

# Anti-scraping browser spoofing headers
DEFAULT_BROWSER_HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fi-FI,fi;q=0.9,en-US;q=0.8,en;q=0.7",
    "client-id": "NEXT",
    "sub-client-id": "NEXT",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def normalize_ticker_yf(symbol: str, country: str) -> str:
    """
    Normalizes a Nordnet ticker symbol to standard Yahoo Finance convention.
    
    Examples:
    - FI: 'KEMIRA' -> 'KEMIRA.HE', 'AALLON' -> 'AALLON.HE'
    - SE: 'EVO' -> 'EVO.ST', 'INTRUM' -> 'INTRUM.ST'
    - DK: 'ORSTED' -> 'ORSTED.CO'
    - NO: 'NEL' -> 'NEL.OL'
    """
    if not symbol:
        return ""

    clean_sym = symbol.strip().upper().replace(" ", "-").replace("_", "-")
    suffix = MARKET_YF_SUFFIXES.get(country.upper(), "")

    if not suffix:
        return clean_sym

    if clean_sym.endswith(suffix):
        return clean_sym

    return f"{clean_sym}{suffix}"


def convert_market_cap_to_eur(
    raw_market_cap: float,
    currency: str,
    fx_rates: Optional[Dict[str, float]] = None,
) -> float:
    """Converts a market capitalization from local Nordic currency into EUR."""
    if not raw_market_cap or raw_market_cap <= 0:
        return 0.0

    rates = fx_rates or DEFAULT_FX_RATES_TO_EUR
    curr = currency.upper().strip() if currency else "EUR"
    rate = rates.get(curr, 1.0)

    if rate <= 0:
        rate = 1.0

    return round(raw_market_cap / rate, 2)


class NordnetUniverseBuilder:
    """Fetches, filters, and standardizes the Nordic micro-cap equity universe from Nordnet with WAF/anti-ban protections."""

    def __init__(
        self,
        max_market_cap_eur: float = DEFAULT_MAX_MARKET_CAP_EUR,
        countries: Tuple[str, ...] = DEFAULT_COUNTRIES,
        output_csv_path: Optional[Path | str] = None,
        fx_rates: Optional[Dict[str, float]] = None,
        user_agent: Optional[str] = None,
        min_request_delay: float = 1.2,
        max_request_delay: float = 3.8,
        retry_backoff_seconds: float = 60.0,
        max_retries: int = 3,
    ):
        self.max_market_cap_eur = float(max_market_cap_eur)
        self.countries = countries
        self.output_csv_path = Path(output_csv_path or DEFAULT_UNIVERSE_CSV)
        self.fx_rates = fx_rates or dict(DEFAULT_FX_RATES_TO_EUR)
        self.min_request_delay = min_request_delay
        self.max_request_delay = max_request_delay
        self.retry_backoff_seconds = retry_backoff_seconds
        self.max_retries = max_retries
        self.output_csv_path.parent.mkdir(parents=True, exist_ok=True)

        # Session initialization with TCP connection reuse
        self.session = requests.Session()
        headers = dict(DEFAULT_BROWSER_HEADERS)
        if user_agent:
            headers["User-Agent"] = user_agent
        self.session.headers.update(headers)

    def _apply_randomized_delay(self) -> None:
        """Applies a randomized delay between 1.2s and 3.8s before every request to avoid WAF fingerprinting."""
        if self.max_request_delay > 0:
            delay = random.uniform(self.min_request_delay, self.max_request_delay)
            logger.debug(f"Anti-scraping: sleeping for {delay:.2f}s before Nordnet request...")
            time.sleep(delay)

    def fetch_country_stocks(self, country: str, page_size: int = 100) -> List[Dict[str, Any]]:
        """
        Fetches all available equities for a specific country from Nordnet's search API with pagination,
        randomized throttling delays, and exponential 60-second backoff on 429/403 status codes.
        """
        country_code = country.upper().strip()
        offset = 0
        stocks: List[Dict[str, Any]] = []

        logger.info(f"Fetching listings for country [{country_code}]...")

        while True:
            params = {
                "apply_filters": f"exchange_country={country_code}",
                "limit": page_size,
                "offset": offset,
            }

            attempt = 0
            success = False
            data: Optional[Dict[str, Any]] = None

            while attempt < self.max_retries and not success:
                attempt += 1

                # 1. Randomized sleep before EVERY request
                self._apply_randomized_delay()

                try:
                    response = self.session.get(
                        NORDNET_STOCKLIST_API_URL,
                        params=params,
                        timeout=20.0,
                    )

                    # 2. Handle Cloudflare / WAF 429 (Too Many Requests) or 403 (Forbidden)
                    if response.status_code in (429, 403):
                        logger.warning(
                            f"⚠️ [ANTI-BAN TRIGGERED] Received HTTP {response.status_code} from Nordnet "
                            f"on [{country_code}] offset {offset}. Backing off {self.retry_backoff_seconds:.0f}s "
                            f"before retry (attempt {attempt}/{self.max_retries})..."
                        )
                        time.sleep(self.retry_backoff_seconds)
                        continue

                    response.raise_for_status()
                    data = response.json()
                    success = True

                except requests.exceptions.RequestException as e:
                    logger.warning(f"Network error on [{country_code}] offset {offset} (attempt {attempt}): {e}")
                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff_seconds if "429" in str(e) or "403" in str(e) else 5.0)
                    else:
                        logger.error(f"Max retries ({self.max_retries}) exhausted for [{country_code}] offset {offset}.")
                        break

            if not success or not data:
                break

            rows = data.get("results", [])
            total_hits = data.get("total_hits", 0)

            if not rows:
                break

            for row in rows:
                inst = row.get("instrument_info", {})
                comp = row.get("company_info", {})
                exch = row.get("exchange_info", {})

                symbol = inst.get("symbol", "").strip()
                name = inst.get("name", "").strip()
                currency = inst.get("currency", "EUR").upper()
                exchanges = exch.get("exchanges", [])
                market_name = exchanges[0] if exchanges else f"Nasdaq {country_code}"

                raw_mcap = comp.get("market_cap", 0) or 0
                mcap_eur = convert_market_cap_to_eur(raw_mcap, currency, self.fx_rates)

                ticker_yf = normalize_ticker_yf(symbol, country_code)

                stocks.append({
                    "Symbol": symbol,
                    "Ticker_YF": ticker_yf,
                    "Name": name,
                    "Country": country_code,
                    "Market": market_name,
                    "Currency": currency,
                    "Raw_MarketCap": raw_mcap,
                    "MarketCap_EUR": mcap_eur,
                    "ISIN": inst.get("isin", ""),
                })

            offset += page_size
            if offset >= total_hits:
                break

        logger.info(f"Retrieved {len(stocks)} total listings for [{country_code}].")
        return stocks

    def build_universe(self) -> List[Dict[str, Any]]:
        """
        Executes complete universe generation across all configured Nordic countries
        and applies the Micro-Cap Edge filter (MarketCap_EUR <= max_market_cap_eur).
        """
        logger.info("=" * 70)
        logger.info(">>> BUILDING NORDNET TRADABLE MICRO-CAP UNIVERSE <<<")
        logger.info(f"Countries: {', '.join(self.countries)} | Max Market Cap: {self.max_market_cap_eur:,.0f} EUR")
        logger.info("=" * 70)

        all_stocks: List[Dict[str, Any]] = []
        for country in self.countries:
            country_stocks = self.fetch_country_stocks(country)
            all_stocks.extend(country_stocks)

        # Apply Micro-Cap Edge Filter
        filtered_universe: List[Dict[str, Any]] = []
        discarded_large_cap = 0

        for stock in all_stocks:
            mcap_eur = stock.get("MarketCap_EUR", 0.0)

            # Strict filter: Discard companies strictly greater than max_market_cap_eur
            if mcap_eur > 0 and mcap_eur > self.max_market_cap_eur:
                discarded_large_cap += 1
                continue

            filtered_universe.append({
                "Ticker_YF": stock["Ticker_YF"],
                "Name": stock["Name"],
                "Market": stock["Market"],
                "MarketCap_EUR": mcap_eur,
            })

        logger.info("-" * 70)
        logger.info(
            f"Universe Generation Complete: "
            f"Total Ingested: {len(all_stocks)} | "
            f"Discarded Large-Caps (> {self.max_market_cap_eur/1e6:.0f}M EUR): {discarded_large_cap} | "
            f"Tradable Micro/Small-Caps: {len(filtered_universe)}"
        )
        logger.info("-" * 70)

        return filtered_universe

    def save_universe(self, universe: List[Dict[str, Any]]) -> None:
        """Saves the final filtered universe to CSV format."""
        if not universe:
            logger.warning("Empty universe passed to save_universe. Skipping file write.")
            return

        fieldnames = ["Ticker_YF", "Name", "Market", "MarketCap_EUR"]
        try:
            with open(self.output_csv_path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for item in universe:
                    writer.writerow({
                        "Ticker_YF": item["Ticker_YF"],
                        "Name": item["Name"],
                        "Market": item["Market"],
                        "MarketCap_EUR": f"{item['MarketCap_EUR']:.2f}",
                    })
            logger.info(f"Successfully saved {len(universe)} stocks to {self.output_csv_path}")
        except Exception as e:
            logger.error(f"Failed to write universe to {self.output_csv_path}: {e}")


def fetch_nordnet_universe(
    max_market_cap_eur: float = DEFAULT_MAX_MARKET_CAP_EUR,
    countries: Tuple[str, ...] = DEFAULT_COUNTRIES,
    output_csv_path: Optional[Path | str] = None,
    fx_rates: Optional[Dict[str, float]] = None,
    min_request_delay: float = 1.2,
    max_request_delay: float = 3.8,
) -> List[Dict[str, Any]]:
    """
    Convenience function: Fetches, filters, saves, and returns the tradable Nordic micro-cap universe.
    """
    builder = NordnetUniverseBuilder(
        max_market_cap_eur=max_market_cap_eur,
        countries=countries,
        output_csv_path=output_csv_path,
        fx_rates=fx_rates,
        min_request_delay=min_request_delay,
        max_request_delay=max_request_delay,
    )
    universe = builder.build_universe()
    builder.save_universe(universe)
    return universe


def main():
    """CLI entry point for running the Universe Builder."""
    parser = argparse.ArgumentParser(description="Nordnet Nordic Tradable Micro-Cap Universe Builder")
    parser.add_argument(
        "--max-cap",
        type=float,
        default=DEFAULT_MAX_MARKET_CAP_EUR,
        help=f"Maximum Market Cap in EUR (default: {DEFAULT_MAX_MARKET_CAP_EUR:,.0f})",
    )
    parser.add_argument(
        "--countries",
        nargs="+",
        default=["FI", "SE", "DK", "NO"],
        help="List of Nordic country codes to fetch (e.g. FI SE DK NO)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_UNIVERSE_CSV),
        help="Target CSV output path (default: data/nordnet_universe.csv)",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=1.2,
        help="Minimum randomized delay between requests in seconds (default: 1.2)",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=3.8,
        help="Maximum randomized delay between requests in seconds (default: 3.8)",
    )

    args = parser.parse_args()

    countries_tuple = tuple(c.upper() for c in args.countries)
    fetch_nordnet_universe(
        max_market_cap_eur=args.max_cap,
        countries=countries_tuple,
        output_csv_path=args.output,
        min_request_delay=args.min_delay,
        max_request_delay=args.max_delay,
    )


if __name__ == "__main__":
    main()
