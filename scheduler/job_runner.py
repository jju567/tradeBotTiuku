import logging
import time
from typing import Dict, Any
import schedule
import config
from clients.nordnet_client import NordnetClient
from core.portfolio import PortfolioManager
from core.ai_advisor import StockAdvisorAI
from core.rebalancer import PortfolioRebalancer
from core.risk_manager import RiskManager
from reporting.weekly_reporter import WeeklyReporter

logger = logging.getLogger(__name__)


class WeeklyJobRunner:
    """Orchestrates the weekly NordnetBot portfolio analysis, risk audit, and rebalance proposal workflow."""

    def __init__(self):
        self.nordnet_client = NordnetClient()
        self.portfolio_mgr = PortfolioManager()
        self.ai_advisor = StockAdvisorAI()
        self.rebalancer = PortfolioRebalancer()
        self.risk_mgr = RiskManager()
        self.reporter = WeeklyReporter()
    def run_analysis_cycle(self) -> Dict[str, Any]:
        """Runs full portfolio sync, AI evaluation, rebalancing plan, risk audit, and report generation."""
        logger.info("=== Starting Weekly NordnetBot Portfolio Analysis Cycle ===")

        # 1. Sync current portfolio state from tiuku_portfolio.json & yfinance
        tiuku_state = self.portfolio_mgr.load_state()
        nordnet_holdings = self.nordnet_client.get_portfolio_holdings(tiuku_state)
        portfolio_summary = self.portfolio_mgr.sync_valuation(nordnet_holdings)

        # 2. Fetch market data & technical indicators for all portfolio & watchlist symbols
        portfolio_symbols = list(portfolio_summary.get("holdings", {}).keys())
        all_symbols = list(dict.fromkeys(portfolio_symbols + self.nordnet_client.market_client.get_default_symbols()))
        market_data = self.nordnet_client.get_market_data(all_symbols)

        # 3. Perform AI equity evaluation & scoring (1-10)
        ai_evaluations = self.ai_advisor.evaluate_equities(market_data, portfolio_summary)

        # 4. Audit portfolio risks (Stop Loss, max weight)
        risk_alerts = self.risk_mgr.audit_portfolio_risks(portfolio_summary)

        # 5. Calculate proposed rebalancing plan
        raw_proposal = self.rebalancer.calculate_rebalance_plan(portfolio_summary, ai_evaluations)

        # 6. Filter & validate proposed trades against risk guardrails
        validated_trades = self.risk_mgr.validate_proposed_trades(raw_proposal.get("proposed_trades", []))
        raw_proposal["proposed_trades"] = validated_trades
        raw_proposal["trade_count"] = len(validated_trades)

        # 7. Generate weekly markdown report
        report_path = self.reporter.generate_report(portfolio_summary, ai_evaluations, raw_proposal, risk_alerts)

        logger.info(f"Analysis cycle complete. Report saved to: {report_path}")
        return {
            "portfolio": portfolio_summary,
            "ai_evaluations": ai_evaluations,
            "proposal": raw_proposal,
            "risk_alerts": risk_alerts,
            "report_path": str(report_path),
        }

    def start_scheduler(self):
        """Starts background weekly schedule loop."""
        logger.info(f"Scheduling weekly job runner every {config.WEEKLY_REPORT_DAY.capitalize()}...")
        
        # Schedule weekly on configured day at 08:00 AM
        getattr(schedule.every(), config.WEEKLY_REPORT_DAY).at("08:00").do(self.run_analysis_cycle)

        # Also fallback interval timer
        schedule.every(config.REBALANCE_INTERVAL_DAYS).days.do(self.run_analysis_cycle)

        logger.info("Scheduler started. Running loop...")
        while True:
            schedule.run_pending()
            time.sleep(60)
