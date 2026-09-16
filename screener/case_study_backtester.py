"""
Case Study Backtester for Micro-Cap Core Fundamental Pipeline (Tenbagger Hunting).

Evaluates historical fundamental quality of known winners and losers from local PDF earnings reports:
1. Reads PDF files from `data/historical_reports/`.
2. Extracts text using `pdfplumber` (falling back to `pypdf`/`PyPDF2`), focusing on executive summaries
   and financial tables (first 5-10 pages).
3. Calls OpenRouter LLM API with the CORE Fundamental Prompt to evaluate:
   - gross_margin_over_40 (bool)
   - rule_of_40_passed (bool) (Revenue Growth % + Profit Margin % >= 40)
   - recurring_revenue_mentioned (bool)
   - cash_or_debt_issues (bool)
   - verdict ("STRONG BUY" | "HOLD" | "REJECT")
   - reasoning (concise 2-sentence summary)
4. Prints formatted console reports and persists consolidated results to `data/case_study_results.csv`.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from dotenv import load_dotenv

import requests

# Load environment variables
load_dotenv()

# Reconfigure stdout/stderr for cross-platform encoding compatibility (Windows cp1252 fix)
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
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("screener.case_study_backtester")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPORTS_DIR = BASE_DIR / "data" / "historical_reports"
DEFAULT_RESULTS_CSV = BASE_DIR / "data" / "case_study_results.csv"

# OpenRouter Constants
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3-8b-instruct:free")
MAX_RETRIES = 3
RETRY_BACKOFF_DELAYS = [5.0, 10.0, 20.0]

# Specialized Core Case Study System Prompt
CASE_STUDY_CORE_SYSTEM_PROMPT = """You are an expert quantitative equity analyst evaluating Nordic micro-cap companies for long-term "Core Tenbagger" potential based on historical earnings reports.
Analyze the provided report text and financial statements and evaluate fundamental quality according to these rules:

1. "cash_or_debt_issues": Set to TRUE if there is evidence of negative equity, going concern uncertainty ('toiminnan jatkuvuus'), severe liquidity shortage (< 12m runway), covenant breaches, or emergency dilutive loans.
2. "gross_margin_over_40": Set to TRUE if Gross Margin ('myyntikate' / 'bruttokate') > 40.0%, or if the company is a high-margin software/scalable IP business with low COGS.
3. "recurring_revenue_mentioned": Set to TRUE if the company mentions SaaS, subscription models, ARR, recurring maintenance, or long-term software licensing ('toistuva liikevaihto', 'jatkuvalaskutteinen'). Set to FALSE if purely one-off project delivery / construction.
4. "rule_of_40_passed": Set to TRUE if (Revenue Growth YoY % + Operating Profit Margin / EBIT %) >= 40.0%.
5. "verdict":
   - "REJECT": If "cash_or_debt_issues" is TRUE, OR if both growth and margins are severely negative / low quality.
   - "STRONG BUY": If "cash_or_debt_issues" is FALSE, "gross_margin_over_40" is TRUE, "recurring_revenue_mentioned" is TRUE, and "rule_of_40_passed" is TRUE.
   - "HOLD": If viable business without critical cash issues, but fails one or more Rule of 40 / gross margin / recurring revenue criteria.
6. "reasoning": Provide a concise 2-sentence summary explaining the decision and key drivers/risks in Finnish or English.

OUTPUT FORMAT:
Output strictly a valid JSON object with no markdown fences or preamble:
{
  "gross_margin_over_40": bool,
  "rule_of_40_passed": bool,
  "recurring_revenue_mentioned": bool,
  "cash_or_debt_issues": bool,
  "verdict": "STRONG BUY" | "HOLD" | "REJECT",
  "reasoning": string
}"""


@dataclass
class CaseStudyResult:
    filename: str
    company_name: str
    report_period: str
    gross_margin_over_40: bool
    rule_of_40_passed: bool
    recurring_revenue_mentioned: bool
    cash_or_debt_issues: bool
    verdict: str
    reasoning: str


class CaseStudyBacktester:
    """Manages parsing, LLM evaluation, and reporting for historical case studies."""

    def __init__(
        self,
        reports_dir: Optional[Path | str] = None,
        results_csv_path: Optional[Path | str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_pages: int = 10,
        max_words_context: int = 3500,
    ):
        self.reports_dir = Path(reports_dir or DEFAULT_REPORTS_DIR)
        self.results_csv_path = Path(results_csv_path or DEFAULT_RESULTS_CSV)
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.model = model or DEFAULT_OPENROUTER_MODEL
        self.max_pages = max_pages
        self.max_words_context = max_words_context

        # Ensure directories exist
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.results_csv_path.parent.mkdir(parents=True, exist_ok=True)

    def extract_text_from_pdf(self, pdf_path: Path) -> Tuple[str, str]:
        """
        Extracts text from the first N pages of a PDF file using pdfplumber or pypdf/PyPDF2.
        Returns: (extracted_text, extraction_backend)
        """
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            logger.warning(f"PDF file {pdf_path.name} is missing or empty.")
            return "", "None"

        # 1. Try pdfplumber
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(pdf_path) as pdf:
                num_pages = len(pdf.pages)
                pages_to_extract = min(num_pages, self.max_pages)
                page_texts = []
                for idx in range(pages_to_extract):
                    text = pdf.pages[idx].extract_text() or ""
                    if text.strip():
                        page_texts.append(f"--- Page {idx + 1} ---\n{text.strip()}")
                if page_texts:
                    return "\n\n".join(page_texts), f"pdfplumber ({pages_to_extract}/{num_pages} pages)"
        except Exception as e:
            logger.debug(f"pdfplumber extraction failed on {pdf_path.name}: {e}. Trying pypdf/PyPDF2...")

        # 2. Try pypdf / PyPDF2 fallback
        for mod_name in ("pypdf", "PyPDF2"):
            try:
                mod = __import__(mod_name)
                reader = mod.PdfReader(str(pdf_path))
                num_pages = len(reader.pages)
                pages_to_extract = min(num_pages, self.max_pages)
                page_texts = []
                for idx in range(pages_to_extract):
                    text = reader.pages[idx].extract_text() or ""
                    if text.strip():
                        page_texts.append(f"--- Page {idx + 1} ---\n{text.strip()}")
                if page_texts:
                    return "\n\n".join(page_texts), f"{mod_name} ({pages_to_extract}/{num_pages} pages)"
            except Exception as e:
                logger.debug(f"{mod_name} extraction failed on {pdf_path.name}: {e}")

        logger.error(f"Failed to extract text from PDF: {pdf_path.name}")
        return "", "None"

    def clean_and_chunk_text(self, text: str) -> str:
        """Limits extracted text to context window max words."""
        if not text:
            return ""
        words = text.split()
        if len(words) <= self.max_words_context:
            return text
        return " ".join(words[:self.max_words_context])

    def parse_company_and_period_from_filename(self, filename: str) -> Tuple[str, str]:
        """Infers company name and report period from filename like QT_Group_Q3_2020.pdf."""
        stem = Path(filename).stem
        parts = stem.replace("-", "_").split("_")
        
        # Check for period patterns like Q1_2022, H1_2021, 2020, etc.
        period_parts = []
        company_parts = []
        
        for part in parts:
            if re.match(r"^(?:q[1-4]|h[1-2]|20\d\d|fy\d\d)$", part, re.IGNORECASE):
                period_parts.append(part.upper())
            else:
                company_parts.append(part)

        company = " ".join(company_parts) if company_parts else stem
        period = " ".join(period_parts) if period_parts else "N/A"
        return company, period

    def call_llm_core_analysis(self, text: str, filename: str) -> Dict[str, Any]:
        """Calls OpenRouter LLM API with the CORE prompt or uses rule-based fallback."""
        if not self.api_key:
            logger.warning(f"No OPENROUTER_API_KEY set. Using deterministic rule-based analysis for {filename}.")
            return self.rule_based_core_analysis(text)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": CASE_STUDY_CORE_SYSTEM_PROMPT},
                {"role": "user", "content": f"Earnings Report File: {filename}\n\nReport Content:\n{text}"},
            ],
            "temperature": 0.0,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key.strip()}",
            "HTTP-Referer": "https://github.com/tradeBotTiuku/screener",
            "X-Title": "Nasdaq Helsinki MicroCap Case Study Backtester",
            "Content-Type": "application/json",
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    OPENROUTER_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=45.0,
                )

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    backoff = RETRY_BACKOFF_DELAYS[attempt - 1] if attempt - 1 < len(RETRY_BACKOFF_DELAYS) else 20.0
                    logger.warning(f"Rate limited or server error ({response.status_code}). Backing off {backoff}s...")
                    time.sleep(backoff)
                    continue

                response.raise_for_status()
                res_json = response.json()
                content = res_json.get("choices", [{}])[0].get("message", {}).get("content", "")

                # Parse JSON
                parsed = self.extract_json_response(content)
                if parsed:
                    return self.validate_and_normalize_output(parsed)

            except Exception as e:
                logger.error(f"Error calling LLM (attempt {attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(5.0)

        logger.warning(f"LLM API calls failed for {filename}. Using rule-based fallback.")
        return self.rule_based_core_analysis(text)

    def extract_json_response(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """Safely extracts JSON object from raw response string."""
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

    def validate_and_normalize_output(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes and ensures types for evaluation output."""
        gm_over_40 = bool(data.get("gross_margin_over_40", False))
        r40_passed = bool(data.get("rule_of_40_passed", False))
        recurring = bool(data.get("recurring_revenue_mentioned", False))
        cash_issues = bool(data.get("cash_or_debt_issues", False))
        verdict = str(data.get("verdict", "")).strip().upper()
        
        if verdict not in ("STRONG BUY", "HOLD", "REJECT"):
            if cash_issues:
                verdict = "REJECT"
            elif gm_over_40 and r40_passed and recurring and not cash_issues:
                verdict = "STRONG BUY"
            else:
                verdict = "HOLD"

        reasoning = str(data.get("reasoning", "")).strip()
        if not reasoning:
            reasoning = f"Verdict {verdict}: GM>40%={gm_over_40}, Rule40={r40_passed}, Recurring={recurring}, CashIssues={cash_issues}."

        return {
            "gross_margin_over_40": gm_over_40,
            "rule_of_40_passed": r40_passed,
            "recurring_revenue_mentioned": recurring,
            "cash_or_debt_issues": cash_issues,
            "verdict": verdict,
            "reasoning": reasoning,
        }

    def rule_based_core_analysis(self, text: str) -> Dict[str, Any]:
        """Deterministic keyword-based analysis fallback."""
        lower = text.lower()
        
        # Cash/Debt Issues
        cash_issues = any(
            re.search(pat, lower) for pat in [
                r"käyttöpääoma ei riitä", r"toiminnan jatkuvuus", r"going concern",
                r"negatiivinen oma pääoma", r"maksuvalmius on heikentynyt",
                r"kovenanttirikko", r"covenant breach", r"tarvitsee lisärahoitusta"
            ]
        )

        # Recurring Revenue
        recurring = any(
            k in lower for k in [
                "saas", "toistuva liikevaihto", "jatkuvalaskutteinen", "recurring revenue",
                "arr", "tilauspohjainen", "subscription", "ylläpitotuotot", "maintenance revenue"
            ]
        )

        # Gross margin > 40%
        gm_over_40 = any(
            k in lower for k in [
                "ohjelmisto", "software", "lisenssit", "license", "myyntikate 5", "myyntikate 6",
                "myyntikate 7", "myyntikate 8", "myyntikate 9", "bruttokate 5", "bruttokate 6"
            ]
        )

        # Rule of 40 (growth + margin)
        has_growth = bool(re.search(r"kasvoi\s+(?:[3-9]\d|1\d\d)\s*%", lower) or "vahva kasvu" in lower)
        has_margin = bool(re.search(r"liikevoittomarginaali\s+(?:1[5-9]|[2-9]\d)\s*%", lower) or "kannattavuus parani" in lower)
        r40_passed = has_growth and (has_margin or recurring)

        if cash_issues:
            verdict = "REJECT"
            reasoning = "Yhtiö hylättiin vakavien maksuvalmius-, velkaantumis- tai käyttöpääomariskien vuoksi."
        elif gm_over_40 and recurring and r40_passed and not cash_issues:
            verdict = "STRONG BUY"
            reasoning = "Vahva kymmenkertaistajaehdokas: korkea bruttokate, toistuva skaalautuva liikevaihto ja vahva Rule of 40 kasvu."
        else:
            verdict = "HOLD"
            reasoning = "Liiketoiminta on kohtuullista ilman akuuttia kassakriisiä, mutta ei täytä kaikkia Core Tenbagger -kriteerejä."

        return {
            "gross_margin_over_40": gm_over_40,
            "rule_of_40_passed": r40_passed,
            "recurring_revenue_mentioned": recurring,
            "cash_or_debt_issues": cash_issues,
            "verdict": verdict,
            "reasoning": reasoning,
        }

    def run_all(self) -> List[CaseStudyResult]:
        """Runs the case study pipeline over all PDFs in `reports_dir`."""
        logger.info(f"Scanning for PDF reports in {self.reports_dir}...")
        pdf_files = sorted(list(self.reports_dir.glob("*.pdf")))

        if not pdf_files:
            logger.warning(
                f"No PDF files found in {self.reports_dir}!\n"
                f"Place historical earnings reports (e.g. QT_Group_Q3_2020.pdf, Lehto_Q2_2022.pdf) in that directory and run again."
            )
            return []

        logger.info(f"Found {len(pdf_files)} PDF report(s) to analyze.")
        results: List[CaseStudyResult] = []

        for idx, pdf_path in enumerate(pdf_files, 1):
            filename = pdf_path.name
            company, period = self.parse_company_and_period_from_filename(filename)
            logger.info(f"\n[{idx}/{len(pdf_files)}] Processing: {filename} ({company} | {period})...")

            raw_text, backend = self.extract_text_from_pdf(pdf_path)
            if not raw_text:
                logger.error(f"Skipping {filename} due to empty text extraction.")
                continue

            cleaned_text = self.clean_and_chunk_text(raw_text)
            logger.info(f"Extracted {len(cleaned_text.split())} words using {backend}. Sending to LLM...")

            eval_res = self.call_llm_core_analysis(cleaned_text, filename)

            res_obj = CaseStudyResult(
                filename=filename,
                company_name=company,
                report_period=period,
                gross_margin_over_40=eval_res["gross_margin_over_40"],
                rule_of_40_passed=eval_res["rule_of_40_passed"],
                recurring_revenue_mentioned=eval_res["recurring_revenue_mentioned"],
                cash_or_debt_issues=eval_res["cash_or_debt_issues"],
                verdict=eval_res["verdict"],
                reasoning=eval_res["reasoning"],
            )
            results.append(res_obj)

            # Print individual report summary
            self.print_report_result(res_obj)

        # Save consolidated results to CSV
        self.save_results_csv(results)
        self.print_final_summary_table(results)

        return results

    def print_report_result(self, res: CaseStudyResult) -> None:
        """Prints a clean, formatted console report for an individual evaluated report."""
        verdict_icon = "🟢" if res.verdict == "STRONG BUY" else ("🟡" if res.verdict == "HOLD" else "🔴")
        print("\n" + "=" * 75)
        print(f"📄 CASE STUDY: {res.company_name} ({res.report_period}) — {res.filename}")
        print("=" * 75)
        print(f"VERDICT:                     {verdict_icon} {res.verdict}")
        print(f"1. Gross Margin > 40%:       {'✅ YES' if res.gross_margin_over_40 else '❌ NO'}")
        print(f"2. Rule of 40 Passed:        {'✅ YES' if res.rule_of_40_passed else '❌ NO'}")
        print(f"3. Recurring Revenue:        {'✅ YES' if res.recurring_revenue_mentioned else '❌ NO'}")
        print(f"4. Cash / Debt Issues:       {'⚠️ YES (DISTRESS)' if res.cash_or_debt_issues else '🛡️ NO (HEALTHY)'}")
        print(f"Reasoning / Assessment:")
        print(f"  👉 {res.reasoning}")
        print("=" * 75 + "\n")

    def print_final_summary_table(self, results: List[CaseStudyResult]) -> None:
        """Prints summary table of all case study runs."""
        if not results:
            return
        print("\n" + "*" * 80)
        print("                    CASE STUDY BACKTESTER SUMMARY TABLE                    ")
        print("*" * 80)
        print(f"{'Company':<18} | {'Period':<10} | {'Verdict':<12} | {'GM>40%':<7} | {'R40':<5} | {'Recur':<6} | {'DebtRisk':<8}")
        print("-" * 80)
        for r in results:
            verdict_str = f"[{r.verdict}]"
            gm_str = "YES" if r.gross_margin_over_40 else "NO"
            r40_str = "YES" if r.rule_of_40_passed else "NO"
            rec_str = "YES" if r.recurring_revenue_mentioned else "NO"
            debt_str = "RISK" if r.cash_or_debt_issues else "OK"
            print(f"{r.company_name[:18]:<18} | {r.report_period[:10]:<10} | {verdict_str:<12} | {gm_str:<7} | {r40_str:<5} | {rec_str:<6} | {debt_str:<8}")
        print("*" * 80)
        print(f"Results saved to: {self.results_csv_path}\n")

    def save_results_csv(self, results: List[CaseStudyResult]) -> None:
        """Saves consolidated results to CSV."""
        if not results:
            return
        fieldnames = [
            "filename",
            "company_name",
            "report_period",
            "gross_margin_over_40",
            "rule_of_40_passed",
            "recurring_revenue_mentioned",
            "cash_or_debt_issues",
            "verdict",
            "reasoning",
        ]
        try:
            with open(self.results_csv_path, mode="w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for r in results:
                    writer.writerow(asdict(r))
            logger.info(f"Successfully saved {len(results)} case study result(s) to {self.results_csv_path}")
        except Exception as e:
            logger.error(f"Failed to save CSV to {self.results_csv_path}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Micro-Cap Core Tenbagger Case Study Backtester")
    parser.add_argument("--reports-dir", type=str, default=str(DEFAULT_REPORTS_DIR), help="Directory containing PDF reports")
    parser.add_argument("--results-csv", type=str, default=str(DEFAULT_RESULTS_CSV), help="Output CSV path")
    parser.add_argument("--model", type=str, default=DEFAULT_OPENROUTER_MODEL, help="OpenRouter model identifier")
    parser.add_argument("--max-pages", type=int, default=10, help="Max PDF pages to parse (default: 10)")
    args = parser.parse_args()

    backtester = CaseStudyBacktester(
        reports_dir=args.reports_dir,
        results_csv_path=args.results_csv,
        model=args.model,
        max_pages=args.max_pages,
    )
    backtester.run_all()


if __name__ == "__main__":
    main()
