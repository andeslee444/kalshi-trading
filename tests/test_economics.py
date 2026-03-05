"""Tests for economics-bot.py helper functions (Fed matching, gas threshold parsing, nowcast cache)."""

import importlib
import json
import time
import types
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Import helper -- economics-bot.py has a hyphen and performs side-effects
# at import time. Stub kalshi_auth, probability, and capital_allocator.
# ---------------------------------------------------------------------------

def _load_economics_bot():
    # Track all modules we stub so we can restore them
    stubs = ["kalshi_auth", "probability", "capital_allocator",
             "cpi_belief_filter", "scenario_engine", "macro_engine"]
    originals = {name: sys.modules.get(name) for name in stubs}

    # Lightweight kalshi_auth stub
    fake_auth = types.ModuleType("kalshi_auth")
    fake_auth.KalshiClient = lambda *a, **kw: MagicMock()
    fake_auth.setup_unbuffered = lambda: None
    fake_auth.setup_signal_handlers = lambda: None
    fake_auth.is_shutdown_requested = lambda: False
    fake_auth.setup_logging = lambda *a, **kw: __import__("logging").getLogger("test")
    fake_auth.PROJECT_DIR = Path(__file__).resolve().parent.parent
    fake_auth.retry_request = lambda *a, **kw: MagicMock()
    fake_auth.TradeManager = lambda *a, **kw: MagicMock()
    fake_auth.trim_trade_log = lambda *a, **kw: None
    fake_auth.build_market_snapshot = lambda *a, **kw: {}
    fake_auth.HealthCheckMonitor = lambda *a, **kw: MagicMock()
    fake_auth.OrderMonitor = lambda *a, **kw: MagicMock()
    def _fake_atomic_write(path, data):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2))
    fake_auth._atomic_write_json = _fake_atomic_write
    fake_auth.ScanSummary = type("ScanSummary", (), {
        "__init__": lambda self, *a, **kw: None,
        "skip": lambda self, *a, **kw: None,
        "source_ok": lambda self, *a, **kw: None,
        "source_fail": lambda self, *a, **kw: None,
        "finalize": lambda self, *a, **kw: {},
        "markets_fetched": 0,
        "markets_evaluated": 0,
        "trades_placed": 0,
    })
    sys.modules["kalshi_auth"] = fake_auth

    # Lightweight probability stub
    fake_prob = types.ModuleType("probability")
    fake_prob.econ_nowcast_probability = lambda *a, **kw: 0.5
    fake_prob.cpi_nowcast_sigma = lambda *a, **kw: 0.05
    fake_prob.gdp_nowcast_sigma = lambda *a, **kw: 0.10
    fake_prob.quarter_kelly = lambda *a, **kw: (0, 0)
    fake_prob.uncertainty_kelly = lambda *a, **kw: (0, 0, {})
    fake_prob.compute_limit_price = lambda *a, **kw: 50
    fake_prob.kalshi_fee_cents = lambda *a, **kw: 1.0
    fake_prob.gas_price_probability = lambda *a, **kw: 0.5
    fake_prob._norm_cdf = lambda x: 0.5 * (1 + __import__("math").erf(x / __import__("math").sqrt(2)))
    sys.modules["probability"] = fake_prob

    # Lightweight capital_allocator stub
    fake_alloc = types.ModuleType("capital_allocator")
    fake_alloc.PortfolioAllocator = lambda *a, **kw: MagicMock()
    sys.modules["capital_allocator"] = fake_alloc

    # Lightweight cpi_belief_filter stub
    fake_belief = types.ModuleType("cpi_belief_filter")
    fake_belief.CPIBeliefFilter = type("CPIBeliefFilter", (), {
        "__init__": lambda self, *a, **kw: None,
        "update": lambda self, *a, **kw: None,
        "posterior": property(lambda self: (2.8, 0.10)),
    })
    sys.modules["cpi_belief_filter"] = fake_belief

    # Lightweight scenario_engine stub
    fake_scenario = types.ModuleType("scenario_engine")
    fake_scenario.compute_scenario_weights = lambda *a, **kw: {}
    fake_scenario.scenario_probability = lambda *a, **kw: MagicMock(
        probability=0.5, agreement=0.8, per_scenario={}, weights_used={})
    sys.modules["scenario_engine"] = fake_scenario

    # macro_engine — set to None (mimics ImportError path)
    fake_macro = types.ModuleType("macro_engine")
    fake_macro.MacroEngine = None
    sys.modules["macro_engine"] = fake_macro

    bot_path = Path(__file__).resolve().parent.parent / "src" / "kalshi" / "economics-bot.py"
    spec = importlib.util.spec_from_file_location("economics_bot", bot_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore originals
    for name in stubs:
        if originals[name] is not None:
            sys.modules[name] = originals[name]
        else:
            sys.modules.pop(name, None)

    return mod


_econ = _load_economics_bot()


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
