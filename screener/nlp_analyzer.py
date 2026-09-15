"""
NLP & LLM Analysis Module for Financial News & Regulatory Filings using OpenRouter API.

Extracts structured financial signals from unstructured company releases and reports:
1. `cash_issue`: Running out of cash, funding needs, covenant distress, or dilution.
2. `management_buying`: Explicit mentions of CEO, Board, or insider share purchases.
3. `positive_guidance`: Guidance upgrades / positive profit warnings.

Includes:
- OpenRouter Free Tier integration with exponential backoff for 429 / 5xx rate limits.
- Mandatory post-request throttling sleep.
- Text chunking for context window protection.
- Deterministic fallback for offline testing or missing API keys.
"""

import json
import logging
import os
import re
import time
from typing import Optional, Dict, Any, List

import requests

from .config import ScreenerConfig
from .models import ScrapedRelease, NLPExtractionResult

logger = logging.getLogger(__name__)

# OpenRouter API Endpoints and Defaults
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3-8b-instruct:free"

# Rate Limiting Configuration
DEFAULT_THROTTLE_SLEEP = 3.0      # Mandatory politeness delay between successful API requests (seconds)
MAX_RETRIES = 3                   # Maximum retries on 429 or 5xx errors
RETRY_BACKOFF_DELAYS = [5.0, 10.0, 20.0]  # Progressive sleep on rate-limit breaches (seconds)

SAFE_DEFAULT_RESPONSE: Dict[str, bool] = {
    "insider_buying_personal": False,
    "company_share_buyback": False,
    "cash_issue": False,
    "positive_guidance": False,
    # Backwards compatibility alias:
    "management_buying": False,
}

SYSTEM_PROMPT = """You are an expert quantitative financial analyst specializing in Nordic and Helsinki Nasdaq micro-cap disclosures.
Analyze the provided company disclosure text and extract financial signals strictly according to these rules:

RULES:
1. "company_share_buyback": Set to TRUE ONLY if the text mentions 'omien osakkeiden hankinta' (acquisition of own shares), 'share buyback program', or a broker acquiring shares on behalf of the COMPANY. When TRUE, set "insider_buying_personal" to FALSE.
2. "insider_buying_personal": Set to TRUE ONLY if the text is a 'Johdon liiketoimet' (Manager's transactions / MAR notification) and explicitly states that an individual person (e.g., CEO, Board Member, CFO) has purchased shares ('hankinta' / 'merkintä' / 'purchase' / 'subscription') with their personal funds.
3. "cash_issue": Set to TRUE if there are explicit mentions of severe liquidity distress, running out of working capital ('käyttöpääoma loppunut', 'maksuvalmius heikentynyt'), immediate emergency financing needs, going concern warnings, or emergency dilutive bridge financing.
4. "positive_guidance": Set to TRUE if the company is raising its revenue/profit guidance or issuing a positive profit warning ('positiivinen tulosvaroitus', 'nostaa ohjeistustaan', 'upgrades guidance'). Set to FALSE if negative profit warning or lowered outlook.

OUTPUT FORMAT:
Output strictly a valid JSON object with no explanations or preamble:
{
  "insider_buying_personal": bool,
  "company_share_buyback": bool,
  "cash_issue": bool,
  "positive_guidance": bool
}"""


def chunk_text(text: str, max_words: int = 1500) -> List[str]:
    """
    Splits long unstructured text (press releases, PDF reports) into
    manageable chunks based on word count to protect the LLM context window.

    :param text: Raw text content to split.
    :param max_words: Maximum number of words per chunk.
    :return: List of text chunk strings.
    """
    if not text or not text.strip():
        return []

    words = text.split()
    if len(words) <= max_words:
        return [text]

    chunks = []
    for i in range(0, len(words), max_words):
        chunk = " ".join(words[i : i + max_words])
        chunks.append(chunk)

    logger.debug(f"Chunked document ({len(words)} words) into {len(chunks)} chunks of max {max_words} words.")
    return chunks


def extract_json_from_llm_response(raw_text: str) -> Dict[str, Any]:
    """
    Safely extract and parse JSON from LLM text, stripping markdown code fences if present.
    """
    if not raw_text or not raw_text.strip():
        raise json.JSONDecodeError("Empty LLM response", "", 0)

    cleaned = raw_text.strip()

    # Strip markdown code blocks e.g. ```json ... ``` or ``` ... ```
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()

    # Find the outer JSON object boundaries if surrounding conversation text exists
    start_idx = cleaned.find("{")
    end_idx = cleaned.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        cleaned = cleaned[start_idx : end_idx + 1]

    return json.loads(cleaned)


def analyze_text(
    text_content: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_words: int = 1500,
    analyze_first_chunk_only: bool = True,
    throttle_sleep_seconds: float = DEFAULT_THROTTLE_SLEEP,
) -> Dict[str, bool]:
    """
    Send unstructured text to OpenRouter LLM API to extract financial signals.
    Implements exponential backoff for 429/5xx and post-call request throttling.

    :param text_content: Unstructured article or report text.
    :param api_key: OpenRouter API key (defaults to OPENROUTER_API_KEY environment variable).
    :param model: LLM model identifier (defaults to meta-llama/llama-3-8b-instruct:free).
    :param max_words: Maximum words per chunk.
    :param analyze_first_chunk_only: When True, analyzes the executive summary/first chunk.
    :param throttle_sleep_seconds: Politeness delay after successful requests to prevent hitting RPM limits.
    :return: Dictionary with bool keys: 'cash_issue', 'management_buying', 'positive_guidance'.
    """
    key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not key:
        logger.warning("OPENROUTER_API_KEY not set. Using rule-based keyword fallback.")
        return rule_based_analyze_text(text_content)

    selected_model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)

    # 1. Chunk document to prevent exceeding context window
    chunks = chunk_text(text_content, max_words=max_words)
    if not chunks:
        return dict(SAFE_DEFAULT_RESPONSE)

    target_chunks = [chunks[0]] if analyze_first_chunk_only else chunks

    headers = {
        "Authorization": f"Bearer {key.strip()}",
        "HTTP-Referer": "https://github.com/tradeBotTiuku/screener",
        "X-Title": "Nasdaq Helsinki MicroCap Screener",
        "Content-Type": "application/json",
    }

    aggregated_result = dict(SAFE_DEFAULT_RESPONSE)

    for chunk_idx, chunk in enumerate(target_chunks):
        payload = {
            "model": selected_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Company Announcement Content:\n{chunk}"},
            ],
            "temperature": 0.0,
        }

        success = False

        # 2. Strict Rate Limiting & Exponential Backoff Loop
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.debug(f"Calling OpenRouter (attempt {attempt}/{MAX_RETRIES}, model: {selected_model})...")
                response = requests.post(
                    OPENROUTER_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=30.0,
                )

                # Handle HTTP 401/403/404 (Invalid API key, unauthorized, or deprecated model)
                if response.status_code in (401, 403, 404):
                    logger.warning(
                        f"OpenRouter API returned HTTP {response.status_code} ({response.reason or 'Auth/Model Error'}). "
                        "Skipping retries and falling back to rule-based engine."
                    )
                    break

                # Handle HTTP 429 (Rate Limit) and HTTP 5xx (Server/Upstream overload)
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    backoff = (
                        RETRY_BACKOFF_DELAYS[attempt - 1]
                        if attempt - 1 < len(RETRY_BACKOFF_DELAYS)
                        else RETRY_BACKOFF_DELAYS[-1]
                    )
                    logger.warning(
                        f"OpenRouter API returned HTTP {response.status_code} on attempt {attempt}/{MAX_RETRIES}. "
                        f"Backing off for {backoff:.1f}s before retry..."
                    )
                    time.sleep(backoff)
                    continue

                response.raise_for_status()

                # Parse JSON response
                response_data = response.json()
                choices = response_data.get("choices", [])
                if not choices:
                    logger.error("OpenRouter response contained no choices.")
                    break

                raw_llm_output = choices[0].get("message", {}).get("content", "")
                parsed_json = extract_json_from_llm_response(raw_llm_output)

                # Validate required boolean fields
                buyback = bool(parsed_json.get("company_share_buyback", False))
                # If it's a company buyback, personal insider buying is False
                personal_buying = False if buyback else bool(
                    parsed_json.get("insider_buying_personal", parsed_json.get("management_buying", False))
                )

                chunk_result = {
                    "insider_buying_personal": personal_buying,
                    "company_share_buyback": buyback,
                    "cash_issue": bool(parsed_json.get("cash_issue", False)),
                    "positive_guidance": bool(parsed_json.get("positive_guidance", False)),
                    "management_buying": personal_buying,
                }

                # Aggregate signals (OR condition across chunks)
                aggregated_result["insider_buying_personal"] |= chunk_result["insider_buying_personal"]
                aggregated_result["company_share_buyback"] |= chunk_result["company_share_buyback"]
                aggregated_result["cash_issue"] |= chunk_result["cash_issue"]
                aggregated_result["positive_guidance"] |= chunk_result["positive_guidance"]
                aggregated_result["management_buying"] |= chunk_result["management_buying"]

                success = True
                logger.info(
                    f"OpenRouter analysis successful for chunk {chunk_idx + 1}/{len(target_chunks)}: "
                    f"insider_buying_personal={chunk_result['insider_buying_personal']}, "
                    f"company_share_buyback={chunk_result['company_share_buyback']}, "
                    f"cash_issue={chunk_result['cash_issue']}, "
                    f"positive_guidance={chunk_result['positive_guidance']}"
                )
                break

            except requests.exceptions.RequestException as req_err:
                backoff = (
                    RETRY_BACKOFF_DELAYS[attempt - 1]
                    if attempt - 1 < len(RETRY_BACKOFF_DELAYS)
                    else RETRY_BACKOFF_DELAYS[-1]
                )
                logger.warning(
                    f"Network error calling OpenRouter (attempt {attempt}/{MAX_RETRIES}): {req_err}. "
                    f"Retrying in {backoff:.1f}s..."
                )
                time.sleep(backoff)

            except json.JSONDecodeError as json_err:
                logger.error(
                    f"Failed to decode JSON from LLM response: {json_err}. Raw output was: {raw_llm_output[:200] if 'raw_llm_output' in locals() else 'None'}"
                )
                return dict(SAFE_DEFAULT_RESPONSE)

            except Exception as e:
                logger.error(f"Unexpected error in OpenRouter call (attempt {attempt}): {e}")
                break

        if not success:
            logger.error(f"Exhausted all {MAX_RETRIES} retries for OpenRouter API call. Falling back to rule-based analysis.")
            rule_signals = rule_based_analyze_text(chunk)
            aggregated_result["insider_buying_personal"] |= rule_signals["insider_buying_personal"]
            aggregated_result["company_share_buyback"] |= rule_signals["company_share_buyback"]
            aggregated_result["cash_issue"] |= rule_signals["cash_issue"]
            aggregated_result["positive_guidance"] |= rule_signals["positive_guidance"]
            aggregated_result["management_buying"] |= rule_signals["management_buying"]

        # 3. Mandatory Rate Limiting Throttle Sleep between successful calls
        if throttle_sleep_seconds > 0:
            time.sleep(throttle_sleep_seconds)

    return aggregated_result


def rule_based_analyze_text(text: str) -> Dict[str, bool]:
    """
    Deterministic rule-based keyword matcher for Finnish & English disclosures.
    Used when OPENROUTER_API_KEY is not configured or in offline test environments.
    """
    if not text:
        return dict(SAFE_DEFAULT_RESPONSE)

    lower = text.lower()

    # 1. Cash Issue detection
    cash_issue = False
    cash_patterns = [
        r"kassavarat riittävät\s+(?:vain\s+)?(\d+)\s+kuukaud",
        r"kassavarantojen riittävyys",
        r"tarvitsee lisärahoitusta",
        r"käyttöpääoma ei riitä",
        r"käyttöpääoma on loppunut",
        r"maksuvalmius on (?:merkittävästi )?heikentynyt",
        r"maksuvalmiuden turvaamiseksi",
        r"yrityssaneerau(?:ksen|kseen)",
        r"toiminnan jatkuvuuteen liittyy merkittävää epävarmuutta",
        r"going concern",
        r"material uncertainty related to going concern",
        r"covenant.*breach|kovenantti.*rikko",
        r"likviditeettiasema on heikentynyt",
        r"likviditeettikriisi",
    ]
    for pattern in cash_patterns:
        if re.search(pattern, lower):
            cash_issue = True
            break

    # Positive runway override
    pos_runway = re.search(r"kassa(?:varat)? riittävät.*?(\d+)\s*kuukaut", lower)
    if pos_runway:
        try:
            if int(pos_runway.group(1)) >= 18:
                cash_issue = False
        except ValueError:
            pass

    # 2. Company share buyback detection (strictly separate from personal insider buying)
    company_buyback = any(
        k in lower
        for k in [
            "omien osakkeiden hankinta",
            "omien osakkeiden hankintaan",
            "omien osakkeiden osto",
            "acquisition of own shares",
            "repurchase of own shares",
            "share buy-back",
            "share buyback",
        ]
    )

    # 3. Personal Insider Buying (Johdon liiketoimet / MAR personal purchase)
    insider_buying_personal = False
    if not company_buyback:
        is_mar = any(
            k in lower
            for k in [
                "johdon liiketoimet",
                "johtohenkilöiden liiketoimet",
                "johtoryhmän jäsen",
                "toimitusjohtaja",
                "hallituksen jäsen",
                "hallituksen puheenjohtaja",
                "managers' transactions",
                "manager transaction",
                "insider",
                "sisäpiiri",
            ]
        )
        has_buy = any(
            k in lower
            for k in [
                "hankinta",
                "hankkinut",
                "acquisition",
                "merkintä",
                "subscription",
                "ostanut",
                "ostos",
                "bought",
                "purchased",
            ]
        )
        if is_mar and has_buy:
            insider_buying_personal = True

    # 4. Positive Guidance detection
    pos_guidance = False
    guidance_upgrade_patterns = [
        r"positiivinen\s+tulosvaroitus",
        r"nostaa\s+.*?ohjeistus",
        r"nostaa\s+.*?näkymiään",
        r"parantaa\s+.*?ohjeistus",
        r"parantaa\s+.*?näkymiään",
        r"raises?\s+.*?guidance",
        r"upgrades?\s+.*?outlook",
    ]
    for pattern in guidance_upgrade_patterns:
        if re.search(pattern, lower):
            pos_guidance = True
            break

    # Rejection if negative
    if re.search(r"negatiivinen\s+tulosvaroitus|laskee\s+.*?ohjeistus|lowers?\s+.*?guidance", lower):
        pos_guidance = False

    return {
        "insider_buying_personal": insider_buying_personal,
        "company_share_buyback": company_buyback,
        "cash_issue": cash_issue,
        "positive_guidance": pos_guidance,
        "management_buying": insider_buying_personal,
    }


class FinancialNLPAnalyzer:
    """
    High-level analyzer integrating OpenRouter API and rule-based fallbacks
    with the screener pipeline's ScrapedRelease data models.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        config: Optional[ScreenerConfig] = None,
    ):
        self.config = config or ScreenerConfig.from_env()
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)

    def analyze_release(self, release: ScrapedRelease) -> NLPExtractionResult:
        """Analyze a release document and return structured NLPExtractionResult."""
        doc_text = release.document.full_combined_text.strip()
        header_parts = [release.feed_item.title]
        if release.feed_item.category:
            header_parts.append(release.feed_item.category)
        if release.feed_item.summary:
            header_parts.append(release.feed_item.summary)

        header_text = "\n".join(header_parts)
        full_text = f"{header_text}\n\n{doc_text}".strip() if doc_text else header_text

        # Call OpenRouter analyze_text
        signals = analyze_text(
            text_content=full_text,
            api_key=self.api_key,
            model=self.model,
            throttle_sleep_seconds=0.0 if not self.api_key else DEFAULT_THROTTLE_SLEEP,
        )

        direction = "BUY" if signals["management_buying"] else "NONE"
        tx_details = "Management transaction (BUY) detected." if signals["management_buying"] else ""
        guidance_summary = "Positive guidance upgrade detected." if signals["positive_guidance"] else ""
        funding_explanation = "Cash flow / liquidity distress signaled by LLM." if signals["cash_issue"] else ""

        return NLPExtractionResult(
            release_id=release.id,
            ticker=release.feed_item.ticker,
            company_name=release.feed_item.company_name,
            has_cash_flow_issues=signals["cash_issue"],
            funding_need_explanation=funding_explanation,
            has_management_transactions=signals["management_buying"],
            transaction_direction=direction,
            transaction_details=tx_details,
            is_positive_profit_warning=signals["positive_guidance"],
            guidance_change_summary=guidance_summary,
            confidence_score=0.90 if self.api_key else 0.75,
            raw_llm_response=json.dumps(signals),
        )
