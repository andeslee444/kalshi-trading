"""Direct tests for the research.registry module."""

import json

from research.registry import (
    ExperimentRunRegistry,
    ModelRegistry,
    StrategyConfigRegistry,
    annotate_research_record,
)


def test_strategy_config_registry_is_idempotent(tmp_path):
    registry = StrategyConfigRegistry(tmp_path / "strategy-config-registry.json")

    first = registry.register("weather", {"maxTradeAmount": 5})
    second = registry.register("weather", {"maxTradeAmount": 5})
    state = json.loads((tmp_path / "strategy-config-registry.json").read_text())

    assert first["config_version"] == second["config_version"]
    assert len(state["entries"]) == 1


def test_model_registry_registers_stable_versions(tmp_path):
    registry = ModelRegistry(tmp_path / "model-registry.json")

    first = registry.register(
        "ensemble_weather",
        {"strategy_id": "weather", "model_family": "weather"},
    )
    second = registry.register(
        "ensemble_weather",
        {"strategy_id": "weather", "model_family": "weather"},
    )
    state = json.loads((tmp_path / "model-registry.json").read_text())

    assert first["model_version"] == second["model_version"]
    assert len(state["entries"]) == 1


def test_experiment_run_registry_tracks_history_and_artifacts(tmp_path):
    registry = ExperimentRunRegistry(tmp_path / "experiment-runs.json")

    first = registry.register(
        "calibration:2026-03-16",
        "calibration_suggestion",
        strategy_id="calibration",
        source_bot="calibration-pipeline",
        status="suggested",
        promotion_stage="research",
        metadata={"trigger": "weather improved"},
        artifact_path=tmp_path / "suggestion.json",
        event_type="suggestion_generated",
        event_at="2026-03-16T10:00:00+00:00",
    )
    second = registry.register(
        "calibration:2026-03-16",
        "calibration_suggestion",
        status="applied",
        promotion_stage="live",
        metadata={"apply_mode": "manual"},
        artifact_path=tmp_path / "suggestion.json",
        event_type="applied",
        event_at="2026-03-16T11:00:00+00:00",
    )
    state = json.loads((tmp_path / "experiment-runs.json").read_text())
    entry = state["entries"]["calibration:2026-03-16"]

    assert first["status"] == "suggested"
    assert second["status"] == "applied"
    assert second["promotion_stage"] == "live"
    assert entry["artifact_paths"] == [str(tmp_path / "suggestion.json")]
    assert len(entry["history"]) == 2
    assert entry["history"][0]["event_type"] == "suggestion_generated"
    assert entry["history"][1]["metadata"]["apply_mode"] == "manual"


def test_annotate_research_record_uses_explicit_strategy_and_source_bot(tmp_path):
    registry = ModelRegistry(tmp_path / "model-registry.json")

    annotated = annotate_research_record(
        {
            "strategy_id": "weather",
            "source_bot": "weather",
            "model_name": "weather_single_model",
            "model_descriptor": {"model_family": "weather", "market_type": "threshold"},
            "inline_model_inputs": {"forecast_temp": 88.5},
        },
        model_registry=registry,
        source_path=tmp_path / "kalshi-trades.json",
    )
    state = json.loads((tmp_path / "model-registry.json").read_text())
    entry = next(iter(state["entries"].values()))

    assert annotated["model_version"]
    assert annotated["model_inputs"] == {"forecast_temp": 88.5}
    assert entry["strategy_id"] == "weather"
    assert entry["source_bot"] == "weather"
