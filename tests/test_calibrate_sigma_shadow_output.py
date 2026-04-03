"""Tests for Phase-4-safe shadow output handling in calibrate-sigma.py."""

import json
import importlib.util
import sys
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "calibrate_sigma",
    str(Path(__file__).resolve().parent.parent / "scripts" / "calibrate-sigma.py"),
)
calibrate_sigma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calibrate_sigma)


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


@pytest.fixture(autouse=True)
def patch_atomic_writer(monkeypatch):
    monkeypatch.setattr(calibrate_sigma, "_atomic_write_json", _write_json)


def test_shadow_output_skips_backup_and_runtime_sync(tmp_path, monkeypatch):
    canonical_path = tmp_path / "config" / "calibration.json"
    backup_path = tmp_path / "config" / "calibration-backup.json"
    weather_config_path = tmp_path / "config" / "kalshi-config.json"
    weather_config_path.parent.mkdir(parents=True, exist_ok=True)
    weather_config_path.write_text(json.dumps({"ensemble": {"weights": {"gfs": 0.3}}}))

    monkeypatch.setattr(calibrate_sigma, "CALIBRATION_PATH", canonical_path)
    monkeypatch.setattr(calibrate_sigma, "CALIBRATION_BACKUP_PATH", backup_path)
    monkeypatch.setattr(calibrate_sigma, "WEATHER_CONFIG_PATH", weather_config_path)

    payload = {
        "generated_at": "2026-03-22T00:00:00Z",
        "ensemble": {"weights": {"gfs": 0.5, "ecmwf": 0.5}},
    }
    shadow_path = tmp_path / "shadow" / "weather-calibration.json"

    saved_path, is_canonical = calibrate_sigma._save_calibration_output(payload, shadow_path)

    assert saved_path == shadow_path
    assert is_canonical is False
    assert shadow_path.exists()
    assert json.loads(shadow_path.read_text()) == payload
    assert not canonical_path.exists()
    assert not backup_path.exists()
    assert json.loads(weather_config_path.read_text()) == {"ensemble": {"weights": {"gfs": 0.3}}}


def test_canonical_output_keeps_backup_and_runtime_sync(tmp_path, monkeypatch):
    canonical_path = tmp_path / "config" / "calibration.json"
    backup_path = tmp_path / "config" / "calibration-backup.json"
    weather_config_path = tmp_path / "config" / "kalshi-config.json"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(json.dumps({
        "generated_at": "old",
        "ensemble": {"weights": {"gfs": 0.1}},
        "sigma_updated_at": "older",
        "weather": {"bias_correction": {"generated_at": "bias-old"}},
    }))
    weather_config_path.write_text(json.dumps({"ensemble": {"weights": {"gfs": 0.3}}}))

    monkeypatch.setattr(calibrate_sigma, "CALIBRATION_PATH", canonical_path)
    monkeypatch.setattr(calibrate_sigma, "CALIBRATION_BACKUP_PATH", backup_path)
    monkeypatch.setattr(calibrate_sigma, "WEATHER_CONFIG_PATH", weather_config_path)

    payload = {
        "generated_at": "2026-03-22T00:00:00Z",
        "ensemble": {"weights": {"gfs": 0.6, "ecmwf": 0.4}},
    }

    saved_path, is_canonical = calibrate_sigma._save_calibration_output(payload)

    assert saved_path == canonical_path
    assert is_canonical is True
    assert canonical_path.exists()
    assert json.loads(canonical_path.read_text()) == {
        "generated_at": "2026-03-22T00:00:00Z",
        "ensemble": {"weights": {"gfs": 0.6, "ecmwf": 0.4}},
        "sigma_updated_at": "2026-03-22T00:00:00Z",
        "weather": {"bias_correction": {"generated_at": "bias-old"}},
    }
    assert backup_path.exists()
    assert json.loads(backup_path.read_text()) == {
        "generated_at": "old",
        "ensemble": {"weights": {"gfs": 0.1}},
        "sigma_updated_at": "older",
        "weather": {"bias_correction": {"generated_at": "bias-old"}},
    }
    assert json.loads(weather_config_path.read_text()) == {"ensemble": {"weights": {"gfs": 0.6, "ecmwf": 0.4}}}


def test_canonical_output_recovers_preserved_fields_from_backup(tmp_path, monkeypatch):
    canonical_path = tmp_path / "config" / "calibration.json"
    backup_path = tmp_path / "config" / "calibration-backup.json"
    weather_config_path = tmp_path / "config" / "kalshi-config.json"
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(json.dumps({
        "generated_at": "newer-but-incomplete",
        "ensemble": {"weights": {"gfs": 0.2}},
    }))
    backup_path.write_text(json.dumps({
        "generated_at": "older-complete",
        "sigma_updated_at": "older-sigma",
        "weather": {"bias_correction": {"generated_at": "bias-backup"}},
    }))
    weather_config_path.write_text(json.dumps({"ensemble": {"weights": {"gfs": 0.3}}}))

    monkeypatch.setattr(calibrate_sigma, "CALIBRATION_PATH", canonical_path)
    monkeypatch.setattr(calibrate_sigma, "CALIBRATION_BACKUP_PATH", backup_path)
    monkeypatch.setattr(calibrate_sigma, "WEATHER_CONFIG_PATH", weather_config_path)

    payload = {
        "generated_at": "2026-03-29T19:34:27Z",
        "ensemble": {"weights": {"gfs": 0.7, "ecmwf": 0.3}},
        "weather": {"n": 10},
    }

    calibrate_sigma._save_calibration_output(payload)
    saved = json.loads(canonical_path.read_text())

    assert saved["sigma_updated_at"] == "2026-03-29T19:34:27Z"
    assert saved["weather"]["n"] == 10
    assert saved["weather"]["bias_correction"] == {"generated_at": "bias-backup"}


def test_no_api_canonical_save_requires_explicit_override(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["calibrate-sigma.py", "--no-api", "--save"],
    )

    with pytest.raises(SystemExit):
        calibrate_sigma.main()
