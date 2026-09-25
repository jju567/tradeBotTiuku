"""
screener/dynamic_universe_updater.py - Automated Periodic Dynamic Universe Updater.

Discovers new listings (IPOs, spin-offs, new Nordic micro-caps) from Nordnet,
validates their liquidity and market cap via Yahoo Finance, and dynamically appends
approved liquid candidates to data/clean_microcap_universe.csv.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import yfinance as yf

# Configure workspace base directory
_current_dir = Path(__file__).resolve().parent
BASE_DIR = _current_dir.parent if _current_dir.name == "screener" else _current_dir
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("dynamic_universe")

DEFAULT_CLEAN_UNIVERSE_CSV = BASE_DIR / "data" / "clean_microcap_universe.csv"
DEFAULT_STATUS_FILE = BASE_DIR / "data" / "universe_sync_status.json"
DEFAULT_MAX_MARKET_CAP_USD = 300_000_000.0  # $300M USD micro-cap ceiling
DEFAULT_MIN_ADV_USD = 50_000.0              # $50,000 USD / EUR minimum 20d daily turnover
DEFAULT_MIN_PRICE = 0.10                    # Penny stock protection threshold


def load_universe_sync_status(status_file: Path = DEFAULT_STATUS_FILE) -> Dict[str, Any]:
    """Loads current universe synchronization status metadata."""
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Could not read sync status: {e}")
    return {
        "last_sync_timestamp": None,
        "total_tickers": 0,
        "last_added_tickers": [],
        "last_sync_success": False,
    }


def save_universe_sync_status(status: Dict[str, Any], status_file: Path = DEFAULT_STATUS_FILE) -> None:
    """Atomically saves updated sync status metadata to JSON."""
    status_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = status_file.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2, ensure_ascii=False)
        tmp_path.replace(status_file)
    except Exception as e:
        logger.warning(f"Failed to save universe sync status: {e}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def should_update(
    max_age_days: float = 7.0,
    status_file: Path = DEFAULT_STATUS_FILE,
) -> bool:
    """
    Checks whether a periodic universe synchronization is due.
    Returns True if no prior sync exists or if elapsed time >= max_age_days.
    """
    status = load_universe_sync_status(status_file)
    last_ts = status.get("last_sync_timestamp")
    if not last_ts:
        return True
    try:
        if last_ts.endswith("Z"):
            last_ts = last_ts[:-1] + "+00:00"
        dt_last = datetime.fromisoformat(last_ts)
        if dt_last.tzinfo is None:
            dt_last = dt_last.replace(tzinfo=timezone.utc)
        elapsed_days = (datetime.now(timezone.utc) - dt_last).total_seconds() / 86400.0
        return elapsed_days >= max_age_days
    except Exception:
        return True


def get_live_fx_rates() -> Dict[str, float]:
    """Retrieves conversion rates to USD (EUR -> USD, SEK -> USD)."""
    rates = {"USD": 1.0, "EUR": 1.08, "SEK": 0.096, "NOK": 0.094, "DKK": 0.145}
    try:
        t_eur = yf.Ticker("EURUSD=X").history(period="5d")
        if not t_eur.empty:
            rates["EUR"] = float(t_eur["Close"].dropna().iloc[-1])
    except Exception:
        pass
    try:
        t_sek = yf.Ticker("SEKUSD=X").history(period="5d")
        if not t_sek.empty:
            rates["SEK"] = float(t_sek["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return rates


def validate_candidate_ticker(
    ticker: str,
    market: str = "FI",
    min_adv_usd: float = DEFAULT_MIN_ADV_USD,
    max_cap_usd: float = DEFAULT_MAX_MARKET_CAP_USD,
    min_price: float = DEFAULT_MIN_PRICE,
    fx_rates: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Validates whether a new candidate stock meets micro-cap liquidity and pricing criteria.
    Returns dictionary formatted for clean_microcap_universe.csv if approved, None otherwise.
    """
    clean_t = ticker.strip().upper()
    fx = fx_rates or get_live_fx_rates()

    # Determine currency
    currency = "USD"
    if clean_t.endswith(".HE"):
        currency = "EUR"
        market = "FI"
    elif clean_t.endswith(".ST"):
        currency = "SEK"
        market = "SE"
    elif clean_t.endswith(".CO"):
        currency = "DKK"
        market = "DK"
    elif clean_t.endswith(".OL"):
        currency = "NOK"
        market = "NO"

    try:
        t = yf.Ticker(clean_t)
        hist = t.history(period="1mo")
        if hist.empty or len(hist) < 15:
            # Insufficient trading days for a verified 20d liquid calculation
            return None

        recent_close = hist["Close"].dropna()
        recent_vol = hist["Volume"].dropna()
        if recent_close.empty or recent_vol.empty:
            return None

        current_price = float(recent_close.iloc[-1])
        if current_price < min_price:
            return None

        # Calculate 20d ADV
        dollar_vol = (recent_close * recent_vol).tail(20)
        adv_local = float(dollar_vol.mean()) if not dollar_vol.empty else 0.0

        fx_to_usd = fx.get(currency, 1.0)
        adv_usd = adv_local * fx_to_usd

        if adv_usd < min_adv_usd:
            return None

        # Fetch market cap
        fast_info = getattr(t, "fast_info", None)
        mc_local = getattr(fast_info, "market_cap", None) if fast_info else None
        if not mc_local:
            info = getattr(t, "info", {}) or {}
            mc_local = info.get("marketCap")

        mc_local_float = float(mc_local) if mc_local else (adv_local * 100)
        mc_usd = mc_local_float * fx_to_usd

        if mc_usd > max_cap_usd:
            return None

        return {
            "ticker": clean_t,
            "market": market,
            "market_cap_usd": round(mc_usd, 2),
            "market_cap_local": round(mc_local_float, 2),
            "currency": currency,
            "current_price": round(current_price, 4),
            "adv_20d_local": round(adv_local, 2),
            "adv_20d_usd": round(adv_usd, 2),
        }
    except Exception as e:
        logger.debug(f"Validation failed for candidate {clean_t}: {e}")
        return None


def scan_and_update_universe(
    clean_csv_path: Path = DEFAULT_CLEAN_UNIVERSE_CSV,
    status_file: Path = DEFAULT_STATUS_FILE,
    force: bool = False,
    max_age_days: float = 7.0,
    countries: Tuple[str, ...] = ("FI", "SE"),
) -> Tuple[int, List[str]]:
    """
    Executes dynamic universe scanning:
      1. Checks if update is due (unless force=True).
      2. Queries Nordnet API for all Nordic micro-caps.
      3. Identifies new tickers not yet in data/clean_microcap_universe.csv.
      4. Validates each new candidate through Yahoo Finance liquidity screen.
      5. Atomically appends approved new listings to clean_microcap_universe.csv.
      6. Updates status file.
    Returns: (number_of_added_tickers, list_of_added_ticker_symbols)
    """
    clean_csv = Path(clean_csv_path)
    if not force and not should_update(max_age_days=max_age_days, status_file=status_file):
        logger.info("⏳ [DYNAMIC UNIVERSE] Universe sync is up to date (last sync < 7d ago). Skipping.")
        return 0, []

    logger.info("🚀 [DYNAMIC UNIVERSE] Initiating periodic dynamic universe scan via Nordnet API...")

    existing_tickers: Set[str] = set()
    existing_rows: List[Dict[str, Any]] = []

    if clean_csv.exists():
        try:
            with open(clean_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    t = (row.get("ticker") or "").strip().upper()
                    if t:
                        existing_tickers.add(t)
                        existing_rows.append(row)
        except Exception as e:
            logger.error(f"Error reading existing universe CSV: {e}")

    logger.info(f"Loaded {len(existing_tickers)} existing tickers from {clean_csv.name}.")

    # Fetch fresh candidate list from Nordnet API
    try:
        from screener.universe_builder import fetch_nordnet_universe
        nordnet_stocks = fetch_nordnet_universe(
            countries=countries,
            output_csv_path=None,
        )
    except Exception as e:
        logger.warning(f"Could not fetch Nordnet universe: {e}. Aborting sync.")
        return 0, []

    candidates_to_eval = [
        item for item in nordnet_stocks
        if (item.get("Ticker_YF") or "").strip().upper() not in existing_tickers
    ]
    logger.info(f"Discovered {len(candidates_to_eval)} candidate tickers not in clean universe.")

    fx_rates = get_live_fx_rates()
    added_rows: List[Dict[str, Any]] = []
    added_tickers: List[str] = []

    for item in candidates_to_eval:
        ticker = item.get("Ticker_YF", "").strip().upper()
        if not ticker:
            continue

        validated = validate_candidate_ticker(
            ticker=ticker,
            market=item.get("Market", "FI"),
            fx_rates=fx_rates,
        )
        if validated:
            logger.info(f"✨ [NEW LISTING APPROVED] {ticker} passed liquidity screen (ADV: ${validated['adv_20d_usd']:,.0f}).")
            added_rows.append(validated)
            added_tickers.append(ticker)

    # Save additions to CSV atomically
    if added_rows:
        all_rows = existing_rows + added_rows
        tmp_csv = clean_csv.with_suffix(".tmp")
        try:
            fieldnames = [
                "ticker", "market", "market_cap_usd", "market_cap_local",
                "currency", "current_price", "adv_20d_local", "adv_20d_usd"
            ]
            with open(tmp_csv, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for r in all_rows:
                    writer.writerow({k: r.get(k, "") for k in fieldnames})
            tmp_csv.replace(clean_csv)
            logger.info(f"✅ [DYNAMIC UNIVERSE] Appended {len(added_rows)} new stocks to {clean_csv.name} (Total: {len(all_rows)}).")
        except Exception as e:
            logger.error(f"Failed to save updated universe CSV: {e}")
            if tmp_csv.exists():
                tmp_csv.unlink(missing_ok=True)
            return 0, []
    else:
        logger.info("ℹ️ [DYNAMIC UNIVERSE] Scan completed. No new candidates met liquidity threshold (ADV >= $50k).")

    # Update status metadata
    total_count = len(existing_tickers) + len(added_tickers)
    status_payload = {
        "last_sync_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_tickers": total_count,
        "last_added_tickers": added_tickers,
        "last_sync_success": True,
    }
    save_universe_sync_status(status_payload, status_file=status_file)

    return len(added_tickers), added_tickers
