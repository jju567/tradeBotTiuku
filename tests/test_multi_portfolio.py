"""
tests/test_multi_portfolio.py - Tests for Multi-Portfolio Configuration, Market Engine, and Email Notifier.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import yaml
import pytest

from email_notifier import send_portfolio_alert
from main_controller import (
    PortfolioConfig,
    PortfolioInstance,
    MarketDataEngine,
    MasterLiveTradingDaemon,
    DEFAULT_PORTFOLIOS_YAML,
)


def test_portfolios_yaml_structure():
    """Verify that portfolios_config.yaml exists and contains all 10 expected portfolios with valid parameters."""
    assert DEFAULT_PORTFOLIOS_YAML.exists()
    with open(DEFAULT_PORTFOLIOS_YAML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert "portfolios" in data
    portfolios = data["portfolios"]
    assert len(portfolios) == 10

    expected_portfolios = [
        "P1_Base",
        "P2_Fast_Cycle",
        "P3_Diamond_Hands",
        "P4_Institutional",
        "P5_Nordic_Only",
        "P6_US_Only",
        "P7_Deep_Value_Extreme",
        "P8_Quality_Growth",
        "P9_High_Conviction",
        "P10_Micro_Sniper",
    ]
    for p in expected_portfolios:
        assert p in portfolios, f"Missing portfolio {p}"
        cfg = portfolios[p]
        assert "strategy" in cfg
        assert "dead_money_days" in cfg
        assert "min_adv" in cfg
        assert "slots" in cfg
        assert "start_cash" in cfg
        assert "regions" in cfg
        assert cfg["start_cash"] == 10000


def test_portfolio_instance_creation(tmp_path):
    """Test PortfolioInstance state initialization, save, load, and snapshot."""
    cfg = PortfolioConfig(
        portfolio_id="P_Test",
        strategy="Profile B",
        dead_money_days=180,
        min_adv=50000.0,
        slots=10,
        start_cash=10000.0,
        regions=["FI", "SE"],
    )
    inst = PortfolioInstance(cfg, base_dir=tmp_path)
    assert inst.cash_balance == 10000.0
    assert inst.currency == "EUR"
    assert inst.state_file.exists()
    assert inst.history_file.exists()

    # Append dummy trade
    trade = {
        "Ticker": "TEST.HE",
        "Buy Date": "2026-09-01",
        "Sell Date": "2026-09-20",
        "Buy Price": 10.0,
        "Sell Price": 12.0,
        "Shares": 100,
        "Capital Invested": 1009.0,
        "Gross Sale Value": 1200.0,
        "Transaction Fee": 9.0,
        "Net Return": 1191.0,
        "Net PnL": 182.0,
        "Exit Reason": "TAKE_PROFIT",
    }
    inst.append_trade(trade)
    trades = inst.load_trade_history()
    assert len(trades) == 1
    assert trades[0]["Ticker"] == "TEST.HE"


def test_email_notifier_graceful_failure():
    """Verify that email notifier handles socket/SMTP failure gracefully without raising."""
    with patch("smtplib.SMTP", side_effect=Exception("SMTP Connection Refused")):
        with patch("email_notifier.load_env_credentials", return_value={
            "SMTP_SERVER": "invalid.smtp.host",
            "SMTP_PORT": "587",
            "SMTP_USER": "test@user.com",
            "SMTP_PASS": "secret",
            "ALERT_EMAIL": "alert@user.com",
        }):
            res = send_portfolio_alert(
                portfolio_id="P5_Nordic_Only",
                event_type="BUY",
                ticker="EXEL.HE",
                details={"shares": 50, "price": 13.40},
            )
            assert res is False
