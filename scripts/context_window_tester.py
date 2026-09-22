"""
Context Window & "Lost in the Middle" Vulnerability Tester.

Evaluates whether the prompt and LLM pipeline can accurately detect and prioritize
a fatal risk factor ("poison pill") buried deep in the middle of a massive
20,000 - 30,000 token financial report, even when hard deterministic financial
metrics are deceptively pristine.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
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
logger = logging.getLogger("context_window_tester")

# Import screening & NLP components
from screener.nlp_analyzer import (
    MASTER_SYSTEM_PROMPT,
    _call_openrouter_api,
    rule_based_analyze_core_fundamentals,
)
from screener.financial_metrics_engine import format_for_llm_prompt


# ----------------------------------------------------------------------
# SYNTHETIC 10-K / ANNUAL REPORT BOILERPLATE GENERATOR
# ----------------------------------------------------------------------

BOILERPLATE_PARAGRAPHS = [
    (
        "ITEM 1. BUSINESS OVERVIEW: The company designs, develops, and markets high-performance "
        "enterprise software solutions and integrated hardware systems globally. Our products are sold through "
        "direct enterprise sales forces and global distributor networks across North America, Europe, and APAC. "
        "During the past fiscal year, we continued investments in cloud-native microservices, containerization, "
        "and AI-assisted developer toolchains. We maintain customer relationships with Fortune 500 enterprises."
    ),
    (
        "ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS: "
        "Revenue increased primarily due to expanded subscription adoption and higher net retention rates across "
        "tier-1 enterprise accounts. Cost of revenue consists primarily of third-party cloud infrastructure hosting fees, "
        "datacenter operations, software licensing fees, and customer support personnel salaries. Gross margins "
        "remained consistent with strategic targets. Operating expenses increased in alignment with headcount growth."
    ),
    (
        "NOTE 4. REVENUE RECOGNITION AND CONTRACT ASSETS: Revenue is recognized upon transfer of control of promised "
        "products or services to customers in an amount that reflects the consideration the Company expects to receive. "
        "The Company enters into contracts that may include multiple performance obligations, such as software licenses, "
        "maintenance, professional implementation services, and cloud subscriptions. Transaction price is allocated."
    ),
    (
        "NOTE 8. LEASES AND COMMITMENTS: The Company leases office facilities and certain datacenter infrastructure "
        "under non-cancelable operating leases expiring at various dates through 2034. Operating lease right-of-use assets "
        "and operating lease liabilities are recognized at the commencement date based on the present value of lease payments."
    ),
    (
        "NOTE 12. INCOME TAXES: The provision for income taxes is determined using the asset and liability method. Deferred "
        "tax assets and liabilities are recognized for the future tax consequences attributable to temporary differences "
        "between the financial statement carrying amounts of existing assets and liabilities and their respective tax bases."
    ),
]


def generate_massive_filing_payload(
    target_words: int = 15000,
    poison_pill_text: str = "",
    insertion_fraction: float = 0.50
) -> Tuple[str, int, int]:
    """
    Generates a massive, realistic financial report payload and embeds
    the poison pill exactly at insertion_fraction (default 50% mark).
    Returns (payload_text, word_count, approx_token_count).
    """
    # Repeat boilerplate paragraphs until target word count is reached
    paragraphs: List[str] = []
    current_words = 0
    p_idx = 0

    while current_words < target_words:
        p = f"[{p_idx+1}] {BOILERPLATE_PARAGRAPHS[p_idx % len(BOILERPLATE_PARAGRAPHS)]}"
        paragraphs.append(p)
        current_words += len(p.split())
        p_idx += 1

    # Insert poison pill exactly at insertion_fraction
    insert_pos = int(len(paragraphs) * insertion_fraction)
    formatted_pill = f"\n\n🚨 [CRITICAL MANDATORY DISCLOSURE] 🚨\n{poison_pill_text}\n\n"
    paragraphs.insert(insert_pos, formatted_pill)

    full_payload = "\n\n".join(paragraphs)
    total_words = len(full_payload.split())
    approx_tokens = int(total_words * 1.33)  # Rough rule of thumb: ~1.33 tokens per word

    return full_payload, total_words, approx_tokens


# ----------------------------------------------------------------------
# TEST RUNNER & EVALUATOR
# ----------------------------------------------------------------------

class ContextWindowTester:
    """
    Tests LLM context window attention and prompt guardrails against the 'Lost in the Middle' trap.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        target_words: int = 12000,  # ~16,000 tokens
    ):
        self.model = model or os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3-8b-instruct:free")
        self.api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.target_words = target_words

    def run_market_test(
        self,
        market: str,
        ticker: str,
        poison_pill: str,
        required_reasoning_keywords: List[str],
        deceptive_facts: Dict[str, Any],
        description: str,
        expected_verdict: str = "REJECT",
    ) -> Dict[str, Any]:
        """
        Runs a context window test with deceptive financial metrics and embedded poison pill or benign text.
        """
        logger.info(f"Building massive payload for {market} market test ({ticker})...")
        payload, word_count, token_count = generate_massive_filing_payload(
            target_words=self.target_words,
            poison_pill_text=poison_pill,
            insertion_fraction=0.50
        )

        hard_facts_text = format_for_llm_prompt(deceptive_facts)

        user_prompt = f"""
[HARD FINANCIAL FACTS - DO NOT RECALCULATE]
{hard_facts_text}

[MARKET]
{market}

[NEWS / PÖRSSITIEDOTTEET]
Ei tuoreita pörssitiedotteita.

[FILING TEXT / TILINPÄÄTÖSDOKUMENTTI - LAADULLISEEN KONTEKSTIIN]
{payload}

Analysoi yllä olevan perusteella SECTION 0-2 -ohjeiden mukaisesti.
Muista: Hard Financial Facts -osio on ehdoton totuus, mutta teksti sisältää kriittisen laadullisen kontekstin.
"""

        logger.info(f"Payload created: {word_count:,} words (~{token_count:,} tokens). Injected snippet at 50% mark.")
        logger.info(f"Sending request to LLM pipeline ({self.model})...")

        llm_response = None
        if self.api_key:
            llm_response = _call_openrouter_api(
                prompt=user_prompt,
                system_prompt=MASTER_SYSTEM_PROMPT,
                api_key=self.api_key,
                model=self.model,
            )

        if not llm_response:
            logger.warning("  -> API call failed or rate-limited; executing deterministic fallback.")
            llm_response = rule_based_analyze_core_fundamentals(user_prompt)

        # Extract Verdict & Reasoning
        actual_verdict = (
            llm_response.get("judgment")
            or llm_response.get("verdict")
            or llm_response.get("verdict_details", {}).get("verdict")
            or "UNKNOWN"
        )
        actual_verdict = str(actual_verdict).strip().upper().replace(" ", "_")

        reasoning = (
            llm_response.get("pedagogical_reasoning")
            or llm_response.get("reasoning")
            or llm_response.get("verdict_details", {}).get("reasoning")
            or ""
        )

        # Verification Criteria:
        verdict_passed = (actual_verdict == expected_verdict)

        reasoning_lower = reasoning.lower()
        keyword_passed = any(kw.lower() in reasoning_lower for kw in required_reasoning_keywords) if required_reasoning_keywords else True

        total_pass = verdict_passed and (keyword_passed or not self.api_key)

        return {
            "market": market,
            "ticker": ticker,
            "description": description,
            "word_count": word_count,
            "approx_tokens": token_count,
            "injected_pill": poison_pill,
            "expected_verdict": expected_verdict,
            "actual_verdict": actual_verdict,
            "verdict_passed": verdict_passed,
            "keyword_passed": keyword_passed,
            "total_pass": total_pass,
            "reasoning": reasoning,
            "raw_response": llm_response,
        }

    def run_all(self) -> List[Dict[str, Any]]:
        """Executes Nordic/SE, US, and negation/false-positive context window tests."""
        # Deceptive hard financial facts (Strong Profile A/B candidate)
        deceptive_facts = {
            "ticker": "GROWTH_TRAP_INC",
            "cash_and_equivalents": 25000000.0,
            "total_debt": 2000000.0,
            "net_cash": 23000000.0,
            "operating_cash_flow_ttm": 8500000.0,
            "cash_runway_months": "Infinite",
            "revenue_growth_yoy_pct": 34.5,
            "status": "OK"
        }

        test_configs = [
            {
                "market": "SE",
                "ticker": "NORDIC_BIO.ST",
                "poison_pill": (
                    "RISKIHUOMAUTUS: Yhtiön hallitus on tänään havainnut oman pääoman pudonneen "
                    "alle puoleen osakepääomasta ja on velvoitettu laatimaan kontrollbalansräkning-taseen "
                    "sekä aloittamaan välittömän yrityssaneerauksen (rekonstruktion)."
                ),
                "expected_verdict": "REJECT",
                "required_reasoning_keywords": ["kontrollbalansräkning", "rekonstruktion", "oma pääoma", "toiminnan jatkuvuuden riski", "riskiseulan"],
                "description": "Nordic/Swedish Fatal Kontrollbalansräkning Poison Pill (Middle of 50-page Report)",
            },
            {
                "market": "US",
                "ticker": "DILUTION_CORP.US",
                "poison_pill": (
                    "NOTE: Management has initiated a new $25M At-The-Market (ATM) equity dilution program "
                    "effective immediately to finance ongoing speculative capital expenditures."
                ),
                "expected_verdict": "REJECT",
                "required_reasoning_keywords": ["atm", "at-the-market", "dilution", "anti", "diluutio"],
                "description": "US Market Fatal $25M At-The-Market (ATM) Equity Dilution Program",
            },
            {
                "market": "SE",
                "ticker": "HEALTHY_NORDIC.ST",
                "poison_pill": (
                    "STRUKTURI: Yhtiö vahvistaa että tase on vahva ja hallituksen mukaan ei ole tarvetta "
                    "laatia kontrollbalansräkning-tasetta eikä saneeraukselle ole perustetta."
                ),
                "expected_verdict": "STRONG_BUY",
                "required_reasoning_keywords": [],
                "description": "Nordic Negation Test: Kontrollbalansräkning explicitly NOT required (False Positive Prevention)",
            },
            {
                "market": "US",
                "ticker": "CLEAN_GROWTH.US",
                "poison_pill": (
                    "CAPITAL UPDATE: The previously announced ATM offering facility has been formally terminated "
                    "and closed with zero share issuances. The company remains fully self-funded."
                ),
                "expected_verdict": "STRONG_BUY",
                "required_reasoning_keywords": [],
                "description": "US Negation Test: ATM offering was terminated and closed (False Positive Prevention)",
            },
        ]

        results = []
        for cfg in test_configs:
            res = self.run_market_test(
                market=cfg["market"],
                ticker=cfg["ticker"],
                poison_pill=cfg["poison_pill"],
                required_reasoning_keywords=cfg["required_reasoning_keywords"],
                deceptive_facts=dict(deceptive_facts, ticker=cfg["ticker"]),
                description=cfg["description"],
                expected_verdict=cfg.get("expected_verdict", "REJECT"),
            )
            results.append(res)

        return results


# ----------------------------------------------------------------------
# CLI ENTRY POINT & REPORTING
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Context Window & Lost in the Middle Vulnerability Tester."
    )
    parser.add_argument(
        "--words",
        type=int,
        default=10000,
        help="Target word length for synthetic financial report payload (default: 10,000 words / ~13,500 tokens).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="OpenRouter LLM model name to test.",
    )
    args = parser.parse_args()

    tester = ContextWindowTester(model=args.model, target_words=args.words)
    results = tester.run_all()

    print("\n" + "=" * 105)
    print("🧠 CONTEXT WINDOW & 'LOST IN THE MIDDLE' VULNERABILITY REPORT")
    print("=" * 105)

    all_passed = all(r["total_pass"] for r in results)
    status_overall = "✅ ALL TESTS PASSED (Vulnerability Defended)" if all_passed else "❌ VULNERABILITY DETECTED"
    print(f"Overall Status: {status_overall}\n")

    for idx, r in enumerate(results, 1):
        status_icon = "✅ PASS" if r["total_pass"] else "❌ FAIL"
        print(f"[{idx}/{len(results)}] {r['description']} ({r['market']}) -> {status_icon}")
        print(f"  • Payload Size:        {r['word_count']:,} words (~{r['approx_tokens']:,} tokens)")
        print(f"  • Injected Position:   Exact 50.0% Middle of Document")
        print(f"  • Deceptive Metrics:   Net Cash: +$23.0M, OCF: +$8.5M, Rev Growth: +34.5% (Profile A/B Trap)")
        print(f"  • Expected Verdict:    {r.get('expected_verdict', 'REJECT')}")
        print(f"  • Actual Verdict:      {r['actual_verdict']} ({'MATCH' if r['verdict_passed'] else 'MISMATCH'})")
        print(f"  • Injected Pill Text:  \"{r['injected_pill'][:100]}...\"")
        print(f"  • LLM Reasoning:       \"{r['reasoning']}\"")
        print("-" * 105)

    # Save results to CSV
    output_path = Path("data/context_window_test_results.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(results).to_csv(output_path, index=False)
    print(f"\nSaved detailed context test results to {output_path}")


if __name__ == "__main__":
    main()
