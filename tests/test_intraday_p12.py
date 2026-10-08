"""
tests/test_intraday_p12.py - Comprehensive Unit Tests for Intraday P12 Scanner.
"""

from datetime import datetime, date, time as dtime
from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import pytest

from intraday_p12_scanner import (
    IntradayP12Scanner,
    is_nordic_market_open,
    is_us_market_open,
    get_ticker_region,
    is_market_open_for_ticker,
)


def test_market_hours_logic():
    # 1. Weekend test (Saturday: weekday 5)
    sat = datetime(2026, 10, 10, 14, 0, 0)
    assert not is_nordic_market_open(sat)
    assert not is_us_market_open(sat)

    # 2. Weekday Nordic hours (Wednesday 12:00 -> Open)
    wed_noon = datetime(2026, 10, 7, 12, 0, 0)
    assert is_nordic_market_open(wed_noon)
    assert not is_us_market_open(wed_noon)  # US not open yet at 12:00 Helsinki

    # 3. Weekday US hours (Wednesday 17:00 -> Both Open)
    wed_evening = datetime(2026, 10, 7, 17, 0, 0)
    assert is_nordic_market_open(wed_evening)
    assert is_us_market_open(wed_evening)

    # 4. Weekday late night (Wednesday 22:00 -> Only US open)
    wed_night = datetime(2026, 10, 7, 22, 0, 0)
    assert not is_nordic_market_open(wed_night)
    assert is_us_market_open(wed_night)

    # 5. Weekday closed hours (Wednesday 08:00 -> Both Closed)
    wed_morning = datetime(2026, 10, 7, 8, 0, 0)
    assert not is_nordic_market_open(wed_morning)
    assert not is_us_market_open(wed_morning)


def test_ticker_region_classification():
    assert get_ticker_region("VIAFIN.HE") == "NORDIC"
    assert get_ticker_region("SEDANA.ST") == "NORDIC"
    assert get_ticker_region("MAERSK-B.CO") == "NORDIC"
    assert get_ticker_region("AAPL") == "US"
    assert get_ticker_region("CODX") == "US"


def test_is_market_open_for_ticker():
    wed_noon = datetime(2026, 10, 7, 12, 0, 0)
    assert is_market_open_for_ticker("VIAFIN.HE", wed_noon) is True
    assert is_market_open_for_ticker("AAPL", wed_noon) is False
    assert is_market_open_for_ticker("AAPL", wed_noon, force_open=True) is True


def test_trailing_stop_triggers_sell(tmp_path):
    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True)
    state_file = portfolios_dir / "portfolio_P12_Momentum_Breakout_state.json"

    initial_state = {
        "portfolio_id": "P12_Momentum_Breakout",
        "starting_balance": 10000.0,
        "cash_balance": 2000.0,
        "currency": "EUR",
        "positions": [
            {
                "Ticker": "TEST.ST",
                "Buy Date": "2026-10-07",
                "Buy Price": 10.0,
                "Shares": 200,
                "Capital Invested": 2000.0,
                "Strategy": "Momentum_Breakout",
                "Currency": "SEK",
                "Peak Price": 10.0,
            }
        ],
    }
    state_file.write_text(json.dumps(initial_state), encoding="utf-8")

    scanner = IntradayP12Scanner(
        portfolios_dir=portfolios_dir,
        force_open=True,
    )

    # Mock live quote showing a 7% drop from peak (10.0 -> 9.30)
    scanner.fetch_live_intraday_data = MagicMock(return_value={
        "price": 9.30,
        "day_volume": 100000.0,
        "prev_close": 9.50,
        "day_change_pct": -2.1,
    })

    now_h = datetime(2026, 10, 7, 14, 0, 0)
    closed = scanner.execute_trailing_stops(now_h)

    assert closed == 1
    assert len(scanner.state["positions"]) == 0
    # Verify trade history written
    history_file = portfolios_dir / "portfolio_P12_Momentum_Breakout_history.csv"
    assert history_file.exists()
    content = history_file.read_text(encoding="utf-8")
    assert "TRAILING_STOP" in content
    assert "TEST.ST" in content


def test_peak_price_updates_when_higher(tmp_path):
    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True)
    state_file = portfolios_dir / "portfolio_P12_Momentum_Breakout_state.json"

    initial_state = {
        "portfolio_id": "P12_Momentum_Breakout",
        "starting_balance": 10000.0,
        "cash_balance": 2000.0,
        "currency": "EUR",
        "positions": [
            {
                "Ticker": "WINNER.ST",
                "Buy Date": "2026-10-07",
                "Buy Price": 10.0,
                "Shares": 200,
                "Capital Invested": 2000.0,
                "Strategy": "Momentum_Breakout",
                "Currency": "SEK",
                "Peak Price": 10.0,
            }
        ],
    }
    state_file.write_text(json.dumps(initial_state), encoding="utf-8")

    scanner = IntradayP12Scanner(
        portfolios_dir=portfolios_dir,
        force_open=True,
    )

    # Mock live quote showing price rising to 12.50
    scanner.fetch_live_intraday_data = MagicMock(return_value={
        "price": 12.50,
        "day_volume": 500000.0,
        "prev_close": 11.00,
        "day_change_pct": +13.6,
    })

    now_h = datetime(2026, 10, 7, 14, 0, 0)
    closed = scanner.execute_trailing_stops(now_h)

    assert closed == 0
    assert len(scanner.state["positions"]) == 1
    assert scanner.state["positions"][0]["Peak Price"] == 12.50


def test_breakout_scanner_executes_buy(tmp_path):
    portfolios_dir = tmp_path / "portfolios"
    portfolios_dir.mkdir(parents=True)
    state_file = portfolios_dir / "portfolio_P12_Momentum_Breakout_state.json"

    initial_state = {
        "portfolio_id": "P12_Momentum_Breakout",
        "starting_balance": 10000.0,
        "cash_balance": 10000.0,
        "currency": "EUR",
        "positions": [],
    }
    state_file.write_text(json.dumps(initial_state), encoding="utf-8")

    universe_file = tmp_path / "clean_universe.csv"
    universe_file.write_text(
        "ticker,market,adv_20d_local,adv_20d_usd,currency\n"
        "BREAKOUT.ST,SE,100000.0,10000.0,SEK\n",
        encoding="utf-8",
    )

    scanner = IntradayP12Scanner(
        portfolios_dir=portfolios_dir,
        universe_path=universe_file,
        force_open=True,
    )

    # Mock quote with 4x ADV surge and +5% return
    scanner.fetch_live_intraday_data = MagicMock(return_value={
        "price": 10.0,
        "day_volume": 40000.0,  # 40000 * 10 = 400,000 SEK (4x 100,000 ADV)
        "prev_close": 9.52,
        "day_change_pct": 5.04,
    })

    now_h = datetime(2026, 10, 7, 14, 0, 0)
    buys = scanner.scan_and_execute_breakouts(now_h)

    assert buys == 1
    assert len(scanner.state["positions"]) == 1
    pos = scanner.state["positions"][0]
    assert pos["Ticker"] == "BREAKOUT.ST"
    assert pos["Buy Price"] == 10.0
    assert pos["Peak Price"] == 10.0
    assert scanner.state["cash_balance"] < 10000.0
