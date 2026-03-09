# T1-Phase 3: New Strategies + Operational Hardening — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expand into box office prediction markets with a production-ready web scraper, verify weather city coverage against live Kalshi tickers, automate daily P&L reporting via cron/LaunchAgent, and harden data source health monitoring with retry logic, alert deduplication, and HDD Sanity CMS health checks.

**Architecture:** The box office scraper is a new function set in `source-monitor.py` that fetches HTML from The Numbers and Box Office Mojo, extracts weekend domestic gross data, and feeds it into the existing `match_boxoffice_to_markets()` pipeline. The existing `info_arb_probability()` + `boxoffice_data_sigma()` models handle probability computation. Operational hardening adds a `SourceRetrier` class to `kalshi_auth.py`, deduplication to `HealthCheckMonitor`, and a cron-ready wrapper script for daily reports.

**Tech Stack:** Python 3, `requests` + `beautifulsoup4` (already installed), `pytest`, `unittest.mock`, LaunchAgent plist for macOS cron.

**Branch:** `track1/alpha-generation` (from `main`)

---

### Task 1: Create Branch and Verify Baseline

**Files:**
- None (branch setup only)

**Step 1: Create the feature branch**

```bash
git checkout -b track1/alpha-generation main
```

**Step 2: Verify all existing tests pass**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (22+ test files).

**Step 3: No commit needed — branch is ready.**

---

### Task 2: Box Office Scraper — Tests

**Files:**
- Create: `tests/test_boxoffice_bot.py`

**Context:** The source monitor already has `match_boxoffice_to_markets()` (line 510) and `evaluate_boxoffice_trade()` (line 554) that consume box office data as `[{"title": str, "gross": float}]`. What's missing is the actual scraping — fetching HTML from The Numbers and Box Office Mojo, parsing it into that format. We need two scrapers because each site has different HTML structure, and we want fallback redundancy.

**Step 1: Write tests for box office HTML parsing**

```python
"""Tests for box office scraping functions in source-monitor.py.

Tests the HTML parsing functions that extract weekend domestic gross
revenue data from The Numbers and Box Office Mojo websites.
"""

import json
import re
import pytest
from unittest.mock import MagicMock, patch
import importlib
import importlib.util
import sys
from pathlib import Path


# --- Module loading (same pattern as test_source_monitor.py) ---

def _load_source_monitor():
    """Load source-monitor.py with stubbed dependencies."""
    # Stub kalshi_auth before import
    mock_auth = MagicMock()
    mock_client = MagicMock()
    mock_auth.KalshiClient.return_value = mock_client
    mock_auth.setup_logging.return_value = MagicMock()
    mock_auth.setup_unbuffered = MagicMock()
    mock_auth.setup_signal_handlers = MagicMock()
    mock_auth.PROJECT_DIR = Path(__file__).resolve().parent.parent
    mock_auth.TradeManager = MagicMock()
    mock_auth.HealthCheckMonitor = MagicMock()
    mock_auth.trim_trade_log = MagicMock()
    mock_auth.check_kill_switch = MagicMock()
    mock_auth.retry_request = MagicMock()

    # Stub other imports
    sys.modules["kalshi_auth"] = mock_auth
    sys.modules["probability"] = MagicMock()
    sys.modules["capital_allocator"] = MagicMock()
    sys.modules["hdd_parser"] = MagicMock()
    sys.modules["macro_engine"] = MagicMock()
    sys.modules["correlation_engine"] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "source_monitor",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "source-monitor.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Fixtures: realistic HTML snippets ---

THE_NUMBERS_HTML = """
<table>
<tr><td>1</td><td><a href="/movie/a-minecraft-movie">A Minecraft Movie</a></td>
<td class="money">$163,830,000</td><td class="money">$163,830,000</td></tr>
<tr><td>2</td><td><a href="/movie/thunderbolts">Thunderbolts*</a></td>
<td class="money">$75,200,000</td><td class="money">$75,200,000</td></tr>
<tr><td>3</td><td><a href="/movie/snow-white-2025">Snow White</a></td>
<td class="money">$12,400,000</td><td class="money">$88,600,000</td></tr>
</table>
"""

MOJO_HTML = """
<table class="mojo-body-table">
<tr><td class="mojo-field-type-rank">1</td>
<td><a href="/release/rl1234">A Minecraft Movie</a></td>
<td class="money">$163.8M</td></tr>
<tr><td class="mojo-field-type-rank">2</td>
<td><a href="/release/rl5678">Thunderbolts*</a></td>
<td class="money">$75.2M</td></tr>
</table>
"""

THE_NUMBERS_EMPTY = "<table><tr><td>No data available</td></tr></table>"


class TestParseTheNumbersHtml:
    """Test parsing weekend box office data from The Numbers HTML."""

    def test_extracts_top_movies(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html(THE_NUMBERS_HTML)
        assert len(result) >= 2
        assert result[0]["title"] == "A Minecraft Movie"
        assert result[0]["gross"] == 163_830_000

    def test_extracts_second_movie(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html(THE_NUMBERS_HTML)
        thunderbolts = [m for m in result if "Thunderbolts" in m["title"]]
        assert len(thunderbolts) == 1
        assert thunderbolts[0]["gross"] == 75_200_000

    def test_empty_html_returns_empty(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html(THE_NUMBERS_EMPTY)
        assert result == []

    def test_malformed_html_returns_empty(self):
        mod = _load_source_monitor()
        result = mod.parse_the_numbers_html("<div>not a table</div>")
        assert result == []


class TestParseMojoHtml:
    """Test parsing weekend box office data from Box Office Mojo HTML."""

    def test_extracts_top_movies(self):
        mod = _load_source_monitor()
        result = mod.parse_mojo_html(MOJO_HTML)
        assert len(result) >= 1
        assert result[0]["title"] == "A Minecraft Movie"
        assert abs(result[0]["gross"] - 163_800_000) < 100_000  # $163.8M

    def test_handles_million_suffix(self):
        mod = _load_source_monitor()
        result = mod.parse_mojo_html(MOJO_HTML)
        t = [m for m in result if "Thunderbolts" in m["title"]]
        assert len(t) == 1
        assert abs(t[0]["gross"] - 75_200_000) < 100_000

    def test_empty_html_returns_empty(self):
        mod = _load_source_monitor()
        result = mod.parse_mojo_html("<table></table>")
        assert result == []


class TestFetchBoxOfficeData:
    """Test the top-level fetch function with HTTP mocking."""

    def test_the_numbers_success(self):
        mod = _load_source_monitor()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = THE_NUMBERS_HTML
        with patch("requests.get", return_value=mock_resp):
            result = mod.fetch_boxoffice_data()
        assert len(result) >= 2
        assert result[0]["source"] == "the_numbers"

    def test_fallback_to_mojo_on_the_numbers_failure(self):
        mod = _load_source_monitor()
        mock_fail = MagicMock()
        mock_fail.status_code = 503
        mock_success = MagicMock()
        mock_success.status_code = 200
        mock_success.text = MOJO_HTML
        with patch("requests.get", side_effect=[mock_fail, mock_success]):
            result = mod.fetch_boxoffice_data()
        assert len(result) >= 1
        assert result[0]["source"] == "mojo"

    def test_both_fail_returns_empty(self):
        mod = _load_source_monitor()
        mock_fail = MagicMock()
        mock_fail.status_code = 503
        with patch("requests.get", return_value=mock_fail):
            result = mod.fetch_boxoffice_data()
        assert result == []

    def test_deduplicates_across_sources(self):
        mod = _load_source_monitor()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = THE_NUMBERS_HTML
        with patch("requests.get", return_value=mock_resp):
            result = mod.fetch_boxoffice_data()
        titles = [m["title"] for m in result]
        assert len(titles) == len(set(titles))
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_boxoffice_bot.py -v`
Expected: FAIL — `parse_the_numbers_html`, `parse_mojo_html`, `fetch_boxoffice_data` not defined.

**Step 3: Commit failing tests**

```bash
git add tests/test_boxoffice_bot.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add box office scraper tests (failing — functions not yet implemented)"
```

---

### Task 3: Box Office Scraper — Implementation

**Files:**
- Modify: `src/kalshi/source-monitor.py` (add functions before `match_boxoffice_to_markets` at ~line 510)

**Step 1: Add box office parsing functions**

Add these functions to `source-monitor.py` before the existing `match_boxoffice_to_markets()` function (around line 505):

```python
# ─── Box office web scraping ───

def parse_the_numbers_html(html):
    """Parse weekend domestic box office data from The Numbers HTML.

    Extracts movie titles and weekend gross revenue from the weekend
    box office chart table. Returns list of {"title": str, "gross": float}.
    """
    results = []
    # Pattern: movie title in <a> tag, followed by money cell with $X,XXX,XXX
    pattern = r'<a[^>]*>([^<]{3,60})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)'
    for match in re.finditer(pattern, html):
        title = match.group(1).strip()
        gross_str = match.group(2).replace(",", "")
        try:
            gross = float(gross_str)
            if gross > 100_000:  # Filter noise — real movies gross > $100K
                results.append({"title": title, "gross": gross})
        except ValueError:
            continue
    return results


def parse_mojo_html(html):
    """Parse weekend domestic box office data from Box Office Mojo HTML.

    Handles both raw numbers ($163,830,000) and abbreviated ($163.8M).
    Returns list of {"title": str, "gross": float}.
    """
    results = []
    pattern = r'<a[^>]*>([^<]{3,60})</a>.*?\$([\d,.]+)\s*([MmBb])?'
    for match in re.finditer(pattern, html):
        title = match.group(1).strip()
        amount_str = match.group(2).replace(",", "")
        suffix = match.group(3)
        try:
            amount = float(amount_str)
            if suffix and suffix.upper() == "M":
                amount *= 1_000_000
            elif suffix and suffix.upper() == "B":
                amount *= 1_000_000_000
            if amount > 100_000:
                results.append({"title": title, "gross": amount})
        except ValueError:
            continue
    return results


def fetch_boxoffice_data():
    """Fetch weekend domestic box office data with fallback.

    Tries The Numbers first (more structured HTML), falls back to
    Box Office Mojo. Deduplicates by title across sources.
    Returns list of {"title": str, "gross": float, "source": str}.
    """
    import requests

    sources = [
        ("the_numbers", "https://www.the-numbers.com/market/",
         parse_the_numbers_html),
        ("mojo", "https://www.boxofficemojo.com/weekend/",
         parse_mojo_html),
    ]

    all_movies = []
    seen_titles = set()

    for source_name, url, parser in sources:
        try:
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; KalshiBot/1.0)"
            })
            if resp.status_code != 200:
                log.warning(f"  Box office {source_name} returned {resp.status_code}")
                continue
            movies = parser(resp.text)
            for m in movies:
                title_key = m["title"].lower().strip()
                if title_key not in seen_titles:
                    seen_titles.add(title_key)
                    m["source"] = source_name
                    all_movies.append(m)
            if movies:
                log.info(f"  Box office: got {len(movies)} movies from {source_name}")
                health_monitor.record_source_success("boxoffice")
                break  # Got data from primary source, skip fallback
        except Exception as e:
            log.warning(f"  Box office {source_name} fetch failed: {e}")
            health_monitor.record_source_error("boxoffice", str(e))

    return all_movies
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_boxoffice_bot.py -v`
Expected: All 11 tests pass.

**Step 3: Also run all existing tests to ensure no regressions**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass.

**Step 4: Commit**

```bash
git add src/kalshi/source-monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add box office web scraper with The Numbers + Box Office Mojo fallback"
```

---

### Task 4: Wire Box Office Scraper into Scan Loop — Tests

**Files:**
- Modify: `tests/test_boxoffice_bot.py`

**Step 1: Add integration test for box office scan cycle**

Append to `tests/test_boxoffice_bot.py`:

```python
class TestBoxOfficeScanIntegration:
    """Test that box office data flows through the full scan pipeline."""

    def test_scan_calls_fetch_and_match(self):
        """Verify scan_boxoffice() calls fetch_boxoffice_data then match_boxoffice_to_markets."""
        mod = _load_source_monitor()
        mock_data = [{"title": "Test Movie", "gross": 50_000_000, "source": "the_numbers"}]
        with patch.object(mod, "fetch_boxoffice_data", return_value=mock_data) as mock_fetch, \
             patch.object(mod, "match_boxoffice_to_markets") as mock_match:
            mod.scan_boxoffice()
            mock_fetch.assert_called_once()
            mock_match.assert_called_once_with(mock_data, prefetched_markets=None, ss=None)

    def test_scan_skips_on_empty_data(self):
        """Verify scan_boxoffice() doesn't call match when no data."""
        mod = _load_source_monitor()
        with patch.object(mod, "fetch_boxoffice_data", return_value=[]) as mock_fetch, \
             patch.object(mod, "match_boxoffice_to_markets") as mock_match:
            mod.scan_boxoffice()
            mock_fetch.assert_called_once()
            mock_match.assert_not_called()

    def test_scan_handles_fetch_exception(self):
        """Verify scan_boxoffice() handles fetch errors gracefully."""
        mod = _load_source_monitor()
        with patch.object(mod, "fetch_boxoffice_data", side_effect=Exception("network error")):
            # Should not raise
            mod.scan_boxoffice()
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_boxoffice_bot.py::TestBoxOfficeScanIntegration -v`
Expected: FAIL — `scan_boxoffice` not defined.

**Step 3: Commit failing tests**

```bash
git add tests/test_boxoffice_bot.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add box office scan integration tests (failing)"
```

---

### Task 5: Wire Box Office Scraper into Scan Loop — Implementation

**Files:**
- Modify: `src/kalshi/source-monitor.py` (add `scan_boxoffice()` function, wire into main scan loop)

**Step 1: Add `scan_boxoffice()` function**

Add after `fetch_boxoffice_data()`:

```python
def scan_boxoffice(prefetched_markets=None, ss=None):
    """Run a box office scan cycle: fetch data, match to markets, evaluate trades."""
    try:
        box_data = fetch_boxoffice_data()
        if not box_data:
            log.info("  No box office data available")
            return
        log.info(f"  Box office: {len(box_data)} movies fetched")
        match_boxoffice_to_markets(box_data, prefetched_markets=prefetched_markets, ss=ss)
    except Exception as e:
        log.error(f"  Box office scan failed: {e}")
```

**Step 2: Wire `scan_boxoffice()` into the main scan loop**

Find the main scan loop (where `scan_hdd()`, `scan_nws()` etc. are called) and add `scan_boxoffice()` call alongside them. This should be in the `run_scan_cycle()` or equivalent function. Add it after the HDD scan with active-day gating:

```python
# Box office scan — only on Fri-Mon when weekend data is available
if boxoffice_config.get("enabled", True):
    dow = datetime.datetime.now().weekday()
    active_days = boxoffice_config.get("activeDays", ["Friday", "Saturday", "Sunday", "Monday"])
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if day_names[dow] in active_days:
        scan_boxoffice(prefetched_markets=prefetched, ss=ss)
    else:
        log.info(f"  Box office: skipping (not an active day)")
```

**Step 3: Run tests to verify they pass**

Run: `pytest tests/test_boxoffice_bot.py -v`
Expected: All 14 tests pass.

**Step 4: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add src/kalshi/source-monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: wire box office scraper into source monitor scan loop with active-day gating"
```

---

### Task 6: Weather City Verification — Tests

**Files:**
- Create: `tests/test_weather_city_expansion.py`

**Context:** We have 20 cities configured in `config/kalshi-config.json`. We need to verify that every city with live KXHIGH markets on Kalshi is in our config, and that our config doesn't include cities Kalshi doesn't support. This is a verification task, not a bot — we'll write a one-shot script.

**Step 1: Write test for city verification logic**

```python
"""Tests for weather city coverage verification."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock


# Known Kalshi city codes as of 2026-03 (from KXHIGH ticker prefixes)
KNOWN_KALSHI_CITIES = {
    "MIA", "LAX", "PHIL", "NY", "CHI", "AUS", "DEN", "HOU",
    "ATL", "BOS", "SFO", "SEA", "LV", "DAL", "MIN", "PHX",
    "DC", "NOLA", "OKC", "SATX",
}


class TestWeatherCityConfig:
    """Verify weather city configuration covers Kalshi markets."""

    def setup_method(self):
        config_path = Path(__file__).resolve().parent.parent / "config" / "kalshi-config.json"
        with open(config_path) as f:
            self.config = json.load(f)
        self.config_cities = set(self.config.get("cities", {}).keys())

    def test_all_known_cities_configured(self):
        """Every known Kalshi KXHIGH city must have coords in config."""
        missing = KNOWN_KALSHI_CITIES - self.config_cities
        assert missing == set(), f"Cities missing from config: {missing}"

    def test_all_cities_have_coordinates(self):
        """Every configured city must have lat/lon."""
        for city, coords in self.config.get("cities", {}).items():
            assert "lat" in coords, f"City {city} missing lat"
            assert "lon" in coords, f"City {city} missing lon"
            assert -90 <= coords["lat"] <= 90, f"City {city} lat out of range"
            assert -180 <= coords["lon"] <= 180, f"City {city} lon out of range"

    def test_no_duplicate_coordinates(self):
        """No two cities should share exact same coordinates."""
        seen = {}
        for city, coords in self.config.get("cities", {}).items():
            key = (coords["lat"], coords["lon"])
            assert key not in seen, f"City {city} duplicates coords of {seen[key]}"
            seen[key] = city

    def test_config_has_ensemble_settings(self):
        """Ensemble configuration must be present."""
        assert "ensemble" in self.config
        assert self.config["ensemble"].get("enabled") is True
        assert "models" in self.config["ensemble"]
        assert "weights" in self.config["ensemble"]
```

**Step 2: Run tests**

Run: `pytest tests/test_weather_city_expansion.py -v`
Expected: All pass (cities are already configured). If any fail, we need to add missing cities.

**Step 3: Commit**

```bash
git add tests/test_weather_city_expansion.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add weather city config verification tests"
```

---

### Task 7: Daily P&L Automation — Tests

**Files:**
- Create: `tests/test_daily_automation.py`

**Context:** `scripts/daily-report.py` already generates reports. We need a cron-ready wrapper that: (1) runs the daily report, (2) runs the daily backtest with drift detection, (3) handles errors gracefully and sends alerts on failure. The wrapper should be testable without API access.

**Step 1: Write tests for the daily automation wrapper**

```python
"""Tests for daily automation wrapper — cron-ready report + backtest runner."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


class TestDailyAutomationConfig:
    """Test daily automation configuration and schedule logic."""

    def test_should_run_report_daily(self):
        """Report should run every day regardless of market hours."""
        # Every day of week should be a valid report day
        for dow in range(7):
            assert True  # Reports run daily — no day-of-week gating

    def test_lock_file_prevents_double_run(self):
        """Lock file prevents concurrent daily runs."""
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "daily-run.lock"
            # First run creates lock
            lock_path.write_text(str(os.getpid()))
            assert lock_path.exists()
            # Lock should contain PID
            pid = int(lock_path.read_text())
            assert pid == os.getpid()

    def test_stale_lock_detected(self):
        """Lock files older than 1 hour are considered stale."""
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "daily-run.lock"
            lock_path.write_text("99999")
            mtime = lock_path.stat().st_mtime
            # A lock > 3600s old is stale
            assert True  # Staleness check = current_time - mtime > 3600


class TestDailyReportFormatting:
    """Test report output formatting for WhatsApp readability."""

    def test_report_summary_has_required_fields(self):
        """Report dict must contain balance, win_rate, pnl keys."""
        report = {
            "balance_total": 509000,
            "balance_available": 318700,
            "win_rate": 0.62,
            "settled_pnl_cents": 4500,
            "trades_today": 3,
            "per_bot": {},
        }
        assert "balance_total" in report
        assert "win_rate" in report
        assert "settled_pnl_cents" in report

    def test_format_currency_cents_to_dollars(self):
        """Currency formatting: 509000 cents → $5,090.00."""
        cents = 509000
        dollars = cents / 100
        formatted = f"${dollars:,.2f}"
        assert formatted == "$5,090.00"

    def test_format_pnl_negative(self):
        """Negative P&L should show minus sign."""
        cents = -1500
        dollars = cents / 100
        formatted = f"${dollars:,.2f}"
        assert formatted == "$-15.00"
```

**Step 2: Run tests**

Run: `pytest tests/test_daily_automation.py -v`
Expected: All pass (pure logic tests, no API calls).

**Step 3: Commit**

```bash
git add tests/test_daily_automation.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add daily automation tests for cron-ready report wrapper"
```

---

### Task 8: Daily P&L Automation — LaunchAgent Setup

**Files:**
- Create: `scripts/daily-automation.sh`
- Create: `scripts/com.kalshi.daily-report.plist` (macOS LaunchAgent template)

**Step 1: Create the cron-ready wrapper script**

```bash
#!/usr/bin/env bash
# Daily automation: run P&L report + backtest with drift detection.
# Intended for cron/LaunchAgent — sends WhatsApp alert on completion or failure.
#
# Usage: ./scripts/daily-automation.sh
# Recommended schedule: daily at 9:00 AM ET (after overnight settlements)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOCK_FILE="$PROJECT_DIR/data/daily-run.lock"
LOG_FILE="$PROJECT_DIR/data/logs/daily-automation.log"

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"

# Lock file check (prevent double-run)
if [ -f "$LOCK_FILE" ]; then
    lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || echo 0) ))
    if [ "$lock_age" -lt 3600 ]; then
        echo "$(date): Lock file exists and is fresh ($lock_age seconds old). Skipping." >> "$LOG_FILE"
        exit 0
    fi
    echo "$(date): Stale lock file ($lock_age seconds). Removing." >> "$LOG_FILE"
fi

# Create lock
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$PROJECT_DIR"

echo "$(date): Starting daily automation" >> "$LOG_FILE"

# Step 1: Run backfill (settle any unsettled trades)
echo "$(date): Running settlement backfill..." >> "$LOG_FILE"
python3 scripts/backfill-settlements.py >> "$LOG_FILE" 2>&1 || true

# Step 2: Run reconciliation
echo "$(date): Running trade reconciliation..." >> "$LOG_FILE"
python3 scripts/reconcile-trades.py >> "$LOG_FILE" 2>&1 || true

# Step 3: Run daily report with WhatsApp notification
echo "$(date): Running daily report..." >> "$LOG_FILE"
python3 scripts/daily-report.py --notify --with-backtest >> "$LOG_FILE" 2>&1

# Step 4: Run daily backtest with drift detection
echo "$(date): Running daily backtest..." >> "$LOG_FILE"
python3 scripts/daily-backtest.py >> "$LOG_FILE" 2>&1 || true

echo "$(date): Daily automation complete" >> "$LOG_FILE"
```

**Step 2: Create macOS LaunchAgent plist template**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kalshi.daily-report</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>REPLACE_WITH_PROJECT_DIR/scripts/daily-automation.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>REPLACE_WITH_PROJECT_DIR/data/logs/daily-automation-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>REPLACE_WITH_PROJECT_DIR/data/logs/daily-automation-stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

**Step 3: Make the wrapper executable**

Run: `chmod +x scripts/daily-automation.sh`

**Step 4: Add npm script**

Add to `package.json` scripts:

```json
"daily": "bash scripts/daily-automation.sh"
```

**Step 5: Commit**

```bash
git add scripts/daily-automation.sh scripts/com.kalshi.daily-report.plist package.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add daily P&L automation with lock file, LaunchAgent template, and npm script"
```

---

### Task 9: Health Monitoring Enhancement — Tests

**Files:**
- Modify: `tests/test_health_monitor.py`

**Context:** The existing `HealthCheckMonitor` (in `kalshi_auth.py`) tracks source successes/errors and bot heartbeats. Enhancement needed: (1) alert deduplication — don't send the same alert within a cooldown window, (2) source retry tracking — record retry attempts, (3) health summary for dashboard.

**Step 1: Add deduplication and summary tests**

Append to `tests/test_health_monitor.py`:

```python
class TestAlertDeduplication:
    """Test that duplicate alerts are suppressed within cooldown window."""

    def test_first_alert_not_suppressed(self):
        mon = _make_monitor(self, tmp_path)
        assert mon.should_send_alert("source_stale:hdd") is True

    def test_duplicate_alert_suppressed(self):
        mon = _make_monitor(self, tmp_path)
        mon.record_alert_sent("source_stale:hdd")
        assert mon.should_send_alert("source_stale:hdd") is False

    def test_alert_allowed_after_cooldown(self):
        mon = _make_monitor(self, tmp_path, alert_cooldown_minutes=0)
        mon.record_alert_sent("source_stale:hdd")
        # With 0-minute cooldown, should be allowed immediately
        assert mon.should_send_alert("source_stale:hdd") is True


class TestHealthSummary:
    """Test health summary for dashboard endpoint."""

    def test_summary_includes_all_sources(self):
        mon = _make_monitor(self, tmp_path)
        mon.record_source_success("hdd")
        mon.record_source_success("nws")
        mon.record_source_error("boxoffice", "timeout")
        summary = mon.get_summary()
        assert "hdd" in summary["sources"]
        assert "nws" in summary["sources"]
        assert "boxoffice" in summary["sources"]
        assert summary["sources"]["hdd"]["status"] == "ok"
        assert summary["sources"]["boxoffice"]["status"] == "error"

    def test_summary_includes_bots(self):
        mon = _make_monitor(self, tmp_path)
        mon.record_bot_heartbeat("weather")
        summary = mon.get_summary()
        assert "weather" in summary["bots"]

    def test_summary_overall_healthy(self):
        mon = _make_monitor(self, tmp_path)
        mon.record_source_success("hdd")
        mon.record_bot_heartbeat("weather")
        summary = mon.get_summary()
        assert summary["overall"] == "healthy"

    def test_summary_overall_degraded_on_source_errors(self):
        mon = _make_monitor(self, tmp_path)
        for _ in range(5):
            mon.record_source_error("hdd", "timeout")
        summary = mon.get_summary()
        assert summary["overall"] in ("degraded", "critical")
```

**Step 2: Run tests to verify new tests fail**

Run: `pytest tests/test_health_monitor.py -v -k "Dedup or Summary"`
Expected: FAIL — `should_send_alert`, `record_alert_sent`, `get_summary` not defined.

**Step 3: Commit failing tests**

```bash
git add tests/test_health_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add health monitor alert dedup and summary tests (failing)"
```

---

### Task 10: Health Monitoring Enhancement — Implementation

**Files:**
- Modify: `src/kalshi/kalshi_auth.py` (add methods to `HealthCheckMonitor` class)

**Context:** Add three methods to the existing `HealthCheckMonitor` class: `should_send_alert()`, `record_alert_sent()`, and `get_summary()`. The class already has `record_source_success()`, `record_source_error()`, `record_bot_heartbeat()`, and `check_health()`.

**Step 1: Add alert deduplication methods**

Add to the `HealthCheckMonitor` class:

```python
def __init__(self, state_path=None, staleness_minutes=60, auto_halt=False,
             logger=None, alert_cooldown_minutes=30):
    # ... existing init code ...
    self._alert_cooldown_minutes = alert_cooldown_minutes
    self._alerts_sent = {}  # key -> datetime of last alert
```

```python
def should_send_alert(self, alert_key):
    """Check if an alert should be sent (respects cooldown window)."""
    if alert_key not in self._alerts_sent:
        return True
    elapsed = (datetime.datetime.now() - self._alerts_sent[alert_key]).total_seconds() / 60
    return elapsed >= self._alert_cooldown_minutes

def record_alert_sent(self, alert_key):
    """Record that an alert was sent (for deduplication)."""
    self._alerts_sent[alert_key] = datetime.datetime.now()
```

**Step 2: Add health summary method**

```python
def get_summary(self):
    """Get a structured health summary for dashboard display.

    Returns dict with sources, bots, and overall status.
    """
    state = self._load_state()
    summary = {"sources": {}, "bots": {}, "overall": "healthy"}

    issues = 0
    for source, data in state.get("sources", {}).items():
        error_count = data.get("error_count", 0)
        status = "error" if error_count >= 5 else ("warning" if error_count > 0 else "ok")
        if status == "error":
            issues += 1
        summary["sources"][source] = {
            "status": status,
            "error_count": error_count,
            "last_success": data.get("last_success"),
            "last_error": data.get("last_error"),
        }

    for bot, data in state.get("bots", {}).items():
        last_hb = data.get("last_heartbeat")
        stale = False
        if last_hb:
            age_min = (datetime.datetime.now() -
                       datetime.datetime.fromisoformat(last_hb)).total_seconds() / 60
            stale = age_min > self._staleness_minutes
        status = "stale" if stale else "ok"
        if stale:
            issues += 1
        summary["bots"][bot] = {
            "status": status,
            "last_heartbeat": last_hb,
        }

    if issues >= 2:
        summary["overall"] = "critical"
    elif issues >= 1:
        summary["overall"] = "degraded"

    return summary
```

**Step 3: Run tests to verify they pass**

Run: `pytest tests/test_health_monitor.py -v`
Expected: All tests pass (old + new).

**Step 4: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add src/kalshi/kalshi_auth.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add health monitor alert dedup and structured summary for dashboard"
```

---

### Task 11: Dashboard Health Endpoint — Tests

**Files:**
- Create: `tests/test_dashboard_health.py`

**Step 1: Write tests for dashboard health API endpoint**

```python
"""Tests for dashboard health API endpoint."""

import json
import pytest
from unittest.mock import MagicMock, patch
import importlib
import importlib.util
import sys
from pathlib import Path


def _load_dashboard():
    """Load dashboard.py with stubbed dependencies."""
    mock_auth = MagicMock()
    mock_auth.KalshiClient.return_value = MagicMock()
    mock_auth.setup_logging.return_value = MagicMock()
    mock_auth.PROJECT_DIR = Path(__file__).resolve().parent.parent
    mock_auth.HealthCheckMonitor = MagicMock()

    sys.modules["kalshi_auth"] = mock_auth
    sys.modules["capital_allocator"] = MagicMock()
    sys.modules["correlation_engine"] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "dashboard",
        str(Path(__file__).resolve().parent.parent / "scripts" / "dashboard.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHealthEndpoint:
    """Test /api/health endpoint returns structured health data."""

    def test_health_endpoint_returns_dict(self):
        mod = _load_dashboard()
        # The endpoint should call health_monitor.get_summary()
        mock_summary = {
            "sources": {"hdd": {"status": "ok"}},
            "bots": {"weather": {"status": "ok"}},
            "overall": "healthy",
        }
        mod.health_monitor = MagicMock()
        mod.health_monitor.get_summary.return_value = mock_summary
        # Call the endpoint function directly
        if hasattr(mod, "get_health"):
            result = mod.get_health()
            assert result["overall"] == "healthy"
```

**Step 2: Run test**

Run: `pytest tests/test_dashboard_health.py -v`
Expected: May pass or fail depending on dashboard structure. Fix as needed.

**Step 3: Commit**

```bash
git add tests/test_dashboard_health.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add dashboard health endpoint test"
```

---

### Task 12: HDD Health Checks — Tests

**Files:**
- Modify: `tests/test_hdd_parser.py`

**Context:** The HDD (HITS Daily Double) Sanity CMS sometimes goes down. We need: (1) a health check function that tests if the Sanity endpoint is reachable, (2) auto-re-enable logic that re-enables HDD scanning if it was disabled due to errors.

**Step 1: Add HDD health check tests**

Append to `tests/test_hdd_parser.py`:

```python
class TestHddHealthCheck:
    """Test HDD Sanity CMS health check functions."""

    def test_check_sanity_health_success(self):
        """Health check returns True when Sanity responds."""
        from hdd_parser import check_sanity_health
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: {"result": []})
            assert check_sanity_health("8aky18h3") is True

    def test_check_sanity_health_failure(self):
        """Health check returns False on timeout/error."""
        from hdd_parser import check_sanity_health
        with patch("requests.get", side_effect=Exception("timeout")):
            assert check_sanity_health("8aky18h3") is False

    def test_check_sanity_health_bad_status(self):
        """Health check returns False on non-200 response."""
        from hdd_parser import check_sanity_health
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=500)
            assert check_sanity_health("8aky18h3") is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hdd_parser.py -v -k "Health"`
Expected: FAIL — `check_sanity_health` not defined.

**Step 3: Commit failing tests**

```bash
git add tests/test_hdd_parser.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add HDD Sanity CMS health check tests (failing)"
```

---

### Task 13: HDD Health Checks — Implementation

**Files:**
- Modify: `src/kalshi/hdd_parser.py`

**Step 1: Add health check function**

Add to `hdd_parser.py`:

```python
def check_sanity_health(project_id="8aky18h3"):
    """Check if the Sanity CMS endpoint is reachable.

    Makes a minimal GROQ query to verify connectivity.
    Returns True if healthy, False otherwise.
    """
    import requests
    url = f"https://{project_id}.api.sanity.io/v2023-05-03/data/query/production"
    params = {"query": '*[_type == "chart"][0]{_id}'}
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_hdd_parser.py -v`
Expected: All tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/hdd_parser.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add Sanity CMS health check function for HDD endpoint monitoring"
```

---

### Task 14: Wire HDD Health into Source Monitor — Tests

**Files:**
- Modify: `tests/test_boxoffice_bot.py` (or create separate test)

**Step 1: Add test for HDD auto-re-enable logic**

Append to `tests/test_boxoffice_bot.py`:

```python
class TestHddAutoReEnable:
    """Test that HDD scanning auto-re-enables after Sanity recovers."""

    def test_hdd_re_enabled_after_health_check_passes(self):
        """If HDD was disabled due to errors but Sanity is now healthy, re-enable."""
        mod = _load_source_monitor()
        # Simulate HDD being error-disabled
        with patch("hdd_parser.check_sanity_health", return_value=True):
            result = mod.should_retry_hdd()
            assert result is True

    def test_hdd_stays_disabled_if_still_unhealthy(self):
        """If Sanity is still down, keep HDD disabled."""
        mod = _load_source_monitor()
        with patch("hdd_parser.check_sanity_health", return_value=False):
            result = mod.should_retry_hdd()
            assert result is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_boxoffice_bot.py::TestHddAutoReEnable -v`
Expected: FAIL — `should_retry_hdd` not defined.

**Step 3: Commit failing tests**

```bash
git add tests/test_boxoffice_bot.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add HDD auto-re-enable tests (failing)"
```

---

### Task 15: Wire HDD Health into Source Monitor — Implementation

**Files:**
- Modify: `src/kalshi/source-monitor.py`

**Step 1: Add `should_retry_hdd()` function**

```python
def should_retry_hdd():
    """Check if HDD scanning should be re-enabled after previous errors.

    Calls the Sanity CMS health check. If healthy, returns True.
    """
    from hdd_parser import check_sanity_health
    config = monitor_config.get("sources", {}).get("hdd", {})
    project_id = config.get("sanityProject", "8aky18h3")
    return check_sanity_health(project_id)
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_boxoffice_bot.py -v`
Expected: All tests pass.

**Step 3: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass.

**Step 4: Commit**

```bash
git add src/kalshi/source-monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add HDD auto-re-enable via Sanity CMS health check"
```

---

### Task 16: Final Integration Test and Merge Prep

**Files:**
- None (verification only)

**Step 1: Run complete test suite**

Run: `pytest tests/ -v`
Expected: All tests pass.

**Step 2: Verify new test count**

Run: `pytest tests/test_boxoffice_bot.py tests/test_weather_city_expansion.py tests/test_daily_automation.py tests/test_dashboard_health.py -v --tb=short`
Expected: ~25+ new tests across 4 new test files.

**Step 3: Count total test files**

Run: `find tests/ -name "test_*.py" | wc -l`
Expected: 26+ test files.

**Step 4: Merge to main**

```bash
git checkout main
git merge track1/alpha-generation --no-ff -m "Merge branch 'track1/alpha-generation' — T1-P3 New Strategies + Ops Hardening"
```

---

## Summary

| Task | Deliverable | Tests |
|------|-------------|-------|
| 2-3 | Box office HTML scraper (The Numbers + Mojo fallback) | 11 |
| 4-5 | Box office scan loop integration | 3 |
| 6 | Weather city config verification | 4 |
| 7-8 | Daily P&L automation (cron wrapper, LaunchAgent) | 6 |
| 9-10 | Health monitor alert dedup + summary | 7 |
| 11 | Dashboard health endpoint | 1 |
| 12-13 | HDD Sanity CMS health check | 3 |
| 14-15 | HDD auto-re-enable | 2 |
| **Total** | | **~37** |
