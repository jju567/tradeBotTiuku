"""
Unit tests for screener/historical_pdf_crawler.py.
"""

import tempfile
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from screener.historical_pdf_crawler import (
    extract_period_and_year,
    generate_company_slug_candidates,
    HistoricalPDFCrawler,
    CORE_REPORT_KEYWORDS_REGEX,
    EXCLUSION_KEYWORDS_REGEX,
)


def test_extract_period_and_year():
    # 1. Finnish titles
    y1, p1 = extract_period_and_year("Kemira Oyj:n puolivuosikatsaus 2024: Kannattavuus parani")
    assert y1 == 2024
    assert p1 == "Q2"

    y2, p2 = extract_period_and_year("Raute Oyj: Tilinpäätöstiedote 2023")
    assert y2 == 2023
    assert p2 == "FY"

    y3, p3 = extract_period_and_year("Faron Pharmaceuticals: Liiketoimintakatsaus tammi-syyskuu 2024 (Q3)")
    assert y3 == 2024
    assert p3 == "Q3"

    y4, p4 = extract_period_and_year("Qt Group: Osavuosikatsaus tammi-maaliskuu 2025")
    assert y4 == 2025
    assert p4 == "Q1"

    # 2. Swedish titles
    y5, p5 = extract_period_and_year("Evolution AB: Delårsrapport januari-september 2024")
    assert y5 == 2024
    assert p5 == "Q3"

    y6, p6 = extract_period_and_year("Bokslutskommuniké 2023")
    assert y6 == 2023
    assert p6 == "FY"

    # 3. English titles
    y7, p7 = extract_period_and_year("Interim Report Q1 2024")
    assert y7 == 2024
    assert p7 == "Q1"


def test_generate_company_slug_candidates():
    slugs = generate_company_slug_candidates("Kemira Oyj")
    assert "kemira-oyj" in slugs
    assert "kemira" in slugs

    slugs_qt = generate_company_slug_candidates("Qt Group Plc")
    assert "qt-group-plc" in slugs_qt
    assert "qt-group" in slugs_qt


def test_keyword_matching_and_exclusions():
    # Reports match
    assert CORE_REPORT_KEYWORDS_REGEX.search("Kemira Oyj:n puolivuosikatsaus 2024")
    assert CORE_REPORT_KEYWORDS_REGEX.search("Tilinpäätöstiedote 2023")
    assert CORE_REPORT_KEYWORDS_REGEX.search("Interim Report Q3 2024")
    assert CORE_REPORT_KEYWORDS_REGEX.search("Delårsrapport januari-mars 2025")

    # Administrative noise excluded
    assert EXCLUSION_KEYWORDS_REGEX.search("Kutsu varsinaiseen yhtiökokoukseen")
    assert EXCLUSION_KEYWORDS_REGEX.search("Kallelse till årsstämma")
    assert EXCLUSION_KEYWORDS_REGEX.search("Hallituksen järjestäytyminen")
    assert EXCLUSION_KEYWORDS_REGEX.search("Johdon liiketoimet: Toimitusjohtaja osti osakkeita")


def test_crawler_load_universe(tmp_path):
    csv_file = tmp_path / "nordnet_universe.csv"
    csv_file.write_text(
        "Ticker_YF,Name,Market,MarketCap_EUR\n"
        "KEMIRA.HE,Kemira Oyj,Nasdaq Helsinki,250000000.00\n"
        "RAUTE.HE,Raute Corporation,Nasdaq Helsinki,90000000.00\n"
        "FARON.HE,Faron Pharmaceuticals,First North Suomi,85000000.00\n",
        encoding="utf-8"
    )

    crawler = HistoricalPDFCrawler(
        universe_csv_path=csv_file,
        output_dir=tmp_path / "reports",
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
    )

    stocks = crawler.load_universe(limit=2)
    assert len(stocks) == 2
    assert stocks[0]["Ticker_YF"] == "KEMIRA.HE"
    assert stocks[1]["Ticker_YF"] == "RAUTE.HE"

    # Filter by ticker
    filtered = crawler.load_universe(filter_ticker="FARON")
    assert len(filtered) == 1
    assert filtered[0]["Ticker_YF"] == "FARON.HE"


def test_extract_reports_and_download(tmp_path):
    reports_dir = tmp_path / "reports"
    crawler = HistoricalPDFCrawler(
        universe_csv_path=tmp_path / "dummy.csv",
        output_dir=reports_dir,
        min_delay_seconds=0.0,
        max_delay_seconds=0.0,
    )

    mock_html = """
    <html>
      <body>
        <a href="/fi/kemira/r/kemira-oyj-puolivuosikatsaus-2024,c123">Kemira Oyj:n puolivuosikatsaus 2024</a>
        <a href="/fi/kemira/r/kutsu-yhtiokokoukseen,c124">Kutsu yhtiökokoukseen 2024</a>
        <a href="/fi/kemira/r/tilinpaatostiedote-2023,c125">Kemira Oyj: Tilinpäätöstiedote 2023</a>
      </body>
    </html>
    """

    mock_release_html = """
    <html>
      <body>
        <a href="https://mb.cision.com/Main/123/456/kemira_h1_2024.pdf">Lataa raportti (PDF)</a>
      </body>
    </html>
    """

    mock_pdf_bytes = b"%PDF-1.4 Mock PDF Content"

    def mock_get(url, *args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        if "c123" in url:
            mock_resp.text = mock_release_html
            mock_resp.content = mock_release_html.encode("utf-8")
        elif ".pdf" in url:
            mock_resp.content = mock_pdf_bytes
            mock_resp.text = ""
        else:
            mock_resp.text = mock_html
            mock_resp.content = mock_html.encode("utf-8")
        return mock_resp

    with patch.object(crawler, "_safe_get", side_effect=mock_get):
        candidates = crawler.extract_reports_from_cision_archive(
            company_url="https://news.cision.com/fi/kemira",
            ticker="KEMIRA.HE",
            company_name="Kemira Oyj",
            max_pages=1,
        )

        assert len(candidates) == 2  # puolivuosikatsaus 2024 & tilinpaatostiedote 2023 (kutsu excluded)
        assert candidates[0].target_filename == "KEMIRA.HE_2024_Q2.pdf"
        assert candidates[1].target_filename == "KEMIRA.HE_2023_FY.pdf"

        # Test download
        pdf_url = crawler.extract_pdf_download_url(candidates[0].release_url)
        assert pdf_url == "https://mb.cision.com/Main/123/456/kemira_h1_2024.pdf"

        success = crawler.download_pdf(pdf_url, candidates[0].target_filename)
        assert success is True
        assert (reports_dir / "KEMIRA.HE_2024_Q2.pdf").exists()
