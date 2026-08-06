import argparse
import logging
import sys
from pathlib import Path
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(config.APP_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("tradeBotTiuku")

from scheduler.job_runner import WeeklyJobRunner
from core.portfolio import PortfolioManager


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="tradeBotTiuku - Open Source Portfolio Advisor & Advisory Agent")
    parser.add_argument("--run-once", action="store_true", help="Run a single Tiuku analysis cycle and generate report")
    parser.add_argument("--schedule", action="store_true", help="Start background weekly analysis schedule")
    parser.add_argument("--show-portfolio", action="store_true", help="Display current local portfolio holdings")
    parser.add_argument("--set-cash", type=float, metavar="EUR", help="Set current cash balance in EUR")
    parser.add_argument("--set-holding", nargs=3, metavar=("SYMBOL", "QTY", "AVG_PRICE"), help="Add or update a stock holding (e.g. NESTE.HE 100 25.40)")
    parser.add_argument("--import-csv", type=str, metavar="FILEPATH", help="Import portfolio holdings from a Nordnet CSV/tab export file")
    parser.add_argument("--set-equity", type=float, metavar="EUR", help="Set known total equity from Nordnet (overrides yfinance-computed total for weights)")

    args = parser.parse_args()

    portfolio_mgr = PortfolioManager()

    if args.import_csv:
        filepath = Path(args.import_csv)
        try:
            holdings, count = portfolio_mgr.import_from_csv(filepath)
            print(f"✅ Tuotiin onnistuneesti {count} omistusta tiedostosta {filepath.name} tiedostoon tiuku_portfolio.json")
        except Exception as e:
            print(f"❌ Virhe tuotaessa CSV-tiedostoa: {e}")
        return

    if args.set_equity is not None:
        portfolio_mgr.set_equity_override(args.set_equity)
        print(f"✅ Total equity override set to {args.set_equity:,.2f} EUR in tiuku_portfolio.json")
        print("   (Paino- ja tasapainoituslaskelmat käyttävät tätä arvoa yfinancen sijaan)")
        return

    if args.set_cash is not None:
        portfolio_mgr.set_cash_balance(args.set_cash)
        print(f"✅ Cash balance updated to {args.set_cash:.2f} EUR in tiuku_portfolio.json")
        return

    if args.set_holding is not None:
        sym, qty_str, price_str = args.set_holding
        try:
            qty = int(qty_str)
            price = float(price_str)
            portfolio_mgr.set_holding(sym, qty, price)
            print(f"✅ Holding {sym.upper()} updated: {qty} pcs @ {price:.2f} EUR")
        except ValueError as e:
            print(f"❌ Invalid argument format for --set-holding: {e}")
        return

    if args.show_portfolio:
        state = portfolio_mgr.load_state()
        print("\n=== tradeBotTiuku Local Portfolio ===")
        print(f"Cash Balance: {state.get('cash_balance', 0.0):.2f} {state.get('currency', 'EUR')}")
        print("\nHoldings:")
        for sym, h in state.get("holdings", {}).items():
            print(f"  - {sym}: {h.get('quantity')} pcs @ {h.get('avg_price'):.2f} EUR (Target weight: {h.get('target_weight', 0.10)*100:.1f}%)")
        print("===================================\n")
        return

    runner = WeeklyJobRunner()

    if args.schedule:
        runner.start_scheduler()
    else:
        # Default action: run once
        logger.info("Running single tradeBotTiuku analysis & advisory cycle...")
        result = runner.run_analysis_cycle()
        print("\n==================================================")
        print("tradeBotTiuku Analysis & Advisory Report Complete")
        print("==================================================")
        print(f"Report File: {result['report_path']}")
        print(f"Dashboard File: {config.BASE_DIR / 'tiuku_dashboard.html'}")
        print(f"Total Equity: {result['portfolio'].get('total_equity'):,.2f} EUR")
        print(f"Proposed Trades: {result['proposal'].get('trade_count', 0)}")
        print(f"Est. Commissions: {result['proposal'].get('total_estimated_commission', 0.0):.2f} EUR")
        print("==================================================\n")


if __name__ == "__main__":
    main()
