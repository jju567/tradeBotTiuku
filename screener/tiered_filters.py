"""
screener/tiered_filters.py - Tiered Market-Cap and Quality Filtering for Core & Satellite.

Implements the three distinct universe filters required for P13_Live_Candidate & Screener:
1. Nordic_MidCap_Compounder (Core):
   - Market Cap: 150,000,000 EUR <= market_cap <= 2,000,000,000 EUR
     (Excludes illiquid micro-caps and stagnant mega-caps like Nokia/Fortum).
   - Fundamentals:
     * ROE > 15% (0.15)
     * EBIT_margin > 12% (0.12)
     * Positive Free Cash Flow (FCF > 0)
     * Net_Debt_to_EBITDA < 2.5 (or Net Debt <= 0)
   - High liquidity (tight bid-ask spreads / ADV >= 50k-100k EUR).

2. US_MicroCap_Growth (Satellite 1):
   - Exchange: Strictly NASDAQ or NYSE (No OTC/Pink Sheets).
   - Market Cap: 30,000,000 USD <= market_cap <= 350,000,000 USD.
   - Dividend: dividend_yield == 0.0 (Strict OST rule).
   - Catalyst: Strong revenue/EPS growth (YoY > 15%) or high NLP earnings score.

3. Nordic_MicroCap_Catalyst (Satellite 2):
   - Market Cap: 15,000,000 EUR <= market_cap < 150,000,000 EUR (OMXH / OMXS / First North).
   - Criteria: Either undervalued quality micro-cap (Profile A or Profile B)
     OR WATCH_TURNAROUND with verified news/earnings NLP catalyst, cash runway >= 12 months, and volume confirmation.
"""

from __future__ import annotations

import logging
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger(__name__)

# Allowed regulated US exchanges for OST accounts
ALLOWED_US_EXCHANGES = {
    "NASDAQ", "NYSE", "NMS", "NGM", "NCM", "NYQ", "AMEX", "ASE", "BATS",
}

DISALLOWED_US_EXCHANGES = {
    "OTC", "PNK", "PINK", "OQX", "OBB", "GREY", "OTCQX", "OTCQB",
}


def get_market_cap_in_eur(
    meta: Dict[str, Any],
    hard_facts: Optional[Dict[str, Any]] = None,
    fx_rates: Optional[Dict[str, float]] = None,
) -> float:
    """Extracts or converts market capitalization to EUR."""
    rates = fx_rates or {"EURUSD": 1.08, "EURSEK": 11.30, "EURNOK": 11.50, "EURDKK": 7.46}
    
    # 1. Direct EUR field if present
    if "market_cap_eur" in meta and float(meta.get("market_cap_eur") or 0.0) > 0:
        return float(meta["market_cap_eur"])
    if "MarketCap_EUR" in meta and float(meta.get("MarketCap_EUR") or 0.0) > 0:
        return float(meta["MarketCap_EUR"])

    currency = str(meta.get("currency", "")).upper()
    mcap_local = float(meta.get("market_cap_local", 0.0) or 0.0)
    mcap_usd = float(meta.get("market_cap_usd", 0.0) or 0.0)

    if mcap_local <= 0 and hard_facts:
        mcap_local = float(hard_facts.get("market_cap", 0.0) or 0.0)

    if currency == "EUR" and mcap_local > 0:
        return mcap_local
    if currency == "SEK" and mcap_local > 0:
        rate = rates.get("EURSEK", 11.30)
        return mcap_local / rate if rate > 0 else mcap_local
    if currency == "NOK" and mcap_local > 0:
        rate = rates.get("EURNOK", 11.50)
        return mcap_local / rate if rate > 0 else mcap_local
    if currency == "DKK" and mcap_local > 0:
        rate = rates.get("EURDKK", 7.46)
        return mcap_local / rate if rate > 0 else mcap_local

    if mcap_usd > 0:
        rate = rates.get("EURUSD", 1.08)
        return mcap_usd / rate if rate > 0 else mcap_usd

    if mcap_local > 0:
        return mcap_local

    return 0.0


def get_market_cap_in_usd(
    meta: Dict[str, Any],
    hard_facts: Optional[Dict[str, Any]] = None,
    fx_rates: Optional[Dict[str, float]] = None,
) -> float:
    """Extracts or converts market capitalization to USD."""
    rates = fx_rates or {"EURUSD": 1.08, "EURSEK": 11.30}
    if "market_cap_usd" in meta and float(meta.get("market_cap_usd") or 0.0) > 0:
        return float(meta["market_cap_usd"])

    mcap_local = float(meta.get("market_cap_local", 0.0) or 0.0)
    if mcap_local <= 0 and hard_facts:
        mcap_local = float(hard_facts.get("market_cap", 0.0) or 0.0)

    currency = str(meta.get("currency", "USD")).upper()
    if currency == "USD" and mcap_local > 0:
        return mcap_local
    if currency == "EUR" and mcap_local > 0:
        return mcap_local * rates.get("EURUSD", 1.08)

    return mcap_local


def matches_nordic_midcap_compounder(
    ticker: str,
    hard_facts: Dict[str, Any],
    meta: Dict[str, Any],
    fx_rates: Optional[Dict[str, float]] = None,
) -> Tuple[bool, List[str]]:
    """
    Tier 1: Nordic_MidCap_Compounder (Core)
    - Market Cap: 150,000,000 EUR <= market_cap <= 2,000,000,000 EUR
    - Fundamentals:
      * ROE > 15% (0.15)
      * EBIT_margin > 12% (0.12)
      * Positive Free Cash Flow (FCF > 0)
      * Net_Debt_to_EBITDA < 2.5 (or Net Debt <= 0)
    - High liquidity (tight bid-ask spread / ADV >= 50k EUR).
    """
    reasons: List[str] = []
    t_clean = ticker.strip().upper()
    market = str(meta.get("market", "")).upper()

    # Must be Nordic
    is_nordic = (
        market in ["FI", "SE", "DK", "NO", "HE", "ST", "CO", "OL"]
        or any(t_clean.endswith(s) for s in [".HE", ".ST", ".CO", ".OL"])
    )
    if not is_nordic:
        return False, ["Not a Nordic equity"]

    # 1. Market Cap in EUR: 150M <= MCAP <= 2000M
    mcap_eur = get_market_cap_in_eur(meta, hard_facts, fx_rates)
    if mcap_eur < 150_000_000.0:
        reasons.append(f"Market Cap {mcap_eur:,.0f} EUR < 150M EUR (Micro-Cap)")
    elif mcap_eur > 2_000_000_000.0:
        reasons.append(f"Market Cap {mcap_eur:,.0f} EUR > 2,000M EUR (Stagnant Mega-Cap)")

    # 2. ROE > 15%
    roe = hard_facts.get("roe")
    if roe is not None:
        roe_val = roe * 100.0 if abs(roe) <= 2.0 else roe
        if roe_val <= 15.0:
            reasons.append(f"ROE {roe_val:.1f}% <= 15.0%")
    else:
        # Fallback check net income / equity if available
        reasons.append("ROE data missing")

    # 3. EBIT Margin > 12%
    ebit_margin = hard_facts.get("ebit_margin") or hard_facts.get("operating_margin")
    if ebit_margin is not None:
        ebit_val = ebit_margin * 100.0 if abs(ebit_margin) <= 2.0 else ebit_margin
        if ebit_val <= 12.0:
            reasons.append(f"EBIT Margin {ebit_val:.1f}% <= 12.0%")
    else:
        # Fallback check gross margin
        gm = hard_facts.get("gross_margin_pct")
        if gm is not None and gm < 25.0:
            reasons.append(f"EBIT margin missing and Gross Margin {gm:.1f}% low")
        elif gm is None:
            reasons.append("EBIT Margin data missing")

    # 4. Positive Free Cash Flow
    fcf = hard_facts.get("free_cash_flow")
    ocf = hard_facts.get("operating_cash_flow_ttm")
    if fcf is not None:
        if fcf <= 0:
            reasons.append(f"Free Cash Flow <= 0 ({fcf:,.0f})")
    elif ocf is not None:
        if ocf <= 0:
            reasons.append(f"Operating Cash Flow <= 0 ({ocf:,.0f})")
    else:
        reasons.append("Free Cash Flow / OCF data missing")

    # 5. Net Debt to EBITDA < 2.5
    net_debt = hard_facts.get("net_debt")
    net_cash = hard_facts.get("net_cash")
    if net_debt is None and net_cash is not None:
        net_debt = -net_cash

    nd_ebitda = hard_facts.get("net_debt_to_ebitda")
    if nd_ebitda is not None:
        if nd_ebitda >= 2.5:
            reasons.append(f"Net Debt/EBITDA {nd_ebitda:.2f} >= 2.5")
    elif net_debt is not None:
        if net_debt <= 0:
            # Net cash -> perfectly solvent
            pass
        else:
            ebitda = hard_facts.get("ebitda")
            if ebitda and ebitda > 0:
                calc_nd = net_debt / ebitda
                if calc_nd >= 2.5:
                    reasons.append(f"Net Debt/EBITDA {calc_nd:.2f} >= 2.5")
            else:
                reasons.append("Net Debt > 0 but EBITDA <= 0 or missing")

    # 6. High Liquidity (tight spread / ADV >= 50,000 EUR)
    adv_local = float(meta.get("adv_20d_local", 0.0) or 0.0)
    adv_usd = float(meta.get("adv_20d_usd", 0.0) or 0.0)
    if adv_local > 0 and adv_local < 50_000.0 and adv_usd < 50_000.0:
        reasons.append(f"ADV {adv_local:,.0f} < 50,000 EUR threshold")

    passed = len(reasons) == 0
    return passed, reasons


def matches_us_microcap_growth(
    ticker: str,
    hard_facts: Dict[str, Any],
    meta: Dict[str, Any],
    nlp_verdict: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """
    Tier 2: US_MicroCap_Growth (Satellite 1)
    - Exchange: Strictly NASDAQ or NYSE (No OTC/Pink Sheets).
    - Market Cap: 30,000,000 USD <= market_cap <= 350,000,000 USD.
    - Dividend: dividend_yield == 0.0 (Strict OST rule). Also checks dividend_rate.
    - Catalyst: Strong revenue/EPS growth (YoY > 15%) or Profile A or high NLP earnings score with viability.
    """
    reasons: List[str] = []
    t_clean = ticker.strip().upper()
    market = str(meta.get("market", "")).upper()

    # Must be US
    is_us = (market in ["US", "USA"] or "." not in t_clean)
    if not is_us:
        return False, ["Not a US equity"]

    # 1. Strictly NASDAQ or NYSE
    exchange = str(hard_facts.get("exchange") or meta.get("exchange", "")).upper().strip()
    if exchange in DISALLOWED_US_EXCHANGES or len(t_clean) == 5 and t_clean.endswith(("F", "Y")):
        return False, [f"Disallowed exchange: {exchange or 'OTC/Pink'}"]
    if exchange and exchange not in ALLOWED_US_EXCHANGES and "NASDAQ" not in exchange and "NYSE" not in exchange:
        reasons.append(f"Exchange {exchange} not verified NASDAQ/NYSE")

    # 2. Market Cap in USD: 30M <= MCAP <= 350M
    mcap_usd = get_market_cap_in_usd(meta, hard_facts)
    if mcap_usd < 30_000_000.0:
        reasons.append(f"Market Cap {mcap_usd:,.0f} USD < 30M USD")
    elif mcap_usd > 350_000_000.0:
        reasons.append(f"Market Cap {mcap_usd:,.0f} USD > 350M USD")

    # 3. Dividend Yield == 0.0 (Strict OST rule). Verify dividend is truly 0.
    div_yield = hard_facts.get("dividend_yield")
    div_rate = hard_facts.get("dividend_rate") or meta.get("dividend_rate")
    trailing_div_rate = hard_facts.get("trailing_annual_dividend_rate") or meta.get("trailing_annual_dividend_rate")

    has_dividend = False
    if div_yield is not None and float(div_yield) > 0.0001:
        has_dividend = True
    elif div_rate is not None and float(div_rate) > 0.0001:
        has_dividend = True
    elif trailing_div_rate is not None and float(trailing_div_rate) > 0.0001:
        has_dividend = True

    if has_dividend:
        reasons.append("Dividend yield detected > 0.0% (Forbidden on OST)")

    # 4. Catalyst: Strong revenue growth (>15%) or Profile A or high NLP score with viability
    rev_yoy = hard_facts.get("revenue_growth_yoy_pct") or hard_facts.get("revenue_yoy")
    is_prof_a = hard_facts.get("is_profile_a", False)
    nlp_strong = nlp_verdict in ["STRONG BUY", "BUY_PROFILE_A"]
    growth_ok = (rev_yoy is not None and float(rev_yoy) > 15.0)

    if growth_ok or is_prof_a:
        catalyst_ok = True
    elif nlp_strong:
        # Prevent buying pure hype: require positive revenue AND (cash runway >= 12m OR FCF > 0)
        revenue = hard_facts.get("revenue") or hard_facts.get("total_revenue") or meta.get("revenue", 0.0)
        rev_pos = (revenue is not None and float(revenue) > 0)

        runway = hard_facts.get("cash_runway_months")
        runway_12m = False
        if runway is not None:
            if str(runway).lower() == "infinite":
                runway_12m = True
            else:
                try:
                    runway_12m = float(runway) >= 12.0
                except Exception:
                    runway_12m = False

        fcf = hard_facts.get("free_cash_flow") or hard_facts.get("fcf")
        fcf_pos = (fcf is not None and float(fcf) > 0)

        if rev_pos and (runway_12m or fcf_pos):
            catalyst_ok = True
        else:
            catalyst_ok = False
            reasons.append("NLP catalyst present but fails basic viability (requires revenue > 0 and runway >= 12m or FCF > 0)")
    else:
        catalyst_ok = False
        reasons.append(f"No catalyst: YoY growth={rev_yoy}%, Profile A={is_prof_a}, NLP={nlp_verdict}")

    passed = len(reasons) == 0
    return passed, reasons


def matches_nordic_microcap_catalyst(
    ticker: str,
    hard_facts: Dict[str, Any],
    meta: Dict[str, Any],
    nlp_verdict: Optional[str] = None,
    fx_rates: Optional[Dict[str, float]] = None,
) -> Tuple[bool, List[str]]:
    """
    Tier 3: Nordic_MicroCap_Catalyst (Satellite 2)
    - Market Cap: 15,000,000 EUR <= market_cap < 150,000,000 EUR (OMXH / OMXS / First North).
    - Liquidity: adv_eur >= 20,000 EUR mandatory for all candidates.
    - Criteria: Either undervalued quality micro-cap (Profile A or Profile B)
      OR WATCH_TURNAROUND with verified news/earnings NLP catalyst and cash runway >= 12 months (or FCF/EBIT > 0).
    """
    reasons: List[str] = []
    t_clean = ticker.strip().upper()
    market = str(meta.get("market", "")).upper()

    # Must be Nordic
    is_nordic = (
        market in ["FI", "SE", "DK", "NO", "HE", "ST", "CO", "OL"]
        or any(t_clean.endswith(s) for s in [".HE", ".ST", ".CO", ".OL"])
    )
    if not is_nordic:
        return False, ["Not a Nordic equity"]

    # 1. Market Cap in EUR: 15M <= MCAP < 150M
    mcap_eur = get_market_cap_in_eur(meta, hard_facts, fx_rates)
    if mcap_eur < 15_000_000.0:
        reasons.append(f"Market Cap {mcap_eur:,.0f} EUR < 15M EUR (Too small/illiquid)")
    elif mcap_eur >= 150_000_000.0:
        reasons.append(f"Market Cap {mcap_eur:,.0f} EUR >= 150M EUR (Belongs to Core Mid-Cap tier)")

    # 2. Mandatory EUR liquidity gate for all candidates (Profile A/B and Turnaround)
    rates = fx_rates or {"EURUSD": 1.08, "EURSEK": 11.30, "EURNOK": 11.50, "EURDKK": 7.46}
    curr = str(meta.get("currency", "")).upper()
    if not curr:
        if t_clean.endswith(".ST") or ".ST" in t_clean or market == "SE":
            curr = "SEK"
        elif t_clean.endswith(".NO") or t_clean.endswith(".OL") or market == "NO":
            curr = "NOK"
        elif t_clean.endswith(".CO") or market == "DK":
            curr = "DKK"
        else:
            curr = "EUR"

    adv_local = float(meta.get("adv_20d_local", 0.0) or 0.0)
    if curr == "EUR":
        adv_eur = adv_local
    elif curr == "SEK":
        r = rates.get("EURSEK", 11.30)
        adv_eur = adv_local / r if r > 0 else adv_local
    elif curr == "NOK":
        r = rates.get("EURNOK", 11.50)
        adv_eur = adv_local / r if r > 0 else adv_local
    elif curr == "DKK":
        r = rates.get("EURDKK", 7.46)
        adv_eur = adv_local / r if r > 0 else adv_local
    else:
        adv_eur = adv_local

    if adv_local <= 0 and meta.get("adv_20d_usd"):
        r_usd = rates.get("EURUSD", 1.08)
        adv_eur = float(meta["adv_20d_usd"]) / r_usd if r_usd > 0 else float(meta["adv_20d_usd"])

    if adv_eur < 20_000.0:
        reasons.append(f"ADV {adv_eur:,.0f} EUR < 20,000 EUR threshold")

    # 3. Criteria: (A) Quality Micro-Cap (Profile A or Profile B) OR (B) Turnaround with runway >= 12m (or FCF/EBIT > 0)
    is_prof_a = hard_facts.get("is_profile_a", False)
    is_prof_b = hard_facts.get("is_profile_b", False)
    is_turnaround = (nlp_verdict in ["WATCH_TURNAROUND", "STRONG BUY"] or meta.get("is_turnaround", False))

    runway = hard_facts.get("cash_runway_months")
    runway_ok = False
    if runway is not None:
        if str(runway).lower() == "infinite":
            runway_ok = True
        else:
            try:
                runway_ok = float(runway) >= 12.0
            except Exception:
                runway_ok = False
    else:
        # Missing cash runway: verify if Free Cash Flow > 0 or EBIT > 0
        fcf = hard_facts.get("free_cash_flow") or hard_facts.get("fcf")
        ebit = hard_facts.get("ebit") or hard_facts.get("operating_income") or hard_facts.get("operating_profit") or hard_facts.get("operating_cash_flow_ttm")
        fcf_pos = (fcf is not None and float(fcf) > 0)
        ebit_pos = (ebit is not None and float(ebit) > 0)
        runway_ok = (fcf_pos or ebit_pos)

    criteria_ok = False
    if is_prof_a or is_prof_b:
        criteria_ok = True
    elif is_turnaround and runway_ok:
        criteria_ok = True
    else:
        if not (is_prof_a or is_prof_b):
            reasons.append(f"Neither Profile A nor Profile B met (ProfA={is_prof_a}, ProfB={is_prof_b})")
        if is_turnaround and not runway_ok:
            reasons.append(f"Turnaround candidate but cash runway {runway} insufficient/missing without positive FCF/EBIT")

    passed = len(reasons) == 0 and criteria_ok
    return passed, reasons


def matches_universe_filter(
    ticker: str,
    filter_name: str,
    hard_facts: Dict[str, Any],
    meta: Dict[str, Any],
    nlp_verdict: Optional[str] = None,
    fx_rates: Optional[Dict[str, float]] = None,
) -> Tuple[bool, List[str]]:
    """
    Unified router for universe filtering.
    Dispatches to the correct tiered filter based on filter_name.
    """
    fname = (filter_name or "").strip()
    if fname in ["Nordic_MidCap_Compounder", "Nordic_MidCap_Quality"]:
        return matches_nordic_midcap_compounder(ticker, hard_facts, meta, fx_rates=fx_rates)
    elif fname in ["US_MicroCap_Growth", "US_Microcap_Growth"]:
        return matches_us_microcap_growth(ticker, hard_facts, meta, nlp_verdict=nlp_verdict)
    elif fname in ["Nordic_MicroCap_Catalyst", "Nordic_Microcap_Catalyst"]:
        return matches_nordic_microcap_catalyst(ticker, hard_facts, meta, nlp_verdict=nlp_verdict, fx_rates=fx_rates)
    else:
        # Fallback to standard Profile B
        is_prof_b = hard_facts.get("is_profile_b", False)
        return is_prof_b, ([] if is_prof_b else ["Failed default Profile B"])
