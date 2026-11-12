import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core.portfolio import PortfolioManager, StateVault
from main_controller import MarketDataEngine, PortfolioInstance, PortfolioConfig
from screener.tiered_filters import (
    matches_nordic_midcap_compounder,
    matches_nordic_microcap_catalyst,
    matches_us_microcap_growth,
)


# =====================================================================
# 1. LLM API Katkossuoja (Graceful Degradation)
# =====================================================================

def test_evaluate_news_radar_graceful_degradation_on_llm_exception(tmp_path):
    """
    Test that when LLM API throws an exception (e.g. network down, rate limit, timeout):
      - System does not crash.
      - catalyst_source is set to 'API_DOWN'.
      - manual_review_required is set to True.
      - Candidate receives 'MANUAL_REVIEW_API_DOWN' in audit_flags.
    """
    engine = MarketDataEngine()
    engine.hard_facts_cache["TEST.HE"] = {
        "roe": 0.20,
        "ebit_margin": 0.15,
        "free_cash_flow": 1_000_000.0,
    }

    mock_news = [
        {"title": "Test Company announces quarterly results", "summary": "Profitable quarter"}
    ]
    test_archive = tmp_path / "nlp_decisions_archive.csv"

    with patch("main_controller.DEFAULT_NLP_ARCHIVE_CSV", test_archive), \
         patch("yfinance.Ticker") as mock_ticker, \
         patch.dict("os.environ", {"OPENROUTER_API_KEY": "fake_key"}), \
         patch("screener.nlp_analyzer.analyze_core_fundamentals", side_effect=RuntimeError("Rate limit / API down 429")):
        
        mock_inst = MagicMock()
        mock_inst.news = mock_news
        mock_ticker.return_value = mock_inst

        verdict, reason = engine._evaluate_news_radar("TEST.HE")

        # Järjestelmä ei kaadu
        assert verdict in ("HOLD", "WARN")
        hf = engine.hard_facts_cache["TEST.HE"]
        assert hf.get("catalyst_source") == "API_DOWN_MANUAL_CHECK"
        assert hf.get("score_impact") == 0.0
        assert hf.get("reject") is False
        assert hf.get("manual_review") is True
        assert hf.get("manual_review_required") is True
        assert hf.get("requires_manual_review") is True
        assert hf.get("reason") == "Gemini API kaatui tai ei vastannut. Tarkista katalyytti käsin."
        assert "MANUAL_REVIEW_API_DOWN" in hf.get("audit_flags", [])
        assert "Gemini API kaatui tai ei vastannut" in hf.get("audit_warning", "")


def test_core_candidate_passes_normally_without_news_catalyst():
    """
    Core compounders (Nordic_MidCap_Compounder) do not require a news catalyst
    and must pass normally even when catalyst_source is 'API_DOWN'.
    """
    meta = {
        "market": "FI",
        "market_cap_eur": 250_000_000.0,
        "adv_20d_local": 100_000.0,
        "currency": "EUR",
    }
    hard_facts = {
        "roe": 0.22,
        "ebit_margin": 0.16,
        "free_cash_flow": 15_000_000.0,
        "net_debt_to_ebitda": 1.2,
        "catalyst_source": "API_DOWN",
        "manual_review_required": True,
    }
    passed, reasons = matches_nordic_midcap_compounder("CORE.HE", hard_facts, meta)
    assert passed is True
    assert len(reasons) == 0


def test_satellite_turnaround_rejected_when_catalyst_source_api_down():
    """
    Satellite turnaround candidate requiring news catalyst must be rejected
    from automatic entry when catalyst_source is 'API_DOWN'.
    """
    meta = {
        "market": "FI",
        "market_cap_eur": 50_000_000.0,
        "currency": "EUR",
        "adv_20d_local": 35_000.0,
    }
    hard_facts = {
        "is_profile_a": False,
        "is_profile_b": False,
        "cash_runway_months": 24.0,
        "catalyst_source": "API_DOWN",
        "manual_review_required": True,
    }
    passed, reasons = matches_nordic_microcap_catalyst("SAT.HE", hard_facts, meta, nlp_verdict="WATCH_TURNAROUND")
    assert passed is False
    assert any("REJECTED_UNVERIFIED_CATALYST" in r for r in reasons)


# =====================================================================
# 2. 7 Päivän Rullaava Varmuuskopio (State Vault)
# =====================================================================

def test_state_vault_rolling_backups(tmp_path):
    """
    Test StateVault creation, automated backups/ directory creation,
    and rolling Finnish weekday backup naming.
    """
    state_file = tmp_path / "portfolio_P13_Live_Candidate_state.json"
    state_file.write_text(json.dumps({"portfolio_id": "P13_Live_Candidate", "cash": 5000}), encoding="utf-8")

    vault = StateVault()
    # Mock datetime to a specific Monday (2026-11-09 was a Monday)
    dt_monday = datetime(2026, 11, 9, 14, 30)
    backup_path = vault.backup_state(state_file, dt=dt_monday)

    assert backup_path is not None
    assert backup_path.exists()
    assert backup_path.name == "portfolio_P13_Live_Candidate_state_maanantai.bak"
    assert backup_path.parent.name == "backups"
    assert backup_path.parent == tmp_path / "backups"

    # Backup on Wednesday
    dt_wednesday = datetime(2026, 11, 11, 10, 0)
    backup_wed = vault.backup_state(state_file, dt=dt_wednesday)
    assert backup_wed.name == "portfolio_P13_Live_Candidate_state_keskiviikko.bak"
    assert backup_wed.exists()

    # Verify content preserved
    data = json.loads(backup_wed.read_text(encoding="utf-8"))
    assert data["cash"] == 5000


def test_save_state_triggers_state_vault(tmp_path):
    """
    Test that PortfolioManager.save_state automatically invokes StateVault.
    """
    pf_file = tmp_path / "tiuku_portfolio.json"
    hf_file = tmp_path / "history.json"
    mgr = PortfolioManager(portfolio_file=pf_file, history_file=hf_file)

    # Calling save_state should create backup in backups/
    mgr.save_state()

    backup_dir = tmp_path / "backups"
    assert backup_dir.exists()
    backups = list(backup_dir.glob("tiuku_portfolio_*.bak"))
    assert len(backups) >= 1


# =====================================================================
# 3. Osakesplit- ja Datan Vääristymän Hälytin (_sync_to_p13_state)
# =====================================================================

def test_stock_split_alarm_and_prevent_blind_overwrite(tmp_path, caplog):
    """
    Test 2:1 stock split on Alma Media (ALMA.HE) and normal update on TIETO.HE:
      - Initial ALMA: 100 shares @ 10.00 EUR (Capital Invested 1000.0)
      - Imported ALMA: 200 shares @ 5.00 EUR (new_qty >= old_qty * 1.5 AND new_price <= old_price * 0.75)
      - Initial TIETO: 50 shares @ 20.00 EUR
      - Imported TIETO: 50 shares @ 21.00 EUR (normal update)
      - Result:
        * Critical error logged: "⚠️ SPLIT DETECTED (ALMA.HE) - MANUAL STATE UPDATE REQUIRED. Kpl muuttui: 100 -> 200."
        * Blind overwrite prevented on ALMA: Shares remains 100.0, Buy Price remains 10.00 EUR
        * ALMA update skipped via continue, keeping old values intact
        * TIETO processed normally (Shares 50, Buy Price 21.00)
    """
    import logging
    pf_file = tmp_path / "tiuku_portfolio.json"
    hf_file = tmp_path / "history.json"
    mgr = PortfolioManager(portfolio_file=pf_file, history_file=hf_file)

    p13_file = tmp_path / "portfolio_P13_Live_Candidate_state.json"
    initial_p13 = {
        "portfolio_id": "P13_Live_Candidate",
        "positions": [
            {
                "Ticker": "ALMA.HE",
                "Shares": 100.0,
                "Buy Price": 10.00,
                "Peak Price": 12.00,
                "Highest Close": 12.00,
                "highest_price_seen": 12.00,
                "stop_threshold": 8.50,
                "Capital Invested": 1000.0,
            },
            {
                "Ticker": "TIETO.HE",
                "Shares": 50.0,
                "Buy Price": 20.00,
                "Capital Invested": 1000.0,
            }
        ],
    }
    p13_file.write_text(json.dumps(initial_p13), encoding="utf-8")

    updated_holdings = {
        "ALMA.HE": {
            "name": "Alma Media",
            "quantity": 200,
            "avg_price": 5.00,
            "market_value_eur": 1000.0,
        },
        "TIETO.HE": {
            "name": "Tietoevry",
            "quantity": 50,
            "avg_price": 21.00,
            "market_value_eur": 1050.0,
        }
    }

    with caplog.at_level(logging.ERROR):
        synced = mgr._sync_to_p13_state(p13_file, updated_holdings)
    assert synced is True

    # 1. Tarkista kriittinen varoitusloki täsmällisellä tekstillä
    expected_error = "⚠️ SPLIT DETECTED (ALMA.HE) - MANUAL STATE UPDATE REQUIRED. Kpl muuttui: 100 -> 200."
    assert any(expected_error in record.message for record in caplog.records)

    updated_data = json.loads(p13_file.read_text(encoding="utf-8"))
    positions = {p["Ticker"]: p for p in updated_data["positions"]}

    # 2. Estetty sokea ylikirjoittaminen: ALMA säilyttää vanhan varmistetun tilan (continue ohitti päivityksen)
    alma = positions["ALMA.HE"]
    assert alma["Shares"] == 100.0
    assert alma["Buy Price"] == 10.00
    assert alma["Capital Invested"] == 1000.0
    assert alma.get("split_detected") is True
    assert alma.get("split_pending_manual_update") is True
    assert alma.get("manual_review_required") is True
    assert alma.get("unapplied_split_shares") == 200.0
    assert alma.get("unapplied_split_price") == 5.00
    assert "SPLIT_DETECTED_MANUAL_REQUIRED" in alma.get("Audit_Flags", [])

    # 3. Prosessi jatkoi muiden osakkeiden käsittelyä normaalisti
    tieto = positions["TIETO.HE"]
    assert tieto["Shares"] == 50.0
    assert tieto["Buy Price"] == 21.00
    assert round(tieto["Capital Invested"], 2) == 1050.0


def test_data_distortion_alarm(tmp_path):
    """
    Test data distortion:
      - Initial: 100 shares @ 10.00 EUR
      - Imported: 300 shares @ 10.00 EUR (3x shares without inverse price adjustment)
      - Result:
        * data_distortion_detected is True
        * manual_review_required is True
        * DATA_DISTORTION_WARNING in Audit_Flags
    """
    pf_file = tmp_path / "tiuku_portfolio.json"
    hf_file = tmp_path / "history.json"
    mgr = PortfolioManager(portfolio_file=pf_file, history_file=hf_file)

    p13_file = tmp_path / "portfolio_P13_Live_Candidate_state.json"
    initial_p13 = {
        "portfolio_id": "P13_Live_Candidate",
        "positions": [
            {
                "Ticker": "DISTORT.HE",
                "Shares": 100.0,
                "Buy Price": 10.00,
                "Peak Price": 10.00,
                "stop_threshold": 8.50,
            }
        ],
    }
    p13_file.write_text(json.dumps(initial_p13), encoding="utf-8")

    updated_holdings = {
        "DISTORT.HE": {
            "name": "Distorted Stock",
            "quantity": 300,
            "avg_price": 10.00,  # Price unchanged despite 3x share count
        }
    }

    synced = mgr._sync_to_p13_state(p13_file, updated_holdings)
    assert synced is True

    updated_data = json.loads(p13_file.read_text(encoding="utf-8"))
    pos = updated_data["positions"][0]

    assert pos.get("data_distortion_detected") is True
    assert pos.get("manual_review_required") is True
    assert "DATA_DISTORTION_WARNING" in pos.get("Audit_Flags", [])
    assert "DATAN VÄÄRISTYMÄ" in pos.get("Audit_Warning", "")
