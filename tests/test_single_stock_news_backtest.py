"""
Unit tests for Single-Stock Time Machine Backtester (Layer 2 LLM News Radar).
"""

from pathlib import Path
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from scripts.single_stock_news_backtest import SingleStockNewsBacktester, evaluate_release_layer2



def test_evaluate_release_layer2_reject_and_warn_patterns():
    # Swedish fatal legal landmine
    verdict_fatal, reason_fatal = evaluate_release_layer2(
        ticker="SEZI.ST",
        headline="Seafire AB: Styrelsen upprättar kontrollbalansräkning",
        full_text="Bolagets eget kapital understiger hälften av det registrerade aktiekapitalet."
    )
    assert verdict_fatal == "REJECT"
    assert "kontrollbalansräkning" in reason_fatal.lower()

    # Swedish rights issue warning
    verdict_warn, reason_warn = evaluate_release_layer2(
        ticker="SEZI.ST",
        headline="Seafire AB: Styrelsen beslutar om företrädesemission av aktier",
        full_text="Bolaget genomför en nyemission om 140 MSEK med utspädning."
    )
    assert verdict_warn == "WARN"
    assert "företrädesemission" in reason_warn.lower()

    # US reverse split / ATM warning
    verdict_us, reason_us = evaluate_release_layer2(
        ticker="WATT",
        headline="Energous Announces 1-for-20 Reverse Stock Split",
        full_text="The company announced a reverse split and at-the-market offering."
    )
    assert verdict_us == "WARN"
    assert "reverse stock split" in reason_us.lower() or "at-the-market" in reason_us.lower()


def test_evaluate_release_layer2_hold():
    verdict, reason = evaluate_release_layer2(
        ticker="SEZI.ST",
        headline="Seafire slutför förvärvet av Splendor Plant AB",
        full_text="Förvärvet finansieras genom befintlig kassa och bidrar positivt till EBITA."
    )
    assert verdict == "HOLD"


def test_single_stock_news_backtester_execution(tmp_path):
    # Mock news CSV
    news_csv = tmp_path / "mock_news.csv"
    news_df = pd.DataFrame([
        {
            "Date": "2026-02-17",
            "Ticker": "TEST",
            "Headline": "Company Announces Massive Företrädesemission",
            "Full_Text": "Emergency capital raise with heavy dilution."
        }
    ])
    news_df.to_csv(news_csv, index=False)

    # Mock prices
    mock_dates = pd.date_range("2026-02-16", periods=4, freq="B")
    mock_prices = pd.DataFrame(
        {
            "Open": [10.0, 9.5, 8.0, 7.5],
            "High": [10.5, 9.8, 8.2, 7.8],
            "Low": [9.8, 9.0, 7.8, 7.0],
            "Close": [10.0, 9.2, 8.1, 7.2],
            "Volume": [1000, 1500, 3000, 1200],
        },
        index=mock_dates
    )

    backtester = SingleStockNewsBacktester(
        ticker="TEST",
        news_csv_path=news_csv,
        buy_date="2026-02-16",
        start_date="2026-02-01",
        end_date="2026-02-28",
        slippage_penalty_pct=0.50,
    )

    with patch.object(backtester, "fetch_price_data", return_value=mock_prices):
        result = backtester.run()

    assert result["status"] == "SUCCESS"
    assert result["ticker"] == "TEST"
    assert result["llm_exit_date"] == "2026-02-18"
    assert result["llm_exit_price"] == 8.0
    # Bought at 10.0, sold at 8.0 -> Gross -20%, Net -20.50%
    assert result["llm_net_return_pct"] == -20.50
    assert "dilution" in result["exit_reason"].lower() or "företrädesemission" in result["exit_reason"].lower()
