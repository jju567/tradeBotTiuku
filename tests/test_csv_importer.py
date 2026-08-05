"""Unit tests for utils/csv_importer.py — parse_finnish_number, map_name_to_symbol, NordnetCSVImporter."""
import io
import pytest
from pathlib import Path
from unittest.mock import patch, mock_open

from utils.csv_importer import (
    parse_finnish_number,
    map_name_to_symbol,
    NordnetCSVImporter,
)


# ---------------------------------------------------------------------------
# parse_finnish_number
# ---------------------------------------------------------------------------


class TestParseFinnishNumber:
    def test_empty_string_returns_zero(self):
        assert parse_finnish_number("") == 0.0

    def test_none_value_returns_zero(self):
        assert parse_finnish_number(None) == 0.0

    def test_comma_decimal_separator(self):
        assert parse_finnish_number("32,01") == pytest.approx(32.01)

    def test_space_thousands_separator(self):
        assert parse_finnish_number("2 699,50") == pytest.approx(2699.50)

    def test_non_breaking_space(self):
        # \xa0 is the Finnish Nordnet thousand separator
        assert parse_finnish_number("1\xa0234,56") == pytest.approx(1234.56)

    def test_plain_integer_string(self):
        assert parse_finnish_number("100") == pytest.approx(100.0)

    def test_plain_float_with_dot(self):
        assert parse_finnish_number("25.40") == pytest.approx(25.40)

    def test_invalid_string_returns_zero(self):
        assert parse_finnish_number("N/A") == 0.0

    def test_whitespace_only_returns_zero(self):
        assert parse_finnish_number("   ") == 0.0


# ---------------------------------------------------------------------------
# map_name_to_symbol
# ---------------------------------------------------------------------------


class TestMapNameToSymbol:
    def test_maps_known_nordnet_name(self):
        result = map_name_to_symbol("Nokia", existing_holdings={})
        assert result == "NOKIA.HE"

    def test_maps_known_name_case_insensitive(self):
        result = map_name_to_symbol("NOKIA", existing_holdings={})
        assert result == "NOKIA.HE"

    def test_maps_nordnet_fund_name(self):
        result = map_name_to_symbol("Nordnet Norge Indeks", existing_holdings={})
        assert result == "NN_NORGE"

    def test_prefers_existing_holdings_name_match(self):
        existing = {"CUSTOM.HE": {"name": "Custom Company", "quantity": 10, "avg_price": 5.0}}
        result = map_name_to_symbol("Custom Company AB", existing_holdings=existing)
        assert result == "CUSTOM.HE"

    def test_prefers_existing_holdings_symbol_match(self):
        existing = {"NOKIA.HE": {"quantity": 100, "avg_price": 3.60}}
        result = map_name_to_symbol("NOKIA.HE Stock", existing_holdings=existing)
        assert result == "NOKIA.HE"

    def test_fallback_for_unknown_name(self):
        result = map_name_to_symbol("Unknown Exotic Corp", existing_holdings={})
        assert result == "UNKNOWN_EXOTIC_CORP"

    def test_partial_name_match(self):
        result = map_name_to_symbol("Neste Oyj", existing_holdings={})
        assert result == "NESTE.HE"

    def test_apple_maps_correctly(self):
        result = map_name_to_symbol("Apple Inc", existing_holdings={})
        assert result == "AAPL"


# ---------------------------------------------------------------------------
# NordnetCSVImporter
# ---------------------------------------------------------------------------


class TestNordnetCSVImporter:
    """Tests for NordnetCSVImporter.import_csv using tmp files."""

    def _write_csv(self, tmp_path: Path, content: str, filename: str = "holdings.csv") -> Path:
        filepath = tmp_path / filename
        filepath.write_text(content, encoding="utf-8")
        return filepath

    def test_raises_file_not_found_for_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            NordnetCSVImporter.import_csv(tmp_path / "missing.csv", {})

    def test_raises_value_error_for_empty_file(self, tmp_path):
        filepath = self._write_csv(tmp_path, "")
        with pytest.raises(ValueError, match="empty"):
            NordnetCSVImporter.import_csv(filepath, {})

    def test_imports_basic_tab_separated_csv(self, tmp_path):
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\n"
        filepath = self._write_csv(tmp_path, content)
        holdings, count = NordnetCSVImporter.import_csv(filepath, {})
        assert count == 1
        assert "NOKIA.HE" in holdings
        assert holdings["NOKIA.HE"]["quantity"] == 100
        assert holdings["NOKIA.HE"]["avg_price"] == pytest.approx(3.60)

    def test_imports_semicolon_separated_csv(self, tmp_path):
        content = "Nimi;Määrä;Keskikurssi\nNeste;50;25,40\n"
        filepath = self._write_csv(tmp_path, content)
        holdings, count = NordnetCSVImporter.import_csv(filepath, {})
        assert count == 1
        assert "NESTE.HE" in holdings

    def test_skips_rows_with_zero_quantity(self, tmp_path):
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t0\t3,60\n"
        filepath = self._write_csv(tmp_path, content)
        holdings, count = NordnetCSVImporter.import_csv(filepath, {})
        assert count == 0
        assert "NOKIA.HE" not in holdings

    # ----- Sold position removal (critical bug fix) -------------------------

    def test_removes_sold_positions_not_in_csv(self, tmp_path):
        """Positions absent from the CSV must be removed (they were sold in Nordnet)."""
        existing = {
            "NOKIA.HE": {"quantity": 100, "avg_price": 4.0},
            "NESTE.HE": {"quantity": 50, "avg_price": 25.0},  # will be sold
        }
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\n"  # only Nokia
        filepath = self._write_csv(tmp_path, content)
        holdings, _ = NordnetCSVImporter.import_csv(filepath, existing)
        assert "NOKIA.HE" in holdings
        assert "NESTE.HE" not in holdings  # sold → removed

    def test_hodl_position_preserved_even_when_absent_from_csv(self, tmp_path):
        """HODL-locked positions must survive import even if they are not in the CSV."""
        existing = {
            "NOKIA.HE": {"quantity": 100, "avg_price": 4.0},
            "NESTE.HE": {"quantity": 50, "avg_price": 25.0, "hodl": True, "note": "Lottolappu"},
        }
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\n"  # NESTE absent
        filepath = self._write_csv(tmp_path, content)
        holdings, _ = NordnetCSVImporter.import_csv(filepath, existing)
        assert "NESTE.HE" in holdings  # HODL → kept
        assert holdings["NESTE.HE"].get("hodl") is True

    def test_non_hodl_not_in_csv_is_removed_hodl_is_not(self, tmp_path):
        """Mixed scenario: sold non-HODL removed, HODL kept, CSV position added."""
        existing = {
            "SOLD.HE": {"quantity": 30, "avg_price": 10.0},          # sold, not HODL → remove
            "LOCKED.HE": {"quantity": 200, "avg_price": 5.0, "hodl": True},  # HODL → keep
        }
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\n"  # new position in CSV
        filepath = self._write_csv(tmp_path, content)
        holdings, count = NordnetCSVImporter.import_csv(filepath, existing)
        assert "SOLD.HE" not in holdings
        assert "LOCKED.HE" in holdings
        assert "NOKIA.HE" in holdings
        assert count == 1

    # ----- Metadata preservation --------------------------------------------

    def test_preserves_hodl_flag_for_existing_holdings(self, tmp_path):
        existing = {"NOKIA.HE": {"quantity": 50, "avg_price": 4.0, "hodl": True, "note": "Long-term"}}
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\n"
        filepath = self._write_csv(tmp_path, content)
        holdings, _ = NordnetCSVImporter.import_csv(filepath, existing)
        assert holdings["NOKIA.HE"].get("hodl") is True
        assert holdings["NOKIA.HE"].get("note") == "Long-term"

    def test_preserves_target_weight_for_existing_holdings(self, tmp_path):
        existing = {"NOKIA.HE": {"quantity": 50, "avg_price": 4.0, "target_weight": 0.15}}
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\n"
        filepath = self._write_csv(tmp_path, content)
        holdings, _ = NordnetCSVImporter.import_csv(filepath, existing)
        assert holdings["NOKIA.HE"]["target_weight"] == pytest.approx(0.15)

    def test_sets_default_target_weight_for_new_holdings(self, tmp_path):
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\n"
        filepath = self._write_csv(tmp_path, content)
        holdings, _ = NordnetCSVImporter.import_csv(filepath, {})
        assert holdings["NOKIA.HE"]["target_weight"] == pytest.approx(0.10)

    # ----- avg_price handling ------------------------------------------------

    def test_avg_price_zero_when_column_missing_and_warns(self, tmp_path, caplog):
        """Missing price column must set avg_price=0.0 and emit a warning."""
        import logging
        content = "Nimi\tMäärä\nNokia\t100\n"  # no price column
        filepath = self._write_csv(tmp_path, content)
        with caplog.at_level(logging.WARNING):
            holdings, _ = NordnetCSVImporter.import_csv(filepath, {})
        assert holdings["NOKIA.HE"]["avg_price"] == pytest.approx(0.0)
        assert any("avg_price" in msg for msg in caplog.messages)

    # ----- Multiple rows -----------------------------------------------------

    def test_multiple_rows_imported(self, tmp_path):
        content = "Nimi\tMäärä\tKeskikurssi\nNokia\t100\t3,60\nNeste\t50\t25,40\n"
        filepath = self._write_csv(tmp_path, content)
        holdings, count = NordnetCSVImporter.import_csv(filepath, {})
        assert count == 2
        assert "NOKIA.HE" in holdings
        assert "NESTE.HE" in holdings
