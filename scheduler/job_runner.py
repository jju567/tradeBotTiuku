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

from clients.email_client import EmailClient

from scheduler.market_monitor import MarketMonitor

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
        self.email_client = EmailClient()
        self.market_monitor = MarketMonitor()

    def run_analysis_cycle(self) -> Dict[str, Any]:
        """Runs full portfolio sync, AI evaluation, rebalancing plan, risk audit, report generation, and email dispatch."""
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

        # 3b. Perform ETF dip scoring for tracked ETFs
        etf_evaluations = []
        etf_items = self.nordnet_client.market_client.get_etf_items()
        market_dict = {m["symbol"]: m for m in market_data}
        for etf_meta in etf_items:
            sym = etf_meta.get("symbol")
            m_item = market_dict.get(sym)
            if m_item:
                score_res = self.nordnet_client.market_client.calculate_etf_dip_score(m_item, etf_meta)
                score_res["rsi_14"] = m_item.get("rsi_14", 50.0)
                score_res["sma_50"] = m_item.get("sma_50", 0.0)
                score_res["current_price"] = m_item.get("current_price", 0.0)
                etf_evaluations.append(score_res)

        # 4. Audit portfolio risks (Stop Loss, max weight)
        risk_alerts = self.risk_mgr.audit_portfolio_risks(portfolio_summary)

        # 4b. Perform overall high-level portfolio AI strategic assessment
        overall_ai_summary = self.ai_advisor.generate_overall_portfolio_analysis(portfolio_summary, ai_evaluations, risk_alerts)

        # 5. Calculate proposed rebalancing plan
        raw_proposal = self.rebalancer.calculate_rebalance_plan(portfolio_summary, ai_evaluations)

        # 6. Filter & validate proposed trades against risk guardrails
        validated_trades = self.risk_mgr.validate_proposed_trades(raw_proposal.get("proposed_trades", []))
        raw_proposal["proposed_trades"] = validated_trades
        raw_proposal["trade_count"] = len(validated_trades)

        # 7. Generate weekly markdown report & HTML Dashboard
        report_path = self.reporter.generate_report(
            portfolio_summary,
            ai_evaluations,
            raw_proposal,
            risk_alerts,
            overall_ai_summary,
            etf_evaluations=etf_evaluations
        )

        # 8. Send report via email if enabled or configured
        if config.ENABLE_EMAIL_REPORTS or self.email_client.is_configured():
            dashboard_path = config.BASE_DIR / "tiuku_dashboard.html"
            self.email_client.send_report_email(
                portfolio_summary=portfolio_summary,
                proposal=raw_proposal,
                risk_alerts=risk_alerts,
                overall_ai_summary=overall_ai_summary,
                dashboard_path=dashboard_path,
                report_md_path=report_path,
            )

        logger.info(f"Analysis cycle complete. Report saved to: {report_path}")
        return {
            "portfolio": portfolio_summary,
            "ai_evaluations": ai_evaluations,
            "overall_ai_summary": overall_ai_summary,
            "proposal": raw_proposal,
            "risk_alerts": risk_alerts,
            "report_path": str(report_path),
        }

    def start_scheduler(self):
        """Starts background weekly schedule loop and zero-token market monitor."""
        logger.info(f"Scheduling weekly job runner every {config.WEEKLY_REPORT_DAY.capitalize()}...")
        
        # Schedule weekly on configured day at 08:00 AM
        getattr(schedule.every(), config.WEEKLY_REPORT_DAY).at("08:00").do(self.run_analysis_cycle)

        # Fallback interval timer
        schedule.every(config.REBALANCE_INTERVAL_DAYS).days.do(self.run_analysis_cycle)

        # Schedule zero-token market monitor
        if config.ENABLE_MARKET_MONITOR:
            logger.info(f"Scheduling zero-token Market Monitor every {config.MONITOR_INTERVAL_MINUTES} minutes...")
            schedule.every(config.MONITOR_INTERVAL_MINUTES).minutes.do(self.market_monitor.check_market)

        logger.info("Scheduler started. Running loop...")
        while True:
            schedule.run_pending()
            time.sleep(10)

