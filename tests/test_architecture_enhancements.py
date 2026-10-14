"""
tests/test_architecture_enhancements.py - Unit tests for:
1. 500k ADV Liquidity Protection for P12
2. Smart Routing & Strict Limit Orders (Market order ban & 15-min FOK kill)
3. Tax Drag Optimization (AOT allocation cap vs. OY corporate compounding)
"""

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import pytest
import yaml

from main_controller import (
    PortfolioConfig,
    PortfolioInstance,
    MasterLiveTradingDaemon,
)
from intraday_p12_scanner import IntradayP12Scanner


@pytest.fixture
def temp_workspace():
    tmp_dir = Path(tempfile.mkdtemp())
    portfolios_dir = tmp_dir / "data" / "portfolios"
    portfolios_dir.mkdir(parents=True, exist_ok=True)
    yield tmp_dir
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_adv_500k_liquidity_protection(temp_workspace):
    """Verify P12 rejects stocks with ADV < 500,000 to keep market impact < 0.5%."""
    cfg = PortfolioConfig(
        portfolio_id="P12_Momentum_Breakout",
        strategy="Momentum_Breakout",
        min_adv=500000.0,
        min_adv_20d=500000.0,
        slots=5,
        slot_size=2000.0,
        start_cash=10000.0,
    )
    p = PortfolioInstance(cfg, base_dir=temp_workspace)
    p.cash_balance = 10000.0

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08, "EURSEK": 11.30}
    daemon.market_engine.prices_cache = {"LOW_ADV": 10.0, "HIGH_ADV": 10.0}
    daemon.market_engine.momentum_cache = {
        "LOW_ADV": {"price": 10.0, "day_change_pct": 5.0, "day_volume": 200000, "runup_3d_pct": 5.0, "rsi_14": 55.0},
        "HIGH_ADV": {"price": 10.0, "day_change_pct": 5.0, "day_volume": 600000, "runup_3d_pct": 5.0, "rsi_14": 55.0},
    }
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "Clear")

    # LOW_ADV has ADV 200,000 (< 500k), HIGH_ADV has ADV 1,000,000 (>= 500k)
    daemon.market_engine.universe_metadata = {
        "LOW_ADV": {"market": "US", "currency": "USD", "adv_20d_usd": 200000.0, "adv_20d_local": 200000.0, "current_price": 10.0},
        "HIGH_ADV": {"market": "US", "currency": "USD", "adv_20d_usd": 1000000.0, "adv_20d_local": 1000000.0, "current_price": 10.0},
    }

    buys = daemon.execute_phase2_screening(p)
    assert buys == 1
    assert len(p.positions) == 1
    assert p.positions[0]["Ticker"] == "HIGH_ADV"


def test_smart_routing_limit_orders(temp_workspace):
    """Verify that orders are executed strictly as LIMIT orders and price runups above limit are killed."""
    cfg = PortfolioConfig(
        portfolio_id="P12_Momentum_Breakout",
        strategy="Momentum_Breakout",
        min_adv=500000.0,
        slots=5,
        slot_size=2000.0,
        start_cash=10000.0,
        order_type="LIMIT",
        limit_buffer_pct=0.002,  # +0.2% limit cap
        time_in_force="FOK_15MIN",
    )
    p = PortfolioInstance(cfg, base_dir=temp_workspace)
    p.cash_balance = 10000.0

    daemon = MasterLiveTradingDaemon.__new__(MasterLiveTradingDaemon)
    daemon.market_engine = type("Engine", (), {})()
    daemon.market_engine.fx_rates = {"EURUSD": 1.08}
    daemon.market_engine.get_ticker_nlp_status = lambda t: ("HOLD", "Clear")

    # Case 1: Ask price spiked to 10.50 while last candle close was 10.00 (+5% > limit buffer +0.2%).
    # Target limit = 10.00 * 1.002 = 10.02. Current ask 10.50 > 10.02 -> Must be killed (FOK 15-min TTL).
    daemon.market_engine.universe_metadata = {
        "SPIKE": {"market": "US", "currency": "USD", "adv_20d_usd": 1000000.0, "current_price": 10.50},
    }
    daemon.market_engine.momentum_cache = {
        "SPIKE": {"price": 10.50, "last_candle_close": 10.00, "prev_close": 10.00, "day_change_pct": 5.0, "day_volume": 600000, "runup_3d_pct": 5.0, "rsi_14": 55.0},
    }

    buys = daemon.execute_phase2_screening(p)
    assert buys == 0
    assert len(p.positions) == 0

    # Case 2: Candidate within limit buffer (ask 10.01 <= limit 10.02). Must execute as LIMIT order!
    daemon.market_engine.universe_metadata = {
        "EXEC": {"market": "US", "currency": "USD", "adv_20d_usd": 1000000.0, "current_price": 10.01},
    }
    daemon.market_engine.momentum_cache = {
        "EXEC": {"price": 10.01, "last_candle_close": 10.00, "prev_close": 10.00, "day_change_pct": 3.0, "day_volume": 600000, "runup_3d_pct": 3.0, "rsi_14": 55.0},
    }

    buys2 = daemon.execute_phase2_screening(p)
    assert buys2 == 1
    assert len(p.positions) == 1
    pos = p.positions[0]
    assert pos["Ticker"] == "EXEC"
    assert pos["Order Type"] == "LIMIT"
    assert pos["Limit Price"] == 10.02
    assert pos["Time In Force"] == "FOK_15MIN"
    assert pos["Buy Price"] == 10.01


def test_tax_profile_aot_vs_oy(temp_workspace):
    """Verify that AOT caps P12 allocation at 5k to minimize tax churn and reallocates to long holding portfolios, while OY allows full 10k."""
    config_yaml = temp_workspace / "portfolios_config.yaml"
    
    yaml_content_aot = {
        "account_tax_profile": "AOT",
        "portfolios": {
            "P1_Base": {"strategy": "Profile B", "slots": 10, "start_cash": 10000},
            "P5_Nordic_Only": {"strategy": "Profile B", "slots": 10, "start_cash": 10000},
            "P11_Meta_Consensus": {"strategy": "Meta_Consensus", "slots": 5, "slot_size": 2000, "start_cash": 10000},
            "P12_Momentum_Breakout": {"strategy": "Momentum_Breakout", "slots": 5, "slot_size": 2000, "start_cash": 10000, "min_adv_20d": 500000},
        }
    }
    with open(config_yaml, "w", encoding="utf-8") as f:
        yaml.dump(yaml_content_aot, f)

    daemon = MasterLiveTradingDaemon(config_yaml_path=config_yaml)
    p_map = {p.config.portfolio_id: p.config for p in daemon.portfolios}

    # In AOT mode:
    # P12 must be scaled down to 5,000 € start cash and 1,000 € slot size
    assert p_map["P12_Momentum_Breakout"].start_cash == 5000.0
    assert p_map["P12_Momentum_Breakout"].slot_size == 1000.0
    # Long-holding portfolios boosted to 11,250 €
    assert p_map["P5_Nordic_Only"].start_cash == 11250.0
    assert p_map["P11_Meta_Consensus"].start_cash == 11250.0

    # In OY mode:
    daemon_oy = MasterLiveTradingDaemon(config_yaml_path=config_yaml, tax_profile="OY")
    p_map_oy = {p.config.portfolio_id: p.config for p in daemon_oy.portfolios}

    assert p_map_oy["P12_Momentum_Breakout"].start_cash == 10000.0
    assert p_map_oy["P12_Momentum_Breakout"].slot_size == 2000.0
