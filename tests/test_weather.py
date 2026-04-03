"""Tests for parse_ticker() and compute_probability() in weather-bot.py."""

import json
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from probability import _reset_calibration
from conftest import make_fake_auth, load_bot_module
import singleton_lock as _singleton_lock


# ---------------------------------------------------------------------------
# Import helper -- weather-bot.py has a hyphen in its name and performs
# heavy side-effects at import time (reads config, instantiates client).
# We stub those out so we can test the pure functions.
# ---------------------------------------------------------------------------

_fake_project = Path("/tmp/fake_project")
(_fake_project / "config").mkdir(parents=True, exist_ok=True)
(_fake_project / "config" / "kalshi-config.json").write_text(json.dumps({
    "cities": {},
    "mode": "demo",
    "maxTradeAmount": 1,
    "edgeThreshold": 0.10,
    "scanIntervalMinutes": 60,
    "maxDailyTrades": 10,
    "maxDailyLoss": 10,
}))
(_fake_project / "data").mkdir(parents=True, exist_ok=True)

_fake_auth = make_fake_auth(PROJECT_DIR=_fake_project)
_mod = load_bot_module("weather-bot.py", _fake_auth)
parse_ticker = _mod.parse_ticker
compute_probability = _mod.compute_probability


class TestLiveModelBias:

    def test_apply_live_model_bias_shrinks_correction_by_confidence(self):
        adjusted, applied = _mod._apply_live_model_bias(
            "DEN",
            {"gfs": 70.0, "graphcast": 68.0, "nws": 69.0},
            {
                "DEN": {
                    "gfs": {"bias_f": 4.0, "confidence": 0.25, "n": 2},
                    "graphcast": {"bias_f": 2.0, "confidence": 0.5, "n": 5},
                }
            },
        )

        assert adjusted["gfs"] == pytest.approx(69.0)
        assert adjusted["graphcast"] == pytest.approx(67.0)
        assert adjusted["nws"] == pytest.approx(69.0)
        assert applied == [
            {"model": "gfs", "bias_f": 4.0, "confidence": 0.25, "correction_f": 1.0},
            {"model": "graphcast", "bias_f": 2.0, "confidence": 0.5, "correction_f": 1.0},
        ]


# ===================================================================
# parse_ticker tests
# ===================================================================

class TestParseTicker:

    def test_valid_threshold_ticker(self):
        result = parse_ticker("KXHIGHMIA-26FEB16-T86")
        assert result is not None
        assert result["city"] == "MIA"
        assert result["date"] == "2026-02-16"
        assert result["direction"] == "T"
        assert result["threshold"] == 86.0

    def test_bracket_ticker(self):
        result = parse_ticker("KXHIGHMIA-26FEB16-B85.5")
        assert result is not None
        assert result["direction"] == "B"
        assert result["threshold"] == 85.5

    def test_invalid_ticker_returns_none(self):
        assert parse_ticker("INVALID-TICKER") is None

    def test_completely_empty_string(self):
        assert parse_ticker("") is None

    def test_wrong_prefix(self):
        assert parse_ticker("KXLOWMIA-26FEB16-T86") is None

    def test_different_city(self):
        result = parse_ticker("KXHIGHCHI-26JAN10-T32")
        assert result is not None
        assert result["city"] == "CHI"
        assert result["date"] == "2026-01-10"
        assert result["threshold"] == 32.0

    def test_integer_threshold(self):
        result = parse_ticker("KXHIGHNYC-26MAR05-T50")
        assert result is not None
        assert result["threshold"] == 50.0

    def test_invalid_month(self):
        """A three-letter month code that isn't in the MONTHS map."""
        assert parse_ticker("KXHIGHMIA-26XYZ16-T86") is None


class TestWeatherMarketDiscovery:
    """Verify weather markets are fetched via city series, not global prefix scans."""

    def test_get_weather_markets_queries_city_series_and_normalizes(self, monkeypatch):
        monkeypatch.setattr(_mod, "CITIES", {
            "MIA": {"lat": 25.8, "lon": -80.3},
            "NY": {"lat": 40.8, "lon": -74.0},
        })
        monkeypatch.setattr(_mod, "_weather_market_cache", {"fetched_at": 0.0, "markets": []})
        monkeypatch.setattr(
            _mod,
            "normalize_markets",
            lambda markets: [
                dict(
                    market,
                    yes_bid=int(round(float(market.get("yes_bid_dollars", 0)) * 100)),
                    yes_ask=int(round(float(market.get("yes_ask_dollars", 0)) * 100)),
                )
                for market in markets
            ],
        )

        seen_paths = []

        class FakeClient:
            def get(self, path):
                seen_paths.append(path)
                if "series_ticker=KXHIGHMIA" in path:
                    return {
                        "markets": [{
                            "ticker": "KXHIGHMIA-26MAR14-T86",
                            "yes_bid_dollars": "0.50",
                            "yes_ask_dollars": "0.51",
                        }]
                    }
                if "series_ticker=KXHIGHNY" in path:
                    return {
                        "markets": [{
                            "ticker": "KXHIGHNY-26MAR14-T56",
                            "yes_bid_dollars": "0.42",
                            "yes_ask_dollars": "0.43",
                        }]
                    }
                raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(_mod, "client", FakeClient())

        markets = _mod.get_weather_markets(cache_ttl=0)

        assert [m["ticker"] for m in markets] == [
            "KXHIGHMIA-26MAR14-T86",
            "KXHIGHNY-26MAR14-T56",
        ]
        assert markets[0]["yes_bid"] == 50
        assert markets[1]["yes_ask"] == 43
        assert any("series_ticker=KXHIGHMIA" in path for path in seen_paths)
        assert any("series_ticker=KXHIGHNY" in path for path in seen_paths)
        assert all("status=open" in path for path in seen_paths)


class TestWeatherCalibrationPathResolution:
    def test_resolve_optional_project_path_returns_none_for_empty(self):
        assert _mod._resolve_optional_project_path(None) is None
        assert _mod._resolve_optional_project_path("") is None

    def test_resolve_optional_project_path_joins_relative_paths(self):
        resolved = _mod._resolve_optional_project_path("data/test-bias.json")
        assert resolved == _mod.PROJECT_DIR / "data" / "test-bias.json"

    def test_city_weather_edge_threshold_uses_override_when_present(self, monkeypatch):
        monkeypatch.setattr(_mod, "config", {"cityEdgeThresholds": {"CHI": 0.12}})
        assert _mod._city_weather_edge_threshold("CHI", 0.10) == 0.12
        assert _mod._city_weather_edge_threshold("DEN", 0.10) == 0.10


class TestWeatherExecutionPlanning:
    """Thin-book weather markets should still produce passive entry plans."""

    def test_build_maker_entry_plan_prices_inside_wide_spread(self):
        market = {"yes_bid": 40, "yes_ask": 78, "volume": 12}
        plan = _mod._build_maker_entry_plan(market, "yes", 0.74, days_out=1)
        assert plan is not None
        assert plan["execution_mode"] == "maker"
        assert 41 <= plan["price"] <= 77
        assert plan["edge"] >= _mod._maker_min_edge(1)

    def test_build_maker_entry_plan_disabled_for_far_markets(self):
        market = {"yes_bid": 40, "yes_ask": 78, "volume": 12}
        plan = _mod._build_maker_entry_plan(
            market,
            "yes",
            0.74,
            days_out=3,
            maker_cfg={"enabled": True, "maxDaysOut": 1, "minEdgeNearTerm": 0.10, "minEdgeFar": 0.14,
                       "minImprovementCents": 1, "maxJoinUpliftCents": 18,
                       "pricePriorityEdgeBuffer": 0.02, "maxPriceCents": 95},
        )
        assert plan is None

    def test_select_execution_plan_prefers_maker_when_book_is_illiquid(self):
        displayed = {"execution_mode": "taker", "price": 78, "edge": 0.06}
        maker = {"execution_mode": "maker", "price": 67, "edge": 0.17}
        chosen = _mod._select_weather_execution_plan(displayed, maker, market_liquid=False)
        assert chosen == maker


class TestWeatherOpportunitySelection:
    def _opp(self, city, date_str, direction, edge, price, execution_mode="maker", verification=1.0):
        return {
            "city": city,
            "days_out": 0,
            "edge": edge,
            "entry_price": price,
            "execution_mode": execution_mode,
            "verification_confidence": verification,
            "parsed": {"city": city, "date": date_str, "direction": direction},
        }

    def test_selection_prefers_thresholds_and_diversifies_city_dates(self):
        opps = [
            self._opp("MIA", "2026-03-14", "T", 0.50, 49),
            self._opp("MIA", "2026-03-14", "B", 0.60, 37),
            self._opp("MIA", "2026-03-14", "B", 0.58, 38),
            self._opp("CHI", "2026-03-14", "T", 0.55, 45),
            self._opp("CHI", "2026-03-14", "B", 0.53, 46),
            self._opp("NY", "2026-03-14", "T", 0.49, 49),
        ]
        selected, pruned = _mod._select_weather_opportunities(
            opps,
            remaining_slots=2,
            selection_cfg={
                "enabled": True,
                "oversampleFactor": 1,
                "minCandidates": 2,
                "maxPerCity": 2,
                "maxPerCityDate": 1,
                "maxBracketPerCityDate": 0,
            },
        )
        assert len(selected) == 2
        assert all(opp["parsed"]["direction"] == "T" for opp in selected)
        assert {opp["city"] for opp in selected} == {"CHI", "MIA"}
        assert len(pruned) == 4

    def test_selection_caps_brackets_per_city_date(self):
        opps = [
            self._opp("LAX", "2026-03-14", "T", 0.42, 49),
            self._opp("LAX", "2026-03-14", "B", 0.60, 37),
            self._opp("LAX", "2026-03-14", "B", 0.59, 38),
        ]
        selected, _ = _mod._select_weather_opportunities(
            opps,
            remaining_slots=3,
            selection_cfg={
                "enabled": True,
                "oversampleFactor": 3,
                "minCandidates": 3,
                "maxPerCity": 3,
                "maxPerCityDate": 3,
                "maxBracketPerCityDate": 1,
            },
        )
        bracket_count = sum(1 for opp in selected if opp["parsed"]["direction"] == "B")
        assert bracket_count == 1


class TestWeatherDailyTradeSlots:
    def test_remaining_slots_uses_trade_manager_sync(self, monkeypatch):
        class FakeTradeManager:
            def __init__(self):
                self.called = 0

            def remaining_daily_trade_slots(self):
                self.called += 1
                return 0

        fake_manager = FakeTradeManager()
        monkeypatch.setattr(_mod, "trade_manager", fake_manager)

        remaining = _mod._remaining_weather_trade_slots()

        assert remaining == 0
        assert fake_manager.called == 1


# ===================================================================
# compute_probability tests
# ===================================================================

class TestComputeProbability:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    # --- direction = "T" (above threshold) ---

    def test_forecast_way_above_threshold(self):
        """Forecast 10 degrees above threshold -> high probability."""
        prob = compute_probability(96, 86, "T")
        assert prob > 0.9

    def test_forecast_way_below_threshold(self):
        """Forecast 10 degrees below threshold -> very low probability."""
        prob = compute_probability(76, 86, "T")
        assert prob < 0.1

    def test_forecast_near_threshold(self):
        """Forecast within 1 degree of threshold -> mid probability (~0.45)."""
        prob = compute_probability(86, 86, "T")
        assert 0.3 <= prob <= 0.6

    def test_slightly_above_threshold(self):
        """Forecast 2 degrees above -> moderately high probability."""
        prob = compute_probability(88, 86, "T")
        assert 0.5 < prob < 0.9

    def test_slightly_below_threshold(self):
        """Forecast 2 degrees below -> moderately low probability."""
        prob = compute_probability(84, 86, "T")
        assert 0.1 < prob < 0.5

    # --- direction = "B" (bracket) ---

    def test_bracket_forecast_at_center(self):
        """Forecast right at bracket center -> peak bracket probability.
        CDF model: 1F bracket at sigma=2.5F gives ~0.16 (PDF peak * width)."""
        # Bracket center for threshold 85 is 85.5
        prob = compute_probability(85.5, 85, "B")
        assert 0.10 < prob < 0.25

    def test_bracket_forecast_far_from_center(self):
        """Forecast 10 degrees from bracket center -> very low probability."""
        prob = compute_probability(95, 85, "B")
        assert prob < 0.10

    def test_bracket_moderate_distance(self):
        """Forecast 2 degrees from center -> ~0.20."""
        prob = compute_probability(87.5, 85, "B")
        assert 0.10 <= prob <= 0.25

    def test_bracket_large_distance(self):
        """Forecast 4 degrees from center -> ~0.06."""
        prob = compute_probability(89.5, 85, "B")
        assert prob <= 0.10


# ===================================================================
# ensemble spread sigma multiplier tests
# ===================================================================

class TestEnsembleSpreadSigma:
    """Test ensemble spread tracking for adaptive weather sigma."""

    def setup_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def teardown_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def test_tight_ensemble_no_sigma_increase(self):
        """When models agree (spread < 2°F), no sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        # Models within 1°F of each other
        mult = ensemble_spread_sigma_multiplier(spread_f=1.0)
        assert mult == pytest.approx(1.0, abs=0.05)

    def test_moderate_spread_mild_increase(self):
        """When models diverge moderately (3-5°F), mild sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=4.0)
        assert 1.1 < mult < 1.5

    def test_large_spread_significant_increase(self):
        """When models diverge significantly (>8°F), large sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=10.0)
        assert mult > 1.5

    def test_zero_spread_no_increase(self):
        """Zero spread should give multiplier of 1.0."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=0.0)
        assert mult == pytest.approx(1.0)

    def test_multiplier_capped(self):
        """Multiplier should be capped at reasonable maximum (e.g., 2.5)."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=20.0)
        assert mult <= 2.5


# ===================================================================
# ENSEMBLE_MODELS regression tests
# ===================================================================

class TestEnsembleModels:
    """Verify ENSEMBLE_MODELS dict in weather-bot.py."""

    def test_ensemble_models_has_six_entries(self):
        """Verify ENSEMBLE_MODELS includes all premium models."""
        assert len(_mod.ENSEMBLE_MODELS) == 6
        expected = {"gfs", "ecmwf", "icon", "nbm", "aifs", "graphcast"}
        assert set(_mod.ENSEMBLE_MODELS.keys()) == expected

    def test_ecmwf_model_string_is_025(self):
        """Regression: ECMWF must use ecmwf_ifs025 (was incorrectly ecmwf_ifs04)."""
        assert _mod.ENSEMBLE_MODELS["ecmwf"] == "ecmwf_ifs025"

    def test_nbm_model_string(self):
        assert _mod.ENSEMBLE_MODELS["nbm"] == "nbm_conus"

    def test_aifs_model_string(self):
        assert _mod.ENSEMBLE_MODELS["aifs"] == "ecmwf_aifs025"

    def test_graphcast_model_string(self):
        assert _mod.ENSEMBLE_MODELS["graphcast"] == "gfs_graphcast025"

    def test_nbm_uses_premium_gfs_endpoint_when_premium_key_set(self, monkeypatch):
        monkeypatch.setattr(_mod, "_OPEN_METEO_API_KEY", "premium-key")
        request_model = _mod.open_meteo_model_name("nbm_conus", api_key=_mod._OPEN_METEO_API_KEY)
        url = _mod._open_meteo_url(f"models={request_model}", model_name=request_model)
        assert url.startswith("https://customer-api.open-meteo.com/v1/gfs?")
        assert "&apikey=premium-key" in url
        assert "models=ncep_nbm_conus" in url

    def test_batch_nbm_falls_back_to_per_city_on_http_400(self, monkeypatch):
        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class _HTTP400(Exception):
            def __init__(self):
                super().__init__("bad request")
                self.response = MagicMock(status_code=400)

        cities = {
            "MIA": {"lat": 25.8, "lon": -80.3},
            "NY": {"lat": 40.8, "lon": -74.0},
        }

        def fake_cached_request(url, timeout=30, max_retries=2):
            if "latitude=25.8,40.8" in url and "models=ncep_nbm_conus" in url:
                raise _HTTP400()
            if "latitude=25.8&longitude=-80.3" in url and "models=ncep_nbm_conus" in url:
                return _Resp({"daily": {"time": ["2026-03-14"], "temperature_2m_max": [85.0]}})
            if "latitude=40.8&longitude=-74.0" in url and "models=ncep_nbm_conus" in url:
                return _Resp({"daily": {"time": ["2026-03-14"], "temperature_2m_max": [52.0]}})
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr(_mod, "ENSEMBLE_MODELS", {"nbm": "nbm_conus"})
        monkeypatch.setattr(_mod, "_OPEN_METEO_API_KEY", "premium-key")
        monkeypatch.setattr(_mod.health, "is_source_open", lambda source: False)
        record_source_success = MagicMock()
        monkeypatch.setattr(_mod.health, "record_source_success", record_source_success)
        monkeypatch.setattr(_mod, "_cached_request", fake_cached_request)

        forecasts = _mod.get_batch_ensemble_forecasts(cities)

        assert forecasts["MIA"]["2026-03-14"]["nbm"] == 85.0
        assert forecasts["NY"]["2026-03-14"]["nbm"] == 52.0
        record_source_success.assert_called_once_with("open-meteo-nbm")


def test_bias_corrector_imported():
    """BiasCorrector must be importable from weather_data and used in weather-bot."""
    assert hasattr(_mod, 'bias_corrector'), "weather-bot.py must instantiate bias_corrector"


class TestBiasBlending:
    """Test the alpha-ramp blending between historical and live bias."""

    def _compute_blend(self, hist_bias, live_bias, live_n):
        """Replicate the blending logic from weather-bot.py scan_and_trade."""
        alpha = min(1.0, live_n / 20.0) if live_n > 0 else 0.0
        return alpha * live_bias + (1 - alpha) * hist_bias

    def test_no_live_data_uses_historical(self):
        """When live_n=0, alpha=0, 100% historical bias."""
        assert self._compute_blend(hist_bias=8.5, live_bias=3.0, live_n=0) == 8.5

    def test_half_ramp_blends_50_50(self):
        """When live_n=10, alpha=0.5, 50/50 blend."""
        result = self._compute_blend(hist_bias=8.0, live_bias=4.0, live_n=10)
        assert abs(result - 6.0) < 0.01

    def test_full_ramp_uses_live(self):
        """When live_n=20, alpha=1.0, 100% live bias."""
        assert self._compute_blend(hist_bias=8.0, live_bias=3.0, live_n=20) == 3.0

    def test_beyond_ramp_stays_live(self):
        """When live_n>20, alpha still capped at 1.0."""
        assert self._compute_blend(hist_bias=8.0, live_bias=3.0, live_n=100) == 3.0

    def test_none_city_bias_defaults_to_historical(self):
        """Simulates city_bias=None scenario (no ForecastVerifier data)."""
        result = self._compute_blend(hist_bias=8.5, live_bias=0.0, live_n=0)
        assert result == 8.5


class TestWeatherBiasTradeFields:
    def test_trade_fields_capture_conflict_and_cap_metadata(self):
        fields = _mod._weather_bias_trade_fields(
            bias_applied=6.0,
            hist_bias=14.64,
            live_bias=-4.65,
            live_n=2,
            live_confidence=0.2,
            alpha=0.35,
            bias_meta={"capped": True, "conflict": True},
        )

        assert fields == {
            "bias_applied_f": 6.0,
            "bias_hist_f": 14.64,
            "bias_live_f": -4.65,
            "bias_live_n": 2,
            "bias_live_confidence": 0.2,
            "bias_alpha": 0.35,
            "bias_capped": True,
            "bias_conflict": True,
        }

    def test_trade_fields_default_cleanly_when_bias_not_used(self):
        fields = _mod._weather_bias_trade_fields()

        assert fields["bias_applied_f"] is None
        assert fields["bias_hist_f"] is None
        assert fields["bias_live_f"] is None
        assert fields["bias_live_n"] == 0
        assert fields["bias_live_confidence"] == 0.0
        assert fields["bias_alpha"] is None
        assert fields["bias_capped"] is False
        assert fields["bias_conflict"] is False


class TestSourceBreakerRegression:

    def test_record_source_failure_trips_breaker_on_bad_request(self):
        _mod.health = MagicMock()
        _mod._record_source_failure(
            "open-meteo-nam",
            "status=400",
            status_code=400,
            immediate_on_bad_request=True,
        )
        _mod.health.trip_source_breaker.assert_called_once_with("open-meteo-nam", "status=400")
        _mod.health.record_source_error.assert_not_called()

    def test_record_source_failure_uses_regular_error_path_for_non_400(self):
        _mod.health = MagicMock()
        _mod._record_source_failure(
            "open-meteo-nam",
            "status=503",
            status_code=503,
            immediate_on_bad_request=True,
        )
        _mod.health.record_source_error.assert_called_once_with("open-meteo-nam", "status=503")
        _mod.health.trip_source_breaker.assert_not_called()


class TestWeatherSingletonLock:

    def teardown_method(self):
        _mod._release_singleton_lock()

    def test_acquire_singleton_lock_writes_metadata(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "weather.lock"
        monkeypatch.setattr(_mod, "WEATHER_SINGLETON_LOCK_PATH", lock_path)
        _mod._release_singleton_lock()

        assert _mod._acquire_singleton_lock() is True

        payload = json.loads(lock_path.read_text())
        assert payload["pid"] == _mod.os.getpid()
        assert payload["argv"]

    def test_acquire_singleton_lock_returns_false_when_locked(self, tmp_path, monkeypatch):
        lock_path = tmp_path / "weather.lock"
        lock_path.write_text('{"pid":999,"started_at":"2026-03-13T00:00:00+00:00"}')
        monkeypatch.setattr(_mod, "WEATHER_SINGLETON_LOCK_PATH", lock_path)
        _mod._release_singleton_lock()

        def _raise_blocking(fd, flags):
            raise BlockingIOError()

        monkeypatch.setattr(_singleton_lock.fcntl, "flock", _raise_blocking)

        assert _mod._acquire_singleton_lock() is False

    def test_acquire_singleton_lock_handles_corrupt_metadata(self, tmp_path, monkeypatch):
        """Lock with corrupt JSON metadata should not crash — logs truncated raw data."""
        lock_path = tmp_path / "weather.lock"
        lock_path.write_text("CORRUPT{not-json")
        monkeypatch.setattr(_mod, "WEATHER_SINGLETON_LOCK_PATH", lock_path)
        _mod._release_singleton_lock()

        def _raise_blocking(fd, flags):
            raise BlockingIOError()

        monkeypatch.setattr(_singleton_lock.fcntl, "flock", _raise_blocking)

        # Should return False (locked) without crashing on corrupt metadata
        assert _mod._acquire_singleton_lock() is False

    def test_main_exits_before_auth_when_singleton_lock_unavailable(self, monkeypatch):
        monkeypatch.setattr(_mod, "_acquire_singleton_lock", lambda lock_path=None: False)
        get_balance = MagicMock(return_value=(1000, {}))
        monkeypatch.setattr(_mod.client, "get_balance", get_balance)
        monkeypatch.setattr(sys, "argv", ["weather-bot.py", "--once"])

        _mod.main()

        get_balance.assert_not_called()
