"""
Unit and Integration Tests for Nordnet Master Universe Ingestion and Intelligent Document Router.
"""

import tempfile
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from screener.config import ScreenerConfig
from screener.models import FeedItem, DocumentPayload, StrategyType
from screener.main_controller import (
    load_nordnet_universe,
    match_universe_item,
    route_document,
    ScreenerPipelineController,
)


def test_load_nordnet_universe_default():
    """Test loading the generated Nordic master universe."""
    universe = load_nordnet_universe()
    assert universe["total_count"] > 0
    assert "EXEL.HE" in universe["by_ticker_yf"]
    assert "EXEL" in universe["by_base_ticker"]


def test_match_universe_item_by_ticker_and_name():
    """Test matching incoming feed items against the universe."""
    mock_universe = {
        "by_ticker_yf": {
            "FARON.HE": {"Ticker_YF": "FARON.HE", "Name": "Faron Pharmaceuticals Oy", "Market": "Nasdaq Helsinki", "MarketCap_EUR": 25000000.0},
            "EVO.ST": {"Ticker_YF": "EVO.ST", "Name": "Evolution AB", "Market": "Stockholm", "MarketCap_EUR": 250000000.0},
        },
        "by_base_ticker": {
            "FARON": {"Ticker_YF": "FARON.HE", "Name": "Faron Pharmaceuticals Oy", "Market": "Nasdaq Helsinki", "MarketCap_EUR": 25000000.0},
            "EVO": {"Ticker_YF": "EVO.ST", "Name": "Evolution AB", "Market": "Stockholm", "MarketCap_EUR": 250000000.0},
        },
        "by_name": {
            "faron pharmaceuticals": {"Ticker_YF": "FARON.HE", "Name": "Faron Pharmaceuticals Oy", "Market": "Nasdaq Helsinki", "MarketCap_EUR": 25000000.0},
            "evolution": {"Ticker_YF": "EVO.ST", "Name": "Evolution AB", "Market": "Stockholm", "MarketCap_EUR": 250000000.0},
        },
        "total_count": 2,
    }

    # 1. Match by exact Ticker_YF
    item1 = FeedItem(guid="1", title="Update", link="https://ex.com/1", ticker="FARON.HE")
    matched1 = match_universe_item(item1, mock_universe)
    assert matched1 is not None
    assert matched1["Ticker_YF"] == "FARON.HE"

    # 2. Match by base ticker
    item2 = FeedItem(guid="2", title="Update", link="https://ex.com/2", ticker="EVO")
    matched2 = match_universe_item(item2, mock_universe)
    assert matched2 is not None
    assert matched2["Ticker_YF"] == "EVO.ST"

    # 3. Match by company name
    item3 = FeedItem(guid="3", title="Update", link="https://ex.com/3", company_name="Faron Pharmaceuticals Oy")
    matched3 = match_universe_item(item3, mock_universe)
    assert matched3 is not None
    assert matched3["Ticker_YF"] == "FARON.HE"

    # 4. Match by title prefix
    item4 = FeedItem(guid="4", title="[FARON] Johdon kaupat", link="https://ex.com/4")
    matched4 = match_universe_item(item4, mock_universe)
    assert matched4 is not None
    assert matched4["Ticker_YF"] == "FARON.HE"

    # 5. Non-universe stock returns None
    item5 = FeedItem(guid="5", title="Nokia Oyj: Lehdistötiedote", link="https://ex.com/5", ticker="NOKIA.HE", company_name="Nokia Oyj")
    matched5 = match_universe_item(item5, mock_universe)
    assert matched5 is None


def test_route_document_trash():
    """Test routing routine administrative disclosures to TRASH."""
    trash_titles = [
        "Kutsu varsinaiseen yhtiökokoukseen",
        "Kutsu ylimääräiseen yhtiökokoukseen",
        "Yhtiökokouksen päätökset 2026",
        "Varsinaisen yhtiökokouksen päätökset",
        "Hallituksen järjestäytyminen",
        "Nimitystoimikunnan ehdotukset yhtiökokoukselle",
        "Kallelse till årsstämma",
        "Kallelse till extra bolagsstämma",
        "Beslut vid årsstämma",
        "Kommuniké från årsstämma",
        "Konstituerande styrelsemöte",
        "Notice to Annual General Meeting",
        "Resolutions of the Annual General Meeting",
        "Decisions of the Extraordinary General Meeting",
        "Constitutive meeting of the Board of Directors",
        "Osakepalkkiojärjestelmän ansaintajakso",
        "Incitamentsprogram för ledande befattningshavare",
        "Taloudellinen kalenteri vuodelle 2026",
        "Taloudellisen katsauksen julkistamisajankohdat",
        "Finansiell kalender",
        "Financial reporting calendar",
        "Liputusilmoitus arvopaperimarkkinalain mukaan",
        "Flaggningsmeddelande",
    ]

    for title in trash_titles:
        route = route_document(title)
        assert route == "TRASH", f"Expected '{title}' to route to TRASH, got '{route}'"


def test_route_document_core():
    """Test routing quarterly reports and annual statements to CORE."""
    core_titles = [
        "Faron Oy: Osavuosikatsaus Q3 2026",
        "Puolivuosikatsaus tammi-kesäkuu 2026",
        "Tilinpäätöstiedote 2025",
        "Vuosikertomus ja tilinpäätös 2025",
        "Liiketoimintakatsaus tammi-maaliskuu 2026",
        "Delårsrapport januari-september 2026",
        "Bokslutskommuniké 2025",
        "Halvårsrapport 2026",
        "Kvartalsrapport Q2 2026",
        "Interim Report Q1-Q3 2026",
        "Half-year financial report 2026",
        "Financial Statement Release 2025",
        "Annual Report 2025",
    ]

    for title in core_titles:
        route = route_document(title)
        assert route == StrategyType.CORE.value, f"Expected '{title}' to route to CORE, got '{route}'"

    # PDF attachment heuristic
    pdf_route = route_document("Yhtiö tiedottaa tuloksestaan", has_pdf=True)
    assert pdf_route == StrategyType.CORE.value


def test_route_document_satellite():
    """Test routing catalysts, insider trades, and profit warnings to SATELLITE."""
    sat_titles = [
        "Harvia Oyj: Johdon liiketoimet",
        "Johtohenkilöiden liiketoimet: Toimitusjohtaja osti osakkeita",
        "Sisäpiiritieto: Positiivinen tulosvaroitus – Nostaa ohjeistustaan",
        "Tulosvaroitus: Yhtiö nostaa liikevoittonäkymiään",
        "Evolution AB: Insynshandel",
        "Ledande befattningshavares transaktioner",
        "Omvänd vinstvarning",
        "Vinstvarning: Höjer prognos för helåret",
        "Managers' transactions: CEO purchase",
        "Inside information: Positive profit warning",
        "Merkittävä tilaus Pohjois-Amerikasta",
        "Yhtiö solmi merkittävän puitesopimuksen",
        "Lehdistötiedote: Uusi tuotejulkistus",
    ]

    for title in sat_titles:
        route = route_document(title)
        assert route == StrategyType.SATELLITE.value, f"Expected '{title}' to route to SATELLITE, got '{route}'"


def test_main_controller_trash_and_universe_filtering(tmp_path, monkeypatch):
    """Test that main_controller discards TRASH and outside-universe releases before LLM processing."""
    db_file = tmp_path / "test_screener.db"
    csv_file = tmp_path / "alerts.csv"

    cfg = ScreenerConfig(db_path=db_file)
    controller = ScreenerPipelineController(
        config=cfg,
        alerts_csv_path=csv_file,
        open_positions_path=tmp_path / "open_positions.csv",
        trade_history_path=tmp_path / "trade_history.csv",
        watchlist_turnarounds_path=tmp_path / "watchlist_turnarounds.csv",
        enforce_universe=True,
    )

    # Inject mock universe containing only FARON.HE
    controller.universe_data = {
        "by_ticker_yf": {"FARON.HE": {"Ticker_YF": "FARON.HE", "Name": "Faron Pharmaceuticals Oy"}},
        "by_base_ticker": {"FARON": {"Ticker_YF": "FARON.HE", "Name": "Faron Pharmaceuticals Oy"}},
        "by_name": {"faron pharmaceuticals": {"Ticker_YF": "FARON.HE", "Name": "Faron Pharmaceuticals Oy"}},
        "total_count": 1,
    }

    releases = [
        # 1. Non-universe stock -> Discarded
        FeedItem(
            guid="out-1",
            title="Nokia Oyj: Positiivinen tulosvaroitus",
            link="https://mock.com/out1",
            company_name="Nokia Oyj",
            ticker="NOKIA.HE"
        ),
        # 2. In-universe stock but TRASH AGM noise -> Discarded
        FeedItem(
            guid="trash-1",
            title="Faron Pharmaceuticals Oy: Kutsu varsinaiseen yhtiökokoukseen",
            link="https://mock.com/trash1",
            company_name="Faron Pharmaceuticals Oy",
            ticker="FARON"
        ),
        # 3. In-universe stock & SATELLITE catalyst -> Processed
        FeedItem(
            guid="sat-1",
            title="Faron Pharmaceuticals Oy: Positiivinen tulosvaroitus",
            link="https://mock.com/sat1",
            company_name="Faron Pharmaceuticals Oy",
            ticker="FARON"
        ),
    ]

    mock_doc = DocumentPayload(
        url="https://mock.com/sat1",
        html_text="Faron nostaa ohjeistustaan vahvan kasvun ansiosta. Kassavarat 25 MEUR."
    )

    monkeypatch.setattr("screener.main_controller.fetch_latest_releases", lambda config: releases)
    monkeypatch.setattr(controller.scraper, "fetch_and_parse_document", lambda url: mock_doc)
    monkeypatch.setattr(
        "screener.main_controller.check_liquidity_and_spread",
        lambda ticker, **kwargs: {
            "passed_spread_check": True,
            "spread_pct": 1.10,
            "bid": 2.40,
            "ask": 2.43,
            "volume": 15000,
            "total_friction_pct": 2.50,
            "commission_round_trip_eur": 14.0,
            "min_recommended_trade_eur": 500.0,
            "reason": "Passed checks",
        }
    )
    monkeypatch.setattr(
        controller.web_verifier,
        "verify",
        lambda **kwargs: {"passed_web_check": True, "reason": "Passed test mock"}
    )

    candidates = controller.run_pipeline_cycle()

    assert len(candidates) == 1
    assert candidates[0]["ticker"] == "FARON.HE"
    assert candidates[0]["strategy_type"] == "SATELLITE"
    assert candidates[0]["positive_guidance"] is True
