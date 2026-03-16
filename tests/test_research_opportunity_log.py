"""Direct tests for the research.opportunity_log module."""

import json

from research.opportunity_log import OpportunityLog
from research.registry import ModelRegistry


def test_opportunity_log_records_timestamp_and_artifact(tmp_path):
    log = OpportunityLog(tmp_path / "opportunity-log.json")

    record = log.record(
        {
            "ticker": "KXHIGHMIA-26MAR15-T86",
            "side": "no",
            "action": "skipped",
            "reason": "edge below threshold",
        }
    )
    stored = json.loads((tmp_path / "opportunity-log.json").read_text())

    assert record["artifact"] == "opportunity_log"
    assert record["timestamp"]
    assert stored[-1] == record


def test_opportunity_log_applies_research_provenance(tmp_path):
    registry = ModelRegistry(tmp_path / "model-registry.json")
    log = OpportunityLog(
        tmp_path / "opportunity-log.json",
        strategy_id="weather",
        config_version="cfg-weather",
        model_registry=registry,
        source_bot="weather",
        source_path=tmp_path / "kalshi-trades.json",
    )

    record = log.record(
        {
            "ticker": "KXHIGHMIA-26MAR15-T86",
            "side": "no",
            "action": "pruned",
            "reason": "selection_pruned",
            "feature_snapshot_id": "weather:test123",
            "model_name": "weather_parametric_ensemble_v2",
            "model_descriptor": {"model_family": "weather", "market_type": "threshold"},
            "inline_model_inputs": {"forecast_temp": 88.25},
        }
    )
    state = json.loads((tmp_path / "model-registry.json").read_text())
    entry = next(iter(state["entries"].values()))

    assert record["strategy_id"] == "weather"
    assert record["config_version"] == "cfg-weather"
    assert record["source_bot"] == "weather"
    assert record["model_version"]
    assert "model_inputs" not in record
    assert entry["source_path"] == str(tmp_path / "kalshi-trades.json")
