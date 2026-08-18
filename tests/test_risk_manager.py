"""Unit tests for core/risk_manager.py — RiskManager audit and trade validation."""
import pytest
from core.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Helpers / Factories
# ---------------------------------------------------------------------------

def make_holding(
    symbol: str = "TEST.HE",
    weight: float = 0.10,
    unrealized_pnl_pct: float = 0.0,
    hodl: bool = False,
    note: str = "",
) -> dict:
    return {
        "symbol": symbol,
        "weight": weight,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "hodl": hodl,
        "note": note,
        "market_value": 1000.0,
    }


def make_trade(
    symbol: str = "TEST.HE",
    trade_value: float = 500.0,
    estimated_commission: float = 7.0,
    action: str = "BUY",
) -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "trade_value": trade_value,
        "estimated_commission": estimated_commission,
    }


# ---------------------------------------------------------------------------
# audit_portfolio_risks — Stop Loss
# ---------------------------------------------------------------------------


class TestAuditPortfolioRisksStopLoss:
    def setup_method(self):
        self.rm = RiskManager(stop_loss_pct=0.15, max_position_weight=0.20, min_trade_eur=200.0)

    def test_no_alerts_for_healthy_portfolio(self):
        portfolio = {"holdings": {"TEST.HE": make_holding(unrealized_pnl_pct=5.0)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        assert alerts == []

    def test_stop_loss_breach_generates_high_severity_alert(self):
        portfolio = {"holdings": {"TEST.HE": make_holding(unrealized_pnl_pct=-20.0)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "STOP_LOSS_BREACH"
        assert alerts[0]["severity"] == "HIGH"
        assert alerts[0]["symbol"] == "TEST.HE"

    def test_stop_loss_breach_recommends_sell_all(self):
        portfolio = {"holdings": {"TEST.HE": make_holding(unrealized_pnl_pct=-15.0)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        assert alerts[0]["recommended_action"] == "SELL_ALL"

    def test_hodl_lock_overrides_stop_loss_to_info(self):
        portfolio = {"holdings": {"TEST.HE": make_holding(unrealized_pnl_pct=-25.0, hodl=True)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        assert alerts[0]["type"] == "HODL_LOCK_PROTECTION"
        assert alerts[0]["severity"] == "INFO"

    def test_hodl_bypasses_take_profit_alert(self):
        """HODL positions like QDVE.DE (+39.9%) should completely bypass Take-Profit alerts."""
        portfolio = {"holdings": {"QDVE.DE": make_holding("QDVE.DE", unrealized_pnl_pct=39.9, hodl=True)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        tp_alerts = [a for a in alerts if a["type"] == "TAKE_PROFIT_TARGET"]
        assert tp_alerts == []

    def test_hodl_bypasses_overconcentration_alert(self):
        """HODL positions should not raise Overconcentration trim alerts."""
        portfolio = {"holdings": {"QDVE.DE": make_holding("QDVE.DE", weight=0.35, hodl=True)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        oc_alerts = [a for a in alerts if a["type"] == "OVERCONCENTRATION"]
        assert oc_alerts == []

    def test_exactly_at_threshold_triggers_alert(self):
        """At exactly -15% PnL an alert should be raised."""
        portfolio = {"holdings": {"TEST.HE": make_holding(unrealized_pnl_pct=-15.0)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        assert len(alerts) >= 1

    def test_just_below_threshold_no_alert(self):
        """At -14.9% (below threshold) no stop-loss alert should be raised."""
        portfolio = {"holdings": {"TEST.HE": make_holding(unrealized_pnl_pct=-14.9)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        stop_loss_alerts = [a for a in alerts if a["type"] == "STOP_LOSS_BREACH"]
        assert stop_loss_alerts == []


# ---------------------------------------------------------------------------
# audit_portfolio_risks — Overconcentration
# ---------------------------------------------------------------------------


class TestAuditPortfolioRisksOverconcentration:
    def setup_method(self):
        self.rm = RiskManager(stop_loss_pct=0.15, max_position_weight=0.20, min_trade_eur=200.0)

    def test_overconcentration_alert_when_weight_exceeds_max(self):
        portfolio = {"holdings": {"BIG.HE": make_holding(weight=0.25)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        oc_alerts = [a for a in alerts if a["type"] == "OVERCONCENTRATION"]
        assert len(oc_alerts) == 1
        assert oc_alerts[0]["severity"] == "MEDIUM"
        assert oc_alerts[0]["recommended_action"] == "REDUCE_POSITION"

    def test_no_overconcentration_at_exactly_max_weight(self):
        portfolio = {"holdings": {"TEST.HE": make_holding(weight=0.20)}}
        alerts = self.rm.audit_portfolio_risks(portfolio)
        oc_alerts = [a for a in alerts if a["type"] == "OVERCONCENTRATION"]
        assert oc_alerts == []

    def test_multiple_alerts_for_multiple_violating_positions(self):
        portfolio = {
            "holdings": {
                "BIG1.HE": make_holding("BIG1.HE", weight=0.30),
                "BIG2.HE": make_holding("BIG2.HE", weight=0.28),
            }
        }
        alerts = self.rm.audit_portfolio_risks(portfolio)
        oc_alerts = [a for a in alerts if a["type"] == "OVERCONCENTRATION"]
        assert len(oc_alerts) == 2

    def test_empty_holdings_returns_no_alerts(self):
        alerts = self.rm.audit_portfolio_risks({"holdings": {}})
        assert alerts == []


# ---------------------------------------------------------------------------
# validate_proposed_trades
# ---------------------------------------------------------------------------


class TestValidateProposedTrades:
    def setup_method(self):
        self.rm = RiskManager(stop_loss_pct=0.15, max_position_weight=0.20, min_trade_eur=200.0)

    def test_valid_trade_passes_through(self):
        trades = [make_trade(trade_value=500.0, estimated_commission=7.0)]
        result = self.rm.validate_proposed_trades(trades)
        assert len(result) == 1

    def test_trade_below_min_eur_is_rejected(self):
        trades = [make_trade(trade_value=100.0, estimated_commission=7.0)]
        result = self.rm.validate_proposed_trades(trades)
        assert result == []

    def test_trade_with_high_commission_ratio_is_rejected(self):
        """Commission > 2.5% of trade value should be rejected."""
        trades = [make_trade(trade_value=200.0, estimated_commission=10.0)]  # 5% ratio
        result = self.rm.validate_proposed_trades(trades)
        assert result == []

    def test_trade_at_exactly_min_eur_passes(self):
        trades = [make_trade(trade_value=200.0, estimated_commission=5.0)]
        result = self.rm.validate_proposed_trades(trades)
        assert len(result) == 1

    def test_empty_trade_list_returns_empty(self):
        result = self.rm.validate_proposed_trades([])
        assert result == []

    def test_mixed_valid_and_invalid_trades(self):
        trades = [
            make_trade("GOOD.HE", trade_value=500.0, estimated_commission=7.0),
            make_trade("SMALL.HE", trade_value=50.0, estimated_commission=7.0),  # too small
            make_trade("EXPENSIVE.HE", trade_value=210.0, estimated_commission=9.0),  # high ratio
        ]
        result = self.rm.validate_proposed_trades(trades)
        assert len(result) == 1
        assert result[0]["symbol"] == "GOOD.HE"


# ---------------------------------------------------------------------------
# check_sltp_triggers & Hysteresis
# ---------------------------------------------------------------------------


class TestCheckSltpTriggers:
    def setup_method(self):
        self.rm = RiskManager(take_profit_pct=0.20, stop_loss_pct=0.15)

    def test_hodl_position_bypasses_triggers(self):
        portfolio = {"holdings": {"QDVE.DE": make_holding("QDVE.DE", unrealized_pnl_pct=35.0, hodl=True)}}
        triggers = self.rm.check_sltp_triggers(portfolio, [], {}, cooldown_hours=24.0, now_ts=1000.0)
        assert triggers == []

    def test_repetitive_static_pnl_filtered_out(self):
        portfolio = {"holdings": {"STOCK.HE": make_holding("STOCK.HE", unrealized_pnl_pct=25.0, hodl=False)}}
        cooldowns = {"STOCK.HE:TAKE_PROFIT_TARGET": {"timestamp": 1000.0, "pnl_pct": 24.8}}
        # 48 hours passed (cooldown expired), but PnL moved only +0.2% (< 5.0%)
        triggers = self.rm.check_sltp_triggers(portfolio, [], cooldowns, cooldown_hours=24.0, now_ts=1000.0 + 48 * 3600)
        assert triggers == []

    def test_significant_pnl_jump_fires_alert(self):
        portfolio = {"holdings": {"STOCK.HE": make_holding("STOCK.HE", unrealized_pnl_pct=32.0, hodl=False)}}
        cooldowns = {"STOCK.HE:TAKE_PROFIT_TARGET": {"timestamp": 1000.0, "pnl_pct": 24.8}}
        # 48 hours passed and PnL jumped from 24.8% to 32.0% (>= 5.0% change)
        triggers = self.rm.check_sltp_triggers(portfolio, [], cooldowns, cooldown_hours=24.0, now_ts=1000.0 + 48 * 3600)
        assert len(triggers) == 1
        assert triggers[0]["type"] == "TAKE_PROFIT_TARGET"

