import sys
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import math
from scipy.stats import norm

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load 240 trades
trades_df = pd.read_csv("data/institutional_backtest_results.csv")
print(f"Total trades loaded: {len(trades_df)}")

unique_tickers = trades_df["ticker"].unique()
print(f"Unique tickers across 240 trades: {len(unique_tickers)}")

# Load PIT fundamentals for Layer 3 checking
pit_df = pd.read_csv("data/clean_microcap_pit_fundamentals.csv")

price_cache = {}

def get_prices(ticker):
    if ticker not in price_cache:
        t = yf.Ticker(ticker)
        df = t.history(period="5y", auto_adjust=True)
        if not df.empty:
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = df.sort_index()
        price_cache[ticker] = df
    return price_cache[ticker]

# Pre-fetch all tickers
for t in unique_tickers:
    get_prices(t)

bh_returns = []
trilayer_returns = []
trilayer_exits = []
atr_returns = trades_df["net_return_pct"].tolist()

for idx, row in trades_df.iterrows():
    ticker = row["ticker"]
    buy_date_str = row["buy_date"]
    buy_px = float(row["buy_price"])
    df = price_cache.get(ticker)
    
    if df is None or df.empty or buy_px <= 0:
        bh_returns.append(0.0)
        trilayer_returns.append(0.0)
        trilayer_exits.append("ERROR")
        continue

    buy_dt = pd.to_datetime(buy_date_str).tz_localize(None)
    sub = df[df.index >= buy_dt]
    if sub.empty:
        bh_returns.append(0.0)
        trilayer_returns.append(0.0)
        trilayer_exits.append("ERROR")
        continue

    # Window up to 126 trading days (~6 months)
    window = sub.iloc[: min(len(sub), 127)]
    if len(window) < 1:
        bh_returns.append(0.0)
        trilayer_returns.append(0.0)
        trilayer_exits.append("ERROR")
        continue

    # Buy & Hold return at end of 6m window (or latest valid mark-to-market close)
    valid_closes = window["Close"].dropna()
    end_px = float(valid_closes.iloc[-1]) if not valid_closes.empty else buy_px
    bh_ret = ((end_px - buy_px) / buy_px * 100.0) - 0.50 # deduct 0.5% slippage
    bh_returns.append(bh_ret)

    # Tri-Layer Simulation:
    # Layer 1: Catastrophic Stop (-50% from buy price)
    # Layer 2: LLM news reject (passive in backtest)
    # Layer 3: Quarterly PIT deterioration
    tl_stopped = False
    tl_ret = None
    tl_exit = "6M_EXPIRY"

    # Check future quarterly reports within the 6m holding window
    t_pit = pit_df[pit_df["ticker"] == ticker].copy()
    t_pit["dt"] = pd.to_datetime(t_pit["report_date"])

    last_valid_close = buy_px
    for d_idx in range(1, len(window)):
        cur_day = window.index[d_idx]
        low_val = window.iloc[d_idx]["Low"]
        close_val = window.iloc[d_idx]["Close"]

        low_px = float(low_val) if pd.notna(low_val) else last_valid_close
        close_px = float(close_val) if pd.notna(close_val) else last_valid_close
        if pd.notna(close_val):
            last_valid_close = close_px

        # Check Layer 1: Catastrophic Stop (-50%)
        if low_px <= buy_px * 0.50:
            tl_stopped = True
            sell_px = buy_px * 0.50
            tl_ret = ((sell_px - buy_px) / buy_px * 100.0) - 0.50
            tl_exit = "CATASTROPHIC_STOP"
            break

        # Check Layer 3: Quarterly fundamental report filed during holding
        reports_due = t_pit[(t_pit["dt"] > buy_dt) & (t_pit["dt"] <= cur_day)]
        if not reports_due.empty:
            latest_rep = reports_due.iloc[-1]
            rev_yoy = latest_rep["revenue_yoy"]
            runway = latest_rep["cash_runway_months"]
            ocf = latest_rep["ocf"]
            
            runway_num = float(runway) if (pd.notna(runway) and str(runway).lower() != "infinite") else float("inf")
            rev_deterioration = (pd.notna(rev_yoy) and float(rev_yoy) < 0.0)
            runway_deterioration = (runway_num < 12.0 and float(ocf) < 0.0)

            if rev_deterioration or runway_deterioration:
                tl_stopped = True
                sell_px = close_px
                tl_ret = ((sell_px - buy_px) / buy_px * 100.0) - 0.50
                tl_exit = "FUNDAMENTAL_DETERIORATION"
                break

    if not tl_stopped:
        tl_ret = bh_ret
        tl_exit = "6M_EXPIRY"

    trilayer_returns.append(tl_ret)
    trilayer_exits.append(tl_exit)

trades_df["bh_net_return_pct"] = bh_returns
trades_df["trilayer_net_return_pct"] = trilayer_returns
trades_df["trilayer_exit"] = trilayer_exits

def calc_stats(series, name="Strategy"):
    s = np.array(series)
    n = len(s)
    win_rate = (s > 0).mean() * 100
    mean_ret = s.mean()
    med_ret = np.median(s)
    std_ret = s.std(ddof=1) if n > 1 else 0.0
    rf_quarterly = 3.0 / 2.0 # ~1.5% for 6m
    sharpe = (mean_ret - rf_quarterly) / std_ret if std_ret > 0 else 0.0
    worst = s.min()
    best = s.max()
    return {
        "Name": name,
        "N": n,
        "Win Rate (%)": round(win_rate, 1),
        "Mean (%)": round(mean_ret, 2),
        "Median (%)": round(med_ret, 2),
        "Std Dev (%)": round(std_ret, 2),
        "Sharpe": round(sharpe, 3),
        "Worst (%)": round(worst, 2),
        "Best (%)": round(best, 2),
    }

print("\n" + "="*80)
print(f"3-WAY COMPARISON ACROSS N={len(trades_df)} MICRO-CAP TRADES (COST-ADJUSTED)")
print("="*80)
stats_list = [
    calc_stats(trades_df["bh_net_return_pct"], "1. Buy & Hold (6-Month)"),
    calc_stats(trades_df["net_return_pct"], "2. 2.5x ATR Trailing Stop"),
    calc_stats(trades_df["trilayer_net_return_pct"], "3. Tri-Layer Fundamental Exit"),
]
comp_df = pd.DataFrame(stats_list)
print(comp_df.to_string(index=False))

# Exit breakdown for Tri-Layer
print("\nTri-Layer Exit Types:")
print(trades_df["trilayer_exit"].value_counts())

# Exit breakdown for ATR
print("\nATR Exit Types:")
print(trades_df["exit_type"].value_counts())

# Top Outliers in Buy & Hold
top_outliers = trades_df.sort_values("bh_net_return_pct", ascending=False)[["ticker", "buy_date", "bh_net_return_pct", "net_return_pct", "trilayer_net_return_pct"]].head(5)
print("\nTop 5 Outlier Trades:")
print(top_outliers.to_string(index=False))

# DSR Calculation
def deflated_sharpe_ratio(sr_observed, n_trials, var_sr_null, skewness, kurtosis, n_obs):
    # Bailey & Lopez de Prado (2014)
    # Expected max SR under null
    gamma = 0.5772156649
    z = (1 - gamma) * norm.ppf(1 - 1/n_trials) + gamma * norm.ppf(1 - 1/(n_trials * math.e))
    sr_star = math.sqrt(var_sr_null) * z
    
    # Standard deviation of SR estimator
    denom = 1 - skewness * sr_observed + ((kurtosis - 1) / 4) * (sr_observed ** 2)
    std_sr = math.sqrt(max(0.0001, denom / (n_obs - 1)))
    
    t_stat = (sr_observed - sr_star) / std_sr
    dsr = norm.cdf(t_stat)
    return dsr, sr_star

n_obs = len(trades_df)
for name, col in [("Buy & Hold", "bh_net_return_pct"), ("2.5x ATR", "net_return_pct"), ("Tri-Layer", "trilayer_net_return_pct")]:
    s = trades_df[col]
    mean_s = s.mean()
    std_s = s.std()
    sr = (mean_s - 1.5) / std_s
    skew = float(s.skew())
    kurt = float(s.kurtosis()) + 3.0 # Pearson kurtosis
    dsr_val, sr_bench = deflated_sharpe_ratio(sr, n_trials=5, var_sr_null=0.01, skewness=skew, kurtosis=kurt, n_obs=n_obs)
    print(f"DSR {name} (N={n_obs}, skew={skew:.2f}, kurt={kurt:.2f}): {dsr_val*100:.1f}% (Benchmark SR: {sr_bench:.3f}, Observed SR: {sr:.3f})")

trades_df.to_csv("data/all_240_trades_comparison.csv", index=False)
print("\nSaved full 240 comparison dataset to data/all_240_trades_comparison.csv")
