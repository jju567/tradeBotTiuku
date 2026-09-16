"""
Exit Manager for Nasdaq OMX Helsinki Micro-Cap Equities.

Manages open paper positions and evaluates strategy-aware exit conditions:

1. Pipeline A: SATELLITE (Daily Catalyst Trades):
   - Hard Take Profit (+25.0% gain from EntryPrice triggers 100% full exit).
   - Trailing Stop Loss (15.0% drop from peak HighestPrice).
   - Hard Stop Loss (15.0% drop from initial EntryPrice).
   - Time-Based Decay Exit (held > 45 days and Unrealized PnL < +5.0%).
   - Slippage & Spread Penalty: Mandatory 2.5% penalty on executed exit price.
   - Evaluated EVERY DAY using closing prices from yfinance.

2. Pipeline B: CORE (Quarterly Fundamental Tenbagger Holdings):
   - NEVER sell based on daily price drops, market volatility, trailing stops, or 45-day time decay.
   - Core positions are long-term fundamental holdings evaluated on quarterly/annual reports.
   - Position is ONLY exited if a subsequent financial report fails the fundamental thesis
     (e.g., Gross Margin drops < 40%, SaaS growth stalls, or cash distress arises).

3. State & Data Persistence:
   - Reads/updates `data/open_positions.csv` (Columns: Ticker, EntryDate, EntryPrice, HighestPrice, Shares, PositionValue, Strategy_Type).
   - Appends closed trades to `data/trade_history.csv` with strategy tag, full PnL, duration, and exit reason.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, date, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

import pandas as pd
import yfinance as yf

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("screener.exit_manager")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OPEN_POSITIONS_CSV = BASE_DIR / "data" / "open_positions.csv"
DEFAULT_TRADE_HISTORY_CSV = BASE_DIR / "data" / "trade_history.csv"

# Micro-cap quantitative defaults
DEFAULT_TAKE_PROFIT_PCT = 0.25        # +25.0% gain triggers 100% full exit
DEFAULT_TRAILING_STOP_PCT = 0.15      # 15.0% drop from highest high
DEFAULT_HARD_STOP_PCT = 0.15          # 15.0% drop from entry price
DEFAULT_MAX_HOLDING_DAYS = 45         # Max 45 days for stagnant satellite positions
DEFAULT_TIME_DECAY_MIN_PNL_PCT = 0.05 # Minimum +5.0% required after 45 days
DEFAULT_EXIT_SLIPPAGE_PCT = 0.025     # 2.5% exit slippage / spread penalty


@dataclass
class OpenPosition:
    """Represents an active paper trading position."""
    Ticker: str
    EntryDate: str
    EntryPrice: float
    HighestPrice: float
    Shares: int = 100
    PositionValue: float = 1000.0
    Strategy_Type: str = "SATELLITE"


@dataclass
class ClosedTrade:
    """Represents an exited trade archived to trade history."""
    Ticker: str
    Strategy_Type: str
    EntryDate: str
    EntryPrice: float
    ExitDate: str
    ExitPriceRaw: float
    ExitPriceExec: float
    HighestPrice: float
    HoldingDays: int
    Shares: int
    GrossReturnPct: float
    NetReturnPct: float
    GrossPnLEur: float
    NetPnLEur: float
    SlippagePenaltyPct: float
    ExitReason: str


class ExitManager:
    """Evaluates and executes strategy-aware exits on active equity positions."""

    def __init__(
        self,
        open_positions_path: Optional[Path | str] = None,
        trade_history_path: Optional[Path | str] = None,
        take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
        trailing_stop_pct: float = DEFAULT_TRAILING_STOP_PCT,
        hard_stop_pct: float = DEFAULT_HARD_STOP_PCT,
        max_holding_days: int = DEFAULT_MAX_HOLDING_DAYS,
        time_decay_min_pnl_pct: float = DEFAULT_TIME_DECAY_MIN_PNL_PCT,
        exit_slippage_pct: float = DEFAULT_EXIT_SLIPPAGE_PCT,
    ):
        self.open_positions_path = Path(open_positions_path or DEFAULT_OPEN_POSITIONS_CSV)
        self.trade_history_path = Path(trade_history_path or DEFAULT_TRADE_HISTORY_CSV)
        self.take_profit_pct = take_profit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.hard_stop_pct = hard_stop_pct
        self.max_holding_days = max_holding_days
        self.time_decay_min_pnl_pct = time_decay_min_pnl_pct
        self.exit_slippage_pct = exit_slippage_pct

        # Ensure target data directory exists
        self.open_positions_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_history_path.parent.mkdir(parents=True, exist_ok=True)

    def load_open_positions(self) -> List[Dict[str, Any]]:
        """Reads open positions from CSV file with Strategy_Type backward compatibility."""
        if not self.open_positions_path.exists():
            logger.info(f"Open positions file does not exist at {self.open_positions_path}. Returning empty list.")
            return []

        positions = []
        try:
            with open(self.open_positions_path, mode="r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not row.get("Ticker"):
                        continue
                    try:
                        ticker = row["Ticker"].strip()
                        entry_date = row.get("EntryDate", "").strip()
                        entry_price = float(row.get("EntryPrice", 0.0))
                        highest_price = float(row.get("HighestPrice", entry_price))
                        shares = int(float(row.get("Shares", 100)))
                        pos_value = float(row.get("PositionValue", entry_price * shares if entry_price > 0 else 1000.0))
                        strategy_type = row.get("Strategy_Type", "SATELLITE").strip().upper() if row.get("Strategy_Type") else "SATELLITE"

                        positions.append({
                            "Ticker": ticker,
                            "EntryDate": entry_date,
                            "EntryPrice": entry_price,
                            "HighestPrice": max(highest_price, entry_price),
                            "Shares": shares,
                            "PositionValue": pos_value,
                            "Strategy_Type": strategy_type,
                        })
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Skipping malformed row {row}: {e}")
        except Exception as e:
            logger.error(f"Error reading {self.open_positions_path}: {e}")
            return []

        return positions

    def save_open_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Saves active open positions back to CSV file including Strategy_Type column."""
        fieldnames = ["Ticker", "EntryDate", "EntryPrice", "HighestPrice", "Shares", "PositionValue", "Strategy_Type"]
        try:
            with open(self.open_positions_path, mode="w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for pos in positions:
                    writer.writerow({
                        "Ticker": pos["Ticker"],
                        "EntryDate": pos["EntryDate"],
                        "EntryPrice": round(float(pos["EntryPrice"]), 4),
                        "HighestPrice": round(float(pos["HighestPrice"]), 4),
                        "Shares": int(pos.get("Shares", 100)),
                        "PositionValue": round(float(pos.get("PositionValue", 1000.0)), 2),
                        "Strategy_Type": pos.get("Strategy_Type", "SATELLITE").upper(),
                    })
            logger.info(f"Saved {len(positions)} open position(s) to {self.open_positions_path}")
        except Exception as e:
            logger.error(f"Error saving open positions to {self.open_positions_path}: {e}")

    def append_to_trade_history(self, closed_trades: List[Dict[str, Any]]) -> None:
        """Appends closed trades to trade history CSV file."""
        if not closed_trades:
            return

        file_exists = self.trade_history_path.exists()
        fieldnames = [
            "Ticker",
            "Strategy_Type",
            "EntryDate",
            "EntryPrice",
            "ExitDate",
            "ExitPriceRaw",
            "ExitPriceExec",
            "HighestPrice",
            "HoldingDays",
            "Shares",
            "GrossReturnPct",
            "NetReturnPct",
            "GrossPnLEur",
            "NetPnLEur",
            "SlippagePenaltyPct",
            "ExitReason",
        ]

        try:
            with open(self.trade_history_path, mode="a", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not file_exists:
                    writer.writeheader()
                for trade in closed_trades:
                    writer.writerow({
                        "Ticker": trade["Ticker"],
                        "Strategy_Type": trade.get("Strategy_Type", "SATELLITE"),
                        "EntryDate": trade["EntryDate"],
                        "EntryPrice": round(float(trade["EntryPrice"]), 4),
                        "ExitDate": trade["ExitDate"],
                        "ExitPriceRaw": round(float(trade["ExitPriceRaw"]), 4),
                        "ExitPriceExec": round(float(trade["ExitPriceExec"]), 4),
                        "HighestPrice": round(float(trade["HighestPrice"]), 4),
                        "HoldingDays": int(trade["HoldingDays"]),
                        "Shares": int(trade.get("Shares", 100)),
                        "GrossReturnPct": round(float(trade["GrossReturnPct"]), 2),
                        "NetReturnPct": round(float(trade["NetReturnPct"]), 2),
                        "GrossPnLEur": round(float(trade["GrossPnLEur"]), 2),
                        "NetPnLEur": round(float(trade["NetPnLEur"]), 2),
                        "SlippagePenaltyPct": round(float(trade["SlippagePenaltyPct"]), 2),
                        "ExitReason": trade["ExitReason"],
                    })
            logger.info(f"Appended {len(closed_trades)} closed trade(s) to {self.trade_history_path}")
        except Exception as e:
            logger.error(f"Error writing to trade history at {self.trade_history_path}: {e}")

    def fetch_price_history(self, ticker: str, start_date_str: str) -> Optional[pd.DataFrame]:
        """Fetches daily price history from EntryDate to current date with error handling."""
        try:
            try:
                start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
            except ValueError:
                start_dt = datetime.fromisoformat(start_date_str.split("T")[0])

            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(start=start_dt.strftime("%Y-%m-%d"), interval="1d", auto_adjust=False)

            if df is None or df.empty:
                df = ticker_obj.history(period="6mo", interval="1d", auto_adjust=False)
                if df is not None and not df.empty:
                    df = df[df.index >= pd.Timestamp(start_dt).tz_localize(df.index.tz)]

            if df is None or df.empty:
                logger.warning(f"No price history found for {ticker} starting from {start_date_str}")
                return None

            return df
        except Exception as e:
            logger.error(f"Failed to fetch market data for {ticker}: {e}")
            return None

    def evaluate_position_exit(
        self,
        position: Dict[str, Any],
        price_df: pd.DataFrame,
        fundamental_exit_reason: Optional[str] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Evaluates an active position against strategy-specific exit rules.
        
        Strategy Separation:
        - SATELLITE: Evaluates daily price rules (Take Profit +25%, Trailing Stop 15%, Hard Stop 15%, Time Decay 45d).
        - CORE: NEVER sells based on daily price drops. Only exits if `fundamental_exit_reason` is passed
                (e.g., quarterly report fails gross margin > 40%, SaaS growth, or cash runway).
        
        Returns:
            (should_exit: bool, closed_trade_dict_or_None, updated_position_dict_or_None)
        """
        ticker = position["Ticker"]
        strategy_type = position.get("Strategy_Type", "SATELLITE").upper()
        entry_price = float(position["EntryPrice"])
        current_highest = float(position["HighestPrice"])
        entry_date_str = position["EntryDate"]
        shares = int(position.get("Shares", 100))

        try:
            entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
        except ValueError:
            entry_dt = datetime.fromisoformat(entry_date_str.split("T")[0]).date()

        # Update highest price seen across historical daily Highs since entry
        historical_high = float(price_df["High"].max()) if not price_df.empty else entry_price
        new_highest = max(current_highest, historical_high, entry_price)
        updated_position = dict(position)
        updated_position["HighestPrice"] = new_highest
        updated_position["Strategy_Type"] = strategy_type

        # Get latest available trading day
        latest_row = price_df.iloc[-1]
        latest_date = price_df.index[-1].date() if hasattr(price_df.index[-1], "date") else date.today()
        latest_price = float(latest_row["Close"])
        latest_date_str = latest_date.strftime("%Y-%m-%d")

        holding_days = (latest_date - entry_dt).days
        if holding_days < 0:
            holding_days = len(price_df)

        unrealized_return_pct = (latest_price - entry_price) / entry_price if entry_price > 0 else 0.0
        drop_from_highest = (latest_price - new_highest) / new_highest if new_highest > 0 else 0.0

        should_exit = False
        exit_reason = ""

        # =====================================================================
        # CORE STRATEGY EXIT LOGIC (Quarterly Fundamental Tenbagger Holdings)
        # =====================================================================
        if strategy_type == "CORE":
            if fundamental_exit_reason:
                should_exit = True
                exit_reason = f"Core Fundamental Deterioration: {fundamental_exit_reason}"
            else:
                # CORE holdings NEVER sell based on daily price volatility / trailing stops / time decay
                return False, None, updated_position

        # =====================================================================
        # SATELLITE STRATEGY EXIT LOGIC (Daily Catalyst Trades)
        # =====================================================================
        else:
            # Rule 1: Hard Take Profit (100% Exit at >= +25.0% Gain)
            if unrealized_return_pct >= self.take_profit_pct:
                should_exit = True
                exit_reason = f"Take Profit Hit (+{self.take_profit_pct*100:.0f}%)"

            # Rule 2: Hard Stop Loss (15% drop from Entry Price)
            elif unrealized_return_pct <= -self.hard_stop_pct:
                should_exit = True
                exit_reason = f"Hard Stop Loss Hit ({unrealized_return_pct * 100:.1f}% <= -{self.hard_stop_pct * 100:.1f}%)"

            # Rule 3: Trailing Stop Loss (15% drop from Highest Price peak)
            elif drop_from_highest <= -self.trailing_stop_pct:
                should_exit = True
                exit_reason = f"Trailing Stop Hit ({drop_from_highest * 100:.1f}% <= -{self.trailing_stop_pct * 100:.1f}% from Peak {new_highest:.2f}€)"

            # Rule 4: Time-Based Decay Exit (>45 days and PnL < +5.0%)
            elif holding_days > self.max_holding_days and unrealized_return_pct < self.time_decay_min_pnl_pct:
                should_exit = True
                exit_reason = f"Time Decay Exit ({holding_days}d > {self.max_holding_days}d with Return {unrealized_return_pct * 100:.1f}% < +{self.time_decay_min_pnl_pct * 100:.1f}%)"

        if should_exit:
            # Apply mandatory 2.5% exit slippage/spread penalty
            exit_price_raw = latest_price
            exit_price_exec = exit_price_raw * (1.0 - self.exit_slippage_pct)

            gross_return_pct = ((exit_price_raw - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
            net_return_pct = ((exit_price_exec - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

            gross_pnl_eur = (exit_price_raw - entry_price) * shares
            net_pnl_eur = (exit_price_exec - entry_price) * shares

            closed_trade = {
                "Ticker": ticker,
                "Strategy_Type": strategy_type,
                "EntryDate": entry_date_str,
                "EntryPrice": entry_price,
                "ExitDate": latest_date_str,
                "ExitPriceRaw": exit_price_raw,
                "ExitPriceExec": exit_price_exec,
                "HighestPrice": new_highest,
                "HoldingDays": holding_days,
                "Shares": shares,
                "GrossReturnPct": gross_return_pct,
                "NetReturnPct": net_return_pct,
                "GrossPnLEur": gross_pnl_eur,
                "NetPnLEur": net_pnl_eur,
                "SlippagePenaltyPct": self.exit_slippage_pct * 100.0,
                "ExitReason": exit_reason,
            }
            return True, closed_trade, None

        return False, None, updated_position

    def evaluate_core_fundamental_exit(
        self,
        ticker: str,
        cash_issue: bool = False,
        gross_margin_pct: Optional[float] = None,
        recurring_revenue: bool = True,
        rule_of_40_passed: bool = True,
        notes: str = "",
    ) -> Tuple[bool, str]:
        """
        Evaluates subsequent quarterly report for an active CORE position.
        Triggers exit if gross margins drop, recurring revenue model fails, or cash distress arises.
        """
        reasons = []
        if cash_issue:
            reasons.append("Cash/Runway distress in quarterly report")
        if gross_margin_pct is not None and gross_margin_pct < 40.0:
            reasons.append(f"Gross margin compressed to {gross_margin_pct:.1f}% (< 40%)")
        if not recurring_revenue:
            reasons.append("Recurring revenue / SaaS model broken")
        if not rule_of_40_passed:
            reasons.append("Rule of 40 score failed in latest report")

        if reasons:
            detail = "; ".join(reasons)
            if notes:
                detail += f" ({notes})"
            return True, detail
        return False, ""

    def check_exits(self) -> Dict[str, Any]:
        """
        Main execution routine:
        1. Reads open positions from `data/open_positions.csv`.
        2. Fetches latest yfinance data for each position.
        3. Evaluates SATELLITE positions against daily price exit rules.
        4. Maintains CORE positions (ignoring daily price drops).
        5. Updates `open_positions.csv` and appends closed trades to `trade_history.csv`.
        """
        logger.info(f"Checking exit conditions for open positions at {self.open_positions_path}...")
        open_positions = self.load_open_positions()

        if not open_positions:
            logger.info("No open positions found to evaluate.")
            return {
                "total_open": 0,
                "exited_count": 0,
                "surviving_count": 0,
                "closed_trades": [],
                "active_positions": [],
            }

        surviving_positions: List[Dict[str, Any]] = []
        closed_trades: List[Dict[str, Any]] = []

        for idx, pos in enumerate(open_positions, 1):
            ticker = pos["Ticker"]
            entry_date = pos["EntryDate"]
            strategy = pos.get("Strategy_Type", "SATELLITE").upper()
            logger.info(f"[{idx}/{len(open_positions)}] Evaluating [{strategy}] {ticker} (Entry: {pos['EntryPrice']}€ on {entry_date})...")

            df = self.fetch_price_history(ticker, entry_date)
            if df is None or df.empty:
                logger.warning(f"Skipping exit evaluation for {ticker} due to missing price data. Keeping position active.")
                surviving_positions.append(pos)
                continue

            should_exit, closed_trade, updated_pos = self.evaluate_position_exit(pos, df)

            if should_exit and closed_trade:
                logger.info(
                    f"🔴 [EXIT TRIGGERED - {strategy}] {ticker}: {closed_trade['ExitReason']} | "
                    f"Raw Exit: {closed_trade['ExitPriceRaw']:.2f}€, Exec: {closed_trade['ExitPriceExec']:.2f}€ | "
                    f"Net Return: {closed_trade['NetReturnPct']:+.2f}% ({closed_trade['NetPnLEur']:+.2f}€)"
                )
                closed_trades.append(closed_trade)
            else:
                current_price = float(df.iloc[-1]['Close'])
                entry_p = float(pos['EntryPrice'])
                unrealized = ((current_price - entry_p) / entry_p * 100) if entry_p > 0 else 0.0
                if strategy == "CORE":
                    logger.info(
                        f"💎 [CORE HOLD] {ticker}: Current={current_price:.2f}€, Peak={updated_pos['HighestPrice']:.2f}€, "
                        f"Unrealized={unrealized:+.2f}% (Daily price stops bypassed)"
                    )
                else:
                    logger.info(
                        f"🟢 [SATELLITE HOLD] {ticker}: Current={current_price:.2f}€, Peak={updated_pos['HighestPrice']:.2f}€, "
                        f"Unrealized={unrealized:+.2f}%"
                    )
                surviving_positions.append(updated_pos)

        # Update persistent CSV files
        self.save_open_positions(surviving_positions)
        if closed_trades:
            self.append_to_trade_history(closed_trades)

        logger.info(
            f"Exit check cycle completed. Active positions: {len(surviving_positions)}, "
            f"Closed trades: {len(closed_trades)}"
        )

        return {
            "total_open": len(open_positions),
            "exited_count": len(closed_trades),
            "surviving_count": len(surviving_positions),
            "closed_trades": closed_trades,
            "active_positions": surviving_positions,
        }


def check_exits(
    open_positions_path: Optional[str | Path] = None,
    trade_history_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Convenience functional wrapper for check_exits."""
    manager = ExitManager(
        open_positions_path=open_positions_path,
        trade_history_path=trade_history_path,
    )
    return manager.check_exits()


def main():
    """CLI interface for running exit manager checks."""
    parser = argparse.ArgumentParser(description="Nasdaq OMX Helsinki Micro-Cap Core & Satellite Exit Manager")
    parser.add_argument("--positions", type=str, default=str(DEFAULT_OPEN_POSITIONS_CSV), help="Path to open_positions.csv")
    parser.add_argument("--history", type=str, default=str(DEFAULT_TRADE_HISTORY_CSV), help="Path to trade_history.csv")
    parser.add_argument("--take-profit", type=float, default=0.25, help="Hard take profit trigger for Satellite (default: 0.25 = +25%)")
    parser.add_argument("--trailing-stop", type=float, default=0.15, help="Trailing stop percentage for Satellite (default: 0.15 = 15%)")
    parser.add_argument("--hard-stop", type=float, default=0.15, help="Hard stop loss percentage for Satellite (default: 0.15 = 15%)")
    parser.add_argument("--max-days", type=int, default=45, help="Max holding days for Satellite time decay exit (default: 45)")
    parser.add_argument("--time-decay-min-pnl", type=float, default=0.05, help="Min required return after max days (default: 0.05 = 5%)")
    parser.add_argument("--slippage", type=float, default=0.025, help="Exit slippage penalty (default: 0.025 = 2.5%)")

    args = parser.parse_args()

    manager = ExitManager(
        open_positions_path=args.positions,
        trade_history_path=args.history,
        take_profit_pct=args.take_profit,
        trailing_stop_pct=args.trailing_stop,
        hard_stop_pct=args.hard_stop,
        max_holding_days=args.max_days,
        time_decay_min_pnl_pct=args.time_decay_min_pnl,
        exit_slippage_pct=args.slippage,
    )
    res = manager.check_exits()
    print("\n" + "=" * 60)
    print("        CORE & SATELLITE EXIT MANAGER SUMMARY REPORT         ")
    print("=" * 60)
    print(f"Total Evaluated Positions: {res['total_open']}")
    print(f"Active / Surviving:        {res['surviving_count']}")
    print(f"Closed / Exited:           {res['exited_count']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
