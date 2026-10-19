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
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import schedule
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

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

from email_notifier import send_portfolio_alert, send_run_summary_email
from screener.financial_metrics_engine import evaluate_profiles, get_hard_financials
from screener.nlp_analyzer import rule_based_analyze_core_fundamentals
from screener.portfolio_manager import evaluate_position
from screener.web_verifier import (
    FATAL_RED_FLAG_PATTERNS,
    RED_FLAG_PATTERNS,
    WARN_PATTERNS,
    TURNAROUND_CATALYST_PATTERNS,
)
from screener.tiered_filters import (
    matches_universe_filter,
    get_market_cap_in_eur,
    get_market_cap_in_usd,
)
from quant_analytics import calculate_top_picks_conviction

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


def _atomic_write_json(target_path: Path, payload: Any, ensure_ascii: bool = False) -> None:
    """Writes JSON atomically via a PID-scoped temporary file and os.replace to prevent partial reads."""
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=ensure_ascii)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_path, target_path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


def _safe_load_json_list(target_path: Path, retries: int = 3) -> Optional[List[Dict[str, Any]]]:
    """
    Safely loads a JSON list (such as portfolio_*_history.json).
    - Retries on transient read/decode errors (e.g. concurrent network/SMB access).
    - Falls back to regex object recovery if the file tail was truncated.
    - Returns None if a non-empty file cannot be read at all, preventing callers from wiping history.
    """
    target_path = Path(target_path)
    if not target_path.exists():
        return []
    try:
        if target_path.stat().st_size == 0:
            return []
    except Exception:
        pass

    raw_text = ""
    for attempt in range(retries):
        try:
            raw_text = target_path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            if isinstance(data, list):
                return data
        except Exception:
            if attempt < retries - 1:
                time.sleep(0.1)

    # Regex recovery for truncated JSON list of flat objects
    if raw_text:
        recovered: List[Dict[str, Any]] = []
        for match in re.findall(r"\{[^{}]+\}", raw_text):
            try:
                obj = json.loads(match)
                if isinstance(obj, dict):
                    recovered.append(obj)
            except Exception:
                continue
        if recovered:
            logger.warning(f"Recovered {len(recovered)} JSON entries from partially truncated {target_path.name}")
            return recovered

    return None


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

        _atomic_write_json(metrics_path, metrics, ensure_ascii=True)
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


from screener.currency_utils import (
    get_live_fx_rates,
    get_ticker_currency,
    get_fx_to_account,
    get_ticker_region,
)



def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    """Calculates Relative Strength Index (RSI)."""
    if len(prices) < period + 1:
        return 50.0
    delta = prices.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    valid_g = avg_gain.dropna()
    valid_l = avg_loss.dropna()
    if valid_g.empty or valid_l.empty:
        return 50.0
    last_gain = float(valid_g.iloc[-1])
    last_loss = float(valid_l.iloc[-1])
    if last_gain == 0.0 and last_loss == 0.0:
        return 50.0
    if last_loss == 0.0:
        return 100.0
    rs = last_gain / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


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
    slot_size: Optional[float] = None
    min_conviction_score: Optional[int] = None
    min_exit_conviction_score: Optional[int] = None
    required_tags: List[str] = field(default_factory=list)
    volume_surge_multiplier: float = 3.0
    min_price_change_pct: float = 2.0
    trailing_stop_pct: float = 0.06
    max_holding_days: int = 21
    require_nlp_clean: bool = True
    min_adv_20d: Optional[float] = None
    # Task 1: Exhaustion & Overbought filter for momentum breakouts
    max_3d_runup_pct: Optional[float] = 0.40
    max_rsi_14: Optional[float] = 80.0
    # Task 2: Partial Take-Profit for fundamental portfolios
    partial_take_profit: bool = False
    take_profit_threshold_pct: float = 0.25
    partial_sell_fraction: float = 0.50
    partial_trailing_stop_pct: float = 0.15
    # Task 4: Region-differentiated Stop-Loss (US -8%, Nordic -12%)
    stop_loss_pct: Optional[float] = None
    us_stop_loss_pct: float = 0.08
    nordic_stop_loss_pct: float = 0.12
    # Task 3: Market Regime cash buffer in CHOPPY/BEAR market
    market_regime_cash_buffer_pct: float = 0.40
    # Architecture requirements: Smart Routing, Strict Limit Orders & Tax Drag Optimization
    order_type: str = "LIMIT"
    limit_buffer_pct: float = 0.002
    time_in_force: str = "FOK_15MIN"
    account_tax_profile: str = "AOT"
    description: Optional[str] = None
    ignore_hodl_positions: bool = False
    portfolio_max_drawdown_alert_pct: float = -15.0
    sub_strategies: Dict[str, Any] = field(default_factory=dict)
    execution_constraints: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, pid: str, data: Dict[str, Any]) -> "PortfolioConfig":
        adv_val = float(data.get("min_adv_20d", data.get("min_adv", 50000.0)))
        sub_strats = data.get("sub_strategies", {})
        exec_constraints = data.get("execution_constraints", {})

        # Compute default slots from sub_strategies if not explicitly present
        if "slots" in data:
            slots_val = int(data["slots"])
        elif sub_strats:
            slots_val = sum(int(s.get("slots", 0)) for s in sub_strats.values())
        else:
            slots_val = 10

        strat_val = data.get("strategy")
        if not strat_val:
            strat_val = "Hybrid Core-Satellite" if sub_strats else "Profile B"

        # Check market_regime_cash_buffer_pct in execution_constraints or top-level
        cash_buf = exec_constraints.get("market_regime_cash_buffer_pct")
        if cash_buf is None:
            cash_buf = float(data.get("market_regime_cash_buffer_pct", 0.40))
        else:
            cash_buf = float(cash_buf)
            if cash_buf > 1.0:
                cash_buf = cash_buf / 100.0

        order_types = exec_constraints.get("allowed_order_types")
        order_type_val = order_types[0] if (order_types and isinstance(order_types, list)) else str(data.get("order_type", "LIMIT"))

        limit_buf = exec_constraints.get("limit_price_buffer_pct")
        if limit_buf is None:
            limit_buf = exec_constraints.get("max_slippage_allowance_pct")
        if limit_buf is not None:
            limit_buf = float(limit_buf)
            if limit_buf > 0.05:
                limit_buf = limit_buf / 100.0
        else:
            limit_buf = float(data.get("limit_buffer_pct", 0.002))

        dd_alert = float(exec_constraints.get("portfolio_max_drawdown_alert_pct", data.get("portfolio_max_drawdown_alert_pct", -15.0)))
        ignore_hodl = bool(data.get("ignore_hodl_positions", False))

        has_sub_strat_partial_tp = False
        if isinstance(sub_strats, dict):
            for s_info in sub_strats.values():
                rules = s_info.get("rules", []) if isinstance(s_info, dict) else []
                if any("Partial_TP" in str(r) for r in rules):
                    has_sub_strat_partial_tp = True
                    break

        partial_tp = bool(data.get("partial_take_profit", False)) or has_sub_strat_partial_tp

        return cls(
            portfolio_id=pid,
            strategy=strat_val,
            dead_money_days=int(data.get("dead_money_days", 180)),
            min_adv=adv_val,
            min_adv_20d=adv_val,
            slots=slots_val,
            start_cash=float(data.get("start_cash", 10000.0)),
            regions=data.get("regions", ["FI", "SE", "US"]),
            extra_filter=data.get("extra_filter"),
            slot_size=float(data["slot_size"]) if data.get("slot_size") is not None else None,
            min_conviction_score=int(data["min_conviction_score"]) if data.get("min_conviction_score") is not None else None,
            min_exit_conviction_score=int(data["min_exit_conviction_score"]) if data.get("min_exit_conviction_score") is not None else None,
            required_tags=data.get("required_tags", []),
            volume_surge_multiplier=float(data.get("volume_surge_multiplier", 3.0)),
            min_price_change_pct=float(data.get("min_price_change_pct", 2.0)),
            trailing_stop_pct=float(data.get("trailing_stop_pct", 0.06)),
            max_holding_days=int(data.get("max_holding_days", 21)),
            require_nlp_clean=bool(data.get("require_nlp_clean", True)),
            max_3d_runup_pct=float(data["max_3d_runup_pct"]) if data.get("max_3d_runup_pct") is not None else 0.40,
            max_rsi_14=float(data["max_rsi_14"]) if data.get("max_rsi_14") is not None else 80.0,
            partial_take_profit=partial_tp,
            take_profit_threshold_pct=float(data.get("take_profit_threshold_pct", 0.25)),
            partial_sell_fraction=float(data.get("partial_sell_fraction", 0.50)),
            partial_trailing_stop_pct=float(data.get("partial_trailing_stop_pct", 0.15)),
            stop_loss_pct=float(data["stop_loss_pct"]) if data.get("stop_loss_pct") is not None else None,
            us_stop_loss_pct=float(data.get("us_stop_loss_pct", 0.08)),
            nordic_stop_loss_pct=float(data.get("nordic_stop_loss_pct", 0.12)),
            market_regime_cash_buffer_pct=cash_buf,
            order_type=order_type_val,
            limit_buffer_pct=limit_buf,
            time_in_force=str(data.get("time_in_force", "FOK_15MIN")),
            account_tax_profile=str(data.get("account_tax_profile", "AOT")),
            description=data.get("description"),
            ignore_hodl_positions=ignore_hodl,
            portfolio_max_drawdown_alert_pct=dd_alert,
            sub_strategies=sub_strats,
            execution_constraints=exec_constraints,
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
            last_err = None
            for attempt in range(3):
                try:
                    with open(self.state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.cash_balance = float(data.get("cash_balance", self.config.start_cash))
                        self.starting_balance = float(data.get("starting_balance", self.config.start_cash))
                        self.currency = data.get("currency", "EUR")
                        self.positions = [self._normalize_position(p) for p in data.get("positions", [])]
                        self.created_at = data.get("created_at", self.created_at)
                        self.updated_at = data.get("updated_at", self.updated_at)
                        try:
                            self._loaded_mtime = self.state_file.stat().st_mtime
                        except Exception:
                            self._loaded_mtime = 0.0
                        return
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        time.sleep(0.1)
            logger.warning(f"[{self.config.portfolio_id}] Could not load {self.state_file.name}: {last_err}. Initializing.")

        self.save_state()

    def reload_if_changed_on_disk(self) -> None:
        """Reloads state from disk if the state file was modified externally (e.g. via CLI --set-holding) since last load/save."""
        if not self.state_file.exists():
            return
        try:
            current_mtime = self.state_file.stat().st_mtime
            last_mtime = getattr(self, "_loaded_mtime", 0.0) or 0.0
            if current_mtime > last_mtime + 1e-6:
                logger.info(f"[{self.config.portfolio_id}] Detected external state update on disk ({self.state_file.name}); reloading before cycle execution.")
                self.load_state()
        except Exception:
            pass

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

        buy_px = float(_get("buy_price", "buy price") or pos.get("Buy Price", 0.0))
        sh = float(_get("shares") or pos.get("Shares", 0.0))
        cap_inv = float(_get("capital_invested", "capital invested") or pos.get("Capital Invested", 0.0))
        if cap_inv <= 0.0 and buy_px > 0.0 and sh > 0.0:
            cap_inv = round(buy_px * sh, 2)

        normalized = {
            "Ticker":           _get("ticker") or pos.get("Ticker", ""),
            "Buy Date":         _get("buy_date", "buy date") or pos.get("Buy Date", ""),
            "Buy Price":        buy_px,
            "Shares":           sh,
            "Capital Invested": cap_inv,
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


    def is_hodl_position(self, pos: Dict[str, Any]) -> bool:
        """Returns True if the position is marked as an isolated HODL position."""
        if bool(pos.get("hodl", False)):
            return True
        if self.config.ignore_hodl_positions:
            t_sym = str(pos.get("Ticker") or pos.get("ticker", "")).strip().upper()
            if t_sym in ["FARON.HE", "FARON"] or pos.get("note") == "Lottolappu":
                return True
        return False

    def active_positions(self) -> List[Dict[str, Any]]:
        """Returns non-HODL positions that count toward active strategy trading slots."""
        return [p for p in self.positions if not self.is_hodl_position(p)]

    def set_holding(self, symbol: str, shares: int, buy_price: float, hodl: bool = False) -> Optional[Dict[str, Any]]:
        """Adds, updates, or removes (if shares <= 0) a stock holding in this portfolio and saves state."""
        sym_upper = str(symbol).strip().upper()
        shares_int = int(shares)
        price_f = float(buy_price)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if shares_int <= 0:
            self.positions = [
                p for p in self.positions
                if str(p.get("Ticker") or p.get("ticker", "")).strip().upper() != sym_upper
            ]
            self.save_state()
            return None

        curr = get_ticker_currency(sym_upper)
        cap_inv = round(shares_int * price_f, 2)

        updated_pos: Optional[Dict[str, Any]] = None
        for p in self.positions:
            if str(p.get("Ticker") or p.get("ticker", "")).strip().upper() == sym_upper:
                p["Ticker"] = sym_upper
                p["Shares"] = shares_int
                p["Buy Price"] = price_f
                p["Capital Invested"] = cap_inv
                p["Currency"] = p.get("Currency") or curr
                if not p.get("Buy Date"):
                    p["Buy Date"] = today_str
                if hodl:
                    p["hodl"] = True
                    p["Strategy"] = "HODL"
                    p["Sub_Strategy"] = "hodl_isolated"
                updated_pos = p
                break

        if updated_pos is None:
            new_pos: Dict[str, Any] = {
                "Ticker": sym_upper,
                "Buy Date": today_str,
                "Buy Price": price_f,
                "Shares": shares_int,
                "Capital Invested": cap_inv,
                "Strategy": "HODL" if hodl else self.config.strategy,
                "Currency": curr,
                "Peak Price": price_f,
            }
            if hodl:
                new_pos["hodl"] = True
                new_pos["Sub_Strategy"] = "hodl_isolated"
            self.positions.append(new_pos)
            updated_pos = new_pos

        self.save_state()
        return updated_pos

    def set_cash(self, cash_eur: float) -> None:
        """Sets current cash balance and saves state atomically."""
        self.cash_balance = float(cash_eur)
        self.save_state()

    def save_state(self) -> None:
        """Saves current state to JSON file atomically."""
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
        try:
            _atomic_write_json(self.state_file, payload, ensure_ascii=False)
            try:
                self._loaded_mtime = self.state_file.stat().st_mtime
            except Exception:
                pass
        except Exception as e:
            logger.error(f"[{self.config.portfolio_id}] Failed to save state to {self.state_file}: {e}")

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
        loaded_history = _safe_load_json_list(self.equity_file)
        if loaded_history is None:
            logger.warning(
                f"[{self.config.portfolio_id}] Skipping snapshot write: could not safely read existing {self.equity_file.name}"
            )
            return
        history = loaded_history

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

        # Guard: If portfolio has active (non-HODL) positions but valuation returned 0.0 (API/feed glitch), skip snapshot
        active_pos = self.active_positions()
        if active_pos and stock_value_eur <= 0.0:
            logger.warning(
                f"[{self.config.portfolio_id}] Skipping snapshot: {len(active_pos)} active positions exist but stock_value_eur is 0.0"
            )
            return

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
                _atomic_write_json(self.equity_file, history, ensure_ascii=False)
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
        self.momentum_cache: Dict[str, Dict[str, Any]] = {}
        self.universe_metadata: Dict[str, Dict[str, Any]] = {}
        self.market_regime: Dict[str, Any] = {}
        # Operational health tracker (LLM fallback rate, data freshness blocks)
        self.operational_metrics: Dict[str, Any] = load_operational_metrics()
        # Global cache of evaluated news items (ticker, clean_headline) -> (decision, reason)
        self.evaluated_news_cache: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self._load_evaluated_news_cache()

    def _load_evaluated_news_cache(self, archive_path: Path = DEFAULT_NLP_ARCHIVE_CSV, max_age_days: float = 14.0) -> None:
        """Pre-populates the in-memory news evaluation cache from existing archive (filtering out stale >14d REJECTs)."""
        if not archive_path.exists():
            return
        now_utc = datetime.now(timezone.utc)
        try:
            with open(archive_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 5:
                        ts_str = row[0].strip()
                        ticker = row[1].strip().upper()
                        headline = " ".join(row[2].split())
                        decision = row[3].strip().upper()
                        reason = row[4].strip()
                        if not (ticker and headline):
                            continue
                        if decision == "REJECT" and ts_str:
                            try:
                                clean_ts = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
                                dt_val = datetime.fromisoformat(clean_ts)
                                if dt_val.tzinfo is None:
                                    dt_val = dt_val.replace(tzinfo=timezone.utc)
                                age_days = (now_utc - dt_val).total_seconds() / 86400.0
                                if age_days > max_age_days:
                                    # Stale historical REJECT (>14d old): do not permanently blacklist ticker
                                    continue
                            except Exception:
                                pass
                        self.evaluated_news_cache[(ticker, headline)] = (decision, reason)
            if self.evaluated_news_cache:
                logger.info(f"Loaded {len(self.evaluated_news_cache)} active news evaluations into NLP cache.")
        except Exception as e:
            logger.debug(f"Could not pre-load NLP cache: {e}")

    def maybe_update_universe(self, force: bool = False, max_age_days: float = 7.0) -> Tuple[int, List[str]]:
        """
        Triggers periodic dynamic universe update (weekly) to scan Nordnet for new liquid listings.
        If new tickers were added, re-reads universe metadata into memory.
        """
        try:
            from screener.dynamic_universe_updater import scan_and_update_universe
            added_cnt, added_tickers = scan_and_update_universe(
                clean_csv_path=self.clean_universe_path,
                force=force,
                max_age_days=max_age_days,
            )
            return added_cnt, added_tickers
        except Exception as e:
            logger.warning(f"Dynamic universe update encountered an error: {e}")
            return 0, []

    def refresh_data(self, held_tickers: Set[str], force_universe_sync: bool = False) -> None:
        """Fetches live data strictly ONCE for all universe stocks and held tickers."""
        # 0. Check periodic dynamic universe update (weekly scan from Nordnet)
        self.maybe_update_universe(force=force_universe_sync)

        logger.info("📡 [MARKET ENGINE] Refreshing shared market data strictly ONCE for this cycle...")
        self.fx_rates = get_live_fx_rates()
        logger.info(f"📡 [MARKET ENGINE] Live FX Rates: EURUSD={self.fx_rates.get('EURUSD', 1.08):.4f}, EURSEK={self.fx_rates.get('EURSEK', 11.30):.4f}")

        # Load or compute institutional market regime (IWC / Russell Microcap benchmark)
        self.market_regime = {}
        regime_file = BASE_DIR / "data" / "market_regime.json"
        if regime_file.exists():
            try:
                self.market_regime = json.loads(regime_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        if not self.market_regime:
            try:
                from quant_analytics import get_market_regime
                self.market_regime = get_market_regime(benchmark_ticker="IWC")
            except Exception as e:
                logger.debug(f"Could not calculate market regime: {e}")
        if self.market_regime:
            reg_name = self.market_regime.get("regime", "UNKNOWN")
            sma_50 = self.market_regime.get("sma_50", 0.0)
            last_p = self.market_regime.get("last_price", 0.0)
            logger.info(f"📊 [MARKET ENGINE] Market Regime: {reg_name} (IWC: {last_p:.2f}, SMA50: {sma_50:.2f})")

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
        logger.info(f"📡 [MARKET ENGINE] Initializing data cache for {len(all_tickers)} symbols ({len(held_tickers)} held across portfolios)...")

        for ticker in all_tickers:
            fallback_px = self.universe_metadata.get(ticker, {}).get("current_price", 0.0)
            if fallback_px > 0:
                self.prices_cache[ticker] = fallback_px

        # Refresh live price, financials, and news radar for active held positions
        for ticker in held_tickers:
            px = self._fetch_live_price(ticker)
            if px and px > 0:
                self.prices_cache[ticker] = px
            self.get_ticker_hard_facts(ticker)
            self.news_radar_cache[ticker] = self._evaluate_news_radar(ticker)

        logger.info(f"✅ [MARKET ENGINE] Market data pre-fetch completed. Ready to process portfolios.")

    def get_ticker_hard_facts(self, ticker: str) -> Dict[str, Any]:
        """Returns hard financials from cache, or fetches on-demand."""
        if ticker in self.hard_facts_cache:
            hard_facts = self.hard_facts_cache[ticker]
            if "is_profile_a" not in hard_facts:
                prof_eval = evaluate_profiles(hard_facts)
                hard_facts["is_profile_a"] = prof_eval.get("is_profile_a", False)
                hard_facts["is_profile_b"] = prof_eval.get("is_profile_b", False)
                hard_facts["profile_signal"] = prof_eval.get("signal", "REJECT")
                self.eval_profiles_cache[ticker] = prof_eval
            return hard_facts
        hard_facts = get_hard_financials(ticker)
        prof_eval = evaluate_profiles(hard_facts)
        hard_facts["is_profile_a"] = prof_eval.get("is_profile_a", False)
        hard_facts["is_profile_b"] = prof_eval.get("is_profile_b", False)
        hard_facts["profile_signal"] = prof_eval.get("signal", "REJECT")
        self.hard_facts_cache[ticker] = hard_facts
        self.eval_profiles_cache[ticker] = prof_eval
        return hard_facts

    def _fetch_live_price(self, ticker: str) -> Optional[float]:
        try:
            t = yf.Ticker(ticker)
            cur_price = None
            info = getattr(t, "fast_info", None)
            if info and hasattr(info, "last_price") and info.last_price:
                cur_price = float(info.last_price)
            hist = t.history(period="1mo")
            if not hist.empty:
                valid = hist["Close"].dropna()
                if not valid.empty:
                    if not cur_price:
                        cur_price = float(valid.iloc[-1])
                    day_vol = float(hist["Volume"].iloc[-1]) if "Volume" in hist else 0.0
                    prev = float(valid.iloc[-2]) if len(valid) >= 2 else (float(getattr(info, "previous_close", 0.0) or 0.0) if info else 0.0)

                    # Split sanity check: If overnight ratio > 2.0 or < 0.5, verify if an unadjusted stock split occurred
                    if prev > 0 and (cur_price / prev > 2.0 or cur_price / prev < 0.5):
                        try:
                            sp = t.splits
                            if not sp.empty:
                                last_sp_date = pd.Timestamp(sp.index[-1]).tz_convert(None).floor("D") if getattr(sp.index[-1], "tz", None) else pd.Timestamp(sp.index[-1]).floor("D")
                                today_dt = pd.Timestamp.now().floor("D")
                                if (today_dt - last_sp_date).days <= 7:
                                    factor = float(sp.iloc[-1])
                                    if factor > 0 and factor != 1.0:
                                        prev = prev / factor
                        except Exception:
                            pass

                    chg_pct = round(((cur_price - prev) / prev) * 100.0, 2) if prev > 0 else 0.0

                    # 3-day cumulative runup calculation
                    if len(valid) >= 4:
                        p_3d = float(valid.iloc[-4])
                        runup_3d_pct = round(((cur_price - p_3d) / p_3d) * 100.0, 2) if p_3d > 0 else 0.0
                    elif len(valid) >= 2:
                        p_old = float(valid.iloc[0])
                        runup_3d_pct = round(((cur_price - p_old) / p_old) * 100.0, 2) if p_old > 0 else 0.0
                    else:
                        runup_3d_pct = chg_pct

                    # 14-period technical RSI calculation
                    rsi_14 = round(calculate_rsi(valid, period=14), 2)

                    self.momentum_cache[ticker] = {
                        "price": cur_price,
                        "day_volume": day_vol,
                        "day_change_pct": chg_pct,
                        "runup_3d_pct": runup_3d_pct,
                        "rsi_14": rsi_14,
                    }
                    return cur_price
            if cur_price:
                # History was empty or missing valid closes.
                # Invalidate stale momentum cache so stale price and signals won't be used!
                self.momentum_cache.pop(ticker, None)
                return cur_price
        except Exception as e:
            logger.debug(f"Could not fetch price for {ticker}: {e}")
            self.momentum_cache.pop(ticker, None)
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
                # Yahoo Finance does not index Nordic micro/midcap news.
                # Fall back to localized Google News RSS & regulatory PR channels (Cision, MFN, Kauppalehti).
                if not hasattr(self, "_web_verifier") or self._web_verifier is None:
                    from screener.web_verifier import WebSearchVerifier
                    self._web_verifier = WebSearchVerifier()
                try:
                    snippets = self._web_verifier.fetch_snippets(ticker)
                    raw_news = [{"title": s, "summary": ""} for s in snippets]
                except Exception as snip_err:
                    logger.debug(f"WebSearchVerifier fetch failed for {ticker}: {snip_err}")
                    raw_news = []

            if not raw_news:
                return "HOLD", "No new press releases"

            warning_found = False
            warning_reason = ""
            strong_buy_found = False
            strong_buy_reason = ""
            turnaround_found = False
            turnaround_reason = ""
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
                        elif item_decision not in ("REJECT", "WARN"):
                            # Check positive catalysts (STRONG BUY / WATCH_TURNAROUND)
                            v_details = nlp_res.get("verdict_details", {}) if isinstance(nlp_res, dict) else {}
                            verdict = (
                                (nlp_res.get("verdict") if isinstance(nlp_res, dict) else None)
                                or v_details.get("verdict")
                            )
                            v_reason = v_details.get("reasoning", "")
                            if verdict in ("STRONG BUY", "BUY_PROFILE_A"):
                                item_decision = "STRONG BUY"
                                item_reason = f"Strong NLP Catalyst: {v_reason or title or clean_headline}"
                            elif verdict == "WATCH_TURNAROUND":
                                item_decision = "WATCH_TURNAROUND"
                                item_reason = f"Turnaround Catalyst: {v_reason or title or clean_headline}"
                            else:
                                for pat in TURNAROUND_CATALYST_PATTERNS:
                                    if pat.lower() in full_text.lower():
                                        item_decision = "WATCH_TURNAROUND"
                                        item_reason = f"Turnaround catalyst '{pat}' detected in headline: {title or clean_headline}"
                                        break

                    # Log ONCE to CSV
                    append_nlp_decision(ticker, clean_headline, item_decision, item_reason)

                    # Store in global cache
                    self.evaluated_news_cache[cache_key] = (item_decision, item_reason)

                # Aggregate ticker status: REJECT > WARN > STRONG BUY > WATCH_TURNAROUND > HOLD
                if item_decision == "REJECT":
                    return "REJECT", item_reason
                elif item_decision == "WARN":
                    warning_found = True
                    warning_reason = item_reason
                elif item_decision == "STRONG BUY":
                    if not strong_buy_found:
                        strong_buy_found = True
                        strong_buy_reason = item_reason
                elif item_decision == "WATCH_TURNAROUND":
                    if not turnaround_found:
                        turnaround_found = True
                        turnaround_reason = item_reason

            if warning_found:
                return "WARN", warning_reason
            if strong_buy_found:
                return "STRONG BUY", strong_buy_reason
            if turnaround_found:
                return "WATCH_TURNAROUND", turnaround_reason

            if valid_headline_count == 0:
                return "HOLD", "No new press releases"

            return "HOLD", f"Scanned {valid_headline_count} recent news items with no fatal red flags"

        except Exception as e:
            logger.debug(f"Error checking news for {ticker}: {e}")
            return "HOLD", "News scan error"

    def get_ticker_nlp_status(self, ticker: str) -> Tuple[str, str]:
        """
        Determines the active global NLP status for a ticker.
        Checks:
          1. Any REJECT in evaluated_news_cache (populated from nlp_decisions_archive.csv & runtime)
          2. Cached result in news_radar_cache
          3. Direct archive CSV scan fallback
          4. On-demand evaluation via _evaluate_news_radar(ticker)
        Returns (decision, reason), e.g. ("REJECT", "Fatal red flag...") or ("HOLD", "...").
        """
        ticker_clean = ticker.strip().upper()

        # 1. Any historical or cached REJECT in evaluated_news_cache overrides everything
        for (t, _), (dec, rsn) in self.evaluated_news_cache.items():
            if t == ticker_clean and dec == "REJECT":
                return "REJECT", rsn

        # 2. Check pre-fetched news radar cache
        if ticker_clean in self.news_radar_cache:
            v, r = self.news_radar_cache[ticker_clean]
            if v == "REJECT":
                return v, r

        # 3. Direct archive scan fallback (only recent <= 14d REJECTs)
        archive_file = self.clean_universe_path.parent / "nlp_decisions_archive.csv"
        if archive_file.exists():
            now_utc = datetime.now(timezone.utc)
            try:
                with open(archive_file, "r", encoding="utf-8", errors="ignore") as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if len(row) >= 5 and row[1].strip().upper() == ticker_clean and row[3].strip().upper() == "REJECT":
                            ts_str = row[0].strip()
                            if ts_str:
                                try:
                                    clean_ts = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
                                    dt_val = datetime.fromisoformat(clean_ts)
                                    if dt_val.tzinfo is None:
                                        dt_val = dt_val.replace(tzinfo=timezone.utc)
                                    if (now_utc - dt_val).total_seconds() / 86400.0 > 14.0:
                                        continue
                                except Exception:
                                    pass
                            return "REJECT", row[4].strip()
            except Exception:
                pass

        if ticker_clean in self.news_radar_cache:
            return self.news_radar_cache[ticker_clean]

        # 4. On-demand evaluation if never checked
        verdict, reason = self._evaluate_news_radar(ticker_clean)
        self.news_radar_cache[ticker_clean] = (verdict, reason)
        return verdict, reason



class MasterLiveTradingDaemon:
    """
    Master Live Trading Daemon.
    Orchestrates the 10 parallel portfolios defined in portfolios_config.yaml.
    """

    def __init__(self, config_yaml_path: Path = DEFAULT_PORTFOLIOS_YAML, tax_profile: Optional[str] = None):
        self.config_yaml_path = config_yaml_path
        self.tax_profile = tax_profile
        self.portfolios: List[PortfolioInstance] = []
        self.market_engine = MarketDataEngine()
        self.load_configurations(tax_profile_override=tax_profile)

    def load_configurations(self, tax_profile_override: Optional[str] = None) -> None:
        """Loads portfolios from YAML and applies tax efficiency & smart routing policies."""
        if not self.config_yaml_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_yaml_path}")

        with open(self.config_yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        tax_profile = (tax_profile_override or str(data.get("account_tax_profile", "AOT"))).upper()
        order_routing = data.get("order_routing", {})
        limit_buffer = float(order_routing.get("limit_buffer_pct", 0.002))
        tif = str(order_routing.get("time_in_force", "FOK_15MIN"))

        portfolios_dict = data.get("portfolios", {})
        self.portfolios = []
        for pid, pdata in portfolios_dict.items():
            pdata_copy = dict(pdata)
            pdata_copy.setdefault("account_tax_profile", tax_profile)
            pdata_copy.setdefault("limit_buffer_pct", limit_buffer)
            pdata_copy.setdefault("time_in_force", tif)

            cfg = PortfolioConfig.from_dict(pid, pdata_copy)

            # Tax Drag Optimization & Capital Allocation
            if tax_profile == "AOT":
                # On personal AOT account: suppress P12 allocation to 5,000 € (slot 1,000 €) to minimize tax churn
                if pid == "P12_Momentum_Breakout":
                    cfg.start_cash = 5000.0
                    cfg.slot_size = 1000.0
                    cfg.min_adv = max(cfg.min_adv, 500000.0)
                    cfg.min_adv_20d = max(cfg.min_adv_20d or 0.0, 500000.0)
                elif pid in ["P5_Nordic_Only", "P9_High_Conviction", "P10_Micro_Sniper", "P11_Meta_Consensus"]:
                    # Reallocate capital into multi-week/month holding portfolios
                    cfg.start_cash = 11250.0
                    if cfg.slot_size:
                        cfg.slot_size = cfg.start_cash / cfg.slots
            else:
                # OY / corporate account: full 10,000 € compounding
                if pid == "P12_Momentum_Breakout":
                    cfg.min_adv = max(cfg.min_adv, 500000.0)
                    cfg.min_adv_20d = max(cfg.min_adv_20d or 0.0, 500000.0)

            self.portfolios.append(PortfolioInstance(cfg))

        logger.info(f"Loaded {len(self.portfolios)} portfolio configurations from {self.config_yaml_path.name}")
        if tax_profile == "AOT":
            logger.info(
                f"🏛️ [TAX OPTIMIZATION: AOT ACTIVE] High-turnover P12 capital reduced to 5,000 € (slot 1,000 €) "
                f"to minimize capital gains tax drag. Long-holding portfolios P5/P9/P10/P11 boosted to 11,250 €."
            )
        elif tax_profile == "OST":
            logger.info(
                f"🏛️ [TAX PROFILE: OST ACTIVE] Finnish Equity Savings Account mode active (0% capital gains tax within envelope). "
                f"0.50% FX conversion cost on US stocks, no dividend-paying US stocks permitted."
            )
        else:
            logger.info(
                f"🏢 [TAX OPTIMIZATION: OY ACTIVE] Full compounding mode active. All portfolios operate at 10,000 € capital."
            )

        # Task 3 Architectural Note (P1_Base, P2_Fast_Cycle, P3_Diamond_Hands):
        # Empirical overlap tests confirmed that P1, P2, and P3 currently have 100% portfolio overlap.
        # However, they intentionally require no parameter changes: their strategic divergence is handled
        # by existing temporal exit rules (dead_money_days: 90d for P2 vs 180d for P1 vs 365d for P3)
        # which have simply not triggered yet due to the short walk-forward history.


    def execute_phase1_exits(self, portfolio: PortfolioInstance, run_trades: Optional[List[Dict[str, Any]]] = None) -> int:
        """
        Phase 1: Portfolio Management (Tri-Layer Exit & Dead Money Timer).
        Returns count of closed positions.
        """
        closed_count = 0
        retained_positions: List[Dict[str, Any]] = []
        today = datetime.now(timezone.utc).date()

        # Check if portfolio uses consensus conviction exits
        conviction_map: Dict[str, int] = {}
        if portfolio.config.strategy == "Meta_Consensus" or portfolio.config.min_exit_conviction_score is not None:
            try:
                df_conv = calculate_top_picks_conviction(portfolio.portfolio_dir)
                if not df_conv.empty and "Ticker" in df_conv.columns and "Conviction Score (0-14)" in df_conv.columns:
                    conviction_map = dict(zip(df_conv["Ticker"].str.upper(), df_conv["Conviction Score (0-14)"]))
            except Exception as e:
                logger.debug(f"Failed to calculate conviction map for {portfolio.config.portfolio_id} exits: {e}")

        for pos in portfolio.positions:
            ticker = pos.get("Ticker") or pos.get("ticker")
            buy_price = float(pos.get("Buy Price") or pos.get("buy_price", 0.0))
            shares = int(float(pos.get("Shares") or pos.get("shares", 0)))
            capital_invested = float(pos.get("Capital Invested") or pos.get("capital_invested", shares * buy_price))
            buy_date = pos.get("Buy Date") or pos.get("buy_date")
            ticker_curr = pos.get("Currency") or pos.get("currency") or get_ticker_currency(ticker)
            fx_to_acc = get_fx_to_account(ticker_curr, portfolio.currency, self.market_engine.fx_rates)
            fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

            # --- HODL Protection (Task 3: Never touch holdings where hodl=True) ---
            is_hodl = bool(pos.get("hodl", False))
            if not is_hodl and portfolio.config.ignore_hodl_positions:
                if str(ticker).upper() in ["FARON.HE", "FARON"] or pos.get("note") == "Lottolappu":
                    is_hodl = True
            if is_hodl:
                logger.info(f"[{portfolio.config.portfolio_id}] 🔒 HODL position {ticker} protected. Retaining.")
                retained_positions.append(pos)
                continue

            current_price = self.market_engine.prices_cache.get(ticker)
            if not current_price or current_price <= 0:
                logger.warning(f"[{portfolio.config.portfolio_id}] ⚠️ {ticker}: Price unavailable. Retaining.")
                retained_positions.append(pos)
                continue

            cur_peak = float(pos.get("Peak Price") or pos.get("peak_price") or pos.get("highest_price_seen") or buy_price)
            if current_price > cur_peak:
                cur_peak = current_price
                pos["Peak Price"] = cur_peak
                if "peak_price" in pos:
                    pos["peak_price"] = cur_peak
                if "highest_price_seen" in pos:
                    pos["highest_price_seen"] = cur_peak

            # --- Task 2: Partial Take-Profit (+25% gain triggers 50% partial sale into cash) ---
            if portfolio.config.partial_take_profit:
                has_taken_partial = bool(pos.get("partial_profit_taken", False))
                gain_from_buy = (current_price - buy_price) / buy_price if buy_price > 0 else 0.0

                if not has_taken_partial and gain_from_buy >= portfolio.config.take_profit_threshold_pct:
                    shares_to_sell = math.floor(shares * portfolio.config.partial_sell_fraction)
                    if shares_to_sell >= 1 and (shares - shares_to_sell) >= 1:
                        gross_sale_value = shares_to_sell * current_price
                        min_fee_local = MIN_BROKER_FEE * fx_acc_to_local
                        transaction_fee = calculate_transaction_fee(gross_sale_value, min_fee=min_fee_local)
                        net_return = gross_sale_value - transaction_fee
                        cap_sold = capital_invested * (shares_to_sell / shares)
                        net_pnl = net_return - cap_sold
                        net_return_acc = net_return * fx_to_acc

                        portfolio.cash_balance += net_return_acc

                        tp_reason = (
                            f"PARTIAL_TAKE_PROFIT (+{gain_from_buy*100:.1f}% >= +{portfolio.config.take_profit_threshold_pct*100:.0f}%, "
                            f"sold {shares_to_sell}/{shares} shares)"
                        )
                        trade_record = {
                            "Ticker": ticker,
                            "Buy Date": buy_date,
                            "Sell Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                            "Buy Price": buy_price,
                            "Sell Price": current_price,
                            "Shares": shares_to_sell,
                            "Capital Invested": round(cap_sold, 2),
                            "Gross Sale Value": round(gross_sale_value, 2),
                            "Transaction Fee": round(transaction_fee, 2),
                            "Net Return": round(net_return, 2),
                            "Net PnL": round(net_pnl, 2),
                            "Exit Reason": tp_reason,
                        }
                        portfolio.append_trade(trade_record)

                        logger.info(
                            f"💰 [{portfolio.config.portfolio_id} PARTIAL TP] {ticker} | "
                            f"Sold {shares_to_sell}/{shares} shares @ {current_price:.2f} {ticker_curr} (+{gain_from_buy*100:.1f}%) | "
                            f"Locked profit: {net_pnl:+.2f} {ticker_curr} ({net_return_acc:,.2f} {portfolio.currency} to cash) | "
                            f"Remaining: {shares - shares_to_sell} shares"
                        )

                        if run_trades is not None:
                            run_trades.append({
                                "portfolio_id": portfolio.config.portfolio_id,
                                "action": "PARTIAL_TP",
                                "ticker": ticker,
                                "details": {
                                    "exit_reason": tp_reason,
                                    "sold_shares": shares_to_sell,
                                    "remaining_shares": shares - shares_to_sell,
                                    "sell_price": f"{current_price:.2f} {ticker_curr}",
                                    "net_pnl": f"{net_pnl:+.2f} {ticker_curr}",
                                    "cash_balance": f"{portfolio.cash_balance:,.2f} {portfolio.currency}",
                                },
                            })

                        rem_shares = shares - shares_to_sell
                        rem_capital = round(capital_invested - cap_sold, 2)
                        for k_sh in ("Shares", "shares"):
                            if k_sh in pos:
                                pos[k_sh] = rem_shares
                        for k_cap in ("Capital Invested", "capital_invested"):
                            if k_cap in pos:
                                pos[k_cap] = rem_capital
                        pos["partial_profit_taken"] = True
                        pos["partial_profit_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        pos["Peak Price"] = cur_peak
                        if "peak_price" in pos:
                            pos["peak_price"] = cur_peak
                        if "highest_price_seen" in pos:
                            pos["highest_price_seen"] = cur_peak

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

            # Check conviction drop for Meta_Consensus
            exit_min_score = portfolio.config.min_exit_conviction_score
            is_conviction_exit = False
            conviction_reason = ""
            if exit_min_score is not None or portfolio.config.strategy == "Meta_Consensus":
                thresh = exit_min_score if exit_min_score is not None else 6
                current_score = int(conviction_map.get(ticker.upper(), 0))
                if current_score < thresh:
                    is_conviction_exit = True
                    conviction_reason = f"CONVICTION_DROP (Score {current_score} < {thresh})"

            sub_strat = pos.get("Sub_Strategy") or pos.get("sub_strategy")
            is_p13_sub_strat = sub_strat in (
                "core_nordic_midcap_quality",
                "satellite_us_microcap_growth",
                "satellite_nordic_microcap_catalyst",
            )

            # For P13 sub-strategies, do not let generic Profile B Layer 3 (revenue_growth_yoy < 0) or generic dead_money_days
            # prematurely force-sell Core compounders or Turnaround satellites; P13 sub-strategies enforce their own tailored rules below.
            action, reason = evaluate_position(
                position={
                    "buy_price": buy_price,
                    "ticker": ticker,
                    "buy_date": buy_date,
                    "highest_price_seen": cur_peak,
                    "catastrophic_stop": pos.get("catastrophic_stop") or pos.get("trailing_stop"),
                },
                current_price=current_price,
                latest_news_judgment=news_verdict,
                latest_financials=None if is_p13_sub_strat else financials_payload,
                dead_money_days=None if (pos.get("partial_profit_taken") or is_p13_sub_strat) else portfolio.config.dead_money_days,
            )

            if is_p13_sub_strat and reason == "LLM_NEWS_WARN_CONFIRMED":
                action = "HOLD"
                reason = "HOLD"

            if is_conviction_exit:
                action = "SELL"
                reason = conviction_reason
            elif portfolio.config.strategy == "Meta_Consensus" and news_verdict in ("WARN", "REJECT"):
                action = "SELL"
                reason = f"NLP_{news_verdict} ({news_reason})"

            # --- Task 4 & Task 3: Stop-Loss, Runner Rule, and Sub-Strategy Exits ---
            if portfolio.config.strategy != "Momentum_Breakout":
                if pos.get("partial_profit_taken", False):
                    # Runner_Rule: After Partial_TP, move stop to Break-Even (0.0%) and activate -15.0% Trailing_Stop
                    if current_price < buy_price:
                        action = "SELL"
                        reason = f"RUNNER_BREAKEVEN_STOP (Current {current_price:.2f} < Entry {buy_price:.2f})"
                    else:
                        drawdown_from_peak = (cur_peak - current_price) / cur_peak if cur_peak > 0 else 0.0
                        if drawdown_from_peak >= portfolio.config.partial_trailing_stop_pct:
                            action = "SELL"
                            reason = f"PARTIAL_RUNNER_TRAILING_STOP (Peak {cur_peak:.2f} -> {current_price:.2f}, -{drawdown_from_peak*100:.1f}% >= -{portfolio.config.partial_trailing_stop_pct*100:.1f}%)"
                elif action != "SELL":
                    loss_from_buy = (buy_price - current_price) / buy_price if buy_price > 0 else 0.0

                    if sub_strat == "core_nordic_midcap_quality":
                        # Core: Exit on Quarterly Fundamental Breakdown or Catastrophic Stop Loss (-15.0%)
                        if loss_from_buy >= 0.15:
                            action = "SELL"
                            reason = f"CATASTROPHIC_STOP_LOSS_CORE (Loss -{loss_from_buy*100:.1f}% >= -15.0%)"
                        elif news_verdict == "REJECT":
                            action = "SELL"
                            reason = f"QUARTERLY_FUNDAMENTAL_BREAKDOWN ({news_reason})"
                        else:
                            # Check fundamental deterioration from hard_facts
                            roe = hard_facts.get("roe")
                            roe_val = (roe * 100.0 if abs(roe) <= 2.0 else float(roe)) if roe is not None else None
                            ebit_margin = hard_facts.get("ebit_margin") or hard_facts.get("operating_margin")
                            ebit_val = (ebit_margin * 100.0 if abs(ebit_margin) <= 2.0 else float(ebit_margin)) if ebit_margin is not None else None
                            nd_ebitda = hard_facts.get("net_debt_to_ebitda")
                            if nd_ebitda is None:
                                net_debt = hard_facts.get("net_debt")
                                net_cash = hard_facts.get("net_cash")
                                if net_debt is None and net_cash is not None:
                                    net_debt = -net_cash
                                ebitda = hard_facts.get("ebitda")
                                if net_debt is not None and ebitda and ebitda > 0:
                                    nd_ebitda = net_debt / ebitda

                            breakdown_reasons = []
                            if roe_val is not None and roe_val < 8.0:
                                breakdown_reasons.append(f"ROE {roe_val:.1f}% < 8.0%")
                            if ebit_val is not None and ebit_val < 5.0:
                                breakdown_reasons.append(f"EBIT Margin {ebit_val:.1f}% < 5.0%")
                            if nd_ebitda is not None and float(nd_ebitda) > 3.5:
                                breakdown_reasons.append(f"Net Debt/EBITDA {float(nd_ebitda):.2f} > 3.5")

                            if breakdown_reasons:
                                action = "SELL"
                                reason = f"QUARTERLY_FUNDAMENTAL_BREAKDOWN ({', '.join(breakdown_reasons)})"

                    elif sub_strat in ["satellite_us_microcap_growth", "satellite_nordic_microcap_catalyst"]:
                        # Satellite: Stop-Loss -12.0% and Time-Stop: 45 trading days (or 63 calendar days) if PnL < +5.0%
                        if loss_from_buy >= 0.12:
                            action = "SELL"
                            reason = f"STOP_LOSS_SATELLITE (Loss -{loss_from_buy*100:.1f}% >= -12.0%)"
                        elif news_verdict == "REJECT":
                            action = "SELL"
                            reason = f"LLM_NEWS_REJECT ({news_reason})"
                        else:
                            try:
                                b_date = datetime.strptime(str(buy_date)[:10], "%Y-%m-%d").date()
                                try:
                                    import numpy as np
                                    trading_days = int(np.busday_count(b_date, today))
                                except Exception:
                                    trading_days = int((today - b_date).days * 5 / 7)
                                cal_days = (today - b_date).days
                            except Exception as e_dt:
                                logger.debug(f"Could not parse buy_date {buy_date} for {ticker}: {e_dt}")
                                trading_days = 0
                                cal_days = 0
                            pnl_pct = ((current_price - buy_price) / buy_price * 100.0) if buy_price > 0 else 0.0
                            if (trading_days >= 45 or cal_days >= 63) and pnl_pct < 5.0:
                                action = "SELL"
                                reason = f"TIME_STOP_DECAY ({trading_days} trading days >= 45d, PnL {pnl_pct:+.1f}% < +5.0%)"
                    else:
                        pos_market = pos.get("market") or pos.get("Market")
                        region = get_ticker_region(ticker, pos_market)
                        if portfolio.config.stop_loss_pct is not None:
                            effective_stop = portfolio.config.stop_loss_pct
                        elif region == "US":
                            effective_stop = portfolio.config.us_stop_loss_pct
                        else:
                            effective_stop = portfolio.config.nordic_stop_loss_pct

                        if loss_from_buy >= effective_stop:
                            action = "SELL"
                            reason = f"STOP_LOSS_{region} (Loss -{loss_from_buy*100:.1f}% >= -{effective_stop*100:.1f}%)"

            # Dedicated Momentum Breakout Exits (Strict Trailing Stop 6%, Stalled Momentum 21d, NLP Clean Guard)
            if portfolio.config.strategy == "Momentum_Breakout":
                drawdown_from_peak = (cur_peak - current_price) / cur_peak if cur_peak > 0 else 0.0
                try:
                    b_date = datetime.strptime(str(buy_date)[:10], "%Y-%m-%d").date()
                    holding_days = (today - b_date).days
                except Exception:
                    holding_days = 0

                if drawdown_from_peak >= portfolio.config.trailing_stop_pct:
                    action = "SELL"
                    reason = f"TRAILING_STOP (Peak {cur_peak:.2f} -> {current_price:.2f}, -{drawdown_from_peak*100:.1f}%)"
                elif holding_days >= portfolio.config.max_holding_days:
                    action = "SELL"
                    reason = f"MOMENTUM_STALL ({holding_days}d >= {portfolio.config.max_holding_days}d)"
                elif portfolio.config.require_nlp_clean and news_verdict == "REJECT":
                    action = "SELL"
                    reason = f"NLP_REJECT ({news_reason})"
                else:
                    action = "HOLD"
                    reason = ""

            if action == "SELL":
                gross_sale_value = shares * current_price
                ticker_curr = pos.get("Currency") or get_ticker_currency(ticker)
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
                            "reason": reason,
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

    def execute_phase2_sub_strategies_screening(
        self,
        portfolio: PortfolioInstance,
        run_trades: Optional[List[Dict[str, Any]]] = None,
        available_cash: float = 0.0,
        held_tickers: Optional[Set[str]] = None,
        cooldown_tickers: Optional[Dict[str, Any]] = None,
        today: Optional[date] = None,
    ) -> int:
        """
        Executes screening and sizing for composite/hybrid portfolios using sub-strategies
        (such as P13_Live_Candidate with Nordic MidCap Core and US/Nordic MicroCap Satellites).
        """
        cfg = portfolio.config
        if held_tickers is None:
            held_tickers = {p["Ticker"] for p in portfolio.positions}
        if cooldown_tickers is None:
            cooldown_tickers = {}
        if today is None:
            today = datetime.now(timezone.utc).date()

        new_buys = 0

        # Map each existing active (non-HODL) position to its sub-strategy
        positions_by_sub: Dict[str, List[Dict[str, Any]]] = {k: [] for k in cfg.sub_strategies.keys()}
        for pos in portfolio.positions:
            if portfolio.is_hodl_position(pos):
                continue
            s_name = pos.get("Sub_Strategy") or pos.get("sub_strategy")
            if s_name and s_name in positions_by_sub:
                positions_by_sub[s_name].append(pos)
            else:
                # Infer for legacy or untagged positions
                t_sym = str(pos.get("Ticker") or pos.get("ticker", "")).strip().upper()
                is_us = ("." not in t_sym)
                assigned = False
                for k, sc in cfg.sub_strategies.items():
                    u_filt = sc.get("universe_filter", "")
                    if is_us and "US" in u_filt:
                        positions_by_sub[k].append(pos)
                        assigned = True
                        break
                    elif not is_us and "Nordic" in u_filt:
                        if "MidCap" in u_filt and len(positions_by_sub[k]) < int(sc.get("slots", 3)):
                            positions_by_sub[k].append(pos)
                            assigned = True
                            break
                        elif "MicroCap" in u_filt:
                            positions_by_sub[k].append(pos)
                            assigned = True
                            break
                if not assigned and positions_by_sub:
                    first_k = next(iter(cfg.sub_strategies.keys()))
                    positions_by_sub[first_k].append(pos)

        # Iterate through each sub_strategy in order
        for sub_name, sub_cfg in cfg.sub_strategies.items():
            if len(portfolio.active_positions()) >= cfg.slots:
                break

            sub_slots = int(sub_cfg.get("slots", 1))
            current_held = len(positions_by_sub.get(sub_name, []))
            needed = sub_slots - current_held

            if needed <= 0:
                logger.info(f"[{cfg.portfolio_id}] Sub-strategy `{sub_name}` slots full ({current_held}/{sub_slots}). Skipping.")
                continue

            u_filter = str(sub_cfg.get("universe_filter", ""))
            slot_sizes = sub_cfg.get("slot_sizes") or [sub_cfg.get("slot_size") or (float(sub_cfg.get("allocated_capital", 1500)) / sub_slots)]

            # Reference target allocation for sizing and ADV constraint evaluation
            target_alloc_ref = float(slot_sizes[current_held] if current_held < len(slot_sizes) else slot_sizes[-1])
            if available_cash < (target_alloc_ref * 0.75):
                logger.info(
                    f"[{cfg.portfolio_id}] Available cash ({available_cash:.2f}€) < 75% of target slot ({target_alloc_ref:.2f}€) "
                    f"for `{sub_name}`. Skipping sub-strategy to prevent dwarf position."
                )
                continue

            logger.info(f"[{cfg.portfolio_id}] Screening for `{sub_name}` ({needed} slots open, filter: `{u_filter}`)...")

            eff_allocation_acc = min(target_alloc_ref, available_cash)

            # Step 1: Collect, validate, and score all eligible candidates for this sub-strategy
            valid_candidates = []

            for ticker, meta in self.market_engine.universe_metadata.items():
                if ticker in held_tickers:
                    continue

                # Anti-whipsaw cooldown
                if ticker in cooldown_tickers:
                    days_since_exit = (today - cooldown_tickers[ticker]).days
                    if days_since_exit < REENTRY_COOLDOWN_DAYS:
                        continue

                # Region pre-filter to bypass irrelevant stocks instantly
                m_reg = str(meta.get("market", "")).strip().upper()
                if "nordic" in u_filter.lower() and m_reg not in ("FI", "SE", "DK", "NO"):
                    continue
                if "us_" in u_filter.lower() and m_reg != "US":
                    continue

                # Market cap tier pre-filter to avoid expensive network calls for out-of-tier tickers
                if u_filter == "Nordic_MidCap_Compounder":
                    mcap_eur = get_market_cap_in_eur(meta, fx_rates=self.market_engine.fx_rates)
                    if mcap_eur > 0 and not (150_000_000.0 <= mcap_eur <= 2_000_000_000.0):
                        continue
                elif u_filter == "US_MicroCap_Growth":
                    mcap_usd = get_market_cap_in_usd(meta, fx_rates=self.market_engine.fx_rates)
                    if mcap_usd > 0 and not (30_000_000.0 <= mcap_usd <= 350_000_000.0):
                        continue
                elif u_filter == "Nordic_MicroCap_Catalyst":
                    mcap_eur = get_market_cap_in_eur(meta, fx_rates=self.market_engine.fx_rates)
                    if mcap_eur > 0 and not (15_000_000.0 <= mcap_eur < 150_000_000.0):
                        continue

                # NLP Safety Guard
                news_verdict, news_reason = self.market_engine.get_ticker_nlp_status(ticker)
                if news_verdict == "REJECT":
                    continue

                if hasattr(self.market_engine, "get_ticker_hard_facts"):
                    hard_facts = self.market_engine.get_ticker_hard_facts(ticker)
                else:
                    hard_facts = getattr(self.market_engine, "hard_facts_cache", {}).get(ticker, {})

                # Tiered Filter Validation
                passed, reasons = matches_universe_filter(
                    ticker=ticker,
                    filter_name=u_filter,
                    hard_facts=hard_facts,
                    meta=meta,
                    nlp_verdict=news_verdict,
                    fx_rates=self.market_engine.fx_rates,
                )

                if not passed:
                    continue

                # Fetch fresh live market price for the validated candidate
                cand_price = None
                if hasattr(self.market_engine, "_fetch_live_price"):
                    try:
                        live_px = self.market_engine._fetch_live_price(ticker)
                        if isinstance(live_px, (int, float)) and not isinstance(live_px, bool) and live_px > 0:
                            cand_price = float(live_px)
                    except Exception:
                        cand_price = None
                if cand_price is None:
                    cached_px = self.market_engine.prices_cache.get(ticker, meta.get("current_price", 0.0))
                    if isinstance(cached_px, (int, float)) and not isinstance(cached_px, bool) and cached_px > 0:
                        cand_price = float(cached_px)
                if not cand_price or cand_price <= 0:
                    continue
                self.market_engine.prices_cache[ticker] = cand_price

                # Exit criteria pre-validation (only check Layer 1/2 safety, as matches_universe_filter already validated sub-strategy fundamentals)
                exit_act, _ = evaluate_position(
                    position={"buy_price": cand_price, "ticker": ticker, "buy_date": today.strftime("%Y-%m-%d")},
                    current_price=cand_price,
                    latest_news_judgment=news_verdict,
                    latest_financials=None,
                    dead_money_days=None,
                )
                if exit_act == "SELL":
                    continue

                # Position sizing & liquidity checks
                ticker_curr = meta.get("currency", get_ticker_currency(ticker))
                fx_to_acc = get_fx_to_account(ticker_curr, portfolio.currency, self.market_engine.fx_rates)
                fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

                full_slot_local = target_alloc_ref * fx_acc_to_local
                target_allocation_local = eff_allocation_acc * fx_acc_to_local
                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

                # Max ADV allocation constraint
                adv_local = float(meta.get("adv_20d_local", 0.0) or 0.0)
                max_adv_allowed = adv_local * MAX_ADV_ALLOCATION_PCT if adv_local > 0 else 0.0

                # Bug 6 fix: Reject dwarf positions if ADV cap does not cover at least 75% of full target slot
                if max_adv_allowed <= 0 or max_adv_allowed < (full_slot_local * 0.75):
                    continue

                if target_allocation_local < (min_fee_local + (10.0 * fx_acc_to_local)):
                    continue

                investable_cash_local = min(target_allocation_local, max_adv_allowed) - min_fee_local
                shares_to_buy = math.floor(investable_cash_local / cand_price)
                if shares_to_buy <= 0:
                    continue

                # Step 1.3: Calculate candidate_score
                if "midcap" in u_filter.lower():
                    # Core (Nordic_MidCap_Compounder): (roe_pct * 1.5) + (ebit_margin_pct * 1.5) - (net_debt_to_ebitda * 5.0)
                    roe = hard_facts.get("roe") or hard_facts.get("return_on_equity") or 0.0
                    roe_pct = roe * 100.0 if abs(roe) <= 2.0 else float(roe)

                    ebit_margin = hard_facts.get("ebit_margin") or hard_facts.get("operating_margin") or 0.0
                    ebit_margin_pct = ebit_margin * 100.0 if abs(ebit_margin) <= 2.0 else float(ebit_margin)

                    net_debt = hard_facts.get("net_debt")
                    net_cash = hard_facts.get("net_cash")
                    if net_debt is None and net_cash is not None:
                        net_debt = -net_cash
                    nd_ebitda = hard_facts.get("net_debt_to_ebitda")
                    if nd_ebitda is None and net_debt is not None:
                        ebitda = hard_facts.get("ebitda") or 0.0
                        nd_ebitda = (net_debt / ebitda) if ebitda > 0 else (0.0 if net_debt <= 0 else 3.0)
                    nd_val = float(nd_ebitda) if nd_ebitda is not None else 0.0

                    cand_score = (roe_pct * 1.5) + (ebit_margin_pct * 1.5) - (nd_val * 5.0)

                elif "us_" in u_filter.lower():
                    # Satellite 1 (US_MicroCap_Growth): rev_yoy + (30.0 if is_profile_a) + (25.0 if nlp_verdict == "STRONG BUY")
                    rev_yoy = float(hard_facts.get("revenue_growth_yoy_pct") or hard_facts.get("revenue_yoy") or 0.0)
                    is_prof_a = bool(hard_facts.get("is_profile_a", False))
                    cand_score = float(rev_yoy or 0.0) + (30.0 if is_prof_a else 0.0) + (25.0 if news_verdict == "STRONG BUY" else 0.0)

                else:
                    # Satellite 2 (Nordic_MicroCap_Catalyst): (35.0 if is_profile_a) + (25.0 if is_profile_b) + (30.0 if nlp in STRONG BUY/WATCH_TURNAROUND) + rev_yoy * 0.5
                    rev_yoy = float(hard_facts.get("revenue_growth_yoy_pct") or hard_facts.get("revenue_yoy") or 0.0)
                    is_prof_a = bool(hard_facts.get("is_profile_a", False))
                    is_prof_b = bool(hard_facts.get("is_profile_b", False))
                    nlp_cat = (news_verdict in ["STRONG BUY", "WATCH_TURNAROUND"])
                    cand_score = (35.0 if is_prof_a else 0.0) + (25.0 if is_prof_b else 0.0) + (30.0 if nlp_cat else 0.0) + (float(rev_yoy or 0.0) * 0.5)

                rates = self.market_engine.fx_rates or {"EURUSD": 1.08, "EURSEK": 11.30, "EURNOK": 11.50, "EURDKK": 7.46}
                fx_to_eur = 1.0
                if ticker_curr == "SEK":
                    fx_to_eur = 1.0 / rates.get("EURSEK", 11.30)
                elif ticker_curr == "NOK":
                    fx_to_eur = 1.0 / rates.get("EURNOK", 11.50)
                elif ticker_curr == "DKK":
                    fx_to_eur = 1.0 / rates.get("EURDKK", 7.46)
                elif ticker_curr == "USD":
                    fx_to_eur = 1.0 / rates.get("EURUSD", 1.08)
                adv_eur = adv_local * fx_to_eur

                valid_candidates.append({
                    "ticker": ticker,
                    "score": cand_score,
                    "candidate_score": cand_score,
                    "adv_eur": adv_eur,
                    "meta": meta,
                    "hard_facts": hard_facts,
                    "cand_price": cand_price,
                    "ticker_curr": ticker_curr,
                    "fx_to_acc": fx_to_acc,
                    "fx_acc_to_local": fx_acc_to_local,
                    "adv_local": adv_local,
                    "max_adv_allowed": max_adv_allowed,
                })

            # Step 1.4: Sort valid_candidates descending by candidate_score (score) then adv_eur
            valid_candidates.sort(key=lambda x: (x["score"], x["adv_eur"]), reverse=True)

            # Step 1.5: Fill available slots using top-ranked candidates
            for slot_idx in range(current_held, sub_slots):
                if len(portfolio.active_positions()) >= cfg.slots:
                    break
                if available_cash < (MIN_BROKER_FEE + 10.0):
                    logger.info(f"[{cfg.portfolio_id}] Available cash exhausted ({available_cash:.2f}€). Halting buys.")
                    break
                if not valid_candidates:
                    logger.info(f"[{cfg.portfolio_id}] No eligible candidates found for slot {slot_idx+1} in `{sub_name}`.")
                    break

                target_alloc_acc = float(slot_sizes[slot_idx] if slot_idx < len(slot_sizes) else slot_sizes[-1])
                if available_cash < (target_alloc_acc * 0.75):
                    logger.info(
                        f"[{cfg.portfolio_id}] Remaining cash ({available_cash:.2f}€) < 75% of target slot {slot_idx+1} ({target_alloc_acc:.2f}€) "
                        f"in `{sub_name}`. Halting slot fills for `{sub_name}` to prevent dwarf position."
                    )
                    break

                eff_allocation_acc = min(target_alloc_acc, available_cash)

                cand = valid_candidates.pop(0)
                ticker = cand["ticker"]
                cand_price = cand["cand_price"]
                ticker_curr = cand["ticker_curr"]
                fx_to_acc = cand["fx_to_acc"]
                fx_acc_to_local = cand["fx_acc_to_local"]
                max_adv_allowed = cand["max_adv_allowed"]

                full_slot_local = target_alloc_acc * fx_acc_to_local
                target_allocation_local = eff_allocation_acc * fx_acc_to_local
                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

                if max_adv_allowed <= 0 or max_adv_allowed < (full_slot_local * 0.75):
                    continue

                if target_allocation_local > max_adv_allowed:
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

                if total_cost_acc > available_cash:
                    continue

                # Limit price calculation (Prev Close + 0.2%)
                limit_buf = getattr(cfg, "limit_buffer_pct", 0.002) or 0.002
                target_limit = round(cand_price * (1.0 + limit_buf), 4)

                portfolio.cash_balance -= total_cost_acc
                available_cash -= total_cost_acc

                new_pos = {
                    "Ticker": ticker,
                    "Buy Date": today.strftime("%Y-%m-%d"),
                    "Buy Price": cand_price,
                    "Shares": shares_to_buy,
                    "Capital Invested": round(total_cost_local, 2),
                    "Strategy": "Hybrid Core-Satellite",
                    "Sub_Strategy": sub_name,
                    "Universe_Filter": u_filter,
                    "Currency": ticker_curr,
                    "Peak Price": cand_price,
                    "Order Type": "LIMIT",
                    "Limit Price": target_limit,
                    "Time In Force": getattr(cfg, "time_in_force", "FOK_15MIN"),
                }
                portfolio.positions.append(new_pos)
                positions_by_sub[sub_name].append(new_pos)
                held_tickers.add(ticker)
                new_buys += 1

                logger.info(
                    f"🎯 [{cfg.portfolio_id} BUY] [{sub_name}] {ticker} (Filter: {u_filter}, Score: {cand['candidate_score']:.1f}) | "
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
                            "strategy": "Hybrid Core-Satellite",
                            "sub_strategy": sub_name,
                            "universe_filter": u_filter,
                            "candidate_score": round(cand["candidate_score"], 2),
                            "shares": shares_to_buy,
                            "buy_price": f"{cand_price:.2f} {ticker_curr}",
                            "total_cost": f"{total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} {portfolio.currency})",
                            "remaining_cash": f"{portfolio.cash_balance:,.2f} {portfolio.currency}",
                        },
                    })

        if new_buys > 0:
            portfolio.save_state()

        return new_buys

    def execute_phase2_screening(self, portfolio: PortfolioInstance, run_trades: Optional[List[Dict[str, Any]]] = None) -> int:
        """
        Phase 2: Screening & Sizing for a specific portfolio based on its parameters.
        Returns count of new positions opened.
        """
        cfg = portfolio.config
        open_count = len(portfolio.active_positions())
        if open_count >= cfg.slots:
            if open_count > cfg.slots:
                logger.info(
                    f"[{cfg.portfolio_id}] ⏳ Transition limit active: open positions ({open_count}) exceed new target slots ({cfg.slots}). "
                    f"Gracefully retaining existing {open_count} positions with active SELL stops; no new BUY orders permitted until count naturally drops below {cfg.slots}."
                )
            else:
                logger.info(f"[{cfg.portfolio_id}] Max slots full ({open_count}/{cfg.slots}). Skipping new buys.")
            return 0

        # Market Regime Cash Buffer (Task 3: Enforce max 60% investment / min 40% cash in CHOPPY/BEAR market)
        regime_data = getattr(self.market_engine, "market_regime", {}) or {}
        regime_str = str(regime_data.get("regime", "")).upper()
        last_px = float(regime_data.get("last_price", 0.0) or 0.0)
        sma_50 = float(regime_data.get("sma_50", 0.0) or 0.0)
        is_below_sma50 = (last_px > 0 and sma_50 > 0 and last_px < sma_50)
        is_choppy_or_bear = ("CHOPPY" in regime_str or "BEAR" in regime_str or is_below_sma50)

        min_required_cash = 0.0
        if is_choppy_or_bear:
            buffer_pct = getattr(cfg, "market_regime_cash_buffer_pct", 0.40) or 0.40
            min_required_cash = cfg.start_cash * buffer_pct  # e.g. 4,000 EUR for 10,000 EUR start cash
            if portfolio.cash_balance < min_required_cash:
                logger.info(
                    f"[{cfg.portfolio_id}] 🛡️ Market Regime Filter ACTIVE (Regime: {regime_str or 'CHOPPY'}, IWC < SMA50={is_below_sma50}). "
                    f"Current cash ({portfolio.cash_balance:,.2f} EUR) < required cash reserve ({min_required_cash:,.2f} EUR / {buffer_pct*100:.0f}%). "
                    f"Halting new BUY orders to enforce max {100-buffer_pct*100:.0f}% investment limit."
                )
                return 0

        target_allocation_acc = cfg.slot_size if (cfg.slot_size and cfg.slot_size > 0) else (cfg.start_cash / cfg.slots)
        available_cash = portfolio.cash_balance - min_required_cash
        if available_cash < (MIN_BROKER_FEE + 10.0):
            logger.info(f"[{cfg.portfolio_id}] Available investable cash above regime buffer depleted ({available_cash:.2f}€). Skipping new buys.")
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

        # Dedicated execution branch for composite portfolios with sub_strategies (e.g. P13_Live_Candidate)
        if cfg.sub_strategies:
            return self.execute_phase2_sub_strategies_screening(
                portfolio=portfolio,
                run_trades=run_trades,
                available_cash=available_cash,
                held_tickers=held_tickers,
                cooldown_tickers=cooldown_tickers,
                today=today,
            )

        # Dedicated execution branch for P11 Meta_Consensus strategy
        if cfg.strategy == "Meta_Consensus":
            try:
                df_conv = calculate_top_picks_conviction(portfolio.portfolio_dir)
            except Exception as e:
                logger.error(f"[{cfg.portfolio_id}] Failed to calculate conviction: {e}")
                df_conv = pd.DataFrame()

            if df_conv.empty:
                logger.info(f"[{cfg.portfolio_id}] No consensus conviction candidates available.")
                return 0

            min_score = cfg.min_conviction_score if cfg.min_conviction_score is not None else 8

            for _, row in df_conv.iterrows():
                if open_count + new_buys >= cfg.slots:
                    break

                ticker = str(row["Ticker"]).strip().upper()
                if ticker in held_tickers:
                    continue

                # Anti-Whipsaw Cooldown
                if ticker in cooldown_tickers:
                    days_since_exit = (today - cooldown_tickers[ticker]).days
                    if days_since_exit < REENTRY_COOLDOWN_DAYS:
                        continue

                # Conviction Score check (e.g. >= 8)
                score = int(row.get("Conviction Score (0-14)", 0))
                if score < min_score:
                    continue

                # Required tags check: Must have Institutional + (Deep Value OR Quality Growth)
                tags_str = str(row.get("Premium Tags (e.g., Institutional, Deep Value)", ""))
                tags_lower = tags_str.lower()
                has_inst = "institutional" in tags_lower
                has_dv = "deep value" in tags_lower
                has_qg = "quality growth" in tags_lower

                if not (has_inst and (has_dv or has_qg)):
                    continue

                if cfg.required_tags:
                    if not all(rt.lower() in tags_lower for rt in cfg.required_tags):
                        continue

                meta = self.market_engine.universe_metadata.get(ticker, {})

                # Region filter
                market = meta.get("market", "")
                if cfg.regions and market and market not in cfg.regions:
                    continue

                # ADV check
                adv_local = meta.get("adv_20d_local", 0.0)
                adv_usd = meta.get("adv_20d_usd", 0.0)
                if cfg.min_adv > 0 and (adv_usd > 0 or adv_local > 0):
                    if adv_usd < cfg.min_adv and adv_local < cfg.min_adv:
                        continue

                # -----------------------------------------------------------------
                # Global NLP Safety Guard: Block if active NLP status is REJECT
                # -----------------------------------------------------------------
                news_verdict, news_reason = self.market_engine.get_ticker_nlp_status(ticker)
                if news_verdict == "REJECT":
                    logger.warning(
                        f"🚨 [{cfg.portfolio_id} BUY BLOCKED] {ticker} has high conviction "
                        f"(Score {score}) but an active NLP REJECT status: {news_reason}"
                    )
                    continue

                cand_price = self.market_engine.prices_cache.get(ticker, meta.get("current_price", 0.0))
                if not cand_price or cand_price <= 0:
                    cand_price = self.market_engine._fetch_live_price(ticker)
                if not cand_price or cand_price <= 0:
                    continue

                hard_facts = self.market_engine.hard_facts_cache.get(ticker, {})
                financials_payload = {
                    "revenue_growth_yoy": hard_facts.get("revenue_growth_yoy_pct"),
                    "cash_runway_months": hard_facts.get("cash_runway_months"),
                    "operating_cash_flow": hard_facts.get("operating_cash_flow_ttm"),
                }
                exit_act, _ = evaluate_position(
                    position={"buy_price": cand_price, "ticker": ticker, "buy_date": today.strftime("%Y-%m-%d")},
                    current_price=cand_price,
                    latest_news_judgment=news_verdict,
                    latest_financials=financials_payload,
                    dead_money_days=cfg.dead_money_days,
                )
                if exit_act == "SELL":
                    continue

                # Position sizing (slot_size or start_cash/slots)
                eff_allocation_acc = min(target_allocation_acc, available_cash)
                ticker_curr = meta.get("currency", get_ticker_currency(ticker))
                fx_to_acc = get_fx_to_account(ticker_curr, portfolio.currency, self.market_engine.fx_rates)
                fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

                target_allocation_local = eff_allocation_acc * fx_acc_to_local
                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

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

                if total_cost_acc > available_cash:
                    continue

                portfolio.cash_balance -= total_cost_acc
                available_cash -= total_cost_acc
                limit_buf = getattr(cfg, "limit_buffer_pct", 0.002) or 0.002
                target_limit = round(cand_price * (1.0 + limit_buf), 4)
                new_pos = {
                    "Ticker": ticker,
                    "Buy Date": today.strftime("%Y-%m-%d"),
                    "Buy Price": cand_price,
                    "Shares": shares_to_buy,
                    "Capital Invested": round(total_cost_local, 2),
                    "Strategy": cfg.strategy,
                    "Currency": ticker_curr,
                    "Order Type": "LIMIT",
                    "Limit Price": target_limit,
                    "Time In Force": getattr(cfg, "time_in_force", "FOK_15MIN"),
                }
                portfolio.positions.append(new_pos)
                held_tickers.add(ticker)
                new_buys += 1

                logger.info(
                    f"⭐ [{cfg.portfolio_id} BUY] {ticker} ({cfg.strategy}, Score: {score}) | "
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
                            "conviction_score": score,
                            "shares": shares_to_buy,
                            "buy_price": f"{cand_price:.2f} {ticker_curr}",
                            "total_cost": f"{total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} {portfolio.currency})",
                            "remaining_cash": f"{portfolio.cash_balance:,.2f} {portfolio.currency}",
                        },
                    })

            if new_buys > 0:
                portfolio.save_state()

            return new_buys

        # Dedicated execution branch for P12 Momentum_Breakout strategy
        if cfg.strategy == "Momentum_Breakout":
            breakout_candidates = []
            for ticker, meta in self.market_engine.universe_metadata.items():
                if ticker in held_tickers:
                    continue

                # Region filter
                market = meta.get("market", "")
                if cfg.regions and market and market not in cfg.regions:
                    continue

                # Micro-cap ceiling guard (shared universe may include Nordic Mid-Caps up to 2,000M EUR for P13)
                mcap_usd = float(meta.get("market_cap_usd", 0.0) or 0.0)
                if mcap_usd > 350_000_000.0:
                    continue

                # Anti-whipsaw cooldown
                if ticker in cooldown_tickers:
                    days_since_exit = (today - cooldown_tickers[ticker]).days
                    if days_since_exit < REENTRY_COOLDOWN_DAYS:
                        continue

                # ADV baseline check (P12 enforces minimum 500,000 USD/EUR liquidity protection)
                adv_local = meta.get("adv_20d_local", 0.0)
                adv_usd = meta.get("adv_20d_usd", 0.0)
                min_adv_threshold = float(cfg.min_adv if cfg.min_adv is not None else 500000.0)
                adv_curr = adv_local if adv_local > 0 else adv_usd
                if adv_curr < min_adv_threshold:
                    continue

                # NLP clean guard: Must NOT have an active NLP REJECT
                if cfg.require_nlp_clean:
                    news_verdict, news_reason = self.market_engine.get_ticker_nlp_status(ticker)
                    if news_verdict == "REJECT":
                        continue
                    # Extra protection for momentum breakouts: reject reverse splits and dilutive consolidation warnings
                    split_patterns = ["reverse split", "omvänd split", "käänteinen split", "share consolidation", "sammanläggning"]
                    if any(sp in (news_reason or "").lower() for sp in split_patterns):
                        logger.info(f"[{cfg.portfolio_id}] 🛡️ Skipping {ticker}: Reverse split / consolidation news detected ({news_reason}).")
                        continue

                # Retrieve momentum metrics from cache or live query
                mom = self.market_engine.momentum_cache.get(ticker)
                if not mom:
                    cand_px = self.market_engine.prices_cache.get(ticker, 0.0)
                    if not cand_px or cand_px <= 0:
                        cand_px = self.market_engine._fetch_live_price(ticker)
                    mom = self.market_engine.momentum_cache.get(ticker, {})

                cand_price = mom.get("price") or self.market_engine.prices_cache.get(ticker, meta.get("current_price", 0.0))
                if not cand_price or cand_price <= 0:
                    continue

                day_change_pct = float(mom.get("day_change_pct", 0.0) or 0.0)
                day_volume_shares = float(mom.get("day_volume", 0.0) or 0.0)
                runup_3d_pct = float(mom.get("runup_3d_pct", 0.0) or 0.0)
                rsi_14 = float(mom.get("rsi_14", 50.0) or 50.0)

                # Sanity guard: reject unadjusted glitch jumps exceeding +300% on a single day
                if day_change_pct > 300.0:
                    logger.warning(f"[{cfg.portfolio_id}] ⚠️ Skipping {ticker}: Suspicious parabolic spike (+{day_change_pct:.1f}%), likely split glitch.")
                    continue

                # Task 1: Exhaustion Filter (max 3-day runup, default +40%)
                max_runup = cfg.max_3d_runup_pct * 100.0 if (cfg.max_3d_runup_pct and cfg.max_3d_runup_pct <= 1.0) else (cfg.max_3d_runup_pct or 40.0)
                if runup_3d_pct > max_runup:
                    logger.info(f"[{cfg.portfolio_id}] 🛑 Exhaustion filter: Skipping {ticker} (3-day runup +{runup_3d_pct:.1f}% > +{max_runup:.1f}%).")
                    continue

                # Task 1: Overbought Filter (max RSI14, default 80.0)
                if cfg.max_rsi_14 is not None and rsi_14 > cfg.max_rsi_14:
                    logger.info(f"[{cfg.portfolio_id}] 🛑 Overbought filter: Skipping {ticker} (RSI14 {rsi_14:.1f} > {cfg.max_rsi_14:.1f}).")
                    continue

                # Volume surge multiplier: current volume in local currency vs 20d ADV
                day_volume_curr = day_volume_shares * cand_price
                adv_curr = adv_local if adv_local > 0 else adv_usd
                vol_surge = (day_volume_curr / adv_curr) if adv_curr > 0 else 0.0

                # Check breakout criteria:
                # 1. Price change >= min_price_change_pct (default +2.0%)
                # 2. Volume surge >= volume_surge_multiplier (default 3.0x ADV)
                if day_change_pct >= cfg.min_price_change_pct and vol_surge >= cfg.volume_surge_multiplier:
                    breakout_candidates.append({
                        "ticker": ticker,
                        "cand_price": cand_price,
                        "day_change_pct": day_change_pct,
                        "runup_3d_pct": runup_3d_pct,
                        "rsi_14": rsi_14,
                        "vol_surge": vol_surge,
                        "meta": meta,
                        "mom": mom or {},
                        "score": vol_surge * day_change_pct,
                    })

            # Sort breakout candidates primarily by highest volume surge anomaly (tie-break: day_change_pct)
            breakout_candidates.sort(key=lambda x: (x["vol_surge"], x["day_change_pct"]), reverse=True)

            for cand in breakout_candidates:
                if open_count + new_buys >= cfg.slots:
                    break

                ticker = cand["ticker"]
                cand_price = cand["cand_price"]
                meta = cand["meta"]
                score = cand["score"]
                vol_surge = cand["vol_surge"]
                day_change_pct = cand["day_change_pct"]

                # Smart Routing & Limit Protection (Strict prohibition of Market orders)
                cand_mom = cand.get("mom") or self.market_engine.momentum_cache.get(ticker) or {}
                ref_px = cand_mom.get("vwap") if (cand_mom.get("vwap") and cand_mom.get("vwap") > 0) else (cand_mom.get("last_candle_close") or cand_price)
                limit_buf = getattr(cfg, "limit_buffer_pct", 0.002) or 0.002
                target_limit = round(ref_px * (1.0 + limit_buf), 4)

                if cand_price > target_limit:
                    logger.info(
                        f"🛑 [{cfg.portfolio_id}] Smart Routing Limit: Skipping {ticker} (Market ask {cand_price:.2f} > Limit {target_limit:.2f}, "
                        f"Ref {ref_px:.2f} +{limit_buf*100:.1f}%). Order killed (FOK 15min TTL) to prevent market impact slippage."
                    )
                    continue

                fill_price = min(cand_price, target_limit)

                eff_allocation_acc = min(target_allocation_acc, available_cash)
                ticker_curr = meta.get("currency", get_ticker_currency(ticker))
                fx_to_acc = get_fx_to_account(ticker_curr, portfolio.currency, self.market_engine.fx_rates)
                fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

                target_allocation_local = eff_allocation_acc * fx_acc_to_local
                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

                if target_allocation_local < (min_fee_local + (10.0 * fx_acc_to_local)):
                    continue

                investable_cash_local = target_allocation_local - min_fee_local
                if investable_cash_local <= 0:
                    continue

                shares_to_buy = math.floor(investable_cash_local / fill_price)
                if shares_to_buy <= 0:
                    continue

                actual_gross_buy = shares_to_buy * fill_price
                actual_fee = calculate_transaction_fee(actual_gross_buy, min_fee=min_fee_local)
                total_cost_local = actual_gross_buy + actual_fee
                total_cost_acc = total_cost_local * fx_to_acc

                if total_cost_acc > available_cash:
                    continue

                portfolio.cash_balance -= total_cost_acc
                available_cash -= total_cost_acc
                new_pos = {
                    "Ticker": ticker,
                    "Buy Date": today.strftime("%Y-%m-%d"),
                    "Buy Price": fill_price,
                    "Shares": shares_to_buy,
                    "Capital Invested": round(total_cost_local, 2),
                    "Strategy": cfg.strategy,
                    "Currency": ticker_curr,
                    "Peak Price": fill_price,
                    "Order Type": "LIMIT",
                    "Limit Price": target_limit,
                    "Time In Force": getattr(cfg, "time_in_force", "FOK_15MIN"),
                }
                portfolio.positions.append(new_pos)
                held_tickers.add(ticker)
                new_buys += 1

                logger.info(
                    f"🚀 [{cfg.portfolio_id} LIMIT BUY] {ticker} (Surge: {vol_surge:.1f}x ADV, +{day_change_pct:.1f}%) | "
                    f"Limit: {target_limit:.2f} | Fill: {fill_price:.2f} {ticker_curr} | "
                    f"Bought {shares_to_buy} shares | Cost: {total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} {portfolio.currency}) | "
                    f"Remaining Cash: {portfolio.cash_balance:,.2f} {portfolio.currency}"
                )

                if run_trades is not None:
                    run_trades.append({
                        "portfolio_id": cfg.portfolio_id,
                        "action": "BUY",
                        "ticker": ticker,
                        "details": {
                            "strategy": cfg.strategy,
                            "volume_surge": f"{vol_surge:.1f}x ADV",
                            "day_change": f"+{day_change_pct:.1f}%",
                            "shares": shares_to_buy,
                            "buy_price": f"{cand_price:.2f} {ticker_curr}",
                            "total_cost": f"{total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} {portfolio.currency})",
                            "remaining_cash": f"{portfolio.cash_balance:,.2f} {portfolio.currency}",
                        },
                    })

            if new_buys > 0:
                portfolio.save_state()

            return new_buys

        for ticker, meta in self.market_engine.universe_metadata.items():
            if open_count + new_buys >= cfg.slots:
                break

            if ticker in held_tickers:
                continue

            # Check geographic region filter
            market = meta.get("market", "")
            if cfg.regions and market not in cfg.regions:
                continue

            # Micro-cap ceiling guard (shared universe may include Nordic Mid-Caps up to 2,000M EUR for P13)
            mcap_usd = float(meta.get("market_cap_usd", 0.0) or 0.0)
            if mcap_usd > 350_000_000.0:
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
            hard_facts = self.market_engine.get_ticker_hard_facts(ticker) if hasattr(self.market_engine, "get_ticker_hard_facts") else getattr(self.market_engine, "hard_facts_cache", {}).get(ticker, {})
            eval_res = getattr(self.market_engine, "eval_profiles_cache", {}).get(ticker, {})

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
            cand_price = None
            if hasattr(self.market_engine, "_fetch_live_price"):
                try:
                    live_px = self.market_engine._fetch_live_price(ticker)
                    if isinstance(live_px, (int, float)) and not isinstance(live_px, bool) and live_px > 0:
                        cand_price = float(live_px)
                except Exception:
                    cand_price = None
            if cand_price is None:
                cached_px = self.market_engine.prices_cache.get(ticker, meta.get("current_price", 0.0))
                if isinstance(cached_px, (int, float)) and not isinstance(cached_px, bool) and cached_px > 0:
                    cand_price = float(cached_px)
            if not cand_price or cand_price <= 0:
                continue
            self.market_engine.prices_cache[ticker] = cand_price

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
            eff_allocation_acc = min(target_allocation_acc, available_cash)
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

            if total_cost_acc > available_cash:
                continue

            portfolio.cash_balance -= total_cost_acc
            available_cash -= total_cost_acc
            limit_buf = getattr(cfg, "limit_buffer_pct", 0.002) or 0.002
            target_limit = round(cand_price * (1.0 + limit_buf), 4)
            new_pos = {
                "Ticker": ticker,
                "Buy Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "Buy Price": cand_price,
                "Shares": shares_to_buy,
                "Capital Invested": round(total_cost_local, 2),
                "Strategy": cfg.strategy,
                "Currency": ticker_curr,
                "Order Type": "LIMIT",
                "Limit Price": target_limit,
                "Time In Force": getattr(cfg, "time_in_force", "FOK_15MIN"),
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
        """Calculates total market value of portfolio active (non-HODL) open positions converted to EUR."""
        total_eur = 0.0
        for p in portfolio.positions:
            if portfolio.is_hodl_position(p):
                continue
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
            p.reload_if_changed_on_disk()
            for pos in p.positions:
                all_held.add(pos["Ticker"])

        self.market_engine.refresh_data(held_tickers=all_held)

        results: Dict[str, Dict[str, Any]] = {}
        run_trades: List[Dict[str, Any]] = []

        # Step 1: Execute Base Portfolios (P1 through P10) first so their state files and active positions are fully updated
        base_portfolios = [p for p in self.portfolios if p.config.strategy != "Meta_Consensus"]
        meta_portfolios = [p for p in self.portfolios if p.config.strategy == "Meta_Consensus"]

        logger.info(f"Executing Phase A: Base Signal Portfolios ({len(base_portfolios)} portfolios: P1–P10)")
        for p in base_portfolios:
            p.reload_if_changed_on_disk()
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

        # Step 2: Execute Meta-Portfolios (P11_Meta_Consensus) based on fresh, updated consensus from P1-P10
        if meta_portfolios:
            logger.info(f"Executing Phase B: Meta-Portfolios ({len(meta_portfolios)} portfolios: P11_Meta_Consensus)")
        for p in meta_portfolios:
            p.reload_if_changed_on_disk()
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

        # Send dedicated P13 EOD Action List email for manual OST execution
        p13_inst = next((p for p in self.portfolios if p.config.portfolio_id == "P13_Live_Candidate"), None)
        if p13_inst:
            try:
                from email_generator import generate_and_send_p13_eod_action_email
                generate_and_send_p13_eod_action_email(
                    portfolio=p13_inst,
                    daemon=self,
                    run_trades=run_trades,
                )
            except Exception as e:
                logger.warning(f"Could not send P13 EOD action list email: {e}")

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

    def run_intraday_p12_tick(self) -> None:
        """DECOMMISSIONED: Intraday scanning is disabled. Retained for backward compatibility."""
        logger.info("ℹ️ Intraday P12 scanning is decommissioned. All analysis operates purely End-Of-Day (EOD).")

    def run_scheduler(
        self,
        daily_at: str = "23:05",
        interval_hours: Optional[float] = None,
        run_immediately: bool = True,
        intraday_p12_minutes: Optional[int] = None,
    ) -> None:
        """Runs the multi-portfolio engine on an autonomous schedule using Python's schedule library.

        Designed for 24/7 background deployment on Linux servers (e.g. systemd).
        - Executes daily post-market close sync at daily_at (Nordic + US markets close by 23:00).
        - Purely End-Of-Day (EOD) analysis. Intraday market-hours scanning is decommissioned.
        """
        if run_immediately:
            logger.info("🚀 [Scheduler] Suoritetaan välitön alkukierros käynnistyksen yhteydessä...")
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"Virhe alkukierroksessa: {e}")

        logger.info(f"⏰ [Scheduler] Ajastin aktivoitu: Päivittäinen iltasykli asetettu klo {daily_at} (järjestelmäaika)")
        schedule.every().day.at(daily_at).do(self.run_cycle)

        if interval_hours and interval_hours > 0:
            logger.info(f"⏰ [Scheduler] Lisätty jaksottainen ajo {interval_hours} tunnin välein")
            schedule.every(interval_hours).hours.do(self.run_cycle)

        logger.info("⏳ [Scheduler] Valmiustilassa. Odotetaan ajastettua ilta-ajoa (tarkistus 30s välein)...")
        while True:
            try:
                schedule.run_pending()
                time.sleep(30)
            except KeyboardInterrupt:
                logger.info("🛑 [Scheduler] Ajastin pysäytetty käyttäjän toimesta.")
                break
            except Exception as e:
                logger.error(f"❌ [Scheduler] Odottamaton virhe ajastinsilmukassa: {e}. Yritetään uudelleen 60s kuluttua...")
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
    parser.add_argument("--run-once", action="store_true", help="Execute a single daily cycle across all portfolios and exit")
    parser.add_argument("--schedule", action="store_true", help="Run with in-code scheduler at configured daily time (e.g. 23:05)")
    parser.add_argument("--daily-at", type=str, default="23:05", help="Daily execution time in HH:MM format (default: 23:05)")
    parser.add_argument("--no-initial-run", action="store_true", help="Skip the initial execution upon startup in schedule mode")
    parser.add_argument("--intraday-p12-minutes", type=int, default=30, help="Interval in minutes for intraday P12 scans during market hours (default: 30)")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a sleep loop")
    parser.add_argument("--interval-hours", type=float, default=24.0, help="Interval in hours for loop mode (default: 24)")
    parser.add_argument("--reset-portfolios", action="store_true", help="Reset all portfolios to 10,000 EUR starting capital")
    parser.add_argument("--portfolio", type=str, default=None, help="Optionally run or test a single portfolio by ID")
    parser.add_argument("--sync-universe", action="store_true", help="Force an immediate dynamic universe scan for new listings from Nordnet")
    parser.add_argument("--tax-profile", type=str, choices=["AOT", "OY", "OST"], default=None, help="Tax structure ('AOT' for taxable account, 'OY' for corporate account, 'OST' for Finnish Equity Savings Account)")
    parser.add_argument("--set-holding", nargs=3, metavar=("SYMBOL", "QTY", "AVG_PRICE"), help="Add, update, or remove (QTY=0) a stock holding in target portfolio (default: P13_Live_Candidate)")
    parser.add_argument("--hodl", action="store_true", help="Mark the holding set via --set-holding as an isolated HODL position (never sold by bot, does not consume trading slots)")
    parser.add_argument("--set-cash", type=float, metavar="EUR", help="Set current cash balance in EUR for target portfolio (default: P13_Live_Candidate)")
    parser.add_argument("--show-portfolio", action="store_true", help="Display current holdings and cash balance for target portfolio (default: P13_Live_Candidate)")

    args = parser.parse_args()

    daemon = MasterLiveTradingDaemon(tax_profile=args.tax_profile)

    if args.sync_universe:
        logger.info("🔄 [CLI] Forcing immediate dynamic universe scan from Nordnet...")
        added_cnt, added_tickers = daemon.market_engine.maybe_update_universe(force=True)
        logger.info(f"Scan complete. Added {added_cnt} new stocks: {added_tickers}")

    if args.portfolio:
        selected = [p for p in daemon.portfolios if p.config.portfolio_id.lower() == args.portfolio.lower()]
        if not selected:
            logger.error(f"Portfolio '{args.portfolio}' not found in configuration.")
            return
        daemon.portfolios = selected

    # Handle manual portfolio state management flags (--set-holding, --set-cash, --show-portfolio)
    cli_state_action = False
    target_pid = args.portfolio or "P13_Live_Candidate"
    target_p = next((p for p in daemon.portfolios if p.config.portfolio_id.lower() == target_pid.lower()), None)

    if args.set_cash is not None:
        if not target_p:
            logger.error(f"Target portfolio '{target_pid}' not found.")
            return
        target_p.set_cash(args.set_cash)
        print(f"✅ [{target_p.config.portfolio_id}] Cash balance updated to {target_p.cash_balance:,.2f} {target_p.currency} ({target_p.state_file.name})")
        cli_state_action = True

    if args.set_holding is not None:
        if not target_p:
            logger.error(f"Target portfolio '{target_pid}' not found.")
            return
        sym, qty_str, price_str = args.set_holding
        try:
            qty = int(float(qty_str))
            price = float(price_str)
            pos = target_p.set_holding(sym, qty, price, hodl=bool(args.hodl))
            if pos is None:
                print(f"🗑️ [{target_p.config.portfolio_id}] Holding {sym.upper()} removed ({target_p.state_file.name})")
            else:
                hodl_tag = " [🔒 HODL=True - Isolated from bot exits & trading slots]" if pos.get("hodl") else ""
                print(
                    f"✅ [{target_p.config.portfolio_id}] Holding {pos['Ticker']} updated: "
                    f"{pos['Shares']} pcs @ {pos['Buy Price']:.4f} {pos['Currency']} "
                    f"(Invested: {pos['Capital Invested']:,.2f} {pos['Currency']}){hodl_tag}"
                )
        except ValueError as e:
            logger.error(f"Invalid argument format for --set-holding: {e}")
            return
        cli_state_action = True

    if args.show_portfolio:
        if not target_p:
            logger.error(f"Target portfolio '{target_pid}' not found.")
            return
        print(f"\n=== {target_p.config.portfolio_id} ({target_p.state_file.name}) ===")
        print(f"Cash Balance:     {target_p.cash_balance:,.2f} {target_p.currency}")
        print(f"Active Slots:     {len(target_p.active_positions())} / {target_p.config.slots}")
        print("Positions:")
        if not target_p.positions:
            print("  (empty)")
        for p in target_p.positions:
            h_str = " [🔒 HODL]" if target_p.is_hodl_position(p) else f" [{p.get('Sub_Strategy') or p.get('Strategy', '')}]"
            print(
                f"  - {p.get('Ticker')}: {int(float(p.get('Shares', 0)))} pcs @ {float(p.get('Buy Price', 0.0)):.4f} {p.get('Currency', 'EUR')} "
                f"(Invested: {float(p.get('Capital Invested', 0.0)):,.2f} {p.get('Currency', 'EUR')}){h_str}"
            )
        print("=" * 60 + "\n")
        cli_state_action = True

    if cli_state_action and not (args.run_once or args.schedule or args.loop or args.reset_portfolios):
        return

    if args.reset_portfolios:
        for p in daemon.portfolios:
            p.reset()
        logger.info(f"Portfolios reset successfully ({len(daemon.portfolios)} portfolio(s)).")

    if args.schedule:
        daemon.run_scheduler(
            daily_at=args.daily_at,
            interval_hours=args.interval_hours if args.loop else None,
            run_immediately=not args.no_initial_run,
            intraday_p12_minutes=args.intraday_p12_minutes,
        )
    elif args.loop:
        daemon.run_loop(interval_hours=args.interval_hours)
    else:
        daemon.run_cycle()


if __name__ == "__main__":
    main()
