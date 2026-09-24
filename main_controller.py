"""
main_controller.py - Master Live Daemon for Multi-Portfolio Live Walk-Forward Testing.

Orchestrates 10 parallel paper-trading portfolios with independent parameterizations:
1. Shared Market Data Ingestion (Fetched ONCE per cycle):
   - Ingests data/clean_microcap_universe.csv (106 liquid micro-caps).
   - Fetches live quotes, 20d ADV, and exchange rates strictly ONCE.
   - Pre-computes deterministic financials and LLM/rule-based news radar once per cycle.
2. Multi-Portfolio Execution (P1_Base to P10_Micro_Sniper):
   - Defined in portfolios_config.yaml.
   - Independent state files: data/portfolios/portfolio_<id>_state.json
   - Independent trade logs: data/portfolios/portfolio_<id>_history.csv
   - Independent equity snapshots: data/portfolios/portfolio_<id>_history.json
3. Parameterized Rules per Portfolio:
   - Strategy: Profile B (Deep Value) vs Profile A (Quality Growth)
   - Dead Money Days: 90d, 180d, or 365d
   - Minimum ADV: 50k, 150k, or 250k EUR/USD
   - Position Sizing & Slots: start_cash / slots (e.g., 5 slots = 2,000€, 10 slots = 1,000€, 20 slots = 500€)
   - Geographic Universe: Nordic Only (FI, SE), US Only, or All (FI, SE, US)
   - Extra Filters: e.g. price_to_cash < 0.5
4. Real-Time Email Alerts via email_notifier.py:
   - Dispatches BUY, SELL, and WARN alerts per portfolio with exact ID tagging.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import yfinance as yf
import yaml

# Configure cross-platform terminal encoding (Windows cp1252 fix)
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

# Ensure workspace root is in sys.path
_current_dir = Path(__file__).resolve().parent
BASE_DIR = _current_dir.parent if _current_dir.name == "screener" else _current_dir
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from email_notifier import send_portfolio_alert, send_run_summary_email
from screener.financial_metrics_engine import evaluate_profiles, get_hard_financials
from screener.nlp_analyzer import rule_based_analyze_core_fundamentals
from screener.portfolio_manager import evaluate_position
from screener.web_verifier import FATAL_RED_FLAG_PATTERNS, RED_FLAG_PATTERNS, WARN_PATTERNS

# Backwards compatibility exports
try:
    from screener.main_controller import (
        ScreenerPipelineController,
        load_nordnet_universe,
        match_universe_item,
    )
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("master_daemon")

# Path Constants
DEFAULT_PORTFOLIOS_YAML = BASE_DIR / "portfolios_config.yaml"
PORTFOLIOS_DIR = BASE_DIR / "data" / "portfolios"
DEFAULT_CLEAN_UNIVERSE_CSV = BASE_DIR / "data" / "clean_microcap_universe.csv"
DEFAULT_NLP_ARCHIVE_CSV = BASE_DIR / "data" / "nlp_decisions_archive.csv"
DEFAULT_OPERATIONAL_METRICS_JSON = BASE_DIR / "data" / "operational_metrics.json"

# Legacy fallback paths for backwards compatibility
DEFAULT_PAPER_ACCOUNT_JSON = BASE_DIR / "data" / "paper_account.json"
DEFAULT_OPEN_POSITIONS_CSV = BASE_DIR / "data" / "open_positions.csv"
DEFAULT_TRADE_HISTORY_CSV = BASE_DIR / "data" / "trade_history.csv"
DEFAULT_PORTFOLIO_HISTORY_JSON = BASE_DIR / "data" / "portfolio_history.json"


def load_operational_metrics(metrics_path: Path = DEFAULT_OPERATIONAL_METRICS_JSON) -> Dict[str, Any]:
    """Loads lightweight operational health counters (LLM fallback rate, data freshness blocks)."""
    default_metrics = {
        "nlp_evaluations_total": 0,
        "llm_calls_attempted": 0,
        "llm_fallbacks": 0,
        "llm_fallback_rate_pct": 0.0,
        "freshness_checks_total": 0,
        "freshness_blocks": 0,
        "freshness_block_rate_pct": 0.0,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "status": "HEALTHY",
    }
    if metrics_path.exists():
        try:
            with open(metrics_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                default_metrics.update(saved)
        except Exception as e:
            logger.debug(f"Could not load operational metrics from {metrics_path}: {e}")
    return default_metrics


def save_operational_metrics(metrics: Dict[str, Any], metrics_path: Path = DEFAULT_OPERATIONAL_METRICS_JSON) -> None:
    """Saves updated operational health counters to JSON disk storage."""
    try:
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        llm_attempts = max(metrics.get("llm_calls_attempted", 0), metrics.get("nlp_evaluations_total", 0))
        fallbacks = metrics.get("llm_fallbacks", 0)
        metrics["llm_fallback_rate_pct"] = round((fallbacks / max(llm_attempts, 1)) * 100.0, 1)

        f_checks = metrics.get("freshness_checks_total", 0)
        f_blocks = metrics.get("freshness_blocks", 0)
        metrics["freshness_block_rate_pct"] = round((f_blocks / max(f_checks, 1)) * 100.0, 1)
        metrics["last_updated"] = datetime.now(timezone.utc).isoformat()

        if llm_attempts < 10 or f_checks < 10:
            metrics["status"] = "INITIALIZING (Pieni otos — odottaa syklejä)"
        elif metrics["llm_fallback_rate_pct"] > 30.0:
            metrics["status"] = "DEGRADED (High LLM Fallback)"
        elif metrics["freshness_block_rate_pct"] > 30.0:
            metrics["status"] = "DATA STALE (High Latency)"
        else:
            metrics["status"] = "HEALTHY"

        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save operational metrics to {metrics_path}: {e}")


def append_nlp_decision(
    ticker: str,
    headline: str,
    llm_decision: str,
    reasoning: str,
    archive_path: Optional[Path] = None,
    timestamp: Optional[str] = None,
) -> None:
    """
    Appends an evaluated press release / news decision to the permanent NLP decision archive CSV.
    Header: Timestamp, Ticker, Headline, LLM_Decision, Reasoning
    """
    clean_headline = " ".join(str(headline).split()) if headline else ""
    if not clean_headline:
        # Strictly skip writing empty headlines to the archive CSV
        return

    target_path = archive_path or DEFAULT_NLP_ARCHIVE_CSV
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = target_path.exists()
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        # Sanitize single-line strings
        clean_ticker = str(ticker).strip().upper()
        clean_decision = str(llm_decision).strip().upper()
        clean_reasoning = " ".join(str(reasoning).split())

        with open(target_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if not file_exists or target_path.stat().st_size == 0:
                writer.writerow(["Timestamp", "Ticker", "Headline", "LLM_Decision", "Reasoning"])
            writer.writerow([ts, clean_ticker, clean_headline, clean_decision, clean_reasoning])
    except Exception as e:
        logger.warning(f"Failed to record NLP decision to {target_path}: {e}")


# Strict Fee & Execution Parameters
MIN_BROKER_FEE = 9.00           # Flat minimum broker commission ($9.00 / €9.00)
VARIABLE_BROKER_FEE_PCT = 0.002 # 0.20% variable broker commission
MAX_ADV_ALLOCATION_PCT = 0.10   # Max 10% of 20-day ADV to protect order book liquidity
REENTRY_COOLDOWN_DAYS = 30      # 30-day anti-whipsaw cooldown after selling a position
STARTING_BALANCE = 10_000.0     # Default starting balance per portfolio


def get_live_fx_rates() -> Dict[str, float]:
    """Fetches live FX rates EURUSD and EURSEK from Yahoo Finance with fallback."""
    fx_dict = {"EURUSD": 1.08, "EURSEK": 11.30}
    try:
        t_usd = yf.Ticker("EURUSD=X").history(period="5d")
        if not t_usd.empty:
            fx_dict["EURUSD"] = float(t_usd["Close"].dropna().iloc[-1])
    except Exception:
        pass
    try:
        t_sek = yf.Ticker("EURSEK=X").history(period="5d")
        if not t_sek.empty:
            fx_dict["EURSEK"] = float(t_sek["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return fx_dict


def get_ticker_currency(ticker: str, known_currency: Optional[str] = None) -> str:
    """Resolves local currency for ticker: EUR (.HE), SEK (.ST), or USD."""
    if known_currency and isinstance(known_currency, str) and known_currency.strip():
        return known_currency.strip().upper()
    t_clean = str(ticker).strip().upper()
    if t_clean.endswith(".HE"):
        return "EUR"
    if t_clean.endswith(".ST"):
        return "SEK"
    return "USD"


def get_fx_to_account(
    ticker_currency: str,
    account_currency: str = "EUR",
    fx_rates: Optional[Dict[str, float]] = None,
) -> float:
    """
    Returns multiplier to convert an amount in ticker_currency to account_currency.
    amount_in_account_currency = amount_in_ticker_currency * fx_to_account
    """
    ticker_curr = ticker_currency.strip().upper()
    acc_curr = account_currency.strip().upper()
    if ticker_curr == acc_curr:
        return 1.0

    rates = fx_rates or get_live_fx_rates()
    eur_usd = rates.get("EURUSD", 1.08)
    eur_sek = rates.get("EURSEK", 11.30)

    if ticker_curr == "EUR":
        to_eur = 1.0
    elif ticker_curr == "USD":
        to_eur = 1.0 / eur_usd
    elif ticker_curr == "SEK":
        to_eur = 1.0 / eur_sek
    else:
        to_eur = 1.0

    if acc_curr == "EUR":
        return to_eur
    elif acc_curr == "USD":
        return to_eur * eur_usd
    elif acc_curr == "SEK":
        return to_eur * eur_sek

    return to_eur


def calculate_transaction_fee(gross_value: float, min_fee: float = MIN_BROKER_FEE) -> float:
    """Calculates realistic transaction fee: max(Gross Value * 0.20%, min_fee)."""
    return max(gross_value * VARIABLE_BROKER_FEE_PCT, min_fee)


@dataclass
class PortfolioConfig:
    portfolio_id: str
    strategy: str = "Profile B"
    dead_money_days: int = 180
    min_adv: float = 50000.0
    slots: int = 10
    start_cash: float = 10000.0
    regions: List[str] = field(default_factory=lambda: ["FI", "SE", "US"])
    extra_filter: Optional[str] = None

    @classmethod
    def from_dict(cls, pid: str, data: Dict[str, Any]) -> "PortfolioConfig":
        return cls(
            portfolio_id=pid,
            strategy=data.get("strategy", "Profile B"),
            dead_money_days=int(data.get("dead_money_days", 180)),
            min_adv=float(data.get("min_adv", 50000.0)),
            slots=int(data.get("slots", 10)),
            start_cash=float(data.get("start_cash", 10000.0)),
            regions=data.get("regions", ["FI", "SE", "US"]),
            extra_filter=data.get("extra_filter"),
        )


class PortfolioInstance:
    """
    Manages state and trade log for a single portfolio in data/portfolios/.
    State JSON contains cash balance, starting balance, currency, timestamps, and open positions.
    """

    def __init__(self, config: PortfolioConfig, base_dir: Path = BASE_DIR):
        self.config = config
        self.portfolio_dir = base_dir / "data" / "portfolios"
        self.portfolio_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.portfolio_dir / f"portfolio_{config.portfolio_id}_state.json"
        self.history_file = self.portfolio_dir / f"portfolio_{config.portfolio_id}_history.csv"
        self.equity_file = self.portfolio_dir / f"portfolio_{config.portfolio_id}_history.json"

        self.cash_balance = float(config.start_cash)
        self.starting_balance = float(config.start_cash)
        self.currency = "EUR"
        self.positions: List[Dict[str, Any]] = []
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at

        self.ensure_history_csv()
        self.load_state()

    def ensure_history_csv(self) -> None:
        """Ensures trade history CSV has standard header."""
        header = [
            "Ticker", "Buy Date", "Sell Date", "Buy Price", "Sell Price",
            "Shares", "Capital Invested", "Gross Sale Value", "Transaction Fee",
            "Net Return", "Net PnL", "Exit Reason"
        ]
        if not self.history_file.exists():
            with open(self.history_file, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(header)

    def load_state(self) -> None:
        """Loads state from JSON or initializes default."""
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.cash_balance = float(data.get("cash_balance", self.config.start_cash))
                    self.starting_balance = float(data.get("starting_balance", self.config.start_cash))
                    self.currency = data.get("currency", "EUR")
                    self.positions = [self._normalize_position(p) for p in data.get("positions", [])]
                    self.created_at = data.get("created_at", self.created_at)
                    self.updated_at = data.get("updated_at", self.updated_at)
                    return
            except Exception as e:
                logger.warning(f"[{self.config.portfolio_id}] Could not load {self.state_file.name}: {e}. Initializing.")

        self.save_state()

    @staticmethod
    def _normalize_position(pos: Dict[str, Any]) -> Dict[str, Any]:
        """Ensures position dict uses canonical Title Case keys regardless of source format."""
        # Build a lowercase lookup for flexible key matching
        lower = {k.lower().replace(" ", "_").replace("-", "_"): v for k, v in pos.items()}
        def _get(*candidates):
            for c in candidates:
                if c in lower:
                    return lower[c]
            return None

        normalized = {
            "Ticker":           _get("ticker") or pos.get("Ticker", ""),
            "Buy Date":         _get("buy_date", "buy date") or pos.get("Buy Date", ""),
            "Buy Price":        float(_get("buy_price", "buy price") or pos.get("Buy Price", 0.0)),
            "Shares":           float(_get("shares") or pos.get("Shares", 0.0)),
            "Capital Invested": float(_get("capital_invested", "capital invested") or pos.get("Capital Invested", 0.0)),
            "Strategy":         _get("strategy") or pos.get("Strategy", "Profile B"),
            "Currency":         _get("currency") or pos.get("Currency", "EUR"),
        }
        # Preserve any extra keys (highest_price_seen, catastrophic_stop, market, etc.)
        canonical_lower = {"ticker", "buy_date", "buy price", "buy_price", "shares",
                           "capital_invested", "capital invested", "strategy", "currency"}
        for k, v in pos.items():
            k_norm = k.lower().replace(" ", "_")
            if k_norm not in canonical_lower and k not in normalized:
                normalized[k] = v
        return normalized


    def save_state(self) -> None:
        """Saves current state to JSON file."""
        self.updated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "portfolio_id": self.config.portfolio_id,
            "starting_balance": self.starting_balance,
            "cash_balance": round(self.cash_balance, 2),
            "currency": self.currency,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "positions": self.positions,
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def reset(self) -> None:
        """Resets portfolio state to starting cash and empty positions."""
        self.cash_balance = self.config.start_cash
        self.starting_balance = self.config.start_cash
        self.positions = []
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.save_state()
        if self.history_file.exists():
            try:
                self.history_file.unlink()
            except Exception:
                pass
        self.ensure_history_csv()
        if self.equity_file.exists():
            try:
                self.equity_file.unlink()
            except Exception:
                pass
        logger.info(f"🔄 [{self.config.portfolio_id}] Reset state to {self.cash_balance:,.2f} {self.currency}")

    def append_trade(self, trade: Dict[str, Any]) -> None:
        """Appends closed trade to trade history CSV."""
        with open(self.history_file, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                trade["Ticker"],
                trade["Buy Date"],
                trade["Sell Date"],
                f"{trade['Buy Price']:.4f}",
                f"{trade['Sell Price']:.4f}",
                int(trade["Shares"]),
                f"{trade['Capital Invested']:.2f}",
                f"{trade['Gross Sale Value']:.2f}",
                f"{trade['Transaction Fee']:.2f}",
                f"{trade['Net Return']:.2f}",
                f"{trade['Net PnL']:+.2f}",
                trade["Exit Reason"],
            ])

    def load_trade_history(self) -> List[Dict[str, Any]]:
        """Reads closed trade history."""
        if not self.history_file.exists():
            return []
        trades = []
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    t = (row.get("Ticker") or row.get("ticker") or "").strip().upper()
                    if t:
                        trades.append(row)
        except Exception as e:
            logger.debug(f"[{self.config.portfolio_id}] Error reading trade history: {e}")
        return trades

    def record_snapshot(self, stock_value_eur: float) -> None:
        """Records an equity snapshot to history JSON for chart visualization."""
        total_equity = self.cash_balance + stock_value_eur
        history = []
        if self.equity_file.exists():
            try:
                with open(self.equity_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []

        now_dt = datetime.now(timezone.utc)
        should_append = True
        if history:
            last = history[-1]
            try:
                last_dt = datetime.fromisoformat(str(last.get("timestamp")))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if abs((now_dt - last_dt).total_seconds()) < 60 and abs(float(last.get("total_equity", 0.0)) - total_equity) < 0.50:
                    should_append = False
            except Exception:
                pass

        if should_append:
            snapshot = {
                "timestamp": now_dt.isoformat(),
                "total_equity": round(total_equity, 2),
                "cash_balance": round(self.cash_balance, 2),
                "total_stock_value": round(stock_value_eur, 2),
                "total_return": round(total_equity - self.starting_balance, 2),
                "total_return_pct": round(
                    ((total_equity - self.starting_balance) / self.starting_balance) * 100.0, 2
                ) if self.starting_balance > 0 else 0.0,
            }
            history.append(snapshot)
            if len(history) > 10000:
                history = history[-10000:]
            try:
                with open(self.equity_file, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.debug(f"[{self.config.portfolio_id}] Failed to save snapshot: {e}")


class MarketDataEngine:
    """
    Central shared market data cache. Fetches price updates, 20d ADV,
    press releases / news radar, and deterministic financials strictly ONCE per cycle.
    """

    def __init__(self, clean_universe_path: Path = DEFAULT_CLEAN_UNIVERSE_CSV):
        self.clean_universe_path = clean_universe_path
        self.universe_df: pd.DataFrame = pd.DataFrame()
        self.fx_rates: Dict[str, float] = {"EURUSD": 1.08, "EURSEK": 11.30}
        self.prices_cache: Dict[str, float] = {}
        self.hard_facts_cache: Dict[str, Dict[str, Any]] = {}
        self.eval_profiles_cache: Dict[str, Dict[str, Any]] = {}
        self.news_radar_cache: Dict[str, Tuple[str, str]] = {}
        self.universe_metadata: Dict[str, Dict[str, Any]] = {}
        # Operational health tracker (LLM fallback rate, data freshness blocks)
        self.operational_metrics: Dict[str, Any] = load_operational_metrics()
        # Global cache of evaluated news items (ticker, clean_headline) -> (decision, reason)
        self.evaluated_news_cache: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self._load_evaluated_news_cache()

    def _load_evaluated_news_cache(self, archive_path: Path = DEFAULT_NLP_ARCHIVE_CSV) -> None:
        """Pre-populates the in-memory news evaluation cache from existing archive to ensure deduplication."""
        if not archive_path.exists():
            return
        try:
            with open(archive_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 5:
                        ticker = row[1].strip().upper()
                        headline = " ".join(row[2].split())
                        decision = row[3].strip().upper()
                        reason = row[4].strip()
                        if ticker and headline:
                            self.evaluated_news_cache[(ticker, headline)] = (decision, reason)
            if self.evaluated_news_cache:
                logger.info(f"Loaded {len(self.evaluated_news_cache)} historical news evaluations into NLP cache.")
        except Exception as e:
            logger.debug(f"Could not pre-load NLP cache: {e}")

    def refresh_data(self, held_tickers: Set[str]) -> None:
        """Fetches live data strictly ONCE for all universe stocks and held tickers."""
        logger.info("📡 [MARKET ENGINE] Refreshing shared market data strictly ONCE for this cycle...")
        self.fx_rates = get_live_fx_rates()
        logger.info(f"📡 [MARKET ENGINE] Live FX Rates: EURUSD={self.fx_rates.get('EURUSD', 1.08):.4f}, EURSEK={self.fx_rates.get('EURSEK', 11.30):.4f}")

        if self.clean_universe_path.exists():
            try:
                self.universe_df = pd.read_csv(self.clean_universe_path)
            except Exception as e:
                logger.error(f"Failed to read universe file {self.clean_universe_path}: {e}")
                self.universe_df = pd.DataFrame()

        # Build metadata map from universe CSV
        for _, row in self.universe_df.iterrows():
            ticker = str(row["ticker"]).strip().upper()
            self.universe_metadata[ticker] = {
                "market": str(row.get("market", "")).strip().upper(),
                "market_cap_usd": float(row.get("market_cap_usd", 0.0) or 0.0),
                "market_cap_local": float(row.get("market_cap_local", 0.0) or 0.0),
                "currency": str(row.get("currency", "")).strip().upper() or get_ticker_currency(ticker),
                "adv_20d_local": float(row.get("adv_20d_local", 0.0) or 0.0),
                "adv_20d_usd": float(row.get("adv_20d_usd", 0.0) or 0.0),
                "current_price": float(row.get("current_price", 0.0) or 0.0),
            }

        all_tickers = set(self.universe_metadata.keys()).union(held_tickers)
        logger.info(f"📡 [MARKET ENGINE] Querying data for {len(all_tickers)} unique symbols ({len(held_tickers)} held across portfolios)...")

        for ticker in all_tickers:
            # 1. Fetch live price
            px = self._fetch_live_price(ticker)
            if px and px > 0:
                self.prices_cache[ticker] = px
            else:
                fallback_px = self.universe_metadata.get(ticker, {}).get("current_price", 0.0)
                if fallback_px > 0:
                    self.prices_cache[ticker] = fallback_px

            # 2. Evaluate hard financials
            hard_facts = get_hard_financials(ticker)
            self.hard_facts_cache[ticker] = hard_facts
            self.eval_profiles_cache[ticker] = evaluate_profiles(hard_facts)

            # 3. Evaluate News Radar for held positions and viable candidates
            if ticker in held_tickers or self.eval_profiles_cache[ticker].get("is_profile_b") or self.eval_profiles_cache[ticker].get("is_profile_a"):
                self.news_radar_cache[ticker] = self._evaluate_news_radar(ticker)

        logger.info(f"✅ [MARKET ENGINE] Market data pre-fetch completed. Ready to process portfolios.")

    def _fetch_live_price(self, ticker: str) -> Optional[float]:
        try:
            t = yf.Ticker(ticker)
            info = getattr(t, "fast_info", None)
            if info and hasattr(info, "last_price") and info.last_price:
                return float(info.last_price)
            hist = t.history(period="5d")
            if not hist.empty:
                valid = hist["Close"].dropna()
                if not valid.empty:
                    return float(valid.iloc[-1])
        except Exception as e:
            logger.debug(f"Could not fetch price for {ticker}: {e}")
        return None

    def _evaluate_news_radar(self, ticker: str) -> Tuple[str, str]:
        """
        Evaluates news items for a ticker using rule-based/LLM radar.
        Workflow:
          1. Extract valid news items with non-empty headlines (supports both yfinance formats).
          2. Skip empty headlines completely.
          3. Check global cache:
             - If (ticker, headline) in self.evaluated_news_cache:
                 use cached decision and reasoning without re-running NLP and without re-appending to CSV.
             - If new:
                 run NLP checks, call append_nlp_decision() EXACTLY ONCE, and store in self.evaluated_news_cache.
          4. Aggregate decisions: FATAL REJECT overrides WARN overrides HOLD.
        """
        try:
            t = yf.Ticker(ticker)
            raw_news = getattr(t, "news", []) or []
            if not raw_news:
                return "HOLD", "No new press releases"

            warning_found = False
            warning_reason = ""
            valid_headline_count = 0

            for item in raw_news[:10]:
                if not isinstance(item, dict):
                    continue

                # Support both yfinance formats: item["content"]["title"] or item["title"]
                content = item.get("content", item) if isinstance(item.get("content"), dict) else item
                title = str(content.get("title", "") or "").strip()
                summary = str(content.get("summary", "") or "").strip()

                # Requirement 1: Skip Empty News strictly
                clean_headline = " ".join((title or summary).split())
                if not clean_headline:
                    continue

                valid_headline_count += 1
                full_text = f"{title} {summary}".strip()
                cache_key = (ticker, clean_headline)

                # Requirement 2: Check global cache -> if new, call LLM -> log ONCE -> store in cache
                if cache_key in self.evaluated_news_cache:
                    item_decision, item_reason = self.evaluated_news_cache[cache_key]
                else:
                    item_decision = "HOLD"
                    item_reason = "No fatal red flags detected"
                    fatal_detected = False

                    # Check fatal patterns
                    for pattern in FATAL_RED_FLAG_PATTERNS:
                        if pattern.lower() in full_text.lower():
                            item_decision = "REJECT"
                            item_reason = f"Fatal red flag '{pattern}' detected in headline: {title or clean_headline}"
                            fatal_detected = True
                            break

                    if not fatal_detected:
                        # Check warning patterns
                        for pattern in WARN_PATTERNS:
                            if pattern.lower() in full_text.lower():
                                item_decision = "WARN"
                                item_reason = f"Warning '{pattern}' detected in headline: {title or clean_headline}"
                                break

                        # Core NLP analysis with operational health tracking
                        self.operational_metrics["nlp_evaluations_total"] = self.operational_metrics.get("nlp_evaluations_total", 0) + 1
                        self.operational_metrics["llm_calls_attempted"] = self.operational_metrics.get("llm_calls_attempted", 0) + 1
                        nlp_res = None

                        # Check if LLM API is configured and attempt LLM evaluation
                        if os.getenv("OPENROUTER_API_KEY"):
                            try:
                                from screener.nlp_analyzer import analyze_core_fundamentals
                                nlp_res = analyze_core_fundamentals(full_text)
                            except Exception as e:
                                logger.warning(f"⚠️ [OPERATIONAL HEALTH] LLM API call error for {ticker}: {e}")
                                nlp_res = None

                        # If LLM failed, timed out, or unconfigured, execute rule-based fallback
                        if not nlp_res or not isinstance(nlp_res, dict) or "financial_safety" not in nlp_res:
                            if os.getenv("OPENROUTER_API_KEY"):
                                self.operational_metrics["llm_fallbacks"] = self.operational_metrics.get("llm_fallbacks", 0) + 1
                                logger.info(f"⚡ [OPERATIONAL HEALTH] LLM check failed/timeout for {ticker}. Seamlessly fallen back to rule-based engine.")
                            nlp_res = rule_based_analyze_core_fundamentals(full_text)

                        safety = nlp_res.get("financial_safety", {})
                        if safety.get("going_concern_risk") or safety.get("erratic_pivots_detected"):
                            item_decision = "REJECT"
                            item_reason = f"Fatal NLP Risk triggered: {nlp_res.get('verdict_details', {}).get('reasoning')}"
                        elif nlp_res.get("verdict_details", {}).get("verdict") == "WARN" or safety.get("warning_detected"):
                            if item_decision != "REJECT":
                                item_decision = "WARN"
                                item_reason = f"NLP Warning: {nlp_res.get('verdict_details', {}).get('reasoning')}"

                    # Log ONCE to CSV
                    append_nlp_decision(ticker, clean_headline, item_decision, item_reason)

                    # Store in global cache
                    self.evaluated_news_cache[cache_key] = (item_decision, item_reason)

                # Aggregate ticker status
                if item_decision == "REJECT":
                    return "REJECT", item_reason
                elif item_decision == "WARN":
                    warning_found = True
                    warning_reason = item_reason

            if warning_found:
                return "WARN", warning_reason

            if valid_headline_count == 0:
                return "HOLD", "No new press releases"

            return "HOLD", f"Scanned {valid_headline_count} recent news items with no fatal red flags"

        except Exception as e:
            logger.debug(f"Error checking news for {ticker}: {e}")
            return "HOLD", "News scan error"



class MasterLiveTradingDaemon:
    """
    Master Live Trading Daemon.
    Orchestrates the 10 parallel portfolios defined in portfolios_config.yaml.
    """

    def __init__(self, config_yaml_path: Path = DEFAULT_PORTFOLIOS_YAML):
        self.config_yaml_path = config_yaml_path
        self.portfolios: List[PortfolioInstance] = []
        self.market_engine = MarketDataEngine()
        self.load_configurations()

    def load_configurations(self) -> None:
        """Loads portfolios from YAML."""
        if not self.config_yaml_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_yaml_path}")

        with open(self.config_yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        portfolios_dict = data.get("portfolios", {})
        self.portfolios = []
        for pid, pdata in portfolios_dict.items():
            cfg = PortfolioConfig.from_dict(pid, pdata)
            self.portfolios.append(PortfolioInstance(cfg))

        logger.info(f"Loaded {len(self.portfolios)} portfolio configurations from {self.config_yaml_path.name}")

    def execute_phase1_exits(self, portfolio: PortfolioInstance, run_trades: Optional[List[Dict[str, Any]]] = None) -> int:
        """
        Phase 1: Portfolio Management (Tri-Layer Exit & Dead Money Timer).
        Returns count of closed positions.
        """
        closed_count = 0
        retained_positions: List[Dict[str, Any]] = []

        for pos in portfolio.positions:
            ticker = pos["Ticker"]
            buy_price = float(pos["Buy Price"])
            shares = int(pos["Shares"])
            capital_invested = float(pos.get("Capital Invested", shares * buy_price))
            buy_date = pos["Buy Date"]

            current_price = self.market_engine.prices_cache.get(ticker)
            if not current_price or current_price <= 0:
                logger.warning(f"[{portfolio.config.portfolio_id}] ⚠️ {ticker}: Price unavailable. Retaining.")
                retained_positions.append(pos)
                continue

            news_verdict, news_reason = self.market_engine.news_radar_cache.get(ticker, ("HOLD", "No news"))
            if news_verdict == "WARN" and run_trades is not None:
                run_trades.append({
                    "portfolio_id": portfolio.config.portfolio_id,
                    "action": "WARN",
                    "ticker": ticker,
                    "details": {
                        "reason": news_reason,
                        "current_price": f"{current_price:.2f}",
                        "shares": shares,
                    },
                })

            hard_facts = self.market_engine.hard_facts_cache.get(ticker, {})
            financials_payload = {
                "revenue_growth_yoy": hard_facts.get("revenue_growth_yoy_pct"),
                "cash_runway_months": hard_facts.get("cash_runway_months"),
                "operating_cash_flow": hard_facts.get("operating_cash_flow_ttm"),
            }

            action, reason = evaluate_position(
                position={"buy_price": buy_price, "ticker": ticker, "buy_date": buy_date},
                current_price=current_price,
                latest_news_judgment=news_verdict,
                latest_financials=financials_payload,
                dead_money_days=portfolio.config.dead_money_days,
            )

            if action == "SELL":
                gross_sale_value = shares * current_price
                ticker_curr = get_ticker_currency(ticker)
                fx_to_acc = get_fx_to_account(ticker_curr, portfolio.currency, self.market_engine.fx_rates)
                fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local
                transaction_fee = calculate_transaction_fee(gross_sale_value, min_fee=min_fee_local)
                net_return = gross_sale_value - transaction_fee
                net_pnl = net_return - capital_invested
                net_return_acc = net_return * fx_to_acc

                portfolio.cash_balance += net_return_acc
                trade_record = {
                    "Ticker": ticker,
                    "Buy Date": buy_date,
                    "Sell Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "Buy Price": buy_price,
                    "Sell Price": current_price,
                    "Shares": shares,
                    "Capital Invested": capital_invested,
                    "Gross Sale Value": gross_sale_value,
                    "Transaction Fee": transaction_fee,
                    "Net Return": net_return,
                    "Net PnL": net_pnl,
                    "Exit Reason": reason,
                }
                portfolio.append_trade(trade_record)
                closed_count += 1

                logger.info(
                    f"🚨 [{portfolio.config.portfolio_id} SELL] {ticker} | Exit: {reason} | "
                    f"Shares: {shares} @ {current_price:.2f} {ticker_curr} | PnL: {net_pnl:+.2f} {ticker_curr}"
                )

                if run_trades is not None:
                    run_trades.append({
                        "portfolio_id": portfolio.config.portfolio_id,
                        "action": "SELL",
                        "ticker": ticker,
                        "details": {
                            "exit_reason": reason,
                            "shares": shares,
                            "sell_price": f"{current_price:.2f} {ticker_curr}",
                            "net_pnl": f"{net_pnl:+.2f} {ticker_curr}",
                            "remaining_cash": f"{portfolio.cash_balance:,.2f} {portfolio.currency}",
                        },
                    })
            else:
                retained_positions.append(pos)

        portfolio.positions = retained_positions
        portfolio.save_state()
        return closed_count

    def execute_phase2_screening(self, portfolio: PortfolioInstance, run_trades: Optional[List[Dict[str, Any]]] = None) -> int:
        """
        Phase 2: Screening & Sizing for a specific portfolio based on its parameters.
        Returns count of new positions opened.
        """
        cfg = portfolio.config
        open_count = len(portfolio.positions)
        if open_count >= cfg.slots:
            logger.info(f"[{cfg.portfolio_id}] Max slots full ({open_count}/{cfg.slots}). Skipping new buys.")
            return 0

        target_allocation_acc = cfg.start_cash / cfg.slots  # Fixed slot sizing (e.g. 10k / 10 = 1,000€)
        if portfolio.cash_balance < (MIN_BROKER_FEE + 10.0):
            logger.info(f"[{cfg.portfolio_id}] Cash depleted ({portfolio.cash_balance:.2f}€). Skipping new buys.")
            return 0

        held_tickers = {p["Ticker"] for p in portfolio.positions}
        today = datetime.now(timezone.utc).date()

        # Cooldown guard from closed trades
        cooldown_tickers: Dict[str, Any] = {}
        for trade in portfolio.load_trade_history():
            t_sym = (trade.get("Ticker") or "").strip().upper()
            s_date_str = trade.get("Sell Date")
            if t_sym and s_date_str:
                try:
                    s_dt = datetime.strptime(str(s_date_str)[:10], "%Y-%m-%d").date()
                    if t_sym not in cooldown_tickers or s_dt > cooldown_tickers[t_sym]:
                        cooldown_tickers[t_sym] = s_dt
                except Exception:
                    pass

        new_buys = 0

        for ticker, meta in self.market_engine.universe_metadata.items():
            if open_count + new_buys >= cfg.slots:
                break

            if ticker in held_tickers:
                continue

            # Check geographic region filter
            market = meta.get("market", "")
            if cfg.regions and market not in cfg.regions:
                continue

            # Anti-Whipsaw Cooldown
            if ticker in cooldown_tickers:
                days_since_exit = (today - cooldown_tickers[ticker]).days
                if days_since_exit < REENTRY_COOLDOWN_DAYS:
                    continue

            # Check ADV threshold
            adv_local = meta.get("adv_20d_local", 0.0)
            adv_usd = meta.get("adv_20d_usd", 0.0)
            if adv_usd < cfg.min_adv and adv_local < cfg.min_adv:
                continue

            # Check Strategy profile
            eval_res = self.market_engine.eval_profiles_cache.get(ticker, {})
            hard_facts = self.market_engine.hard_facts_cache.get(ticker, {})

            if cfg.strategy.lower() == "profile a":
                if not eval_res.get("is_profile_a"):
                    continue
            else:
                if not eval_res.get("is_profile_b"):
                    continue

            # Extra filter check (e.g. price_to_cash < 0.5)
            if cfg.extra_filter:
                if "price_to_cash" in cfg.extra_filter:
                    cash_val = hard_facts.get("cash_and_equivalents")
                    mkt_cap = meta.get("market_cap_local") or meta.get("market_cap_usd")
                    if not cash_val or not mkt_cap or cash_val <= 0:
                        continue
                    price_to_cash = mkt_cap / cash_val
                    if "< 0.5" in cfg.extra_filter and price_to_cash >= 0.5:
                        continue

            # Exit criteria pre-validation (ensure entry does not immediately fail exit rules)
            cand_price = self.market_engine.prices_cache.get(ticker, meta.get("current_price", 0.0))
            if cand_price <= 0:
                continue

            financials_payload = {
                "revenue_growth_yoy": hard_facts.get("revenue_growth_yoy_pct"),
                "cash_runway_months": hard_facts.get("cash_runway_months"),
                "operating_cash_flow": hard_facts.get("operating_cash_flow_ttm"),
            }
            exit_act, exit_rsn = evaluate_position(
                position={"buy_price": cand_price, "ticker": ticker, "buy_date": today.strftime("%Y-%m-%d")},
                current_price=cand_price,
                latest_news_judgment="HOLD",
                latest_financials=financials_payload,
                dead_money_days=cfg.dead_money_days,
            )
            if exit_act == "SELL":
                continue

            # Stale statement guard (>120 days) with operational freshness tracking
            dq = hard_facts.get("data_quality", {})
            self.market_engine.operational_metrics["freshness_checks_total"] = (
                self.market_engine.operational_metrics.get("freshness_checks_total", 0) + 1
            )
            if dq.get("is_fresh") is False:
                self.market_engine.operational_metrics["freshness_blocks"] = (
                    self.market_engine.operational_metrics.get("freshness_blocks", 0) + 1
                )
                logger.info(
                    f"🚫 [FRESHNESS GUARD] Candidate {ticker} trade blocked due to stale/delayed data (>120d). "
                    f"Total freshness blocks: {self.market_engine.operational_metrics['freshness_blocks']}"
                )
                continue

            # Position sizing
            eff_allocation_acc = min(target_allocation_acc, portfolio.cash_balance)
            ticker_curr = meta.get("currency", get_ticker_currency(ticker))
            fx_to_acc = get_fx_to_account(ticker_curr, portfolio.currency, self.market_engine.fx_rates)
            fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

            target_allocation_local = eff_allocation_acc * fx_acc_to_local
            min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

            # Cap to 10% ADV
            max_adv_allowed = adv_local * MAX_ADV_ALLOCATION_PCT if adv_local > 0 else 0.0
            if max_adv_allowed > 0 and target_allocation_local > max_adv_allowed:
                target_allocation_local = max_adv_allowed

            if target_allocation_local < (min_fee_local + (10.0 * fx_acc_to_local)):
                continue

            investable_cash_local = target_allocation_local - min_fee_local
            if investable_cash_local <= 0:
                continue

            shares_to_buy = math.floor(investable_cash_local / cand_price)
            if shares_to_buy <= 0:
                continue

            actual_gross_buy = shares_to_buy * cand_price
            actual_fee = calculate_transaction_fee(actual_gross_buy, min_fee=min_fee_local)
            total_cost_local = actual_gross_buy + actual_fee
            total_cost_acc = total_cost_local * fx_to_acc

            if total_cost_acc > portfolio.cash_balance:
                continue

            portfolio.cash_balance -= total_cost_acc
            new_pos = {
                "Ticker": ticker,
                "Buy Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "Buy Price": cand_price,
                "Shares": shares_to_buy,
                "Capital Invested": round(total_cost_local, 2),
                "Strategy": cfg.strategy,
                "Currency": ticker_curr,
            }
            portfolio.positions.append(new_pos)
            held_tickers.add(ticker)
            new_buys += 1

            logger.info(
                f"🎯 [{cfg.portfolio_id} BUY] {ticker} ({cfg.strategy}) | "
                f"Bought {shares_to_buy} shares @ {cand_price:.2f} {ticker_curr} | "
                f"Cost: {total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} {portfolio.currency}) | "
                f"Remaining Cash: {portfolio.cash_balance:,.2f} {portfolio.currency}"
            )

            if run_trades is not None:
                run_trades.append({
                    "portfolio_id": cfg.portfolio_id,
                    "action": "BUY",
                    "ticker": ticker,
                    "details": {
                        "strategy": cfg.strategy,
                        "shares": shares_to_buy,
                        "buy_price": f"{cand_price:.2f} {ticker_curr}",
                        "total_cost": f"{total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} {portfolio.currency})",
                        "remaining_cash": f"{portfolio.cash_balance:,.2f} {portfolio.currency}",
                    },
                })

        if new_buys > 0:
            portfolio.save_state()

        return new_buys

    def calculate_stock_value_eur(self, portfolio: PortfolioInstance) -> float:
        """Calculates total market value of portfolio open positions converted to EUR."""
        total_eur = 0.0
        for p in portfolio.positions:
            t = p["Ticker"]
            shares = float(p.get("Shares", 0))
            px = self.market_engine.prices_cache.get(t, float(p.get("Buy Price", 0.0)))
            curr = p.get("Currency", get_ticker_currency(t))
            fx_to_eur = get_fx_to_account(curr, "EUR", self.market_engine.fx_rates)
            total_eur += shares * px * fx_to_eur
        return total_eur

    def run_cycle(self) -> Dict[str, Dict[str, Any]]:
        """Runs a complete walk-forward cycle across all 10 portfolios."""
        print("\n" + "=" * 100)
        print(f"🚀 MULTI-PORTFOLIO LIVE WALK-FORWARD ENGINE: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 100)

        # Step 0: Gather union of held tickers and pre-fetch shared market data strictly ONCE
        all_held: Set[str] = set()
        for p in self.portfolios:
            for pos in p.positions:
                all_held.add(pos["Ticker"])

        self.market_engine.refresh_data(held_tickers=all_held)

        results: Dict[str, Dict[str, Any]] = {}
        run_trades: List[Dict[str, Any]] = []

        # Process each portfolio independently
        for p in self.portfolios:
            closed = self.execute_phase1_exits(p, run_trades=run_trades)
            opened = self.execute_phase2_screening(p, run_trades=run_trades)
            stock_val_eur = self.calculate_stock_value_eur(p)
            p.record_snapshot(stock_val_eur)

            total_equity = p.cash_balance + stock_val_eur
            ret_pct = ((total_equity - p.starting_balance) / p.starting_balance) * 100.0 if p.starting_balance > 0 else 0.0

            results[p.config.portfolio_id] = {
                "closed": closed,
                "opened": opened,
                "positions_count": len(p.positions),
                "cash": p.cash_balance,
                "stock_val_eur": stock_val_eur,
                "total_equity": total_equity,
                "return_pct": ret_pct,
            }

        # Send a single consolidated email summary of all executed trades across all portfolios
        if run_trades:
            send_run_summary_email(
                run_trades=run_trades,
                portfolio_results=results,
            )

        # Consolidated Summary Table
        print("\n" + "-" * 100)
        print(f"{'PORTFOLIO ID':<22} | {'STRATEGY':<12} | {'SLOTS':<6} | {'POS':<5} | {'CASH (€)':<10} | {'EQUITY (€)':<11} | {'RETURN':<8}")
        print("-" * 100)
        for p in self.portfolios:
            r = results[p.config.portfolio_id]
            print(
                f"{p.config.portfolio_id:<22} | "
                f"{p.config.strategy:<12} | "
                f"{p.config.slots:<6} | "
                f"{r['positions_count']:<5} | "
                f"{r['cash']:>10,.2f} | "
                f"{r['total_equity']:>11,.2f} | "
                f"{r['return_pct']:>+7.2f}%"
            )
        print("-" * 100 + "\n")

        # Save and display operational health metrics
        save_operational_metrics(self.market_engine.operational_metrics)
        m = self.market_engine.operational_metrics
        print(f"🏥 [BOT HEALTH] LLM Fallback Rate: {m.get('llm_fallbacks', 0)} / {max(m.get('llm_calls_attempted', 0), 1)} ({m.get('llm_fallback_rate_pct', 0.0):.1f}%) | "
              f"Freshness Blocks: {m.get('freshness_blocks', 0)} / {max(m.get('freshness_checks_total', 0), 1)} ({m.get('freshness_block_rate_pct', 0.0):.1f}%) | "
              f"Status: {m.get('status', 'HEALTHY')}\n")

        return results

    def run_loop(self, interval_hours: float = 24.0) -> None:
        """Runs the multi-portfolio engine continuously with a sleep loop."""
        interval_seconds = int(interval_hours * 3600)
        logger.info(f"Starting continuous multi-portfolio daemon loop (Interval: {interval_hours}h)...")
        while True:
            try:
                self.run_cycle()
                logger.info(f"Sleeping for {interval_hours} hours until next cycle...")
                time.sleep(interval_seconds)
            except KeyboardInterrupt:
                logger.info("Daemon stopped by user.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in daemon loop: {e}. Retrying in 60s...")
                time.sleep(60)


KNOWN_ENTRY_PRICES: Dict[str, float] = {
    "VIAFIN.HE": 19.80,
    "SEDANA.ST": 10.54,
    "MOB.ST": 10.60,
    "MSAB-B.ST": 93.00,
    "WATT": 11.73,
    "OSS": 9.18,
    "SSH1V.HE": 2.205,
    "RAUTE.HE": 15.20,
    "STIL.ST": 241.50,
    "SEZI.ST": 2.87,
    "VUZI": 2.76,
    "DUOT": 8.54,
    "HOLO": 1.66,
    "CAMP": 4.24,
    "EGAN": 5.36,
    "GROW": 3.06,
}


class PaperAccountManager:
    """Manages virtual paper trading cash, total portfolio equity, and persistence."""

    def __init__(
        self,
        account_path: Path | str = DEFAULT_PAPER_ACCOUNT_JSON,
        starting_balance: float = STARTING_BALANCE,
        currency: str = "USD",
    ):
        self.account_path = Path(account_path)
        self.starting_balance = float(starting_balance)
        self.cash_balance = self.starting_balance
        self.currency = currency
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.created_at
        self.load()

    def load(self) -> None:
        """Loads paper account state from JSON file or initializes default."""
        if self.account_path.exists():
            try:
                with open(self.account_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.cash_balance = float(data.get("cash_balance", self.starting_balance))
                    self.currency = data.get("currency", self.currency)
                    self.created_at = data.get("created_at", self.created_at)
                    self.updated_at = data.get("updated_at", self.updated_at)
                    return
            except Exception as e:
                logger.warning(f"Could not read {self.account_path}: {e}. Reinitializing.")
        self.save()

    def save(self) -> None:
        """Saves current paper account state to JSON file."""
        self.account_path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "cash_balance": round(self.cash_balance, 2),
            "currency": self.currency,
            "starting_balance": self.starting_balance,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        with open(self.account_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def reset(self, balance: Optional[float] = None) -> None:
        """Resets account to starting capital."""
        if balance is not None:
            self.starting_balance = float(balance)
        self.cash_balance = self.starting_balance
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.save()


class LiveTradingDaemon:
    """
    Single-portfolio live trading daemon (retained for backward compatibility and test suites).
    """

    def __init__(
        self,
        account_path: Path | str = DEFAULT_PAPER_ACCOUNT_JSON,
        open_positions_path: Path | str = DEFAULT_OPEN_POSITIONS_CSV,
        trade_history_path: Path | str = DEFAULT_TRADE_HISTORY_CSV,
        clean_universe_path: Path | str = DEFAULT_CLEAN_UNIVERSE_CSV,
        portfolio_history_path: Path | str = DEFAULT_PORTFOLIO_HISTORY_JSON,
        starting_balance: float = STARTING_BALANCE,
    ):
        self.account_path = Path(account_path)
        self.open_positions_path = Path(open_positions_path)
        self.trade_history_path = Path(trade_history_path)
        self.clean_universe_path = Path(clean_universe_path)
        self.portfolio_history_path = Path(portfolio_history_path)

        self.account = PaperAccountManager(self.account_path, starting_balance)
        self.ensure_files_exist()

    def ensure_files_exist(self) -> None:
        self.open_positions_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_history_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.open_positions_path.exists():
            with open(self.open_positions_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Ticker", "Buy Date", "Buy Price", "Shares", "Capital Invested", "Strategy"])

        standard_history_header = [
            "Ticker", "Buy Date", "Sell Date", "Buy Price", "Sell Price",
            "Shares", "Capital Invested", "Gross Sale Value", "Transaction Fee",
            "Net Return", "Net PnL", "Exit Reason"
        ]
        if not self.trade_history_path.exists():
            with open(self.trade_history_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(standard_history_header)

    def load_open_positions(self) -> List[Dict[str, Any]]:
        if not self.open_positions_path.exists():
            return []
        positions: List[Dict[str, Any]] = []
        try:
            with open(self.open_positions_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for raw_row in reader:
                    row = {str(k).strip().lower(): str(v).strip() for k, v in raw_row.items() if k}
                    ticker = (row.get("ticker") or "").strip().upper()
                    if not ticker:
                        continue
                    buy_date = row.get("buy date") or row.get("buy_date") or row.get("entrydate") or row.get("entry_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    buy_price = float(row.get("buy price") or row.get("buy_price") or row.get("entryprice") or row.get("entry_price") or 0.0)
                    shares = int(float(row.get("shares") or 0))
                    if buy_price <= 0.0:
                        if ticker in KNOWN_ENTRY_PRICES:
                            buy_price = KNOWN_ENTRY_PRICES[ticker]
                        else:
                            curr_fallback = float(row.get("current_price") or row.get("currentprice") or row.get("highest_price_seen") or 0.0)
                            if curr_fallback > 0.0:
                                buy_price = curr_fallback
                    cap_invested_raw = row.get("capital invested") or row.get("capital_invested") or row.get("positionvalue") or row.get("position_value")
                    if cap_invested_raw is not None and str(cap_invested_raw).strip() and float(cap_invested_raw) > 0:
                        capital_invested = float(cap_invested_raw)
                    else:
                        capital_invested = (shares * buy_price) + calculate_transaction_fee(shares * buy_price)
                    strategy = row.get("strategy") or row.get("strategy_type") or "PROFILE_B"
                    if buy_price > 0 and shares > 0:
                        positions.append({
                            "Ticker": ticker,
                            "Buy Date": buy_date,
                            "Buy Price": buy_price,
                            "Shares": shares,
                            "Capital Invested": capital_invested,
                            "Strategy": strategy,
                        })
        except Exception as e:
            logger.error(f"Error reading {self.open_positions_path}: {e}")
        return positions

    def save_open_positions(self, positions: List[Dict[str, Any]]) -> None:
        with open(self.open_positions_path, "w", encoding="utf-8", newline="") as f:
            fieldnames = ["Ticker", "Buy Date", "Buy Price", "Shares", "Capital Invested", "Strategy"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for p in positions:
                writer.writerow({
                    "Ticker": p["Ticker"],
                    "Buy Date": p["Buy Date"],
                    "Buy Price": round(float(p["Buy Price"]), 4),
                    "Shares": int(p["Shares"]),
                    "Capital Invested": round(float(p.get("Capital Invested", p["Shares"] * p["Buy Price"])), 2),
                    "Strategy": p.get("Strategy", "PROFILE_B"),
                })

    def append_trade_history(self, trade: Dict[str, Any]) -> None:
        with open(self.trade_history_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                trade["Ticker"],
                trade["Buy Date"],
                trade["Sell Date"],
                f"{trade['Buy Price']:.4f}",
                f"{trade['Sell Price']:.4f}",
                int(trade["Shares"]),
                f"{trade['Capital Invested']:.2f}",
                f"{trade['Gross Sale Value']:.2f}",
                f"{trade['Transaction Fee']:.2f}",
                f"{trade['Net Return']:.2f}",
                f"{trade['Net PnL']:+.2f}",
                trade["Exit Reason"],
            ])

    def load_trade_history(self) -> List[Dict[str, Any]]:
        if not self.trade_history_path.exists():
            return []
        trades: List[Dict[str, Any]] = []
        try:
            with open(self.trade_history_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ticker = (row.get("Ticker") or row.get("ticker") or "").strip().upper()
                    if ticker:
                        trades.append(row)
        except Exception as e:
            logger.debug(f"Could not read {self.trade_history_path}: {e}")
        return trades

    def fetch_live_price(self, ticker: str) -> Optional[float]:
        try:
            t = yf.Ticker(ticker)
            info = getattr(t, "fast_info", None)
            if info and hasattr(info, "last_price") and info.last_price:
                return float(info.last_price)
            hist = t.history(period="5d")
            if not hist.empty:
                valid = hist["Close"].dropna()
                if not valid.empty:
                    return float(valid.iloc[-1])
        except Exception as e:
            logger.warning(f"Could not fetch live price for {ticker}: {e}")
        return None

    def evaluate_news_radar(self, ticker: str) -> Tuple[str, str]:
        try:
            t = yf.Ticker(ticker)
            news_items = getattr(t, "news", []) or []
            if not news_items:
                return "HOLD", "No new press releases"
            warning_found = False
            warning_reason = ""
            for item in news_items[:10]:
                title = str(item.get("title", "")).strip()
                summary = str(item.get("summary", "")).strip()
                full_text = f"{title} {summary}"
                for pattern in FATAL_RED_FLAG_PATTERNS:
                    if pattern.lower() in full_text.lower():
                        reason = f"Fatal red flag '{pattern}' detected in headline: {title}"
                        return "REJECT", reason
                for pattern in WARN_PATTERNS:
                    if pattern.lower() in full_text.lower():
                        warning_found = True
                        warning_reason = f"Warning '{pattern}' detected in headline: {title}"
                nlp_res = rule_based_analyze_core_fundamentals(full_text)
                safety = nlp_res.get("financial_safety", {})
                if safety.get("going_concern_risk") or safety.get("erratic_pivots_detected"):
                    reason = f"Fatal NLP Risk triggered: {nlp_res.get('verdict_details', {}).get('reasoning')}"
                    return "REJECT", reason
                elif nlp_res.get("verdict_details", {}).get("verdict") == "WARN" or safety.get("warning_detected"):
                    warning_found = True
                    warning_reason = f"NLP Warning: {nlp_res.get('verdict_details', {}).get('reasoning')}"
            if warning_found:
                return "WARN", warning_reason
            return "HOLD", f"Scanned {len(news_items)} recent news items with no fatal red flags"
        except Exception as e:
            logger.warning(f"Error checking news for {ticker}: {e}")
            return "HOLD", "News scan error"

    def execute_portfolio_management(self) -> int:
        open_positions = self.load_open_positions()
        if not open_positions:
            return 0
        retained_positions: List[Dict[str, Any]] = []
        closed_count = 0
        for pos in open_positions:
            ticker = pos["Ticker"]
            buy_price = float(pos["Buy Price"])
            shares = int(pos["Shares"])
            capital_invested = float(pos.get("Capital Invested", shares * buy_price))
            buy_date = pos["Buy Date"]
            current_price = self.fetch_live_price(ticker)
            if current_price is None or current_price <= 0:
                retained_positions.append(pos)
                continue
            news_verdict, news_reason = self.evaluate_news_radar(ticker)
            hard_facts = get_hard_financials(ticker)
            financials_payload = {
                "revenue_growth_yoy": hard_facts.get("revenue_growth_yoy_pct"),
                "cash_runway_months": hard_facts.get("cash_runway_months"),
                "operating_cash_flow": hard_facts.get("operating_cash_flow_ttm"),
            }
            action, reason = evaluate_position(
                position={"buy_price": buy_price, "ticker": ticker, "buy_date": buy_date},
                current_price=current_price,
                latest_news_judgment=news_verdict,
                latest_financials=financials_payload,
            )
            if action == "SELL":
                gross_sale_value = shares * current_price
                ticker_curr = get_ticker_currency(ticker)
                fx_to_acc = get_fx_to_account(ticker_curr, self.account.currency)
                fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0
                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local
                transaction_fee = calculate_transaction_fee(gross_sale_value, min_fee=min_fee_local)
                net_return = gross_sale_value - transaction_fee
                net_pnl = net_return - capital_invested
                net_return_acc = net_return * fx_to_acc
                self.account.cash_balance += net_return_acc
                self.account.save()
                trade_record = {
                    "Ticker": ticker,
                    "Buy Date": buy_date,
                    "Sell Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "Buy Price": buy_price,
                    "Sell Price": current_price,
                    "Shares": shares,
                    "Capital Invested": capital_invested,
                    "Gross Sale Value": gross_sale_value,
                    "Transaction Fee": transaction_fee,
                    "Net Return": net_return,
                    "Net PnL": net_pnl,
                    "Exit Reason": reason,
                }
                self.append_trade_history(trade_record)
                closed_count += 1
            else:
                retained_positions.append(pos)
        self.save_open_positions(retained_positions)
        return closed_count

    def execute_market_screening(self) -> int:
        if not self.clean_universe_path.exists():
            return 0
        u_df = pd.read_csv(self.clean_universe_path)
        open_positions = self.load_open_positions()
        held_tickers = {p["Ticker"] for p in open_positions}
        new_buys = 0
        today = datetime.now(timezone.utc).date()
        cooldown_tickers: Dict[str, Any] = {}
        for trade in self.load_trade_history():
            t_sym = (trade.get("Ticker") or "").strip().upper()
            s_date_str = trade.get("Sell Date")
            if t_sym and s_date_str:
                try:
                    s_dt = datetime.strptime(str(s_date_str)[:10], "%Y-%m-%d").date()
                    if t_sym not in cooldown_tickers or s_dt > cooldown_tickers[t_sym]:
                        cooldown_tickers[t_sym] = s_dt
                except Exception:
                    pass
        for idx, row in u_df.iterrows():
            ticker = str(row["ticker"]).strip().upper()
            if ticker in held_tickers:
                continue
            if ticker in cooldown_tickers:
                days_since_exit = (today - cooldown_tickers[ticker]).days
                if days_since_exit < REENTRY_COOLDOWN_DAYS:
                    continue
            if self.account.cash_balance < (MIN_BROKER_FEE + 10.0):
                break
            hard_facts = get_hard_financials(ticker)
            eval_res = evaluate_profiles(hard_facts)
            if not eval_res.get("is_profile_b", False):
                continue
            financials_payload = {
                "revenue_growth_yoy": hard_facts.get("revenue_growth_yoy_pct"),
                "cash_runway_months": hard_facts.get("cash_runway_months"),
                "operating_cash_flow": hard_facts.get("operating_cash_flow_ttm"),
            }
            cand_price = self.fetch_live_price(ticker) or float(row.get("current_price", 0.0) or 0.0)
            if cand_price > 0:
                exit_action, exit_reason = evaluate_position(
                    position={"buy_price": cand_price, "ticker": ticker, "buy_date": today.strftime("%Y-%m-%d")},
                    current_price=cand_price,
                    latest_news_judgment="HOLD",
                    latest_financials=financials_payload,
                )
                if exit_action == "SELL":
                    continue
            target_allocation_acc = 1_000.0
            if target_allocation_acc > self.account.cash_balance:
                target_allocation_acc = self.account.cash_balance
            ticker_curr = get_ticker_currency(ticker, row.get("currency"))
            fx_to_acc = get_fx_to_account(ticker_curr, self.account.currency)
            fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0
            target_allocation_local = target_allocation_acc * fx_acc_to_local
            min_fee_local = MIN_BROKER_FEE * fx_acc_to_local
            adv_20d_local = float(row.get("adv_20d_local", 0.0) or 0.0)
            max_adv_allowed = adv_20d_local * MAX_ADV_ALLOCATION_PCT if adv_20d_local > 0 else 0.0
            if max_adv_allowed > 0 and target_allocation_local > max_adv_allowed:
                target_allocation_local = max_adv_allowed
            if target_allocation_local < (min_fee_local + (10.0 * fx_acc_to_local)):
                continue
            investable_cash_local = target_allocation_local - min_fee_local
            if investable_cash_local <= 0 or cand_price <= 0:
                continue
            shares_to_buy = math.floor(investable_cash_local / cand_price)
            if shares_to_buy <= 0:
                continue
            actual_gross_buy = shares_to_buy * cand_price
            actual_fee = calculate_transaction_fee(actual_gross_buy, min_fee=min_fee_local)
            total_cost_local = actual_gross_buy + actual_fee
            total_cost_acc = total_cost_local * fx_to_acc
            if total_cost_acc > self.account.cash_balance:
                continue
            self.account.cash_balance -= total_cost_acc
            self.account.save()
            new_pos = {
                "Ticker": ticker,
                "Buy Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "Buy Price": cand_price,
                "Shares": shares_to_buy,
                "Capital Invested": round(total_cost_local, 2),
                "Strategy": "PROFILE_B",
            }
            open_positions.append(new_pos)
            held_tickers.add(ticker)
            new_buys += 1
        if new_buys > 0:
            self.save_open_positions(open_positions)
        return new_buys

    def record_portfolio_snapshot(self) -> None:
        pass

    def run_daily_cycle(self) -> Tuple[int, int]:
        sold_count = self.execute_portfolio_management()
        bought_count = self.execute_market_screening()
        self.record_portfolio_snapshot()
        return sold_count, bought_count


def main() -> None:
    parser = argparse.ArgumentParser(description="tradeBotTiuku Multi-Portfolio Walk-Forward Engine")
    parser.add_argument("--run-once", action="store_true", help="Execute a single daily cycle across all 10 portfolios and exit")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a sleep loop")
    parser.add_argument("--interval-hours", type=float, default=24.0, help="Interval in hours for loop mode (default: 24)")
    parser.add_argument("--reset-portfolios", action="store_true", help="Reset all 10 portfolios to 10,000 EUR starting capital")
    parser.add_argument("--portfolio", type=str, default=None, help="Optionally run or test a single portfolio by ID")

    args = parser.parse_args()

    daemon = MasterLiveTradingDaemon()

    if args.reset_portfolios:
        for p in daemon.portfolios:
            p.reset()
        logger.info("All 10 portfolios reset successfully.")

    if args.portfolio:
        selected = [p for p in daemon.portfolios if p.config.portfolio_id.lower() == args.portfolio.lower()]
        if not selected:
            logger.error(f"Portfolio '{args.portfolio}' not found in configuration.")
            return
        daemon.portfolios = selected

    if args.run_once or not args.loop:
        daemon.run_cycle()
    else:
        daemon.run_loop(interval_hours=args.interval_hours)


if __name__ == "__main__":
    main()
