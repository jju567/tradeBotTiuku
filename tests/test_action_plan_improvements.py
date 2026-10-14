"""
test_action_plan_improvements.py - Unit tests for:
1. Exhaustion & Overbought filters in P12 (max_3d_runup_pct, max_rsi_14)
2. Partial Take-Profit and runner trailing stop (P1-P4)
3. Market Regime cash buffer enforcement (max 60% invested / min 40% cash in CHOPPY/BEAR)
4. Region-differentiated Stop-Loss (US -8%, Nordic -12%)
"""

import json
import math
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import pytest

from main_controller import (
    PortfolioConfig,
    PortfolioInstance,
    MasterLiveTradingDaemon,
    get_ticker_region,
    calculate_rsi,
)


@pytest.fixture
def temp_workspace():
    tmp_dir = Path(tempfile.mkdtemp())
    portfolios_dir = tmp_dir / "data" / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    yield tmp_dir
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_helper_get_ticker_region_and_rsi():
    assert get_ticker_region("KAMUX.HE") == "FI"
    assert get_ticker_region("ELON.ST") == "SE"
    assert get_ticker_region("CHEMM.CO") == "DK"
    assert get_ticker_region("MNDR") == "US"
    assert get_ticker_region("MNDR", known_market="FI") == "FI"

    # Test RSI
    flat = pd.Series([10.0] * 20)
    assert calculate_rsi(flat) == 50.0

    rising = pd.Series(list(range(1, 30)))
    rsi_up = calculate_rsi(rising)
    assert rsi_up > 80.0

    falling = pd.Series(list(range(30, 1, -1)))
    rsi_down = calculate_rsi(falling)
    assert rsi_down < 20.0


def test_partial_take_profit_and_runner_trailing_stop(temp_workspace):
    cfg = PortfolioConfig(
        portfolio_id="P1_Base",
        strategy="Profile B",
        start_cash=10000.0,
        slots=10,
        partial_take_profit=True,
        take_profit_threshold_pct=0.25,
        partial_sell_fraction=0.50,
        partial_trailing_stop_pct=0.15,
    )
    p = PortfolioInstance(cfg, base_dir=temp_workspace)
    p.cash_balance = 8000.0
    p.positions = [
        {
            "Ticker": "TEST.HE",
            "Buy Price": 10.0,
            "Shares": 100,
            "Capital Invested": 1000.0,
            "Buy Date": "2026-09-01",
            "Peak Price": 10.0,
            "Currency": "EUR",
        }
    ]
    p.save_state()

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.prices_cache = {"TEST.HE": 12.60}  # +26% gain (above +25% threshold)
    daemon.market_engine.news_radar_cache = {"TEST.HE": ("HOLD", "Clear")}
    daemon.market_engine.hard_facts_cache = {"TEST.HE": {}}
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}

    # Execute Phase 1
    closed = daemon.execute_phase1_exits(p)
    assert closed == 0  # Position is NOT fully closed, it's partially closed

    # Check updated position state
    assert len(p.positions) == 1
    pos = p.positions[0]
    assert pos["Shares"] == 50
    assert pos["Capital Invested"] == 500.0
    assert pos["partial_profit_taken"] is True
    assert pos["Peak Price"] == 12.60

    # Cash balance increased (8000 + 50 * 12.60 - fee)
    assert p.cash_balance > 8500.0

    # Trade history has partial sale
    trades = p.load_trade_history()
    assert len(trades) == 1
    assert "PARTIAL_TAKE_PROFIT" in trades[0]["Exit Reason"]
    assert int(trades[0]["Shares"]) == 50

    # Now simulate the runner reaching new peak 15.00, then dropping to 12.00 (-20% from peak, exceeds -15% runner stop)
    daemon.market_engine.prices_cache["TEST.HE"] = 15.00
    daemon.execute_phase1_exits(p)
    assert p.positions[0]["Peak Price"] == 15.00

    daemon.market_engine.prices_cache["TEST.HE"] = 12.00  # (15 - 12)/15 = 20% pullback
    closed2 = daemon.execute_phase1_exits(p)
    assert closed2 == 1
    assert len(p.positions) == 0

    trades2 = p.load_trade_history()
    assert len(trades2) == 2
    assert "PARTIAL_RUNNER_TRAILING_STOP" in trades2[1]["Exit Reason"]


def test_market_regime_cash_buffer_enforcement(temp_workspace):
    cfg = PortfolioConfig(
        portfolio_id="P1_Base",
        strategy="Profile B",
        start_cash=10000.0,
        slots=10,
        market_regime_cash_buffer_pct=0.40,  # Min 4,000 EUR cash in CHOPPY
    )
    p = PortfolioInstance(cfg, base_dir=temp_workspace)
    p.cash_balance = 3500.0  # Under 4,000 EUR buffer
    p.positions = []

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.operational_metrics = {}
    daemon.market_engine.market_regime = {
        "regime": "NEUTRAL / CHOPPY",
        "last_price": 180.0,
        "sma_50": 190.0,
    }
    daemon.market_engine.universe_metadata = {
        "TEST.HE": {
            "market": "FI",
            "adv_20d_local": 100000.0,
            "currency": "EUR",
            "current_price": 10.0,
        }
    }
    daemon.market_engine.prices_cache = {"TEST.HE": 10.0}
    daemon.market_engine.eval_profiles_cache = {"TEST.HE": {"is_profile_b": True, "conviction_score": 10}}
    daemon.market_engine.news_radar_cache = {"TEST.HE": ("HOLD", "Clear")}
    daemon.market_engine.hard_facts_cache = {"TEST.HE": {}}
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}

    # Screening should halt buys because cash (3,500) < required buffer (4,000)
    buys = daemon.execute_phase2_screening(p)
    assert buys == 0
    assert len(p.positions) == 0

    # If cash is 6,000 (2,000 above buffer), it should allow a buy
    p.cash_balance = 6000.0
    buys2 = daemon.execute_phase2_screening(p)
    assert buys2 == 1
    assert len(p.positions) == 1
    # Remaining cash must still be >= 4,000 EUR buffer (minus broker fee)
    assert p.cash_balance >= 3990.0


def test_regional_stop_loss_differentiation(temp_workspace):
    cfg = PortfolioConfig(
        portfolio_id="P1_Base",
        strategy="Profile B",
        start_cash=10000.0,
        slots=10,
        us_stop_loss_pct=0.08,      # -8% for US
        nordic_stop_loss_pct=0.12,  # -12% for Nordic
    )
    p = PortfolioInstance(cfg, base_dir=temp_workspace)
    p.cash_balance = 8000.0
    p.positions = [
        {
            "Ticker": "US_STOCK",   # US region
            "Buy Price": 10.0,
            "Shares": 100,
            "Capital Invested": 1000.0,
            "Buy Date": "2026-09-01",
            "Currency": "USD",
        },
        {
            "Ticker": "NORDIC.HE",  # Nordic region
            "Buy Price": 10.0,
            "Shares": 100,
            "Capital Invested": 1000.0,
            "Buy Date": "2026-09-01",
            "Currency": "EUR",
        },
    ]

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    # US stock dropped -9% (exceeds -8% US stop loss)
    # Nordic stock dropped -9% (below -12% Nordic stop loss -> must HOLD)
    daemon.market_engine.prices_cache = {"US_STOCK": 9.10, "NORDIC.HE": 9.10}
    daemon.market_engine.news_radar_cache = {
        "US_STOCK": ("HOLD", "OK"),
        "NORDIC.HE": ("HOLD", "OK"),
    }
    daemon.market_engine.hard_facts_cache = {"US_STOCK": {}, "NORDIC.HE": {}}
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}

    closed = daemon.execute_phase1_exits(p)
    assert closed == 1  # Exactly 1 closed (US_STOCK)
    assert len(p.positions) == 1
    assert p.positions[0]["Ticker"] == "NORDIC.HE"

    trades = p.load_trade_history()
    assert len(trades) == 1
    assert trades[0]["Ticker"] == "US_STOCK"
    assert "STOP_LOSS_US" in trades[0]["Exit Reason"]


def test_p12_exhaustion_and_overbought_filters(temp_workspace):
    cfg = PortfolioConfig(
        portfolio_id="P12_Momentum_Breakout",
        strategy="Momentum_Breakout",
        start_cash=10000.0,
        slots=5,
        slot_size=2000.0,
        volume_surge_multiplier=3.0,
        min_price_change_pct=2.0,
        max_3d_runup_pct=0.40,  # Max +40% 3-day runup
        max_rsi_14=80.0,        # Max RSI 80
    )
    p = PortfolioInstance(cfg, base_dir=temp_workspace)
    p.cash_balance = 10000.0
    p.positions = []

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.market_regime = {"regime": "BULL", "last_price": 200, "sma_50": 190}
    daemon.market_engine.universe_metadata = {
        "RUNUP_CAND": {
            "market": "US",
            "adv_20d_usd": 100000.0,
            "currency": "USD",
            "current_price": 5.0,
        },
        "RSI_CAND": {
            "market": "US",
            "adv_20d_usd": 100000.0,
            "currency": "USD",
            "current_price": 5.0,
        },
        "CLEAN_CAND": {
            "market": "US",
            "adv_20d_usd": 100000.0,
            "currency": "USD",
            "current_price": 5.0,
        },
    }
    daemon.market_engine.prices_cache = {
        "RUNUP_CAND": 5.0,
        "RSI_CAND": 5.0,
        "CLEAN_CAND": 5.0,
    }
    daemon.market_engine.momentum_cache = {
        "RUNUP_CAND": {
            "price": 5.0,
            "day_volume": 100000.0,  # 500k USD > 3x ADV
            "day_change_pct": 10.0,
            "runup_3d_pct": 55.0,    # > +40% -> REJECTED by Exhaustion
            "rsi_14": 65.0,
        },
        "RSI_CAND": {
            "price": 5.0,
            "day_volume": 100000.0,
            "day_change_pct": 10.0,
            "runup_3d_pct": 20.0,
            "rsi_14": 88.0,          # > 80.0 -> REJECTED by Overbought
        },
        "CLEAN_CAND": {
            "price": 5.0,
            "day_volume": 100000.0,
            "day_change_pct": 10.0,
            "runup_3d_pct": 15.0,    # <= 40% -> PASS
            "rsi_14": 65.0,          # <= 80.0 -> PASS
        },
    }
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "Clear")

    buys = daemon.execute_phase2_screening(p)
    assert buys == 1
    assert len(p.positions) == 1
    assert p.positions[0]["Ticker"] == "CLEAN_CAND"


def test_p12_candidate_limit_uses_own_momentum_data(temp_workspace):
    cfg = PortfolioConfig(
        portfolio_id="P12_Momentum_Breakout",
        strategy="Momentum_Breakout",
        start_cash=10000.0,
        slots=5,
        slot_size=2000.0,
        volume_surge_multiplier=3.0,
        min_price_change_pct=2.0,
        limit_buffer_pct=0.002,
    )
    p = PortfolioInstance(cfg, base_dir=temp_workspace)
    p.cash_balance = 10000.0
    p.positions = []

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.market_regime = {"regime": "BULL", "last_price": 200, "sma_50": 190}
    daemon.market_engine.universe_metadata = {
        "CAND_A": {
            "market": "US",
            "adv_20d_usd": 100000.0,
            "currency": "USD",
            "current_price": 10.0,
        },
        "CAND_B": {
            "market": "US",
            "adv_20d_usd": 100000.0,
            "currency": "USD",
            "current_price": 20.0,
        },
    }
    daemon.market_engine.prices_cache = {
        "CAND_A": 10.0,
        "CAND_B": 20.0,
    }
    daemon.market_engine.momentum_cache = {
        "CAND_A": {
            "price": 10.0,
            "day_volume": 100000.0,
            "day_change_pct": 10.0,
            "runup_3d_pct": 10.0,
            "rsi_14": 50.0,
            "vwap": 10.0,
        },
        "CAND_B": {
            "price": 20.0,
            "day_volume": 100000.0,
            "day_change_pct": 10.0,
            "runup_3d_pct": 10.0,
            "rsi_14": 50.0,
            "vwap": 20.0,
        },
    }
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "Clear")

    buys = daemon.execute_phase2_screening(p)
    assert buys == 2
    pos_map = {pos["Ticker"]: pos for pos in p.positions}
    assert pos_map["CAND_A"]["Limit Price"] == round(10.0 * 1.002, 4)
    assert pos_map["CAND_B"]["Limit Price"] == round(20.0 * 1.002, 4)
