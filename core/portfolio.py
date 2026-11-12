import json
import logging
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
import config

logger = logging.getLogger(__name__)

_WRITE_LOCK = threading.Lock()


def _atomic_write_json(target_path: Path, payload: Any, ensure_ascii: bool = False) -> None:
    """Writes JSON atomically via a thread-safe, uniquely-named temporary file, fsync, and os.replace."""
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    with _WRITE_LOCK:
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
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass


class StateVault:
    """Manages rolling 7-day backups of portfolio state JSON files.

    Creates weekday-based backups (e.g. portfolio_P13_Live_Candidate_state_maanantai.bak)
    in a designated 'backups/' directory before any write operation.
    """

    WEEKDAYS_FI = {
        0: "maanantai",
        1: "tiistai",
        2: "keskiviikko",
        3: "torstai",
        4: "perjantai",
        5: "lauantai",
        6: "sunnuntai",
    }

    def __init__(self, backup_dir: Optional[Path] = None):
        self.backup_dir = Path(backup_dir) if backup_dir else None

    def get_backup_dir(self, state_file: Path) -> Path:
        """Resolves target backup directory for a given state file."""
        if self.backup_dir:
            b_dir = self.backup_dir
        else:
            b_dir = Path(state_file).parent / "backups"
        b_dir.mkdir(parents=True, exist_ok=True)
        return b_dir

    def get_weekday_name(self, dt: Optional[datetime] = None) -> str:
        """Returns lowercase Finnish weekday name (maanantai..sunnuntai)."""
        target_dt = dt or datetime.now()
        return self.WEEKDAYS_FI.get(target_dt.weekday(), "maanantai")

    def backup_state(self, state_file: Path, dt: Optional[datetime] = None) -> Optional[Path]:
        """Creates a rolling weekday backup of state_file if the file exists on disk.

        Example filename: portfolio_P13_Live_Candidate_state_maanantai.bak
        """
        try:
            state_path = Path(state_file)
            if not state_path.exists():
                return None
            b_dir = self.get_backup_dir(state_path)
            weekday_str = self.get_weekday_name(dt)
            backup_filename = f"{state_path.stem}_{weekday_str}.bak"
            backup_path = b_dir / backup_filename

            shutil.copy2(state_path, backup_path)
            logger.info(f"StateVault: Created rolling backup {backup_path.name} in {b_dir}")
            return backup_path
        except Exception as e:
            logger.error(f"StateVault backup failed for {state_file}: {e}")
            return None

    def backup_payload(self, state_file: Path, payload: Any, dt: Optional[datetime] = None) -> Optional[Path]:
        """Writes current payload directly to a weekday backup file."""
        try:
            state_path = Path(state_file)
            b_dir = self.get_backup_dir(state_path)
            weekday_str = self.get_weekday_name(dt)
            backup_filename = f"{state_path.stem}_{weekday_str}.bak"
            backup_path = b_dir / backup_filename
            _atomic_write_json(backup_path, payload, ensure_ascii=False)
            logger.info(f"StateVault: Wrote payload backup {backup_path.name} in {b_dir}")
            return backup_path
        except Exception as e:
            logger.error(f"StateVault payload backup failed for {state_file}: {e}")
            return None

    backup = backup_state
    create_backup = backup_state


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
        """Saves current state to local tiuku_portfolio.json atomically, creating StateVault backup first."""
        try:
            vault = StateVault()
            vault.backup_state(self.portfolio_file)
            _atomic_write_json(self.portfolio_file, self.portfolio_state, ensure_ascii=False)
            logger.info(f"Saved Tiuku portfolio state to {self.portfolio_file}")
        except Exception as e:
            logger.error(f"Failed to save portfolio state: {e}")

    def set_cash_balance(self, cash_amount: float, sync_p13: bool = True) -> None:
        """Updates the cash balance in EUR and optionally syncs to P13 Live Candidate."""
        self.portfolio_state["cash_balance"] = round(cash_amount, 2)
        self.save_state()
        logger.info(f"Updated cash balance to {cash_amount:.2f} EUR")
        if sync_p13 and self.portfolio_file == config.TIUKU_PORTFOLIO_FILE:
            p13_file = config.DATA_DIR / "portfolios" / "portfolio_P13_Live_Candidate_state.json"
            if p13_file.exists():
                try:
                    StateVault().backup_state(p13_file)
                    with open(p13_file, "r", encoding="utf-8") as f:
                        p13_data = json.load(f)
                    p13_data["cash_balance"] = round(cash_amount, 2)
                    p13_data["updated_at"] = datetime.now().isoformat()
                    _atomic_write_json(p13_file, p13_data)
                    logger.info(f"Synced cash balance {cash_amount:.2f} EUR to P13 state file {p13_file}")
                except Exception as e:
                    logger.warning(f"Could not sync cash balance to P13: {e}")

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

    def import_from_csv(self, csv_filepath: Path, sync_p13: bool = False, p13_state_path: Optional[Path] = None, preserve_hodl: bool = True, cash_balance: Optional[float] = None) -> Tuple[Dict[str, Any], int]:
        """Imports holdings from a Nordnet CSV export file."""
        from utils.csv_importer import NordnetCSVImporter
        existing = self.portfolio_state.get("holdings", {})
        updated_holdings, count = NordnetCSVImporter.import_csv(csv_filepath, existing, preserve_hodl=preserve_hodl)
        self.portfolio_state["holdings"] = updated_holdings
        if cash_balance is not None:
            self.portfolio_state["cash_balance"] = round(float(cash_balance), 2)
        self.save_state()
        logger.info(f"Successfully imported {count} holdings from {csv_filepath} (cash: {self.portfolio_state.get('cash_balance')})")

        if sync_p13:
            p13_file = p13_state_path or (config.DATA_DIR / "portfolios" / "portfolio_P13_Live_Candidate_state.json")
            self._sync_to_p13_state(p13_file, updated_holdings)

        return updated_holdings, count

    def import_from_csv_bytes(
        self,
        content_bytes: bytes,
        sync_p13: bool = False,
        p13_state_path: Optional[Path] = None,
        preserve_hodl: bool = True,
        cash_balance: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Imports holdings directly from raw CSV bytes (e.g. from UI file uploader).

        Parses using NordnetCSVImporter, updates self.portfolio_state, and optionally
        syncs with P13 Live Candidate state.
        """
        from utils.csv_importer import NordnetCSVImporter
        if not content_bytes:
            raise ValueError("CSV-tiedoston sisältö on tyhjä.")

        existing = dict(self.portfolio_state.get("holdings", {}))
        preview = NordnetCSVImporter.preview_csv_bytes(content_bytes, existing, preserve_hodl=preserve_hodl)
        updated_holdings = preview["updated_holdings"]
        count = preview["total_rows"]

        self.portfolio_state["holdings"] = updated_holdings
        if cash_balance is not None:
            self.portfolio_state["cash_balance"] = round(float(cash_balance), 2)

        self.save_state()
        logger.info(f"Successfully imported {count} holdings from CSV bytes (cash: {self.portfolio_state.get('cash_balance')})")

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

    def preview_csv_bytes(self, content_bytes: bytes, preserve_hodl: bool = True) -> Dict[str, Any]:
        """Previews parsed holdings from raw CSV bytes without modifying state."""
        from utils.csv_importer import NordnetCSVImporter
        if not content_bytes:
            raise ValueError("CSV-tiedoston sisältö on tyhjä.")
        existing = dict(self.portfolio_state.get("holdings", {}))
        return NordnetCSVImporter.preview_csv_bytes(content_bytes, existing, preserve_hodl=preserve_hodl)

    def _sync_to_p13_state(self, p13_file: Path, updated_holdings: Dict[str, Any]) -> bool:
        """Syncs imported holdings into P13 Live Candidate state file."""
        p13_path = Path(p13_file)
        if not p13_path.exists():
            logger.warning(f"P13 state file not found at {p13_path}; skipping P13 sync.")
            return False
        try:
            from screener.price_fetcher import normalize_ticker
            with open(p13_path, "r", encoding="utf-8") as f:
                p13_data = json.load(f)

            existing_p13_positions = p13_data.get("positions", [])
            pos_by_ticker = {normalize_ticker(p.get("Ticker", "")): p for p in existing_p13_positions if p.get("Ticker")}

            new_p13_positions = []
            today_str = datetime.now().strftime("%Y-%m-%d")

            for sym, h in updated_holdings.items():
                sym_up = normalize_ticker(sym)
                # Skip HODL positions (e.g. FARON) so they don't pollute P13 algorithmic trading portfolio
                if h.get("hodl") or sym_up in ("FARON.HE", "FARON"):
                    continue

                # Match exact ticker, or root symbol (e.g. LIME vs LIME.ST or LIME_TECHNOLOGIES)
                matched_key = None
                if sym_up in pos_by_ticker:
                    matched_key = sym_up
                else:
                    for k in pos_by_ticker:
                        if k.split(".")[0] == sym_up.split(".")[0] or (k.startswith("LIME") and sym_up.startswith("LIME")):
                            matched_key = k
                            break

                if matched_key:
                    pos = dict(pos_by_ticker[matched_key])
                    old_shares = float(pos.get("Shares", 0) or 0.0)
                    new_shares = float(h.get("quantity", 0) or 0.0)
                    old_price = float(pos.get("Buy Price", 0.0) or 0.0)
                    new_price = float(h.get("avg_price", 0.0) or 0.0)

                    # Osakesplit- ja Datan Vääristymän Hälytin
                    if old_shares > 0 and new_shares > 0:
                        share_diff_pct = abs(new_shares - old_shares) / old_shares
                        if share_diff_pct >= 0.50:
                            share_ratio = new_shares / old_shares
                            expected_price = old_price / share_ratio if share_ratio > 0 else old_price
                            price_adjusted_inversely = (
                                old_price > 0
                                and new_price > 0
                                and (abs(new_price - expected_price) / expected_price <= 0.35)
                            )
                            if price_adjusted_inversely:
                                logger.warning(
                                    f"🚨 [STOCK SPLIT DETECTED] {sym_up}: Shares changed {old_shares} -> {new_shares} "
                                    f"({share_ratio:.2f}x), price adjusted {old_price:.2f} -> {new_price:.2f}. "
                                    f"Adjusting historical reference levels to prevent false stop loss."
                                )
                                pos["split_detected"] = True
                                pos["split_ratio"] = round(share_ratio, 4)
                                pos["split_date"] = today_str
                                for p_key in ("Peak Price", "Highest Close", "highest_price_seen", "stop_threshold", "eod_stop_threshold"):
                                    if p_key in pos and pos[p_key] is not None:
                                        try:
                                            pos[p_key] = round(float(pos[p_key]) / share_ratio, 4)
                                        except Exception:
                                            pass
                                a_flags = list(pos.get("Audit_Flags") or pos.get("audit_flags") or [])
                                if "STOCK_SPLIT_DETECTED" not in a_flags:
                                    a_flags.append("STOCK_SPLIT_DETECTED")
                                pos["Audit_Flags"] = a_flags
                                pos["audit_flags"] = a_flags
                                pos["Audit_Warning"] = f"⚠️ OSAKESPLIT HAVAITTU: {old_shares} -> {new_shares} kpl (kerroin {share_ratio:.2f}x). Hintatasot mukautettu."
                                pos["audit_warning"] = pos["Audit_Warning"]
                            elif (old_price > 0 and new_price > 0 and (abs(new_price - old_price) / old_price >= 0.50)) or (old_shares > 0 and new_shares > 0 and share_diff_pct >= 0.50):
                                logger.warning(
                                    f"🚨 [DATA DISTORTION DETECTED] {sym_up}: Significant quantity discrepancy "
                                    f"({old_shares} -> {new_shares}) without proportional price adjustment "
                                    f"({old_price:.2f} vs {new_price:.2f}). Flagged for manual review."
                                )
                                pos["data_distortion_detected"] = True
                                pos["manual_review_required"] = True
                                pos["requires_manual_review"] = True
                                a_flags = list(pos.get("Audit_Flags") or pos.get("audit_flags") or [])
                                if "DATA_DISTORTION_WARNING" not in a_flags:
                                    a_flags.append("DATA_DISTORTION_WARNING")
                                pos["Audit_Flags"] = a_flags
                                pos["audit_flags"] = a_flags
                                pos["Audit_Warning"] = f"⚠️ DATAN VÄÄRISTYMÄ: Osakemäärä poikkeaa merkittävästi ({old_shares} -> {new_shares}) ilman käänteistä hintamuutosta. Vaatii manuaalisen tarkistuksen!"
                                pos["audit_warning"] = pos["Audit_Warning"]

                    pos["Ticker"] = sym_up
                    pos["Shares"] = float(new_shares)
                    pos["Buy Price"] = float(new_price)
                    curr = h.get("currency") or pos.get("Currency") or ("SEK" if sym_up.endswith(".ST") else "EUR")
                    pos["Currency"] = curr
                    pos["Capital Invested"] = round(pos["Shares"] * pos["Buy Price"], 2)
                    if h.get("market_value_eur"):
                        pos["Capital Invested EUR"] = round(float(h["market_value_eur"]), 2)
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
                    curr = h.get("currency") or ("SEK" if is_se else ("EUR" if is_fi else "USD"))
                    # TASK-11: Market cap-based classification (>= 150M EUR -> Core Mid-Cap, < 150M EUR or missing -> Satellite Micro-Cap)
                    mcap_eur = float(h.get("market_cap_eur") or 0.0)
                    if mcap_eur <= 0:
                        try:
                            clean_u_file = Path(__file__).resolve().parent.parent / "data" / "clean_microcap_universe.csv"
                            if clean_u_file.exists():
                                import csv
                                with open(clean_u_file, "r", encoding="utf-8") as f_cu:
                                    for r in csv.DictReader(f_cu):
                                        if r.get("ticker", "").strip().upper() == sym_up:
                                            mc_loc = float(r.get("market_cap_local") or 0.0)
                                            mc_usd = float(r.get("market_cap_usd") or 0.0)
                                            r_curr = str(r.get("currency") or curr).upper()
                                            if r_curr == "EUR" and mc_loc > 0:
                                                mcap_eur = mc_loc
                                            elif r_curr == "SEK" and mc_loc > 0:
                                                mcap_eur = mc_loc / 11.30
                                            elif mc_usd > 0:
                                                mcap_eur = mc_usd / 1.08
                                            break
                        except Exception:
                            pass

                    if (is_fi or is_se) and mcap_eur <= 0:
                        try:
                            import yfinance as yf
                            t_inst = yf.Ticker(sym_up)
                            raw_mc = getattr(t_inst.fast_info, "market_cap", None) or 0.0
                            # Handle dual-class A/B shares (e.g., BEAMMW-B.ST) via impliedSharesOutstanding
                            info_d = getattr(t_inst, "info", None)
                            if isinstance(info_d, dict):
                                imp_sh = float(info_d.get("impliedSharesOutstanding") or info_d.get("sharesOutstanding") or 0.0)
                                px_now = float(info_d.get("currentPrice") or info_d.get("previousClose") or buy_price or 0.0)
                                raw_mc = max(float(raw_mc or 0.0), float(info_d.get("marketCap") or 0.0), imp_sh * px_now)
                            if raw_mc and float(raw_mc) > 0:
                                fx_eur = 1.0 if is_fi else (1.0 / 11.30)
                                mcap_eur = float(raw_mc) * fx_eur
                        except Exception:
                            pass

                    if is_fi or is_se:
                        if mcap_eur >= 150_000_000.0:
                            sub_strat = "core_nordic_midcap_quality"
                        else:
                            # Missing market cap (<= 0) or < 150M EUR defaults safely to Satellite (never Core!)
                            sub_strat = "satellite_nordic_microcap_catalyst"
                    else:
                        sub_strat = "satellite_us_microcap_growth"

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
                    if h.get("market_value_eur"):
                        new_pos["Capital Invested EUR"] = round(float(h["market_value_eur"]), 2)
                    if h.get("hodl"):
                        new_pos["hodl"] = True
                    new_p13_positions.append(new_pos)

            p13_data["positions"] = new_p13_positions
            if "cash_balance" in self.portfolio_state:
                p13_data["cash_balance"] = float(self.portfolio_state["cash_balance"])
            p13_data["updated_at"] = datetime.now().isoformat()
            StateVault().backup_state(p13_path)
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
