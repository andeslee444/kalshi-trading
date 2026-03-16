"""Focused tests for entertainment bot provenance helpers."""

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


def _load_entertainment_provenance_helpers():
    bot_path = resolve_bot_source_path("entertainment-bot.py")
    source = bot_path.read_text(encoding="utf-8")
    ns = {"json": json, "hashlib": hashlib, "re": re}
    exec(_extract_function_source(source, "_entertainment_model_name"), ns)
    exec(_extract_function_source(source, "_entertainment_research_fields"), ns)
    exec(_extract_function_source(source, "_record_entertainment_opportunity"), ns)
    return ns["_entertainment_model_name"], ns["_entertainment_research_fields"], ns["_record_entertainment_opportunity"], source


def test_entertainment_model_name_tracks_source_kind():
    model_name, _research_fields, _record_entertainment_opportunity, _source = _load_entertainment_provenance_helpers()

    assert model_name("album") == "entertainment_info_arb_album"
    assert model_name("boxoffice") == "entertainment_info_arb_boxoffice"


def test_entertainment_research_fields_include_descriptor_and_inputs():
    _model_name, research_fields, _record_entertainment_opportunity, _source = _load_entertainment_provenance_helpers()

    fields = research_fields(
        source_kind="album",
        observed_value=215000,
        threshold=180000,
        confidence=0.73,
        sigma=0.041,
        data_age_hours=6.5,
        source_name="hdd-hits-top-50",
        entity_name="Taylor Swift",
        market_title="Will Taylor Swift sell above 180K units?",
    )

    assert fields["model_name"] == "entertainment_info_arb_album"
    assert fields["model_descriptor"]["model_family"] == "entertainment"
    assert fields["model_descriptor"]["source_kind"] == "album"
    assert fields["feature_snapshot_id"].startswith("entertainment:")
    assert fields["inline_model_inputs"]["observed_value"] == 215000.0
    assert fields["inline_model_inputs"]["entity_name"] == "Taylor Swift"


def test_entertainment_research_fields_snapshot_id_changes_with_inputs():
    _model_name, research_fields, _record_entertainment_opportunity, _source = _load_entertainment_provenance_helpers()

    base = research_fields(source_kind="boxoffice", observed_value=75_000_000, threshold=70_000_000)
    changed = research_fields(source_kind="boxoffice", observed_value=85_000_000, threshold=70_000_000)

    assert base["feature_snapshot_id"] != changed["feature_snapshot_id"]


def test_record_entertainment_opportunity_sets_stage_and_rounds_edge():
    _model_name, _research_fields, record_entertainment_opportunity, _source = _load_entertainment_provenance_helpers()

    class StubOpportunityLog:
        def __init__(self):
            self.records = []

        def record(self, record):
            self.records.append(record)
            return record

    sink = StubOpportunityLog()
    record = record_entertainment_opportunity(
        "KXALBUMSALES-TEST",
        "yes",
        "skipped",
        "confidence below threshold",
        opportunity_stage="decision",
        opportunity_log_obj=sink,
        edge=0.123456,
        price_cents=44,
        model_name="entertainment_info_arb_album",
    )

    assert record["opportunity_stage"] == "decision"
    assert record["edge"] == 0.1235
    assert sink.records[-1]["model_name"] == "entertainment_info_arb_album"


def test_entertainment_bot_source_threads_research_fields_into_trade_and_opportunity_calls():
    _model_name, _research_fields, _record_entertainment_opportunity, source = _load_entertainment_provenance_helpers()

    assert "**research_fields" in source
    assert "_log_entertainment_decision(" in source
    assert "OpportunityLog(" in source

