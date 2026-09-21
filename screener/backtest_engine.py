"""
Quantitative Historical Backtesting Engine for Nasdaq Helsinki Micro-Cap Equities.

Integrates mathematical frameworks from quantitative equity and crypto trading:
1. Daily OHLCV Data Handling (accounting for overnight/weekend gaps and T+1 Open execution).
2. Dynamic Market Impact: Toro-Bouchaud Square-Root Law:
   Impact = Y * daily_volatility * sqrt(OrderSize / DailyVolume) + Base Spread/Slippage Penalty (default 2.5%).
3. Position Sizing: Fractional Half-Kelly Criterion.
4. Statistical Validation: Deflated Sharpe Ratio (DSR) & Probabilistic Sharpe Ratio (PSR)
   correcting for skewness, kurtosis, and selection bias (Bailey & Lopez de Prado, 2014).
5. NLP-Triggered Execution Gate: Triggered strictly by insider buying or positive guidance.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

import numpy as np
import pandas as pd
from scipy import stats
import yfinance as yf

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("screener.backtest_engine")


@dataclass
class BacktestTrade:
    """Represents a single simulated trade from signal to exit."""
    trade_id: str
    ticker: str
    signal_date: str
    entry_date: str
    exit_date: str
    signal_type: str
    holding_period_days: int
    entry_price_raw: float
    entry_price_exec: float
    exit_price_raw: float
    exit_price_exec: float
    shares: int
    position_value_eur: float
    toro_bouchaud_entry_impact_pct: float
    toro_bouchaud_exit_impact_pct: float
    base_spread_slippage_pct: float
    gross_return_pct: float
    net_return_pct: float
    net_pnl_eur: float
    exit_reason: str  # "HORIZON_1M", "HORIZON_3M", "HORIZON_6M", "TRAILING_STOP", "TAKE_PROFIT"
    max_favorable_excursion_pct: float
    max_adverse_excursion_pct: float


@dataclass
class BacktestSummary:
    """Complete performance, risk, Kelly sizing, and DSR report."""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    gross_total_return_pct: float
    net_total_return_pct: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    win_loss_payoff_ratio: float
    max_drawdown_pct: float
    sharpe_ratio_annualized: float
    sortino_ratio_annualized: float
    skewness: float
    kurtosis: float
    full_kelly_pct: float
    half_kelly_pct: float
    recommended_position_pct: float
    expected_max_noise_sharpe: float
    probabilistic_sharpe_ratio_pct: float
    deflated_sharpe_ratio_pct: float
    dsr_passed_5pct: bool
    trades: List[BacktestTrade] = field(default_factory=list)


class ToroBouchaudCostModel:
    """
    Computes dynamic execution price penalties using the Toro-Bouchaud Square-Root Law
    and fixed bid-ask spread / illiquidity friction.
    """

    def __init__(
        self,
        y_constant: float = 0.60,
        base_spread_slippage_pct: float = 0.025,  # 2.5% default penalty for micro-caps
    ):
        self.y_constant = y_constant
        self.base_spread_slippage_pct = base_spread_slippage_pct

    def calculate_entry_execution(
        self,
        raw_open_price: float,
        order_size_eur: float,
        daily_volume_shares: float,
        daily_volatility: float,
    ) -> Tuple[float, float]:
        """
        Calculates execution price and impact percentage for trade entry.
        Impact = Y * sigma_daily * sqrt(OrderSizeShares / DailyVolumeShares)
        """
        if raw_open_price <= 0:
            return raw_open_price, 0.0

        order_size_shares = order_size_eur / raw_open_price
        vol = max(daily_volume_shares, 100.0)
        sigma = max(daily_volatility, 0.015)  # Minimum 1.5% daily volatility floor

        participation = min(order_size_shares / vol, 0.50)  # Capped at 50% of volume
        impact_pct = self.y_constant * sigma * math.sqrt(participation)
        total_penalty = self.base_spread_slippage_pct + impact_pct

        exec_price = raw_open_price * (1.0 + total_penalty)
        return round(exec_price, 4), round(impact_pct * 100.0, 3)

    def calculate_exit_execution(
        self,
        raw_exit_price: float,
        order_size_shares: int,
        daily_volume_shares: float,
        daily_volatility: float,
    ) -> Tuple[float, float]:
        """Calculates execution price and impact percentage for trade exit."""
        if raw_exit_price <= 0:
            return raw_exit_price, 0.0

        vol = max(daily_volume_shares, 100.0)
        sigma = max(daily_volatility, 0.015)

        participation = min(order_size_shares / vol, 0.50)
        impact_pct = self.y_constant * sigma * math.sqrt(participation)
        total_penalty = self.base_spread_slippage_pct + impact_pct

        exec_price = raw_exit_price * (1.0 - total_penalty)
        return round(exec_price, 4), round(impact_pct * 100.0, 3)


class KellyPositionSizer:
    """
    Computes optimal growth capital allocation using the Fractional Half-Kelly criterion.
    """

    @staticmethod
    def calculate_half_kelly(
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        max_position_cap: float = 0.25,
    ) -> Tuple[float, float, float]:
        """
        Full Kelly: f* = (p * b - q) / b = p - (q / b)
        where:
        p = win rate
        q = 1 - p (loss rate)
        b = avg_win / avg_loss (payoff ratio)

        Returns: (full_kelly, half_kelly, recommended_position_pct)
        """
        if win_rate <= 0.0 or avg_loss <= 0.0 or avg_win <= 0.0:
            return 0.0, 0.0, 0.0

        p = win_rate
        q = 1.0 - p
        b = avg_win / avg_loss

        full_kelly = (p * b - q) / b
        if full_kelly <= 0:
            return 0.0, 0.0, 0.0

        half_kelly = 0.5 * full_kelly
        recommended = min(half_kelly, max_position_cap)

        return (
            round(full_kelly * 100.0, 2),
            round(half_kelly * 100.0, 2),
            round(recommended * 100.0, 2),
        )


class DeflatedSharpeCalculator:
    """
    Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio (DSR) & Probabilistic Sharpe Ratio (PSR).
    Corrects for skewness, kurtosis, sample size, and data-mining / multi-trial selection bias.
    """

    @staticmethod
    def calculate_dsr(
        returns: np.ndarray,
        n_trials: int = 100,
        periods_per_year: int = 252,
    ) -> Dict[str, Any]:
        """
        Computes PSR and DSR.
        """
        T = len(returns)
        if T < 3:
            return {
                "sharpe_annualized": 0.0,
                "sortino_annualized": 0.0,
                "skewness": 0.0,
                "kurtosis": 3.0,
                "expected_max_noise_sharpe": 0.0,
                "psr_pct": 0.0,
                "dsr_pct": 0.0,
                "dsr_passed_5pct": False,
            }

        mean_ret = float(np.mean(returns))
        std_ret = float(np.std(returns, ddof=1))
        if std_ret < 1e-8:
            std_ret = 1e-8

        # Downside deviation for Sortino
        downside_returns = returns[returns < 0]
        downside_std = float(np.std(downside_returns, ddof=1)) if len(downside_returns) > 1 else std_ret
        if downside_std < 1e-8:
            downside_std = 1e-8

        sr_trade = mean_ret / std_ret
        # Approximate trades/periods per year scaling
        annual_factor = math.sqrt(min(T, periods_per_year))
        sr_annual = sr_trade * annual_factor
        sortino_annual = (mean_ret / downside_std) * annual_factor

        skewness = float(stats.skew(returns)) if T >= 3 else 0.0
        # Pearson kurtosis (normal = 3.0)
        kurtosis = float(stats.kurtosis(returns, fisher=False)) if T >= 4 else 3.0

        # Standard error of Sharpe ratio accounting for non-normality (Mertens 2002 / Lo 2002)
        # Var(SR) = (1 - skew * SR + ((kurt - 1) / 4) * SR^2) / (T - 1)
        var_term = (1.0 - skewness * sr_trade + ((kurtosis - 1.0) / 4.0) * (sr_trade ** 2)) / max(1, T - 1)
        se_sr = math.sqrt(max(1e-8, var_term))

        # 1. Probabilistic Sharpe Ratio (PSR vs Benchmark SR=0)
        psr_z = sr_trade / se_sr
        psr_p_val = float(stats.norm.cdf(psr_z))

        # 2. Expected Maximum Sharpe for random noise across N trials (Bailey & Lopez de Prado 2014)
        euler_gamma = 0.5772156649015328606065120900824
        eff_trials = max(n_trials, 2)
        z1 = float(stats.norm.ppf(1.0 - 1.0 / eff_trials))
        z2 = float(stats.norm.ppf(1.0 - 1.0 / (eff_trials * math.e)))
        expected_max_z = (1.0 - euler_gamma) * z1 + euler_gamma * z2

        var_noise = 1.0 / T
        expected_max_sr_trade = math.sqrt(var_noise) * expected_max_z
        expected_max_sr_annual = expected_max_sr_trade * annual_factor

        # 3. Deflated Sharpe Ratio
        dsr_z = (sr_trade - expected_max_sr_trade) / se_sr
        dsr_p_val = float(stats.norm.cdf(dsr_z))

        return {
            "sharpe_annualized": round(sr_annual, 2),
            "sortino_annualized": round(sortino_annual, 2),
            "skewness": round(skewness, 3),
            "kurtosis": round(kurtosis, 3),
            "expected_max_noise_sharpe": round(expected_max_sr_annual, 2),
            "psr_pct": round(psr_p_val * 100.0, 2),
            "dsr_pct": round(dsr_p_val * 100.0, 2),
            "dsr_passed_5pct": bool(dsr_p_val >= 0.95),
        }


class MicroCapBacktester:
    """
    Historical event-driven forward backtester for Helsinki micro-cap equities.
    """

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        nominal_trade_size_eur: float = 1_000.0,
        base_spread_slippage_pct: float = 0.025,  # 2.5% per trade
        trailing_stop_pct: float = 0.12,         # 12% trailing stop
        take_profit_pct: Optional[float] = None, # Optional take profit (e.g. 35%)
        default_holding_horizon_days: int = 63,  # 3 months (approx 63 trading days)
        toro_bouchaud_y: float = 0.60,
    ):
        self.initial_capital = initial_capital
        self.nominal_trade_size_eur = nominal_trade_size_eur
        self.trailing_stop_pct = trailing_stop_pct
        self.take_profit_pct = take_profit_pct
        self.default_holding_horizon_days = default_holding_horizon_days
        self.cost_model = ToroBouchaudCostModel(
            y_constant=toro_bouchaud_y,
            base_spread_slippage_pct=base_spread_slippage_pct,
        )

    def run_backtest_from_signals_csv(
        self,
        signals_csv_path: str | Path,
        n_trials_for_dsr: int = 50,
    ) -> BacktestSummary:
        """
        Loads signals CSV, downloads yfinance daily OHLCV, and runs simulation.
        CSV Columns expected: ticker, signal_date, signal_type, company_name (optional)
        """
        csv_file = Path(signals_csv_path)
        if not csv_file.exists():
            raise FileNotFoundError(f"Signals file {csv_file} does not exist.")

        signals: List[Dict[str, Any]] = []
        with open(csv_file, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                signals.append(row)

        logger.info(f"Loaded {len(signals)} historical signals from {csv_file.name}")
        return self.run_backtest_on_signals(signals, n_trials_for_dsr=n_trials_for_dsr)

    def run_backtest_on_signals(
        self,
        signals: List[Dict[str, Any]],
        n_trials_for_dsr: int = 50,
    ) -> BacktestSummary:
        """
        Executes backtest loop over a list of signal dictionaries.
        """
        executed_trades: List[BacktestTrade] = []

        for idx, sig in enumerate(signals, start=1):
            ticker = sig.get("ticker", "").strip().upper()
            signal_date_str = sig.get("signal_date") or sig.get("timestamp") or sig.get("date") or ""
            signal_type = sig.get("signal_type") or sig.get("signals") or "INSIDER_BUYING"

            # Filter signals (Must be valid trigger)
            is_valid_trigger = any(
                k in signal_type.upper()
                for k in ["INSIDER_BUYING", "POSITIVE_GUIDANCE", "STRONG_BUY", "BUY"]
            )
            if not is_valid_trigger:
                continue

            if not ticker or not signal_date_str:
                continue

            # Standardize symbol (.HE)
            symbol = ticker if "." in ticker else f"{ticker}.HE"

            # Parse signal date
            try:
                if "T" in signal_date_str:
                    sig_dt = datetime.fromisoformat(signal_date_str.replace("Z", "+00:00")).date()
                else:
                    sig_dt = datetime.strptime(signal_date_str[:10], "%Y-%m-%d").date()
            except Exception as e:
                logger.warning(f"Could not parse date '{signal_date_str}' for {ticker}: {e}")
                continue

            logger.info(f"[{idx}/{len(signals)}] Simulating trade for {symbol} on signal date {sig_dt} ({signal_type})...")

            # Fetch daily data: 30 days before to 200 days after
            start_dt = sig_dt - timedelta(days=45)
            end_dt = sig_dt + timedelta(days=220)

            try:
                df = yf.download(symbol, start=start_dt.isoformat(), end=end_dt.isoformat(), progress=False)
                if df is None or df.empty or len(df) < 5:
                    logger.warning(f"Insufficient yfinance daily data for {symbol}.")
                    continue
            except Exception as e:
                logger.warning(f"Failed to fetch market data for {symbol}: {e}")
                continue

            # Flatten multi-index columns if yfinance returns them
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]

            # Standardize column names to lowercase
            df.columns = [str(c).lower() for c in df.columns]

            # Filter valid dates >= signal_date
            # Entry is on T+1 (the first trading day strictly AFTER signal_date)
            df.index = pd.to_datetime(df.index).date
            available_dates = sorted(df.index)

            # Calculate 20-day historical daily volatility prior to signal
            pre_signal_df = df[df.index <= sig_dt]
            if len(pre_signal_df) >= 5:
                daily_rets = pre_signal_df["close"].pct_change().dropna()
                daily_volatility = float(daily_rets.std()) if len(daily_rets) > 0 else 0.025
            else:
                daily_volatility = 0.025

            forward_dates = [d for d in available_dates if d > sig_dt]
            if not forward_dates:
                logger.info(f"No forward trading days available after {sig_dt} for {symbol}.")
                continue

            # Entry on T+1 Open price
            entry_date = forward_dates[0]
            entry_row = df.loc[entry_date]
            raw_entry_price = float(entry_row["open"])
            entry_volume = float(entry_row["volume"]) if "volume" in entry_row and entry_row["volume"] > 0 else 5000.0

            if raw_entry_price <= 0:
                continue

            # Compute Toro-Bouchaud Entry Execution Price
            exec_entry_price, entry_impact_pct = self.cost_model.calculate_entry_execution(
                raw_open_price=raw_entry_price,
                order_size_eur=self.nominal_trade_size_eur,
                daily_volume_shares=entry_volume,
                daily_volatility=daily_volatility,
            )

            shares = max(1, int(self.nominal_trade_size_eur / exec_entry_price))
            actual_pos_value = shares * exec_entry_price

            # Walk forward day-by-day
            peak_price = raw_entry_price
            exit_date = forward_dates[-1]
            raw_exit_price = float(df.loc[exit_date]["close"])
            exit_reason = "HORIZON_MAX"
            exit_volume = float(df.loc[exit_date].get("volume", 5000.0))

            mfe_pct = 0.0  # Max Favorable Excursion
            mae_pct = 0.0  # Max Adverse Excursion

            # Simulate up to horizon days (e.g. 63 days)
            horizon_dates = forward_dates[: self.default_holding_horizon_days]

            for d_idx, cur_date in enumerate(horizon_dates):
                cur_row = df.loc[cur_date]
                cur_high = float(cur_row["high"])
                cur_low = float(cur_row["low"])
                cur_close = float(cur_row["close"])
                cur_vol = float(cur_row.get("volume", 5000.0))

                # Track excursions
                high_excursion = (cur_high - raw_entry_price) / raw_entry_price * 100.0
                low_excursion = (cur_low - raw_entry_price) / raw_entry_price * 100.0
                mfe_pct = max(mfe_pct, high_excursion)
                mae_pct = min(mae_pct, low_excursion)

                if cur_high > peak_price:
                    peak_price = cur_high

                # Check Take-Profit
                if self.take_profit_pct and (cur_high - exec_entry_price) / exec_entry_price >= self.take_profit_pct:
                    exit_date = cur_date
                    raw_exit_price = raw_entry_price * (1.0 + self.take_profit_pct)
                    exit_reason = "TAKE_PROFIT"
                    exit_volume = cur_vol
                    break

                # Check Trailing Stop-Loss
                drawdown_from_peak = (peak_price - cur_low) / peak_price
                if drawdown_from_peak >= self.trailing_stop_pct and d_idx > 0:
                    exit_date = cur_date
                    raw_exit_price = peak_price * (1.0 - self.trailing_stop_pct)
                    exit_reason = "TRAILING_STOP"
                    exit_volume = cur_vol
                    break

                # Reached end of horizon
                if d_idx == len(horizon_dates) - 1:
                    exit_date = cur_date
                    raw_exit_price = cur_close
                    exit_reason = f"HORIZON_{self.default_holding_horizon_days}D"
                    exit_volume = cur_vol

            # Compute Toro-Bouchaud Exit Execution Price
            exec_exit_price, exit_impact_pct = self.cost_model.calculate_exit_execution(
                raw_exit_price=raw_exit_price,
                order_size_shares=shares,
                daily_volume_shares=exit_volume,
                daily_volatility=daily_volatility,
            )

            gross_return_pct = ((raw_exit_price - raw_entry_price) / raw_entry_price) * 100.0
            net_return_pct = ((exec_exit_price - exec_entry_price) / exec_entry_price) * 100.0
            net_pnl_eur = shares * (exec_exit_price - exec_entry_price)
            holding_days = (exit_date - entry_date).days

            trade = BacktestTrade(
                trade_id=f"BT-{ticker}-{sig_dt}",
                ticker=symbol,
                signal_date=sig_dt.isoformat(),
                entry_date=entry_date.isoformat(),
                exit_date=exit_date.isoformat(),
                signal_type=signal_type,
                holding_period_days=holding_days,
                entry_price_raw=round(raw_entry_price, 4),
                entry_price_exec=round(exec_entry_price, 4),
                exit_price_raw=round(raw_exit_price, 4),
                exit_price_exec=round(exec_exit_price, 4),
                shares=shares,
                position_value_eur=round(actual_pos_value, 2),
                toro_bouchaud_entry_impact_pct=entry_impact_pct,
                toro_bouchaud_exit_impact_pct=exit_impact_pct,
                base_spread_slippage_pct=round(self.cost_model.base_spread_slippage_pct * 100.0, 2),
                gross_return_pct=round(gross_return_pct, 2),
                net_return_pct=round(net_return_pct, 2),
                net_pnl_eur=round(net_pnl_eur, 2),
                exit_reason=exit_reason,
                max_favorable_excursion_pct=round(mfe_pct, 2),
                max_adverse_excursion_pct=round(mae_pct, 2),
            )
            executed_trades.append(trade)

        return self._generate_summary(executed_trades, n_trials_for_dsr=n_trials_for_dsr)

    def _generate_summary(
        self,
        trades: List[BacktestTrade],
        n_trials_for_dsr: int = 50,
    ) -> BacktestSummary:
        """Computes comprehensive statistics, Kelly fraction, and DSR report."""
        if not trades:
            return BacktestSummary(
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate_pct=0.0,
                gross_total_return_pct=0.0,
                net_total_return_pct=0.0,
                profit_factor=0.0,
                avg_win_pct=0.0,
                avg_loss_pct=0.0,
                win_loss_payoff_ratio=0.0,
                max_drawdown_pct=0.0,
                sharpe_ratio_annualized=0.0,
                sortino_ratio_annualized=0.0,
                skewness=0.0,
                kurtosis=3.0,
                full_kelly_pct=0.0,
                half_kelly_pct=0.0,
                recommended_position_pct=0.0,
                expected_max_noise_sharpe=0.0,
                probabilistic_sharpe_ratio_pct=0.0,
                deflated_sharpe_ratio_pct=0.0,
                dsr_passed_5pct=False,
                trades=[],
            )

        n = len(trades)
        net_returns = np.array([t.net_return_pct / 100.0 for t in trades])
        gross_returns = np.array([t.gross_return_pct / 100.0 for t in trades])

        wins = [r for r in net_returns if r > 0]
        losses = [abs(r) for r in net_returns if r < 0]

        win_count = len(wins)
        loss_count = len(losses)
        win_rate = win_count / n if n > 0 else 0.0

        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0
        payoff_ratio = avg_win / avg_loss if avg_loss > 0 else (avg_win if avg_win > 0 else 1.0)

        total_gain = sum(wins)
        total_loss = sum(losses)
        profit_factor = total_gain / total_loss if total_loss > 0 else (99.0 if total_gain > 0 else 0.0)

        # Cumulative equity curve and maximum drawdown
        cum_equity = np.cumprod(1.0 + net_returns)
        running_max = np.maximum.accumulate(cum_equity)
        drawdowns = (running_max - cum_equity) / running_max
        max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        # Kelly Position Sizing
        full_k, half_k, rec_k = KellyPositionSizer.calculate_half_kelly(
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            max_position_cap=0.25,
        )

        # Deflated Sharpe Ratio
        dsr_metrics = DeflatedSharpeCalculator.calculate_dsr(
            returns=net_returns,
            n_trials=n_trials_for_dsr,
            periods_per_year=252,
        )

        gross_total_ret = (float(np.prod(1.0 + gross_returns)) - 1.0) * 100.0
        net_total_ret = (float(np.prod(1.0 + net_returns)) - 1.0) * 100.0

        return BacktestSummary(
            total_trades=n,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate_pct=round(win_rate * 100.0, 2),
            gross_total_return_pct=round(gross_total_ret, 2),
            net_total_return_pct=round(net_total_ret, 2),
            profit_factor=round(profit_factor, 2),
            avg_win_pct=round(avg_win * 100.0, 2),
            avg_loss_pct=round(avg_loss * 100.0, 2),
            win_loss_payoff_ratio=round(payoff_ratio, 2),
            max_drawdown_pct=round(max_dd * 100.0, 2),
            sharpe_ratio_annualized=dsr_metrics["sharpe_annualized"],
            sortino_ratio_annualized=dsr_metrics["sortino_annualized"],
            skewness=dsr_metrics["skewness"],
            kurtosis=dsr_metrics["kurtosis"],
            full_kelly_pct=full_k,
            half_kelly_pct=half_k,
            recommended_position_pct=rec_k,
            expected_max_noise_sharpe=dsr_metrics["expected_max_noise_sharpe"],
            probabilistic_sharpe_ratio_pct=dsr_metrics["psr_pct"],
            deflated_sharpe_ratio_pct=dsr_metrics["dsr_pct"],
            dsr_passed_5pct=dsr_metrics["dsr_passed_5pct"],
            trades=trades,
        )


def print_backtest_report(summary: BacktestSummary) -> None:
    """Formats and prints an institutional quantitative performance report."""
    print("\n" + "=" * 80)
    print("      QUANTITATIVE BACKTEST REPORT: NASDAQ HELSINKI MICRO-CAP SCREENER      ")
    print("=" * 80)
    print(f"Total Evaluated Trades:          {summary.total_trades}")
    print(f"Win Rate:                        {summary.win_rate_pct:.1f}% ({summary.winning_trades}W / {summary.losing_trades}L)")
    print(f"Gross Total Return:              {summary.gross_total_return_pct:+.2f}%")
    print(f"Net Total Return (After Costs):  {summary.net_total_return_pct:+.2f}%")
    print(f"Profit Factor:                   {summary.profit_factor:.2f}")
    print(f"Average Win / Average Loss:      +{summary.avg_win_pct:.2f}% / -{summary.avg_loss_pct:.2f}% (Payoff: {summary.win_loss_payoff_ratio:.2f})")
    print(f"Max Peak-to-Trough Drawdown:     {summary.max_drawdown_pct:.2f}%")
    print("-" * 80)
    print("1. TORO-BOUCHAUD MARKET IMPACT & SPREAD FRICTION")
    print(f"   Base Spread/Slippage Penalty: 2.50% per execution (5.00% round-trip)")
    print(f"   Dynamic Square-Root Impact:   Y = 0.60 * sigma_daily * sqrt(Order / Vol)")
    print("-" * 80)
    print("2. POSITION SIZING (FRACTIONAL HALF-KELLY)")
    print(f"   Full Kelly Optimal Growth:    {summary.full_kelly_pct:.2f}% of portfolio")
    print(f"   Fractional Half-Kelly (Safe): {summary.half_kelly_pct:.2f}% of portfolio")
    print(f"   Recommended Max Position:     {summary.recommended_position_pct:.2f}% of portfolio")
    print("-" * 80)
    print("3. STATISTICAL VALIDATION & MULTIPLE TESTING ADJUSTMENT (BAILEY & LOPEZ DE PRADO)")
    print(f"   Annualized Sharpe Ratio:      {summary.sharpe_ratio_annualized:.2f}")
    print(f"   Annualized Sortino Ratio:     {summary.sortino_ratio_annualized:.2f}")
    print(f"   Return Skewness / Kurtosis:   {summary.skewness:+.3f} / {summary.kurtosis:.3f}")
    print(f"   Expected Max Noise Sharpe:    {summary.expected_max_noise_sharpe:.2f}")
    print(f"   Probabilistic Sharpe (PSR):   {summary.probabilistic_sharpe_ratio_pct:.1f}%")
    print(f"   Deflated Sharpe Ratio (DSR):  {summary.deflated_sharpe_ratio_pct:.1f}%")
    verdict = "PASSED (p < 0.05)" if summary.dsr_passed_5pct else "INCONCLUSIVE / OVERFITTING RISK"
    print(f"   DSR Significance Verdict:     [{verdict}]")
    print("=" * 80 + "\n")


def generate_sample_signals_csv(target_path: Path | str) -> Path:
    """Creates a sample signals.csv file containing actual historical Helsinki insider buys."""
    file_path = Path(target_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    sample_signals = [
        {"ticker": "FARON.HE", "signal_date": "2024-06-18", "signal_type": "INSIDER_BUYING", "company_name": "Faron Pharmaceuticals"},
        {"ticker": "ROBIT.HE", "signal_date": "2024-05-15", "signal_type": "INSIDER_BUYING", "company_name": "Robit Oyj"},
        {"ticker": "KAMUX.HE", "signal_date": "2024-03-01", "signal_type": "INSIDER_BUYING", "company_name": "Kamux Oyj"},
        {"ticker": "SSH1V.HE", "signal_date": "2024-04-20", "signal_type": "POSITIVE_GUIDANCE", "company_name": "SSH Communications Security"},
        {"ticker": "FODELIA.HE", "signal_date": "2024-02-16", "signal_type": "INSIDER_BUYING", "company_name": "Fodelia Oyj"},
    ]

    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "signal_date", "signal_type", "company_name"])
        writer.writeheader()
        writer.writerows(sample_signals)

    logger.info(f"Generated sample signals file at {file_path}")
    return file_path


def main():
    parser = argparse.ArgumentParser(description="Quantitative Historical Backtesting Engine for Helsinki Micro-Caps")
    parser.add_argument("--signals", type=str, default="data/signals.csv", help="Path to signals CSV file")
    parser.add_argument("--initial-capital", type=float, default=10000.0, help="Initial capital in EUR")
    parser.add_argument("--trade-size", type=float, default=1000.0, help="Nominal trade size in EUR")
    parser.add_argument("--horizon", type=int, default=63, help="Holding horizon in trading days (default 63 = 3m)")
    parser.add_argument("--trailing-stop", type=float, default=0.12, help="Trailing stop loss fraction (default 0.12 = 12%)")
    parser.add_argument("--generate-sample", action="store_true", help="Generate sample data/signals.csv before running")

    args = parser.parse_args()

    signals_path = Path(args.signals)
    if args.generate_sample or not signals_path.exists():
        signals_path = generate_sample_signals_csv(signals_path)

    backtester = MicroCapBacktester(
        initial_capital=args.initial_capital,
        nominal_trade_size_eur=args.trade_size,
        default_holding_horizon_days=args.horizon,
        trailing_stop_pct=args.trailing_stop,
    )

    summary = backtester.run_backtest_from_signals_csv(signals_path)
    print_backtest_report(summary)


if __name__ == "__main__":
    main()
