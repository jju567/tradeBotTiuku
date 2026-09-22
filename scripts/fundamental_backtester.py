"""
Fundamental Financial Rules Quantitative Backtester.

Evaluates historical financial statements using yfinance and pandas,
isolating deterministic quantitative screening logic (Profile A & Profile B) from LLMs.
Detects historical quarters meeting survival and growth/value criteria and calculates
forward returns (3M / ~63 trading days, 6M / ~126 trading days) against a benchmark (e.g., ^RUT).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Union

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("fundamental_backtester")


# ----------------------------------------------------------------------
# DATA MODELS & RESULT STRUCTURES
# ----------------------------------------------------------------------

@dataclass
class FundamentalQuarter:
    ticker: str
    report_date: pd.Timestamp
    period_end_date: pd.Timestamp
    cash_and_equivalents: float
    total_debt: float
    net_cash: float
    operating_cash_flow: float
    total_revenue: float
    gross_profit: Optional[float]
    gross_margin_pct: Optional[float]
    revenue_growth_yoy_pct: Optional[float]
    ocf_growth_yoy_pct: Optional[float]
    cash_runway_months: Union[float, str]
    # Filter evaluations
    hard_filter_passed: bool
    is_profile_a: bool  # Growth
    is_profile_b: bool  # Value / Anti-Shrinking
    signal: str  # "BUY_PROFILE_A", "BUY_PROFILE_B", "BUY_BOTH", "REJECT"
    rejection_reasons: List[str]


@dataclass
class BacktestSignalResult:
    ticker: str
    report_date: str
    period_end_date: str
    signal: str
    profile: str
    entry_price: float
    # 3-Month Forward (approx 63 trading days)
    return_3m_pct: Optional[float]
    benchmark_3m_pct: Optional[float]
    excess_3m_pct: Optional[float]
    max_drawdown_3m_pct: Optional[float]
    # 6-Month Forward (approx 126 trading days)
    return_6m_pct: Optional[float]
    benchmark_6m_pct: Optional[float]
    excess_6m_pct: Optional[float]
    max_drawdown_6m_pct: Optional[float]
    # Fundamental summary metrics
    revenue_growth_yoy_pct: Optional[float]
    gross_margin_pct: Optional[float]
    net_cash: float
    cash_runway_months: Union[float, str]


# ----------------------------------------------------------------------
# HELPER FUNCTIONS FOR YFINANCE EXTRACTION
# ----------------------------------------------------------------------

def _get_metric_series(df: Optional[pd.DataFrame], keys: List[str]) -> Optional[pd.Series]:
    """Safely extracts a pandas Series from a yfinance statement DataFrame matching given keys."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    # Exact index match
    for key in keys:
        if key in df.index:
            try:
                s = df.loc[key]
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[0]
                return s
            except Exception:
                continue

    # Case-insensitive / substring match
    index_map = {str(k).strip().lower(): k for k in df.index}
    for key in keys:
        key_l = key.strip().lower()
        if key_l in index_map:
            orig_k = index_map[key_l]
            s = df.loc[orig_k]
            if isinstance(s, pd.DataFrame):
                s = s.iloc[0]
            return s

    return None


def _clean_val(val: Any) -> Optional[float]:
    """Converts a value to float if valid and not NaN/None."""
    if val is None or pd.isna(val):
        return None
    try:
        f = float(val)
        return f if not np.isnan(f) else None
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------
# CORE FUNDAMENTAL BACKTESTER CLASS
# ----------------------------------------------------------------------

class FundamentalBacktester:
    """
    Backtests deterministic financial quality and growth/value rules across quarterly statements.
    """

    CASH_KEYS = [
        "Cash And Cash Equivalents",
        "Cash Cash Equivalents And Short Term Investments",
        "Cash And Short Term Investments",
        "Cash Financial",
        "Cash",
    ]

    DEBT_KEYS = [
        "Total Debt",
        "Long Term Debt And Capital Lease Obligation",
        "Current Debt And Capital Lease Obligation",
        "Long Term Debt",
    ]

    OCF_KEYS = [
        "Operating Cash Flow",
        "Cash Flow From Continuing Operating Activities",
        "Operating Cashflow",
    ]

    REV_KEYS = [
        "Total Revenue",
        "Operating Revenue",
        "Revenue",
    ]

    GROSS_PROFIT_KEYS = [
        "Gross Profit",
        "Gross Margin",
    ]

    def __init__(
        self,
        tickers: List[str],
        benchmark_ticker: str = "^RUT",
        min_gross_margin_pct: float = 40.0,
        min_rev_growth_pct: float = 15.0,
        min_runway_months: float = 12.0,
        forward_days_3m: int = 63,
        forward_days_6m: int = 126,
        filing_lag_days: int = 45,
    ):
        self.tickers = [t.strip().upper() for t in tickers]
        self.benchmark_ticker = benchmark_ticker.strip().upper()
        self.min_gross_margin_pct = min_gross_margin_pct
        self.min_rev_growth_pct = min_rev_growth_pct
        self.min_runway_months = min_runway_months
        self.forward_days_3m = forward_days_3m
        self.forward_days_6m = forward_days_6m
        self.filing_lag_days = filing_lag_days

        self.benchmark_prices: Optional[pd.DataFrame] = None

    def fetch_price_history(self, ticker: str, start: Optional[datetime] = None) -> Optional[pd.DataFrame]:
        """Fetches daily historical price data from yfinance gracefully."""
        try:
            t = yf.Ticker(ticker)
            df = t.history(period="max" if start is None else "10y", auto_adjust=True)
            if df.empty:
                logger.warning(f"No price history found for {ticker}")
                return None
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.sort_index()
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch price history for {ticker}: {e}")
            return None

    def extract_quarterly_fundamentals(self, ticker: str) -> List[FundamentalQuarter]:
        """
        Extracts historical quarterly financials for balance sheet, cash flow, and income statement.
        Evaluates deterministic financial filters per quarter.
        """
        quarters: List[FundamentalQuarter] = []
        try:
            t = yf.Ticker(ticker)

            bs_q = getattr(t, "quarterly_balance_sheet", None)
            if bs_q is None or bs_q.empty:
                bs_q = getattr(t, "quarterly_balancesheet", None)

            cf_q = getattr(t, "quarterly_cashflow", None)
            if cf_q is None or cf_q.empty:
                cf_q = getattr(t, "quarterly_cash_flow", None)

            inc_q = getattr(t, "quarterly_financials", None)
            if inc_q is None or inc_q.empty:
                inc_q = getattr(t, "quarterly_income_stmt", None)

            if bs_q is None or bs_q.empty or inc_q is None or inc_q.empty:
                logger.warning(f"Insufficient quarterly financial statements for {ticker}")
                return quarters

            # Standardize date columns
            bs_cols = [pd.to_datetime(c).tz_localize(None) for c in bs_q.columns]
            bs_q.columns = bs_cols

            if cf_q is not None and not cf_q.empty:
                cf_cols = [pd.to_datetime(c).tz_localize(None) for c in cf_q.columns]
                cf_q.columns = cf_cols

            inc_cols = [pd.to_datetime(c).tz_localize(None) for c in inc_q.columns]
            inc_q.columns = inc_cols

            # Get series for each metric across quarters
            cash_series = _get_metric_series(bs_q, self.CASH_KEYS)
            debt_series = _get_metric_series(bs_q, self.DEBT_KEYS)
            ocf_series = _get_metric_series(cf_q, self.OCF_KEYS) if cf_q is not None else None
            rev_series = _get_metric_series(inc_q, self.REV_KEYS)
            gp_series = _get_metric_series(inc_q, self.GROSS_PROFIT_KEYS)

            # Dates available in income statement (sorted ascending)
            all_dates = sorted(list(inc_q.columns))

            for idx, p_date in enumerate(all_dates):
                rev_val = _clean_val(rev_series.get(p_date)) if rev_series is not None else None
                if rev_val is None or rev_val <= 0:
                    continue

                cash_val = _clean_val(cash_series.get(p_date)) if cash_series is not None else None
                if cash_val is None:
                    # Look for closest available balance sheet date within +/- 90 days
                    if cash_series is not None and not cash_series.empty:
                        close_dates = [d for d in cash_series.index if abs((d - p_date).days) <= 90]
                        if close_dates:
                            closest = min(close_dates, key=lambda d: abs((d - p_date).days))
                            cash_val = _clean_val(cash_series.get(closest))
                cash_val = cash_val if cash_val is not None else 0.0

                debt_val = _clean_val(debt_series.get(p_date)) if debt_series is not None else None
                if debt_val is None:
                    if debt_series is not None and not debt_series.empty:
                        close_dates = [d for d in debt_series.index if abs((d - p_date).days) <= 90]
                        if close_dates:
                            closest = min(close_dates, key=lambda d: abs((d - p_date).days))
                            debt_val = _clean_val(debt_series.get(closest))
                debt_val = debt_val if debt_val is not None else 0.0

                ocf_val = _clean_val(ocf_series.get(p_date)) if ocf_series is not None else None
                if ocf_val is None and ocf_series is not None and not ocf_series.empty:
                    close_dates = [d for d in ocf_series.index if abs((d - p_date).days) <= 90]
                    if close_dates:
                        closest = min(close_dates, key=lambda d: abs((d - p_date).days))
                        ocf_val = _clean_val(ocf_series.get(closest))
                ocf_val = ocf_val if ocf_val is not None else 0.0

                gp_val = _clean_val(gp_series.get(p_date)) if gp_series is not None else None

                # Calculate Net Cash
                net_cash = cash_val - debt_val

                # Calculate Gross Margin (%)
                gross_margin_pct = (gp_val / rev_val * 100.0) if (gp_val is not None and rev_val > 0) else None

                # Calculate Cash Runway (Months)
                if ocf_val >= 0:
                    cash_runway: Union[float, str] = "Infinite"
                    runway_months_num = float("inf")
                else:
                    # Quarterly OCF to monthly burn
                    monthly_burn = abs(ocf_val) / 3.0
                    if monthly_burn > 0:
                        runway_months_num = cash_val / monthly_burn
                        cash_runway = round(runway_months_num, 1)
                    else:
                        cash_runway = "Infinite"
                        runway_months_num = float("inf")

                # YoY comparisons (find quarter approx 365 days / 4 quarters prior)
                rev_growth_yoy_pct: Optional[float] = None
                ocf_growth_yoy_pct: Optional[float] = None

                # Find prior YoY date (around 300-430 days earlier)
                prior_dates = [d for d in all_dates if 300 <= (p_date - d).days <= 430]
                if prior_dates:
                    prior_date = min(prior_dates, key=lambda d: abs((p_date - d).days - 365))
                    prior_rev = _clean_val(rev_series.get(prior_date)) if rev_series is not None else None
                    if prior_rev is not None and prior_rev > 0:
                        rev_growth_yoy_pct = round(((rev_val - prior_rev) / prior_rev) * 100.0, 2)

                    if ocf_series is not None:
                        prior_ocf = _clean_val(ocf_series.get(prior_date))
                        if prior_ocf is not None:
                            ocf_growth_yoy_pct = round((ocf_val - prior_ocf), 2)

                # Report filed date approximation (period end date + filing lag, e.g. 45 days)
                report_date = p_date + timedelta(days=self.filing_lag_days)

                # ----------------------------------------------------
                # DETERMINISTIC RULES EVALUATION
                # ----------------------------------------------------
                rejections: List[str] = []

                # 1. Hard Filter: Reject period if cash_runway_months < 12
                hard_filter_passed = True
                if runway_months_num < self.min_runway_months:
                    hard_filter_passed = False
                    rejections.append(f"Cash runway {cash_runway}m < {self.min_runway_months}m")

                # 2. Profile A (Growth):
                # Revenue growth > 15% YoY and high gross margin (>= 40%)
                is_profile_a = False
                if hard_filter_passed:
                    growth_ok = (rev_growth_yoy_pct is not None and rev_growth_yoy_pct >= self.min_rev_growth_pct)
                    # If gross margin isn't explicitly reported, treat high-revenue scalable models cautiously
                    margin_ok = (gross_margin_pct is not None and gross_margin_pct >= self.min_gross_margin_pct) or (gross_margin_pct is None and growth_ok)
                    if growth_ok and margin_ok:
                        is_profile_a = True

                # 3. Profile B (Value / Anti-Shrinking):
                # Net Cash is positive (> 0)
                # Anti-Shrinking rule: REJECT if revenue YoY is negative AND OCF is not improving
                is_profile_b = False
                if hard_filter_passed and net_cash > 0:
                    revenue_shrinking = (rev_growth_yoy_pct is not None and rev_growth_yoy_pct < 0.0)
                    ocf_not_improving = (ocf_growth_yoy_pct is not None and ocf_growth_yoy_pct <= 0) or (ocf_growth_yoy_pct is None and ocf_val < 0)

                    if revenue_shrinking and ocf_not_improving:
                        rejections.append("Anti-Shrinking rule triggered (Revenue shrinking & OCF not improving)")
                    else:
                        is_profile_b = True

                # Determine Signal
                if is_profile_a and is_profile_b:
                    signal = "BUY_BOTH"
                elif is_profile_a:
                    signal = "BUY_PROFILE_A"
                elif is_profile_b:
                    signal = "BUY_PROFILE_B"
                else:
                    signal = "REJECT"

                quarter_entry = FundamentalQuarter(
                    ticker=ticker,
                    report_date=report_date,
                    period_end_date=p_date,
                    cash_and_equivalents=cash_val,
                    total_debt=debt_val,
                    net_cash=net_cash,
                    operating_cash_flow=ocf_val,
                    total_revenue=rev_val,
                    gross_profit=gp_val,
                    gross_margin_pct=gross_margin_pct,
                    revenue_growth_yoy_pct=rev_growth_yoy_pct,
                    ocf_growth_yoy_pct=ocf_growth_yoy_pct,
                    cash_runway_months=cash_runway,
                    hard_filter_passed=hard_filter_passed,
                    is_profile_a=is_profile_a,
                    is_profile_b=is_profile_b,
                    signal=signal,
                    rejection_reasons=rejections,
                )
                quarters.append(quarter_entry)

        except Exception as e:
            logger.warning(f"Error processing quarterly fundamentals for {ticker}: {e}")

        return quarters

    def _calculate_forward_return(
        self,
        prices_df: pd.DataFrame,
        start_date: pd.Timestamp,
        trading_days: int
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Calculates forward percentage return, entry price, and maximum drawdown over N trading days.
        """
        if prices_df is None or prices_df.empty:
            return None, None, None

        # Filter price history on or after start_date
        sub_df = prices_df[prices_df.index >= start_date]
        if sub_df.empty:
            return None, None, None

        entry_price = float(sub_df.iloc[0]["Close"])
        if entry_price <= 0:
            return None, None, None

        window_df = sub_df.iloc[: min(len(sub_df), trading_days + 1)]
        if len(window_df) < 2:
            return None, None, None

        exit_price = float(window_df.iloc[-1]["Close"])
        total_return_pct = round(((exit_price - entry_price) / entry_price) * 100.0, 2)

        # Max Drawdown during the window
        cummax = window_df["Close"].cummax()
        drawdown_series = (window_df["Close"] - cummax) / cummax * 100.0
        max_dd_pct = round(float(drawdown_series.min()), 2)

        return total_return_pct, entry_price, max_dd_pct

    def run(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Executes the full quantitative backtest over all tickers.
        Returns:
            (signals_df, summary_df)
        """
        logger.info(f"Starting Fundamental Backtest on {len(self.tickers)} tickers...")
        logger.info(f"Fetching benchmark ({self.benchmark_ticker}) historical prices...")
        self.benchmark_prices = self.fetch_price_history(self.benchmark_ticker)

        all_signals: List[BacktestSignalResult] = []

        for idx, ticker in enumerate(self.tickers, 1):
            logger.info(f"[{idx}/{len(self.tickers)}] Evaluating fundamentals for {ticker}...")
            price_df = self.fetch_price_history(ticker)
            if price_df is None or price_df.empty:
                logger.warning(f"Skipping {ticker} due to missing price data.")
                continue

            quarters = self.extract_quarterly_fundamentals(ticker)
            logger.info(f"  -> Extracted {len(quarters)} quarters for {ticker}")

            for q in quarters:
                # Check if quarter triggered a BUY signal
                if q.signal.startswith("BUY"):
                    # Calculate 3M forward return
                    ret_3m, entry_px, dd_3m = self._calculate_forward_return(
                        price_df, q.report_date, self.forward_days_3m
                    )
                    # Calculate 6M forward return
                    ret_6m, _, dd_6m = self._calculate_forward_return(
                        price_df, q.report_date, self.forward_days_6m
                    )

                    if entry_px is None:
                        # Report date might be in the future or no price available yet
                        continue

                    # Calculate Benchmark returns
                    bm_ret_3m, _, _ = (
                        self._calculate_forward_return(self.benchmark_prices, q.report_date, self.forward_days_3m)
                        if self.benchmark_prices is not None
                        else (None, None, None)
                    )
                    bm_ret_6m, _, _ = (
                        self._calculate_forward_return(self.benchmark_prices, q.report_date, self.forward_days_6m)
                        if self.benchmark_prices is not None
                        else (None, None, None)
                    )

                    excess_3m = round(ret_3m - bm_ret_3m, 2) if (ret_3m is not None and bm_ret_3m is not None) else None
                    excess_6m = round(ret_6m - bm_ret_6m, 2) if (ret_6m is not None and bm_ret_6m is not None) else None

                    profile_label = "PROFILE_A (Growth)" if q.signal == "BUY_PROFILE_A" else (
                        "PROFILE_B (Value)" if q.signal == "BUY_PROFILE_B" else "PROFILE_A & B"
                    )

                    res = BacktestSignalResult(
                        ticker=ticker,
                        report_date=q.report_date.strftime("%Y-%m-%d"),
                        period_end_date=q.period_end_date.strftime("%Y-%m-%d"),
                        signal=q.signal,
                        profile=profile_label,
                        entry_price=entry_px,
                        return_3m_pct=ret_3m,
                        benchmark_3m_pct=bm_ret_3m,
                        excess_3m_pct=excess_3m,
                        max_drawdown_3m_pct=dd_3m,
                        return_6m_pct=ret_6m,
                        benchmark_6m_pct=bm_ret_6m,
                        excess_6m_pct=excess_6m,
                        max_drawdown_6m_pct=dd_6m,
                        revenue_growth_yoy_pct=q.revenue_growth_yoy_pct,
                        gross_margin_pct=q.gross_margin_pct,
                        net_cash=q.net_cash,
                        cash_runway_months=q.cash_runway_months,
                    )
                    all_signals.append(res)

        signals_df = pd.DataFrame([asdict(s) for s in all_signals])
        summary_df = self._generate_summary(signals_df)
        return signals_df, summary_df

    def _generate_summary(self, signals_df: pd.DataFrame) -> pd.DataFrame:
        """Generates performance and benchmark comparison summary metrics."""
        if signals_df.empty:
            logger.warning("No BUY signals generated across the test universe.")
            return pd.DataFrame()

        profiles = ["ALL", "PROFILE_A (Growth)", "PROFILE_B (Value)"]
        summary_rows: List[Dict[str, Any]] = []

        for prof in profiles:
            if prof == "ALL":
                df_sub = signals_df
            else:
                df_sub = signals_df[signals_df["profile"].str.contains(prof.split()[0], regex=False)]

            if df_sub.empty:
                continue

            total_signals = len(df_sub)

            # 3M Metrics
            valid_3m = df_sub["return_3m_pct"].dropna()
            win_rate_3m = round((valid_3m > 0).mean() * 100.0, 2) if not valid_3m.empty else 0.0
            avg_ret_3m = round(valid_3m.mean(), 2) if not valid_3m.empty else 0.0
            median_ret_3m = round(valid_3m.median(), 2) if not valid_3m.empty else 0.0
            avg_max_dd_3m = round(df_sub["max_drawdown_3m_pct"].dropna().mean(), 2) if not df_sub["max_drawdown_3m_pct"].dropna().empty else 0.0

            valid_bm_3m = df_sub["benchmark_3m_pct"].dropna()
            avg_bm_3m = round(valid_bm_3m.mean(), 2) if not valid_bm_3m.empty else 0.0
            avg_excess_3m = round(df_sub["excess_3m_pct"].dropna().mean(), 2) if not df_sub["excess_3m_pct"].dropna().empty else 0.0

            # 6M Metrics
            valid_6m = df_sub["return_6m_pct"].dropna()
            win_rate_6m = round((valid_6m > 0).mean() * 100.0, 2) if not valid_6m.empty else 0.0
            avg_ret_6m = round(valid_6m.mean(), 2) if not valid_6m.empty else 0.0
            median_ret_6m = round(valid_6m.median(), 2) if not valid_6m.empty else 0.0
            avg_max_dd_6m = round(df_sub["max_drawdown_6m_pct"].dropna().mean(), 2) if not df_sub["max_drawdown_6m_pct"].dropna().empty else 0.0

            valid_bm_6m = df_sub["benchmark_6m_pct"].dropna()
            avg_bm_6m = round(valid_bm_6m.mean(), 2) if not valid_bm_6m.empty else 0.0
            avg_excess_6m = round(df_sub["excess_6m_pct"].dropna().mean(), 2) if not df_sub["excess_6m_pct"].dropna().empty else 0.0

            summary_rows.append({
                "Profile": prof,
                "Signals Count": total_signals,
                "3M Win Rate (%)": f"{win_rate_3m}%",
                "3M Avg Return (%)": f"{avg_ret_3m}%",
                "3M Median Return (%)": f"{median_ret_3m}%",
                "3M Benchmark Avg (%)": f"{avg_bm_3m}%",
                "3M Avg Alpha/Excess (%)": f"{avg_excess_3m}%",
                "3M Avg Max DD (%)": f"{avg_max_dd_3m}%",
                "6M Win Rate (%)": f"{win_rate_6m}%",
                "6M Avg Return (%)": f"{avg_ret_6m}%",
                "6M Median Return (%)": f"{median_ret_6m}%",
                "6M Benchmark Avg (%)": f"{avg_bm_6m}%",
                "6M Avg Alpha/Excess (%)": f"{avg_excess_6m}%",
                "6M Avg Max DD (%)": f"{avg_max_dd_6m}%",
            })

        return pd.DataFrame(summary_rows)


# ----------------------------------------------------------------------
# CLI ENTRY POINT
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fundamental Backtester: Quantitatively backtest Profile A & B deterministic filters."
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=["QTCOM.HE", "REMEDY.HE", "HARVIA.HE", "KAMUX.HE", "PLUG", "NVDA", "CELH"],
        help="List of stock tickers to backtest.",
    )
    parser.add_argument(
        "--benchmark",
        default="^RUT",
        help="Benchmark ticker symbol (default: ^RUT).",
    )
    parser.add_argument(
        "--min-growth",
        type=float,
        default=15.0,
        help="Minimum YoY revenue growth %% for Profile A (default: 15.0%%).",
    )
    parser.add_argument(
        "--min-margin",
        type=float,
        default=40.0,
        help="Minimum gross margin %% for Profile A (default: 40.0%%).",
    )
    parser.add_argument(
        "--min-runway",
        type=float,
        default=12.0,
        help="Minimum cash runway months before rejection (default: 12.0).",
    )
    parser.add_argument(
        "--output",
        default="data/fundamental_backtest_results.csv",
        help="Path to save signal results CSV.",
    )

    args = parser.parse_args()

    backtester = FundamentalBacktester(
        tickers=args.tickers,
        benchmark_ticker=args.benchmark,
        min_rev_growth_pct=args.min_growth,
        min_gross_margin_pct=args.min_margin,
        min_runway_months=args.min_runway,
    )

    signals_df, summary_df = backtester.run()

    print("\n" + "=" * 80)
    print("📈 FUNDAMENTAL BACKTESTER: SIGNALS SUMMARY")
    print("=" * 80)

    if not summary_df.empty:
        print(summary_df.to_string(index=False))
    else:
        print("No summary statistics available.")

    print("\n" + "=" * 80)
    print("🎯 INDIVIDUAL SIGNALS TABLE")
    print("=" * 80)

    if not signals_df.empty:
        display_cols = [
            "ticker", "report_date", "profile", "entry_price",
            "return_3m_pct", "excess_3m_pct", "max_drawdown_3m_pct",
            "return_6m_pct", "excess_6m_pct", "max_drawdown_6m_pct",
            "revenue_growth_yoy_pct", "cash_runway_months"
        ]
        available_cols = [c for c in display_cols if c in signals_df.columns]
        print(signals_df[available_cols].to_string(index=False))

        # Save to CSV
        try:
            signals_df.to_csv(args.output, index=False)
            print(f"\nSaved {len(signals_df)} signal records to {args.output}")
        except Exception as e:
            logger.error(f"Failed to save CSV to {args.output}: {e}")
    else:
        print("No signals generated.")


if __name__ == "__main__":
    main()
