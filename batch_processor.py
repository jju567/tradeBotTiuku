"""
Batch Processor for Mass Historical Earnings Reports Analysis.

Iterates over historical reports (.pdf and .txt) in `data/historical_reports/`,
extracts report content, calls the Dual-Lens LLM (Profile A Growth & Profile B Value)
via OpenRouter with rate limiting (3s delay) and exponential retry on 429/502 errors (60s backoff),
and appends structured results to `data/batch_results.csv`.

Maintains state in `data/processed_files.log` to support resumption.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, Any, List, Set
from dotenv import load_dotenv

import requests

# Load environment variables (.env)
load_dotenv()

# Cross-platform stdout encoding fix (Windows cp1252 fix)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("screener.batch_processor")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_REPORTS_DIR = BASE_DIR / "data" / "historical_reports"
DEFAULT_PROCESSED_LOG = BASE_DIR / "data" / "processed_files.log"
DEFAULT_RESULTS_CSV = BASE_DIR / "data" / "batch_results.csv"
DEFAULT_STATUS_JSON = BASE_DIR / "data" / "batch_status.json"

# OpenRouter Constants
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_FREE_MODEL = os.getenv("OPENROUTER_FREE_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
DEFAULT_PAID_MODEL = os.getenv("OPENROUTER_PAID_MODEL", "google/gemini-2.5-flash")
DEFAULT_OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", DEFAULT_FREE_MODEL)
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 60.0
THROTTLE_DELAY_SECONDS = 7.0

# Dual-Lens Core System Prompt
CORE_SYSTEM_PROMPT = """You are an elite quantitative micro/small-cap equity analyst and risk officer specializing in global equities (US NASDAQ/NYSE under $300M market cap, and Nordic markets .HE, .ST, .OL, .CO). Your task is to evaluate financial documents (10-Q, 10-K, interim reports, regulatory filings) and recent news headlines/PR snippets.

Your primary directive is CAPITAL PRESERVATION: rigorously identify and reject toxic financing, ongoing dilution, unsustainable cash burn, and structural value traps before hunting for asymmetric upside.

---

# SECTION 1: MANDATORY HARD FILTERS (INSTANT REJECT)
If ANY of the following conditions are met, the stock MUST receive a verdict of "REJECT", regardless of net cash position or potential narrative:

1. DILUTION & CAPITAL DESTRUCTION:
   - Mentions of past or pending reverse stock splits (e.g., 1-for-5, 1-for-20).
   - Usage of toxic debt, death-spiral financing, or aggressive Equity Lines of Credit (ELOC, SEPA) that dilute existing common shares.
   - Frequent dilutive share issuances, heavy warrant overhangs, or ATM (At-The-Market) continuous offerings.
   - Local Nordic terms indicating dilutive rights issues or emergency financing: "nyemission", "företrädesemission", "suunnattu anti" (if dilutive to common equity without clear accretive M&A).

2. UNSUSTAINABLE CASH BURN & RUNWAY RISK:
   - Operating cash flow is deeply negative, and total cash / liquid equivalents will be exhausted in less than 4 quarters (12 months) at the current burn rate.
   - Explicit "Going Concern" warnings or acute liquidity covenants default warnings.

3. ERRATIC STRATEGIC PIVOTS / SHELL RISKS:
   - Sudden, unrelated business model pivots (e.g., pivot from cosmetics/retail to AI, blockchain, biotech, or mining within a short timeframe).
   - SPAC empty shells without commercial operating assets.

4. NEGATIVE SHAREHOLDERS' EQUITY:
   - Stock has deeply negative book equity resulting from cumulative operational losses, unless explicitly offset by non-recourse project structures.

5. SWEDISH LEGAL LANDMINES:
   - If the market is Sweden (.ST), instantly REJECT if the text mentions "kontrollbalansräkning" or "rekonstruktion".

---

# SECTION 2: DUAL-LENS INVESTMENT PROFILES
Only stocks passing ALL Section 1 filters can be evaluated for Profile A or Profile B:

### PROFILE A: HIGH-MARGIN COMPOUNDER (GROWTH / SAAS)
- Gross Margin > 40% (preferably > 60%).
- Organic revenue growth YoY (> 15% preferred).
- High recurring revenue (SaaS, subscriptions, software maintenance) or expanding high-margin hardware/product footprint.
- Clear path to or already positive operating cash flow.

### PROFILE B: DEEP VALUE / OPERATIONAL TURNAROUND
- Pristine or highly defended balance sheet (Net Cash positive or Debt/Equity <= 0.3).
- Positive operating cash flow OR demonstrable narrowing of operating losses via structural cost reductions.
- STRICT ANTI-SHRINKING RULE: A zero-debt balance sheet alone is INSUFFICIENT. If core business metrics, revenue, or active users/customers are contracting year-over-year without an active operational fix, DO NOT award STRONG BUY.
- Catalyst Requirement: If past quarters were unprofitable, there must be a tangible operational catalyst (tier-1 management replacement, divesting of money-losing segments, or strategic accretive contracts).

---

# SECTION 3: REAL-TIME DIVERGENCE & VERDICT ROUTING
1. "STRONG BUY": Passes all Section 1 safety filters, satisfies Profile A or B, and web news confirms clean operational momentum.
2. "HOLD": Financially solvent and safe, but lacks growth momentum, high margins, or compelling catalysts.
3. "REJECT": Fails any Section 1 hard filter, OR Web News reveals active red flags.
4. "WATCH_TURNAROUND": Document analysis yields poor historical figures, BUT recent verified web news reveals a major turnaround catalyst (CEO/insider buying, multi-million contracts, structural restructuring).

---

# SECTION 4: OUTPUT FORMAT SPECIFICATION
Respond exclusively with a valid, parseable JSON object matching this schema:
{
  "ticker": "string",
  "market": "US" | "FI" | "SE" | "OTHER",
  "profile": "PROFILE_A" | "PROFILE_B" | "NONE",
  "verdict": "STRONG BUY" | "HOLD" | "REJECT" | "WATCH_TURNAROUND",
  "flags": {
    "dilution_risk_detected": bool,
    "unsustainable_cash_burn": bool,
    "erratic_pivots_detected": bool,
    "shrinking_business": bool,
    "organic_growth_confirmed": bool,
    "passed_web_sanity_check": bool
  },
  "metrics": {
    "gross_margin_pct": number_or_null,
    "revenue_growth_yoy_pct": number_or_null,
    "cash_runway_quarters": number_or_null,
    "net_cash_positive": bool
  },
  "pedagogical_reasoning": "Concise, fact-based rationale (2-4 sentences) outlining exact balance sheet conditions, cash burn figures, and the impact of the latest news snippets. ALWAYS refer to the company by its FULL formal corporate name (e.g. 'Atomera Incorporated', 'Vivid Seats Inc.', 'Faron Pharmaceuticals Oyj') rather than bare ticker symbols."
}"""

# ANSI Color codes
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_RED = "\033[91m"
COLOR_CYAN = "\033[96m"
COLOR_BOLD = "\033[1m"
COLOR_RESET = "\033[0m"


def parse_metadata_from_filename(filename: str) -> Tuple[str, str, str]:
    """
    Extracts Ticker, Year, and Quarter/Period directly from filename.
    Examples:
      - 'KEMIRA.HE_2024_Q3.pdf' -> ('KEMIRA.HE', '2024', 'Q3')
      - 'QTCOM.HE_2023_FY.txt' -> ('QTCOM.HE', '2023', 'FY')
      - 'QT_Group_Q3_2020.pdf' -> ('QT_Group', '2020', 'Q3')
    """
    stem = Path(filename).stem
    parts = stem.split("_")

    ticker = "UNKNOWN"
    year = ""
    quarter = ""

    # Check standardized format: {Ticker}_{Year}_{Quarter}
    if len(parts) >= 3 and re.match(r"^(19|20)\d{2}$", parts[1]):
        ticker = parts[0]
        year = parts[1]
        quarter = "_".join(parts[2:])
        return ticker, year, quarter

    # Regex search for year (4 digits between 1900-2099)
    year_match = re.search(r"(?:^|[\W_])((?:19|20)\d{2})(?:$|[\W_])", stem)
    if year_match:
        year = year_match.group(1)

    # Regex search for quarter/period (Q1-Q4, H1, H2, FY)
    q_match = re.search(r"(?:^|[\W_])(Q[1-4]|H[1-2]|FY|TILINPÄÄTÖS|INTERIM|DELÅRSRAPPORT)(?:$|[\W_])", stem, re.IGNORECASE)
    if q_match:
        quarter = q_match.group(1).upper()
    else:
        quarter = "N/A"

    # Ticker / Company name is everything preceding year/quarter or first part
    if year and year in parts:
        y_idx = parts.index(year)
        ticker = "_".join(parts[:y_idx])
    elif year:
        # Split by year
        ticker = stem.split(year)[0].rstrip("_- .")
    else:
        ticker = parts[0] if parts else stem

    return ticker or "UNKNOWN", year, quarter


# Alias for convenience
parse_filename_details = parse_metadata_from_filename


def extract_text_from_file(file_path: Path, max_pages: int = 10, max_words: int = 3000) -> str:
    """Extracts and truncates text from PDF or TXT report file."""
    if not file_path.exists() or file_path.stat().st_size == 0:
        return ""

    text = ""
    if file_path.suffix.lower() == ".txt":
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:
            logger.error(f"Error reading TXT {file_path.name}: {e}")
            return ""

    elif file_path.suffix.lower() == ".pdf":
        # Try pdfplumber first
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                pages_to_read = pdf.pages[:max_pages]
                page_texts = [p.extract_text() or "" for p in pages_to_read]
                text = "\n".join(page_texts)
        except Exception:
            # Fallback to pypdf / PyPDF2
            try:
                try:
                    from pypdf import PdfReader
                except ImportError:
                    from PyPDF2 import PdfReader
                reader = PdfReader(str(file_path))
                pages_to_read = reader.pages[:max_pages]
                page_texts = [p.extract_text() or "" for p in pages_to_read]
                text = "\n".join(page_texts)
            except Exception as e:
                logger.error(f"Error parsing PDF {file_path.name}: {e}")
                return ""

    # Clean and limit words
    cleaned = re.sub(r"\s+", " ", text).strip()
    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words])
    return cleaned


def extract_json_response(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extracts JSON object from LLM response string."""
    if not raw_text or not raw_text.strip():
        return None
    cleaned = raw_text.strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if match:
            cleaned = match.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def call_llm_dual_lens(
    text: str,
    api_key: str,
    ticker: Optional[str] = None,
    hard_financials: Optional[Dict[str, Any]] = None,
    model: str = DEFAULT_FREE_MODEL,
    fallback_model: Optional[str] = DEFAULT_PAID_MODEL,
    max_retries: int = MAX_RETRIES,
    retry_backoff: float = RETRY_BACKOFF_SECONDS,
) -> Optional[Dict[str, Any]]:
    """
    Calls OpenRouter LLM API with intelligent 2-Tier cascading:
    1. Primary: Tries Free Model (e.g. meta-llama/llama-3.3-70b-instruct:free, 0.00 $ cost).
    2. Fallback: If free tier is overloaded (HTTP 429/502), cascades seamlessly to Paid Model (Gemini 2.5 Flash).
    3. Emergency: Returns None to trigger deterministic rule-based evaluation if all API calls fail.
    """
    if hard_financials is None and ticker:
        try:
            from screener.financial_metrics_engine import get_hard_financials
            hard_financials = get_hard_financials(ticker)
        except Exception as e:
            logger.debug(f"Could not retrieve hard financials for {ticker}: {e}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/jju567/tradeBotTiuku",
        "X-Title": "tradeBotTiuku Batch Processor",
    }
    
    current_model = model
    progressive_delays = [2.0, 5.0, 10.0]

    facts_block = ""
    if hard_financials:
        facts_block = (
            "[HARD FINANCIAL FACTS - DO NOT RECALCULATE]\n"
            f"{json.dumps(hard_financials, indent=2)}\n\n"
            "INSTRUCTION: Treat the provided Hard Financial Facts as absolute truth. "
            "Base your 'Section 1' survival analysis strictly on these provided numbers, "
            "and use the text document ONLY for qualitative context (management commentary, restructurings, M&A).\n\n"
        )

    user_content = f"{facts_block}Analyze this earnings report text:\n\n{text}"

    for attempt in range(1, max_retries + 1):
        payload = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": CORE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": 350,
        }

        try:
            response = requests.post(
                OPENROUTER_API_URL,
                headers=headers,
                json=payload,
                timeout=45.0,
            )

            # Credit limit reached (402 Payment Required) -> Fallback immediately
            if response.status_code == 402:
                logger.info("OpenRouter credit balance depleted (HTTP 402). Switching to deterministic rule-based analysis.")
                return None

            # Free model rate limited (429) or busy (502/503) -> Cascade to Paid Model immediately!
            if (response.status_code in [429, 502, 503, 504]) and current_model.endswith(":free") and fallback_model:
                logger.info(f"⚡ Free model '{current_model}' busy ({response.status_code}). Seamlessly cascading to paid tier '{fallback_model}'...")
                current_model = fallback_model
                time.sleep(1.0)
                continue

            if response.status_code == 429 or response.status_code == 502 or 500 <= response.status_code < 600:
                delay = progressive_delays[min(attempt - 1, len(progressive_delays) - 1)]
                logger.warning(
                    f"⚠️ Received HTTP {response.status_code} ({response.reason}) on attempt {attempt}/{max_retries}. "
                    f"Sleeping {delay}s before retry..."
                )
                time.sleep(delay)
                continue

            response.raise_for_status()
            res_json = response.json()
            
            # Record Token Usage
            usage = res_json.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if prompt_tokens > 0 or completion_tokens > 0:
                try:
                    from core.token_tracker import record_tokens
                    record_tokens(prompt_tokens, completion_tokens, model=current_model, source="batch_nlp")
                except Exception:
                    pass

            content = res_json.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = extract_json_response(content)
            if parsed:
                return parsed

        except Exception as e:
            err_str = str(e)
            if "402" in err_str:
                logger.info("OpenRouter credits depleted. Falling back to rule-based analysis.")
                return None
            
            # If free model timed out or errored, switch to paid model
            if current_model.endswith(":free") and fallback_model:
                logger.info(f"⚡ Free model request failed ({e}). Cascading to paid model '{fallback_model}'...")
                current_model = fallback_model
                time.sleep(1.0)
                continue

            logger.error(f"Error calling OpenRouter API ({current_model}) attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                delay = progressive_delays[min(attempt - 1, len(progressive_delays) - 1)]
                logger.warning(f"Sleeping {delay}s before retry...")
                time.sleep(delay)

    return None


def rule_based_fallback_evaluation(text: str) -> Dict[str, Any]:
    """Deterministic fallback when LLM API is unavailable or depleted."""
    lower = text.lower()
    going_concern = any(
        re.search(pat, lower)
        for pat in [
            r"käyttöpääoma ei riitä",
            r"toiminnan jatkuvuus",
            r"going concern",
            r"negatiivinen oma pääoma",
            r"maksuvalmius on heikentynyt",
            r"kovenanttirikko",
            r"tarvitsee lisärahoitusta",
        ]
    )

    dilution_risk = any(
        re.search(pat, lower)
        for pat in [
            r"reverse stock split",
            r"reverse split",
            r"share consolidation",
            r"osakkeiden yhdistäminen",
            r"käänteinen split",
            r"continuous dilution",
            r"death spiral",
        ]
    )

    unsustainable_cash_burn = any(
        re.search(pat, lower)
        for pat in [
            r"kassavarat eivät riitä 12",
            r"cash runway (?:of )?less than",
            r"runway is less than 12",
            r"depleted within 12 months",
            r"depleted within 4 quarters",
        ]
    )

    erratic_pivots = any(
        re.search(pat, lower)
        for pat in [
            r"pivot to crypto",
            r"pivot to ai",
            r"pivot to blockchain",
            r"pivot to bitcoin",
            r"changed business model to crypto",
            r"reverse merger with",
        ]
    )

    safety_failed = going_concern or dilution_risk or unsustainable_cash_burn or erratic_pivots

    recurring = any(
        k in lower
        for k in ["saas", "toistuva liikevaihto", "jatkuvalaskutteinen", "recurring revenue", "arr", "tilauspohjainen"]
    )
    gm_over_40 = any(
        k in lower
        for k in ["ohjelmisto", "software", "lisenssit", "myyntikate 5", "myyntikate 6", "myyntikate 7", "myyntikate 8", "myyntikate 9"]
    )
    has_growth = bool(re.search(r"kasvoi\s+(?:[2-9]\d|1\d\d)\s*%", lower) or "vahva kasvu" in lower)
    has_margin = bool(re.search(r"liikevoittomarginaali\s+(?:1[5-9]|[2-9]\d)\s*%", lower) or "kannattavuus parani" in lower)
    r40_passed = has_growth and (has_margin or recurring)

    net_cash = any(k in lower for k in ["velaton", "nettovelaton", "net cash", "vahva kassa", "vahva tase"])
    pos_ocf = any(k in lower for k in ["positiivinen rahavirta", "positive cash flow", "liiketoiminnan rahavirta oli positiivinen"])
    turnaround = any(k in lower for k in ["käänne", "tuloskäänne", "tehostamisohjelma", "säästöohjelma", "turnaround"])

    if safety_failed:
        matched_profile = "NONE"
        verdict = "REJECT"
        reasoning = "Rejected due to solvency, dilution, cash burn runway, or erratic pivot risk."
    elif gm_over_40 and recurring and r40_passed:
        matched_profile = "GROWTH"
        verdict = "STRONG BUY"
        reasoning = "High-margin recurring revenue compounder meeting Rule of 40."
    elif net_cash and (pos_ocf or turnaround):
        matched_profile = "VALUE"
        verdict = "STRONG BUY"
        reasoning = "Deep value / turnaround setup with net cash balance sheet and positive cash flow."
    else:
        matched_profile = "NONE"
        verdict = "HOLD"
        reasoning = "Viable business but incomplete growth or turnaround criteria."

    return {
        "profile_A_growth": {
            "gross_margin_over_40": gm_over_40,
            "rule_of_40_passed": r40_passed,
            "recurring_revenue_mentioned": recurring,
            "organic_growth_confirmed": True,
        },
        "profile_B_value": {
            "strong_net_cash_position": net_cash,
            "positive_operating_cash_flow": pos_ocf,
            "turnaround_indicators": turnaround,
        },
        "financial_safety": {
            "going_concern_risk": going_concern,
            "dilution_risk_detected": dilution_risk,
            "unsustainable_cash_burn": unsustainable_cash_burn,
            "erratic_pivots_detected": erratic_pivots,
        },
        "verdict_details": {
            "matched_profile": matched_profile,
            "verdict": verdict,
            "reasoning": reasoning,
        }
    }


class BatchProcessor:
    """Processes historical earnings reports in batches with statefulness and rate limits."""

    def __init__(
        self,
        reports_dir: Path = DEFAULT_REPORTS_DIR,
        processed_log_path: Path = DEFAULT_PROCESSED_LOG,
        results_csv_path: Path = DEFAULT_RESULTS_CSV,
        status_json_path: Path = DEFAULT_STATUS_JSON,
        api_key: Optional[str] = None,
        model: str = DEFAULT_OPENROUTER_MODEL,
        throttle_seconds: float = THROTTLE_DELAY_SECONDS,
        limit: Optional[int] = None,
        enable_web_check: bool = False,
        skip_processed: bool = True,
        only_latest: bool = True,
    ):
        self.reports_dir = Path(reports_dir)
        self.processed_log_path = Path(processed_log_path)
        self.results_csv_path = Path(results_csv_path)
        self.status_json_path = Path(status_json_path)
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        self.model = model
        self.throttle_seconds = throttle_seconds
        self.limit = limit
        self.enable_web_check = enable_web_check
        self.skip_processed = skip_processed
        self.only_latest = only_latest

        # Ensure directories exist
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.processed_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.results_csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_json_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize CSV header if file doesn't exist or is empty
        self.init_csv_headers()

    def update_status(
        self,
        is_running: bool,
        current_file: str = "",
        current_ticker: str = "",
        completed: int = 0,
        total: int = 0,
    ) -> None:
        """Writes live execution status to JSON file for UI consumption."""
        try:
            from datetime import datetime, timezone
            status = {
                "is_running": is_running,
                "current_file": current_file,
                "current_ticker": current_ticker,
                "completed_count": completed,
                "total_count": total,
                "pid": os.getpid(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            with open(self.status_json_path, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2)
        except Exception as e:
            logger.debug(f"Failed to update batch status json: {e}")

    def clear_status(self) -> None:
        """Marks batch status as completed."""
        self.update_status(is_running=False)

    def init_csv_headers(self) -> None:
        """Ensures results CSV has correct headers."""
        if not self.results_csv_path.exists() or self.results_csv_path.stat().st_size == 0:
            with open(self.results_csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "filename", "ticker", "year", "quarter", "matched_profile", "verdict", "reasoning"])

    def load_processed_files(self) -> Set[str]:
        """Loads already processed filenames from log file."""
        if not self.processed_log_path.exists():
            return set()
        with open(self.processed_log_path, "r", encoding="utf-8", errors="replace") as f:
            return {line.strip() for line in f if line.strip()}

    def mark_file_processed(self, filename: str) -> None:
        """Appends filename to processed log."""
        with open(self.processed_log_path, "a", encoding="utf-8") as f:
            f.write(f"{filename}\n")

    def append_result_to_csv(
        self,
        filename: str,
        ticker: str,
        year: str,
        quarter: str,
        matched_profile: str,
        verdict: str,
        reasoning: str,
        timestamp: Optional[str] = None,
    ) -> None:
        """Appends an individual evaluation row to CSV with standard 8 columns."""
        from datetime import datetime, timezone
        if not timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()
        with open(self.results_csv_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, filename, ticker, year, quarter, matched_profile, verdict, reasoning])

    def run(self) -> None:
        """Main execution loop over report files (including subdirectories like 'us/')."""
        processed_set = self.load_processed_files() if self.skip_processed else set()
        raw_files = sorted(
            [f for f in self.reports_dir.rglob("*") if f.is_file() and f.suffix.lower() in (".pdf", ".txt")]
        )
        
        # If only_latest is enabled, keep only the single newest report per ticker
        if self.only_latest:
            ticker_latest_map: Dict[str, Path] = {}
            for f in raw_files:
                tick, yr, qtr = parse_metadata_from_filename(f.name)
                # Sort key based on year and quarter rank
                q_rank = 5 if "FY" in qtr else (4 if "Q4" in qtr else (3 if "Q3" in qtr else (2 if "Q2" in qtr else (1 if "Q1" in qtr else 0))))
                yr_val = int(yr) if yr.isdigit() else 2000
                current_score = yr_val * 10 + q_rank

                if tick not in ticker_latest_map:
                    ticker_latest_map[tick] = (f, current_score)
                else:
                    prev_f, prev_score = ticker_latest_map[tick]
                    if current_score >= prev_score:
                        ticker_latest_map[tick] = (f, current_score)
            all_files = sorted([pair[0] for pair in ticker_latest_map.values()], key=lambda x: x.name)
            logger.info(f"Deduplicated to latest report per ticker: {len(all_files)} unique tickers (from {len(raw_files)} total files).")
        else:
            all_files = raw_files

        total_files = len(all_files)
        logger.info(f"Total report files to evaluate: {total_files}")
        logger.info(f"Already processed files: {len(processed_set)} (skip_processed={self.skip_processed})")

        unprocessed = [f for f in all_files if f.name not in processed_set]
        if self.limit and len(unprocessed) > self.limit:
            unprocessed = unprocessed[:self.limit]
            
        logger.info(f"Files to process in this run: {len(unprocessed)}")

        if not unprocessed:
            print(f"\n{COLOR_GREEN}✅ All {total_files} files in {self.reports_dir} are already processed!{COLOR_RESET}\n")
            self.clear_status()
            return

        print("\n" + "=" * 80)
        print(f"{COLOR_BOLD}{COLOR_CYAN}🚀 STARTING BATCH PROCESSOR (Dual-Lens OpenRouter LLM){COLOR_RESET}")
        print(f"Reports Directory:    {self.reports_dir}")
        print(f"Results CSV:          {self.results_csv_path}")
        print(f"Processed State Log:  {self.processed_log_path}")
        print(f"Model:                {self.model}")
        print(f"API Key:              {'CONFIGURED (' + self.api_key[:6] + '...)' if self.api_key else 'NONE (Rule-based)'}")
        print(f"Live Web Check:       {'ENABLED' if self.enable_web_check else 'DISABLED'}")
        if self.limit:
            print(f"Batch Limit:          {self.limit}")
        print("=" * 80 + "\n")

        self.update_status(
            is_running=True,
            current_file="",
            current_ticker="",
            completed=len(processed_set),
            total=len(unprocessed),
        )

        verifier = None
        if self.enable_web_check:
            try:
                from screener.web_verifier import WebSearchVerifier
                verifier = WebSearchVerifier()
            except Exception as e:
                logger.warning(f"Could not initialize WebSearchVerifier: {e}")

        completed_count = 0
        try:
            for idx, file_path in enumerate(unprocessed, 1):
                filename = file_path.name

                ticker, year, quarter = parse_metadata_from_filename(filename)
                progress_prefix = f"[{idx} / {len(unprocessed)}]"

                self.update_status(
                    is_running=True,
                    current_file=filename,
                    current_ticker=ticker,
                    completed=completed_count,
                    total=len(unprocessed),
                )

                # 2. Extract Text
                text = extract_text_from_file(file_path)
                if not text:
                    logger.warning(f"{progress_prefix} ⚠️ Skipped {filename} (Empty or unreadable text).")
                    self.mark_file_processed(filename)
                    processed_set.add(filename)
                    completed_count += 1
                    continue

                # 3. Call LLM or Fallback
                parsed: Optional[Dict[str, Any]] = None
                if self.api_key:
                    parsed = call_llm_dual_lens(text=text, api_key=self.api_key, ticker=ticker, model=self.model)

                if not parsed:
                    parsed = rule_based_fallback_evaluation(text)

                # 4. Extract Verdict Details & Enforce Financial Safety Gatekeeper
                safety = parsed.get("financial_safety", {})
                flags = parsed.get("flags", {})
                metrics = parsed.get("metrics", {})

                going_concern = bool(safety.get("going_concern_risk", False))
                dilution_risk = bool(safety.get("dilution_risk_detected", flags.get("dilution_risk_detected", False)))
                unsustainable_cash_burn = bool(safety.get("unsustainable_cash_burn", flags.get("unsustainable_cash_burn", False)))
                erratic_pivots = bool(safety.get("erratic_pivots_detected", flags.get("erratic_pivots_detected", False)))
                safety_failed = going_concern or dilution_risk or unsustainable_cash_burn or erratic_pivots

                verdict_info = parsed.get("verdict_details", {})
                raw_profile = str(parsed.get("profile", verdict_info.get("matched_profile", "NONE"))).upper().strip()
                if "PROFILE_A" in raw_profile or "GROWTH" in raw_profile:
                    matched_profile = "GROWTH"
                elif "PROFILE_B" in raw_profile or "VALUE" in raw_profile:
                    matched_profile = "VALUE"
                else:
                    matched_profile = "NONE"

                verdict = str(parsed.get("verdict", verdict_info.get("verdict", "HOLD"))).upper().strip()
                reasoning = str(parsed.get("pedagogical_reasoning", verdict_info.get("reasoning", parsed.get("reasoning", "")))).strip()

                prof_b = parsed.get("profile_B_value", {})
                turnaround = bool(prof_b.get("turnaround_indicators", False))
                rev_shrinking = bool(prof_b.get("revenue_shrinking", flags.get("shrinking_business", False)))

                if rev_shrinking and not turnaround:
                    if matched_profile == "VALUE" and verdict == "STRONG BUY":
                        verdict = "HOLD"
                        matched_profile = "NONE"

                if safety_failed:
                    matched_profile = "NONE"
                    verdict = "REJECT"

                # Optional live web verification
                if verifier and verdict in ["STRONG BUY", "BUY", "HOLD"]:
                    try:
                        web_res = verifier.verify(ticker)
                        if not web_res.get("passed_web_check", True):
                            verdict = "REJECT"
                            reasoning += f" [Web Check Failed: {', '.join(web_res.get('red_flags_found', []))}]"
                        elif verdict in ["HOLD", "REJECT"] and web_res.get("turnaround_catalyst_detected", False):
                            verdict = "👀 WATCH_TURNAROUND"
                            reasoning += f" [Turnaround Catalyst: {', '.join(web_res.get('positive_catalysts_found', []))}]"
                    except Exception as e:
                        logger.debug(f"Web verification error for {ticker}: {e}")

                # 5. Append immediately to CSV & Mark Log
                self.append_result_to_csv(
                    filename=filename,
                    ticker=ticker,
                    year=year or "",
                    quarter=quarter or "",
                    matched_profile=matched_profile,
                    verdict=verdict,
                    reasoning=reasoning,
                )
                self.mark_file_processed(filename)
                processed_set.add(filename)
                completed_count += 1
                self.update_status(
                    is_running=True,
                    current_file=filename,
                    current_ticker=ticker,
                    completed=completed_count,
                    total=len(unprocessed),
                )

                # 6. Console UI Output
                if verdict == "STRONG BUY":
                    color = COLOR_GREEN
                    icon = "🔥 STRONG BUY"
                elif verdict == "HOLD":
                    color = COLOR_YELLOW
                    icon = "🟡 HOLD"
                else:
                    color = COLOR_RED
                    icon = "❌ REJECT"

                print(
                    f"{progress_prefix} {COLOR_BOLD}{ticker:<12}{COLOR_RESET} "
                    f"({year or 'N/A':<4} {quarter or 'N/A':<4}) -> "
                    f"{color}{icon:<14} [{matched_profile:<6}]{COLOR_RESET} | {reasoning[:70]}..."
                )

                # 7. Rate Limiting Sleep
                if self.api_key and self.throttle_seconds > 0:
                    time.sleep(self.throttle_seconds)

            print("\n" + "=" * 80)
            print(f"{COLOR_BOLD}{COLOR_GREEN}🎉 Batch Processing Complete! Consolidated results saved to {self.results_csv_path}{COLOR_RESET}")
            print("=" * 80 + "\n")
        finally:
            self.clear_status()


def main():
    parser = argparse.ArgumentParser(description="Batch Processor for Mass Historical Earnings Reports")
    parser.add_argument("--reports-dir", type=str, default=str(DEFAULT_REPORTS_DIR), help="Directory of reports")
    parser.add_argument("--log-file", type=str, default=str(DEFAULT_PROCESSED_LOG), help="Path to state log")
    parser.add_argument("--results-csv", type=str, default=str(DEFAULT_RESULTS_CSV), help="Path to results CSV")
    parser.add_argument("--model", type=str, default=DEFAULT_OPENROUTER_MODEL, help="OpenRouter model")
    parser.add_argument("--throttle", type=float, default=THROTTLE_DELAY_SECONDS, help="Delay between calls in seconds")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of files to process")
    parser.add_argument("--web-check", action="store_true", help="Enable Google News live sanity check")
    parser.add_argument("--reprocess-all", action="store_true", help="Reprocess all files without skipping previously processed ones")
    parser.add_argument("--all-historical", action="store_true", help="Process all historical quarters instead of only the single latest report per ticker")
    args = parser.parse_args()

    processor = BatchProcessor(
        reports_dir=Path(args.reports_dir),
        processed_log_path=Path(args.log_file),
        results_csv_path=Path(args.results_csv),
        model=args.model,
        throttle_seconds=args.throttle,
        limit=args.limit,
        enable_web_check=args.web_check,
        skip_processed=not args.reprocess_all,
        only_latest=not args.all_historical,
    )
    processor.run()


if __name__ == "__main__":
    main()
