"""Focused tests for economics bot provenance helpers."""

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


def _load_economics_provenance_helpers():
    bot_path = resolve_bot_source_path("economics-bot.py")
    source = bot_path.read_text(encoding="utf-8")
    ns = {"json": json, "hashlib": hashlib, "re": re}
    exec(_extract_function_source(source, "_economics_model_name"), ns)
    exec(_extract_function_source(source, "_economics_research_fields"), ns)
    exec(_extract_function_source(source, "_record_economics_opportunity"), ns)
    return ns["_economics_model_name"], ns["_economics_research_fields"], ns["_record_economics_opportunity"], source


def test_economics_model_name_tracks_source_kind():
    model_name, _research_fields, _record_economics_opportunity, _source = _load_economics_provenance_helpers()

    assert model_name("CPI") == "economics_nowcast_cpi"
    assert model_name("FED", source_kind="fedwatch") == "economics_fedwatch_fed"


def test_economics_research_fields_include_descriptor_and_inputs():
    _model_name, research_fields, _record_economics_opportunity, _source = _load_economics_provenance_helpers()

    fields = research_fields(
        market_type="CPI",
        threshold=3.0,
        direction_type="T",
        source_kind="nowcast",
        nowcast_value=2.83,
        posterior_sigma=0.1042,
        days_to_release=5,
        scenario_agreement=0.76,
        per_scenario={"base": 0.61, "hot": 0.22},
        scenario_weights={"base": 0.7, "hot": 0.3},
        nowcast_age_hours=4.5,
        data_source_timestamp="2026-03-16T03:00:00Z",
        truflation_cpi=2.9,
        tips_breakeven=2.45,
        gdpnow_value=2.1,
    )

    assert fields["model_name"] == "economics_nowcast_cpi"
    assert fields["model_descriptor"]["model_family"] == "economics"
    assert fields["model_descriptor"]["source_kind"] == "nowcast"
    assert fields["feature_snapshot_id"].startswith("economics:")
    assert fields["inline_model_inputs"]["scenario_weights"]["base"] == 0.7
    assert fields["inline_model_inputs"]["truflation_cpi"] == 2.9


def test_economics_research_fields_snapshot_id_changes_with_inputs():
    _model_name, research_fields, _record_economics_opportunity, _source = _load_economics_provenance_helpers()

    base = research_fields(market_type="GAS", threshold=3.5, direction_type="T", source_kind="gas", gas_price=3.45)
    changed = research_fields(market_type="GAS", threshold=3.5, direction_type="T", source_kind="gas", gas_price=3.65)

    assert base["feature_snapshot_id"] != changed["feature_snapshot_id"]


def test_record_economics_opportunity_sets_stage_and_rounds_edge():
    _model_name, _research_fields, record_economics_opportunity, _source = _load_economics_provenance_helpers()

    class StubOpportunityLog:
        def __init__(self):
            self.records = []

        def record(self, record):
            self.records.append(record)
            return record

    sink = StubOpportunityLog()
    record = record_economics_opportunity(
        "KXCPI-26APR-T3.0",
        "yes",
        "skipped",
        "edge below threshold (8.0%)",
        opportunity_stage="decision",
        opportunity_log_obj=sink,
        edge=0.123456,
        price_cents=44,
        model_name="economics_nowcast_cpi",
    )

    assert record["opportunity_stage"] == "decision"
    assert record["edge"] == 0.1235
    assert sink.records[-1]["model_name"] == "economics_nowcast_cpi"


def test_economics_bot_source_threads_research_fields_into_trade_and_opportunity_calls():
    _model_name, _research_fields, _record_economics_opportunity, source = _load_economics_provenance_helpers()

    assert "**opp.get(\"research_fields\", {})" in source
    assert "_log_economics_decision(" in source
    assert "OpportunityLog(" in source

