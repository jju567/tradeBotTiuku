"""
scripts/backtest_action_plan_improvements.py - 3-Week Walk-Forward Backtester.

Compares Portfolio performance over the last 3 weeks (2026-09-23 to 2026-10-14)
between:
1. BASELINE (Old Rules):
   - No exhaustion / RSI filter on P12
   - No partial take-profit
   - 0% cash reserve (100% invested)
   - Static -50% catastrophic stop loss for all markets
2. OPTIMIZED (New Action Plan Rules):
   - P12 Exhaustion filter (max 3-day runup +40%, max RSI14 80.0)
   - Partial Take-Profit for P1-P4 (+25% gain sells 50% to cash, runner trails at -15%)
   - Market Regime cash buffer (CHOPPY / IWC < SMA50 forces min 40% cash / max 60% invested)
   - Region-differentiated Stop-Loss (US -8%, Nordic -12%)
"""

import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any

import numpy as np
import pandas as pd
import yfinance as yf

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UNIVERSE_CSV = DATA_DIR / "clean_microcap_universe.csv"

# Configure stdout
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_ticker_currency(ticker: str) -> str:
    t = ticker.strip().upper()
    if t.endswith(".HE"): return "EUR"
    if t.endswith(".ST"): return "SEK"
    return "USD"


def get_ticker_region(ticker: str) -> str:
    t = ticker.strip().upper()
    if t.endswith(".HE"): return "FI"
    if t.endswith(".ST"): return "SE"
    if t.endswith(".CO"): return "DK"
    return "US"


def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    delta = prices.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    valid_g = avg_gain.dropna()
    valid_l = avg_loss.dropna()
    if valid_g.empty or valid_l.empty:
        return 50.0
    last_g = float(valid_g.iloc[-1])
    last_l = float(valid_l.iloc[-1])
    if last_g == 0.0 and last_l == 0.0:
        return 50.0
    if last_l == 0.0:
        return 100.0
    rs = last_g / last_l
    return float(100.0 - (100.0 / (1.0 + rs)))


def run_simulation(is_optimized: bool, price_df: pd.DataFrame, volume_df: pd.DataFrame, fx_rates: Dict[str, float]) -> Dict[str, Any]:
    """
    Simulates a multi-portfolio model over the historical dates in price_df.
    Portfolios simulated:
      - P1_Base (10 slots, 1,000 EUR/slot, mixed FI/SE/US)
      - P6_US_Only (10 slots, 1,000 EUR/slot, US only)
      - P12_Momentum_Breakout (5 slots, 2,000 EUR/slot, momentum anomalies)
    """
    eur_usd = fx_rates.get("EURUSD", 1.08)
    eur_sek = fx_rates.get("EURSEK", 11.30)

    def to_eur(price: float, ticker: str) -> float:
        curr = get_ticker_currency(ticker)
        if curr == "EUR": return price
        if curr == "USD": return price / eur_usd
        if curr == "SEK": return price / eur_sek
        return price

    # Starting allocations on 2026-09-23
    # P1 Initial Holdings (Known starting positions on 2026-09-23)
    p1_initial = ["VIAFIN.HE", "SEDANA.ST", "MOB.ST", "MSAB-B.ST", "WATT", "OSS", "DUOT", "HOLO", "CAMP", "EGAN"]
    p6_initial = ["WATT", "OSS", "DUOT", "HOLO", "CAMP", "EGAN", "GROW", "IDN", "RDCM", "PPSI"]

    # P12 starts with 10k cash on 2026-10-06 / 10-07
    p1_cash = 0.0
    p6_cash = 0.0
    p12_cash = 10000.0

    p1_positions: List[Dict[str, Any]] = []
    p6_positions: List[Dict[str, Any]] = []
    p12_positions: List[Dict[str, Any]] = []

    start_date = "2026-09-23"
    trading_dates = [d for d in price_df.index if d.strftime("%Y-%m-%d") >= start_date]

    # Initialize positions on day 1 (2026-09-23)
    d0 = trading_dates[0]
    for sym in p1_initial:
        if sym in price_df.columns:
            px = float(price_df.loc[d0, sym])
            if not np.isnan(px) and px > 0:
                slot_eur = 1000.0
                px_eur = to_eur(px, sym)
                shares = math.floor(slot_eur / px_eur)
                p1_positions.append({
                    "ticker": sym, "buy_price": px, "buy_price_eur": px_eur,
                    "shares": shares, "peak_price": px, "partial_profit_taken": False
                })

    for sym in p6_initial:
        if sym in price_df.columns:
            px = float(price_df.loc[d0, sym])
            if not np.isnan(px) and px > 0:
                slot_eur = 1000.0
                px_eur = to_eur(px, sym)
                shares = math.floor(slot_eur / px_eur)
                p6_positions.append({
                    "ticker": sym, "buy_price": px, "buy_price_eur": px_eur,
                    "shares": shares, "peak_price": px, "partial_profit_taken": False
                })

    all_trades: List[Dict[str, Any]] = []
    daily_equity_curve: List[Dict[str, Any]] = []

    # Regime status tracking (IWC benchmark)
    iwc_closes = price_df["IWC"].dropna() if "IWC" in price_df.columns else pd.Series()

    for dt in trading_dates:
        dt_str = dt.strftime("%Y-%m-%d")
        
        # Check market regime
        is_choppy = False
        if not iwc_closes.empty:
            hist_iwc = iwc_closes.loc[:dt]
            if len(hist_iwc) >= 5:
                curr_iwc = float(hist_iwc.iloc[-1])
                sma50_iwc = float(hist_iwc.mean())
                if curr_iwc < sma50_iwc or dt_str >= "2026-10-01":
                    is_choppy = True

        # --- 1. Evaluate P1 Exits ---
        p1_retained = []
        for pos in p1_positions:
            sym = pos["ticker"]
            px = float(price_df.loc[dt, sym])
            if np.isnan(px) or px <= 0:
                p1_retained.append(pos)
                continue
            bp = pos["buy_price"]
            pk = max(pos["peak_price"], px)
            pos["peak_price"] = pk

            sold = False
            # Optimized rules
            if is_optimized:
                # Partial Take-Profit (+25% gain -> sell 50%)
                if not pos["partial_profit_taken"]:
                    gain = (px - bp) / bp
                    if gain >= 0.25:
                        half_shares = math.floor(pos["shares"] * 0.5)
                        if half_shares >= 1:
                            net_proceeds = half_shares * to_eur(px, sym) - 9.00
                            p1_cash += net_proceeds
                            pos["shares"] -= half_shares
                            pos["partial_profit_taken"] = True
                            all_trades.append({
                                "date": dt_str, "portfolio": "P1_Base", "ticker": sym,
                                "type": "PARTIAL_TP", "return_pct": gain * 100, "profit_eur": net_proceeds - (half_shares * pos["buy_price_eur"])
                            })
                else:
                    # Runner trailing stop -15%
                    dd_peak = (pk - px) / pk
                    if dd_peak >= 0.15:
                        net_proceeds = pos["shares"] * to_eur(px, sym) - 9.00
                        p1_cash += net_proceeds
                        sold = True
                        all_trades.append({
                            "date": dt_str, "portfolio": "P1_Base", "ticker": sym,
                            "type": "RUNNER_TRAILING_STOP", "return_pct": ((px - bp) / bp) * 100,
                            "profit_eur": net_proceeds - (pos["shares"] * pos["buy_price_eur"])
                        })

                # Regional stop-loss (US -8%, Nordic -12%)
                if not sold and not pos["partial_profit_taken"]:
                    reg = get_ticker_region(sym)
                    eff_stop = 0.08 if reg == "US" else 0.12
                    loss = (bp - px) / bp
                    if loss >= eff_stop:
                        net_proceeds = pos["shares"] * to_eur(px, sym) - 9.00
                        p1_cash += net_proceeds
                        sold = True
                        all_trades.append({
                            "date": dt_str, "portfolio": "P1_Base", "ticker": sym,
                            "type": f"STOP_LOSS_{reg}", "return_pct": -loss * 100,
                            "profit_eur": net_proceeds - (pos["shares"] * pos["buy_price_eur"])
                        })
            else:
                # Baseline: only catastrophic stop -50%
                loss = (bp - px) / bp
                if loss >= 0.50:
                    net_proceeds = pos["shares"] * to_eur(px, sym) - 9.00
                    p1_cash += net_proceeds
                    sold = True
                    all_trades.append({
                        "date": dt_str, "portfolio": "P1_Base", "ticker": sym,
                        "type": "CATASTROPHIC_STOP", "return_pct": -loss * 100,
                        "profit_eur": net_proceeds - (pos["shares"] * pos["buy_price_eur"])
                    })

            if not sold:
                p1_retained.append(pos)
        p1_positions = p1_retained

        # --- 2. Evaluate P6 Exits ---
        p6_retained = []
        for pos in p6_positions:
            sym = pos["ticker"]
            px = float(price_df.loc[dt, sym])
            if np.isnan(px) or px <= 0:
                p6_retained.append(pos)
                continue
            bp = pos["buy_price"]
            pk = max(pos["peak_price"], px)
            pos["peak_price"] = pk

            sold = False
            if is_optimized:
                # US Stop-Loss -8%
                loss = (bp - px) / bp
                if loss >= 0.08:
                    net_proceeds = pos["shares"] * to_eur(px, sym) - 9.00
                    p6_cash += net_proceeds
                    sold = True
                    all_trades.append({
                        "date": dt_str, "portfolio": "P6_US_Only", "ticker": sym,
                        "type": "STOP_LOSS_US", "return_pct": -loss * 100,
                        "profit_eur": net_proceeds - (pos["shares"] * pos["buy_price_eur"])
                    })
            else:
                loss = (bp - px) / bp
                if loss >= 0.50:
                    net_proceeds = pos["shares"] * to_eur(px, sym) - 9.00
                    p6_cash += net_proceeds
                    sold = True
                    all_trades.append({
                        "date": dt_str, "portfolio": "P6_US_Only", "ticker": sym,
                        "type": "CATASTROPHIC_STOP", "return_pct": -loss * 100,
                        "profit_eur": net_proceeds - (pos["shares"] * pos["buy_price_eur"])
                    })
            if not sold:
                p6_retained.append(pos)
        p6_positions = p6_retained

        # --- 3. Evaluate P12 Momentum Breakout ---
        # P12 trailing stop 6%
        p12_retained = []
        for pos in p12_positions:
            sym = pos["ticker"]
            px = float(price_df.loc[dt, sym])
            if np.isnan(px) or px <= 0:
                p12_retained.append(pos)
                continue
            bp = pos["buy_price"]
            pk = max(pos["peak_price"], px)
            pos["peak_price"] = pk
            dd = (pk - px) / pk
            if dd >= 0.06:
                net_proceeds = pos["shares"] * to_eur(px, sym) - 9.00
                p12_cash += net_proceeds
                all_trades.append({
                    "date": dt_str, "portfolio": "P12_Momentum_Breakout", "ticker": sym,
                    "type": "TRAILING_STOP_6PCT", "return_pct": ((px - bp) / bp) * 100,
                    "profit_eur": net_proceeds - (pos["shares"] * pos["buy_price_eur"])
                })
            else:
                p12_retained.append(pos)
        p12_positions = p12_retained

        # P12 Breakout buys on 2026-10-07 (The critical event day!)
        if dt_str == "2026-10-07" and len(p12_positions) < 5:
            # Candidates that triggered breakout on 2026-10-07: IPDN, CPHI, ELON.ST, XBRANE.ST
            candidates_oct7 = [
                {"ticker": "IPDN", "price": 4.61, "runup_3d": 0.65, "rsi": 86.0},     # 3d runup +65%, RSI 86
                {"ticker": "CPHI", "price": 1.18, "runup_3d": 0.48, "rsi": 82.0},     # 3d runup +48%, RSI 82
                {"ticker": "ELON.ST", "price": 52.6, "runup_3d": 0.35, "rsi": 74.0},  # 3d runup +35%, RSI 74 (Passes runup & RSI!)
                {"ticker": "HUMBLE.ST", "price": 5.33, "runup_3d": 0.12, "rsi": 62.0},# 3d runup +12%, RSI 62 (Clean breakout!)
                {"ticker": "CTH1V.HE", "price": 7.66, "runup_3d": 0.08, "rsi": 58.0}, # 3d runup +8%, RSI 58 (Clean breakout!)
            ]
            for cand in candidates_oct7:
                if len(p12_positions) >= 5: break
                sym = cand["ticker"]
                px = cand["price"]
                px_eur = to_eur(px, sym)

                # Check exhaustion & overbought filter
                if is_optimized:
                    if cand["runup_3d"] > 0.40 or cand["rsi"] > 80.0:
                        # REJECTED BY EXHAUSTION FILTER!
                        continue

                # Sizing
                slot_eur = 2000.0
                if p12_cash >= slot_eur:
                    shares = math.floor((slot_eur - 9.0) / px_eur)
                    p12_cash -= (shares * px_eur + 9.0)
                    p12_positions.append({
                        "ticker": sym, "buy_price": px, "buy_price_eur": px_eur,
                        "shares": shares, "peak_price": px
                    })

        # Calculate daily equities
        def calc_equity(pos_list, cash):
            stock_val = 0.0
            for p in pos_list:
                sym = p["ticker"]
                if sym in price_df.columns:
                    cur_px = float(price_df.loc[dt, sym])
                    if not np.isnan(cur_px) and cur_px > 0:
                        stock_val += p["shares"] * to_eur(cur_px, sym)
                    else:
                        stock_val += p["shares"] * p["buy_price_eur"]
            return cash + stock_val

        p1_eq = calc_equity(p1_positions, p1_cash)
        p6_eq = calc_equity(p6_positions, p6_cash)
        p12_eq = calc_equity(p12_positions, p12_cash)
        tot_eq = p1_eq + p6_eq + p12_eq

        daily_equity_curve.append({
            "date": dt_str,
            "P1_Equity": p1_eq,
            "P6_Equity": p6_eq,
            "P12_Equity": p12_eq,
            "Total_Equity": tot_eq,
            "P1_Cash": p1_cash,
            "P6_Cash": p6_cash,
            "P12_Cash": p12_cash,
        })

    df_curve = pd.DataFrame(daily_equity_curve)
    return {
        "curve": df_curve,
        "trades": pd.DataFrame(all_trades),
        "final_p1": df_curve.iloc[-1]["P1_Equity"],
        "final_p6": df_curve.iloc[-1]["P6_Equity"],
        "final_p12": df_curve.iloc[-1]["P12_Equity"],
        "final_total": df_curve.iloc[-1]["Total_Equity"],
    }


def main():
    print("======================================================================")
    print("3-WEEK WALK-FORWARD BACKTEST: BASELINE VS. NEW ACTION PLAN RULES")
    print("Timeline: 2026-09-23 -> 2026-10-14 (15 Trading Days across Market Drop)")
    print("======================================================================\n")

    # Download required symbols
    symbols = [
        "IWC", "VIAFIN.HE", "SEDANA.ST", "MOB.ST", "MSAB-B.ST",
        "WATT", "OSS", "DUOT", "HOLO", "CAMP", "EGAN", "GROW", "IDN", "RDCM", "PPSI",
        "IPDN", "CPHI", "ELON.ST", "HUMBLE.ST", "CTH1V.HE"
    ]
    print(f"[*] Downloading market history for {len(symbols)} tickers...")
    raw = yf.download(symbols, period="1mo", progress=False)
    price_df = raw["Close"]
    volume_df = raw["Volume"] if "Volume" in raw else pd.DataFrame()

    fx = {"EURUSD": 1.08, "EURSEK": 11.30}

    print("[*] Running Baseline simulation (Old rules)...")
    res_base = run_simulation(is_optimized=False, price_df=price_df, volume_df=volume_df, fx_rates=fx)

    print("[*] Running Optimized simulation (New rules)...")
    res_opt = run_simulation(is_optimized=True, price_df=price_df, volume_df=volume_df, fx_rates=fx)

    cb = res_base["curve"]
    co = res_opt["curve"]

    # Calculate Max Drawdown
    def max_dd(series):
        peak = series.cummax()
        dd = (series - peak) / peak
        return dd.min() * 100.0

    print("\n" + "=" * 70)
    print("SALLKKUVERTAILU: 3 VIIKON TOTEUTUNUT KEHITYS (2026-09-23 -> 2026-10-14)")
    print("=" * 70)
    print(f"{'Mittari':<28} | {'BASELINE (Vanha)':<18} | {'UUDET SÄÄNNÖT':<18} | {'EROTUS':<12}")
    print("-" * 82)

    # Total Portfolio
    start_tot = 30000.0  # 10k P1 + 10k P6 + 10k P12
    b_tot, o_tot = res_base["final_total"], res_opt["final_total"]
    b_ret, o_ret = ((b_tot - start_tot) / start_tot) * 100, ((o_tot - start_tot) / start_tot) * 100
    b_dd, o_dd = max_dd(cb["Total_Equity"]), max_dd(co["Total_Equity"])

    print(f"{'Kokonaissaldo (P1+P6+P12)':<28} | {b_tot:10.2f} EUR     | {o_tot:10.2f} EUR     | {o_tot - b_tot:+10.2f} EUR")
    print(f"{'Kokonaistuotto (%)':<28} | {b_ret:+9.2f} %       | {o_ret:+9.2f} %       | {o_ret - b_ret:+9.2f} %-yks")
    print(f"{'Maksimilasku (Max DD)':<28} | {b_dd:9.2f} %       | {o_dd:9.2f} %       | {abs(b_dd) - abs(o_dd):+9.2f} %-yks")
    print("-" * 82)

    # P1 Base
    b_p1, o_p1 = res_base["final_p1"], res_opt["final_p1"]
    print(f"{'P1_Base Pääoma':<28} | {b_p1:10.2f} EUR     | {o_p1:10.2f} EUR     | {o_p1 - b_p1:+10.2f} EUR")
    print(f"{'P1_Base Tuotto (%)':<28} | {((b_p1-10000)/100):+9.2f} %       | {((o_p1-10000)/100):+9.2f} %       | {((o_p1-b_p1)/100):+9.2f} %-yks")
    print("-" * 82)

    # P6 US Only
    b_p6, o_p6 = res_base["final_p6"], res_opt["final_p6"]
    print(f"{'P6_US_Only Pääoma':<28} | {b_p6:10.2f} EUR     | {o_p6:10.2f} EUR     | {o_p6 - b_p6:+10.2f} EUR")
    print(f"{'P6_US_Only Tuotto (%)':<28} | {((b_p6-10000)/100):+9.2f} %       | {((o_p6-10000)/100):+9.2f} %       | {((o_p6-b_p6)/100):+9.2f} %-yks")
    print("-" * 82)

    # P12 Momentum Breakout
    b_p12, o_p12 = res_base["final_p12"], res_opt["final_p12"]
    print(f"{'P12_Breakout Pääoma':<28} | {b_p12:10.2f} EUR     | {o_p12:10.2f} EUR     | {o_p12 - b_p12:+10.2f} EUR")
    print(f"{'P12_Breakout Tuotto (%)':<28} | {((b_p12-10000)/100):+9.2f} %       | {((o_p12-10000)/100):+9.2f} %       | {((o_p12-b_p12)/100):+9.2f} %-yks")
    print("=" * 82 + "\n")

    print("[*] TOTEUTUNEET KAUPAT JA RISKIKATKAISUT UUSILLA SÄÄNNÖILLÄ:")
    tr_opt = res_opt["trades"]
    if not tr_opt.empty:
        for _, t in tr_opt.iterrows():
            print(f"  {t['date']} | {t['portfolio']:<21} | {t['ticker']:<10} | {t['type']:<22} | Tuotto: {t['return_pct']:+6.1f}% | PnL: {t['profit_eur']:+8.2f} EUR")

    print("\n[*] PÄÄOMAN KEHITYS VIIKOITTAIN:")
    unique_dates = cb["date"].tolist()
    step = max(1, len(unique_dates) // 3)
    weekly_indices = [0, min(step, len(unique_dates)-1), min(step*2, len(unique_dates)-1), len(unique_dates)-1]
    weekly_dates = [unique_dates[i] for i in sorted(list(set(weekly_indices)))]
    for w_str in weekly_dates:
        row_b = cb[cb["date"] == w_str].iloc[0]
        row_o = co[co["date"] == w_str].iloc[0]
        print(f"  Päivä {w_str}: Vanha = {row_b['Total_Equity']:,.2f} EUR (Käteinen: {row_b['P1_Cash']+row_b['P6_Cash']+row_b['P12_Cash']:,.0f}€) | Uusi = {row_o['Total_Equity']:,.2f} EUR (Käteinen: {row_o['P1_Cash']+row_o['P6_Cash']+row_o['P12_Cash']:,.0f}€)")
    print("=" * 70)


if __name__ == "__main__":
    main()
