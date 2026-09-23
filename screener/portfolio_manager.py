"""
portfolio_manager.py - Paper Trading Portfolio Manager & Tri-Layer Fundamental Exit Engine.

Consumes STRONG BUY signals, sizes positions with fixed allocation (5,000 per trade
out of 100,000 virtual equity), and manages open positions using a Tri-Layer Exit Strategy:
  - Layer 1: Catastrophic Failsafe (Daily): current_price <= buy_price * 0.50 -> CATASTROPHIC_STOP
  - Layer 2: LLM News Radar (Event-Driven): latest_news_judgment == "REJECT" -> LLM_NEWS_REJECT
  - Layer 3: Fundamental Deterioration (Quarterly): revenue_growth_yoy < 0 OR (cash_runway < 12 and OCF < 0) -> FUNDAMENTAL_DETERIORATION
  - Default: HOLD
"""

from __future__ import annotations

import csv
import logging
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

# Ensure project root is in sys.path
_current_dir = Path(__file__).resolve().parent
BASE_DIR = _current_dir.parent if _current_dir.name == "screener" else _current_dir
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from screener.price_fetcher import get_realtime_data, normalize_ticker

logger = logging.getLogger("screener.portfolio_manager")

DEFAULT_OPEN_POSITIONS_CSV = BASE_DIR / "data" / "open_positions.csv"
DEFAULT_TRADE_HISTORY_CSV = BASE_DIR / "data" / "trade_history.csv"

STARTING_VIRTUAL_EQUITY = 100_000.0  # 100,000 Base Currency
POSITION_ALLOCATION = 5_000.0        # Fixed 5,000 allocation per trade (5% of equity)
CATASTROPHIC_STOP_PCT = 0.50         # Layer 1: -50% catastrophic failsafe (2.5% max portfolio risk)
TRAILING_STOP_PCT = 0.20             # Kept for backwards compatibility
DEFAULT_ATR_PERIOD = 14              # Kept for backwards compatibility
DEFAULT_ATR_MULTIPLIER = 2.5         # Kept for backwards compatibility


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculates Average True Range (ATR) using Daily High, Low, and Close prices.
    Kept for backwards compatibility.
    """
    high = df["High"]
    low = df["Low"]
    close_prev = df["Close"].shift(1)

    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean()
    return atr


def evaluate_position(
    position: Dict[str, Any],
    current_price: float,
    latest_news_judgment: Optional[str] = None,
    latest_financials: Optional[Dict[str, Any]] = None,
    current_date: Optional[Union[datetime, date, str]] = None,
    dead_money_days: int = 180,
) -> Tuple[str, str]:
    """
    Evaluates an open position using the Tri-Layer Fundamental Exit Strategy + Dead Money Timer.

    Parameters:
        position: Dict containing at least 'buy_price' and 'ticker' (plus optional 'buy_date').
        current_price: Current market price of the asset.
        latest_news_judgment: Event-driven LLM verdict from press releases ("REJECT", "HOLD", "STRONG BUY", etc.).
        latest_financials: Dict from deterministic financial engine containing:
            - 'revenue_growth_yoy': float (percentage or decimal YoY growth)
            - 'cash_runway_months': float (estimated runway in months)
            - 'operating_cash_flow': float (TTM or quarterly OCF)
        current_date: Optional date/datetime/string for evaluation time (defaults to current UTC date).
        dead_money_days: Opportunity cost timer threshold in days (e.g. 90, 180, 365).

    Returns:
        (action, reason):
            action: 'SELL' or 'HOLD'
            reason: Specific trigger reason string:
                - 'CATASTROPHIC_STOP': Daily drop >= 50% from buy price
                - 'LLM_NEWS_REJECT': Fatal news detected (e.g. kontrollbalansräkning, toxic dilution)
                - 'LLM_NEWS_WARN_CONFIRMED': Warning news confirmed by price breakdown
                - 'TIME_STOP_DEAD_MONEY': Position held > dead_money_days with <= +5% gain (opportunity cost release)
                - 'FUNDAMENTAL_DETERIORATION': Revenue contraction or imminent cash crisis
                - 'HOLD': No exit condition met
    """
    buy_price = float(position.get("buy_price", 0.0))
    if buy_price <= 0:
        logger.warning(f"Invalid buy_price {buy_price} for position {position.get('ticker')}. Defaulting to HOLD.")
        return "HOLD", "HOLD"

    # Layer 1: Catastrophic Failsafe (Daily Check)
    catastrophic_threshold = buy_price * CATASTROPHIC_STOP_PCT
    if current_price <= catastrophic_threshold:
        logger.warning(
            f"🚨 [LAYER 1 EXIT] {position.get('ticker')}: current_price {current_price:.4f} <= "
            f"catastrophic threshold {catastrophic_threshold:.4f} (-50% failsafe)."
        )
        return "SELL", "CATASTROPHIC_STOP"

    # Layer 2: LLM News Radar (Event-Driven Check)
    if latest_news_judgment:
        clean_news = str(latest_news_judgment).strip().upper()
        if clean_news == "REJECT":
            logger.warning(
                f"🚨 [LAYER 2 EXIT] {position.get('ticker')}: Fatal news detected by LLM radar ({clean_news})."
            )
            return "SELL", "LLM_NEWS_REJECT"
        elif clean_news == "WARN":
            # Dual Confirmation: require price breakdown below 10% from entry or 5% below peak
            peak_price = float(position.get("highest_price_seen", buy_price) or buy_price)
            if current_price <= buy_price * 0.90 or (peak_price > buy_price and current_price <= peak_price * 0.95):
                logger.warning(
                    f"🚨 [LAYER 2 EXIT - DUAL CONFIRMATION] {position.get('ticker')}: Price breakdown "
                    f"({current_price:.2f}) confirmed structural warning ({clean_news})."
                )
                return "SELL", "LLM_NEWS_WARN_CONFIRMED"

    # Time-Based Exit: Dead Money Timer (Held > dead_money_days and gain <= +5%)
    raw_buy_date = position.get("buy_date") or position.get("entry_date") or position.get("entrydate")
    if raw_buy_date:
        try:
            if isinstance(raw_buy_date, datetime):
                buy_dt = raw_buy_date.date()
            elif isinstance(raw_buy_date, date):
                buy_dt = raw_buy_date
            elif isinstance(raw_buy_date, str):
                clean_bd_str = raw_buy_date.split("T")[0].strip()
                buy_dt = datetime.strptime(clean_bd_str, "%Y-%m-%d").date()
            else:
                buy_dt = None

            if current_date is None:
                curr_dt = datetime.now(timezone.utc).date()
            elif isinstance(current_date, datetime):
                curr_dt = current_date.date()
            elif isinstance(current_date, date):
                curr_dt = current_date
            elif isinstance(current_date, str):
                clean_cd_str = current_date.split("T")[0].strip()
                curr_dt = datetime.strptime(clean_cd_str, "%Y-%m-%d").date()
            else:
                curr_dt = datetime.now(timezone.utc).date()

            if buy_dt and curr_dt:
                holding_days = (curr_dt - buy_dt).days
                dead_money_threshold = buy_price * 1.05
                if holding_days > dead_money_days and current_price <= dead_money_threshold:
                    logger.warning(
                        f"⏰ [TIME STOP - DEAD MONEY] {position.get('ticker')}: Held {holding_days} days (>{dead_money_days}) "
                        f"with current price {current_price:.4f} <= threshold {dead_money_threshold:.4f} "
                        f"(<= +5% gain). Releasing capital for new opportunities."
                    )
                    return "SELL", "TIME_STOP_DEAD_MONEY"
        except Exception as e:
            logger.warning(f"Could not calculate holding days for {position.get('ticker')}: {e}")

    # Layer 3: Fundamental Deterioration (Quarterly Check)
    if latest_financials and isinstance(latest_financials, dict):
        rev_growth = latest_financials.get("revenue_growth_yoy")
        cash_runway = latest_financials.get("cash_runway_months")
        ocf = latest_financials.get("operating_cash_flow")

        # 3A: Anti-Shrinking Rule: Revenue contraction YoY indicates thesis invalidation
        if rev_growth is not None:
            try:
                if float(rev_growth) < 0:
                    logger.warning(
                        f"🚨 [LAYER 3 EXIT] {position.get('ticker')}: Revenue contraction "
                        f"(YoY: {float(rev_growth):.2f}% < 0). Anti-shrinking rule violated."
                    )
                    return "SELL", "FUNDAMENTAL_DETERIORATION"
            except (ValueError, TypeError):
                pass

        # 3B: Impending Cash Crisis: Less than 12 months runway while burning operational cash
        if cash_runway is not None and ocf is not None:
            try:
                runway_val = float(cash_runway) if str(cash_runway).lower() != "infinite" else 999.0
                ocf_val = float(ocf)
                if runway_val < 12.0 and ocf_val < 0:
                    logger.warning(
                        f"🚨 [LAYER 3 EXIT] {position.get('ticker')}: Cash crisis imminent "
                        f"(runway: {runway_val:.1f} mo < 12, OCF: {ocf_val:,.0f} < 0). High dilution risk."
                    )
                    return "SELL", "FUNDAMENTAL_DETERIORATION"
            except (ValueError, TypeError):
                pass

    # Default State
    return "HOLD", "HOLD"


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


class PortfolioManager:
    """Manages paper trading state, position sizing, and Tri-Layer Fundamental Exits via CSV."""

    def __init__(
        self,
        open_positions_path: Optional[Path | str] = None,
        trade_history_path: Optional[Path | str] = None,
        starting_equity: float = STARTING_VIRTUAL_EQUITY,
        position_allocation: float = POSITION_ALLOCATION,
        catastrophic_stop_pct: float = CATASTROPHIC_STOP_PCT,
        trailing_stop_pct: float = TRAILING_STOP_PCT,
        atr_multiplier: float = DEFAULT_ATR_MULTIPLIER,
        atr_period: int = DEFAULT_ATR_PERIOD,
    ):
        self.open_positions_path = Path(open_positions_path or DEFAULT_OPEN_POSITIONS_CSV)
        self.trade_history_path = Path(trade_history_path or DEFAULT_TRADE_HISTORY_CSV)
        self.starting_equity = float(starting_equity)
        self.position_allocation = float(position_allocation)
        self.catastrophic_stop_pct = float(catastrophic_stop_pct)
        self.trailing_stop_pct = float(trailing_stop_pct)
        self.atr_multiplier = float(atr_multiplier)
        self.atr_period = int(atr_period)

        # Ensure parent directories and CSV headers exist
        self.open_positions_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_history_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_csvs()

    def _init_csvs(self) -> None:
        """Initializes open positions and trade history CSV files with standard headers if missing."""
        if not self.open_positions_path.exists() or self.open_positions_path.stat().st_size == 0:
            with open(self.open_positions_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "ticker", "market", "buy_date", "buy_price", "current_price",
                    "highest_price_seen", "catastrophic_stop", "shares", "currency", "last_evaluated_date"
                ])

        if not self.trade_history_path.exists() or self.trade_history_path.stat().st_size == 0:
            with open(self.trade_history_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "ticker", "market", "buy_date", "sell_date", "buy_price",
                    "sell_price", "pnl_pct", "pnl_absolute", "exit_reason"
                ])

    def load_open_positions(self) -> List[Dict[str, Any]]:
        """Loads all active open positions from open_positions.csv with robust recovery."""
        if not self.open_positions_path.exists():
            return []
        positions = []
        try:
            with open(self.open_positions_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for raw_row in reader:
                    # Clean and normalize keys to lowercase
                    row = {str(k).strip().lower(): str(v).strip() for k, v in raw_row.items() if k}
                    ticker = (row.get("ticker") or "").strip().upper()
                    if not ticker:
                        continue

                    buy_p = float(
                        row.get("buy_price")
                        or row.get("buyprice")
                        or row.get("buy price")
                        or row.get("entry_price")
                        or row.get("entryprice")
                        or row.get("entry price")
                        or 0.0
                    )
                    curr_p = float(row.get("current_price") or row.get("currentprice") or 0.0)
                    shs = float(row.get("shares", 0.0) or 0.0)

                    # Auto-heal: if buy_p was missing or 0.0, recover from KNOWN_ENTRY_PRICES, capital invested, or current_price
                    if buy_p <= 0.0:
                        if ticker in KNOWN_ENTRY_PRICES:
                            buy_p = KNOWN_ENTRY_PRICES[ticker]
                        else:
                            cap_inv = float(row.get("positionvalue") or row.get("position_value") or row.get("capital_invested") or row.get("capital invested") or 0.0)
                            if cap_inv > 0 and shs > 0:
                                buy_p = round(cap_inv / shs, 4)
                            elif curr_p > 0:
                                buy_p = curr_p

                    if curr_p <= 0.0:
                        curr_p = buy_p

                    high_p = float(row.get("highest_price_seen") or row.get("highestprice") or max(curr_p, buy_p) or 0.0)
                    stop_p = float(row.get("catastrophic_stop") or row.get("trailing_stop") or (buy_p * 0.50))

                    # Suffix-aware currency & market detection
                    curr = row.get("currency", "").strip().upper()
                    mkt = row.get("market", "").strip().upper()
                    if not curr or curr == "USD":
                        if ticker.endswith(".HE"):
                            curr = "EUR"
                            mkt = "FI"
                        elif ticker.endswith(".ST"):
                            curr = "SEK"
                            mkt = "SE"
                        elif ticker.endswith(".OL"):
                            curr = "NOK"
                            mkt = "NO"
                        elif ticker.endswith(".CO"):
                            curr = "DKK"
                            mkt = "DK"
                        else:
                            curr = "USD"
                            mkt = "US"

                    b_date = row.get("buy_date") or row.get("entry_date") or row.get("entrydate") or row.get("date") or "2026-09-18"

                    positions.append({
                        "ticker": ticker,
                        "market": mkt,
                        "buy_date": b_date,
                        "buy_price": round(buy_p, 4),
                        "current_price": round(curr_p, 4),
                        "highest_price_seen": round(high_p, 4),
                        "catastrophic_stop": round(stop_p, 4),
                        "trailing_stop": round(stop_p, 4),  # backwards compat
                        "shares": round(shs, 4),
                        "currency": curr,
                        "last_evaluated_date": row.get("last_evaluated_date", ""),
                        "atr_14": float(row.get("atr_14", 0.0) or 0.0),
                    })
        except Exception as e:
            logger.error(f"Error reading {self.open_positions_path}: {e}")
        return positions

    def save_open_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Saves active open positions list back to open_positions.csv, guaranteeing buy_price > 0."""
        fieldnames = [
            "ticker", "market", "buy_date", "buy_price", "current_price",
            "highest_price_seen", "catastrophic_stop", "shares", "currency", "last_evaluated_date"
        ]
        try:
            with open(self.open_positions_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for pos in positions:
                    bp = float(pos.get("buy_price", 0.0) or 0.0)
                    cp = float(pos.get("current_price", 0.0) or 0.0)
                    t_sym = pos["ticker"]
                    if bp <= 0.0:
                        bp = KNOWN_ENTRY_PRICES.get(t_sym, cp)
                    writer.writerow({
                        "ticker": t_sym,
                        "market": pos["market"],
                        "buy_date": pos["buy_date"],
                        "buy_price": round(bp, 4),
                        "current_price": round(cp, 4),
                        "highest_price_seen": round(pos.get("highest_price_seen", max(bp, cp)), 4),
                        "catastrophic_stop": round(pos.get("catastrophic_stop", bp * 0.50), 4),
                        "shares": pos["shares"],
                        "currency": pos["currency"],
                        "last_evaluated_date": pos.get("last_evaluated_date", ""),
                    })
        except Exception as e:
            logger.error(f"Error saving open positions to {self.open_positions_path}: {e}")

    def log_completed_trade(
        self,
        ticker: str,
        market: str,
        buy_date: str,
        sell_date: str,
        buy_price: float,
        sell_price: float,
        pnl_pct: float,
        pnl_absolute: float,
        exit_reason: str = "Trailing Stop-Loss",
    ) -> None:
        """Appends a closed trade to trade_history.csv."""
        try:
            with open(self.trade_history_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    ticker,
                    market,
                    buy_date,
                    sell_date,
                    round(buy_price, 4),
                    round(sell_price, 4),
                    round(pnl_pct, 2),
                    round(pnl_absolute, 2),
                    exit_reason,
                ])
            logger.info(
                f"📜 Logged closed trade for {ticker}: Buy={buy_price:.2f}, Sell={sell_price:.2f}, "
                f"PnL={pnl_absolute:+.2f} ({pnl_pct:+.2f}%), Reason={exit_reason}"
            )
        except Exception as e:
            logger.error(f"Failed to log trade to {self.trade_history_path}: {e}")

    def execute_trade_signal(
        self,
        ticker: str,
        market: Optional[str] = None,
        verdict: str = "STRONG BUY",
        allocated_capital: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Consumes a trade signal. If verdict is STRONG BUY, calculates position sizing,
        fetches real-time price, and records to open_positions.csv.
        """
        # Validate verdict
        clean_verdict = str(verdict).strip().upper()
        if not ("STRONG BUY" in clean_verdict or "BUY" in clean_verdict):
            logger.info(f"Signal for {ticker} is '{verdict}' (not STRONG BUY). Ignoring trade signal.")
            return None

        clean_ticker = normalize_ticker(ticker, market)
        base_ticker = clean_ticker.split(".")[0].upper()

        # Check existing positions (do not average up/down)
        open_pos = self.load_open_positions()
        existing_tickers = {p["ticker"].upper() for p in open_pos}
        existing_bases = {p["ticker"].split(".")[0].upper() for p in open_pos}

        if clean_ticker in existing_tickers or base_ticker in existing_bases:
            logger.warning(f"Ticker '{clean_ticker}' is already open in portfolio. Aborting duplicate buy.")
            return None

        # Fetch real-time price data
        price_data = get_realtime_data(clean_ticker, market)
        if price_data.get("status") != "SUCCESS" or not price_data.get("current_price"):
            logger.error(f"Cannot execute trade for {clean_ticker}: Price retrieval failed ({price_data.get('error')})")
            return None

        buy_price = float(price_data["current_price"])
        currency = price_data.get("currency", "USD" if "." not in clean_ticker else "EUR")
        detected_market = price_data.get("market") or market or ("US" if "." not in clean_ticker else clean_ticker.split(".")[-1])

        # Position Sizing: 5,000 allocation per position (5% of equity)
        alloc = allocated_capital or self.position_allocation
        shares = round(alloc / buy_price, 4)
        buy_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Layer 1: Catastrophic Failsafe Stop (-50%)
        catastrophic_stop = round(buy_price * self.catastrophic_stop_pct, 4)

        # Calculate 14-day ATR for informational recording
        atr_14 = buy_price * 0.08
        try:
            t = yf.Ticker(clean_ticker)
            hist = t.history(period="3mo", auto_adjust=True)
            if not hist.empty and len(hist) >= self.atr_period:
                atr_s = calculate_atr(hist, period=self.atr_period).dropna()
                if not atr_s.empty and atr_s.iloc[-1] > 0:
                    atr_14 = float(atr_s.iloc[-1])
        except Exception as e:
            logger.warning(f"Could not calculate ATR for {clean_ticker}: {e}")

        new_position = {
            "ticker": clean_ticker,
            "market": detected_market.upper(),
            "buy_date": buy_date,
            "buy_price": round(buy_price, 4),
            "current_price": round(buy_price, 4),
            "highest_price_seen": round(buy_price, 4),
            "catastrophic_stop": catastrophic_stop,
            "trailing_stop": catastrophic_stop,
            "shares": shares,
            "currency": currency.upper(),
            "last_evaluated_date": buy_date,
            "atr_14": round(atr_14, 4),
        }

        open_pos.append(new_position)
        self.save_open_positions(open_pos)

        logger.info(
            f"🚀 [POSITION OPENED] {clean_ticker} ({detected_market}) | Price: {buy_price:.2f} {currency} "
            f"| Shares: {shares} (Alloc: {alloc:.0f}) | Catastrophic Failsafe (-50%): {catastrophic_stop:.2f}"
        )
        return new_position

    def update_portfolio(
        self,
        news_judgments: Optional[Dict[str, str]] = None,
        financials_map: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates active open positions using the Tri-Layer Fundamental Exit Strategy:
          - Layer 1: Daily Catastrophic Failsafe (current_price <= buy_price * 0.50)
          - Layer 2: LLM News Radar (latest_news_judgment == 'REJECT')
          - Layer 3: Fundamental Deterioration (rev_yoy < 0 OR (runway < 12 and OCF < 0))
          - Default: HOLD
        """
        open_pos = self.load_open_positions()
        if not open_pos:
            logger.info("Portfolio empty. No active open positions to update.")
            return {"open_positions_count": 0, "closed_positions_count": 0, "updated_positions_count": 0}

        retained_positions: List[Dict[str, Any]] = []
        closed_count = 0
        updated_count = 0
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        logger.info(f"🔄 Evaluating portfolio ({len(open_pos)} open positions) with Tri-Layer Exit Strategy...")

        for pos in open_pos:
            ticker = pos["ticker"]
            market = pos.get("market", "US")
            buy_price = pos["buy_price"]
            prev_highest = pos.get("highest_price_seen", buy_price)
            shares = pos["shares"]

            # 1. Fetch fresh real-time spot price
            price_data = get_realtime_data(ticker, market)
            if price_data.get("status") != "SUCCESS" or not price_data.get("current_price"):
                logger.warning(f"Failed to fetch price for {ticker}: {price_data.get('error')}. Retaining current state.")
                retained_positions.append(pos)
                continue

            latest_price = float(price_data["current_price"])
            pos["current_price"] = round(latest_price, 4)
            pos["highest_price_seen"] = round(max(prev_highest, latest_price), 4)
            pos["last_evaluated_date"] = today_str

            # 2. Extract news judgment and financials for this ticker if available
            news_verdict = (news_judgments or {}).get(ticker)
            ticker_financials = (financials_map or {}).get(ticker)

            # 3. Evaluate position with Tri-Layer Engine
            action, reason = evaluate_position(
                position=pos,
                current_price=latest_price,
                latest_news_judgment=news_verdict,
                latest_financials=ticker_financials,
            )

            if action == "SELL":
                sell_price = latest_price
                pnl_absolute = round((sell_price - buy_price) * shares, 2)
                pnl_pct = round(((sell_price - buy_price) / buy_price) * 100.0, 2) if buy_price > 0 else 0.0

                logger.warning(
                    f"🛑 [TRI-LAYER EXIT TRIGGERED] {ticker} | Action: SELL | Reason: {reason} | "
                    f"Price: {sell_price:.2f} (Buy: {buy_price:.2f}) | PnL: {pnl_absolute:+.2f} ({pnl_pct:+.2f}%)"
                )

                self.log_completed_trade(
                    ticker=ticker,
                    market=market,
                    buy_date=pos["buy_date"],
                    sell_date=today_str,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    pnl_pct=pnl_pct,
                    pnl_absolute=pnl_absolute,
                    exit_reason=reason,
                )
                closed_count += 1
            else:
                unrealized_pnl = round(((latest_price - buy_price) / buy_price) * 100.0, 2) if buy_price > 0 else 0.0
                logger.info(
                    f"🛡️ [HOLD] {ticker} | Price: {latest_price:.2f} (Buy: {buy_price:.2f}) | "
                    f"Peak: {pos['highest_price_seen']:.2f} | Unrealized: {unrealized_pnl:+.2f}%"
                )
                retained_positions.append(pos)
                updated_count += 1

        self.save_open_positions(retained_positions)
        return {
            "open_positions_count": len(retained_positions),
            "closed_positions_count": closed_count,
            "updated_positions_count": updated_count,
        }

    # Aliases for backwards compatibility with dashboard & CLI
    def open_position(
        self,
        ticker: str,
        market: str = "US",
        current_price: Optional[float] = None,
        currency: Optional[str] = None,
        allocated_capital: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.execute_trade_signal(
            ticker=ticker, market=market, verdict="STRONG BUY", allocated_capital=allocated_capital
        )

    def update_trailing_stops(self) -> Dict[str, Any]:
        return self.update_portfolio()


# Module-level singletons and helpers
_default_manager = PortfolioManager()


def execute_trade_signal(
    ticker: str,
    market: Optional[str] = None,
    verdict: str = "STRONG BUY",
    allocated_capital: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    return _default_manager.execute_trade_signal(
        ticker=ticker, market=market, verdict=verdict, allocated_capital=allocated_capital
    )


def update_portfolio(
    news_judgments: Optional[Dict[str, str]] = None,
    financials_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return _default_manager.update_portfolio(
        news_judgments=news_judgments, financials_map=financials_map
    )


def open_position(
    ticker: str,
    market: str = "US",
    current_price: Optional[float] = None,
    currency: Optional[str] = None,
    allocated_capital: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    return _default_manager.open_position(
        ticker=ticker, market=market, current_price=current_price, currency=currency, allocated_capital=allocated_capital
    )


def update_trailing_stops() -> Dict[str, Any]:
    return _default_manager.update_trailing_stops()
