import logging
from typing import Dict, List, Any
import config

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces safety guardrails, stop-loss triggers, position size caps, and human-in-the-loop validation."""

    def __init__(
        self,
        stop_loss_pct: float = config.STOP_LOSS_PERCENT,
        max_position_weight: float = config.MAX_POSITION_WEIGHT,
        min_trade_eur: float = config.MIN_TRADE_EUR,
    ):
        self.stop_loss_pct = stop_loss_pct
        self.max_position_weight = max_position_weight
        self.min_trade_eur = min_trade_eur

    def audit_portfolio_risks(self, portfolio_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Audits current portfolio for risk violations (e.g. Stop Loss breaches, overconcentration)."""
        holdings = portfolio_summary.get("holdings", {})
        risk_alerts = []

        for symbol, holding in holdings.items():
            unrealized_pnl_pct = holding.get("unrealized_pnl_pct", 0.0) / 100.0
            weight = holding.get("weight", 0.0)

            is_hodl = holding.get("hodl", False)
            note = holding.get("note", "HODL / Locked position")

            # 1. Stop Loss Audit
            if unrealized_pnl_pct <= -self.stop_loss_pct:
                if is_hodl:
                    risk_alerts.append({
                        "symbol": symbol,
                        "type": "HODL_LOCK_PROTECTION",
                        "severity": "INFO",
                        "message": f"Position dropped by {unrealized_pnl_pct*100:.1f}%, but SELL is bypassed due to HODL rule ('{note}').",
                        "recommended_action": "HOLD_LOCKED",
                    })
                else:
                    risk_alerts.append({
                        "symbol": symbol,
                        "type": "STOP_LOSS_BREACH",
                        "severity": "HIGH",
                        "message": f"Position dropped by {unrealized_pnl_pct*100:.1f}%, exceeding stop-loss threshold ({self.stop_loss_pct*100:.0f}%).",
                        "recommended_action": "SELL_ALL",
                    })

            # 2. Overconcentration Audit
            if weight > self.max_position_weight:
                risk_alerts.append({
                    "symbol": symbol,
                    "type": "OVERCONCENTRATION",
                    "severity": "MEDIUM",
                    "message": f"Position weight ({weight*100:.1f}%) exceeds max single stock limit ({self.max_position_weight*100:.0f}%).",
                    "recommended_action": "REDUCE_POSITION",
                })

        return risk_alerts

    def validate_proposed_trades(self, proposed_trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validates proposed trades against fee thresholds and safety guardrails."""
        validated_trades = []

        for trade in proposed_trades:
            trade_val = trade.get("trade_value", 0.0)
            commission = trade.get("estimated_commission", 0.0)

            # Filter out trades smaller than MIN_TRADE_EUR
            if trade_val < self.min_trade_eur:
                logger.warning(f"Trade for {trade['symbol']} rejected by RiskManager: Value {trade_val:.2f} EUR below MIN_TRADE_EUR ({self.min_trade_eur} EUR)")
                continue

            # Filter out trades where commission eats more than 2.5% of trade value
            if trade_val > 0 and (commission / trade_val) > 0.025:
                logger.warning(f"Trade for {trade['symbol']} rejected by RiskManager: High commission ratio ({commission/trade_val*100:.2f}%)")
                continue

            validated_trades.append(trade)

        return validated_trades
