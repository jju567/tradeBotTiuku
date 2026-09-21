"""
Unit tests for Financial Metrics Engine.
"""

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from screener.financial_metrics_engine import get_hard_financials, _extract_metric_from_df


def test_extract_metric_from_df_exact_and_case_insensitive():
    df = pd.DataFrame(
        {
            "2024-12-31": [1000.0, 500.0],
            "2023-12-31": [800.0, 400.0],
        },
        index=["Cash And Cash Equivalents", "Total debt"],
    )

    cash = _extract_metric_from_df(df, ["Cash And Cash Equivalents", "Cash"])
    assert cash == 1000.0

    debt = _extract_metric_from_df(df, ["Total Debt"])
    assert debt == 500.0

    missing = _extract_metric_from_df(df, ["Nonexistent Row"])
    assert missing is None


def test_extract_metric_from_empty_df():
    assert _extract_metric_from_df(None, ["Cash"]) is None
    assert _extract_metric_from_df(pd.DataFrame(), ["Cash"]) is None


@patch("screener.financial_metrics_engine.yf.Ticker")
def test_get_hard_financials_positive_ocf_infinite_runway(mock_ticker):
    mock_inst = MagicMock()
    mock_ticker.return_value = mock_inst

    # Mock Balance Sheet
    bs = pd.DataFrame(
        {"2024-12-31": [20_000_000.0, 5_000_000.0]},
        index=["Cash And Cash Equivalents", "Total Debt"],
    )
    mock_inst.quarterly_balance_sheet = bs
    mock_inst.balance_sheet = bs

    # Mock Cash Flow (Positive OCF -> Infinite Runway)
    cf = pd.DataFrame(
        {"2024-12-31": [4_000_000.0]},
        index=["Operating Cash Flow"],
    )
    mock_inst.quarterly_cashflow = cf
    mock_inst.cashflow = cf

    # Mock Financials (YoY Growth: (120M - 100M) / 100M = +20.0%)
    fin = pd.DataFrame(
        {"2024": [120_000_000.0], "2023": [100_000_000.0]},
        index=["Total Revenue"],
    )
    mock_inst.financials = fin
    mock_inst.quarterly_financials = None

    metrics = get_hard_financials("TEST")

    assert metrics["ticker"] == "TEST"
    assert metrics["cash_and_equivalents"] == 20_000_000.0
    assert metrics["total_debt"] == 5_000_000.0
    assert metrics["net_cash"] == 15_000_000.0
    assert metrics["operating_cash_flow_ttm"] == 4_000_000.0
    assert metrics["cash_runway_months"] == "Infinite"
    assert metrics["total_revenue_current"] == 120_000_000.0
    assert metrics["total_revenue_previous"] == 100_000_000.0
    assert metrics["revenue_growth_yoy_pct"] == 20.0
    assert metrics["status"] == "OK"


@patch("screener.financial_metrics_engine.yf.Ticker")
def test_get_hard_financials_negative_ocf_runway_calculation(mock_ticker):
    mock_inst = MagicMock()
    mock_ticker.return_value = mock_inst

    # Mock Balance Sheet: Cash = 12M, Debt = 2M -> Net Cash = 10M
    bs = pd.DataFrame(
        {"2024-12-31": [12_000_000.0, 2_000_000.0]},
        index=["Cash And Cash Equivalents", "Total Debt"],
    )
    mock_inst.quarterly_balance_sheet = bs
    mock_inst.balance_sheet = bs

    # Mock Cash Flow: Negative OCF = -6.0M TTM -> monthly burn = 0.5M/mo -> Runway = 12M / 0.5M = 24.0 months
    cf = pd.DataFrame(
        {"2024-12-31": [-6_000_000.0]},
        index=["Operating Cash Flow"],
    )
    mock_inst.quarterly_cashflow = cf
    mock_inst.cashflow = cf

    # Mock Financials: YoY decline: (80M - 100M) / 100M = -20.0%
    fin = pd.DataFrame(
        {"2024": [80_000_000.0], "2023": [100_000_000.0]},
        index=["Total Revenue"],
    )
    mock_inst.financials = fin
    mock_inst.quarterly_financials = None

    metrics = get_hard_financials("BURN")

    assert metrics["ticker"] == "BURN"
    assert metrics["cash_and_equivalents"] == 12_000_000.0
    assert metrics["total_debt"] == 2_000_000.0
    assert metrics["net_cash"] == 10_000_000.0
    assert metrics["operating_cash_flow_ttm"] == -6_000_000.0
    assert metrics["cash_runway_months"] == 24.0
    assert metrics["revenue_growth_yoy_pct"] == -20.0
    assert metrics["status"] == "OK"


@patch("screener.financial_metrics_engine.yf.Ticker")
def test_get_hard_financials_graceful_error_handling(mock_ticker):
    mock_ticker.side_effect = Exception("API connection timed out")

    metrics = get_hard_financials("ERR")
    assert metrics["ticker"] == "ERR"
    assert metrics["status"] == "ERROR"
    assert metrics["cash_and_equivalents"] is None
    assert metrics["net_cash"] is None
