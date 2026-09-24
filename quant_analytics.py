"""
quant_analytics.py - Institutional Quantitative & Operational Analytics Engine.

Calculates institutional-grade quantitative metrics for multi-portfolio walk-forward paper-trading:
1. Risk-Adjusted Returns:
   - 30-day Rolling Sharpe Ratio (annualized)
   - 30-day Rolling Sortino Ratio (annualized downside risk)
   - Maximum Drawdown (MDD)
   - Time-to-Recovery (days from peak through trough to recovery or current duration)
2. Multiple Testing Correction:
   - Deflated Sharpe Ratio (DSR) approximation (Bailey & López de Prado, 2014)
   - Bonferroni correction across the 10 portfolios to test if outperformance is statistically significant
3. Attribution & "Why" Metrics:
   - Position concentration (% of positive / total net PnL from top-1 and top-2 trades)
   - Median holding period (days, replacing noise-sensitive mean)
   - Median return vs. Mean return (skewness diagnostic)
4. Cross-Portfolio Correlation:
   - Daily and Weekly return correlation matrix across all 10 portfolios
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Standard trading frequency constants
TRADING_DAYS_PER_YEAR = 252
WEEKS_PER_YEAR = 52
DEFAULT_RISK_FREE_RATE = 0.02  # 2.0% annual risk-free rate benchmark


# ==============================================================================
# 1. RISK-ADJUSTED RETURN METRICS
# ==============================================================================

def calculate_returns(
    equity_series: pd.Series,
    freq: str = "D",
    weekly_smoothing: bool = False,
) -> pd.Series:
    """
    Extracts periodic returns from an equity time series.
    
    Args:
        equity_series: Series of equity values. DatetimeIndex is preferred.
        freq: Target frequency ("D" for daily, "W" for weekly).
        weekly_smoothing: If True, aggregates/resamples to weekly periods to eliminate daily noise.
        
    Returns:
        pd.Series: Percentage returns (decimal, e.g. 0.01 = 1%).
    """
    if equity_series is None or len(equity_series) < 2:
        return pd.Series(dtype=float)

    # Ensure index is datetime
    s = equity_series.copy()
    if not isinstance(s.index, pd.DatetimeIndex):
        try:
            s.index = pd.to_datetime(s.index, utc=True)
        except Exception:
            pass

    # Sort chronologically and drop duplicate timestamps
    s = s[~s.index.duplicated(keep="last")].sort_index()

    # Resample if index is datetime
    if isinstance(s.index, pd.DatetimeIndex):
        target_freq = "W-FRI" if weekly_smoothing or freq.upper().startswith("W") else "D"
        # Take the last recorded equity of each interval and forward fill gaps
        resampled = s.resample(target_freq).last().ffill().dropna()
        if len(resampled) < 2:
            resampled = s
        returns = resampled.pct_change().dropna()
    else:
        returns = s.pct_change().dropna()

    # Filter out infinite / NaN values
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    return returns


def calculate_rolling_sharpe(
    returns: pd.Series,
    window: int = 30,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pd.Series:
    """
    Computes rolling annualized Sharpe Ratio over a specified window.
    
    Formula:
        Sharpe = ((mean(returns) - rf_period) / std(returns)) * sqrt(periods_per_year)
        
    Args:
        returns: Periodic returns series.
        window: Rolling window size (e.g. 30 days).
        risk_free_rate: Annual risk-free rate.
        periods_per_year: 252 for daily, 52 for weekly.
        
    Returns:
        pd.Series: Rolling annualized Sharpe ratio.
    """
    if returns.empty or len(returns) < 2:
        return pd.Series(dtype=float)

    rf_per_period = risk_free_rate / periods_per_year
    excess_returns = returns - rf_per_period

    # Use min_periods = 3 to give early indications if history is short
    min_p = min(max(3, window // 5), len(returns))
    roll_mean = excess_returns.rolling(window=window, min_periods=min_p).mean()
    roll_std = returns.rolling(window=window, min_periods=min_p).std(ddof=1)

    # Avoid zero division
    roll_sharpe = (roll_mean / roll_std.replace(0, np.nan)) * math.sqrt(periods_per_year)
    return roll_sharpe


def calculate_rolling_sortino(
    returns: pd.Series,
    window: int = 30,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pd.Series:
    """
    Computes rolling annualized Sortino Ratio over a specified window.
    Replaces standard deviation with downside semi-deviation of negative excess returns.
    
    Formula:
        Downside Std = sqrt(mean(min(returns - rf, 0)^2))
        Sortino = ((mean(returns) - rf_period) / Downside Std) * sqrt(periods_per_year)
    """
    if returns.empty or len(returns) < 2:
        return pd.Series(dtype=float)

    rf_per_period = risk_free_rate / periods_per_year
    excess_returns = returns - rf_per_period
    min_p = min(max(3, window // 5), len(returns))

    roll_mean = excess_returns.rolling(window=window, min_periods=min_p).mean()

    # Downside semi-variance calculation
    downside_diff = excess_returns.apply(lambda x: min(0.0, x) ** 2)
    roll_downside_std = downside_diff.rolling(window=window, min_periods=min_p).mean().apply(np.sqrt)

    # If no downside deviations (all returns positive), downside_std is 0. Handle cleanly.
    roll_sortino = (roll_mean / roll_downside_std.replace(0, np.nan)) * math.sqrt(periods_per_year)
    return roll_sortino


def calculate_max_drawdown(equity_series: pd.Series) -> Tuple[float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """
    Calculates Maximum Drawdown (MDD) and identifies peak and trough timestamps.
    
    Returns:
        Tuple: (max_drawdown_pct_negative, peak_date, trough_date)
        e.g. (-0.0842, 2026-09-10, 2026-09-18) for -8.42% MDD.
    """
    if equity_series is None or len(equity_series) < 2:
        return 0.0, None, None

    cum_max = equity_series.cummax()
    drawdown = (equity_series - cum_max) / cum_max

    min_dd = float(drawdown.min())
    if np.isnan(min_dd) or min_dd >= 0:
        return 0.0, None, None

    trough_idx = drawdown.idxmin()
    peak_idx = equity_series.loc[:trough_idx].idxmax()

    return min_dd, peak_idx, trough_idx


def calculate_time_to_recovery(equity_series: pd.Series) -> int:
    """
    Calculates Time-to-Recovery (in days) from the Maximum Drawdown peak.
    
    - If recovered: Days from the MDD peak to the first date equity reached/exceeded the peak.
    - If still in drawdown: Days elapsed from MDD peak to the latest snapshot date.
    - If no drawdown exists: 0 days.
    """
    if equity_series is None or len(equity_series) < 2:
        return 0

    s = equity_series.copy()
    if not isinstance(s.index, pd.DatetimeIndex):
        try:
            s.index = pd.to_datetime(s.index, utc=True)
        except Exception:
            return 0

    mdd, peak_date, trough_date = calculate_max_drawdown(s)
    if mdd >= 0 or peak_date is None:
        return 0

    peak_value = float(s.loc[peak_date])
    after_trough = s.loc[trough_date:]

    # Check if equity reached back to peak
    recovered_points = after_trough[after_trough >= peak_value]
    if not recovered_points.empty:
        recovery_date = recovered_points.index[0]
        days = (recovery_date - peak_date).days
        return max(0, int(days))
    else:
        # Not yet recovered: return elapsed days in ongoing drawdown
        latest_date = s.index[-1]
        days = (latest_date - peak_date).days
        return max(0, int(days))


# ==============================================================================
# 2. MULTIPLE TESTING CORRECTION & SIGNIFICANCE (DSR & BONFERRONI)
# ==============================================================================

def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function Phi(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def deflated_sharpe_ratio(
    sharpe: float,
    all_sharpes: List[float],
    nb_trials: int = 10,
    sample_length: int = TRADING_DAYS_PER_YEAR,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> Dict[str, Any]:
    """
    Computes the Deflated Sharpe Ratio (DSR) approximation across N tested portfolios
    following Bailey & López de Prado (2014) / Marcos López de Prado (AFML).
    
    DSR corrects for:
    1. Selection bias / multiple testing (picking the best among N models).
    2. Non-normal returns (skewness & kurtosis fat-tails).
    3. Memory / sample length (T).
    
    Formula:
        E[max_N] ≈ sqrt(2 * ln(N)) + gamma / sqrt(2 * ln(N))
        where gamma ≈ 0.5772156649 (Euler-Mascheroni constant)
        
        SR* = std(all_sharpes) * E[max_N]
        sigma_SR = sqrt((1 - skew*SR + (kurtosis - 1)/4 * SR^2) / T)
        z = (SR - SR*) / sigma_SR
        DSR = Phi(z)
        
    Returns:
        Dict containing:
          - dsr: float between 0.0 and 1.0 (probability true SR > 0 under selection bias)
          - is_significant: bool (True if DSR >= 0.95, i.e. p < 0.05)
          - expected_max_sharpe: SR* benchmark under null
          - bonferroni_p_value: Bonferroni adjusted p-value
          - bonferroni_significant: bool
          - verdict: Institutional interpretation string
    """
    if np.isnan(sharpe) or not all_sharpes:
        return {
            "dsr": 0.0,
            "is_significant": False,
            "expected_max_sharpe": 0.0,
            "bonferroni_p_value": 1.0,
            "bonferroni_significant": False,
            "verdict": "Insufficient Data",
        }

    n = max(nb_trials, len(all_sharpes), 2)
    clean_sharpes = [s for s in all_sharpes if not np.isnan(s)]
    var_sharpes = float(np.var(clean_sharpes, ddof=1)) if len(clean_sharpes) > 1 else 0.5
    std_sharpes = math.sqrt(max(var_sharpes, 0.01))

    # Expected maximum Sharpe under Null Hypothesis of zero alpha across N trials
    gamma = 0.57721566490153286  # Euler-Mascheroni constant
    e_max = math.sqrt(2.0 * math.log(n)) + (gamma / math.sqrt(2.0 * math.log(n)))
    sr_benchmark = std_sharpes * e_max

    # Standard error of Sharpe with skewness and kurtosis adjustment
    t = max(sample_length, 30)
    denom = 1.0 - (skew * sharpe) + (((kurtosis - 1.0) / 4.0) * (sharpe ** 2))
    se_sharpe = math.sqrt(max(denom, 0.001) / t)

    # Deflated Sharpe z-score
    z_dsr = (sharpe - sr_benchmark) / se_sharpe
    dsr_value = norm_cdf(z_dsr)
    is_dsr_sig = dsr_value >= 0.95  # 95% confidence level

    # Bonferroni Multiple Testing Correction:
    # Single test p-value: z_single = sharpe / se_sharpe (testing H0: SR <= 0)
    z_single = sharpe / se_sharpe
    single_p = 1.0 - norm_cdf(z_single)
    bonf_p = min(1.0, single_p * n)
    bonf_sig = bonf_p < 0.05

    if is_dsr_sig and bonf_sig:
        verdict = "Statistically Significant (Outperforms Multiple Testing)"
    elif is_dsr_sig or bonf_sig:
        verdict = "Borderline Significant (Low Random Variance Risk)"
    else:
        verdict = "Random Variance / Not Significant (N=10 Selection Bias)"

    return {
        "dsr": round(dsr_value, 4),
        "is_significant": is_dsr_sig,
        "expected_max_sharpe": round(sr_benchmark, 3),
        "bonferroni_p_value": round(bonf_p, 4),
        "bonferroni_significant": bonf_sig,
        "verdict": verdict,
    }


# ==============================================================================
# 3. ATTRIBUTION & "WHY" METRICS
# ==============================================================================

def calculate_trade_attribution(trade_history_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Computes trade-level attribution metrics:
    - Position Concentration: % of total positive PnL generated by top 1 and top 2 trades
    - Median holding period in days (robust against long-tail dead money exits)
    - Median return vs Mean return (skewness diagnostic)
    - Total closed trades & win rate
    """
    default_res = {
        "total_trades": 0,
        "win_rate_pct": 0.0,
        "concentration_top1_pct": 0.0,
        "concentration_top2_pct": 0.0,
        "median_holding_days": 0.0,
        "mean_holding_days": 0.0,
        "median_return_pct": 0.0,
        "mean_return_pct": 0.0,
        "median_pnl_eur": 0.0,
        "mean_pnl_eur": 0.0,
        "total_net_pnl_eur": 0.0,
    }

    if trade_history_df is None or trade_history_df.empty:
        return default_res

    df = trade_history_df.copy()

    # Standardize column names
    col_map = {c.strip().lower().replace(" ", "_"): c for c in df.columns}
    pnl_col = col_map.get("net_pnl")
    buy_date_col = col_map.get("buy_date")
    sell_date_col = col_map.get("sell_date")
    invested_col = col_map.get("capital_invested")
    return_col = col_map.get("net_return")

    if not pnl_col or pnl_col not in df.columns:
        return default_res

    # Parse PnL to float
    df["clean_pnl"] = (
        df[pnl_col]
        .astype(str)
        .str.replace("€", "")
        .str.replace("$", "")
        .str.replace(",", "")
        .str.strip()
    )
    df["clean_pnl"] = pd.to_numeric(df["clean_pnl"], errors="coerce").fillna(0.0)

    # Parse Invested & Returns
    if invested_col and invested_col in df.columns:
        df["clean_invested"] = (
            df[invested_col]
            .astype(str)
            .str.replace("€", "")
            .str.replace("$", "")
            .str.replace(",", "")
            .str.strip()
        )
        df["clean_invested"] = pd.to_numeric(df["clean_invested"], errors="coerce").fillna(0.0)
    else:
        df["clean_invested"] = 0.0

    # Trade return pct
    if "clean_invested" in df.columns and (df["clean_invested"] > 0).any():
        df["trade_ret_pct"] = np.where(
            df["clean_invested"] > 0,
            (df["clean_pnl"] / df["clean_invested"]) * 100.0,
            0.0,
        )
    else:
        df["trade_ret_pct"] = df["clean_pnl"]

    total_trades = len(df)
    if total_trades == 0:
        return default_res

    wins = df[df["clean_pnl"] > 0]
    win_rate = (len(wins) / total_trades) * 100.0
    total_net_pnl = float(df["clean_pnl"].sum())

    # Concentration Calculation:
    # Quant definition: fraction of gross positive gains generated by top 1 or 2 winners
    total_positive_pnl = float(wins["clean_pnl"].sum()) if not wins.empty else 0.0
    top_trades = df["clean_pnl"].sort_values(ascending=False).values

    if total_positive_pnl > 0:
        top1_pct = float((top_trades[0] / total_positive_pnl) * 100.0) if len(top_trades) >= 1 and top_trades[0] > 0 else 0.0
        top2_sum = float(top_trades[:2].sum()) if len(top_trades) >= 2 else float(top_trades[0])
        top2_pct = float((top2_sum / total_positive_pnl) * 100.0) if top2_sum > 0 else top1_pct
    elif total_net_pnl > 0:
        top1_pct = float((top_trades[0] / total_net_pnl) * 100.0) if len(top_trades) >= 1 and top_trades[0] > 0 else 0.0
        top2_sum = float(top_trades[:2].sum()) if len(top_trades) >= 2 else float(top_trades[0])
        top2_pct = float((top2_sum / total_net_pnl) * 100.0) if top2_sum > 0 else top1_pct
    else:
        top1_pct = 0.0
        top2_pct = 0.0

    # Holding Period Calculation
    holding_days_list: List[int] = []
    if buy_date_col and sell_date_col and buy_date_col in df.columns and sell_date_col in df.columns:
        b_dates = pd.to_datetime(df[buy_date_col], errors="coerce")
        s_dates = pd.to_datetime(df[sell_date_col], errors="coerce")
        valid_dates = (~b_dates.isna()) & (~s_dates.isna())
        if valid_dates.any():
            diffs = (s_dates[valid_dates] - b_dates[valid_dates]).dt.days
            holding_days_list = [max(0, int(d)) for d in diffs if not np.isnan(d)]

    median_hold = float(np.median(holding_days_list)) if holding_days_list else 0.0
    mean_hold = float(np.mean(holding_days_list)) if holding_days_list else 0.0

    return {
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 1),
        "concentration_top1_pct": round(top1_pct, 1),
        "concentration_top2_pct": round(top2_pct, 1),
        "median_holding_days": round(median_hold, 1),
        "mean_holding_days": round(mean_hold, 1),
        "median_return_pct": round(float(df["trade_ret_pct"].median()), 2),
        "mean_return_pct": round(float(df["trade_ret_pct"].mean()), 2),
        "median_pnl_eur": round(float(df["clean_pnl"].median()), 2),
        "mean_pnl_eur": round(float(df["clean_pnl"].mean()), 2),
        "total_net_pnl_eur": round(total_net_pnl, 2),
    }


# ==============================================================================
# 4. CROSS-PORTFOLIO CORRELATION MATRIX
# ==============================================================================

def build_portfolio_correlation_matrix(
    equity_series_dict: Dict[str, pd.Series],
    freq: str = "D",
    weekly_smoothing: bool = False,
) -> pd.DataFrame:
    """
    Builds a periodic return correlation matrix across all 10 portfolios.
    
    Args:
        equity_series_dict: Dict mapping {portfolio_id: equity_series}.
        freq: "D" for daily, "W" for weekly.
        weekly_smoothing: If True, aggregates to weekly returns to eliminate micro daily noise.
        
    Returns:
        pd.DataFrame: 10x10 correlation matrix with values between -1.0 and 1.0.
    """
    if not equity_series_dict:
        return pd.DataFrame()

    returns_dict: Dict[str, pd.Series] = {}
    for pid, s in equity_series_dict.items():
        if s is not None and len(s) >= 2:
            r = calculate_returns(s, freq=freq, weekly_smoothing=weekly_smoothing)
            if not r.empty:
                returns_dict[pid] = r

    if not returns_dict:
        # Fallback: empty matrix with portfolio names
        pids = list(equity_series_dict.keys())
        return pd.DataFrame(np.eye(len(pids)), index=pids, columns=pids)

    combined_returns = pd.DataFrame(returns_dict)

    # Compute correlation with minimum 2 valid pairwise points
    corr_matrix = combined_returns.corr(method="pearson", min_periods=2)

    # Replace NaNs on diagonal with 1.0, off-diagonal with 0.0 if uncalculated
    for col in corr_matrix.columns:
        corr_matrix.loc[col, col] = 1.0
    corr_matrix = corr_matrix.fillna(0.0)

    # Ensure all original portfolios are present in the matrix
    all_keys = list(equity_series_dict.keys())
    corr_matrix = corr_matrix.reindex(index=all_keys, columns=all_keys).fillna(0.0)
    for k in all_keys:
        corr_matrix.loc[k, k] = 1.0

    return corr_matrix.round(3)


# ==============================================================================
# 5. HIGH-LEVEL MULTI-PORTFOLIO INSTITUTIONAL SUMMARY BUILDER
# ==============================================================================

def load_portfolio_data(
    portfolios_dir: Path,
) -> Tuple[Dict[str, pd.Series], Dict[str, pd.DataFrame]]:
    """
    Reads all 10 portfolios' equity series and trade history CSVs from data/portfolios/.
    
    Returns:
        Tuple: (equity_dict, trades_dict)
    """
    equity_dict: Dict[str, pd.Series] = {}
    trades_dict: Dict[str, pd.DataFrame] = {}

    PORTFOLIO_IDS = [
        "P1_Base",
        "P2_Fast_Cycle",
        "P3_Diamond_Hands",
        "P4_Institutional",
        "P5_Nordic_Only",
        "P6_US_Only",
        "P7_Deep_Value_Extreme",
        "P8_Quality_Growth",
        "P9_High_Conviction",
        "P10_Micro_Sniper",
    ]

    for pid in PORTFOLIO_IDS:
        eq_file = portfolios_dir / f"portfolio_{pid}_history.json"
        csv_file = portfolios_dir / f"portfolio_{pid}_history.csv"

        # Load equity curve
        if eq_file.exists():
            try:
                import json
                with open(eq_file, "r", encoding="utf-8") as f:
                    eq_data = json.load(f)
                if isinstance(eq_data, list) and len(eq_data) > 0:
                    df_eq = pd.DataFrame(eq_data)
                    if "timestamp" in df_eq.columns and "total_equity" in df_eq.columns:
                        df_eq["timestamp"] = pd.to_datetime(df_eq["timestamp"], utc=True, errors="coerce")
                        df_eq["total_equity"] = pd.to_numeric(df_eq["total_equity"], errors="coerce")
                        df_eq = df_eq.dropna(subset=["timestamp", "total_equity"]).sort_values("timestamp")
                        if not df_eq.empty:
                            equity_dict[pid] = pd.Series(
                                df_eq["total_equity"].values,
                                index=df_eq["timestamp"],
                                name=pid,
                            )
            except Exception as e:
                logger.debug(f"Could not load equity for {pid}: {e}")

        # Load trade history CSV
        if csv_file.exists():
            try:
                df_trades = pd.read_csv(csv_file)
                trades_dict[pid] = df_trades
            except Exception as e:
                logger.debug(f"Could not load trades for {pid}: {e}")
        else:
            trades_dict[pid] = pd.DataFrame()

    return equity_dict, trades_dict


def generate_institutional_summary(
    portfolios_dir: Path,
    weekly_smoothing: bool = False,
    rolling_window: int = 30,
) -> Dict[str, Any]:
    """
    Master generator for institutional quantitative analysis across 10 portfolios.
    
    Generates:
    1. Summary DataFrame (Rows = 10 Portfolios.
       Columns = 30d Sharpe, 30d Sortino, Median Return, Mean Return, Max DD, Top-1 PnL %, Median Hold Days).
    2. 10x10 Return Correlation Matrix.
    3. Deflated Sharpe Ratio (DSR) & Bonferroni multiple testing report for best portfolio.
    4. Attribution metrics breakdown.
    """
    equity_dict, trades_dict = load_portfolio_data(portfolios_dir)

    PORTFOLIO_IDS = [
        "P1_Base",
        "P2_Fast_Cycle",
        "P3_Diamond_Hands",
        "P4_Institutional",
        "P5_Nordic_Only",
        "P6_US_Only",
        "P7_Deep_Value_Extreme",
        "P8_Quality_Growth",
        "P9_High_Conviction",
        "P10_Micro_Sniper",
    ]

    # Pre-compute Sharpes across all portfolios to feed into DSR
    sharpes_map: Dict[str, float] = {}
    sortinos_map: Dict[str, float] = {}
    returns_map: Dict[str, pd.Series] = {}

    for pid in PORTFOLIO_IDS:
        eq = equity_dict.get(pid)
        if eq is not None and len(eq) >= 2:
            r = calculate_returns(eq, weekly_smoothing=weekly_smoothing)
            returns_map[pid] = r
            if not r.empty and len(r) >= 2:
                roll_sh = calculate_rolling_sharpe(r, window=rolling_window)
                roll_so = calculate_rolling_sortino(r, window=rolling_window)
                latest_sh = float(roll_sh.dropna().iloc[-1]) if not roll_sh.dropna().empty else 0.0
                latest_so = float(roll_so.dropna().iloc[-1]) if not roll_so.dropna().empty else 0.0
                sharpes_map[pid] = latest_sh
                sortinos_map[pid] = latest_so
            else:
                sharpes_map[pid] = 0.0
                sortinos_map[pid] = 0.0
        else:
            sharpes_map[pid] = 0.0
            sortinos_map[pid] = 0.0

    all_sharpe_values = list(sharpes_map.values())

    # Build Summary Rows matching user specification
    summary_rows: List[Dict[str, Any]] = []

    for pid in PORTFOLIO_IDS:
        eq = equity_dict.get(pid)
        trades_df = trades_dict.get(pid, pd.DataFrame())
        attrib = calculate_trade_attribution(trades_df)

        # Risk-adjusted metrics
        sharpe_val = sharpes_map.get(pid, 0.0)
        sortino_val = sortinos_map.get(pid, 0.0)

        # Max Drawdown & Recovery
        if eq is not None and len(eq) >= 2:
            mdd_val, _, _ = calculate_max_drawdown(eq)
            recovery_days = calculate_time_to_recovery(eq)
            # Daily equity returns for Median vs Mean equity return
            r_series = returns_map.get(pid, pd.Series())
            if not r_series.empty:
                median_ret = float(r_series.median() * 100.0)
                mean_ret = float(r_series.mean() * 100.0)
            else:
                median_ret = attrib["median_return_pct"]
                mean_ret = attrib["mean_return_pct"]
        else:
            mdd_val = 0.0
            recovery_days = 0
            median_ret = attrib["median_return_pct"]
            mean_ret = attrib["mean_return_pct"]

        summary_rows.append({
            "Portfolio": pid,
            "30d Sharpe": round(sharpe_val, 2),
            "30d Sortino": round(sortino_val, 2),
            "Median Return": f"{median_ret:+.2f}%",
            "Mean Return": f"{mean_ret:+.2f}%",
            "Max DD": f"{mdd_val * 100.0:+.2f}%",
            "Top-1 PnL %": f"{attrib['concentration_top1_pct']:.1f}%",
            "Median Hold Days": f"{attrib['median_holding_days']:.0f}d",
            "_raw_sharpe": sharpe_val,
            "_raw_sortino": sortino_val,
            "_raw_mdd": mdd_val,
            "_trades_count": attrib["total_trades"],
            "_recovery_days": recovery_days,
        })

    summary_df = pd.DataFrame(summary_rows)

    # Correlation Matrix
    corr_matrix = build_portfolio_correlation_matrix(
        equity_dict,
        weekly_smoothing=weekly_smoothing,
    )

    # Deflated Sharpe Ratio / Multiple Testing analysis on best portfolio
    best_pid = max(sharpes_map, key=sharpes_map.get) if sharpes_map else "P1_Base"
    best_sharpe = sharpes_map.get(best_pid, 0.0)

    dsr_report = deflated_sharpe_ratio(
        sharpe=best_sharpe,
        all_sharpes=all_sharpe_values,
        nb_trials=len(PORTFOLIO_IDS),
        sample_length=TRADING_DAYS_PER_YEAR,
    )
    dsr_report["best_portfolio"] = best_pid
    dsr_report["best_sharpe"] = round(best_sharpe, 2)

    return {
        "summary_df": summary_df,
        "correlation_matrix": corr_matrix,
        "dsr_report": dsr_report,
        "equity_dict": equity_dict,
        "trades_dict": trades_dict,
    }
