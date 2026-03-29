"""Tests for the derived weather promotion candidate artifact."""

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "weather_promotion_candidates",
    str(_SCRIPTS_DIR / "weather-promotion-candidates.py"),
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["weather_promotion_candidates"] = _mod
_spec.loader.exec_module(_mod)

from weather_promotion_candidates import build_promotion_artifact, classify_city  # noqa: E402


def _observation_pack():
    return {
        "generated_at": "2026-03-23T00:47:47.383397+00:00",
        "city_pnl": {
            "by_city": [
                {"city": "PHIL", "pnl_cents": 81280, "settled": 18, "trades": 18, "win_rate": 0.778},
                {"city": "LAX", "pnl_cents": 67251, "settled": 36, "trades": 36, "win_rate": 0.694},
                {"city": "DEN", "pnl_cents": 53871, "settled": 34, "trades": 34, "win_rate": 0.676},
                {"city": "AUS", "pnl_cents": 4779, "settled": 26, "trades": 26, "win_rate": 0.731},
                {"city": "CHI", "pnl_cents": 4377, "settled": 33, "trades": 33, "win_rate": 0.576},
                {"city": "NY", "pnl_cents": 4216, "settled": 32, "trades": 32, "win_rate": 0.656},
                {"city": "MIA", "pnl_cents": 1602, "settled": 23, "trades": 23, "win_rate": 0.565},
                {"city": "HOU", "pnl_cents": 1257, "settled": 15, "trades": 15, "win_rate": 0.467},
                {"city": "BOS", "pnl_cents": 3948, "settled": 1, "trades": 1, "win_rate": 1.0},
            ]
        },
        "city_bias": {
            "conflicts": [
                {"city": "PHIL", "bias_conflict": True, "sign_flip": True, "gap_f": 9.12, "weather_pnl_cents": 81280, "weather_trades": 18, "live_samples": 8},
                {"city": "LAX", "bias_conflict": True, "sign_flip": False, "gap_f": 7.60, "weather_pnl_cents": 67251, "weather_trades": 36, "live_samples": 7},
                {"city": "DEN", "bias_conflict": True, "sign_flip": False, "gap_f": 17.32, "weather_pnl_cents": 53871, "weather_trades": 34, "live_samples": 9},
                {"city": "AUS", "bias_conflict": True, "sign_flip": True, "gap_f": 17.46, "weather_pnl_cents": 4779, "weather_trades": 26, "live_samples": 8},
                {"city": "CHI", "bias_conflict": True, "sign_flip": False, "gap_f": 7.25, "weather_pnl_cents": 4377, "weather_trades": 33, "live_samples": 9},
                {"city": "NY", "bias_conflict": True, "sign_flip": False, "gap_f": 4.87, "weather_pnl_cents": 4216, "weather_trades": 32, "live_samples": 12},
                {"city": "MIA", "bias_conflict": True, "sign_flip": True, "gap_f": 9.60, "weather_pnl_cents": 1602, "weather_trades": 23, "live_samples": 10},
                {"city": "HOU", "bias_conflict": True, "sign_flip": True, "gap_f": 11.74, "weather_pnl_cents": 1257, "weather_trades": 15, "live_samples": 9},
                {"city": "BOS", "bias_conflict": True, "sign_flip": True, "gap_f": 8.90, "weather_pnl_cents": 3948, "weather_trades": 1, "live_samples": 8},
            ]
        },
    }


def _city_audit_rows():
    return [
        {"city": "PHIL", "trade_count": 18, "executed_count": 12, "resting_count": 6, "maker_count": 11},
        {"city": "LAX", "trade_count": 36, "executed_count": 8, "resting_count": 15, "maker_count": 12},
        {"city": "DEN", "trade_count": 34, "executed_count": 1, "resting_count": 20, "maker_count": 19},
        {"city": "AUS", "trade_count": 26, "executed_count": 3, "resting_count": 12, "maker_count": 12},
        {"city": "MIA", "trade_count": 23, "executed_count": 1, "resting_count": 14, "maker_count": 15},
        {"city": "HOU", "trade_count": 15, "executed_count": 1, "resting_count": 1, "maker_count": 1},
        {"city": "BOS", "trade_count": 1, "executed_count": 0, "resting_count": 1, "maker_count": 0},
    ]


def test_classify_city_matches_expected_phase4_tiers():
    pack = _observation_pack()
    artifact = build_promotion_artifact(pack, city_audit_rows=_city_audit_rows())
    rows = {row["city"]: row for row in artifact["ranked_cities"]}

    assert classify_city(rows["PHIL"]) == "expand_after_refresh"
    assert classify_city(rows["LAX"]) == "expand_after_refresh"
    assert classify_city(rows["DEN"]) == "expand_after_refresh"
    assert classify_city(rows["MIA"]) == "tighten"
    assert classify_city(rows["HOU"]) == "tighten"
    assert classify_city(rows["BOS"]) == "shadow_only"
    assert classify_city(rows["NY"]) == "hold"


def test_build_promotion_artifact_ranks_expand_first_and_shadow_last():
    artifact = build_promotion_artifact(_observation_pack(), city_audit_rows=_city_audit_rows())
    ranked = artifact["ranked_cities"]

    assert [row["city"] for row in ranked[:3]] == ["PHIL", "LAX", "DEN"]
    assert ranked[0]["recommended_action"] == "expand_after_refresh"
    assert ranked[0]["executed_count"] == 12
    assert ranked[-1]["recommended_action"] == "shadow_only"
    assert ranked[-1]["city"] == "BOS"
    assert artifact["summary"]["counts_by_action"]["expand_after_refresh"] == 3
    assert artifact["summary"]["counts_by_action"]["tighten"] == 2
    assert artifact["summary"]["top_expand_after_refresh_candidates"] == ["PHIL", "LAX", "DEN"]


def test_cli_save_writes_derived_artifact(tmp_path):
    observation_pack_path = tmp_path / "weather-observation-pack.json"
    city_audit_path = tmp_path / "weather-city-audit.json"
    output_path = tmp_path / "weather-promotion-candidates.json"
    observation_pack_path.write_text(json.dumps(_observation_pack()))
    city_audit_path.write_text(json.dumps({"rows": _city_audit_rows()}))

    _mod.main([
        "--observation-pack-path",
        str(observation_pack_path),
        "--city-audit-path",
        str(city_audit_path),
        "--output",
        str(output_path),
        "--save",
    ])

    saved = json.loads(output_path.read_text())
    assert saved["artifact_type"] == "weather_promotion_candidates"
    assert saved["schema_version"] == 1
    assert saved["summary"]["top_expand_after_refresh_candidates"] == ["PHIL", "LAX", "DEN"]


def test_city_audit_supplies_conflict_fields_when_observation_pack_is_truncated():
    pack = _observation_pack()
    pack["city_bias"]["conflicts"] = []
    artifact = build_promotion_artifact(pack, city_audit_rows=[
        {
            "city": "PHIL",
            "bias_conflict": True,
            "sign_flip": True,
            "gap_f": 9.12,
            "hist_bias_f": 7.62,
            "live_bias_f": -1.5,
            "live_n": 8,
            "live_confidence": 0.8,
            "trade_count": 18,
        }
    ])
    rows = {row["city"]: row for row in artifact["ranked_cities"]}

    assert rows["PHIL"]["bias_conflict"] is True
    assert rows["PHIL"]["sign_flip"] is True
    assert rows["PHIL"]["gap_f"] == 9.12
    assert rows["PHIL"]["recommended_action"] == "expand_after_refresh"


def test_missing_city_audit_still_preserves_expand_after_refresh_when_conflicts_are_in_pack():
    artifact = build_promotion_artifact(_observation_pack(), city_audit_rows=[])
    rows = {row["city"]: row for row in artifact["ranked_cities"]}

    assert rows["PHIL"]["recommended_action"] == "expand_after_refresh"
    assert rows["LAX"]["recommended_action"] == "expand_after_refresh"
    assert rows["DEN"]["recommended_action"] == "expand_after_refresh"
