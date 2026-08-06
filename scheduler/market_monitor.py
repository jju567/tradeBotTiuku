import argparse
import json
import logging
import time
from typing import Dict, List, Any
import schedule
import config
from clients.nordnet_client import NordnetClient
from core.portfolio import PortfolioManager
from core.ai_advisor import StockAdvisorAI
from core.risk_manager import RiskManager
from clients.email_client import EmailClient

logger = logging.getLogger(__name__)


class MarketMonitor:
    """
    Lightweight zero-token background process that monitors portfolio holdings
    for Stop-Loss breaches, Take-Profit targets, and intraday market swings.

    Token economy:
    - Normal checks: 0 tokens consumed (pure Python + free Yahoo Finance quotes).
    - Event-driven: Only calls LLM (AI Advisor) when triggers breach configured safety bounds.
    """

    def __init__(self):
        self.nordnet_client = NordnetClient()
        self.portfolio_mgr = PortfolioManager()
        self.ai_advisor = StockAdvisorAI()
        self.risk_mgr = RiskManager()
        self.email_client = EmailClient()
        self.cooldown_file = config.MONITOR_COOLDOWN_FILE

    def load_cooldowns(self) -> Dict[str, float]:
        """Loads alert cooldown timestamps from disk."""
        if self.cooldown_file.exists():
            try:
                with open(self.cooldown_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load monitor cooldown file: {e}")
        return {}

    def save_cooldowns(self, cooldowns: Dict[str, float]) -> None:
        """Saves alert cooldown timestamps to disk."""
        try:
            with open(self.cooldown_file, "w", encoding="utf-8") as f:
                json.dump(cooldowns, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save monitor cooldown file: {e}")

    def check_market(self) -> Dict[str, Any]:
        """
        Executes a zero-token market check cycle.
        If triggers fire, wakes up AI evaluation and dispatches urgent email alerts.
        """
        logger.info("--- Running Zero-Token Market Monitor Check ---")

        # 1. Sync current portfolio valuation
        tiuku_state = self.portfolio_mgr.load_state()
        nordnet_holdings = self.nordnet_client.get_portfolio_holdings(tiuku_state)
        portfolio_summary = self.portfolio_mgr.sync_valuation(nordnet_holdings)

        # 2. Fetch current market data (Zero-Token yfinance quotes)
        portfolio_symbols = list(portfolio_summary.get("holdings", {}).keys())
        all_symbols = list(dict.fromkeys(portfolio_symbols + self.nordnet_client.market_client.get_default_symbols()))
        market_data = self.nordnet_client.get_market_data(all_symbols)

        # 3. Check for SL / TP / Volatility triggers
        cooldowns = self.load_cooldowns()
        now_ts = time.time()

        active_triggers = self.risk_mgr.check_sltp_triggers(
            portfolio_summary=portfolio_summary,
            market_data=market_data,
            cooldown_tracker=cooldowns,
            cooldown_hours=config.ALERT_COOLDOWN_HOURS,
            now_ts=now_ts,
        )

        if not active_triggers:
            logger.info("✅ Market check clear: 0 triggers activated (0 LLM tokens consumed).")
            return {
                "status": "OK",
                "triggers_found": 0,
                "triggers": [],
                "ai_wakeup": False,
                "tokens_used": 0,
            }

        logger.warning(f"🚨 Market monitor detected {len(active_triggers)} active trigger(s)!")
        for trg in active_triggers:
            logger.warning(f"   -> [{trg['type']}] {trg['message']}")

        # 4. Filter triggers requiring AI wake-up
        ai_wakeup_required = any(trg.get("requires_ai_wakeup", False) for trg in active_triggers)
        ai_evaluations = None

        if ai_wakeup_required:
            logger.info("🧠 Waking up StockAdvisorAI for event-driven trigger evaluation...")
            # Filter market_data to symbols with active triggers to conserve tokens
            triggered_symbols = set(trg["symbol"] for trg in active_triggers)
            target_market_data = [m for m in market_data if m.get("symbol") in triggered_symbols]
            
            try:
                ai_evaluations = self.ai_advisor.evaluate_equities(target_market_data, portfolio_summary)
            except Exception as e:
                logger.error(f"Failed during AI wake-up evaluation: {e}")

        # 5. Send urgent email notification
        if config.ENABLE_EMAIL_REPORTS or self.email_client.is_configured():
            self.email_client.send_urgent_alert_email(
                triggers=active_triggers,
                portfolio_summary=portfolio_summary,
                ai_evaluations=ai_evaluations,
            )

        # 6. Update cooldown tracker for fired triggers
        for trg in active_triggers:
            trg_key = trg.get("trigger_key")
            if trg_key:
                cooldowns[trg_key] = now_ts
        self.save_cooldowns(cooldowns)

        return {
            "status": "ALERT_TRIGGERED",
            "triggers_found": len(active_triggers),
            "triggers": active_triggers,
            "ai_wakeup": ai_wakeup_required,
            "ai_evaluations": ai_evaluations,
        }

    def start_loop(self, interval_minutes: int = config.MONITOR_INTERVAL_MINUTES):
        """Starts background interval monitoring loop."""
        logger.info(f"Starting Market Monitor loop every {interval_minutes} minutes...")
        schedule.every(interval_minutes).minutes.do(self.check_market)

        # Run immediately on launch
        self.check_market()

        while True:
            schedule.run_pending()
            time.sleep(10)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="tradeBotTiuku Zero-Token Market Monitor")
    parser.add_argument("--once", action="store_true", help="Run a single market check and exit")
    parser.add_argument("--interval", type=int, default=config.MONITOR_INTERVAL_MINUTES, help="Monitoring interval in minutes")
    args = parser.parse_args()

    monitor = MarketMonitor()
    if args.once:
        result = monitor.check_market()
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        monitor.start_loop(interval_minutes=args.interval)
