"""
Currency & FX Utilities for Nordic and US Micro-Cap Screener.

Provides currency resolution, market region detection, and FX conversion rates.
"""

from __future__ import annotations

from typing import Dict, Optional
import yfinance as yf


def get_live_fx_rates() -> Dict[str, float]:
    """Fetches live FX rates EURUSD, EURSEK, EURDKK, EURNOK from Yahoo Finance with fallback."""
    fx_dict = {"EURUSD": 1.08, "EURSEK": 11.30, "EURDKK": 7.46, "EURNOK": 11.60}
    for pair in ["EURUSD=X", "EURSEK=X", "EURDKK=X", "EURNOK=X"]:
        key = pair.replace("=X", "")
        try:
            t = yf.Ticker(pair).history(period="5d")
            if not t.empty:
                fx_dict[key] = float(t["Close"].dropna().iloc[-1])
        except Exception:
            pass
    return fx_dict


def get_ticker_currency(ticker: str, known_currency: Optional[str] = None) -> str:
    """Resolves local currency for ticker: EUR (.HE), SEK (.ST), DKK (.CO), NOK (.OL), or USD."""
    if known_currency and isinstance(known_currency, str) and known_currency.strip():
        return known_currency.strip().upper()
    t_clean = str(ticker).strip().upper()
    if t_clean.endswith(".HE"):
        return "EUR"
    if t_clean.endswith(".ST"):
        return "SEK"
    if t_clean.endswith(".CO"):
        return "DKK"
    if t_clean.endswith(".OL"):
        return "NOK"
    return "USD"


def get_fx_to_account(
    ticker_currency: str,
    account_currency: str = "EUR",
    fx_rates: Optional[Dict[str, float]] = None,
) -> float:
    """
    Returns multiplier to convert an amount in ticker_currency to account_currency.
    amount_in_account_currency = amount_in_ticker_currency * fx_to_account
    """
    ticker_curr = ticker_currency.strip().upper()
    acc_curr = account_currency.strip().upper()
    if ticker_curr == acc_curr:
        return 1.0

    rates = fx_rates or get_live_fx_rates()
    eur_usd = rates.get("EURUSD", 1.08)
    eur_sek = rates.get("EURSEK", 11.30)
    eur_dkk = rates.get("EURDKK", 7.46)
    eur_nok = rates.get("EURNOK", 11.60)

    if ticker_curr == "EUR":
        to_eur = 1.0
    elif ticker_curr == "USD":
        to_eur = 1.0 / eur_usd
    elif ticker_curr == "SEK":
        to_eur = 1.0 / eur_sek
    elif ticker_curr == "DKK":
        to_eur = 1.0 / eur_dkk
    elif ticker_curr == "NOK":
        to_eur = 1.0 / eur_nok
    else:
        to_eur = 1.0

    if acc_curr == "EUR":
        return to_eur
    elif acc_curr == "USD":
        return to_eur * eur_usd
    elif acc_curr == "SEK":
        return to_eur * eur_sek
    elif acc_curr == "DKK":
        return to_eur * eur_dkk
    elif acc_curr == "NOK":
        return to_eur * eur_nok

    return to_eur


def get_ticker_region(ticker: str, known_market: Optional[str] = None) -> str:
    """Resolves market region for ticker: FI (.HE), SE (.ST), DK (.CO), NO (.OL), or US."""
    if known_market and isinstance(known_market, str) and known_market.strip():
        m = known_market.strip().upper()
        if m in ("FI", "SE", "DK", "NO", "US"):
            return m
    t_clean = str(ticker).strip().upper()
    if t_clean.endswith(".HE"):
        return "FI"
    if t_clean.endswith(".ST"):
        return "SE"
    if t_clean.endswith(".CO"):
        return "DK"
    if t_clean.endswith(".OL"):
        return "NO"
    return "US"
