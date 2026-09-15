"""
Unit tests for Historical Proxy-Signal Generator (Pump Hunter volume anomaly model).
"""

from datetime import date, timedelta
import numpy as np
import pandas as pd
import pytest

from screener.proxy_signal_generator import (
    HistoricalProxySignalGenerator,
    ProxySignal,
)


def test_proxy_signal_generator_detects_volume_anomaly(tmp_path):
    """Verify that a 3.5x volume surge on a +4% green day generates a proxy signal."""
    csv_file = tmp_path / "test_proxy.csv"
    generator = HistoricalProxySignalGenerator(
        surge_multiplier=3.0,
        min_daily_return_pct=2.0,
        rolling_window_days=20,
        min_median_volume=1000,
        output_csv_path=csv_file,
    )

    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(35)]
    
    # 35 days: first 25 days baseline 5,000 volume at 10.0 EUR
    opens = [10.0] * 35
    highs = [10.2] * 35
    lows = [9.8] * 35
    closes = [10.0] * 35
    volumes = [5000] * 35

    # Day 26: Massive volume surge (20,000 shares = 4.0x) + price jump to 10.50 (+5.0%)
    opens[25] = 10.0
    highs[25] = 10.60
    lows[25] = 9.95
    closes[25] = 10.50
    volumes[25] = 20_000

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=pd.to_datetime(dates))

    signals = generator.detect_signals_in_dataframe("TEST.HE", df)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.ticker == "TEST.HE"
    assert sig.signal_date == "2024-01-26"
    assert sig.volume_surge_ratio == 4.0
    assert sig.daily_return_pct == 5.0
    assert sig.signal_type == "POSITIVE_GUIDANCE"  # >= 5.0% return


def test_proxy_signal_generator_rejects_red_bearish_surge():
    """Verify that a high volume dump day (Close < Open) is rejected and does not produce a buy signal."""
    generator = HistoricalProxySignalGenerator(
        surge_multiplier=3.0,
        min_daily_return_pct=2.0,
        rolling_window_days=20,
    )

    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(35)]
    opens = [10.0] * 35
    highs = [10.2] * 35
    lows = [9.8] * 35
    closes = [10.0] * 35
    volumes = [5000] * 35

    # Day 26: 4.0x volume surge but price dumps -5% (10.0 -> 9.50)
    opens[25] = 10.0
    highs[25] = 10.10
    lows[25] = 9.40
    closes[25] = 9.50
    volumes[25] = 20_000

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=pd.to_datetime(dates))

    signals = generator.detect_signals_in_dataframe("BEAR.HE", df)
    assert len(signals) == 0  # Must be zero because it's a negative day


def test_proxy_signal_generator_rejects_dead_liquidity_stocks():
    """Verify that illiquid stocks with median volume < min_median_volume are excluded."""
    generator = HistoricalProxySignalGenerator(
        surge_multiplier=3.0,
        min_daily_return_pct=2.0,
        rolling_window_days=20,
        min_median_volume=1000,
    )

    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(35)]
    opens = [1.0] * 35
    highs = [1.05] * 35
    lows = [0.95] * 35
    closes = [1.0] * 35
    # Very dead stock: 50 shares a day median
    volumes = [50] * 35

    # Day 26: 500 shares (+10.0% return), surge ratio 10x, but median is only 50
    opens[25] = 1.0
    closes[25] = 1.10
    volumes[25] = 500

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=pd.to_datetime(dates))

    signals = generator.detect_signals_in_dataframe("DEAD.HE", df)
    assert len(signals) == 0
