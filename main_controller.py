"""
main_controller.py - Central Live Daemon for Forward-Testing & Paper Portfolio Management.

Orchestrates realistic live forward-testing (paper trading) for tradeBotTiuku:
1. Realistic State Management:
   - Initial virtual balance of exactly STARTING_BALANCE = 10,000.0 USD/EUR.
   - Active holdings tracked in data/open_positions.csv (Ticker, Buy Date, Buy Price, Shares, Capital Invested).
   - Closed trades archived in data/trade_history.csv with realistic broker fee mechanics.
2. Phase 1: Portfolio Management (Tri-Layer Fundamental Exit & Real Costs):
   - Layer 1: Catastrophic Failsafe (Daily): current_price <= buy_price * 0.50 -> CATASTROPHIC_STOP
   - Layer 2: LLM News Radar (Event-Driven via nlp_analyzer.py): latest_news_judgment == "REJECT" -> LLM_NEWS_REJECT
   - Layer 3: Fundamental Deterioration (Quarterly via financial_metrics_engine.py): rev_growth_yoy < 0 OR (runway < 12 and OCF < 0) -> FUNDAMENTAL_DETERIORATION
   - SELL Execution:
     * Gross Sale Value = Shares * Current Price
     * Transaction Fee = max(Gross Sale Value * 0.002, 9.00)  (0.20% variable or 9.00 flat minimum fee)
     * Net Return (Proceeds) = Gross Sale Value - Transaction Fee
     * Updates cash balance, removes position, archives trade.
3. Phase 2: Market Scanning (Profile B Only & Small Account Sizing):
   - Ingests data/clean_microcap_universe.csv (106 liquid micro-caps).
   - Evaluates deterministic financial metrics (financial_metrics_engine.py).
   - Profile A is strictly disabled based on N=240 empirical backtest findings.
   - Accepts only verified PROFILE_B (Deep Value & Anti-Shrinking) signals.
   - Sizing:
     * Target Allocation = 1,000.0 (Strictly 10% of the 10k starting balance).
     * Liquidity Check: Ensure Target Allocation <= 0.10 * 20d_ADV.
     * Deduct Buy Fee: Investable Cash = Target Allocation - 9.00.
     * Whole Shares: Shares to Buy = math.floor(Investable Cash / Current Price).
     * Deduct Total Cost (Shares * Price + 9.00) from cash, record to open_positions.csv.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import pandas as pd
import yfinance as yf

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

from screener.financial_metrics_engine import get_hard_financials, evaluate_profiles
from screener.portfolio_manager import evaluate_position
from screener.nlp_analyzer import rule_based_analyze_core_fundamentals
from screener.web_verifier import RED_FLAG_PATTERNS, FATAL_RED_FLAG_PATTERNS, WARN_PATTERNS

# Backwards compatibility exports for legacy modules/tests
try:
    from screener.main_controller import (
        load_nordnet_universe,
        match_universe_item,
        ScreenerPipelineController,
    )
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("live_daemon")

DEFAULT_PAPER_ACCOUNT_JSON = BASE_DIR / "data" / "paper_account.json"
DEFAULT_OPEN_POSITIONS_CSV = BASE_DIR / "data" / "open_positions.csv"
DEFAULT_TRADE_HISTORY_CSV = BASE_DIR / "data" / "trade_history.csv"
DEFAULT_CLEAN_UNIVERSE_CSV = BASE_DIR / "data" / "clean_microcap_universe.csv"

# Strict Account & Hyper-Realistic Fee Configuration
STARTING_BALANCE = 10_000.0         # Exactly 10,000 USD/EUR Paper Trading Starting Capital
POSITION_ALLOCATION = 1_000.0       # Exactly 10% of starting balance per position (1,000 USD)
MIN_BROKER_FEE = 9.00               # Flat minimum broker commission ($9.00 / €9.00)
VARIABLE_BROKER_FEE_PCT = 0.002     # 0.20% variable broker commission
MAX_PORTFOLIO_EQUITY_PCT = 0.10     # Max 10% of portfolio equity per position
MAX_ADV_ALLOCATION_PCT = 0.10       # Max 10% of 20-day ADV to protect order book liquidity
SLIPPAGE_PENALTY_PCT = 0.005        # Kept for backwards compatibility


def get_live_fx_rates() -> Dict[str, float]:
    """Fetches live FX rates EURUSD and EURSEK from Yahoo Finance with fallbacks."""
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

    # First convert ticker_currency to EUR
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
                    curr_sym = "€" if self.currency == "EUR" else "$"
                    logger.info(f"Loaded Paper Account: Cash Balance = {curr_sym}{self.cash_balance:,.2f} {self.currency}")
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
        logger.debug(f"Saved Paper Account state: Cash = ${self.cash_balance:,.2f}")

    def reset(self, balance: Optional[float] = None) -> None:
        """Resets account to starting capital."""
        if balance is not None:
            self.starting_balance = float(balance)
        self.cash_balance = self.starting_balance
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.save()
        logger.info(f"🔄 Paper Account Reset: Starting Cash = ${self.cash_balance:,.2f} {self.currency}")


class LiveTradingDaemon:
    """
    Central daemon executing Phase 1 (Tri-Layer Portfolio Exit Check)
    and Phase 2 (Clean Universe Profile B Screening & Sizing).
    """

    def __init__(
        self,
        account_path: Path | str = DEFAULT_PAPER_ACCOUNT_JSON,
        open_positions_path: Path | str = DEFAULT_OPEN_POSITIONS_CSV,
        trade_history_path: Path | str = DEFAULT_TRADE_HISTORY_CSV,
        clean_universe_path: Path | str = DEFAULT_CLEAN_UNIVERSE_CSV,
        starting_balance: float = STARTING_BALANCE,
    ):
        self.account_path = Path(account_path)
        self.open_positions_path = Path(open_positions_path)
        self.trade_history_path = Path(trade_history_path)
        self.clean_universe_path = Path(clean_universe_path)

        self.account = PaperAccountManager(self.account_path, starting_balance)
        self.ensure_files_exist()

    def ensure_files_exist(self) -> None:
        """Ensures CSV files have correct headers if not present."""
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
        else:
            try:
                with open(self.trade_history_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if lines:
                    first_cols = [c.strip() for c in lines[0].strip().split(",")]
                    if len(first_cols) != len(standard_history_header) and len(lines) > 1:
                        data_cols = [c.strip() for c in lines[1].strip().split(",")]
                        if len(data_cols) == len(standard_history_header):
                            lines[0] = ",".join(standard_history_header) + "\n"
                            with open(self.trade_history_path, "w", encoding="utf-8") as f:
                                f.writelines(lines)
                            logger.info(f"Fixed mismatched header in {self.trade_history_path.name}")
            except Exception as e:
                logger.warning(f"Could not inspect/fix {self.trade_history_path.name} header: {e}")

    def load_open_positions(self) -> List[Dict[str, Any]]:
        """Reads open positions from CSV, normalizing legacy headers."""
        if not self.open_positions_path.exists():
            return []

        positions: List[Dict[str, Any]] = []
        try:
            with open(self.open_positions_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ticker = (row.get("Ticker") or row.get("ticker") or "").strip().upper()
                    if not ticker:
                        continue
                    buy_date = row.get("Buy Date") or row.get("EntryDate") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    buy_price = float(row.get("Buy Price") or row.get("EntryPrice") or 0.0)
                    shares = int(float(row.get("Shares") or row.get("shares") or 0))

                    cap_invested_raw = row.get("Capital Invested")
                    if cap_invested_raw is not None and str(cap_invested_raw).strip():
                        capital_invested = float(cap_invested_raw)
                    else:
                        capital_invested = (shares * buy_price) + calculate_transaction_fee(shares * buy_price)

                    strategy = row.get("Strategy") or row.get("Strategy_Type") or "PROFILE_B"

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
        """Saves current open positions back to CSV."""
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
        """Appends a closed trade with full fee and PnL breakdown to trade_history.csv."""
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


    def fetch_live_price(self, ticker: str) -> Optional[float]:
        """Fetches latest real-time closing price via yfinance."""
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
        """
        Layer 2 (LLM News Radar):
        Fetches latest press releases via yfinance news and runs them through
        RED_FLAG_PATTERNS and nlp_analyzer.rule_based_analyze_core_fundamentals.
        Returns ('REJECT', reason) if fatal structural risk detected, otherwise ('HOLD', reason).

        ⚠️  DEGRADED MODE NOTE: This function currently uses ONLY rule-based logic
        (RED_FLAG_PATTERNS hard-gate + rule_based_analyze_core_fundamentals).
        No OpenRouter/LLM API call is made here. The configured model is free-tier
        (meta-llama/llama-3-8b-instruct:free via OpenRouter). HTTP 402 during tests
        indicates daily free quota was exhausted — this resets after ~24h automatically.
        The rule-based fallback is context-free and may produce false positives for
        ambiguous signals (e.g., 'reverse stock split' as Nasdaq compliance vs toxic dilution).
        Upgrade path: replace rule_based_analyze_core_fundamentals with
        analyze_core_fundamentals (LLM) once RED_FLAG_PATTERNS pre-filter is refactored.
        """
        logger.info(
            f"[LAYER 2 RADAR] {ticker}: Running in RULE-BASED DEGRADED MODE "
            f"(no LLM call). Context-free RED_FLAG_PATTERNS scan only."
        )
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

                # 1. Pattern-based fatal red flags (Instant Exit)
                for pattern in FATAL_RED_FLAG_PATTERNS:
                    if pattern.lower() in full_text.lower():
                        reason = f"Fatal red flag '{pattern}' detected in headline: {title}"
                        logger.warning(f"🚨 [LAYER 2 RADAR] {ticker}: {reason}")
                        return "REJECT", reason

                # 2. Pattern-based warning signals (Dual Confirmation)
                for pattern in WARN_PATTERNS:
                    if pattern.lower() in full_text.lower():
                        warning_found = True
                        warning_reason = f"Warning '{pattern}' detected in headline: {title}"

                # 3. NLP fundamental safety check via nlp_analyzer.py (rule-based only)
                nlp_res = rule_based_analyze_core_fundamentals(full_text)
                safety = nlp_res.get("financial_safety", {})
                if safety.get("going_concern_risk") or safety.get("erratic_pivots_detected"):
                    reason = f"Fatal NLP Risk triggered: {nlp_res.get('verdict_details', {}).get('reasoning')}"
                    logger.warning(f"🚨 [LAYER 2 RADAR] {ticker}: {reason}")
                    return "REJECT", reason
                elif nlp_res.get("verdict_details", {}).get("verdict") == "WARN" or safety.get("warning_detected"):
                    warning_found = True
                    warning_reason = f"NLP Warning: {nlp_res.get('verdict_details', {}).get('reasoning')}"

            if warning_found:
                logger.info(f"⚠️ [LAYER 2 RADAR] {ticker}: {warning_reason}")
                return "WARN", warning_reason

            return "HOLD", f"Scanned {len(news_items)} recent news items with no fatal red flags"
        except Exception as e:
            logger.warning(f"Error checking news for {ticker}: {e}")
            return "HOLD", "News scan error"


    def execute_portfolio_management(self) -> int:
        """
        Phase 1: Portfolio Management (Tri-Layer Fundamental Exit Check & Real Costs).
        Evaluates each position against:
          - Layer 1: Catastrophic stop (-50%)
          - Layer 2: LLM news radar
          - Layer 3: Quarterly fundamental deterioration
        On SELL:
          - Gross Sale Value = Shares * Current Price
          - Transaction Fee = max(Gross Sale Value * 0.002, 9.00)
          - Net Return = Gross Sale Value - Transaction Fee
          - Updates cash balance, removes position, logs to trade_history.csv.
        Returns count of positions sold.
        """
        open_positions = self.load_open_positions()
        if not open_positions:
            logger.info("💼 [PHASE 1: PORTFOLIO] No open positions to manage.")
            return 0

        logger.info(f"💼 [PHASE 1: PORTFOLIO] Evaluating {len(open_positions)} active position(s) with Tri-Layer Exit...")

        retained_positions: List[Dict[str, Any]] = []
        closed_count = 0

        for pos in open_positions:
            ticker = pos["Ticker"]
            buy_price = float(pos["Buy Price"])
            shares = int(pos["Shares"])
            capital_invested = float(pos.get("Capital Invested", shares * buy_price))
            buy_date = pos["Buy Date"]

            # 1. Live Price
            current_price = self.fetch_live_price(ticker)
            if current_price is None or current_price <= 0:
                logger.warning(f"⚠️ {ticker}: Price unavailable. Retaining position.")
                retained_positions.append(pos)
                continue

            # 2. Layer 2 News Radar (nlp_analyzer.py)
            news_verdict, news_reason = self.evaluate_news_radar(ticker)

            # 3. Layer 3 Quarterly Financials (financial_metrics_engine.py)
            hard_facts = get_hard_financials(ticker)
            financials_payload = {
                "revenue_growth_yoy": hard_facts.get("revenue_growth_yoy_pct"),
                "cash_runway_months": hard_facts.get("cash_runway_months"),
                "operating_cash_flow": hard_facts.get("operating_cash_flow_ttm"),
            }

            # 4. Tri-Layer Evaluation
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

                # Convert net_return to account currency (EUR) before crediting cash balance
                net_return_acc = net_return * fx_to_acc

                # Update cash balance
                self.account.cash_balance += net_return_acc
                self.account.save()

                # Archive trade
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

                curr_sym = "€" if self.account.currency == "EUR" else "$"
                logger.info(
                    f"🚨 [SELL TRIGGERED] {ticker} | Exit: {reason} | "
                    f"Shares: {shares} @ {current_price:.2f} {ticker_curr} | "
                    f"Gross: {gross_sale_value:.2f} {ticker_curr} | "
                    f"Fee: {transaction_fee:.2f} {ticker_curr} | Net Proceeds: {net_return:.2f} {ticker_curr} "
                    f"({net_return_acc:,.2f} {self.account.currency}) | "
                    f"Net PnL: {net_pnl:+.2f} {ticker_curr} | Remaining Cash: {curr_sym}{self.account.cash_balance:,.2f}"
                )
            else:
                unrealized_gross = shares * current_price
                ticker_curr = get_ticker_currency(ticker)
                fx_to_acc = get_fx_to_account(ticker_curr, self.account.currency)
                fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0
                min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

                unrealized_fee = calculate_transaction_fee(unrealized_gross, min_fee=min_fee_local)
                unrealized_net = unrealized_gross - unrealized_fee
                unrealized_pnl = unrealized_net - capital_invested
                pnl_pct = (unrealized_pnl / capital_invested) * 100.0 if capital_invested > 0 else 0.0

                logger.info(
                    f"  • {ticker}: HOLD | Px: {current_price:.2f} {ticker_curr} | "
                    f"Val: {unrealized_gross:,.2f} {ticker_curr} | PnL: {unrealized_pnl:+.2f} {ticker_curr} ({pnl_pct:+.1f}%) | "
                    f"News: {news_verdict} | Runway: {financials_payload.get('cash_runway_months')}m"
                )
                retained_positions.append(pos)


        self.save_open_positions(retained_positions)
        return closed_count

    def execute_market_screening(self) -> int:
        """
        Phase 2: Market Scanning (Profile B Only & Small Account Sizing).
        Reads clean_microcap_universe.csv, evaluates PROFILE_B only,
        enforces 1,000.0 target allocation, checks 10% 20d ADV liquidity,
        deducts buy fee, and routes whole shares only (math.floor).
        Returns count of new positions opened.
        """
        if not self.clean_universe_path.exists():
            logger.error(f"Universe file {self.clean_universe_path} not found. Aborting screening.")
            return 0

        u_df = pd.read_csv(self.clean_universe_path)
        logger.info(f"🔍 [PHASE 2: SCREENING] Scanning {len(u_df)} verified micro-caps for PROFILE_B signals...")

        open_positions = self.load_open_positions()
        held_tickers = {p["Ticker"] for p in open_positions}

        # Calculate Total Invested Capital in open positions (converted to account currency)
        open_capital = 0.0
        for p in open_positions:
            p_tick = p["Ticker"]
            p_curr = get_ticker_currency(p_tick)
            p_fx = get_fx_to_account(p_curr, self.account.currency)
            p_cap = float(p.get("Capital Invested", p["Shares"] * p["Buy Price"]))
            open_capital += p_cap * p_fx

        curr_sym = "€" if self.account.currency == "EUR" else "$"
        logger.info(
            f"📊 Portfolio Status: Cash = {curr_sym}{self.account.cash_balance:,.2f} | "
            f"Active Capital = {curr_sym}{open_capital:,.2f} | Target Allocation/Stock = {curr_sym}{POSITION_ALLOCATION:,.2f}"
        )

        new_buys = 0

        for idx, row in u_df.iterrows():
            ticker = str(row["ticker"]).strip().upper()
            if ticker in held_tickers:
                continue

            if self.account.cash_balance < (MIN_BROKER_FEE + 10.0):
                logger.info("Cash balance depleted (< min fee + buffer). Ending market screening cycle.")
                break

            # Evaluate hard financials
            hard_facts = get_hard_financials(ticker)
            eval_res = evaluate_profiles(hard_facts)

            is_profile_b = eval_res.get("is_profile_b", False)
            is_profile_a = eval_res.get("is_profile_a", False)

            # Profile A is strictly disabled based on N=240 empirical backtest findings
            if is_profile_a and not is_profile_b:
                logger.debug(f"  • {ticker}: Rejected Profile A growth signal (Profile A disabled).")
                continue

            if not is_profile_b:
                continue

            # Data Freshness Guard: Skip companies with stale statements (>120 days)
            dq = hard_facts.get("data_quality", {})
            if dq.get("is_fresh") is False:
                logger.warning(
                    f"⚠️ {ticker}: Financial statement is stale ({hard_facts.get('data_age_days')} days old > 120d). "
                    "Skipping entry to protect against unreflected deterioration."
                )
                continue

            # 1. Target Allocation in Account Currency = 1,000.0 (Strictly 10% of starting balance)
            target_allocation_acc = POSITION_ALLOCATION
            if target_allocation_acc > self.account.cash_balance:
                target_allocation_acc = self.account.cash_balance

            # Convert target allocation & minimum fee to local ticker currency
            ticker_curr = get_ticker_currency(ticker, row.get("currency"))
            fx_to_acc = get_fx_to_account(ticker_curr, self.account.currency)
            fx_acc_to_local = 1.0 / fx_to_acc if fx_to_acc > 0 else 1.0

            target_allocation_local = target_allocation_acc * fx_acc_to_local
            min_fee_local = MIN_BROKER_FEE * fx_acc_to_local

            # 2. Check Liquidity: Ensure Target Allocation <= 0.10 * 20d_ADV
            adv_20d_local = float(row.get("adv_20d_local", 0.0) or 0.0)
            if adv_20d_local <= 0.0:
                adv_20d_usd = float(row.get("adv_20d_usd", 0.0) or 0.0)
                fx_usd_to_local = get_fx_to_account("USD", ticker_curr)
                adv_20d_local = adv_20d_usd * fx_usd_to_local

            max_adv_allowed = adv_20d_local * MAX_ADV_ALLOCATION_PCT if adv_20d_local > 0 else 0.0

            if max_adv_allowed > 0 and target_allocation_local > max_adv_allowed:
                logger.warning(
                    f"⚠️ {ticker}: Target allocation {target_allocation_local:,.2f} {ticker_curr} exceeds 10% of 20d ADV "
                    f"({max_adv_allowed:,.2f} {ticker_curr}). Capping to ADV limit."
                )
                target_allocation_local = max_adv_allowed

            if target_allocation_local < (min_fee_local + (10.0 * fx_acc_to_local)):
                logger.debug(f"  • {ticker}: Allocation {target_allocation_local:.2f} {ticker_curr} too small after liquidity check. Skipping.")
                continue

            # 3. Deduct Buy Fee to determine Investable Cash in local currency
            investable_cash_local = target_allocation_local - min_fee_local
            if investable_cash_local <= 0:
                logger.debug(f"  • {ticker}: Investable cash <= 0 after fee deduction. Skipping.")
                continue

            # 4. Fetch Current Live Price
            current_price = self.fetch_live_price(ticker) or float(row.get("current_price", 0.0) or 0.0)
            if current_price <= 0:
                logger.warning(f"Could not determine valid price for {ticker}. Skipping buy.")
                continue

            # 5. Whole Shares Only via math.floor
            shares_to_buy = math.floor(investable_cash_local / current_price)
            if shares_to_buy <= 0:
                logger.debug(
                    f"  • {ticker}: Investable cash {investable_cash_local:.2f} {ticker_curr} cannot afford 1 whole share @ {current_price:.2f} {ticker_curr}"
                )
                continue

            # 6. Deduct Total Cost (Shares * Price + Fee) from cash in account currency
            actual_gross_buy = shares_to_buy * current_price
            actual_fee = calculate_transaction_fee(actual_gross_buy, min_fee=min_fee_local)
            total_cost_local = actual_gross_buy + actual_fee
            total_cost_acc = total_cost_local * fx_to_acc

            if total_cost_acc > self.account.cash_balance:
                logger.warning(
                    f"Total cost {total_cost_acc:.2f} {self.account.currency} exceeds cash {self.account.cash_balance:.2f} {self.account.currency}. Skipping."
                )
                continue

            self.account.cash_balance -= total_cost_acc
            self.account.save()

            new_pos = {
                "Ticker": ticker,
                "Buy Date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "Buy Price": current_price,
                "Shares": shares_to_buy,
                "Capital Invested": round(total_cost_local, 2),
                "Strategy": "PROFILE_B",
            }
            open_positions.append(new_pos)
            held_tickers.add(ticker)
            new_buys += 1

            logger.info(
                f"🎯 [BUY TRIGGERED] {ticker} (Profile B Value) | "
                f"Bought {shares_to_buy} whole shares @ {current_price:.2f} {ticker_curr} | "
                f"Gross: {actual_gross_buy:.2f} {ticker_curr} | Fee: {actual_fee:.2f} {ticker_curr} | "
                f"Total Cost: {total_cost_local:,.2f} {ticker_curr} ({total_cost_acc:,.2f} {self.account.currency}) | "
                f"Remaining Cash: {curr_sym}{self.account.cash_balance:,.2f}"
            )

        if new_buys > 0:
            self.save_open_positions(open_positions)

        return new_buys


    def run_daily_cycle(self) -> Tuple[int, int]:
        """Runs full daily workflow: Phase 1 (Exit Check) + Phase 2 (Screening)."""
        print("\n" + "=" * 90)
        print(f"🚀 TRADEBOTTIUKU — LIVE FORWARD-TESTING DAEMON CYCLE: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 90)

        sold_count = self.execute_portfolio_management()
        bought_count = self.execute_market_screening()

        curr_sym = "€" if self.account.currency == "EUR" else "$"
        print("\n" + "-" * 90)
        print(f"✅ CYCLE SUMMARY: {sold_count} position(s) closed | {bought_count} position(s) opened")
        print(f"💰 CURRENT CASH BALANCE: {curr_sym}{self.account.cash_balance:,.2f} {self.account.currency}")
        print("-" * 90 + "\n")

        return sold_count, bought_count

    def run_loop(self, interval_hours: float = 24.0) -> None:
        """Runs the daemon continuously with a sleep loop."""
        interval_seconds = int(interval_hours * 3600)
        logger.info(f"Starting continuous live daemon loop (Interval: {interval_hours}h / {interval_seconds}s)...")

        while True:
            try:
                self.run_daily_cycle()
                logger.info(f"Sleeping for {interval_hours} hours until next cycle...")
                time.sleep(interval_seconds)
            except KeyboardInterrupt:
                logger.info("Daemon stopped by user.")
                break
            except Exception as e:
                logger.error(f"Unexpected error in daemon loop: {e}. Retrying in 60s...")
                time.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser(description="tradeBotTiuku Live Forward-Testing Daemon")
    parser.add_argument("--run-once", action="store_true", help="Execute a single daily cycle and exit")
    parser.add_argument("--loop", action="store_true", help="Run continuously in a sleep loop")
    parser.add_argument("--interval-hours", type=float, default=24.0, help="Interval in hours for loop mode (default: 24)")
    parser.add_argument("--reset-paper", action="store_true", help="Reset paper account to starting capital ($10,000)")
    parser.add_argument("--starting-balance", type=float, default=STARTING_BALANCE, help="Initial paper capital (default: 10000)")

    args = parser.parse_args()

    daemon = LiveTradingDaemon(starting_balance=args.starting_balance)

    if args.reset_paper:
        daemon.account.reset(args.starting_balance)
        # Clear open positions to match clean account
        daemon.save_open_positions([])
        logger.info("Cleared open positions for fresh forward-testing session.")

    if args.run_once or not args.loop:
        daemon.run_daily_cycle()
    else:
        daemon.run_loop(interval_hours=args.interval_hours)


if __name__ == "__main__":
    main()
