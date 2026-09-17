"""
Unit tests for screener/sec_crawler.py.
"""

import csv
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from screener.sec_crawler import SECEdgarCrawler


@pytest.fixture
def temp_sec_env(tmp_path):
    univ_csv = tmp_path / "nordnet_global_universe.csv"
    output_dir = tmp_path / "us_reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write dummy universe
    with open(univ_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Ticker_YF", "Name", "Market", "MarketCap_EUR"])
        writer.writerow(["KEMIRA.HE", "Kemira Oyj", "Nasdaq Helsinki", "250000000"])
        writer.writerow(["NVDA", "NVIDIA CORP", "NASDAQ", "280000000"])
        writer.writerow(["IONQ", "IonQ Inc.", "NYSE", "150000000"])

    return univ_csv, output_dir


def test_load_us_tickers_from_universe(temp_sec_env):
    univ_csv, output_dir = temp_sec_env
    crawler = SECEdgarCrawler(universe_csv_path=univ_csv, output_dir=output_dir, rate_limit_sleep=0.0)

    us_stocks = crawler.load_us_tickers_from_universe()
    tickers = [s["Ticker_YF"] for s in us_stocks]

    assert "NVDA" in tickers
    assert "IONQ" in tickers
    assert "KEMIRA.HE" not in tickers


def test_load_ticker_cik_map(temp_sec_env):
    univ_csv, output_dir = temp_sec_env
    crawler = SECEdgarCrawler(universe_csv_path=univ_csv, output_dir=output_dir, rate_limit_sleep=0.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
        "1": {"cik_str": 1824920, "ticker": "IONQ", "title": "IonQ, Inc."},
    }
    mock_resp.raise_for_status.return_value = None

    with patch.object(crawler.session, "get", return_value=mock_resp):
        cik_map = crawler.load_ticker_cik_map()
        assert cik_map["NVDA"] == 1045810
        assert cik_map["IONQ"] == 1824920


def test_get_company_filings_list(temp_sec_env):
    univ_csv, output_dir = temp_sec_env
    crawler = SECEdgarCrawler(universe_csv_path=univ_csv, output_dir=output_dir, min_year=2023, max_year=2026, rate_limit_sleep=0.0)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "filings": {
            "recent": {
                "form": ["10-Q", "10-K", "8-K"],
                "accessionNumber": ["0001-23-0001", "0001-24-0002", "0001-24-0003"],
                "primaryDocument": ["nvda-20240428.htm", "nvda-20240128.htm", "nvda-8k.htm"],
                "filingDate": ["2024-05-22", "2024-02-21", "2024-01-10"],
                "reportDate": ["2024-04-28", "2024-01-28", "2024-01-10"],
            }
        }
    }
    mock_resp.raise_for_status.return_value = None

    with patch.object(crawler.session, "get", return_value=mock_resp):
        filings = crawler.get_company_filings_list(1045810)
        assert len(filings) == 2  # 10-Q and 10-K only
        assert filings[0]["form"] == "10-Q"
        assert filings[0]["quarter"] == "Q1"
        assert filings[1]["form"] == "10-K"
        assert filings[1]["quarter"] == "FY"
