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
    assert metrics["adjusted_cash"] == 10_000_000.0
    assert metrics["operating_cash_flow_ttm"] == -6_000_000.0
    assert metrics["cash_runway_years"] == 1.67
    assert metrics["cash_runway_months"] == 20.0
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


def test_profile_b_anti_shrinking_rejects_missing_or_negative_ocf():
    from screener.financial_metrics_engine import evaluate_profiles

    # 1. Shrinking revenue (-10%) with positive net cash but OCF is None -> must be rejected from Profile B
    res1 = evaluate_profiles({
        "revenue_growth_yoy_pct": -10.0,
        "gross_margin_pct": 30.0,
        "cash_runway_months": "Infinite",
        "operating_cash_flow_ttm": None,
        "net_cash": 5_000_000.0,
    })
    assert not res1["is_profile_b"]
    assert "BUY_PROFILE_B" not in res1["signal"]
    assert any("Anti-Shrinking rule triggered" in r for r in res1["reasons"])

    # 2. Shrinking revenue (-10%) with positive net cash and negative OCF -> must be rejected
    res2 = evaluate_profiles({
        "revenue_growth_yoy_pct": -10.0,
        "gross_margin_pct": 30.0,
        "cash_runway_months": "10.0",
        "operating_cash_flow_ttm": -1_000_000.0,
        "net_cash": 5_000_000.0,
    })
    assert not res2["is_profile_b"]
    assert "BUY_PROFILE_B" not in res2["signal"]

    # 3. Shrinking revenue (-10%) with positive net cash and positive OCF -> turnaround allowed in Profile B
    res3 = evaluate_profiles({
        "revenue_growth_yoy_pct": -10.0,
        "gross_margin_pct": 30.0,
        "cash_runway_months": "Infinite",
        "operating_cash_flow_ttm": 2_000_000.0,
        "net_cash": 5_000_000.0,
    })
    assert res3["is_profile_b"]
    assert "BUY_PROFILE_B" in res3["signal"]


def test_state_isolation_batch_prevents_leakage():
    """Verifies that consecutive ticker evaluations never leak data between tickers."""
    from screener.financial_metrics_engine import get_hard_financials_batch

    with patch("screener.financial_metrics_engine.yf.Ticker") as mock_ticker:
        # Ticker 1: Full data (High growth, positive cash, OCF)
        mock1 = MagicMock()
        mock1.quarterly_balance_sheet = pd.DataFrame(
            {"2024-12-31": [50_000_000.0, 0.0]}, index=["Cash And Cash Equivalents", "Total Debt"]
        )
        mock1.quarterly_cashflow = None
        mock1.cashflow = pd.DataFrame(
            {"2024": [10_000_000.0]}, index=["Operating Cash Flow"]
        )
        mock1.financials = pd.DataFrame(
            {"2024": [100_000_000.0], "2023": [50_000_000.0]}, index=["Total Revenue"]
        )
        mock1.info = {}
        mock1.fast_info = None

        # Ticker 2: Fails or missing revenue/cash
        mock2 = MagicMock()
        mock2.quarterly_balance_sheet = pd.DataFrame()
        mock2.balance_sheet = pd.DataFrame()
        mock2.quarterly_cashflow = pd.DataFrame()
        mock2.cashflow = pd.DataFrame()
        mock2.quarterly_financials = pd.DataFrame()
        mock2.financials = pd.DataFrame()
        mock2.info = {}
        mock2.fast_info = None

        mock_ticker.side_effect = [mock1, mock2]

        batch_res = get_hard_financials_batch(["STOCK_A", "STOCK_B"])

        res_a = batch_res["STOCK_A"]
        res_b = batch_res["STOCK_B"]

        # Stock A has valid metrics
        assert res_a["revenue_growth_yoy_pct"] == 100.0
        assert res_a["cash_and_equivalents"] == 50_000_000.0
        assert res_a["operating_cash_flow_ttm"] == 10_000_000.0
        assert res_a["has_positive_cash_flow"] is True

        # Stock B MUST NOT have leaked any values from Stock A
        assert res_b["revenue_growth_yoy_pct"] is None
        assert res_b["cash_and_equivalents"] is None
        assert res_b["operating_cash_flow_ttm"] is None
        assert res_b["free_cash_flow"] is None
        assert res_b["has_positive_cash_flow"] is False


def test_cash_burner_rejection_negative_cash_flow():
    """Verifies that early-stage cash burners with negative OCF/FCF are rejected from profiles and tiers."""
    from screener.financial_metrics_engine import evaluate_profiles
    from screener.tiered_filters import matches_nordic_microcap_catalyst, matches_us_microcap_growth

    # BeammWave-like profile: High revenue growth (+140%), positive net cash, but negative OCF
    cash_burner_facts = {
        "ticker": "BEAMMW-B.ST",
        "revenue_growth_yoy_pct": 140.0,
        "gross_margin_pct": 50.0,
        "operating_cash_flow_ttm": -500_000.0,
        "free_cash_flow": -600_000.0,
        "cash_and_equivalents": 20_000_000.0,
        "net_cash": 18_000_000.0,
        "cash_runway_months": 24.0,  # Even if theoretical runway appears positive
        "has_positive_cash_flow": False,
    }

    # Profile evaluation: MUST be rejected from both Profile A and Profile B
    eval_res = evaluate_profiles(cash_burner_facts)
    assert not eval_res["is_profile_a"], "Cash burner must not pass Profile A"
    assert not eval_res["is_profile_b"], "Cash burner must not pass Profile B"
    assert eval_res["signal"] == "REJECT"

    # Tiered filters: Nordic MicroCap Catalyst MUST reject cash burners
    meta_nordic = {
        "market": "SE",
        "market_cap_local": 300_000_000.0,  # ~26M EUR
        "currency": "SEK",
        "adv_20d_local": 2_000_000.0,
    }
    passed_nordic, reasons_nordic = matches_nordic_microcap_catalyst("BEAMMW-B.ST", cash_burner_facts, meta_nordic)
    assert not passed_nordic
    assert any("Profile" in r or "runway" in r or "cash" in r.lower() for r in reasons_nordic)

    # Tiered filters: US MicroCap Growth MUST reject cash burners
    meta_us = {
        "market": "US",
        "exchange": "NASDAQ",
        "market_cap_usd": 100_000_000.0,
        "currency": "USD",
        "adv_20d_local": 500_000.0,
    }
    passed_us, reasons_us = matches_us_microcap_growth("BURN_US", cash_burner_facts, meta_us)
    assert not passed_us
    assert any("runway" in r or "cash" in r.lower() for r in reasons_us)


def test_relative_ev_ebit_valuation_filter():
    """Verifies that relative EV/EBIT > 20% above 5y median is flagged as historically expensive."""
    from screener.financial_metrics_engine import calculate_relative_ev_ebit

    # Case 1: Expensive (24.0 vs 15.0 -> +60.0% > 20%)
    res_exp = calculate_relative_ev_ebit(
        t=None,
        override_current_ev_ebit=24.0,
        override_5y_median=15.0,
    )
    assert res_exp["is_historically_expensive"] is True
    assert res_exp["ev_ebit_premium_pct"] == 60.0

    # Case 2: Fair/Cheap (14.0 vs 15.0 -> -6.67% <= 20%)
    res_cheap = calculate_relative_ev_ebit(
        t=None,
        override_current_ev_ebit=14.0,
        override_5y_median=15.0,
    )
    assert res_cheap["is_historically_expensive"] is False
    assert res_cheap["ev_ebit_premium_pct"] < 0.0

    # Case 3: Missing 5y history -> neutral fallback without error or penalty
    res_missing = calculate_relative_ev_ebit(
        t=None,
        current_ev=200_000_000.0,
        current_ebit=10_000_000.0,
    )
    assert res_missing["is_historically_expensive"] is False
    assert res_missing["ev_ebit_premium_pct"] is None


def test_bid_ask_spread_and_liquidity_gate():
    """Verifies that spread > 3.0% or turnover < 50,000 EUR flags REJECTED_LOW_LIQUIDITY."""
    from unittest.mock import MagicMock
    from screener.financial_metrics_engine import get_hard_financials

    # Case 1: Wide spread 4.0% (> 3.0%)
    mock_wide = MagicMock()
    mock_wide.quarterly_balance_sheet = None
    mock_wide.balance_sheet = None
    mock_wide.quarterly_cashflow = None
    mock_wide.cashflow = None
    mock_wide.quarterly_financials = None
    mock_wide.financials = None
    mock_wide.fast_info = None
    mock_wide.info = {
        "bid": 9.60,
        "ask": 10.00,  # spread = (10 - 9.60) / 10 = 4.0% > 3.0%
        "averageDailyVolume10Day": 100_000,
        "currentPrice": 10.0,
        "currency": "EUR",
    }
    with patch("yfinance.Ticker", return_value=mock_wide):
        res = get_hard_financials("WIDE_SPREAD")
        assert res["bid_ask_spread_pct"] == 4.0
        assert res["liquidity_status"] == "REJECTED_LOW_LIQUIDITY"
        assert res["status"] == "REJECTED_LOW_LIQUIDITY"

    # Case 2: Low daily turnover 30,000 EUR (< 50,000 EUR)
    mock_illiquid = MagicMock()
    mock_illiquid.quarterly_balance_sheet = None
    mock_illiquid.balance_sheet = None
    mock_illiquid.quarterly_cashflow = None
    mock_illiquid.cashflow = None
    mock_illiquid.quarterly_financials = None
    mock_illiquid.financials = None
    mock_illiquid.fast_info = None
    mock_illiquid.info = {
        "bid": 9.95,
        "ask": 10.00,  # spread = 0.5% (good)
        "averageDailyVolume10Day": 3_000,
        "currentPrice": 10.0,  # turnover = 30,000 EUR < 50,000 EUR
        "currency": "EUR",
    }
    with patch("yfinance.Ticker", return_value=mock_illiquid):
        res_liq = get_hard_financials("ILLIQUID")
        assert res_liq["avg_daily_turnover_eur"] == 30_000.0
        assert res_liq["liquidity_status"] == "REJECTED_LOW_LIQUIDITY"
        assert res_liq["status"] == "REJECTED_LOW_LIQUIDITY"
