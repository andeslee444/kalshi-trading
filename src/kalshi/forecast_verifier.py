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

    def verify_past_forecasts(self, city_coords=None):
        """Check forecasts from 2+ days ago against actual temperatures.

        Fetches actual high from Open-Meteo historical API. Rate-limits
        to max 5 API calls per scan to avoid hammering the archive API.

        Args:
            city_coords: dict of {city_code: {"lat": float, "lon": float}}
                Required to look up coordinates for API calls.
                If None, skips verification silently.
        """
        if not city_coords:
            self.log.debug("No city_coords provided, skipping verification")
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
            if city not in city_coords:
                self.log.debug("No coordinates for city %s, skipping verification", city)
                remaining.append(record)
                continue

            coords = city_coords[city]
            lat = coords.get("lat")
            lon = coords.get("lon")
            if lat is None or lon is None:
                remaining.append(record)
                continue

            actual_high = self._fetch_actual_high(lat, lon, record["date"])
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

    def _fetch_actual_high(self, lat, lon, date_str):
        """Fetch actual high temperature from Open-Meteo historical API.

        Returns temperature in Fahrenheit, or None on failure.
        Non-blocking: logs warning and returns None on any error.
        """
        try:
            from kalshi_auth import retry_request
        except ImportError:
            self.log.warning("Cannot import retry_request, skipping verification")
            return None

        url = (
            f"https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat}&longitude={lon}"
            f"&start_date={date_str}&end_date={date_str}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=America%2FNew_York"
        )

        try:
            resp = retry_request("GET", url, timeout=10, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.log.warning("Archive API returned status %s for %s",
                                getattr(resp, 'status_code', 'None'), date_str)
                return None

            data = resp.json()
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                return temps[0]
            return None
        except Exception as e:
            self.log.warning("Archive API error for %s: %s", date_str, e)
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
