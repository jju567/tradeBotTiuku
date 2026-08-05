"""Unit tests for config.py — commission calculation and min trade size."""
import pytest
import config


class TestCalculateCommission:
    def test_nordnet_fund_has_zero_commission(self):
        """NN_ prefixed symbols should have 0 EUR commission."""
        assert config.calculate_commission("NN_NORGE", 1000.0) == pytest.approx(0.0)

    def test_domestic_he_uses_domestic_minimum(self):
        """Very small .HE trade should return the domestic minimum fee."""
        result = config.calculate_commission("NOKIA.HE", 10.0)
        assert result == pytest.approx(config.COMMISSION_MIN_EUR)

    def test_foreign_uses_foreign_minimum(self):
        """Very small foreign trade should return the foreign minimum fee."""
        result = config.calculate_commission("AAPL", 10.0)
        assert result == pytest.approx(config.COMMISSION_MIN_FOREIGN_EUR)

    def test_large_trade_uses_percentage(self):
        """Large .HE trade where % > minimum should use percentage."""
        trade_value = 1_000_000.0
        expected = round(trade_value * config.COMMISSION_PERCENT, 2)
        result = config.calculate_commission("NESTE.HE", trade_value)
        assert result == pytest.approx(expected)

    def test_commission_is_never_negative(self):
        assert config.calculate_commission("NOKIA.HE", 0.0) >= 0.0
        assert config.calculate_commission("AAPL", 0.0) >= 0.0

    def test_commission_rounds_to_two_decimals(self):
        result = config.calculate_commission("NESTE.HE", 100.0)
        assert result == round(result, 2)


class TestGetMinTradeSizeForSymbol:
    def test_nordnet_fund_returns_small_min(self):
        result = config.get_min_trade_size_for_symbol("NN_NORGE")
        assert result == pytest.approx(50.0)

    def test_domestic_symbol_returns_at_least_min_trade_eur(self):
        result = config.get_min_trade_size_for_symbol("NOKIA.HE")
        assert result >= config.MIN_TRADE_EUR

    def test_foreign_symbol_returns_at_least_min_trade_eur(self):
        result = config.get_min_trade_size_for_symbol("AAPL")
        assert result >= config.MIN_TRADE_EUR

    def test_foreign_min_larger_than_domestic(self):
        domestic = config.get_min_trade_size_for_symbol("NOKIA.HE")
        foreign = config.get_min_trade_size_for_symbol("AAPL")
        assert foreign >= domestic
