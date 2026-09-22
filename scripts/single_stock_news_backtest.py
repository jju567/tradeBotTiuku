"""
single_stock_news_backtest.py - Single-Stock Time Machine Backtester for LLM News Radar (Layer 2).

Evaluates whether the Layer 2 LLM News Radar can front-run price crashes by ingesting
historical company press releases and triggering an exit on the next market open.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
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

# Ensure workspace root in sys.path
_current = Path(__file__).resolve().parent
_root = _current.parent if _current.name == "scripts" else _current
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


from screener.nlp_analyzer import analyze_core_fundamentals, rule_based_analyze_core_fundamentals
from screener.web_verifier import RED_FLAG_PATTERNS, FATAL_RED_FLAG_PATTERNS, WARN_PATTERNS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("news_time_machine")


def evaluate_release_layer2(ticker: str, headline: str, full_text: str) -> Tuple[str, str]:
    """
    Layer 2 LLM News Radar Evaluation:
    Checks headline and full text against FATAL_RED_FLAG_PATTERNS, WARN_PATTERNS,
    and LLM / NLP Core Analysis.
    Returns ('REJECT', reason), ('WARN', reason), or ('HOLD', reason).
    """
    combined_text = f"{headline}\n\n{full_text}".strip()
    lower_text = combined_text.lower()

    # 1. Pattern-based Fatal Red Flags (Instant Reject)
    for pat in FATAL_RED_FLAG_PATTERNS:
        if pat.lower() in lower_text:
            reason = f"Fatal red flag pattern detected: '{pat}'"
            return "REJECT", reason

    # 2. Pattern-based Warning Signals (Dual Confirmation)
    for pat in WARN_PATTERNS:
        if pat.lower() in lower_text:
            reason = f"Structural warning pattern detected: '{pat}'"
            return "WARN", reason

    # 3. LLM / NLP Core Analysis
    try:
        nlp_res = analyze_core_fundamentals(
            text_content=combined_text,
            ticker=None,
            hard_financials={},
            throttle_sleep_seconds=0.5,
        )
        verdict = nlp_res.get("verdict_details", {}).get("verdict", "HOLD")
        safety = nlp_res.get("financial_safety", {})
        
        if safety.get("going_concern_risk") or safety.get("erratic_pivots_detected"):
            reason = nlp_res.get("verdict_details", {}).get("reasoning") or "LLM identified fatal structural capital risk"
            return "REJECT", reason

        if verdict == "WARN" or safety.get("dilution_risk_detected") or safety.get("unsustainable_cash_burn"):
            reason = nlp_res.get("verdict_details", {}).get("reasoning") or "LLM flagged structural warning (runway/dilution)"
            return "WARN", reason

        if verdict == "REJECT":
            reason = nlp_res.get("verdict_details", {}).get("reasoning", "")
            if any(k in reason.lower() for k in ["riskiseulan", "taseriski", "pääomatuho", "going concern", "konkurs"]):
                return "REJECT", reason
            return "HOLD", "Routine disclosure (no structural fatal risk)"
        
        return "HOLD", nlp_res.get("verdict_details", {}).get("reasoning", "No structural risks identified")
    except Exception as e:
        logger.warning(f"LLM analysis error, falling back to rule-based: {e}")
        nlp_res = rule_based_analyze_core_fundamentals(combined_text)
        verdict = nlp_res.get("verdict_details", {}).get("verdict", "HOLD")
        safety = nlp_res.get("financial_safety", {})
        if safety.get("going_concern_risk") or safety.get("erratic_pivots_detected"):
            reason = nlp_res.get("verdict_details", {}).get("reasoning") or "Rule-based gate identified fatal capital risk"
            return "REJECT", reason
        if verdict == "WARN" or safety.get("dilution_risk_detected") or safety.get("unsustainable_cash_burn"):
            reason = nlp_res.get("verdict_details", {}).get("reasoning") or "Rule-based scan flagged structural warning"
            return "WARN", reason
        return "HOLD", "Rule-based scan found no fatal red flags"


class SingleStockNewsBacktester:
    """
    Simulates day-by-day price evolution and checks for real-time news alerts.
    """

    def __init__(
        self,
        ticker: str,
        news_csv_path: Path | str = "data/historical_news_mock.csv",
        buy_date: str = "2026-02-16",
        start_date: str = "2026-01-01",
        end_date: str = "2026-04-30",
        slippage_penalty_pct: float = 0.50,
    ):
        self.ticker = ticker.upper()
        self.news_csv_path = Path(news_csv_path)
        self.buy_date = buy_date
        self.start_date = start_date
        self.end_date = end_date
        self.slippage_penalty_pct = slippage_penalty_pct

    def load_news(self) -> pd.DataFrame:
        """Loads and filters news for the specified ticker."""
        if not self.news_csv_path.exists():
            logger.error(f"News file not found: {self.news_csv_path}")
            return pd.DataFrame()

        df = pd.read_csv(self.news_csv_path)
        df["Ticker"] = df["Ticker"].str.upper().str.strip()
        df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
        filtered = df[df["Ticker"] == self.ticker].sort_values("Date")
        return filtered

    def fetch_price_data(self) -> pd.DataFrame:
        """Fetches daily OHLC price history via yfinance."""
        logger.info(f"Fetching market data for {self.ticker} ({self.start_date} to {self.end_date})...")
        t = yf.Ticker(self.ticker)
        df = t.history(start=self.start_date, end=self.end_date, auto_adjust=True)
        if df.empty:
            logger.error(f"No price data found for {self.ticker}.")
            return pd.DataFrame()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df = df.dropna(subset=["Close"])
        return df.sort_index()

    def run(self) -> Dict[str, Any]:
        """Runs the day-by-day simulation loop."""
        prices = self.fetch_price_data()
        if prices.empty:
            return {"status": "ERROR", "message": "Price data empty"}

        news_df = self.load_news()
        logger.info(f"Loaded {len(news_df)} historical press release(s) for {self.ticker}.")

        # Find initial buy date and execution
        buy_dt = pd.to_datetime(self.buy_date)
        eligible_days = prices[prices.index >= buy_dt]
        if eligible_days.empty:
            return {"status": "ERROR", "message": f"No trading days found on or after {self.buy_date}"}

        initial_idx = 0
        buy_actual_dt = eligible_days.index[initial_idx]
        buy_date_str = buy_actual_dt.strftime("%Y-%m-%d")
        buy_price = float(eligible_days.iloc[initial_idx]["Close"])

        print("\n" + "=" * 95)
        print(f"🚀 SINGLE-STOCK TIME MACHINE: LLM NEWS RADAR (LAYER 2) BACKTEST")
        print(f"🎯 Target Ticker: {self.ticker} | Buy Date: {buy_date_str} @ {buy_price:.2f} | Slippage: {self.slippage_penalty_pct:.2f}%")
        print("=" * 95 + "\n")

        timeline_logs: List[Dict[str, Any]] = []
        is_holding = True
        sell_date_str: Optional[str] = None
        sell_price: Optional[float] = None
        exit_reason: Optional[str] = None
        exit_day_num: Optional[int] = None

        trading_days = list(eligible_days.index)
        n_days = len(trading_days)

        pending_sell_next_open = False
        pending_sell_reason = ""
        warning_active = False
        warn_ref_price: Optional[float] = None
        warn_date_str: Optional[str] = None
        warn_reason: Optional[str] = None

        for day_i, cur_dt in enumerate(trading_days):
            date_str = cur_dt.strftime("%Y-%m-%d")
            row = eligible_days.loc[cur_dt]
            open_px = float(row["Open"])
            close_px = float(row["Close"])
            low_px = float(row["Low"])
            high_px = float(row["High"])

            # 1. Check if we have a pending sell from yesterday's FATAL news
            if pending_sell_next_open and is_holding:
                sell_price = open_px
                sell_date_str = date_str
                exit_reason = pending_sell_reason
                exit_day_num = day_i
                is_holding = False

                gross_ret = ((sell_price - buy_price) / buy_price) * 100.0
                net_ret = gross_ret - self.slippage_penalty_pct

                log_entry = {
                    "day": day_i,
                    "date": date_str,
                    "event": "SELL EXECUTION",
                    "price": sell_price,
                    "details": f"⚡ EXECUTED SELL AT MARKET OPEN @ {sell_price:.2f} (Net Return: {net_ret:+.2f}%)",
                    "reason": exit_reason,
                }
                timeline_logs.append(log_entry)
                print(f"[{date_str}] [DAY {day_i:02d}] 🔴 {log_entry['details']}")
                print(f"            Reason: {exit_reason}\n")
                break

            # 1b. Check Dual Confirmation if a structural warning is active
            if warning_active and is_holding and warn_ref_price is not None and date_str != warn_date_str:
                confirmed_drop = False
                confirm_px = open_px
                
                if open_px <= warn_ref_price * 0.95:
                    confirmed_drop = True
                    confirm_px = open_px
                elif close_px <= warn_ref_price * 0.95:
                    confirmed_drop = True
                    confirm_px = close_px
                elif close_px <= buy_price * 0.90:
                    confirmed_drop = True
                    confirm_px = close_px

                if confirmed_drop:
                    sell_price = confirm_px
                    sell_date_str = date_str
                    exit_reason = f"Dual Confirmation Triggered: Price confirmed breakdown ({sell_price:.2f} vs ref {warn_ref_price:.2f}) after warning: {warn_reason}"
                    exit_day_num = day_i
                    is_holding = False

                    gross_ret = ((sell_price - buy_price) / buy_price) * 100.0
                    net_ret = gross_ret - self.slippage_penalty_pct

                    log_entry = {
                        "day": day_i,
                        "date": date_str,
                        "event": "SELL EXECUTION (DUAL CONFIRMATION)",
                        "price": sell_price,
                        "details": f"🛡️ DUAL CONFIRMATION EXIT @ {sell_price:.2f} (Net Return: {net_ret:+.2f}%)",
                        "reason": exit_reason,
                    }
                    timeline_logs.append(log_entry)
                    print(f"[{date_str}] [DAY {day_i:02d}] 🔴 {log_entry['details']}")
                    print(f"            Reason: {exit_reason}\n")
                    break
                else:
                    # Check if price has rallied significantly (+10% above warn ref), clearing warning
                    if close_px >= warn_ref_price * 1.10:
                        print(f"[{date_str}] [DAY {day_i:02d}] 🟢 WARNING CLEARED: Price rallied +10% above warning level ({close_px:.2f} vs {warn_ref_price:.2f}).")
                        warning_active = False

            # 2. Regular holding status
            unrealized_gross = ((close_px - buy_price) / buy_price) * 100.0
            warn_tag = f" [WARN ACTIVE: Ref={warn_ref_price:.2f}]" if warning_active and warn_ref_price else ""
            print(f"[{date_str}] [DAY {day_i:02d}] 💼 HOLDING: Close={close_px:.2f} (Unrealized: {unrealized_gross:+.2f}%){warn_tag}")

            # 3. Check for press releases on this date
            day_news = news_df[news_df["Date"] == date_str]
            if not day_news.empty:
                for _, news_item in day_news.iterrows():
                    headline = str(news_item.get("Headline", ""))
                    full_text = str(news_item.get("Full_Text", ""))
                    print(f"  📢 NEWS DETECTED: '{headline[:75]}...'")

                    verdict, reason = evaluate_release_layer2(self.ticker, headline, full_text)
                    print(f"  🤖 LLM LAYER 2 RADAR VERDICT: [{verdict}] -> {reason}")

                    if verdict == "REJECT":
                        pending_sell_next_open = True
                        pending_sell_reason = f"Layer 2 Fatal Red Flag: {reason}"
                        print(f"  🚨 [FATAL EXIT SIGNAL TRIGGERED]: Queuing IMMEDIATE SELL for next trading day's OPEN.")
                    elif verdict == "WARN":
                        warning_active = True
                        warn_ref_price = close_px
                        warn_date_str = date_str
                        warn_reason = reason
                        print(f"  ⚠️ [STRUCTURAL WARNING SIGNAL]: Activated Dual Confirmation. Ref Price: {warn_ref_price:.2f}. Monitoring for price breakdown (-5%).")

            timeline_logs.append({
                "day": day_i,
                "date": date_str,
                "event": "HOLD",
                "price": close_px,
                "details": f"Close {close_px:.2f}",
            })

        # Calculate final comparison stats
        # Scenario A: LLM News Radar Exit
        if not is_holding and sell_price is not None:
            llm_gross_ret = ((sell_price - buy_price) / buy_price) * 100.0
            llm_net_ret = llm_gross_ret - self.slippage_penalty_pct
            llm_holding_days = exit_day_num
        else:
            # Never exited, held to end
            last_close = float(eligible_days.iloc[-1]["Close"])
            llm_gross_ret = ((last_close - buy_price) / buy_price) * 100.0
            llm_net_ret = llm_gross_ret - self.slippage_penalty_pct
            llm_holding_days = n_days
            sell_date_str = trading_days[-1].strftime("%Y-%m-%d")
            sell_price = last_close
            exit_reason = "HELD_TO_END_OF_PERIOD"

        # Scenario B: Buy & Hold to End of Period
        final_close = float(eligible_days.iloc[-1]["Close"])
        bh_gross_ret = ((final_close - buy_price) / buy_price) * 100.0
        bh_net_ret = bh_gross_ret - self.slippage_penalty_pct
        bh_max_dd = ((eligible_days["Low"].min() - buy_price) / buy_price) * 100.0

        pnl_delta = llm_net_ret - bh_net_ret

        print("\n" + "=" * 95)
        print(f"📊 SINGLE-STOCK TIME MACHINE RESULTS COMPARISON: {self.ticker}")
        print("=" * 95)
        print(f"• Initial Buy:                 {buy_date_str} @ {buy_price:.2f}")
        print(f"• Buy & Hold End Date:         {trading_days[-1].strftime('%Y-%m-%d')} @ {final_close:.2f}")
        print(f"• Buy & Hold Net Return:       {bh_net_ret:+.2f}% (Max Intraday Drawdown: {bh_max_dd:.2f}%)")
        print("-" * 95)
        print(f"• Layer 2 LLM Exit Date:       {sell_date_str} @ {sell_price:.2f}")
        print(f"• Layer 2 Exit Reason:         {exit_reason}")
        print(f"• Layer 2 Holding Days:        {llm_holding_days} trading days")
        print(f"• Layer 2 Net Return:          {llm_net_ret:+.2f}%")
        print("-" * 95)
        if pnl_delta > 0:
            print(f"🏆 ALPHA GENERATED BY LAYER 2: +{pnl_delta:.2f}% (Saved Capital / Front-Ran Crash!)")
        else:
            print(f"⚠️ LAYER 2 PERFORMANCE DELTA:  {pnl_delta:+.2f}% vs. Buy & Hold")
        print("=" * 95 + "\n")

        return {
            "ticker": self.ticker,
            "buy_date": buy_date_str,
            "buy_price": buy_price,
            "llm_exit_date": sell_date_str,
            "llm_exit_price": sell_price,
            "llm_holding_days": llm_holding_days,
            "llm_net_return_pct": round(llm_net_ret, 2),
            "bh_end_date": trading_days[-1].strftime("%Y-%m-%d"),
            "bh_end_price": final_close,
            "bh_net_return_pct": round(bh_net_ret, 2),
            "alpha_saved_pct": round(pnl_delta, 2),
            "delta_pct": round(pnl_delta, 2),
            "exit_reason": exit_reason,
            "status": "SUCCESS",
        }


def main():
    parser = argparse.ArgumentParser(description="Single-Stock Time Machine: LLM News Radar (Layer 2) Backtester.")
    parser.add_argument("--ticker", default="SEZI.ST", help="Stock ticker symbol (default: SEZI.ST)")
    parser.add_argument("--buy-date", default="2026-02-16", help="Simulation start/buy date (default: 2026-02-16)")
    parser.add_argument("--start-date", default="2026-01-01", help="Price history fetch start date (default: 2026-01-01)")
    parser.add_argument("--end-date", default="2026-04-30", help="Price history fetch end date (default: 2026-04-30)")
    parser.add_argument("--news", default="data/historical_news_mock.csv", help="Path to historical news CSV")
    parser.add_argument("--slippage", type=float, default=0.50, help="Slippage penalty %% (default: 0.50%%)")

    args = parser.parse_args()

    backtester = SingleStockNewsBacktester(
        ticker=args.ticker,
        news_csv_path=args.news,
        buy_date=args.buy_date,
        start_date=args.start_date,
        end_date=args.end_date,
        slippage_penalty_pct=args.slippage,
    )
    backtester.run()


if __name__ == "__main__":
    main()
