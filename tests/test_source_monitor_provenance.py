"""Focused tests for source-monitor provenance helpers."""

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


def _load_source_monitor_provenance_helpers():
    bot_path = resolve_bot_source_path("source-monitor.py")
    source = bot_path.read_text(encoding="utf-8")
    ns = {"json": json, "hashlib": hashlib, "re": re}
    exec(_extract_function_source(source, "_source_monitor_model_name"), ns)
    exec(_extract_function_source(source, "_source_monitor_research_fields"), ns)
    exec(_extract_function_source(source, "_record_source_monitor_opportunity"), ns)
    return ns["_source_monitor_model_name"], ns["_source_monitor_research_fields"], ns["_record_source_monitor_opportunity"], source


def test_source_monitor_model_name_tracks_source_family():
    model_name, _research_fields, _record_source_monitor_opportunity, _source = _load_source_monitor_provenance_helpers()

    assert model_name("album") == "entertainment_info_arb_album"
    assert model_name("boxoffice") == "entertainment_info_arb_boxoffice"
    assert model_name("nws", direction="T") == "weather_nws_observation_threshold"
    assert model_name("nws", direction="B") == "weather_nws_observation_bracket"


def test_source_monitor_research_fields_include_descriptor_and_inputs():
    _model_name, research_fields, _record_source_monitor_opportunity, _source = _load_source_monitor_provenance_helpers()

    fields = research_fields(
        source_kind="nws",
        observed_value=87.4,
        threshold=86,
        confidence=0.81234,
        source_name="KDCA",
        market_title="Will DC hit 86F or higher?",
        city="DC",
        direction="T",
        hour=15,
        observation_timestamp="2026-03-16T19:51:00Z",
        observation_age_minutes=12.5,
        running_high=87.4,
        current_temp=86.9,
    )

    assert fields["model_name"] == "weather_nws_observation_threshold"
    assert fields["model_descriptor"]["model_family"] == "weather"
    assert fields["model_descriptor"]["market_type"] == "threshold"
    assert fields["model_descriptor"]["city"] == "DC"
    assert fields["feature_snapshot_id"].startswith("weather:")
    assert fields["inline_model_inputs"]["running_high"] == 87.4
    assert fields["inline_model_inputs"]["observation_age_minutes"] == 12.5


def test_source_monitor_research_fields_snapshot_id_changes_with_inputs():
    _model_name, research_fields, _record_source_monitor_opportunity, _source = _load_source_monitor_provenance_helpers()

    base = research_fields(source_kind="boxoffice", observed_value=75_000_000, threshold=70_000_000, confidence=0.71)
    changed = research_fields(source_kind="boxoffice", observed_value=82_000_000, threshold=70_000_000, confidence=0.71)

    assert base["feature_snapshot_id"] != changed["feature_snapshot_id"]


def test_record_source_monitor_opportunity_sets_stage_and_rounds_edge():
    _model_name, _research_fields, record_source_monitor_opportunity, _source = _load_source_monitor_provenance_helpers()

    class StubOpportunityLog:
        def __init__(self):
            self.records = []

        def record(self, record):
            self.records.append(record)
            return record

    sink = StubOpportunityLog()
    record = record_source_monitor_opportunity(
        "KXHIGHDC-26MAR16-T86",
        "yes",
        "skipped",
        "edge_below_min",
        opportunity_stage="decision",
        opportunity_log_obj=sink,
        edge=0.123456,
        price_cents=44,
        model_name="weather_nws_observation_threshold",
    )

    assert record["opportunity_stage"] == "decision"
    assert record["edge"] == 0.1235
    assert sink.records[-1]["model_name"] == "weather_nws_observation_threshold"


def test_source_monitor_source_threads_research_fields_into_trade_and_opportunity_calls():
    _model_name, _research_fields, _record_source_monitor_opportunity, source = _load_source_monitor_provenance_helpers()

    assert "**research_fields" in source
    assert "_log_source_monitor_decision(" in source
    assert "OpportunityLog(" in source
