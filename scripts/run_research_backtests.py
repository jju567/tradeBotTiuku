#!/usr/bin/env python3
"""
scripts/run_research_backtests.py — Four Comprehensive Quantitative Research Tests
1. P12 Breakout Backtest (Volume >= 3 * ADV_20) on 288 stocks (26.8.–5.10.)
2. Trailing Stop Parameter Sweep (4%, 6%, 8%, 10%) on Oracle 23 Winners
3. Portfolio Overlap Matrix across P1–P11
4. Realistic Trading Friction (Spread & Slippage Stress Test) on P1–P11
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Optional

import numpy as np
import pandas as pd
import yfinance as yf

# Configure encoding for Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ResearchSuite")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UNIVERSE_CSV = DATA_DIR / "clean_microcap_universe.csv"
ORACLE_TRADES_CSV = DATA_DIR / "oracle_trades.csv"
PORTFOLIOS_DIR = DATA_DIR / "portfolios"


# ==============================================================================
# COMMON MARKET DATA LOADER
# ==============================================================================

def backadjust_unadjusted_splits(close_df: pd.DataFrame) -> pd.DataFrame:
    df = close_df.copy()
    for sym in df.columns:
        if sym in ["EURUSD=X", "EURSEK=X"]:
            continue
        s_clean = df[sym].dropna()
        if len(s_clean) < 2:
            continue
        col_ratios = s_clean / s_clean.shift(1)
        if (col_ratios > 2.0).any() or (col_ratios < 0.5).any():
            try:
                t = yf.Ticker(sym)
                sp = t.splits
                if sp.empty:
                    continue
                s = df[sym]
                for sp_dt, factor in sp.items():
                    if factor <= 0 or factor == 1.0:
                        continue
                    sp_date = pd.Timestamp(sp_dt).tz_convert(None).floor("D") if getattr(sp_dt, "tz", None) else pd.Timestamp(sp_dt).floor("D")
                    pre_dates = s.index[s.index < sp_date]
                    valid_pre = s.loc[pre_dates].dropna()
                    valid_post = s.loc[s.index >= sp_date].dropna()
                    if not valid_pre.empty and not valid_post.empty:
                        ratio = float(valid_post.iloc[0]) / float(valid_pre.iloc[-1])
                        if (factor < 1.0 and ratio > 2.0) or (factor > 1.0 and ratio < 0.5):
                            df.loc[pre_dates, sym] = df.loc[pre_dates, sym] / factor
            except Exception:
                pass
    return df


def load_universe_data() -> Tuple[List[str], Dict[str, str], Dict[str, float]]:
    df = pd.read_csv(UNIVERSE_CSV)
    tickers = [str(t).strip().upper() for t in df["ticker"].dropna().unique()]
    currency_map = {}
    adv_map = {}
    for _, row in df.iterrows():
        t = str(row["ticker"]).strip().upper()
        c = str(row.get("currency", "EUR")).strip().upper()
        currency_map[t] = c if c in ["EUR", "USD", "SEK"] else "EUR"
        adv_map[t] = float(row.get("adv_20d_local", 0.0) or row.get("adv_20d_usd", 0.0) or 0.0)
    return tickers, currency_map, adv_map


def fetch_extended_market_data(tickers: List[str]) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    fx_symbols = ["EURUSD=X", "EURSEK=X"]
    all_symbols = list(set(tickers + fx_symbols))
    logger.info(f"Downloading historical data for {len(all_symbols)} symbols (period=3mo)...")
    data = yf.download(
        all_symbols,
        period="3mo",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=True,
    )
    price_col = "Adj Close" if "Adj Close" in data else "Close"
    close_df = data[price_col]
    volume_df = data["Volume"] if "Volume" in data else pd.DataFrame(index=close_df.index)
    close_df = backadjust_unadjusted_splits(close_df)

    eurusd = close_df["EURUSD=X"].ffill().bfill() if "EURUSD=X" in close_df else pd.Series(1.08, index=close_df.index)
    eursek = close_df["EURSEK=X"].ffill().bfill() if "EURSEK=X" in close_df else pd.Series(11.30, index=close_df.index)

    return close_df, volume_df, eurusd, eursek


# ==============================================================================
# TEST 1: P12 BREAKOUT BACKTEST (Volume >= 3 * ADV_20)
# ==============================================================================

def run_test_1_breakout_backtest(
    tickers: List[str],
    close_df: pd.DataFrame,
    volume_df: pd.DataFrame,
    adv_map: Dict[str, float],
    start_date: str = "2026-08-26",
    end_date: str = "2026-10-06",
) -> Dict[str, Any]:
    logger.info(f"--- RUNNING TEST 1: P12 Breakout Backtest ({start_date} -> {end_date}) ---")

    # Dates filtering
    trading_dates = close_df.dropna(how="all").index
    # Clean tz if any
    trading_dates = [pd.Timestamp(d).tz_convert(None).floor("D") if getattr(d, "tz", None) else pd.Timestamp(d).floor("D") for d in trading_dates]
    close_df.index = trading_dates
    volume_df.index = trading_dates

    # Rolling 20-day ADV (volume in shares)
    adv_20d_shares = volume_df.rolling(window=20, min_periods=5).mean().shift(1)

    signals_all = []
    signals_p12_filtered = []

    ts_start = pd.Timestamp(start_date)
    ts_end = pd.Timestamp(end_date)
    eval_dates = [d for d in trading_dates if ts_start <= d <= ts_end]

    for d in eval_dates:
        d_idx = trading_dates.index(d)
        for sym in tickers:
            if sym not in close_df or sym not in volume_df:
                continue

            v_today = volume_df.loc[d, sym]
            p_today = close_df.loc[d, sym]
            adv_shares = adv_20d_shares.loc[d, sym]

            if np.isnan(v_today) or np.isnan(p_today) or p_today <= 0 or np.isnan(adv_shares) or adv_shares <= 0:
                continue

            # Prior day close
            if d_idx > 0:
                p_prev = close_df.iloc[d_idx - 1][sym]
            else:
                p_prev = np.nan

            if np.isnan(p_prev) or p_prev <= 0:
                continue

            day_change_pct = ((p_today - p_prev) / p_prev) * 100.0
            vol_surge = v_today / adv_shares
            day_volume_curr = v_today * p_today
            adv_curr = adv_shares * p_prev

            # Rule 1: Volume >= 3.0 * ADV_20
            if vol_surge >= 3.0:
                # Track future performance over 1d, 5d, 10d, 21d
                future_dates = trading_dates[d_idx + 1 : d_idx + 22]
                future_prices = [close_df.iloc[trading_dates.index(fd)][sym] for fd in future_dates if sym in close_df]
                future_prices = [p for p in future_prices if not np.isnan(p) and p > 0]

                ret_1d = ((future_prices[0] - p_today) / p_today) * 100.0 if len(future_prices) >= 1 else np.nan
                ret_5d = ((future_prices[4] - p_today) / p_today) * 100.0 if len(future_prices) >= 5 else (
                    ((future_prices[-1] - p_today) / p_today) * 100.0 if len(future_prices) >= 1 else np.nan
                )
                ret_10d = ((future_prices[9] - p_today) / p_today) * 100.0 if len(future_prices) >= 10 else (
                    ((future_prices[-1] - p_today) / p_today) * 100.0 if len(future_prices) >= 1 else np.nan
                )
                ret_21d = ((future_prices[-1] - p_today) / p_today) * 100.0 if len(future_prices) >= 1 else np.nan
                max_runup = max([((fp - p_today) / p_today) * 100.0 for fp in future_prices]) if future_prices else 0.0
                max_drawdown = min([((fp - p_today) / p_today) * 100.0 for fp in future_prices]) if future_prices else 0.0

                sig_record = {
                    "date": d.strftime("%Y-%m-%d"),
                    "ticker": sym,
                    "price": round(float(p_today), 4),
                    "day_change_pct": round(float(day_change_pct), 2),
                    "vol_surge": round(float(vol_surge), 2),
                    "volume_today": int(v_today),
                    "adv_curr": round(float(adv_curr), 0),
                    "ret_1d": round(float(ret_1d), 2) if not np.isnan(ret_1d) else None,
                    "ret_5d": round(float(ret_5d), 2) if not np.isnan(ret_5d) else None,
                    "ret_10d": round(float(ret_10d), 2) if not np.isnan(ret_10d) else None,
                    "ret_21d": round(float(ret_21d), 2) if not np.isnan(ret_21d) else None,
                    "max_runup": round(float(max_runup), 2),
                    "max_drawdown": round(float(max_drawdown), 2),
                }
                signals_all.append(sig_record)

                # Rule 2: Strict P12 filter:
                # - Volume >= 3.0 * ADV
                # - Day change >= +2.0%
                # - adv_curr >= 50,000 (Liquidity guard)
                # - day_change_pct <= +300% (Glitch guard)
                if day_change_pct >= 2.0 and adv_curr >= 50000.0 and day_change_pct <= 300.0:
                    signals_p12_filtered.append(sig_record)

    df_all = pd.DataFrame(signals_all)
    df_p12 = pd.DataFrame(signals_p12_filtered)

    # Check key winners
    key_winners = ["NANEXA.ST", "ELON.ST", "FLUX"]
    winner_catches = {}
    for kw in key_winners:
        kw_sigs = [s for s in signals_p12_filtered if s["ticker"] == kw]
        winner_catches[kw] = kw_sigs

    return {
        "total_raw_signals": len(df_all),
        "unique_raw_tickers": df_all["ticker"].nunique() if not df_all.empty else 0,
        "total_p12_signals": len(df_p12),
        "unique_p12_tickers": df_p12["ticker"].nunique() if not df_p12.empty else 0,
        "winner_catches": winner_catches,
        "p12_df": df_p12,
        "raw_df": df_all,
    }


# ==============================================================================
# TEST 2: TRAILING STOP PARAMETER SWEEP ON ORACLE 23 WINNERS
# ==============================================================================

def run_test_2_trailing_stop_sweep(
    oracle_trades_df: pd.DataFrame,
    close_df: pd.DataFrame,
    stop_levels: List[float] = [0.04, 0.06, 0.08, 0.10],
) -> Dict[str, Any]:
    logger.info("--- RUNNING TEST 2: Trailing Stop Sweep on Oracle 23 Winners ---")

    trading_dates = list(close_df.index)
    results_by_level = {level: [] for level in stop_levels}

    for _, trade in oracle_trades_df.iterrows():
        sym = trade["ticker"]
        entry_date_str = str(trade["entry_date"])[:10]
        oracle_exit_str = str(trade["exit_date"])[:10]
        oracle_ret = float(trade["ret"])

        ts_entry = pd.Timestamp(entry_date_str)
        if ts_entry not in trading_dates:
            # find closest >=
            valid_ents = [d for d in trading_dates if d >= ts_entry]
            if not valid_ents:
                continue
            ts_entry = valid_ents[0]

        start_idx = trading_dates.index(ts_entry)
        if sym not in close_df:
            continue

        p_entry = close_df.iloc[start_idx][sym]
        if np.isnan(p_entry) or p_entry <= 0:
            continue

        for level in stop_levels:
            peak_price = p_entry
            exit_date = None
            exit_price = None
            exit_reason = "HOLD_TO_END"

            for cur_idx in range(start_idx, len(trading_dates)):
                cur_date = trading_dates[cur_idx]
                p_cur = close_df.iloc[cur_idx][sym]
                if np.isnan(p_cur) or p_cur <= 0:
                    continue

                if p_cur > peak_price:
                    peak_price = p_cur

                dd_from_peak = (peak_price - p_cur) / peak_price

                # Check if trailing stop hit (strictly on daily Close)
                if dd_from_peak >= level and cur_idx > start_idx:
                    exit_date = cur_date
                    exit_price = p_cur
                    exit_reason = f"TRAILING_STOP_{int(level*100)}%"
                    break

            if exit_date is None:
                # Still held at the end of the data
                exit_date = trading_dates[-1]
                exit_price = close_df.iloc[-1][sym]

            ret = (exit_price - p_entry) / p_entry
            holding_days = (exit_date - ts_entry).days

            results_by_level[level].append({
                "ticker": sym,
                "entry_date": ts_entry.strftime("%Y-%m-%d"),
                "exit_date": exit_date.strftime("%Y-%m-%d"),
                "oracle_exit_date": oracle_exit_str,
                "entry_price": p_entry,
                "exit_price": exit_price,
                "peak_price": peak_price,
                "holding_days": holding_days,
                "ret_pct": round(ret * 100.0, 2),
                "oracle_ret_pct": round(oracle_ret * 100.0, 2),
                "profit_eur_2000": round(2000.0 * ret, 2),
                "exit_reason": exit_reason,
            })

    summary_by_level = {}
    for level, rows in results_by_level.items():
        df_lvl = pd.DataFrame(rows)
        wins = df_lvl[df_lvl["ret_pct"] > 0]
        summary_by_level[level] = {
            "total_trades": len(df_lvl),
            "win_rate_pct": round((len(wins) / max(len(df_lvl), 1)) * 100.0, 1),
            "avg_return_pct": round(df_lvl["ret_pct"].mean(), 2),
            "median_return_pct": round(df_lvl["ret_pct"].median(), 2),
            "total_profit_eur": round(df_lvl["profit_eur_2000"].sum(), 2),
            "avg_holding_days": round(df_lvl["holding_days"].mean(), 1),
            "nanexa_ret_pct": df_lvl[df_lvl["ticker"] == "NANEXA.ST"]["ret_pct"].values[0] if (df_lvl["ticker"] == "NANEXA.ST").any() else None,
            "nanexa_days": int(df_lvl[df_lvl["ticker"] == "NANEXA.ST"]["holding_days"].values[0]) if (df_lvl["ticker"] == "NANEXA.ST").any() else None,
            "elon_ret_pct": df_lvl[df_lvl["ticker"] == "ELON.ST"]["ret_pct"].values[0] if (df_lvl["ticker"] == "ELON.ST").any() else None,
            "shtb_ret_pct": df_lvl[df_lvl["ticker"] == "SHT-B.ST"]["ret_pct"].values[0] if (df_lvl["ticker"] == "SHT-B.ST").any() else None,
            "ipdn_ret_pct": df_lvl[df_lvl["ticker"] == "IPDN"]["ret_pct"].values[0] if (df_lvl["ticker"] == "IPDN").any() else None,
            "flux_ret_pct": df_lvl[df_lvl["ticker"] == "FLUX"]["ret_pct"].values[0] if (df_lvl["ticker"] == "FLUX").any() else None,
        }

    return {
        "by_level": results_by_level,
        "summary": summary_by_level,
    }


# ==============================================================================
# TEST 3: PORTFOLIO OVERLAP MATRIX (P1–P11)
# ==============================================================================

def run_test_3_portfolio_overlap() -> Dict[str, Any]:
    logger.info("--- RUNNING TEST 3: Portfolio Overlap Matrix across P1–P11 ---")

    portfolio_ids = [
        "P1_Base", "P2_Fast_Cycle", "P3_Diamond_Hands", "P4_Institutional",
        "P5_Nordic_Only", "P6_US_Only", "P7_Deep_Value_Extreme", "P8_Quality_Growth",
        "P9_High_Conviction", "P10_Micro_Sniper", "P11_Meta_Consensus"
    ]

    holdings_by_pid = {}
    for pid in portfolio_ids:
        state_file = PORTFOLIOS_DIR / f"portfolio_{pid}_state.json"
        hist_file = PORTFOLIOS_DIR / f"portfolio_{pid}_history.csv"
        held = set()
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    st = json.load(f)
                    for pos in st.get("positions", []):
                        t = pos.get("Ticker") or pos.get("ticker")
                        if t:
                            held.add(t.strip().upper())
            except Exception:
                pass
        if hist_file.exists():
            try:
                df_h = pd.read_csv(hist_file)
                for t in df_h.get("Ticker", []):
                    if pd.notna(t):
                        held.add(str(t).strip().upper())
            except Exception:
                pass
        holdings_by_pid[pid] = held

    # Overlap matrix (Jaccard and min-overlap %)
    jaccard_df = pd.DataFrame(index=portfolio_ids, columns=portfolio_ids, dtype=float)
    overlap_pct_df = pd.DataFrame(index=portfolio_ids, columns=portfolio_ids, dtype=float)
    common_count_df = pd.DataFrame(index=portfolio_ids, columns=portfolio_ids, dtype=int)

    for p1 in portfolio_ids:
        s1 = holdings_by_pid[p1]
        for p2 in portfolio_ids:
            s2 = holdings_by_pid[p2]
            intersection = s1.intersection(s2)
            union = s1.union(s2)

            common_count_df.loc[p1, p2] = len(intersection)
            jaccard = (len(intersection) / len(union) * 100.0) if len(union) > 0 else 0.0
            jaccard_df.loc[p1, p2] = round(jaccard, 1)

            min_len = min(len(s1), len(s2))
            overlap_pct = (len(intersection) / min_len * 100.0) if min_len > 0 else 0.0
            overlap_pct_df.loc[p1, p2] = round(overlap_pct, 1)

    return {
        "holdings_by_pid": {k: sorted(list(v)) for k, v in holdings_by_pid.items()},
        "jaccard_matrix": jaccard_df,
        "overlap_pct_matrix": overlap_pct_df,
        "common_count_matrix": common_count_df,
    }


# ==============================================================================
# TEST 4: REALISTIC TRADING FRICTION (SPREAD & SLIPPAGE STRESS TEST)
# ==============================================================================

def run_test_4_trading_friction_stress() -> Dict[str, Any]:
    logger.info("--- RUNNING TEST 4: Realistic Trading Friction (Spread & Slippage Stress Test) ---")

    portfolio_ids = [
        "P1_Base", "P2_Fast_Cycle", "P3_Diamond_Hands", "P4_Institutional",
        "P5_Nordic_Only", "P6_US_Only", "P7_Deep_Value_Extreme", "P8_Quality_Growth",
        "P9_High_Conviction", "P10_Micro_Sniper", "P11_Meta_Consensus"
    ]

    # Commission schedule: Nordnet Standard by region
    # FI (.HE): 3.00 €, SE (.ST): 5.00 €, US: 9.00 € (approx Nordnet mini/standard)
    def get_nordnet_commission(ticker: str) -> float:
        if ticker.endswith(".HE"):
            return 3.00
        elif ticker.endswith(".ST"):
            return 5.00
        else:
            return 9.00

    results = []

    for pid in portfolio_ids:
        state_file = PORTFOLIOS_DIR / f"portfolio_{pid}_state.json"
        hist_file = PORTFOLIOS_DIR / f"portfolio_{pid}_history.csv"
        hist_json = PORTFOLIOS_DIR / f"portfolio_{pid}_history.json"

        start_cash = 10000.0
        current_equity = 10000.0

        if hist_json.exists():
            try:
                with open(hist_json, "r", encoding="utf-8") as f:
                    h_list = json.load(f)
                    if h_list:
                        current_equity = float(h_list[-1].get("total_equity", 10000.0))
            except Exception:
                pass

        positions = []
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    st = json.load(f)
                    positions = st.get("positions", [])
            except Exception:
                pass

        # Closed trades from history.csv
        closed_trades = []
        if hist_file.exists():
            try:
                df_h = pd.read_csv(hist_file)
                closed_trades = df_h.to_dict("records")
            except Exception:
                pass

        total_buys = len(positions) + len(closed_trades)
        total_sells = len(closed_trades)
        # In addition, if positions are liquidated (round-trip test), another round of sells occurs
        total_orders_executed = total_buys + total_sells
        total_orders_roundtrip = total_buys * 2

        # Friction calculations:
        # 1. Nordnet Commissions
        commission_executed = sum(get_nordnet_commission(p.get("Ticker", "")) for p in positions) + \
                              sum(get_nordnet_commission(t.get("Ticker", "")) * 2 for t in closed_trades)
        
        commission_roundtrip = sum(get_nordnet_commission(p.get("Ticker", "")) * 2 for p in positions) + \
                               sum(get_nordnet_commission(t.get("Ticker", "")) * 2 for t in closed_trades)

        # 2. Spread & Slippage (0.4% buy + 0.4% sell = 0.8% round-trip)
        total_invested_open = sum(float(p.get("Capital Invested", 0.0)) for p in positions)
        total_invested_closed = sum(float(t.get("Capital Invested", 0.0)) for t in closed_trades)
        
        # 0.4% slippage on entry
        slippage_executed = (total_invested_open + total_invested_closed) * 0.004
        # 0.8% slippage roundtrip
        slippage_roundtrip = (total_invested_open + total_invested_closed) * 0.008

        total_friction_executed = commission_executed + slippage_executed
        total_friction_roundtrip = commission_roundtrip + slippage_roundtrip

        raw_pnl = current_equity - start_cash
        raw_return_pct = (raw_pnl / start_cash) * 100.0

        net_equity_executed = current_equity - total_friction_executed
        net_pnl_executed = net_equity_executed - start_cash
        net_return_pct_executed = (net_pnl_executed / start_cash) * 100.0

        net_equity_roundtrip = current_equity - total_friction_roundtrip
        net_pnl_roundtrip = net_equity_roundtrip - start_cash
        net_return_pct_roundtrip = (net_pnl_roundtrip / start_cash) * 100.0

        friction_drag_pct = (total_friction_roundtrip / start_cash) * 100.0

        results.append({
            "portfolio_id": pid,
            "open_positions": len(positions),
            "closed_trades": len(closed_trades),
            "raw_equity": round(current_equity, 2),
            "raw_return_pct": round(raw_return_pct, 2),
            "commission_eur": round(commission_roundtrip, 2),
            "slippage_eur": round(slippage_roundtrip, 2),
            "total_friction_eur": round(total_friction_roundtrip, 2),
            "net_equity_after_friction": round(net_equity_roundtrip, 2),
            "net_return_pct": round(net_return_pct_roundtrip, 2),
            "friction_drag_pct": round(friction_drag_pct, 2),
            "status": "PROFITABLE" if net_return_pct_roundtrip > 0 else ("BREAK-EVEN" if net_return_pct_roundtrip == 0 else "LOSS")
        })

    return {
        "friction_table": pd.DataFrame(results),
    }


# ==============================================================================
# MAIN EXECUTION DISPATCHER
# ==============================================================================

def main():
    logger.info("=== STARTING QUANTITATIVE RESEARCH SUITE (TESTS 1, 2, 3, 4) ===")

    # 1. Load universe
    tickers, curr_map, adv_map = load_universe_data()
    logger.info(f"Loaded {len(tickers)} tickers from universe.")

    # 2. Fetch market data for Tests 1 & 2
    close_df, vol_df, eurusd, eursek = fetch_extended_market_data(tickers)

    # 3. Load Oracle trades for Test 2
    oracle_df = pd.read_csv(ORACLE_TRADES_CSV)
    logger.info(f"Loaded {len(oracle_df)} trades from Oracle Benchmark.")

    # RUN TEST 1
    t1_res = run_test_1_breakout_backtest(
        tickers=tickers,
        close_df=close_df,
        volume_df=vol_df,
        adv_map=adv_map,
    )

    # RUN TEST 2
    t2_res = run_test_2_trailing_stop_sweep(
        oracle_trades_df=oracle_df,
        close_df=close_df,
        stop_levels=[0.04, 0.06, 0.08, 0.10],
    )

    # RUN TEST 3
    t3_res = run_test_3_portfolio_overlap()

    # RUN TEST 4
    t4_res = run_test_4_trading_friction_stress()

    # PRINT COMPREHENSIVE OUTPUTS
    print("\n" + "=" * 90)
    print("TEST 1: P12 BREAKOUT BACKTEST (Volume >= 3 * ADV_20) RESULTS")
    print("=" * 90)
    print(f"Total Raw Volume >= 3x ADV Signals: {t1_res['total_raw_signals']} across {t1_res['unique_raw_tickers']} tickers")
    print(f"Filtered P12 Signals (+2% change & >=50k ADV): {t1_res['total_p12_signals']} across {t1_res['unique_p12_tickers']} tickers")
    print("\nKey Winner Detection:")
    for kw, sigs in t1_res["winner_catches"].items():
        if sigs:
            for s in sigs:
                print(f"  🎯 {kw}: Fired on {s['date']} | Price: {s['price']} | Day: +{s['day_change_pct']}% | Surge: {s['vol_surge']}x | Runup: +{s['max_runup']}% | +5d: {s['ret_5d']}% | +21d: {s['ret_21d']}%")
        else:
            print(f"  ❌ {kw}: No signal fired in window")

    df_p12 = t1_res["p12_df"]
    if not df_p12.empty:
        pos_5d = df_p12[df_p12["ret_5d"] > 0]
        pos_21d = df_p12[df_p12["ret_21d"] > 0]
        print(f"\nP12 Signals Performance Summary:")
        print(f"  Win Rate (+5d > 0): {len(pos_5d)}/{len(df_p12)} ({len(pos_5d)/len(df_p12)*100:.1f}%) | Avg 5d: {df_p12['ret_5d'].mean():.2f}% | Median 5d: {df_p12['ret_5d'].median():.2f}%")
        print(f"  Win Rate (+21d > 0): {len(pos_21d)}/{len(df_p12)} ({len(pos_21d)/len(df_p12)*100:.1f}%) | Avg 21d: {df_p12['ret_21d'].mean():.2f}% | Median 21d: {df_p12['ret_21d'].median():.2f}%")
        print(f"  Average Max Runup: +{df_p12['max_runup'].mean():.2f}% | Average Max Drawdown: {df_p12['max_drawdown'].mean():.2f}%")

    print("\n" + "=" * 90)
    print("TEST 2: TRAILING STOP PARAMETER SWEEP ON ORACLE 23 WINNERS")
    print("=" * 90)
    print(f"{'Stop Level':<12} | {'Win Rate':<10} | {'Avg Ret':<10} | {'Med Ret':<10} | {'Total Profit (EUR)':<20} | {'Avg Hold':<10} | {'NANEXA Ret':<12}")
    print("-" * 90)
    for lvl, s in t2_res["summary"].items():
        nanexa_str = f"+{s['nanexa_ret_pct']:.1f}% ({s['nanexa_days']}d)" if s['nanexa_ret_pct'] is not None else "N/A"
        print(f"{int(lvl*100)}% Trailing  | {s['win_rate_pct']:>8.1f}% | {s['avg_return_pct']:>8.2f}% | {s['median_return_pct']:>8.2f}% | {s['total_profit_eur']:>18,.2f} EUR | {s['avg_holding_days']:>7.1f} d | {nanexa_str:<12}")

    print("\n" + "=" * 90)
    print("TEST 3: SALKKUJEN PÄÄLLEKKÄISYYS (PORTFOLIO OVERLAP MATRIX %)")
    print("=" * 90)
    print("Overlap % suhteessa pienempään salkkuun (|A ∩ B| / min(|A|, |B|)):")
    print(t3_res["overlap_pct_matrix"].to_string())

    print("\n" + "=" * 90)
    print("TEST 4: REALISTINEN KAUPANKÄYNTIKITKA (SPREAD & SLIPPAGE STRESS TEST)")
    print("=" * 90)
    f_df = t4_res["friction_table"]
    print(f_df[["portfolio_id", "open_positions", "raw_return_pct", "commission_eur", "slippage_eur", "total_friction_eur", "net_return_pct", "friction_drag_pct"]].to_string(index=False))

    # Save all results to data/research_tests_results.json
    results_export = {
        "test_1_breakout": {
            "total_raw_signals": t1_res["total_raw_signals"],
            "total_p12_signals": t1_res["total_p12_signals"],
            "winner_catches": {k: [{key: v[key] for key in ["date", "price", "day_change_pct", "vol_surge", "max_runup", "ret_5d", "ret_21d"]} for v in val] for k, val in t1_res["winner_catches"].items()},
            "p12_signals_summary": {
                "count": len(df_p12),
                "avg_5d_ret": round(float(df_p12['ret_5d'].mean()), 2) if not df_p12.empty else 0.0,
                "median_5d_ret": round(float(df_p12['ret_5d'].median()), 2) if not df_p12.empty else 0.0,
                "avg_21d_ret": round(float(df_p12['ret_21d'].mean()), 2) if not df_p12.empty else 0.0,
                "avg_max_runup": round(float(df_p12['max_runup'].mean()), 2) if not df_p12.empty else 0.0,
            }
        },
        "test_2_trailing_sweep": t2_res["summary"],
        "test_3_overlap_pct": t3_res["overlap_pct_matrix"].to_dict(),
        "test_4_friction": f_df.to_dict(orient="records"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    out_file = DATA_DIR / "research_tests_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results_export, f, indent=2)
    logger.info(f"Saved complete research results to {out_file}")

if __name__ == "__main__":
    main()
