import importlib.util
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_script():
    path = PROJECT_DIR / "scripts" / "ledger-parity-report.py"
    spec = importlib.util.spec_from_file_location("ledger_parity_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_overall_ok_rejects_no_overlap_without_pre_coverage():
    module = _load_script()
    report = {
        "trade_logs": [
            {
                "comparison_status": "no_overlap",
                "pre_coverage": False,
                "count_match": True,
                "hash_match": True,
            }
        ],
        "decision_logs": [],
        "verification": [],
    }

    assert module._overall_ok(report) is False


def test_overall_ok_allows_pre_coverage_no_overlap():
    module = _load_script()
    report = {
        "trade_logs": [
            {
                "comparison_status": "no_overlap",
                "pre_coverage": True,
                "count_match": True,
                "hash_match": True,
            }
        ],
        "decision_logs": [],
        "verification": [],
    }

    assert module._overall_ok(report) is True
