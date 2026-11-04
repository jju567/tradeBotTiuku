"""
Unit tests for EOD Trailing Stop-Loss, Highest Close tracking, and Sell Limit price calculation.
"""

import pytest
from unittest.mock import MagicMock, patch
from main_controller import MasterLiveTradingDaemon, PortfolioInstance, PortfolioConfig
from email_generator import (
    EODActionOrder,
    EODPortfolioSummary,
    build_eod_action_orders,
    format_eod_action_list_text,
    format_eod_action_list_html,
)


def test_highest_close_tracking_and_eod_trailing_stop():
    """
    Test 1:
    - highest_close starts at entry price (10.00).
    - Price rises to 12.00 Close -> highest_close updates to 12.00, stop_threshold = 10.20.
    - Price falls to 10.50 Close (> 10.20) -> Retained (HOLD).
    - Price falls to 10.10 Close (<= 10.20) -> SELL with reason containing EOD_TRAILING_STOP.
    - Limit price is calculated as Close * 0.985 (or Bid).
    """
    daemon = MasterLiveTradingDaemon()
    p13 = next((p for p in daemon.portfolios if p.config.portfolio_id == "P13_Live_Candidate"), None)
    assert p13 is not None

    # Create a mock satellite position
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


def test_sell_limit_price_uses_bid_when_available():
    """
    Test 2: When Bid is reliably available, Limit price is set to Bid.
    """
    daemon = MasterLiveTradingDaemon()
    p13 = next((p for p in daemon.portfolios if p.config.portfolio_id == "P13_Live_Candidate"), None)
    assert p13 is not None

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
