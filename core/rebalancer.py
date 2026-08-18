import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any
import config

logger = logging.getLogger(__name__)


class PortfolioRebalancer:
    """Calculates portfolio rebalancing proposals based on target weights and fee constraints."""

    def __init__(self, min_trade_eur: float = config.MIN_TRADE_EUR):
        self.min_trade_eur = min_trade_eur

    def calculate_rebalance_plan(
        self,
        portfolio_summary: Dict[str, Any],
        ai_evaluations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculates exact buy/sell trade proposals to rebalance the portfolio towards target weights."""
        total_equity = portfolio_summary.get("total_equity", 0.0)
        current_holdings = portfolio_summary.get("holdings", {})
        cash_balance = portfolio_summary.get("cash_balance", 0.0)

        # Build lookup for AI recommendations
        ai_dict = {item["symbol"]: item for item in ai_evaluations}

        proposed_trades = []
        total_estimated_commission = 0.0

        # Target allocation setup
        target_cash = total_equity * config.TARGET_CASH_PERCENT
        investable_equity = total_equity - target_cash

        # Evaluate existing holdings for SELL / DOWNWEIGHT recommendations
        for symbol, holding in current_holdings.items():
            if holding.get("hodl", False):
                logger.info(f"Skipping sell proposal for {symbol}: Position is locked under user HODL rule ('{holding.get('note')}')")
                continue

            curr_val = holding["market_value"]
            curr_price = holding["current_price"]
            curr_qty = holding["quantity"]
            curr_weight = holding.get("weight", 0.0)
            ai_eval = ai_dict.get(symbol, {})
            score = ai_eval.get("score", 5)
            rec = ai_eval.get("recommendation", "HOLD")
            target_weight = min(ai_eval.get("target_weight", 0.0), config.MAX_POSITION_WEIGHT)
            min_symbol_trade_eur = config.get_min_trade_size_for_symbol(symbol)

            target_val = investable_equity * target_weight
            diff_val = target_val - curr_val

            global_strat = portfolio_summary.get("global_strategy") or config.INVESTMENT_STRATEGY
            strat = config.get_holding_strategy(holding, global_strat)

            # Generate SELL if AI recommends SELL/STRONG_SELL or position exceeds MAX_POSITION_WEIGHT or overweight trim
            if (rec in ["SELL", "STRONG_SELL"] or curr_weight > config.MAX_POSITION_WEIGHT or (diff_val < 0 and rec != "HOLD")) and abs(diff_val) >= min_symbol_trade_eur:
                sell_val = abs(diff_val)
                sell_qty = int(sell_val / curr_price)
                if sell_qty > 0:
                    trade_val = round(sell_qty * curr_price, 2)
                    commission = config.calculate_commission(symbol, trade_val)
                    total_estimated_commission += commission
                    proposed_trades.append({
                        "symbol": symbol,
                        "name": holding.get("name", ai_eval.get("name", symbol)),
                        "action": "SELL",
                        "quantity": sell_qty,
                        "price": curr_price,
                        "trade_value": trade_val,
                        "estimated_commission": commission,
                        "current_weight": curr_weight,
                        "target_weight": target_weight,
                        "ai_score": score,
                        "strategy": strat,
                        "reason": ai_eval.get("reasoning", "Rebalancing trim"),
                    })

        # Calculate net cash available for BUY trades (cash_balance + cash from SELLs - target cash reserve)
        cash_gained_from_sells = sum(t["trade_value"] - t["estimated_commission"] for t in proposed_trades if t["action"] == "SELL")
        total_available_cash = cash_balance + cash_gained_from_sells
        available_buy_cash = max(0.0, total_available_cash - target_cash)

        # Collect BUY / UPWEIGHT candidates (ONLY if AI score indicates BUY or STRONG_BUY)
        buy_candidates = []
        for ai_eval in ai_evaluations:
            symbol = ai_eval["symbol"]
            curr_price = ai_eval["current_price"]
            rec = ai_eval.get("recommendation", "HOLD")
            score = ai_eval.get("score", 5)

            # Strict conviction check: Only BUY if AI explicitly recommends BUY or STRONG_BUY
            if rec not in ["BUY", "STRONG_BUY"]:
                continue

            target_weight = min(ai_eval.get("target_weight", 0.0), config.MAX_POSITION_WEIGHT)
            min_symbol_trade_eur = config.get_min_trade_size_for_symbol(symbol)

            holding = current_holdings.get(symbol, {"market_value": 0.0, "quantity": 0, "weight": 0.0})
            curr_val = holding["market_value"]

            target_val = investable_equity * target_weight
            diff_val = target_val - curr_val

            if diff_val > 0 and diff_val >= min_symbol_trade_eur:
                buy_candidates.append({
                    "ai_eval": ai_eval,
                    "symbol": symbol,
                    "curr_price": curr_price,
                    "rec": rec,
                    "score": score,
                    "target_weight": target_weight,
                    "min_symbol_trade_eur": min_symbol_trade_eur,
                    "holding": holding,
                    "desired_val": diff_val,
                })

        # Prioritize BUY candidates by AI score (desc), conviction level (STRONG_BUY > BUY), and desired value (desc)
        rec_priority = {"STRONG_BUY": 2, "BUY": 1}
        buy_candidates.sort(
            key=lambda c: (c["score"], rec_priority.get(c["rec"], 0), c["desired_val"]),
            reverse=True
        )

        remaining_cash = available_buy_cash

        for cand in buy_candidates:
            if remaining_cash < cand["min_symbol_trade_eur"]:
                logger.info(
                    f"Skipping BUY proposal for {cand['symbol']}: Remaining available cash ({remaining_cash:.2f} EUR) "
                    f"is less than minimum trade size ({cand['min_symbol_trade_eur']:.2f} EUR)"
                )
                continue

            symbol = cand["symbol"]
            curr_price = cand["curr_price"]
            desired_val = cand["desired_val"]
            ai_eval = cand["ai_eval"]
            holding = cand["holding"]

            # Cap max spend to available remaining cash budget
            max_spend = min(desired_val, remaining_cash)
            buy_qty = int(max_spend / curr_price)

            # Ensure trade_value + estimated commission fits within remaining_cash
            while buy_qty > 0:
                trade_val = round(buy_qty * curr_price, 2)
                commission = config.calculate_commission(symbol, trade_val)
                if (trade_val + commission) <= remaining_cash:
                    break
                buy_qty -= 1

            if buy_qty > 0:
                trade_val = round(buy_qty * curr_price, 2)
                if trade_val >= cand["min_symbol_trade_eur"]:
                    commission = config.calculate_commission(symbol, trade_val)
                    total_estimated_commission += commission
                    remaining_cash -= (trade_val + commission)

                    proposed_trades.append({
                        "symbol": symbol,
                        "name": ai_eval.get("name", holding.get("name", symbol)),
                        "action": "BUY",
                        "quantity": buy_qty,
                        "price": curr_price,
                        "trade_value": trade_val,
                        "estimated_commission": commission,
                        "current_weight": holding.get("weight", 0.0),
                        "target_weight": cand["target_weight"],
                        "ai_score": cand["score"],
                        "reason": ai_eval.get("reasoning", "Rebalancing buy"),
                    })

        total_proposed_buy_val = sum(t["trade_value"] for t in proposed_trades if t["action"] == "BUY")
        total_proposed_sell_val = sum(t["trade_value"] for t in proposed_trades if t["action"] == "SELL")

        proposal = {
            "proposal_id": f"PROP-{datetime.now().strftime('%Y%m%d-%H%M')}",
            "created_at": datetime.now().isoformat(),
            "status": "PENDING_HUMAN_APPROVAL",
            "total_equity": total_equity,
            "cash_balance": cash_balance,
            "available_buy_cash": round(available_buy_cash, 2),
            "remaining_cash_after_proposal": round(remaining_cash, 2),
            "total_proposed_buy_val": round(total_proposed_buy_val, 2),
            "total_proposed_sell_val": round(total_proposed_sell_val, 2),
            "proposed_trades": proposed_trades,
            "trade_count": len(proposed_trades),
            "total_estimated_commission": round(total_estimated_commission, 2),
        }

        self.save_proposal(proposal)
        return proposal

    def save_proposal(self, proposal: Dict[str, Any], filepath: Path = config.REBALANCE_PROPOSALS_FILE):
        """Saves proposal to file."""
        proposals = []
        if filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    proposals = json.load(f)
            except Exception:
                proposals = []

        proposals.append(proposal)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(proposals, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved rebalance proposal {proposal['proposal_id']} to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save proposal: {e}")
