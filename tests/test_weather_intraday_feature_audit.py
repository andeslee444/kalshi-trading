import importlib.util
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _load_script(filename, module_name):
    path = PROJECT_DIR / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_mod = _load_script("weather-intraday-feature-audit.py", "weather_intraday_feature_audit")


class StubFetcher:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch_same_day_summary(self, lat, lon, *, city_code=None, target_date=None, forecast_hours=24):
        self.calls.append((city_code, target_date, forecast_hours))
        return self.payloads.get(city_code)


def test_build_intraday_feature_audit_marks_risk_flags(monkeypatch):
    monkeypatch.setattr(
        audit_mod,
        "_load_cities",
        lambda: {
            "MIA": {"lat": 25.79, "lon": -80.29, "name": "Miami"},
            "LAX": {"lat": 33.94, "lon": -118.41, "name": "Los Angeles"},
        },
    )
    fetcher = StubFetcher(
        {
            "MIA": {
                "date": "2026-04-02",
                "samples": 10,
                "time_window": {"start": "2026-04-02T00:00", "end": "2026-04-02T02:15"},
                "source": "open_meteo_hrrr_minutely_15",
                "model": "hrrr",
                "temperature_max_f": 84.0,
                "temperature_min_f": 81.5,
                "temperature_latest_f": 83.0,
                "temperature_peak_time": "2026-04-02T01:15",
                "cape_max_jkg": 1450.0,
                "precipitation_total": 0.11,
                "cloud_cover_mean_pct": 76.0,
                "cloud_cover_max_pct": 96.0,
                "dewpoint_max_f": 71.0,
                "units": {},
            },
            "LAX": {
                "date": "2026-04-02",
                "samples": 8,
                "time_window": {"start": "2026-04-02T00:00", "end": "2026-04-02T01:45"},
                "source": "open_meteo_hrrr_minutely_15",
                "model": "hrrr",
                "temperature_max_f": 68.0,
                "temperature_min_f": 60.0,
                "temperature_latest_f": 66.0,
                "temperature_peak_time": "2026-04-02T01:30",
                "cape_max_jkg": 100.0,
                "precipitation_total": 0.0,
                "cloud_cover_mean_pct": 25.0,
                "cloud_cover_max_pct": 40.0,
                "dewpoint_max_f": 55.0,
                "units": {},
            },
        }
    )

    artifact = audit_mod.build_intraday_feature_audit(target_date="2026-04-02", fetcher=fetcher)

    assert artifact["artifact_type"] == "weather_intraday_feature_audit"
    assert artifact["n_cities"] == 2
    assert artifact["per_city"]["MIA"]["risk_flags"] == [
        "convective_risk",
        "cloud_cap_risk",
        "flat_diurnal_range",
    ]
    assert artifact["summary"]["convective_risk_cities"] == ["MIA"]
    assert artifact["summary"]["cloud_cap_risk_cities"] == ["MIA"]
    assert artifact["summary"]["flat_diurnal_range_cities"] == ["MIA"]
