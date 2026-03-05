"""Forecast verification pipeline for weather bot adaptive calibration.

Records forecasts, verifies against actual temperatures from Open-Meteo
historical API, and provides verification data for adaptive ensemble
weights and city-level bias estimation.
"""

import json
import logging
import datetime
from pathlib import Path

_log = logging.getLogger("forecast_verifier")

# Canonical mapping of Kalshi city codes to IEM ASOS station IDs
DEFAULT_STATION_MAP = {
    "MIA": "KMIA",
    "LAX": "KLAX",
    "PHIL": "KPHL",
    "NY": "KNYC",
    "CHI": "KMDW",
    "AUS": "KAUS",
    "DEN": "KDEN",
    "HOU": "KHOU",
    "ATL": "KATL",
    "BOS": "KBOS",
    "SFO": "KSFO",
    "SEA": "KSEA",
    "LV": "KLAS",
    "DAL": "KDFW",
    "MIN": "KMSP",
    "PHX": "KPHX",
    "DC": "KDCA",
    "NOLA": "KMSY",
    "OKC": "KOKC",
    "SATX": "KSAT",
}


class ForecastVerifier:
    """Tracks forecast accuracy and provides adaptive calibration data.

    Lifecycle:
    1. record_forecast(city, date, model_forecasts, ...) -- called each scan
    2. verify_past_forecasts(city_coords) -- checks if past forecasts can be verified
    3. get_verification_summary() -- returns data for adaptive ensemble weights
    4. get_city_bias() -- returns systematic bias per city for skew parameter
    """

    def __init__(self, state_path, logger=None):
        """
        Args:
            state_path: Path to data/weather-verification.json
            logger: optional logger (defaults to module logger)
        """
        self.state_path = Path(state_path)
        self.log = logger or _log
        self.state = {
            "pending": [],
            "verified": [],
            "stats": {"last_verification": None, "total_verified": 0},
        }

    def record_forecast(self, city, date_str, model_forecasts, threshold=None,
                        model_prob=None, hour_of_day=None):
        """Record a forecast for later verification.

        Args:
            city: city code (e.g. "MIA")
            date_str: date string in YYYY-MM-DD format
            model_forecasts: dict of {model_name: temp_f}
            threshold: optional market threshold for Brier scoring
            model_prob: optional model probability for Brier scoring
            hour_of_day: optional hour when forecast was made

        Deduplicates by (city, date) -- keeps latest forecast for each.
        """
        if not model_forecasts or not isinstance(model_forecasts, dict):
            return

        # Remove any existing pending forecast for same (city, date)
        self.state["pending"] = [
            p for p in self.state["pending"]
            if not (p["city"] == city and p["date"] == date_str)
        ]

        record = {
            "city": city,
            "date": date_str,
            "models": {k: v for k, v in model_forecasts.items() if v is not None},
            "recorded_at": datetime.datetime.utcnow().isoformat(),
        }
        if threshold is not None:
            record["threshold"] = threshold
        if model_prob is not None:
            record["model_prob"] = model_prob
        if hour_of_day is not None:
            record["hour_of_day"] = hour_of_day

        self.state["pending"].append(record)

    def verify_past_forecasts(self, city_coords=None, station_map=None):
        """Check forecasts from 2+ days ago against actual temperatures.

        Fetches actual high from IEM ASOS station data. Rate-limits
        to max 5 API calls per scan to avoid hammering the API.

        Args:
            station_map: dict of {city_code: station_id} (e.g. {"NY": "KNYC"}).
                Preferred parameter. Uses IEM ASOS for actual temps.
            city_coords: DEPRECATED. dict of {city_code: {"lat": float, "lon": float}}.
                If station_map is not provided and city_coords is given, logs a
                deprecation warning and skips verification.
        """
        if station_map is None and city_coords is not None:
            self.log.warning(
                "verify_past_forecasts(city_coords=...) is deprecated. "
                "Pass station_map={city: station_id} instead. Skipping verification."
            )
            return

        if station_map is None:
            self.log.debug("No station_map provided, skipping verification")
            return

        today = datetime.date.today()
        cutoff = today - datetime.timedelta(days=2)

        # Find pending forecasts old enough to verify
        to_verify = []
        remaining = []
        for record in self.state["pending"]:
            try:
                forecast_date = datetime.date.fromisoformat(record["date"])
            except (ValueError, TypeError):
                remaining.append(record)
                continue

            if forecast_date <= cutoff:
                to_verify.append(record)
            else:
                remaining.append(record)

        if not to_verify:
            return

        # Rate limit: max 5 API calls per scan
        api_calls = 0
        max_calls = 5
        verified_this_scan = []

        for record in to_verify:
            if api_calls >= max_calls:
                remaining.append(record)  # Put back for next scan
                continue

            city = record["city"]
            if city not in station_map:
                self.log.debug("No station mapping for city %s, skipping verification", city)
                remaining.append(record)
                continue

            station_id = station_map[city]
            actual_high = self._fetch_actual_high(station_id, record["date"])
            api_calls += 1

            if actual_high is None:
                # API failed or data not available yet -- try again next scan
                remaining.append(record)
                continue

            # Compute errors per model
            errors = {}
            for model_name, forecast_temp in record.get("models", {}).items():
                if forecast_temp is not None:
                    errors[model_name] = round(forecast_temp - actual_high, 2)

            verified_record = {
                "city": city,
                "date": record["date"],
                "models": record.get("models", {}),
                "actual_high": actual_high,
                "errors": errors,
                "recorded_at": record.get("recorded_at"),
                "verified_at": datetime.datetime.utcnow().isoformat(),
            }
            if "threshold" in record:
                verified_record["threshold"] = record["threshold"]
            if "model_prob" in record:
                verified_record["model_prob"] = record["model_prob"]

            verified_this_scan.append(verified_record)

        self.state["pending"] = remaining
        self.state["verified"].extend(verified_this_scan)
        self.state["stats"]["total_verified"] = len(self.state["verified"])
        if verified_this_scan:
            self.state["stats"]["last_verification"] = datetime.datetime.utcnow().isoformat()
            self.log.info("Verified %d forecasts (total: %d)",
                         len(verified_this_scan), len(self.state["verified"]))

    def _fetch_actual_high(self, station_id, date_str):
        """Fetch actual high temperature from IEM ASOS station data.

        Uses Iowa Environmental Mesonet (IEM) for official ASOS observations,
        which match how Kalshi settles weather markets.

        Args:
            station_id: IEM ASOS station ID (e.g. "KNYC", "KMIA").
            date_str: date in YYYY-MM-DD format.

        Returns:
            Temperature in Fahrenheit (float), or None on failure.
            Non-blocking: logs warning and returns None on any error.
        """
        try:
            from kalshi_auth import retry_request
        except ImportError:
            self.log.warning("Cannot import retry_request, skipping verification")
            return None

        try:
            parts = date_str.split("-")
            year, month, day = parts[0], parts[1], parts[2]
        except (IndexError, ValueError):
            self.log.warning("Invalid date format: %s", date_str)
            return None

        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
            f"station={station_id}&data=max_tmpf&tz=America/New_York"
            f"&format=comma&year1={year}&month1={month}&day1={day}"
            f"&year2={year}&month2={month}&day2={day}"
        )

        try:
            resp = retry_request("GET", url, timeout=10, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.log.warning("IEM ASOS returned status %s for %s/%s",
                                getattr(resp, 'status_code', 'None'),
                                station_id, date_str)
                return None

            # Parse CSV response: skip comment lines starting with #
            lines = resp.text.strip().split("\n")
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Header line (station,valid,max_tmpf) -- skip it
                if "station" in line.lower() and "max_tmpf" in line.lower():
                    continue
                # Data row
                parts = line.split(",")
                if len(parts) >= 3:
                    max_tmpf = parts[2].strip()
                    if max_tmpf == "M" or max_tmpf == "":
                        return None  # Missing data
                    try:
                        return float(max_tmpf)
                    except ValueError:
                        self.log.warning("IEM ASOS non-numeric max_tmpf: %s", max_tmpf)
                        return None

            return None  # No data rows found
        except Exception as e:
            self.log.warning("IEM ASOS error for %s/%s: %s", station_id, date_str, e)
            return None

    def get_verification_summary(self, lookback_days=30):
        """Return per-model verification data for adaptive ensemble weights.

        Returns:
            dict of {model_name: {"mae": float, "bias": float, "n": int,
                     "brier_predictions": [(pred_prob, actual_outcome), ...]}}
            Only includes models with >= 10 verified forecasts in the lookback window.
        """
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        recent = [v for v in self.state["verified"]
                  if self._parse_date(v.get("date")) and self._parse_date(v["date"]) >= cutoff]

        if not recent:
            return {}

        # Aggregate per-model stats
        model_data = {}  # model_name -> {"errors": [...], "brier": [...]}
        for record in recent:
            errors = record.get("errors", {})
            actual = record.get("actual_high")
            threshold = record.get("threshold")
            model_prob = record.get("model_prob")

            for model_name, error in errors.items():
                if model_name not in model_data:
                    model_data[model_name] = {"errors": [], "brier_predictions": []}
                model_data[model_name]["errors"].append(error)

                # If we have threshold and probability, compute Brier prediction
                if threshold is not None and model_prob is not None and actual is not None:
                    actual_outcome = 1 if actual > threshold else 0
                    model_data[model_name]["brier_predictions"].append(
                        (model_prob, actual_outcome)
                    )

        # Build summary with minimum sample requirement
        min_samples = 10
        summary = {}
        for model_name, data in model_data.items():
            if len(data["errors"]) < min_samples:
                continue

            errors = data["errors"]
            mae = sum(abs(e) for e in errors) / len(errors)
            bias = sum(errors) / len(errors)  # positive = warm bias

            summary[model_name] = {
                "mae": round(mae, 2),
                "bias": round(bias, 2),
                "n": len(errors),
                "brier_predictions": data["brier_predictions"],
            }

        return summary

    def get_city_bias(self, lookback_days=30):
        """Return per-city forecast bias for skew parameter estimation.

        Returns:
            dict of {city: {"bias_f": float, "skew_alpha": float, "n": int}}
            skew_alpha is derived from bias: positive bias (warm) -> negative alpha
            (model overestimates), negative bias (cold) -> positive alpha.
            Scaling: alpha = -bias_f * 0.3 (heuristic).
            Only includes cities with >= 15 verified forecasts.
        """
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        recent = [v for v in self.state["verified"]
                  if self._parse_date(v.get("date")) and self._parse_date(v["date"]) >= cutoff]

        if not recent:
            return {}

        # Aggregate per-city errors (average across all models)
        city_errors = {}  # city -> [errors]
        for record in recent:
            city = record.get("city")
            errors = record.get("errors", {})
            if city and errors:
                if city not in city_errors:
                    city_errors[city] = []
                # Average error across models for this forecast
                model_errors = list(errors.values())
                if model_errors:
                    avg_error = sum(model_errors) / len(model_errors)
                    city_errors[city].append(avg_error)

        # Build bias summary with minimum sample requirement
        min_samples = 15
        bias_summary = {}
        for city, errors in city_errors.items():
            if len(errors) < min_samples:
                continue

            bias_f = sum(errors) / len(errors)  # positive = warm bias
            # Skew alpha: negative bias maps to positive alpha (model underestimates)
            # positive bias maps to negative alpha (model overestimates)
            skew_alpha = -bias_f * 0.3

            bias_summary[city] = {
                "bias_f": round(bias_f, 2),
                "skew_alpha": round(skew_alpha, 3),
                "n": len(errors),
            }

        return bias_summary

    def save(self):
        """Persist state to disk using atomic write."""
        try:
            from kalshi_auth import _atomic_write_json
            _atomic_write_json(self.state_path, self.state)
        except ImportError:
            # Fallback: direct write
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state, indent=2))
        except Exception as e:
            self.log.warning("Failed to save verification state: %s", e)

    def load(self):
        """Load state from disk. Start fresh if file missing or corrupt."""
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text())
                if isinstance(data, dict):
                    self.state = {
                        "pending": data.get("pending", []),
                        "verified": data.get("verified", []),
                        "stats": data.get("stats", {
                            "last_verification": None,
                            "total_verified": 0,
                        }),
                    }
                    self.log.info("Loaded verification state: %d pending, %d verified",
                                 len(self.state["pending"]), len(self.state["verified"]))
                    return
        except (json.JSONDecodeError, OSError) as e:
            self.log.warning("Failed to load verification state: %s (starting fresh)", e)

        # Start fresh
        self.state = {
            "pending": [],
            "verified": [],
            "stats": {"last_verification": None, "total_verified": 0},
        }

    def cleanup(self, max_age_days=90):
        """Remove verification records older than max_age_days."""
        cutoff = datetime.date.today() - datetime.timedelta(days=max_age_days)

        original_pending = len(self.state["pending"])
        original_verified = len(self.state["verified"])

        self.state["pending"] = [
            p for p in self.state["pending"]
            if self._parse_date(p.get("date")) and self._parse_date(p["date"]) >= cutoff
        ]
        self.state["verified"] = [
            v for v in self.state["verified"]
            if self._parse_date(v.get("date")) and self._parse_date(v["date"]) >= cutoff
        ]

        removed_pending = original_pending - len(self.state["pending"])
        removed_verified = original_verified - len(self.state["verified"])
        if removed_pending or removed_verified:
            self.log.info("Cleanup: removed %d pending, %d verified records (> %d days old)",
                         removed_pending, removed_verified, max_age_days)

        self.state["stats"]["total_verified"] = len(self.state["verified"])

    @staticmethod
    def _parse_date(date_str):
        """Parse a YYYY-MM-DD date string, returning None on failure."""
        if not date_str:
            return None
        try:
            return datetime.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            return None
