"""Regression checks for Phase 8 desk-ops docs."""

from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DOCS_ROOT = PROJECT_DIR / "docs" / "desk-ops"
RUNBOOKS_ROOT = DOCS_ROOT / "runbooks"


def _read(path):
    return path.read_text()


def test_phase8_docs_exist():
    required = [
        DOCS_ROOT / "README.md",
        DOCS_ROOT / "service-ownership.md",
        DOCS_ROOT / "operating-cadence.md",
        DOCS_ROOT / "change-management.md",
        DOCS_ROOT / "incident-template.md",
        RUNBOOKS_ROOT / "README.md",
        RUNBOOKS_ROOT / "supervisor-and-stale-bots.md",
        RUNBOOKS_ROOT / "source-freshness-and-parser-failures.md",
        RUNBOOKS_ROOT / "ledger-parity-regression.md",
        RUNBOOKS_ROOT / "promotion-rollback.md",
    ]
    missing = [str(path) for path in required if not path.exists()]
    assert not missing, f"Missing Phase 8 docs: {missing}"


def test_phase8_readme_keeps_operating_cadence():
    text = _read(DOCS_ROOT / "README.md")
    assert "### Daily" in text
    assert "### Weekly" in text
    assert "### Monthly" in text
    assert "python3 scripts/reconcile-trades.py" in text
    assert "python3 scripts/source-scorecard.py" in text


def test_operating_cadence_covers_daily_weekly_monthly_reviews():
    text = _read(DOCS_ROOT / "operating-cadence.md")
    assert "## Daily Checklist" in text
    assert "## Weekly Checklist" in text
    assert "## Monthly Checklist" in text
    assert "python3 scripts/reconcile-trades.py" in text
    assert "python3 scripts/daily-attribution.py --save" in text
    assert "python3 scripts/source-scorecard.py" in text
    assert "python3 scripts/supervisor.py status" in text
    assert "python3 scripts/calibration-pipeline.py --dry-run" in text
    assert "python3 scripts/analyze-performance.py --reconcile" in text
    assert "python3 scripts/promotion-workflow.py show <experiment_id>" in text


def test_service_ownership_covers_core_services():
    text = _read(DOCS_ROOT / "service-ownership.md")
    for service in (
        "Weather bot",
        "Crypto bot",
        "Economics bot",
        "Entertainment bot",
        "Source Monitor",
        "Position Monitor",
        "Supervisor",
        "Dashboard",
        "Calibration pipeline",
    ):
        assert service in text


def test_change_management_references_canonical_promotion_workflow():
    text = _read(DOCS_ROOT / "change-management.md")
    for stage in ("research", "shadow", "capped_live", "live"):
        assert f"`{stage}`" in text
    assert "python3 scripts/promotion-workflow.py" in text
    assert "data/experiment-runs.json" in text
    assert "config/calibration-backup.json" in text


def test_runbooks_include_required_sections():
    for path in RUNBOOKS_ROOT.glob("*.md"):
        if path.name == "README.md":
            continue
        text = _read(path)
        assert "## When To Use" in text
        assert "## Immediate Actions" in text
        assert "## Exit Criteria" in text
