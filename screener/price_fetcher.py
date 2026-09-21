"""
price_fetcher.py - Real-Time Stock Price and Market Capitalization Data Fetcher
Uses yfinance to retrieve spot prices, market cap, and currencies for US and Nordic equities.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Any, Optional

import yfinance as yf

logger = logging.getLogger("screener.price_fetcher")

# Mapping from market identifiers to standard Yahoo Finance suffixes
MARKET_SUFFIX_MAP = {
    "FI": ".HE",
    "HE": ".HE",
    "HELSINKI": ".HE",
    "SE": ".ST",
    "ST": ".ST",
    "STOCKHOLM": ".ST",
    "NO": ".OL",
    "OL": ".OL",
    "OSLO": ".OL",
    "DK": ".CO",
    "CO": ".CO",
    "COPENHAGEN": ".CO",
    "US": "",
    "USA": "",
    "NASDAQ": "",
    "NYSE": "",
}


def normalize_ticker(ticker: str, market: Optional[str] = None) -> str:
    """
    Normalizes a ticker symbol to a Yahoo Finance compatible ticker with correct country suffix.
    Examples:
      - normalize_ticker("AAPL", "US") -> "AAPL"
      - normalize_ticker("VINCIT", "FI") -> "VINCIT.HE"
      - normalize_ticker("VINCIT.HE", "FI") -> "VINCIT.HE"
      - normalize_ticker("EVO", "SE") -> "EVO.ST"
      - normalize_ticker("NEL", "NO") -> "NEL.OL"
    """
    clean_ticker = ticker.strip().upper()
    market_clean = (market or "").strip().upper()

    # If the ticker already contains a recognized Nordic suffix, return as-is
    for suffix in [".HE", ".ST", ".OL", ".CO"]:
        if clean_ticker.endswith(suffix):
            return clean_ticker

    # Check if market code is mapped
    if market_clean in MARKET_SUFFIX_MAP:
        suffix = MARKET_SUFFIX_MAP[market_clean]
        # Remove any existing unexpected suffix if appending
        base_ticker = clean_ticker.split(".")[0]
        return f"{base_ticker}{suffix}" if suffix else base_ticker

    # If no market provided or unrecognized, treat as US or raw ticker
    return clean_ticker


def get_realtime_data(ticker: str, market: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetches real-time price, market cap, and currency using yfinance.

    Args:
        ticker: The stock ticker symbol (e.g. "AAPL", "VINCIT.HE", "EVO").
        market: Optional market identifier (e.g. "US", "FI", "SE", "NO", "DK").

    Returns:
        dict containing:
          - ticker: Normalized Yahoo Finance ticker string
          - raw_ticker: Original input ticker
          - market: Identified or supplied market code
          - current_price: Latest spot or closing price (float)
          - market_cap: Total market capitalization in base currency (float)
          - currency: Currency symbol (e.g. 'USD', 'EUR', 'SEK', 'NOK', 'DKK')
          - previous_close: Previous trading session closing price (float)
          - day_change_pct: Percent change today (float)
          - status: 'SUCCESS' or 'ERROR'
          - error: Error description string (only if status == 'ERROR')
    """
    norm_ticker = normalize_ticker(ticker, market)
    
    result: Dict[str, Any] = {
        "ticker": norm_ticker,
        "raw_ticker": ticker,
        "market": market or ("US" if "." not in norm_ticker else norm_ticker.split(".")[-1]),
        "current_price": None,
        "market_cap": None,
        "currency": None,
        "previous_close": None,
        "day_change_pct": None,
        "status": "ERROR",
        "error": None,
    }

    try:
        yf_ticker = yf.Ticker(norm_ticker)
        
        # 1. Fast path: fetch fast_info
        price = None
        market_cap = None
        currency = None
        prev_close = None

        try:
            fast_info = getattr(yf_ticker, "fast_info", None)
            if fast_info:
                price = getattr(fast_info, "last_price", None) or getattr(fast_info, "regular_market_price", None)
                market_cap = getattr(fast_info, "market_cap", None)
                currency = getattr(fast_info, "currency", None)
                prev_close = getattr(fast_info, "previous_close", None) or getattr(fast_info, "regular_market_previous_close", None)
        except Exception as fast_err:
            logger.debug(f"fast_info lookup failed for {norm_ticker}: {fast_err}")

        # 2. Fallback to info dict if fast_info is missing essential data
        if price is None or currency is None:
            try:
                info = yf_ticker.info or {}
                if not price:
                    price = (
                        info.get("currentPrice")
                        or info.get("regularMarketPrice")
                        or info.get("ask")
                        or info.get("bid")
                        or info.get("previousClose")
                    )
                if not market_cap:
                    market_cap = info.get("marketCap")
                if not currency:
                    currency = info.get("currency")
                if not prev_close:
                    prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
            except Exception as info_err:
                logger.debug(f"info lookup fallback failed for {norm_ticker}: {info_err}")

        # 3. Fallback to historical intraday bars (1d) if still no price
        if price is None:
            try:
                hist = yf_ticker.history(period="5d", interval="1d")
                if not hist.empty and "Close" in hist.columns:
                    valid_closes = hist["Close"].dropna()
                    if not valid_closes.empty:
                        price = float(valid_closes.iloc[-1])
                        if len(valid_closes) > 1:
                            prev_close = float(valid_closes.iloc[-2])
            except Exception as hist_err:
                logger.debug(f"history lookup fallback failed for {norm_ticker}: {hist_err}")

        # Validate that we retrieved a valid spot price
        if price is None or price <= 0:
            result["error"] = f"No valid pricing data returned for ticker '{norm_ticker}' (possibly delisted or suspended)."
            logger.warning(f"[{norm_ticker}] {result['error']}")
            return result

        # Compute intraday percent change if possible
        day_change_pct = None
        if price is not None and prev_close is not None and prev_close > 0:
            day_change_pct = round(((price - prev_close) / prev_close) * 100.0, 2)

        # Populate success result
        result["current_price"] = float(price)
        result["market_cap"] = float(market_cap) if market_cap else None
        result["currency"] = str(currency).upper() if currency else ("EUR" if norm_ticker.endswith(".HE") else ("SEK" if norm_ticker.endswith(".ST") else "USD"))
        result["previous_close"] = float(prev_close) if prev_close else None
        result["day_change_pct"] = day_change_pct
        result["status"] = "SUCCESS"
        result["error"] = None

        return result

    except Exception as e:
        err_msg = f"Unexpected error fetching real-time data for '{norm_ticker}': {e}"
        logger.error(err_msg, exc_info=True)
        result["error"] = err_msg
        return result


if __name__ == "__main__":
    import sys
    test_tick = sys.argv[1] if len(sys.argv) > 1 else "VINCIT"
    test_mkt = sys.argv[2] if len(sys.argv) > 2 else "FI"
    print(f"Testing get_realtime_data('{test_tick}', '{test_mkt}')...")
    res = get_realtime_data(test_tick, test_mkt)
    print("\nResult:")
    for k, v in res.items():
        print(f"  {k}: {v}")
