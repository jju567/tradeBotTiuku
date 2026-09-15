"""
Integration & unit tests for NLP analyzer, Quant engine, and Main controller.
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import os

from screener.config import ScreenerConfig
from screener.models import FeedItem, DocumentPayload, ScrapedRelease, ProcessingStatus, NLPExtractionResult
from screener.nlp_analyzer import FinancialNLPAnalyzer
from screener.quant_engine import QuantitativeRiskEngine, MicroCapBalanceSheetParser
from screener.main_controller import ScreenerPipelineController


def test_nlp_cash_flow_issues_detection():
    analyzer = FinancialNLPAnalyzer(api_key=None)  # Rule-based fallback

    # 1. Distressed cash release
    distressed_item = FeedItem(
        guid="rel-distress",
        title="Yhtiö X Oyj: Sisäpiiritieto: Käyttöpääoma ei riitä seuraavan 12 kuukauden tarpeisiin",
        link="https://example.com/rel/distress"
    )
    distressed_doc = DocumentPayload(
        url="https://example.com/rel/distress",
        html_text="Toiminnan jatkuvuuteen liittyy merkittävää epävarmuutta. Yhtiö tarvitsee lisärahoitusta välittömästi."
    )
    res = analyzer.analyze_release(ScrapedRelease(feed_item=distressed_item, document=distressed_doc))
    assert res.has_cash_flow_issues is True

    # 2. Healthy runway release
    healthy_item = FeedItem(
        guid="rel-healthy",
        title="Faron Pharmaceuticals Oy: Tilinpäätöstiedote 2025",
        link="https://example.com/rel/healthy"
    )
    healthy_doc = DocumentPayload(
        url="https://example.com/rel/healthy",
        html_text="Kassavarat riittävät nykyisen suunnitelman mukaan seuraavat 24 kuukautta."
    )
    res_healthy = analyzer.analyze_release(ScrapedRelease(feed_item=healthy_item, document=healthy_doc))
    assert res_healthy.has_cash_flow_issues is False


def test_nlp_management_transactions_detection():
    analyzer = FinancialNLPAnalyzer(api_key=None)

    # Buy transaction
    buy_item = FeedItem(
        guid="rel-mar-buy",
        title="Faron Pharmaceuticals Oy: Johdon liiketoimet",
        link="https://example.com/rel/buy",
        category="Johdon liiketoimet"
    )
    buy_doc = DocumentPayload(
        url="https://example.com/rel/buy",
        html_text="Ilmoitusvelvollinen: Toimitusjohtaja. Liiketoimen luonne: Hankinta. Volyymi: 50 000 kpl. Hinta: 2.10 EUR."
    )
    res_buy = analyzer.analyze_release(ScrapedRelease(feed_item=buy_item, document=buy_doc))
    assert res_buy.has_management_transactions is True
    assert res_buy.transaction_direction == "BUY"

    # Sell transaction (management_buying is False, direction is NONE)
    sell_item = FeedItem(
        guid="rel-mar-sell",
        title="Nightingale Health Oyj: Johtohenkilöiden liiketoimet",
        link="https://example.com/rel/sell",
        category="Johtohenkilöiden liiketoimet"
    )
    sell_doc = DocumentPayload(
        url="https://example.com/rel/sell",
        html_text="Liiketoimen luonne: Luovutus / myynti pörssissä."
    )
    res_sell = analyzer.analyze_release(ScrapedRelease(feed_item=sell_item, document=sell_doc))
    assert res_sell.has_management_transactions is False
    assert res_sell.transaction_direction == "NONE"


def test_nlp_positive_profit_warning():
    analyzer = FinancialNLPAnalyzer(api_key=None)

    pw_item = FeedItem(
        guid="rel-pw",
        title="Kamux Oyj: Sisäpiiritieto: Positiivinen tulosvaroitus – Nostaa koko vuoden näkymiään",
        link="https://example.com/rel/pw"
    )
    pw_doc = DocumentPayload(
        url="https://example.com/rel/pw",
        html_text="Kamux nostaa ohjeistustaan vahvan kysynnän ansiosta."
    )
    res_pw = analyzer.analyze_release(ScrapedRelease(feed_item=pw_item, document=pw_doc))
    assert res_pw.is_positive_profit_warning is True


def test_balance_sheet_table_parsing():
    tables = [
        [
            ["Tase / Varat (TEUR)", "31.12.2025", "31.12.2024"],
            ["Myyntisaamiset", "1 200", "950"],
            ["Rahat ja pankkisaamiset", "4 500", "2 100"],
            ["Varat yhteensä", "12 000", "8 500"],
        ],
        [
            ["Rahavirtalaskelma (TEUR)", "1-12/2025", "1-12/2024"],
            ["Liiketoiminnan rahavirta", "-600", "-1 200"],
        ]
    ]
    cash, burn = MicroCapBalanceSheetParser.extract_cash_and_burn("", tables)
    assert cash == 4500.0
    assert burn == 600.0


def test_quant_risk_engine_filters(monkeypatch):
    engine = QuantitativeRiskEngine()
    monkeypatch.setattr(engine, "_get_market_spread_and_price", lambda ticker: (1.5, 2.50))
    monkeypatch.setattr(engine, "_calculate_cash_runway", lambda ticker, rel: (24.0, 10_000_000.0, 0.0))

    feed_item = FeedItem(guid="g1", title="Test Release", link="https://test.com", ticker="TEST")
    doc = DocumentPayload(url="https://test.com", html_text="Report text")
    release = ScrapedRelease(feed_item=feed_item, document=doc)

    # 1. Reject if cash flow issue flagged
    nlp_bad_cash = NLPExtractionResult(
        release_id="g1",
        has_cash_flow_issues=True,
        funding_need_explanation="Short runway < 6m"
    )
    cand_bad = engine.evaluate_candidate(release, nlp_bad_cash)
    assert cand_bad.passed_filters is False
    assert any("Cash Alert" in r for r in cand_bad.rejection_reasons)

    # 2. Pass if clean NLP and valid parameters
    nlp_clean = NLPExtractionResult(release_id="g1", has_cash_flow_issues=False)
    cand_good = engine.evaluate_candidate(release, nlp_clean)
    assert cand_good.passed_filters is True


def test_pipeline_controller_cycle(tmp_path, monkeypatch):
    db_file = tmp_path / "test_screener.db"
    csv_file = tmp_path / "alerts.csv"

    cfg = ScreenerConfig(db_path=db_file)
    controller = ScreenerPipelineController(config=cfg, alerts_csv_path=csv_file)

    mock_release = FeedItem(
        guid="mock-item-1",
        title="Faron Pharmaceuticals Oy: Positiivinen tulosvaroitus",
        link="https://mock.com/1",
        company_name="Faron Pharmaceuticals Oy",
        ticker="FARON"
    )
    mock_doc = DocumentPayload(
        url="https://mock.com/1",
        html_text="Faron nostaa ohjeistustaan. Rahat ja pankkisaamiset ovat 25 MEUR."
    )

    # Mock fetch_latest_releases and document parsing
    monkeypatch.setattr("screener.main_controller.fetch_latest_releases", lambda config: [mock_release])
    monkeypatch.setattr(controller.scraper, "fetch_and_parse_document", lambda url: mock_doc)
    monkeypatch.setattr(
        "screener.main_controller.check_liquidity_and_spread",
        lambda ticker, **kwargs: {
            "passed_spread_check": True,
            "spread_pct": 1.25,
            "bid": 2.40,
            "ask": 2.43,
            "volume": 15000,
            "total_friction_pct": 2.65,
            "commission_round_trip_eur": 14.0,
            "min_recommended_trade_eur": 500.0,
            "reason": "Passed liquidity and spread checks",
        }
    )

    candidates = controller.run_pipeline_cycle()

    assert len(candidates) == 1
    assert candidates[0]["positive_guidance"] is True
    assert candidates[0]["spread_pct"] == 1.25
    assert csv_file.exists()

    # Second run should skip already processed release
    candidates_second_run = controller.run_pipeline_cycle()
    assert len(candidates_second_run) == 0


def test_check_liquidity_and_spread_filters(monkeypatch):
    from quant_engine import check_liquidity_and_spread

    class MockTickerPass:
        info = {
            "bid": 10.00,
            "ask": 10.20,
            "regularMarketVolume": 50000,
        }

    class MockTickerHighSpread:
        info = {
            "bid": 1.00,
            "ask": 1.10,  # 9.1% spread
            "regularMarketVolume": 10000,
        }

    class MockTickerLowVolume:
        info = {
            "bid": 5.00,
            "ask": 5.10,  # 2.0% spread
            "regularMarketVolume": 500,  # < 2000
        }

    class MockTickerClosed:
        info = {
            "bid": 0.0,
            "ask": 0.0,
            "regularMarketVolume": 0,
        }

    import yfinance as yf

    # 1. Pass Case
    monkeypatch.setattr(yf, "Ticker", lambda sym: MockTickerPass())
    res_pass = check_liquidity_and_spread("KEMIRA")
    assert res_pass["passed_spread_check"] is True
    assert res_pass["spread_pct"] == 1.96
    assert res_pass["volume"] == 50000

    # 2. High Spread Rejection
    monkeypatch.setattr(yf, "Ticker", lambda sym: MockTickerHighSpread())
    res_high_spread = check_liquidity_and_spread("WAPICE", max_spread_pct=4.0)
    assert res_high_spread["passed_spread_check"] is False
    assert res_high_spread["spread_pct"] == 9.09
    assert "too high" in res_high_spread["reason"]

    # 3. Low Volume Rejection
    monkeypatch.setattr(yf, "Ticker", lambda sym: MockTickerLowVolume())
    res_low_vol = check_liquidity_and_spread("MICRO", min_volume=2000)
    assert res_low_vol["passed_spread_check"] is False
    assert "below minimum threshold" in res_low_vol["reason"]

    # 4. Market Closed / No Quotes
    monkeypatch.setattr(yf, "Ticker", lambda sym: MockTickerClosed())
    res_closed = check_liquidity_and_spread("CLOSED")
    assert res_closed["passed_spread_check"] is False
    assert "Market closed" in res_closed["reason"]

    # 5. Nordnet Small Investor Total Friction Rejection
    # Spread 3.80% (under 4.0% limit), but with round trip commission of 14 EUR on 500 EUR (2.80%), total friction is 6.60% > 5.5% limit
    class MockTickerMarginalSpread:
        info = {
            "bid": 9.62,
            "ask": 10.00,  # 3.80% spread
            "regularMarketVolume": 10000,
        }
    monkeypatch.setattr(yf, "Ticker", lambda sym: MockTickerMarginalSpread())
    res_friction = check_liquidity_and_spread(
        "MARGINAL",
        max_spread_pct=4.0,
        max_total_friction_pct=5.5,
        sample_trade_size_eur=500.0,
        commission_min_eur=7.0,
    )
    assert res_friction["passed_spread_check"] is False
    assert "Total friction" in res_friction["reason"]
    assert res_friction["total_friction_pct"] == 6.60
