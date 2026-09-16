"""
Unit tests for batch_processor.py.
"""

import csv
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from batch_processor import (
    parse_metadata_from_filename,
    extract_json_response,
    rule_based_fallback_evaluation,
    BatchProcessor,
)


def test_parse_metadata_from_filename():
    t1, y1, q1 = parse_metadata_from_filename("KEMIRA.HE_2024_Q3.pdf")
    assert t1 == "KEMIRA.HE"
    assert y1 == "2024"
    assert q1 == "Q3"

    t2, y2, q2 = parse_metadata_from_filename("QTCOM.HE_2023_FY.txt")
    assert t2 == "QTCOM.HE"
    assert y2 == "2023"
    assert q2 == "FY"

    t3, y3, q3 = parse_metadata_from_filename("QT_Group_Q3_2020.pdf")
    assert "QT" in t3
    assert y3 == "2020"
    assert q3 == "Q3"


def test_extract_json_response():
    raw_1 = '{"verdict_details": {"matched_profile": "GROWTH", "verdict": "STRONG BUY"}}'
    parsed_1 = extract_json_response(raw_1)
    assert parsed_1 is not None
    assert parsed_1["verdict_details"]["verdict"] == "STRONG BUY"

    raw_2 = "```json\n" + raw_1 + "\n```"
    parsed_2 = extract_json_response(raw_2)
    assert parsed_2 is not None
    assert parsed_2["verdict_details"]["matched_profile"] == "GROWTH"


def test_rule_based_fallback_evaluation():
    text_growth = "Liikevaihto kasvoi 35 %. Myyntikate 80 %. Jatkuvalaskutteinen SaaS toistuva liikevaihto. Liikevoittomarginaali 15 %."
    res_g = rule_based_fallback_evaluation(text_growth)
    assert res_g["verdict_details"]["verdict"] == "STRONG BUY"
    assert res_g["verdict_details"]["matched_profile"] == "GROWTH"

    text_value = "Yhtiö on täysin velaton ja kassa on erittäin vahva. Liiketoiminnan rahavirta oli positiivinen. Tuloskäänne käynnissä."
    res_v = rule_based_fallback_evaluation(text_value)
    assert res_v["verdict_details"]["verdict"] == "STRONG BUY"
    assert res_v["verdict_details"]["matched_profile"] == "VALUE"

    text_risk = "Käyttöpääoma ei riitä. Toiminnan jatkuvuuteen liittyy merkittävää epävarmuutta. Negatiivinen oma pääoma."
    res_r = rule_based_fallback_evaluation(text_risk)
    assert res_r["verdict_details"]["verdict"] == "REJECT"
    assert res_r["verdict_details"]["matched_profile"] == "NONE"


def test_batch_processor_statefulness_and_csv_append(tmp_path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "processed_files.log"
    results_csv = tmp_path / "batch_results.csv"

    # Create dummy reports
    rep1 = reports_dir / "KEMIRA.HE_2024_Q3.txt"
    rep1.write_text("Vahva kassa ja positiivinen rahavirta. Tuloskäänne etenee.", encoding="utf-8")

    rep2 = reports_dir / "QTCOM.HE_2024_Q1.txt"
    rep2.write_text("Liikevaihto kasvoi 40 %. Myyntikate 85 %. SaaS toistuva liikevaihto.", encoding="utf-8")

    processor = BatchProcessor(
        reports_dir=reports_dir,
        processed_log_path=log_file,
        results_csv_path=results_csv,
        api_key="",  # Test rule-based execution
        throttle_seconds=0.0,
    )

    processor.run()

    # Verify CSV content
    assert results_csv.exists()
    rows = list(csv.reader(results_csv.read_text(encoding="utf-8").splitlines()))
    assert len(rows) == 3  # Header + 2 rows
    assert rows[0] == ["Filename", "Ticker", "Year", "Quarter", "Matched_Profile", "Verdict", "Reasoning"]

    # Verify log content
    assert log_file.exists()
    logged = log_file.read_text(encoding="utf-8").splitlines()
    assert "KEMIRA.HE_2024_Q3.txt" in logged
    assert "QTCOM.HE_2024_Q1.txt" in logged

    # Run again - should skip all
    processor.run()
    rows_after = list(csv.reader(results_csv.read_text(encoding="utf-8").splitlines()))
    assert len(rows_after) == 3
