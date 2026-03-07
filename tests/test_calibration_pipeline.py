"""Tests for the calibration pipeline core logic.

Tests pure functions from calibration-pipeline.py: drift detection,
baseline management, suggestion evaluation, WhatsApp formatting,
regression gate, calibration history, and auto-apply decision.
Uses importlib to load the hyphenated script file.
"""

import importlib.util
import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "calibration-pipeline.py"


# ---------------------------------------------------------------------------
# Module loader -- stubs kalshi_auth to avoid real imports
# ---------------------------------------------------------------------------

_mod = None


def _load_pipeline():
    """Load calibration-pipeline.py module with stubbed dependencies."""
    global _mod
    if _mod is not None:
        return _mod

    # Save original before stubbing
    orig_auth = sys.modules.get("kalshi_auth")

    mock_auth = types.ModuleType("kalshi_auth")
    mock_auth.notify_whatsapp = lambda *a, **kw: None
    mock_auth._atomic_write_json = MagicMock()
    mock_auth.setup_logging = lambda name: logging.getLogger(name)
    mock_auth.setup_unbuffered = lambda: None
    mock_auth.PROJECT_DIR = Path(__file__).resolve().parent.parent
    sys.modules["kalshi_auth"] = mock_auth

    spec = importlib.util.spec_from_file_location("calibration_pipeline", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore original
    if orig_auth is None:
        sys.modules.pop("kalshi_auth", None)
    else:
        sys.modules["kalshi_auth"] = orig_auth

    _mod = mod
    return mod


@pytest.fixture
def pipeline():
    """Return the loaded pipeline module."""
    return _load_pipeline()


# ---------------------------------------------------------------------------
# Drift Detection Tests
# ---------------------------------------------------------------------------

class TestCheckDrift:
    """Tests for check_drift() -- comparing current results against baselines."""

    def test_check_drift_detects_degradation(self, pipeline):
        """Baseline brier 0.30, current 0.35 (16.7% degradation > 10%)."""
        baselines = {
            "aggregate": {"brier": 0.30, "n": 100},
            "per_bot": {},
            "per_city": {},
        }
        current_results = {
            "brier_score": 0.35,
            "n_evaluated": 100,
            "per_bot": {},
            "per_city_brier": {},
        }
        findings = pipeline.check_drift(baselines, current_results)
        agg = [f for f in findings if f["entity"] == "aggregate"][0]
        assert agg["drifted"] is True
        assert agg["change_pct"] > 0.10

    def test_check_drift_no_drift_within_threshold(self, pipeline):
        """Baseline 0.30, current 0.32 (6.7% < 10%)."""
        baselines = {
            "aggregate": {"brier": 0.30, "n": 50},
            "per_bot": {},
            "per_city": {},
        }
        current_results = {
            "brier_score": 0.32,
            "n_evaluated": 50,
            "per_bot": {},
            "per_city_brier": {},
        }
        findings = pipeline.check_drift(baselines, current_results)
        agg = [f for f in findings if f["entity"] == "aggregate"][0]
        assert agg["drifted"] is False

    def test_check_drift_skips_low_sample(self, pipeline):
        """Baseline n=5, current n=20. Baseline below MIN_SAMPLES=10."""
        baselines = {
            "aggregate": {"brier": 0.30, "n": 5},
            "per_bot": {},
            "per_city": {},
        }
        current_results = {
            "brier_score": 0.50,
            "n_evaluated": 20,
            "per_bot": {},
            "per_city_brier": {},
        }
        findings = pipeline.check_drift(baselines, current_results)
        agg = [f for f in findings if f["entity"] == "aggregate"][0]
        assert agg["drifted"] is False
        assert "low sample" in agg["reason"]

    def test_check_drift_per_city_independent(self, pipeline):
        """Aggregate fine but one city drifted."""
        baselines = {
            "aggregate": {"brier": 0.30, "n": 100},
            "per_bot": {},
            "per_city": {
                "MIA": {"brier": 0.25, "n": 20},
                "DEN": {"brier": 0.30, "n": 20},
            },
        }
        current_results = {
            "brier_score": 0.31,  # aggregate within threshold
            "n_evaluated": 100,
            "per_bot": {},
            "per_city_brier": {
                "MIA": {"brier": 0.40, "n": 20},  # 60% degradation
                "DEN": {"brier": 0.31, "n": 20},  # healthy
            },
        }
        findings = pipeline.check_drift(baselines, current_results)
        agg = [f for f in findings if f["entity"] == "aggregate"][0]
        mia = [f for f in findings if f["entity"] == "MIA"][0]
        den = [f for f in findings if f["entity"] == "DEN"][0]

        assert agg["drifted"] is False
        assert mia["drifted"] is True
        assert den["drifted"] is False

    def test_check_drift_handles_none_brier(self, pipeline):
        """Bot with brier_score=None in current results."""
        baselines = {
            "aggregate": {"brier": 0.30, "n": 100},
            "per_bot": {"weather": {"brier": 0.33, "n": 50}},
            "per_city": {},
        }
        current_results = {
            "brier_score": 0.30,
            "n_evaluated": 100,
            "per_bot": {"weather": {"brier_score": None, "n_evaluated": 0}},
            "per_city_brier": {},
        }
        findings = pipeline.check_drift(baselines, current_results)
        weather = [f for f in findings if f["entity"] == "weather"][0]
        assert weather["drifted"] is False
        assert weather["reason"] == "insufficient data"


# ---------------------------------------------------------------------------
# Baseline Initialization Tests
# ---------------------------------------------------------------------------

class TestInitializeBaselines:
    """Tests for initialize_baselines() -- snapshot creation from backtest results."""

    def test_initialize_baselines_structure(self, pipeline):
        """Verify baselines have correct structure; bots with None brier_score excluded."""
        backtest_results = {
            "brier_score": 0.309,
            "n_evaluated": 115,
            "per_bot": {
                "weather": {"brier_score": 0.330, "n_evaluated": 69},
                "entertainment": {"brier_score": None, "n_evaluated": 0},
                "crypto": {"brier_score": 0.400, "n_evaluated": 20},
            },
            "per_city_brier": {
                "MIA": {"brier": 0.640, "n": 8},
                "AUS": {"brier": 0.248, "n": 10},
            },
        }
        baselines = pipeline.initialize_baselines(backtest_results)

        # Structure keys
        assert "initialized_at" in baselines
        assert "last_updated" in baselines
        assert "aggregate" in baselines
        assert "per_bot" in baselines
        assert "per_city" in baselines

        # Aggregate
        assert baselines["aggregate"]["brier"] == 0.309
        assert baselines["aggregate"]["n"] == 115

        # Per-bot: weather and crypto included, entertainment excluded (None brier)
        assert "weather" in baselines["per_bot"]
        assert "crypto" in baselines["per_bot"]
        assert "entertainment" not in baselines["per_bot"]
        assert baselines["per_bot"]["weather"]["brier"] == 0.330

        # Per-city
        assert "MIA" in baselines["per_city"]
        assert "AUS" in baselines["per_city"]


# ---------------------------------------------------------------------------
# Suggestion Evaluation Tests
# ---------------------------------------------------------------------------

class TestEvaluateSuggestion:
    """Tests for evaluate_suggestion() -- proposed vs current calibration comparison."""

    def test_evaluate_suggestion_triggers_on_improvement(self, pipeline):
        """Proposed weather Brier 0.28, current 0.33 (15% improvement > 5%)."""
        proposed = {
            "weather": {"global_brier": 0.28, "n": 69},
            "nws": {"n": 0},
            "album_sales": {"n": 0},
            "box_office": {"n": 0},
            "ensemble": {"n": 0},
        }
        current = {
            "weather": {"global_brier": 0.33, "n": 69},
            "nws": {"n": 0},
            "album_sales": {"n": 0},
            "box_office": {"n": 0},
            "ensemble": {"n": 0},
        }
        result = pipeline.evaluate_suggestion(proposed, current)
        assert result["should_suggest"] is True
        assert result["aggregate_improvement_pct"] > 0.05
        assert "weather" in result["improvements"]
        assert result["improvements"]["weather"]["improvement_pct"] > 10

    def test_evaluate_suggestion_skips_negligible(self, pipeline):
        """Proposed 0.305, current 0.309 (1.3% improvement < 5%)."""
        proposed = {
            "weather": {"global_brier": 0.305, "n": 69},
            "nws": {"n": 0},
            "album_sales": {"n": 0},
            "box_office": {"n": 0},
            "ensemble": {"n": 0},
        }
        current = {
            "weather": {"global_brier": 0.309, "n": 69},
            "nws": {"n": 0},
            "album_sales": {"n": 0},
            "box_office": {"n": 0},
            "ensemble": {"n": 0},
        }
        result = pipeline.evaluate_suggestion(proposed, current)
        assert result["should_suggest"] is False
        assert result["aggregate_improvement_pct"] < 0.05


# ---------------------------------------------------------------------------
# WhatsApp Summary Formatting Tests
# ---------------------------------------------------------------------------

class TestFormatWhatsappSummary:
    """Tests for format_whatsapp_summary() -- message formatting and length limits."""

    def _make_stage_results(self, all_pass=True, failed=None):
        """Create stage results dict for testing."""
        stages = {
            "reconcile": {"success": True, "duration_s": 1.0},
            "backfill": {"success": True, "duration_s": 1.5},
            "backtest": {"success": True, "duration_s": 0.5},
            "calibrate": {"success": True, "duration_s": 18.0},
        }
        if failed:
            for name in failed:
                stages[name]["success"] = False
        return stages

    def test_format_whatsapp_summary_healthy(self, pipeline):
        """No drifted entities, all stages passed."""
        stages = self._make_stage_results(all_pass=True)
        drift_findings = [
            {"entity": "aggregate", "drifted": False, "reason": "healthy",
             "baseline_brier": 0.30, "current_brier": 0.30, "change_pct": 0.0},
            {"entity": "weather", "drifted": False, "reason": "healthy",
             "baseline_brier": 0.33, "current_brier": 0.33, "change_pct": 0.0},
        ]
        msg = pipeline.format_whatsapp_summary(stages, drift_findings, False)
        assert "All models healthy" in msg
        assert len(msg) < 500

    def test_format_whatsapp_summary_with_drift(self, pipeline):
        """Two drifted entities. Verify message contains DRIFT DETECTED and both names."""
        stages = self._make_stage_results(all_pass=True)
        drift_findings = [
            {"entity": "aggregate", "drifted": False, "reason": "healthy",
             "baseline_brier": 0.30, "current_brier": 0.30, "change_pct": 0.0},
            {"entity": "MIA", "drifted": True, "reason": "drifted",
             "baseline_brier": 0.25, "current_brier": 0.40, "change_pct": 0.60},
            {"entity": "weather", "drifted": True, "reason": "drifted",
             "baseline_brier": 0.33, "current_brier": 0.40, "change_pct": 0.2121},
        ]
        msg = pipeline.format_whatsapp_summary(stages, drift_findings, False)
        assert "DRIFT DETECTED" in msg
        assert "MIA" in msg
        assert "weather" in msg
        assert len(msg) < 500

    def test_format_whatsapp_summary_with_stage_failure(self, pipeline):
        """One stage failed. Verify message mentions the failed stage name."""
        stages = self._make_stage_results(failed=["backfill"])
        drift_findings = [
            {"entity": "aggregate", "drifted": False, "reason": "healthy",
             "baseline_brier": 0.30, "current_brier": 0.30, "change_pct": 0.0},
        ]
        msg = pipeline.format_whatsapp_summary(stages, drift_findings, True)
        assert "FAILED" in msg
        assert "backfill" in msg
        assert len(msg) < 500


# ---------------------------------------------------------------------------
# Regression Gate Tests
# ---------------------------------------------------------------------------

class TestRegressionGate:
    """Tests for check_regression_gate() -- per-bot regression safety."""

    def test_all_bots_improved_passes(self, pipeline):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
                "crypto": {"brier_score": 0.35, "n_evaluated": 30},
            },
        }
        after = {
            "brier_score": 0.27,
            "per_bot": {
                "weather": {"brier_score": 0.25, "n_evaluated": 50},
                "crypto": {"brier_score": 0.33, "n_evaluated": 30},
            },
        }
        safe, reason = pipeline.check_regression_gate(before, after)
        assert safe, f"Should pass when all improve: {reason}"

    def test_one_bot_regressed_fails(self, pipeline):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
                "crypto": {"brier_score": 0.35, "n_evaluated": 30},
            },
        }
        after = {
            "brier_score": 0.27,
            "per_bot": {
                "weather": {"brier_score": 0.25, "n_evaluated": 50},
                "crypto": {"brier_score": 0.40, "n_evaluated": 30},  # 14% worse
            },
        }
        safe, reason = pipeline.check_regression_gate(before, after)
        assert not safe, "Should fail when a bot regresses >5%"
        assert "crypto" in reason

    def test_small_regression_within_tolerance(self, pipeline):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
            },
        }
        after = {
            "brier_score": 0.29,
            "per_bot": {
                "weather": {"brier_score": 0.29, "n_evaluated": 50},  # 3.6% worse -- within 5%
            },
        }
        safe, reason = pipeline.check_regression_gate(before, after)
        assert safe, f"Small regression within 5% should pass: {reason}"

    def test_insufficient_samples_skips_bot(self, pipeline):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
                "strategy": {"brier_score": 0.80, "n_evaluated": 3},  # too few
            },
        }
        after = {
            "brier_score": 0.28,
            "per_bot": {
                "weather": {"brier_score": 0.26, "n_evaluated": 50},
                "strategy": {"brier_score": 0.90, "n_evaluated": 3},  # worse but <10 samples
            },
        }
        safe, reason = pipeline.check_regression_gate(before, after)
        assert safe, f"Bots with <10 samples should be skipped: {reason}"

    def test_no_aggregate_improvement_fails(self, pipeline):
        before = {"brier_score": 0.30, "per_bot": {}}
        after = {"brier_score": 0.31, "per_bot": {}}
        safe, reason = pipeline.check_regression_gate(before, after)
        assert not safe, "Should fail when aggregate Brier gets worse"


# ---------------------------------------------------------------------------
# Calibration History Tests
# ---------------------------------------------------------------------------

def _real_atomic_write(path, data):
    """Actual file writer for tests (replaces mocked _atomic_write_json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


class TestCalibrationHistory:
    """Tests for archive_calibration() -- versioned calibration storage."""

    def test_archive_creates_versioned_file(self, pipeline, tmp_path):
        # Patch _atomic_write_json to actually write files in tests
        orig = pipeline._atomic_write_json
        pipeline._atomic_write_json = _real_atomic_write
        try:
            history_dir = tmp_path / "calibration-history"
            cal = {"weather": {"global_brier": 0.30}, "generated_at": "2026-03-07T06:00:00"}
            path = pipeline.archive_calibration(cal, history_dir=history_dir)
            assert path.exists()
            assert "calibration-" in path.name
            # Verify content
            saved = json.loads(path.read_text())
            assert saved["calibration"]["weather"]["global_brier"] == 0.30
            assert "archived_at" in saved
        finally:
            pipeline._atomic_write_json = orig

    def test_archive_deduplicates_same_day(self, pipeline, tmp_path):
        orig = pipeline._atomic_write_json
        pipeline._atomic_write_json = _real_atomic_write
        try:
            history_dir = tmp_path / "calibration-history"
            cal = {"weather": {"global_brier": 0.30}}
            path1 = pipeline.archive_calibration(cal, history_dir=history_dir)
            path2 = pipeline.archive_calibration(cal, history_dir=history_dir)
            assert path1 != path2  # Different filenames (appends -2, -3, etc.)
            assert len(list(history_dir.iterdir())) == 2
        finally:
            pipeline._atomic_write_json = orig
