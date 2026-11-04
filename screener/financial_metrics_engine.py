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
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Union, List
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

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
    }

    # Respect explicit overrides (e.g. from unit tests or point-in-time datasets)
    if override_current_ev_ebit is not None and override_5y_median is not None:
        curr = round(float(override_current_ev_ebit), 2)
        med = round(float(override_5y_median), 2)
        res["current_ev_ebit"] = curr
        res["ev_ebit_5y_median"] = med
        res["historical_ev_ebit_years_count"] = 5
        if med > 0:
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
    if curr_ebit is None or curr_ebit <= 0:
        op_raw = info_dict.get("operatingIncome") or info_dict.get("ebitda")
        if isinstance(op_raw, (int, float)) and op_raw > 0:
            curr_ebit = float(op_raw)

    curr_ev_ebit = None
    if curr_ev is not None and curr_ebit is not None and curr_ebit > 0:
        curr_ev_ebit = round(curr_ev / curr_ebit, 2)
    elif info_dict.get("enterpriseToEbitda"):
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
                    hist = t.history(period="5y")
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
                                ev_ebits.append(round(hist_ev / y_ebit, 2))
        except Exception as e:
            logger.debug(f"Historical EV/EBIT extraction exception: {e}")

    if hist_ev_ebits:
        med = round(float(statistics.median(hist_ev_ebits)), 2)
        res["ev_ebit_5y_median"] = med
        res["historical_ev_ebit_years_count"] = len(hist_ev_ebits)
        if curr_ev_ebit is not None and med > 0:
            prem = round(((curr_ev_ebit - med) / med) * 100.0, 2)
            res["ev_ebit_premium_pct"] = prem
            res["is_historically_expensive"] = (prem > 20.0)
    else:
        res["ev_ebit_5y_median"] = None
        res["ev_ebit_premium_pct"] = None
        res["is_historically_expensive"] = False

    return res


def check_share_dilution(
    t: Any,
    info: Dict[str, Any],
    bs_a: Optional[pd.DataFrame],
    bs_q: Optional[pd.DataFrame] = None,
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
    
    # 1. Current shares from info or fast_info or latest balance sheet
    shares_curr = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
    if shares_curr is None or shares_curr <= 0:
        fast_info = getattr(t, "fast_info", None)
        if fast_info:
            shares_curr = getattr(fast_info, "shares", None) or getattr(fast_info, "shares_outstanding", None)
            
    if (shares_curr is None or shares_curr <= 0) and bs_q is not None and not bs_q.empty:
        shares_curr = _extract_metric_from_df(bs_q, _SHARE_COUNT_KEYS)
    if (shares_curr is None or shares_curr <= 0) and bs_a is not None and not bs_a.empty:
        shares_curr = _extract_metric_from_df(bs_a, _SHARE_COUNT_KEYS)
        
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

    if shares_past_candidates:
        shares_past = min(shares_past_candidates)
        res["shares_outstanding_historical"] = float(shares_past)
        dilution_pct = ((float(shares_curr) - float(shares_past)) / float(shares_past)) * 100.0
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
                has_bs_structure = (
                    _extract_metric_from_df(bs_q, _bs_check_keys) is not None
                    or _extract_metric_from_df(bs_a, _bs_check_keys) is not None
                )
                if cash is not None and has_bs_structure:
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
        capex = _extract_metric_from_df(cf_a, _CAPEX_KEYS)
        if capex is None:
            capex = _extract_metric_from_df(cf_q, _CAPEX_KEYS)

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

        # ---- 4. Liikevaihto ja YoY-kasvu ----
        inc_q = _get_first_valid_df(t, ["quarterly_financials", "quarterly_income_stmt"])
        inc_a = _get_first_valid_df(t, ["financials", "income_stmt"])

        rev_curr: Optional[float] = None
        rev_prev: Optional[float] = None

        if inc_a is not None and not inc_a.empty:
            for key in _REVENUE_KEYS:
                if key in inc_a.index:
                    series = inc_a.loc[key].dropna()
                    if len(series) >= 1:
                        rev_curr = float(series.iloc[0])
                    if len(series) >= 2:
                        rev_prev = float(series.iloc[1])
                    if rev_curr is not None:
                        break

        if (rev_curr is None or rev_prev is None) and inc_q is not None and not inc_q.empty:
            for key in _REVENUE_KEYS:
                if key in inc_q.index:
                    series = inc_q.loc[key].dropna()
                    if len(series) >= 8:
                        rev_curr = float(series.iloc[:4].sum())
                        rev_prev = float(series.iloc[4:8].sum())
                        break
                    elif len(series) >= 4:
                        rev_curr = float(series.iloc[:4].sum())
                        break

        result["total_revenue_current"] = rev_curr
        result["total_revenue_previous"] = rev_prev
        result["revenue_current"] = rev_curr
        result["revenue_previous"] = rev_prev

        if rev_curr is not None and rev_prev not in (None, 0):
            result["revenue_growth_yoy_pct"] = round(
                ((rev_curr - rev_prev) / abs(rev_prev)) * 100.0, 2
            )

        # ---- 5. Bruttokate (Gross Profit & Gross Margin %) ----
        gp_val = _extract_metric_from_df(inc_a, _GROSS_PROFIT_KEYS)
        if gp_val is None:
            gp_val = _extract_metric_from_df(inc_q, _GROSS_PROFIT_KEYS)

        gm_pct = None
        if gp_val is not None and rev_curr is not None and rev_curr > 0:
            gm_pct = round((gp_val / rev_curr) * 100.0, 2)

        result["gross_profit"] = gp_val
        result["gross_margin_pct"] = gm_pct

        # ---- 6. Data Freshness Check ----
        latest_dt = None
        for cand_df in [bs_q, inc_q, cf_q]:
            if cand_df is not None and not cand_df.empty and len(cand_df.columns) > 0:
                try:
                    c_dt = pd.to_datetime(cand_df.columns[0]).tz_localize(None)
                    if latest_dt is None or c_dt > latest_dt:
                        latest_dt = c_dt
                except Exception:
                    pass

        if latest_dt is not None:
            age_days = (datetime.now(timezone.utc).replace(tzinfo=None) - latest_dt).days
            result["latest_statement_date"] = latest_dt.strftime("%Y-%m-%d")
            result["data_age_days"] = age_days
            is_fresh = age_days <= 120
            result["data_quality"]["is_fresh"] = is_fresh
            if not is_fresh:
                result["errors"].append(f"DATA_STALENESS_WARNING: Latest statement is {age_days} days old (>120d).")

        # ---- 7. Tiered Quality & Constraint Metrics (Core MidCap, US Growth, Nordic Catalyst) ----
        try:
            info = getattr(t, "info", None) or {}
            fast_info = getattr(t, "fast_info", None)

            # ROE
            roe = info.get("returnOnEquity")
            if roe is None:
                net_inc = _extract_metric_from_df(inc_a, ["Net Income", "NetIncome"])
                equity = _extract_metric_from_df(bs_a, ["Stockholders Equity", "StockholdersEquity", "Total Stockholder Equity"])
                if net_inc is not None and equity is not None and equity > 0:
                    roe = net_inc / equity
            result["roe"] = float(roe) if roe is not None else None

            # EBIT Margin
            ebit_m = info.get("operatingMargins") or info.get("ebitMargins")
            if ebit_m is None:
                op_inc = _extract_metric_from_df(inc_a, ["Operating Income", "OperatingIncome", "EBIT"])
                if op_inc is not None and rev_curr is not None and rev_curr > 0:
                    ebit_m = op_inc / rev_curr
            result["ebit_margin"] = float(ebit_m) if ebit_m is not None else None

            # Free Cash Flow
            fcf = info.get("freeCashflow")
            if fcf is None and ocf_val is not None:
                capex = _extract_metric_from_df(cf_a, ["Capital Expenditure", "CapitalExpenditure", "CapitalExpenditures"]) or 0.0
                fcf = ocf_val - abs(capex)
            result["free_cash_flow"] = float(fcf) if fcf is not None else None

            # EBITDA & Net Debt to EBITDA
            ebitda = info.get("ebitda")
            if ebitda is None:
                _ebitda_keys = ["EBITDA", "Normalized EBITDA", "NormalizedEBITDA", "EBIT", "Operating Income", "OperatingIncome"]
                ebitda = _extract_metric_from_df(inc_a, _ebitda_keys)
                if ebitda is None:
                    ebitda = _extract_metric_from_df(inc_q, _ebitda_keys)
            result["ebitda"] = float(ebitda) if ebitda is not None else None
            
            nd_ebitda = None
            if debt is not None and cash is not None:
                net_d = debt - cash
                if net_d <= 0:
                    nd_ebitda = 0.0  # Net cash -> solvent
                elif ebitda is not None and ebitda > 0:
                    nd_ebitda = round(net_d / ebitda, 2)
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

            # Exchange & Country
            exch = info.get("exchange")
            result["exchange"] = str(exch).upper() if exch else None
            country_val = info.get("country")
            result["country"] = str(country_val).strip() if country_val else None

            # Market Cap
            mcap = (fast_info and getattr(fast_info, "market_cap", None)) or info.get("marketCap")
            result["market_cap"] = float(mcap) if mcap is not None else None

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
                fx_to_eur = 1.0
                if t_curr == "SEK":
                    fx_to_eur = 1.0 / 11.30
                elif t_curr == "NOK":
                    fx_to_eur = 1.0 / 11.50
                elif t_curr == "DKK":
                    fx_to_eur = 1.0 / 7.46
                elif t_curr == "USD":
                    fx_to_eur = 1.0 / 1.08
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

            # Relative Valuation: EV/EBIT vs. 5-year Historical Median
            curr_ev = info.get("enterpriseValue")
            if curr_ev is None and mcap is not None and debt is not None and cash is not None:
                curr_ev = float(mcap) + float(debt) - float(cash)
            curr_ebit = op_inc if 'op_inc' in locals() and op_inc is not None else (info.get("operatingIncome") or ebitda)

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

            # 8. Osakeanti- ja diluutiolippu (Share Dilution Flag)
            dil_res = check_share_dilution(t=t, info=info, bs_a=bs_a, bs_q=bs_q)
            result["shares_outstanding_current"] = dil_res.get("shares_outstanding_current")
            result["shares_outstanding_historical"] = dil_res.get("shares_outstanding_historical")
            result["share_dilution_pct"] = dil_res.get("share_dilution_pct")
            result["has_share_dilution"] = dil_res.get("has_share_dilution", False)
            if dil_res.get("has_share_dilution") and dil_res.get("dilution_flag"):
                d_flag = dil_res["dilution_flag"]
                if d_flag not in result["audit_flags"]:
                    result["audit_flags"].append(d_flag)
                result["audit_flag"] = d_flag
        except Exception as e_extra:
            logger.debug(f"Tiered metrics extraction skipped for {cleaned_ticker}: {e_extra}")

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
        logger.debug(f"Profile evaluation skipped for {cleaned_ticker}: {e_prof}")
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


def audit_candidates_data(candidates: Any) -> Any:
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

        # 1. Collect for Duplicate Detector
        try:
            if rev_raw is not None:
                rev_f = float(rev_raw)
                rev_round = round(rev_f, 4)
                if rev_round != 0.0:
                    rev_map.setdefault(rev_round, []).append(cand)
        except (ValueError, TypeError):
            pass

        try:
            if ebit_raw is not None:
                ebit_f = float(ebit_raw)
                ebit_round = round(ebit_f, 4)
                if ebit_round != 0.0:
                    ebit_map.setdefault(ebit_round, []).append(cand)
        except (ValueError, TypeError):
            pass

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
            if "MANUAL_REVIEW_OUTLIER" in flags:
                reasons.append("Ääriarvo havaittu (Kasvu/ROE/EBIT > 100%)")
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

    return candidates
