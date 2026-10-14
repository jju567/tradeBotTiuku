import pytest
import config
from clients.market_data_client import MarketDataClient


def test_etf_watchlist_loading():
    client = MarketDataClient()
    watchlist = client.load_etf_watchlist()
    assert "categories" in watchlist
    assert len(watchlist["categories"]) >= 4

    symbols = client.get_etf_symbols()
    assert "EUNL.DE" in symbols
    assert "VWCE.DE" in symbols
    assert "EXSA.DE" in symbols
    assert "VHYL.L" in symbols
    assert "IQQH.DE" in symbols
    assert "QDVE.DE" in symbols




def test_etf_dip_score_calculation():
    client = MarketDataClient()
    
    etf_meta = {
        "symbol": "EUNL.DE",
        "name": "iShares Core MSCI World",
        "category": "core",
        "target_rsi_dip": 40.0,
        "sma_dip_below_pct": 2.0,
        "ter_percent": 0.20
    }

    # Case 1: Oversold ETF dip condition (RSI 38, price below SMA50)
    market_dip = {
        "symbol": "EUNL.DE",
        "current_price": 95.0,
        "rsi_14": 38.0,
        "sma_50": 100.0,
        "sma_200": 90.0,
    }
    score_res = client.calculate_etf_dip_score(market_dip, etf_meta)
    assert score_res["score"] >= 8.0
    assert score_res["recommendation"] == "ERINOMAINEN LISÄYSPAIKKA (BUY THE DIP)"

    # Case 2: Overbought / high RSI (no dip)
    market_high = {
        "symbol": "EUNL.DE",
        "current_price": 105.0,
        "rsi_14": 65.0,
        "sma_50": 100.0,
        "sma_200": 95.0,
    }
    score_normal = client.calculate_etf_dip_score(market_high, etf_meta)
    assert score_normal["score"] < 7.0


def test_market_data_client_converts_usd_indicators_consistently():
    from unittest.mock import MagicMock, patch
    import pandas as pd

    client = MarketDataClient()
    usd_prices = [100.0 + i * 1.0 for i in range(250)]
    df = pd.DataFrame({"Close": usd_prices})

    mock_ticker = MagicMock()
    mock_ticker.history.return_value = df
    mock_ticker.info = {
        "currency": "USD",
        "shortName": "Apple Inc.",
        "sector": "Technology",
    }

    fx_df = pd.DataFrame({"Close": [1.25]})
    mock_fx_ticker = MagicMock()
    mock_fx_ticker.history.return_value = fx_df

    def ticker_side_effect(sym):
        if sym == "EURUSD=X":
            return mock_fx_ticker
        return mock_ticker

    with patch("clients.market_data_client.yf.Ticker", side_effect=ticker_side_effect):
        data = client.get_market_data_for_symbols(["AAPL"])

    assert len(data) == 1
    res = data[0]
    assert res["current_price"] == pytest.approx(279.20, abs=0.5)
    assert res["sma_50"] < res["current_price"]
    assert res["sma_200"] < res["sma_50"]
    assert res["trend"] == "BULLISH"
