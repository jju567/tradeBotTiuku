"""
Unit tests for Quantitative Backtesting Engine (Toro-Bouchaud, Half-Kelly, DSR).
"""

import math
from datetime import date, timedelta
import numpy as np
import pandas as pd
import pytest

from screener.backtest_engine import (
    ToroBouchaudCostModel,
    KellyPositionSizer,
    DeflatedSharpeCalculator,
    MicroCapBacktester,
    BacktestTrade,
)


def test_toro_bouchaud_cost_model_impact_scaling():
    """Verify Toro-Bouchaud market impact scales with sqrt of order participation and daily volatility."""
    cost_model = ToroBouchaudCostModel(y_constant=0.60, base_spread_slippage_pct=0.025)

    raw_price = 10.0
    daily_vol = 0.04  # 4% daily volatility
    daily_volume = 10_000.0  # 10k shares ($100k daily volume)

    # 1. Small order: 1,000 EUR -> 100 shares -> 1% participation
    exec_price_small, impact_small = cost_model.calculate_entry_execution(
        raw_open_price=raw_price,
        order_size_eur=1_000.0,
        daily_volume_shares=daily_volume,
        daily_volatility=daily_vol,
    )
    # participation = 100 / 10000 = 0.01 -> sqrt(0.01) = 0.10
    # impact = 0.60 * 0.04 * 0.10 = 0.0024 = 0.24%
    assert impact_small == pytest.approx(0.24, abs=0.05)
    # Total penalty = 2.5% base + 0.24% impact = 2.74% -> Exec price ~ 10.274
    assert exec_price_small > raw_price

    # 2. Large order: 10,000 EUR -> 1000 shares -> 10% participation
    exec_price_large, impact_large = cost_model.calculate_entry_execution(
        raw_open_price=raw_price,
        order_size_eur=10_000.0,
        daily_volume_shares=daily_volume,
        daily_volatility=daily_vol,
    )
    # sqrt(0.10) approx 0.3162 -> impact = 0.60 * 0.04 * 0.3162 = 0.759%
    assert impact_large > impact_small
    assert impact_large == pytest.approx(0.759, abs=0.1)

    # 3. Exit execution gives price below raw
    exec_exit_price, exit_impact = cost_model.calculate_exit_execution(
        raw_exit_price=raw_price,
        order_size_shares=500,
        daily_volume_shares=daily_volume,
        daily_volatility=daily_vol,
    )
    assert exec_exit_price < raw_price
    assert exit_impact > 0


def test_fractional_half_kelly_position_sizer():
    """Verify Full and Half Kelly calculations and caps."""
    # Case 1: 60% win rate, 2:1 payoff ratio (avg win 20%, avg loss 10%)
    # Full Kelly: (0.60 * 2 - 0.40) / 2 = 0.80 / 2 = 0.40 (40%)
    # Half Kelly: 20%
    full_k, half_k, rec_k = KellyPositionSizer.calculate_half_kelly(
        win_rate=0.60,
        avg_win=0.20,
        avg_loss=0.10,
        max_position_cap=0.25,
    )
    assert full_k == pytest.approx(40.0, abs=0.1)
    assert half_k == pytest.approx(20.0, abs=0.1)
    assert rec_k == pytest.approx(20.0, abs=0.1)

    # Case 2: Unfavorable coin (40% win rate, 1:1 payoff) -> Kelly <= 0 -> 0% allocation
    full_neg, half_neg, rec_neg = KellyPositionSizer.calculate_half_kelly(
        win_rate=0.40,
        avg_win=0.10,
        avg_loss=0.10,
    )
    assert full_neg == 0.0
    assert half_neg == 0.0
    assert rec_neg == 0.0

    # Case 3: Huge edge capped at max_position_cap (25%)
    full_huge, half_huge, rec_huge = KellyPositionSizer.calculate_half_kelly(
        win_rate=0.80,
        avg_win=0.50,
        avg_loss=0.10,
        max_position_cap=0.25,
    )
    assert half_huge > 25.0
    assert rec_huge == 25.0


def test_deflated_sharpe_ratio_calculator():
    """Verify Bailey & Lopez de Prado DSR and PSR statistical metrics."""
    np.random.seed(42)
    # Generate 50 positive skewed returns with mean +3% and std 5%
    returns = np.random.normal(loc=0.03, scale=0.05, size=60)

    metrics = DeflatedSharpeCalculator.calculate_dsr(
        returns=returns,
        n_trials=50,
        periods_per_year=252,
    )

    assert "sharpe_annualized" in metrics
    assert "sortino_annualized" in metrics
    assert "psr_pct" in metrics
    assert "dsr_pct" in metrics
    assert metrics["sharpe_annualized"] > 0
    assert 0.0 <= metrics["psr_pct"] <= 100.0
    assert 0.0 <= metrics["dsr_pct"] <= 100.0


def test_microcap_backtester_simulation(monkeypatch):
    """Verify full event-driven forward backtesting cycle with mocked daily OHLCV."""
    # Create mock daily OHLCV DataFrame
    dates = [date(2024, 6, 1) + timedelta(days=i) for i in range(100)]
    # Upward trending prices
    opens = [10.0 + i * 0.10 for i in range(100)]
    highs = [p + 0.20 for p in opens]
    lows = [p - 0.10 for p in opens]
    closes = [p + 0.05 for p in opens]
    volumes = [10_000 for _ in range(100)]

    mock_df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=pd.to_datetime(dates))

    # Mock yfinance.download
    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *args, **kwargs: mock_df)

    backtester = MicroCapBacktester(
        initial_capital=10000.0,
        nominal_trade_size_eur=1000.0,
        base_spread_slippage_pct=0.025,
        default_holding_horizon_days=30,
        trailing_stop_pct=0.15,
    )

    signals = [
        {
            "ticker": "FARON",
            "signal_date": "2024-06-10",
            "signal_type": "INSIDER_BUYING",
            "company_name": "Faron Pharmaceuticals",
        }
    ]

    summary = backtester.run_backtest_on_signals(signals)

    assert summary.total_trades == 1
    assert summary.winning_trades == 1
    assert summary.win_rate_pct == 100.0
    assert summary.net_total_return_pct > 0
    trade = summary.trades[0]
    assert trade.ticker == "FARON.HE"
    assert trade.entry_price_exec > trade.entry_price_raw  # Slippage added on buy
    assert trade.exit_price_exec < trade.exit_price_raw    # Slippage deducted on sell


def test_backtest_trailing_stop_gap_down_execution(monkeypatch):
    """TASK-05: Gap-down execution must execute at min(Open, trailing_stop), not at imaginary stop."""
    dates = pd.date_range("2024-06-10", periods=5, freq="B")
    # Day 0: Open=10, Close=10 (Entry)
    # Day 1: Open=10, High=10, Low=9.5, Close=9.8
    # Day 2: Gap down! Open=7.0, High=7.2, Low=6.8, Close=7.0 (Stop at 10 * 0.85 = 8.5)
    # Market opened at 7.0, so fill must be at min(7.0, 8.5) = 7.0 (before exit slippage).
    mock_df = pd.DataFrame({
        "open": [10.0, 10.0, 7.0, 7.0, 7.0],
        "high": [10.0, 10.0, 7.2, 7.0, 7.0],
        "low": [10.0, 9.5, 6.8, 6.9, 7.0],
        "close": [10.0, 9.8, 7.0, 7.0, 7.0],
        "volume": [10000.0, 10000.0, 10000.0, 10000.0, 10000.0],
    }, index=dates)

    import yfinance as yf
    monkeypatch.setattr(yf, "download", lambda *args, **kwargs: mock_df)

    backtester = MicroCapBacktester(
        initial_capital=10000.0,
        nominal_trade_size_eur=1000.0,
        base_spread_slippage_pct=0.0, # Zero slippage to test exact execution price
        toro_bouchaud_y=0.0,
        trailing_stop_pct=0.15,       # 15% stop (8.50)
    )

    signals = [{"ticker": "GAPDOWN.HE", "signal_date": "2024-06-10", "signal_type": "BUY"}]
    summary = backtester.run_backtest_on_signals(signals)

    assert summary.total_trades == 1
    trade = summary.trades[0]
    assert trade.exit_reason == "TRAILING_STOP"
    # Should execute at open (7.0), NOT at trailing stop price (8.5)
    assert trade.exit_price_raw == 7.0
