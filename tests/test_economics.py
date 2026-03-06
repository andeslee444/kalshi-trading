"""Tests for economics-bot.py helper functions (Fed matching, gas threshold parsing, nowcast cache)."""

import json
import math
import time
import types
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from conftest import make_fake_auth, load_bot_module


# ---------------------------------------------------------------------------
# Import helper -- economics-bot.py has a hyphen and performs side-effects
# at import time. Stub kalshi_auth, probability, and capital_allocator.
# ---------------------------------------------------------------------------

def _fake_atomic_write(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))

_fake_auth = make_fake_auth(
    retry_request=lambda *a, **kw: MagicMock(),
    _atomic_write_json=_fake_atomic_write,
)

# Extra module stubs
_fake_prob = types.ModuleType("probability")
_fake_prob.econ_nowcast_probability = lambda *a, **kw: 0.5
_fake_prob.cpi_nowcast_sigma = lambda *a, **kw: 0.05
_fake_prob.gdp_nowcast_sigma = lambda *a, **kw: 0.10
_fake_prob.quarter_kelly = lambda *a, **kw: (0, 0)
_fake_prob.uncertainty_kelly = lambda *a, **kw: (0, 0, {})
_fake_prob.compute_limit_price = lambda *a, **kw: 50
_fake_prob.kalshi_fee_cents = lambda *a, **kw: 1.0
_fake_prob.gas_price_probability = lambda *a, **kw: 0.5
_fake_prob.is_market_liquid = lambda *a, **kw: True
_fake_prob._norm_cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))

_fake_alloc = types.ModuleType("capital_allocator")
_fake_alloc.PortfolioAllocator = lambda *a, **kw: MagicMock()

_fake_belief = types.ModuleType("cpi_belief_filter")
_fake_belief.CPIBeliefFilter = type("CPIBeliefFilter", (), {
    "__init__": lambda self, *a, **kw: None,
    "update": lambda self, *a, **kw: None,
    "posterior": property(lambda self: (2.8, 0.10)),
})

_fake_scenario = types.ModuleType("scenario_engine")
_fake_scenario.compute_scenario_weights = lambda *a, **kw: {}
_fake_scenario.scenario_probability = lambda *a, **kw: MagicMock(
    probability=0.5, agreement=0.8, per_scenario={}, weights_used={})

_fake_macro = types.ModuleType("macro_engine")
_fake_macro.MacroEngine = None

_econ = load_bot_module("economics-bot.py", _fake_auth, extra_stubs={
    "probability": _fake_prob,
    "capital_allocator": _fake_alloc,
    "cpi_belief_filter": _fake_belief,
    "scenario_engine": _fake_scenario,
    "macro_engine": _fake_macro,
})


# ===================================================================
# match_fed_market_to_fedwatch tests
# ===================================================================

class TestFedMatchLogic:

    # Sample FedWatch data: rate buckets as decimal fractions
    # e.g. 0.0425 = midpoint of 4.00-4.50 range -> 4.25%
    SAMPLE_FEDWATCH = {
        0.0400: 0.05,   # 4.00% -> 5%
        0.0425: 0.10,   # 4.25% -> 10%
        0.0450: 0.70,   # 4.50% -> 70% (current rate = highest prob)
        0.0475: 0.10,   # 4.75% -> 10%
        0.0500: 0.05,   # 5.00% -> 5%
    }

    def test_cut_sums_lower_rates(self):
        """'cut' title -> sum of probabilities below current rate."""
        market = {"ticker": "KXFED-MAR-CUT", "title": "Will the Fed cut rates at March meeting?"}
        prob = _econ.match_fed_market_to_fedwatch(market, self.SAMPLE_FEDWATCH)
        # Current rate = 0.0450 (highest prob); cut = rates below = 0.05 + 0.10 = 0.15
        assert prob is not None
        assert abs(prob - 0.15) < 0.01

    def test_hold_returns_current_rate_prob(self):
        """'hold' title -> probability of the current (highest-prob) rate."""
        market = {"ticker": "KXFED-MAR-HOLD", "title": "Will the Fed hold rates unchanged?"}
        prob = _econ.match_fed_market_to_fedwatch(market, self.SAMPLE_FEDWATCH)
        assert prob is not None
        assert abs(prob - 0.70) < 0.01

    def test_hike_sums_higher_rates(self):
        """'raise' title -> sum of probabilities above current rate."""
        market = {"ticker": "KXFED-MAR-HIKE", "title": "Will the Fed raise rates?"}
        prob = _econ.match_fed_market_to_fedwatch(market, self.SAMPLE_FEDWATCH)
        # Rates above 0.0450: 0.10 + 0.05 = 0.15
        assert prob is not None
        assert abs(prob - 0.15) < 0.01

    def test_empty_fedwatch_returns_none(self):
        """Empty FedWatch dict -> None."""
        market = {"ticker": "KXFED-MAR-CUT", "title": "Will the Fed cut rates?"}
        prob = _econ.match_fed_market_to_fedwatch(market, {})
        assert prob is None

    def test_unmatched_title_returns_none(self):
        """Title with no rate action keyword -> None."""
        market = {"ticker": "KXFED-MAR", "title": "Some other Fed market"}
        prob = _econ.match_fed_market_to_fedwatch(market, self.SAMPLE_FEDWATCH)
        assert prob is None


# ===================================================================
# parse_gas_threshold tests
# ===================================================================

class TestParseGasThreshold:

    def test_ticker_above(self):
        """KXGAS ticker with T suffix -> above threshold."""
        market = {"ticker": "KXGAS-MAR-T3.50", "title": ""}
        threshold, direction = _econ.parse_gas_threshold(market)
        assert threshold == 3.50
        assert direction == "T"

    def test_ticker_below(self):
        """KXGAS ticker with B suffix -> below threshold."""
        market = {"ticker": "KXGAS-MAR-B3.00", "title": ""}
        threshold, direction = _econ.parse_gas_threshold(market)
        assert threshold == 3.00
        assert direction == "B_below"

    def test_title_above(self):
        """Title with 'above' keyword."""
        market = {"ticker": "KXGAS-MAR", "title": "Will gas prices be above $3.50?"}
        threshold, direction = _econ.parse_gas_threshold(market)
        assert threshold == 3.50
        assert direction == "T"

    def test_title_below(self):
        """Title with 'below' keyword."""
        market = {"ticker": "KXGAS-MAR", "title": "Will gas be below $3.00?"}
        threshold, direction = _econ.parse_gas_threshold(market)
        assert threshold == 3.00
        assert direction == "B_below"

    def test_no_match(self):
        """No threshold found -> (None, None)."""
        market = {"ticker": "KXGAS-MAR", "title": "Gas prices this month"}
        threshold, direction = _econ.parse_gas_threshold(market)
        assert threshold is None
        assert direction is None


# ===================================================================
# Nowcast cache tests
# ===================================================================

class TestNowcastCache:

    def test_save_and_load_cache(self, tmp_path):
        """Cache round-trip: save then load returns same data."""
        cache_file = tmp_path / "econ-nowcast-cache.json"
        data = {"cpi_yoy": 2.8, "core_cpi_yoy": 3.1}

        # Patch the cache path and _atomic_write_json
        orig_path = _econ.NOWCAST_CACHE_PATH
        _econ.NOWCAST_CACHE_PATH = cache_file
        try:
            _econ._save_nowcast_cache(data)
            assert cache_file.exists()
            loaded = json.loads(cache_file.read_text())
            assert loaded["data"] == data
            assert "cached_at" in loaded

            # Load should return the data
            result = _econ._load_nowcast_cache()
            assert result == data
        finally:
            _econ.NOWCAST_CACHE_PATH = orig_path

    def test_expired_cache_returns_none(self, tmp_path):
        """Cache older than TTL returns None."""
        cache_file = tmp_path / "econ-nowcast-cache.json"
        old_cache = {"cached_at": time.time() - 100000, "data": {"cpi_yoy": 2.5}}
        cache_file.write_text(json.dumps(old_cache))

        orig_path = _econ.NOWCAST_CACHE_PATH
        _econ.NOWCAST_CACHE_PATH = cache_file
        try:
            result = _econ._load_nowcast_cache()
            assert result is None
        finally:
            _econ.NOWCAST_CACHE_PATH = orig_path

    def test_missing_cache_returns_none(self, tmp_path):
        """Non-existent cache file returns None."""
        orig_path = _econ.NOWCAST_CACHE_PATH
        _econ.NOWCAST_CACHE_PATH = tmp_path / "nonexistent.json"
        try:
            result = _econ._load_nowcast_cache()
            assert result is None
        finally:
            _econ.NOWCAST_CACHE_PATH = orig_path


# ===================================================================
# BS4 / regex nowcast parsing tests
# ===================================================================

class TestNowcastParsing:

    SAMPLE_TABLE_HTML = """
    <html><body>
    <table>
      <tr><th>Measure</th><th>Nowcast</th></tr>
      <tr><td>CPI</td><td>2.83%</td></tr>
      <tr><td>Core CPI</td><td>3.14%</td></tr>
      <tr><td>PCE</td><td>2.51%</td></tr>
    </table>
    </body></html>
    """

    SAMPLE_SPAN_HTML = """
    <html><body>
    <div><span>CPI inflation: 2.9%</span></div>
    <div><span>Core CPI: 3.2%</span></div>
    </body></html>
    """

    def test_bs4_parses_table(self):
        """BS4 parser extracts values from table rows."""
        result, strategy = _econ._parse_nowcast_bs4(self.SAMPLE_TABLE_HTML)
        assert result["cpi_yoy"] == 2.83
        assert result["core_cpi_yoy"] == 3.14
        assert result["pce_yoy"] == 2.51
        assert strategy is not None

    def test_bs4_parses_spans(self):
        """BS4 parser extracts values from span elements."""
        result, strategy = _econ._parse_nowcast_bs4(self.SAMPLE_SPAN_HTML)
        assert result.get("cpi_yoy") == 2.9
        assert result.get("core_cpi_yoy") == 3.2
        assert strategy == "span_text"

    def test_regex_parses_cpi(self):
        """Regex fallback extracts CPI percentage."""
        html = "The CPI nowcast is 2.8% for this month."
        result, strategy = _econ._parse_nowcast_regex(html)
        assert result["cpi_yoy"] == 2.8
        assert strategy is not None

    def test_regex_parses_core_cpi(self):
        """Regex fallback extracts Core CPI percentage."""
        html = "Core CPI is expected at 3.1% year-over-year."
        result, strategy = _econ._parse_nowcast_regex(html)
        assert result["core_cpi_yoy"] == 3.1

    def test_bs4_empty_html(self):
        """BS4 parser returns empty dict/None for empty HTML."""
        result, strategy = _econ._parse_nowcast_bs4("<html><body></body></html>")
        assert result == {}
        assert strategy is None


# ===================================================================
# Cross-measure dispersion tests
# ===================================================================

class TestCrossMeasureDispersion:

    def test_cross_measure_dispersion_three_measures(self):
        """With cpi=2.83, core_cpi=3.14, pce=2.51 -> dispersion ~0.26, CI ~0.42."""
        nowcast = {"cpi_yoy": 2.83, "core_cpi_yoy": 3.14, "pce_yoy": 2.51}
        result = _econ._compute_cross_measure_dispersion(nowcast)
        assert result is not None
        # std([2.83, 3.14, 2.51]) ~ 0.258
        assert abs(result - 0.258 * 1.645) < 0.05  # CI width

    def test_cross_measure_dispersion_two_measures(self):
        """With cpi=2.8, core_cpi=3.1 -> dispersion = 0.15, CI ~0.25."""
        nowcast = {"cpi_yoy": 2.8, "core_cpi_yoy": 3.1}
        result = _econ._compute_cross_measure_dispersion(nowcast)
        assert result is not None
        # abs(2.8 - 3.1) / 2 = 0.15, CI = 0.15 * 1.645 ~ 0.247
        assert abs(result - 0.15 * 1.645) < 0.02

    def test_cross_measure_dispersion_one_measure(self):
        """With only one measure, returns None."""
        nowcast = {"cpi_yoy": 2.8}
        result = _econ._compute_cross_measure_dispersion(nowcast)
        assert result is None

    def test_cross_measure_dispersion_stored_in_nowcast(self):
        """After calling the function, nowcast dict has cross_measure_dispersion key."""
        nowcast = {"cpi_yoy": 2.83, "core_cpi_yoy": 3.14, "pce_yoy": 2.51}
        _econ._compute_cross_measure_dispersion(nowcast)
        assert "cross_measure_dispersion" in nowcast


# ===================================================================
# Nowcast source info tests
# ===================================================================

class TestNowcastSourceInfo:

    def test_nowcast_source_info_returns_age(self, tmp_path):
        """Mock cache file with known timestamp, verify age calculation."""
        cache_file = tmp_path / "econ-nowcast-cache.json"
        # Cache saved 2 hours ago
        cached_at = time.time() - 7200
        cache_data = {"cached_at": cached_at, "data": {"cpi_yoy": 2.8}}
        cache_file.write_text(json.dumps(cache_data))

        orig_path = _econ.NOWCAST_CACHE_PATH
        _econ.NOWCAST_CACHE_PATH = cache_file
        try:
            info = _econ._nowcast_source_info()
            assert "nowcast_age_hours" in info
            assert abs(info["nowcast_age_hours"] - 2.0) < 0.1
        finally:
            _econ.NOWCAST_CACHE_PATH = orig_path

    def test_nowcast_source_info_returns_iso_timestamp(self, tmp_path):
        """Verify ISO 8601 format in data_source_timestamp."""
        cache_file = tmp_path / "econ-nowcast-cache.json"
        cached_at = 1709640000.0  # 2024-03-05T12:00:00Z
        cache_data = {"cached_at": cached_at, "data": {"cpi_yoy": 2.8}}
        cache_file.write_text(json.dumps(cache_data))

        orig_path = _econ.NOWCAST_CACHE_PATH
        _econ.NOWCAST_CACHE_PATH = cache_file
        try:
            info = _econ._nowcast_source_info()
            assert "data_source_timestamp" in info
            # Should be an ISO 8601 string ending in Z
            ts = info["data_source_timestamp"]
            assert ts.endswith("Z")
            assert "T" in ts
        finally:
            _econ.NOWCAST_CACHE_PATH = orig_path

    def test_nowcast_source_info_missing_cache(self, tmp_path):
        """Missing cache returns age None and current timestamp."""
        orig_path = _econ.NOWCAST_CACHE_PATH
        _econ.NOWCAST_CACHE_PATH = tmp_path / "nonexistent.json"
        try:
            info = _econ._nowcast_source_info()
            assert info["nowcast_age_hours"] is None
            assert info["data_source_timestamp"].endswith("Z")
        finally:
            _econ.NOWCAST_CACHE_PATH = orig_path


# ===================================================================
# Horizon-scaled edge threshold tests
# ===================================================================

class TestHorizonEdgeThreshold:

    def test_near_term_uses_base_threshold(self):
        """7-day contract should use base 8% edge threshold."""
        threshold = _econ._horizon_edge_threshold(days_to_release=7)
        assert threshold == pytest.approx(0.08, abs=0.005)

    def test_long_horizon_higher_threshold(self):
        """107-day contract should require higher edge than 8%."""
        threshold = _econ._horizon_edge_threshold(days_to_release=107)
        assert threshold > 0.08
        assert threshold < 0.50  # Not absurdly high

    def test_30_day_moderate_threshold(self):
        """30-day contract should have moderate threshold increase."""
        threshold = _econ._horizon_edge_threshold(days_to_release=30)
        assert 0.08 <= threshold <= 0.20

    def test_zero_days_uses_base(self):
        """Release day should use base threshold."""
        threshold = _econ._horizon_edge_threshold(days_to_release=0)
        assert threshold == pytest.approx(0.08, abs=0.005)

    def test_monotonically_increasing(self):
        """Threshold should increase with horizon."""
        prev = _econ._horizon_edge_threshold(0)
        for d in [7, 14, 30, 60, 107]:
            t = _econ._horizon_edge_threshold(d)
            assert t >= prev
            prev = t


# ===================================================================
# Liquidity filter tests
# ===================================================================

class TestLiquidityFilter:

    def test_market_with_no_bid_is_illiquid(self):
        """Market with yes_bid=0 should be filtered."""
        from probability import is_market_liquid
        m = {"yes_bid": 0, "yes_ask": 5, "volume": 100}
        assert not is_market_liquid(m)

    def test_wide_spread_is_illiquid(self):
        """Market with >20c spread should be filtered."""
        from probability import is_market_liquid
        m = {"yes_bid": 10, "yes_ask": 35, "volume": 100}
        assert not is_market_liquid(m)

    def test_low_volume_is_illiquid(self):
        """Market with <10 volume should be filtered."""
        from probability import is_market_liquid
        m = {"yes_bid": 10, "yes_ask": 15, "volume": 5}
        assert not is_market_liquid(m)

    def test_normal_market_is_liquid(self):
        """Market with reasonable spread/volume is liquid."""
        from probability import is_market_liquid
        m = {"yes_bid": 45, "yes_ask": 50, "volume": 100}
        assert is_market_liquid(m)


# ===================================================================
# GDP nowcast classification tests
# ===================================================================

class TestGdpNowcast:

    def test_classify_gdp_market(self):
        """GDP ticker should classify as GDP."""
        assert _econ._classify_econ_market("KXGDP-26Q1-T2.0") == "GDP"

    def test_classify_jobs_market(self):
        """Jobs ticker should classify as JOBS."""
        assert _econ._classify_econ_market("KXJOBS-26MAR-T200") == "JOBS"


# ===================================================================
# Adaptive scan interval tests
# ===================================================================

class TestAdaptiveScanInterval:

    def test_normal_interval(self):
        """Far from release, use configured interval."""
        interval = _econ._adaptive_scan_interval(days_to_release=30)
        assert interval == _econ.SCAN_INTERVAL

    def test_near_release_faster(self):
        """Within 3 days, scan every 30 minutes."""
        interval = _econ._adaptive_scan_interval(days_to_release=2)
        assert interval == 30

    def test_release_day_fastest(self):
        """On release day, scan every 5 minutes."""
        interval = _econ._adaptive_scan_interval(days_to_release=0)
        assert interval == 5

    def test_one_week_out_moderate(self):
        """7 days out, scan every 2 hours."""
        interval = _econ._adaptive_scan_interval(days_to_release=7)
        assert interval == 120

    def test_none_uses_default(self):
        """None days_to_release uses configured interval."""
        interval = _econ._adaptive_scan_interval(days_to_release=None)
        assert interval == _econ.SCAN_INTERVAL
