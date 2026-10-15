"""
Unit tests for screener/price_fetcher.py

Validates real-time price fetching, NaN handling, error states, and normalization.
"""

import math
from unittest.mock import MagicMock
import pandas as pd
import pytest

from screener.price_fetcher import get_realtime_data, normalize_ticker


def test_normalize_ticker():
    assert normalize_ticker("NOKIA", "FI") == "NOKIA.HE"
    assert normalize_ticker("EVO", "SE") == "EVO.ST"
    assert normalize_ticker("KAHOT", "NO") == "KAHOT.OL"
    assert normalize_ticker("ORSTED", "DK") == "ORSTED.CO"
    assert normalize_ticker("AAPL", "US") == "AAPL"
    assert normalize_ticker("SAMPO.HE") == "SAMPO.HE"


def test_get_realtime_data_normal_float_price(monkeypatch):
    """Verifies that a valid float price (e.g. 10.0) succeeds without NameError."""
    mock_ticker = MagicMock()
    # Fast info returns clean valid price
    mock_ticker.fast_info.last_price = 10.0
    mock_ticker.fast_info.previous_close = 9.50
    mock_ticker.fast_info.market_cap = 50_000_000.0
    mock_ticker.fast_info.currency = "EUR"

    monkeypatch.setattr("yfinance.Ticker", lambda symbol: mock_ticker)

    result = get_realtime_data("NOKIA.HE")
    assert result["status"] == "SUCCESS"
    assert result["current_price"] == 10.0
    assert result["day_change_pct"] == pytest.approx(5.26, abs=0.01)
    assert result["market_cap"] == 50_000_000.0
    assert result["currency"] == "EUR"
    assert result["error"] is None


def test_get_realtime_data_rejects_nan_price(monkeypatch):
    """Verifies that NaN price is caught and returns status ERROR."""
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = float("nan")
    mock_ticker.fast_info.previous_close = 10.0
    mock_ticker.fast_info.market_cap = 10_000_000.0
    mock_ticker.fast_info.currency = "USD"
    mock_ticker.info = {}
    mock_ticker.history.return_value = pd.DataFrame()

    monkeypatch.setattr("yfinance.Ticker", lambda symbol: mock_ticker)

    result = get_realtime_data("DEAD.US")
    assert result["status"] == "ERROR"
    assert result["current_price"] is None
    assert "No valid pricing data" in result["error"]


def test_get_realtime_data_rejects_zero_or_negative_price(monkeypatch):
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 0.0
    mock_ticker.fast_info.previous_close = 0.0
    mock_ticker.fast_info.market_cap = 10_000_000.0
    mock_ticker.info = {}
    mock_ticker.history.return_value = pd.DataFrame()

    monkeypatch.setattr("yfinance.Ticker", lambda symbol: mock_ticker)

    result = get_realtime_data("ZERO.HE")
    assert result["status"] == "ERROR"
    assert result["current_price"] is None


def test_get_realtime_data_handles_nan_prev_close_and_market_cap(monkeypatch):
    """Verifies that NaN in optional fields (prev_close, market_cap) does not crash execution."""
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 15.5
    mock_ticker.fast_info.previous_close = float("nan")
    mock_ticker.fast_info.market_cap = float("nan")
    mock_ticker.fast_info.currency = "SEK"

    monkeypatch.setattr("yfinance.Ticker", lambda symbol: mock_ticker)

    result = get_realtime_data("NORDIC.ST")
    assert result["status"] == "SUCCESS"
    assert result["current_price"] == 15.5
    assert result["day_change_pct"] is None
    assert result["market_cap"] is None
    assert result["currency"] == "SEK"


def test_get_realtime_data_rejects_inf_price(monkeypatch):
    """Verifies that infinite price (float('inf')) is rejected with ERROR status."""
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = float("inf")
    mock_ticker.fast_info.previous_close = 10.0
    mock_ticker.fast_info.market_cap = 10_000_000.0
    mock_ticker.fast_info.currency = "USD"
    mock_ticker.info = {}
    mock_ticker.history.return_value = pd.DataFrame()

    monkeypatch.setattr("yfinance.Ticker", lambda symbol: mock_ticker)

    result = get_realtime_data("INF.US")
    assert result["status"] == "ERROR"
    assert result["current_price"] is None
    assert "No valid pricing data" in result["error"]

