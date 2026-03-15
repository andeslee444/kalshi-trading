"""Direct tests for the research.registry module."""

import json

from research.registry import ModelRegistry, StrategyConfigRegistry


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
