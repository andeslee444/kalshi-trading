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
    orig_auth = sys.modules.get("kalshi_auth")
    orig_prob = sys.modules.get("probability")
    orig_alloc = sys.modules.get("capital_allocator")

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
    fake_prob.compute_limit_price = lambda *a, **kw: 50
    fake_prob.kalshi_fee_cents = lambda *a, **kw: 1.0
    fake_prob.gas_price_probability = lambda *a, **kw: 0.5
    sys.modules["probability"] = fake_prob

    # Lightweight capital_allocator stub
    fake_alloc = types.ModuleType("capital_allocator")
    fake_alloc.PortfolioAllocator = lambda *a, **kw: MagicMock()
    sys.modules["capital_allocator"] = fake_alloc

    bot_path = Path(__file__).resolve().parent.parent / "src" / "kalshi" / "economics-bot.py"
    spec = importlib.util.spec_from_file_location("economics_bot", bot_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore originals
    if orig_auth is not None:
        sys.modules["kalshi_auth"] = orig_auth
    else:
        sys.modules.pop("kalshi_auth", None)
    if orig_prob is not None:
        sys.modules["probability"] = orig_prob
    else:
        sys.modules.pop("probability", None)
    if orig_alloc is not None:
        sys.modules["capital_allocator"] = orig_alloc
    else:
        sys.modules.pop("capital_allocator", None)

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
