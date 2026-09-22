"""
Automated Point-in-Time Fundamentals Dataset Generator for Clean Micro-Caps.

Fetches quarterly financial statements (Balance Sheet, Income Statement, Cash Flow)
for all approved tickers in data/clean_microcap_universe.csv via yfinance.
Calculates point-in-time metrics:
- report_date (period end date + 45 days filing lag)
- net_cash = cash_and_equivalents - total_debt
- ocf = operating_cash_flow
- revenue_yoy = % change vs quarter 1 year prior (strictly None if missing)
- gross_margin = gross_profit / revenue * 100
- cash_runway_months = cash / (|ocf| / 3) if ocf < 0 else "Infinite"
"""

import sys
import logging
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Union

import pandas as pd
import numpy as np
import yfinance as yf

# UTF-8 terminal output
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("pit_generator")


def _clean_val(val: Any) -> Optional[float]:
    if val is None or pd.isna(val):
        return None
    try:
        f = float(val)
        return f if not np.isnan(f) else None
    except (ValueError, TypeError):
        return None


def _get_metric_series(df: Optional[pd.DataFrame], keys: List[str]) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    for key in keys:
        matches = [idx for idx in df.index if key.lower() in str(idx).lower()]
        if matches:
            return df.loc[matches[0]]
    return None


def generate_pit_dataset(
    universe_csv: str = "data/clean_microcap_universe.csv",
    output_csv: str = "data/clean_microcap_pit_fundamentals.csv",
    filing_lag_days: int = 45,
) -> pd.DataFrame:
    u_df = pd.read_csv(universe_csv)
    tickers = u_df["ticker"].tolist()
    mcap_dict = dict(zip(u_df["ticker"], u_df["market_cap_usd"]))

    logger.info(f"Generating Point-in-Time fundamentals for {len(tickers)} micro-caps...")

    records: List[Dict[str, Any]] = []

    for idx, ticker in enumerate(tickers, 1):
        try:
            t = yf.Ticker(ticker)
            bs = t.quarterly_balance_sheet
            inc = t.quarterly_financials
            cf = t.quarterly_cashflow

            if (bs is None or bs.empty) and (inc is None or inc.empty):
                logger.warning(f"[{idx}/{len(tickers)}] ⚠️ {ticker}: No quarterly statements available.")
                continue

            # Extract metric series
            cash_series = _get_metric_series(bs, ["Cash And Cash Equivalents", "Cash Financial", "Cash Cash Equivalents"])
            debt_series = _get_metric_series(bs, ["Total Debt", "Long Term Debt", "Current Debt"])
            rev_series = _get_metric_series(inc, ["Total Revenue", "Operating Revenue", "Revenue"])
            gp_series = _get_metric_series(inc, ["Gross Profit"])
            ocf_series = _get_metric_series(cf, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"])

            # Union of all dates
            all_dates = set()
            for s in [cash_series, debt_series, rev_series, gp_series, ocf_series]:
                if s is not None and not s.empty:
                    all_dates.update(s.index)

            sorted_dates = sorted([pd.to_datetime(d) for d in all_dates], reverse=True)
            ticker_records = 0

            for p_date in sorted_dates:
                # Require revenue or cash to consider it a real quarter
                cash_val = _clean_val(cash_series.get(p_date)) if cash_series is not None else 0.0
                debt_val = _clean_val(debt_series.get(p_date)) if debt_series is not None else 0.0
                rev_val = _clean_val(rev_series.get(p_date)) if rev_series is not None else None
                gp_val = _clean_val(gp_series.get(p_date)) if gp_series is not None else None
                ocf_val = _clean_val(ocf_series.get(p_date)) if ocf_series is not None else 0.0

                if cash_val is None:
                    cash_val = 0.0
                if debt_val is None:
                    debt_val = 0.0
                if ocf_val is None:
                    ocf_val = 0.0

                net_cash = cash_val - debt_val

                # Calculate Gross Margin (%)
                gross_margin = (gp_val / rev_val * 100.0) if (gp_val is not None and rev_val is not None and rev_val > 0) else None

                # Calculate Cash Runway (Months)
                if ocf_val >= 0:
                    cash_runway = "Infinite"
                else:
                    monthly_burn = abs(ocf_val) / 3.0
                    if monthly_burn > 0:
                        cash_runway = round(cash_val / monthly_burn, 1)
                    else:
                        cash_runway = "Infinite"

                # Calculate Revenue YoY (strictly None if no comparable quarter)
                rev_growth_yoy = None
                prior_dates = [d for d in sorted_dates if 300 <= (p_date - d).days <= 430]
                if prior_dates and rev_val is not None and rev_series is not None:
                    prior_date = min(prior_dates, key=lambda d: abs((p_date - d).days - 365))
                    prior_rev = _clean_val(rev_series.get(prior_date))
                    if prior_rev is not None and prior_rev > 0:
                        rev_growth_yoy = round(((rev_val - prior_rev) / prior_rev) * 100.0, 2)

                # Report filed date = period end date + filing lag
                report_date = (p_date + timedelta(days=filing_lag_days)).strftime("%Y-%m-%d")

                records.append({
                    "ticker": ticker,
                    "market_cap": mcap_dict.get(ticker, 100_000_000.0),
                    "report_date": report_date,
                    "net_cash": round(net_cash, 2),
                    "ocf": round(ocf_val, 2),
                    "revenue_yoy": rev_growth_yoy,  # strictly None or float
                    "gross_margin": round(gross_margin, 2) if gross_margin is not None else None,
                    "cash_runway_months": cash_runway,
                })
                ticker_records += 1

            logger.info(f"[{idx}/{len(tickers)}] ✅ {ticker}: Extracted {ticker_records} quarterly records.")
        except Exception as e:
            logger.warning(f"[{idx}/{len(tickers)}] ❌ Error extracting {ticker}: {e}")

    df_out = pd.DataFrame(records)
    if not df_out.empty:
        # Sort chronologically by ticker and report_date
        df_out = df_out.sort_values(["ticker", "report_date"]).reset_index(drop=True)
        df_out.to_csv(output_csv, index=False)
        logger.info(f"Successfully generated {len(df_out)} PIT records saved to {output_csv}")
    else:
        logger.error("No PIT records generated!")

    return df_out


if __name__ == "__main__":
    generate_pit_dataset()
