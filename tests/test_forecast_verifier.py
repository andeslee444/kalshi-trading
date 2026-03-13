"""Tests for forecast_verifier.py."""

import datetime
import sys
import types
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src/kalshi")

from forecast_verifier import ForecastVerifier


class TestForecastVerifier:

    def test_get_run_to_run_deltas_only_uses_snapshot_records(self, tmp_path):
        verifier = ForecastVerifier(tmp_path / "weather-verification.json")
        verifier.state["pending"] = [
            {
                "city": "MIA",
                "date": "2026-03-06",
                "models": {"gfs": 80.0},
                "model_run_tags": {"gfs": "2026-03-05T06:00:00"},
                "record_kind": "snapshot",
            },
            {
                "city": "MIA",
                "date": "2026-03-06",
                "models": {"gfs": 81.0},
                "threshold": 86.0,
                "direction": "T",
                "record_kind": "market",
            },
        ]

        current = {"MIA": {"2026-03-06": {"gfs": 82.5}}}

        same_run = verifier.get_run_to_run_deltas(
            current,
            "gfs",
            "2026-03-05T06:00:00",
        )
        assert same_run == {}

        next_run = verifier.get_run_to_run_deltas(
            current,
            "gfs",
            "2026-03-05T12:00:00",
        )
        assert next_run["MIA"]["2026-03-06"]["previous"] == 80.0
        assert next_run["MIA"]["2026-03-06"]["current"] == 82.5
        assert next_run["MIA"]["2026-03-06"]["delta"] == 2.5

    def test_record_forecast_dedupes_market_records_by_threshold_and_direction(self, tmp_path):
        verifier = ForecastVerifier(tmp_path / "weather-verification.json")
        verifier.record_forecast(
            "MIA",
            "2026-03-06",
            {"gfs": 82.0, "ecmwf": None},
            threshold=86.0,
            direction="T",
            per_model_probs={"gfs": 0.61, "ecmwf": 0.55},
            model_run_tags={"gfs": "2026-03-05T12:00:00", "ecmwf": "2026-03-05T12:00:00"},
            record_kind="market",
        )
        verifier.record_forecast(
            "MIA",
            "2026-03-06",
            {"gfs": 83.0},
            threshold=86.0,
            direction="T",
            per_model_probs={"gfs": 0.64},
            record_kind="market",
        )
        verifier.record_forecast(
            "MIA",
            "2026-03-06",
            {"gfs": 83.0},
            threshold=87.0,
            direction="T",
            per_model_probs={"gfs": 0.52},
            record_kind="market",
        )

        assert len(verifier.state["pending"]) == 2
        first = verifier.state["pending"][0]
        assert first["models"] == {"gfs": 83.0}
        assert first["per_model_probs"] == {"gfs": 0.64}
        assert first["direction"] == "T"
        assert first["record_kind"] == "market"

    def test_verification_summary_separates_snapshot_mae_from_market_brier(self, tmp_path):
        verifier = ForecastVerifier(tmp_path / "weather-verification.json")
        verifier.state["verified"] = [
            {
                "city": "MIA",
                "date": "2026-03-01",
                "models": {"gfs": 82.0},
                "errors": {"gfs": 2.0},
                "actual_high": 80.0,
                "record_kind": "snapshot",
            },
            {
                "city": "MIA",
                "date": "2026-03-02",
                "models": {"gfs": 81.0},
                "errors": {"gfs": 1.0},
                "actual_high": 80.0,
                "record_kind": "snapshot",
            },
        ] * 5 + [
            {
                "city": "MIA",
                "date": "2026-03-01",
                "models": {"gfs": 82.0},
                "actual_high": 80.0,
                "threshold": 79.0,
                "direction": "T",
                "per_model_probs": {"gfs": 0.70},
                "record_kind": "market",
            },
            {
                "city": "MIA",
                "date": "2026-03-02",
                "models": {"gfs": 81.0},
                "actual_high": 80.0,
                "threshold": 80.0,
                "direction": "B",
                "per_model_probs": {"gfs": 0.40},
                "record_kind": "market",
            },
        ] * 10

        summary = verifier.get_verification_summary(lookback_days=30)
        assert summary["gfs"]["mae"] == 1.5
        assert summary["gfs"]["bias"] == 1.5
        assert summary["gfs"]["n"] == 10
        assert len(summary["gfs"]["brier_predictions"]) == 20
        assert summary["gfs"]["brier_predictions"][0] == (0.70, 1)
        assert summary["gfs"]["brier_predictions"][1] == (0.40, 1)

    def test_fetch_actual_high_uses_settlement_fetcher(self, tmp_path):
        with patch("forecast_verifier.SettlementTemperatureFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_daily_high_with_source.return_value = (71.5, "nws_cli")
            mock_fetcher_cls.return_value = mock_fetcher
            verifier = ForecastVerifier(tmp_path / "weather-verification.json")

        assert verifier._fetch_actual_high("KNYC", "2026-03-01", city_code="NY") == (71.5, "nws_cli")
        mock_fetcher.fetch_daily_high_with_source.assert_called_once_with(
            "KNYC",
            "2026-03-01",
            city_code="NY",
        )

    def test_verify_past_forecasts_stores_actual_source(self, tmp_path):
        with patch("forecast_verifier.SettlementTemperatureFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_daily_high_with_source.return_value = (80.0, "nws_cli")
            mock_fetcher_cls.return_value = mock_fetcher
            verifier = ForecastVerifier(tmp_path / "weather-verification.json")

        verifier.state["pending"] = [{
            "city": "MIA",
            "date": "2026-03-10",
            "models": {"gfs": 82.0},
            "record_kind": "snapshot",
            "recorded_at": "2026-03-10T12:00:00+00:00",
        }]

        with patch.object(verifier, "_city_local_today", return_value=datetime.date(2026, 3, 12)):
            verifier.verify_past_forecasts(station_map={"MIA": "KMIA"})

        assert len(verifier.state["verified"]) == 1
        assert verifier.state["verified"][0]["actual_high"] == 80.0
        assert verifier.state["verified"][0]["actual_source"] == "nws_cli"

    def test_get_actual_source_summary_counts_missing_and_present_sources(self, tmp_path):
        verifier = ForecastVerifier(tmp_path / "weather-verification.json")
        verifier.state["verified"] = [
            {"city": "MIA", "date": "2026-03-10", "actual_source": "nws_cli"},
            {"city": "NY", "date": "2026-03-10", "actual_source": "iem_fallback"},
            {"city": "CHI", "date": "2026-03-10"},
        ]

        summary = verifier.get_actual_source_summary(lookback_days=30)

        assert summary["total"] == 3
        assert summary["counts"] == {
            "iem_fallback": 1,
            "missing": 1,
            "nws_cli": 1,
        }

    def test_backfill_actual_sources_prefers_matching_nws_value(self, tmp_path):
        with patch("forecast_verifier.SettlementTemperatureFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.nws.fetch_daily_high.return_value = 80.0
            mock_fetcher.iem.fetch_daily_high.return_value = 79.0
            mock_fetcher_cls.return_value = mock_fetcher
            verifier = ForecastVerifier(tmp_path / "weather-verification.json")

        verifier.state["verified"] = [{
            "city": "MIA",
            "date": "2026-03-10",
            "actual_high": 80.0,
        }]

        result = verifier.backfill_actual_sources(station_map={"MIA": "KMIA"})

        assert result == {"updated": 1, "unresolved": 0, "checked": 1}
        assert verifier.state["verified"][0]["actual_source"] == "nws_cli"

    def test_backfill_actual_sources_uses_iem_when_nws_does_not_match(self, tmp_path):
        with patch("forecast_verifier.SettlementTemperatureFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.nws.fetch_daily_high.return_value = 78.0
            mock_fetcher.iem.fetch_daily_high.return_value = 80.0
            mock_fetcher_cls.return_value = mock_fetcher
            verifier = ForecastVerifier(tmp_path / "weather-verification.json")

        verifier.state["verified"] = [{
            "city": "MIA",
            "date": "2026-03-10",
            "actual_high": 80.0,
        }]

        result = verifier.backfill_actual_sources(station_map={"MIA": "KMIA"})

        assert result == {"updated": 1, "unresolved": 0, "checked": 1}
        assert verifier.state["verified"][0]["actual_source"] == "iem_fallback"
