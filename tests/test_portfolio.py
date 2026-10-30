"""Unit tests for core/portfolio.py — PortfolioManager."""
import json
import pytest
from pathlib import Path
from core.portfolio import PortfolioManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_portfolio(tmp_path) -> tuple[Path, Path]:
    """Returns (portfolio_file, history_file) paths in a temp directory."""
    portfolio_file = tmp_path / "tiuku_portfolio.json"
    history_file = tmp_path / "portfolio_history.json"
    return portfolio_file, history_file


@pytest.fixture
def default_manager(tmp_portfolio) -> PortfolioManager:
    """PortfolioManager with fresh empty tmp files."""
    pf, hf = tmp_portfolio
    return PortfolioManager(portfolio_file=pf, history_file=hf)


# ---------------------------------------------------------------------------
# Initialization & Default State
# ---------------------------------------------------------------------------


class TestPortfolioManagerInit:
    def test_creates_default_portfolio_when_file_missing(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        state = mgr.portfolio_state
        assert "holdings" in state
        assert "cash_balance" in state
        assert state["cash_balance"] > 0

    def test_saves_default_portfolio_to_disk(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        PortfolioManager(portfolio_file=pf, history_file=hf)
        assert pf.exists()

    def test_loads_existing_portfolio_file(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        data = {"currency": "EUR", "cash_balance": 9999.0, "holdings": {}}
        pf.write_text(json.dumps(data), encoding="utf-8")

        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        assert mgr.portfolio_state["cash_balance"] == pytest.approx(9999.0)

    def test_falls_back_to_default_on_corrupt_file(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        pf.write_text("NOT VALID JSON", encoding="utf-8")
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        assert "holdings" in mgr.portfolio_state


# ---------------------------------------------------------------------------
# set_cash_balance
# ---------------------------------------------------------------------------


class TestSetCashBalance:
    def test_updates_cash_balance(self, default_manager):
        default_manager.set_cash_balance(5000.0)
        assert default_manager.portfolio_state["cash_balance"] == pytest.approx(5000.0)

    def test_persists_cash_balance_to_disk(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        mgr.set_cash_balance(1234.56)

        reloaded = json.loads(pf.read_text(encoding="utf-8"))
        assert reloaded["cash_balance"] == pytest.approx(1234.56)

    def test_rounds_to_two_decimal_places(self, default_manager):
        default_manager.set_cash_balance(1000.999)
        assert default_manager.portfolio_state["cash_balance"] == pytest.approx(1001.0, abs=0.01)


# ---------------------------------------------------------------------------
# set_holding
# ---------------------------------------------------------------------------


class TestSetHolding:
    def test_adds_new_holding(self, default_manager):
        default_manager.set_holding("TSLA", 10, 200.0)
        assert "TSLA" in default_manager.portfolio_state["holdings"]

    def test_uppercases_symbol(self, default_manager):
        default_manager.set_holding("tsla", 10, 200.0)
        assert "TSLA" in default_manager.portfolio_state["holdings"]

    def test_updates_existing_holding(self, default_manager):
        default_manager.set_holding("NOKIA.HE", 100, 3.60)
        default_manager.set_holding("NOKIA.HE", 200, 4.00)
        h = default_manager.portfolio_state["holdings"]["NOKIA.HE"]
        assert h["quantity"] == 200
        assert h["avg_price"] == pytest.approx(4.00)

    def test_removes_holding_when_quantity_zero(self, default_manager):
        default_manager.set_holding("NOKIA.HE", 100, 3.60)
        default_manager.set_holding("NOKIA.HE", 0, 3.60)
        assert "NOKIA.HE" not in default_manager.portfolio_state["holdings"]

    def test_removes_holding_when_quantity_negative(self, default_manager):
        default_manager.set_holding("NOKIA.HE", 100, 3.60)
        default_manager.set_holding("NOKIA.HE", -1, 3.60)
        assert "NOKIA.HE" not in default_manager.portfolio_state["holdings"]

    def test_stores_default_target_weight(self, default_manager):
        default_manager.set_holding("TEST.HE", 50, 10.0)
        h = default_manager.portfolio_state["holdings"]["TEST.HE"]
        assert h["target_weight"] == pytest.approx(0.10)

    def test_stores_custom_target_weight(self, default_manager):
        default_manager.set_holding("TEST.HE", 50, 10.0, target_weight=0.15)
        h = default_manager.portfolio_state["holdings"]["TEST.HE"]
        assert h["target_weight"] == pytest.approx(0.15)

    def test_set_holding_preserves_hodl_and_metadata(self, default_manager):
        """Updating quantity/price must preserve existing metadata like hodl and custom target_weight."""
        # Initialize holding with HODL protection, custom name, and 25% target weight
        default_manager.portfolio_state["holdings"]["QDVE.DE"] = {
            "quantity": 100,
            "avg_price": 20.0,
            "target_weight": 0.25,
            "hodl": True,
            "name": "iShares S&P 500 Info Tech",
        }
        # CLI call: only passes symbol, quantity, price
        default_manager.set_holding("QDVE.DE", 150, 22.0)
        h = default_manager.portfolio_state["holdings"]["QDVE.DE"]
        assert h["quantity"] == 150
        assert h["avg_price"] == pytest.approx(22.0)
        assert h["target_weight"] == pytest.approx(0.25)
        assert h["hodl"] is True
        assert h["name"] == "iShares S&P 500 Info Tech"

    def test_persists_holding_to_disk(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        mgr.set_holding("ELISA.HE", 25, 48.0)

        reloaded = json.loads(pf.read_text(encoding="utf-8"))
        assert "ELISA.HE" in reloaded["holdings"]


# ---------------------------------------------------------------------------
# sync_valuation & append_history_snapshot
# ---------------------------------------------------------------------------


class TestSyncValuation:
    def test_returns_summary_with_required_keys(self, default_manager):
        nordnet_data = {
            "currency": "EUR",
            "cash_balance": 1000.0,
            "cash_weight": 0.10,
            "total_stock_value": 9000.0,
            "total_equity": 10000.0,
            "holdings": {},
        }
        result = default_manager.sync_valuation(nordnet_data)
        assert "total_equity" in result
        assert "cash_balance" in result
        assert "holdings" in result

    def test_appends_snapshot_to_history(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        nordnet_data = {
            "currency": "EUR",
            "cash_balance": 500.0,
            "cash_weight": 0.05,
            "total_stock_value": 9500.0,
            "total_equity": 10000.0,
            "holdings": {},
        }
        mgr.sync_valuation(nordnet_data)
        mgr.sync_valuation(nordnet_data)  # call twice

        history = json.loads(hf.read_text(encoding="utf-8"))
        assert len(history) == 2

    def test_history_snapshot_contains_equity(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        nordnet_data = {
            "currency": "EUR",
            "cash_balance": 500.0,
            "cash_weight": 0.05,
            "total_stock_value": 9500.0,
            "total_equity": 12345.0,
            "holdings": {},
        }
        mgr.sync_valuation(nordnet_data)
        history = json.loads(hf.read_text(encoding="utf-8"))
        assert history[0]["total_equity"] == pytest.approx(12345.0)


# ---------------------------------------------------------------------------
# import_from_csv_bytes & UI helpers
# ---------------------------------------------------------------------------


class TestPortfolioManagerCSVBytes:
    def test_import_from_csv_bytes_updates_portfolio(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        raw_csv = "Nimi\tMäärä\tKeskikurssi\nNokia\t150\t3,80\nNeste\t40\t26,50\n".encode("utf-8")

        result = mgr.import_from_csv_bytes(raw_csv)
        assert result["success"] is True
        assert result["count"] == 2
        assert "NOKIA.HE" in result["holdings"]
        assert result["holdings"]["NOKIA.HE"]["quantity"] == 150
        assert result["holdings"]["NOKIA.HE"]["avg_price"] == pytest.approx(3.80)

        # Check persistence
        reloaded = json.loads(pf.read_text(encoding="utf-8"))
        assert "NOKIA.HE" in reloaded["holdings"]

    def test_preview_csv_bytes_does_not_modify_state(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        orig_holdings = dict(mgr.portfolio_state["holdings"])
        raw_csv = "Nimi\tMäärä\tKeskikurssi\nNokia\t999\t1,00\n".encode("utf-8")

        preview = mgr.preview_csv_bytes(raw_csv)
        assert preview["total_rows"] == 1
        assert preview["parsed_positions"][0]["symbol"] == "NOKIA.HE"
        # State must remain unchanged
        assert mgr.portfolio_state["holdings"] == orig_holdings

    def test_toggle_hodl_and_remove_holding(self, tmp_portfolio):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)
        mgr.set_holding("FARON.HE", 1000, 1.20)
        assert mgr.portfolio_state["holdings"]["FARON.HE"].get("hodl", False) is False

        # Toggle HODL on
        new_val = mgr.toggle_hodl("FARON.HE")
        assert new_val is True
        assert mgr.portfolio_state["holdings"]["FARON.HE"]["hodl"] is True

        # Toggle HODL off
        new_val = mgr.toggle_hodl("FARON.HE")
        assert new_val is False

        # Remove holding
        removed = mgr.remove_holding("FARON.HE")
        assert removed is True
        assert "FARON.HE" not in mgr.portfolio_state["holdings"]

    def test_sync_to_p13_state(self, tmp_portfolio, tmp_path):
        pf, hf = tmp_portfolio
        mgr = PortfolioManager(portfolio_file=pf, history_file=hf)

        p13_file = tmp_path / "portfolio_P13_Live_Candidate_state.json"
        p13_init = {
            "portfolio_id": "P13_Live_Candidate",
            "starting_balance": 10000.0,
            "positions": [
                {
                    "Ticker": "NOKIA.HE",
                    "Shares": 50,
                    "Buy Price": 3.0,
                    "Capital Invested": 150.0,
                    "Strategy": "Profile B",
                }
            ],
        }
        p13_file.write_text(json.dumps(p13_init), encoding="utf-8")

        raw_csv = "Nimi\tMäärä\tKeskikurssi\nNokia\t200\t4,10\nViafin Service\t50\t20,00\n".encode("utf-8")
        result = mgr.import_from_csv_bytes(raw_csv, sync_p13=True, p13_state_path=p13_file)
        assert result["p13_synced"] is True

        p13_updated = json.loads(p13_file.read_text(encoding="utf-8"))
        tickers = {p["Ticker"]: p for p in p13_updated["positions"]}
        assert "NOKIA.HE" in tickers
        assert tickers["NOKIA.HE"]["Shares"] == 200
        assert tickers["NOKIA.HE"]["Buy Price"] == pytest.approx(4.10)
        assert "VIAFIN.HE" in tickers
        assert tickers["VIAFIN.HE"]["Shares"] == 50
        assert p13_updated["cash_balance"] == pytest.approx(mgr.portfolio_state.get("cash_balance", 0.0))
