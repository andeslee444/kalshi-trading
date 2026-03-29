"""Tests for the read-only weather observation pack script."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "weather_observation_pack",
    str(_SCRIPTS_DIR / "weather-observation-pack.py"),
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["weather_observation_pack"] = _mod
_spec.loader.exec_module(_mod)

from weather_observation_pack import (  # noqa: E402
    _age_summary,
    build_city_bias_conflicts,
    build_observation_pack,
    compute_historical_city_bias,
    compute_live_city_bias,
    compute_weather_execution_quality,
    compute_verification_source_mix,
    compute_weather_pnl_from_trades,
)


def test_age_summary_handles_iso_timestamps():
    now = datetime(2026, 3, 22, 12, 0, tzinfo=timezone.utc)
    summary = _age_summary("2026-03-20T12:00:00Z", now=now)
    assert summary["age_days"] == 2.0
    assert summary["age_hours"] == 48.0


def test_weather_pnl_uses_settlement_result_and_cost():
    trades = [
        {
            "ticker": "KXHIGHMIA-26MAR21-T82",
            "city": "MIA",
            "source_bot": "weather",
            "settlement_result": "won",
            "count": 6,
            "cost_cents": 474,
        },
        {
            "ticker": "KXHIGHLAX-26MAR21-T83",
            "city": "LAX",
            "source_bot": "weather",
            "settlement_result": "lost",
            "count": 6,
            "price_cents": 79,
        },
    ]
    pack = compute_weather_pnl_from_trades(trades)
    by_city = {row["city"]: row for row in pack["by_city"]}
    assert by_city["MIA"]["pnl_cents"] == 126
    assert by_city["LAX"]["pnl_cents"] == -474
    assert pack["overall"]["pnl_cents"] == -348


def test_verification_source_mix_counts_recent_rows():
    verified = [
        {"date": "2026-03-21", "actual_source": "nws_cli"},
        {"date": "2026-03-20", "actual_source": "iem_fallback"},
        {"date": "2026-03-01", "actual_source": "nws_cli"},
    ]
    now = datetime(2026, 3, 22, tzinfo=timezone.utc)
    summary = compute_verification_source_mix(verified, 7, now=now)
    assert summary["total"] == 2
    assert summary["counts"] == {"iem_fallback": 1, "nws_cli": 1}
    assert summary["shares"]["nws_cli"] == 0.5


def test_execution_quality_summarizes_weather_trades():
    trades = [
        {
            "source_bot": "weather",
            "city": "AUS",
            "status": "executed",
            "execution_style": "maker",
            "edge": 0.42,
            "bias_applied_f": 1.2,
            "bias_conflict": True,
            "bias_capped": False,
        },
        {
            "source_bot": "weather",
            "city": "AUS",
            "status": "resting",
            "execution_style": "taker",
            "edge": 0.18,
            "bias_applied_f": -0.8,
            "bias_conflict": False,
            "bias_capped": True,
        },
        {
            "source_bot": "crypto",
            "city": "AUS",
            "status": "executed",
        },
    ]
    quality = compute_weather_execution_quality(trades)
    assert quality["overall"]["trades"] == 2
    assert quality["overall"]["executed"] == 1
    assert quality["overall"]["resting"] == 1
    assert quality["overall"]["maker"] == 1
    assert quality["overall"]["bias_conflict"] == 1
    assert quality["per_city"]["AUS"]["avg_edge"] == 0.3


def test_observation_pack_includes_verification_source_mix_and_execution_quality(monkeypatch):
    payloads = {
        "financial-snapshot.json": {
            "realized_pnl": {"by_bot": {"weather": {"pnl_cents": 1250, "wins": 1, "losses": 0, "win_rate": 1.0}}}
        },
        "backtest-results.json": {"generated_at": "2026-03-21T00:00:00Z"},
        "calibration.json": {"generated_at": "2026-03-21T01:00:00Z", "sigma_updated_at": "2026-03-21T01:00:00Z"},
        "weather-verification.json": {
            "verified": [
                {"date": "2026-03-21", "actual_source": "nws_cli", "city": "AUS", "errors": {"gfs": -1.0}},
                {"date": "2026-03-20", "actual_source": "iem_fallback", "city": "AUS", "errors": {"gfs": -2.0}},
            ]
        },
        "kalshi-trades.json": [
            {
                "source_bot": "weather",
                "city": "AUS",
                "ticker": "KXHIGHAUS-26MAR21-T82",
                "status": "executed",
                "execution_style": "maker",
                "edge": 0.42,
                "bias_applied_f": 1.2,
                "bias_conflict": True,
                "bias_capped": False,
                "settlement_result": "won",
                "count": 2,
                "cost_cents": 150,
                "fee_cents": 3,
            }
        ],
        "weather-live-bias.json": {
            "per_city": {
                "AUS": {"gfs": {"bias": 8.0}},
            }
        },
    }

    def fake_load_json(path, default=None):
        return payloads.get(path.name, default)

    monkeypatch.setattr("weather_observation_pack._load_json", fake_load_json)
    pack = build_observation_pack(source_lookback_days=(7,), lookback_days=7, top_cities=3, now=datetime(2026, 3, 22, tzinfo=timezone.utc))

    assert pack["verification_source_mix"]["state_path"].endswith("weather-verification.json")
    assert pack["verification_source_mix"]["summaries"] == [
        {
            "lookback_days": 7,
            "total": 2,
            "counts": {"iem_fallback": 1, "nws_cli": 1},
            "shares": {"iem_fallback": 0.5, "nws_cli": 0.5},
        }
    ]
    assert pack["execution_quality"]["overall"]["trades"] == 1
    assert pack["execution_quality"]["overall"]["executed"] == 1
    assert pack["execution_quality"]["overall"]["maker_share"] == 1.0
    assert pack["execution_quality"]["per_city"]["AUS"]["bias_conflict"] == 1
    assert pack["sources"]["weather_bias"].endswith("weather-live-bias.json")
    assert pack["city_bias"]["top_conflicts"][0]["city"] == "AUS"


def test_city_bias_conflicts_rank_sign_flips_first():
    historical = {
        "LAX": {"bias_f": 7.44, "n_models": 4},
        "NY": {"bias_f": -0.19, "n_models": 4},
    }
    live = {
        "LAX": {"bias_f": 1.22, "n": 7},
        "NY": {"bias_f": 1.00, "n": 12},
    }
    city_pnl = {
        "LAX": {"pnl_cents": 67251, "trades": 36},
        "NY": {"pnl_cents": 4216, "trades": 32},
    }
    conflicts = build_city_bias_conflicts(historical, live, city_pnl, min_gap_f=4.0)
    assert conflicts[0]["city"] == "NY"
    assert conflicts[0]["sign_flip"] is True
    assert conflicts[0]["bias_conflict"] is True
    assert conflicts[1]["city"] == "LAX"


def test_observation_pack_honors_shadow_measurement_paths(monkeypatch, tmp_path):
    payloads = {
        "financial-snapshot.json": {"realized_pnl": {"by_bot": {"weather": {"pnl_cents": 1}}}},
        "weather-verification.json": {"verified": []},
        "kalshi-trades.json": [],
        "weather-live-bias.json": {"per_city": {"AUS": {"gfs": {"bias": 7.0}}}},
        "weather-backtest-results.json": {"generated_at": "2026-03-22T00:00:00Z"},
        "weather-calibration.json": {"generated_at": "2026-03-22T01:00:00Z", "sigma_updated_at": "2026-03-22T01:00:00Z"},
    }

    def fake_load_json(path, default=None):
        return payloads.get(path.name, default)

    monkeypatch.setattr("weather_observation_pack._load_json", fake_load_json)
    backtest_path = tmp_path / "weather-backtest-results.json"
    calibration_path = tmp_path / "weather-calibration.json"

    pack = build_observation_pack(
        source_lookback_days=(7,),
        backtest_results_path=backtest_path,
        calibration_path=calibration_path,
        now=datetime(2026, 3, 22, tzinfo=timezone.utc),
    )

    assert pack["sources"]["backtest_results"] == str(backtest_path)
    assert pack["sources"]["calibration"] == str(calibration_path)
    assert pack["freshness"]["backtest_results"]["path"] == str(backtest_path)
    assert pack["freshness"]["calibration"]["path"] == str(calibration_path)


def test_historical_and_live_bias_maps_reduce_to_city_floats():
    historical_data = {
        "per_city": {
            "MIA": {
                "gfs": {"bias": 8.0},
                "icon": {"bias": 10.0},
            }
        }
    }
    verified_rows = [
        {"record_kind": "snapshot", "city": "MIA", "date": "2026-03-21", "errors": {"gfs": -2.0, "icon": -4.0}},
        {"record_kind": "snapshot", "city": "MIA", "date": "2026-03-20", "errors": {"gfs": -3.0, "icon": -1.0}},
    ]
    now = datetime(2026, 3, 22, tzinfo=timezone.utc)
    hist = compute_historical_city_bias(historical_data)
    live = compute_live_city_bias(verified_rows, 7, now=now, min_samples=1)
    assert hist["MIA"]["bias_f"] == 9.0
    assert live["MIA"]["bias_f"] == -2.5
