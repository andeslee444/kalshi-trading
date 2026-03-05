"""Tests for weather_data.py — STATION_MAP, EnsembleCollector, IEMFetcher, TrainingStore."""

import sys
import types
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, "src/kalshi")

from weather_data import STATION_MAP, EnsembleCollector, IEMFetcher, TrainingStore


# ===================================================================
# STATION_MAP tests
# ===================================================================

class TestStationMap:

    def test_has_20_cities(self):
        assert len(STATION_MAP) == 20

    def test_all_values_start_with_k(self):
        for city, station in STATION_MAP.items():
            assert station.startswith("K"), f"{city} -> {station} doesn't start with K"

    def test_ny_maps_to_knyc(self):
        assert STATION_MAP["NY"] == "KNYC"

    def test_mia_maps_to_kmia(self):
        assert STATION_MAP["MIA"] == "KMIA"

    def test_chi_maps_to_kmdw(self):
        assert STATION_MAP["CHI"] == "KMDW"

    def test_all_expected_cities_present(self):
        expected = ["MIA", "LAX", "PHIL", "NY", "CHI", "AUS", "DEN", "HOU",
                    "ATL", "BOS", "SFO", "SEA", "LV", "DAL", "MIN", "PHX",
                    "DC", "NOLA", "OKC", "SATX"]
        for city in expected:
            assert city in STATION_MAP, f"Missing city: {city}"


# ===================================================================
# TrainingStore tests
# ===================================================================

class TestTrainingStore:

    def setup_method(self):
        self.store = TrainingStore(db_path=":memory:")

    def teardown_method(self):
        self.store.close()

    def test_insert_and_count(self):
        self.store.insert_pair("MIA", "2026-03-01", "gfs", 1, 0, 85.5)
        assert self.store.count() == 1

    def test_insert_batch(self):
        rows = [
            ("MIA", "2026-03-01", "gfs", 1, 0, 85.0, 84.0, None),
            ("MIA", "2026-03-01", "gfs", 1, 1, 86.0, 84.0, None),
            ("NY", "2026-03-01", "ecmwf", 2, 0, 55.0, 53.0, None),
        ]
        self.store.insert_batch(rows)
        assert self.store.count() == 3

    def test_query_by_city(self):
        rows = [
            ("MIA", "2026-03-01", "gfs", 1, 0, 85.0, 84.0, None),
            ("NY", "2026-03-01", "gfs", 1, 0, 55.0, 53.0, None),
        ]
        self.store.insert_batch(rows)
        mia_pairs = self.store.get_pairs(city="MIA")
        assert len(mia_pairs) == 1
        assert mia_pairs[0]["city"] == "MIA"
        assert mia_pairs[0]["forecast_temp"] == 85.0

    def test_query_by_lead_days_range(self):
        rows = [
            ("MIA", "2026-03-01", "gfs", 1, 0, 85.0, None, None),
            ("MIA", "2026-03-02", "gfs", 3, 0, 86.0, None, None),
            ("MIA", "2026-03-03", "gfs", 7, 0, 87.0, None, None),
        ]
        self.store.insert_batch(rows)
        short_range = self.store.get_pairs(min_lead_days=1, max_lead_days=3)
        assert len(short_range) == 2

    def test_query_by_model(self):
        rows = [
            ("MIA", "2026-03-01", "gfs", 1, 0, 85.0, None, None),
            ("MIA", "2026-03-01", "ecmwf", 1, 0, 84.5, None, None),
        ]
        self.store.insert_batch(rows)
        gfs_pairs = self.store.get_pairs(model="gfs")
        assert len(gfs_pairs) == 1
        assert gfs_pairs[0]["model"] == "gfs"

    def test_insert_or_replace(self):
        self.store.insert_pair("MIA", "2026-03-01", "gfs", 1, 0, 85.0)
        self.store.insert_pair("MIA", "2026-03-01", "gfs", 1, 0, 86.0)
        assert self.store.count() == 1
        pairs = self.store.get_pairs()
        assert pairs[0]["forecast_temp"] == 86.0  # Updated value

    def test_actual_temp_and_outcome(self):
        self.store.insert_pair("MIA", "2026-03-01", "gfs", 1, 0, 85.0,
                               actual_temp_cli=84.0, market_outcome="yes")
        pairs = self.store.get_pairs()
        assert pairs[0]["actual_temp_cli"] == 84.0
        assert pairs[0]["market_outcome"] == "yes"


# ===================================================================
# EnsembleCollector tests (with mocked HTTP)
# ===================================================================

class TestEnsembleCollector:

    def _make_mock_response(self, json_data, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    @patch("weather_data._retry_request")
    def test_fetch_ensemble_parses_members(self, mock_retry):
        # Simulate Open-Meteo response with 3 member columns
        json_data = {
            "daily": {
                "time": ["2026-03-01", "2026-03-02"],
                "temperature_2m_max_member00": [85.0, 86.0],
                "temperature_2m_max_member01": [84.5, 85.5],
                "temperature_2m_max_member02": [86.0, 87.0],
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)

        collector = EnsembleCollector()
        result = collector.fetch_ensemble(25.7, -80.2)

        assert result is not None
        assert "2026-03-01" in result
        assert len(result["2026-03-01"]) == 3
        assert result["2026-03-01"] == [85.0, 84.5, 86.0]
        assert result["2026-03-02"] == [86.0, 85.5, 87.0]

    @patch("weather_data._retry_request")
    def test_fetch_ensemble_handles_api_failure(self, mock_retry):
        mock_retry.return_value = None
        collector = EnsembleCollector()
        result = collector.fetch_ensemble(25.7, -80.2)
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_ensemble_handles_bad_status(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_retry.return_value = mock_resp
        collector = EnsembleCollector()
        result = collector.fetch_ensemble(25.7, -80.2)
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_ensemble_skips_none_values(self, mock_retry):
        json_data = {
            "daily": {
                "time": ["2026-03-01"],
                "temperature_2m_max_member00": [85.0],
                "temperature_2m_max_member01": [None],
                "temperature_2m_max_member02": [86.0],
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)

        collector = EnsembleCollector()
        result = collector.fetch_ensemble(25.7, -80.2)
        assert result is not None
        # None members should be filtered out
        assert len(result["2026-03-01"]) == 2


# ===================================================================
# IEMFetcher tests (with mocked HTTP)
# ===================================================================

class TestIEMFetcher:

    def _make_mock_response(self, text, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.text = text
        return mock_resp

    @patch("weather_data._retry_request")
    def test_fetch_daily_high_parses_csv(self, mock_retry):
        csv_text = """#DEBUG: 1 rows
station,valid,max_tmpf
KNYC,2026-03-01,72.5
"""
        mock_retry.return_value = self._make_mock_response(csv_text)

        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_high("KNYC", "2026-03-01")
        assert result == 72.5

    @patch("weather_data._retry_request")
    def test_fetch_daily_high_handles_missing(self, mock_retry):
        csv_text = """#DEBUG: 1 rows
station,valid,max_tmpf
KNYC,2026-03-01,M
"""
        mock_retry.return_value = self._make_mock_response(csv_text)

        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_high("KNYC", "2026-03-01")
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_daily_high_handles_api_failure(self, mock_retry):
        mock_retry.return_value = None
        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_high("KNYC", "2026-03-01")
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_daily_high_handles_empty_csv(self, mock_retry):
        csv_text = """#DEBUG: 0 rows
station,valid,max_tmpf
"""
        mock_retry.return_value = self._make_mock_response(csv_text)

        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_high("KNYC", "2026-03-01")
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_daily_highs_range(self, mock_retry):
        csv_text = """#DEBUG: 3 rows
station,valid,max_tmpf
KMIA,2026-03-01,85.0
KMIA,2026-03-02,86.5
KMIA,2026-03-03,84.0
"""
        mock_retry.return_value = self._make_mock_response(csv_text)

        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_highs("KMIA", "2026-03-01", "2026-03-03")
        assert len(result) == 3
        assert result["2026-03-01"] == 85.0
        assert result["2026-03-02"] == 86.5
        assert result["2026-03-03"] == 84.0

    @patch("weather_data._retry_request")
    def test_fetch_daily_highs_handles_missing_values(self, mock_retry):
        csv_text = """#DEBUG: 3 rows
station,valid,max_tmpf
KMIA,2026-03-01,85.0
KMIA,2026-03-02,M
KMIA,2026-03-03,84.0
"""
        mock_retry.return_value = self._make_mock_response(csv_text)

        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_highs("KMIA", "2026-03-01", "2026-03-03")
        assert len(result) == 2  # M row excluded
        assert "2026-03-02" not in result

    def test_invalid_date_format(self):
        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_high("KNYC", "invalid-date")
        assert result is None
