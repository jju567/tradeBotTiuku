"""
Unit and integration tests for screener/case_study_backtester.py.
"""

import io
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from screener.case_study_backtester import CaseStudyBacktester, CaseStudyResult


@pytest.fixture
def temp_case_study_dirs(tmp_path):
    reports_dir = tmp_path / "historical_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    results_csv = tmp_path / "case_study_results.csv"
    return reports_dir, results_csv


def test_parse_company_and_period_from_filename(temp_case_study_dirs):
    reports_dir, results_csv = temp_case_study_dirs
    tester = CaseStudyBacktester(reports_dir=reports_dir, results_csv_path=results_csv)

    comp1, per1 = tester.parse_company_and_period_from_filename("QT_Group_Q3_2020.pdf")
    assert "QT Group" in comp1
    assert "Q3" in per1 and "2020" in per1

    comp2, per2 = tester.parse_company_and_period_from_filename("Lehto_Q2_2022.pdf")
    assert "Lehto" in comp2
    assert "Q2" in per2 and "2022" in per2


def test_rule_based_core_analysis_winner(temp_case_study_dirs):
    reports_dir, results_csv = temp_case_study_dirs
    tester = CaseStudyBacktester(reports_dir=reports_dir, results_csv_path=results_csv)

    winner_text = """
    QT Group Oyj osavuosikatsaus Q3 2020.
    Liikevaihto kasvoi 45 % ja oli 18,5 miljoonaa euroa.
    Toistuva liikevaihto ja SaaS-lisenssit kasvoivat voimakkaasti.
    Myyntikate 85 % ja liikevoittomarginaali 28 %. Yhtiön tase on velaton ja kassa erittäin vahva.
    """
    res = tester.rule_based_core_analysis(winner_text)
    assert res["gross_margin_over_40"] is True
    assert res["recurring_revenue_mentioned"] is True
    assert res["rule_of_40_passed"] is True
    assert res["cash_or_debt_issues"] is False
    assert res["verdict"] == "STRONG BUY"


def test_rule_based_core_analysis_loser(temp_case_study_dirs):
    reports_dir, results_csv = temp_case_study_dirs
    tester = CaseStudyBacktester(reports_dir=reports_dir, results_csv_path=results_csv)

    loser_text = """
    Lehto Group Oyj puolivuosikatsaus 2022.
    Käyttöpääoma ei riitä seuraavalle 12 kuukaudelle.
    Toiminnan jatkuvuuteen liittyy merkittävää epävarmuutta. Negatiivinen oma pääoma ja kovenanttirikko.
    Projektiliiketoiminnan katteet heikkenivät.
    """
    res = tester.rule_based_core_analysis(loser_text)
    assert res["cash_or_debt_issues"] is True
    assert res["verdict"] == "REJECT"


def test_extract_json_response(temp_case_study_dirs):
    reports_dir, results_csv = temp_case_study_dirs
    tester = CaseStudyBacktester(reports_dir=reports_dir, results_csv_path=results_csv)

    raw_fenced = """```json
    {
      "gross_margin_over_40": true,
      "rule_of_40_passed": true,
      "recurring_revenue_mentioned": true,
      "cash_or_debt_issues": false,
      "verdict": "STRONG BUY",
      "reasoning": "High margin SaaS compounder."
    }
    ```"""
    parsed = tester.extract_json_response(raw_fenced)
    assert parsed is not None
    assert parsed["verdict"] == "STRONG BUY"
    assert parsed["gross_margin_over_40"] is True


def test_save_results_csv(temp_case_study_dirs):
    reports_dir, results_csv = temp_case_study_dirs
    tester = CaseStudyBacktester(reports_dir=reports_dir, results_csv_path=results_csv)

    results = [
        CaseStudyResult(
            filename="QT_Group_Q3_2020.pdf",
            company_name="QT Group",
            report_period="Q3 2020",
            matched_profile="GROWTH",
            verdict="STRONG BUY",
            gross_margin_over_40=True,
            rule_of_40_passed=True,
            recurring_revenue_mentioned=True,
            strong_net_cash_position=True,
            positive_operating_cash_flow=True,
            turnaround_indicators=False,
            going_concern_risk=False,
            reasoning="Scale compounder with high SaaS growth.",
        ),
        CaseStudyResult(
            filename="Lehto_Q2_2022.pdf",
            company_name="Lehto",
            report_period="Q2 2022",
            matched_profile="NONE",
            verdict="REJECT",
            gross_margin_over_40=False,
            rule_of_40_passed=False,
            recurring_revenue_mentioned=False,
            strong_net_cash_position=False,
            positive_operating_cash_flow=False,
            turnaround_indicators=False,
            going_concern_risk=True,
            reasoning="Severe debt distress and covenant breaches.",
        ),
    ]

    tester.save_results_csv(results)
    assert results_csv.exists()

    content = results_csv.read_text(encoding="utf-8")
    assert "QT Group" in content
    assert "STRONG BUY" in content
    assert "Lehto" in content
    assert "REJECT" in content
