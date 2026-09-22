"""
LLM Ground Truth & Qualitative Reasoning Tester.

Validates the LLM's qualitative reasoning and deterministic guardrail adherence
against a known ground-truth dataset of historical financial text snippets
(e.g., Swedish kontrollbalansräkning, toxic dilution/ATM offering, heavy insider buying turnaround,
and neutral calendar/administrative announcements).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from dotenv import load_dotenv
import pandas as pd

# Load environment variables (.env)
load_dotenv()

# Configure cross-platform terminal encoding (Windows cp1252 fix)
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("llm_truth_tester")

# Import screening & NLP components
from screener.nlp_analyzer import MASTER_SYSTEM_PROMPT, _call_openrouter_api, rule_based_analyze_core_fundamentals
from screener.financial_metrics_engine import format_for_llm_prompt


# ----------------------------------------------------------------------
# DEFAULT GROUND TRUTH TEST CASES
# ----------------------------------------------------------------------

DEFAULT_TEST_CASES: List[Dict[str, Any]] = [
    {
        "test_id": "TEST_01_SWEDISH_KONTROLLBALANSRÄKNING",
        "market": "SE",
        "ticker": "ANIMA.ST",
        "hard_facts_mock": {
            "ticker": "ANIMA.ST",
            "cash_and_equivalents": 500000.0,
            "total_debt": 3500000.0,
            "net_cash": -3000000.0,
            "operating_cash_flow_ttm": -1200000.0,
            "cash_runway_months": 5.0,
            "revenue_growth_yoy_pct": -12.5,
            "status": "OK"
        },
        "text_snippet": (
            "Styrelsen för Anima Group AB har idag beslutat att upprätta en kontrollbalansräkning "
            "då det finns skäl att anta att bolagets eget kapital understiger hälften av det registrerade "
            "aktiekapitalet. Bolaget undersöker nu olika kapitalanskaffningsalternativ och rekonstruktion."
        ),
        "expected_judgment": "REJECT",
        "description": "Swedish legal emergency (kontrollbalansräkning / rekonstruktion) - Absolute REJECT gatekeeper."
    },
    {
        "test_id": "TEST_02_ATM_DILUTION_CRISIS",
        "market": "US",
        "ticker": "MULLEN.US",
        "hard_facts_mock": {
            "ticker": "MULLEN.US",
            "cash_and_equivalents": 1200000.0,
            "total_debt": 15000000.0,
            "net_cash": -13800000.0,
            "operating_cash_flow_ttm": -24000000.0,
            "cash_runway_months": 0.6,
            "revenue_growth_yoy_pct": -85.0,
            "status": "OK"
        },
        "text_snippet": (
            "The Company has entered into an At-The-Market (ATM) equity offering agreement with an investment bank "
            "to sell up to $50 million of common shares. In addition, the Board approved a 1-for-25 reverse stock split "
            "to maintain compliance with Nasdaq minimum bid requirements. Cash burn continues at $2M per month."
        ),
        "expected_judgment": "REJECT",
        "description": "Severe dilution, active ATM offering, short cash runway (<1m) and reverse split - Absolute REJECT."
    },
    {
        "test_id": "TEST_03_INSIDER_BUYING_TURNAROUND",
        "market": "FI",
        "ticker": "FARON.HE",
        "hard_facts_mock": {
            "ticker": "FARON.HE",
            "cash_and_equivalents": 15000000.0,
            "total_debt": 2000000.0,
            "net_cash": 13000000.0,
            "operating_cash_flow_ttm": -3500000.0,
            "cash_runway_months": 51.4,
            "revenue_growth_yoy_pct": 45.0,
            "status": "OK"
        },
        "text_snippet": (
            "Johdon liiketoimet: Toimitusjohtaja ja hallituksen puheenjohtaja ovat hankkineet markkinalta "
            "yhteensä 250 000 yhtiön osaketta omilla henkilökohtaisilla varoillaan. Samalla yhtiö tiedottaa "
            "allekirjoittaneensa merkittävän strategisen lisensointisopimuksen globaalin lääkejätin kanssa, "
            "joka tuo 15 miljoonan euron etumaksun ja turvaa toiminnan kannattavuuskäänteen."
        ),
        "expected_judgment": "WATCH_TURNAROUND",
        "description": "Heavy executive open-market buying with major turnaround catalyst & positive net cash."
    },
    {
        "test_id": "TEST_04_ADMIN_CALENDAR_HOLD",
        "market": "FI",
        "ticker": "HARVIA.HE",
        "hard_facts_mock": {
            "ticker": "HARVIA.HE",
            "cash_and_equivalents": 42000000.0,
            "total_debt": 35000000.0,
            "net_cash": 7000000.0,
            "operating_cash_flow_ttm": 38000000.0,
            "cash_runway_months": "Infinite",
            "revenue_growth_yoy_pct": 6.5,
            "status": "OK"
        },
        "text_snippet": (
            "Harvia Oyj julkaisee taloudelliset katsauksensa vuonna 2026 seuraavasti: "
            "Tilinpäätöstiedote vuodelta 2025 julkaistaan 12. helmikuuta 2026. "
            "Osavuosikatsaus tammi-maaliskuulta 2026 julkaistaan 6. toukokuuta 2026. "
            "Varsinainen yhtiökokous pidetään torstaina 2. huhtikuuta 2026 Muuramessa."
        ),
        "expected_judgment": "HOLD",
        "description": "Neutral administrative financial calendar release with stable ongoing business."
    },
    {
        "test_id": "TEST_05_HIGH_MARGIN_HYPERGROWTH",
        "market": "US",
        "ticker": "NVDA",
        "hard_facts_mock": {
            "ticker": "NVDA",
            "cash_and_equivalents": 34800000000.0,
            "total_debt": 8460000000.0,
            "net_cash": 26340000000.0,
            "operating_cash_flow_ttm": 40500000000.0,
            "cash_runway_months": "Infinite",
            "revenue_growth_yoy_pct": 105.8,
            "status": "OK"
        },
        "text_snippet": (
            "NVIDIA reported record quarterly revenue of $30.0 billion, up 122% year-over-year. "
            "Data Center revenue was $26.3 billion, up 154% from a year ago. Gross margin expanded to 75.1%. "
            "Management raised full-year guidance and reiterated robust enterprise adoption of Hopper and Blackwell architectures."
        ),
        "expected_judgment": "STRONG_BUY",
        "allowed_judgments": ["STRONG_BUY", "STRONG BUY"],
        "description": "High margin hypergrowth compounder with stellar net cash and positive cash flow (Profile A)."
    }
]


# ----------------------------------------------------------------------
# TEST RUNNER & EVALUATOR
# ----------------------------------------------------------------------

class LLMTruthTester:
    """
    Executes qualitative ground-truth tests against the LLM prompt pipeline.
    """

    def __init__(
        self,
        test_cases: Optional[List[Dict[str, Any]]] = None,
        csv_path: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.test_cases = test_cases or []
        if csv_path and Path(csv_path).exists():
            self.test_cases = self._load_from_csv(csv_path)
        elif not self.test_cases:
            self.test_cases = DEFAULT_TEST_CASES

        self.model = model or os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3-8b-instruct:free")
        self.api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")

    def _load_from_csv(self, path: str) -> List[Dict[str, Any]]:
        """Loads test dataset from CSV file."""
        df = pd.read_csv(path)
        cases = []
        for _, row in df.iterrows():
            mock_facts = row.get("hard_facts_mock")
            if isinstance(mock_facts, str):
                try:
                    mock_facts = json.loads(mock_facts)
                except Exception:
                    mock_facts = {}
            cases.append({
                "test_id": str(row.get("test_id")),
                "market": str(row.get("market", "US")),
                "ticker": str(row.get("ticker", "TEST")),
                "hard_facts_mock": mock_facts,
                "text_snippet": str(row.get("text_snippet", "")),
                "expected_judgment": str(row.get("expected_judgment")),
                "description": str(row.get("description", "")),
            })
        return cases

    def build_test_prompt(self, case: Dict[str, Any]) -> str:
        """Constructs the prompt exactly mimicking nlp_analyzer.build_analysis_prompt."""
        hard_facts = case.get("hard_facts_mock", {})
        hard_facts_text = format_for_llm_prompt(hard_facts)
        market = case.get("market", "US")
        text = case.get("text_snippet", "")

        prompt = f"""
[HARD FINANCIAL FACTS - DO NOT RECALCULATE]
{hard_facts_text}

[MARKET]
{market}

[NEWS / PÖRSSITIEDOTTEET]
{text}

[FILING TEXT / TILINPÄÄTÖSDOKUMENTTI - LAADULLISEEN KONTEKSTIIN]
{text}

Analysoi yllä olevan perusteella SECTION 0-2 -ohjeiden mukaisesti.
Muista: Hard Financial Facts -osio on ehdoton totuus, älä laske uudelleen.
"""
        return prompt.strip()

    def run_single_test(self, case: Dict[str, Any]) -> Dict[str, Any]:
        """Runs evaluation for a single ground truth test case."""
        prompt = self.build_test_prompt(case)
        test_id = case.get("test_id", "UNKNOWN")
        expected = str(case.get("expected_judgment")).strip().upper()
        allowed = [a.strip().upper() for a in case.get("allowed_judgments", [expected])]

        logger.info(f"Running {test_id} (Expected: {expected})...")

        llm_response = None
        if self.api_key:
            llm_response = _call_openrouter_api(
                prompt=prompt,
                system_prompt=MASTER_SYSTEM_PROMPT,
                api_key=self.api_key,
                model=self.model,
            )

        if not llm_response:
            logger.warning(f"  -> API call failed or no API key, executing deterministic rule fallback.")
            llm_response = rule_based_analyze_core_fundamentals(prompt)

        # Extract Judgment & Reasoning
        # The prompt asks for judgment or verdict
        actual_raw = (
            llm_response.get("judgment")
            or llm_response.get("verdict")
            or llm_response.get("verdict_details", {}).get("verdict")
            or "UNKNOWN"
        )
        actual_judgment = str(actual_raw).strip().upper().replace(" ", "_")
        expected_norm = expected.replace(" ", "_")

        # Allow flexible match for allowed judgments (e.g. STRONG_BUY / STRONG BUY / WATCH_TURNAROUND)
        passed = (
            actual_judgment == expected_norm
            or any(actual_judgment == a.replace(" ", "_") for a in allowed)
        )

        reasoning = (
            llm_response.get("pedagogical_reasoning")
            or llm_response.get("reasoning")
            or llm_response.get("verdict_details", {}).get("reasoning")
            or "Ei saatavilla"
        )

        return {
            "test_id": test_id,
            "market": case.get("market"),
            "expected_judgment": expected,
            "llm_judgment": actual_judgment,
            "passed": passed,
            "pedagogical_reasoning": reasoning,
            "raw_response": llm_response,
        }

    def run_all(self) -> Tuple[pd.DataFrame, float]:
        """Executes all ground truth test cases and returns summary dataframe and accuracy."""
        results = []
        for case in self.test_cases:
            res = self.run_single_test(case)
            results.append(res)

        df = pd.DataFrame(results)
        accuracy = (df["passed"].mean() * 100.0) if not df.empty else 0.0
        return df, accuracy


# ----------------------------------------------------------------------
# CLI ENTRY POINT & MARKDOWN OUTPUT
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM Ground Truth Tester: Validate LLM qualitative reasoning and deterministic guardrails."
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Path to custom ground truth CSV dataset (optional).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Custom OpenRouter LLM model to evaluate.",
    )
    args = parser.parse_args()

    tester = LLMTruthTester(csv_path=args.csv, model=args.model)
    df, accuracy = tester.run_all()

    # Format Markdown Report
    print("\n" + "=" * 90)
    print("🧪 LLM GROUND TRUTH QUALITATIVE REASONING EVALUATION REPORT")
    print("=" * 90)
    print(f"Total Evaluated Test Cases: {len(df)}")
    print(f"Overall Accuracy:          {accuracy:.1f}%\n")

    # Generate Markdown Table
    print("| Test ID | Expected | LLM Result | Status | Pedagogical Reasoning |")
    print("| :--- | :---: | :---: | :---: | :--- |")

    for _, row in df.iterrows():
        status_icon = "✅ PASS" if row["passed"] else "❌ FAIL"
        # Truncate reasoning to 120 chars for clean table output
        clean_reasoning = str(row["pedagogical_reasoning"]).replace("\n", " ").replace("|", "-").strip()
        truncated_reasoning = (clean_reasoning[:115] + "...") if len(clean_reasoning) > 115 else clean_reasoning

        print(f"| `{row['test_id']}` | **{row['expected_judgment']}** | `{row['llm_judgment']}` | {status_icon} | {truncated_reasoning} |")

    print("=" * 90 + "\n")

    # Save to data directory
    output_path = Path("data/ground_truth_test_results.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"Saved detailed test execution results to {output_path}")


if __name__ == "__main__":
    main()
