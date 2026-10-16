"""
tests/test_tiered_filters.py - Comprehensive Unit Tests for Tiered Market-Cap & Quality Filters.
Tests:
1. Nordic_MidCap_Compounder (Core)
2. US_MicroCap_Growth (Satellite 1)
3. Nordic_MicroCap_Catalyst (Satellite 2)
"""

import pytest
from screener.tiered_filters import (
    matches_nordic_midcap_compounder,
    matches_us_microcap_growth,
    matches_nordic_microcap_catalyst,
    matches_universe_filter,
    get_market_cap_in_eur,
    get_market_cap_in_usd,
)


def test_nordic_midcap_compounder_valid():
    """Valid Nordic MidCap Compounder meeting all criteria must pass."""
    meta = {
        "market": "FI",
        "market_cap_eur": 500_000_000.0,
        "adv_20d_local": 150_000.0,
        "currency": "EUR",
    }
    hard_facts = {
        "roe": 0.18,  # 18% > 15%
        "ebit_margin": 0.15,  # 15% > 12%
        "free_cash_flow": 25_000_000.0,  # FCF > 0
        "net_debt_to_ebitda": 1.2,  # < 2.5
        "net_debt": 30_000_000.0,
        "ebitda": 25_000_000.0,
    }
    passed, reasons = matches_nordic_midcap_compounder("HARVIA.HE", hard_facts, meta)
    assert passed is True
    assert len(reasons) == 0


def test_nordic_midcap_compounder_rejects_microcap():
    """Micro-caps (< 150M EUR) must be rejected from Core tier."""
    meta = {
        "market": "FI",
        "market_cap_eur": 80_000_000.0,  # < 150M EUR
        "currency": "EUR",
    }
    hard_facts = {
        "roe": 0.20,
        "ebit_margin": 0.16,
        "free_cash_flow": 5_000_000.0,
        "net_debt_to_ebitda": 0.5,
    }
    passed, reasons = matches_nordic_midcap_compounder("VIAFIN.HE", hard_facts, meta)
    assert passed is False
    assert any("150M EUR" in r for r in reasons)


def test_nordic_midcap_compounder_rejects_megacap():
    """Stagnant mega-caps (> 2B EUR) like Nokia/Fortum must be rejected from Core tier."""
    meta = {
        "market": "FI",
        "market_cap_eur": 25_000_000_000.0,  # 25B EUR > 2B EUR
        "currency": "EUR",
    }
    hard_facts = {
        "roe": 0.16,
        "ebit_margin": 0.14,
        "free_cash_flow": 1_000_000_000.0,
        "net_debt_to_ebitda": 1.0,
    }
    passed, reasons = matches_nordic_midcap_compounder("NOKIA.HE", hard_facts, meta)
    assert passed is False
    assert any("2,000M EUR" in r for r in reasons)


def test_nordic_midcap_compounder_rejects_non_nordic():
    """Non-Nordic equities must be rejected."""
    meta = {"market": "US", "market_cap_eur": 300_000_000.0}
    hard_facts = {"roe": 0.20, "ebit_margin": 0.20, "free_cash_flow": 10_000_000.0}
    passed, reasons = matches_nordic_midcap_compounder("AAPL", hard_facts, meta)
    assert passed is False
    assert any("Not a Nordic equity" in r for r in reasons)


def test_nordic_midcap_compounder_rejects_failing_fundamentals():
    """Rejects if ROE <= 15%, EBIT margin <= 12%, FCF <= 0, or Net Debt/EBITDA >= 2.5."""
    meta = {"market": "SE", "market_cap_eur": 400_000_000.0, "currency": "SEK"}
    
    # Low ROE
    passed, reasons = matches_nordic_midcap_compounder(
        "FAIL.ST",
        {"roe": 0.10, "ebit_margin": 0.16, "free_cash_flow": 10_000_000.0, "net_debt_to_ebitda": 1.0},
        meta,
    )
    assert passed is False
    assert any("ROE" in r for r in reasons)

    # Low EBIT margin
    passed, reasons = matches_nordic_midcap_compounder(
        "FAIL.ST",
        {"roe": 0.18, "ebit_margin": 0.08, "free_cash_flow": 10_000_000.0, "net_debt_to_ebitda": 1.0},
        meta,
    )
    assert passed is False
    assert any("EBIT Margin" in r for r in reasons)

    # Negative FCF
    passed, reasons = matches_nordic_midcap_compounder(
        "FAIL.ST",
        {"roe": 0.18, "ebit_margin": 0.15, "free_cash_flow": -2_000_000.0, "net_debt_to_ebitda": 1.0},
        meta,
    )
    assert passed is False
    assert any("Free Cash Flow" in r for r in reasons)

    # High Net Debt / EBITDA
    passed, reasons = matches_nordic_midcap_compounder(
        "FAIL.ST",
        {"roe": 0.18, "ebit_margin": 0.15, "free_cash_flow": 5_000_000.0, "net_debt_to_ebitda": 3.2},
        meta,
    )
    assert passed is False
    assert any("Net Debt/EBITDA" in r for r in reasons)


def test_us_microcap_growth_valid():
    """Valid US MicroCap Growth meeting all OST rules must pass."""
    meta = {
        "market": "US",
        "market_cap_usd": 150_000_000.0,
        "exchange": "NASDAQ",
        "currency": "USD",
    }
    hard_facts = {
        "exchange": "NASDAQ",
        "dividend_yield": 0.0,
        "revenue_growth_yoy_pct": 24.5,
        "is_profile_a": True,
    }
    passed, reasons = matches_us_microcap_growth("IDN", hard_facts, meta)
    assert passed is True
    assert len(reasons) == 0


def test_us_microcap_growth_rejects_otc():
    """OTC and Pink Sheet equities must be rejected."""
    meta = {
        "market": "US",
        "market_cap_usd": 50_000_000.0,
        "exchange": "OTC",
    }
    hard_facts = {
        "exchange": "OTC",
        "dividend_yield": 0.0,
        "revenue_growth_yoy_pct": 30.0,
    }
    passed, reasons = matches_us_microcap_growth("SHADY", hard_facts, meta)
    assert passed is False
    assert any("Disallowed exchange" in r for r in reasons)


def test_us_microcap_growth_rejects_dividends_on_ost():
    """Dividends are strictly forbidden on OST for US stocks due to unrecoverable withholding tax."""
    meta = {
        "market": "US",
        "market_cap_usd": 100_000_000.0,
        "exchange": "NASDAQ",
    }
    hard_facts = {
        "exchange": "NASDAQ",
        "dividend_yield": 0.025,  # 2.5% dividend yield
        "revenue_growth_yoy_pct": 20.0,
    }
    passed, reasons = matches_us_microcap_growth("DIVSTOCK", hard_facts, meta)
    assert passed is False
    assert any("Dividend yield" in r and "Forbidden on OST" in r for r in reasons)


def test_us_microcap_growth_market_cap_bounds():
    """Must be between 30M USD and 350M USD."""
    meta_small = {"market": "US", "market_cap_usd": 20_000_000.0, "exchange": "NASDAQ"}
    hard_facts = {"exchange": "NASDAQ", "dividend_yield": 0.0, "revenue_growth_yoy_pct": 25.0}
    passed, reasons = matches_us_microcap_growth("TINY", hard_facts, meta_small)
    assert passed is False
    assert any("< 30M USD" in r for r in reasons)

    meta_large = {"market": "US", "market_cap_usd": 500_000_000.0, "exchange": "NASDAQ"}
    passed, reasons = matches_us_microcap_growth("BIG", hard_facts, meta_large)
    assert passed is False
    assert any("> 350M USD" in r for r in reasons)


def test_nordic_microcap_catalyst_valid():
    """Valid Nordic microcap quality (e.g. Viafin) or turnaround must pass."""
    meta = {
        "market": "FI",
        "market_cap_eur": 80_000_000.0,  # 15M <= MCAP < 150M
        "currency": "EUR",
        "adv_20d_local": 45_000.0,
    }
    hard_facts = {
        "is_profile_b": True,
        "cash_runway_months": "Infinite",
    }
    passed, reasons = matches_nordic_microcap_catalyst("VIAFIN.HE", hard_facts, meta)
    assert passed is True
    assert len(reasons) == 0


def test_nordic_microcap_catalyst_turnaround_valid():
    """Turnaround with verified NLP catalyst, runway >= 12m, and EUR ADV >= 20k must pass."""
    meta = {
        "market": "SE",
        "market_cap_eur": 45_000_000.0,
        "currency": "SEK",
        "adv_20d_local": 300_000.0,  # ~26,500 EUR >= 20k EUR
    }
    hard_facts = {
        "is_profile_a": False,
        "is_profile_b": False,
        "cash_runway_months": 15.0,  # >= 12m
    }
    passed, reasons = matches_nordic_microcap_catalyst(
        "TURN.ST", hard_facts, meta, nlp_verdict="WATCH_TURNAROUND"
    )
    assert passed is True


def test_nordic_microcap_catalyst_rejects_low_sek_liquidity():
    """Stock on Stockholm exchange with 30,000 SEK ADV (~2,650 EUR) must be rejected."""
    meta = {
        "market": "SE",
        "market_cap_eur": 45_000_000.0,
        "currency": "SEK",
        "adv_20d_local": 30_000.0,  # ~2,654 EUR < 20,000 EUR
    }
    hard_facts = {
        "is_profile_b": True,
        "cash_runway_months": "Infinite",
    }
    passed, reasons = matches_nordic_microcap_catalyst("SEKFAIL.ST", hard_facts, meta)
    assert passed is False
    assert any("< 20,000 EUR threshold" in r for r in reasons)


def test_nordic_microcap_catalyst_rejects_missing_runway_negative_fcf():
    """Turnaround candidate with cash_runway_months = None and FCF <= 0 must be rejected."""
    meta = {
        "market": "FI",
        "market_cap_eur": 30_000_000.0,
        "currency": "EUR",
        "adv_20d_local": 50_000.0,
    }
    hard_facts = {
        "is_profile_a": False,
        "is_profile_b": False,
        "cash_runway_months": None,
        "free_cash_flow": -1_000_000.0,
        "ebit": -500_000.0,
    }
    passed, reasons = matches_nordic_microcap_catalyst(
        "CRISIS.HE", hard_facts, meta, nlp_verdict="WATCH_TURNAROUND"
    )
    assert passed is False
    assert any("cash runway" in r and "insufficient/missing" in r for r in reasons)


def test_us_microcap_growth_rejects_pure_hype_without_viability():
    """US stock relying only on NLP verdict without revenue or runway/FCF must be rejected."""
    meta = {
        "market": "US",
        "market_cap_usd": 100_000_000.0,
        "exchange": "NASDAQ",
    }
    hard_facts = {
        "exchange": "NASDAQ",
        "dividend_yield": 0.0,
        "revenue_growth_yoy_pct": -10.0,  # shrinking
        "is_profile_a": False,
        "revenue": 0.0,  # zero revenue
        "cash_runway_months": 2.0,
        "free_cash_flow": -5_000_000.0,
    }
    passed, reasons = matches_us_microcap_growth(
        "HYPE", hard_facts, meta, nlp_verdict="STRONG BUY"
    )
    assert passed is False
    assert any("basic viability" in r for r in reasons)


def test_nordic_microcap_catalyst_rejects_bounds():
    """Must be between 15M EUR and 150M EUR."""
    # Too small (< 15M EUR)
    meta_tiny = {"market": "FI", "market_cap_eur": 8_000_000.0, "currency": "EUR", "adv_20d_local": 50_000.0}
    hard_facts = {"is_profile_b": True}
    passed, reasons = matches_nordic_microcap_catalyst("TINY.HE", hard_facts, meta_tiny)
    assert passed is False
    assert any("< 15M EUR" in r for r in reasons)

    # Too big (>= 150M EUR) -> belongs to MidCap Core
    meta_mid = {"market": "FI", "market_cap_eur": 200_000_000.0, "currency": "EUR", "adv_20d_local": 50_000.0}
    passed, reasons = matches_nordic_microcap_catalyst("MID.HE", hard_facts, meta_mid)
    assert passed is False
    assert any(">= 150M EUR" in r for r in reasons)


def test_unified_router():
    """matches_universe_filter correctly dispatches to all three tiers."""
    meta_core = {"market": "FI", "market_cap_eur": 400_000_000.0, "currency": "EUR", "adv_20d_local": 100_000.0}
    hf_core = {"roe": 0.18, "ebit_margin": 0.14, "free_cash_flow": 10_000_000.0, "net_debt_to_ebitda": 0.0}
    p_core, _ = matches_universe_filter("CORE.HE", "Nordic_MidCap_Compounder", hf_core, meta_core)
    assert p_core is True

    meta_us = {"market": "US", "market_cap_usd": 100_000_000.0, "exchange": "NASDAQ"}
    hf_us = {"exchange": "NASDAQ", "dividend_yield": 0.0, "revenue_growth_yoy_pct": 20.0}
    p_us, _ = matches_universe_filter("USGROW", "US_MicroCap_Growth", hf_us, meta_us)
    assert p_us is True

    meta_nordic = {"market": "SE", "market_cap_eur": 50_000_000.0, "currency": "SEK", "adv_20d_local": 300_000.0}
    hf_nordic = {"is_profile_b": True}
    p_nordic, _ = matches_universe_filter("NORD.ST", "Nordic_MicroCap_Catalyst", hf_nordic, meta_nordic)
    assert p_nordic is True


def test_nordic_midcap_compounder_rejects_low_sek_liquidity():
    """Stock on Stockholm exchange with 30,000 SEK ADV (~2,650 EUR) must be rejected from Core."""
    meta = {
        "market": "SE",
        "market_cap_eur": 400_000_000.0,
        "currency": "SEK",
        "adv_20d_local": 30_000.0,  # ~2,654 EUR < 50,000 EUR
    }
    hard_facts = {
        "roe": 0.20,
        "ebit_margin": 0.15,
        "free_cash_flow": 20_000_000.0,
        "net_debt_to_ebitda": 0.5,
    }
    passed, reasons = matches_nordic_midcap_compounder("SEKCORE.ST", hard_facts, meta)
    assert passed is False
    assert any("< 50,000 EUR threshold" in r for r in reasons)


def test_both_nordic_filters_reject_30k_sek_liquidity():
    """SEK-denominated stock with 30,000 SEK ADV is rejected by BOTH Core and Nordic Satellite."""
    meta_core = {
        "market": "SE",
        "market_cap_eur": 300_000_000.0,
        "currency": "SEK",
        "adv_20d_local": 30_000.0,
    }
    hf_core = {
        "roe": 0.25,
        "ebit_margin": 0.20,
        "free_cash_flow": 10_000_000.0,
        "net_debt_to_ebitda": 0.2,
    }
    passed_core, reasons_core = matches_nordic_midcap_compounder("TEST.ST", hf_core, meta_core)
    assert passed_core is False
    assert any("50,000 EUR threshold" in r for r in reasons_core)

    meta_sat = {
        "market": "SE",
        "market_cap_eur": 50_000_000.0,
        "currency": "SEK",
        "adv_20d_local": 30_000.0,
    }
    hf_sat = {
        "is_profile_b": True,
        "cash_runway_months": "Infinite",
    }
    passed_sat, reasons_sat = matches_nordic_microcap_catalyst("TEST.ST", hf_sat, meta_sat)
    assert passed_sat is False
    assert any("20,000 EUR threshold" in r for r in reasons_sat)


