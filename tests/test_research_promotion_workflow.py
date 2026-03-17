"""Direct tests for the research.promotion_workflow module."""

import pytest

from research.promotion_workflow import PromotionWorkflow, PromotionWorkflowError
from research.registry import ExperimentRunRegistry


def _seed_experiment(registry, *, experiment_id="exp-1", stage="research", status="suggested"):
    return registry.register(
        experiment_id,
        "calibration_suggestion",
        strategy_id="weather",
        source_bot="calibration-pipeline",
        status=status,
        promotion_stage=stage,
        metadata={"trigger": "weather improved"},
        event_type="seeded",
        event_at="2026-03-17T00:00:00+00:00",
    )


def test_promote_advances_one_stage_and_records_metadata(tmp_path):
    registry = ExperimentRunRegistry(tmp_path / "experiment-runs.json")
    _seed_experiment(registry)
    workflow = PromotionWorkflow(
        experiment_registry=registry,
        source_path=tmp_path / "promotion-workflow.py",
        utc_now_iso_func=lambda: "2026-03-17T01:00:00+00:00",
    )

    entry = workflow.promote(
        "exp-1",
        target_stage="shadow",
        actor="andes",
        note="start shadow rollout",
        incident_id="INC-17",
        config_version="cfg-2026-03-17",
        model_version="mdl-weather-v3",
        pr_number=38,
        change_ref="config/calibration-backup.json",
        metadata={"shadow_markets": 12},
    )

    assert entry["promotion_stage"] == "shadow"
    assert entry["status"] == "shadow"
    assert entry["history"][-1]["event_type"] == "promoted_to_shadow"
    assert entry["history"][-1]["metadata"]["from_stage"] == "research"
    assert entry["history"][-1]["metadata"]["actor"] == "andes"
    assert entry["history"][-1]["metadata"]["incident_id"] == "INC-17"
    assert entry["history"][-1]["metadata"]["config_version"] == "cfg-2026-03-17"
    assert entry["history"][-1]["metadata"]["model_version"] == "mdl-weather-v3"
    assert entry["history"][-1]["metadata"]["pr_number"] == 38
    assert entry["history"][-1]["metadata"]["change_ref"] == "config/calibration-backup.json"
    assert entry["history"][-1]["metadata"]["shadow_markets"] == 12


def test_promote_rejects_skipped_stage(tmp_path):
    registry = ExperimentRunRegistry(tmp_path / "experiment-runs.json")
    _seed_experiment(registry)
    workflow = PromotionWorkflow(experiment_registry=registry)

    with pytest.raises(PromotionWorkflowError, match="Skipped promotion stages"):
        workflow.promote("exp-1", target_stage="capped_live")


def test_rollback_moves_to_earlier_stage_and_marks_status(tmp_path):
    registry = ExperimentRunRegistry(tmp_path / "experiment-runs.json")
    _seed_experiment(registry, stage="capped_live", status="capped_live")
    workflow = PromotionWorkflow(
        experiment_registry=registry,
        utc_now_iso_func=lambda: "2026-03-17T02:00:00+00:00",
    )

    entry = workflow.rollback(
        "exp-1",
        target_stage="research",
        actor="andes",
        reason="quality regressed in shadow",
        metadata={"rollback_ticket": "INC-17"},
    )

    assert entry["promotion_stage"] == "research"
    assert entry["status"] == "rolled_back"
    assert entry["history"][-1]["event_type"] == "rolled_back_to_research"
    assert entry["history"][-1]["metadata"]["from_stage"] == "capped_live"
    assert entry["history"][-1]["metadata"]["reason"] == "quality regressed in shadow"
    assert entry["history"][-1]["metadata"]["rollback_ticket"] == "INC-17"
