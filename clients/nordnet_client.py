import logging
from typing import Dict, List, Any
import config
from clients.market_data_client import MarketDataClient

logger = logging.getLogger(__name__)


class NordnetClient:
    """Advisory mode market data client (powered by open-source MarketDataClient)."""

    def __init__(self, is_sandbox: bool = True):
        self.market_client = MarketDataClient()
        logger.info("NordnetClient operating in Advisory Mode using open-source yfinance market data.")

    def get_portfolio_holdings(self, tiuku_holdings: Dict[str, Any] = None) -> Dict[str, Any]:
        """Calculates current portfolio holdings and valuations based on live yfinance market prices.

        If ``total_equity_override`` is set in tiuku_portfolio.json (via ``--set-equity``),
        that value is used as the portfolio total for weight calculations instead of the
        yfinance-derived sum. This keeps portfolio weights accurate when yfinance prices
        diverge from the real Nordnet account value.
        """
        if not tiuku_holdings or not tiuku_holdings.get("holdings"):
            from core.portfolio import PortfolioManager
            tiuku_holdings = PortfolioManager().load_state()

        symbols = list(tiuku_holdings.get("holdings", {}).keys())
        market_list = self.market_client.get_market_data_for_symbols(symbols)
        price_dict = {item["symbol"]: item["current_price"] for item in market_list}

        holdings_data = {}
        total_stock_value = 0.0

        for symbol, data in tiuku_holdings.get("holdings", {}).items():
            qty = data.get("quantity", 0)
            avg_price = data.get("avg_price", 0.0)
            current_price = price_dict.get(symbol, avg_price)

            market_val = round(qty * current_price, 2)
            total_stock_value += market_val

            unrealized_pnl = round((current_price - avg_price) * qty, 2)
            unrealized_pnl_pct = round(((current_price / avg_price) - 1.0) * 100, 2) if avg_price > 0 else 0.0

            holdings_data[symbol] = {
                "symbol": symbol,
                "name": data.get("name", symbol),
                "quantity": qty,
                "avg_price": avg_price,
                "current_price": current_price,
                "market_value": market_val,
                "currency": tiuku_holdings.get("currency", "EUR"),
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
                "target_weight": data.get("target_weight", 0.10),
                "hodl": data.get("hodl", False),
                "note": data.get("note", ""),
            }

        cash_balance = tiuku_holdings.get("cash_balance", 0.0)
        yfinance_total = round(total_stock_value + cash_balance, 2)

        # Use manually set Nordnet total equity if available (overrides yfinance-computed sum)
        equity_override = tiuku_holdings.get("total_equity_override")
        if equity_override and equity_override > 0:
            total_equity = round(float(equity_override), 2)
            logger.info(
                f"Using total_equity_override {total_equity:,.2f} EUR "
                f"(yfinance computed: {yfinance_total:,.2f} EUR, "
                f"diff: {total_equity - yfinance_total:+,.2f} EUR)"
            )
        else:
            total_equity = yfinance_total

        # Calculate actual weights against the authoritative total equity
        for symbol, h in holdings_data.items():
            h["weight"] = round(h["market_value"] / total_equity, 4) if total_equity > 0 else 0.0

        cash_weight = round(cash_balance / total_equity, 4) if total_equity > 0 else 0.0

        return {
            "account_id": "TIUKU-LOCAL",
            "currency": tiuku_holdings.get("currency", "EUR"),
            "cash_balance": cash_balance,
            "cash_weight": cash_weight,
            "total_stock_value": round(total_stock_value, 2),
            "total_equity": total_equity,
            "holdings": holdings_data,
        }


    def get_market_data(self, symbols: List[str] = None) -> List[Dict[str, Any]]:
        """Fetches market data using open-source yfinance."""
        return self.market_client.get_market_data_for_symbols(symbols)
