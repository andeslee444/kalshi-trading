"""Tests for source-monitor optimizations: time-decay sigma, data freshness,
cross-market consistency, retry logic, and edge thresholds."""

import math
import datetime
import sys
import os
import importlib.util
import tempfile
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from probability import album_data_sigma, boxoffice_data_sigma, _reset_calibration
from probability import nws_sigma_for_hour, nws_probability


# === Module import helper for source-monitor.py ===
# source-monitor.py has module-level initialization (KalshiClient, TradeManager, etc.)
# that requires API keys. Stub the dependencies before importing.

_sm_module = None


def _load_source_monitor():
    """Load source-monitor.py with stubbed dependencies. Cached after first call."""
    global _sm_module
    if _sm_module is not None:
        return _sm_module

    # Stub kalshi_auth so module-level KalshiClient() doesn't need real keys
    mock_auth = MagicMock()
    mock_auth.KalshiClient.return_value = MagicMock()
    mock_auth.setup_unbuffered = MagicMock()
    mock_auth.setup_signal_handlers = MagicMock()
    mock_auth.setup_logging.return_value = MagicMock()
    mock_auth.TradeManager.return_value = MagicMock()
    mock_auth.trim_trade_log = MagicMock()
    mock_auth.PROJECT_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mock_auth.HealthCheckMonitor.return_value = MagicMock()
    mock_auth.OrderMonitor.return_value = MagicMock()
    mock_auth.ScanSummary = MagicMock()
    mock_auth.build_market_snapshot = MagicMock(return_value={})
    mock_auth.CITY_TIMEZONES = {}
    mock_auth._local_today = MagicMock()
    mock_auth.round_half_up = round
    mock_auth.load_trades.return_value = []
    mock_auth.fetch_parallel = MagicMock(return_value={})
    mock_auth.retry_request = MagicMock()
    mock_auth.is_market_liquid = MagicMock(return_value=True)
    mock_auth.compute_limit_price = MagicMock(return_value=50)
    mock_auth.kalshi_fee_cents = MagicMock(return_value=1)

    # Stub capital_allocator
    mock_allocator_mod = MagicMock()
    mock_allocator_mod.PortfolioAllocator.return_value = MagicMock()

    # Stub hdd_parser
    mock_hdd = MagicMock()
    mock_hdd.get_album_sales = MagicMock(return_value=[])
    mock_hdd.compute_data_age_hours = MagicMock(return_value=0)
    mock_hdd.parse_album_threshold = MagicMock(return_value=None)

    # Stub ticker_utils
    mock_ticker = MagicMock()

    # Write a minimal config file for the module to load
    _config = {
        "maxTradeAmount": 25, "maxDailyTrades": 25, "maxDailyLoss": 50,
        "sources": {
            "hdd": {"enabled": True, "intervalMinutes": 15, "chartSlugs": ["hits-top-50"]},
            "boxoffice": {"enabled": True, "intervalMinutes": 30, "activeDays": ["Friday", "Saturday", "Sunday", "Monday"], "tmdbEnabled": False},
            "nws": {"enabled": True, "intervalMinutes": 10, "stations": {}},
        },
    }
    config_dir = Path(tempfile.mkdtemp()) / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "kalshi-monitor-config.json").write_text(json.dumps(_config))
    # Ensure data dirs exist for module-level mkdir calls
    data_dir = config_dir.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "kalshi-source-snapshots").mkdir(parents=True, exist_ok=True)
    # Create empty trades file
    (data_dir / "kalshi-monitor-trades.json").write_text("[]")

    # Save originals before stubbing
    stub_names = ["kalshi_auth", "capital_allocator", "hdd_parser", "ticker_utils"]
    originals = {name: sys.modules.get(name) for name in stub_names}

    # Inject stubs
    sys.modules["kalshi_auth"] = mock_auth
    sys.modules["capital_allocator"] = mock_allocator_mod
    sys.modules["hdd_parser"] = mock_hdd
    sys.modules["ticker_utils"] = mock_ticker

    # Temporarily patch PROJECT_DIR so config loads from our temp dir
    mock_auth.PROJECT_DIR = config_dir.parent

    src_dir = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "src" / "kalshi"
    spec = importlib.util.spec_from_file_location("source_monitor", src_dir / "source-monitor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Restore originals
    for name in stub_names:
        if originals[name] is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = originals[name]

    _sm_module = mod
    return mod


class TestTimeDecaySigma:
    """Test time-decay sigma for album and box office data."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_album_default_no_decay(self):
        """With hours_since_publication=0, behavior is identical to current."""
        assert album_data_sigma(0) == 0.15  # Monday
        assert album_data_sigma(2) == 0.10  # Wednesday
        assert album_data_sigma(4) == 0.05  # Friday

    def test_album_friday_48h_decay(self):
        """48 hours of staleness increases sigma by 50%."""
        result = album_data_sigma(4, hours_since_publication=48)
        assert result == pytest.approx(0.05 * 1.5, rel=1e-3)

    def test_album_friday_96h_decay(self):
        """96 hours of staleness increases sigma by 100%."""
        result = album_data_sigma(4, hours_since_publication=96)
        assert result == pytest.approx(0.05 * 2.0, rel=1e-3)

    def test_album_decay_capped_at_3x(self):
        """Decay factor caps at 3x regardless of age."""
        result = album_data_sigma(4, hours_since_publication=500)
        assert result == pytest.approx(0.05 * 3.0, rel=1e-3)

    def test_album_monday_no_decay(self):
        """Monday with 0 hours_since_publication is unchanged."""
        assert album_data_sigma(0, hours_since_publication=0) == 0.15

    def test_album_monday_with_decay(self):
        """Monday with 48h decay."""
        result = album_data_sigma(0, hours_since_publication=48)
        assert result == pytest.approx(0.15 * 1.5, rel=1e-3)

    def test_boxoffice_default_no_decay(self):
        """With hours_since_publication=0, behavior is identical to current."""
        assert boxoffice_data_sigma(4) == 0.12  # Friday
        assert boxoffice_data_sigma(6) == 0.05  # Sunday
        assert boxoffice_data_sigma(0) == 0.04  # Monday

    def test_boxoffice_friday_48h_decay(self):
        """48h staleness on Friday estimate."""
        result = boxoffice_data_sigma(4, hours_since_publication=48)
        assert result == pytest.approx(0.12 * 1.5, rel=1e-3)

    def test_boxoffice_decay_capped_at_3x(self):
        """Decay caps at 3x."""
        result = boxoffice_data_sigma(0, hours_since_publication=500)
        assert result == pytest.approx(0.04 * 3.0, rel=1e-3)

    def test_boxoffice_sunday_24h_decay(self):
        """Sunday with 24h decay."""
        result = boxoffice_data_sigma(6, hours_since_publication=24)
        assert result == pytest.approx(0.05 * 1.25, rel=1e-3)


class TestNwsSigma:
    """Test extracted NWS sigma helper."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_overnight_sigma(self):
        """Before 6am, sigma should be 5.0 (max uncertainty)."""
        assert nws_sigma_for_hour(3) == 5.0

    def test_morning_sigma(self):
        """At hour 6, sigma should be ~4.0."""
        sigma = nws_sigma_for_hour(6)
        assert sigma == pytest.approx(4.0, abs=0.1)

    def test_midday_sigma(self):
        """At hour 12, sigma should be ~1.4."""
        sigma = nws_sigma_for_hour(12)
        assert sigma == pytest.approx(4.0 * math.exp(-0.18 * 6), abs=0.1)

    def test_afternoon_sigma(self):
        """At hour 15, sigma should be ~0.7."""
        sigma = nws_sigma_for_hour(15)
        assert sigma == pytest.approx(4.0 * math.exp(-0.18 * 9), abs=0.1)

    def test_evening_sigma_floor(self):
        """At hour 20, sigma should hit the 0.5 floor."""
        sigma = nws_sigma_for_hour(20)
        assert sigma == 0.5

    def test_nws_probability_unchanged(self):
        """Verify nws_probability still works identically after refactor."""
        # Running high 85F, threshold 80F, direction T, hour 15
        prob = nws_probability(85, 80, "T", 15)
        assert prob > 0.9

    def test_nws_probability_bracket(self):
        """Bracket probability still works."""
        prob = nws_probability(80, 80, "B", 12)
        assert 0 < prob < 1

    def test_sigma_monotonically_decreasing(self):
        """Sigma should decrease from morning to evening."""
        sigmas = [nws_sigma_for_hour(h) for h in range(6, 20)]
        for i in range(len(sigmas) - 1):
            assert sigmas[i] >= sigmas[i + 1]


# === Integration tests using source-monitor module import ===


class TestRetryLogic:
    """Test _check_with_retry from source-monitor.py (lines 69-88)."""

    def test_success_on_first_try(self):
        sm = _load_source_monitor()
        check_fn = MagicMock()
        ss = MagicMock()
        sm._check_with_retry(check_fn, "test_source", None, ss)
        assert check_fn.call_count == 1
        ss.source_ok.assert_called_once_with("test_source")

    def test_success_on_retry(self):
        sm = _load_source_monitor()
        check_fn = MagicMock(side_effect=[Exception("transient"), None])
        ss = MagicMock()
        with patch("time.sleep"):
            sm._check_with_retry(check_fn, "test_source", None, ss)
        assert check_fn.call_count == 2
        ss.source_ok.assert_called_once()

    def test_failure_after_max_retries(self):
        sm = _load_source_monitor()
        check_fn = MagicMock(side_effect=Exception("persistent"))
        ss = MagicMock()
        with patch("time.sleep"):
            sm._check_with_retry(check_fn, "test_source", None, ss, max_retries=2)
        assert check_fn.call_count == 3
        ss.source_fail.assert_called_once()

    def test_no_scan_summary(self):
        """ss=None should not raise."""
        sm = _load_source_monitor()
        check_fn = MagicMock()
        sm._check_with_retry(check_fn, "test_source", None, None)
        check_fn.assert_called_once()


class TestCrossMarketConsistency:
    """Test _validate_market_cluster from source-monitor.py (lines 90-109)."""

    def test_single_market_passthrough(self):
        sm = _load_source_monitor()
        signals = [("MKT1", 100, 0.80)]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 1

    def test_monotonic_pass(self):
        """P(>100) > P(>200) should pass."""
        sm = _load_source_monitor()
        signals = [
            ("MKT1", 100, 0.90),
            ("MKT2", 200, 0.60),
            ("MKT3", 300, 0.30),
        ]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 3

    def test_non_monotonic_rejection(self):
        """P(>200) > P(>100) violates monotonicity — reject all."""
        sm = _load_source_monitor()
        signals = [
            ("MKT1", 100, 0.50),
            ("MKT2", 200, 0.80),  # higher prob at higher threshold = inconsistent
        ]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 0

    def test_tolerance_boundary(self):
        """Small violations within 5% tolerance should pass."""
        sm = _load_source_monitor()
        signals = [
            ("MKT1", 100, 0.80),
            ("MKT2", 200, 0.83),  # 3% higher — within 5% tolerance
        ]
        result = sm._validate_market_cluster("artist", signals)
        assert len(result) == 2

    def test_empty_list(self):
        sm = _load_source_monitor()
        result = sm._validate_market_cluster("artist", [])
        assert result == []


class TestNWSEdgeThresholds:
    """Test CI-based edge threshold logic from source-monitor.py (lines 938-950).

    The logic:
      sigma = nws_sigma_for_hour(hour)
      margin = abs(running_high - threshold)
      ci_99 = 2.576 * sigma
      if margin > ci_99:        min_edge = 0.05 (very confident)
      elif margin > ci_99 * 0.5: min_edge = 0.10 (moderate)
      else:                      min_edge = 0.15 (uncertain)
      Brackets always: min_edge = 0.20
    """

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def _compute_min_edge(self, running_high, threshold, hour, is_bracket=False):
        """Replicate the edge threshold logic for testing."""
        if is_bracket:
            return 0.20
        sigma = nws_sigma_for_hour(hour)
        margin = abs(running_high - threshold)
        ci_99 = 2.576 * sigma
        if margin > ci_99:
            return 0.05
        elif margin > ci_99 * 0.5:
            return 0.10
        else:
            return 0.15

    def test_very_confident_afternoon(self):
        """Hour 15, margin 5F >> ci_99 ~1.8F -> min_edge 0.05."""
        min_edge = self._compute_min_edge(90, 85, 15)
        assert min_edge == 0.05

    def test_moderate_confidence_midday(self):
        """Hour 12, margin 2F, ci_99 ~3.6F, margin > ci/2 -> min_edge 0.10."""
        min_edge = self._compute_min_edge(82, 80, 12)
        assert min_edge == 0.10

    def test_uncertain_morning(self):
        """Hour 8, margin 1F, ci_99 ~6.8F -> min_edge 0.15."""
        min_edge = self._compute_min_edge(81, 80, 8)
        assert min_edge == 0.15

    def test_bracket_always_020(self):
        """Bracket markets always require 20% edge."""
        min_edge = self._compute_min_edge(80, 80, 18, is_bracket=True)
        assert min_edge == 0.20

    def test_evening_confident(self):
        """Hour 18, sigma=0.5, ci_99=1.29, margin 5F -> very confident."""
        min_edge = self._compute_min_edge(85, 80, 18)
        assert min_edge == 0.05

    def test_overnight_uncertain(self):
        """Hour 4, sigma=5.0, ci_99=12.88, margin 3F -> uncertain."""
        min_edge = self._compute_min_edge(83, 80, 4)
        assert min_edge == 0.15


class TestDataFreshness:
    """Test data freshness gating logic.

    The source-monitor uses compute_data_age_hours() from hdd_parser
    and MAX_DATA_AGE_HOURS = 168 (7 days) from source-monitor.py:65.
    """

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_max_data_age_constant(self):
        sm = _load_source_monitor()
        assert sm.MAX_DATA_AGE_HOURS == 168

    def test_sigma_increase_at_72h(self):
        """At 72 hours, sigma should be 1.75x base (50% per 48h)."""
        result = album_data_sigma(0, hours_since_publication=72)
        expected = 0.15 * (1.0 + 0.5 * (72 / 48))
        assert result == pytest.approx(expected, rel=1e-3)

    def test_sigma_at_168h_before_rejection(self):
        """At exactly 168h (7 days), probability.py still computes sigma (rejection is in bot logic)."""
        result = album_data_sigma(0, hours_since_publication=168)
        # 1 + 0.5 * 168/48 = 2.75 -> sigma = 0.15 * 2.75 = 0.4125
        assert result == pytest.approx(0.15 * 2.75, rel=1e-3)

    def test_fresh_data_no_decay(self):
        """Data at 0 hours has no decay applied."""
        result = album_data_sigma(4, hours_since_publication=0)
        assert result == 0.05  # Friday base, no decay


class TestSourceAwareSigma:
    """Test source-aware album_data_sigma with different HDD data sources."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_hits_top_50_lowest_sigma(self):
        """HDD Hits Top 50 is the settlement source — should have lowest sigma."""
        sigma = album_data_sigma(4, source="hdd-hits-top-50")
        assert sigma == 0.02

    def test_midweek_20_higher_sigma(self):
        """Midweek 20 is an estimate — should have higher sigma than Top 50."""
        sigma_top50 = album_data_sigma(4, source="hdd-hits-top-50")
        sigma_mid = album_data_sigma(4, source="hdd-midweek-20")
        assert sigma_mid > sigma_top50

    def test_article_highest_sigma(self):
        """Article text extraction has highest uncertainty."""
        sigma_article = album_data_sigma(4, source="hdd-article")
        assert sigma_article >= 0.15

    def test_unknown_source_uses_day_default(self):
        """Unknown source falls back to day-of-week default."""
        sigma = album_data_sigma(4, source="unknown-source")
        assert sigma == 0.05  # Friday default

    def test_none_source_uses_day_default(self):
        """No source falls back to day-of-week default."""
        sigma = album_data_sigma(4, source=None)
        assert sigma == album_data_sigma(4)
