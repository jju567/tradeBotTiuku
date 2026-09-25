import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd

from screener.dynamic_universe_updater import (
    load_universe_sync_status,
    save_universe_sync_status,
    should_update,
    validate_candidate_ticker,
    scan_and_update_universe,
)


def test_status_load_and_save(tmp_path):
    status_file = tmp_path / "test_sync_status.json"
    assert not status_file.exists()

    initial = load_universe_sync_status(status_file)
    assert initial["last_sync_timestamp"] is None

    payload = {
        "last_sync_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_tickers": 107,
        "last_added_tickers": ["NEWCO.HE"],
        "last_sync_success": True,
    }
    save_universe_sync_status(payload, status_file)
    assert status_file.exists()

    loaded = load_universe_sync_status(status_file)
    assert loaded["total_tickers"] == 107
    assert loaded["last_added_tickers"] == ["NEWCO.HE"]
    assert loaded["last_sync_success"] is True


def test_should_update_timing(tmp_path):
    status_file = tmp_path / "test_timing_status.json"

    # No status file -> should update immediately
    assert should_update(max_age_days=7.0, status_file=status_file) is True

    # 2 days old -> should not update
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    save_universe_sync_status({"last_sync_timestamp": recent.isoformat()}, status_file)
    assert should_update(max_age_days=7.0, status_file=status_file) is False

    # 8 days old -> should update
    old = datetime.now(timezone.utc) - timedelta(days=8)
    save_universe_sync_status({"last_sync_timestamp": old.isoformat()}, status_file)
    assert should_update(max_age_days=7.0, status_file=status_file) is True


@patch("yfinance.Ticker")
def test_validate_candidate_ticker(mock_ticker_cls):
    mock_t = MagicMock()
    # Mock 25 trading days
    dates = pd.date_range("2026-08-01", periods=25, freq="B")
    mock_hist = pd.DataFrame({
        "Close": [5.0] * 25,
        "Volume": [200_000] * 25,  # 5.0 * 200_000 = 1,000_000 SEK * 0.096 = 96,000 USD daily turnover
    }, index=dates)
    mock_t.history.return_value = mock_hist
    mock_t.fast_info.market_cap = 50_000_000.0
    mock_ticker_cls.return_value = mock_t

    # Test passing ticker
    res = validate_candidate_ticker(
        ticker="SENSO.ST",
        market="SE",
        min_adv_usd=50_000.0,
        fx_rates={"USD": 1.0, "EUR": 1.08, "SEK": 0.096},
    )
    assert res is not None
    assert res["ticker"] == "SENSO.ST"
    assert res["currency"] == "SEK"
    assert res["market"] == "SE"
    assert res["adv_20d_usd"] > 50_000.0

    # Test failing low-volume ticker
    mock_hist_low = pd.DataFrame({
        "Close": [5.0] * 25,
        "Volume": [100] * 25,  # Only 500 local volume
    }, index=dates)
    mock_t.history.return_value = mock_hist_low
    res_low = validate_candidate_ticker(
        ticker="SENSO.ST",
        market="SE",
        min_adv_usd=50_000.0,
        fx_rates={"USD": 1.0, "EUR": 1.08, "SEK": 0.096},
    )
    assert res_low is None


def test_scan_and_update_universe(tmp_path):
    clean_csv = tmp_path / "test_clean_universe.csv"
    status_file = tmp_path / "test_status.json"

    # Create initial universe with 1 ticker
    clean_csv.write_text(
        "ticker,market,market_cap_usd,market_cap_local,currency,current_price,adv_20d_local,adv_20d_usd\n"
        "EXISTING.HE,FI,50000000.0,46000000.0,EUR,10.0,60000.0,65000.0\n",
        encoding="utf-8"
    )

    fake_nordnet_stocks = [
        {"Ticker_YF": "EXISTING.HE", "Name": "Existing Oyj", "Market": "FI", "MarketCap_EUR": 46000000.0},
        {"Ticker_YF": "NEWCO.HE", "Name": "New Company Oyj", "Market": "FI", "MarketCap_EUR": 25000000.0},
        {"Ticker_YF": "LOWVOL.ST", "Name": "Low Vol AB", "Market": "SE", "MarketCap_EUR": 15000000.0},
    ]

    def fake_validate(ticker, **kwargs):
        if ticker == "NEWCO.HE":
            return {
                "ticker": "NEWCO.HE",
                "market": "FI",
                "market_cap_usd": 27000000.0,
                "market_cap_local": 25000000.0,
                "currency": "EUR",
                "current_price": 4.5,
                "adv_20d_local": 70000.0,
                "adv_20d_usd": 75600.0,
            }
        return None

    with patch("screener.universe_builder.fetch_nordnet_universe", return_value=fake_nordnet_stocks):
        with patch("screener.dynamic_universe_updater.validate_candidate_ticker", side_effect=fake_validate):
            added_cnt, added_list = scan_and_update_universe(
                clean_csv_path=clean_csv,
                status_file=status_file,
                force=True,
            )

    assert added_cnt == 1
    assert added_list == ["NEWCO.HE"]

    df = pd.read_csv(clean_csv)
    assert len(df) == 2
    assert "EXISTING.HE" in df["ticker"].values
    assert "NEWCO.HE" in df["ticker"].values

    status = load_universe_sync_status(status_file)
    assert status["total_tickers"] == 2
    assert status["last_added_tickers"] == ["NEWCO.HE"]
    assert status["last_sync_success"] is True
