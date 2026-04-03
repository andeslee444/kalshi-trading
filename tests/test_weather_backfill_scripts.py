import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_script(filename, module_name):
    path = PROJECT_DIR / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill_mod = _load_script("backfill-weather-data.py", "backfill_weather_data")
calibrate_mod = _load_script("calibrate-historical.py", "calibrate_historical")


class TestBackfillWeatherDataScript:
    def test_parse_previous_runs_daily_extracts_current_and_previous_day(self):
        payload = {
            "hourly": {
                "time": [
                    "2026-03-14T00:00",
                    "2026-03-14T12:00",
                    "2026-03-15T00:00",
                    "2026-03-15T12:00",
                ],
                "temperature_2m": [72.0, 80.0, 75.0, 81.0],
                "temperature_2m_previous_day1": [71.0, 79.0, None, None],
            }
        }

        parsed = backfill_mod._parse_previous_runs_daily(payload)

        assert parsed == {
            0: {
                "2026-03-14": 80.0,
                "2026-03-15": 81.0,
            },
            1: {
                "2026-03-14": 79.0,
            },
        }

    def test_fetch_previous_runs_forecasts_uses_city_timezone_and_premium_alias(self, monkeypatch):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "hourly": {
                "time": ["2026-03-14T00:00", "2026-03-14T12:00"],
                "temperature_2m": [65.0, 70.0],
                "temperature_2m_previous_day1": [63.0, 68.0],
            }
        }
        seen = {}

        def fake_retry(method, url, timeout=15, max_retries=2):
            seen["method"] = method
            seen["url"] = url
            return response

        monkeypatch.setattr(backfill_mod, "retry_request", fake_retry)
        monkeypatch.setenv("OPEN_METEO_API_KEY", "premium-key")

        parsed = backfill_mod.fetch_previous_runs_forecasts(
            39.8561,
            -104.6737,
            7,
            "nbm_conus",
            city_code="DEN",
        )

        assert seen["method"] == "GET"
        assert "customer-previous-runs-api.open-meteo.com" in seen["url"]
        assert "timezone=America%2FDenver" in seen["url"]
        assert "hourly=temperature_2m,temperature_2m_previous_day1" in seen["url"]
        assert "models=ncep_nbm_conus" in seen["url"]
        assert parsed[0]["2026-03-14"] == 70.0
        assert parsed[1]["2026-03-14"] == 68.0

    def test_fetch_previous_runs_forecasts_strict_raises_with_context(self, monkeypatch):
        response = MagicMock()
        response.status_code = 503

        def fake_retry(method, url, timeout=15, max_retries=2):
            return response

        monkeypatch.setattr(backfill_mod, "retry_request", fake_retry)

        with pytest.raises(RuntimeError, match=r"city=DEN.*model=nbm_conus.*status=503"):
            backfill_mod.fetch_previous_runs_forecasts(
                39.8561,
                -104.6737,
                7,
                "nbm_conus",
                city_code="DEN",
                strict=True,
            )


class TestCalibrateHistoricalScript:
    def test_fetch_historical_forecasts_uses_city_timezone(self, monkeypatch):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "daily": {
                "time": ["2026-03-14"],
                "temperature_2m_max": [74.0],
            }
        }
        seen = {}

        def fake_retry(method, url, timeout=30, max_retries=2):
            seen["method"] = method
            seen["url"] = url
            return response

        monkeypatch.setattr(calibrate_mod, "retry_request", fake_retry)

        parsed = calibrate_mod.fetch_historical_forecasts(
            33.9425,
            -118.4081,
            "2026-03-01",
            "2026-03-14",
            "gfs_seamless",
            city_code="LAX",
        )

        assert seen["method"] == "GET"
        assert "timezone=America%2FLos_Angeles" in seen["url"]
        assert parsed["2026-03-14"] == 74.0
