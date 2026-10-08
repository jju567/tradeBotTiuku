"""
intraday_p12_scanner.py - Standalone Intraday Scanner for P12_Momentum_Breakout.

Separates the execution of the momentum breakout strategy from the nightly batch process.
Runs strictly during live market hours to eliminate overnight gap slippage.
Can be triggered via cron every 30-60 minutes on Linux production or executed manually.

Key Features:
1. Market Hours Verification (Europe/Helsinki timezone):
   - Nordic (.HE, .ST, .CO): 10:00 - 18:30 (Mon-Fri)
   - US (no suffix): 16:30 - 23:00 (Mon-Fri)
   - Skips regions when their respective exchanges are closed.
2. Real-Time Intraday Data Fetching:
   - Uses yfinance live quotes (last_price, intraday volume, previous close).
   - Computes Today_Volume_Live and Volume Surge Multiplier vs ADV_20d.
3. Live Breakout Trigger:
   - Triggers BUY when: Today_Volume >= 3.0 * ADV_20d AND Intraday_Return >= +2.0%.
   - Sanity checks: ADV_20d >= 50,000, unadjusted jump <= +300%, price >= 0.10.
   - NLP Safety Guard: checks nlp_decisions_archive.csv / news for REJECT and reverse splits.
   - Executes BUY at the current real-time price, updating P12 state immediately.
4. Intraday Trailing Stop Check:
   - Dynamically tracks highest Peak Price since entry.
   - Executes immediate SELL if price drops >= 6% below peak.
   - Enforces 21-day holding time stop.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import logging
import math
import sys
import time
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from dateutil.tz import gettz as ZoneInfo  # type: ignore

import pandas as pd
import yfinance as yf
import yaml

# Cross-platform terminal encoding fix
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Paths
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORTFOLIOS_YAML = BASE_DIR / "portfolios_config.yaml"
DEFAULT_PORTFOLIOS_DIR = BASE_DIR / "data" / "portfolios"
DEFAULT_CLEAN_UNIVERSE_CSV = BASE_DIR / "data" / "clean_microcap_universe.csv"
DEFAULT_NLP_ARCHIVE_CSV = BASE_DIR / "data" / "nlp_decisions_archive.csv"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [intraday_p12]: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("intraday_p12")

# Constants
MIN_BROKER_FEE = 9.00           # Minimum commission (Nordnet Tier 3 / US)
VARIABLE_BROKER_FEE_PCT = 0.002 # 0.20% variable broker commission
REENTRY_COOLDOWN_DAYS = 30      # 30-day anti-whipsaw cooldown after selling

try:
    from email_notifier import send_portfolio_alert
except ImportError:
    def send_portfolio_alert(*args, **kwargs) -> bool:
        return False


def get_helsinki_time() -> datetime:
    """Returns current datetime in Europe/Helsinki timezone."""
    try:
        tz = ZoneInfo("Europe/Helsinki")
        return datetime.now(tz)
    except Exception:
        # Fallback to UTC+3 (EEST)
        return datetime.now(timezone.utc).astimezone()


def is_nordic_market_open(dt_helsinki: datetime) -> bool:
    """Nordic markets (OMX Helsinki, Stockholm, Copenhagen): Mon-Fri 10:00 - 18:30."""
    if dt_helsinki.weekday() >= 5:  # Saturday or Sunday
        return False
    t = dt_helsinki.time()
    return dtime(10, 0) <= t <= dtime(18, 30)


def is_us_market_open(dt_helsinki: datetime) -> bool:
    """US markets (NYSE, NASDAQ): Mon-Fri 16:30 - 23:00 Helsinki time."""
    if dt_helsinki.weekday() >= 5:  # Saturday or Sunday
        return False
    t = dt_helsinki.time()
    return dtime(16, 30) <= t <= dtime(23, 0)


def get_ticker_region(ticker: str) -> str:
    """Classifies ticker into NORDIC or US based on ticker suffix."""
    t = ticker.strip().upper()
    if t.endswith(".HE") or t.endswith(".ST") or t.endswith(".CO"):
        return "NORDIC"
    return "US"


def is_market_open_for_ticker(ticker: str, dt_helsinki: datetime, force_open: bool = False) -> bool:
    """Determines whether the trading venue for the given ticker is currently open."""
    if force_open:
        return True
    region = get_ticker_region(ticker)
    if region == "NORDIC":
        return is_nordic_market_open(dt_helsinki)
    return is_us_market_open(dt_helsinki)


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


def get_fx_to_account(ticker_currency: str, account_currency: str = "EUR", fx_rates: Optional[Dict[str, float]] = None) -> float:
    """Returns multiplier to convert an amount in ticker_currency to account_currency."""
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


def calculate_transaction_fee(gross_amount: float, rate: float = VARIABLE_BROKER_FEE_PCT, min_fee: float = MIN_BROKER_FEE) -> float:
    """Calculates broker fee with flat floor."""
    return max(min_fee, gross_amount * rate)


class IntradayP12Scanner:
    """Standalone quantitative engine for P12_Momentum_Breakout intraday operations."""

    def __init__(
        self,
        portfolios_dir: Path = DEFAULT_PORTFOLIOS_DIR,
        config_path: Path = DEFAULT_PORTFOLIOS_YAML,
        universe_path: Path = DEFAULT_CLEAN_UNIVERSE_CSV,
        nlp_archive_path: Path = DEFAULT_NLP_ARCHIVE_CSV,
        force_open: bool = False,
        check_only: bool = False,
    ):
        self.portfolios_dir = Path(portfolios_dir)
        self.config_path = Path(config_path)
        self.universe_path = Path(universe_path)
        self.nlp_archive_path = Path(nlp_archive_path)
        self.force_open = force_open
        self.check_only = check_only

        self.portfolio_id = "P12_Momentum_Breakout"
        self.state_file = self.portfolios_dir / f"portfolio_{self.portfolio_id}_state.json"
        self.history_file = self.portfolios_dir / f"portfolio_{self.portfolio_id}_history.csv"
        self.equity_file = self.portfolios_dir / f"portfolio_{self.portfolio_id}_history.json"

        self.fx_rates = get_live_fx_rates()
        self.config = self._load_strategy_config()
        self.state = self._load_portfolio_state()

    def _load_strategy_config(self) -> Dict[str, Any]:
        """Loads strategy parameters for P12 from portfolios_config.yaml."""
        defaults = {
            "slots": 5,
            "slot_size": 2000.0,
            "start_cash": 10000.0,
            "volume_surge_multiplier": 3.0,
            "min_price_change_pct": 2.0,
            "min_adv_20d": 50000.0,
            "trailing_stop_pct": 0.06,
            "max_holding_days": 21,
            "require_nlp_clean": True,
        }
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                p_cfg = data.get("portfolios", {}).get(self.portfolio_id, {})
                for k, v in p_cfg.items():
                    if k in defaults and v is not None:
                        defaults[k] = v
            except Exception as e:
                logger.warning(f"Error loading {self.config_path}, using defaults: {e}")
        return defaults

    def _load_portfolio_state(self) -> Dict[str, Any]:
        """Loads P12 state or initializes if missing."""
        self.portfolios_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error reading {self.state_file}: {e}")

        initial_state = {
            "portfolio_id": self.portfolio_id,
            "starting_balance": float(self.config.get("start_cash", 10000.0)),
            "cash_balance": float(self.config.get("start_cash", 10000.0)),
            "currency": "EUR",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "positions": [],
        }
        self._save_portfolio_state(initial_state)
        return initial_state

    def _save_portfolio_state(self, state: Dict[str, Any]) -> None:
        """Saves updated state JSON."""
        if self.check_only:
            logger.info("🔍 [Check-Only Mode] Skipping write to state file.")
            return
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state to {self.state_file}: {e}")

    def _append_trade_history(self, trade: Dict[str, Any]) -> None:
        """Appends closed trade to portfolio history CSV."""
        if self.check_only:
            return
        file_exists = self.history_file.exists()
        headers = [
            "Ticker", "Buy Date", "Sell Date", "Buy Price", "Sell Price",
            "Shares", "Capital Invested", "Gross Sale Value", "Transaction Fee",
            "Net Return", "Net PnL", "Exit Reason"
        ]
        try:
            with open(self.history_file, "a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                if not file_exists or self.history_file.stat().st_size == 0:
                    writer.writerow(headers)
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
        except Exception as e:
            logger.error(f"Error appending trade history: {e}")

    def _load_recent_exits(self) -> Dict[str, date]:
        """Loads exit dates from trade history for anti-whipsaw cooldown."""
        cooldowns: Dict[str, date] = {}
        if not self.history_file.exists():
            return cooldowns
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ticker = (row.get("Ticker") or "").strip().upper()
                    sell_date_str = (row.get("Sell Date") or "").strip()
                    if ticker and sell_date_str:
                        try:
                            s_date = datetime.strptime(sell_date_str[:10], "%Y-%m-%d").date()
                            if ticker not in cooldowns or s_date > cooldowns[ticker]:
                                cooldowns[ticker] = s_date
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"Could not load recent exits: {e}")
        return cooldowns

    def _check_nlp_rejection(self, ticker: str) -> Tuple[bool, str]:
        """
        Checks if ticker has an active REJECT verdict in nlp_decisions_archive.csv or recent news.
        Also blocks reverse splits and share consolidations.
        """
        clean_t = ticker.strip().upper()
        if self.nlp_archive_path.exists():
            try:
                with open(self.nlp_archive_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reversed(list(reader)):
                        t = (row.get("Ticker") or "").strip().upper()
                        if t == clean_t:
                            dec = (row.get("LLM_Decision") or "").strip().upper()
                            reason = (row.get("Reasoning") or "").strip()
                            if dec == "REJECT":
                                return True, f"NLP REJECT: {reason}"
                            split_patterns = ["reverse split", "omvänd split", "käänteinen split", "share consolidation", "sammanläggning"]
                            if any(sp in reason.lower() for sp in split_patterns):
                                return True, f"Reverse split / consolidation warning: {reason}"
            except Exception as e:
                logger.debug(f"Error reading NLP archive: {e}")
        return False, "CLEAN"

    def fetch_live_intraday_data(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Fetches live intraday price, volume traded so far, and previous close using yfinance.
        """
        try:
            t = yf.Ticker(ticker)
            cur_price: Optional[float] = None
            day_volume: float = 0.0
            prev_close: Optional[float] = None

            info = getattr(t, "fast_info", None)
            if info and hasattr(info, "last_price") and info.last_price:
                cur_price = float(info.last_price)
            if info and hasattr(info, "last_volume") and info.last_volume:
                day_volume = float(info.last_volume)
            if info and hasattr(info, "previous_close") and info.previous_close:
                prev_close = float(info.previous_close)

            # Retrieve candle history for volume and split verification
            hist = t.history(period="5d")
            if not hist.empty:
                valid = hist["Close"].dropna()
                if not valid.empty:
                    if not cur_price:
                        cur_price = float(valid.iloc[-1])
                    if day_volume <= 0 and "Volume" in hist:
                        day_volume = float(hist["Volume"].iloc[-1])
                    if not prev_close and len(valid) >= 2:
                        prev_close = float(valid.iloc[-2])

                    # Unadjusted split / reverse split sanity check
                    if prev_close and prev_close > 0 and (cur_price / prev_close > 2.0 or cur_price / prev_close < 0.5):
                        try:
                            sp = t.splits
                            if not sp.empty:
                                last_sp_date = pd.Timestamp(sp.index[-1]).tz_convert(None).floor("D") if getattr(sp.index[-1], "tz", None) else pd.Timestamp(sp.index[-1]).floor("D")
                                today_dt = pd.Timestamp.now().floor("D")
                                if (today_dt - last_sp_date).days <= 7:
                                    factor = float(sp.iloc[-1])
                                    if factor > 0 and factor != 1.0:
                                        prev_close = prev_close / factor
                        except Exception:
                            pass

            if not cur_price or cur_price <= 0:
                return None

            prev = prev_close if (prev_close and prev_close > 0) else cur_price
            intraday_return_pct = round(((cur_price - prev) / prev) * 100.0, 2) if prev > 0 else 0.0

            return {
                "price": cur_price,
                "day_volume": day_volume,
                "prev_close": prev,
                "day_change_pct": intraday_return_pct,
            }
        except Exception as e:
            logger.debug(f"Failed to fetch live intraday data for {ticker}: {e}")
            return None

    def execute_trailing_stops(self, now_helsinki: datetime) -> int:
        """
        Evaluates current holdings for trailing stops and holding time stops.
        Executes immediate SELL if price drops >= 6% below highest peak since entry.
        """
        positions = self.state.get("positions", [])
        if not positions:
            logger.info("ℹ️ No open positions in P12.")
            return 0

        logger.info(f"🔍 [Trailing Stop Check] Checking {len(positions)} open positions...")
        retained_positions: List[Dict[str, Any]] = []
        closed_count = 0
        today_date = now_helsinki.date()

        for pos in positions:
            ticker = pos.get("Ticker", "")
            buy_price = float(pos.get("Buy Price", 0.0))
            shares = int(pos.get("Shares", 0))
            capital_invested = float(pos.get("Capital Invested", shares * buy_price))
            buy_date_str = str(pos.get("Buy Date", ""))[:10]
            cur_peak = float(pos.get("Peak Price", buy_price))
            ticker_curr = pos.get("Currency", get_ticker_currency(ticker))

            # 1. Check if market for this ticker is open
            if not is_market_open_for_ticker(ticker, now_helsinki, force_open=self.force_open):
                region = get_ticker_region(ticker)
                logger.info(f"⏸️ [Market Closed] {ticker} ({region}) exchange is currently closed. Holding position.")
                retained_positions.append(pos)
                continue

            # 2. Fetch live intraday price
            quote = self.fetch_live_intraday_data(ticker)
            if not quote or quote["price"] <= 0:
                logger.warning(f"⚠️ Could not obtain live quote for held ticker {ticker}. Retaining position.")
                retained_positions.append(pos)
                continue

            current_price = quote["price"]

            # 3. Dynamic Peak Price Update
            if current_price > cur_peak:
                logger.info(f"📈 [{ticker}] New Highest Peak reached: {cur_peak:.2f} -> {current_price:.2f} {ticker_curr}")
                cur_peak = current_price
                pos["Peak Price"] = cur_peak

            # 4. Trailing Stop & Holding Time Logic
            drawdown_from_peak = (cur_peak - current_price) / cur_peak if cur_peak > 0 else 0.0
            try:
                b_date = datetime.strptime(buy_date_str, "%Y-%m-%d").date()
                holding_days = (today_date - b_date).days
            except Exception:
                holding_days = 0

            action = "HOLD"
            reason = ""

            trailing_stop_threshold = float(self.config.get("trailing_stop_pct", 0.06))
            max_days = int(self.config.get("max_holding_days", 21))

            if drawdown_from_peak >= trailing_stop_threshold:
                action = "SELL"
                reason = f"TRAILING_STOP (Peak {cur_peak:.2f} -> {current_price:.2f}, -{drawdown_from_peak*100:.1f}%)"
            elif holding_days >= max_days:
                action = "SELL"
                reason = f"MOMENTUM_STALL ({holding_days}d >= {max_days}d)"

            if action == "SELL":
                gross_sale_value = shares * current_price
                fx_to_acc = get_fx_to_account(ticker_curr, self.state.get("currency", "EUR"), self.fx_rates)
                fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local
                transaction_fee = calculate_transaction_fee(gross_sale_value, min_fee=min_fee_local)
                net_return = gross_sale_value - transaction_fee
                net_pnl = net_return - capital_invested
                net_return_acc = net_return * fx_to_acc

                self.state["cash_balance"] += net_return_acc

                trade_record = {
                    "Ticker": ticker,
                    "Buy Date": buy_date_str,
                    "Sell Date": now_helsinki.strftime("%Y-%m-%d"),
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
                self._append_trade_history(trade_record)
                closed_count += 1

                logger.info(
                    f"🚨 [P12 SELL TRIGGERED] {ticker} | Reason: {reason} | "
                    f"Shares: {shares} @ {current_price:.2f} {ticker_curr} | "
                    f"Net PnL: {net_pnl:+.2f} {ticker_curr} ({net_pnl * fx_to_acc:+.2f} EUR)"
                )
                if not self.check_only:
                    try:
                        send_portfolio_alert(
                            portfolio_id=self.portfolio_id,
                            event_type="SELL",
                            ticker=ticker,
                            details={
                                "action": "SELL",
                                "shares": shares,
                                "price": f"{current_price:.2f} {ticker_curr}",
                                "pnl": f"{net_pnl:+.2f} {ticker_curr}",
                                "reason": reason,
                            },
                        )
                    except Exception:
                        pass
            else:
                retained_positions.append(pos)

        self.state["positions"] = retained_positions
        if closed_count > 0:
            self._save_portfolio_state(self.state)
            logger.info(f"✅ Executed {closed_count} trailing stop sales. Current cash: {self.state['cash_balance']:,.2f} EUR")
        else:
            self._save_portfolio_state(self.state)  # Saves any updated Peak Prices
            logger.info("🛡️ All active positions safely within trailing stop tolerance.")

        return closed_count

    def scan_and_execute_breakouts(self, now_helsinki: datetime) -> int:
        """
        Scans universe for active momentum breakouts and executes immediate BUYs into open slots.
        """
        cfg_slots = int(self.config.get("slots", 5))
        current_open_count = len(self.state.get("positions", []))
        available_slots = cfg_slots - current_open_count

        if available_slots <= 0:
            logger.info(f"ℹ️ P12 slots full ({current_open_count}/{cfg_slots}). Skipping breakout scanner.")
            return 0

        cash_balance = float(self.state.get("cash_balance", 0.0))
        if cash_balance < 100.0:
            logger.info(f"ℹ️ Insufficient cash ({cash_balance:.2f} EUR) for new allocations. Skipping breakout scanner.")
            return 0

        if not self.universe_path.exists():
            logger.error(f"Universe file {self.universe_path} not found.")
            return 0

        # Load Universe metadata
        universe_rows: List[Dict[str, Any]] = []
        try:
            with open(self.universe_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    t = (r.get("ticker") or r.get("Ticker") or "").strip().upper()
                    if t:
                        universe_rows.append(r)
        except Exception as e:
            logger.error(f"Error reading universe {self.universe_path}: {e}")
            return 0

        held_tickers = {p["Ticker"].strip().upper() for p in self.state.get("positions", [])}
        cooldown_tickers = self._load_recent_exits()
        today_date = now_helsinki.date()

        # Filter universe to open markets
        eligible_candidates: List[Dict[str, Any]] = []
        for r in universe_rows:
            ticker = (r.get("ticker") or r.get("Ticker") or "").strip().upper()
            if ticker in held_tickers:
                continue

            # Anti-whipsaw cooldown
            if ticker in cooldown_tickers:
                days_since_exit = (today_date - cooldown_tickers[ticker]).days
                if days_since_exit < REENTRY_COOLDOWN_DAYS:
                    continue

            # Market open check
            if not is_market_open_for_ticker(ticker, now_helsinki, force_open=self.force_open):
                continue

            adv_local = float(r.get("adv_20d_local", 0.0) or 0.0)
            adv_usd = float(r.get("adv_20d_usd", 0.0) or 0.0)
            adv_curr = adv_local if adv_local > 0 else adv_usd
            min_adv = float(self.config.get("min_adv_20d", 50000.0))
            if adv_curr < min_adv:
                continue

            # NLP Guard
            if self.config.get("require_nlp_clean", True):
                is_rejected, nlp_reason = self._check_nlp_rejection(ticker)
                if is_rejected:
                    logger.debug(f"Skipping {ticker} due to NLP guard: {nlp_reason}")
                    continue

            eligible_candidates.append({
                "ticker": ticker,
                "adv_curr": adv_curr,
                "currency": r.get("currency"),
                "market": r.get("market"),
            })

        logger.info(f"🔎 Scanning {len(eligible_candidates)} eligible tickers across active open markets...")

        breakout_candidates: List[Dict[str, Any]] = []
        vol_surge_threshold = float(self.config.get("volume_surge_multiplier", 3.0))
        min_return_pct = float(self.config.get("min_price_change_pct", 2.0))

        # Parallelize quote fetching with ThreadPoolExecutor
        tickers_to_query = [c["ticker"] for c in eligible_candidates]
        quotes_map: Dict[str, Optional[Dict[str, Any]]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            fetched_quotes = list(executor.map(self.fetch_live_intraday_data, tickers_to_query))
            for t_sym, q in zip(tickers_to_query, fetched_quotes):
                quotes_map[t_sym] = q

        for cand in eligible_candidates:
            ticker = cand["ticker"]
            quote = quotes_map.get(ticker)
            if not quote:
                continue

            cand_price = quote["price"]
            day_volume = quote["day_volume"]
            day_change_pct = quote["day_change_pct"]

            if cand_price < 0.10 or day_change_pct > 300.0:
                continue

            # Today's turnover in currency vs 20-day ADV
            day_volume_curr = day_volume * cand_price
            vol_surge = (day_volume_curr / cand["adv_curr"]) if cand["adv_curr"] > 0 else 0.0

            if day_change_pct >= min_return_pct and vol_surge >= vol_surge_threshold:
                logger.info(
                    f"🔥 [BREAKOUT DETECTED] {ticker} | Volume Surge: {vol_surge:.1f}x ADV | "
                    f"Day Return: +{day_change_pct:.1f}% | Price: {cand_price:.2f}"
                )
                breakout_candidates.append({
                    "ticker": ticker,
                    "price": cand_price,
                    "vol_surge": vol_surge,
                    "day_change_pct": day_change_pct,
                    "currency": cand["currency"],
                    "score": vol_surge * day_change_pct,
                })

        # Sort primarily by Volume Surge Multiplier
        breakout_candidates.sort(key=lambda x: (x["vol_surge"], x["day_change_pct"]), reverse=True)

        new_buys = 0
        target_allocation_acc = float(self.config.get("slot_size", 2000.0))

        for b_cand in breakout_candidates:
            if current_open_count + new_buys >= cfg_slots:
                break

            ticker = b_cand["ticker"]
            cand_price = b_cand["price"]
            vol_surge = b_cand["vol_surge"]
            day_change_pct = b_cand["day_change_pct"]

            eff_allocation_acc = min(target_allocation_acc, self.state["cash_balance"])
            ticker_curr = get_ticker_currency(ticker, b_cand.get("currency"))
            fx_to_acc = get_fx_to_account(ticker_curr, self.state.get("currency", "EUR"), self.fx_rates)
            fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

            target_allocation_local = eff_allocation_acc * fx_acc_to_local
            min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

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

            if total_cost_acc > self.state["cash_balance"]:
                continue

            self.state["cash_balance"] -= total_cost_acc
            new_pos = {
                "Ticker": ticker,
                "Buy Date": now_helsinki.strftime("%Y-%m-%d"),
                "Buy Price": cand_price,
                "Shares": shares_to_buy,
                "Capital Invested": round(total_cost_local, 2),
                "Strategy": "Momentum_Breakout",
                "Currency": ticker_curr,
                "Peak Price": cand_price,
            }
            self.state["positions"].append(new_pos)
            new_buys += 1

            logger.info(
                f"🚀 [P12 BUY EXECUTED] {ticker} (Surge: {vol_surge:.1f}x ADV, +{day_change_pct:.1f}%) | "
                f"Bought {shares_to_buy} shares @ {cand_price:.2f} {ticker_curr} | "
                f"Cost: {total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} EUR) | "
                f"Remaining Cash: {self.state['cash_balance']:,.2f} EUR"
            )
            if not self.check_only:
                try:
                    send_portfolio_alert(
                        portfolio_id=self.portfolio_id,
                        event_type="BUY",
                        ticker=ticker,
                        details={
                            "action": "BUY",
                            "volume_surge": f"{vol_surge:.1f}x ADV",
                            "day_change": f"+{day_change_pct:.1f}%",
                            "shares": shares_to_buy,
                            "buy_price": f"{cand_price:.2f} {ticker_curr}",
                            "total_cost": f"{total_cost_acc:,.2f} EUR",
                        },
                    )
                except Exception:
                    pass

        if new_buys > 0:
            self._save_portfolio_state(self.state)
            logger.info(f"✅ Successfully executed {new_buys} intraday breakout buys.")

        return new_buys

    def record_equity_snapshot(self) -> None:
        """Calculates current total equity and appends snapshot to history JSON."""
        if self.check_only:
            return
        total_stock_value_acc = 0.0
        for pos in self.state.get("positions", []):
            ticker = pos.get("Ticker", "")
            shares = int(pos.get("Shares", 0))
            ticker_curr = pos.get("Currency", get_ticker_currency(ticker))
            peak_or_buy = float(pos.get("Peak Price") or pos.get("Buy Price", 0.0))
            fx = get_fx_to_account(ticker_curr, self.state.get("currency", "EUR"), self.fx_rates)
            total_stock_value_acc += (shares * peak_or_buy) * fx

        cash = float(self.state.get("cash_balance", 0.0))
        total_equity = round(cash + total_stock_value_acc, 2)
        start_bal = float(self.state.get("starting_balance", 10000.0))
        total_return = round(total_equity - start_bal, 2)
        return_pct = round((total_return / start_bal) * 100.0, 2) if start_bal > 0 else 0.0

        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_equity": total_equity,
            "cash_balance": round(cash, 2),
            "total_stock_value": round(total_stock_value_acc, 2),
            "total_return": total_return,
            "total_return_pct": return_pct,
        }

        try:
            history: List[Dict[str, Any]] = []
            if self.equity_file.exists():
                with open(self.equity_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            history.append(snapshot)
            with open(self.equity_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not record equity snapshot: {e}")

    def run(self) -> Tuple[int, int]:
        """Main execution flow for an intraday scanning tick."""
        now_h = get_helsinki_time()
        time_str = now_h.strftime("%Y-%m-%d %H:%M:%S %Z")
        nordic_open = is_nordic_market_open(now_h) or self.force_open
        us_open = is_us_market_open(now_h) or self.force_open

        logger.info("=" * 70)
        logger.info(f"🕒 [Intraday P12 Scanner Tick] Time (Helsinki): {time_str}")
        logger.info(f"🏛️ Market Status: Nordic (10:00-18:30)={'OPEN' if nordic_open else 'CLOSED'} | US (16:30-23:00)={'OPEN' if us_open else 'CLOSED'}")

        if not nordic_open and not us_open:
            logger.info("🛑 Both Nordic and US markets are currently closed. Exiting cleanly.")
            logger.info("=" * 70)
            return 0, 0

        # Step 1: Trailing stop execution
        sells = self.execute_trailing_stops(now_h)

        # Step 2: Live breakout screening and buying
        buys = self.scan_and_execute_breakouts(now_h)

        # Step 3: Equity snapshot
        self.record_equity_snapshot()

        logger.info(f"🏁 [Tick Complete] Sells: {sells} | Buys: {buys} | Active Positions: {len(self.state.get('positions', []))}/5")
        logger.info("=" * 70)
        return sells, buys


def main() -> None:
    parser = argparse.ArgumentParser(description="tradeBotTiuku Standalone Intraday P12 Momentum Breakout Scanner")
    parser.add_argument("--check-only", action="store_true", help="Run in read-only simulation mode (no actual state changes)")
    parser.add_argument("--force-market-open", action="store_true", help="Bypass market hours check for off-hours testing")
    parser.add_argument("--portfolios-dir", type=str, default=str(DEFAULT_PORTFOLIOS_DIR), help="Path to portfolios directory")
    parser.add_argument("--universe-path", type=str, default=str(DEFAULT_CLEAN_UNIVERSE_CSV), help="Path to clean microcap universe CSV")
    parser.add_argument("--config-path", type=str, default=str(DEFAULT_PORTFOLIOS_YAML), help="Path to portfolios_config.yaml")

    args = parser.parse_args()

    scanner = IntradayP12Scanner(
        portfolios_dir=Path(args.portfolios_dir),
        config_path=Path(args.config_path),
        universe_path=Path(args.universe_path),
        force_open=args.force_market_open,
        check_only=args.check_only,
    )
    scanner.run()


if __name__ == "__main__":
    main()
