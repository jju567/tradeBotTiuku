"""
Master Test Suite Runner for tradeBotTiuku.

Executes all 5 backtest, quality, and robustness test modules sequentially
and outputs a single, consolidated master evaluation report:
1. Institutional Bias-Free Backtester (institutional_backtester.py)
2. Context Window & 'Lost in the Middle' Tester (context_window_tester.py)
3. LLM Ground Truth Qualitative Tester (llm_truth_tester.py)
4. Trailing Stop-Loss Mechanical Simulator (stop_loss_simulator.py)
5. Quantitative Market Impact & DSR Engine (backtest_engine.py)
"""

from __future__ import annotations

import io
import logging
import os
import sys
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

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

# Ensure project root and scripts directory are in sys.path
BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts"
for p in [str(BASE_DIR), str(SCRIPTS_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("master_test_runner")


def run_master_test_suite() -> None:
    print("\n" + "=" * 110)
    print("🚀 TRADEBOTTIUKU — MASTER TEST SUITE: AJETAAN KAIKKI TESTIMODUULIT")
    print("=" * 110)
    start_time = time.time()

    # ------------------------------------------------------------------
    # 1. INSTITUTIONAL BIAS-FREE BACKTESTER
    # ------------------------------------------------------------------
    print("\n" + "#" * 110)
    print("1️⃣  INSTITUTIONAL BIAS-FREE BACKTESTER (PIT + 0.5% Slippage + 2.5x ATR Trailing Stop)")
    print("#" * 110)
    from institutional_backtester import InstitutionalBacktester, _dataframe_to_markdown
    inst_tester = InstitutionalBacktester(
        fundamentals_csv="data/clean_microcap_pit_fundamentals.csv",
        clean_universe_csv="data/clean_microcap_universe.csv",
        benchmark_ticker="^RUT",
        slippage_penalty_pct=0.50,
        atr_multiplier=2.5,
        atr_period=14,
    )
    inst_trades_df, inst_summary_df = inst_tester.run()
    if not inst_summary_df.empty:
        print("\n" + _dataframe_to_markdown(inst_summary_df))
    print(f"-> Suoritettu {len(inst_trades_df)} institutionaalista mikroyhtiökauppaa.")
    if not inst_trades_df.empty:
        inst_trades_df.to_csv("data/institutional_backtest_results.csv", index=False)

    # ------------------------------------------------------------------
    # 2. CONTEXT WINDOW & 'LOST IN THE MIDDLE' VULNERABILITY TESTER
    # ------------------------------------------------------------------
    print("\n" + "#" * 110)
    print("2️⃣  CONTEXT WINDOW & 'LOST IN THE MIDDLE' VULNERABILITY TESTER (5,000 words / 50% Poison Pill)")
    print("#" * 110)
    from context_window_tester import ContextWindowTester
    ctx_tester = ContextWindowTester(target_words=5000)
    ctx_results = ctx_tester.run_all()
    for idx, r in enumerate(ctx_results, 1):
        status_icon = "✅ PASS" if r["total_pass"] else "❌ FAIL"
        print(f"[{idx}/{len(ctx_results)}] {r['description']} -> {status_icon}")
        print(f"  • Syötekoko: {r['word_count']:,} sanaa (~{r['approx_tokens']:,} tokenia) | Upotus: 50.0% (Keskellä)")
        print(f"  • Odotettu tuomio: {r.get('expected_verdict', 'REJECT')} | Toteutunut: {r['actual_verdict']}")
        print(f"  • Perustelu: \"{r['reasoning']}\"")

    # ------------------------------------------------------------------
    # 3. LLM GROUND TRUTH QUALITATIVE REASONING TESTER
    # ------------------------------------------------------------------
    print("\n" + "#" * 110)
    print("3️⃣  LLM GROUND TRUTH QUALITATIVE REASONING TESTER (5 Validointitapausta)")
    print("#" * 110)
    from llm_truth_tester import LLMTruthTester
    truth_tester = LLMTruthTester()
    truth_df, truth_acc = truth_tester.run_all()
    print(f"\nKokonaisosuvuus (Accuracy): {truth_acc:.1f}%\n")
    print("| Test ID | Odotettu | Toteutunut | Tila | Pedagoginen Perustelu |")
    print("| :--- | :---: | :---: | :---: | :--- |")
    for _, row in truth_df.iterrows():
        status_icon = "✅ PASS" if row["passed"] else "❌ FAIL"
        clean_reasoning = str(row["pedagogical_reasoning"]).replace("\n", " ").replace("|", "-").strip()
        truncated = (clean_reasoning[:95] + "...") if len(clean_reasoning) > 95 else clean_reasoning
        print(f"| `{row['test_id']}` | **{row['expected_judgment']}** | `{row['llm_judgment']}` | {status_icon} | {truncated} |")

    # ------------------------------------------------------------------
    # 4. TRAILING STOP-LOSS MECHANICAL SIMULATOR
    # ------------------------------------------------------------------
    print("\n" + "#" * 110)
    print("4️⃣  DYNAMIC ATR TRAILING STOP-LOSS SIMULATOR (2.5x ATR vs. 6M Buy & Hold)")
    print("#" * 110)
    from stop_loss_simulator import StopLossSimulator
    sl_sim = StopLossSimulator(
        signals_csv="data/institutional_backtest_results.csv",
        atr_multiplier=2.5,
        atr_period=14,
    )
    sl_trades_df, sl_summary_df = sl_sim.run()
    if not sl_summary_df.empty:
        print("\n" + sl_summary_df.to_string(index=False))

    # ------------------------------------------------------------------
    # 5. MARKET IMPACT & DEFLATED SHARPE RATIO (DSR) ENGINE
    # ------------------------------------------------------------------
    print("\n" + "#" * 110)
    print("5️⃣  MARKET IMPACT & DEFLATED SHARPE RATIO (DSR) ENGINE (Toro-Bouchaud & Half-Kelly)")
    print("#" * 110)
    from screener.backtest_engine import MicroCapBacktester, print_backtest_report, generate_sample_signals_csv
    sample_csv = Path("data/sample_signals.csv")
    if not sample_csv.exists():
        generate_sample_signals_csv(sample_csv)
    engine_tester = MicroCapBacktester()
    engine_summary = engine_tester.run_backtest_from_signals_csv(signals_csv_path=sample_csv)
    print_backtest_report(engine_summary)

    elapsed = time.time() - start_time
    print("=" * 110)
    print(f"✅ KAIKKI 5 TESTIMODUULIA SUORITETTU ONNISTUNEESTI (Kokonaisaika: {elapsed:.1f} s)")
    print("=" * 110 + "\n")


if __name__ == "__main__":
    run_master_test_suite()
