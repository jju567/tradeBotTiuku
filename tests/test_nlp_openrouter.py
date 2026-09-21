"""
Unit tests for OpenRouter LLM text analysis, rate limiting, chunking, and JSON error handling.
"""

import json
import pytest
from unittest.mock import MagicMock, patch

from screener.nlp_analyzer import (
    chunk_text,
    extract_json_from_llm_response,
    analyze_text,
    analyze_core_fundamentals,
    classify_release_strategy,
    SAFE_DEFAULT_RESPONSE,
    FinancialNLPAnalyzer
)


def test_chunk_text_under_limit():
    text = "Word " * 500
    chunks = chunk_text(text, max_words=1000)
    assert len(chunks) == 1
    assert len(chunks[0].split()) == 500


def test_chunk_text_over_limit():
    text = "Word " * 3500
    chunks = chunk_text(text, max_words=1500)
    assert len(chunks) == 3
    assert len(chunks[0].split()) == 1500
    assert len(chunks[1].split()) == 1500
    assert len(chunks[2].split()) == 500


def test_extract_json_from_llm_response_clean_and_fenced():
    # 1. Clean JSON
    raw_1 = '{"cash_issue": true, "management_buying": false, "positive_guidance": true}'
    parsed_1 = extract_json_from_llm_response(raw_1)
    assert parsed_1["cash_issue"] is True
    assert parsed_1["positive_guidance"] is True

    # 2. Markdown code fenced JSON
    raw_2 = """Here is the financial analysis:
```json
{
  "cash_issue": false,
  "management_buying": true,
  "positive_guidance": false
}
```
Hope this helps!"""
    parsed_2 = extract_json_from_llm_response(raw_2)
    assert parsed_2["management_buying"] is True
    assert parsed_2["cash_issue"] is False


def test_extract_json_malformed_raises():
    with pytest.raises(json.JSONDecodeError):
        extract_json_from_llm_response("This is not valid json at all")


@patch("screener.nlp_analyzer.requests.post")
def test_analyze_text_openrouter_success(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"cash_issue": false, "management_buying": true, "positive_guidance": true}'
                }
            }
        ]
    }
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    result = analyze_text(
        text_content="Faron management bought shares and upgraded guidance.",
        api_key="mock-openrouter-key",
        throttle_sleep_seconds=0.0  # Fast unit test
    )

    assert result["cash_issue"] is False
    assert result["management_buying"] is True
    assert result["positive_guidance"] is True
    assert mock_post.call_count == 1


@patch("screener.nlp_analyzer.requests.post")
def test_analyze_core_fundamentals_openrouter(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "profile_A_growth": {
                            "gross_margin_over_40": True,
                            "rule_of_40_passed": True,
                            "recurring_revenue_mentioned": True,
                        },
                        "profile_B_value": {
                            "strong_net_cash_position": True,
                            "positive_operating_cash_flow": True,
                            "turnaround_indicators": False,
                        },
                        "financial_safety": {
                            "going_concern_risk": False,
                        },
                        "verdict_details": {
                            "matched_profile": "GROWTH",
                            "verdict": "STRONG BUY",
                            "reasoning": "High margin software compounder with over 40% growth."
                        }
                    })
                }
            }
        ]
    }
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    res = analyze_core_fundamentals(
        text_content="Osavuosikatsaus Q3: Liikevaihdon kasvu 30%, SaaS-tuotot 80%, EBIT 15%.",
        api_key="mock-openrouter-key",
        throttle_sleep_seconds=0.0,
    )

    assert res["profile_A_growth"]["gross_margin_over_40"] is True
    assert res["profile_A_growth"]["rule_of_40_passed"] is True
    assert res["profile_A_growth"]["recurring_revenue_mentioned"] is True
    assert res["profile_B_value"]["strong_net_cash_position"] is True
    assert res["financial_safety"]["going_concern_risk"] is False
    assert res["verdict_details"]["matched_profile"] == "GROWTH"
    assert res["verdict_details"]["verdict"] == "STRONG BUY"
    assert res["core_quality_passed"] is True


@patch("screener.nlp_analyzer.requests.post")
def test_analyze_core_fundamentals_dilution_and_cash_burn_rejection(mock_post):
    # Test that even if LLM claimed STRONG BUY, dilution_risk_detected or unsustainable_cash_burn forces REJECT
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "profile_A_growth": {
                            "gross_margin_over_40": True,
                            "rule_of_40_passed": True,
                            "recurring_revenue_mentioned": True,
                            "organic_growth_confirmed": False,
                        },
                        "profile_B_value": {
                            "strong_net_cash_position": False,
                            "positive_operating_cash_flow": False,
                            "turnaround_indicators": False,
                        },
                        "financial_safety": {
                            "going_concern_risk": False,
                            "dilution_risk_detected": True,
                            "unsustainable_cash_burn": True,
                            "erratic_pivots_detected": False,
                        },
                        "verdict_details": {
                            "matched_profile": "GROWTH",
                            "verdict": "STRONG BUY",
                            "reasoning": "High margin on paper but severe dilution and cash burn."
                        }
                    })
                }
            }
        ]
    }
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    res = analyze_core_fundamentals(
        text_content="10-Q filing: 1-for-20 reverse stock split completed, operating cash burn will exhaust cash in 2 quarters.",
        api_key="mock-openrouter-key",
        throttle_sleep_seconds=0.0,
    )

    assert res["financial_safety"]["dilution_risk_detected"] is True
    assert res["financial_safety"]["unsustainable_cash_burn"] is True
    assert res["verdict_details"]["matched_profile"] == "NONE"
    assert res["verdict_details"]["verdict"] == "REJECT"
    assert res["core_quality_passed"] is False


def test_classify_release_strategy_router():
    assert classify_release_strategy("Faron Oy: Osavuosikatsaus Q3 2026") == "CORE"
    assert classify_release_strategy("Kamux Oyj: Tilinpäätöstiedote 2025") == "CORE"
    assert classify_release_strategy("Wired Oy: Puolivuosikatsaus tammi-kesäkuu") == "CORE"
    assert classify_release_strategy("Harvia Oyj: Johdon liiketoimet") == "SATELLITE"
    assert classify_release_strategy("Remedy: Sisäpiiritieto: Positiivinen tulosvaroitus") == "SATELLITE"
    assert classify_release_strategy("Nokia Oyj: Lehdistötiedote") == "SATELLITE"


def test_build_analysis_prompt_and_analyze_company():
    from screener.nlp_analyzer import build_analysis_prompt, analyze_company, MASTER_SYSTEM_PROMPT

    prompt = build_analysis_prompt(
        ticker="QTCOM.HE",
        market="FI",
        news_text="Qt Group: Uusi suurtilaus autoteollisuudelta.",
        filing_text="Osavuosikatsaus Q2 2026: Liikevaihto kasvoi 15%."
    )

    assert "[HARD FINANCIAL FACTS - DO NOT RECALCULATE]" in prompt
    assert "[MARKET]\nFI" in prompt
    assert "[NEWS / PÖRSSITIEDOTTEET]" in prompt
    assert "[FILING TEXT / TILINPÄÄTÖSDOKUMENTTI - LAADULLISEEN KONTEKSTIIN]" in prompt

    # Test analyze_company with mock llm_client
    mock_client = MagicMock()
    mock_client.generate.return_value = {
        "judgment": "STRONG_BUY",
        "confidence": 0.95,
        "pedagogical_reasoning": "Vahva tase ja positiivinen OCF.",
        "triggered_hard_filters": [],
    }

    res = analyze_company("QTCOM.HE", "FI", "Uutinen", "Raportti", llm_client=mock_client)
    assert res["judgment"] == "STRONG_BUY"
    assert res["confidence"] == 0.95
    mock_client.generate.assert_called_once()

