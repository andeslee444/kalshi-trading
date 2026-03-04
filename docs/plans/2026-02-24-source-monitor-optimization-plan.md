# Source Monitor Optimization Suite — Implementation Plan

> **DEPRECATED**: This document is superseded by [Consolidated Quant Desk Design](2026-03-02-consolidated-quant-desk-design.md). Kept for historical reference only.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement 10 optimizations to the source-monitor bot: data freshness, time-decay sigma, retry logic, CI-based NWS thresholds, entertainment consolidation, cross-market consistency, high-confidence sizing, TMDb box office API, test suite, and settlement feedback loop.

**Architecture:** Changes touch 3 layers: probability models (`probability.py`), capital allocation (`capital_allocator.py`), and the bot itself (`source-monitor.py`). Each layer is independent — probability changes are backward-compatible via optional parameters, allocator changes add a new parameter with default `None`, and bot changes consume both. Tests validate the full stack.

**Tech Stack:** Python 3, pytest, TMDb REST API, Kalshi REST API, JSON config files.

**Design doc:** `docs/plans/2026-02-24-source-monitor-optimization-design.md`

---

### Task 1: Time-Decay Sigma — `probability.py`

Add `hours_since_publication` parameter to `album_data_sigma()` and `boxoffice_data_sigma()`.

**Files:**
- Modify: `src/kalshi/probability.py:340-360` (album_data_sigma)
- Modify: `src/kalshi/probability.py:421-441` (boxoffice_data_sigma)
- Test: `tests/test_source_monitor.py` (new file, TestTimeDecaySigma class)

**Step 1: Write failing tests for time-decay sigma**

Create `tests/test_source_monitor.py` with initial test class:

```python
"""Tests for source-monitor optimizations: time-decay sigma, data freshness,
cross-market consistency, retry logic, and edge thresholds."""

import math
import pytest
from probability import album_data_sigma, boxoffice_data_sigma, _reset_calibration


class TestTimeDecaySigma:
    """Test time-decay sigma for album and box office data."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    # --- album_data_sigma ---

    def test_album_default_no_decay(self):
        """With hours_since_publication=0, behavior is identical to current."""
        assert album_data_sigma(0) == 0.15  # Monday
        assert album_data_sigma(2) == 0.10  # Wednesday
        assert album_data_sigma(4) == 0.05  # Friday

    def test_album_friday_48h_decay(self):
        """48 hours of staleness increases sigma by 50%."""
        base = 0.05  # Friday base
        result = album_data_sigma(4, hours_since_publication=48)
        assert result == pytest.approx(base * 1.5, rel=1e-3)

    def test_album_friday_96h_decay(self):
        """96 hours of staleness increases sigma by 100%."""
        base = 0.05
        result = album_data_sigma(4, hours_since_publication=96)
        assert result == pytest.approx(base * 2.0, rel=1e-3)

    def test_album_decay_capped_at_3x(self):
        """Decay factor caps at 3x regardless of age."""
        base = 0.05
        result = album_data_sigma(4, hours_since_publication=500)
        assert result == pytest.approx(base * 3.0, rel=1e-3)

    def test_album_monday_no_decay(self):
        """Monday with 0 hours_since_publication is unchanged."""
        assert album_data_sigma(0, hours_since_publication=0) == 0.15

    # --- boxoffice_data_sigma ---

    def test_boxoffice_default_no_decay(self):
        """With hours_since_publication=0, behavior is identical to current."""
        assert boxoffice_data_sigma(4) == 0.12  # Friday
        assert boxoffice_data_sigma(6) == 0.05  # Sunday
        assert boxoffice_data_sigma(0) == 0.04  # Monday

    def test_boxoffice_friday_48h_decay(self):
        """48h staleness on Friday estimate."""
        base = 0.12
        result = boxoffice_data_sigma(4, hours_since_publication=48)
        assert result == pytest.approx(base * 1.5, rel=1e-3)

    def test_boxoffice_decay_capped_at_3x(self):
        """Decay caps at 3x."""
        base = 0.04  # Monday
        result = boxoffice_data_sigma(0, hours_since_publication=500)
        assert result == pytest.approx(base * 3.0, rel=1e-3)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_source_monitor.py::TestTimeDecaySigma -v`
Expected: FAIL — `album_data_sigma() got an unexpected keyword argument 'hours_since_publication'`

**Step 3: Implement time-decay in `album_data_sigma`**

In `src/kalshi/probability.py`, replace lines 340-359:

```python
def album_data_sigma(day_of_week, hours_since_publication=0):
    """Day-dependent + time-decay uncertainty for album sales data.

    day_of_week: 0=Monday ... 6=Sunday
    hours_since_publication: hours since data was published (0=fresh).
        Increases sigma by 50% per 48 hours of staleness, capped at 3x.

    Mon/Tue (early projections): sigma = 15% of threshold
    Wed/Thu (mid-week updates):  sigma = 10%
    Fri+ (actual data):          sigma = 5%

    Overridden by calibration.json album_sales.sigma_by_day if present.
    """
    cal = _load_calibration()
    album_cal = cal.get("album_sales", {}).get("sigma_by_day", {})

    if day_of_week <= 1:  # Mon, Tue
        base_sigma = album_cal.get("mon_tue", 0.15)
    elif day_of_week <= 3:  # Wed, Thu
        base_sigma = album_cal.get("wed_thu", 0.10)
    else:  # Fri, Sat, Sun
        base_sigma = album_cal.get("fri_sun", 0.05)

    # Time decay: data uncertainty grows 50% per 48 hours of staleness
    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)  # cap at 3x

    return base_sigma
```

**Step 4: Implement time-decay in `boxoffice_data_sigma`**

In `src/kalshi/probability.py`, replace lines 421-440:

```python
def boxoffice_data_sigma(day_of_week, hours_since_publication=0):
    """Day-dependent + time-decay uncertainty for box office data.

    day_of_week: 0=Monday ... 6=Sunday
    hours_since_publication: hours since data was published (0=fresh).
        Increases sigma by 50% per 48 hours of staleness, capped at 3x.

    Fri/Sat (estimates): sigma = 12%
    Sun (Sunday actuals): sigma = 5%
    Mon+ (final):         sigma = 4%

    Overridden by calibration.json box_office.sigma_by_day if present.
    """
    cal = _load_calibration()
    box_cal = cal.get("box_office", {}).get("sigma_by_day", {})

    if day_of_week in (4, 5):  # Fri, Sat
        base_sigma = box_cal.get("fri_sat", 0.12)
    elif day_of_week == 6:  # Sun
        base_sigma = box_cal.get("sun", 0.05)
    else:  # Mon-Thu
        base_sigma = box_cal.get("mon_thu", 0.04)

    # Time decay: data uncertainty grows 50% per 48 hours of staleness
    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)  # cap at 3x

    return base_sigma
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/test_source_monitor.py::TestTimeDecaySigma -v`
Expected: All 10 tests PASS

**Step 6: Commit**

```bash
git add src/kalshi/probability.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add time-decay sigma to album and box office probability models"
```

---

### Task 2: Extract `nws_sigma_for_hour()` Helper — `probability.py`

Extract sigma computation from `nws_probability()` into a reusable public function for CI-based edge gating.

**Files:**
- Modify: `src/kalshi/probability.py:270-322` (nws_probability + new helper)
- Test: `tests/test_source_monitor.py` (add TestNwsSigma class)

**Step 1: Write failing tests for `nws_sigma_for_hour`**

Append to `tests/test_source_monitor.py`:

```python
from probability import nws_sigma_for_hour


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
        """Verify nws_probability still works after refactor."""
        from probability import nws_probability
        # Running high 85F, threshold 80F, direction T, hour 15
        # Should be high probability (> 0.9)
        prob = nws_probability(85, 80, "T", 15)
        assert prob > 0.9
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_source_monitor.py::TestNwsSigma -v`
Expected: FAIL — `cannot import name 'nws_sigma_for_hour' from 'probability'`

**Step 3: Extract `nws_sigma_for_hour` and refactor `nws_probability`**

In `src/kalshi/probability.py`, add new function before `nws_probability` (before line 270):

```python
def nws_sigma_for_hour(hour_of_day):
    """NWS temperature uncertainty (sigma in degrees F) for a given hour.

    Continuous exponential decay model:
      sigma = max(0.5, 4.0 * exp(-0.18 * (hour - 6)))

    Falls back to legacy step-function if calibration.json has nws.sigma_by_hour.

    Exported for use in source-monitor CI-based edge gating.
    """
    cal = _load_calibration()
    nws_section = cal.get("nws", {})
    nws_cal = nws_section.get("sigma_by_hour", {})

    if nws_cal:
        # Legacy step-function: use calibrated values
        if hour_of_day < 6:
            return max(0.5, nws_cal.get("overnight", 5.0))
        elif hour_of_day >= 17:
            return max(0.5, nws_cal.get("17+", 0.5))
        elif hour_of_day >= 15:
            return max(0.5, nws_cal.get("15-16", 1.5))
        else:
            return max(0.5, nws_cal.get("before_15", 3.0))
    else:
        # Continuous model: exponential decay from morning uncertainty
        if hour_of_day < 6:
            return max(0.5, 5.0)
        else:
            return max(0.5, 4.0 * math.exp(-0.18 * (hour_of_day - 6)))
```

Then refactor `nws_probability` to use it — replace the sigma computation block (lines ~289-312) with:

```python
def nws_probability(running_high, threshold, direction, hour_of_day):
    """Probability for NWS actual-temp arbitrage (source-monitor).

    Uses nws_sigma_for_hour() for residual uncertainty estimation.

    direction="T": P(final_high > threshold)
    direction="B": P(threshold <= final_high < threshold+1)
    """
    cal = _load_calibration()
    nws_df = cal.get("nws", {}).get("df", 6)
    if not isinstance(nws_df, (int, float)) or nws_df < 2:
        _log.warning("Invalid nws_df=%s in calibration, using default df=6", nws_df)
        nws_df = 6

    sigma = nws_sigma_for_hour(hour_of_day)

    if direction == "T":
        z = (threshold - running_high) / sigma
        return 1.0 - _student_t_cdf(z, nws_df)
    else:
        # Bracket
        z_low = (threshold - running_high) / sigma
        z_high = (threshold + 1 - running_high) / sigma
        return _student_t_cdf(z_high, nws_df) - _student_t_cdf(z_low, nws_df)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_source_monitor.py::TestNwsSigma -v`
Expected: All 6 tests PASS

Also run existing tests to ensure no regression:
Run: `pytest tests/test_probability.py tests/test_weather.py tests/test_optimization.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/kalshi/probability.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "refactor: extract nws_sigma_for_hour() from nws_probability for CI-based gating"
```

---

### Task 3: High-Confidence Sizing — `capital_allocator.py`

Add `source_type` parameter to `request_budget()` with relaxed thresholds for info-arb trades.

**Files:**
- Modify: `src/kalshi/capital_allocator.py:405-435` (request_budget signature)
- Modify: `src/kalshi/capital_allocator.py:525-539` (high-confidence gate)
- Test: `tests/test_source_monitor.py` (add TestHighConfidenceSizing class)

**Step 1: Write failing tests**

Append to `tests/test_source_monitor.py`:

```python
from unittest.mock import MagicMock, patch
import sys
from pathlib import Path


class TestHighConfidenceSizing:
    """Test allocator high-confidence gate with source_type."""

    def _make_allocator(self, balance=100_00):
        """Create a PortfolioAllocator with mocked client."""
        from capital_allocator import PortfolioAllocator
        mock_client = MagicMock()
        mock_client.get_balance.return_value = (balance, balance)
        allocator = PortfolioAllocator(mock_client, state_path=None)
        return allocator

    def test_info_arb_relaxed_threshold(self):
        """Info-arb trades should trigger high-confidence at 85% conf / 10% edge."""
        allocator = self._make_allocator(balance=500_00)
        budget = allocator.request_budget(
            "source-monitor", "KXALBUM-TEST",
            edge=0.12, confidence=0.87, source_type="info_arb"
        )
        assert budget.approved

    def test_default_strict_threshold(self):
        """Non-info-arb trades need 90% conf / 15% edge for high-confidence."""
        allocator = self._make_allocator(balance=500_00)
        # 87% confidence, 12% edge — should NOT trigger high-confidence scaling
        budget = allocator.request_budget(
            "weather", "KXHIGH-TEST",
            edge=0.12, confidence=0.87
        )
        assert budget.approved  # still approved, just at normal allocation

    def test_source_type_none_backward_compat(self):
        """source_type=None uses default thresholds (backward compatible)."""
        allocator = self._make_allocator(balance=500_00)
        budget = allocator.request_budget(
            "source-monitor", "KXALBUM-TEST",
            edge=0.12, confidence=0.87
        )
        # Without source_type, 87%/12% won't trigger high-conf scaling
        assert budget.approved
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_source_monitor.py::TestHighConfidenceSizing -v`
Expected: FAIL — `request_budget() got an unexpected keyword argument 'source_type'`

**Step 3: Add `source_type` to `request_budget` signature**

In `src/kalshi/capital_allocator.py`, modify `request_budget` (line 405):

```python
    def request_budget(self, bot_name, ticker, edge=0.0, confidence=0.0,
                       bot_max_cost_cents=500, source_type=None):
```

And update `_do_request` (line 421-424):

```python
        def _do_request():
            self._load_state()
            self._reset_daily_if_needed_inner()
            return self._request_budget_inner(bot_name, ticker, edge, confidence, bot_max_cost_cents, source_type)
```

And update `_request_budget_inner` signature (line 437):

```python
    def _request_budget_inner(self, bot_name, ticker, edge, confidence, bot_max_cost_cents, source_type=None):
```

**Step 4: Modify high-confidence gate**

In `src/kalshi/capital_allocator.py`, replace lines 525-539:

```python
        # 7. Scale up for high-confidence trades
        # Info-arb (source_type="info_arb") uses relaxed thresholds since
        # edge is based on observed settlement data, not model predictions.
        if source_type == "info_arb":
            high_conf_threshold = 0.85
            high_edge_threshold = 0.10
        else:
            high_conf_threshold = 0.90
            high_edge_threshold = 0.15

        if confidence > high_conf_threshold and edge > high_edge_threshold:
            high_conf_max = int(available_balance * 0.25)
            allocated = min(
                high_conf_max,
                remaining_portfolio,
                remaining_bot * 2,  # relax bot cap for high-confidence
                max_ticker_risk * 2,
                remaining_city,     # NEVER bypass city limit
            )
            self.log.info(
                "High-confidence trade: %s edge=%.1f%% conf=%.0f%% type=%s -> budget $%.2f",
                ticker, edge * 100, confidence * 100, source_type or "default", allocated / 100
            )
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/test_source_monitor.py::TestHighConfidenceSizing -v`
Expected: All 3 tests PASS

Also run existing allocator tests:
Run: `pytest tests/test_allocator.py tests/test_optimization.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/kalshi/capital_allocator.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add source_type parameter to allocator for relaxed info-arb sizing"
```

---

### Task 4: Data Freshness Validation — `source-monitor.py`

Add HDD data age checking and staleness gating.

**Files:**
- Modify: `src/kalshi/source-monitor.py:72-85` (check_hdd)
- Modify: `src/kalshi/source-monitor.py:119-139` (evaluate_album_trade)
- Test: `tests/test_source_monitor.py` (add TestDataFreshness class)

**Step 1: Write failing tests for data freshness**

Append to `tests/test_source_monitor.py`:

```python
import datetime


class TestDataFreshness:
    """Test data freshness validation for HDD album data."""

    def test_compute_data_age_fresh(self):
        """Data from 1 hour ago returns ~1 hour."""
        from source_monitor_helpers import compute_data_age_hours
        now = datetime.datetime.now(datetime.timezone.utc)
        chart_date = (now - datetime.timedelta(hours=1)).isoformat()
        age = compute_data_age_hours(chart_date)
        assert 0.5 < age < 1.5

    def test_compute_data_age_missing(self):
        """Missing chart_date returns 0 (fail-open)."""
        from source_monitor_helpers import compute_data_age_hours
        assert compute_data_age_hours(None) == 0
        assert compute_data_age_hours("") == 0

    def test_compute_data_age_unparseable(self):
        """Unparseable chart_date returns 0 (fail-open)."""
        from source_monitor_helpers import compute_data_age_hours
        assert compute_data_age_hours("not-a-date") == 0

    def test_stale_data_rejected_at_168h(self):
        """Data older than 168 hours should be rejected."""
        from source_monitor_helpers import compute_data_age_hours, MAX_DATA_AGE_HOURS
        now = datetime.datetime.now(datetime.timezone.utc)
        old_date = (now - datetime.timedelta(hours=170)).isoformat()
        age = compute_data_age_hours(old_date)
        assert age > MAX_DATA_AGE_HOURS
```

Note: Since source-monitor.py has hyphens and does module-level init, we'll extract the pure helper functions into a testable form. The actual test will use a helper function extracted inline.

**Actually, better approach**: Add `compute_data_age_hours()` directly in `source-monitor.py` and test it by extracting to a standalone test that reimplements the logic (same pattern as `test_entertainment.py`).

Replace the test above with:

```python
class TestDataFreshness:
    """Test data freshness computation (mirrors source-monitor logic)."""

    MAX_DATA_AGE_HOURS = 168

    @staticmethod
    def _compute_data_age_hours(chart_date_str):
        """Mirror of source-monitor._compute_data_age_hours."""
        if not chart_date_str:
            return 0
        try:
            dt = datetime.datetime.fromisoformat(chart_date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            return (now - dt).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0

    def test_fresh_data_age(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        chart_date = (now - datetime.timedelta(hours=1)).isoformat()
        age = self._compute_data_age_hours(chart_date)
        assert 0.5 < age < 1.5

    def test_missing_chart_date_fail_open(self):
        assert self._compute_data_age_hours(None) == 0
        assert self._compute_data_age_hours("") == 0

    def test_unparseable_chart_date_fail_open(self):
        assert self._compute_data_age_hours("not-a-date") == 0

    def test_old_data_detected(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old_date = (now - datetime.timedelta(hours=170)).isoformat()
        age = self._compute_data_age_hours(old_date)
        assert age > self.MAX_DATA_AGE_HOURS

    def test_sigma_increased_for_stale_data(self):
        """48h-old Friday data should have sigma > base 5%."""
        result = album_data_sigma(4, hours_since_publication=48)
        assert result > 0.05  # base is 0.05
        assert result == pytest.approx(0.075, rel=1e-3)

    def test_data_within_168h_passes(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ok_date = (now - datetime.timedelta(hours=100)).isoformat()
        age = self._compute_data_age_hours(ok_date)
        assert age < self.MAX_DATA_AGE_HOURS
```

**Step 2: Run tests to verify they pass** (these are pure functions, should pass immediately)

Run: `pytest tests/test_source_monitor.py::TestDataFreshness -v`
Expected: All 6 tests PASS

**Step 3: Add `_compute_data_age_hours` and staleness gating to `source-monitor.py`**

Add helper function after `save_snapshot` (after line 61):

```python
MAX_DATA_AGE_HOURS = 168  # 7 days — same as entertainment-bot

def _compute_data_age_hours(chart_date_str):
    """Compute hours since chart data was published. Returns 0 if unparseable (fail-open)."""
    if not chart_date_str:
        return 0
    try:
        dt = datetime.datetime.fromisoformat(chart_date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return 0
```

Modify `evaluate_album_trade` (line 138) to pass age to sigma and skip stale data:

Replace line 138:
```python
    sigma = album_data_sigma(datetime.datetime.now().weekday())
```

With:
```python
    data_age_hours = _compute_data_age_hours(sale.get("chart_date"))
    if data_age_hours > MAX_DATA_AGE_HOURS:
        log.info(f"  {artist}: data {data_age_hours:.0f}h stale (>{MAX_DATA_AGE_HOURS}h), skipping")
        trade_manager.log_decision(
            ticker, "skip", "skipped", f"data {data_age_hours:.0f}h stale",
            edge=0, price_cents=market.get("yes_ask", 0),
        )
        return
    sigma = album_data_sigma(datetime.datetime.now().weekday(), hours_since_publication=data_age_hours)
```

Similarly in `evaluate_boxoffice_trade` (line 334), replace:
```python
    sigma = boxoffice_data_sigma(datetime.datetime.now().weekday())
```
With:
```python
    # Box office: estimate hours since data publication based on day of week
    dow = datetime.datetime.now().weekday()
    box_age_hours = {4: 0, 5: 0, 6: 24, 0: 48, 1: 72, 2: 96, 3: 120}.get(dow, 0)
    sigma = boxoffice_data_sigma(dow, hours_since_publication=box_age_hours)
```

**Step 4: Run all tests**

Run: `pytest tests/test_source_monitor.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/kalshi/source-monitor.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add data freshness validation to source-monitor HDD and box office"
```

---

### Task 5: Retry Logic — `source-monitor.py`

Add `_check_with_retry()` wrapper and refactor main loop.

**Files:**
- Modify: `src/kalshi/source-monitor.py:660-778` (main loop)
- Test: `tests/test_source_monitor.py` (add TestRetryLogic class)

**Step 1: Write failing tests for retry logic**

Append to `tests/test_source_monitor.py`:

```python
from unittest.mock import MagicMock, call
import time


class TestRetryLogic:
    """Test _check_with_retry wrapper."""

    @staticmethod
    def _check_with_retry(check_fn, source_name, prefetched, ss, health, log, max_retries=2):
        """Mirror of source-monitor._check_with_retry."""
        for attempt in range(max_retries + 1):
            try:
                check_fn(prefetched_markets=prefetched)
                health.record_source_success(source_name)
                if ss:
                    ss.source_ok(source_name)
                return True
            except Exception as e:
                if attempt < max_retries:
                    log.warning(f"{source_name} attempt {attempt+1} failed: {e}, retrying in {2**attempt}s")
                    time.sleep(0.01)  # fast sleep in tests
                else:
                    log.error(f"{source_name} failed after {max_retries+1} attempts: {e}")
                    health.record_source_error(source_name, str(e))
                    if ss:
                        ss.source_fail(source_name, str(e))
        return False

    def test_success_first_try(self):
        check_fn = MagicMock()
        health = MagicMock()
        ss = MagicMock()
        log = MagicMock()
        result = self._check_with_retry(check_fn, "hdd", None, ss, health, log)
        assert result is True
        assert check_fn.call_count == 1
        health.record_source_success.assert_called_once_with("hdd")

    def test_success_on_retry(self):
        check_fn = MagicMock(side_effect=[ConnectionError("timeout"), None])
        health = MagicMock()
        ss = MagicMock()
        log = MagicMock()
        result = self._check_with_retry(check_fn, "hdd", None, ss, health, log)
        assert result is True
        assert check_fn.call_count == 2

    def test_failure_after_max_retries(self):
        check_fn = MagicMock(side_effect=ConnectionError("timeout"))
        health = MagicMock()
        ss = MagicMock()
        log = MagicMock()
        result = self._check_with_retry(check_fn, "hdd", None, ss, health, log, max_retries=2)
        assert result is False
        assert check_fn.call_count == 3
        health.record_source_error.assert_called_once()
```

**Step 2: Run tests to verify they pass** (pure logic, no import dependency)

Run: `pytest tests/test_source_monitor.py::TestRetryLogic -v`
Expected: All 3 tests PASS

**Step 3: Add `_check_with_retry` to `source-monitor.py`**

Add after `_compute_data_age_hours`:

```python
def _check_with_retry(check_fn, source_name, prefetched, ss, max_retries=2):
    """Retry a source check with exponential backoff on transient failures."""
    for attempt in range(max_retries + 1):
        try:
            check_fn(prefetched_markets=prefetched)
            health.record_source_success(source_name)
            if ss:
                ss.source_ok(source_name)
            return
        except Exception as e:
            if attempt < max_retries:
                delay = 2 ** attempt
                log.warning(f"{source_name} attempt {attempt+1} failed: {e}, retrying in {delay}s")
                time.sleep(delay)
            else:
                log.error(f"{source_name} failed after {max_retries+1} attempts: {e}")
                health.record_source_error(source_name, str(e))
                if ss:
                    ss.source_fail(source_name, str(e))
                traceback.print_exc()
```

**Step 4: Refactor main loop to use `_check_with_retry`**

Replace the 3 try/except blocks in the main loop (lines 725-765) with:

```python
            if need_hdd:
                _check_with_retry(check_hdd, "hdd", prefetched, ss)
                last_hdd = now

            if need_box:
                _check_with_retry(check_boxoffice, "boxoffice", prefetched, ss)
                last_boxoffice = now

            if need_nws:
                _check_with_retry(check_nws, "nws", prefetched, ss)
                last_nws = now
```

**Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/kalshi/source-monitor.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add retry logic with exponential backoff to source-monitor data fetches"
```

---

### Task 6: CI-Based NWS Edge Thresholds — `source-monitor.py`

Replace the hard hour-17 step function with margin-aware dynamic thresholds.

**Files:**
- Modify: `src/kalshi/source-monitor.py:576-624` (NWS edge threshold logic)
- Modify: `src/kalshi/source-monitor.py:17` (import nws_sigma_for_hour)
- Test: `tests/test_source_monitor.py` (add TestNwsEdgeThresholds class)

**Step 1: Write failing tests**

Append to `tests/test_source_monitor.py`:

```python
class TestNwsEdgeThresholds:
    """Test CI-based NWS edge threshold computation."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    @staticmethod
    def _compute_nws_min_edge(running_high, threshold, hour, is_bracket):
        """Mirror of source-monitor CI-based edge gating logic."""
        sigma = nws_sigma_for_hour(hour)
        margin = abs(running_high - threshold)
        ci_99 = 2.576 * sigma

        if is_bracket:
            return 0.20  # Brackets always need high threshold
        elif margin > ci_99:
            return 0.05  # Very confident
        elif margin > ci_99 * 0.5:
            return 0.10  # Moderate
        else:
            return 0.15  # Uncertain

    def test_large_margin_afternoon_low_threshold(self):
        """5F margin at hour 15 (sigma ~0.7) is well outside CI -> 5% min edge."""
        # ci_99 = 2.576 * 0.7 ~= 1.8, margin 5 > 1.8
        min_edge = self._compute_nws_min_edge(85, 80, 15, False)
        assert min_edge == 0.05

    def test_small_margin_morning_high_threshold(self):
        """0.5F margin at hour 8 (sigma ~2.7) is inside CI -> 15% min edge."""
        # ci_99 = 2.576 * 2.7 ~= 6.9, margin 0.5 < 6.9 * 0.5
        min_edge = self._compute_nws_min_edge(80.5, 80, 8, False)
        assert min_edge == 0.15

    def test_moderate_margin(self):
        """2F margin at hour 15 (sigma ~0.7), ci_99 ~1.8, margin > ci/2 but < ci."""
        min_edge = self._compute_nws_min_edge(82, 80, 15, False)
        assert min_edge == 0.05  # 2 > 1.8, very confident

    def test_bracket_always_20pct(self):
        """Brackets always require 20% min edge regardless of margin."""
        min_edge = self._compute_nws_min_edge(85, 80, 17, True)
        assert min_edge == 0.20

    def test_evening_close_margin(self):
        """0.3F margin at hour 18 (sigma ~0.5), ci_99 ~1.3, margin < ci/2 -> 15%."""
        min_edge = self._compute_nws_min_edge(80.3, 80, 18, False)
        assert min_edge == 0.15
```

**Step 2: Run tests to verify they pass** (pure logic)

Run: `pytest tests/test_source_monitor.py::TestNwsEdgeThresholds -v`
Expected: All 5 tests PASS

**Step 3: Update source-monitor imports**

In `src/kalshi/source-monitor.py` line 17, add `nws_sigma_for_hour` to the import:

```python
from probability import info_arb_probability, album_data_sigma, boxoffice_data_sigma, nws_probability, half_kelly, compute_limit_price, kalshi_fee_cents, is_market_liquid, nws_sigma_for_hour
```

**Step 4: Replace edge threshold logic in NWS section**

In `match_nws_to_markets`, replace the YES-side edge gating (lines 582-588):

From:
```python
            if prob > 0.5 and yes_ask and yes_ask < 99:
                # Buy YES (raw edge, fees handled in Kelly)
                edge = prob - yes_ask / 100
                # Rec 1: Brackets need 2x edge threshold (20% for NWS)
                base_min_edge = 0.20 if is_bracket else 0.10
                # Lower threshold for high-confidence NWS (hour >= 17, non-bracket)
                min_edge = base_min_edge * 0.5 if now.hour >= 17 and not is_bracket else base_min_edge
```

To:
```python
            if prob > 0.5 and yes_ask and yes_ask < 99:
                # Buy YES (raw edge, fees handled in Kelly)
                edge = prob - yes_ask / 100
                # CI-based edge threshold: margin-aware instead of hour-17 step
                if is_bracket:
                    min_edge = 0.20
                else:
                    sigma = nws_sigma_for_hour(now.hour)
                    margin = abs(running_high - threshold)
                    ci_99 = 2.576 * sigma
                    if margin > ci_99:
                        min_edge = 0.05
                    elif margin > ci_99 * 0.5:
                        min_edge = 0.10
                    else:
                        min_edge = 0.15
```

Apply identical replacement for the NO-side (lines 617-624).

**Step 5: Run full tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/kalshi/source-monitor.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: replace hour-17 step with CI-based NWS edge thresholds"
```

---

### Task 7: Cross-Market Consistency Validation — `source-monitor.py`

Add monotonicity check for multiple markets on the same entity.

**Files:**
- Modify: `src/kalshi/source-monitor.py` (add `_validate_market_cluster`, modify `match_hdd_to_markets`, `match_boxoffice_to_markets`, `match_nws_to_markets`)
- Test: `tests/test_source_monitor.py` (add TestCrossMarketConsistency class)

**Step 1: Write failing tests**

Append to `tests/test_source_monitor.py`:

```python
class TestCrossMarketConsistency:
    """Test cross-market consistency validation."""

    @staticmethod
    def _validate_market_cluster(entity_key, market_signals):
        """Mirror of source-monitor._validate_market_cluster."""
        if len(market_signals) < 2:
            return market_signals
        sorted_signals = sorted(market_signals, key=lambda s: s[1])
        for i in range(len(sorted_signals) - 1):
            lower_prob = sorted_signals[i][2]
            higher_prob = sorted_signals[i + 1][2]
            if lower_prob < higher_prob - 0.05:
                return []
        return sorted_signals

    def test_single_market_passthrough(self):
        signals = [("MKT1", 150000, 0.90)]
        result = self._validate_market_cluster("Drake", signals)
        assert result == signals

    def test_monotonic_pass(self):
        """P(>150K) > P(>200K) is consistent."""
        signals = [("MKT1", 150000, 0.95), ("MKT2", 200000, 0.30)]
        result = self._validate_market_cluster("Drake", signals)
        assert len(result) == 2

    def test_non_monotonic_rejection(self):
        """P(>150K) < P(>200K) is contradictory -> skip all."""
        signals = [("MKT1", 150000, 0.30), ("MKT2", 200000, 0.90)]
        result = self._validate_market_cluster("Drake", signals)
        assert result == []

    def test_tolerance_boundary(self):
        """Within 5% tolerance, passes."""
        signals = [("MKT1", 150000, 0.50), ("MKT2", 200000, 0.53)]
        result = self._validate_market_cluster("Drake", signals)
        assert len(result) == 2

    def test_tolerance_exceeded(self):
        """Beyond 5% tolerance, fails."""
        signals = [("MKT1", 150000, 0.50), ("MKT2", 200000, 0.56)]
        result = self._validate_market_cluster("Drake", signals)
        assert result == []

    def test_three_markets_monotonic(self):
        """Three thresholds, all consistent."""
        signals = [("M1", 100000, 0.99), ("M2", 150000, 0.80), ("M3", 200000, 0.20)]
        result = self._validate_market_cluster("Drake", signals)
        assert len(result) == 3

    def test_empty_list(self):
        result = self._validate_market_cluster("Drake", [])
        assert result == []
```

**Step 2: Run tests** (pure logic, passes immediately)

Run: `pytest tests/test_source_monitor.py::TestCrossMarketConsistency -v`
Expected: All 7 tests PASS

**Step 3: Add `_validate_market_cluster` to source-monitor.py**

Add after `_check_with_retry`:

```python
def _validate_market_cluster(entity_key, market_signals):
    """Check that markets for the same entity have monotonically decreasing
    probabilities as thresholds increase.

    market_signals: list of (market, threshold, probability) tuples
    Returns filtered consistent list, or empty list if contradictory.
    """
    if len(market_signals) < 2:
        return market_signals
    sorted_signals = sorted(market_signals, key=lambda s: s[1])
    for i in range(len(sorted_signals) - 1):
        lower_prob = sorted_signals[i][2]
        higher_prob = sorted_signals[i + 1][2]
        if lower_prob < higher_prob - 0.05:  # 5% tolerance
            log.warning(
                f"Inconsistent {entity_key}: P(>{sorted_signals[i][1]})={lower_prob:.0%} "
                f"< P(>{sorted_signals[i+1][1]})={higher_prob:.0%} — skipping all"
            )
            return []
    return sorted_signals
```

**Step 4: Integrate into `match_hdd_to_markets`**

Refactor `match_hdd_to_markets` to group by artist, compute probabilities, validate, then evaluate:

Replace lines 102-114 with:

```python
        for sale in sales_data:
            artist = sale["artist"].lower()
            units = sale["units"]

            # Group all matching markets for this artist
            matched_markets = []
            for m in markets:
                title = m.get("title", "").lower()
                subtitle = m.get("subtitle", "").lower()
                if re.search(r'\b' + re.escape(artist) + r'\b', title) or re.search(r'\b' + re.escape(artist) + r'\b', subtitle):
                    if not is_market_liquid(m):
                        continue
                    # Parse threshold for consistency check
                    threshold_match = re.search(r'(\d{1,3}(?:,\d{3})*)\s*(?:K|thousand|copies|units)', m.get("title", ""), re.I)
                    if not threshold_match:
                        threshold_match = re.search(r'T(\d+)', m.get("ticker", ""))
                    if threshold_match:
                        threshold = int(threshold_match.group(1).replace(",", ""))
                        if threshold < 1000:
                            threshold *= 1000
                        data_age_hours = _compute_data_age_hours(sale.get("chart_date"))
                        sigma = album_data_sigma(datetime.datetime.now().weekday(), hours_since_publication=data_age_hours)
                        prob = info_arb_probability(units, threshold, sigma)
                        matched_markets.append((m, threshold, prob))

            # Validate consistency across all markets for this artist
            if matched_markets:
                consistent = _validate_market_cluster(artist, matched_markets)
                for m, threshold, _ in consistent:
                    evaluate_album_trade(m, sale)
```

Apply similar grouping logic to `match_boxoffice_to_markets` and `match_nws_to_markets` (group by movie title and city+date respectively).

**Step 5: Run full tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/kalshi/source-monitor.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add cross-market consistency validation to source-monitor"
```

---

### Task 8: Entertainment Bot Consolidation + Source-Monitor Config

Disable entertainment bot, adjust source-monitor parameters.

**Files:**
- Modify: `config/bots-config.json:1-9` (add enabled=false to entertainment)
- Modify: `config/kalshi-monitor-config.json:4-5` (increase daily trades)
- Modify: `src/kalshi/source-monitor.py:160` (lower min_edge for confirmed data)

**Step 1: Disable entertainment bot**

In `config/bots-config.json`, add `"enabled": false` to entertainment section:

```json
  "entertainment": {
    "enabled": false,
    "maxTradeAmount": 5,
    ...
  },
```

**Step 2: Increase source-monitor daily capacity**

In `config/kalshi-monitor-config.json`, change:
- `"maxDailyTrades": 20` -> `"maxDailyTrades": 25`

**Step 3: Lower min_edge for confirmed album data**

In `source-monitor.py` line 160, change:
```python
    min_edge = 0.05 if sigma <= 0.05 else 0.10
```
To:
```python
    min_edge = 0.04 if sigma <= 0.05 else 0.10
```

Apply same change in `evaluate_boxoffice_trade` line 351.

**Step 4: Run tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add config/bots-config.json config/kalshi-monitor-config.json src/kalshi/source-monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: consolidate entertainment bot into source-monitor, adjust config"
```

---

### Task 9: Source-Monitor Passes `source_type` to Allocator

Wire up the `source_type` parameter in all source-monitor allocator calls.

**Files:**
- Modify: `src/kalshi/source-monitor.py` (6 `allocator.request_budget` call sites)

**Step 1: Update HDD album allocator calls**

In `evaluate_album_trade`, update the 2 `request_budget` calls (lines 165, 193):

```python
budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence, source_type="info_arb")
```

**Step 2: Update box office allocator calls**

In `evaluate_boxoffice_trade`, update the 2 `request_budget` calls (lines 356, 382):

```python
budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence, source_type="info_arb")
```

**Step 3: Update NWS allocator calls**

In `match_nws_to_markets`, update the 2 `request_budget` calls (lines 590, 626):

```python
budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=prob, source_type="nws")
```

(NWS uses `source_type="nws"`, not `"info_arb"`, since it's model-based, not direct observation.)

**Step 4: Run tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/kalshi/source-monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: pass source_type to allocator for info-arb sizing differentiation"
```

---

### Task 10: TMDb Box Office API Integration — `source-monitor.py`

Add TMDb as primary box office data source with HTML scraping as fallback.

**Files:**
- Modify: `src/kalshi/source-monitor.py:223-290` (check_boxoffice)
- Modify: `config/kalshi-monitor-config.json` (add tmdbEnabled flag)

**Step 1: Add TMDb config**

In `config/kalshi-monitor-config.json`, add to boxoffice section:

```json
    "boxoffice": {
      "enabled": true,
      "tmdbEnabled": true,
      "urls": ["https://www.boxofficemojo.com/", "https://www.the-numbers.com/"],
      "intervalMinutes": 30,
      "activeDays": ["Friday", "Saturday", "Sunday", "Monday"]
    },
```

**Step 2: Add TMDb fetch function to source-monitor.py**

Add before `check_boxoffice`:

```python
def _load_tmdb_api_key():
    """Load TMDb API key from env var or config/keys/tmdb.txt."""
    key = os.environ.get("TMDB_API_KEY", "")
    if not key:
        key_path = PROJECT_DIR / "config" / "keys" / "tmdb.txt"
        if key_path.exists():
            key = key_path.read_text().strip()
    return key

def _fetch_tmdb_boxoffice():
    """Fetch current box office data from TMDb API. Returns list of {title, gross, source}."""
    api_key = _load_tmdb_api_key()
    if not api_key:
        return None  # No key configured, fall back to HTML scraping

    try:
        url = f"https://api.themoviedb.org/3/movie/now_playing?api_key={api_key}&region=US&page=1"
        r = retry_request("GET", url, timeout=15)
        data = r.json()
        movies = data.get("results", [])

        box_office_data = []
        for movie in movies[:15]:
            movie_id = movie.get("id")
            if not movie_id:
                continue
            # Fetch detailed info including revenue
            detail_url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={api_key}"
            detail_r = retry_request("GET", detail_url, timeout=10)
            detail = detail_r.json()
            revenue = detail.get("revenue", 0)
            title = detail.get("title", movie.get("title", ""))
            if revenue and revenue > 100000:
                box_office_data.append({
                    "title": title,
                    "gross": revenue,
                    "source": "tmdb"
                })

        if box_office_data:
            log.info(f"  TMDb: {len(box_office_data)} movies with revenue data")
            for d in box_office_data[:5]:
                log.info(f"    -> {d['title']}: ${d['gross']:,}")
        return box_office_data

    except Exception as e:
        log.warning(f"  TMDb fetch failed: {e}, falling back to HTML scraping")
        return None
```

**Step 3: Modify `check_boxoffice` to use TMDb first**

At the start of `check_boxoffice()`, after the active_days check:

```python
    box_office_data = []

    # Try TMDb first (structured API, more reliable)
    if config["sources"]["boxoffice"].get("tmdbEnabled", False):
        tmdb_data = _fetch_tmdb_boxoffice()
        if tmdb_data:
            box_office_data = tmdb_data

    # Fall back to HTML scraping if TMDb didn't return data
    if not box_office_data:
        # ... existing The Numbers + Box Office Mojo scraping code ...
```

Move the existing scraping code into an `else` / fallback block.

**Step 4: Run tests**

Run: `pytest tests/ -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/kalshi/source-monitor.py config/kalshi-monitor-config.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add TMDb API as primary box office source with HTML scraping fallback"
```

---

### Task 11: Settlement Feedback Loop — `calibrate-sigma.py`

Extend calibration pipeline to include source-monitor trade files and info-arb-specific sigma calibration.

**Files:**
- Modify: `scripts/calibrate-sigma.py:31-36` (add monitor trade file)
- Modify: `scripts/calibrate-sigma.py:366-422` (enhance info-arb calibration)

**Step 1: Add source-monitor trades to calibration input**

In `scripts/calibrate-sigma.py`, add to `TRADE_FILES` (line 31-36):

```python
TRADE_FILES = [
    {"label": "Weather Bot", "path": PROJECT_DIR / "data" / "kalshi-trades.json"},
    {"label": "Strategy Trader", "path": PROJECT_DIR / "data" / "kalshi-strategy-trades.json"},
    {"label": "Entertainment Bot", "path": PROJECT_DIR / "data" / "kalshi-entertainment-trades.json"},
    {"label": "BeatRelease Scanner", "path": PROJECT_DIR / "data" / "beatrelease-trades.json"},
    {"label": "Source Monitor", "path": PROJECT_DIR / "data" / "kalshi-monitor-trades.json"},
    {"label": "Economics Bot", "path": PROJECT_DIR / "data" / "kalshi-economics-trades.json"},
    {"label": "Crypto Bot", "path": PROJECT_DIR / "data" / "kalshi-crypto-trades.json"},
]
```

**Step 2: Enhance `calibrate_info_arb` to use `source_type` and `model_prob`**

In `calibrate_info_arb` (line 366), improve trade matching to use source-monitor fields:

Replace lines 375-397 with:

```python
    for t in trades:
        ticker = t.get("ticker", "")
        revenue = settlement_map.get(ticker)
        if revenue is None:
            continue

        # Use source_type field (set by source-monitor) or fall back to confidence
        source_type = t.get("source_type", "")
        if label == "album_sales" and source_type not in ("album", ""):
            continue
        if label == "box_office" and source_type not in ("boxoffice", ""):
            continue

        # Prefer model_prob (source-monitor), fall back to confidence (entertainment)
        model_prob = t.get("model_prob") or t.get("confidence") or t.get("est_edge")
        if model_prob is None:
            continue

        side = t.get("side", "").lower()
        if side == "yes":
            actual = 1 if revenue > 0 else 0
        elif side == "no":
            actual = 0 if revenue > 0 else 1
        else:
            continue

        ts = t.get("timestamp", "")
        try:
            dow = datetime.fromisoformat(ts.replace("Z", "+00:00")).weekday()
        except (ValueError, TypeError):
            dow = 2

        try:
            conf_val = float(str(model_prob).rstrip("%")) / 100 if "%" in str(model_prob) else float(model_prob)
        except (ValueError, TypeError):
            continue

        matched.append({"predicted": conf_val, "actual": actual, "dow": dow})
```

**Step 3: Add proper sigma grid search with shrinkage**

Replace the heuristic `sigma ~ sqrt(brier_score)` (line 420) with actual grid search:

```python
    sigma_by_day = {}
    for bucket, items in day_buckets.items():
        if len(items) < 10:  # Increased from 3 to 10 for reliability
            continue

        # Grid search for optimal sigma
        best_sigma = {"mon_tue": 0.15, "wed_thu": 0.10, "fri_sun": 0.05}[bucket]
        default_sigma = best_sigma
        best_loss = float("inf")

        for sigma_x100 in range(1, 51):  # 0.01 to 0.50
            sigma = sigma_x100 / 100.0
            preds = []
            for m in items:
                # Re-compute probability with test sigma
                z = (m["predicted"] - 0.5) / sigma if sigma > 0 else 0
                prob = 0.5 * (1 + math.erf(z / math.sqrt(2)))
                preds.append((prob, m["actual"]))
            loss = log_loss(preds)
            if loss is not None and loss < best_loss:
                best_loss = loss
                best_sigma = sigma

        # Bayesian shrinkage toward default
        weight = len(items) / (len(items) + 15)
        shrunk_sigma = weight * best_sigma + (1 - weight) * default_sigma
        sigma_by_day[bucket] = round(shrunk_sigma, 3)

    return {"sigma_by_day": sigma_by_day, "n": len(matched)}
```

**Step 4: Run calibration in dry-run mode**

Run: `python3 scripts/calibrate-sigma.py --no-api`
Expected: Outputs calibration report including source-monitor trades (may show n=0 if no settled trades yet)

**Step 5: Commit**

```bash
git add scripts/calibrate-sigma.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: extend calibration pipeline with source-monitor trades and grid-search sigma"
```

---

### Task 12: Final Test Suite + Regression Check

Run the full test suite, ensure all existing tests still pass, add any missing edge cases.

**Files:**
- Test: `tests/test_source_monitor.py` (verify complete)
- Test: `tests/` (full regression)

**Step 1: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS (including all 22 existing test files + new test_source_monitor.py)

**Step 2: Run backtest in dry mode to ensure no regression**

Run: `python3 scripts/backtest.py --no-api 2>/dev/null || echo "Backtest completed (may show warnings if no data)"`

**Step 3: Verify probability model backward compatibility**

Run: `pytest tests/test_probability.py tests/test_optimization.py tests/test_weather.py tests/test_entertainment.py -v`
Expected: All PASS — confirms existing behavior unchanged for callers not passing new parameters

**Step 4: Final commit**

```bash
git add tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: complete source-monitor test suite with 35+ test cases"
```

---

## Summary

| Task | Files | Tests | Description |
|------|-------|-------|-------------|
| 1 | probability.py | 10 | Time-decay sigma for album/box office |
| 2 | probability.py | 6 | Extract nws_sigma_for_hour helper |
| 3 | capital_allocator.py | 3 | High-confidence sizing with source_type |
| 4 | source-monitor.py | 6 | Data freshness validation |
| 5 | source-monitor.py | 3 | Retry logic with backoff |
| 6 | source-monitor.py | 5 | CI-based NWS edge thresholds |
| 7 | source-monitor.py | 7 | Cross-market consistency |
| 8 | configs | 0 | Entertainment consolidation |
| 9 | source-monitor.py | 0 | Wire source_type to allocator |
| 10 | source-monitor.py, config | 0 | TMDb box office API |
| 11 | calibrate-sigma.py | 0 | Settlement feedback loop |
| 12 | tests/ | regression | Full regression check |
| **Total** | **8 files** | **40+** | **10 optimizations** |
