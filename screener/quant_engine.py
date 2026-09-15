"""
Quantitative Risk Engine for Micro-Cap Equities.

Applies strict mathematical rules:
1. Cash Runway > 18 months.
2. Bid-Ask Spread < 4%.
3. Fallback table and balance sheet parser for micro-caps lacking API coverage.
"""

import logging
import re
from typing import Optional, Dict, Any, List, Tuple

import yfinance as yf

from .config import ScreenerConfig
from .models import ScrapedRelease, NLPExtractionResult, ScreeningCandidate

logger = logging.getLogger(__name__)


def check_liquidity_and_spread(
    ticker: str,
    max_spread_pct: float = 4.0,
    min_volume: int = 2000,
    commission_min_eur: float = 7.00,
    commission_percent: float = 0.0015,
    sample_trade_size_eur: float = 1000.0,
    max_total_friction_pct: float = 5.5,
) -> Dict[str, Any]:
    """
    Checks if a stock is liquid enough and if its bid-ask spread is acceptable for a retail entry,
    taking into account Nordnet small user commission pricing (Taso 3/4) and total round-trip friction.

    :param ticker: Stock ticker symbol (e.g. "FARON", "KEMIRA.HE", "WAPICE")
    :param max_spread_pct: Maximum allowed bid-ask spread percentage (default 4.0%)
    :param min_volume: Minimum acceptable daily volume in shares (default 2,000)
    :param commission_min_eur: Nordnet minimum commission per trade for small user (default 7.00 EUR)
    :param commission_percent: Nordnet commission percentage (default 0.15% = 0.0015)
    :param sample_trade_size_eur: Nominal trade size in EUR used to calculate round-trip friction (default 1000 EUR)
    :param max_total_friction_pct: Maximum allowable combined friction (Spread % + Round-trip Fee %) (default 5.5%)
    :return: Dictionary containing:
             - passed_spread_check: bool
             - spread_pct: float or None
             - bid: float or None
             - ask: float or None
             - volume: int
             - commission_round_trip_eur: float
             - commission_drag_pct: float
             - total_friction_pct: float or None
             - min_recommended_trade_eur: float
             - reason: str
    """
    # Calculate baseline Nordnet small user commission parameters
    trade_size = max(sample_trade_size_eur, 100.0)
    one_way_commission = max(commission_min_eur, round(trade_size * commission_percent, 2))
    round_trip_commission = round(2 * one_way_commission, 2)
    commission_drag_pct = round((round_trip_commission / trade_size) * 100.0, 2)
    min_recommended_trade = round(commission_min_eur / 0.015, 0)  # Keeps single-way fee <= 1.5%

    if not ticker or not isinstance(ticker, str):
        return {
            "passed_spread_check": False,
            "spread_pct": None,
            "bid": None,
            "ask": None,
            "volume": 0,
            "commission_round_trip_eur": round_trip_commission,
            "commission_drag_pct": commission_drag_pct,
            "total_friction_pct": None,
            "min_recommended_trade_eur": min_recommended_trade,
            "reason": "Missing or invalid ticker symbol",
        }

    # Standardize ticker for Nasdaq Helsinki (.HE suffix)
    symbol = ticker.strip().upper()
    if not symbol.endswith(".HE") and "." not in symbol:
        symbol = f"{symbol}.HE"

    try:
        ticker_obj = yf.Ticker(symbol)
        info = ticker_obj.info or {}

        bid = info.get("bid")
        ask = info.get("ask")
        volume = (
            info.get("regularMarketVolume")
            or info.get("volume")
            or info.get("averageVolume")
            or 0
        )

        # Fallback to fast_info if info is sparse
        if bid is None or ask is None:
            fast_info = getattr(ticker_obj, "fast_info", None)
            if fast_info:
                if bid is None and hasattr(fast_info, "bid"):
                    bid = getattr(fast_info, "bid", None)
                if ask is None and hasattr(fast_info, "ask"):
                    ask = getattr(fast_info, "ask", None)
                if volume == 0 and hasattr(fast_info, "last_volume"):
                    volume = getattr(fast_info, "last_volume", 0)

        # Normalize volume
        try:
            volume = int(volume or 0)
        except (ValueError, TypeError):
            volume = 0

        # Handle missing or zero quotes (market closed, pre-market, or halted)
        if bid is None or ask is None or float(ask) <= 0 or float(bid) <= 0:
            return {
                "passed_spread_check": False,
                "spread_pct": None,
                "bid": float(bid) if bid else None,
                "ask": float(ask) if ask else None,
                "volume": volume,
                "commission_round_trip_eur": round_trip_commission,
                "commission_drag_pct": commission_drag_pct,
                "total_friction_pct": None,
                "min_recommended_trade_eur": min_recommended_trade,
                "reason": "Market closed/No bid-ask quote data in Yahoo Finance",
            }

        bid_val = float(bid)
        ask_val = float(ask)
        spread_pct = round(((ask_val - bid_val) / ask_val) * 100.0, 2)
        total_friction_pct = round(spread_pct + commission_drag_pct, 2)

        # Filter 1: Bid-Ask Spread check
        if spread_pct > max_spread_pct:
            return {
                "passed_spread_check": False,
                "spread_pct": spread_pct,
                "bid": bid_val,
                "ask": ask_val,
                "volume": volume,
                "commission_round_trip_eur": round_trip_commission,
                "commission_drag_pct": commission_drag_pct,
                "total_friction_pct": total_friction_pct,
                "min_recommended_trade_eur": min_recommended_trade,
                "reason": f"Spread {spread_pct:.2f}% is too high (exceeds {max_spread_pct:.2f}% limit)",
            }

        # Filter 2: Total Friction check (Spread % + Nordnet Round-trip Commission %)
        if total_friction_pct > max_total_friction_pct:
            return {
                "passed_spread_check": False,
                "spread_pct": spread_pct,
                "bid": bid_val,
                "ask": ask_val,
                "volume": volume,
                "commission_round_trip_eur": round_trip_commission,
                "commission_drag_pct": commission_drag_pct,
                "total_friction_pct": total_friction_pct,
                "min_recommended_trade_eur": min_recommended_trade,
                "reason": f"Total friction {total_friction_pct:.2f}% (Spread {spread_pct:.2f}% + Nordnet fees {commission_drag_pct:.2f}%) exceeds limit {max_total_friction_pct:.2f}%",
            }

        # Filter 3: Volume threshold check
        if min_volume > 0 and volume < min_volume:
            return {
                "passed_spread_check": False,
                "spread_pct": spread_pct,
                "bid": bid_val,
                "ask": ask_val,
                "volume": volume,
                "commission_round_trip_eur": round_trip_commission,
                "commission_drag_pct": commission_drag_pct,
                "total_friction_pct": total_friction_pct,
                "min_recommended_trade_eur": min_recommended_trade,
                "reason": f"Volume {volume} shares is below minimum threshold of {min_volume} shares",
            }

        return {
            "passed_spread_check": True,
            "spread_pct": spread_pct,
            "bid": bid_val,
            "ask": ask_val,
            "volume": volume,
            "commission_round_trip_eur": round_trip_commission,
            "commission_drag_pct": commission_drag_pct,
            "total_friction_pct": total_friction_pct,
            "min_recommended_trade_eur": min_recommended_trade,
            "reason": "Passed liquidity, spread, and Nordnet small user friction checks",
        }

    except Exception as e:
        logger.warning(f"Error checking liquidity/spread for {symbol} via yfinance: {e}")
        return {
            "passed_spread_check": False,
            "spread_pct": None,
            "bid": None,
            "ask": None,
            "volume": 0,
            "commission_round_trip_eur": round_trip_commission,
            "commission_drag_pct": commission_drag_pct,
            "total_friction_pct": None,
            "min_recommended_trade_eur": min_recommended_trade,
            "reason": f"yfinance lookup error: {str(e)}",
        }


class MicroCapBalanceSheetParser:
    """
    Fallback parser that extracts Cash and Cash Burn from unstructured
    financial tables and report text when financial APIs lack coverage.
    """

    CASH_KEYWORDS = [
        r"rahat ja pankkisaamiset",
        r"kassavarat ja pankkisaamiset",
        r"rahavarat",
        r"likvidit varat",
        r"cash and cash equivalents",
        r"cash and bank balances",
        r"total cash",
    ]

    CASH_FLOW_KEYWORDS = [
        r"liiketoiminnan rahavirta",
        r"operatiivinen rahavirta",
        r"cash flow from operating activities",
        r"operating cash flow",
    ]

    @classmethod
    def extract_cash_and_burn(
        cls,
        text: str,
        tables: List[List[List[str]]]
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Attempt to extract:
        - Cash position (in EUR)
        - Quarterly operating burn / outflow (in EUR)
        """
        cash = cls._search_tables_or_text(tables, text, cls.CASH_KEYWORDS)
        operating_cf = cls._search_tables_or_text(tables, text, cls.CASH_FLOW_KEYWORDS)

        quarterly_burn = None
        if operating_cf is not None:
            if operating_cf < 0:
                quarterly_burn = abs(operating_cf)
            else:
                quarterly_burn = 0.0  # Positive operating cash flow

        return cash, quarterly_burn

    @classmethod
    def _search_tables_or_text(
        cls,
        tables: List[List[List[str]]],
        text: str,
        keywords: List[str]
    ) -> Optional[float]:
        # 1. Search in structured tables
        for table in tables:
            for row in table:
                if not row:
                    continue
                row_str = " ".join(row).lower()
                for kw in keywords:
                    if re.search(kw, row_str):
                        # Extract numerical values from the row
                        nums = cls._extract_numbers_from_row(row)
                        if nums:
                            # Take the most recent period (usually the first column after label)
                            return nums[0]

        # 2. Search in raw text paragraphs
        for kw in keywords:
            m = re.search(
                rf"{kw}[^\d\n\r]{{1,40}}([+-]?\s*[\d\s\.,]+)\s*(?:teur|meur|milj|tuhatta|euroa|eur)?",
                text,
                re.IGNORECASE,
            )
            if m:
                val = cls._parse_number_str(m.group(1))
                if val is not None:
                    # Scale according to unit in surrounding context
                    context = text[max(0, m.start() - 50):min(len(text), m.end() + 50)].lower()
                    if "meur" in context or "milj" in context or "miljoona" in context:
                        return val * 1_000_000
                    elif "teur" in context or "tuhatta" in context:
                        return val * 1_000
                    return val

        return None

    @staticmethod
    def _extract_numbers_from_row(row: List[str]) -> List[float]:
        results = []
        for cell in row[1:]:
            val = MicroCapBalanceSheetParser._parse_number_str(cell)
            if val is not None:
                results.append(val)
        return results

    @staticmethod
    def _parse_number_str(s: str) -> Optional[float]:
        if not s:
            return None
        # Clean currency symbols, parentheses, spaces
        cleaned = s.replace(" ", "").replace("€", "").replace("\xa0", "").strip()
        is_negative = False
        if cleaned.startswith("(") and cleaned.endswith(")"):
            is_negative = True
            cleaned = cleaned[1:-1]
        elif cleaned.startswith("-"):
            is_negative = True
            cleaned = cleaned[1:]

        # Handle European decimal comma vs thousands separator
        if "," in cleaned and "." in cleaned:
            # e.g. 1.250,50 or 1,250.50
            if cleaned.find(".") < cleaned.find(","):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")

        try:
            val = float(cleaned)
            return -val if is_negative else val
        except ValueError:
            return None


class QuantitativeRiskEngine:
    """
    Applies strict quantitative screening filters:
    1. Cash Runway > min_cash_runway_months (default 18 months).
    2. Bid-Ask Spread < max_bid_ask_spread_pct (default 4.0%).
    """

    def __init__(self, config: Optional[ScreenerConfig] = None):
        self.config = config or ScreenerConfig.from_env()

    def evaluate_candidate(
        self,
        release: ScrapedRelease,
        nlp_result: NLPExtractionResult
    ) -> ScreeningCandidate:
        """Run quantitative metrics & risk filters against a candidate."""
        ticker = nlp_result.ticker or release.feed_item.ticker
        rejection_reasons = []

        # 1. NLP Hard Red Flags Check
        if nlp_result.has_cash_flow_issues:
            rejection_reasons.append(
                f"NLP Cash Alert: {nlp_result.funding_need_explanation or 'Liquidity/funding distress detected'}"
            )

        # 2. Fetch Market Data & Calculate Spread
        bid_ask_spread_pct, latest_price = self._get_market_spread_and_price(ticker)
        
        if bid_ask_spread_pct is not None:
            if bid_ask_spread_pct > self.config.max_bid_ask_spread_pct:
                rejection_reasons.append(
                    f"Bid-Ask Spread too high: {bid_ask_spread_pct:.2f}% > limit {self.config.max_bid_ask_spread_pct:.2f}%"
                )
        else:
            logger.info(f"Could not calculate live spread for {ticker}; skipping spread rejection.")

        # 3. Calculate Cash Runway (Months)
        cash_runway_months, cash_eur, burn_eur = self._calculate_cash_runway(ticker, release)

        if cash_runway_months is not None:
            if cash_runway_months < self.config.min_cash_runway_months:
                rejection_reasons.append(
                    f"Cash Runway too short: {cash_runway_months:.1f} months < minimum {self.config.min_cash_runway_months:.1f} months"
                )
        else:
            # If runway couldn't be calculated mathematically, but NLP flagged no issues:
            logger.debug(f"Runway estimation unavailable for {ticker}")

        passed = len(rejection_reasons) == 0

        return ScreeningCandidate(
            release=release,
            nlp_result=nlp_result,
            cash_runway_months=cash_runway_months,
            bid_ask_spread_pct=bid_ask_spread_pct,
            current_cash_eur=cash_eur,
            quarterly_burn_rate_eur=burn_eur,
            latest_price_eur=latest_price,
            passed_filters=passed,
            rejection_reasons=rejection_reasons
        )

    def _get_market_spread_and_price(self, ticker: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
        """Fetch real-time Bid-Ask Spread and latest price from yfinance."""
        if not ticker:
            return None, None

        # Standardize ticker for Nasdaq Helsinki (e.g. FARON -> FARON.HE)
        symbol = ticker.upper()
        if not symbol.endswith(".HE") and "." not in symbol:
            symbol = f"{symbol}.HE"

        try:
            ticker_obj = yf.Ticker(symbol)
            info = ticker_obj.info or {}

            bid = info.get("bid")
            ask = info.get("ask")
            regular_price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")

            if bid and ask and ask > 0:
                spread_pct = ((ask - bid) / ask) * 100.0
                return spread_pct, float(regular_price or ask)

            # Fallback: check fast_info or latest day's high/low
            fast_info = getattr(ticker_obj, "fast_info", None)
            if fast_info:
                last_price = getattr(fast_info, "last_price", None)
                if last_price:
                    return None, float(last_price)

            return None, float(regular_price) if regular_price else None

        except Exception as e:
            logger.debug(f"yfinance lookup error for {symbol}: {e}")
            return None, None

    def _calculate_cash_runway(
        self,
        ticker: Optional[str],
        release: ScrapedRelease
    ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Calculate cash runway in months.
        Tries yfinance fundamentals first; falls back to parsing the document tables.
        """
        cash = None
        quarterly_burn = None

        # 1. Try yfinance financial statements
        if ticker:
            symbol = ticker.upper() if "." in ticker else f"{ticker.upper()}.HE"
            try:
                t = yf.Ticker(symbol)
                bs = t.balance_sheet
                cf = t.cashflow

                if bs is not None and not bs.empty:
                    # Look for Cash and Cash Equivalents
                    for row_name in ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"]:
                        if row_name in bs.index:
                            cash = float(bs.loc[row_name].iloc[0])
                            break

                if cf is not None and not cf.empty:
                    # Look for Operating Cash Flow
                    for row_name in ["Operating Cash Flow", "Cash Flowsfromusedin Operating Activities"]:
                        if row_name in cf.index:
                            annual_op_cf = float(cf.loc[row_name].iloc[0])
                            if annual_op_cf < 0:
                                quarterly_burn = abs(annual_op_cf) / 4.0
                            else:
                                quarterly_burn = 0.0  # Cash flow positive
                            break
            except Exception as e:
                logger.debug(f"Could not load yfinance statements for {ticker}: {e}")

        # 2. Fallback: Parse directly from scraped report tables & text
        if cash is None or quarterly_burn is None:
            parsed_cash, parsed_burn = MicroCapBalanceSheetParser.extract_cash_and_burn(
                release.document.full_combined_text,
                release.document.extracted_tables
            )
            if cash is None:
                cash = parsed_cash
            if quarterly_burn is None:
                quarterly_burn = parsed_burn

        # 3. Compute Runway
        if cash is not None:
            if quarterly_burn is not None:
                if quarterly_burn == 0.0:
                    return 999.0, cash, quarterly_burn  # Positive cash flow / self-funding
                monthly_burn = quarterly_burn / 3.0
                runway_months = cash / monthly_burn if monthly_burn > 0 else 999.0
                return round(runway_months, 1), cash, quarterly_burn
            else:
                # If cash is known but burn is unknown, return cash with None runway
                return None, cash, None

        return None, None, None
