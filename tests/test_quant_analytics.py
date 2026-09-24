"""
test_quant_analytics.py - Unit and Integration Tests for quant_analytics.py.
Tests:
1. Returns and frequency resampling (daily & weekly smoothing).
2. Risk-adjusted ratios: 30-day Rolling Sharpe & Sortino.
3. Max Drawdown & Time-to-Recovery calculations.
4. Multiple testing corrections: Deflated Sharpe Ratio (DSR) & Bonferroni.
5. Trade attribution metrics: Concentration (% PnL from top 1/2 trades), median holding days, median vs mean return.
6. 10-Portfolio return correlation matrix.
7. End-to-end institutional summary generator.
"""

import math
import numpy as np
import pandas as pd
import pytest
from pathlib import Path

import quant_analytics as qa


@pytest.fixture
def sample_equity_curve():
    """Generates a realistic 100-day daily equity series with known drawdown."""
    dates = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
    # Base path: climbs from 10,000 to 12,000, drops to 9,600 (-20% MDD), then recovers to 12,500
    values = np.linspace(10000, 12000, 30).tolist()
    values += np.linspace(12000, 9600, 20).tolist()   # Drawdown trough
    values += np.linspace(9600, 12500, 50).tolist()   # Recovery
    return pd.Series(values, index=dates, name="test_equity")


@pytest.fixture
def sample_trades_df():
    """Generates a sample trade history DataFrame."""
    return pd.DataFrame({
        "Ticker": ["AAA.HE", "BBB.ST", "CCC", "DDD.HE", "EEE"],
        "Buy Date": ["2026-01-01", "2026-01-05", "2026-01-10", "2026-01-15", "2026-01-20"],
        "Sell Date": ["2026-01-06", "2026-01-15", "2026-01-12", "2026-01-25", "2026-02-10"],
        "Capital Invested": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "Net Return": [1150.0, 900.0, 1050.0, 1400.0, 950.0],
        "Net PnL": [150.0, -100.0, 50.0, 400.0, -50.0],
        "Exit Reason": ["TARGET", "STOP", "TARGET", "TARGET", "STOP"],
    })


def test_calculate_returns_daily_and_weekly(sample_equity_curve):
    r_daily = qa.calculate_returns(sample_equity_curve, freq="D", weekly_smoothing=False)
    assert not r_daily.empty
    assert len(r_daily) == len(sample_equity_curve) - 1

    r_weekly = qa.calculate_returns(sample_equity_curve, weekly_smoothing=True)
    assert not r_weekly.empty
    assert len(r_weekly) < len(r_daily)


def test_rolling_sharpe_and_sortino(sample_equity_curve):
    r_daily = qa.calculate_returns(sample_equity_curve)
    sh = qa.calculate_rolling_sharpe(r_daily, window=30)
    so = qa.calculate_rolling_sortino(r_daily, window=30)

    assert not sh.empty
    assert not so.empty
    assert not np.isnan(sh.dropna().iloc[-1])
    assert not np.isnan(so.dropna().iloc[-1])


def test_max_drawdown_and_recovery(sample_equity_curve):
    mdd, peak, trough = qa.calculate_max_drawdown(sample_equity_curve)
    assert mdd < 0
    # Expected drop is from 12000 to 9600 = (9600 - 12000)/12000 = -0.20 (-20%)
    assert pytest.approx(mdd, 0.01) == -0.20
    assert peak is not None
    assert trough is not None
    assert peak < trough

    rec_days = qa.calculate_time_to_recovery(sample_equity_curve)
    assert rec_days > 0


def test_deflated_sharpe_ratio():
    sharpes = [0.2, 0.5, 0.8, 1.2, 0.4, 0.1, -0.3, 0.6, 0.7, 1.8]
    best_sharpe = 1.8

    report = qa.deflated_sharpe_ratio(
        sharpe=best_sharpe,
        all_sharpes=sharpes,
        nb_trials=10,
        sample_length=252,
    )

    assert "dsr" in report
    assert "is_significant" in report
    assert "bonferroni_p_value" in report
    assert 0.0 <= report["dsr"] <= 1.0
    assert 0.0 <= report["bonferroni_p_value"] <= 1.0


def test_trade_attribution(sample_trades_df):
    attrib = qa.calculate_trade_attribution(sample_trades_df)

    assert attrib["total_trades"] == 5
    assert attrib["win_rate_pct"] == 60.0  # 3 wins out of 5
    assert attrib["total_net_pnl_eur"] == 450.0  # 150 - 100 + 50 + 400 - 50 = 450
    # Top trades: 400 and 150. Gross positive gains = 400 + 150 + 50 = 600.
    # Top-1 concentration: 400 / 600 = 66.7%
    assert pytest.approx(attrib["concentration_top1_pct"], 0.1) == 66.7
    # Top-2 concentration: (400 + 150) / 600 = 550 / 600 = 91.7%
    assert pytest.approx(attrib["concentration_top2_pct"], 0.1) == 91.7
    # Holding days:
    # 2026-01-01 to 2026-01-06 = 5d
    # 2026-01-05 to 2026-01-15 = 10d
    # 2026-01-10 to 2026-01-12 = 2d
    # 2026-01-15 to 2026-01-25 = 10d
    # 2026-01-20 to 2026-02-10 = 21d
    # Sorted days: [2, 5, 10, 10, 21] -> median is 10
    assert attrib["median_holding_days"] == 10.0


def test_correlation_matrix_builder(sample_equity_curve):
    eq_dict = {
        "P1": sample_equity_curve,
        "P2": sample_equity_curve * 1.05,
        "P3": sample_equity_curve * 0.95,
    }
    corr = qa.build_portfolio_correlation_matrix(eq_dict, freq="D")

    assert corr.shape == (3, 3)
    assert pytest.approx(corr.loc["P1", "P1"], 0.01) == 1.0
    assert pytest.approx(corr.loc["P2", "P1"], 0.01) == 1.0


def test_dsr_data_sufficiency_guard():
    sharpes = [0.5, 0.2, -0.1, 0.8, 1.2, 0.4, 0.3, 0.1, -0.2, 0.6]

    # Test with insufficient observations (e.g. T = 2 days)
    short_report = qa.deflated_sharpe_ratio(
        sharpe=1.2,
        all_sharpes=sharpes,
        nb_trials=10,
        sample_length=2,
    )
    assert short_report["has_sufficient_data"] is False
    assert short_report["days_needed"] == 18
    # Null E[max] must NOT explode to 15.74; it must report the stable 1-year baseline (~1.57)
    assert 1.0 <= short_report["expected_max_sharpe"] <= 2.5

    # Test with sufficient observations (T = 50 days)
    suff_report = qa.deflated_sharpe_ratio(
        sharpe=1.2,
        all_sharpes=sharpes,
        nb_trials=10,
        sample_length=50,
    )
    assert suff_report["has_sufficient_data"] is True
    assert suff_report["days_needed"] == 0


def test_get_market_regime():
    regime = qa.get_market_regime()
    assert "regime" in regime
    assert "badge" in regime
    assert "volatility_20d_pct" in regime
    assert regime["volatility_20d_pct"] > 0
