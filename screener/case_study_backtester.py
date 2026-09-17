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
CASE_STUDY_CORE_SYSTEM_PROMPT = """You are an expert quantitative equity analyst evaluating Nordic and US micro-cap companies for long-term "Core Tenbagger" potential based on historical earnings reports.
Evaluate the company simultaneously through two distinct investment lenses:
- Profile A: "High-Margin Growth"
- Profile B: "Deep Value / Turnaround"

EVALUATION CRITERIA & RULES:

1. FINANCIAL SAFETY (Absolute Mandatory Gatekeeper - Instant REJECT if ANY are True):
   - "going_concern_risk": Set to TRUE if there is evidence of negative equity, going concern uncertainty ('toiminnan jatkuvuus'), severe liquidity shortage (< 12m runway), covenant breaches, or emergency dilutive bridge financing.
   - "dilution_risk_detected": Set to TRUE if there are mentions of "reverse stock split" / share consolidation ('käänteinen split', 'osakkeiden yhdistäminen'), continuous massive dilutive share issuances, death-spiral convertible notes, or toxic equity lines destroying shareholder value.
   - "unsustainable_cash_burn": Set to TRUE if the company has negative operating cash flow and its existing cash balance will be entirely consumed within less than 4 quarters (12 months runway) without near-term breakeven or non-dilutive financing.
   - "erratic_pivots_detected": Set to TRUE if the company has opportunistically changed its core business model across unrelated industries (e.g., from beverages/skincare to crypto/blockchain/AI/mining/drones/shells).
   * MANDATORY ENFORCEMENT: If ANY of going_concern_risk, dilution_risk_detected, unsustainable_cash_burn, or erratic_pivots_detected is TRUE, verdict MUST be "REJECT" and matched_profile MUST be "NONE".

2. PROFILE A (High-Margin Growth):
   - "gross_margin_over_40": Set to TRUE if Gross Margin ('myyntikate' / 'bruttokate') > 40.0%, or if high-margin software/scalable IP business with low COGS.
   - "rule_of_40_passed": Set to TRUE if (Revenue Growth YoY % + Operating Profit Margin / EBIT %) >= 40.0%.
   - "recurring_revenue_mentioned": Set to TRUE if SaaS, subscription models, ARR, recurring maintenance, or long-term software licensing ('toistuva liikevaihto', 'jatkuvalaskutteinen').
   - "organic_growth_confirmed": Set to TRUE if revenue growth is driven by core organic product/customer traction rather than artificial shell acquisitions or one-time accounting gains.

3. PROFILE B (Deep Value / Turnaround):
   - "strong_net_cash_position": Set to TRUE if company has significant cash reserves and minimal/zero debt (net cash positive balance sheet).
   - "positive_operating_cash_flow": Set to TRUE if core business generates positive cash flow ('liiketoiminnan rahavirta positiivinen').
   - "turnaround_indicators": Set to TRUE if significant cost cuts, restructuring taking effect, sequential margin expansion, or returning to profitability.

4. VERDICT DETAILS:
   - "matched_profile": "GROWTH" if meets Profile A; "VALUE" if meets Profile B; "NONE" if neither or fails financial safety.
   - "verdict": "STRONG BUY" if matched_profile is "GROWTH" or "VALUE" and all financial_safety flags are FALSE.
               "HOLD" if viable but incomplete criteria.
               "REJECT" if ANY financial_safety flag is TRUE or weak fundamentals.
   - "reasoning": Concise 2-sentence summary explaining the decision and key drivers/risks in Finnish or English.

OUTPUT FORMAT:
Output strictly a valid JSON object with no markdown fences or preamble:
{
  "profile_A_growth": {
    "gross_margin_over_40": bool,
    "rule_of_40_passed": bool,
    "recurring_revenue_mentioned": bool,
    "organic_growth_confirmed": bool
  },
  "profile_B_value": {
    "strong_net_cash_position": bool,
    "positive_operating_cash_flow": bool,
    "turnaround_indicators": bool
  },
  "financial_safety": {
    "going_concern_risk": bool,
    "dilution_risk_detected": bool,
    "unsustainable_cash_burn": bool,
    "erratic_pivots_detected": bool
  },
  "verdict_details": {
    "matched_profile": "GROWTH" | "VALUE" | "NONE",
    "verdict": "STRONG BUY" | "HOLD" | "REJECT",
    "reasoning": string
  }
}"""


@dataclass
class CaseStudyResult:
    filename: str
    company_name: str
    report_period: str
    matched_profile: str
    verdict: str
    gross_margin_over_40: bool
    rule_of_40_passed: bool
    recurring_revenue_mentioned: bool
    strong_net_cash_position: bool
    positive_operating_cash_flow: bool
    turnaround_indicators: bool
    going_concern_risk: bool
    reasoning: str
    dilution_risk_detected: bool = False
    unsustainable_cash_burn: bool = False
    erratic_pivots_detected: bool = False
    organic_growth_confirmed: bool = True
    # Backwards compatibility alias
    cash_or_debt_issues: bool = False


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

    def extract_text_from_file(self, file_path: Path) -> Tuple[str, str]:
        """
        Extracts text from a report file (.pdf or .txt).
        Returns: (extracted_text, extraction_backend)
        """
        if not file_path.exists() or file_path.stat().st_size == 0:
            logger.warning(f"File {file_path.name} is missing or empty.")
            return "", "None"

        # 1. Plain text file (HTML fallback extraction)
        if file_path.suffix.lower() == ".txt":
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                return content.strip(), "HTML-TXT"
            except Exception as e:
                logger.error(f"Failed to read TXT file {file_path.name}: {e}")
                return "", "None"

        # 2. PDF file (try pdfplumber, then pypdf/PyPDF2)
        return self.extract_text_from_pdf(file_path)

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
        """Normalizes and ensures types for dual-lens evaluation output."""
        prof_a = data.get("profile_A_growth", {})
        prof_b = data.get("profile_B_value", {})
        safety = data.get("financial_safety", {})
        verdict_info = data.get("verdict_details", {})

        gm_over_40 = bool(prof_a.get("gross_margin_over_40", data.get("gross_margin_over_40", False)))
        r40_passed = bool(prof_a.get("rule_of_40_passed", data.get("rule_of_40_passed", False)))
        recurring = bool(prof_a.get("recurring_revenue_mentioned", data.get("recurring_revenue_mentioned", False)))
        organic_growth = bool(prof_a.get("organic_growth_confirmed", True))

        net_cash = bool(prof_b.get("strong_net_cash_position", False))
        pos_ocf = bool(prof_b.get("positive_operating_cash_flow", False))
        turnaround = bool(prof_b.get("turnaround_indicators", False))

        going_concern = bool(safety.get("going_concern_risk", data.get("cash_or_debt_issues", False)))
        dilution_risk = bool(safety.get("dilution_risk_detected", False))
        unsustainable_cash_burn = bool(safety.get("unsustainable_cash_burn", False))
        erratic_pivots = bool(safety.get("erratic_pivots_detected", False))

        safety_failed = going_concern or dilution_risk or unsustainable_cash_burn or erratic_pivots

        matched_profile = str(verdict_info.get("matched_profile", data.get("matched_profile", ""))).strip().upper()
        verdict = str(verdict_info.get("verdict", data.get("verdict", ""))).strip().upper()

        if safety_failed:
            matched_profile = "NONE"
            verdict = "REJECT"
        elif matched_profile not in ("GROWTH", "VALUE", "NONE"):
            if gm_over_40 and r40_passed and recurring and organic_growth:
                matched_profile = "GROWTH"
                verdict = "STRONG BUY"
            elif net_cash and pos_ocf and turnaround:
                matched_profile = "VALUE"
                verdict = "STRONG BUY"
            else:
                matched_profile = "NONE"
                verdict = "HOLD"

        if safety_failed:
            verdict = "REJECT"
        elif verdict not in ("STRONG BUY", "HOLD", "REJECT"):
            verdict = "STRONG BUY" if matched_profile in ("GROWTH", "VALUE") else "HOLD"

        reasoning = str(verdict_info.get("reasoning", data.get("reasoning", ""))).strip()
        if not reasoning:
            reasoning = f"Verdict {verdict} ({matched_profile}): GM>40%={gm_over_40}, R40={r40_passed}, Recur={recurring}, NetCash={net_cash}, PosOCF={pos_ocf}, Turnaround={turnaround}, Risk={safety_failed}."

        return {
            "matched_profile": matched_profile,
            "verdict": verdict,
            "gross_margin_over_40": gm_over_40,
            "rule_of_40_passed": r40_passed,
            "recurring_revenue_mentioned": recurring,
            "organic_growth_confirmed": organic_growth,
            "strong_net_cash_position": net_cash,
            "positive_operating_cash_flow": pos_ocf,
            "turnaround_indicators": turnaround,
            "going_concern_risk": going_concern,
            "dilution_risk_detected": dilution_risk,
            "unsustainable_cash_burn": unsustainable_cash_burn,
            "erratic_pivots_detected": erratic_pivots,
            "cash_or_debt_issues": safety_failed,
            "reasoning": reasoning,
        }

    def rule_based_core_analysis(self, text: str) -> Dict[str, Any]:
        """Deterministic keyword-based analysis fallback supporting dual lenses."""
        lower = text.lower()
        
        # Financial Safety / Going concern
        going_concern = any(
            re.search(pat, lower) for pat in [
                r"käyttöpääoma ei riitä", r"toiminnan jatkuvuus", r"going concern",
                r"negatiivinen oma pääoma", r"maksuvalmius on heikentynyt",
                r"kovenanttirikko", r"covenant breach", r"tarvitsee lisärahoitusta"
            ]
        )

        dilution_risk = any(
            re.search(pat, lower) for pat in [
                r"reverse stock split", r"reverse split", r"share consolidation",
                r"osakkeiden yhdistäminen", r"käänteinen split", r"continuous dilution",
                r"death spiral",
            ]
        )

        unsustainable_cash_burn = any(
            re.search(pat, lower) for pat in [
                r"kassavarat eivät riitä 12", r"cash runway (?:of )?less than",
                r"runway is less than 12", r"depleted within 12 months",
                r"depleted within 4 quarters",
            ]
        )

        erratic_pivots = any(
            re.search(pat, lower) for pat in [
                r"pivot to crypto", r"pivot to ai", r"pivot to blockchain",
                r"pivot to bitcoin", r"changed business model to crypto",
                r"reverse merger with",
            ]
        )

        safety_failed = going_concern or dilution_risk or unsustainable_cash_burn or erratic_pivots

        # Profile A (Growth)
        recurring = any(
            k in lower for k in [
                "saas", "toistuva liikevaihto", "jatkuvalaskutteinen", "recurring revenue",
                "arr", "tilauspohjainen", "subscription", "ylläpitotuotot", "maintenance revenue"
            ]
        )

        gm_over_40 = any(
            k in lower for k in [
                "ohjelmisto", "software", "lisenssit", "license", "myyntikate 5", "myyntikate 6",
                "myyntikate 7", "myyntikate 8", "myyntikate 9", "bruttokate 5", "bruttokate 6",
                "myyntikate 85", "myyntikate 80", "myyntikate 70", "myyntikate 60", "myyntikate 50"
            ]
        )

        has_growth = bool(re.search(r"kasvoi\s+(?:[2-9]\d|1\d\d)\s*%", lower) or "vahva kasvu" in lower)
        has_margin = bool(re.search(r"liikevoittomarginaali\s+(?:1[5-9]|[2-9]\d)\s*%", lower) or "kannattavuus parani" in lower)
        r40_passed = has_growth and (has_margin or recurring)

        # Profile B (Value / Turnaround)
        net_cash = any(
            k in lower for k in [
                "velaton", "nettovelaton", "net cash", "vahva kassa", "vahva tase",
                "kassavarat olivat", "kassavarat ylittävät"
            ]
        )
        pos_ocf = any(
            k in lower for k in [
                "positiivinen rahavirta", "positive cash flow", "liiketoiminnan rahavirta oli positiivinen",
                "rahavirta parani"
            ]
        )
        turnaround = any(
            k in lower for k in [
                "käänne", "tuloskäänne", "tehostamisohjelma", "säästöohjelma", "kannattavuus parani",
                "turnaround", "restructuring"
            ]
        )

        if safety_failed:
            matched_profile = "NONE"
            verdict = "REJECT"
            reasoning = "Yhtiö hylättiin vakavien maksuvalmius-, diluutio-, kassapoltto- tai toiminnan jatkuvuuden riskien vuoksi."
        elif gm_over_40 and recurring and r40_passed:
            matched_profile = "GROWTH"
            verdict = "STRONG BUY"
            reasoning = "Vahva Kasvu-profiilin kymmenkertaistaja: korkea bruttokate, toistuva SaaS-liikevaihto ja Rule of 40 kasvu."
        elif net_cash and (pos_ocf or turnaround):
            matched_profile = "VALUE"
            verdict = "STRONG BUY"
            reasoning = "Vahva Arvo/Käänne-profiilin ehdokas: vahva nettovelaton kassapuskuri ja positiivinen operatiivinen rahavirta."
        else:
            matched_profile = "NONE"
            verdict = "HOLD"
            reasoning = "Liiketoiminta on kohtuullista ilman akuuttia kassakriisiä, mutta ei täytä täysin Kasvu- tai Arvokriteerejä."

        return {
            "matched_profile": matched_profile,
            "verdict": verdict,
            "gross_margin_over_40": gm_over_40,
            "rule_of_40_passed": r40_passed,
            "recurring_revenue_mentioned": recurring,
            "organic_growth_confirmed": True,
            "strong_net_cash_position": net_cash,
            "positive_operating_cash_flow": pos_ocf,
            "turnaround_indicators": turnaround,
            "going_concern_risk": going_concern,
            "dilution_risk_detected": dilution_risk,
            "unsustainable_cash_burn": unsustainable_cash_burn,
            "erratic_pivots_detected": erratic_pivots,
            "cash_or_debt_issues": safety_failed,
            "reasoning": reasoning,
        }

    def run_all(self) -> List[CaseStudyResult]:
        """Runs the case study pipeline over all PDF and TXT reports in `reports_dir`."""
        logger.info(f"Scanning for PDF and TXT reports in {self.reports_dir}...")
        report_files = sorted([
            f for f in self.reports_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".pdf", ".txt")
        ])

        if not report_files:
            logger.warning(
                f"No PDF or TXT report files found in {self.reports_dir}!\n"
                f"Place historical earnings reports (e.g. QT_Group_Q3_2020.pdf, SGG.ST_2023_Q3.txt) in that directory and run again."
            )
            return []

        logger.info(f"Found {len(report_files)} report file(s) to analyze.")
        results: List[CaseStudyResult] = []

        for idx, file_path in enumerate(report_files, 1):
            filename = file_path.name
            company, period = self.parse_company_and_period_from_filename(filename)
            logger.info(f"\n[{idx}/{len(report_files)}] Processing: {filename} ({company} | {period})...")

            raw_text, backend = self.extract_text_from_file(file_path)
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
                matched_profile=eval_res["matched_profile"],
                verdict=eval_res["verdict"],
                gross_margin_over_40=eval_res["gross_margin_over_40"],
                rule_of_40_passed=eval_res["rule_of_40_passed"],
                recurring_revenue_mentioned=eval_res["recurring_revenue_mentioned"],
                organic_growth_confirmed=eval_res.get("organic_growth_confirmed", True),
                strong_net_cash_position=eval_res["strong_net_cash_position"],
                positive_operating_cash_flow=eval_res["positive_operating_cash_flow"],
                turnaround_indicators=eval_res["turnaround_indicators"],
                going_concern_risk=eval_res["going_concern_risk"],
                dilution_risk_detected=eval_res.get("dilution_risk_detected", False),
                unsustainable_cash_burn=eval_res.get("unsustainable_cash_burn", False),
                erratic_pivots_detected=eval_res.get("erratic_pivots_detected", False),
                cash_or_debt_issues=eval_res.get("cash_or_debt_issues", eval_res["going_concern_risk"]),
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
        print(f"MATCHED PROFILE:             [{res.matched_profile}]")
        print(f"VERDICT:                     {verdict_icon} {res.verdict}")
        print(f"Profile A (Growth):")
        print(f"  - Gross Margin > 40%:      {'✅ YES' if res.gross_margin_over_40 else '❌ NO'}")
        print(f"  - Rule of 40 Passed:       {'✅ YES' if res.rule_of_40_passed else '❌ NO'}")
        print(f"  - Recurring Revenue:       {'✅ YES' if res.recurring_revenue_mentioned else '❌ NO'}")
        print(f"Profile B (Value / Turnaround):")
        print(f"  - Strong Net Cash:         {'✅ YES' if res.strong_net_cash_position else '❌ NO'}")
        print(f"  - Positive Operating CF:   {'✅ YES' if res.positive_operating_cash_flow else '❌ NO'}")
        print(f"  - Turnaround Indicators:   {'✅ YES' if res.turnaround_indicators else '❌ NO'}")
        print(f"Financial Safety:")
        print(f"  - Going Concern Risk:      {'⚠️ YES (DISTRESS)' if res.going_concern_risk else '🛡️ NO (HEALTHY)'}")
        print(f"Reasoning / Assessment:")
        print(f"  👉 {res.reasoning}")
        print("=" * 75 + "\n")

    def print_final_summary_table(self, results: List[CaseStudyResult]) -> None:
        """Prints summary table of all case study runs."""
        if not results:
            return
        print("\n" + "*" * 90)
        print("                    CASE STUDY BACKTESTER SUMMARY TABLE                    ")
        print("*" * 90)
        print(f"{'Company':<16} | {'Period':<8} | {'Profile':<8} | {'Verdict':<12} | {'GM>40%':<6} | {'R40':<5} | {'NetCash':<7} | {'Risk':<6}")
        print("-" * 90)
        for r in results:
            verdict_str = f"[{r.verdict}]"
            gm_str = "YES" if r.gross_margin_over_40 else "NO"
            r40_str = "YES" if r.rule_of_40_passed else "NO"
            cash_str = "YES" if r.strong_net_cash_position else "NO"
            risk_str = "RISK" if r.going_concern_risk else "OK"
            print(f"{r.company_name[:16]:<16} | {r.report_period[:8]:<8} | {r.matched_profile:<8} | {verdict_str:<12} | {gm_str:<6} | {r40_str:<5} | {cash_str:<7} | {risk_str:<6}")
        print("*" * 90)
        print(f"Results saved to: {self.results_csv_path}\n")

    def save_results_csv(self, results: List[CaseStudyResult]) -> None:
        """Saves consolidated results to CSV."""
        if not results:
            return
        fieldnames = [
            "filename",
            "company_name",
            "report_period",
            "matched_profile",
            "verdict",
            "gross_margin_over_40",
            "rule_of_40_passed",
            "recurring_revenue_mentioned",
            "organic_growth_confirmed",
            "strong_net_cash_position",
            "positive_operating_cash_flow",
            "turnaround_indicators",
            "going_concern_risk",
            "dilution_risk_detected",
            "unsustainable_cash_burn",
            "erratic_pivots_detected",
            "cash_or_debt_issues",
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
