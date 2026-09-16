"""
Unit tests for screener/universe_builder.py.
"""

import csv
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from screener.universe_builder import (
    normalize_ticker_yf,
    convert_market_cap_to_eur,
    fetch_nordnet_universe,
    NordnetUniverseBuilder,
    DEFAULT_MAX_MARKET_CAP_EUR,
)


def test_normalize_ticker_yf():
    # Finnish stocks (.HE)
    assert normalize_ticker_yf("KEMIRA", "FI") == "KEMIRA.HE"
    assert normalize_ticker_yf("AALLON", "FI") == "AALLON.HE"
    assert normalize_ticker_yf("FARON.HE", "FI") == "FARON.HE"

    # Swedish stocks (.ST)
    assert normalize_ticker_yf("EVO", "SE") == "EVO.ST"
    assert normalize_ticker_yf("INTRUM", "SE") == "INTRUM.ST"
    assert normalize_ticker_yf("SEB_A", "SE") == "SEB-A.ST"

    # Danish stocks (.CO)
    assert normalize_ticker_yf("ORSTED", "DK") == "ORSTED.CO"
    assert normalize_ticker_yf("NOVO-B", "DK") == "NOVO-B.CO"

    # Norwegian stocks (.OL)
    assert normalize_ticker_yf("NEL", "NO") == "NEL.OL"
    assert normalize_ticker_yf("EQNR", "NO") == "EQNR.OL"


def test_convert_market_cap_to_eur():
    fx_rates = {"EUR": 1.0, "SEK": 10.0, "NOK": 10.0, "DKK": 7.5}

    # EUR
    assert convert_market_cap_to_eur(50_000_000, "EUR", fx_rates) == 50_000_000.0

    # SEK (500M SEK / 10 = 50M EUR)
    assert convert_market_cap_to_eur(500_000_000, "SEK", fx_rates) == 50_000_000.0

    # DKK (75M DKK / 7.5 = 10M EUR)
    assert convert_market_cap_to_eur(75_000_000, "DKK", fx_rates) == 10_000_000.0

    # Zero or missing
    assert convert_market_cap_to_eur(0, "EUR", fx_rates) == 0.0


def test_micro_cap_filtering_and_csv_generation(tmp_path):
    output_file = tmp_path / "nordnet_universe.csv"

    builder = NordnetUniverseBuilder(
        max_market_cap_eur=300_000_000,
        countries=("FI", "SE"),
        output_csv_path=output_file,
    )

    mock_fi_stocks = [
        # Small Cap (Passes: 35M EUR <= 300M EUR)
        {
            "Symbol": "AALLON",
            "Ticker_YF": "AALLON.HE",
            "Name": "Aallon Group Oyj",
            "Country": "FI",
            "Market": "First North Suomi",
            "Currency": "EUR",
            "Raw_MarketCap": 35_000_000,
            "MarketCap_EUR": 35_000_000.0,
        },
        # Large Cap (Discarded: 15B EUR > 300M EUR)
        {
            "Symbol": "NOKIA",
            "Ticker_YF": "NOKIA.HE",
            "Name": "Nokia Oyj",
            "Country": "FI",
            "Market": "Nasdaq Helsinki Large Cap",
            "Currency": "EUR",
            "Raw_MarketCap": 15_000_000_000,
            "MarketCap_EUR": 15_000_000_000.0,
        },
    ]

    mock_se_stocks = [
        # Swedish Micro-cap (Passes: 100M SEK ≈ 8.7M EUR <= 300M EUR)
        {
            "Symbol": "SWED",
            "Ticker_YF": "SWED.ST",
            "Name": "Swedish Small AB",
            "Country": "SE",
            "Market": "Spotlight Stock Market",
            "Currency": "SEK",
            "Raw_MarketCap": 100_000_000,
            "MarketCap_EUR": 8_733_624.45,
        },
        # Swedish Large-cap (Discarded: 50B SEK ≈ 4.3B EUR > 300M EUR)
        {
            "Symbol": "VOLV_B",
            "Ticker_YF": "VOLV-B.ST",
            "Name": "Volvo AB",
            "Country": "SE",
            "Market": "Large Cap Stockholm",
            "Currency": "SEK",
            "Raw_MarketCap": 50_000_000_000,
            "MarketCap_EUR": 4_366_812_227.0,
        },
    ]

    with patch.object(builder, "fetch_country_stocks") as mock_fetch:
        mock_fetch.side_effect = lambda country: mock_fi_stocks if country == "FI" else mock_se_stocks

        universe = builder.build_universe()
        builder.save_universe(universe)

    assert len(universe) == 2
    assert universe[0]["Ticker_YF"] == "AALLON.HE"
    assert universe[0]["MarketCap_EUR"] == 35_000_000.0
    assert universe[1]["Ticker_YF"] == "SWED.ST"

    # Verify CSV file output
    assert output_file.exists()
    with open(output_file, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2
        assert reader[0]["Ticker_YF"] == "AALLON.HE"
        assert reader[0]["Name"] == "Aallon Group Oyj"
        assert reader[0]["Market"] == "First North Suomi"
        assert reader[0]["MarketCap_EUR"] == "35000000.00"
        assert reader[1]["Ticker_YF"] == "SWED.ST"


def test_fetch_country_stocks_mocked_http():
    builder = NordnetUniverseBuilder(min_request_delay=0.0, max_request_delay=0.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "total_hits": 1,
        "results": [
            {
                "instrument_info": {
                    "symbol": "FARON",
                    "name": "Faron Pharmaceuticals Oy",
                    "currency": "EUR",
                    "isin": "FI4000153309",
                },
                "company_info": {
                    "market_cap": 85_000_000,
                },
                "exchange_info": {
                    "exchanges": ["First North Suomi"],
                },
            }
        ],
    }

    with patch.object(builder.session, "get", return_value=mock_resp):
        stocks = builder.fetch_country_stocks("FI", page_size=10)

    assert len(stocks) == 1
    assert stocks[0]["Ticker_YF"] == "FARON.HE"
    assert stocks[0]["MarketCap_EUR"] == 85_000_000.0
    assert stocks[0]["Market"] == "First North Suomi"


def test_anti_ban_browser_headers():
    """Verify standard browser headers and client-id spoofing."""
    builder = NordnetUniverseBuilder()
    headers = builder.session.headers

    assert "Mozilla/5.0" in headers["User-Agent"]
    assert "Chrome/120" in headers["User-Agent"]
    assert "application/json" in headers["Accept"]
    assert "fi-FI" in headers["Accept-Language"]
    assert headers["client-id"] == "NEXT"
    assert headers["sub-client-id"] == "NEXT"


def test_anti_ban_randomized_delay():
    """Verify randomized delay between 1.2s and 3.8s is called before requests."""
    builder = NordnetUniverseBuilder(min_request_delay=1.2, max_request_delay=3.8)

    with patch("time.sleep") as mock_sleep, patch("random.uniform", return_value=2.45) as mock_uniform:
        builder._apply_randomized_delay()
        mock_uniform.assert_called_once_with(1.2, 3.8)
        mock_sleep.assert_called_once_with(2.45)


def test_anti_ban_429_and_403_backoff_retry():
    """Verify that HTTP 429 / 403 triggers 60s backoff and retries cleanly without crashing."""
    builder = NordnetUniverseBuilder(
        min_request_delay=0.0,
        max_request_delay=0.0,
        retry_backoff_seconds=0.01,  # Fast unit test
        max_retries=3,
    )

    # 1. First response: 429 Too Many Requests
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429

    # 2. Second response: 200 OK
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = {
        "total_hits": 1,
        "results": [
            {
                "instrument_info": {"symbol": "BIOBV", "name": "Biohit Oyj", "currency": "EUR"},
                "company_info": {"market_cap": 35_000_000},
                "exchange_info": {"exchanges": ["Nasdaq Helsinki Small Cap"]},
            }
        ],
    }

    with patch.object(builder.session, "get", side_effect=[mock_resp_429, mock_resp_200]) as mock_get:
        stocks = builder.fetch_country_stocks("FI", page_size=10)

    assert mock_get.call_count == 2
    assert len(stocks) == 1
    assert stocks[0]["Ticker_YF"] == "BIOBV.HE"

