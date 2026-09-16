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

# OpenRouter Constants
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3-8b-instruct:free")
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 60.0
THROTTLE_DELAY_SECONDS = 7.0

# Dual-Lens Core System Prompt
CORE_SYSTEM_PROMPT = """You are an expert quantitative equity analyst evaluating Nordic micro-cap companies from Earnings Reports and Annual Reports.
You must evaluate the company through TWO distinct investment lenses simultaneously:
- Profile A (High-Margin Growth / Tenbagger Compounder)
- Profile B (Deep Value / Turnaround Setup)

EVALUATION CRITERIA & RULES:

1. FINANCIAL SAFETY (Absolute Gatekeeper):
   - "going_concern_risk": Set to TRUE if there is evidence of severe insolvency, going concern uncertainty ('toiminnan jatkuvuus'), severe cash burn with < 12m runway, covenant breaches, or emergency dilutive bridge financing. If TRUE, the company MUST receive verdict: "REJECT" and matched_profile: "NONE".

2. PROFILE A (High-Margin Growth):
   - "gross_margin_over_40": Set to TRUE if Gross Margin ('myyntikate' / 'bruttokate') > 40.0%, or if high-margin software/scalable IP business with low COGS.
   - "rule_of_40_passed": Set to TRUE if (Revenue Growth YoY % + Operating Profit Margin / EBIT %) >= 40.0%.
   - "recurring_revenue_mentioned": Set to TRUE if SaaS, subscription models, ARR, recurring maintenance, or long-term contracts ('toistuva liikevaihto', 'jatkuvalaskutteinen').

3. PROFILE B (Deep Value / Turnaround):
   - "strong_net_cash_position": Set to TRUE if company has significant cash reserves and minimal/zero debt (net cash positive balance sheet).
   - "positive_operating_cash_flow": Set to TRUE if core operations generate positive cash flow ('liiketoiminnan rahavirta positiivinen').
   - "turnaround_indicators": Set to TRUE if significant cost cuts, restructuring taking effect, sequential margin expansion, or returning to profitability.

4. VERDICT DETAILS:
   - "matched_profile": "GROWTH" if meets Profile A criteria; "VALUE" if meets Profile B criteria; "NONE" if neither or fails safety.
   - "verdict": "STRONG BUY" if matched_profile is "GROWTH" or "VALUE" AND going_concern_risk is FALSE.
               "HOLD" if viable but incomplete criteria.
               "REJECT" if going_concern_risk is TRUE or fundamentally weak.
   - "reasoning": Concise 2-sentence summary explaining the logic and key drivers.

OUTPUT FORMAT:
Output strictly a valid JSON object with no markdown fences or preamble:
{
  "profile_A_growth": {
    "gross_margin_over_40": bool,
    "rule_of_40_passed": bool,
    "recurring_revenue_mentioned": bool
  },
  "profile_B_value": {
    "strong_net_cash_position": bool,
    "positive_operating_cash_flow": bool,
    "turnaround_indicators": bool
  },
  "financial_safety": {
    "going_concern_risk": bool
  },
  "verdict_details": {
    "matched_profile": "GROWTH" | "VALUE" | "NONE",
    "verdict": "STRONG BUY" | "HOLD" | "REJECT",
    "reasoning": string
  }
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
    model: str = DEFAULT_OPENROUTER_MODEL,
    max_retries: int = MAX_RETRIES,
    retry_backoff: float = RETRY_BACKOFF_SECONDS,
) -> Optional[Dict[str, Any]]:
    """
    Calls OpenRouter LLM API with dual-lens prompt, with retries on 429/502 errors.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/jju567/tradeBotTiuku",
        "X-Title": "tradeBotTiuku Batch Processor",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CORE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze this earnings report text:\n\n{text}"},
        ],
        "temperature": 0.1,
        "max_tokens": 700,
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                OPENROUTER_API_URL,
                headers=headers,
                json=payload,
                timeout=45.0,
            )

            # Rate limit or server error backoff
            if response.status_code == 429 or response.status_code == 502 or 500 <= response.status_code < 600:
                logger.warning(
                    f"⚠️ Received HTTP {response.status_code} ({response.reason}) on attempt {attempt}/{max_retries}. "
                    f"Rate limit / gateway breach. Sleeping {retry_backoff}s before retry..."
                )
                time.sleep(retry_backoff)
                continue

            response.raise_for_status()
            res_json = response.json()
            content = res_json.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = extract_json_response(content)
            if parsed:
                return parsed

        except Exception as e:
            logger.error(f"Error calling OpenRouter API (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                logger.warning(f"Sleeping {retry_backoff}s before retry...")
                time.sleep(retry_backoff)

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

    if going_concern:
        matched_profile = "NONE"
        verdict = "REJECT"
        reasoning = "Rejected due to solvency, going concern, or liquidity risk."
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
        api_key: Optional[str] = None,
        model: str = DEFAULT_OPENROUTER_MODEL,
        throttle_seconds: float = THROTTLE_DELAY_SECONDS,
    ):
        self.reports_dir = Path(reports_dir)
        self.processed_log_path = Path(processed_log_path)
        self.results_csv_path = Path(results_csv_path)
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        self.model = model
        self.throttle_seconds = throttle_seconds

        # Ensure directories exist
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.processed_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.results_csv_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize CSV header if file doesn't exist or is empty
        self.init_csv_headers()

    def init_csv_headers(self) -> None:
        """Ensures results CSV has correct headers."""
        if not self.results_csv_path.exists() or self.results_csv_path.stat().st_size == 0:
            with open(self.results_csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Filename", "Ticker", "Year", "Quarter", "Matched_Profile", "Verdict", "Reasoning"])

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
    ) -> None:
        """Appends an individual evaluation row to CSV."""
        with open(self.results_csv_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([filename, ticker, year, quarter, matched_profile, verdict, reasoning])

    def run(self) -> None:
        """Main execution loop over report files."""
        processed_set = self.load_processed_files()
        all_files = sorted(
            [f for f in self.reports_dir.iterdir() if f.is_file() and f.suffix.lower() in (".pdf", ".txt")]
        )
        total_files = len(all_files)

        logger.info(f"Total report files found: {total_files}")
        logger.info(f"Already processed files: {len(processed_set)}")

        unprocessed = [f for f in all_files if f.name not in processed_set]
        logger.info(f"Files to process in this run: {len(unprocessed)}")

        if not unprocessed:
            print(f"\n{COLOR_GREEN}✅ All {total_files} files in {self.reports_dir} are already processed!{COLOR_RESET}\n")
            return

        print("\n" + "=" * 80)
        print(f"{COLOR_BOLD}{COLOR_CYAN}🚀 STARTING BATCH PROCESSOR (Dual-Lens OpenRouter LLM){COLOR_RESET}")
        print(f"Reports Directory:    {self.reports_dir}")
        print(f"Results CSV:          {self.results_csv_path}")
        print(f"Processed State Log:  {self.processed_log_path}")
        print(f"Model:                {self.model}")
        print(f"API Key:              {'CONFIGURED (' + self.api_key[:6] + '...)' if self.api_key else 'NONE (Rule-based)'}")
        print("=" * 80 + "\n")

        for idx, file_path in enumerate(all_files, 1):
            filename = file_path.name

            # 1. Check statefulness
            if filename in processed_set:
                continue

            ticker, year, quarter = parse_metadata_from_filename(filename)
            progress_prefix = f"[{idx} / {total_files}]"

            # 2. Extract Text
            text = extract_text_from_file(file_path)
            if not text:
                logger.warning(f"{progress_prefix} ⚠️ Skipped {filename} (Empty or unreadable text).")
                self.mark_file_processed(filename)
                processed_set.add(filename)
                continue

            # 3. Call LLM or Fallback
            parsed: Optional[Dict[str, Any]] = None
            if self.api_key:
                parsed = call_llm_dual_lens(text=text, api_key=self.api_key, model=self.model)

            if not parsed:
                parsed = rule_based_fallback_evaluation(text)

            # 4. Extract Verdict Details
            verdict_info = parsed.get("verdict_details", {})
            matched_profile = str(verdict_info.get("matched_profile", "NONE")).upper().strip()
            verdict = str(verdict_info.get("verdict", "HOLD")).upper().strip()
            reasoning = str(verdict_info.get("reasoning", "")).strip()

            # 5. Append immediately to CSV & Mark Log
            self.append_result_to_csv(
                filename=filename,
                ticker=ticker,
                year=year,
                quarter=quarter,
                matched_profile=matched_profile,
                verdict=verdict,
                reasoning=reasoning,
            )
            self.mark_file_processed(filename)
            processed_set.add(filename)

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


def main():
    parser = argparse.ArgumentParser(description="Batch Processor for Mass Historical Earnings Reports")
    parser.add_argument("--reports-dir", type=str, default=str(DEFAULT_REPORTS_DIR), help="Directory of reports")
    parser.add_argument("--log-file", type=str, default=str(DEFAULT_PROCESSED_LOG), help="Path to state log")
    parser.add_argument("--results-csv", type=str, default=str(DEFAULT_RESULTS_CSV), help="Path to results CSV")
    parser.add_argument("--model", type=str, default=DEFAULT_OPENROUTER_MODEL, help="OpenRouter model")
    parser.add_argument("--throttle", type=float, default=THROTTLE_DELAY_SECONDS, help="Delay between calls in seconds")
    args = parser.parse_args()

    processor = BatchProcessor(
        reports_dir=Path(args.reports_dir),
        processed_log_path=Path(args.log_file),
        results_csv_path=Path(args.results_csv),
        model=args.model,
        throttle_seconds=args.throttle,
    )
    processor.run()


if __name__ == "__main__":
    main()
