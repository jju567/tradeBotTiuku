import json
import pytest
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import zoneinfo

def test_portfolio_snapshot_recording(tmp_path):
    history_file = tmp_path / "test_portfolio_history.json"
    from dashboard import record_portfolio_snapshot

    # First recording
    record_portfolio_snapshot(
        total_equity=12500.0,
        cash_balance=2500.0,
        stock_value=10000.0,
        starting_balance=10000.0,
        history_file=history_file
    )

    with open(history_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["total_equity"] == 12500.0
    assert data[0]["total_return"] == 2500.0
    assert data[0]["total_return_pct"] == 25.0

    # Rapid second recording with negligible difference should be ignored to prevent file flooding
    record_portfolio_snapshot(
        total_equity=12500.10,
        cash_balance=2500.10,
        stock_value=10000.0,
        starting_balance=10000.0,
        history_file=history_file
    )
    with open(history_file, "r", encoding="utf-8") as f:
        data2 = json.load(f)
    assert len(data2) == 1


def test_portfolio_history_resampling():
    sample_records = [
        {"timestamp": "2026-09-20T10:00:00Z", "total_equity": 10500.0, "cash_balance": 5000.0, "total_stock_value": 5500.0},
        {"timestamp": "2026-09-20T11:00:00Z", "total_equity": 10600.0, "cash_balance": 5000.0, "total_stock_value": 5600.0},
        {"timestamp": "2026-09-21T09:00:00Z", "total_equity": 10800.0, "cash_balance": 5000.0, "total_stock_value": 5800.0},
        {"timestamp": "2026-09-21T10:00:00Z", "total_equity": 10900.0, "cash_balance": 5000.0, "total_stock_value": 5900.0},
    ]
    df = pd.DataFrame(sample_records)
    helsinki_tz = zoneinfo.ZoneInfo("Europe/Helsinki")
    df["dt"] = pd.to_datetime(df["timestamp"], format="ISO8601", utc=True).dt.tz_convert(helsinki_tz)
    df = df.set_index("dt").sort_index()

    # Test Hourly
    df_1h = df[["total_equity"]].resample("1h").last().dropna()
    assert len(df_1h) == 4

    # Test Daily
    df_1d = df[["total_equity"]].resample("1D").last().dropna()
    assert len(df_1d) == 2
    assert df_1d["total_equity"].iloc[-1] == 10900.0


def test_compute_live_portfolio_history_structure():
    from dashboard import compute_live_portfolio_history

    # Empty positions should return empty DataFrame
    res_empty = compute_live_portfolio_history((), free_cash=100.0)
    assert res_empty.empty

    # Test with sample position tuple: (ticker, shares, buy_price, current_price, currency)
    sample_positions = (
        ("VIAFIN.HE", 46.0, 19.80, 19.90, "EUR"),
    )
    df_hist = compute_live_portfolio_history(sample_positions, free_cash=10.0, starting_capital=1000.0, timeframe="Viimeiset 7 päivää")
    if not df_hist.empty:
        assert "total_equity" in df_hist.columns
        assert "cash_balance" in df_hist.columns
        assert "total_stock_value" in df_hist.columns
        assert "total_return" in df_hist.columns
        assert "total_return_pct" in df_hist.columns
        assert (df_hist["total_equity"] > 0).all()
