import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Tuple
import config

logger = logging.getLogger(__name__)


class PortfolioManager:
    """Manages Tiuku's local portfolio tracking file (tiuku_portfolio.json) and history snapshots."""

    def __init__(self, portfolio_file: Path = config.TIUKU_PORTFOLIO_FILE, history_file: Path = config.PORTFOLIO_HISTORY_FILE):
        self.portfolio_file = portfolio_file
        self.history_file = history_file
        self.portfolio_state: Dict[str, Any] = {}
        self.load_state()

    def load_state(self) -> Dict[str, Any]:
        """Loads portfolio watchlist state from local tiuku_portfolio.json."""
        if self.portfolio_file.exists():
            try:
                with open(self.portfolio_file, "r", encoding="utf-8") as f:
                    self.portfolio_state = json.load(f)
                logger.info(f"Loaded Tiuku portfolio from {self.portfolio_file}")
            except Exception as e:
                logger.error(f"Error reading {self.portfolio_file}: {e}")
                self._init_default_portfolio()
        else:
            self._init_default_portfolio()

        return self.portfolio_state

    def _init_default_portfolio(self):
        """Creates a default portfolio if file doesn't exist."""
        self.portfolio_state = {
            "currency": config.CURRENCY,
            "cash_balance": 2500.0,
            "holdings": {
                "NESTE.HE": {"quantity": 100, "avg_price": 28.50, "target_weight": 0.10},
                "KNEBV.HE": {"quantity": 50, "avg_price": 44.00, "target_weight": 0.10},
                "NOKIA.HE": {"quantity": 1000, "avg_price": 3.60, "target_weight": 0.10},
                "SAMPO.HE": {"quantity": 80, "avg_price": 39.00, "target_weight": 0.10},
            }
        }
        self.save_state()

    def save_state(self):
        """Saves current state to local tiuku_portfolio.json."""
        try:
            with open(self.portfolio_file, "w", encoding="utf-8") as f:
                json.dump(self.portfolio_state, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved Tiuku portfolio state to {self.portfolio_file}")
        except Exception as e:
            logger.error(f"Failed to save portfolio state: {e}")

    def set_cash_balance(self, cash_amount: float) -> None:
        """Updates the cash balance in EUR."""
        self.portfolio_state["cash_balance"] = round(cash_amount, 2)
        self.save_state()
        logger.info(f"Updated cash balance to {cash_amount:.2f} EUR")

    def set_equity_override(self, total_equity: float) -> None:
        """Sets a known total equity override from Nordnet.

        When set, this value is used as the portfolio total for weight calculations
        and rebalancing instead of the yfinance-derived sum. Useful when yfinance
        prices diverge from the real Nordnet account value.

        Args:
            total_equity: Known total portfolio value in EUR from Nordnet.
        """
        self.portfolio_state["total_equity_override"] = round(total_equity, 2)
        self.save_state()
        logger.info(f"Set total_equity_override to {total_equity:,.2f} EUR")


    def set_holding(self, symbol: str, quantity: int, avg_price: float, target_weight: float = 0.10):
        """Adds or updates a stock holding in the portfolio."""
        symbol = symbol.upper()
        if "holdings" not in self.portfolio_state:
            self.portfolio_state["holdings"] = {}

        if quantity <= 0:
            if symbol in self.portfolio_state["holdings"]:
                del self.portfolio_state["holdings"][symbol]
                logger.info(f"Removed holding {symbol} from portfolio.")
        else:
            self.portfolio_state["holdings"][symbol] = {
                "quantity": int(quantity),
                "avg_price": round(avg_price, 2),
                "target_weight": round(target_weight, 4),
            }
            logger.info(f"Updated holding {symbol}: {quantity} pcs @ {avg_price:.2f} EUR (target weight: {target_weight*100:.1f}%)")

        self.save_state()

    def import_from_csv(self, csv_filepath: Path) -> Tuple[Dict[str, Any], int]:
        """Imports holdings from a Nordnet CSV export file."""
        from utils.csv_importer import NordnetCSVImporter
        existing = self.portfolio_state.get("holdings", {})
        updated_holdings, count = NordnetCSVImporter.import_csv(csv_filepath, existing)
        self.portfolio_state["holdings"] = updated_holdings
        self.save_state()
        logger.info(f"Successfully imported {count} holdings from {csv_filepath}")
        return updated_holdings, count

    def sync_valuation(self, nordnet_data: Dict[str, Any]) -> Dict[str, Any]:
        """Merges live price valuation with local portfolio holdings."""
        full_summary = {
            "updated_at": datetime.now().isoformat(),
            "currency": nordnet_data.get("currency", config.CURRENCY),
            "cash_balance": nordnet_data.get("cash_balance", 0.0),
            "cash_weight": nordnet_data.get("cash_weight", 0.0),
            "total_stock_value": nordnet_data.get("total_stock_value", 0.0),
            "total_equity": nordnet_data.get("total_equity", 0.0),
            "holdings": nordnet_data.get("holdings", {}),
        }
        self.append_history_snapshot(full_summary)
        return full_summary

    sync_from_nordnet = sync_valuation

    def append_history_snapshot(self, summary: Dict[str, Any]):
        """Records a snapshot of equity history."""
        history = []
        if self.history_file.exists():
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []

        snapshot = {
            "timestamp": datetime.now().isoformat(),
            "total_equity": summary.get("total_equity", 0.0),
            "cash_balance": summary.get("cash_balance", 0.0),
            "total_stock_value": summary.get("total_stock_value", 0.0),
        }
        history.append(snapshot)

        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save portfolio history: {e}")
