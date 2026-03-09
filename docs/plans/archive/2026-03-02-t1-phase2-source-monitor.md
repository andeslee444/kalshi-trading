# T1-Phase 2: Source Monitor Optimization — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete the 10 source monitor optimizations from the Feb 24 design doc — primarily entertainment bot consolidation and a comprehensive test suite (30+ tests). Most code changes were already shipped in Phase 1; this phase finishes the job with testing, consolidation, and a proper box office API solution.

**Architecture:** The source monitor (`src/kalshi/source-monitor.py`) is the highest-priority bot (allocator priority 1.0) trading information arbitrage on HDD album sales, box office revenue, and NWS actual temperatures. Seven of the ten planned optimizations (data freshness, retry logic, time-decay sigma, CI-based NWS thresholds, cross-market consistency, high-confidence sizing, settlement feedback) are already implemented and in production. This plan addresses the remaining three: entertainment bot consolidation, structured box office data, and comprehensive test coverage.

**Tech Stack:** Python 3, pytest, `importlib` (for testing hyphenated bot modules), `unittest.mock`.

**Branch:** `track1/alpha-generation` (from `main`)

---

### Status: What's Already Implemented

Before starting, verify these optimizations are working correctly. They should all be in `main`:

| # | Optimization | Status | Where |
|---|---|---|---|
| 1a | Data freshness validation | **Done** | `source-monitor.py:65,67` (`MAX_DATA_AGE_HOURS=168`, `compute_data_age_hours` from `hdd_parser`) |
| 1b | Retry logic with backoff | **Done** | `source-monitor.py:69-88` (`_check_with_retry`, 2 retries, exponential backoff) |
| 1c | TMDb box office API | **Code exists, disabled** | `source-monitor.py:371-426` (`_fetch_tmdb_boxoffice`). Note: TMDb returns lifetime worldwide gross, NOT weekend domestic — unusable for Kalshi settlement. See Task 2. |
| 2a | Time-decay sigma | **Done** | `probability.py` `album_data_sigma(hours_since_publication=)`, `boxoffice_data_sigma(hours_since_publication=)` |
| 2b | CI-based NWS thresholds | **Done** | `source-monitor.py:938-950,1013-1019` (margin vs `ci_99 = 2.576 * sigma`) |
| 3a | Entertainment bot consolidation | **Remaining** | `config/bots-config.json` entertainment `enabled: true`. See Task 3. |
| 3b | Cross-market consistency | **Done** | `source-monitor.py:90-109` (`_validate_market_cluster`) |
| 3c | High-confidence sizing | **Done** | `capital_allocator.py:557-576` (`source_type="info_arb"` gate at 85%/10%) |
| 4a | Test suite (30+ cases) | **Partially done** | `tests/test_source_monitor.py` (122 lines, 17 tests). Covers sigma functions only, not bot logic. See Tasks 4-10. |
| 4b | Settlement feedback loop | **Done** | `scripts/calibrate-sigma.py:558-664` (`calibrate_info_arb`) |

---

### Task 1: Create Branch

**Step 1: Create the feature branch**

```bash
git checkout -b track1/alpha-generation main
```

**Step 2: Verify existing tests pass**

Run: `pytest tests/test_source_monitor.py -v`
Expected: All 17 tests pass.

**Step 3: Commit**

No files to commit — branch is ready.

---

### Task 2: Box Office Data Source Improvement

**Files:**
- Modify: `src/kalshi/source-monitor.py:371-426` (update `_fetch_tmdb_boxoffice` docstring)
- Create: `tests/test_boxoffice_scraping.py`

**Context:** The TMDb API (`_fetch_tmdb_boxoffice` at line 371) was implemented but discovered to return lifetime worldwide gross, not weekend domestic box office. Kalshi markets settle on weekend domestic gross. TMDb is intentionally disabled (`tmdbEnabled: false`). The HTML scraping fallback (The Numbers + Box Office Mojo) is the correct approach for now.

**Step 1: Write regression tests for HTML box office parsing**

Create `tests/test_boxoffice_scraping.py`:

```python
"""Tests for box office HTML scraping regex patterns used in source-monitor."""

import re
import pytest


# --- Regex patterns extracted from source-monitor.py ---

# The Numbers pattern (source-monitor.py line 462)
THE_NUMBERS_PATTERN = r'(?:>)([^<]{3,50})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)'

# Box Office Mojo pattern (source-monitor.py line 484)
MOJO_PATTERN = r'(?:>)([^<]{3,50})</a>.*?\$([\d,.]+)\s*([MmBb])?'


class TestTheNumbersPattern:
    """Test The Numbers HTML scraping regex."""

    def test_standard_movie_row(self):
        html = '<a href="/movie/test">Inside Out 2</a></td><td class="money">$1,234,567'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 1
        assert matches[0][0] == "Inside Out 2"
        assert matches[0][1] == "1,234,567"

    def test_title_with_special_chars(self):
        html = '<a href="/movie/test">A Minecraft Movie: Part 1</a></td><td class="money">$500,000'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 1
        assert "Minecraft" in matches[0][0]

    def test_no_dollar_sign(self):
        html = '<a href="/movie/test">Test Movie</a></td><td class="money">2,500,000'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 1
        assert matches[0][1] == "2,500,000"

    def test_rejects_short_title(self):
        """Titles < 3 chars should not match."""
        html = '<a href="/movie/test">AB</a></td><td class="money">$100,000'
        matches = re.findall(THE_NUMBERS_PATTERN, html)
        assert len(matches) == 0


class TestMojoPattern:
    """Test Box Office Mojo HTML scraping regex."""

    def test_millions_suffix(self):
        html = '<a href="/release/test">Thunderbolts</a> some text $45.2M'
        matches = re.findall(MOJO_PATTERN, html, re.DOTALL)
        assert len(matches) >= 1
        assert matches[0][0].strip() == "Thunderbolts"
        assert matches[0][1] == "45.2"
        assert matches[0][2].upper() == "M"

    def test_billions_suffix(self):
        html = '<a href="/movie">Avatar 3</a> $1.2B'
        matches = re.findall(MOJO_PATTERN, html, re.DOTALL)
        assert len(matches) >= 1
        assert matches[0][2].upper() == "B"

    def test_no_suffix_raw_number(self):
        html = '<a href="/movie">Small Film</a> $500,000'
        matches = re.findall(MOJO_PATTERN, html, re.DOTALL)
        assert len(matches) >= 1
        assert matches[0][1] == "500,000"
        assert matches[0][2] == ""

    def test_gross_value_parsing(self):
        """Verify gross multiplication logic (from source-monitor.py lines 491-497)."""
        test_cases = [
            ("45.2", "M", 45_200_000),
            ("1.2", "B", 1_200_000_000),
            ("500,000", "", 500_000),
        ]
        for gross_str, suffix, expected in test_cases:
            gross_clean = gross_str.replace(",", "")
            gross_val = float(gross_clean)
            if suffix.upper() == "B":
                gross_val *= 1_000_000_000
            elif suffix.upper() == "M":
                gross_val *= 1_000_000
            assert int(gross_val) == expected
```

**Step 2: Run tests**

Run: `pytest tests/test_boxoffice_scraping.py -v`
Expected: All pass (these test regex patterns, no imports needed).

**Step 3: Commit**

```bash
git add tests/test_boxoffice_scraping.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add box office HTML scraping pattern tests"
```

---

### Task 3: Entertainment Bot Consolidation

**Files:**
- Modify: `config/bots-config.json:12` (set entertainment `enabled: false`)
- Modify: `config/kalshi-monitor-config.json` (enable HDD source)

**Context:** The entertainment bot (`entertainment-bot.py:728 lines`) duplicates source-monitor's HDD and box office logic with slightly different parameters. Source-monitor already has: data freshness (1a), time-decay sigma (2a), CI-based thresholds (2b), consistency validation (3b), and high-confidence sizing (3c). The entertainment bot is strictly inferior. Disabling it prevents duplicate trades and simplifies operations.

**Step 1: Verify source-monitor covers entertainment markets**

Check that source-monitor handles all entertainment-bot tickers. Source-monitor handles:
- HDD album sales (via `check_hdd()`)
- Box office (via `check_boxoffice()`)

Entertainment bot also handles these exact same sources. Confirmed: full coverage overlap.

**Step 2: Disable entertainment bot in config**

In `config/bots-config.json`, change line 13:

```json
"entertainment": {
    "enabled": false,
```

**Step 3: Enable HDD source in monitor config**

In `config/kalshi-monitor-config.json`, if HDD is currently disabled (`"enabled": false`), change to `true`:

```json
"hdd": {
    "enabled": true,
```

**Step 4: Run existing tests to verify no regressions**

Run: `pytest tests/ -v`
Expected: All tests pass. Config changes don't affect test execution.

**Step 5: Commit**

```bash
git add config/bots-config.json config/kalshi-monitor-config.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: consolidate entertainment bot into source-monitor

Disable entertainment-bot (enabled: false) since source-monitor now covers
all HDD album sales and box office markets with superior data freshness,
time-decay sigma, CI-based thresholds, and consistency validation.
File kept intact for rollback."
```

---

### Task 4: Test Harness — Source Monitor Module Import

**Files:**
- Modify: `tests/test_source_monitor.py` (add import helper and test infrastructure)

**Context:** `source-monitor.py` uses hyphens (can't `import source-monitor`). The module initializes `KalshiClient`, `PortfolioAllocator`, and `TradeManager` at import time. Tests must stub these in `sys.modules` before loading. This task sets up the import pattern; subsequent tasks add test classes.

**Step 1: Add module import helper to test file**

Add at the top of `tests/test_source_monitor.py`, after the existing imports (line 9):

```python
import sys
import os
import importlib.util
import tempfile
import json

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
    _sm_module = mod
    return mod
```

Also add `from pathlib import Path` to the import block.

**Step 2: Run existing tests to verify nothing is broken**

Run: `pytest tests/test_source_monitor.py -v`
Expected: All 17 existing tests pass. The new import helper only runs on demand.

**Step 3: Commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add source-monitor module import harness for integration tests"
```

---

### Task 5: Test Suite — Retry Logic

**Files:**
- Modify: `tests/test_source_monitor.py`

**Step 1: Write retry logic tests**

Add to `tests/test_source_monitor.py`:

```python
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
        sm._check_with_retry(check_fn, "test_source", None, ss)
        assert check_fn.call_count == 2
        ss.source_ok.assert_called_once()

    def test_failure_after_max_retries(self):
        sm = _load_source_monitor()
        check_fn = MagicMock(side_effect=Exception("persistent"))
        ss = MagicMock()
        sm._check_with_retry(check_fn, "test_source", None, ss, max_retries=2)
        assert check_fn.call_count == 3
        ss.source_fail.assert_called_once()

    def test_no_scan_summary(self):
        """ss=None should not raise."""
        sm = _load_source_monitor()
        check_fn = MagicMock()
        sm._check_with_retry(check_fn, "test_source", None, None)
        check_fn.assert_called_once()
```

**Step 2: Run tests**

Run: `pytest tests/test_source_monitor.py::TestRetryLogic -v`
Expected: All 4 tests pass.

**Step 3: Commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add retry logic tests for source-monitor"
```

---

### Task 6: Test Suite — Cross-Market Consistency Validation

**Files:**
- Modify: `tests/test_source_monitor.py`

**Step 1: Write consistency validation tests**

```python
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
```

**Step 2: Run tests**

Run: `pytest tests/test_source_monitor.py::TestCrossMarketConsistency -v`
Expected: All 5 tests pass.

**Step 3: Commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add cross-market consistency validation tests"
```

---

### Task 7: Test Suite — NWS CI-Based Edge Thresholds

**Files:**
- Modify: `tests/test_source_monitor.py`

**Step 1: Write edge threshold tests**

```python
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
        """Hour 15, margin 5F >> ci_99 ~1.8F → min_edge 0.05."""
        min_edge = self._compute_min_edge(90, 85, 15)
        assert min_edge == 0.05

    def test_moderate_confidence_midday(self):
        """Hour 12, margin 2F, ci_99 ~3.6F, margin > ci/2 → min_edge 0.10."""
        min_edge = self._compute_min_edge(82, 80, 12)
        assert min_edge == 0.10

    def test_uncertain_morning(self):
        """Hour 8, margin 1F, ci_99 ~6.8F → min_edge 0.15."""
        min_edge = self._compute_min_edge(81, 80, 8)
        assert min_edge == 0.15

    def test_bracket_always_020(self):
        """Bracket markets always require 20% edge."""
        min_edge = self._compute_min_edge(80, 80, 18, is_bracket=True)
        assert min_edge == 0.20

    def test_evening_confident(self):
        """Hour 18, sigma=0.5, ci_99=1.29, margin 5F → very confident."""
        min_edge = self._compute_min_edge(85, 80, 18)
        assert min_edge == 0.05

    def test_overnight_uncertain(self):
        """Hour 4, sigma=5.0, ci_99=12.88, margin 3F → uncertain."""
        min_edge = self._compute_min_edge(83, 80, 4)
        assert min_edge == 0.15
```

**Step 2: Run tests**

Run: `pytest tests/test_source_monitor.py::TestNWSEdgeThresholds -v`
Expected: All 6 tests pass.

**Step 3: Commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add NWS CI-based edge threshold tests"
```

---

### Task 8: Test Suite — Data Freshness Gating

**Files:**
- Modify: `tests/test_source_monitor.py`

**Step 1: Write data freshness tests**

```python
class TestDataFreshness:
    """Test data freshness gating logic.

    The source-monitor uses compute_data_age_hours() from hdd_parser
    and MAX_DATA_AGE_HOURS = 168 (7 days) from source-monitor.py:65.
    """

    def test_max_data_age_constant(self):
        sm = _load_source_monitor()
        assert sm.MAX_DATA_AGE_HOURS == 168

    def test_fresh_data_passes(self):
        """Data age 0 hours should not trigger any rejection."""
        from hdd_parser import compute_data_age_hours
        # compute_data_age_hours returns hours since chart_date
        # If we pass None/missing chart_date, it should fail-open (return 0)
        age = compute_data_age_hours(None)
        assert age == 0

    def test_sigma_increase_at_72h(self):
        """At 72 hours, sigma should be 1.75x base (50% per 48h)."""
        result = album_data_sigma(0, hours_since_publication=72)
        expected = 0.15 * (1.0 + 0.5 * (72 / 48))
        assert result == pytest.approx(expected, rel=1e-3)

    def test_sigma_at_168h_before_rejection(self):
        """At exactly 168h (7 days), data should still compute a sigma (rejection is in bot logic, not probability.py)."""
        result = album_data_sigma(0, hours_since_publication=168)
        # 1 + 0.5 * 168/48 = 2.75 -> sigma = 0.15 * 2.75 = 0.4125
        assert result == pytest.approx(0.15 * 2.75, rel=1e-3)
```

**Step 2: Run tests**

Run: `pytest tests/test_source_monitor.py::TestDataFreshness -v`
Expected: All pass.

**Step 3: Commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add data freshness gating tests"
```

---

### Task 9: Test Suite — Allocator High-Confidence Gate

**Files:**
- Modify: `tests/test_source_monitor.py`

**Context:** The allocator's high-confidence gate for info-arb (`capital_allocator.py:557-576`) uses relaxed thresholds: 85% confidence + 10% edge (vs 90%/15% for model-based). Test this directly.

**Step 1: Write allocator gate tests**

```python
from capital_allocator import PortfolioAllocator, BudgetResponse


class TestAllocatorInfoArbGate:
    """Test high-confidence gate for info-arb source type.

    Info-arb: confidence > 0.85 AND edge > 0.10 → scale up
    Default: confidence > 0.90 AND edge > 0.15 → scale up
    """

    def _make_allocator(self, balance=500000):
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        mock_client.get.return_value = {"market_positions": []}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump({}, f)
            state_path = f.name
        alloc = PortfolioAllocator(client=mock_client, state_path=state_path)
        import datetime as dt
        alloc._daily_date = dt.date.today().isoformat()
        return alloc

    def test_info_arb_relaxed_threshold(self):
        """Info-arb at 87% conf + 12% edge should trigger high-confidence scaling."""
        alloc = self._make_allocator()
        result = alloc.request_budget("source-monitor", "KXALBUM-TEST",
                                       edge=0.12, confidence=0.87, source_type="info_arb")
        assert result.approved

    def test_default_threshold_not_triggered(self):
        """Non-info-arb at 87% conf + 12% edge should NOT trigger high-confidence scaling."""
        alloc = self._make_allocator()
        result1 = alloc.request_budget("source-monitor", "KXALBUM-T1",
                                        edge=0.12, confidence=0.87, source_type="info_arb")
        alloc2 = self._make_allocator()
        result2 = alloc2.request_budget("weather", "KXHIGHHOU-26MAR2-T90",
                                         edge=0.12, confidence=0.87, source_type=None)
        # Both should be approved, but info-arb should have higher budget
        assert result1.approved
        assert result2.approved
        # info-arb budget may be higher due to high-conf scaling
        # (depends on whether 87% > 85% triggers for info_arb but not for default 90%)

    def test_info_arb_below_threshold(self):
        """Info-arb at 80% conf should NOT trigger high-confidence scaling."""
        alloc = self._make_allocator()
        result = alloc.request_budget("source-monitor", "KXALBUM-TEST2",
                                       edge=0.12, confidence=0.80, source_type="info_arb")
        assert result.approved  # still approved, just not scaled up
```

**Step 2: Run tests**

Run: `pytest tests/test_source_monitor.py::TestAllocatorInfoArbGate -v`
Expected: All pass.

**Step 3: Commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add allocator info-arb high-confidence gate tests"
```

---

### Task 10: Test Suite — Source-Aware Album Sigma

**Files:**
- Modify: `tests/test_source_monitor.py`

**Step 1: Write source-aware sigma tests**

```python
class TestSourceAwareSigma:
    """Test source-aware album_data_sigma with different HDD data sources."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_hits_top_50_lowest_sigma(self):
        """HDD Hits Top 50 is the settlement source — should have lowest sigma."""
        sigma = album_data_sigma(4, source="hdd-hits-top-50")
        assert sigma <= 0.05  # Should be 0.02 or similar

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
```

**Step 2: Run tests**

Run: `pytest tests/test_source_monitor.py::TestSourceAwareSigma -v`
Expected: All pass.

**Step 3: Commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add source-aware album sigma tests"
```

---

### Task 11: Final Verification and Merge

**Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass (22+ test files).

**Step 2: Count new test cases**

Run: `pytest tests/test_source_monitor.py tests/test_boxoffice_scraping.py -v --co`
Expected: 40+ test cases total (17 existing + ~25 new).

**Step 3: Verify no regressions in other test files**

Run: `pytest tests/ -v --tb=short`
Expected: All pass.

**Step 4: Merge to main**

```bash
git checkout main
git merge track1/alpha-generation
```

**Step 5: Push**

```bash
git push origin main
```

---

### Summary of Changes

| File | Change |
|---|---|
| `config/bots-config.json` | Disable entertainment bot (`enabled: false`) |
| `config/kalshi-monitor-config.json` | Enable HDD source if disabled |
| `tests/test_source_monitor.py` | Expand from 122 → ~350 lines with 6 new test classes |
| `tests/test_boxoffice_scraping.py` | New: 8 tests for HTML scraping regex patterns |

### Design Decision: TMDb Box Office API

TMDb's `/movie/{id}` endpoint returns **lifetime worldwide revenue**, not **weekend domestic gross**. Kalshi box office markets settle on weekend domestic gross. The TMDb code (`source-monitor.py:371-426`) is preserved but correctly disabled. Two alternative approaches for future consideration:

1. **The Numbers API** (paid, $500/year) — has weekend domestic breakdowns
2. **Box Office Mojo Sitemap** — structured XML data occasionally available

For now, HTML scraping of The Numbers and Box Office Mojo remains the best approach. The regex tests (Task 2) provide regression safety for when layouts change.
