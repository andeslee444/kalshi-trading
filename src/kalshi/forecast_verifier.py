"""Forecast verification pipeline for weather bot adaptive calibration.

Records forecasts, verifies against actual temperatures from Open-Meteo
historical API, and provides verification data for adaptive ensemble
weights and city-level bias estimation.
"""

import logging
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from artifact_contracts import (
    WEATHER_NWS_CROSSCHECK_ARTIFACT,
    WEATHER_NWS_CROSSCHECK_SCHEMA_VERSION,
    WEATHER_VERIFICATION_ARTIFACT,
    WEATHER_VERIFICATION_SCHEMA_VERSION,
    normalize_verification_state,
)
from storage import StateStore
from weather_data import SettlementTemperatureFetcher

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

    STATE_ARTIFACT_TYPE = WEATHER_VERIFICATION_ARTIFACT
    STATE_SCHEMA_VERSION = WEATHER_VERIFICATION_SCHEMA_VERSION

    def __init__(self, state_path, logger=None):
        """
        Args:
            state_path: Path to data/weather-verification.json
            logger: optional logger (defaults to module logger)
        """
        self.state_path = Path(state_path)
        self.log = logger or _log
        self._settlement_fetcher = SettlementTemperatureFetcher(logger=self.log)
        self._state_store = StateStore(
            self.state_path,
            logger=self.log,
            normalizer=self._normalize_state,
        )
        self.state = self._normalize_state(None)

    def record_forecast(self, city, date_str, model_forecasts, threshold=None,
                        model_prob=None, hour_of_day=None, per_model_probs=None,
                        model_run_tags=None, direction=None, record_kind="snapshot"):
        """Record a forecast for later verification.

        Args:
            city: city code (e.g. "MIA")
            date_str: date string in YYYY-MM-DD format
            model_forecasts: dict of {model_name: temp_f}
            threshold: optional market threshold for Brier scoring
            model_prob: optional aggregate model probability for backward compatibility
            hour_of_day: optional hour when forecast was made
            per_model_probs: optional dict of {model_name: probability}
            model_run_tags: optional dict of {model_name: run_tag}

        Deduplicates by logical record identity:
            snapshot -> (city, date, record_kind)
            market   -> (city, date, threshold, direction, record_kind)
        """
        if not model_forecasts or not isinstance(model_forecasts, dict):
            return

        record = {
            "city": city,
            "date": date_str,
            "models": {k: v for k, v in model_forecasts.items() if v is not None},
            "record_kind": record_kind if record_kind in ("snapshot", "market") else "snapshot",
            "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if threshold is not None:
            record["threshold"] = threshold
        if direction in ("T", "B"):
            record["direction"] = direction
        if model_prob is not None:
            record["model_prob"] = model_prob
        if hour_of_day is not None:
            record["hour_of_day"] = hour_of_day
        if per_model_probs:
            record["per_model_probs"] = {
                k: v for k, v in per_model_probs.items()
                if k in record["models"] and v is not None
            }
        if model_run_tags:
            record["model_run_tags"] = {
                k: v for k, v in model_run_tags.items()
                if k in record["models"] and v is not None
            }

        record_id = self._record_identity(record)
        self.state["pending"] = [
            p for p in self.state["pending"]
            if self._record_identity(p) != record_id
        ]
        self.state["pending"].append(record)

    def verify_past_forecasts(self, city_coords=None, station_map=None):
        """Check forecasts from 2+ days ago against actual temperatures.

        Fetches actual high from the final NWS Daily Climate Report with
        IEM ASOS fallback. Rate-limits
        to max 5 API calls per scan to avoid hammering the API.

        Args:
            station_map: dict of {city_code: station_id} (e.g. {"NY": "KNYC"}).
                Preferred parameter. Uses the settlement fetcher for actual temps.
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

        # Find pending forecasts old enough to verify
        to_verify = []
        remaining = []
        for record in self.state["pending"]:
            try:
                forecast_date = datetime.date.fromisoformat(record["date"])
            except (ValueError, TypeError):
                remaining.append(record)
                continue

            cutoff = self._city_local_today(record.get("city")) - datetime.timedelta(days=2)
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
        actual_cache = {}

        for record in to_verify:
            city = record["city"]
            if city not in station_map:
                self.log.debug("No station mapping for city %s, skipping verification", city)
                remaining.append(record)
                continue

            cache_key = (city, record["date"])
            if cache_key in actual_cache:
                actual_high, actual_source = actual_cache[cache_key]
            else:
                if api_calls >= max_calls:
                    remaining.append(record)  # Put back for next scan
                    continue
                station_id = station_map[city]
                actual_high, actual_source = self._fetch_actual_high(
                    station_id,
                    record["date"],
                    city_code=city,
                )
                actual_cache[cache_key] = (actual_high, actual_source)
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
                "record_kind": record.get("record_kind", "snapshot"),
                "recorded_at": record.get("recorded_at"),
                "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            if actual_source is not None:
                verified_record["actual_source"] = actual_source
            if "threshold" in record:
                verified_record["threshold"] = record["threshold"]
            if "direction" in record:
                verified_record["direction"] = record["direction"]
            if "model_prob" in record:
                verified_record["model_prob"] = record["model_prob"]
            if "per_model_probs" in record:
                verified_record["per_model_probs"] = record["per_model_probs"]
            if "hour_of_day" in record:
                verified_record["hour_of_day"] = record["hour_of_day"]

            verified_this_scan.append(verified_record)

        self.state["pending"] = remaining
        self.state["verified"].extend(verified_this_scan)
        self.state["stats"]["total_verified"] = len(self.state["verified"])
        if verified_this_scan:
            self.state["stats"]["last_verification"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.log.info("Verified %d forecasts (total: %d)",
                         len(verified_this_scan), len(self.state["verified"]))

    def _fetch_actual_high(self, station_id, date_str, city_code=None):
        """Fetch actual high temperature from the settlement source path."""
        return self._settlement_fetcher.fetch_daily_high_with_source(
            station_id,
            date_str,
            city_code=city_code,
        )

    def get_verification_summary(self, lookback_days=30):
        """Return per-model verification data for adaptive ensemble weights.

        Returns:
            dict of {model_name: {"mae": float|None, "bias": float|None, "n": int,
                     "brier_predictions": [(pred_prob, actual_outcome), ...]}}
            Includes models with recent pending coverage even when resolved samples
            are still thin, so the live ensemble can blend adaptive weights toward
            current-model coverage instead of overfitting the narrow verified set.
        """
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        recent = [v for v in self.state["verified"]
                  if self._parse_date(v.get("date")) and self._parse_date(v["date"]) >= cutoff]
        pending_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=lookback_days)
        recent_pending = [
            p for p in self.state["pending"]
            if self._parse_datetime(p.get("recorded_at")) and self._parse_datetime(p["recorded_at"]) >= pending_cutoff
        ]

        if not recent and not recent_pending:
            return {}

        # Aggregate per-model stats
        model_data = {}  # model_name -> {"errors": [...], "brier": [...]}
        for record in recent:
            kind = record.get("record_kind", "snapshot")
            errors = record.get("errors", {})
            actual = record.get("actual_high")
            threshold = record.get("threshold")
            model_prob = record.get("model_prob")
            direction = record.get("direction", "T")
            per_model_probs = record.get("per_model_probs", {})
            models = record.get("models", {})

            if kind == "snapshot":
                for model_name, error in errors.items():
                    if model_name not in model_data:
                        model_data[model_name] = {"errors": [], "brier_predictions": []}
                    model_data[model_name]["errors"].append(error)
                continue

            if threshold is None or actual is None:
                continue

            actual_outcome = self._market_outcome(actual, threshold, direction)
            for model_name in models.keys():
                if model_name not in model_data:
                    model_data[model_name] = {"errors": [], "brier_predictions": []}
                prob_for_model = per_model_probs.get(model_name)
                if prob_for_model is None and model_prob is not None and len(models) == 1:
                    prob_for_model = model_prob
                if prob_for_model is not None:
                    model_data[model_name]["brier_predictions"].append(
                        (prob_for_model, actual_outcome)
                    )

        pending_counts = {}
        for record in recent_pending:
            key = "pending_market_n" if record.get("record_kind", "snapshot") == "market" else "pending_snapshot_n"
            for model_name in record.get("models", {}).keys():
                stats = pending_counts.setdefault(
                    model_name,
                    {"pending_snapshot_n": 0, "pending_market_n": 0},
                )
                stats[key] += 1

        # Build summary with minimum sample requirement
        min_samples = 10
        summary = {}
        for model_name in sorted(set(model_data) | set(pending_counts)):
            data = model_data.get(model_name, {"errors": [], "brier_predictions": []})
            pending = pending_counts.get(
                model_name,
                {"pending_snapshot_n": 0, "pending_market_n": 0},
            )
            resolved_n = len(data["errors"])
            market_n = len(data["brier_predictions"])
            if resolved_n < min_samples and market_n < min_samples and (
                pending["pending_snapshot_n"] + pending["pending_market_n"]
            ) < min_samples:
                continue

            errors = data["errors"]
            mae = sum(abs(e) for e in errors) / len(errors) if errors else None
            bias = sum(errors) / len(errors) if errors else None  # positive = warm bias

            summary[model_name] = {
                "mae": round(mae, 2) if mae is not None else None,
                "bias": round(bias, 2) if bias is not None else None,
                "n": len(errors),
                "market_n": market_n,
                "brier_predictions": data["brier_predictions"],
                "pending_snapshot_n": pending["pending_snapshot_n"],
                "pending_market_n": pending["pending_market_n"],
            }

        return summary

    def get_run_to_run_deltas(self, current_forecasts, model_name, current_run_tag=None):
        """Compare current forecast temps to the last persisted run for one model."""
        if not current_forecasts or not model_name or current_run_tag is None:
            return {}

        deltas = {}
        for record in self.state["pending"]:
            if record.get("record_kind", "snapshot") != "snapshot":
                continue
            city = record.get("city")
            date_str = record.get("date")
            prev_models = record.get("models", {})
            prev_tags = record.get("model_run_tags", {})
            prev_temp = prev_models.get(model_name)
            prev_tag = prev_tags.get(model_name)
            if prev_temp is None or prev_tag is None or prev_tag == current_run_tag:
                continue

            city_forecasts = current_forecasts.get(city, {})
            current_entry = city_forecasts.get(date_str)
            if not isinstance(current_entry, dict):
                continue

            current_temp = current_entry.get(model_name)
            if current_temp is None:
                continue

            deltas.setdefault(city, {})[date_str] = {
                "current": current_temp,
                "previous": prev_temp,
                "delta": round(current_temp - prev_temp, 2),
            }

        return deltas

    def get_city_bias(self, lookback_days=30, min_samples=2, full_weight_n=10):
        """Return per-city forecast bias for skew parameter estimation.

        Returns:
            dict of {city: {"bias_f": float, "skew_alpha": float, "n": int, "confidence": float}}
            skew_alpha is derived from bias: positive bias (warm) -> negative alpha
            (model overestimates), negative bias (cold) -> positive alpha.
            Scaling: alpha = -bias_f * 0.3 (heuristic), further shrunk by sample
            confidence so thin live samples influence the distribution gently.
        """
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        recent = [v for v in self.state["verified"]
                  if self._parse_date(v.get("date")) and self._parse_date(v["date"]) >= cutoff]

        if not recent:
            return {}

        # Aggregate per-city errors (average across all models)
        city_errors = {}  # city -> [errors]
        for record in recent:
            if record.get("record_kind", "snapshot") != "snapshot":
                continue
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
        bias_summary = {}
        for city, errors in city_errors.items():
            if len(errors) < min_samples:
                continue

            bias_f = sum(errors) / len(errors)  # positive = warm bias
            confidence = min(1.0, len(errors) / max(1.0, float(full_weight_n)))
            # Skew alpha: negative bias maps to positive alpha (model underestimates)
            # positive bias maps to negative alpha (model overestimates)
            skew_alpha = -bias_f * 0.3 * confidence

            bias_summary[city] = {
                "bias_f": round(bias_f, 2),
                "skew_alpha": round(skew_alpha, 3),
                "n": len(errors),
                "confidence": round(confidence, 3),
            }

        return bias_summary

    def get_city_model_bias(self, lookback_days=30, min_samples=2, full_weight_n=10):
        """Return per-city, per-model bias from verified settlement-aligned forecasts.

        Returns:
            dict of {city: {model: {"bias_f": float, "n": int, "confidence": float}}}
            Confidence is a simple sample-size shrinkage factor so thin live samples
            can influence corrections without fully overriding raw model output.
        """
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        recent = [
            v for v in self.state["verified"]
            if self._parse_date(v.get("date")) and self._parse_date(v["date"]) >= cutoff
        ]

        if not recent:
            return {}

        city_model_errors = {}
        for record in recent:
            if record.get("record_kind", "snapshot") != "snapshot":
                continue
            city = record.get("city")
            errors = record.get("errors", {})
            if not city or not isinstance(errors, dict):
                continue
            for model_name, error in errors.items():
                city_model_errors.setdefault(city, {}).setdefault(model_name, []).append(error)

        summary = {}
        for city, per_model in city_model_errors.items():
            city_summary = {}
            for model_name, errors in per_model.items():
                if len(errors) < min_samples:
                    continue
                confidence = min(1.0, len(errors) / max(1.0, float(full_weight_n)))
                bias_f = sum(errors) / len(errors)
                city_summary[model_name] = {
                    "bias_f": round(bias_f, 2),
                    "n": len(errors),
                    "confidence": round(confidence, 3),
                }
            if city_summary:
                summary[city] = city_summary

        return summary

    def get_actual_source_summary(self, lookback_days=30):
        """Summarize verification actual sources over a recent window."""
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        recent = [
            v for v in self.state["verified"]
            if self._parse_date(v.get("date")) and self._parse_date(v["date"]) >= cutoff
        ]

        counts = {}
        for record in recent:
            source = record.get("actual_source") or "missing"
            counts[source] = counts.get(source, 0) + 1

        total = len(recent)
        shares = {
            source: round(count / total, 3)
            for source, count in sorted(counts.items())
        } if total else {}

        return {
            "lookback_days": lookback_days,
            "total": total,
            "counts": dict(sorted(counts.items())),
            "shares": shares,
        }

    def backfill_actual_sources(self, station_map=None, tolerance_f=0.11):
        """Infer missing actual_source on historical verified rows.

        Uses the stored `actual_high` and compares it to today's NWS CLI and IEM
        values for the same station/day. If the contract source matches the stored
        value, mark `nws_cli`; otherwise fall back to `iem_fallback` if that matches.

        Returns:
            dict with counts for updated and unresolved records.
        """
        if not station_map:
            return {"updated": 0, "unresolved": 0, "checked": 0}

        cache = {}
        updated = 0
        unresolved = 0
        checked = 0

        for record in self.state["verified"]:
            if record.get("actual_source"):
                continue

            city = record.get("city")
            date_str = record.get("date")
            actual_high = record.get("actual_high")
            station_id = station_map.get(city)
            if not city or not date_str or actual_high is None or not station_id:
                continue

            checked += 1
            cache_key = (city, date_str)
            if cache_key not in cache:
                cache[cache_key] = {
                    "nws_cli": self._settlement_fetcher.nws.fetch_daily_high(
                        station_id,
                        date_str,
                        city_code=city,
                    ),
                    "iem_fallback": self._settlement_fetcher.iem.fetch_daily_high(
                        station_id,
                        date_str,
                        city_code=city,
                    ),
                }

            source = self._infer_actual_source(
                actual_high,
                cache[cache_key]["nws_cli"],
                cache[cache_key]["iem_fallback"],
                tolerance_f=tolerance_f,
            )
            if source is None:
                unresolved += 1
                continue

            record["actual_source"] = source
            updated += 1

        return {"updated": updated, "unresolved": unresolved, "checked": checked}

    def save(self):
        """Persist state to disk using atomic write."""
        try:
            self.state = self._state_store.save(self.state)
        except Exception as e:
            self.log.warning("Failed to save verification state: %s", e)

    def load(self):
        """Load state from disk. Start fresh if file missing or corrupt."""
        try:
            had_state = self.state_path.exists()
            self.state = self._state_store.load()
            if had_state:
                self.log.info(
                    "Loaded verification state: %d pending, %d verified",
                    len(self.state["pending"]),
                    len(self.state["verified"]),
                )
        except Exception as e:
            self.log.warning("Failed to load verification state: %s (starting fresh)", e)
            self.state = self._normalize_state(None)

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

    @staticmethod
    def _parse_datetime(dt_str):
        """Parse an ISO datetime string, returning None on failure."""
        if not dt_str:
            return None
        try:
            dt = datetime.datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt

    @staticmethod
    def _record_identity(record):
        kind = record.get("record_kind", "snapshot")
        base = (record.get("city"), record.get("date"), kind)
        if kind == "market":
            return base + (record.get("threshold"), record.get("direction"))
        return base

    @staticmethod
    def _infer_actual_source(actual_high, nws_value, iem_value, tolerance_f=0.11):
        if ForecastVerifier._temps_match(actual_high, nws_value, tolerance_f):
            return "nws_cli"
        if ForecastVerifier._temps_match(actual_high, iem_value, tolerance_f):
            return "iem_fallback"
        return None

    @staticmethod
    def _temps_match(lhs, rhs, tolerance_f=0.11):
        try:
            return lhs is not None and rhs is not None and abs(float(lhs) - float(rhs)) <= tolerance_f
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _market_outcome(actual_high, threshold, direction):
        if direction == "B":
            return 1 if threshold <= actual_high < (threshold + 1) else 0
        return 1 if actual_high > threshold else 0

    @staticmethod
    def _city_tz_name(city_code):
        try:
            from kalshi_auth import CITY_TIMEZONES
            return CITY_TIMEZONES.get(city_code, "America/New_York")
        except ImportError:
            return "America/New_York"

    def _city_local_today(self, city_code):
        tz = ZoneInfo(self._city_tz_name(city_code))
        return datetime.datetime.now(tz).date()

    def _normalize_state(self, data):
        return normalize_verification_state(
            data,
            artifact_type=self.STATE_ARTIFACT_TYPE,
            schema_version=self.STATE_SCHEMA_VERSION,
        )


class NWSCrossCheckVerifier(ForecastVerifier):
    """Persist Open-Meteo vs NWS gridpoint comparisons and verify against settlement."""

    STATE_ARTIFACT_TYPE = WEATHER_NWS_CROSSCHECK_ARTIFACT
    STATE_SCHEMA_VERSION = WEATHER_NWS_CROSSCHECK_SCHEMA_VERSION

    def record_comparison(
        self,
        city,
        date_str,
        open_meteo_temp,
        nws_temp,
        days_out=None,
        threshold_f=None,
        mode="gridpoint",
        open_meteo_models=None,
        model_run_tags=None,
        recorded_at=None,
    ):
        """Record one cross-check comparison for later settlement audit."""
        try:
            open_meteo_temp = float(open_meteo_temp)
            nws_temp = float(nws_temp)
        except (TypeError, ValueError):
            return

        record = {
            "city": city,
            "date": date_str,
            "open_meteo_temp": round(open_meteo_temp, 2),
            "nws_temp": round(nws_temp, 2),
            "open_meteo_minus_nws": round(open_meteo_temp - nws_temp, 2),
            "mode": str(mode or "gridpoint"),
            "recorded_at": recorded_at or datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if days_out is not None:
            try:
                record["days_out"] = max(0, int(days_out))
            except (TypeError, ValueError):
                pass
        if isinstance(threshold_f, (int, float)):
            record["threshold_f"] = round(float(threshold_f), 2)
        if isinstance(open_meteo_models, dict):
            record["open_meteo_models"] = {
                model: temp
                for model, temp in open_meteo_models.items()
                if temp is not None
            }
        if isinstance(model_run_tags, dict):
            record["model_run_tags"] = {
                model: tag
                for model, tag in model_run_tags.items()
                if tag is not None
            }

        record_id = self._comparison_identity(record)
        self.state["pending"] = [
            existing for existing in self.state["pending"]
            if self._comparison_identity(existing) != record_id
        ]
        self.state["pending"].append(record)

    def verify_past_comparisons(self, station_map=None, max_calls=5):
        """Resolve older cross-check records against settlement actual highs."""
        if not station_map:
            return

        to_verify = []
        remaining = []
        for record in self.state["pending"]:
            forecast_date = self._parse_date(record.get("date"))
            if forecast_date is None:
                remaining.append(record)
                continue
            cutoff = self._city_local_today(record.get("city")) - datetime.timedelta(days=2)
            if forecast_date <= cutoff:
                to_verify.append(record)
            else:
                remaining.append(record)

        if not to_verify:
            return

        api_calls = 0
        verified_this_scan = []
        actual_cache = {}

        for record in to_verify:
            city = record.get("city")
            station_id = station_map.get(city)
            date_str = record.get("date")
            if not city or not station_id or not date_str:
                remaining.append(record)
                continue

            cache_key = (city, date_str)
            if cache_key in actual_cache:
                actual_high, actual_source = actual_cache[cache_key]
            else:
                if api_calls >= max_calls:
                    remaining.append(record)
                    continue
                actual_high, actual_source = self._fetch_actual_high(
                    station_id,
                    date_str,
                    city_code=city,
                )
                actual_cache[cache_key] = (actual_high, actual_source)
                api_calls += 1

            if actual_high is None:
                remaining.append(record)
                continue

            om_error = round(float(record["open_meteo_temp"]) - actual_high, 2)
            nws_error = round(float(record["nws_temp"]) - actual_high, 2)
            verified_record = dict(record)
            verified_record["actual_high"] = actual_high
            if actual_source is not None:
                verified_record["actual_source"] = actual_source
            verified_record["open_meteo_error"] = om_error
            verified_record["nws_error"] = nws_error
            verified_record["open_meteo_abs_error"] = round(abs(om_error), 2)
            verified_record["nws_abs_error"] = round(abs(nws_error), 2)
            verified_record["winner"] = self._winner(
                verified_record["open_meteo_abs_error"],
                verified_record["nws_abs_error"],
            )
            verified_record["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            verified_this_scan.append(verified_record)

        self.state["pending"] = remaining
        self.state["verified"].extend(verified_this_scan)
        self.state["stats"]["total_verified"] = len(self.state["verified"])
        if verified_this_scan:
            self.state["stats"]["last_verification"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.log.info(
                "Verified %d NWS cross-check records (total: %d)",
                len(verified_this_scan),
                len(self.state["verified"]),
            )

    def get_summary(self, lookback_days=30, tolerance_f=0.11):
        """Summarize verified Open-Meteo vs NWS cross-check performance."""
        cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
        recent = [
            record for record in self.state["verified"]
            if self._parse_date(record.get("date")) and self._parse_date(record["date"]) >= cutoff
        ]

        return {
            "lookback_days": lookback_days,
            "overall": self._summarize_records(recent, tolerance_f=tolerance_f),
            "per_city": {
                city: self._summarize_records(rows, tolerance_f=tolerance_f)
                for city, rows in sorted(self._group_by(recent, "city").items())
            },
            "per_mode": {
                mode: self._summarize_records(rows, tolerance_f=tolerance_f)
                for mode, rows in sorted(self._group_by(recent, "mode").items())
            },
            "per_days_out": {
                str(days_out): self._summarize_records(rows, tolerance_f=tolerance_f)
                for days_out, rows in sorted(self._group_by(recent, "days_out").items())
            },
        }

    @staticmethod
    def _comparison_identity(record):
        return (
            record.get("city"),
            record.get("date"),
            record.get("mode", "gridpoint"),
            record.get("days_out"),
            round(float(record.get("open_meteo_temp", 0.0)), 1),
            round(float(record.get("nws_temp", 0.0)), 1),
        )

    @staticmethod
    def _group_by(records, key):
        grouped = {}
        for record in records:
            value = record.get(key)
            grouped.setdefault(value, []).append(record)
        return grouped

    @staticmethod
    def _winner(open_meteo_abs_error, nws_abs_error, tolerance_f=0.11):
        if ForecastVerifier._temps_match(open_meteo_abs_error, nws_abs_error, tolerance_f):
            return "tie"
        if open_meteo_abs_error < nws_abs_error:
            return "open_meteo"
        return "nws"

    @classmethod
    def _summarize_records(cls, records, tolerance_f=0.11):
        if not records:
            return {
                "n": 0,
                "open_meteo_mae": None,
                "nws_mae": None,
                "open_meteo_bias": None,
                "nws_bias": None,
                "mean_open_meteo_minus_nws": None,
                "open_meteo_better": 0,
                "nws_better": 0,
                "ties": 0,
                "open_meteo_better_share": None,
                "nws_better_share": None,
                "tie_share": None,
            }

        om_errors = [float(record["open_meteo_error"]) for record in records if record.get("open_meteo_error") is not None]
        nws_errors = [float(record["nws_error"]) for record in records if record.get("nws_error") is not None]
        diffs = [float(record["open_meteo_minus_nws"]) for record in records if record.get("open_meteo_minus_nws") is not None]
        open_meteo_better = 0
        nws_better = 0
        ties = 0
        for record in records:
            winner = record.get("winner")
            if winner == "open_meteo":
                open_meteo_better += 1
            elif winner == "nws":
                nws_better += 1
            else:
                winner = cls._winner(
                    abs(float(record.get("open_meteo_error", 0.0))),
                    abs(float(record.get("nws_error", 0.0))),
                    tolerance_f=tolerance_f,
                )
                if winner == "open_meteo":
                    open_meteo_better += 1
                elif winner == "nws":
                    nws_better += 1
                else:
                    ties += 1
                    continue
            if winner == "tie":
                ties += 1

        total = len(records)
        om_mae = sum(abs(err) for err in om_errors) / len(om_errors) if om_errors else None
        nws_mae = sum(abs(err) for err in nws_errors) / len(nws_errors) if nws_errors else None
        om_bias = sum(om_errors) / len(om_errors) if om_errors else None
        nws_bias = sum(nws_errors) / len(nws_errors) if nws_errors else None
        mean_diff = sum(diffs) / len(diffs) if diffs else None

        return {
            "n": total,
            "open_meteo_mae": round(om_mae, 2) if om_mae is not None else None,
            "nws_mae": round(nws_mae, 2) if nws_mae is not None else None,
            "open_meteo_bias": round(om_bias, 2) if om_bias is not None else None,
            "nws_bias": round(nws_bias, 2) if nws_bias is not None else None,
            "mean_open_meteo_minus_nws": round(mean_diff, 2) if mean_diff is not None else None,
            "open_meteo_better": open_meteo_better,
            "nws_better": nws_better,
            "ties": ties,
            "open_meteo_better_share": round(open_meteo_better / total, 3) if total else None,
            "nws_better_share": round(nws_better / total, 3) if total else None,
            "tie_share": round(ties / total, 3) if total else None,
        }
