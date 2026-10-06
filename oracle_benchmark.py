#!/usr/bin/env python3
"""
oracle_benchmark.py — Omniscient Benchmark (Theoretical Maximum Return Engine)
Part of the tradeBotTiuku algorithmic trading engine.

Calculates the theoretical maximum return over a historical window using perfect hindsight,
strictly constrained by realistic market mechanics:
1. Capital: 10,000 € starting capital.
2. Portfolio Size: Maximum 5 simultaneous open positions (slots), exactly 2,000 € per slot.
3. Liquidity Rule: Allocation cannot exceed 2% of the ticker's Daily Traded Volume
   (Close Price * Volume in EUR) on the entry day. If 2,000 € > 0.02 * Daily_Volume, trade is invalid.
4. Execution Price: Strictly daily Close price (no High/Low cheats).
5. Output: Daily equity curve saved to data/oracle_equity_curve.csv (Date, Total_Equity).
"""

import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Optional

import numpy as np
import pandas as pd
import yfinance as yf

# Reconfigure stdout/stderr for utf-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("OracleBenchmark")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_UNIVERSE_CSV = DATA_DIR / "clean_microcap_universe.csv"
DEFAULT_OUTPUT_CSV = DATA_DIR / "oracle_equity_curve.csv"
DEFAULT_TRADES_CSV = DATA_DIR / "oracle_trades.csv"

STARTING_CAPITAL = 10000.0
MAX_SLOTS = 5
SLOT_CAPITAL = 2000.0
MAX_VOLUME_PCT = 0.02  # 2% liquidity constraint


def load_universe(universe_csv: Path) -> Tuple[List[str], Dict[str, str]]:
    """Loads ticker symbols and currency mapping from the universe CSV."""
    if not universe_csv.exists():
        raise FileNotFoundError(f"Universe file not found: {universe_csv}")

    df = pd.read_csv(universe_csv)
    if "ticker" not in df.columns:
        raise ValueError(f"Universe CSV {universe_csv} missing 'ticker' column")

    tickers = [str(t).strip().upper() for t in df["ticker"].dropna().unique() if str(t).strip()]
    
    currency_map = {}
    if "currency" in df.columns:
        for _, row in df.iterrows():
            sym = str(row["ticker"]).strip().upper()
            curr = str(row.get("currency", "EUR")).strip().upper()
            currency_map[sym] = curr if curr in ["EUR", "USD", "SEK"] else "EUR"
    else:
        for t in tickers:
            if t.endswith(".HE"):
                currency_map[t] = "EUR"
            elif t.endswith(".ST"):
                currency_map[t] = "SEK"
            else:
                currency_map[t] = "USD"

    logger.info(f"Loaded {len(tickers)} tickers from universe: {universe_csv.name}")
    return tickers, currency_map


def backadjust_unadjusted_splits(close_df: pd.DataFrame) -> pd.DataFrame:
    """
    Detects un-adjusted reverse and forward stock splits in Yahoo Finance data.
    When an overnight jump (> 2.0x or < 0.5x) occurs:
    1. Checks yfinance Ticker.splits for the symbol.
    2. If a split factor S occurred and Yahoo failed to back-adjust historical prices,
       multiplies pre-split prices by (1 / S) so historical prices are properly on the post-split basis.
    """
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
                    # Normalize split timestamp cleanly with or without tz
                    sp_date = pd.Timestamp(sp_dt).tz_convert(None).floor("D") if getattr(sp_dt, "tz", None) else pd.Timestamp(sp_dt).floor("D")
                    pre_dates = s.index[s.index < sp_date]
                    post_dates = s.index[s.index >= sp_date]

                    valid_pre = s.loc[pre_dates].dropna()
                    valid_post = s.loc[post_dates].dropna()

                    if not valid_pre.empty and not valid_post.empty:
                        p_pre = float(valid_pre.iloc[-1])
                        p_post = float(valid_post.iloc[0])
                        if p_pre > 0:
                            ratio = p_post / p_pre
                            # If jump matches split direction (e.g. ratio > 2.0 for reverse split factor < 1.0),
                            # Yahoo Finance failed to back-adjust: adjust pre-split prices now!
                            if (factor < 1.0 and ratio > 2.0) or (factor > 1.0 and ratio < 0.5):
                                df.loc[pre_dates, sym] = df.loc[pre_dates, sym] / factor
                                logger.info(
                                    f"Applied split adjustment for {sym} on {sp_date.strftime('%Y-%m-%d')} "
                                    f"(factor {factor:.4f}, multiplied pre-split prices by {1.0 / factor:.2f}x)"
                                )
            except Exception as e:
                logger.debug(f"Could not check splits for {sym}: {e}")

    return df


def fetch_historical_market_data(
    tickers: List[str],
    lookback_days: int = 30,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """
    Downloads historical Close and Volume data for all tickers and FX pairs (EURUSD=X, EURSEK=X).
    Requests buffer window to guarantee lookback_days trading days.
    Strictly uses Adj Close and automatically back-adjusts unadjusted splits.
    """
    fx_symbols = ["EURUSD=X", "EURSEK=X"]
    all_symbols = list(set(tickers + fx_symbols))

    # Request ~1.5x calendar days to ensure at least lookback_days trading days
    download_period = f"{max(int(lookback_days * 1.6), 40)}d"
    logger.info(f"Downloading daily market data for {len(all_symbols)} symbols (period={download_period})...")

    try:
        data = yf.download(
            all_symbols,
            period=download_period,
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"Failed to download market data via yfinance: {e}")
        raise

    if data.empty:
        raise ValueError("yfinance returned empty data")

    # Strictly use 'Adj Close' instead of 'Close' to naturally account for splits
    price_col = "Adj Close" if "Adj Close" in data else "Close"
    close_df = data[price_col]
    volume_df = data["Volume"] if "Volume" in data else pd.DataFrame(index=close_df.index)

    # Apply Option B: Detect and back-adjust unadjusted splits from Yahoo Finance
    close_df = backadjust_unadjusted_splits(close_df)

    # Extract FX series with fallbacks
    if "EURUSD=X" in close_df:
        eurusd = close_df["EURUSD=X"].ffill().bfill()
    else:
        eurusd = pd.Series(1.08, index=close_df.index)

    if "EURSEK=X" in close_df:
        eursek = close_df["EURSEK=X"].ffill().bfill()
    else:
        eursek = pd.Series(11.30, index=close_df.index)

    # Determine valid dates (trading days where market was open)
    # Exclude dates where virtually no stock traded
    trading_dates = close_df.dropna(how="all").index
    if len(trading_dates) > lookback_days:
        trading_dates = trading_dates[-lookback_days:]

    close_df = close_df.reindex(trading_dates)
    volume_df = volume_df.reindex(trading_dates)
    eurusd = eurusd.reindex(trading_dates).ffill().bfill()
    eursek = eursek.reindex(trading_dates).ffill().bfill()

    logger.info(
        f"Prepared market matrix: {len(trading_dates)} trading days "
        f"({trading_dates[0].strftime('%Y-%m-%d')} to {trading_dates[-1].strftime('%Y-%m-%d')})"
    )
    return close_df, volume_df, eurusd, eursek


def normalize_to_eur(
    close_df: pd.DataFrame,
    volume_df: pd.DataFrame,
    eurusd: pd.Series,
    eursek: pd.Series,
    tickers: List[str],
    currency_map: Dict[str, str],
) -> Tuple[Dict[str, pd.Series], Dict[str, pd.Series]]:
    """
    Converts local Close prices and Traded Volumes into EUR.
    Returns dictionaries of price_eur and volume_eur series per ticker.
    """
    close_eur = {}
    vol_eur = {}
    dates = close_df.index

    for sym in tickers:
        if sym not in close_df:
            continue

        raw_close = close_df[sym].ffill()
        raw_vol = volume_df[sym].fillna(0.0)

        # Skip tickers with insufficient price history
        if raw_close.dropna().count() < 3:
            continue

        curr = currency_map.get(sym, "EUR")
        if curr == "USD":
            fx = 1.0 / eurusd
        elif curr == "SEK":
            fx = 1.0 / eursek
        else:
            fx = pd.Series(1.0, index=dates)

        c_eur = raw_close * fx
        v_eur = c_eur * raw_vol  # Daily traded volume in EUR

        if (c_eur > 0).any():
            close_eur[sym] = c_eur
            vol_eur[sym] = v_eur

    logger.info(f"Normalized {len(close_eur)} tickers to EUR basis")
    return close_eur, vol_eur


def generate_candidate_swings(
    close_eur: Dict[str, pd.Series],
    vol_eur: Dict[str, pd.Series],
    trading_dates: pd.DatetimeIndex,
    slot_capital: float = SLOT_CAPITAL,
    max_volume_pct: float = MAX_VOLUME_PCT,
) -> List[Dict[str, Any]]:
    """
    Scans all pairs of (entry_date, exit_date) with entry_date < exit_date.
    Filters by:
    1. Liquidity Constraint: slot_capital <= max_volume_pct * Daily_Traded_Volume_EUR(entry)
    2. Positive Return: Close_EUR(exit) > Close_EUR(entry)
    """
    candidates = []
    T = len(trading_dates)
    min_required_daily_volume = slot_capital / max_volume_pct  # 2000 / 0.02 = 100,000 EUR

    for sym, c_series in close_eur.items():
        v_series = vol_eur[sym]
        prices = c_series.values
        vols = v_series.values

        for i in range(T - 1):
            p_in = prices[i]
            v_in = vols[i]

            if np.isnan(p_in) or p_in <= 0:
                continue

            # Strict Liquidity Constraint at entry
            if np.isnan(v_in) or v_in < min_required_daily_volume:
                continue

            for j in range(i + 1, T):
                p_out = prices[j]
                if np.isnan(p_out) or p_out <= 0:
                    continue

                price_ratio = p_out / p_in
                ret = price_ratio - 1.0
                if ret > 0:
                    days_held = j - i

                    # Calculate average daily volume in EUR during holding period [i, j]
                    holding_vols = vols[i : j + 1]
                    avg_holding_vol_eur = float(np.mean(holding_vols)) if len(holding_vols) > 0 else 0.0

                    # Hard Sanity Filter: Reject swings where total return (exit/entry) > 5.0 (+400%)
                    # AND average daily volume during holding period is less than 50,000 €
                    if price_ratio > 5.0 and avg_holding_vol_eur < 50_000.0:
                        continue

                    profit_eur = slot_capital * ret
                    volume_pct = (slot_capital / v_in) * 100.0 if v_in > 0 else 0.0

                    candidates.append({
                        "ticker": sym,
                        "i": i,
                        "j": j,
                        "entry_date": trading_dates[i],
                        "exit_date": trading_dates[j],
                        "days": days_held,
                        "entry_price_eur": p_in,
                        "exit_price_eur": p_out,
                        "price_ratio": price_ratio,
                        "ret": ret,
                        "profit_eur": profit_eur,
                        "entry_vol_eur": v_in,
                        "avg_holding_vol_eur": avg_holding_vol_eur,
                        "volume_pct": volume_pct,
                    })

    logger.info(
        f"Identified {len(candidates)} profitable swing candidates meeting strict liquidity (>= {min_required_daily_volume:,.0f} € daily volume)"
    )
    return candidates


def optimize_greedy_schedule(
    candidates: List[Dict[str, Any]],
    num_slots: int = MAX_SLOTS,
) -> List[Dict[str, Any]]:
    """
    Greedy Hindsight Algorithm:
    Iterates through candidate swings ordered by total profitability (profit_eur descending).
    Assigns each swing into an available portfolio slot provided:
    1. Sanity rule: total return (exit/entry) <= 5.0 OR avg holding daily volume >= 50,000 €.
    2. The ticker is NOT already held in any slot during the trade interval [entry, exit].
    3. A slot is free during the trade interval [entry, exit].
    """
    # Sort candidates greedily by profit_eur descending (tie-break: shorter holding period first)
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (c["profit_eur"], -c["days"]),
        reverse=True,
    )

    # Slots tracker: list of scheduled intervals [(i, j, ticker)] for each slot
    slot_intervals: List[List[Tuple[int, int, str]]] = [[] for _ in range(num_slots)]
    
    # Global ticker active intervals: ticker -> [(i, j)]
    ticker_intervals: Dict[str, List[Tuple[int, int]]] = {}

    assigned_trades = []

    for cand in sorted_candidates:
        sym = cand["ticker"]
        ci, cj = cand["i"], cand["j"]

        # Hard Sanity Filter: reject if return > 5.0 and avg holding daily volume < 50,000 €
        price_ratio = cand.get("price_ratio", cand["exit_price_eur"] / cand["entry_price_eur"])
        avg_holding_vol = cand.get("avg_holding_vol_eur", 0.0)
        if price_ratio > 5.0 and avg_holding_vol < 50_000.0:
            continue

        # Check ticker conflict: a ticker cannot be held in two slots at the same time
        sym_intervals = ticker_intervals.get(sym, [])
        ticker_conflict = any(max(ci, ti) < min(cj, tj) for (ti, tj) in sym_intervals)
        if ticker_conflict:
            continue

        # Find first available slot free during [ci, cj]
        # Intervals [ci, cj] and [si, sj] overlap if max(ci, si) < min(cj, sj)
        placed_slot = -1
        for s_idx in range(num_slots):
            slot_free = not any(max(ci, si) < min(cj, sj) for (si, sj, _) in slot_intervals[s_idx])
            if slot_free:
                placed_slot = s_idx
                break

        if placed_slot != -1:
            # Assign trade to slot
            slot_intervals[placed_slot].append((ci, cj, sym))
            if sym not in ticker_intervals:
                ticker_intervals[sym] = []
            ticker_intervals[sym].append((ci, cj))

            trade_record = dict(cand)
            trade_record["slot"] = placed_slot + 1  # 1-indexed (Slot 1 to 5)
            assigned_trades.append(trade_record)

    # Sort executed trades chronologically by entry date and slot
    assigned_trades.sort(key=lambda x: (x["i"], x["slot"]))
    logger.info(f"Greedy optimization scheduled {len(assigned_trades)} optimal non-overlapping trades across {num_slots} slots")
    return assigned_trades


def simulate_daily_equity_curve(
    assigned_trades: List[Dict[str, Any]],
    close_eur: Dict[str, pd.Series],
    trading_dates: pd.DatetimeIndex,
    starting_capital: float = STARTING_CAPITAL,
    slot_capital: float = SLOT_CAPITAL,
) -> pd.DataFrame:
    """
    Calculates the exact daily mark-to-market total equity of the Oracle portfolio.
    
    At market Close on each day t:
    1. Positions reaching exit day (j == t) are sold at Close(t); capital + realized profit credited to Cash.
    2. Positions starting on entry day (i == t) are bought at Close(t); slot_capital deducted from Cash.
    3. Positions currently open (i <= t < j) are marked to market using Close(t).
    4. Total_Equity(t) = Cash + sum(open_positions_market_value).
    """
    T = len(trading_dates)
    daily_records = []

    cash = starting_capital

    for t in range(T):
        curr_date = trading_dates[t]

        # 1. Process exits at Close(t)
        for tr in assigned_trades:
            if tr["j"] == t:
                p_in = tr["entry_price_eur"]
                p_out = tr["exit_price_eur"]
                proceeds = slot_capital * (p_out / p_in)
                cash += proceeds

        # 2. Process entries at Close(t)
        for tr in assigned_trades:
            if tr["i"] == t:
                cash -= slot_capital

        # 3. Mark to market open positions at Close(t)
        open_positions_val = 0.0
        open_count = 0
        for tr in assigned_trades:
            if tr["i"] <= t < tr["j"]:
                sym = tr["ticker"]
                p_in = tr["entry_price_eur"]
                prices = close_eur[sym].values
                p_cur = prices[t]
                if np.isnan(p_cur) or p_cur <= 0:
                    p_cur = p_in
                val = slot_capital * (p_cur / p_in)
                open_positions_val += val
                open_count += 1

        total_equity = cash + open_positions_val

        daily_records.append({
            "Date": curr_date.strftime("%Y-%m-%d"),
            "Total_Equity": round(total_equity, 2),
            "Cash": round(cash, 2),
            "Open_Positions_Value": round(open_positions_val, 2),
            "Open_Positions_Count": open_count,
        })

    df_equity = pd.DataFrame(daily_records)
    return df_equity


def print_summary_report(
    df_equity: pd.DataFrame,
    assigned_trades: List[Dict[str, Any]],
    starting_capital: float,
) -> None:
    """Prints a clear terminal summary of the Oracle benchmark performance."""
    initial_eq = df_equity["Total_Equity"].iloc[0]
    final_eq = df_equity["Total_Equity"].iloc[-1]
    total_profit = final_eq - starting_capital
    total_return_pct = (total_profit / starting_capital) * 100.0

    print("\n" + "=" * 70)
    print("[ORACLE BENCHMARK] (OMNISCIENT 5-SLOT) RESULTS")
    print("=" * 70)
    print(f"Starting Capital:       {starting_capital:12,.2f} EUR")
    print(f"Final Total Equity:     {final_eq:12,.2f} EUR")
    print(f"Total Net Profit:       {total_profit:+12,.2f} EUR ({total_return_pct:+.2f}%)")
    print(f"Max Portfolio Slots:    {MAX_SLOTS} (Max 2,000 EUR / slot)")
    print(f"Trades Executed:        {len(assigned_trades)}")
    print(f"Win Rate:               100.0% (Perfect foresight swing selection)")
    
    if assigned_trades:
        avg_ret = np.mean([t["ret"] * 100 for t in assigned_trades])
        avg_days = np.mean([t["days"] for t in assigned_trades])
        print(f"Average Trade Return:   {avg_ret:+.2f}%")
        print(f"Average Holding Period: {avg_days:.1f} trading days")
        
        print("\n[*] Top 5 Most Profitable Oracle Swings:")
        top_trades = sorted(assigned_trades, key=lambda x: x["profit_eur"], reverse=True)[:5]
        for idx, t in enumerate(top_trades, 1):
            e_dt = t["entry_date"].strftime("%Y-%m-%d")
            x_dt = t["exit_date"].strftime("%Y-%m-%d")
            print(
                f"  {idx}. Slot {t['slot']} | {t['ticker']:<8} | {e_dt} -> {x_dt} ({t['days']:2d}d) | "
                f"Gain: {t['ret']*100:+7.2f}% | Profit: {t['profit_eur']:+9,.2f} EUR | "
                f"Vol: {t['entry_vol_eur']:,.0f} EUR (took {t['volume_pct']:.2f}% vol)"
            )
    print("=" * 70 + "\n")


def run_oracle_benchmark(
    universe_path: Path = DEFAULT_UNIVERSE_CSV,
    output_path: Path = DEFAULT_OUTPUT_CSV,
    trades_path: Optional[Path] = DEFAULT_TRADES_CSV,
    lookback_days: int = 30,
    starting_capital: float = STARTING_CAPITAL,
    num_slots: int = MAX_SLOTS,
    slot_capital: float = SLOT_CAPITAL,
    max_volume_pct: float = MAX_VOLUME_PCT,
) -> pd.DataFrame:
    """Executes the full Oracle Benchmark pipeline."""
    # 1. Load universe
    tickers, curr_map = load_universe(universe_path)

    # 2. Fetch market data
    close_df, vol_df, eurusd, eursek = fetch_historical_market_data(
        tickers=tickers,
        lookback_days=lookback_days,
    )
    trading_dates = close_df.index

    # 3. Normalize to EUR
    close_eur, vol_eur = normalize_to_eur(
        close_df=close_df,
        volume_df=vol_df,
        eurusd=eurusd,
        eursek=eursek,
        tickers=tickers,
        currency_map=curr_map,
    )

    # 4. Generate candidate swings meeting realistic constraints
    candidates = generate_candidate_swings(
        close_eur=close_eur,
        vol_eur=vol_eur,
        trading_dates=trading_dates,
        slot_capital=slot_capital,
        max_volume_pct=max_volume_pct,
    )

    # 5. Greedy scheduling
    assigned_trades = optimize_greedy_schedule(
        candidates=candidates,
        num_slots=num_slots,
    )

    # 6. Daily mark-to-market simulation
    df_equity = simulate_daily_equity_curve(
        assigned_trades=assigned_trades,
        close_eur=close_eur,
        trading_dates=trading_dates,
        starting_capital=starting_capital,
        slot_capital=slot_capital,
    )

    # 7. Save outputs
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Save clean Date, Total_Equity CSV
    df_equity[["Date", "Total_Equity"]].to_csv(output_path, index=False)
    logger.info(f"Saved Oracle equity curve to {output_path} ({len(df_equity)} daily rows)")

    if trades_path and assigned_trades:
        df_trades = pd.DataFrame(assigned_trades)
        export_cols = [
            "slot", "ticker", "entry_date", "exit_date", "days",
            "entry_price_eur", "exit_price_eur", "ret", "profit_eur",
            "entry_vol_eur", "avg_holding_vol_eur", "volume_pct"
        ]
        available_cols = [c for c in export_cols if c in df_trades.columns]
        df_trades[available_cols].to_csv(trades_path, index=False)
        logger.info(f"Saved Oracle executed trades to {trades_path}")

    # 8. Print Summary
    print_summary_report(df_equity, assigned_trades, starting_capital)

    return df_equity


def main():
    parser = argparse.ArgumentParser(
        description="Oracle Benchmark: Theoretical maximum return constrained by realistic liquidity and slot mechanics."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback period in trading days (default: 30)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=STARTING_CAPITAL,
        help=f"Starting capital in EUR (default: {STARTING_CAPITAL})",
    )
    parser.add_argument(
        "--slots",
        type=int,
        default=MAX_SLOTS,
        help=f"Maximum simultaneous positions/slots (default: {MAX_SLOTS})",
    )
    parser.add_argument(
        "--slot-size",
        type=float,
        default=SLOT_CAPITAL,
        help=f"Allocation per slot in EUR (default: {SLOT_CAPITAL})",
    )
    parser.add_argument(
        "--universe",
        type=str,
        default=str(DEFAULT_UNIVERSE_CSV),
        help=f"Path to universe CSV (default: {DEFAULT_UNIVERSE_CSV})",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(DEFAULT_OUTPUT_CSV),
        help=f"Path to output equity curve CSV (default: {DEFAULT_OUTPUT_CSV})",
    )

    args = parser.parse_args()

    run_oracle_benchmark(
        universe_path=Path(args.universe),
        output_path=Path(args.output),
        trades_path=DEFAULT_TRADES_CSV,
        lookback_days=args.days,
        starting_capital=args.capital,
        num_slots=args.slots,
        slot_capital=args.slot_size,
    )


if __name__ == "__main__":
    main()
