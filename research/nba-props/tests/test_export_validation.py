"""Tests for export/export_calibration.py — parameter bounds, missing params, crash row filtering."""

import pytest
from pathlib import Path


def test_validate_params_rejects_oversized_position():
    from export.export_calibration import validate_params
    violations = validate_params({"BOOK_B_SIZE_PCT": 0.50})
    assert any("BOOK_B_SIZE_PCT" in v for v in violations)


def test_validate_params_rejects_extreme_penalty():
    from export.export_calibration import validate_params
    violations = validate_params({"B2B_PENALTY": -0.50})
    assert any("B2B_PENALTY" in v for v in violations)


def test_validate_params_accepts_all_defaults():
    from export.export_calibration import validate_params
    from run_experiment import DEFAULTS
    violations = validate_params(DEFAULTS)
    assert violations == []


def test_validate_params_boundary_inclusive():
    from export.export_calibration import validate_params
    assert validate_params({"MATCHUP_MULTIPLIER": 0.0}) == []
    assert validate_params({"MATCHUP_MULTIPLIER": 1.0}) == []


def test_validate_params_ignores_non_numeric():
    from export.export_calibration import validate_params
    assert validate_params({"USE_STAT_SPACE": True}) == []


def test_load_experiment_fails_on_missing_param(tmp_path):
    from export.export_calibration import load_experiment
    exp = tmp_path / "experiment.py"
    exp.write_text("MATCHUP_CAP = 0.10\n")  # missing most params
    with pytest.raises(ValueError, match="MATCHUP_MULTIPLIER"):
        load_experiment(exp)


def test_load_best_result_skips_crash_rows(tmp_path):
    from export.export_calibration import load_best_result
    tsv = tmp_path / "results.tsv"
    tsv.write_text(
        "commit\tbrier_score\tcalibration_error\texpected_profit_pct\t"
        "hit_rate\tsample_size\ttrades_taken\tbrier_95ci\tstatus\tdescription\n"
        "abc1234\t-1.0000\t-1.0000\t0.0\t0.000\t0\t0\tN/A\tcrash\tCRASH: error\n"
        "def5678\t0.2400\t0.0500\t3.5\t0.520\t8000\t200\t0.23-0.25\trun\ttest\n"
    )
    best = load_best_result(tsv)
    assert best is not None
    assert float(best["brier_score"]) == 0.24


def test_load_best_result_skips_integrity_rows(tmp_path):
    from export.export_calibration import load_best_result
    tsv = tmp_path / "results.tsv"
    tsv.write_text(
        "commit\tbrier_score\tcalibration_error\texpected_profit_pct\t"
        "hit_rate\tsample_size\ttrades_taken\tbrier_95ci\tstatus\tdescription\n"
        "abc1234\t0.0500\t0.0100\t0.0\t0.000\t100\t0\tN/A\tintegrity\tINTEGRITY: low sample\n"
        "def5678\t0.2400\t0.0500\t3.5\t0.520\t8000\t200\t0.23-0.25\trun\ttest\n"
    )
    best = load_best_result(tsv)
    assert best is not None
    assert float(best["brier_score"]) == 0.24


def test_load_best_result_returns_none_for_all_crashes(tmp_path):
    from export.export_calibration import load_best_result
    tsv = tmp_path / "results.tsv"
    tsv.write_text(
        "commit\tbrier_score\tcalibration_error\texpected_profit_pct\t"
        "hit_rate\tsample_size\ttrades_taken\tbrier_95ci\tstatus\tdescription\n"
        "abc1234\t-1.0000\t-1.0000\t0.0\t0.000\t0\t0\tN/A\tcrash\tCRASH: error\n"
    )
    assert load_best_result(tsv) is None
