"""
financial_metrics_engine.py

"Deterministic First" -kerros kvanttiseulontaputkelle.

Tarkoitus: laskea kriittiset selviytymis- ja arvostustunnusluvut PUHTAASTI
Pythonilla suoraan raakadatasta, ilman että mikään LLM osallistuu laskentaan.
LLM saa nämä luvut myöhemmin vain kontekstina (nlp_analyzer.py), ei koskaan
tehtäväksi laskea niitä itse.

Suunnitteluperiaatteet:
  1. Ei koskaan palauteta 0, jos oikea arvo on "ei tiedossa" -> käytetään None.
     Sekaannus 0:n ja puuttuvan datan välillä on yleinen syy vääriin REJECT/BUY-
     päätöksiin jatkopipelinessa.
  2. Jokaiseen laskelmaan liitetään data_quality-metatieto (esim. onko OCF
     todellinen TTM vai fallback viimeisimpään tilikauteen), jotta downstream-
     kuluttaja (LLM-promptti tai sääntömoottori) voi painottaa epävarmuutta.
  3. yfinance-rivinimet vaihtelevat markkinan/yhtiön mukaan (esim. "Total Debt"
     puuttuu usein) -> haetaan usealla vaihtoehtoisella avaimella ja lasketaan
     tarvittaessa itse (Long Term Debt + Current Debt).
  4. Kaikki poikkeukset (verkko, puuttuva ticker, yfinance-skeeman muutokset)
     kiinni -> funktio ei koskaan kaadu koko putkea, vaan palauttaa
     osittaisen tuloksen ja error-listan.
"""

from __future__ import annotations

import logging
import math
import statistics
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Union, List
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

_LIVE_FX_CACHE: Dict[str, float] = {}


def get_live_fx_rates(force_refresh: bool = False) -> Dict[str, float]:
    """
    Fetches live EUR/SEK, EUR/USD, EUR/NOK, and EUR/DKK rates from yfinance once per session,
    with safe deterministic fallback during unit tests or offline runs.
    """
    defaults = {"EURUSD": 1.08, "EURSEK": 11.30, "EURNOK": 11.50, "EURDKK": 7.46}
    if "PYTEST_CURRENT_TEST" in os.environ or " MagicMock" in str(type(yf.Ticker)):
        return dict(defaults)
    if _LIVE_FX_CACHE and not force_refresh:
        return dict(_LIVE_FX_CACHE)
    rates = dict(defaults)
    pairs = {"EURSEK": "EURSEK=X", "EURUSD": "EURUSD=X", "EURNOK": "EURNOK=X", "EURDKK": "EURDKK=X"}
    for key, sym in pairs.items():
        try:
            tk = yf.Ticker(sym)
            fi = getattr(tk, "fast_info", None)
            px = getattr(fi, "last_price", None) if fi is not None else None
            if not isinstance(px, (int, float)) or px <= 0:
                inf = getattr(tk, "info", None) or {}
                px = inf.get("regularMarketPrice") or inf.get("previousClose")
            if isinstance(px, (int, float)) and px > 0:
                rates[key] = round(float(px), 4)
        except Exception:
            pass
    _LIVE_FX_CACHE.update(rates)
    return dict(_LIVE_FX_CACHE)

# Rivinimet joita yfinance käyttää eri raporteissa (versiot/markkinat vaihtelevat)
_DEBT_KEYS = ["Total Debt", "TotalDebt"]
_LT_DEBT_KEYS = ["Long Term Debt", "LongTermDebt", "Long Term Debt Noncurrent", "Long Term Debt And Capital Lease Obligation"]
_CUR_DEBT_KEYS = ["Current Debt", "CurrentDebt", "Current Debt And Capital Lease Obligation", "Short Term Debt", "ShortTermDebt"]
_CUR_DEBT_ONLY_KEYS = ["Current Debt", "CurrentDebt", "Short Term Debt", "ShortTermDebt"]
_LEASE_OBLIGATION_KEYS = ["Current Capital Lease Obligation", "CurrentCapitalLeaseObligation", "Capital Lease Obligation Current"]
_CUR_DEBT_COMBINED_KEYS = ["Current Debt And Capital Lease Obligation", "CurrentDebtAndCapitalLeaseObligation"]
_SHARE_COUNT_KEYS = [
    "Ordinary Shares Number",
    "OrdinarySharesNumber",
    "Share Issued",
    "ShareIssued",
    "Common Stock Shares Outstanding",
    "CommonStockSharesOutstanding",
]
_CASH_KEYS = [
    "Cash And Cash Equivalents",
    "CashAndCashEquivalents",
    "Cash Cash Equivalents And Short Term Investments",
    "Cash And Short Term Investments",
    "Cash Financial",
    "Cash",
]
_OCF_KEYS = [
    "Operating Cash Flow",
    "OperatingCashFlow",
    "Total Cash From Operating Activities",
    "Cash Flow From Continuing Operating Activities",
]
_REVENUE_KEYS = ["Total Revenue", "TotalRevenue", "Operating Revenue", "Revenue"]
_GROSS_PROFIT_KEYS = ["Gross Profit", "GrossProfit", "Gross Margin"]
_CAPEX_KEYS = [
    "Capital Expenditure",
    "CapitalExpenditure",
    "CapitalExpenditures",
    "Capital Expenditure Reported",
    "Purchase Of PPE",
    "Payments For Property Plant And Equipment",
    "Purchase Of Property Plant And Equipment",
]

_EBIT_KEYS = [
    "Operating Income",
    "OperatingIncome",
    "Operating Profit",
    "OperatingProfit",
    "EBIT",
    "Pretax Income",
    "Normalized EBITDA",
    "EBITDA",
]



def _get_first_valid_df(obj: Any, attr_names: list[str]) -> Optional[pd.DataFrame]:
    """Safely returns the first non-empty DataFrame from a list of attribute names."""
    if obj is None:
        return None
    for attr in attr_names:
        try:
            val = getattr(obj, attr, None)
            if isinstance(val, pd.DataFrame) and not val.empty:
                return val
        except Exception:
            continue
    return None


def _extract_4q_ttm_from_df(df: Optional[pd.DataFrame], possible_keys: list[str]) -> Optional[float]:
    """
    Laskee tuoreimman 4 peräkkäisen kvartaalin TTM-summan (iloc[:4].sum()),
    kun aikaväli q0..q3 on 240..310 päivää (tai synteettisessä testissä 4 saraketta ilman väli-NaNeja).
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for key in possible_keys:
        if key in df.index:
            series = df.loc[key].dropna()
            if len(series) >= 4:
                try:
                    dq0 = pd.to_datetime(series.index[0]).tz_localize(None)
                    dq3 = pd.to_datetime(series.index[3]).tz_localize(None)
                    if 240 <= abs((dq0 - dq3).days) <= 310:
                        return float(series.iloc[:4].sum())
                except Exception:
                    raw_row = df.loc[key]
                    if len(raw_row) >= 4 and not raw_row.iloc[:4].isna().any():
                        return float(raw_row.iloc[:4].sum())
    return None


def _extract_metric_from_df(df: Optional[pd.DataFrame], possible_keys: list[str]) -> Optional[float]:
    """Extracts the most recent non-NaN value matching any of the possible row names."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return None

    # Try exact match first
    for key in possible_keys:
        if key in df.index:
            try:
                series = df.loc[key].dropna()
                if not series.empty:
                    val = float(series.iloc[0])
                    if pd.notna(val):
                        return val
            except (ValueError, TypeError, IndexError):
                continue

    # Try case-insensitive substring match
    try:
        index_lower = {str(k).strip().lower(): k for k in df.index}
        for key in possible_keys:
            key_l = key.strip().lower()
            if key_l in index_lower:
                orig_k = index_lower[key_l]
                series = df.loc[orig_k].dropna()
                if not series.empty:
                    val = float(series.iloc[0])
                    if pd.notna(val):
                        return val
    except Exception:
        pass

    return None


def calculate_relative_ev_ebit(
    t: Any,
    info: Optional[Dict[str, Any]] = None,
    bs_a: Optional[pd.DataFrame] = None,
    inc_a: Optional[pd.DataFrame] = None,
    current_ev: Optional[float] = None,
    current_ebit: Optional[float] = None,
    override_current_ev_ebit: Optional[float] = None,
    override_5y_median: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Calculates current EV/EBIT (or EV/EBITDA) and compares it against its 5-year historical median.

    Rule:
      If current EV/EBIT is > 20% higher than its 5-year historical median (ev_ebit_premium_pct > 20%),
      flags is_historically_expensive = True for downstream penalty/rejection.

    Error handling:
      If 5-year history is not available (e.g. newer IPO or missing statements), calculates median
      from available shorter history or gracefully falls back to neutral (is_historically_expensive = False).
    """
    res: Dict[str, Any] = {
        "current_ev_ebit": None,
        "ev_ebit_5y_median": None,
        "ev_ebit_premium_pct": None,
        "is_historically_expensive": False,
        "historical_ev_ebit_years_count": 0,
        "valuation_status": "UNKNOWN",
    }

    # Respect explicit overrides (e.g. from unit tests or point-in-time datasets)
    if override_current_ev_ebit is not None and override_5y_median is not None:
        curr = round(float(override_current_ev_ebit), 2)
        med = round(float(override_5y_median), 2)
        res["current_ev_ebit"] = curr
        res["ev_ebit_5y_median"] = med
        res["historical_ev_ebit_years_count"] = 5
        res["valuation_status"] = "REJECTED_NEGATIVE_EBIT" if curr <= 0 else "FULL_5Y_HISTORY"
        if med > 0 and curr > 0:
            prem = round(((curr - med) / med) * 100.0, 2)
            res["ev_ebit_premium_pct"] = prem
            res["is_historically_expensive"] = (prem > 20.0)
        return res

    info_dict = info if isinstance(info, dict) else {}

    # 1. Determine current EV & EBIT
    curr_ev = current_ev
    if curr_ev is None or curr_ev <= 0:
        ev_raw = info_dict.get("enterpriseValue")
        if isinstance(ev_raw, (int, float)) and ev_raw > 0:
            curr_ev = float(ev_raw)

    curr_ebit = current_ebit
    if curr_ebit is None:
        op_raw = info_dict.get("operatingIncome")
        if isinstance(op_raw, (int, float)):
            curr_ebit = float(op_raw)
        elif isinstance(info_dict.get("ebitda"), (int, float)):
            curr_ebit = float(info_dict["ebitda"])

    curr_ev_ebit = None
    if curr_ebit is not None and float(curr_ebit) <= 0.0:
        # Negative or zero EBIT: NEVER fall back to positive EBITDA or enterpriseToEbitda!
        curr_ev_ebit = None
        res["valuation_status"] = "REJECTED_NEGATIVE_EBIT"
    elif curr_ev is not None and curr_ebit is not None and float(curr_ebit) > 0.0:
        curr_ev_ebit = round(float(curr_ev) / float(curr_ebit), 2)
    elif curr_ebit is None and info_dict.get("enterpriseToEbitda"):
        try:
            val = float(info_dict["enterpriseToEbitda"])
            if val > 0:
                curr_ev_ebit = round(val, 2)
        except Exception:
            pass

    res["current_ev_ebit"] = curr_ev_ebit

    # 2. Compute historical annual EV/EBIT for up to 5 annual periods
    hist_ev_ebits: List[float] = []
    if inc_a is not None and isinstance(inc_a, pd.DataFrame) and not inc_a.empty:
        try:
            hist = None
            if hasattr(t, "history"):
                try:
                    hist = t.history(period="5y", auto_adjust=False)
                except TypeError:
                    try:
                        hist = t.history(period="5y")
                    except Exception:
                        hist = None
                except Exception:
                    hist = None

            mcap_val = float(info_dict.get("marketCap") or (curr_ev if curr_ev else 0.0) or 0.0)
            px_val = float(info_dict.get("currentPrice") or info_dict.get("previousClose") or 1.0)
            shares = float(info_dict.get("sharesOutstanding") or 0.0)
            if shares <= 0 and mcap_val > 0 and px_val > 0:
                shares = mcap_val / px_val

            if hist is not None and isinstance(hist, pd.DataFrame) and not hist.empty and shares > 0:
                hist_naive = hist.index.tz_localize(None) if hasattr(hist.index, "tz_localize") and hist.index.tz is not None else hist.index

                for col in list(inc_a.columns)[:5]:
                    y_ebit = _extract_metric_from_df(inc_a[[col]], _EBIT_KEYS)
                    if y_ebit is None or y_ebit <= 0:
                        y_ebit = _extract_metric_from_df(inc_a[[col]], ["EBITDA", "Normalized EBITDA", "Operating Income"])

                    y_debt = 0.0
                    y_cash = 0.0
                    if bs_a is not None and isinstance(bs_a, pd.DataFrame) and col in bs_a.columns:
                        d_val = _extract_metric_from_df(bs_a[[col]], _DEBT_KEYS)
                        if d_val is not None:
                            y_debt = float(d_val)
                        c_val = _extract_metric_from_df(bs_a[[col]], _CASH_KEYS)
                        if c_val is not None:
                            y_cash = float(c_val)

                    target_dt = pd.to_datetime(col)
                    if hasattr(target_dt, "tz_localize") and target_dt.tz is not None:
                        target_dt = target_dt.tz_localize(None)

                    diffs = abs(hist_naive - target_dt)
                    if not diffs.empty:
                        nearest_idx = diffs.argmin()
                        p = float(hist["Close"].iloc[nearest_idx])
                        if p > 0:
                            hist_ev = (p * shares) + y_debt - y_cash
                            if y_ebit and y_ebit > 0 and hist_ev > 0:
                                hist_ev_ebits.append(round(hist_ev / y_ebit, 2))
        except Exception as e:
            logger.warning(f"Historical EV/EBIT extraction exception: {e}", exc_info=True)

    raw_op_inc = current_ebit if current_ebit is not None else (info_dict.get("operatingIncome") or info_dict.get("ebitda"))
    if (raw_op_inc is not None and isinstance(raw_op_inc, (int, float)) and raw_op_inc <= 0) or (curr_ev_ebit is not None and curr_ev_ebit <= 0):
        res["valuation_status"] = "REJECTED_NEGATIVE_EBIT"

    if hist_ev_ebits:
        med = round(float(statistics.median(hist_ev_ebits)), 2)
        res["ev_ebit_5y_median"] = med
        yrs_cnt = len(hist_ev_ebits)
        res["historical_ev_ebit_years_count"] = yrs_cnt
        if res["valuation_status"] != "REJECTED_NEGATIVE_EBIT":
            if yrs_cnt >= 5:
                res["valuation_status"] = "FULL_5Y_HISTORY"
            elif yrs_cnt >= 3:
                res["valuation_status"] = "PARTIAL_4Y_HISTORY"
            else:
                res["valuation_status"] = "INSUFFICIENT_5Y_HISTORY"
        if curr_ev_ebit is not None and curr_ev_ebit > 0 and med > 0:
            prem = round(((curr_ev_ebit - med) / med) * 100.0, 2)
            res["ev_ebit_premium_pct"] = prem
            res["is_historically_expensive"] = (prem > 20.0)
    else:
        res["ev_ebit_5y_median"] = None
        res["ev_ebit_premium_pct"] = None
        res["is_historically_expensive"] = False
        if res["valuation_status"] != "REJECTED_NEGATIVE_EBIT":
            res["valuation_status"] = "MISSING_5Y_HISTORY"

    return res


_DILUTED_SHARE_COUNT_KEYS = [
    "Diluted Average Shares",
    "DilutedAverageShares",
    "Diluted Shares",
]


def check_share_dilution(
    t: Any,
    info: Dict[str, Any],
    bs_a: Optional[pd.DataFrame],
    bs_q: Optional[pd.DataFrame] = None,
    inc_a: Optional[pd.DataFrame] = None,
    inc_q: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    Checks for recent share dilution / secondary offerings over a 1-2 year window.
    Compares current shares outstanding against historical balance sheet share counts.
    
    If shares outstanding grew by > 5.0%, flags as DILUTION_WARNING_RECENT_ISSUE.
    """
    res: Dict[str, Any] = {
        "shares_outstanding_current": None,
        "shares_outstanding_historical": None,
        "share_dilution_pct": None,
        "has_share_dilution": False,
        "dilution_flag": None,
    }
    
    # 1. Current shares from info, impliedSharesOutstanding (for Nordic A/B share classes), or latest balance sheet
    sh_out = info.get("sharesOutstanding")
    sh_imp = info.get("impliedSharesOutstanding")
    bs_sh_q = _extract_metric_from_df(bs_q, _SHARE_COUNT_KEYS) if (bs_q is not None and not bs_q.empty) else None
    bs_sh_a = _extract_metric_from_df(bs_a, _SHARE_COUNT_KEYS) if (bs_a is not None and not bs_a.empty) else None

    # For dual-class stocks (e.g. NTEK-B.ST), sharesOutstanding is only B-shares (9.1M) while impliedSharesOutstanding
    # and balance sheet Ordinary Shares Number include A+B shares (10.6M). Use consistent total-company share count!
    cand_curr = [
        float(x)
        for x in (sh_out, sh_imp, bs_sh_q)
        if isinstance(x, (int, float)) and not isinstance(x, bool) and float(x) > 0
    ]
    shares_curr = max(cand_curr) if cand_curr else None
    if shares_curr is None or shares_curr <= 0:
        fast_info = getattr(t, "fast_info", None)
        if fast_info:
            cand_sh = getattr(fast_info, "shares", None) or getattr(fast_info, "shares_outstanding", None)
            if isinstance(cand_sh, (int, float)) and cand_sh > 0:
                shares_curr = float(cand_sh)
    if (shares_curr is None or shares_curr <= 0) and bs_sh_a is not None and bs_sh_a > 0:
        shares_curr = float(bs_sh_a)

    if shares_curr is None or shares_curr <= 0:
        return res

    res["shares_outstanding_current"] = float(shares_curr)
    
    # 2. Historical shares from annual balance sheet (1-2 years ago)
    shares_past_candidates: list[float] = []
    if bs_a is not None and not bs_a.empty:
        for key in _SHARE_COUNT_KEYS:
            if key in bs_a.index:
                try:
                    s = bs_a.loc[key].dropna()
                    if len(s) >= 2 and float(s.iloc[1]) > 0:
                        shares_past_candidates.append(float(s.iloc[1]))
                    if len(s) >= 3 and float(s.iloc[2]) > 0:
                        shares_past_candidates.append(float(s.iloc[2]))
                    if len(s) >= 1 and float(s.iloc[0]) > 0 and float(s.iloc[0]) < float(shares_curr):
                        shares_past_candidates.append(float(s.iloc[0]))
                    if shares_past_candidates:
                        break
                except Exception:
                    continue

    if not shares_past_candidates and bs_q is not None and not bs_q.empty:
        for key in _SHARE_COUNT_KEYS:
            if key in bs_q.index:
                try:
                    s = bs_q.loc[key].dropna()
                    if len(s) >= 4 and float(s.iloc[3]) > 0:
                        shares_past_candidates.append(float(s.iloc[3]))
                    elif len(s) >= 2 and float(s.iloc[1]) > 0:
                        shares_past_candidates.append(float(s.iloc[1]))
                    if shares_past_candidates:
                        break
                except Exception:
                    continue

    # 3. Also check Diluted Average Shares vs Basic Average Shares (warrants/options overhang) from income statements
    _basic_share_keys = ["Basic Average Shares", "BasicAverageShares", "Basic Shares"]
    diluted_curr = _extract_metric_from_df(inc_q, _DILUTED_SHARE_COUNT_KEYS) or _extract_metric_from_df(inc_a, _DILUTED_SHARE_COUNT_KEYS)
    basic_curr = _extract_metric_from_df(inc_q, _basic_share_keys) or _extract_metric_from_df(inc_a, _basic_share_keys)
    base_for_overhang = float(basic_curr) if (basic_curr is not None and basic_curr > 0) else float(shares_curr)
    effective_curr_shares = max(float(shares_curr), float(diluted_curr)) if (diluted_curr is not None and diluted_curr > 0) else float(shares_curr)

    if diluted_curr is not None and base_for_overhang > 0 and float(diluted_curr) > base_for_overhang * 1.05:
        overhang_pct = ((float(diluted_curr) - base_for_overhang) / base_for_overhang) * 100.0
        res["share_dilution_pct"] = round(overhang_pct, 2)
        res["has_share_dilution"] = True
        res["dilution_flag"] = "DILUTION_WARNING_RECENT_ISSUE"

    if shares_past_candidates:
        shares_past = min(shares_past_candidates)
        res["shares_outstanding_historical"] = float(shares_past)
        dilution_pct = ((effective_curr_shares - float(shares_past)) / float(shares_past)) * 100.0
        if res["share_dilution_pct"] is None or dilution_pct > res["share_dilution_pct"]:
            res["share_dilution_pct"] = round(dilution_pct, 2)
        if dilution_pct > 5.0:
            res["has_share_dilution"] = True
            res["dilution_flag"] = "DILUTION_WARNING_RECENT_ISSUE"

    return res


def get_hard_financials(ticker: str) -> Dict[str, Any]:
    """
    Laskee deterministiset selviytymis- ja kasvutunnusluvut annetulle tickerille.

    Palauttaa dictin, joka on suunniteltu injektoitavaksi suoraan LLM-promptiin
    osiona "[HARD FINANCIAL FACTS - DO NOT RECALCULATE]".

    Kaikki numeeriset kentät ovat joko float tai None (ei koskaan puuttuva avain,
    jotta downstream-koodi/prompti voi aina luottaa rakenteeseen).
    """
    cleaned_ticker = ticker.strip().upper()

    # Estä tietovuoto (State Leakage): alustetaan kaikki muuttujat eksplisiittisesti
    t = None
    bs_q = None
    bs_a = None
    cf_q = None
    cf_a = None
    inc_q = None
    inc_a = None
    cash = None
    debt = None
    lt_q = None
    cur_q = None
    lt_a = None
    cur_a = None
    calc_debt = None
    ocf_val = None
    capex = None
    fcf = None
    rev_curr = None
    rev_prev = None
    gp_val = None
    gm_pct = None
    latest_dt = None
    age_days = None
    is_fresh = True
    info = None
    fast_info = None
    roe = None
    ebit_m = None
    ebitda = None
    nd_ebitda = None
    div_y = None
    div_r = None
    trail_div_r = None
    div_paid = None
    exch = None
    country_val = None
    mcap = None
    bid_px = None
    ask_px = None
    earn_ts = None
    earn_dt_obj = None
    d_to_earn = None
    ins_pct = None
    net_ins = None
    has_positive_cash_flow = False
    current_ev = None
    current_ebit = None
    current_ev_ebit = None
    median_5y_ev_ebit = None
    ev_ebit_premium_pct = None
    is_historically_expensive = False
    historical_ev_ebit_years_count = 0
    avg_turnover_eur = None
    liquidity_status = "LIQUID_OK" 

    result: Dict[str, Any] = {
        "ticker": cleaned_ticker,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "net_cash": None,
        "cash_and_equivalents": None,
        "total_debt": None,
        "operating_cash_flow_ttm": None,
        "free_cash_flow": None,
        "has_positive_cash_flow": False,
        "cash_flow_viable": False,
        "adjusted_cash": None,
        "current_debt": None,
        "current_capital_lease_obligation": None,
        "cash_runway_months": None,
        "cash_runway_years": None,
        "runway_status": "RUNWAY_OK",
        "shares_outstanding_current": None,
        "shares_outstanding_historical": None,
        "share_dilution_pct": None,
        "has_share_dilution": False,
        "audit_flags": [],
        "audit_flag": None,
        "audit_warning": None,
        "total_revenue_current": None,
        "total_revenue_previous": None,
        "revenue_current": None,
        "revenue_previous": None,
        "revenue_growth_yoy_pct": None,
        "gross_profit": None,
        "gross_margin_pct": None,
        "roe": None,
        "ebit_margin": None,
        "ebitda": None,
        "net_debt_to_ebitda": None,
        "dividend_yield": None,
        "dividend_rate": None,
        "trailing_annual_dividend_rate": None,
        "cash_dividends_paid": None,
        "exchange": None,
        "country": None,
        "market_cap": None,
        "bid_ask_spread_pct": None,
        "next_earnings_date": None,
        "days_to_earnings": None,
        "held_percent_insiders": None,
        "insider_buying_signal": False,
        "latest_statement_date": None,
        "data_age_days": None,
        "data_source": "yfinance",
        "data_quality": {
            "ocf_source": None,   # "ttm_quarterly" | "latest_annual" | None
            "debt_source": None,  # "total_debt_field" | "lt_plus_current" | "zero_debt_inferred" | None
            "is_fresh": True,     # True if statement age <= 120 days
            "cash_burner": False, # True if operating/free cash flow <= 0
        },
        "errors": [],
        "status": "ERROR",
    }

    try:
        t = yf.Ticker(cleaned_ticker)
    except Exception as e:
        result["errors"].append(f"yf.Ticker init failed: {e}")
        return result

    try:
        # ---- 1. Taseraportti (Balance Sheet) ----
        bs_q = _get_first_valid_df(t, ["quarterly_balance_sheet"])
        bs_a = _get_first_valid_df(t, ["balance_sheet"])

        cash = _extract_metric_from_df(bs_q, _CASH_KEYS)
        if cash is None:
            cash = _extract_metric_from_df(bs_a, _CASH_KEYS)

        # Velkaluvun haku: 'Total Debt' tai 'Long Term Debt' + 'Current Debt'
        debt = _extract_metric_from_df(bs_q, _DEBT_KEYS)
        if debt is not None:
            result["data_quality"]["debt_source"] = "total_debt_field"
        else:
            debt = _extract_metric_from_df(bs_a, _DEBT_KEYS)
            if debt is not None:
                result["data_quality"]["debt_source"] = "total_debt_field"

        if debt is None:
            lt_q = _extract_metric_from_df(bs_q, _LT_DEBT_KEYS)
            cur_q = _extract_metric_from_df(bs_q, _CUR_DEBT_KEYS)
            lt_a = _extract_metric_from_df(bs_a, _LT_DEBT_KEYS)
            cur_a = _extract_metric_from_df(bs_a, _CUR_DEBT_KEYS)
            
            calc_debt = max((lt_q or 0.0) + (cur_q or 0.0), (lt_a or 0.0) + (cur_a or 0.0))
            if calc_debt > 0:
                debt = calc_debt
                result["data_quality"]["debt_source"] = "lt_plus_current"
            elif any(v is not None for v in (lt_q, cur_q, lt_a, cur_a)):
                debt = 0.0
                result["data_quality"]["debt_source"] = "lt_plus_current"
            else:
                # If a valid balance sheet exists with equity/assets and cash, and no debt rows exist, company is debt-free
                _bs_check_keys = [
                    "Total Assets", "TotalAssets", "Stockholders Equity",
                    "StockholdersEquity", "Total Stockholder Equity", "Common Stock Equity",
                ]
                tot_assets = _extract_metric_from_df(bs_q, ["Total Assets", "TotalAssets"]) or _extract_metric_from_df(bs_a, ["Total Assets", "TotalAssets"])
                equity = _extract_metric_from_df(bs_q, ["Stockholders Equity", "StockholdersEquity", "Total Stockholder Equity", "Common Stock Equity"]) or _extract_metric_from_df(bs_a, ["Stockholders Equity", "StockholdersEquity", "Total Stockholder Equity", "Common Stock Equity"])
                has_bs_structure = (
                    tot_assets is not None or equity is not None
                    or _extract_metric_from_df(bs_q, _bs_check_keys) is not None
                    or _extract_metric_from_df(bs_a, _bs_check_keys) is not None
                )
                liabilities_ratio = ((tot_assets - equity) / tot_assets) if (tot_assets and equity and tot_assets > 0) else 0.0
                if cash is not None and has_bs_structure:
                    if liabilities_ratio > 0.65:
                        debt = None
                        result["data_quality"]["debt_source"] = "debt_missing_high_liabilities"
                    else:
                        debt = 0.0
                        result["data_quality"]["debt_source"] = "zero_debt_inferred"

        result["cash_and_equivalents"] = cash
        result["total_debt"] = debt

        # Lyhytaikaiset korolliset velat ja vuokravastuut Adjusted Cash -laskentaa varten
        cur_debt_val = 0.0
        lease_ob_val = 0.0

        c_debt = _extract_metric_from_df(bs_q, _CUR_DEBT_ONLY_KEYS)
        if c_debt is None:
            c_debt = _extract_metric_from_df(bs_a, _CUR_DEBT_ONLY_KEYS)

        l_ob = _extract_metric_from_df(bs_q, _LEASE_OBLIGATION_KEYS)
        if l_ob is None:
            l_ob = _extract_metric_from_df(bs_a, _LEASE_OBLIGATION_KEYS)

        if c_debt is not None:
            cur_debt_val = float(c_debt)
            if l_ob is not None:
                lease_ob_val = float(l_ob)
        else:
            comb_debt = _extract_metric_from_df(bs_q, _CUR_DEBT_COMBINED_KEYS)
            if comb_debt is None:
                comb_debt = _extract_metric_from_df(bs_a, _CUR_DEBT_COMBINED_KEYS)
            if comb_debt is not None:
                cur_debt_val = float(comb_debt)
                lease_ob_val = 0.0
            elif l_ob is not None:
                lease_ob_val = float(l_ob)
            elif debt is not None:
                lt_known = _extract_metric_from_df(bs_q, _LT_DEBT_KEYS)
                if lt_known is None:
                    lt_known = _extract_metric_from_df(bs_a, _LT_DEBT_KEYS)
                if lt_known is not None:
                    cur_debt_val = max(0.0, float(debt) - float(lt_known))
                else:
                    cur_debt_val = float(debt)

        cash_val = float(cash) if cash is not None else 0.0
        adjusted_cash_raw = cash_val - cur_debt_val - lease_ob_val
        adjusted_cash = max(0.0, adjusted_cash_raw)

        result["adjusted_cash"] = round(adjusted_cash, 2)
        result["current_debt"] = round(cur_debt_val, 2)
        result["current_capital_lease_obligation"] = round(lease_ob_val, 2)

        if cash is not None and debt is not None:
            result["net_cash"] = round(cash - debt, 2)
        elif cash is not None and debt is None:
            result["errors"].append("total_debt not found; net_cash left as None to avoid false optimism")

        # ---- 2. Kassavirtaraportti (Cash Flow) & Vapaa Kassavirta (FCF) ----
        cf_q = _get_first_valid_df(t, ["quarterly_cashflow"])
        cf_a = _get_first_valid_df(t, ["cashflow"])

        # Yritetään ensin 4 kvartaalin TTM-summaa
        if cf_q is not None and not cf_q.empty:
            for key in _OCF_KEYS:
                if key in cf_q.index:
                    series = cf_q.loc[key].dropna()
                    if len(series) >= 4:
                        ocf_val = float(series.iloc[:4].sum())
                        result["data_quality"]["ocf_source"] = "ttm_quarterly"
                        break

        # Fallback: viimeisin tilikausi
        if ocf_val is None and cf_a is not None and not cf_a.empty:
            val = _extract_metric_from_df(cf_a, _OCF_KEYS)
            if val is not None:
                ocf_val = val
                result["data_quality"]["ocf_source"] = "latest_annual"

        # Osittainen kvartaalisumma vuositasolle skaalattuna jos vuosiraporttia ei löydy
        if ocf_val is None and cf_q is not None and not cf_q.empty:
            for key in _OCF_KEYS:
                if key in cf_q.index:
                    series = cf_q.loc[key].dropna()
                    if len(series) >= 1:
                        ocf_val = float(series.sum()) * (4.0 / len(series))
                        result["data_quality"]["ocf_source"] = f"annualized_{len(series)}q"
                        break

        # Capex & Vapaa kassavirta (FCF)
        # TASK-12: If OCF is 4-quarter TTM, sum 4 quarters of CapEx to match time horizons
        if result["data_quality"]["ocf_source"] == "ttm_quarterly" and cf_q is not None and not cf_q.empty:
            for key in _CAPEX_KEYS:
                if key in cf_q.index:
                    series = cf_q.loc[key].dropna()
                    if len(series) >= 4:
                        capex = float(series.iloc[:4].sum())
                        break
        if capex is None:
            capex = _extract_metric_from_df(cf_a, _CAPEX_KEYS)
        if capex is None and cf_q is not None and not cf_q.empty:
            single_q_capex = _extract_metric_from_df(cf_q, _CAPEX_KEYS)
            if single_q_capex is not None:
                capex = single_q_capex * (4.0 if result["data_quality"]["ocf_source"] in ("ttm_quarterly", "latest_annual") else 1.0)

        if ocf_val is not None and capex is not None:
            fcf = ocf_val - abs(capex)

        # Fallback info-metatiedoista
        try:
            info = getattr(t, "info", None)
            if not isinstance(info, dict):
                info = {}
            fast_info = getattr(t, "fast_info", None)
            raw_fcf = info.get("freeCashflow") if isinstance(info, dict) else None
            if fcf is None and raw_fcf is not None and isinstance(raw_fcf, (int, float)):
                fcf = float(raw_fcf)
        except Exception:
            info = {}
            fast_info = None

        has_pos_ocf = (ocf_val is not None and isinstance(ocf_val, (int, float)) and ocf_val > 0.0)
        has_pos_fcf = (fcf is not None and isinstance(fcf, (int, float)) and fcf > 0.0)
        has_positive_cash_flow = (has_pos_ocf or has_pos_fcf)

        result["operating_cash_flow_ttm"] = ocf_val
        result["free_cash_flow"] = fcf
        result["has_positive_cash_flow"] = has_positive_cash_flow
        result["cash_flow_viable"] = has_positive_cash_flow

        # ---- 3. Cash Runway & Cash Burn Gate ----
        # 1. Jos TTM OCF on positiivinen, Runway on "Infinite" (maksipisteet).
        # 2. Jos TTM OCF on negatiivinen, laske Runway_Years = Adjusted_Cash / abs(TTM_OperatingCashFlow).
        # 3. Jos Runway_Years < 1.0 (kassa riittää alle vuoden), yhtiö hylätään suoraan (Status: REJECTED_LOW_RUNWAY).
        if ocf_val is not None and ocf_val > 0.0:
            result["cash_runway_years"] = "Infinite"
            result["cash_runway_months"] = "Infinite"
            result["runway_status"] = "RUNWAY_OK"
            result["data_quality"]["cash_burner"] = False
        elif ocf_val is not None and ocf_val < 0.0:
            result["data_quality"]["cash_burner"] = True
            runway_years = round(adjusted_cash / abs(ocf_val), 2)
            runway_months = round(runway_years * 12.0, 1)
            result["cash_runway_years"] = runway_years
            result["cash_runway_months"] = runway_months
            if runway_years < 1.0:
                result["status"] = "REJECTED_LOW_RUNWAY"
                result["runway_status"] = "REJECTED_LOW_RUNWAY"
                result["errors"].append(
                    f"REJECTED_LOW_RUNWAY: Cash runway {runway_years:.2f} years (< 1.0 year) with negative TTM OCF ({ocf_val:,.0f})."
                )
            else:
                result["runway_status"] = "RUNWAY_OK"
                result["errors"].append(
                    f"NEGATIVE_CASH_FLOW: TTM OCF is negative ({ocf_val:,.0f}), runway is {runway_years:.1f} years ({runway_months:.0f} mo)."
                )
        else:
            if has_positive_cash_flow:
                result["cash_runway_years"] = "Infinite"
                result["cash_runway_months"] = "Infinite"
                result["runway_status"] = "RUNWAY_OK"
                result["data_quality"]["cash_burner"] = False
            else:
                result["data_quality"]["cash_burner"] = True
                result["cash_runway_years"] = 0.0
                result["cash_runway_months"] = 0.0
                result["status"] = "REJECTED_LOW_RUNWAY"
                result["runway_status"] = "REJECTED_LOW_RUNWAY"
                result["errors"].append("ZERO_OR_MISSING_CASH_FLOW: No positive operating cash flow detected.")

        # ---- 4. Liikevaihto, TTM vs. Vuositilinpäätös -täsmäytys, 3v CAGR ja YoY-kasvu ----
        inc_q = _get_first_valid_df(t, ["quarterly_financials", "quarterly_income_stmt"])
        inc_a = _get_first_valid_df(t, ["financials", "income_stmt"])

        rev_curr: Optional[float] = None
        rev_prev: Optional[float] = None
        rev_ttm_4q: Optional[float] = None
        rev_annual_latest: Optional[float] = None
        rev_cagr_3y_pct: Optional[float] = None
        latest_q_dt: Optional[pd.Timestamp] = None
        latest_a_dt: Optional[pd.Timestamp] = None

        rev_annual_yoy_pct: Optional[float] = None
        # 4a. Extract annual series, latest annual revenue, annual YoY, and 3-year CAGR
        if inc_a is not None and not inc_a.empty:
            for key in _REVENUE_KEYS:
                if key in inc_a.index:
                    s_a = inc_a.loc[key].dropna()
                    if len(s_a) >= 1:
                        rev_annual_latest = float(s_a.iloc[0])
                        try:
                            latest_a_dt = pd.to_datetime(s_a.index[0]).tz_localize(None)
                        except Exception:
                            latest_a_dt = None
                    if len(s_a) >= 2:
                        r_prev_a = float(s_a.iloc[1])
                        if r_prev_a != 0 and rev_annual_latest is not None:
                            rev_annual_yoy_pct = round(((rev_annual_latest - r_prev_a) / abs(r_prev_a)) * 100.0, 4)
                        oldest_idx = min(3, len(s_a) - 1)
                        r_old = float(s_a.iloc[oldest_idx])
                        if r_old > 0 and rev_annual_latest is not None and rev_annual_latest > 0:
                            n_yrs = float(oldest_idx)
                            try:
                                d_a0 = pd.to_datetime(s_a.index[0]).tz_localize(None)
                                d_an = pd.to_datetime(s_a.index[oldest_idx]).tz_localize(None)
                                calc_yrs = abs((d_a0 - d_an).days) / 365.25
                                if 0.8 <= calc_yrs <= 4.5:
                                    n_yrs = calc_yrs
                            except Exception:
                                pass
                            if n_yrs > 0:
                                rev_cagr_3y_pct = round((((rev_annual_latest / r_old) ** (1.0 / n_yrs)) - 1.0) * 100.0, 4)
                    break

        # 4b. Extract 4Q TTM sum and date-verified quarterly YoY from inc_q
        if inc_q is not None and not inc_q.empty:
            for key in _REVENUE_KEYS:
                if key in inc_q.index:
                    series = inc_q.loc[key].dropna()
                    if len(series) >= 1:
                        try:
                            latest_q_dt = pd.to_datetime(series.index[0]).tz_localize(None)
                        except Exception:
                            latest_q_dt = None
                    if len(series) >= 4:
                        try:
                            dq0 = pd.to_datetime(series.index[0]).tz_localize(None)
                            dq3 = pd.to_datetime(series.index[3]).tz_localize(None)
                            if 240 <= abs((dq0 - dq3).days) <= 310:
                                rev_ttm_4q = float(series.iloc[:4].sum())
                        except Exception:
                            raw_q = inc_q.loc[key]
                            if len(raw_q) >= 4 and not raw_q.iloc[:4].isna().any():
                                rev_ttm_4q = float(raw_q.iloc[:4].sum())

                    if len(series) >= 8:
                        try:
                            d0 = pd.to_datetime(series.index[0]).tz_localize(None)
                            d3 = pd.to_datetime(series.index[3]).tz_localize(None)
                            d4 = pd.to_datetime(series.index[4]).tz_localize(None)
                            d7 = pd.to_datetime(series.index[7]).tz_localize(None)
                            if 240 <= abs((d0 - d3).days) <= 310 and 330 <= abs((d0 - d4).days) <= 400 and 240 <= abs((d4 - d7).days) <= 310:
                                rev_curr = float(series.iloc[:4].sum())
                                rev_prev = float(series.iloc[4:8].sum())
                                result["data_quality"]["rev_yoy_source"] = "ttm_8q_verified"
                                break
                        except Exception:
                            pass

                    if len(series) >= 2 and rev_curr is None:
                        try:
                            for curr_idx in range(min(2, len(series))):
                                d_curr = pd.to_datetime(series.index[curr_idx]).tz_localize(None)
                                for q_idx in range(curr_idx + 1, len(series)):
                                    d_cand = pd.to_datetime(series.index[q_idx]).tz_localize(None)
                                    day_diff = abs((d_curr - d_cand).days)
                                    if 330 <= day_diff <= 400:
                                        rev_curr = float(series.iloc[curr_idx])
                                        rev_prev = float(series.iloc[q_idx])
                                        result["data_quality"]["rev_yoy_source"] = (
                                            f"quarterly_yoy_verified ({str(series.index[curr_idx])[:10]} vs {str(series.index[q_idx])[:10]})"
                                        )
                                        break
                                if rev_curr is not None:
                                    break
                            if rev_curr is not None:
                                break
                        except Exception:
                            raw_row = inc_q.loc[key]
                            if len(raw_row) >= 5 and not raw_row.iloc[:5].isna().any():
                                rev_curr = float(raw_row.iloc[0])
                                rev_prev = float(raw_row.iloc[4])
                                break

        if (rev_curr is None or rev_prev is None) and inc_a is not None and not inc_a.empty:
            for key in _REVENUE_KEYS:
                if key in inc_a.index:
                    series = inc_a.loc[key].dropna()
                    if len(series) >= 1 and rev_curr is None:
                        rev_curr = float(series.iloc[0])
                    if len(series) >= 2 and rev_prev is None:
                        rev_prev = float(series.iloc[1])
                        result["data_quality"]["rev_yoy_source"] = "annual_yoy_fallback"
                    if rev_curr is not None:
                        break

        # 4c. Sanity check: Reconcile 4Q TTM sum vs Latest Annual Revenue
        result["revenue_ttm_4q"] = rev_ttm_4q
        result["revenue_annual_latest"] = rev_annual_latest
        result["revenue_annual_yoy_pct"] = rev_annual_yoy_pct
        result["revenue_growth_annual_yoy_pct"] = rev_annual_yoy_pct
        result["revenue_cagr_3y_pct"] = rev_cagr_3y_pct
        if rev_ttm_4q is not None and rev_annual_latest is not None and rev_annual_latest > 0:
            ttm_vs_ann_diff_pct = round(((rev_ttm_4q - rev_annual_latest) / rev_annual_latest) * 100.0, 2)
            result["data_quality"]["ttm_vs_annual_diff_pct"] = ttm_vs_ann_diff_pct
            same_end_period = (
                latest_q_dt is not None
                and latest_a_dt is not None
                and abs((latest_q_dt - latest_a_dt).days) <= 15
            )
            if same_end_period and abs(ttm_vs_ann_diff_pct) > 5.0:
                if "TTM_ANNUAL_MISMATCH_ERROR" not in result["audit_flags"]:
                    result["audit_flags"].append("TTM_ANNUAL_MISMATCH_ERROR")
                result["errors"].append(
                    f"TTM_ANNUAL_MISMATCH_ERROR: 4Q sum ({rev_ttm_4q:,.0f}) differs from annual ({rev_annual_latest:,.0f}) by {ttm_vs_ann_diff_pct:+.1f}% on same date {str(latest_a_dt)[:10]}."
                )
            elif latest_q_dt is not None and latest_a_dt is not None and latest_q_dt > latest_a_dt:
                result["data_quality"]["ttm_newer_than_annual"] = True
                if abs(ttm_vs_ann_diff_pct) > 25.0:
                    if "TTM_ACCELERATION_OR_MA_DIVERGENCE" not in result["audit_flags"]:
                        result["audit_flags"].append("TTM_ACCELERATION_OR_MA_DIVERGENCE")

        result["total_revenue_current"] = rev_curr
        result["total_revenue_previous"] = rev_prev
        result["revenue_current"] = rev_curr
        result["revenue_previous"] = rev_prev

        if rev_curr is not None and rev_prev not in (None, 0):
            result["revenue_growth_yoy_pct"] = round(
                ((rev_curr - rev_prev) / abs(rev_prev)) * 100.0, 4
            )

        # ---- 5. Bruttokate (Gross Profit & Gross Margin %) ----
        gp_a = _extract_metric_from_df(inc_a, _GROSS_PROFIT_KEYS)
        rev_a = _extract_metric_from_df(inc_a, _REVENUE_KEYS)
        gp_q = _extract_metric_from_df(inc_q, _GROSS_PROFIT_KEYS)
        rev_q = _extract_metric_from_df(inc_q, _REVENUE_KEYS)

        gp_val = gp_a if gp_a is not None else gp_q
        gm_pct = None
        if gp_a is not None and rev_a is not None and rev_a > 0:
            gm_pct = round((gp_a / rev_a) * 100.0, 2)
        elif gp_q is not None and rev_q is not None and rev_q > 0:
            gm_pct = round((gp_q / rev_q) * 100.0, 2)
        elif gp_val is not None and rev_curr is not None and rev_curr > 0:
            gm_pct = round((gp_val / rev_curr) * 100.0, 2)

        result["gross_profit"] = gp_val
        result["gross_margin_pct"] = gm_pct

        # ---- 6 & 7. Unified Period Basis & Per-Statement Freshness Check (Oldest Statement Governs) ----
        try:
            info = getattr(t, "info", None) or {}
            fast_info = getattr(t, "fast_info", None)

            # ---- UNIFIED PERIOD BASIS (4Q TTM Primary vs. Annual Latest Fallback) ----
            # Ensures Revenue, EBIT, EBITDA, Net Income, EBIT Margin, ROE, ROIC, Net Debt/EBITDA, and EV/EBIT
            # all use the exact same time period (either 4Q TTM or Latest Annual).
            _ebit_keys_list = ["Operating Income", "OperatingIncome", "Total Operating Income As Reported", "EBIT"]
            _ebitda_keys_list = ["EBITDA", "Normalized EBITDA", "NormalizedEBITDA", "EBIT", "Operating Income", "OperatingIncome"]
            _net_inc_keys_list = ["Net Income", "NetIncome", "Net Income Common Stockholders", "Net Income Continuous Operations"]

            ebit_ttm_4q = _extract_4q_ttm_from_df(inc_q, _ebit_keys_list)
            ebitda_ttm_4q = _extract_4q_ttm_from_df(inc_q, _ebitda_keys_list)
            net_inc_ttm_4q = _extract_4q_ttm_from_df(inc_q, _net_inc_keys_list)

            inc_a_latest_col = inc_a.iloc[:, [0]] if (inc_a is not None and not inc_a.empty and len(inc_a.columns) > 0) else inc_a
            ebit_ann = _extract_metric_from_df(inc_a_latest_col, _ebit_keys_list)
            if ebit_ann is None:
                ebit_ann = _extract_metric_from_df(inc_a, _ebit_keys_list)
            ebitda_ann = _extract_metric_from_df(inc_a_latest_col, _ebitda_keys_list)
            if ebitda_ann is None:
                ebitda_ann = _extract_metric_from_df(inc_a, _ebitda_keys_list)
            net_inc_ann = _extract_metric_from_df(inc_a_latest_col, _net_inc_keys_list)
            if net_inc_ann is None:
                net_inc_ann = _extract_metric_from_df(inc_a, _net_inc_keys_list)

            if rev_ttm_4q is not None and rev_ttm_4q > 0 and ebit_ttm_4q is not None:
                period_basis = "TTM_4Q"
                unified_rev = float(rev_ttm_4q)
                unified_ebit = float(ebit_ttm_4q)
                info_ebitda = info.get("ebitda")
                if isinstance(info_ebitda, (int, float)) and float(info_ebitda) >= unified_ebit:
                    unified_ebitda = float(info_ebitda)
                elif ebitda_ttm_4q is not None and float(ebitda_ttm_4q) >= unified_ebit:
                    unified_ebitda = float(ebitda_ttm_4q)
                else:
                    unified_ebitda = unified_ebit
                unified_net_inc = net_inc_ttm_4q
            elif rev_annual_latest is not None and rev_annual_latest > 0 and ebit_ann is not None:
                period_basis = "ANNUAL_LATEST"
                unified_rev = float(rev_annual_latest)
                unified_ebit = float(ebit_ann)
                unified_ebitda = float(ebitda_ann) if ebitda_ann is not None else unified_ebit
                unified_net_inc = net_inc_ann
            else:
                period_basis = "INFO_OR_PARTIAL_FALLBACK"
                unified_rev = rev_curr
                unified_ebit = (
                    ebit_ttm_4q
                    if ebit_ttm_4q is not None
                    else (ebit_ann if ebit_ann is not None else info.get("operatingIncome"))
                )
                unified_ebitda = info.get("ebitda") or ebitda_ttm_4q or ebitda_ann or unified_ebit
                unified_net_inc = net_inc_ttm_4q if net_inc_ttm_4q is not None else net_inc_ann

            result["data_quality"]["metrics_period_basis"] = period_basis

            # ---- Per-Statement Data Freshness Check (Balance Sheet, Income Statement, Cash Flow) ----
            # Calculates age for each statement actually used in the metrics and sets data_age_days
            # to the OLDEST of the used statements (e.g. if BS is 97d old but Income Statement is 278d old, data_age_days = 278d).
            bs_used_df = bs_q if (bs_q is not None and not bs_q.empty and len(bs_q.columns) > 0) else bs_a
            if period_basis == "TTM_4Q" and inc_q is not None and not inc_q.empty and len(inc_q.columns) > 0:
                inc_used_df = inc_q
            elif period_basis == "ANNUAL_LATEST" and inc_a is not None and not inc_a.empty and len(inc_a.columns) > 0:
                inc_used_df = inc_a
            else:
                inc_used_df = inc_q if (inc_q is not None and not inc_q.empty and len(inc_q.columns) > 0) else inc_a

            ocf_src = result["data_quality"].get("ocf_source")
            if ocf_src == "latest_annual" and cf_a is not None and not cf_a.empty and len(cf_a.columns) > 0:
                cf_used_df = cf_a
            else:
                cf_used_df = cf_q if (cf_q is not None and not cf_q.empty and len(cf_q.columns) > 0) else cf_a

            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            stmt_dates: Dict[str, Optional[str]] = {"balance_sheet": None, "income_statement": None, "cash_flow": None}
            stmt_ages: Dict[str, Optional[int]] = {"balance_sheet": None, "income_statement": None, "cash_flow": None}
            valid_dts: List[pd.Timestamp] = []

            for st_name, st_df in [("balance_sheet", bs_used_df), ("income_statement", inc_used_df), ("cash_flow", cf_used_df)]:
                if st_df is not None and not st_df.empty and len(st_df.columns) > 0:
                    try:
                        c_dt = pd.to_datetime(st_df.columns[0]).tz_localize(None)
                        stmt_dates[st_name] = c_dt.strftime("%Y-%m-%d")
                        stmt_ages[st_name] = (now_utc - c_dt).days
                        valid_dts.append(c_dt)
                    except Exception:
                        pass

            result["statement_dates"] = stmt_dates
            result["statement_ages_days"] = stmt_ages

            if valid_dts:
                oldest_dt = min(valid_dts)
                newest_dt = max(valid_dts)
                age_days = (now_utc - oldest_dt).days
                result["oldest_statement_date"] = oldest_dt.strftime("%Y-%m-%d")
                result["newest_statement_date"] = newest_dt.strftime("%Y-%m-%d")
                result["latest_statement_date"] = oldest_dt.strftime("%Y-%m-%d")
                result["data_age_days"] = age_days
                is_fresh = age_days <= 120
                result["data_quality"]["is_fresh"] = is_fresh
                if not is_fresh:
                    if "STALE_FINANCIAL_DATA" not in result["audit_flags"]:
                        result["audit_flags"].append("STALE_FINANCIAL_DATA")
                    result["errors"].append(
                        f"DATA_STALENESS_WARNING: Oldest used statement ({oldest_dt.strftime('%Y-%m-%d')}) is {age_days} days old (>120d). Statements: {stmt_dates}"
                    )
            result["ebit_ttm_or_unified"] = float(unified_ebit) if unified_ebit is not None else None

            # Equity from latest quarterly balance sheet (fallback to annual)
            equity_val = _extract_metric_from_df(bs_q, ["Stockholders Equity", "StockholdersEquity", "Total Stockholder Equity", "Common Stock Equity"])
            if equity_val is None:
                equity_val = _extract_metric_from_df(bs_a, ["Stockholders Equity", "StockholdersEquity", "Total Stockholder Equity", "Common Stock Equity"])

            # ROE (Return on Equity) on unified period
            roe = None
            if unified_net_inc is not None and equity_val is not None and equity_val > 0:
                roe = float(unified_net_inc) / float(equity_val)
            if roe is None:
                roe = info.get("returnOnEquity")
            result["roe"] = float(roe) if roe is not None else None

            # ROIC (Return on Invested Capital) on unified period
            # Formula: NOPAT = Unified_EBIT * (1 - 0.206)
            # Invested Capital = Equity + max(0.0, Total_Debt - Cash)
            # Note: Net cash (Cash > Debt) is NOT subtracted from Stockholders' Equity,
            # so cash-rich companies do not get an artificially shrunken denominator and inflated ROIC!
            roic_val = None
            if unified_ebit is not None and equity_val is not None and equity_val > 0:
                nopat = float(unified_ebit) * (1.0 - 0.206)
                net_debt_positive = max(0.0, float(debt or 0.0) - float(cash or 0.0))
                inv_cap = max(float(equity_val) + net_debt_positive, 1.0)
                roic_val = round(nopat / inv_cap, 4)
            result["roic"] = roic_val

            # EBIT Margin on unified period
            ebit_m = None
            if unified_ebit is not None and unified_rev is not None and unified_rev > 0:
                ebit_m = float(unified_ebit) / float(unified_rev)
            if ebit_m is None:
                ebit_m = info.get("operatingMargins") or info.get("ebitMargins")
                if ebit_m is None:
                    op_inc_q = _extract_metric_from_df(inc_q, _ebit_keys_list)
                    if op_inc_q is not None and rev_q is not None and rev_q > 0:
                        ebit_m = op_inc_q / rev_q
            result["ebit_margin"] = float(ebit_m) if ebit_m is not None else None

            # Free Cash Flow (preserve Section 2 TTM OCF - 4Q CapEx if already computed)
            if result.get("free_cash_flow") is None:
                fcf_info = info.get("freeCashflow")
                if fcf_info is not None:
                    result["free_cash_flow"] = float(fcf_info)

            # EBITDA & Net Debt to EBITDA on unified period
            ebitda = unified_ebitda
            result["ebitda"] = float(ebitda) if ebitda is not None else None

            nd_ebitda = None
            if debt is not None and cash is not None:
                net_d = debt - cash
                if net_d <= 0:
                    nd_ebitda = 0.0  # Net cash -> solvent
                elif ebitda is not None and ebitda > 0:
                    nd_ebitda = round(net_d / float(ebitda), 2)
            result["net_debt_to_ebitda"] = nd_ebitda

            # Dividend Yield, Dividend Rate & Cash Dividends Paid (Strict OST verification)
            div_y = info.get("dividendYield")
            if div_y is None:
                div_y = info.get("trailingAnnualDividendYield")
            div_r = info.get("dividendRate")
            trail_div_r = info.get("trailingAnnualDividendRate")
            _div_cf_keys = ["Cash Dividends Paid", "Common Stock Dividend Paid", "CashDividendsPaid", "Payment Of Dividends"]
            div_paid = _extract_metric_from_df(cf_a, _div_cf_keys)
            if div_paid is None:
                div_paid = _extract_metric_from_df(cf_q, _div_cf_keys)

            result["dividend_rate"] = float(div_r) if div_r is not None else None
            result["trailing_annual_dividend_rate"] = float(trail_div_r) if trail_div_r is not None else None
            result["cash_dividends_paid"] = float(div_paid) if div_paid is not None else None

            if div_y is not None:
                result["dividend_yield"] = float(div_y)
            elif (
                (div_r is not None and float(div_r) > 0.0)
                or (trail_div_r is not None and float(trail_div_r) > 0.0)
                or (div_paid is not None and float(div_paid) < -1000.0)
            ):
                # Non-zero dividend detected via rate or cashflow statement even though dividendYield was None
                result["dividend_yield"] = 0.01
            else:
                result["dividend_yield"] = 0.0

            # Exchange, Country, Sector & Industry
            exch = info.get("exchange")
            result["exchange"] = str(exch).upper() if exch else None
            country_val = info.get("country")
            result["country"] = str(country_val).strip() if country_val else None
            sec_val = info.get("sector")
            result["sector"] = str(sec_val).strip() if sec_val else None
            ind_val = info.get("industry")
            result["industry"] = str(ind_val).strip() if ind_val else None

            # Market Cap & FCF Yield %
            mcap = (fast_info and getattr(fast_info, "market_cap", None)) or info.get("marketCap")
            result["market_cap"] = float(mcap) if mcap is not None else None
            fcf_for_yield = result.get("free_cash_flow")
            if fcf_for_yield is not None and mcap is not None and float(mcap) > 0:
                result["fcf_yield_pct"] = round((float(fcf_for_yield) / float(mcap)) * 100.0, 2)
            else:
                result["fcf_yield_pct"] = None

            # Bid-Ask Spread % (Real-Time Liquidity Friction)
            bid_px = info.get("bid")
            ask_px = info.get("ask")
            if (
                isinstance(bid_px, (int, float))
                and isinstance(ask_px, (int, float))
                and bid_px > 0
                and ask_px >= bid_px
            ):
                result["bid_ask_spread_pct"] = round(((float(ask_px) - float(bid_px)) / float(ask_px)) * 100.0, 3)

            # Earnings Calendar Gate (Next Earnings Date & Days to Earnings)
            earn_ts = (
                info.get("earningsTimestamp")
                or info.get("earningsTimestampStart")
                or info.get("earningsCallTimestampStart")
            )
            earn_dt_obj = None
            if isinstance(earn_ts, (int, float)) and earn_ts > 0:
                try:
                    earn_dt_obj = datetime.fromtimestamp(float(earn_ts), tz=timezone.utc).date()
                except Exception:
                    earn_dt_obj = None
            if earn_dt_obj is not None:
                today_utc = datetime.now(timezone.utc).date()
                d_to_earn = (earn_dt_obj - today_utc).days
                if d_to_earn >= -1:
                    result["next_earnings_date"] = earn_dt_obj.strftime("%Y-%m-%d")
                    result["days_to_earnings"] = int(d_to_earn)

            # Analyst Coverage Count (for Neglected Small-Cap Satellite filter)
            # IMPORTANT: Missing Yahoo data (None) is kept as None (UNVERIFIED), never converted to 0!
            n_analysts = info.get("numberOfAnalystOpinions")
            if isinstance(n_analysts, (int, float)) and not isinstance(n_analysts, bool) and n_analysts >= 0:
                result["analyst_count"] = int(n_analysts)
                result["data_quality"]["analyst_coverage_status"] = "VERIFIED"
            else:
                result["analyst_count"] = None
                result["data_quality"]["analyst_coverage_status"] = "MISSING_UNVERIFIED"

            # Insider Skin-in-the-Game & Net Buying Signal
            ins_pct = info.get("heldPercentInsiders")
            if isinstance(ins_pct, (int, float)) and ins_pct >= 0:
                result["held_percent_insiders"] = round(float(ins_pct) * 100.0 if float(ins_pct) <= 1.0 else float(ins_pct), 2)

            net_ins = info.get("netPercentInsiderShares") or info.get("netInsiderPurchasePercent")
            if isinstance(net_ins, (int, float)) and float(net_ins) > 0.0:
                result["insider_buying_signal"] = True

            # Average Daily Volume & Turnover in EUR (Liquidity Gate)
            adv_shares = info.get("averageDailyVolume10Day") or info.get("averageVolume") or info.get("averageVolume10days")
            px_cur = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
            if (
                isinstance(adv_shares, (int, float))
                and isinstance(px_cur, (int, float))
                and adv_shares > 0
                and px_cur > 0
            ):
                t_curr = str(info.get("currency") or "EUR").strip().upper()
                turnover_local = float(adv_shares) * float(px_cur)
                live_fx = get_live_fx_rates()
                result["fx_rates"] = live_fx
                fx_to_eur = 1.0
                if t_curr == "SEK":
                    fx_to_eur = 1.0 / float(live_fx.get("EURSEK", 11.30))
                elif t_curr == "NOK":
                    fx_to_eur = 1.0 / float(live_fx.get("EURNOK", 11.50))
                elif t_curr == "DKK":
                    fx_to_eur = 1.0 / float(live_fx.get("EURDKK", 7.46))
                elif t_curr == "USD":
                    fx_to_eur = 1.0 / float(live_fx.get("EURUSD", 1.08))
                avg_turnover_eur = round(turnover_local * fx_to_eur, 2)
                result["avg_daily_turnover_eur"] = avg_turnover_eur

            # Strict Liquidity Check: Bid-Ask Spread > 3.0% OR Turnover < 50,000 EUR
            if result.get("bid_ask_spread_pct") is not None and float(result["bid_ask_spread_pct"]) > 3.0:
                result["status"] = "REJECTED_LOW_LIQUIDITY"
                result["liquidity_status"] = "REJECTED_LOW_LIQUIDITY"
                result["errors"].append(f"REJECTED_LOW_LIQUIDITY: Bid-ask spread {float(result['bid_ask_spread_pct']):.2f}% > 3.0% threshold.")
            elif result.get("avg_daily_turnover_eur") is not None and float(result["avg_daily_turnover_eur"]) < 50_000.0:
                result["status"] = "REJECTED_LOW_LIQUIDITY"
                result["liquidity_status"] = "REJECTED_LOW_LIQUIDITY"
                result["errors"].append(f"REJECTED_LOW_LIQUIDITY: Average daily turnover {float(result['avg_daily_turnover_eur']):,.0f} EUR < 50,000 EUR threshold.")
            else:
                result["liquidity_status"] = "LIQUID_OK"

            # Relative Valuation: EV/EBIT vs. 5-year Historical Median (strictly using unified TTM/Annual EBIT!)
            curr_ev = info.get("enterpriseValue")
            if curr_ev is None and mcap is not None and debt is not None and cash is not None:
                curr_ev = float(mcap) + float(debt) - float(cash)
            curr_ebit = unified_ebit if unified_ebit is not None else (info.get("operatingIncome") or ebitda)

            rel_val = calculate_relative_ev_ebit(
                t=t,
                info=info,
                bs_a=bs_a,
                inc_a=inc_a,
                current_ev=float(curr_ev) if curr_ev is not None else None,
                current_ebit=float(curr_ebit) if curr_ebit is not None else None,
            )
            result["current_ev_ebit"] = rel_val.get("current_ev_ebit")
            result["ev_ebit_5y_median"] = rel_val.get("ev_ebit_5y_median")
            result["ev_ebit_premium_pct"] = rel_val.get("ev_ebit_premium_pct")
            result["is_historically_expensive"] = rel_val.get("is_historically_expensive", False)
            result["historical_ev_ebit_years_count"] = rel_val.get("historical_ev_ebit_years_count", 0)
            result["valuation_status"] = rel_val.get("valuation_status", "UNKNOWN")
            if result["valuation_status"] == "PARTIAL_4Y_HISTORY":
                if "VALUATION_4Y_FALLBACK" not in result["audit_flags"]:
                    result["audit_flags"].append("VALUATION_4Y_FALLBACK")

            # 8. Osakeanti- ja diluutiolippu (Share Dilution Flag)
            dil_res = check_share_dilution(t=t, info=info, bs_a=bs_a, bs_q=bs_q, inc_a=inc_a, inc_q=inc_q)
            result["shares_outstanding_current"] = dil_res.get("shares_outstanding_current")
            result["shares_outstanding_historical"] = dil_res.get("shares_outstanding_historical")
            result["share_dilution_pct"] = dil_res.get("share_dilution_pct")
            result["has_share_dilution"] = dil_res.get("has_share_dilution", False)
            result["dilution_flag"] = dil_res.get("dilution_flag")
            if dil_res.get("has_share_dilution") and dil_res.get("dilution_flag"):
                d_flag = dil_res["dilution_flag"]
                if d_flag not in result["audit_flags"]:
                    result["audit_flags"].append(d_flag)
                result["audit_flag"] = d_flag
        except Exception as e_extra:
            logger.warning(f"Tiered metrics extraction error for {cleaned_ticker}: {e_extra}", exc_info=True)
            result["errors"].append(f"TIERED_METRICS_ERROR: {e_extra}")

        has_any_data = (cash is not None or ocf_val is not None or rev_curr is not None)
        if result.get("status") not in ("REJECTED_LOW_LIQUIDITY", "REJECTED_LOW_RUNWAY"):
            result["status"] = "OK" if has_any_data else "PARTIAL" 

    except Exception as e:
        logger.warning(f"Could not retrieve hard financials for {cleaned_ticker}: {e}")
        result["errors"].append(str(e))
        result["status"] = "ERROR"

    try:
        prof_eval = evaluate_profiles(result)
        result["is_profile_a"] = prof_eval.get("is_profile_a", False)
        result["is_profile_b"] = prof_eval.get("is_profile_b", False)
        result["profile_signal"] = prof_eval.get("signal", "REJECT")
    except Exception as e_prof:
        logger.warning(f"Profile evaluation error for {cleaned_ticker}: {e_prof}", exc_info=True)
        result["is_profile_a"] = False
        result["is_profile_b"] = False
        result["profile_signal"] = "REJECT"

    return result


def evaluate_profiles(hard_facts: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates deterministic Profile A (Quality Growth) and Profile B (Value & Anti-Shrinking)
    rules on structured point-in-time or yfinance hard facts dictionary.

    Profile A (Quality Growth):
      - revenue_growth_yoy > 20%
      - gross_margin > 40% (or gross profit proves scalability)
      - Absolute Cash Flow Rule: Must have positive operating cash flow (OCF > 0)
        or positive free cash flow (FCF > 0). Early-stage cash burners cannot qualify
        on runway alone!
      - Survival Check: (OCF > 0 or FCF > 0) AND (OCF > 0 OR cash_runway_months > 18)

    Profile B (Deep Value & Anti-Shrinking):
      - net_cash > 0
      - Absolute Cash Flow Rule: Must have positive operating cash flow (OCF > 0)
        or positive free cash flow (FCF > 0). Cash-burning early-stage R&D firms
        are rejected.

    Returns:
    {
        "is_profile_a": bool,
        "is_profile_b": bool,
        "signal": "BUY_PROFILE_A" | "BUY_PROFILE_B" | "BUY_BOTH" | "REJECT",
        "profile": "PROFILE_A (Growth)" | "PROFILE_B (Value)" | "PROFILE_A & B" | "NONE",
        "reasons": List[str]
    }
    """
    runway_status = hard_facts.get("runway_status")
    if runway_status == "REJECTED_LOW_RUNWAY" or hard_facts.get("status") == "REJECTED_LOW_RUNWAY":
        return {
            "is_profile_a": False,
            "is_profile_b": False,
            "signal": "REJECT",
            "profile": "NONE",
            "reasons": ["REJECTED_LOW_RUNWAY: Cash runway < 1.0 year with negative TTM operating cash flow"],
        }

    rev_yoy = hard_facts.get("revenue_growth_yoy_pct") if hard_facts.get("revenue_growth_yoy_pct") is not None else hard_facts.get("revenue_yoy")
    gm_pct = hard_facts.get("gross_margin_pct") if hard_facts.get("gross_margin_pct") is not None else hard_facts.get("gross_margin")
    net_cash = hard_facts.get("net_cash")
    ocf = hard_facts.get("operating_cash_flow_ttm") if hard_facts.get("operating_cash_flow_ttm") is not None else hard_facts.get("ocf")
    fcf = hard_facts.get("free_cash_flow") if hard_facts.get("free_cash_flow") is not None else hard_facts.get("fcf")
    runway = hard_facts.get("cash_runway_months")

    # Positiivisen kassavirran ehdoton tarkistus (OCF > 0 tai FCF > 0)
    has_pos_cf = bool(hard_facts.get("has_positive_cash_flow", False)) or (
        (ocf is not None and float(ocf) > 0.0) or (fcf is not None and float(fcf) > 0.0)
    )

    # Parse numeric runway
    runway_num = float("inf")
    if runway is not None:
        if isinstance(runway, (int, float)):
            runway_num = float(runway)
        elif str(runway).strip().lower() == "infinite":
            runway_num = float("inf") if has_pos_cf else 0.0
        else:
            try:
                runway_num = float(runway)
            except (ValueError, TypeError):
                runway_num = 0.0

    # 1. Profile A: Quality Growth
    is_profile_a = False
    reasons_a: List[str] = []

    growth_ok = (rev_yoy is not None and float(rev_yoy) > 20.0)
    margin_ok = (gm_pct is not None and float(gm_pct) > 40.0)
    survival_ok = has_pos_cf and ((ocf is not None and float(ocf) > 0.0) or (runway_num > 18.0))

    if growth_ok and margin_ok and survival_ok:
        is_profile_a = True
    else:
        if not growth_ok:
            reasons_a.append(f"Revenue growth {rev_yoy}% <= 20%")
        if not margin_ok:
            reasons_a.append(f"Gross margin {gm_pct}% <= 40%")
        if not survival_ok:
            if not has_pos_cf:
                reasons_a.append(f"Survival check failed: Non-positive cash flow (OCF={ocf}, FCF={fcf}) - cash burners rejected from Profile A")
            else:
                reasons_a.append(f"Survival check failed: OCF={ocf}, Runway={runway}m <= 18m")

    # 2. Profile B: Deep Value & Anti-Shrinking
    is_profile_b = False
    reasons_b: List[str] = []

    if net_cash is not None and float(net_cash) > 0:
        is_shrinking = (rev_yoy is not None and float(rev_yoy) < 0.0)
        if is_shrinking:
            if ocf is not None and float(ocf) > 0.0:
                is_profile_b = True
            else:
                reasons_b.append("Anti-Shrinking rule triggered: Revenue shrinking without positive OCF")
        elif not has_pos_cf:
            reasons_b.append(f"Rejected: Negative or missing cash flow (OCF={ocf}, FCF={fcf}) - cash burners not allowed in Profile B")
        else:
            is_profile_b = True
    else:
        reasons_b.append(f"Net cash {net_cash} <= 0")

    # Determine consolidated signal
    if is_profile_a and is_profile_b:
        signal = "BUY_BOTH"
        profile = "PROFILE_A & B"
    elif is_profile_a:
        signal = "BUY_PROFILE_A"
        profile = "PROFILE_A (Growth)"
    elif is_profile_b:
        signal = "BUY_PROFILE_B"
        profile = "PROFILE_B (Value)"
    else:
        signal = "REJECT"
        profile = "NONE"

    return {
        "is_profile_a": is_profile_a,
        "is_profile_b": is_profile_b,
        "signal": signal,
        "profile": profile,
        "reasons": (reasons_a + reasons_b) if not is_profile_a and not is_profile_b else (reasons_b if not is_profile_b else (reasons_a if not is_profile_a else [])),
    }


def get_hard_financials_batch(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Batch processor for multiple tickers with strict state isolation.
    Guarantees that each ticker starts with fresh zeroed/None variables,
    preventing any state leakage between consecutive tickers.
    """
    batch_results: Dict[str, Dict[str, Any]] = {}
    for raw_ticker in tickers:
        clean_t = str(raw_ticker).strip().upper()
        if not clean_t:
            continue
        try:
            batch_results[clean_t] = get_hard_financials(clean_t)
        except Exception as e:
            logger.warning(f"Error in batch processing for {clean_t}: {e}")
            batch_results[clean_t] = {
                "ticker": clean_t,
                "status": "ERROR",
                "errors": [str(e)],
                "has_positive_cash_flow": False,
                "cash_flow_viable": False,
            }
    return batch_results


def format_for_llm_prompt(hard_facts: Dict[str, Any]) -> str:
    """
    Muotoilee get_hard_financials()-tuloksen ihmisluettavaksi (ja LLM-ystävälliseksi)
    tekstiblokiksi promptiin injektoitavaksi. Ei tee laskentaa, vain formatointia.

    Infinite ja None ilmaistaan eksplisiittisesti tekstinä, jotta LLM ei
    yritä tulkita puuttuvaa arvoa numeroksi 0.
    """
    def fmt(val: Any, suffix: str = "") -> str:
        if val is None:
            return "EI SAATAVILLA (data puuttuu - älä oleta arvoa)"
        if val == "Infinite" or val == math.inf:
            return "POSITIIVINEN / EI KASSAPOLTTOA (runway ei relevantti)"
        if isinstance(val, (float, int)):
            return f"{val:,.1f}{suffix}"
        return f"{val}{suffix}"

    if not hard_facts or hard_facts.get("status") == "ERROR":
        err_msg = "; ".join(hard_facts.get("errors", [])) if hard_facts else "Datanoutovirhe"
        return (
            "Status: EI SAATAVILLA / Datanoutovirhe\n"
            f"Huomio: Kovia lukuja ei saatu noudettua API:sta ({err_msg}). Merkitse epävarmuus."
        )

    dq = hard_facts.get("data_quality", {})
    ticker = hard_facts.get("ticker", "N/A")
    as_of = hard_facts.get("as_of", datetime.now(timezone.utc).isoformat())
    debt_src = dq.get("debt_source") or "ei tiedossa"
    ocf_src = dq.get("ocf_source") or "ei tiedossa"

    lines = [
        f"Ticker: {ticker}",
        f"Tiedot haettu (UTC): {as_of}",
        f"Kassa ja vastaavat: {fmt(hard_facts.get('cash_and_equivalents'))}",
        f"Kokonaisvelka: {fmt(hard_facts.get('total_debt'))} (lähde: {debt_src})",
        f"Nettokassa (Kassa - Velka): {fmt(hard_facts.get('net_cash'))}",
        f"Operatiivinen kassavirta (TTM/viimeisin): {fmt(hard_facts.get('operating_cash_flow_ttm'))} (lähde: {ocf_src})",
        f"Kassariittävyys (kk): {fmt(hard_facts.get('cash_runway_months'))}",
        f"Liikevaihto (viimeisin tilikausi): {fmt(hard_facts.get('total_revenue_current') or hard_facts.get('revenue_current'))}",
        f"Liikevaihto (edellinen tilikausi): {fmt(hard_facts.get('total_revenue_previous') or hard_facts.get('revenue_previous'))}",
        f"Liikevaihdon kasvu YoY: {fmt(hard_facts.get('revenue_growth_yoy_pct'), ' %')}",
    ]
    if hard_facts.get("errors"):
        lines.append("HUOM - datankeruun virheet/puutteet: " + "; ".join(hard_facts["errors"]))

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    import json

    test_ticker = sys.argv[1] if len(sys.argv) > 1 else "NOKIA.HE"
    facts = get_hard_financials(test_ticker)
    print(json.dumps(facts, indent=2, default=str))
    print("\n--- LLM-PROMPTIIN INJEKTOITAVA MUOTO ---\n")
    print(format_for_llm_prompt(facts))


def _normalize_audit_metric_pct(raw_val: Any, is_ratio_convertible: bool = True) -> Optional[float]:
    """
    Normalizes a metric to percentage points rounded to 4 decimal places (equivalent to 6 decimals in ratio form).
    Exempts exact zero (0.0) and clean integer percentages (e.g. 10.0%, 20.0%) to prevent false positives on rounded figures.
    """
    if raw_val is None or isinstance(raw_val, bool):
        return None
    try:
        vf = float(raw_val)
    except (ValueError, TypeError):
        return None
    if vf == 0.0:
        return None
    pct = (vf * 100.0) if (is_ratio_convertible and 0.0 < abs(vf) <= 1.0) else vf
    r_pct = round(pct, 4)
    if r_pct == 0.0 or r_pct == round(r_pct, 0):
        return None
    return r_pct


def audit_candidates_data(
    candidates: Any,
    previous_run_snapshot: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Any:
    """
    Audits candidate data before final scoring and EOD action list generation.

    1. Duplicate Detector (Kopioituvien lukujen hälytin):
       - Tracks exact revenue_growth and ebit_margin (rounded to 4 decimals, ignoring 0 and None).
       - If multiple candidates with different tickers share the exact same metric, flags all of them with:
         'DATA_DUPLICATE_WARNING'.

    2. Outlier Guard (Ääriarvovahti):
       - If revenue_growth > 1.0 (or > 100%), roe > 1.0 (or > 100%), or abs(ebit_margin) > 1.0 (or > 100%),
         flags with 'MANUAL_REVIEW_OUTLIER'.

    3. Dilution Guard (Osakeanti-lippu):
       - Flags 'DILUTION_WARNING_RECENT_ISSUE' if recent share dilution > 5%.

    Populates candidate['audit_flags'], candidate['audit_warning'], and propagates to
    candidate['entry_hard_facts'] / candidate['hard_facts'].
    """
    if not candidates:
        return candidates

    cand_list: list[Dict[str, Any]] = []
    if isinstance(candidates, dict):
        for sub_cands in candidates.values():
            if isinstance(sub_cands, list):
                cand_list.extend(sub_cands)
    elif isinstance(candidates, list):
        cand_list = candidates

    rev_map: Dict[float, list[Dict[str, Any]]] = {}
    ebit_map: Dict[float, list[Dict[str, Any]]] = {}
    roe_map: Dict[float, list[Dict[str, Any]]] = {}

    for cand in cand_list:
        if not isinstance(cand, dict):
            continue

        c_flags = list(cand.get("audit_flags", []))
        hard_facts = cand.get("hard_facts") or cand.get("entry_hard_facts") or {}
        if not isinstance(hard_facts, dict):
            hard_facts = {}

        # Merge existing flags from hard_facts if present
        for f in hard_facts.get("audit_flags", []):
            if f not in c_flags:
                c_flags.append(f)
        if hard_facts.get("audit_flag") and hard_facts["audit_flag"] not in c_flags:
            c_flags.append(hard_facts["audit_flag"])

        # Check dilution flag
        if hard_facts.get("has_share_dilution") or hard_facts.get("dilution_flag") == "DILUTION_WARNING_RECENT_ISSUE":
            if "DILUTION_WARNING_RECENT_ISSUE" not in c_flags:
                c_flags.append("DILUTION_WARNING_RECENT_ISSUE")

        cand["audit_flags"] = c_flags

        # Extract metrics
        rev_raw = (
            cand.get("revenue_growth")
            if cand.get("revenue_growth") is not None
            else cand.get("rev_yoy")
            if cand.get("rev_yoy") is not None
            else cand.get("revenue_growth_yoy_pct")
            if cand.get("revenue_growth_yoy_pct") is not None
            else hard_facts.get("revenue_growth_yoy_pct")
            if hard_facts.get("revenue_growth_yoy_pct") is not None
            else hard_facts.get("rev_yoy")
        )
        ebit_raw = (
            cand.get("ebit_margin")
            if cand.get("ebit_margin") is not None
            else hard_facts.get("ebit_margin")
            if hard_facts.get("ebit_margin") is not None
            else hard_facts.get("operating_margin")
        )
        roe_raw = (
            cand.get("roe")
            if cand.get("roe") is not None
            else hard_facts.get("roe")
            if hard_facts.get("roe") is not None
            else hard_facts.get("return_on_equity")
        )

        # 1. Collect for Duplicate Detector (4 decimals in percentage points; exempts 0.0% and clean integer percentages)
        rev_norm = _normalize_audit_metric_pct(rev_raw, is_ratio_convertible=True)
        if rev_norm is not None:
            rev_map.setdefault(rev_norm, []).append(cand)

        ebit_norm = _normalize_audit_metric_pct(ebit_raw, is_ratio_convertible=True)
        if ebit_norm is not None:
            ebit_map.setdefault(ebit_norm, []).append(cand)

        roe_norm = _normalize_audit_metric_pct(roe_raw, is_ratio_convertible=True)
        if roe_norm is not None:
            roe_map.setdefault(roe_norm, []).append(cand)

        # 1b. Longitudinal Audit: Timestamp & Stale / Repeated Previous Run Check
        t_sym = str(cand.get("ticker") or hard_facts.get("ticker") or "").strip().upper()
        age_days = cand.get("data_age_days") if cand.get("data_age_days") is not None else hard_facts.get("data_age_days")
        if age_days is not None:
            try:
                if float(age_days) > 120.0:
                    if "STALE_FINANCIAL_DATA" not in cand["audit_flags"]:
                        cand["audit_flags"].append("STALE_FINANCIAL_DATA")
                    cand["requires_manual_review"] = True
            except (ValueError, TypeError):
                pass

        if isinstance(previous_run_snapshot, dict) and t_sym and t_sym in previous_run_snapshot:
            prev_info = previous_run_snapshot[t_sym]
            if isinstance(prev_info, dict):
                prev_stmt = prev_info.get("latest_statement_date")
                curr_stmt = cand.get("latest_statement_date") or hard_facts.get("latest_statement_date")
                prev_rev = _normalize_audit_metric_pct(
                    prev_info.get("revenue_growth_yoy_pct")
                    if prev_info.get("revenue_growth_yoy_pct") is not None
                    else prev_info.get("rev_yoy")
                    if prev_info.get("rev_yoy") is not None
                    else prev_info.get("revenue_growth")
                )
                prev_ebit = _normalize_audit_metric_pct(prev_info.get("ebit_margin"))
                prev_roe = _normalize_audit_metric_pct(prev_info.get("roe"))
                same_val = (
                    (rev_norm is not None and prev_rev == rev_norm)
                    or (ebit_norm is not None and prev_ebit == ebit_norm)
                    or (roe_norm is not None and prev_roe == roe_norm)
                )
                stmt_stale = (prev_stmt and curr_stmt and prev_stmt == curr_stmt and age_days is not None and float(age_days) > 95.0)
                stmt_changed_same_val = (prev_stmt and curr_stmt and prev_stmt != curr_stmt and same_val)
                stale_repeated = (age_days is not None and float(age_days) > 95.0 and same_val)
                if stmt_stale or stmt_changed_same_val or stale_repeated:
                    if "REPEATED_PREV_RUN_VALUE" not in cand["audit_flags"]:
                        cand["audit_flags"].append("REPEATED_PREV_RUN_VALUE")
                    cand["requires_manual_review"] = True

        # 2. Outlier Guard
        is_outlier = False
        try:
            if rev_raw is not None:
                rf = float(rev_raw)
                if (rf > 100.0) or (0.0 < rf <= 5.0 and rf > 1.0):
                    is_outlier = True
        except (ValueError, TypeError):
            pass

        try:
            if roe_raw is not None:
                roef = float(roe_raw)
                if (roef > 100.0) or (0.0 < roef <= 5.0 and roef > 1.0):
                    is_outlier = True
        except (ValueError, TypeError):
            pass

        try:
            if ebit_raw is not None:
                ef = float(ebit_raw)
                if (abs(ef) > 100.0) or (abs(ef) <= 5.0 and abs(ef) > 1.0):
                    is_outlier = True
        except (ValueError, TypeError):
            pass

        if is_outlier:
            if "MANUAL_REVIEW_OUTLIER" not in cand["audit_flags"]:
                cand["audit_flags"].append("MANUAL_REVIEW_OUTLIER")

    # Evaluate duplicates across distinct tickers
    for metric_val, matched_cands in rev_map.items():
        distinct_tickers = {c.get("ticker") for c in matched_cands if c.get("ticker")}
        if len(distinct_tickers) > 1:
            for c in matched_cands:
                if "DATA_DUPLICATE_WARNING" not in c["audit_flags"]:
                    c["audit_flags"].append("DATA_DUPLICATE_WARNING")

    for metric_val, matched_cands in ebit_map.items():
        distinct_tickers = {c.get("ticker") for c in matched_cands if c.get("ticker")}
        if len(distinct_tickers) > 1:
            for c in matched_cands:
                if "DATA_DUPLICATE_WARNING" not in c["audit_flags"]:
                    c["audit_flags"].append("DATA_DUPLICATE_WARNING")

    for metric_val, matched_cands in roe_map.items():
        distinct_tickers = {c.get("ticker") for c in matched_cands if c.get("ticker")}
        if len(distinct_tickers) > 1:
            for c in matched_cands:
                if "DATA_DUPLICATE_WARNING" not in c["audit_flags"]:
                    c["audit_flags"].append("DATA_DUPLICATE_WARNING")

    # Generate human-readable audit_warning and sync back to hard facts
    for cand in cand_list:
        if not isinstance(cand, dict):
            continue
        flags = list(dict.fromkeys(cand.get("audit_flags", [])))
        cand["audit_flags"] = flags
        cand["audit_flag"] = flags[0] if flags else None

        if flags:
            reasons = []
            if "DATA_DUPLICATE_WARNING" in flags:
                reasons.append("Data kopioitunut kahden osakkeen välillä")
                cand["data_leak_quarantine"] = True
                cand["status"] = "REJECTED_DATA_LEAK"
            if "MANUAL_REVIEW_OUTLIER" in flags:
                reasons.append("Ääriarvo havaittu (Kasvu/ROE/EBIT > 100%)")
                cand["requires_manual_review"] = True
            if "DILUTION_WARNING_RECENT_ISSUE" in flags:
                reasons.append("Merkittävä osakediluutio/anti (> 5% kasvu)")
            for f in flags:
                if f not in ["DATA_DUPLICATE_WARNING", "MANUAL_REVIEW_OUTLIER", "DILUTION_WARNING_RECENT_ISSUE"]:
                    reasons.append(f)
            cand["audit_warning"] = "⚠️ HUOM: Vaatii manuaalisen tarkistuksen - " + "; ".join(reasons)
        else:
            cand["audit_warning"] = None

        for hf_key in ("hard_facts", "entry_hard_facts"):
            if isinstance(cand.get(hf_key), dict):
                cand[hf_key]["audit_flags"] = cand["audit_flags"]
                cand[hf_key]["audit_flag"] = cand["audit_flag"]
                cand[hf_key]["audit_warning"] = cand["audit_warning"]
                if cand.get("data_leak_quarantine"):
                    cand[hf_key]["data_leak_quarantine"] = True
                    cand[hf_key]["status"] = "REJECTED_DATA_LEAK"
                if cand.get("requires_manual_review"):
                    cand[hf_key]["requires_manual_review"] = True

    return candidates


def calculate_survival_metrics(
    adjusted_cash: float,
    ocf_val: Optional[float],
    capex_val: Optional[float] = None,
) -> Dict[str, Any]:
    """
    TASK-12: Computes cash runway and survival viability from adjusted cash, TTM OCF, and CapEx.
    """
    res: Dict[str, Any] = {
        "cash_runway_years": "Infinite",
        "cash_runway_months": "Infinite",
        "runway_status": "RUNWAY_OK",
        "cash_burner": False,
        "free_cash_flow": None,
    }
    fcf = None
    if ocf_val is not None and capex_val is not None:
        fcf = ocf_val - abs(capex_val)
    elif ocf_val is not None:
        fcf = ocf_val
    res["free_cash_flow"] = fcf

    if ocf_val is not None and ocf_val > 0.0 and (fcf is None or fcf > 0.0):
        res["cash_runway_years"] = "Infinite"
        res["cash_runway_months"] = "Infinite"
        res["runway_status"] = "RUNWAY_OK"
        res["cash_burner"] = False
    elif ocf_val is not None and ocf_val < 0.0:
        res["cash_burner"] = True
        burn = abs(fcf) if (fcf is not None and fcf < 0.0) else abs(ocf_val)
        runway_years = round(adjusted_cash / burn, 2) if burn > 0 else 0.0
        runway_months = round(runway_years * 12.0, 1)
        res["cash_runway_years"] = runway_years
        res["cash_runway_months"] = runway_months
        res["runway_status"] = "REJECTED_LOW_RUNWAY" if runway_years < 1.0 else "RUNWAY_OK"
    elif fcf is not None and fcf < 0.0:
        res["cash_burner"] = True
        burn = abs(fcf)
        runway_years = round(adjusted_cash / burn, 2) if burn > 0 else 0.0
        runway_months = round(runway_years * 12.0, 1)
        res["cash_runway_years"] = runway_years
        res["cash_runway_months"] = runway_months
        res["runway_status"] = "REJECTED_LOW_RUNWAY" if runway_years < 1.0 else "RUNWAY_OK"
    else:
        res["cash_burner"] = True
        res["cash_runway_years"] = 0.0
        res["cash_runway_months"] = 0.0
        res["runway_status"] = "REJECTED_LOW_RUNWAY"
    return res



def _norm_linear(val: Optional[float], low: float, high: float) -> float:
    """Maps val linearly from [low, high] to [0.0, 1.0] with hard clamping."""
    if val is None or isinstance(val, bool) or math.isnan(float(val)):
        return 0.0
    if high <= low:
        return 0.0
    return min(1.0, max(0.0, (float(val) - low) / (high - low)))


def calculate_candidate_score(
    ticker: str,
    hard_facts: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
    universe_filter: str = "Nordic_MidCap_Compounder",
    news_verdict: str = "HOLD",
    news_reason: str = "",
) -> tuple[float, Dict[str, Any]]:
    """
    Normalized 0-100 Continuous Candidate Scoring Function (The Hardened Path v3).

    Architecture & Guarantees:
    1. Output is strictly bounded to [0.0, 100.0] for any input.
    2. Period-consistent inputs (4Q TTM primary, Annual fallback) with ROIC Invested Capital = Equity + max(0, Debt - Cash).
    3. Multi-period Growth Blending & Organic vs. M&A Haircut:
       - Blends 3y CAGR, Annual YoY, and Quarterly YoY so a single noisy quarter does not distort the score.
       - If `organic_revenue_growth_pct` is provided, uses organic growth; otherwise if `TTM_ACCELERATION_OR_MA_DIVERGENCE`
         is flagged (>25% TTM jump over annual, typical of M&A consolidation), caps inorganic growth above 15% by 75% (max 12/20 pts).
       - If `DILUTION_WARNING_RECENT_ISSUE` (>5% share dilution) is present, applies a -8.0 pt penalty and marks
         `manual_review_required = True`.
    4. Satellite Analyst Coverage Handling:
       - Missing `analyst_count` (`None` in Yahoo) receives `0.0` neglect bonus (`MISSING_UNVERIFIED`).
       - Only verified low analyst count (`0..2` analysts -> `+6.0 pts`, `3..5` -> `+3.0 pts`) receives neglect bonus.
    """
    meta = meta or {}
    u_low = str(universe_filter or "").lower()
    audit_flags = list(hard_facts.get("audit_flags") or [])

    # Extract raw metrics (handling decimal ratios vs percentage points)
    roe_raw = hard_facts.get("roe") if hard_facts.get("roe") is not None else hard_facts.get("return_on_equity")
    roe_pct = (float(roe_raw) * 100.0 if abs(float(roe_raw)) <= 2.0 else float(roe_raw)) if roe_raw is not None else 0.0

    roic_raw = hard_facts.get("roic")
    roic_pct = (float(roic_raw) * 100.0 if abs(float(roic_raw)) <= 2.0 else float(roic_raw)) if roic_raw is not None else None

    ebit_raw = hard_facts.get("ebit_margin") if hard_facts.get("ebit_margin") is not None else hard_facts.get("operating_margin")
    ebit_pct = (float(ebit_raw) * 100.0 if abs(float(ebit_raw)) <= 2.0 else float(ebit_raw)) if ebit_raw is not None else 0.0

    rev_yoy_raw = (
        hard_facts.get("revenue_growth_yoy_pct")
        if hard_facts.get("revenue_growth_yoy_pct") is not None
        else hard_facts.get("revenue_yoy")
        if hard_facts.get("revenue_yoy") is not None
        else hard_facts.get("rev_yoy")
    )
    rev_yoy_pct = float(rev_yoy_raw) if rev_yoy_raw is not None else 0.0
    rev_ann_yoy_raw = hard_facts.get("revenue_growth_annual_yoy_pct")
    rev_cagr_raw = hard_facts.get("revenue_cagr_3y_pct")

    # Blend multi-period growth to smooth single-quarter noise (e.g. NTEK-B.ST Q2 +12.1% vs Annual +2.9% & 3y CAGR +5.1%)
    if rev_cagr_raw is not None and rev_ann_yoy_raw is not None and rev_yoy_raw is not None:
        effective_growth_pct = 0.45 * float(rev_cagr_raw) + 0.30 * float(rev_ann_yoy_raw) + 0.25 * rev_yoy_pct
    elif rev_cagr_raw is not None and rev_yoy_raw is not None:
        effective_growth_pct = 0.60 * float(rev_cagr_raw) + 0.40 * rev_yoy_pct
    elif rev_ann_yoy_raw is not None and rev_yoy_raw is not None:
        effective_growth_pct = 0.60 * float(rev_ann_yoy_raw) + 0.40 * rev_yoy_pct
    elif rev_cagr_raw is not None:
        effective_growth_pct = float(rev_cagr_raw)
    else:
        effective_growth_pct = rev_yoy_pct

    # Organic vs. M&A-driven growth separation (applies to both Core and Satellite):
    curr_ev_ebit_val = float(hard_facts.get("current_ev_ebit") or 0.0)
    is_unprofitable_ma_rollup = (
        hard_facts.get("organic_revenue_growth_pct") is None
        and effective_growth_pct > 25.0
        and (roe_pct < 8.0 or ebit_pct < 6.0 or curr_ev_ebit_val > 35.0)
    )
    has_ma_divergence = (
        "TTM_ACCELERATION_OR_MA_DIVERGENCE" in audit_flags
        or bool(hard_facts.get("is_ma_driven_growth", False))
        or is_unprofitable_ma_rollup
    )
    has_dilution_flag = "DILUTION_WARNING_RECENT_ISSUE" in audit_flags or bool(hard_facts.get("has_share_dilution", False))
    organic_rev_raw = hard_facts.get("organic_revenue_growth_pct")
    if organic_rev_raw is not None:
        effective_growth_pct = float(organic_rev_raw)
    elif has_ma_divergence and effective_growth_pct > 15.0:
        anchor_cagr = float(rev_cagr_raw) if rev_cagr_raw is not None else 12.0
        base_org_est = max(12.0, min(20.0, anchor_cagr))
        effective_growth_pct = base_org_est + 0.25 * max(0.0, effective_growth_pct - base_org_est)

    nd_ebitda = hard_facts.get("net_debt_to_ebitda")
    if nd_ebitda is None:
        net_debt = hard_facts.get("net_debt")
        net_cash = hard_facts.get("net_cash")
        if net_debt is None and net_cash is not None:
            net_debt = -float(net_cash)
        if net_debt is not None:
            ebitda = hard_facts.get("ebitda") or 0.0
            nd_ebitda = (float(net_debt) / float(ebitda)) if float(ebitda) > 0 else (0.0 if float(net_debt) <= 0 else 2.5)
        else:
            nd_ebitda = 0.0
    nd_val = float(nd_ebitda)

    # Insider bonus (+4.0 pts max, before final clip)
    news_reason_low = str(news_reason or "").lower()
    insider_sig = bool(hard_facts.get("insider_buying_signal", False)) or any(
        kw in news_reason_low
        for kw in ("johdon liiketoimet", "insider buying", "insynshandel", "manager's transaction", "sisäpiirin osto")
    )
    insider_pts = 4.0 if insider_sig else 0.0

    # Share dilution penalty (-8.0 pts when shares grew > 5%, e.g. M&A equity issue)
    dilution_penalty_pts = -8.0 if has_dilution_flag else 0.0
    manual_review_required = bool(has_ma_divergence or has_dilution_flag or "MANUAL_REVIEW_OUTLIER" in audit_flags)

    # FCF yield %
    fcf_yield_pct = hard_facts.get("fcf_yield_pct")
    if fcf_yield_pct is None:
        fcf_val = hard_facts.get("free_cash_flow") or hard_facts.get("operating_cash_flow_ttm")
        mcap_val = hard_facts.get("market_cap") or meta.get("market_cap_local") or meta.get("market_cap_eur") or meta.get("market_cap_usd")
        if fcf_val is not None and mcap_val is not None and float(mcap_val) > 0:
            fcf_yield_pct = (float(fcf_val) / float(mcap_val)) * 100.0
        else:
            fcf_yield_pct = 4.0 if (fcf_val is not None and float(fcf_val) > 0) else 0.0

    if "midcap" in u_low:
        # 1. Quality (40 pts): ROIC primary (or ROE penalized for debt leverage > 1.0x) + EBIT margin
        effective_return_pct = (
            0.65 * roic_pct + 0.35 * min(roe_pct, 55.0)
            if roic_pct is not None
            else (roe_pct / (1.0 + max(0.0, nd_val - 0.5) * 0.25))
        )
        qual_roic_pts = round(_norm_linear(effective_return_pct, 5.0, 55.0) * 22.0, 2)
        qual_ebit_pts = round(_norm_linear(ebit_pct, 8.0, 45.0) * 18.0, 2)
        quality_pts = round(qual_roic_pts + qual_ebit_pts, 2)

        # Soft threshold buffer penalty: if candidate passed via [13.5%..15.0%] ROE or [10.5%..12.0%] EBIT buffer,
        # apply a smooth linear penalty (-1.5 pts per 1%-pt below 15% ROE / 12% EBIT margin) instead of a cliff.
        soft_threshold_penalty_pts = 0.0
        if 13.5 <= roe_pct < 15.0 and (roic_pct is None or roic_pct < 15.0):
            soft_threshold_penalty_pts -= (15.0 - roe_pct) * 1.5
        if 10.5 <= ebit_pct < 12.0:
            soft_threshold_penalty_pts -= (12.0 - ebit_pct) * 1.5
        soft_threshold_penalty_pts = round(soft_threshold_penalty_pts, 2)

        # 2. Growth (20 pts): Continuous on [-10%, +45%], capped at <= 12.0 pts if unverified M&A spike
        raw_g_pts = _norm_linear(effective_growth_pct, -10.0, 45.0) * 20.0
        if has_ma_divergence and organic_rev_raw is None:
            raw_g_pts = min(12.0, raw_g_pts)
        growth_pts = round(raw_g_pts, 2)

        # 3. Balance Sheet (15 pts): Continuous on ND/EBITDA [-1.0, +2.5] (lower debt -> higher score)
        balance_sheet_pts = round((1.0 - _norm_linear(nd_val, -1.0, 2.5)) * 15.0, 2)

        # 4. Valuation (25 pts): Continuous EV/EBIT [6.0..30.0] (9p) + 5y Premium [-40%..+50%] (6p) + FCF Yield [0%..10%] (10p)
        curr_ev_ebit = hard_facts.get("current_ev_ebit")
        ev_prem_pct = hard_facts.get("ev_ebit_premium_pct")
        if curr_ev_ebit is not None and float(curr_ev_ebit) > 0:
            val_abs_pts = (1.0 - _norm_linear(float(curr_ev_ebit), 6.0, 30.0)) * 9.0
        else:
            val_abs_pts = 4.5

        if ev_prem_pct is not None:
            val_hist_pts = (1.0 - _norm_linear(float(ev_prem_pct), -40.0, 50.0)) * 6.0
        elif hard_facts.get("is_historically_expensive", False):
            val_hist_pts = 0.0
        else:
            val_hist_pts = 3.5

        val_fcf_pts = _norm_linear(float(fcf_yield_pct), 0.0, 10.0) * 10.0
        valuation_pts = round(val_abs_pts + val_hist_pts + val_fcf_pts, 2)

        raw_total = quality_pts + growth_pts + balance_sheet_pts + valuation_pts + insider_pts + dilution_penalty_pts + soft_threshold_penalty_pts
        total_score = round(min(100.0, max(0.0, raw_total)), 2)
        breakdown = {
            "quality_pts": quality_pts,
            "growth_pts": growth_pts,
            "balance_sheet_pts": balance_sheet_pts,
            "valuation_pts": valuation_pts,
            "insider_pts": insider_pts,
            "dilution_penalty_pts": dilution_penalty_pts,
            "soft_threshold_penalty_pts": soft_threshold_penalty_pts,
            "effective_growth_pct": round(effective_growth_pct, 2),
            "manual_review_required": manual_review_required,
            "total_score": total_score,
        }
        return total_score, breakdown

    # Satellite (Nordic_MicroCap_Catalyst: "Neglected, Profitable Small-Caps with Verified Low Analyst Coverage"):
    eff_ret = (0.60 * roic_pct + 0.40 * roe_pct) if roic_pct is not None else roe_pct
    prof_ebit_pts = _norm_linear(ebit_pct, 0.0, 30.0) * 18.0
    prof_roe_pts = _norm_linear(eff_ret, 0.0, 40.0) * 17.0
    profitability_pts = round(prof_ebit_pts + prof_roe_pts, 2)

    is_profitable_smallcap = (roe_pct >= 5.0 and ebit_pct >= 5.0)
    raw_growth_pts = _norm_linear(effective_growth_pct if "nordic" in u_low else rev_yoy_pct, -5.0, 50.0) * 20.0
    if has_ma_divergence and organic_rev_raw is None:
        raw_growth_pts = min(9.5, raw_growth_pts)
    growth_pts = round(raw_growth_pts if is_profitable_smallcap or "us_" in u_low else raw_growth_pts * 0.35, 2)

    cf_yield_pts = _norm_linear(float(fcf_yield_pct), -2.0, 12.0) * 15.0
    solv_pts = (1.0 - _norm_linear(nd_val, -1.0, 3.0)) * 10.0
    viability_pts = round(cf_yield_pts + solv_pts, 2)

    # Neglect Bonus: Missing data (None) gets 0.0 pts!
    # Also requires profitable baseline (is_profitable_smallcap) so unprofitable stocks are not rewarded for being neglected.
    analyst_cnt = hard_facts.get("analyst_count")
    if analyst_cnt is None or not is_profitable_smallcap:
        neglect_pts = 0.0
    elif int(analyst_cnt) <= 2:
        neglect_pts = 6.0
    elif int(analyst_cnt) <= 5:
        neglect_pts = 3.0
    else:
        neglect_pts = 0.0

    has_cat = news_verdict in ("STRONG BUY", "WATCH_TURNAROUND") or bool(hard_facts.get("is_profile_a", False))
    cat_base = (10.0 if is_profitable_smallcap or news_verdict == "WATCH_TURNAROUND" else 4.0) if has_cat else (6.0 if bool(hard_facts.get("is_profile_b", False)) else 0.0)
    catalyst_pts = round(min(20.0, neglect_pts + cat_base + insider_pts), 2)

    raw_total = profitability_pts + growth_pts + viability_pts + catalyst_pts + dilution_penalty_pts
    total_score = round(min(100.0, max(0.0, raw_total)), 2)
    breakdown = {
        "profitability_pts": profitability_pts,
        "growth_pts": growth_pts,
        "viability_pts": viability_pts,
        "neglect_pts": neglect_pts,
        "catalyst_pts": catalyst_pts,
        "insider_pts": insider_pts,
        "dilution_penalty_pts": dilution_penalty_pts,
        "effective_growth_pct": round(effective_growth_pct, 2),
        "manual_review_required": manual_review_required,
        "total_score": total_score,
    }
    return total_score, breakdown
