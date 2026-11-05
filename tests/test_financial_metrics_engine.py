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


@patch("screener.financial_metrics_engine.yf.Ticker")
def test_get_hard_financials_missing_quarter_nan_does_not_compare_q3_to_q2(mock_ticker):
    """
    Regression test for MIPS.ST bug:
    When quarterly_financials has a NaN in 2025-09-30 (Q3 2025), .dropna() leaves 5 rows where
    index 0 is 2026-09-30 (Q3 2026: 215M) and index 4 is 2025-06-30 (Q2 2025: 135M, 457 days apart).
    get_hard_financials MUST NOT compare Q3 against Q2 (+59.26%), and must fall back to the
    verified annual comparison (533M vs 483M = +10.352%).
    """
    import numpy as np
    mock_inst = MagicMock()
    mock_ticker.return_value = mock_inst

    bs = pd.DataFrame(
        {"2026-09-30": [200_000_000.0, 20_000_000.0]},
        index=["Cash And Cash Equivalents", "Total Debt"],
    )
    mock_inst.quarterly_balance_sheet = bs
    mock_inst.balance_sheet = bs

    cf = pd.DataFrame({"2026-09-30": [100_000_000.0]}, index=["Operating Cash Flow"])
    mock_inst.quarterly_cashflow = cf
    mock_inst.cashflow = cf

    qf = pd.DataFrame(
        {
            "2026-09-30": [215_000_000.0],
            "2026-06-30": [232_000_000.0],
            "2026-03-31": [151_000_000.0],
            "2025-12-31": [147_000_000.0],
            "2025-09-30": [np.nan],         # Missing Q3 2025!
            "2025-06-30": [135_000_000.0],  # Q2 2025 (457 days before 2026-09-30)
        },
        index=["Total Revenue"],
    )
    af = pd.DataFrame(
        {
            "2025-12-31": [533_000_000.0],
            "2024-12-31": [483_000_000.0],
        },
        index=["Total Revenue"],
    )
    mock_inst.quarterly_financials = qf
    mock_inst.financials = af
    mock_inst.info = {}

    metrics = get_hard_financials("MIPS.ST")
    # Must NEVER compare Q3 2026 (215M) against Q2 2025 (135M = +59.2593%)!
    # Instead, must find date-verified Q2 2026 vs Q2 2025 (232M vs 135M = +71.8519%) AND reconcile 4Q TTM (745M) vs FY2025 (533M)
    assert metrics["revenue_growth_yoy_pct"] != pytest.approx(59.2593, abs=0.05)
    assert metrics["revenue_growth_yoy_pct"] == pytest.approx(71.8519, abs=0.01)
    assert metrics["revenue_ttm_4q"] == pytest.approx(745_000_000.0)
    assert metrics["revenue_annual_latest"] == pytest.approx(533_000_000.0)
    assert "TTM_ACCELERATION_OR_MA_DIVERGENCE" in metrics["audit_flags"]


@patch("screener.financial_metrics_engine.yf.Ticker")
def test_ttm_vs_annual_mismatch_error_same_date(mock_ticker):
    """
    When the 4-quarter sum (e.g. 600M) ends on the exact same period end date (2025-12-31)
    as the annual income statement (500M, >5% difference), get_hard_financials must flag
    TTM_ANNUAL_MISMATCH_ERROR.
    """
    mock_inst = MagicMock()
    mock_ticker.return_value = mock_inst
    bs = pd.DataFrame(
        {"2025-12-31": [100_000_000.0, 10_000_000.0]},
        index=["Cash And Cash Equivalents", "Total Debt"],
    )
    mock_inst.quarterly_balance_sheet = bs
    mock_inst.balance_sheet = bs
    cf = pd.DataFrame({"2025-12-31": [50_000_000.0]}, index=["Operating Cash Flow"])
    mock_inst.quarterly_cashflow = cf
    mock_inst.cashflow = cf

    qf = pd.DataFrame(
        {
            "2025-12-31": [150_000_000.0],
            "2025-09-30": [150_000_000.0],
            "2025-06-30": [150_000_000.0],
            "2025-03-31": [150_000_000.0],  # 4Q sum = 600M
        },
        index=["Total Revenue"],
    )
    af = pd.DataFrame(
        {
            "2025-12-31": [500_000_000.0],  # Annual on same date = 500M (+20% mismatch!)
            "2024-12-31": [450_000_000.0],
        },
        index=["Total Revenue"],
    )
    mock_inst.quarterly_financials = qf
    mock_inst.financials = af
    mock_inst.info = {}

    metrics = get_hard_financials("BAD_SUM.ST")
    assert "TTM_ANNUAL_MISMATCH_ERROR" in metrics["audit_flags"]
    assert any("TTM_ANNUAL_MISMATCH_ERROR" in e for e in metrics["errors"])


def test_calculate_candidate_score_bounds_monotonicity_sensitivity_and_neglected_satellite():
    """
    Verifies the 4 mandatory mathematical guarantees of calculate_candidate_score():
    1. Bounds [0.0, 100.0]: Never < 0 or > 100 even with extreme inputs + insider bonus.
    2. Monotonicity: Higher revenue growth never decreases score; higher Net Debt/EBITDA never increases score.
    3. Sensitivity: Changing revenue_growth_yoy_pct strictly changes the Core score (prevents dead growth parameter).
    4. Neglected Profitable Satellite: Profitable small-cap with low analyst coverage outscores unprofitable hype stock,
       and >6 analysts is rejected by matches_nordic_microcap_catalyst.
    """
    from screener.financial_metrics_engine import calculate_candidate_score
    from screener.tiered_filters import matches_nordic_microcap_catalyst

    # 1. Bounds [0.0, 100.0]
    s_min, bd_min = calculate_candidate_score(
        "WORST.HE",
        {
            "roe": -5.0,
            "roic": -3.0,
            "ebit_margin": -2.0,
            "revenue_growth_yoy_pct": -95.0,
            "net_debt_to_ebitda": 50.0,
            "current_ev_ebit": 500.0,
            "ev_ebit_premium_pct": 400.0,
            "fcf_yield_pct": -50.0,
        },
        universe_filter="Nordic_MidCap_Compounder",
    )
    assert 0.0 <= s_min <= 100.0
    assert s_min == 0.0

    s_max, bd_max = calculate_candidate_score(
        "BEST.HE",
        {
            "roe": 95.0,
            "roic": 90.0,
            "ebit_margin": 80.0,
            "revenue_growth_yoy_pct": 500.0,
            "revenue_cagr_3y_pct": 300.0,
            "net_debt_to_ebitda": -10.0,
            "current_ev_ebit": 2.0,
            "ev_ebit_premium_pct": -90.0,
            "fcf_yield_pct": 50.0,
            "insider_buying_signal": True,
        },
        universe_filter="Nordic_MidCap_Compounder",
    )
    assert 0.0 <= s_max <= 100.0
    assert s_max == 100.0

    # 2 & 3. Monotonicity and Sensitivity to Revenue Growth & Debt in Core
    base_hf = {
        "roe": 0.25,
        "roic": 0.22,
        "ebit_margin": 0.20,
        "revenue_growth_yoy_pct": 5.0,
        "net_debt_to_ebitda": 1.0,
        "current_ev_ebit": 14.0,
        "ev_ebit_premium_pct": 0.0,
        "fcf_yield_pct": 5.0,
    }
    s_g5, bd_g5 = calculate_candidate_score("CORE.HE", dict(base_hf, revenue_growth_yoy_pct=5.0), universe_filter="Nordic_MidCap_Compounder")
    s_g25, bd_g25 = calculate_candidate_score("CORE.HE", dict(base_hf, revenue_growth_yoy_pct=25.0), universe_filter="Nordic_MidCap_Compounder")
    assert s_g25 > s_g5
    assert bd_g25["growth_pts"] > bd_g5["growth_pts"]

    s_d0, bd_d0 = calculate_candidate_score("CORE.HE", dict(base_hf, net_debt_to_ebitda=0.0), universe_filter="Nordic_MidCap_Compounder")
    s_d2, bd_d2 = calculate_candidate_score("CORE.HE", dict(base_hf, net_debt_to_ebitda=2.0), universe_filter="Nordic_MidCap_Compounder")
    assert s_d0 > s_d2
    assert bd_d0["balance_sheet_pts"] > bd_d2["balance_sheet_pts"]

    # 4. Neglected Profitable Small-Cap (Novotek-like) vs Unprofitable Hype (Nordtech-like)
    s_prof, _ = calculate_candidate_score(
        "NTEK-B.ST",
        {
            "roe": 0.175,
            "roic": 0.366,
            "ebit_margin": 0.08,
            "revenue_growth_yoy_pct": 12.1,
            "net_debt_to_ebitda": 0.0,
            "fcf_yield_pct": 5.0,
            "analyst_count": 1,
            "is_profile_b": True,
        },
        universe_filter="Nordic_MicroCap_Catalyst",
    )
    s_unprof, _ = calculate_candidate_score(
        "NTECH.ST",
        {
            "roe": -0.009,
            "roic": 0.015,
            "ebit_margin": 0.009,
            "revenue_growth_yoy_pct": 49.9,
            "net_debt_to_ebitda": 0.0,
            "fcf_yield_pct": 1.0,
            "analyst_count": 6,
            "is_profile_a": True,
        },
        universe_filter="Nordic_MicroCap_Catalyst",
    )
    assert s_prof > s_unprof

    # Over-covered small-cap (> 6 analysts) is rejected by Neglect Filter
    passed_crowded, reasons_crowded = matches_nordic_microcap_catalyst(
        "CROWDED.HE",
        {"is_profile_b": True, "cash_runway_months": "Infinite", "analyst_count": 8},
        {"market": "FI", "market_cap_eur": 50_000_000.0, "currency": "EUR", "adv_20d_local": 50_000.0},
    )
    assert passed_crowded is False
    assert any("REJECTED_NOT_NEGLECTED" in r for r in reasons_crowded)



def test_missing_analyst_count_gets_zero_neglect_bonus_and_ma_dilution_gate():
    """
    Verifies:
    1. Missing analyst_count (None in Yahoo) gets 0.0 neglect_pts (never treated as 0 analysts = +6.0 pts).
    2. Verified 0..2 analysts gets +6.0 neglect_pts.
    3. M&A divergence (>25% TTM vs Annual) + share dilution (>5%) caps growth points, applies -8.0 dilution penalty,
       sets manual_review_required=True, and is rejected from automatic Core entry by matches_nordic_midcap_compounder.
    """
    from screener.financial_metrics_engine import calculate_candidate_score
    from screener.tiered_filters import matches_nordic_midcap_compounder

    sat_base = {
        "roe": 0.18,
        "roic": 0.16,
        "ebit_margin": 0.12,
        "revenue_growth_yoy_pct": 10.0,
        "net_debt_to_ebitda": 0.0,
        "fcf_yield_pct": 5.0,
        "is_profile_b": True,
    }
    s_missing, bd_missing = calculate_candidate_score(
        "UNVERIFIED.HE",
        dict(sat_base, analyst_count=None),
        universe_filter="Nordic_MicroCap_Catalyst",
    )
    s_verified_1, bd_verified_1 = calculate_candidate_score(
        "NEGLECTED.HE",
        dict(sat_base, analyst_count=1),
        universe_filter="Nordic_MicroCap_Catalyst",
    )
    assert bd_missing["neglect_pts"] == 0.0
    assert bd_verified_1["neglect_pts"] == 6.0
    assert s_verified_1 == pytest.approx(s_missing + 6.0, abs=0.01)

    # M&A + Dilution Gate (MILDEF.ST-like case)
    mildef_hf = {
        "roe": 0.20,
        "roic": 0.12,
        "ebit_margin": 0.18,
        "revenue_growth_yoy_pct": 65.0,
        "revenue_cagr_3y_pct": 40.0,
        "free_cash_flow": 100_000_000.0,
        "net_debt_to_ebitda": 1.0,
        "current_ev_ebit": 20.0,
        "ev_ebit_5y_median": 25.0,
        "ev_ebit_premium_pct": -20.0,
        "has_share_dilution": True,
        "audit_flags": ["TTM_ACCELERATION_OR_MA_DIVERGENCE", "DILUTION_WARNING_RECENT_ISSUE"],
    }
    s_ma, bd_ma = calculate_candidate_score("MILDEF.ST", mildef_hf, universe_filter="Nordic_MidCap_Compounder")
    assert bd_ma["growth_pts"] <= 12.0  # Capped from 20.0 due to unverified M&A divergence
    assert bd_ma["dilution_penalty_pts"] == -8.0
    assert bd_ma["manual_review_required"] is True

    passed_core, reasons_core = matches_nordic_midcap_compounder(
        "MILDEF.ST",
        mildef_hf,
        {"market": "SE", "market_cap_eur": 800_000_000.0, "currency": "SEK", "adv_20d_local": 5_000_000.0},
    )
    assert passed_core is False
    assert any("REJECTED_MA_DILUTION_MANUAL_REVIEW" in r for r in reasons_core)


@patch("screener.financial_metrics_engine.yf.Ticker")
def test_roic_formula_net_cash_does_not_shrink_invested_capital(mock_ticker):
    """
    Regression test for ROIC denominator:
    Invested Capital = Stockholders' Equity + max(0.0, Total_Debt - Cash).
    When Cash > Total_Debt (net cash), excess cash MUST NOT be subtracted from Equity!
    """
    mock_inst = MagicMock()
    mock_ticker.return_value = mock_inst

    # Equity = 100M, Debt = 10M, Cash = 60M (Net Cash = 50M), EBIT = 20M -> NOPAT = 20M * (1 - 0.206) = 15.88M
    # Correct Invested Capital = 100M + max(0, 10M - 60M) = 100M -> ROIC = 15.88% (0.1588)
    # (Old buggy formula subtracted 50M net cash from 100M equity -> 50M IC -> 31.76% ROIC!)
    bs = pd.DataFrame(
        {"2026-06-30": [60_000_000.0, 10_000_000.0, 100_000_000.0]},
        index=["Cash And Cash Equivalents", "Total Debt", "Stockholders Equity"],
    )
    mock_inst.quarterly_balance_sheet = bs
    mock_inst.balance_sheet = bs

    cf = pd.DataFrame({"2026-06-30": [15_000_000.0]}, index=["Operating Cash Flow"])
    mock_inst.quarterly_cashflow = cf
    mock_inst.cashflow = cf

    inc = pd.DataFrame(
        {
            "2026-06-30": [100_000_000.0, 20_000_000.0, 16_000_000.0],
            "2025-06-30": [90_000_000.0, 18_000_000.0, 14_000_000.0],
        },
        index=["Total Revenue", "Operating Income", "Net Income"],
    )
    mock_inst.financials = inc
    mock_inst.quarterly_financials = None
    mock_inst.info = {}

    res_net_cash = get_hard_financials("CASHRICH.ST")
    assert res_net_cash["roic"] == pytest.approx(0.1588, abs=0.0005)

    # Increasing cash from 60M to 90M must NOT change ROIC (since net debt remains 0)
    bs_more_cash = pd.DataFrame(
        {"2026-06-30": [90_000_000.0, 10_000_000.0, 100_000_000.0]},
        index=["Cash And Cash Equivalents", "Total Debt", "Stockholders Equity"],
    )
    mock_inst.quarterly_balance_sheet = bs_more_cash
    mock_inst.balance_sheet = bs_more_cash
    res_more_cash = get_hard_financials("CASHRICH.ST")
    assert res_more_cash["roic"] == pytest.approx(res_net_cash["roic"], abs=0.0001)


def test_dual_class_ab_shares_no_false_dilution_and_satellite_ma_rollup_haircut():
    """
    Verifies:
    1. check_share_dilution() does not trigger a false positive when Yahoo reports B-shares in
       `sharesOutstanding` (9.12M) and A+B total shares in `impliedSharesOutstanding` (10.56M) /
       `Basic Average Shares` (10.56M) (CEVI.ST dual-class share regression).
    2. Satellite scoring (`Nordic_MicroCap_Catalyst`) caps growth points to <= 3.5 pts for unprofitable
       M&A roll-up companies (`NTECH.ST` with +56.5% growth, ROE -0.9%, EV/EBIT 145.8x).
    """
    from screener.financial_metrics_engine import check_share_dilution, calculate_candidate_score

    inc_a = pd.DataFrame(
        {
            "2025-12-31": [10_555_000.0, 10_555_000.0],
            "2024-12-31": [10_555_000.0, 10_555_000.0],
        },
        index=["Basic Average Shares", "Diluted Average Shares"],
    )
    bs_a = pd.DataFrame(
        {
            "2025-12-31": [10_555_000.0],
            "2024-12-31": [10_555_000.0],
        },
        index=["Ordinary Shares Number"],
    )
    info_cevi = {
        "sharesOutstanding": 9_116_680,         # B-shares only
        "impliedSharesOutstanding": 10_555_000, # Total A+B shares
    }
    dil_res = check_share_dilution(t=None, info=info_cevi, bs_a=bs_a, inc_a=inc_a)
    assert dil_res["has_share_dilution"] is False
    assert dil_res["share_dilution_pct"] == pytest.approx(0.0, abs=0.01)

    # Satellite M&A Roll-up Haircut (NTECH.ST-like case)
    ntech_hf = {
        "roe": -0.009,
        "roic": 0.015,
        "ebit_margin": 0.033,
        "revenue_growth_yoy_pct": 49.9,
        "revenue_growth_annual_yoy_pct": 71.7,
        "revenue_cagr_3y_pct": 50.0,
        "current_ev_ebit": 145.8,
        "net_debt_to_ebitda": 0.0,
        "fcf_yield_pct": 4.2,
        "analyst_count": None,
        "is_profile_b": True,
    }
    s_ntech, bd_ntech = calculate_candidate_score("NTECH.ST", ntech_hf, universe_filter="Nordic_MicroCap_Catalyst")
    assert bd_ntech["growth_pts"] <= 3.5
    assert bd_ntech["manual_review_required"] is True


def test_calculate_relative_ev_ebit_auto_adjust_false_mock():
    """
    Regression test for PUUILO.HE / BIOG-B.ST historical 5y EV/EBIT median:
    Verifies that calculate_relative_ev_ebit() requests t.history(period='5y', auto_adjust=False)
    so past dividends are NOT subtracted from historical stock prices.
    """
    from screener.financial_metrics_engine import calculate_relative_ev_ebit

    mock_t = MagicMock()

    def fake_history(period="5y", auto_adjust=True):
        # If auto_adjust=True (wrong), historical price is 6.00 (shrunk by dividends -> EV/EBIT = 12.0x)
        # If auto_adjust=False (correct), historical price is 8.00 (actual point-in-time -> EV/EBIT = 16.0x)
        px = 8.00 if auto_adjust is False else 6.00
        idx = pd.to_datetime(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"])
        return pd.DataFrame({"Close": [px, px, px, px]}, index=idx)

    mock_t.history.side_effect = fake_history
    inc_a = pd.DataFrame(
        {
            "2025-12-31": [50_000_000.0],
            "2024-12-31": [50_000_000.0],
            "2023-12-31": [50_000_000.0],
            "2022-12-31": [50_000_000.0],
        },
        index=["Operating Income"],
    )
    bs_a = pd.DataFrame(
        {
            "2025-12-31": [0.0, 0.0],
            "2024-12-31": [0.0, 0.0],
            "2023-12-31": [0.0, 0.0],
            "2022-12-31": [0.0, 0.0],
        },
        index=["Total Debt", "Cash And Cash Equivalents"],
    )
    info = {"sharesOutstanding": 100_000_000, "currentPrice": 11.0, "marketCap": 1_100_000_000}
    res = calculate_relative_ev_ebit(
        t=mock_t,
        info=info,
        bs_a=bs_a,
        inc_a=inc_a,
        current_ev=1_100_000_000.0,
        current_ebit=50_000_000.0,  # Current EV/EBIT = 22.0x
    )
    mock_t.history.assert_called_with(period="5y", auto_adjust=False)
    # With unadjusted price 8.00 * 100M shares / 50M EBIT -> 5y median MUST be 16.0x (not 12.0x!)
    assert res["ev_ebit_5y_median"] == pytest.approx(16.0, abs=0.01)
    assert res["ev_ebit_premium_pct"] == pytest.approx(37.5, abs=0.01)


@patch("screener.financial_metrics_engine.yf.Ticker")
def test_latest_annual_ebit_row_fallback_and_oldest_statement_data_age_days(mock_ticker):
    """
    Verifies:
    1. When Operating Income is NaN in the latest annual column (2026-01-31) and 70.6M in 2025-01-31,
       while EBIT is 75.0M in 2026-01-31 (PUUILO.HE regression), get_hard_financials extracts 75.0M
       from the latest column (2026-01-31), NOT 70.6M from the previous year!
    2. When quarterly_balance_sheet is fresh (e.g. 60 days old) but the income statement used is
       ANNUAL_LATEST (e.g. 240 days old), data_age_days reports the OLDEST used statement (240 days)
       and flags STALE_FINANCIAL_DATA.
    """
    import numpy as np
    from datetime import datetime, timezone, timedelta

    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh_bs_date = (now_dt - timedelta(days=60)).strftime("%Y-%m-%d")
    stale_inc_date = (now_dt - timedelta(days=240)).strftime("%Y-%m-%d")
    prev_inc_date = (now_dt - timedelta(days=605)).strftime("%Y-%m-%d")

    mock_inst = MagicMock()
    mock_ticker.return_value = mock_inst

    bs_q = pd.DataFrame(
        {fresh_bs_date: [50_000_000.0, 100_000_000.0, 100_000_000.0]},
        index=["Cash And Cash Equivalents", "Total Debt", "Stockholders Equity"],
    )
    mock_inst.quarterly_balance_sheet = bs_q
    mock_inst.balance_sheet = bs_q

    cf_a = pd.DataFrame({stale_inc_date: [80_000_000.0, -10_000_000.0]}, index=["Operating Cash Flow", "Capital Expenditure"])
    mock_inst.quarterly_cashflow = pd.DataFrame()
    mock_inst.cashflow = cf_a

    inc_a = pd.DataFrame(
        {
            stale_inc_date: [451_400_000.0, np.nan, 75_000_000.0, 58_000_000.0],
            prev_inc_date: [390_000_000.0, 70_600_000.0, 70_600_000.0, 50_000_000.0],
        },
        index=["Total Revenue", "Operating Income", "EBIT", "Net Income"],
    )
    mock_inst.quarterly_financials = pd.DataFrame()
    mock_inst.financials = inc_a
    mock_inst.info = {"enterpriseValue": 1_650_000_000.0, "marketCap": 1_600_000_000.0}

    res = get_hard_financials("PUUILO.HE")
    # Must pick 75.0M EBIT from latest annual column, NOT 70.6M from previous year!
    assert res["ebit_ttm_or_unified"] == pytest.approx(75_000_000.0)
    assert res["current_ev_ebit"] == pytest.approx(1_650_000_000.0 / 75_000_000.0, abs=0.02)
    # Must report data_age_days from the OLDEST used statement (~240 days), NOT fresh_bs_date (60 days)!
    assert res["statement_ages_days"]["balance_sheet"] == pytest.approx(60, abs=1)
    assert res["statement_ages_days"]["income_statement"] == pytest.approx(240, abs=1)
    assert res["data_age_days"] == pytest.approx(240, abs=1)
    assert "STALE_FINANCIAL_DATA" in res["audit_flags"]


def test_stale_financial_data_flag_blocks_buy_in_core_and_satellite():
    """
    Verifies that STALE_FINANCIAL_DATA (or data_age_days > 120) blocks automatic PASS
    in both matches_nordic_midcap_compounder and matches_nordic_microcap_catalyst.
    """
    from screener.tiered_filters import matches_nordic_midcap_compounder, matches_nordic_microcap_catalyst

    meta_core = {"market": "SE", "market_cap_eur": 500_000_000.0, "currency": "SEK", "adv_20d_local": 5_000_000.0}
    hf_stale_core = {
        "roe": 0.26,
        "roic": 0.26,
        "ebit_margin": 0.21,
        "free_cash_flow": 300_000_000.0,
        "net_debt_to_ebitda": 0.0,
        "current_ev_ebit": 13.3,
        "ev_ebit_5y_median": 11.8,
        "ev_ebit_premium_pct": 12.7,
        "data_age_days": 128,
        "oldest_statement_date": "2026-06-30",
        "audit_flags": ["STALE_FINANCIAL_DATA"],
    }
    passed_c, reasons_c = matches_nordic_midcap_compounder("RVRC.ST", hf_stale_core, meta_core)
    assert passed_c is False
    assert any("REJECTED_STALE_DATA" in r for r in reasons_c)

    meta_sat = {"market": "SE", "market_cap_eur": 60_000_000.0, "currency": "SEK", "adv_20d_local": 500_000.0}
    hf_stale_sat = {
        "roe": 0.16,
        "ebit_margin": 0.10,
        "is_profile_b": True,
        "cash_runway_months": "Infinite",
        "data_age_days": 128,
        "oldest_statement_date": "2026-06-30",
        "audit_flags": ["STALE_FINANCIAL_DATA"],
    }
    passed_s, reasons_s = matches_nordic_microcap_catalyst("NTEK-B.ST", hf_stale_sat, meta_sat)
    assert passed_s is False
    assert any("REJECTED_STALE_DATA" in r for r in reasons_s)


def test_negative_ebit_never_produces_positive_ev_ebit_and_ssh1v_rejected():
    """
    Regression test for SSH1V.HE:
    1. When current_ebit <= 0 (e.g. -2.2M), calculate_relative_ev_ebit() must NEVER fall back
       to positive info['ebitda'] or info['enterpriseToEbitda'] (which produced EV/EBIT = 393.93x!).
       Instead, current_ev_ebit must be None and valuation_status = 'REJECTED_NEGATIVE_EBIT'.
    2. SSH1V.HE-like candidate (ROE -47.5%, EBIT -10.1%, share dilution > 5%) must be rejected by
       matches_nordic_microcap_catalyst on dilution, profitability brake, and unprofitable status.
    """
    from screener.financial_metrics_engine import calculate_relative_ev_ebit
    from screener.tiered_filters import matches_nordic_microcap_catalyst

    rel_ssh = calculate_relative_ev_ebit(
        t=None,
        info={"enterpriseValue": 98_000_000.0, "ebitda": 250_000.0, "enterpriseToEbitda": 393.93},
        current_ev=98_000_000.0,
        current_ebit=-2_200_000.0,  # Negative EBIT!
    )
    assert rel_ssh["current_ev_ebit"] is None
    assert rel_ssh["valuation_status"] == "REJECTED_NEGATIVE_EBIT"

    meta_ssh = {"market": "FI", "market_cap_eur": 99_000_000.0, "currency": "EUR", "adv_20d_local": 25_000.0}
    hf_ssh = {
        "roe": -0.475,
        "roic": -0.36,
        "ebit_margin": -0.101,
        "is_profile_b": True,
        "cash_runway_months": "Infinite",
        "has_share_dilution": True,
        "dilution_flag": "DILUTION_WARNING_RECENT_ISSUE",
        "valuation_status": "REJECTED_NEGATIVE_EBIT",
        "audit_flags": ["DILUTION_WARNING_RECENT_ISSUE"],
    }
    passed_ssh, reasons_ssh = matches_nordic_microcap_catalyst("SSH1V.HE", hf_ssh, meta_ssh)
    assert passed_ssh is False
    assert any("REJECTED_DILUTION" in r for r in reasons_ssh)
    assert any("REJECTED_PROFITABILITY_BRAKE" in r for r in reasons_ssh)
    assert any("REJECTED_UNPROFITABLE" in r for r in reasons_ssh)
