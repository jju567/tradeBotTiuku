"""
tests/test_email_generator.py - Unit tests for EOD Action List Email Generator (email_generator.py).
"""

from unittest.mock import MagicMock, patch
import pytest

from email_generator import (
    EODActionOrder,
    EODPortfolioSummary,
    build_portfolio_summary,
    build_eod_action_orders,
    format_eod_action_list_text,
    format_eod_action_list_html,
    generate_eod_action_list_email,
    send_eod_action_list_email,
    generate_and_send_p13_eod_action_email,
)


class MockPortfolioConfig:
    portfolio_id = "P13_Live_Candidate"
    limit_price_buffer_pct = 0.2


class MockPortfolio:
    def __init__(self, cash=7000.0, positions=None):
        self.config = MockPortfolioConfig()
        self.cash_balance = cash
        self.positions = positions or []


def test_build_portfolio_summary_hodl_isolation():
    """HODL positions (like FARON.HE or hodl=True) must be excluded from non-HODL equity and listed separately."""
    positions = [
        {"Ticker": "FARON.HE", "Shares": 1000, "Buy Price": 2.50, "hodl": True, "Currency": "EUR"},
        {"Ticker": "QTCOM.HE", "Shares": 20, "Buy Price": 80.0, "hodl": False, "Currency": "EUR"},
    ]
    p = MockPortfolio(cash=5000.0, positions=positions)
    regime = {"regime": "🟢 BULL / LOW VOL", "last_price": 115.0, "sma_50": 110.0}

    summary = build_portfolio_summary(p, regime_data=regime)

    # Cash: 5,000 + QTCOM: 20 * 80 = 1,600 EUR => Total non-HODL equity: 6,600 EUR
    # FARON (2,500 EUR) must NOT be counted into total_equity_eur!
    assert summary.available_cash_eur == 5000.0
    assert summary.total_equity_eur == 6600.0
    assert len(summary.hodl_positions) == 1
    assert summary.hodl_positions[0]["Ticker"] == "FARON.HE"
    assert "BULL" in summary.regime_status


def test_build_eod_action_orders_categorization_and_fields():
    """Validates categorization and required fields for BUYS, FULL EXITS, and PARTIAL TAKE-PROFITS."""
    run_trades = [
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "BUY",
            "ticker": "HARVIA.HE",
            "details": {
                "sub_strategy": "core_nordic_midcap_quality",
                "buy_price": 40.00,
                "shares": 50,
                "currency": "EUR",
                "thesis": "ROE 22.0%, EBIT 16.5%, NetDebt/EBITDA 1.0",
            },
        },
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "SELL",
            "ticker": "DROP.ST",
            "details": {
                "sub_strategy": "satellite_nordic_microcap_catalyst",
                "sell_price": 10.00,
                "shares": 100,
                "currency": "SEK",
                "exit_reason": "STOP_LOSS_SATELLITE (Loss -12.0%)",
            },
        },
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "PARTIAL_TP",
            "ticker": "RUNNER.US",
            "details": {
                "sub_strategy": "satellite_us_microcap_growth",
                "sell_price": 20.00,
                "sold_shares": 75,
                "currency": "USD",
                "entry_price": 16.00,
                "exit_reason": "+25.0% TP Reached",
            },
        },
    ]

    p = MockPortfolio()
    buys, full_exits, partial_tps = build_eod_action_orders(
        portfolio=p,
        run_trades=run_trades,
        limit_buffer_pct=0.002,
    )

    # 1. BUYS check
    assert len(buys) == 1
    b = buys[0]
    assert b.ticker == "HARVIA.HE"
    assert b.sub_strategy == "core_nordic_midcap_quality"
    assert b.order_type == "LIMIT"
    assert b.exact_limit_price == 40.08  # 40.0 * 1.002
    assert b.share_count == 50
    assert b.estimated_total_eur == 2004.0
    assert "-15.0% Core Stop" in b.initial_stop_loss_price
    assert "ROE 22.0%" in b.thesis_catalyst

    # 2. FULL EXITS check
    assert len(full_exits) == 1
    e = full_exits[0]
    assert e.ticker == "DROP.ST"
    assert e.sub_strategy == "satellite_nordic_microcap_catalyst"
    assert e.order_type == "LIMIT"
    assert e.exact_limit_price == 9.98  # 10.0 * 0.998
    assert e.share_count == 100
    assert e.currency == "SEK"
    assert "STOP_LOSS_SATELLITE" in e.thesis_catalyst

    # 3. PARTIAL TP check
    assert len(partial_tps) == 1
    tp = partial_tps[0]
    assert tp.ticker == "RUNNER.US"
    assert tp.sub_strategy == "satellite_us_microcap_growth"
    assert tp.order_type == "LIMIT"
    assert tp.exact_limit_price == 19.96  # 20.0 * 0.998
    assert tp.share_count == 75
    assert tp.currency == "USD"
    assert "Break-Even" in tp.initial_stop_loss_price
    assert "-15.0%" in tp.initial_stop_loss_price


def test_format_eod_action_list_text_and_html():
    """Text and HTML representations must contain header details and all order tables."""
    summary = EODPortfolioSummary(
        portfolio_id="P13_Live_Candidate",
        regime_status="🟢 BULL / LOW VOL",
        available_cash_eur=4250.0,
        total_equity_eur=9800.0,
        hodl_positions=[{"Ticker": "FARON.HE", "Shares": 1000, "Buy Price": 2.0}],
    )
    buys = [
        EODActionOrder(
            category="BUY",
            ticker="CORE1.HE",
            sub_strategy="core_nordic_midcap_quality",
            order_type="LIMIT",
            exact_limit_price=50.10,
            currency="EUR",
            share_count=45,
            estimated_total_eur=2254.5,
            initial_stop_loss_price="42.58 EUR (-15.0%)",
            thesis_catalyst="Compounder Thesis",
        )
    ]
    full_exits = []
    partial_tps = []

    txt = format_eod_action_list_text(summary, buys, full_exits, partial_tps)
    htm = format_eod_action_list_html(summary, buys, full_exits, partial_tps)

    # Verify text formatting
    assert "PORTFOLIO HEADER:" in txt
    assert "Regime Status:              🟢 BULL / LOW VOL" in txt
    assert "Available Cash:             4,250.00 €" in txt
    assert "Total P13 Equity (ex-HODL): 9,800.00 €" in txt
    assert "FARON.HE: Ignored by bot (HODL=True)" in txt
    assert "CORE1.HE" in txt
    assert "EXACT_LIMIT_PRICE:       50.10 EUR" in txt
    assert "INITIAL_STOP_LOSS_PRICE: 42.58 EUR (-15.0%)" in txt

    # Verify HTML formatting
    assert "P13_Live_Candidate" in htm
    assert "4,250.00 €" in htm
    assert "9,800.00 €" in htm
    assert "FARON.HE" in htm
    assert "Ignored by bot / HODL Protected" in htm
    assert "CORE1.HE" in htm
    assert "50.10 EUR" in htm
    assert "Compounder Thesis" in htm


def test_send_eod_action_list_email_graceful_handling():
    """Sending email without credentials should log and return False without throwing exceptions."""
    with patch("email_generator.load_env_credentials", return_value={"ALERT_EMAIL": "", "SMTP_SERVER": ""}):
        res = send_eod_action_list_email("Subject", "Body", "<html>Body</html>")
        assert res is False


def test_send_eod_action_list_email_mock_smtp():
    """Mock SMTP sends successfully when credentials exist."""
    creds = {
        "SMTP_SERVER": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "test@example.com",
        "SMTP_PASS": "secret",
        "ALERT_EMAIL": "recipient@example.com",
    }
    with patch("email_generator.load_env_credentials", return_value=creds):
        with patch("smtplib.SMTP") as mock_smtp:
            instance = mock_smtp.return_value
            instance.sendmail.return_value = {}
            res = send_eod_action_list_email("Subject", "Body", "<html>Body</html>")
            assert res is True
            assert instance.sendmail.called
            assert instance.quit.called
