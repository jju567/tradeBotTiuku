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
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Union, List
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# Rivinimet joita yfinance käyttää eri raporteissa (versiot/markkinat vaihtelevat)
_DEBT_KEYS = ["Total Debt", "TotalDebt"]
_LT_DEBT_KEYS = ["Long Term Debt", "LongTermDebt", "Long Term Debt Noncurrent", "Long Term Debt And Capital Lease Obligation"]
_CUR_DEBT_KEYS = ["Current Debt", "CurrentDebt", "Current Debt And Capital Lease Obligation"]
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


def get_hard_financials(ticker: str) -> Dict[str, Any]:
    """
    Laskee deterministiset selviytymis- ja kasvutunnusluvut annetulle tickerille.

    Palauttaa dictin, joka on suunniteltu injektoitavaksi suoraan LLM-promptiin
    osiona "[HARD FINANCIAL FACTS - DO NOT RECALCULATE]".

    Kaikki numeeriset kentät ovat joko float tai None (ei koskaan puuttuva avain,
    jotta downstream-koodi/prompti voi aina luottaa rakenteeseen).
    """
    cleaned_ticker = ticker.strip().upper()
    result: Dict[str, Any] = {
        "ticker": cleaned_ticker,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "net_cash": None,
        "cash_and_equivalents": None,
        "total_debt": None,
        "operating_cash_flow_ttm": None,
        "cash_runway_months": None,
        "total_revenue_current": None,
        "total_revenue_previous": None,
        "revenue_current": None,
        "revenue_previous": None,
        "revenue_growth_yoy_pct": None,
        "latest_statement_date": None,
        "data_age_days": None,
        "data_source": "yfinance",
        "data_quality": {
            "ocf_source": None,   # "ttm_quarterly" | "latest_annual" | None
            "debt_source": None,  # "total_debt_field" | "lt_plus_current" | None
            "is_fresh": True,     # True if statement age <= 120 days
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
            lt_q = _extract_metric_from_df(bs_q, _LT_DEBT_KEYS) or 0.0
            cur_q = _extract_metric_from_df(bs_q, _CUR_DEBT_KEYS) or 0.0
            lt_a = _extract_metric_from_df(bs_a, _LT_DEBT_KEYS) or 0.0
            cur_a = _extract_metric_from_df(bs_a, _CUR_DEBT_KEYS) or 0.0
            
            calc_debt = max(lt_q + cur_q, lt_a + cur_a)
            if calc_debt > 0:
                debt = calc_debt
                result["data_quality"]["debt_source"] = "lt_plus_current"

        result["cash_and_equivalents"] = cash
        result["total_debt"] = debt

        if cash is not None and debt is not None:
            result["net_cash"] = round(cash - debt, 2)
        elif cash is not None and debt is None:
            result["errors"].append("total_debt not found; net_cash left as None to avoid false optimism")

        # ---- 2. Kassavirtaraportti (Cash Flow) ----
        cf_q = _get_first_valid_df(t, ["quarterly_cashflow"])
        cf_a = _get_first_valid_df(t, ["cashflow"])

        ocf_val = None
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

        result["operating_cash_flow_ttm"] = ocf_val

        # ---- 3. Cash Runway ----
        # Positiivinen tai nolla OCF -> yhtiö ei polta kassaa operatiivisesti -> Infinite
        # Negatiivinen OCF -> lasketaan kuukausissa jäljellä oleva kassa
        if cash is not None and ocf_val is not None:
            if ocf_val >= 0:
                result["cash_runway_months"] = "Infinite"
            else:
                monthly_burn = abs(ocf_val) / 12.0
                if monthly_burn > 0:
                    result["cash_runway_months"] = round(cash / monthly_burn, 1)
                else:
                    result["cash_runway_months"] = "Infinite"
        else:
            result["errors"].append("cash_runway_months not computed: missing cash or OCF")

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

        has_any_data = (cash is not None or ocf_val is not None or rev_curr is not None)
        result["status"] = "OK" if has_any_data else "PARTIAL"

    except Exception as e:
        logger.warning(f"Could not retrieve hard financials for {cleaned_ticker}: {e}")
        result["errors"].append(str(e))
        result["status"] = "ERROR"


    return result


def evaluate_profiles(hard_facts: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates deterministic Profile A (Quality Growth) and Profile B (Value & Anti-Shrinking)
    rules on structured point-in-time or yfinance hard facts dictionary.

    Profile A (Quality Growth):
      - revenue_growth_yoy > 20%
      - gross_margin > 40% (or gross profit proves scalability)
      - Survival Check: Either operating_cash_flow > 0 OR cash_runway_months > 18

    Profile B (Deep Value & Anti-Shrinking):
      - net_cash > 0
      - Anti-Shrinking: Reject if revenue YoY < 0 AND (ocf is negative or not improving)

    Returns:
    {
        "is_profile_a": bool,
        "is_profile_b": bool,
        "signal": "BUY_PROFILE_A" | "BUY_PROFILE_B" | "BUY_BOTH" | "REJECT",
        "profile": "PROFILE_A (Growth)" | "PROFILE_B (Value)" | "PROFILE_A & B" | "NONE",
        "reasons": List[str]
    }
    """
    rev_yoy = hard_facts.get("revenue_growth_yoy_pct") or hard_facts.get("revenue_yoy")
    gm_pct = hard_facts.get("gross_margin_pct") or hard_facts.get("gross_margin")
    net_cash = hard_facts.get("net_cash")
    ocf = hard_facts.get("operating_cash_flow_ttm") or hard_facts.get("ocf")
    runway = hard_facts.get("cash_runway_months")

    # Parse numeric runway
    runway_num = float("inf")
    if runway is not None:
        if isinstance(runway, (int, float)):
            runway_num = float(runway)
        elif str(runway).strip().lower() == "infinite":
            runway_num = float("inf")
        else:
            try:
                runway_num = float(runway)
            except (ValueError, TypeError):
                runway_num = 0.0

    # 1. Profile A: Quality Growth
    is_profile_a = False
    reasons_a: List[str] = []

    growth_ok = (rev_yoy is not None and rev_yoy > 20.0)
    margin_ok = (gm_pct is not None and gm_pct > 40.0)
    survival_ok = (ocf is not None and ocf > 0) or (runway_num > 18.0)

    if growth_ok and margin_ok and survival_ok:
        is_profile_a = True
    else:
        if not growth_ok:
            reasons_a.append(f"Revenue growth {rev_yoy}% <= 20%")
        if not margin_ok:
            reasons_a.append(f"Gross margin {gm_pct}% <= 40%")
        if not survival_ok:
            reasons_a.append(f"Survival check failed: OCF={ocf}, Runway={runway}m <= 18m")

    # 2. Profile B: Deep Value & Anti-Shrinking
    is_profile_b = False
    reasons_b: List[str] = []

    if net_cash is not None and net_cash > 0:
        is_shrinking = (rev_yoy is not None and rev_yoy < 0.0)
        ocf_negative = (ocf is not None and ocf <= 0.0)

        if is_shrinking and ocf_negative:
            reasons_b.append("Anti-Shrinking rule triggered: Revenue shrinking & OCF negative")
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
        "reasons": reasons_a if not is_profile_a and not is_profile_b else (reasons_b if not is_profile_b else []),
    }


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
