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


@patch("screener.nlp_analyzer.time.sleep")
@patch("screener.nlp_analyzer.requests.post")
def test_analyze_text_rate_limit_429_retry_success(mock_post, mock_sleep):
    # 1st call returns 429, 2nd call returns 200
    resp_429 = MagicMock()
    resp_429.status_code = 429

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": '{"cash_issue": true, "management_buying": false, "positive_guidance": false}'
                }
            }
        ]
    }
    resp_200.raise_for_status.return_value = None

    mock_post.side_effect = [resp_429, resp_200]

    result = analyze_text(
        text_content="Company needs urgent refinancing.",
        api_key="mock-openrouter-key",
        throttle_sleep_seconds=0.0
    )

    assert result["cash_issue"] is True
    assert mock_post.call_count == 2
    assert mock_sleep.called


@patch("screener.nlp_analyzer.requests.post")
def test_analyze_text_malformed_json_fallback(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {"message": {"content": "I am unable to parse this report properly."}}
        ]
    }
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    result = analyze_text(
        text_content="Unparseable report text",
        api_key="mock-openrouter-key",
        throttle_sleep_seconds=0.0
    )

    assert result == SAFE_DEFAULT_RESPONSE
