import json
import logging
from typing import Dict, List, Any
import yfinance as yf
import config
from utils.indicators import calculate_rsi, calculate_sma, calculate_ema, calculate_bollinger_bands

logger = logging.getLogger(__name__)

# Comprehensive default watchlist covering OMX Helsinki Large/Mid/Small Caps & Blue Chips
DEFAULT_WATCHLIST = [
    # OMX Helsinki Blue Chips & Large Caps
    "NESTE.HE",
    "KNEBV.HE",
    "NOKIA.HE",
    "UPM.HE",
    "SAMPO.HE",
    "FORTUM.HE",
    "ELISA.HE",
    "KESKOB.HE",
    "VALMT.HE",
    "WRT1V.HE",
    "ORNBV.HE",
    "HUH1V.HE",
    "KEMIRA.HE",
    "TIETO.HE",
    "STERV.HE",
    "METSA.HE",
    "OUT1V.HE",
    # Mid Cap & Growth / Dividend Leaders
    "QTCOM.HE",
    "HARVIA.HE",
    "PUUILO.HE",
    "TOKMAN.HE",
    "GOFORE.HE",
    "REG1V.HE",      # Revenio Group
    "PON1V.HE",      # Ponsse
    "TYRES.HE",      # Nokian Renkaat
    "KAMUX.HE",
    "KEMPOWR.HE",    # Kempower
    "ANORA.HE",
    "VERK.HE",
    # US Tech & Global Giants
    "AAPL",
    "MSFT",
]

# Unlisted Nordnet mutual funds (non-yfinance assets)
NORDNET_FUNDS = {
    "NN_NORGE": {"name": "Nordnet Norge Indeks Rahasto", "fallback_price": 30.818},
    "NN_SVERIGE": {"name": "Nordnet Sverige Index Rahasto", "fallback_price": 77.402},
    "NN_SUOMI": {"name": "Nordnet Suomi Indeksirahasto", "fallback_price": 10.0},
    "NN_TANSKA": {"name": "Nordnet Tanska Indeksirahasto", "fallback_price": 10.0},
}


class MarketDataClient:
    """Fetches real open-market equity data and technical indicators using yfinance."""

    def __init__(self):
        logger.info("MarketDataClient initialized using open-source yfinance market data source.")
        self.etf_watchlist = self.load_etf_watchlist()

    def load_etf_watchlist(self) -> Dict[str, Any]:
        """Loads ETF watchlist configuration from json file."""
        if config.ETF_WATCHLIST_PATH.exists():
            try:
                with open(config.ETF_WATCHLIST_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading ETF watchlist JSON: {e}")
        return {"categories": []}

    def get_etf_items(self) -> List[Dict[str, Any]]:
        """Flattens ETF categories into a list of all ETF metadata dicts."""
        items = []
        for cat in self.etf_watchlist.get("categories", []):
            for item in cat.get("items", []):
                items.append(item)
        return items

    def get_etf_symbols(self) -> List[str]:
        """Returns list of ticker symbols for all tracked ETFs."""
        return [item["symbol"] for item in self.get_etf_items() if "symbol" in item]

    def get_default_symbols(self) -> List[str]:
        """Returns combined watchlist of stock symbols and tracked ETF symbols."""
        etf_symbols = self.get_etf_symbols()
        combined = list(dict.fromkeys(DEFAULT_WATCHLIST + etf_symbols))
        return combined

    def calculate_etf_dip_score(self, market_item: Dict[str, Any], etf_meta: Dict[str, Any]) -> Dict[str, Any]:
        """Calculates a Buy-the-Dip score (1-10) tailored specifically for index ETFs."""
        rsi = market_item.get("rsi_14", 50.0)
        price = market_item.get("current_price", 0.0)
        sma_50 = market_item.get("sma_50", price)
        sma_200 = market_item.get("sma_200", price)
        
        target_rsi = etf_meta.get("target_rsi_dip", 40.0)
        sma_dip_pct = etf_meta.get("sma_dip_below_pct", 2.0)

        score = 5.0
        reasons = []

        # 1. RSI Dip Condition
        if rsi <= target_rsi:
            score += 2.5
            reasons.append(f"RSI {rsi:.1f} on dippin raja-arvon (≤ {target_rsi}) alapuolella")
        elif rsi <= target_rsi + 5.0:
            score += 1.0
            reasons.append(f"RSI {rsi:.1f} lähestyy dippirajoja (≤ {target_rsi + 5.0})")

        # 2. Price below 50-day moving average
        sma_threshold_price = sma_50 * (1.0 - (sma_dip_pct / 100.0))
        if price <= sma_threshold_price:
            score += 2.0
            reasons.append(f"Hinta {price:.2f} EUR on selvästi SMA50 ({sma_50:.2f} EUR) alapuolella (>-{sma_dip_pct}%)")
        elif price < sma_50:
            score += 1.0
            reasons.append(f"Hinta {price:.2f} EUR alle 50d keskiarvon ({sma_50:.2f} EUR)")

        # 3. Long-term trend support (Price > SMA200 means buying a dip in an uptrend)
        if price >= sma_200 and sma_200 > 0:
            score += 1.0
            reasons.append(f"Pitkän aikavälin nouseva trendi säilynyt (Hinta ≥ SMA200 {sma_200:.2f} EUR)")

        final_score = min(10.0, max(1.0, round(score, 1)))
        
        recommendation = "ODOTA / NORMAALI KUUKAUSISÄÄSTÖ"
        if final_score >= 8.0:
            recommendation = "ERINOMAINEN LISÄYSPAIKKA (BUY THE DIP)"
        elif final_score >= 6.5:
            recommendation = "HYVÄ LISÄYSPAIKKA KUUKAUSISÄÄSTÖLLE"

        return {
            "symbol": market_item.get("symbol"),
            "name": etf_meta.get("name", market_item.get("name")),
            "category": etf_meta.get("category", "ETF"),
            "score": final_score,
            "recommendation": recommendation,
            "reasons": reasons,
            "notes": etf_meta.get("notes", ""),
            "ter_percent": etf_meta.get("ter_percent", 0.0)
        }

    def get_market_data_for_symbols(self, symbols: List[str] = None, portfolio_prices: Dict[str, float] = None) -> List[Dict[str, Any]]:
        """Fetches live prices, technical indicators (RSI, SMA, EMA, Bollinger), and fundamentals for given stock tickers."""
        if not symbols:
            symbols = DEFAULT_WATCHLIST

        if not portfolio_prices:
            portfolio_prices = {}

        market_data = []
        for symbol in symbols:
            # Handle Nordnet non-exchange mutual funds directly without querying yfinance
            if symbol.startswith("NN_") or symbol in NORDNET_FUNDS:
                fund_info = NORDNET_FUNDS.get(symbol, {"name": f"Nordnet Rahasto ({symbol})", "fallback_price": 10.0})
                nav_price = portfolio_prices.get(symbol, fund_info["fallback_price"])

                market_data.append({
                    "symbol": symbol,
                    "name": fund_info["name"],
                    "sector": "Indeksirahasto (Nordnet)",
                    "current_price": round(nav_price, 2),
                    "change_24h_pct": 0.0,
                    "rsi_14": 50.0,
                    "sma_50": round(nav_price, 2),
                    "sma_200": round(nav_price, 2),
                    "ema_20": round(nav_price, 2),
                    "bollinger": {"upper": round(nav_price * 1.05, 2), "middle": round(nav_price, 2), "lower": round(nav_price * 0.95, 2), "percent_b": 0.5},
                    "dividend_yield": 0.0,
                    "pe_ratio": 0.0,
                    "trend": "NEUTRAL",
                })
                logger.info(f"Handled Nordnet mutual fund {symbol} using NAV price {nav_price:.2f} EUR")
                continue

            try:
                ticker = yf.Ticker(symbol)
                # Fetch 1 year of daily history for technical analysis
                hist = ticker.history(period="1y")

                clean_closes = hist["Close"].dropna() if not hist.empty else []
                if len(clean_closes) == 0:
                    logger.warning(f"No valid yfinance price data for {symbol}. Using portfolio fallback price.")
                    fallback_price = portfolio_prices.get(symbol, 1.0)
                    closes = [fallback_price]
                    current_price = fallback_price
                    prev_close = fallback_price
                    change_24h_pct = 0.0
                else:
                    closes = [float(x) for x in clean_closes.tolist()]
                    current_price = closes[-1]
                    # Scale down London pence (GBp) to GBP/EUR
                    if symbol.endswith(".L") and current_price > 500:
                        closes = [x / 100.0 for x in closes]
                        current_price = current_price / 100.0

                    prev_close = closes[-2] if len(closes) > 1 else current_price
                    change_24h_pct = round(((current_price - prev_close) / prev_close) * 100, 2)

                # Technical Indicators
                rsi_14 = calculate_rsi(closes, period=14)
                sma_50 = calculate_sma(closes, period=50)
                sma_200 = calculate_sma(closes, period=200)
                ema_20 = calculate_ema(closes, period=20)
                bollinger = calculate_bollinger_bands(closes, period=20, num_std=2.0)

                # Fundamental Information from yfinance info dictionary
                info = {}
                try:
                    info = ticker.info or {}
                except Exception:
                    info = {}

                # Currency conversion for USD assets
                asset_currency = (info.get("currency") or "EUR").upper()
                if asset_currency == "USD" or symbol == "NVDA":
                    try:
                        eur_usd_rate = yf.Ticker("EURUSD=X").history(period="1d")["Close"].iloc[-1]
                        usd_to_eur = 1.0 / float(eur_usd_rate) if eur_usd_rate > 0 else 0.867
                    except Exception:
                        usd_to_eur = 0.867
                    closes = [x * usd_to_eur for x in closes]
                    current_price = current_price * usd_to_eur

                stock_name = info.get("shortName") or info.get("longName") or symbol
                sector = info.get("sector") or "General"
                dividend_yield = info.get("dividendYield") or 0.0
                if dividend_yield > 1.0:
                    dividend_yield = dividend_yield / 100.0
                pe_ratio = info.get("trailingPE") or info.get("forwardPE") or 0.0

                # Determine Trend
                if current_price > sma_50 > sma_200:
                    trend = "BULLISH"
                elif current_price < sma_50 < sma_200:
                    trend = "BEARISH"
                else:
                    trend = "NEUTRAL"

                market_data.append({
                    "symbol": symbol,
                    "name": stock_name,
                    "sector": sector,
                    "current_price": round(current_price, 2),
                    "change_24h_pct": change_24h_pct,
                    "rsi_14": round(rsi_14, 1),
                    "sma_50": round(sma_50, 2),
                    "sma_200": round(sma_200, 2),
                    "ema_20": round(ema_20, 2),
                    "bollinger": bollinger,
                    "dividend_yield": round(dividend_yield, 4) if dividend_yield else 0.0,
                    "pe_ratio": round(pe_ratio, 1) if pe_ratio else 0.0,
                    "trend": trend,
                })
                logger.info(f"Fetched market data for {symbol}: Price {current_price:.2f} EUR, RSI {rsi_14:.1f}, Trend {trend}")

            except Exception as e:
                logger.error(f"Error fetching yfinance market data for {symbol}: {e}")

        return market_data
