"""
Unit and integration tests for screener/exit_manager.py with Core & Satellite strategy separation.
"""

import csv
import os
from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd
import pytest

from screener.exit_manager import ExitManager, check_exits


@pytest.fixture
def temp_csv_paths(tmp_path):
    open_pos_path = tmp_path / "open_positions.csv"
    trade_hist_path = tmp_path / "trade_history.csv"
    return open_pos_path, trade_hist_path


def test_load_and_save_open_positions_with_strategy_type(temp_csv_paths):
    open_pos_path, trade_hist_path = temp_csv_paths
    manager = ExitManager(open_positions_path=open_pos_path, trade_history_path=trade_hist_path)

    # Initial load when file does not exist
    positions = manager.load_open_positions()
    assert positions == []

    # Write positions with distinct Strategy_Type
    test_positions = [
        {"Ticker": "QTCOM.HE", "EntryDate": "2026-08-01", "EntryPrice": 10.0, "HighestPrice": 12.0, "Shares": 100, "PositionValue": 1000.0, "Strategy_Type": "CORE"},
        {"Ticker": "HARVIA.HE", "EntryDate": "2026-08-05", "EntryPrice": 30.0, "HighestPrice": 35.0, "Shares": 50, "PositionValue": 1500.0, "Strategy_Type": "SATELLITE"},
    ]
    manager.save_open_positions(test_positions)

    loaded = manager.load_open_positions()
    assert len(loaded) == 2
    assert loaded[0]["Ticker"] == "QTCOM.HE"
    assert loaded[0]["Strategy_Type"] == "CORE"
    assert loaded[1]["Ticker"] == "HARVIA.HE"
    assert loaded[1]["Strategy_Type"] == "SATELLITE"


def test_satellite_hard_take_profit_triggered(temp_csv_paths):
    open_pos_path, trade_hist_path = temp_csv_paths
    manager = ExitManager(
        open_positions_path=open_pos_path,
        trade_history_path=trade_hist_path,
        take_profit_pct=0.25,
        exit_slippage_pct=0.025,
    )

    pos = {
        "Ticker": "ROCKET.HE",
        "EntryDate": "2026-08-01",
        "EntryPrice": 10.0,
        "HighestPrice": 10.0,
        "Shares": 100,
        "PositionValue": 1000.0,
        "Strategy_Type": "SATELLITE",
    }

    # Stock rallies +26% to 12.60
    dates = pd.date_range(start="2026-08-01", periods=5, freq="D")
    price_df = pd.DataFrame({
        "Open": [10.0, 10.5, 11.2, 12.0, 12.6],
        "High": [10.2, 10.8, 11.5, 12.2, 12.7],
        "Low": [9.9, 10.4, 11.0, 11.8, 12.4],
        "Close": [10.1, 10.7, 11.4, 12.1, 12.6],
        "Volume": [10000] * 5
    }, index=dates)

    should_exit, closed_trade, surviving_pos = manager.evaluate_position_exit(pos, price_df)

    assert should_exit is True
    assert closed_trade is not None
    assert surviving_pos is None
    assert closed_trade["Strategy_Type"] == "SATELLITE"
    assert "Take Profit Hit" in closed_trade["ExitReason"]
    assert closed_trade["Shares"] == 100
    assert closed_trade["ExitPriceRaw"] == 12.6
    assert pytest.approx(closed_trade["ExitPriceExec"], 0.001) == 12.6 * (1.0 - 0.025)
    assert closed_trade["GrossPnLEur"] == (12.6 - 10.0) * 100
    assert closed_trade["NetPnLEur"] > 0
    assert closed_trade["NetReturnPct"] > 20.0


def test_satellite_hard_stop_loss_triggered(temp_csv_paths):
    open_pos_path, trade_hist_path = temp_csv_paths
    manager = ExitManager(
        open_positions_path=open_pos_path,
        trade_history_path=trade_hist_path,
        hard_stop_pct=0.15,
        exit_slippage_pct=0.025,
    )

    pos = {
        "Ticker": "TEST.HE",
        "EntryDate": "2026-08-01",
        "EntryPrice": 10.0,
        "HighestPrice": 10.0,
        "Shares": 100,
        "PositionValue": 1000.0,
        "Strategy_Type": "SATELLITE",
    }

    # Price dropped from 10.0 to 8.40 (-16.0% <= -15.0%)
    dates = pd.date_range(start="2026-08-01", periods=5, freq="D")
    price_df = pd.DataFrame({
        "Open": [10.0, 9.5, 9.0, 8.7, 8.4],
        "High": [10.0, 9.6, 9.1, 8.8, 8.5],
        "Low": [9.8, 9.2, 8.8, 8.4, 8.3],
        "Close": [10.0, 9.4, 8.9, 8.6, 8.4],
        "Volume": [10000, 12000, 15000, 9000, 11000]
    }, index=dates)

    should_exit, closed_trade, surviving_pos = manager.evaluate_position_exit(pos, price_df)

    assert should_exit is True
    assert closed_trade is not None
    assert surviving_pos is None
    assert "Hard Stop Loss Hit" in closed_trade["ExitReason"]
    assert closed_trade["ExitPriceRaw"] == 8.40


def test_satellite_trailing_stop_loss_triggered(temp_csv_paths):
    open_pos_path, trade_hist_path = temp_csv_paths
    manager = ExitManager(
        open_positions_path=open_pos_path,
        trade_history_path=trade_hist_path,
        trailing_stop_pct=0.15,
        exit_slippage_pct=0.025,
    )

    pos = {
        "Ticker": "TEST.HE",
        "EntryDate": "2026-08-01",
        "EntryPrice": 10.0,
        "HighestPrice": 10.0,
        "Shares": 100,
        "PositionValue": 1000.0,
        "Strategy_Type": "SATELLITE",
    }

    # Spiked to 12.0 then pulled back to 10.0 (-16.7% from peak)
    dates = pd.date_range(start="2026-08-01", periods=6, freq="D")
    price_df = pd.DataFrame({
        "Open": [10.0, 11.0, 11.8, 11.5, 10.8, 10.1],
        "High": [10.5, 11.5, 12.0, 11.7, 11.0, 10.2],
        "Low": [9.9, 10.8, 11.2, 10.9, 10.3, 9.9],
        "Close": [10.2, 11.4, 11.9, 11.2, 10.5, 10.0],
        "Volume": [10000, 25000, 30000, 15000, 12000, 14000]
    }, index=dates)

    should_exit, closed_trade, surviving_pos = manager.evaluate_position_exit(pos, price_df)

    assert should_exit is True
    assert closed_trade is not None
    assert surviving_pos is None
    assert "Trailing Stop Hit" in closed_trade["ExitReason"]


def test_satellite_time_decay_exit_triggered(temp_csv_paths):
    open_pos_path, trade_hist_path = temp_csv_paths
    manager = ExitManager(
        open_positions_path=open_pos_path,
        trade_history_path=trade_hist_path,
        max_holding_days=45,
        time_decay_min_pnl_pct=0.05,
    )

    pos = {
        "Ticker": "STAGNANT.HE",
        "EntryDate": "2026-06-01",
        "EntryPrice": 10.0,
        "HighestPrice": 10.3,
        "Shares": 100,
        "PositionValue": 1000.0,
        "Strategy_Type": "SATELLITE",
    }

    dates = pd.date_range(start="2026-06-01", periods=50, freq="D")
    price_df = pd.DataFrame({
        "Open": [10.0] * 50,
        "High": [10.3] * 50,
        "Low": [9.9] * 50,
        "Close": [10.2] * 50,
        "Volume": [5000] * 50
    }, index=dates)

    should_exit, closed_trade, surviving_pos = manager.evaluate_position_exit(pos, price_df)

    assert should_exit is True
    assert closed_trade is not None
    assert surviving_pos is None
    assert "Time Decay Exit" in closed_trade["ExitReason"]


def test_core_position_never_sells_on_daily_price_drops_or_time_decay(temp_csv_paths):
    """
    CRITICAL TEST: Core Tenbagger positions NEVER sell based on daily price drops,
    hard stop losses, trailing stops, or 45-day time decay.
    """
    open_pos_path, trade_hist_path = temp_csv_paths
    manager = ExitManager(
        open_positions_path=open_pos_path,
        trade_history_path=trade_hist_path,
        hard_stop_pct=0.15,
        trailing_stop_pct=0.15,
        max_holding_days=45,
    )

    core_pos = {
        "Ticker": "CORE_GROWTH.HE",
        "EntryDate": "2026-05-01",
        "EntryPrice": 10.0,
        "HighestPrice": 15.0,
        "Shares": 200,
        "PositionValue": 2000.0,
        "Strategy_Type": "CORE",
    }

    # Scenario: 60 days later, stock price crashed to 7.0 (-30% loss, -53% from peak 15.0)
    dates = pd.date_range(start="2026-05-01", periods=60, freq="D")
    price_df = pd.DataFrame({
        "Open": [10.0] * 30 + [15.0] * 10 + [7.0] * 20,
        "High": [10.5] * 30 + [15.5] * 10 + [7.5] * 20,
        "Low": [9.5] * 30 + [14.0] * 10 + [6.5] * 20,
        "Close": [10.0] * 30 + [15.0] * 10 + [7.0] * 20,
        "Volume": [5000] * 60
    }, index=dates)

    should_exit, closed_trade, surviving_pos = manager.evaluate_position_exit(core_pos, price_df)

    # Core MUST HOLD despite -30% drop and >45 days duration
    assert should_exit is False
    assert closed_trade is None
    assert surviving_pos is not None
    assert surviving_pos["Strategy_Type"] == "CORE"
    assert surviving_pos["HighestPrice"] == 15.5


def test_core_position_exits_only_on_fundamental_deterioration(temp_csv_paths):
    open_pos_path, trade_hist_path = temp_csv_paths
    manager = ExitManager(open_positions_path=open_pos_path, trade_history_path=trade_hist_path)

    core_pos = {
        "Ticker": "CORE_FAILS.HE",
        "EntryDate": "2026-05-01",
        "EntryPrice": 10.0,
        "HighestPrice": 12.0,
        "Shares": 100,
        "PositionValue": 1000.0,
        "Strategy_Type": "CORE",
    }

    dates = pd.date_range(start="2026-05-01", periods=10, freq="D")
    price_df = pd.DataFrame({
        "Open": [10.0] * 10,
        "High": [11.0] * 10,
        "Low": [9.5] * 10,
        "Close": [10.5] * 10,
        "Volume": [5000] * 10
    }, index=dates)

    # Subsequent quarterly report indicates gross margin compression and broken SaaS model
    is_failed, reason = manager.evaluate_core_fundamental_exit(
        ticker="CORE_FAILS.HE",
        cash_issue=False,
        gross_margin_pct=32.0,
        recurring_revenue=False,
        rule_of_40_passed=False,
    )
    assert is_failed is True
    assert "Gross margin compressed" in reason

    should_exit, closed_trade, surviving_pos = manager.evaluate_position_exit(
        core_pos, price_df, fundamental_exit_reason=reason
    )

    assert should_exit is True
    assert closed_trade is not None
    assert surviving_pos is None
    assert closed_trade["Strategy_Type"] == "CORE"
    assert "Core Fundamental Deterioration" in closed_trade["ExitReason"]
