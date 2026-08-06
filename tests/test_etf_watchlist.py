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
