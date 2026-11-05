"""
Unit tests for EOD Trailing Stop-Loss, Highest Close tracking, Dynamic 2.5x ATR Stop (-20% cap),
Gap-Down min(Open, Stop) execution, and Sell Limit price calculation.
"""

import pytest
from unittest.mock import MagicMock
from main_controller import MasterLiveTradingDaemon, PortfolioInstance, PortfolioConfig
from email_generator import (
    EODActionOrder,
    EODPortfolioSummary,
    build_eod_action_orders,
    format_eod_action_list_text,
    format_eod_action_list_html,
)


def _make_isolated_daemon_and_p13(tmp_path):
    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.prices_cache = {}
    daemon.market_engine.bids_cache = {}
    daemon.market_engine.news_radar_cache = {}
    daemon.market_engine.hard_facts_cache = {}
    daemon.market_engine.momentum_cache = {}
    daemon.market_engine.universe_metadata = {}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "OK")
    daemon.market_engine.get_ticker_hard_facts = lambda t: daemon.market_engine.hard_facts_cache.get(t, {})
    daemon.market_engine._fetch_live_price = lambda t: daemon.market_engine.prices_cache.get(t)

    cfg = PortfolioConfig(
        portfolio_id="P13_Live_Candidate",
        strategy="Hybrid Core-Satellite",
        start_cash=10000.0,
        slots=5,
        sub_strategies={
            "core_nordic_midcap_quality": {"allocated_capital": 5500, "slots": 3, "slot_sizes": [1900, 1800, 1800]},
            "satellite_us_microcap_growth": {"allocated_capital": 1500, "slots": 1, "slot_sizes": [1500]},
            "satellite_nordic_microcap_catalyst": {"allocated_capital": 1500, "slots": 1, "slot_sizes": [1500]},
        },
    )
    p13 = PortfolioInstance(cfg, base_dir=tmp_path)
    return daemon, p13


def test_highest_close_tracking_and_eod_trailing_stop(tmp_path):
    """
    Test 1:
    - highest_close starts at entry price (10.00).
    - Price rises to 12.00 Close -> highest_close updates to 12.00, stop_threshold = 10.20.
    - Price falls to 10.50 Close (> 10.20) -> Retained (HOLD).
    - Price falls to 10.10 Close (<= 10.20) -> SELL with reason containing EOD_TRAILING_STOP.
    - Limit price is calculated as Close * 0.985 (or Bid).
    """
    daemon, p13 = _make_isolated_daemon_and_p13(tmp_path)

    test_pos = {
        "Ticker": "TEST_SAT.ST",
        "Buy Date": "2026-10-01",
        "Buy Price": 10.00,
        "Shares": 100,
        "Capital Invested": 1000.0,
        "Strategy": "Hybrid Core-Satellite",
        "Sub_Strategy": "satellite_nordic_microcap_catalyst",
        "Currency": "SEK",
        "highest_close": 10.00,
        "stop_threshold": 8.50,
    }
    p13.positions = [test_pos]

    # Day 1: Close rises to 12.00 (highest_close -> 12.00, stop -> 10.20)
    daemon.market_engine.prices_cache["TEST_SAT.ST"] = 12.00
    daemon.execute_phase1_exits(p13)
    assert len(p13.positions) == 1
    assert p13.positions[0]["highest_close"] == 12.00
    assert p13.positions[0]["stop_threshold"] == 10.20

    # Day 2: Close drops to 10.50 (above 10.20) -> Position must be retained
    daemon.market_engine.prices_cache["TEST_SAT.ST"] = 10.50
    daemon.execute_phase1_exits(p13)
    assert len(p13.positions) == 1
    assert p13.positions[0]["highest_close"] == 12.00  # highest close unchanged
    assert p13.positions[0]["stop_threshold"] == 10.20

    # Day 3: Close drops to 10.10 (<= 10.20) -> EOD_TRAILING_STOP must trigger
    daemon.market_engine.prices_cache["TEST_SAT.ST"] = 10.10
    run_trades = []
    closed = daemon.execute_phase1_exits(p13, run_trades=run_trades)

    assert closed == 1
    assert len(p13.positions) == 0  # Position closed
    assert len(run_trades) == 1
    trade = run_trades[0]
    assert trade["action"] == "SELL"
    assert "EOD_TRAILING_STOP" in trade["details"]["exit_reason"]

    # Limit price should be Close * 0.985 = 10.10 * 0.985 = 9.9485
    limit_px = trade["details"]["limit_price"]
    assert limit_px == round(10.10 * 0.985, 4)


def test_sell_limit_price_uses_bid_when_available(tmp_path):
    """
    Test 2: When Bid is reliably available, Limit price is set to Bid.
    """
    daemon, p13 = _make_isolated_daemon_and_p13(tmp_path)

    test_pos = {
        "Ticker": "BID_TEST.ST",
        "Buy Date": "2026-10-01",
        "Buy Price": 20.00,
        "Shares": 50,
        "Capital Invested": 1000.0,
        "Sub_Strategy": "satellite_us_microcap_growth",
        "Currency": "SEK",
        "highest_close": 20.00,
        "stop_threshold": 17.00,
    }
    p13.positions = [test_pos]

    # Close drops to 16.50 (<= 17.00 stop), Bid is 16.40
    daemon.market_engine.prices_cache["BID_TEST.ST"] = 16.50
    daemon.market_engine.bids_cache["BID_TEST.ST"] = 16.40

    run_trades = []
    closed = daemon.execute_phase1_exits(p13, run_trades=run_trades)
    assert closed == 1
    assert run_trades[0]["details"]["limit_price"] == 16.40
    assert "Bid" in run_trades[0]["details"]["limit_note"]


def test_dynamic_25x_atr_stop_capped_at_20pct_and_gap_down_execution(tmp_path):
    """
    TASK-07 & TASK-09:
    1. When 2.5x ATR is 25% (e.g. ATR = 10%), stop is strictly capped at -20.0% (stop_threshold = 80.0 on peak 100.0).
    2. When stock gaps down at Open (75.0) below stop_threshold (80.0), execution fills at min(Open, stop) = 75.0, not 80.0.
    """
    daemon, p13 = _make_isolated_daemon_and_p13(tmp_path)

    test_pos = {
        "Ticker": "VOLATILE.ST",
        "Buy Date": "2026-10-01",
        "Buy Price": 100.00,
        "Shares": 10,
        "Capital Invested": 1000.0,
        "Sub_Strategy": "satellite_nordic_microcap_catalyst",
        "Currency": "SEK",
        "highest_close": 100.00,
        "atr_pct": 10.0,  # 2.5 * 10% = 25% -> capped at 20.0%!
    }
    p13.positions = [test_pos]

    # Day 1: Close is 83.00 (-17% drop, which would trigger -15% stop, but 2.5x ATR capped at -20% sets stop at 80.00 -> HOLD)
    daemon.market_engine.prices_cache["VOLATILE.ST"] = 83.00
    closed_d1 = daemon.execute_phase1_exits(p13)
    assert closed_d1 == 0
    assert p13.positions[0]["stop_threshold"] == 80.00
    assert p13.positions[0]["dynamic_stop_pct"] == 20.0

    # Day 2: Overnight gap-down! Opens at 74.50, Low 73.00, Close 76.00 (stop is 80.00)
    # Must fill at min(Open=74.50, Stop=80.00, Close=76.00) = 74.50
    daemon.market_engine.prices_cache["VOLATILE.ST"] = 76.00
    daemon.market_engine.momentum_cache["VOLATILE.ST"] = {
        "day_open": 74.50,
        "day_low": 73.00,
        "day_high": 77.00,
    }
    run_trades = []
    closed_d2 = daemon.execute_phase1_exits(p13, run_trades=run_trades)
    assert closed_d2 == 1
    trades = p13.load_trade_history()
    assert len(trades) == 1
    assert float(trades[0]["Sell Price"]) == pytest.approx(74.50)


def test_email_generator_sell_instructions_formatting():
    """
    Test 3: EOD Action email formats SELL ACTIONS as LIMIT at 10:15 and instructs not to sell at market.
    """
    portfolio_mock = MagicMock()
    portfolio_mock.config.portfolio_id = "P13_Live_Candidate"

    run_trades = [
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "SELL",
            "ticker": "EXIT_TEST.HE",
            "details": {
                "sub_strategy": "satellite_nordic_microcap_catalyst",
                "shares": 200,
                "sell_price": "5.00 EUR",
                "limit_price": 4.925,
                "currency": "EUR",
                "exit_reason": "EOD_TRAILING_STOP (Close 5.00 <= Stop 5.10, -15.0% from Peak Close 6.00)",
            },
        }
    ]

    buys, exits, tps = build_eod_action_orders(portfolio_mock, run_trades=run_trades)
    assert len(exits) == 1
    assert exits[0].exact_limit_price == 4.925
    assert exits[0].order_type == "LIMIT"

    summary = EODPortfolioSummary(
        portfolio_id="P13_Live_Candidate",
        regime_status="NORMAL",
        available_cash_eur=5000.0,
        total_equity_eur=7900.0,
    )

    text_email = format_eod_action_list_text(summary, buys, exits, tps)
    assert "DO NOT SELL AT MARKET" in text_email
    assert "klo 10:15" in text_email
    assert "4.92 EUR" in text_email

    html_email = format_eod_action_list_html(summary, buys, exits, tps)
    assert "markkinahintaan" in html_email
    assert "klo 10:15" in html_email
    assert "EXIT_TEST.HE" in html_email
