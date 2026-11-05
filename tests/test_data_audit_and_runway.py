"""
Tests for Cash Runway calculation, Share Dilution detection, and Data Auditor.
"""

import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

from screener.financial_metrics_engine import (
    get_hard_financials,
    check_share_dilution,
    audit_candidates_data,
    evaluate_profiles,
)
from email_generator import (
    EODActionOrder,
    EODPortfolioSummary,
    build_eod_action_orders,
    format_eod_action_list_text,
    format_eod_action_list_html,
)


def test_adjusted_cash_and_low_runway_rejection():
    """
    Test 1: Adjusted Cash calculation and REJECTED_LOW_RUNWAY.
    Cash = 5M, Current Debt = 2M, Lease Obligation = 1M -> Adjusted Cash = 2M.
    TTM OCF = -3M (negative).
    Runway = 2M / 3M = 0.67 years (< 1.0 year) -> Status: REJECTED_LOW_RUNWAY.
    """
    with patch("screener.financial_metrics_engine.yf.Ticker") as mock_ticker:
        mock_inst = MagicMock()
        mock_ticker.return_value = mock_inst

        # Balance sheet with Cash, Current Debt, and Capital Lease Obligation
        bs = pd.DataFrame(
            {"2024-12-31": [5_000_000.0, 2_000_000.0, 1_000_000.0, 4_000_000.0]},
            index=["Cash And Cash Equivalents", "Current Debt", "Current Capital Lease Obligation", "Total Debt"],
        )
        mock_inst.quarterly_balance_sheet = bs
        mock_inst.balance_sheet = bs

        # Cash flow: Negative OCF = -3.0M
        cf = pd.DataFrame(
            {"2024-12-31": [-3_000_000.0]},
            index=["Operating Cash Flow"],
        )
        mock_inst.quarterly_cashflow = cf
        mock_inst.cashflow = cf

        # Income statement
        fin = pd.DataFrame(
            {"2024": [10_000_000.0], "2023": [8_000_000.0]},
            index=["Total Revenue"],
        )
        mock_inst.financials = fin
        mock_inst.quarterly_financials = None

        metrics = get_hard_financials("LOW_RUNWAY")

        assert metrics["ticker"] == "LOW_RUNWAY"
        assert metrics["cash_and_equivalents"] == 5_000_000.0
        assert metrics["current_debt"] == 2_000_000.0
        assert metrics["current_capital_lease_obligation"] == 1_000_000.0
        assert metrics["adjusted_cash"] == 2_000_000.0
        assert metrics["operating_cash_flow_ttm"] == -3_000_000.0
        assert metrics["cash_runway_years"] == 0.67
        assert metrics["cash_runway_months"] == 8.0
        assert metrics["status"] == "REJECTED_LOW_RUNWAY"
        assert metrics["runway_status"] == "REJECTED_LOW_RUNWAY"

        # Profile evaluation must reject this candidate
        prof_res = evaluate_profiles(metrics)
        assert prof_res["signal"] == "REJECT"
        assert prof_res["is_profile_a"] is False
        assert prof_res["is_profile_b"] is False


def test_positive_ocf_gives_infinite_runway():
    """
    Test 2: Positive TTM OCF gives Infinite runway.
    """
    with patch("screener.financial_metrics_engine.yf.Ticker") as mock_ticker:
        mock_inst = MagicMock()
        mock_ticker.return_value = mock_inst

        bs = pd.DataFrame(
            {"2024-12-31": [10_000_000.0, 1_000_000.0]},
            index=["Cash And Cash Equivalents", "Total Debt"],
        )
        mock_inst.quarterly_balance_sheet = bs
        mock_inst.balance_sheet = bs

        cf = pd.DataFrame(
            {"2024-12-31": [2_000_000.0]},
            index=["Operating Cash Flow"],
        )
        mock_inst.quarterly_cashflow = cf
        mock_inst.cashflow = cf

        fin = pd.DataFrame(
            {"2024": [20_000_000.0], "2023": [16_000_000.0]},
            index=["Total Revenue"],
        )
        mock_inst.financials = fin
        mock_inst.quarterly_financials = None

        metrics = get_hard_financials("POS_OCF")

        assert metrics["operating_cash_flow_ttm"] == 2_000_000.0
        assert metrics["cash_runway_years"] == "Infinite"
        assert metrics["cash_runway_months"] == "Infinite"
        assert metrics["runway_status"] == "RUNWAY_OK"
        assert metrics["status"] == "OK"


def test_share_dilution_detection():
    """
    Test 3: check_share_dilution detects > 5% dilution vs historical share count.
    """
    t = MagicMock()
    info = {"sharesOutstanding": 50_000_000}

    # Annual balance sheet with 33M shares 1 year ago -> dilution = (50 - 33) / 33 = +51.5%
    bs_a = pd.DataFrame(
        {"2024-12-31": [45_000_000.0], "2023-12-31": [33_000_000.0], "2022-12-31": [16_000_000.0]},
        index=["Ordinary Shares Number"],
    )

    res = check_share_dilution(t=t, info=info, bs_a=bs_a)
    assert res["shares_outstanding_current"] == 50_000_000.0
    assert res["has_share_dilution"] is True
    assert res["dilution_flag"] == "DILUTION_WARNING_RECENT_ISSUE"
    assert res["share_dilution_pct"] > 5.0

    # Stable share count (no dilution)
    info_stable = {"sharesOutstanding": 10_000_000}
    bs_stable = pd.DataFrame(
        {"2024-12-31": [10_000_000.0], "2023-12-31": [10_000_000.0]},
        index=["Ordinary Shares Number"],
    )
    res_stable = check_share_dilution(t=t, info=info_stable, bs_a=bs_stable)
    assert res_stable["has_share_dilution"] is False
    assert res_stable["dilution_flag"] is None
    assert res_stable["share_dilution_pct"] == 0.0


def test_audit_candidates_data_duplicates_and_outliers():
    """
    Test 4: audit_candidates_data flags exact duplicates and extreme outliers.
    """
    candidates = [
        {
            "ticker": "NOVO.ST",
            "score": 85.0,
            "revenue_growth": 0.7170,
            "ebit_margin": 0.1500,
            "roe": 0.18,
            "audit_flags": [],
        },
        {
            "ticker": "TRIO.ST",
            "score": 82.0,
            "revenue_growth": 0.7170,  # DUPLICATE with NOVO.ST!
            "ebit_margin": 0.2200,
            "roe": 0.25,
            "audit_flags": [],
        },
        {
            "ticker": "HYPER.US",
            "score": 90.0,
            "revenue_growth": 1.45,    # OUTLIER > 1.0 (145%)
            "ebit_margin": 0.10,
            "roe": 1.20,               # OUTLIER > 1.0 (120%)
            "audit_flags": [],
        },
        {
            "ticker": "DILUTE.ST",
            "score": 75.0,
            "revenue_growth": 0.30,
            "ebit_margin": 0.12,
            "roe": 0.10,
            "hard_facts": {
                "has_share_dilution": True,
                "dilution_flag": "DILUTION_WARNING_RECENT_ISSUE",
            },
            "audit_flags": [],
        },
    ]

    audited = audit_candidates_data(candidates)

    # NOVO and TRIO must have DATA_DUPLICATE_WARNING
    novo = next(c for c in audited if c["ticker"] == "NOVO.ST")
    trio = next(c for c in audited if c["ticker"] == "TRIO.ST")
    hyper = next(c for c in audited if c["ticker"] == "HYPER.US")
    dilute = next(c for c in audited if c["ticker"] == "DILUTE.ST")

    assert "DATA_DUPLICATE_WARNING" in novo["audit_flags"]
    assert "DATA_DUPLICATE_WARNING" in trio["audit_flags"]
    assert "Data kopioitunut" in novo["audit_warning"]
    assert novo.get("data_leak_quarantine") is True
    assert novo.get("status") == "REJECTED_DATA_LEAK"
    assert trio.get("data_leak_quarantine") is True
    assert trio.get("status") == "REJECTED_DATA_LEAK"

    # HYPER must have MANUAL_REVIEW_OUTLIER
    assert "MANUAL_REVIEW_OUTLIER" in hyper["audit_flags"]
    assert "Ääriarvo havaittu" in hyper["audit_warning"]
    assert hyper.get("requires_manual_review") is True

    # DILUTE must have DILUTION_WARNING_RECENT_ISSUE
    assert "DILUTION_WARNING_RECENT_ISSUE" in dilute["audit_flags"]
    assert "Merkittävä osakediluutio" in dilute["audit_warning"]


def test_email_generator_renders_audit_warning():
    """
    Test 5: EOD Action email formats audit warnings in text and HTML versions.
    """
    portfolio_mock = MagicMock()
    portfolio_mock.config.portfolio_id = "P13_Live_Candidate"

    run_trades = [
        {
            "portfolio_id": "P13_Live_Candidate",
            "action": "BUY",
            "ticker": "BEAMMW-B.ST",
            "details": {
                "sub_strategy": "satellite_nordic_microcap_catalyst",
                "shares": 500,
                "buy_price": "2.00 SEK",
                "limit_price": 2.00,
                "currency": "SEK",
                "audit_flags": ["DILUTION_WARNING_RECENT_ISSUE"],
                "audit_warning": "⚠️ HUOM: Vaatii manuaalisen tarkistuksen - Merkittävä osakediluutio/anti (> 5% kasvu)",
            },
        }
    ]

    buys, exits, tps = build_eod_action_orders(portfolio_mock, run_trades=run_trades)
    assert len(buys) == 1
    assert "DILUTION_WARNING_RECENT_ISSUE" in buys[0].audit_flags
    assert buys[0].audit_warning is not None

    summary = EODPortfolioSummary(
        portfolio_id="P13_Live_Candidate",
        regime_status="NORMAL",
        available_cash_eur=2000.0,
        total_equity_eur=7900.0,
    )

    text_email = format_eod_action_list_text(summary, buys, exits, tps)
    assert "⚠️ AUDIT-VAROITUS:" in text_email
    assert "Merkittävä osakediluutio" in text_email

    html_email = format_eod_action_list_html(summary, buys, exits, tps)
    assert "⚠️ HUOM: Vaatii manuaalisen tarkistuksen" in html_email
    assert "BEAMMW-B.ST" in html_email


def test_audit_candidates_data_roe_duplicate_quarantine():
    """
    Test 4b: Duplicate ROE across distinct tickers triggers DATA_DUPLICATE_WARNING
    and REJECTED_DATA_LEAK quarantine.
    """
    candidates = [
        {"ticker": "ROE1.HE", "roe": 0.245, "ebit_margin": 0.12, "revenue_growth": 0.15},
        {"ticker": "ROE2.ST", "roe": 0.245, "ebit_margin": 0.08, "revenue_growth": 0.20},
    ]
    audited = audit_candidates_data(candidates)
    c1 = next(c for c in audited if c["ticker"] == "ROE1.HE")
    c2 = next(c for c in audited if c["ticker"] == "ROE2.ST")
    assert "DATA_DUPLICATE_WARNING" in c1["audit_flags"]
    assert c1.get("data_leak_quarantine") is True
    assert c1.get("status") == "REJECTED_DATA_LEAK"
    assert "DATA_DUPLICATE_WARNING" in c2["audit_flags"]
    assert c2.get("data_leak_quarantine") is True
    assert c2.get("status") == "REJECTED_DATA_LEAK"


def test_audit_candidates_data_no_false_positive_on_rounded_or_distinct_4dec_pct():
    """
    TASK-00 edge cases:
    1. Two tickers with 0.0% or clean integer 10.0% (0.10) do NOT trigger false duplicate quarantine.
    2. Two tickers with 4.308% (0.04308) and 4.311% (0.04311) ebit_margin (which both round to 0.0431 as 4-dec ratio
       like MEKO.ST vs ITAB.ST) do NOT collide because audit normalizes to percentage points with 4 decimals (4.3080% vs 4.3110%).
    3. Longitudinal audit flags STALE_FINANCIAL_DATA (>120d) and REPEATED_PREV_RUN_VALUE.
    """
    candidates = [
        {"ticker": "MEKO.ST", "roe": 0.10, "ebit_margin": 0.04308, "revenue_growth": 0.0, "data_age_days": 45},
        {"ticker": "ITAB.ST", "roe": 0.10, "ebit_margin": 0.04311, "revenue_growth": 0.0, "data_age_days": 150},
    ]
    prev_snapshot = {
        "ITAB.ST": {"roe": 0.10, "ebit_margin": 0.04311, "revenue_growth": 0.0, "latest_statement_date": "2025-06-30"}
    }
    audited = audit_candidates_data(candidates, previous_run_snapshot=prev_snapshot)
    meko = next(c for c in audited if c["ticker"] == "MEKO.ST")
    itab = next(c for c in audited if c["ticker"] == "ITAB.ST")

    assert "DATA_DUPLICATE_WARNING" not in meko["audit_flags"]
    assert meko.get("data_leak_quarantine") is not True
    assert "DATA_DUPLICATE_WARNING" not in itab["audit_flags"]
    assert itab.get("data_leak_quarantine") is not True

    # ITAB has data_age_days=150 (>120d) and identical values to previous_run_snapshot
    assert "STALE_FINANCIAL_DATA" in itab["audit_flags"]
    assert "REPEATED_PREV_RUN_VALUE" in itab["audit_flags"]


def test_adjusted_cash_does_not_subtract_long_term_debt_and_diluted_shares_warrants():
    """
    TASK-12 & TASK-10 edge cases:
    1. When Current Debt is missing from balance sheet, Adjusted Cash must NOT subtract Total (Long-Term) Debt.
    2. check_share_dilution detects >5% dilution from Diluted Average Shares (options/warrants overhang).
    """
    with patch("screener.financial_metrics_engine.yf.Ticker") as mock_ticker:
        mock_inst = MagicMock()
        mock_ticker.return_value = mock_inst

        # Balance sheet has Cash=10M, Total Debt=25M, Long Term Debt=25M, NO Current Debt row
        bs = pd.DataFrame(
            {"2024-12-31": [10_000_000.0, 25_000_000.0, 25_000_000.0]},
            index=["Cash And Cash Equivalents", "Total Debt", "Long Term Debt"],
        )
        mock_inst.quarterly_balance_sheet = bs
        mock_inst.balance_sheet = bs

        cf = pd.DataFrame(
            {"2024-12-31": [-2_000_000.0]},
            index=["Operating Cash Flow"],
        )
        mock_inst.quarterly_cashflow = cf
        mock_inst.cashflow = cf

        # Income statement with Diluted Average Shares 12M vs Basic 10M (+20% warrant/option dilution)
        fin = pd.DataFrame(
            {"2024": [20_000_000.0, 12_000_000.0, 10_000_000.0], "2023": [18_000_000.0, 10_000_000.0, 10_000_000.0]},
            index=["Total Revenue", "Diluted Average Shares", "Basic Average Shares"],
        )
        mock_inst.financials = fin
        mock_inst.quarterly_financials = None
        mock_inst.info = {"sharesOutstanding": 10_000_000}

        metrics = get_hard_financials("HEALTHY_LT_DEBT")
        # Adjusted cash must remain 10M (not 10M - 25M = 0!), giving 10M / 2M = 5.0 years (60 months) runway
        assert metrics["current_debt"] == 0.0
        assert metrics["adjusted_cash"] == 10_000_000.0
        assert metrics["cash_runway_years"] == 5.0
        assert metrics["runway_status"] == "RUNWAY_OK"
        # Warrant/option overhang (+20% diluted vs basic) must be flagged
        assert metrics["has_share_dilution"] is True
        assert metrics["dilution_flag"] == "DILUTION_WARNING_RECENT_ISSUE"
