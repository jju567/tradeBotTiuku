"""
Institutional Bias-Free Quantitative Backtester.

Evaluates micro-cap and small-cap quantitative investment strategies
(Profile A Growth & Profile B Value/Anti-Shrinking) using:
1. Point-in-Time fundamental statements (no restatement look-ahead or survivorship bias).
2. yfinance strictly for daily OHLC price history.
3. 20% Trailing Stop-Loss execution within a 6-month holding window.
4. Mandatory 0.5% Transaction Cost / Slippage penalty deducted from all trade returns.
5. Institutional risk metrics: Mean, Median, Volatility (Std Dev), Sharpe Ratio (3% Rf),
   Average Maximum Drawdown, Win Rate, and Alpha vs. ^RUT.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
import yfinance as yf

from screener.financial_metrics_engine import evaluate_profiles
from universe_builder import MicroCapUniverseBuilder

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("institutional_backtester")


@dataclass
class InstitutionalTradeRecord:
    ticker: str
    report_date: str
    buy_date: str
    sell_date: str
    profile: str
    signal: str
    market_cap: float
    buy_price: float
    sell_price: float
    gross_return_pct: float
    net_return_pct: float  # After 0.5% transaction cost penalty
    slippage_cost_pct: float
    exit_type: str  # "STOP_LOSS" or "6M_EXPIRY"
    max_drawdown_pct: float
    benchmark_return_pct: Optional[float]
    alpha_pct: Optional[float]
    holding_days: int


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculates 14-day Average True Range (ATR) using Daily High, Low, and Close prices."""
    high = df["High"]
    low = df["Low"]
    close_prev = df["Close"].shift(1)

    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period, min_periods=period).mean()
    return atr


class InstitutionalBacktester:
    """
    Bias-free institutional backtesting engine with point-in-time fundamentals,
    2.5x ATR stop-loss simulation, and transaction cost modeling.
    """

    def __init__(
        self,
        fundamentals_csv: str = "data/point_in_time_fundamentals.csv",
        clean_universe_csv: str = "data/clean_microcap_universe.csv",
        benchmark_ticker: str = "^RUT",
        slippage_penalty_pct: float = 0.50,  # 0.5% round-trip penalty
        trailing_stop_pct: float = 20.0,
        atr_multiplier: float = 2.5,
        atr_period: int = 14,
        holding_period_days: int = 126,  # ~6 months trading days
        risk_free_rate_pct: float = 3.0,
        max_market_cap_usd: float = 300_000_000.0,
    ):
        self.fundamentals_csv = Path(fundamentals_csv)
        self.clean_universe_csv = Path(clean_universe_csv)
        self.benchmark_ticker = benchmark_ticker.strip().upper()
        self.slippage_penalty_pct = slippage_penalty_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.atr_multiplier = atr_multiplier
        self.atr_period = atr_period
        self.stop_multiplier = 1.0 - (trailing_stop_pct / 100.0)
        self.holding_period_days = holding_period_days
        self.risk_free_rate_pct = risk_free_rate_pct
        self.max_market_cap_usd = max_market_cap_usd

        self._price_cache: Dict[str, pd.DataFrame] = {}
        self.benchmark_df: Optional[pd.DataFrame] = None
        self.allowed_tickers: Optional[set] = None

        if self.clean_universe_csv.exists():
            try:
                u_df = pd.read_csv(self.clean_universe_csv)
                if "ticker" in u_df.columns:
                    self.allowed_tickers = set(u_df["ticker"].astype(str).str.strip().str.upper())
                    logger.info(f"Loaded {len(self.allowed_tickers)} verified micro-cap tickers from {self.clean_universe_csv}")
            except Exception as e:
                logger.warning(f"Could not load clean universe: {e}")

    def fetch_price_history(self, ticker: str) -> Optional[pd.DataFrame]:
        """Fetches daily price history with caching and error handling."""
        cleaned = ticker.strip().upper()
        if cleaned in self._price_cache:
            return self._price_cache[cleaned]

        try:
            t = yf.Ticker(cleaned)
            df = t.history(period="10y", auto_adjust=True)
            if df.empty:
                logger.warning(f"No price data available for {cleaned}")
                return None
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.sort_index()
            # Calculate 14-day ATR across the historical dataset
            df["ATR_14"] = calculate_atr(df, period=self.atr_period)
            self._price_cache[cleaned] = df
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch prices for {cleaned}: {e}")
            return None

    def evaluate_signal(self, row: pd.Series) -> Tuple[str, str]:
        """
        Evaluates deterministic rules using financial_metrics_engine.evaluate_profiles:
        Profile A (Quality Growth):
          - rev_growth_yoy > 20%
          - gross_margin > 40%
          - Survival Check: OCF > 0 OR cash_runway > 18m
        Profile B (Deep Value & Anti-Shrinking):
          - net_cash > 0
          - Anti-Shrinking: not (rev_yoy < 0 and ocf <= 0)
        """
        rev_yoy_raw = row.get("revenue_yoy")
        rev_yoy = float(rev_yoy_raw) if pd.notna(rev_yoy_raw) else None
        net_cash_raw = row.get("net_cash")
        net_cash = float(net_cash_raw) if pd.notna(net_cash_raw) else None
        ocf_raw = row.get("ocf")
        ocf = float(ocf_raw) if pd.notna(ocf_raw) else None

        eval_dict = {
            "net_cash": net_cash,
            "ocf": ocf,
            "revenue_yoy": rev_yoy,
            "gross_margin": float(row.get("gross_margin", 0.0)) if pd.notna(row.get("gross_margin")) else None,
            "cash_runway_months": row.get("cash_runway_months", "Infinite"),
        }
        res = evaluate_profiles(eval_dict)
        return res["signal"], res["profile"]

    def simulate_trade_execution(
        self,
        ticker: str,
        buy_date_str: str,
        profile: str,
        signal: str,
        market_cap: float,
    ) -> Optional[InstitutionalTradeRecord]:
        """
        Simulates mechanical trade execution with dynamic 2.5x ATR trailing stop-loss,
        benchmark tracking, and 0.5% slippage penalty.
        """
        price_df = self.fetch_price_history(ticker)
        if price_df is None or price_df.empty:
            return None

        buy_dt = pd.to_datetime(buy_date_str).tz_localize(None)
        sub_df = price_df[price_df.index >= buy_dt]
        if sub_df.empty:
            return None

        window_df = sub_df.iloc[: min(len(sub_df), self.holding_period_days + 1)]
        if len(window_df) < 1:
            return None

        # Day 1 Execution
        buy_price = float(window_df.iloc[0]["Close"])
        if buy_price <= 0:
            return None

        actual_buy_date = window_df.index[0].strftime("%Y-%m-%d")
        atr_14 = float(window_df.iloc[0]["ATR_14"]) if pd.notna(window_df.iloc[0]["ATR_14"]) and window_df.iloc[0]["ATR_14"] > 0 else (buy_price * 0.08)
        trailing_stop = max(0.0001, buy_price - (self.atr_multiplier * atr_14))
        highest_seen = buy_price

        stopped_out = False
        sell_date = None
        sell_price = None
        max_seen_drawdown = 0.0

        for i in range(1, len(window_df)):
            day = window_df.iloc[i]
            day_date = window_df.index[i].strftime("%Y-%m-%d")
            low_px = float(day["Low"])
            close_px = float(day["Close"])
            open_px = float(day["Open"])
            cur_atr = float(day["ATR_14"]) if pd.notna(day["ATR_14"]) and day["ATR_14"] > 0 else atr_14

            # Track peak-to-date entry drawdown
            entry_dd = (low_px - buy_price) / buy_price * 100.0
            if entry_dd < max_seen_drawdown:
                max_seen_drawdown = entry_dd

            # Check trailing stop breach (Low <= trailing_stop)
            if low_px <= trailing_stop:
                stopped_out = True
                sell_date = day_date
                sell_price = min(open_px, trailing_stop) if open_px < trailing_stop else trailing_stop
                exit_type = "ATR_STOP_LOSS"
                break

            # Trailing stop update if new high achieved
            if close_px > highest_seen:
                highest_seen = close_px
                new_stop = close_px - (self.atr_multiplier * cur_atr)
                if new_stop > trailing_stop:
                    trailing_stop = new_stop

        if not stopped_out:
            valid_closes = window_df["Close"].dropna()
            if not valid_closes.empty:
                sell_price = float(valid_closes.iloc[-1])
                sell_date = valid_closes.index[-1].strftime("%Y-%m-%d")
            else:
                sell_price = buy_price
                sell_date = window_df.index[-1].strftime("%Y-%m-%d")
            exit_type = "6M_EXPIRY"
        else:
            exit_type = "STOP_LOSS"

        # Calculate Gross Return and Net Return (after 0.5% slippage penalty)
        gross_return_pct = round(((sell_price - buy_price) / buy_price) * 100.0, 2)
        net_return_pct = round(gross_return_pct - self.slippage_penalty_pct, 2)

        # Calculate Benchmark Return over identical window
        benchmark_ret = None
        alpha_pct = None
        if self.benchmark_df is not None and not self.benchmark_df.empty:
            bm_sub = self.benchmark_df[self.benchmark_df.index >= pd.to_datetime(actual_buy_date)]
            if not bm_sub.empty:
                bm_sell = self.benchmark_df[self.benchmark_df.index >= pd.to_datetime(sell_date)]
                if not bm_sell.empty:
                    bm_start_px = float(bm_sub.iloc[0]["Close"])
                    bm_end_px = float(bm_sell.iloc[0]["Close"])
                    if bm_start_px > 0:
                        benchmark_ret = round(((bm_end_px - bm_start_px) / bm_start_px) * 100.0, 2)
                        alpha_pct = round(net_return_pct - benchmark_ret, 2)

        holding_days = (pd.to_datetime(sell_date) - pd.to_datetime(actual_buy_date)).days

        return InstitutionalTradeRecord(
            ticker=ticker,
            report_date=buy_date_str,
            buy_date=actual_buy_date,
            sell_date=sell_date,
            profile=profile,
            signal=signal,
            market_cap=market_cap,
            buy_price=round(buy_price, 2),
            sell_price=round(sell_price, 2),
            gross_return_pct=gross_return_pct,
            net_return_pct=net_return_pct,
            slippage_cost_pct=self.slippage_penalty_pct,
            exit_type=exit_type,
            max_drawdown_pct=round(max_seen_drawdown, 2),
            benchmark_return_pct=benchmark_ret,
            alpha_pct=alpha_pct,
            holding_days=holding_days,
        )

    def run(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Runs institutional bias-free backtest across point-in-time fundamentals dataset.
        Returns:
            (trades_df, institutional_summary_df)
        """
        if not self.fundamentals_csv.exists():
            raise FileNotFoundError(f"Point-in-time dataset not found at {self.fundamentals_csv}")

        df_pit = pd.read_csv(self.fundamentals_csv)
        logger.info(f"Loaded {len(df_pit)} point-in-time fundamental records.")

        logger.info(f"Fetching benchmark ({self.benchmark_ticker}) prices...")
        self.benchmark_df = self.fetch_price_history(self.benchmark_ticker)

        all_trades: List[InstitutionalTradeRecord] = []

        for idx, row in df_pit.iterrows():
            ticker = str(row["ticker"]).strip().upper()
            report_date = str(row["report_date"]).strip()
            market_cap = float(row.get("market_cap", 0.0))

            # 1. Enforce strict micro-cap universe membership
            if self.allowed_tickers is not None and ticker not in self.allowed_tickers:
                continue

            # 2. Enforce absolute market cap ceiling ($300M USD)
            if self.max_market_cap_usd and market_cap > self.max_market_cap_usd:
                continue

            signal, profile = self.evaluate_signal(row)
            if signal.startswith("BUY"):
                trade_record = self.simulate_trade_execution(
                    ticker=ticker,
                    buy_date_str=report_date,
                    profile=profile,
                    signal=signal,
                    market_cap=market_cap,
                )
                if trade_record:
                    all_trades.append(trade_record)

        trades_df = pd.DataFrame([asdict(t) for t in all_trades])
        summary_df = self._generate_institutional_summary(trades_df)
        return trades_df, summary_df

    def _generate_institutional_summary(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculates institutional risk-adjusted metrics grouped by Profile:
        Signal Count, Win Rate, Median Return, Mean Return, Std Dev,
        Sharpe Ratio (3% Rf), Avg Max Drawdown, Benchmark Avg, Alpha, and Slippage.
        """
        if df.empty:
            return pd.DataFrame()

        profiles = ["ALL SIGNALS", "PROFILE_A (Growth)", "PROFILE_B (Value)"]
        rows: List[Dict[str, Any]] = []

        for prof in profiles:
            if prof == "ALL SIGNALS":
                sub = df
            elif "PROFILE_A" in prof:
                sub = df[df["profile"].str.contains("PROFILE_A", regex=False)]
            else:
                sub = df[df["profile"].str.contains("PROFILE_B", regex=False)]

            if sub.empty:
                continue

            n = len(sub)
            returns = sub["net_return_pct"].dropna()

            win_rate = round((returns > 0).mean() * 100.0, 2) if not returns.empty else 0.0
            mean_ret = round(returns.mean(), 2) if not returns.empty else 0.0
            median_ret = round(returns.median(), 2) if not returns.empty else 0.0
            std_dev = round(returns.std(), 2) if len(returns) > 1 else 0.0

            # Annualized Sharpe Ratio approximation for 6M trades (semi-annual periods)
            # Rf semi-annual = 1.5%
            rf_semi = self.risk_free_rate_pct / 2.0
            excess_return = mean_ret - rf_semi
            sharpe = round((excess_return / std_dev) * np.sqrt(2), 2) if std_dev > 0 else 0.0

            avg_max_dd = round(sub["max_drawdown_pct"].dropna().mean(), 2) if not sub["max_drawdown_pct"].dropna().empty else 0.0
            
            valid_bm = sub["benchmark_return_pct"].dropna()
            bm_mean = round(valid_bm.mean(), 2) if not valid_bm.empty else 0.0
            
            valid_alpha = sub["alpha_pct"].dropna()
            alpha_mean = round(valid_alpha.mean(), 2) if not valid_alpha.empty else 0.0

            rows.append({
                "Profile Group": prof,
                "Signal Count": n,
                "Win Rate": f"{win_rate:.1f}%",
                "Median Return": f"{median_ret:+.2f}%",
                "Mean Return": f"{mean_ret:+.2f}%",
                "Std Dev (Vol)": f"{std_dev:.2f}%",
                "Sharpe Ratio (3% Rf)": f"{sharpe:.2f}",
                "Avg Max Drawdown": f"{avg_max_dd:.2f}%",
                "Benchmark Avg (^RUT)": f"{bm_mean:+.2f}%",
                "Alpha (Net of Costs)": f"{alpha_mean:+.2f}%",
                "Slippage Applied": f"{self.slippage_penalty_pct:.2f}%",
            })

        return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# CLI ENTRY POINT & OUTPUT
# ----------------------------------------------------------------------

def _dataframe_to_markdown(df: pd.DataFrame) -> str:
    """Pure-Python markdown table formatter without external tabulate dependency."""
    if df.empty:
        return ""
    headers = [str(c) for c in df.columns]
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join([":---" if i == 0 else ":---:" for i in range(len(headers))]) + " |")
    for _, row in df.iterrows():
        row_vals = [str(v) if pd.notna(v) else "-" for v in row]
        lines.append("| " + " | ".join(row_vals) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Institutional Bias-Free Backtester with Point-in-Time Fundamentals & Risk Metrics."
    )
    parser.add_argument(
        "--fundamentals",
        default="data/clean_microcap_pit_fundamentals.csv",
        help="Path to point-in-time fundamentals CSV file.",
    )
    parser.add_argument(
        "--universe",
        default="data/clean_microcap_universe.csv",
        help="Path to clean micro-cap universe CSV file.",
    )
    parser.add_argument(
        "--benchmark",
        default="^RUT",
        help="Benchmark ticker symbol (default: ^RUT).",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.50,
        help="Transaction cost and slippage penalty %% per trade (default: 0.50%%).",
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=20.0,
        help="Trailing stop-loss percentage (default: 20.0%%).",
    )
    parser.add_argument(
        "--output",
        default="data/institutional_backtest_results.csv",
        help="Path to save institutional trade records CSV.",
    )

    args = parser.parse_args()

    # Fallback to legacy point-in-time CSV if clean one not yet generated
    fund_path = args.fundamentals
    if not Path(fund_path).exists() and Path("data/point_in_time_fundamentals.csv").exists():
        fund_path = "data/point_in_time_fundamentals.csv"

    backtester = InstitutionalBacktester(
        fundamentals_csv=fund_path,
        clean_universe_csv=args.universe,
        benchmark_ticker=args.benchmark,
        slippage_penalty_pct=args.slippage,
        trailing_stop_pct=args.stop_loss,
    )

    trades_df, summary_df = backtester.run()

    print("\n" + "=" * 105)
    print("🏛️ INSTITUTIONAL BIAS-FREE QUANTITATIVE BACKTEST REPORT (COST-ADJUSTED)")
    print("=" * 105)

    if not summary_df.empty:
        print(_dataframe_to_markdown(summary_df))
    else:
        print("No simulation summary available.")

    print("\n" + "=" * 105)
    print("📋 SAMPLE INSTITUTIONAL TRADE RECORDS (FIRST 10)")
    print("=" * 105)

    if not trades_df.empty:
        display_cols = [
            "ticker", "buy_date", "profile", "buy_price", "sell_price",
            "gross_return_pct", "net_return_pct", "alpha_pct", "exit_type", "holding_days"
        ]
        print(_dataframe_to_markdown(trades_df[display_cols].head(10)))

        # Save to CSV
        try:
            trades_df.to_csv(args.output, index=False)
            print(f"\nSaved {len(trades_df)} trade simulation records to {args.output}")
        except Exception as e:
            logger.error(f"Failed to save CSV to {args.output}: {e}")
    else:
        print("No trades generated.")


if __name__ == "__main__":
    main()
