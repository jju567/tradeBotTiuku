"""
tests/test_multi_portfolio.py - Tests for Multi-Portfolio Configuration, Market Engine, and Email Notifier.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import yaml
import pytest

from email_notifier import send_portfolio_alert
from main_controller import (
    PortfolioConfig,
    PortfolioInstance,
    MarketDataEngine,
    MasterLiveTradingDaemon,
    DEFAULT_PORTFOLIOS_YAML,
)


def test_portfolios_yaml_structure():
    """Verify that portfolios_config.yaml exists and contains all 10 expected portfolios with valid parameters."""
    assert DEFAULT_PORTFOLIOS_YAML.exists()
    with open(DEFAULT_PORTFOLIOS_YAML, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    assert "portfolios" in data
    portfolios = data["portfolios"]
    assert len(portfolios) == 10

    expected_portfolios = [
        "P1_Base",
        "P2_Fast_Cycle",
        "P3_Diamond_Hands",
        "P4_Institutional",
        "P5_Nordic_Only",
        "P6_US_Only",
        "P7_Deep_Value_Extreme",
        "P8_Quality_Growth",
        "P9_High_Conviction",
        "P10_Micro_Sniper",
    ]
    for p in expected_portfolios:
        assert p in portfolios, f"Missing portfolio {p}"
        cfg = portfolios[p]
        assert "strategy" in cfg
        assert "dead_money_days" in cfg
        assert "min_adv" in cfg
        assert "slots" in cfg
        assert "start_cash" in cfg
        assert "regions" in cfg
        assert cfg["start_cash"] == 10000


def test_portfolio_instance_creation(tmp_path):
    """Test PortfolioInstance state initialization, save, load, and snapshot."""
    cfg = PortfolioConfig(
        portfolio_id="P_Test",
        strategy="Profile B",
        dead_money_days=180,
        min_adv=50000.0,
        slots=10,
        start_cash=10000.0,
        regions=["FI", "SE"],
    )
    inst = PortfolioInstance(cfg, base_dir=tmp_path)
    assert inst.cash_balance == 10000.0
    assert inst.currency == "EUR"
    assert inst.state_file.exists()
    assert inst.history_file.exists()

    # Append dummy trade
    trade = {
        "Ticker": "TEST.HE",
        "Buy Date": "2026-09-01",
        "Sell Date": "2026-09-20",
        "Buy Price": 10.0,
        "Sell Price": 12.0,
        "Shares": 100,
        "Capital Invested": 1009.0,
        "Gross Sale Value": 1200.0,
        "Transaction Fee": 9.0,
        "Net Return": 1191.0,
        "Net PnL": 182.0,
        "Exit Reason": "TAKE_PROFIT",
    }
    inst.append_trade(trade)
    trades = inst.load_trade_history()
    assert len(trades) == 1
    assert trades[0]["Ticker"] == "TEST.HE"


def test_email_notifier_graceful_failure():
    """Verify that email notifier handles socket/SMTP failure gracefully without raising."""
    with patch("smtplib.SMTP", side_effect=Exception("SMTP Connection Refused")):
        with patch("email_notifier.load_env_credentials", return_value={
            "SMTP_SERVER": "invalid.smtp.host",
            "SMTP_PORT": "587",
            "SMTP_USER": "test@user.com",
            "SMTP_PASS": "secret",
            "ALERT_EMAIL": "alert@user.com",
        }):
            res = send_portfolio_alert(
                portfolio_id="P5_Nordic_Only",
                event_type="BUY",
                ticker="EXEL.HE",
                details={"shares": 50, "price": 13.40},
            )
            assert res is False


def test_permanent_nlp_decision_archive(tmp_path):
    """Verify that append_nlp_decision writes correct CSV format and handles headers properly."""
    from main_controller import append_nlp_decision

    test_archive = tmp_path / "nlp_decisions_archive.csv"
    assert not test_archive.exists()

    # Append first decision
    append_nlp_decision(
        ticker="BIOA.ST",
        headline="BioArctic presents positive Phase 3 clinical data",
        llm_decision="HOLD",
        reasoning="Positive drug trial progression, no dilution",
        archive_path=test_archive,
        timestamp="2026-09-23T11:00:00+00:00",
    )
    assert test_archive.exists()

    # Append second decision
    append_nlp_decision(
        ticker="SEZI.ST",
        headline="Seafire AB beslutar om företrädesemission av aktier",
        llm_decision="REJECT",
        reasoning="Fatal red flag företrädesemission detected",
        archive_path=test_archive,
        timestamp="2026-09-23T11:05:00+00:00",
    )

    # Attempt to append empty headlines (should be strictly skipped)
    append_nlp_decision(
        ticker="EMPTY.ST",
        headline="",
        llm_decision="HOLD",
        reasoning="Empty headline",
        archive_path=test_archive,
    )
    append_nlp_decision(
        ticker="BLANK.ST",
        headline="    \n   ",
        llm_decision="HOLD",
        reasoning="Whitespace headline",
        archive_path=test_archive,
    )

    with open(test_archive, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines() if line.strip()]

    # Empty headlines were skipped, line count remains 3 (header + 2 valid rows)
    assert len(lines) == 3
    assert lines[0] == "Timestamp,Ticker,Headline,LLM_Decision,Reasoning"
    assert "BIOA.ST" in lines[1]
    assert "HOLD" in lines[1]
    assert "SEZI.ST" in lines[2]
    assert "REJECT" in lines[2]


def test_market_engine_nlp_caching_and_deduplication(tmp_path):
    """Verify that MarketDataEngine evaluates and logs new headlines EXACTLY ONCE globally."""
    archive_file = tmp_path / "test_nlp_archive.csv"
    engine = MarketDataEngine()
    engine.evaluated_news_cache.clear()

    mock_news = [
        {"content": {"title": "Company reports record Q2 profit beat", "summary": "Strong sales"}},
        {"content": {"title": "", "summary": ""}},  # Empty item: must be skipped
        {"title": None, "summary": None},           # Malformed/empty: must be skipped
    ]

    with patch("main_controller.DEFAULT_NLP_ARCHIVE_CSV", archive_file):
        with patch("yfinance.Ticker") as mock_ticker:
            mock_inst = MagicMock()
            mock_inst.news = mock_news
            mock_ticker.return_value = mock_inst

            # First evaluation: new headline -> should log to CSV ONCE
            v1, r1 = engine._evaluate_news_radar("ABC")
            assert v1 in ("HOLD", "WARN", "REJECT")
            assert archive_file.exists()

            with open(archive_file, "r", encoding="utf-8") as f:
                lines_first = [line.strip() for line in f.readlines() if line.strip()]
            assert len(lines_first) == 2  # Header + 1 record

            # Second evaluation for the same ticker/headline (e.g. subsequent portfolio or cycle)
            v2, r2 = engine._evaluate_news_radar("ABC")
            assert (v2, r2) == (v1, r1)

            with open(archive_file, "r", encoding="utf-8") as f:
                lines_second = [line.strip() for line in f.readlines() if line.strip()]
            # Must NOT append duplicate line — exactly 2 lines remain
            assert len(lines_second) == 2



def test_send_run_summary_email():
    """Verify that send_run_summary_email formats summary and sends single consolidated email."""
    from email_notifier import send_run_summary_email

    run_trades = [
        {
            "portfolio_id": "P6_US_Only",
            "action": "BUY",
            "ticker": "IDN",
            "details": {
                "strategy": "Profile B",
                "shares": 465,
                "buy_price": "2.43 USD",
                "total_cost": "1,140.23 USD (997.93 EUR)",
                "remaining_cash": "2,023.36 EUR",
            },
        },
        {
            "portfolio_id": "P1_Base",
            "action": "SELL",
            "ticker": "WRAP",
            "details": {
                "exit_reason": "TAKE_PROFIT",
                "shares": 100,
                "sell_price": "5.50 USD",
                "net_pnl": "+250.00 USD",
            },
        }
    ]
    portfolio_results = {
        "P1_Base": {"positions_count": 5, "cash": 4500.0, "total_equity": 10250.0, "return_pct": 2.5},
        "P6_US_Only": {"positions_count": 8, "cash": 2023.36, "total_equity": 10100.0, "return_pct": 1.0},
    }

    import email
    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value = mock_instance

        with patch("email_notifier.load_env_credentials", return_value={
            "SMTP_SERVER": "smtp.mock.server",
            "SMTP_PORT": "587",
            "SMTP_USER": "test@user.com",
            "SMTP_PASS": "secret",
            "ALERT_EMAIL": "alert@user.com",
        }):
            res = send_run_summary_email(run_trades, portfolio_results)
            assert res is True
            assert mock_instance.sendmail.called
            sent_args = mock_instance.sendmail.call_args[0]
            raw_msg = email.message_from_string(sent_args[2])
            body_parts = []
            for part in raw_msg.walk():
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode("utf-8", errors="ignore"))
            full_body = "\n".join(body_parts)

            assert "P6_US_Only" in full_body
            assert "IDN" in full_body
            assert "P1_Base" in full_body
            assert "WRAP" in full_body
            assert "Ajon Kauppayhteenveto" in full_body


