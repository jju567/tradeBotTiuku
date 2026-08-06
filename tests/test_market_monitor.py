import pytest
from unittest.mock import MagicMock, patch
import time
from core.risk_manager import RiskManager
from scheduler.market_monitor import MarketMonitor


def test_risk_manager_sltp_triggers_stop_loss():
    risk_mgr = RiskManager(stop_loss_pct=0.15, take_profit_pct=0.20, volatility_threshold=0.04)

    portfolio_summary = {
        "holdings": {
            "NOKIA.HE": {
                "unrealized_pnl_pct": -18.5,  # Breaches 15% stop loss
                "hodl": False,
                "market_value": 1000.0,
            },
            "NESTE.HE": {
                "unrealized_pnl_pct": -5.0,  # OK
                "hodl": False,
                "market_value": 2000.0,
            },
        }
    }
    market_data = [{"symbol": "NOKIA.HE", "change_pct": -1.0}, {"symbol": "NESTE.HE", "change_pct": 0.5}]

    triggers = risk_mgr.check_sltp_triggers(portfolio_summary, market_data)

    assert len(triggers) == 1
    assert triggers[0]["symbol"] == "NOKIA.HE"
    assert triggers[0]["type"] == "STOP_LOSS_BREACH"
    assert triggers[0]["severity"] == "HIGH"
    assert triggers[0]["requires_ai_wakeup"] is True


def test_risk_manager_sltp_triggers_take_profit():
    risk_mgr = RiskManager(stop_loss_pct=0.15, take_profit_pct=0.20, volatility_threshold=0.04)

    portfolio_summary = {
        "holdings": {
            "QTCOM.HE": {
                "unrealized_pnl_pct": 25.0,  # Reaches 20% take profit target
                "hodl": False,
                "market_value": 3000.0,
            }
        }
    }
    market_data = [{"symbol": "QTCOM.HE", "change_pct": 2.0}]

    triggers = risk_mgr.check_sltp_triggers(portfolio_summary, market_data)

    assert len(triggers) == 1
    assert triggers[0]["symbol"] == "QTCOM.HE"
    assert triggers[0]["type"] == "TAKE_PROFIT_TARGET"
    assert triggers[0]["severity"] == "HIGH"
    assert triggers[0]["requires_ai_wakeup"] is True


def test_risk_manager_sltp_triggers_hodl_protection():
    risk_mgr = RiskManager(stop_loss_pct=0.15, take_profit_pct=0.20, volatility_threshold=0.04)

    portfolio_summary = {
        "holdings": {
            "BTC-EUR": {
                "unrealized_pnl_pct": -30.0,  # Big drop but HODL position
                "hodl": True,
                "market_value": 500.0,
            }
        }
    }
    market_data = [{"symbol": "BTC-EUR", "change_pct": -10.0}]

    triggers = risk_mgr.check_sltp_triggers(portfolio_summary, market_data)

    assert len(triggers) == 2  # 1 HODL Stop loss alert + 1 Volatility swing alert
    hodl_trg = [t for t in triggers if t["type"] == "STOP_LOSS_BREACH"][0]
    assert hodl_trg["symbol"] == "BTC-EUR"
    assert hodl_trg["is_hodl"] is True
    assert hodl_trg["requires_ai_wakeup"] is False  # HODL position bypasses AI trading wake-up


def test_risk_manager_sltp_triggers_cooldown():
    risk_mgr = RiskManager(stop_loss_pct=0.15, take_profit_pct=0.20, volatility_threshold=0.04)

    portfolio_summary = {
        "holdings": {
            "NOKIA.HE": {
                "unrealized_pnl_pct": -20.0,
                "hodl": False,
                "market_value": 1000.0,
            }
        }
    }
    market_data = [{"symbol": "NOKIA.HE", "change_pct": 0.0}]

    now_ts = time.time()
    cooldown_tracker = {"NOKIA.HE:STOP_LOSS_BREACH": now_ts - 3600.0}  # Alerted 1 hour ago (cooldown is 4h)

    # First check with active cooldown -> no new alert
    triggers = risk_mgr.check_sltp_triggers(portfolio_summary, market_data, cooldown_tracker=cooldown_tracker, cooldown_hours=4.0, now_ts=now_ts)
    assert len(triggers) == 0

    # Second check after 5 hours -> cooldown expired -> returns alert
    triggers_after = risk_mgr.check_sltp_triggers(portfolio_summary, market_data, cooldown_tracker=cooldown_tracker, cooldown_hours=4.0, now_ts=now_ts + 18000.0)
    assert len(triggers_after) == 1


@patch("scheduler.market_monitor.NordnetClient")
@patch("scheduler.market_monitor.PortfolioManager")
@patch("scheduler.market_monitor.StockAdvisorAI")
def test_market_monitor_zero_token_normal_run(mock_ai_cls, mock_pm_cls, mock_nc_cls, tmp_path):
    mock_ai = mock_ai_cls.return_value
    mock_pm = mock_pm_cls.return_value
    mock_nc = mock_nc_cls.return_value

    mock_pm.load_state.return_value = {}
    mock_nc.get_portfolio_holdings.return_value = []
    mock_pm.sync_valuation.return_value = {
        "holdings": {
            "NOKIA.HE": {"unrealized_pnl_pct": 2.0, "hodl": False, "market_value": 1000.0}
        }
    }
    mock_nc.get_market_data.return_value = [{"symbol": "NOKIA.HE", "change_pct": 0.5}]

    monitor = MarketMonitor()
    monitor.cooldown_file = tmp_path / "cooldowns.json"

    res = monitor.check_market()

    assert res["status"] == "OK"
    assert res["triggers_found"] == 0
    assert res["tokens_used"] == 0
    # AI Advisor evaluate_equities should NOT be called in normal state (0 tokens)
    mock_ai.evaluate_equities.assert_not_called()
