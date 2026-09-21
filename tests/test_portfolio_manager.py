"""
Unit tests for screener/portfolio_manager.py (Tri-Layer Fundamental Exit Strategy).
"""

import csv
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from screener.portfolio_manager import (
    PortfolioManager,
    execute_trade_signal,
    update_portfolio,
    evaluate_position,
    CATASTROPHIC_STOP_PCT,
)


@pytest.fixture
def tmp_portfolio_files(tmp_path):
    open_csv = tmp_path / "open_positions.csv"
    trade_csv = tmp_path / "trade_history.csv"
    return open_csv, trade_csv


def test_portfolio_manager_initializes_csv_headers(tmp_portfolio_files):
    open_csv, trade_csv = tmp_portfolio_files
    mgr = PortfolioManager(open_positions_path=open_csv, trade_history_path=trade_csv)

    assert open_csv.exists()
    assert trade_csv.exists()

    with open(open_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        assert headers == [
            "ticker", "market", "buy_date", "buy_price", "current_price",
            "highest_price_seen", "catastrophic_stop", "shares", "currency", "last_evaluated_date"
        ]

    with open(trade_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        assert headers == [
            "ticker", "market", "buy_date", "sell_date", "buy_price",
            "sell_price", "pnl_pct", "pnl_absolute", "exit_reason"
        ]


def test_evaluate_position_layer_1_catastrophic_stop():
    pos = {"ticker": "ATOM", "buy_price": 10.0}
    
    # 50% drop (price <= 5.0) -> Triggers CATASTROPHIC_STOP
    action, reason = evaluate_position(pos, current_price=5.0)
    assert action == "SELL"
    assert reason == "CATASTROPHIC_STOP"

    # Price dropped to 4.90
    action, reason = evaluate_position(pos, current_price=4.90)
    assert action == "SELL"
    assert reason == "CATASTROPHIC_STOP"

    # Price at 5.01 -> Does not trigger catastrophic stop
    action, reason = evaluate_position(pos, current_price=5.01)
    assert action == "HOLD"
    assert reason == "HOLD"


def test_evaluate_position_layer_2_llm_news_radar():
    pos = {"ticker": "EVO.ST", "buy_price": 100.0}

    # Fatal news detected by LLM
    action, reason = evaluate_position(pos, current_price=95.0, latest_news_judgment="REJECT")
    assert action == "SELL"
    assert reason == "LLM_NEWS_REJECT"

    # Neutral or positive news -> HOLD
    action, reason = evaluate_position(pos, current_price=95.0, latest_news_judgment="STRONG BUY")
    assert action == "HOLD"
    assert reason == "HOLD"

    action, reason = evaluate_position(pos, current_price=95.0, latest_news_judgment="HOLD")
    assert action == "HOLD"
    assert reason == "HOLD"


def test_evaluate_position_layer_3_revenue_shrink():
    pos = {"ticker": "GROWTH", "buy_price": 20.0}

    # YoY revenue contraction (< 0) -> Anti-shrinking rule violated
    financials = {
        "revenue_growth_yoy": -5.2,
        "cash_runway_months": 24.0,
        "operating_cash_flow": 1_000_000,
    }
    action, reason = evaluate_position(pos, current_price=22.0, latest_financials=financials)
    assert action == "SELL"
    assert reason == "FUNDAMENTAL_DETERIORATION"


def test_evaluate_position_layer_3_cash_crisis():
    pos = {"ticker": "BURNER", "buy_price": 15.0}

    # Less than 12 months runway AND burning cash (OCF < 0)
    financials = {
        "revenue_growth_yoy": 15.0,
        "cash_runway_months": 8.5,
        "operating_cash_flow": -500_000,
    }
    action, reason = evaluate_position(pos, current_price=16.0, latest_financials=financials)
    assert action == "SELL"
    assert reason == "FUNDAMENTAL_DETERIORATION"

    # Runway < 12 but positive OCF (generating cash) -> Does NOT trigger cash crisis
    healthy_ocf_financials = {
        "revenue_growth_yoy": 15.0,
        "cash_runway_months": 8.5,
        "operating_cash_flow": 200_000,
    }
    action, reason = evaluate_position(pos, current_price=16.0, latest_financials=healthy_ocf_financials)
    assert action == "HOLD"
    assert reason == "HOLD"


def test_evaluate_position_default_hold():
    pos = {"ticker": "SOLID", "buy_price": 50.0}
    healthy_financials = {
        "revenue_growth_yoy": 25.0,
        "cash_runway_months": 36.0,
        "operating_cash_flow": 5_000_000,
    }
    action, reason = evaluate_position(
        pos,
        current_price=55.0,
        latest_news_judgment="HOLD",
        latest_financials=healthy_financials,
    )
    assert action == "HOLD"
    assert reason == "HOLD"


@patch("screener.portfolio_manager.yf.Ticker")
@patch("screener.portfolio_manager.get_realtime_data")
def test_execute_trade_signal_opens_position(mock_price, mock_yf, tmp_portfolio_files):
    open_csv, trade_csv = tmp_portfolio_files
    mgr = PortfolioManager(open_positions_path=open_csv, trade_history_path=trade_csv)

    mock_price.return_value = {
        "ticker": "TESTSTOCK",
        "current_price": 200.0,
        "currency": "USD",
        "market": "US",
        "status": "SUCCESS",
    }
    mock_yf.return_value.history.return_value = MagicMock(empty=True)

    # 1. Open Strong Buy: Buy @ 200, Catastrophic Stop @ 200 * 0.50 = 100.0
    pos = mgr.execute_trade_signal("TESTSTOCK", "US", verdict="STRONG BUY")
    assert pos is not None
    assert pos["ticker"] == "TESTSTOCK"
    assert pos["buy_price"] == 200.0
    assert pos["current_price"] == 200.0
    assert pos["catastrophic_stop"] == 100.0
    assert pos["shares"] == 25.0          # 5000 / 200 = 25
    assert pos["currency"] == "USD"

    # Verify saved in CSV
    positions = mgr.load_open_positions()
    assert len(positions) == 1
    assert positions[0]["ticker"] == "TESTSTOCK"
    assert positions[0]["catastrophic_stop"] == 100.0

    # 2. Reject duplicate buy
    dup_pos = mgr.execute_trade_signal("TESTSTOCK", "US", verdict="STRONG BUY")
    assert dup_pos is None
    assert len(mgr.load_open_positions()) == 1


def test_execute_trade_signal_ignores_non_strong_buy(tmp_portfolio_files):
    open_csv, trade_csv = tmp_portfolio_files
    mgr = PortfolioManager(open_positions_path=open_csv, trade_history_path=trade_csv)

    res = mgr.execute_trade_signal("AAPL", "US", verdict="HOLD")
    assert res is None
    assert len(mgr.load_open_positions()) == 0


@patch("screener.portfolio_manager.yf.Ticker")
@patch("screener.portfolio_manager.get_realtime_data")
def test_update_portfolio_triggers_catastrophic_exit(mock_price, mock_yf, tmp_portfolio_files):
    open_csv, trade_csv = tmp_portfolio_files
    mgr = PortfolioManager(open_positions_path=open_csv, trade_history_path=trade_csv)
    mock_yf.return_value.history.return_value = MagicMock(empty=True)

    # Buy @ 100, Catastrophic Stop = 50.0
    mock_price.return_value = {
        "ticker": "CRASH",
        "current_price": 100.0,
        "currency": "EUR",
        "market": "FI",
        "status": "SUCCESS",
    }
    mgr.execute_trade_signal("CRASH.HE", "FI", verdict="STRONG BUY")

    # Price crashes to 48.0 (<= 50.0) -> Triggers CATASTROPHIC_STOP
    mock_price.return_value = {
        "ticker": "CRASH.HE",
        "current_price": 48.0,
        "currency": "EUR",
        "market": "FI",
        "status": "SUCCESS",
    }
    res = mgr.update_portfolio()
    assert res["open_positions_count"] == 0
    assert res["closed_positions_count"] == 1

    # Open positions CSV should now be empty
    assert len(mgr.load_open_positions()) == 0

    # Trade history CSV should contain closed trade
    with open(trade_csv, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        trade = reader[0]
        assert trade["ticker"] == "CRASH.HE"
        assert float(trade["buy_price"]) == 100.0
        assert float(trade["sell_price"]) == 48.0
        assert float(trade["pnl_pct"]) == -52.0
        assert trade["exit_reason"] == "CATASTROPHIC_STOP"


@patch("screener.portfolio_manager.yf.Ticker")
@patch("screener.portfolio_manager.get_realtime_data")
def test_update_portfolio_triggers_news_and_fundamental_exits(mock_price, mock_yf, tmp_portfolio_files):
    open_csv, trade_csv = tmp_portfolio_files
    mgr = PortfolioManager(open_positions_path=open_csv, trade_history_path=trade_csv)
    mock_yf.return_value.history.return_value = MagicMock(empty=True)

    # 1. Open Pos A and Pos B
    mock_price.return_value = {"ticker": "POSA", "current_price": 10.0, "currency": "USD", "market": "US", "status": "SUCCESS"}
    mgr.execute_trade_signal("POSA", "US", verdict="STRONG BUY")

    mock_price.return_value = {"ticker": "POSB", "current_price": 20.0, "currency": "USD", "market": "US", "status": "SUCCESS"}
    mgr.execute_trade_signal("POSB", "US", verdict="STRONG BUY")

    assert len(mgr.load_open_positions()) == 2

    # 2. Update with News REJECT on POSA and Fundamental Deterioration on POSB
    mock_price.side_effect = lambda t, m: {"ticker": t, "current_price": 9.5 if t == "POSA" else 21.0, "status": "SUCCESS"}

    news_map = {"POSA": "REJECT"}
    fin_map = {"POSB": {"revenue_growth_yoy": -10.0}}

    res = mgr.update_portfolio(news_judgments=news_map, financials_map=fin_map)
    assert res["open_positions_count"] == 0
    assert res["closed_positions_count"] == 2

    with open(trade_csv, "r", encoding="utf-8") as f:
        trades = list(csv.DictReader(f))
        assert len(trades) == 2
        reasons = {t["ticker"]: t["exit_reason"] for t in trades}
        assert reasons["POSA"] == "LLM_NEWS_REJECT"
        assert reasons["POSB"] == "FUNDAMENTAL_DETERIORATION"

