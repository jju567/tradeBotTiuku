import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
import config

logger = logging.getLogger(__name__)


def _atomic_write_json(target_path: Path, payload: Any, ensure_ascii: bool = False) -> None:
    """Writes JSON atomically via a PID-scoped temporary file and os.replace."""
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=ensure_ascii)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        for attempt in range(5):
            try:
                os.replace(tmp_path, target_path)
                break
            except Exception:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


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
                "NESTE.HE": {"name": "Neste", "quantity": 39, "avg_price": 29.11, "target_weight": 0.10},
                "NOKIA.HE": {"name": "Nokia", "quantity": 147, "avg_price": 8.74, "target_weight": 0.10},
                "QDVE.DE": {"name": "iShares S&P 500 IT Sector UCITS ETF", "quantity": 15, "avg_price": 32.01, "target_weight": 0.20},
            }
        }
        self.save_state()

    def save_state(self):
        """Saves current state to local tiuku_portfolio.json atomically."""
        try:
            _atomic_write_json(self.portfolio_file, self.portfolio_state, ensure_ascii=False)
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


    def set_holding(self, symbol: str, quantity: int, avg_price: float, target_weight: Optional[float] = None, hodl: bool = False):
        """Adds or updates a stock holding in the portfolio while preserving existing metadata (such as hodl, name)."""
        symbol = symbol.upper()
        if "holdings" not in self.portfolio_state:
            self.portfolio_state["holdings"] = {}

        if quantity <= 0:
            if symbol in self.portfolio_state["holdings"]:
                del self.portfolio_state["holdings"][symbol]
                logger.info(f"Removed holding {symbol} from portfolio.")
        else:
            existing = self.portfolio_state["holdings"].get(symbol, {})
            updated = dict(existing)
            effective_target_weight = target_weight if target_weight is not None else existing.get("target_weight", 0.10)
            updated.update({
                "quantity": int(quantity),
                "avg_price": round(avg_price, 4),
                "target_weight": round(float(effective_target_weight), 4),
            })
            if hodl:
                updated["hodl"] = True
            self.portfolio_state["holdings"][symbol] = updated
            logger.info(f"Updated holding {symbol}: {quantity} pcs @ {avg_price:.4f} EUR (target weight: {updated['target_weight']*100:.1f}%, hodl={updated.get('hodl', False)})")

        self.save_state()

    def remove_holding(self, symbol: str) -> bool:
        """Removes a single holding from the portfolio."""
        symbol = symbol.upper()
        if "holdings" in self.portfolio_state and symbol in self.portfolio_state["holdings"]:
            del self.portfolio_state["holdings"][symbol]
            self.save_state()
            logger.info(f"Removed holding {symbol} from portfolio.")
            return True
        return False

    def toggle_hodl(self, symbol: str) -> bool:
        """Toggles the HODL lock flag for a holding."""
        symbol = symbol.upper()
        if "holdings" in self.portfolio_state and symbol in self.portfolio_state["holdings"]:
            current = self.portfolio_state["holdings"][symbol].get("hodl", False)
            self.portfolio_state["holdings"][symbol]["hodl"] = not current
            self.save_state()
            logger.info(f"Toggled HODL for {symbol} to {not current}")
            return not current
        return False

    def import_from_csv(self, csv_filepath: Path, sync_p13: bool = False, p13_state_path: Optional[Path] = None) -> Tuple[Dict[str, Any], int]:
        """Imports holdings from a Nordnet CSV export file."""
        from utils.csv_importer import NordnetCSVImporter
        existing = self.portfolio_state.get("holdings", {})
        updated_holdings, count = NordnetCSVImporter.import_csv(csv_filepath, existing)
        self.portfolio_state["holdings"] = updated_holdings
        self.save_state()
        logger.info(f"Successfully imported {count} holdings from {csv_filepath}")

        if sync_p13:
            p13_file = p13_state_path or (config.DATA_DIR / "portfolios" / "portfolio_P13_Live_Candidate_state.json")
            self._sync_to_p13_state(p13_file, updated_holdings)

        return updated_holdings, count

    def import_from_csv_bytes(
        self,
        content_bytes: bytes,
        sync_p13: bool = False,
        p13_state_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Imports holdings directly from raw CSV bytes (e.g. from UI file uploader).

        Parses using NordnetCSVImporter, updates self.portfolio_state, and optionally
        syncs with P13 Live Candidate state.
        """
        from utils.csv_importer import NordnetCSVImporter
        if not content_bytes:
            raise ValueError("CSV-tiedoston sisältö on tyhjä.")

        existing = dict(self.portfolio_state.get("holdings", {}))
        preview = NordnetCSVImporter.preview_csv_bytes(content_bytes, existing)
        updated_holdings = preview["updated_holdings"]
        count = preview["total_rows"]

        self.portfolio_state["holdings"] = updated_holdings
        self.save_state()
        logger.info(f"Successfully imported {count} holdings from CSV bytes")

        added = [r["symbol"] for r in preview["parsed_positions"] if r.get("is_new")]
        updated = [r["symbol"] for r in preview["parsed_positions"] if not r.get("is_new")]
        removed = preview["removed_symbols"]
        hodl_preserved = preview["hodl_preserved_symbols"]

        p13_synced = False
        if sync_p13:
            p13_file = p13_state_path or (config.DATA_DIR / "portfolios" / "portfolio_P13_Live_Candidate_state.json")
            p13_synced = self._sync_to_p13_state(p13_file, updated_holdings)

        return {
            "success": True,
            "count": count,
            "holdings": updated_holdings,
            "added": added,
            "updated": updated,
            "removed": removed,
            "hodl_preserved": hodl_preserved,
            "p13_synced": p13_synced,
            "parsed_positions": preview["parsed_positions"],
        }

    def preview_csv_bytes(self, content_bytes: bytes) -> Dict[str, Any]:
        """Previews parsed holdings from raw CSV bytes without modifying state."""
        from utils.csv_importer import NordnetCSVImporter
        if not content_bytes:
            raise ValueError("CSV-tiedoston sisältö on tyhjä.")
        existing = dict(self.portfolio_state.get("holdings", {}))
        return NordnetCSVImporter.preview_csv_bytes(content_bytes, existing)

    def _sync_to_p13_state(self, p13_file: Path, updated_holdings: Dict[str, Any]) -> bool:
        """Syncs imported holdings into P13 Live Candidate state file."""
        p13_path = Path(p13_file)
        if not p13_path.exists():
            logger.warning(f"P13 state file not found at {p13_path}; skipping P13 sync.")
            return False
        try:
            with open(p13_path, "r", encoding="utf-8") as f:
                p13_data = json.load(f)

            existing_p13_positions = p13_data.get("positions", [])
            pos_by_ticker = {p.get("Ticker", "").upper(): p for p in existing_p13_positions if p.get("Ticker")}

            new_p13_positions = []
            today_str = datetime.now().strftime("%Y-%m-%d")

            for sym, h in updated_holdings.items():
                sym_up = sym.upper()
                if sym_up in pos_by_ticker:
                    pos = dict(pos_by_ticker[sym_up])
                    pos["Shares"] = float(h.get("quantity", 0))
                    pos["Buy Price"] = float(h.get("avg_price", 0.0))
                    pos["Capital Invested"] = round(pos["Shares"] * pos["Buy Price"], 2)
                    pos["last_evaluated_date"] = today_str
                    if h.get("hodl"):
                        pos["hodl"] = True
                    new_p13_positions.append(pos)
                else:
                    shares = float(h.get("quantity", 0))
                    buy_price = float(h.get("avg_price", 0.0))
                    is_se = sym_up.endswith(".ST")
                    is_fi = sym_up.endswith(".HE")
                    mkt = "SE" if is_se else ("FI" if is_fi else "US")
                    curr = "SEK" if is_se else ("EUR" if is_fi else "USD")
                    sub_strat = (
                        "core_nordic_midcap_quality" if is_fi
                        else ("satellite_nordic_microcap_catalyst" if is_se else "satellite_us_microcap_growth")
                    )

                    new_pos = {
                        "Ticker": sym_up,
                        "Buy Date": today_str,
                        "Buy Price": buy_price,
                        "Shares": shares,
                        "Capital Invested": round(shares * buy_price, 2),
                        "Strategy": "Profile B",
                        "Currency": curr,
                        "market": mkt,
                        "current_price": buy_price,
                        "highest_price_seen": buy_price,
                        "Sub_Strategy": sub_strat,
                        "Execution_Verified": True,
                        "Note": "Imported from Nordnet CSV",
                    }
                    if h.get("hodl"):
                        new_pos["hodl"] = True
                    new_p13_positions.append(new_pos)

            p13_data["positions"] = new_p13_positions
            p13_data["updated_at"] = datetime.now().isoformat()
            _atomic_write_json(p13_path, p13_data)
            logger.info(f"Successfully synced {len(new_p13_positions)} positions into P13 state file {p13_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to sync P13 state: {e}")
            return False

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
            _atomic_write_json(self.history_file, history, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save portfolio history: {e}")
