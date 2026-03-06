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


# ===================================================================
# HRRRFetcher tests (with mocked HTTP)
# ===================================================================

from weather_data import HRRRFetcher


class TestHRRRFetcher:

    def _make_mock_response(self, json_data, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    @patch("weather_data._retry_request")
    def test_fetch_hrrr_parses_hourly_to_daily_max(self, mock_retry):
        """HRRR returns hourly data; fetcher should compute daily max."""
        json_data = {
            "hourly": {
                "time": [
                    "2026-03-05T00:00", "2026-03-05T06:00",
                    "2026-03-05T12:00", "2026-03-05T18:00",
                    "2026-03-06T00:00", "2026-03-06T06:00",
                    "2026-03-06T12:00", "2026-03-06T18:00",
                ],
                "temperature_2m": [55.0, 58.0, 72.0, 68.0, 52.0, 56.0, 70.0, 66.0],
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)

        fetcher = HRRRFetcher()
        result = fetcher.fetch_hrrr(25.7, -80.2)

        assert result is not None
        assert "2026-03-05" in result
        assert "2026-03-06" in result
        assert result["2026-03-05"] == 72.0  # max of 55, 58, 72, 68
        assert result["2026-03-06"] == 70.0  # max of 52, 56, 70, 66

    @patch("weather_data._retry_request")
    def test_fetch_hrrr_returns_none_on_failure(self, mock_retry):
        mock_retry.return_value = None
        fetcher = HRRRFetcher()
        result = fetcher.fetch_hrrr(25.7, -80.2)
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_hrrr_returns_none_on_bad_status(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_retry.return_value = mock_resp
        fetcher = HRRRFetcher()
        result = fetcher.fetch_hrrr(25.7, -80.2)
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_hrrr_skips_none_values(self, mock_retry):
        json_data = {
            "hourly": {
                "time": [
                    "2026-03-05T00:00", "2026-03-05T06:00",
                    "2026-03-05T12:00", "2026-03-05T18:00",
                ],
                "temperature_2m": [55.0, None, 72.0, None],
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)

        fetcher = HRRRFetcher()
        result = fetcher.fetch_hrrr(25.7, -80.2)

        assert result is not None
        assert result["2026-03-05"] == 72.0  # max of non-None values

    @patch("weather_data._retry_request")
    def test_fetch_hrrr_handles_empty_hourly(self, mock_retry):
        json_data = {"hourly": {"time": [], "temperature_2m": []}}
        mock_retry.return_value = self._make_mock_response(json_data)

        fetcher = HRRRFetcher()
        result = fetcher.fetch_hrrr(25.7, -80.2)
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_hrrr_url_uses_hrrr_conus(self, mock_retry):
        """Verify the URL uses the hrrr_conus model and auto timezone."""
        mock_retry.return_value = self._make_mock_response(
            {"hourly": {"time": ["2026-03-05T00:00"], "temperature_2m": [72.0]}}
        )
        fetcher = HRRRFetcher()
        fetcher.fetch_hrrr(25.7, -80.2)

        call_args = mock_retry.call_args
        url = call_args[0][1]  # second positional arg is the URL
        assert "models=hrrr_conus" in url
        assert "hourly=temperature_2m" in url
        assert "temperature_unit=fahrenheit" in url
        assert "timezone=auto" in url


# ===================================================================
# OrderBookDepth tests (with mocked client)
# ===================================================================

from weather_data import OrderBookDepth


class TestOrderBookDepth:

    def test_fetch_depth_parses_orderbook(self):
        """Test parsing of Kalshi orderbook API response."""
        mock_client = MagicMock()
        mock_client.get.return_value = {
            "orderbook": {
                "yes": [[85, 10], [84, 20], [83, 15]],
                "no": [[18, 5], [17, 12], [16, 8]],
            }
        }

        ob = OrderBookDepth()
        result = ob.fetch_depth(mock_client, "KXHIGHMIA-26MAR05-T86")

        assert result is not None
        # YES bids sorted descending by price
        assert result["yes_bids"] == [(85, 10), (84, 20), (83, 15)]
        # NO bids become YES asks at 100-price, sorted ascending
        assert result["yes_asks"][0][0] < result["yes_asks"][-1][0]
        assert result["total_bid_depth"] == 45  # 10 + 20 + 15
        assert result["total_ask_depth"] == 25  # 5 + 12 + 8

    def test_fetch_depth_returns_none_on_error(self):
        """Test that API errors return None gracefully."""
        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("API error")

        ob = OrderBookDepth()
        result = ob.fetch_depth(mock_client, "KXHIGHMIA-26MAR05-T86")
        assert result is None

    def test_fetch_depth_handles_empty_book(self):
        """Test with empty orderbook."""
        mock_client = MagicMock()
        mock_client.get.return_value = {
            "orderbook": {"yes": [], "no": []}
        }

        ob = OrderBookDepth()
        result = ob.fetch_depth(mock_client, "KXHIGHMIA-26MAR05-T86")
        assert result is not None
        assert result["total_bid_depth"] == 0
        assert result["total_ask_depth"] == 0

    def test_estimate_fill_price_buy(self):
        """Test estimated fill price for a buy order walking the ask book."""
        depth = {
            "yes_bids": [(85, 10), (84, 20)],
            "yes_asks": [(82, 5), (83, 10), (84, 20)],
            "total_bid_depth": 30,
            "total_ask_depth": 35,
        }
        ob = OrderBookDepth()
        # Buy 10 contracts: 5 at 82 + 5 at 83 = (82*5 + 83*5) / 10 = 82.5
        price = ob.estimate_fill_price(depth, "yes", 10)
        assert price == pytest.approx(82.5, abs=0.01)

    def test_estimate_fill_price_sell(self):
        """Test estimated fill price for a sell order walking the bid book."""
        depth = {
            "yes_bids": [(85, 10), (84, 20)],
            "yes_asks": [(82, 5), (83, 10)],
            "total_bid_depth": 30,
            "total_ask_depth": 15,
        }
        ob = OrderBookDepth()
        # Sell 15 contracts: 10 at 85 + 5 at 84 = (85*10 + 84*5) / 15 = 84.67
        price = ob.estimate_fill_price(depth, "no", 15)
        assert price == pytest.approx(84.667, abs=0.01)

    def test_estimate_fill_price_insufficient_liquidity(self):
        """Return None if insufficient depth for requested quantity."""
        depth = {
            "yes_bids": [(85, 5)],
            "yes_asks": [(82, 3)],
            "total_bid_depth": 5,
            "total_ask_depth": 3,
        }
        ob = OrderBookDepth()
        result = ob.estimate_fill_price(depth, "yes", 10)  # need 10 but only 3 available
        assert result is None


# ===================================================================
# MODEL_RUN_SCHEDULE and next_model_run tests
# ===================================================================

import datetime
from weather_data import MODEL_RUN_SCHEDULE, next_model_run


class TestModelRunSchedule:

    def test_schedule_has_expected_models(self):
        assert "gfs" in MODEL_RUN_SCHEDULE
        assert "ecmwf" in MODEL_RUN_SCHEDULE
        assert "hrrr" in MODEL_RUN_SCHEDULE

    def test_hrrr_runs_hourly(self):
        assert len(MODEL_RUN_SCHEDULE["hrrr"]["hours_utc"]) == 24

    def test_gfs_runs_4_times(self):
        assert MODEL_RUN_SCHEDULE["gfs"]["hours_utc"] == [0, 6, 12, 18]

    def test_ecmwf_runs_2_times(self):
        assert MODEL_RUN_SCHEDULE["ecmwf"]["hours_utc"] == [0, 12]

    def test_next_model_run_returns_tuple(self):
        """next_model_run should return (model_name, minutes_until_available)."""
        result = next_model_run()
        assert isinstance(result, tuple)
        assert len(result) == 2
        model_name, minutes = result
        assert model_name in MODEL_RUN_SCHEDULE
        assert isinstance(minutes, (int, float))
        assert minutes >= 0

    def test_next_model_run_with_specific_time(self):
        """HRRR runs hourly with 45-min delay. At 00:30 UTC, HRRR 00Z run
        should be available at 00:45, so 15 minutes away."""
        now = datetime.datetime(2026, 3, 5, 0, 30, 0)
        model, minutes = next_model_run(now)
        # HRRR 00Z output available at 00:45 = 15 min from now
        # That should be the soonest
        assert minutes <= 15
        assert minutes >= 0

    def test_next_model_run_skips_already_available(self):
        """Already-available runs should be skipped; only future runs returned."""
        # At 01:00, HRRR 00Z was at 00:45 (already past, skip)
        # HRRR 01Z will be at 01:45 (45min away)
        now = datetime.datetime(2026, 3, 5, 1, 0, 0)
        model, minutes = next_model_run(now)
        assert minutes > 0  # must be a future run
        assert minutes == 45  # HRRR 01Z at 01:45

    def test_next_model_run_prefers_soonest(self):
        """Should return the model run that becomes available soonest."""
        # At 05:00 UTC:
        # HRRR 05Z available at 05:45 = 45 min away (soonest)
        # GFS 06Z available at 09:30 = 270 min away
        now = datetime.datetime(2026, 3, 5, 5, 0, 0)
        model, minutes = next_model_run(now)
        assert minutes > 0
        assert model == "hrrr"
        assert minutes == 45

    def test_next_model_run_never_returns_zero(self):
        """next_model_run should never return 0 at any time of day."""
        for h in range(24):
            for m in [0, 15, 30, 45]:
                _, mins = next_model_run(datetime.datetime(2026, 3, 5, h, m))
                assert mins > 0, f"Returned 0 at {h:02d}:{m:02d}"

    def test_next_model_run_hrrr_imminent(self):
        """At 10:43 UTC, HRRR 10Z available at 10:45 -> 2 minutes away."""
        now = datetime.datetime(2026, 3, 5, 10, 43, 0)
        model, minutes = next_model_run(now)
        assert model == "hrrr"
        assert minutes == 2

    def test_next_model_run_after_last_hrrr(self):
        """At 10:50 UTC, HRRR 10Z already past. Next is HRRR 11Z at 11:45 -> 55 min."""
        now = datetime.datetime(2026, 3, 5, 10, 50, 0)
        model, minutes = next_model_run(now)
        assert minutes == 55
