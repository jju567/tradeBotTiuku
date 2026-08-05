"""Unit tests for core/rebalancer.py — PortfolioRebalancer."""
import pytest
from core.rebalancer import PortfolioRebalancer


# ---------------------------------------------------------------------------
# Helpers / Factories
# ---------------------------------------------------------------------------

def make_portfolio(
    total_equity: float = 10000.0,
    cash_balance: float = 500.0,
    holdings: dict | None = None,
) -> dict:
    if holdings is None:
        holdings = {}
    return {
        "total_equity": total_equity,
        "cash_balance": cash_balance,
        "holdings": holdings,
    }


def make_holding_entry(
    symbol: str,
    market_value: float,
    current_price: float,
    quantity: int,
    weight: float,
    hodl: bool = False,
    name: str = "",
) -> dict:
    return {
        "symbol": symbol,
        "market_value": market_value,
        "current_price": current_price,
        "quantity": quantity,
        "weight": weight,
        "hodl": hodl,
        "name": name or symbol,
    }


def make_ai_eval(
    symbol: str,
    score: int = 5,
    recommendation: str = "HOLD",
    target_weight: float = 0.05,
    current_price: float = 10.0,
    name: str = "",
) -> dict:
    return {
        "symbol": symbol,
        "score": score,
        "recommendation": recommendation,
        "target_weight": target_weight,
        "current_price": current_price,
        "reasoning": "Test reasoning.",
        "name": name or symbol,
    }


# ---------------------------------------------------------------------------
# calculate_rebalance_plan
# ---------------------------------------------------------------------------


class TestCalculateRebalancePlan:
    def setup_method(self):
        self.rebalancer = PortfolioRebalancer(min_trade_eur=200.0)

    def test_returns_dict_with_required_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)

        portfolio = make_portfolio()
        result = self.rebalancer.calculate_rebalance_plan(portfolio, [])

        assert "proposal_id" in result
        assert "proposed_trades" in result
        assert "trade_count" in result
        assert "total_estimated_commission" in result
        assert result["status"] == "PENDING_HUMAN_APPROVAL"

    def test_no_trades_for_empty_portfolio_and_hold_evaluations(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)

        portfolio = make_portfolio(holdings={})
        ai_evals = [make_ai_eval("TEST.HE", recommendation="HOLD")]
        result = self.rebalancer.calculate_rebalance_plan(portfolio, ai_evals)
        assert result["trade_count"] == 0

    def test_generates_buy_trade_for_strong_buy_recommendation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)
        monkeypatch.setattr("config.MIN_TRADE_EUR", 200.0)

        portfolio = make_portfolio(
            total_equity=10000.0,
            cash_balance=2000.0,
            holdings={},
        )
        # Stock not yet held, AI says STRONG_BUY with 15% target weight
        ai_evals = [make_ai_eval("NOKIA.HE", score=9, recommendation="STRONG_BUY", target_weight=0.15, current_price=3.60)]

        result = self.rebalancer.calculate_rebalance_plan(portfolio, ai_evals)
        buy_trades = [t for t in result["proposed_trades"] if t["action"] == "BUY"]
        assert len(buy_trades) >= 1
        assert buy_trades[0]["symbol"] == "NOKIA.HE"

    def test_no_buy_for_hold_recommendation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)

        portfolio = make_portfolio(holdings={})
        ai_evals = [make_ai_eval("NOKIA.HE", recommendation="HOLD", target_weight=0.10, current_price=3.60)]
        result = self.rebalancer.calculate_rebalance_plan(portfolio, ai_evals)
        buy_trades = [t for t in result["proposed_trades"] if t["action"] == "BUY"]
        assert buy_trades == []

    def test_generates_sell_trade_for_strong_sell_recommendation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)
        monkeypatch.setattr("config.MIN_TRADE_EUR", 200.0)

        holdings = {
            "NESTE.HE": make_holding_entry(
                "NESTE.HE",
                market_value=2000.0,
                current_price=20.0,
                quantity=100,
                weight=0.20,
            )
        }
        portfolio = make_portfolio(total_equity=10000.0, holdings=holdings)
        ai_evals = [make_ai_eval("NESTE.HE", score=2, recommendation="STRONG_SELL", target_weight=0.0, current_price=20.0)]

        result = self.rebalancer.calculate_rebalance_plan(portfolio, ai_evals)
        sell_trades = [t for t in result["proposed_trades"] if t["action"] == "SELL"]
        assert len(sell_trades) >= 1

    def test_hodl_position_skipped_from_sell_proposals(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)

        holdings = {
            "NOKIA.HE": make_holding_entry(
                "NOKIA.HE",
                market_value=3000.0,
                current_price=3.60,
                quantity=833,
                weight=0.30,
                hodl=True,
            )
        }
        portfolio = make_portfolio(total_equity=10000.0, holdings=holdings)
        ai_evals = [make_ai_eval("NOKIA.HE", recommendation="STRONG_SELL", target_weight=0.0, current_price=3.60)]

        result = self.rebalancer.calculate_rebalance_plan(portfolio, ai_evals)
        sell_trades = [t for t in result["proposed_trades"] if t["action"] == "SELL" and t["symbol"] == "NOKIA.HE"]
        assert sell_trades == []

    def test_target_weight_capped_at_max_position_weight(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)
        monkeypatch.setattr("config.MIN_TRADE_EUR", 200.0)

        portfolio = make_portfolio(total_equity=10000.0, holdings={})
        # AI suggests 50% weight but cap should limit to 20%
        ai_evals = [make_ai_eval("MSFT", recommendation="STRONG_BUY", target_weight=0.50, current_price=300.0)]
        result = self.rebalancer.calculate_rebalance_plan(portfolio, ai_evals)
        buy_trades = [t for t in result["proposed_trades"] if t["action"] == "BUY"]
        if buy_trades:
            assert buy_trades[0]["target_weight"] <= 0.20

    def test_proposal_id_follows_naming_convention(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)

        result = self.rebalancer.calculate_rebalance_plan(make_portfolio(), [])
        assert result["proposal_id"].startswith("PROP-")

    def test_commission_is_non_negative(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.REBALANCE_PROPOSALS_FILE", tmp_path / "proposals.json")
        monkeypatch.setattr("config.TARGET_CASH_PERCENT", 0.05)
        monkeypatch.setattr("config.MAX_POSITION_WEIGHT", 0.20)
        monkeypatch.setattr("config.MIN_TRADE_EUR", 200.0)

        portfolio = make_portfolio(total_equity=10000.0, holdings={})
        ai_evals = [make_ai_eval("NOKIA.HE", recommendation="BUY", target_weight=0.10, current_price=3.60)]
        result = self.rebalancer.calculate_rebalance_plan(portfolio, ai_evals)
        assert result["total_estimated_commission"] >= 0.0
