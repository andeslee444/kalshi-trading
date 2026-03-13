"""Tests for weather_data.py — STATION_MAP, EnsembleCollector, IEMFetcher, NAMFetcher, PreviousRunsFetcher, TrainingStore."""

import datetime
import sys
import types
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, "src/kalshi")

from weather_data import (
    STATION_MAP,
    EnsembleCollector,
    IEMFetcher,
    NWSClimateReportFetcher,
    SettlementTemperatureFetcher,
    TrainingStore,
    NWSForecastFetcher,
    NWS_GRID_MAP,
    NAMFetcher,
    PreviousRunsFetcher,
    BiasCorrector,
    latest_available_model_run,
)


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

    @patch("weather_data._retry_request")
    def test_ensemble_url_includes_icon_gem(self, mock_retry):
        """Verify expanded ensemble models string includes ICON and GEM."""
        mock_retry.return_value = self._make_mock_response(
            {"daily": {"time": [], "temperature_2m_max_member00": []}})
        ec = EnsembleCollector()
        ec.fetch_ensemble(25.79, -80.29)
        url = mock_retry.call_args[0][1]
        assert "icon_global" in url
        assert "gem_global" in url


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
    def test_fetch_daily_high_ignores_tmpf_header_row(self, mock_retry):
        csv_text = """#DEBUG: 1 rows
station,valid,tmpf
KNYC,2026-03-01,72.5
"""
        mock_retry.return_value = self._make_mock_response(csv_text)

        fetcher = IEMFetcher()
        result = fetcher.fetch_daily_high("KNYC", "2026-03-01")
        assert result == 72.5

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


class TestNWSClimateReportFetcher:

    def _make_mock_response(self, json_data, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    def _product_text(self, summary_date, maximum):
        return (
            f"...THE CHICAGO-MIDWAY CLIMATE SUMMARY FOR {summary_date}...\n\n"
            "TEMPERATURE (F)\n"
            " YESTERDAY\n"
            f"  MAXIMUM         {maximum}   1:48 AM\n"
            "PRECIPITATION (IN)\n"
        )

    @patch("weather_data._retry_request")
    def test_fetch_daily_high_uses_matching_cli_product(self, mock_retry):
        list_resp = self._make_mock_response({
            "@graph": [
                {"id": "current", "issuanceTime": "2026-03-12T21:42:00+00:00"},
                {"id": "final", "issuanceTime": "2026-03-12T06:37:00+00:00"},
            ]
        })
        current_resp = self._make_mock_response({
            "productText": self._product_text("MARCH 12 2026", "48")
        })
        final_resp = self._make_mock_response({
            "productText": self._product_text("MARCH 11 2026", "45")
        })
        mock_retry.side_effect = [list_resp, current_resp, final_resp]

        fetcher = NWSClimateReportFetcher()
        result = fetcher.fetch_daily_high("KMDW", "2026-03-11")

        assert result == 45.0
        assert mock_retry.call_args_list[0].args[1].endswith("/products/types/CLI/locations/MDW")
        assert mock_retry.call_args_list[2].args[1].endswith("/products/final")

    @patch("weather_data._retry_request")
    def test_fetch_daily_high_returns_none_when_no_matching_report(self, mock_retry):
        list_resp = self._make_mock_response({
            "@graph": [
                {"id": "only", "issuanceTime": "2026-03-12T06:37:00+00:00"},
            ]
        })
        only_resp = self._make_mock_response({
            "productText": self._product_text("MARCH 12 2026", "48")
        })
        mock_retry.side_effect = [list_resp, only_resp]

        fetcher = NWSClimateReportFetcher()
        result = fetcher.fetch_daily_high("KMDW", "2026-03-11")

        assert result is None


class TestSettlementTemperatureFetcher:

    def test_fetch_daily_high_falls_back_to_iem(self):
        fetcher = SettlementTemperatureFetcher()
        with patch.object(fetcher.nws, "fetch_daily_high", return_value=None), \
             patch.object(fetcher.iem, "fetch_daily_high", return_value=71.5) as mock_iem:
            result = fetcher.fetch_daily_high("KNYC", "2026-03-01", city_code="NY")

        assert result == 71.5
        mock_iem.assert_called_once_with("KNYC", "2026-03-01", city_code="NY")

    def test_fetch_daily_highs_overlays_recent_nws_values(self):
        fetcher = SettlementTemperatureFetcher()
        end = datetime.date.today() - datetime.timedelta(days=1)
        start = end - datetime.timedelta(days=1)
        start_key = start.isoformat()
        end_key = end.isoformat()

        with patch.object(fetcher.iem, "fetch_daily_highs", return_value={start_key: 70.0, end_key: 71.0}), \
             patch.object(fetcher.nws, "fetch_daily_high", side_effect=[72.0, None]):
            result = fetcher.fetch_daily_highs("KNYC", start_key, end_key, city_code="NY")

        assert result[start_key] == 72.0
        assert result[end_key] == 71.0

    def test_fetch_daily_high_with_source_prefers_nws(self):
        fetcher = SettlementTemperatureFetcher()
        with patch.object(fetcher.nws, "fetch_daily_high", return_value=69.0), \
             patch.object(fetcher.iem, "fetch_daily_high") as mock_iem:
            result = fetcher.fetch_daily_high_with_source("KNYC", "2026-03-01", city_code="NY")

        assert result == (69.0, "nws_cli")
        mock_iem.assert_not_called()

    def test_fetch_daily_high_with_source_marks_iem_fallback(self):
        fetcher = SettlementTemperatureFetcher()
        with patch.object(fetcher.nws, "fetch_daily_high", return_value=None), \
             patch.object(fetcher.iem, "fetch_daily_high", return_value=71.5):
            result = fetcher.fetch_daily_high_with_source("KNYC", "2026-03-01", city_code="NY")

        assert result == (71.5, "iem_fallback")


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
    def test_fetch_hrrr_tracks_http_status_on_error(self, mock_retry):
        err = Exception("bad request")
        err.response = MagicMock(status_code=400)
        mock_retry.side_effect = err
        fetcher = HRRRFetcher()
        result = fetcher.fetch_hrrr(25.7, -80.2)
        assert result is None
        assert fetcher.last_status_code == 400
        assert "bad request" in fetcher.last_error

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
        """Verify the URL uses an hrrr_conus model variant and auto timezone."""
        mock_retry.return_value = self._make_mock_response(
            {"hourly": {"time": ["2026-03-05T00:00"], "temperature_2m": [72.0]}}
        )
        fetcher = HRRRFetcher()
        fetcher.fetch_hrrr(25.7, -80.2)

        call_args = mock_retry.call_args
        url = call_args[0][1]  # second positional arg is the URL
        # Premium API uses ncep_hrrr_conus, free uses hrrr_conus
        assert "hrrr_conus" in url
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
        assert "nbm" in MODEL_RUN_SCHEDULE
        assert "nam" in MODEL_RUN_SCHEDULE

    def test_hrrr_runs_hourly(self):
        assert len(MODEL_RUN_SCHEDULE["hrrr"]["hours_utc"]) == 24

    def test_nbm_runs_hourly(self):
        assert len(MODEL_RUN_SCHEDULE["nbm"]["hours_utc"]) == 24

    def test_nam_runs_4_times(self):
        assert MODEL_RUN_SCHEDULE["nam"]["hours_utc"] == [0, 6, 12, 18]

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
        # At 01:00: NBM 00Z at 01:30 (30 min away), HRRR 01Z at 01:45 (45 min)
        now = datetime.datetime(2026, 3, 5, 1, 0, 0)
        model, minutes = next_model_run(now)
        assert minutes > 0  # must be a future run
        assert minutes == 30  # NBM 00Z at 01:30

    def test_next_model_run_prefers_soonest(self):
        """Should return the model run that becomes available soonest."""
        # At 05:00 UTC:
        # NBM 04Z available at 05:30 = 30 min away (soonest)
        # HRRR 05Z available at 05:45 = 45 min away
        # GFS 06Z available at 09:30 = 270 min away
        now = datetime.datetime(2026, 3, 5, 5, 0, 0)
        model, minutes = next_model_run(now)
        assert minutes > 0
        assert model == "nbm"
        assert minutes == 30

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
        """At 10:50 UTC, HRRR 10Z already past. NBM 10Z at 11:30 (40 min), HRRR 11Z at 11:45 (55 min)."""
        now = datetime.datetime(2026, 3, 5, 10, 50, 0)
        model, minutes = next_model_run(now)
        assert minutes == 40  # NBM 10Z at 11:30

    def test_latest_available_model_run_returns_current_cycle(self):
        now = datetime.datetime(2026, 3, 5, 10, 50, 0)
        assert latest_available_model_run("gfs", now) == datetime.datetime(2026, 3, 5, 6, 0, 0)

    def test_latest_available_model_run_returns_none_for_unknown_model(self):
        assert latest_available_model_run("unknown", datetime.datetime(2026, 3, 5, 10, 50, 0)) is None


# ===================================================================
# NWS_GRID_MAP and NWSForecastFetcher tests
# ===================================================================

class TestNWSGridMap:

    def test_has_20_cities(self):
        assert len(NWS_GRID_MAP) == 20

    def test_all_cities_have_required_keys(self):
        for city, grid in NWS_GRID_MAP.items():
            assert "office" in grid, f"{city} missing 'office'"
            assert "gridX" in grid, f"{city} missing 'gridX'"
            assert "gridY" in grid, f"{city} missing 'gridY'"
            assert isinstance(grid["office"], str)
            assert isinstance(grid["gridX"], int)
            assert isinstance(grid["gridY"], int)

    def test_station_map_cities_match(self):
        """NWS_GRID_MAP should cover the same cities as STATION_MAP."""
        assert set(NWS_GRID_MAP.keys()) == set(STATION_MAP.keys())


class TestNWSForecastFetcher:

    def _make_mock_response(self, json_data, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    @patch("weather_data._retry_request")
    def test_fetch_forecast_parses_periods(self, mock_retry):
        """NWS forecast should parse daytime periods into daily highs."""
        json_data = {
            "properties": {
                "periods": [
                    {"isDaytime": True, "temperature": 82, "temperatureUnit": "F",
                     "startTime": "2026-03-07T06:00:00-05:00"},
                    {"isDaytime": False, "temperature": 65, "temperatureUnit": "F",
                     "startTime": "2026-03-07T18:00:00-05:00"},
                    {"isDaytime": True, "temperature": 85, "temperatureUnit": "F",
                     "startTime": "2026-03-08T06:00:00-05:00"},
                ]
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)

        fetcher = NWSForecastFetcher()
        result = fetcher.fetch_forecast("MIA")

        assert result is not None
        assert result["2026-03-07"] == 82.0
        assert result["2026-03-08"] == 85.0
        # Nighttime period should be excluded
        assert len(result) == 2

    @patch("weather_data._retry_request")
    def test_fetch_forecast_handles_celsius(self, mock_retry):
        """NWS sometimes returns Celsius; should convert to Fahrenheit."""
        json_data = {
            "properties": {
                "periods": [
                    {"isDaytime": True, "temperature": 30, "temperatureUnit": "C",
                     "startTime": "2026-03-07T06:00:00-05:00"},
                ]
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)

        fetcher = NWSForecastFetcher()
        result = fetcher.fetch_forecast("MIA")

        assert result is not None
        # 30C = 86F
        assert result["2026-03-07"] == pytest.approx(86.0, abs=0.01)

    @patch("weather_data._retry_request")
    def test_fetch_forecast_returns_none_on_failure(self, mock_retry):
        mock_retry.return_value = None
        fetcher = NWSForecastFetcher()
        result = fetcher.fetch_forecast("MIA")
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_forecast_returns_none_on_bad_status(self, mock_retry):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_retry.return_value = mock_resp
        fetcher = NWSForecastFetcher()
        result = fetcher.fetch_forecast("MIA")
        assert result is None

    def test_fetch_forecast_unknown_city(self):
        fetcher = NWSForecastFetcher()
        result = fetcher.fetch_forecast("UNKNOWN")
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_forecast_empty_periods(self, mock_retry):
        json_data = {"properties": {"periods": []}}
        mock_retry.return_value = self._make_mock_response(json_data)
        fetcher = NWSForecastFetcher()
        result = fetcher.fetch_forecast("MIA")
        assert result is None

    def test_cross_validate_sources_agree(self):
        """Sources within 3F should return True."""
        fetcher = NWSForecastFetcher()
        assert fetcher.cross_validate("MIA", 85.0, 84.0) is True
        assert fetcher.cross_validate("MIA", 85.0, 82.0) is True  # exactly 3F

    def test_cross_validate_sources_disagree(self):
        """Sources diverging >3F should return False."""
        fetcher = NWSForecastFetcher()
        assert fetcher.cross_validate("MIA", 85.0, 81.0) is False  # 4F diff
        assert fetcher.cross_validate("MIA", 85.0, 90.0) is False  # 5F diff


# ===================================================================
# NAMFetcher tests (with mocked HTTP)
# ===================================================================


class TestNAMFetcher:

    def _make_mock_response(self, json_data, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    @patch("weather_data._retry_request")
    def test_fetch_nam_parses_daily_max(self, mock_retry):
        """NAM returns daily max temps directly (no hourly aggregation needed)."""
        json_data = {
            "daily": {
                "time": ["2026-03-05", "2026-03-06", "2026-03-07"],
                "temperature_2m_max": [85.0, 87.5, 83.2],
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)
        fetcher = NAMFetcher()
        result = fetcher.fetch_nam(25.79, -80.29)
        assert result == {"2026-03-05": 85.0, "2026-03-06": 87.5, "2026-03-07": 83.2}

    @patch("weather_data._retry_request")
    def test_fetch_nam_returns_none_on_failure(self, mock_retry):
        mock_retry.return_value = self._make_mock_response({}, status_code=500)
        fetcher = NAMFetcher()
        result = fetcher.fetch_nam(25.79, -80.29)
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_nam_tracks_http_status_on_error(self, mock_retry):
        err = Exception("bad request")
        err.response = MagicMock(status_code=400)
        mock_retry.side_effect = err
        fetcher = NAMFetcher()
        result = fetcher.fetch_nam(25.79, -80.29)
        assert result is None
        assert fetcher.last_status_code == 400
        assert "bad request" in fetcher.last_error

    @patch("weather_data._retry_request")
    def test_fetch_nam_skips_none_values(self, mock_retry):
        json_data = {
            "daily": {
                "time": ["2026-03-05", "2026-03-06"],
                "temperature_2m_max": [85.0, None],
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)
        fetcher = NAMFetcher()
        result = fetcher.fetch_nam(25.79, -80.29)
        assert result == {"2026-03-05": 85.0}
        assert "2026-03-06" not in result

    @patch("weather_data._retry_request")
    def test_fetch_nam_url_contains_nam_conus(self, mock_retry):
        mock_retry.return_value = self._make_mock_response(
            {"daily": {"time": [], "temperature_2m_max": []}})
        fetcher = NAMFetcher()
        fetcher.fetch_nam(25.79, -80.29)
        url = mock_retry.call_args[0][1]
        assert "nam_conus" in url

    @patch("weather_data._retry_request")
    def test_fetch_nam_returns_none_when_no_data(self, mock_retry):
        json_data = {"daily": {"time": [], "temperature_2m_max": []}}
        mock_retry.return_value = self._make_mock_response(json_data)
        fetcher = NAMFetcher()
        result = fetcher.fetch_nam(25.79, -80.29)
        assert result is None

    @patch("weather_data._retry_request")
    def test_fetch_nam_returns_none_on_request_failure(self, mock_retry):
        mock_retry.return_value = None
        fetcher = NAMFetcher()
        result = fetcher.fetch_nam(25.79, -80.29)
        assert result is None


# ===================================================================
# PreviousRunsFetcher tests (with mocked HTTP)
# ===================================================================


class TestPreviousRunsFetcher:

    def _make_mock_response(self, json_data, status_code=200):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        return mock_resp

    @patch("weather_data._retry_request")
    def test_fetch_convergence_batch_parses_response(self, mock_retry):
        """Multi-location response with current + previous temps."""
        json_data = [
            {
                "daily": {
                    "time": ["2026-03-10", "2026-03-11"],
                    "temperature_2m_max": [85.0, 87.0],
                    "temperature_2m_max_previous_day1": [84.0, 88.5],
                }
            }
        ]
        mock_retry.return_value = self._make_mock_response(json_data)
        fetcher = PreviousRunsFetcher()
        result = fetcher.fetch_convergence_batch({"MIA": {"lat": 25.79, "lon": -80.29}})
        assert "MIA" in result
        assert result["MIA"]["2026-03-10"]["current"] == 85.0
        assert result["MIA"]["2026-03-10"]["previous"] == 84.0
        assert result["MIA"]["2026-03-10"]["delta"] == 1.0  # 85 - 84

    @patch("weather_data._retry_request")
    def test_fetch_convergence_batch_handles_missing_previous(self, mock_retry):
        json_data = [{
            "daily": {
                "time": ["2026-03-10"],
                "temperature_2m_max": [85.0],
                "temperature_2m_max_previous_day1": [None],
            }
        }]
        mock_retry.return_value = self._make_mock_response(json_data)
        fetcher = PreviousRunsFetcher()
        result = fetcher.fetch_convergence_batch({"MIA": {"lat": 25.79, "lon": -80.29}})
        assert result["MIA"]["2026-03-10"]["previous"] is None
        assert result["MIA"]["2026-03-10"]["delta"] is None

    @patch("weather_data._retry_request")
    def test_fetch_convergence_batch_single_location_dict(self, mock_retry):
        """Single-location response returns dict, not list."""
        json_data = {
            "daily": {
                "time": ["2026-03-10"],
                "temperature_2m_max": [85.0],
                "temperature_2m_max_previous_day1": [83.0],
            }
        }
        mock_retry.return_value = self._make_mock_response(json_data)
        fetcher = PreviousRunsFetcher()
        result = fetcher.fetch_convergence_batch({"MIA": {"lat": 25.79, "lon": -80.29}})
        assert "MIA" in result
        assert result["MIA"]["2026-03-10"]["delta"] == 2.0

    def test_convergence_multiplier_stable(self):
        assert PreviousRunsFetcher.convergence_multiplier(0.5) == 1.2

    def test_convergence_multiplier_unstable(self):
        assert PreviousRunsFetcher.convergence_multiplier(4.0) == 0.6

    def test_convergence_multiplier_moderate(self):
        # At delta=2.0: 1.2 - (2.0 - 1.0) * 0.3 = 0.9
        assert abs(PreviousRunsFetcher.convergence_multiplier(2.0) - 0.9) < 0.01

    def test_convergence_multiplier_negative_delta(self):
        """Negative delta (cooling) should use absolute value."""
        assert PreviousRunsFetcher.convergence_multiplier(-0.5) == 1.2
        assert PreviousRunsFetcher.convergence_multiplier(-4.0) == 0.6

    def test_convergence_multiplier_boundary_1f(self):
        """At exactly 1.0F, should be 1.2 (stable)."""
        assert PreviousRunsFetcher.convergence_multiplier(1.0) == 1.2

    def test_convergence_multiplier_boundary_3f(self):
        """At exactly 3.0F, should be 0.6 (unstable)."""
        assert PreviousRunsFetcher.convergence_multiplier(3.0) == 0.6

    @patch("weather_data._retry_request")
    def test_fetch_convergence_batch_api_failure(self, mock_retry):
        mock_retry.return_value = self._make_mock_response({}, status_code=500)
        fetcher = PreviousRunsFetcher()
        result = fetcher.fetch_convergence_batch({"MIA": {"lat": 25.79, "lon": -80.29}})
        assert result == {}

    @patch("weather_data._retry_request")
    def test_fetch_convergence_batch_request_none(self, mock_retry):
        mock_retry.return_value = None
        fetcher = PreviousRunsFetcher()
        result = fetcher.fetch_convergence_batch({"MIA": {"lat": 25.79, "lon": -80.29}})
        assert result == {}


class TestBiasCorrector:
    """Tests for BiasCorrector — per-city, per-model forecast bias correction."""

    SAMPLE_CALIBRATION = {
        "n_forecasts": 100,
        "per_city": {
            "MIA": {
                "gfs": {"bias": 8.9, "rmse": 9.9, "mae": 8.9, "n": 178},
                "ecmwf": {"bias": 7.0, "rmse": 8.3, "mae": 7.0, "n": 178},
            },
            "DEN": {
                "gfs": {"bias": 18.1, "rmse": 19.3, "mae": 18.1, "n": 178},
                "graphcast": {"bias": 14.8, "rmse": 16.3, "mae": 14.9, "n": 178},
            },
        },
        "global": {
            "gfs": {"bias": 10.4, "rmse": 12.1, "mae": 10.5, "n": 3560},
            "ecmwf": {"bias": 9.3, "rmse": 11.2, "mae": 9.4, "n": 3417},
        },
    }

    def _make_corrector(self, data=None):
        from weather_data import BiasCorrector
        bc = BiasCorrector.__new__(BiasCorrector)
        bc.log = MagicMock()
        bc._data = data if data is not None else self.SAMPLE_CALIBRATION
        return bc

    def test_correct_subtracts_city_model_bias(self):
        bc = self._make_corrector()
        assert bc.correct("MIA", "gfs", 85.0) == pytest.approx(85.0 - 8.9)

    def test_correct_none_returns_none(self):
        bc = self._make_corrector()
        assert bc.correct("MIA", "gfs", None) is None

    def test_correct_unknown_model_leaves_temp_unchanged(self):
        bc = self._make_corrector()
        assert bc.correct("MIA", "aifs", 85.0) == 85.0

    def test_correct_unknown_city_uses_global_model_bias(self):
        bc = self._make_corrector()
        assert bc.correct("SEA", "gfs", 55.0) == pytest.approx(55.0 - 10.4)

    def test_correct_unknown_city_and_model_leaves_temp_unchanged(self):
        bc = self._make_corrector()
        assert bc.correct("SEA", "aifs", 55.0) == 55.0

    def test_correct_forecast_dict(self):
        bc = self._make_corrector()
        forecasts = {"gfs": 85.0, "ecmwf": 83.0}
        result = bc.correct_forecast_dict("MIA", forecasts)
        assert result["gfs"] == pytest.approx(85.0 - 8.9)
        assert result["ecmwf"] == pytest.approx(83.0 - 7.0)

    def test_correct_forecast_dict_preserves_none(self):
        bc = self._make_corrector()
        result = bc.correct_forecast_dict("MIA", {"gfs": None, "ecmwf": 83.0})
        assert result["gfs"] is None
        assert result["ecmwf"] == pytest.approx(83.0 - 7.0)

    def test_correct_exempt_models_leave_temp_unchanged(self):
        bc = self._make_corrector()
        assert bc.correct("MIA", "nws", 82.0) == 82.0
        assert bc.correct("MIA", "hrrr", 82.0) == 82.0
        assert bc.correct("MIA", "nam", 82.0) == 82.0

    def test_city_average_bias(self):
        bc = self._make_corrector()
        assert bc.city_average_bias("MIA") == pytest.approx((8.9 + 7.0) / 2)

    def test_city_average_bias_unknown_city_uses_global(self):
        bc = self._make_corrector()
        assert bc.city_average_bias("SEA") == pytest.approx((10.4 + 9.3) / 2)

    def test_residual_std_specific_model(self):
        bc = self._make_corrector()
        import math
        expected = math.sqrt(9.9**2 - 8.9**2)
        assert bc.residual_std("MIA", "gfs") == pytest.approx(expected, abs=0.01)

    def test_residual_std_city_average(self):
        bc = self._make_corrector()
        import math
        r_gfs = math.sqrt(9.9**2 - 8.9**2)
        r_ecmwf = math.sqrt(8.3**2 - 7.0**2)
        expected = (r_gfs + r_ecmwf) / 2
        assert bc.residual_std("MIA") == pytest.approx(expected, abs=0.01)

    def test_residual_std_unknown_city_uses_global(self):
        bc = self._make_corrector()
        import math
        r_gfs = math.sqrt(12.1**2 - 10.4**2)
        r_ecmwf = math.sqrt(11.2**2 - 9.3**2)
        expected = (r_gfs + r_ecmwf) / 2
        assert bc.residual_std("SEA") == pytest.approx(expected, abs=0.01)

    def test_empty_data_returns_raw_temp(self):
        bc = self._make_corrector(data={})
        assert bc.correct("MIA", "gfs", 85.0) == 85.0

    def test_empty_data_residual_std_returns_default(self):
        bc = self._make_corrector(data={})
        assert bc.residual_std("MIA") == 4.7

    def test_residual_std_floor(self):
        """residual_std should never go below 0.5F (even when RMSE == bias)."""
        data = {"per_city": {"TEST": {"model": {"bias": 5.0, "rmse": 5.0, "n": 100}}}}
        bc = self._make_corrector(data=data)
        assert bc.residual_std("TEST", "model") == 0.5

    def test_constructor_missing_file(self):
        """BiasCorrector with nonexistent path should work with no corrections."""
        from weather_data import BiasCorrector
        bc = BiasCorrector(calibration_path="/nonexistent/path.json")
        assert bc.correct("MIA", "gfs", 85.0) == 85.0
        assert bc.city_average_bias("MIA") == 0.0

    def test_constructor_loads_real_calibration(self):
        """BiasCorrector loads config/historical-calibration.json if it exists."""
        from weather_data import BiasCorrector
        import os
        cal_path = os.path.join(os.path.dirname(__file__), "..", "config", "historical-calibration.json")
        if os.path.exists(cal_path):
            bc = BiasCorrector(calibration_path=cal_path)
            # Should have loaded data — MIA bias should be > 0
            assert bc.city_average_bias("MIA") > 0

    def test_constructor_loads_bias_section_from_calibration_json(self, tmp_path):
        from weather_data import BiasCorrector
        cal_path = tmp_path / "calibration.json"
        cal_path.write_text("""
{
  "weather": {
    "bias_correction": {
      "n_forecasts": 10,
      "per_city": {
        "MIA": {
          "gfs": {"bias": 8.0, "rmse": 9.0}
        }
      },
      "global": {
        "gfs": {"bias": 7.0, "rmse": 8.0}
      }
    }
  }
}
""".strip())
        bc = BiasCorrector(calibration_path=str(cal_path))
        assert bc.correct("MIA", "gfs", 85.0) == pytest.approx(77.0)
