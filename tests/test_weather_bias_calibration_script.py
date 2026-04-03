import importlib.util
from pathlib import Path

import pytest


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_script(filename, module_name):
    path = PROJECT_DIR / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calibrate_live_mod = _load_script("calibrate-weather-bias.py", "calibrate_weather_bias")
backfill_mod = _load_script("backfill-weather-data.py", "backfill_weather_data_for_bias_tests")


class TestCalibrateWeatherBiasScript:
    def test_build_bias_artifact_marks_output_live_safe(self):
        rows = [
            {
                "city": "MIA",
                "date": "2026-03-10",
                "model": "gfs",
                "lead_days": 0,
                "forecast_temp": 84.0,
                "actual_temp_cli": 82.0,
            },
            {
                "city": "MIA",
                "date": "2026-03-11",
                "model": "gfs",
                "lead_days": 1,
                "forecast_temp": 81.0,
                "actual_temp_cli": 80.0,
            },
            {
                "city": "MIA",
                "date": "2026-03-10",
                "model": "icon",
                "lead_days": 0,
                "forecast_temp": 83.0,
                "actual_temp_cli": 82.0,
            },
            {
                "city": "MIA",
                "date": "2026-03-11",
                "model": "icon",
                "lead_days": 1,
                "forecast_temp": 82.0,
                "actual_temp_cli": 80.0,
            },
            {
                "city": "DEN",
                "date": "2026-03-10",
                "model": "gfs",
                "lead_days": 0,
                "forecast_temp": 70.0,
                "actual_temp_cli": 67.0,
            },
            {
                "city": "DEN",
                "date": "2026-03-11",
                "model": "gfs",
                "lead_days": 1,
                "forecast_temp": 68.0,
                "actual_temp_cli": 66.0,
            },
        ]

        artifact = calibrate_live_mod.build_bias_artifact(
            rows,
            min_samples=2,
            min_lead_days=0,
            max_lead_days=1,
            source_db="data/weather-training.db",
        )

        assert artifact["source"] == "previous_runs_training"
        assert artifact["lead_time_matched"] is True
        assert artifact["actuals_source"] == "settlement"
        assert artifact["lead_days"] == {"min": 0, "max": 1}
        assert artifact["per_city"]["MIA"]["gfs"]["bias"] == 1.5
        assert artifact["per_city"]["DEN"]["gfs"]["bias"] == 2.5
        assert artifact["global"]["gfs"]["n"] == 4

    def test_build_bias_artifact_ignores_null_actuals(self):
        rows = [
            {
                "city": "MIA",
                "date": "2026-03-10",
                "model": "gfs",
                "lead_days": 0,
                "forecast_temp": 84.0,
                "actual_temp_cli": None,
            },
            {
                "city": "MIA",
                "date": "2026-03-11",
                "model": "gfs",
                "lead_days": 0,
                "forecast_temp": 82.0,
                "actual_temp_cli": 80.0,
            },
        ]

        artifact = calibrate_live_mod.build_bias_artifact(rows, min_samples=1)

        assert artifact["n_forecasts"] == 1
        assert artifact["per_city"]["MIA"]["gfs"]["bias"] == 2.0

    def test_validate_bias_artifact_rejects_empty_output(self, tmp_path):
        artifact = {"n_forecasts": 0}

        with pytest.raises(SystemExit, match="Refusing to write empty weather-live-bias artifact"):
            calibrate_live_mod.validate_bias_artifact(
                artifact,
                output_path=tmp_path / "weather-live-bias.json",
            )

    def test_validate_bias_artifact_can_allow_empty_output(self, tmp_path):
        artifact = {"n_forecasts": 0}

        assert calibrate_live_mod.validate_bias_artifact(
            artifact,
            output_path=tmp_path / "weather-live-bias.json",
            allow_empty=True,
        ) == artifact


class TestBackfillWeatherDataEnhancements:
    def test_model_map_exposes_supported_previous_runs_models(self):
        assert backfill_mod.MODEL_MAP["gfs"] == "gfs_seamless"
        assert backfill_mod.MODEL_MAP["ecmwf"] == "ecmwf_ifs025"
        assert backfill_mod.MODEL_MAP["icon"] == "icon_seamless"
        assert backfill_mod.MODEL_MAP["gem"] == "gem_global"
        assert backfill_mod.MODEL_MAP["nbm"] == "nbm_conus"
        assert backfill_mod.MODEL_MAP["aifs"] == "ecmwf_aifs025"
        assert backfill_mod.MODEL_MAP["graphcast"] == "gfs_graphcast025"
