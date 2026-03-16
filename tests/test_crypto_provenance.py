"""Focused tests for crypto bot provenance helpers."""

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


def _load_crypto_provenance_helpers():
    bot_path = resolve_bot_source_path("crypto-bot.py")
    source = bot_path.read_text(encoding="utf-8")
    ns = {"json": json, "hashlib": hashlib, "re": re}
    exec(_extract_function_source(source, "_crypto_model_name"), ns)
    exec(_extract_function_source(source, "_crypto_research_fields"), ns)
    exec(_extract_function_source(source, "_record_crypto_opportunity"), ns)
    return ns["_crypto_model_name"], ns["_crypto_research_fields"], ns["_record_crypto_opportunity"], source


def test_crypto_model_name_tracks_market_type_and_ou():
    model_name, _research_fields, _record_crypto_opportunity, _source = _load_crypto_provenance_helpers()

    parsed = {"market_type": "hourly", "direction": "T"}
    assert model_name(parsed, use_ou=True) == "crypto_ensemble_hourly_ou_particle_filter"
    assert model_name({"direction": "B"}, use_ou=False) == "crypto_ensemble_bracket_gbm_particle_filter"


def test_crypto_research_fields_include_descriptor_and_inputs():
    _model_name, research_fields, _record_crypto_opportunity, _source = _load_crypto_provenance_helpers()

    class FilteredEstimate:
        prob = 0.61234
        ci_low = 0.55
        ci_high = 0.66
        trend = "up"
        n_updates = 8

    fields = research_fields(
        {"asset": "BTC", "date": "2026-03-15", "direction": "T", "threshold": 84500.0, "market_type": "hourly", "settlement_hour": 17},
        current_price=84250.12,
        minutes_to_settle=95,
        vol_used=0.54321,
        raw_prob=0.59876,
        filtered_est=FilteredEstimate(),
        current_regime="trending",
        drift_pct=0.12,
        iv=0.51,
        rv=0.49,
        garch_forecast=0.55,
        season_mult=1.1,
        skew_mult=0.95,
        use_ou=True,
        ou_half_life_minutes=120,
        ou_target=84000.0,
        ou_shadow_prob=0.605,
        heston_params={"v0": 0.25, "xi": 0.3},
    )

    assert fields["model_name"] == "crypto_ensemble_hourly_ou_particle_filter"
    assert fields["model_descriptor"]["model_family"] == "crypto"
    assert fields["model_descriptor"]["asset"] == "BTC"
    assert fields["model_descriptor"]["regime"] == "trending"
    assert fields["feature_snapshot_id"].startswith("crypto:")
    assert fields["inline_model_inputs"]["filtered_prob"] == 0.61234
    assert fields["inline_model_inputs"]["ou_shadow_prob"] == 0.605


def test_crypto_research_fields_snapshot_id_changes_with_inputs():
    _model_name, research_fields, _record_crypto_opportunity, _source = _load_crypto_provenance_helpers()

    class FilteredEstimate:
        prob = 0.5
        ci_low = 0.45
        ci_high = 0.55
        trend = "flat"
        n_updates = 3

    base = research_fields(
        {"asset": "ETH", "date": "2026-03-15", "direction": "T", "threshold": 3500.0},
        current_price=3490.0,
        minutes_to_settle=120,
        filtered_est=FilteredEstimate(),
    )
    changed = research_fields(
        {"asset": "ETH", "date": "2026-03-15", "direction": "T", "threshold": 3500.0},
        current_price=3510.0,
        minutes_to_settle=120,
        filtered_est=FilteredEstimate(),
    )

    assert base["feature_snapshot_id"] != changed["feature_snapshot_id"]


def test_record_crypto_opportunity_sets_stage_and_rounds_edge():
    _model_name, _research_fields, record_crypto_opportunity, _source = _load_crypto_provenance_helpers()

    class StubOpportunityLog:
        def __init__(self):
            self.records = []

        def record(self, record):
            self.records.append(record)
            return record

    sink = StubOpportunityLog()
    record = record_crypto_opportunity(
        "KXBTC-26MAR1517-T84500",
        "yes",
        "skipped",
        "net edge below threshold",
        opportunity_stage="decision",
        opportunity_log_obj=sink,
        edge=0.123456,
        price_cents=44,
        model_name="crypto_ensemble_hourly_gbm_particle_filter",
    )

    assert record["opportunity_stage"] == "decision"
    assert record["edge"] == 0.1235
    assert sink.records[-1]["model_name"] == "crypto_ensemble_hourly_gbm_particle_filter"


def test_crypto_bot_source_threads_research_fields_into_trade_and_opportunity_calls():
    _model_name, _research_fields, _record_crypto_opportunity, source = _load_crypto_provenance_helpers()

    assert "**research_fields" in source
    assert '**opp.get("research_fields", {})' in source
    assert "_log_crypto_decision(" in source

