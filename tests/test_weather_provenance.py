"""Focused tests for weather bot provenance helpers."""

import hashlib
import json
import re

from source_paths import resolve_bot_source_path


def _extract_function_source(source, func_name):
    start = source.index(f"def {func_name}(")
    rest = source[start:]
    lines = rest.split("\n")
    func_lines = [lines[0]]
    for line in lines[1:]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        func_lines.append(line)
    return "\n".join(func_lines)


def _load_weather_provenance_helpers():
    bot_path = resolve_bot_source_path("weather-bot.py")
    source = bot_path.read_text(encoding="utf-8")
    ns = {"json": json, "hashlib": hashlib, "re": re}
    exec(_extract_function_source(source, "_weather_model_name"), ns)
    exec(_extract_function_source(source, "_weather_research_fields"), ns)
    exec(_extract_function_source(source, "_record_weather_opportunity"), ns)
    return ns["_weather_model_name"], ns["_weather_research_fields"], ns["_record_weather_opportunity"], source


def test_weather_model_name_normalizes_probability_method():
    model_name, _research_fields, _record_weather_opportunity, _source = _load_weather_provenance_helpers()

    assert model_name("parametric_ensemble_v2") == "weather_parametric_ensemble_v2"
    assert model_name("Single Model") == "weather_single_model"


def test_weather_research_fields_include_descriptor_and_inputs():
    _model_name, research_fields, _record_weather_opportunity, _source = _load_weather_provenance_helpers()

    fields = research_fields(
        {"city": "MIA", "date": "2026-03-15", "direction": "T", "threshold": 86},
        forecast_temp=88.25,
        sigma_used=3.14159,
        probability_method="parametric_ensemble_v2",
        market_type="threshold",
        execution_mode="maker",
        days_out=1,
        per_model_probs={"gfs": 0.61, "ecmwf": 0.55},
        weights_used={"gfs": 0.6, "ecmwf": 0.4},
        model_run_tags={"gfs": "2026-03-15T12:00:00"},
        verification_confidence=0.7425,
        bias_fields={"bias_applied_f": 0.5, "bias_capped": False},
    )

    assert fields["model_name"] == "weather_parametric_ensemble_v2"
    assert fields["model_descriptor"]["model_family"] == "weather"
    assert fields["model_descriptor"]["execution_mode"] == "maker"
    assert fields["feature_snapshot_id"].startswith("weather:")
    assert fields["inline_model_inputs"]["per_model_probs"]["gfs"] == 0.61
    assert fields["inline_model_inputs"]["weights_used"]["ecmwf"] == 0.4


def test_weather_research_fields_snapshot_id_changes_with_inputs():
    _model_name, research_fields, _record_weather_opportunity, _source = _load_weather_provenance_helpers()

    base = research_fields(
        {"city": "MIA", "date": "2026-03-15", "direction": "T", "threshold": 86},
        forecast_temp=88.0,
        sigma_used=3.0,
    )
    changed = research_fields(
        {"city": "MIA", "date": "2026-03-15", "direction": "T", "threshold": 86},
        forecast_temp=89.0,
        sigma_used=3.0,
    )

    assert base["feature_snapshot_id"] != changed["feature_snapshot_id"]


def test_record_weather_opportunity_sets_stage_and_rounds_edge():
    _model_name, _research_fields, record_weather_opportunity, _source = _load_weather_provenance_helpers()

    class StubOpportunityLog:
        def __init__(self):
            self.records = []

        def record(self, record):
            self.records.append(record)
            return record

    sink = StubOpportunityLog()
    record = record_weather_opportunity(
        "KXHIGHMIA-26MAR15-T86",
        "no",
        "pruned",
        "selection_pruned",
        opportunity_stage="selection",
        opportunity_log_obj=sink,
        edge=0.123456,
        price_cents=44,
        model_name="weather_single_model",
    )

    assert record["opportunity_stage"] == "selection"
    assert record["edge"] == 0.1235
    assert sink.records[-1]["model_name"] == "weather_single_model"


def test_weather_bot_source_threads_research_fields_into_trade_and_opportunity_calls():
    _model_name, _research_fields, _record_weather_opportunity, source = _load_weather_provenance_helpers()

    assert "**research_fields" in source
    assert '**opp.get("research_fields", {})' in source
    assert "_log_weather_decision(" in source
    assert 'opportunity_stage="selection"' in source
