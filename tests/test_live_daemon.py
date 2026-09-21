"""
Unit and Integration Tests for Live Forward-Testing Daemon (main_controller.py).
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from main_controller import (
    PaperAccountManager,
    LiveTradingDaemon,
    STARTING_BALANCE,
)


@pytest.fixture
def temp_daemon_env(tmp_path):
    """Sets up temporary files for paper account, open positions, trade history, and universe."""
    account_file = tmp_path / "paper_account.json"
    open_pos_file = tmp_path / "open_positions.csv"
    history_file = tmp_path / "trade_history.csv"
    universe_file = tmp_path / "clean_universe.csv"

    # Create mock universe
    universe_content = (
        "ticker,market,market_cap_usd,market_cap_local,currency,current_price,adv_20d_local,adv_20d_usd\n"
        "GOOD_B,US,50000000.0,50000000.0,USD,5.00,100000.0,100000.0\n"
        "GROWTH_A,US,80000000.0,80000000.0,USD,10.00,200000.0,200000.0\n"
        "LOW_LIQ,US,20000000.0,20000000.0,USD,2.00,5000.0,5000.0\n"
    )
    universe_file.write_text(universe_content, encoding="utf-8")

    daemon = LiveTradingDaemon(
        account_path=account_file,
        open_positions_path=open_pos_file,
        trade_history_path=history_file,
        clean_universe_path=universe_file,
        starting_balance=10_000.0,
    )
    return daemon, tmp_path


def test_paper_account_initialization(temp_daemon_env):
    daemon, tmp_path = temp_daemon_env
    account = daemon.account

    assert account.cash_balance == 10_000.0
    assert account.currency == "USD"
    assert daemon.account_path.exists()

    # Test cash deduction
    account.cash_balance -= 2_500.0
    account.save()

    # Reload from disk
    reloaded = PaperAccountManager(daemon.account_path)
    assert reloaded.cash_balance == 7_500.0

    # Reset
    account.reset()
    assert account.cash_balance == 10_000.0


def test_phase1_catastrophic_stop(temp_daemon_env):
    daemon, tmp_path = temp_daemon_env

    # Setup open position: Bought at 10.0, current price drops to 4.5 (-55%)
    daemon.save_open_positions([
        {"Ticker": "CRASH", "Buy Date": "2026-09-01", "Buy Price": 10.0, "Shares": 100, "Strategy": "PROFILE_B"}
    ])

    with patch.object(daemon, "fetch_live_price", return_value=4.5), \
         patch.object(daemon, "evaluate_news_radar", return_value=("HOLD", "Clean")), \
         patch("main_controller.get_hard_financials", return_value={}):

        closed = daemon.execute_portfolio_management()
        assert closed == 1

        # Position should be removed
        remaining = daemon.load_open_positions()
        assert len(remaining) == 0

        # Cash balance updated: Gross = 100 * 4.5 = 450.0. Fee = max(450 * 0.002, 9.00) = 9.00. Net Proceeds = 441.00.
        expected_cash = 10_000.0 + 441.00
        assert abs(daemon.account.cash_balance - expected_cash) < 1.0


def test_phase1_news_radar_reject(temp_daemon_env):
    daemon, tmp_path = temp_daemon_env

    # Position at steady price, but news mentions dilution/bankruptcy
    daemon.save_open_positions([
        {"Ticker": "DILUTE", "Buy Date": "2026-09-01", "Buy Price": 10.0, "Shares": 100, "Strategy": "PROFILE_B"}
    ])

    with patch.object(daemon, "fetch_live_price", return_value=10.0), \
         patch.object(daemon, "evaluate_news_radar", return_value=("REJECT", "Toxic dilution detected")), \
         patch("main_controller.get_hard_financials", return_value={}):

        closed = daemon.execute_portfolio_management()
        assert closed == 1

        remaining = daemon.load_open_positions()
        assert len(remaining) == 0


def test_phase1_fundamental_deterioration_exit(temp_daemon_env):
    daemon, tmp_path = temp_daemon_env

    # Position at steady price, but financial report shows shrinking revenue
    daemon.save_open_positions([
        {"Ticker": "SHRINK", "Buy Date": "2026-09-01", "Buy Price": 10.0, "Shares": 100, "Strategy": "PROFILE_B"}
    ])

    mock_financials = {
        "revenue_growth_yoy_pct": -15.0,  # Revenue shrank 15%
        "cash_runway_months": 24.0,
        "operating_cash_flow_ttm": 500_000.0,
    }

    with patch.object(daemon, "fetch_live_price", return_value=10.0), \
         patch.object(daemon, "evaluate_news_radar", return_value=("HOLD", "Clean")), \
         patch("main_controller.get_hard_financials", return_value=mock_financials):

        closed = daemon.execute_portfolio_management()
        assert closed == 1

        remaining = daemon.load_open_positions()
        assert len(remaining) == 0


def test_phase1_hold_healthy_position(temp_daemon_env):
    daemon, tmp_path = temp_daemon_env

    daemon.save_open_positions([
        {"Ticker": "HEALTHY", "Buy Date": "2026-09-01", "Buy Price": 10.0, "Shares": 100, "Strategy": "PROFILE_B"}
    ])

    mock_financials = {
        "revenue_growth_yoy_pct": 12.0,
        "cash_runway_months": "Infinite",
        "operating_cash_flow_ttm": 1_000_000.0,
    }

    with patch.object(daemon, "fetch_live_price", return_value=11.5), \
         patch.object(daemon, "evaluate_news_radar", return_value=("HOLD", "Clean")), \
         patch("main_controller.get_hard_financials", return_value=mock_financials):

        closed = daemon.execute_portfolio_management()
        assert closed == 0

        remaining = daemon.load_open_positions()
        assert len(remaining) == 1
        assert remaining[0]["Ticker"] == "HEALTHY"


def test_phase2_screening_profile_b_sizing(temp_daemon_env):
    daemon, tmp_path = temp_daemon_env

    # Mock evaluate_profiles to return Profile B for GOOD_B, and Profile A for GROWTH_A
    def mock_eval_profiles(facts):
        ticker = facts.get("ticker", "")
        if ticker == "GOOD_B":
            return {"is_profile_b": True, "is_profile_a": False, "signal": "BUY_PROFILE_B"}
        elif ticker == "GROWTH_A":
            return {"is_profile_b": False, "is_profile_a": True, "signal": "BUY_PROFILE_A"}
        return {"is_profile_b": False, "is_profile_a": False, "signal": "REJECT"}

    def mock_hard_facts(ticker):
        return {"ticker": ticker, "net_cash": 10_000_000.0}

    with patch("main_controller.get_hard_financials", side_effect=mock_hard_facts), \
         patch("main_controller.evaluate_profiles", side_effect=mock_eval_profiles), \
         patch.object(daemon, "fetch_live_price", return_value=5.0):

        new_buys = daemon.execute_market_screening()

        # ONLY GOOD_B should be bought (Profile A is disabled!)
        assert new_buys == 1

        open_pos = daemon.load_open_positions()
        assert len(open_pos) == 1
        bought = open_pos[0]
        assert bought["Ticker"] == "GOOD_B"

        # Check Sizing:
        # Target allocation = 1,000 USD (10% of 10k).
        # GOOD_B ADV = 100,000 USD. 10% ADV cap = 10,000 USD.
        # Investable cash = 1,000 - 9.00 = 991.00 USD.
        # Price = 5.00 USD -> math.floor(991.00 / 5.00) = 198 whole shares.
        # Gross buy = 198 * 5.00 = 990.00. Fee = 9.00. Total cost = 999.00 USD.
        assert bought["Shares"] == 198
        assert bought["Capital Invested"] == 999.00

        # Cash balance deducted: 10,000 - 999.00 = 9,001.00
        assert daemon.account.cash_balance == 9_001.00



def test_run_daily_cycle_end_to_end(temp_daemon_env):
    daemon, tmp_path = temp_daemon_env

    with patch.object(daemon, "execute_portfolio_management", return_value=0), \
         patch.object(daemon, "execute_market_screening", return_value=2):

        sold, bought = daemon.run_daily_cycle()
        assert sold == 0
        assert bought == 2


def test_swedish_stock_sell_fx_conversion(temp_daemon_env):
    """
    Critical regression test:
    Selling a Swedish stock (SEK) must NOT credit SEK directly 1:1 into EUR cash balance!
    10,000 SEK must convert to ~885 EUR (with EURSEK=11.30).
    """
    daemon, tmp_path = temp_daemon_env
    daemon.account.currency = "EUR"
    daemon.account.cash_balance = 500.0  # Starting with 500 EUR

    # Position: 42 shares of STIL.ST bought at 241.50 SEK (invested ~10,163 SEK)
    daemon.save_open_positions([
        {"Ticker": "STIL.ST", "Buy Date": "2026-09-01", "Buy Price": 241.50, "Shares": 42, "Capital Invested": 10163.29, "Strategy": "PROFILE_B"}
    ])

    # Sell at 255.50 SEK.
    # Gross SEK = 42 * 255.50 = 10,731.0 SEK.
    # Fee in SEK = max(10,731 * 0.002, 9.0 * 11.30) = max(21.46, 101.7) = 101.7 SEK.
    # Net proceeds SEK = 10,731.0 - 101.7 = 10,629.3 SEK.
    # Converted to EUR @ EURSEK 11.30: 10,629.3 / 11.30 = 940.65 EUR.
    # Cash should be: 500.0 + 940.65 = ~1,440.65 EUR (NOT 500 + 10,629 = 11,129 EUR!).
    with patch.object(daemon, "fetch_live_price", return_value=255.50), \
         patch.object(daemon, "evaluate_news_radar", return_value=("REJECT", "Deterioration")), \
         patch("main_controller.get_hard_financials", return_value={}), \
         patch("main_controller.get_live_fx_rates", return_value={"EURUSD": 1.08, "EURSEK": 11.30}):

        closed = daemon.execute_portfolio_management()
        assert closed == 1

        # Check cash balance: MUST be around ~1,440 EUR, NOT inflated to ~11,000 EUR!
        assert 1400.0 < daemon.account.cash_balance < 1500.0


def test_swedish_stock_buy_fx_conversion(temp_daemon_env):
    """
    Buying a Swedish stock with target allocation of 1,000 EUR
    must convert to ~11,300 SEK, buy proper whole shares, and deduct ~1,000 EUR from EUR cash.
    """
    daemon, tmp_path = temp_daemon_env
    daemon.account.currency = "EUR"
    daemon.account.cash_balance = 10_000.0

    universe_content = (
        "ticker,market,market_cap_usd,market_cap_local,currency,current_price,adv_20d_local,adv_20d_usd\n"
        "SWED_B.ST,SE,50000000.0,550000000.0,SEK,250.0,1000000.0,90000.0\n"
    )
    daemon.clean_universe_path.write_text(universe_content, encoding="utf-8")

    def mock_eval_profiles(facts):
        return {"is_profile_b": True, "is_profile_a": False, "signal": "BUY_PROFILE_B"}

    with patch("main_controller.get_hard_financials", return_value={}), \
         patch("main_controller.evaluate_profiles", side_effect=mock_eval_profiles), \
         patch.object(daemon, "fetch_live_price", return_value=250.0), \
         patch("main_controller.get_live_fx_rates", return_value={"EURUSD": 1.08, "EURSEK": 11.30}):

        new_buys = daemon.execute_market_screening()
        assert new_buys == 1

        open_pos = daemon.load_open_positions()
        assert len(open_pos) == 1
        pos = open_pos[0]
        assert pos["Ticker"] == "SWED_B.ST"

        # 1,000 EUR = 11,300 SEK. Min fee = 9 EUR * 11.30 = 101.7 SEK.
        # Investable SEK = 11,300 - 101.7 = 11,198.3 SEK.
        # Shares @ 250 SEK = floor(11,198.3 / 250) = 44 shares!
        assert pos["Shares"] == 44

        # Total cost SEK = 44 * 250 + 101.7 = 11,101.7 SEK.
        # EUR deducted = 11,101.7 / 11.30 = ~982.45 EUR.
        # Cash remaining = 10,000 - 982.45 = ~9,017.55 EUR.
        assert 9000.0 < daemon.account.cash_balance < 9050.0

