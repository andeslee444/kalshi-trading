"""Tests for the Phase-4-safe weather shadow refresh orchestrator."""

import json
import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "weather_shadow_refresh",
    SCRIPTS_DIR / "weather-shadow-refresh.py",
)
assert SPEC is not None and SPEC.loader is not None
shadow_refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shadow_refresh)


def _result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


@pytest.fixture()
def fake_runner(tmp_path):
    calls = []

    def _runner(command, capture_output=True, text=True):
        calls.append(command)
        script = Path(command[1]).name
        if script == "weather-observation-pack.py":
            payload = {"artifact_type": "weather_observation_pack", "generated_at": "2026-03-23T00:00:00+00:00"}
            if "--output" in command:
                output_index = command.index("--output") + 1
                output_path = Path(command[output_index])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(payload, indent=2) + "\n")
            return _result(stdout=json.dumps(payload))
        if script == "weather-verification-summary.py":
            payload = {"state_path": "x", "summaries": [{"lookback_days": 7, "total": 1, "counts": {"nws_cli": 1}, "shares": {"nws_cli": 1.0}}]}
            return _result(stdout=json.dumps(payload))
        if script == "weather-city-audit.py":
            payload = {"rows": [{"city": "PHIL", "bias_conflict": True, "sign_flip": True, "gap_f": 9.12, "hist_bias_f": 7.62, "live_bias_f": -1.5, "live_n": 8, "live_confidence": 0.8, "trade_count": 18}]}
            return _result(stdout=json.dumps(payload))
        if script == "weather-nws-crosscheck-audit.py":
            payload = {"overall": {"n": 1}, "per_city": {}, "per_days_out": {}}
            return _result(stdout=json.dumps(payload))
        if script == "weather-execution-audit.py":
            payload = {"overall": {"trades": 1}, "per_city": {"PHIL": {"trades": 1}}}
            return _result(stdout=json.dumps(payload))
        if script == "weather-intraday-feature-audit.py":
            payload = {
                "artifact_type": "weather_intraday_feature_audit",
                "schema_version": 1,
                "summary": {"convective_risk_cities": ["MIA"]},
                "per_city": {"MIA": {"risk_flags": ["convective_risk"]}},
            }
            return _result(stdout=json.dumps(payload))
        if script == "backtest.py":
            output_index = command.index("--output") + 1
            output_path = Path(command[output_index])
            payload = {"generated_at": "2026-03-23T00:00:00", "n_evaluated": 1, "per_bot": {"weather": {"n_evaluated": 1}}}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2) + "\n")
            return _result(stdout=json.dumps(payload))
        if script == "calibrate-sigma.py":
            output_index = command.index("--output") + 1
            output_path = Path(command[output_index])
            payload = {"generated_at": "2026-03-23T00:00:00", "weather": {"n": 1, "global_brier": 0.25, "global_sigma_intercept": 5.0, "global_sigma_slope": 1.0}}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2) + "\n")
            return _result(stdout=json.dumps(payload))
        if script == "weather-promotion-candidates.py":
            output_index = command.index("--output") + 1
            output_path = Path(command[output_index])
            payload = {"artifact_type": "weather_promotion_candidates", "summary": {"top_expand_after_refresh_candidates": ["PHIL", "LAX", "DEN"]}}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2) + "\n")
            return _result(stdout=json.dumps(payload))
        if script == "backfill-weather-data.py":
            db_index = command.index("--db-path") + 1
            db_path = Path(command[db_index])
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE training_pairs (
                    city TEXT,
                    date TEXT,
                    model TEXT,
                    lead_days INTEGER,
                    member_id INTEGER,
                    forecast_temp REAL,
                    actual_temp_cli REAL,
                    market_outcome TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO training_pairs
                (city, date, model, lead_days, member_id, forecast_temp, actual_temp_cli, market_outcome)
                VALUES ('AUS', '2026-03-22', 'gfs', 0, 0, 82.0, 80.0, NULL)
                """
            )
            conn.commit()
            conn.close()
            return _result(stdout="backfilled")
        if script == "calibrate-weather-bias.py":
            output_index = command.index("--output") + 1
            output_path = Path(command[output_index])
            payload = {"generated_at": "2026-03-23T00:00:00", "lead_time_matched": True}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2) + "\n")
            return _result(stdout=json.dumps(payload))
        raise AssertionError(f"Unexpected command: {command}")

    return calls, _runner


def test_refresh_writes_shadow_bundle_without_shadow_prior(tmp_path, fake_runner, monkeypatch):
    calls, runner = fake_runner
    output_dir = tmp_path / "shadow-refresh"
    monkeypatch.setattr(shadow_refresh, "DEFAULT_SHADOW_ROOT", tmp_path)

    summary = shadow_refresh.refresh_weather_shadow_artifacts(output_dir=output_dir, runner=runner)

    assert summary["status"] == "ok"
    assert (output_dir / "weather-observation-pack.json").exists()
    assert (output_dir / "weather-city-audit.json").exists()
    assert (output_dir / "weather-intraday-feature-audit.json").exists()
    assert (output_dir / "weather-backtest-results.json").exists()
    assert (output_dir / "weather-calibration.json").exists()
    assert (output_dir / "weather-promotion-candidates.json").exists()
    assert not (output_dir / "weather-training.db").exists()
    assert not (output_dir / "weather-live-bias.json").exists()
    assert any(Path(cmd[1]).name == "weather-observation-pack.py" for cmd in calls)
    assert not any(Path(cmd[1]).name == "backfill-weather-data.py" for cmd in calls)
    obs_cmd = next(cmd for cmd in calls if Path(cmd[1]).name == "weather-observation-pack.py")
    assert "--backtest-results-path" in obs_cmd
    assert "--calibration-path" in obs_cmd
    assert "--weather-bias-path" not in obs_cmd
    assert calls.index(obs_cmd) > max(
        calls.index(cmd)
        for cmd in calls
        if Path(cmd[1]).name in {"backtest.py", "calibrate-sigma.py"}
    )


def test_refresh_can_include_shadow_prior(tmp_path, fake_runner, monkeypatch):
    calls, runner = fake_runner
    output_dir = tmp_path / "shadow-refresh"
    monkeypatch.setattr(shadow_refresh, "DEFAULT_SHADOW_ROOT", tmp_path)

    summary = shadow_refresh.refresh_weather_shadow_artifacts(
        output_dir=output_dir,
        refresh_shadow_prior=True,
        runner=runner,
    )

    assert summary["status"] == "ok"
    assert (output_dir / "weather-training.db").exists()
    assert (output_dir / "weather-live-bias.json").exists()
    assert any(Path(cmd[1]).name == "backfill-weather-data.py" for cmd in calls)
    assert any(Path(cmd[1]).name == "calibrate-weather-bias.py" for cmd in calls)
    backfill_cmd = next(cmd for cmd in calls if Path(cmd[1]).name == "backfill-weather-data.py")
    assert "gfs,ecmwf,icon,gem,graphcast,nbm" in backfill_cmd
    obs_cmd = next(cmd for cmd in calls if Path(cmd[1]).name == "weather-observation-pack.py")
    assert "--weather-bias-path" in obs_cmd


def test_cli_dry_run_prints_plan(capsys):
    shadow_refresh.main(["--dry-run", "--output-dir", "data/shadow/weather-refresh"])
    output = capsys.readouterr().out
    assert "Would refresh shadow weather artifacts" in output


def test_non_shadow_output_dir_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        shadow_refresh.refresh_weather_shadow_artifacts(output_dir=tmp_path / "not-shadow")


def test_empty_shadow_prior_db_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow_refresh, "DEFAULT_SHADOW_ROOT", tmp_path)
    output_dir = tmp_path / "shadow-refresh"

    def runner(command, capture_output=True, text=True):
        script = Path(command[1]).name
        if script == "weather-verification-summary.py":
            return _result(stdout=json.dumps({"summaries": []}))
        if script == "weather-city-audit.py":
            return _result(stdout=json.dumps({"rows": []}))
        if script == "weather-nws-crosscheck-audit.py":
            return _result(stdout=json.dumps({"overall": {"n": 0}, "per_city": {}, "per_days_out": {}}))
        if script == "weather-execution-audit.py":
            return _result(stdout=json.dumps({"overall": {"trades": 0}, "per_city": {}}))
        if script == "weather-intraday-feature-audit.py":
            return _result(stdout=json.dumps({
                "artifact_type": "weather_intraday_feature_audit",
                "schema_version": 1,
                "summary": {"convective_risk_cities": []},
                "per_city": {},
            }))
        if script == "backtest.py":
            output_path = Path(command[command.index("--output") + 1])
            payload = {"generated_at": "2026-03-23T00:00:00", "n_evaluated": 1, "per_bot": {"weather": {"n_evaluated": 1}}}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload))
            return _result(stdout=json.dumps(payload))
        if script == "calibrate-sigma.py":
            output_path = Path(command[command.index("--output") + 1])
            payload = {"generated_at": "2026-03-23T00:00:00", "weather": {"n": 1, "global_brier": 0.25, "global_sigma_intercept": 5.0, "global_sigma_slope": 1.0}}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload))
            return _result(stdout=json.dumps(payload))
        if script == "weather-observation-pack.py":
            output_path = Path(command[command.index("--output") + 1])
            payload = {"artifact_type": "weather_observation_pack", "generated_at": "2026-03-23T00:00:00+00:00"}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload))
            return _result(stdout=json.dumps(payload))
        if script == "weather-promotion-candidates.py":
            output_path = Path(command[command.index("--output") + 1])
            payload = {"artifact_type": "weather_promotion_candidates", "summary": {"top_expand_after_refresh_candidates": []}}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload))
            return _result(stdout=json.dumps(payload))
        if script == "backfill-weather-data.py":
            db_path = Path(command[command.index("--db-path") + 1])
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE training_pairs (
                    city TEXT,
                    date TEXT,
                    model TEXT,
                    lead_days INTEGER,
                    member_id INTEGER,
                    forecast_temp REAL,
                    actual_temp_cli REAL,
                    market_outcome TEXT
                )
                """
            )
            conn.commit()
            conn.close()
            return _result(stdout="backfilled")
        raise AssertionError(f"Unexpected command: {command}")

    summary = shadow_refresh.refresh_weather_shadow_artifacts(
        output_dir=output_dir,
        refresh_shadow_prior=True,
        runner=runner,
    )

    assert summary["status"] == "failed"
    backfill_step = next(step for step in summary["steps"] if step["name"] == "shadow_prior_backfill")
    assert "zero training_pairs" in backfill_step["stderr"]
