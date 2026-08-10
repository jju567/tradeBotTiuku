import logging
import time
from typing import Dict, List, Any
import config

logger = logging.getLogger(__name__)


class RiskManager:
    """Enforces safety guardrails, stop-loss / take-profit triggers, position size caps, and human-in-the-loop validation."""

    def __init__(
        self,
        stop_loss_pct: float = config.STOP_LOSS_PERCENT,
        take_profit_pct: float = config.TAKE_PROFIT_PERCENT,
        volatility_threshold: float = config.MARKET_VOLATILITY_THRESHOLD,
        max_position_weight: float = config.MAX_POSITION_WEIGHT,
        min_trade_eur: float = config.MIN_TRADE_EUR,
    ):
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.volatility_threshold = volatility_threshold
        self.max_position_weight = max_position_weight
        self.min_trade_eur = min_trade_eur

    def audit_portfolio_risks(self, portfolio_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Audits current portfolio for risk violations (e.g. Stop Loss breaches, overconcentration)."""
        holdings = portfolio_summary.get("holdings", {})
        global_strat = portfolio_summary.get("global_strategy") or config.INVESTMENT_STRATEGY
        risk_alerts = []

        for symbol, holding in holdings.items():
            name = holding.get("name") or symbol
            display_name = f"{name} ({symbol})" if name and name != symbol else symbol

            unrealized_pnl_pct = holding.get("unrealized_pnl_pct", 0.0) / 100.0
            weight = holding.get("weight", 0.0)

            is_hodl = holding.get("hodl", False)
            note = holding.get("note", "HODL / Locked position")
            if holding.get("strategy") or holding.get("hodl") or portfolio_summary.get("global_strategy"):
                strat_params = config.get_holding_strategy_params(holding, global_strat)
                eff_sl = strat_params.get("stop_loss_pct", self.stop_loss_pct)
                eff_tp = strat_params.get("take_profit_pct", self.take_profit_pct)
            else:
                eff_sl = self.stop_loss_pct
                eff_tp = self.take_profit_pct

            # 1. Stop Loss Audit
            if unrealized_pnl_pct <= -eff_sl:
                if is_hodl:
                    risk_alerts.append({
                        "symbol": symbol,
                        "name": name,
                        "display_name": display_name,
                        "type": "HODL_LOCK_PROTECTION",
                        "severity": "INFO",
                        "message": f"{display_name} dropped by {unrealized_pnl_pct*100:.1f}%, but SELL is bypassed due to HODL rule ('{note}').",
                        "recommended_action": "HOLD_LOCKED",
                    })
                else:
                    risk_alerts.append({
                        "symbol": symbol,
                        "name": name,
                        "display_name": display_name,
                        "type": "STOP_LOSS_BREACH",
                        "severity": "HIGH",
                        "message": f"{display_name} dropped by {unrealized_pnl_pct*100:.1f}%, exceeding stop-loss threshold ({eff_sl*100:.0f}%).",
                        "recommended_action": "SELL_ALL",
                    })

            # 2. Take Profit Audit
            elif unrealized_pnl_pct >= eff_tp:
                risk_alerts.append({
                    "symbol": symbol,
                    "name": name,
                    "display_name": display_name,
                    "type": "TAKE_PROFIT_TARGET",
                    "severity": "MEDIUM",
                    "message": f"{display_name} gained {unrealized_pnl_pct*100:.1f}%, reaching take-profit target ({eff_tp*100:.0f}%).",
                    "recommended_action": "TAKE_PROFIT_TRIM",
                })

            # 3. Overconcentration Audit
            if weight > self.max_position_weight:
                risk_alerts.append({
                    "symbol": symbol,
                    "name": name,
                    "display_name": display_name,
                    "type": "OVERCONCENTRATION",
                    "severity": "MEDIUM",
                    "message": f"{display_name} weight ({weight*100:.1f}%) exceeds max single stock limit ({self.max_position_weight*100:.0f}%).",
                    "recommended_action": "REDUCE_POSITION",
                })

        return risk_alerts

    def check_sltp_triggers(
        self,
        portfolio_summary: Dict[str, Any],
        market_data: List[Dict[str, Any]],
        cooldown_tracker: Dict[str, float] = None,
        cooldown_hours: float = config.ALERT_COOLDOWN_HOURS,
        now_ts: float = None,
    ) -> List[Dict[str, Any]]:
        """
        Pure Python / Zero-Token check for Stop-Loss, Take-Profit, and market volatility triggers.
        Returns active triggers that require waking up AI analysis or dispatching urgent alerts.
        """
        if cooldown_tracker is None:
            cooldown_tracker = {}
        if now_ts is None:
            now_ts = time.time()

        cooldown_sec = cooldown_hours * 3600.0
        holdings = portfolio_summary.get("holdings", {})
        global_strat = portfolio_summary.get("global_strategy") or config.INVESTMENT_STRATEGY
        market_dict = {item["symbol"]: item for item in market_data if "symbol" in item}

        active_triggers = []

        # 1. Check portfolio holdings for Stop-Loss and Take-Profit breaches
        for symbol, holding in holdings.items():
            is_hodl = holding.get("hodl", False)
            if is_hodl:
                # Bypass HODL / locked positions from stop-loss / take-profit alerts
                continue

            unrealized_pnl_pct = holding.get("unrealized_pnl_pct", 0.0) / 100.0
            name = holding.get("name") or symbol
            display_name = f"{name} ({symbol})" if name and name != symbol else symbol

            current_pnl_pct = unrealized_pnl_pct * 100.0
            if holding.get("strategy") or holding.get("hodl") or portfolio_summary.get("global_strategy"):
                strat_params = config.get_holding_strategy_params(holding, global_strat)
                eff_sl = strat_params.get("stop_loss_pct", self.stop_loss_pct)
                eff_tp = strat_params.get("take_profit_pct", self.take_profit_pct)
            else:
                eff_sl = self.stop_loss_pct
                eff_tp = self.take_profit_pct

            # Check Stop-Loss
            if unrealized_pnl_pct <= -eff_sl:
                trigger_key = f"{symbol}:STOP_LOSS_BREACH"
                entry = cooldown_tracker.get(trigger_key, 0.0)
                if isinstance(entry, dict):
                    last_alert_time = float(entry.get("timestamp", 0.0))
                    last_pnl = entry.get("pnl_pct")
                else:
                    last_alert_time = float(entry) if entry else 0.0
                    last_pnl = None
                time_passed = (now_ts - last_alert_time) >= cooldown_sec
                pnl_moved = last_pnl is None or abs(current_pnl_pct - float(last_pnl)) >= 5.0

                if time_passed and pnl_moved:
                    active_triggers.append({
                        "symbol": symbol,
                        "name": name,
                        "display_name": display_name,
                        "type": "STOP_LOSS_BREACH",
                        "severity": "HIGH",
                        "pnl_pct": current_pnl_pct,
                        "threshold_pct": -eff_sl * 100.0,
                        "is_hodl": False,
                        "message": f"{display_name} dropped {current_pnl_pct:.2f}% (Stop-Loss limit: {-eff_sl*100:.1f}%)",
                        "trigger_key": trigger_key,
                        "requires_ai_wakeup": True,
                    })

            # Check Take-Profit
            elif unrealized_pnl_pct >= eff_tp:
                trigger_key = f"{symbol}:TAKE_PROFIT_TARGET"
                entry = cooldown_tracker.get(trigger_key, 0.0)
                if isinstance(entry, dict):
                    last_alert_time = float(entry.get("timestamp", 0.0))
                    last_pnl = entry.get("pnl_pct")
                else:
                    last_alert_time = float(entry) if entry else 0.0
                    last_pnl = None
                time_passed = (now_ts - last_alert_time) >= cooldown_sec
                pnl_moved = last_pnl is None or abs(current_pnl_pct - float(last_pnl)) >= 5.0

                if time_passed and pnl_moved:
                    active_triggers.append({
                        "symbol": symbol,
                        "name": name,
                        "display_name": display_name,
                        "type": "TAKE_PROFIT_TARGET",
                        "severity": "HIGH",
                        "pnl_pct": current_pnl_pct,
                        "threshold_pct": eff_tp * 100.0,
                        "is_hodl": False,
                        "message": f"{display_name} gained +{current_pnl_pct:.2f}% (Take-Profit target: +{eff_tp*100:.1f}%)",
                        "trigger_key": trigger_key,
                        "requires_ai_wakeup": True,
                    })

        # 2. Check intraday market volatility (spikes / crashes) for tracked symbols
        for symbol, m_item in market_dict.items():
            if symbol in holdings and holdings[symbol].get("hodl", False):
                # Skip HODL positions from market volatility alerts
                continue

            change_pct = m_item.get("change_pct", 0.0) / 100.0 if "change_pct" in m_item else 0.0
            if abs(change_pct) >= self.volatility_threshold:
                trigger_key = f"{symbol}:VOLATILITY_SWING"
                last_alert_time = cooldown_tracker.get(trigger_key, 0.0)
                if (now_ts - last_alert_time) >= cooldown_sec:
                    name = holdings.get(symbol, {}).get("name") or m_item.get("name") or symbol
                    display_name = f"{name} ({symbol})" if name and name != symbol else symbol
                    direction = "jumped" if change_pct > 0 else "dropped"
                    active_triggers.append({
                        "symbol": symbol,
                        "name": name,
                        "display_name": display_name,
                        "type": "VOLATILITY_SWING",
                        "severity": "MEDIUM",
                        "change_pct": change_pct * 100.0,
                        "threshold_pct": self.volatility_threshold * 100.0,
                        "message": f"{display_name} {direction} {change_pct*100:+.2f}% today (Volatility threshold: {self.volatility_threshold*100:.1f}%)",
                        "trigger_key": trigger_key,
                        "requires_ai_wakeup": symbol in holdings,
                    })

        return active_triggers

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

