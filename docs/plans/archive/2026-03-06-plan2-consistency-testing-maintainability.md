# Consistency, Testing & Maintainability Fixes

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the 24 MEDIUM and 14 LOW severity issues covering bot consistency, test infrastructure, config quality, naming conventions, and dead code removal. These improve developer experience and reduce future debugging time.

**Architecture:** This plan is organized into 4 phases: (1) Test infrastructure improvements, (2) Bot consistency fixes, (3) Config/naming cleanup, (4) Dead code removal. Each phase is independent. Earlier phases should be done first because they make later work easier to verify.

**Tech Stack:** Python 3, pytest, json

**Dependency:** Plan 1 (Critical Data Quality Fixes) should be completed first — it fixes shared module fundamentals that this plan builds on.

---

## Phase 1: Test Infrastructure (Tasks 1-3)

### Task 1: Extract Shared `fake_auth` Fixture into conftest.py (H14)

9+ test files duplicate a 60-line import stub for `kalshi_auth`. Extract it into a shared fixture.

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_weather.py`, `tests/test_economics.py`, `tests/test_trade_manager.py`, `tests/test_position_exits.py`, `tests/test_source_monitor.py`, `tests/test_boxoffice_bot.py`, `tests/test_weather_bot_phase3.py`, `tests/test_strategy_bugs.py`, `tests/test_optimization.py` (remove duplicated stubs)

**Step 1: Read all existing fake_auth stubs to find the union of all stubbed attributes**

Read the loader functions in each test file. Build the complete list of attributes that `kalshi_auth` must expose. The typical set is:

```python
KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
TradeManager, HealthCheckMonitor, OrderMonitor, ScanSummary,
save_trade, load_trades, _atomic_write_json, atomic_write_json,
check_kill_switch, PROJECT_DIR, CircuitBreaker, RecentTradeTracker,
trim_trade_log, validate_trade_config, build_market_snapshot,
save_decision, fetch_parallel, notify_whatsapp, notify_webhook,
_local_today, is_shutdown_requested, CITY_TIMEZONES
```

**Step 2: Create the shared fixture in conftest.py**

```python
"""Shared fixtures and path setup for the Kalshi trading bot test suite."""

import sys
import types
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src" / "kalshi")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

PROJECT_DIR = Path(__file__).resolve().parent.parent


def make_fake_auth(**overrides):
    """Create a fake kalshi_auth module with all required stubs.

    Usage in test files:
        fake = make_fake_auth()
        sys.modules["kalshi_auth"] = fake
        # Now import your bot module
    """
    mod = types.ModuleType("kalshi_auth")

    # Core classes — MagicMock instances
    mod.KalshiClient = MagicMock
    mod.TradeManager = MagicMock
    mod.HealthCheckMonitor = MagicMock
    mod.OrderMonitor = MagicMock
    mod.ScanSummary = MagicMock
    mod.CircuitBreaker = MagicMock
    mod.RecentTradeTracker = MagicMock

    # Setup functions — no-ops
    mod.setup_unbuffered = lambda: None
    mod.setup_signal_handlers = lambda: None
    mod.setup_logging = lambda name: __import__("logging").getLogger(name)

    # File operations
    mod.save_trade = MagicMock()
    mod.load_trades = MagicMock(return_value=[])
    mod.save_decision = MagicMock()
    mod._atomic_write_json = MagicMock()
    mod.atomic_write_json = MagicMock()
    mod.trim_trade_log = MagicMock()

    # Safety
    mod.check_kill_switch = MagicMock()
    mod.is_shutdown_requested = MagicMock(return_value=False)
    mod.validate_trade_config = MagicMock()
    mod.build_market_snapshot = MagicMock(return_value={})
    mod.fetch_parallel = MagicMock(return_value=[])
    mod.notify_whatsapp = MagicMock()
    mod.notify_webhook = MagicMock()

    # Constants
    mod.PROJECT_DIR = PROJECT_DIR
    mod.CITY_TIMEZONES = {}
    mod._local_today = MagicMock()

    # Apply overrides
    for k, v in overrides.items():
        setattr(mod, k, v)

    return mod


def load_bot_module(bot_filename, fake_auth=None):
    """Load a hyphenated bot module (e.g., 'weather-bot.py') with fake kalshi_auth.

    Args:
        bot_filename: e.g., 'weather-bot.py'
        fake_auth: Optional pre-configured fake_auth module. If None, creates default.

    Returns:
        The loaded bot module.

    Usage:
        fake = make_fake_auth()
        bot = load_bot_module("weather-bot.py", fake)
    """
    if fake_auth is None:
        fake_auth = make_fake_auth()

    saved_modules = {}
    for mod_name in ["kalshi_auth"]:
        if mod_name in sys.modules:
            saved_modules[mod_name] = sys.modules[mod_name]
        sys.modules[mod_name] = fake_auth

    try:
        bot_path = Path(_SRC_DIR) / bot_filename
        spec = importlib.util.spec_from_file_location(
            bot_filename.replace("-", "_").replace(".py", ""),
            str(bot_path),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        # Restore original modules
        for mod_name, original in saved_modules.items():
            sys.modules[mod_name] = original
        for mod_name in ["kalshi_auth"]:
            if mod_name not in saved_modules and mod_name in sys.modules:
                del sys.modules[mod_name]
```

**Step 3: Run existing tests to ensure conftest changes don't break anything**

Run: `pytest tests/ -x -q`
Expected: All PASS (we only added new functions, didn't change existing import)

**Step 4: Migrate one test file as exemplar**

Pick `tests/test_weather.py` — replace its local `_load_weather_bot()` / fake_auth setup with:

```python
from conftest import make_fake_auth, load_bot_module

# At module level or in fixture:
_fake_auth = make_fake_auth()
weather_bot = load_bot_module("weather-bot.py", _fake_auth)
```

**Step 5: Run the migrated test file**

Run: `pytest tests/test_weather.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add tests/conftest.py tests/test_weather.py
git commit -m "refactor(tests): extract shared fake_auth fixture into conftest.py"
```

**Step 7: Migrate remaining test files (one at a time, test after each)**

Repeat Step 4-5 for each of:
- `test_economics.py`
- `test_trade_manager.py`
- `test_position_exits.py`
- `test_source_monitor.py`
- `test_boxoffice_bot.py`
- `test_weather_bot_phase3.py`
- `test_strategy_bugs.py`
- `test_optimization.py`

Commit after each migration or batch them.

---

### Task 2: Fix Crypto Tests to Test Production Code (H15)

9 test classes re-implement production logic instead of importing it. They verify the copy, not the actual code.

**Files:**
- Modify: `tests/test_crypto.py`

**Step 1: Identify the re-implemented functions**

Search `test_crypto.py` for method definitions starting with `_` that duplicate production logic:
- `_parse_dvol` → should import from `crypto-bot.py` or `crypto_models.py`
- `_blend` → should import from production
- `_effective_edge_threshold` → should import from production
- `_compute_trailing_drift` → should import from production
- `_select_lookback` → should import from production
- `_compute_vol` → should import from production

**Step 2: For each re-implemented function, determine if the production version is importable**

Since `crypto-bot.py` uses hyphens, functions must be loaded via `load_bot_module`. If the functions are module-level in `crypto-bot.py`, they can be accessed after loading.

If functions are nested inside the scan loop (not module-level), they need to be extracted into `crypto_models.py` first. Check this before proceeding.

**Step 3: Replace re-implementations with imports**

For each test class, replace the local `_function` with:

```python
# At top of test file
from conftest import make_fake_auth, load_bot_module
_fake = make_fake_auth()
_crypto_bot = load_bot_module("crypto-bot.py", _fake)

class TestCryptoBlending:
    def test_blend_weights(self):
        # Use production function instead of local copy
        result = _crypto_bot._blend(iv=0.5, rv=0.4)
        assert ...
```

**Step 4: Run crypto tests**

Run: `pytest tests/test_crypto.py -v`
Expected: All PASS (if production logic matches the copies)

**Step 5: Commit**

```bash
git add tests/test_crypto.py
git commit -m "fix(tests): crypto tests now test production code, not re-implemented copies"
```

---

### Task 3: Fix Conditional Test Assertions (M23)

`test_kelly.py` has assertions guarded by `if` conditions that can silently skip.

**Files:**
- Modify: `tests/test_kelly.py:323-347`

**Step 1: Find and fix conditional assertions**

Search for `if ... >= 1 and ... <= 2:` guards around assertions. Replace with unconditional assertions that document expected ranges:

```python
# Before (silently skips if out of range):
if hk_contracts >= 1 and hk_contracts <= 2:
    assert qk_contracts == 1

# After (always asserts, documents expected range):
assert hk_contracts >= 1, f"half_kelly should return >= 1 contract, got {hk_contracts}"
assert hk_contracts <= 2, f"half_kelly should return <= 2 contracts, got {hk_contracts}"
assert qk_contracts == 1, f"quarter_kelly should preserve single contract"
```

**Step 2: Run tests**

Run: `pytest tests/test_kelly.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/test_kelly.py
git commit -m "fix(tests): remove conditional assertions that could silently skip"
```

---

## Phase 2: Bot Consistency (Tasks 4-8)

### Task 4: Add Health Monitoring to Market-Maker (H12)

Market-maker has no heartbeats, no health checks, no decision logging.

**Files:**
- Modify: `src/kalshi/market-maker.py`

**Step 1: Read market-maker.py to understand its scan loop structure**

**Step 2: Add HealthCheckMonitor initialization**

Near the top where other bots initialize health:

```python
health = HealthCheckMonitor(log)
```

**Step 3: Add heartbeat in scan loop**

In the main scan loop (around line 477-488), add before the scan:

```python
health.record_bot_heartbeat("market-maker")
```

**Step 4: Add decision logging**

After each market evaluation, call:

```python
trade_manager.log_decision(ticker, side, action, reason, edge=edge, price_cents=price_cents)
```

**Step 5: Run market-maker tests**

Run: `pytest tests/test_mm_calibration.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/kalshi/market-maker.py
git commit -m "feat(market-maker): add health monitoring, heartbeats, and decision logging"
```

---

### Task 5: Add `--once` Flag to weather-bot, entertainment-bot, source-monitor (L13)

Three daemon bots lack `--once` for single-scan testing.

**Files:**
- Modify: `src/kalshi/weather-bot.py`
- Modify: `src/kalshi/entertainment-bot.py`
- Modify: `src/kalshi/source-monitor.py`

**Step 1: Add argparse to weather-bot**

Near the `if __name__ == "__main__"` block:

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Weather temperature trading bot")
    parser.add_argument("--once", action="store_true", help="Run single scan then exit")
    args = parser.parse_args()

    if args.once:
        run_scan()  # single scan function
    else:
        run_daemon()  # existing loop
```

This requires the scan logic to be extractable into a `run_scan()` function. Read the bot to determine the right refactoring.

**Step 2: Repeat for entertainment-bot and source-monitor**

Same pattern.

**Step 3: Test each bot's `--once` flag**

```bash
python3 src/kalshi/weather-bot.py --once  # should run one scan and exit
```

**Step 4: Commit**

```bash
git add src/kalshi/weather-bot.py src/kalshi/entertainment-bot.py src/kalshi/source-monitor.py
git commit -m "feat(bots): add --once flag to weather, entertainment, and source-monitor bots"
```

---

### Task 6: Add Graceful Shutdown Check to strategy-trader (M18)

Strategy-trader's daemon loop never checks `is_shutdown_requested()`.

**Files:**
- Modify: `src/kalshi/strategy-trader.py:817-839`

**Step 1: Read the scan loop**

**Step 2: Add shutdown check**

In the daemon loop, add:

```python
while True:
    if is_shutdown_requested():
        log.info("Shutdown requested, exiting")
        break
    # ... existing scan logic ...
```

**Step 3: Run tests**

Run: `pytest tests/test_strategy_engine.py tests/test_strategy_bugs.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add src/kalshi/strategy-trader.py
git commit -m "fix(strategy-trader): add is_shutdown_requested() check in daemon loop"
```

---

### Task 7: Fix entertainment-bot Initialization Order (L12)

`entertainment-bot.py` calls `setup_signal_handlers()` before `setup_logging()`.

**Files:**
- Modify: `src/kalshi/entertainment-bot.py:16-30`

**Step 1: Reorder initialization**

Move `setup_signal_handlers()` after `log = setup_logging("entertainment")` to match all other bots.

**Step 2: Run tests**

Run: `pytest tests/test_entertainment.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add src/kalshi/entertainment-bot.py
git commit -m "fix(entertainment): reorder setup_signal_handlers after setup_logging"
```

---

### Task 8: Fix `place_order` Return Type Documentation (M16)

`place_order` returns `None` for 10+ failure reasons. Add a comment documenting possible return states.

**Files:**
- Modify: `src/kalshi/kalshi_auth.py:1163-1170`

**Step 1: Update docstring**

```python
    def place_order(self, ticker, side, price_cents, count, reasoning,
                    available_balance_cents=None, market_data_age_seconds=None,
                    **extra_fields):
        """Place a limit order with full safety checks.

        Returns:
            dict: Order info from API on success (contains 'order_id', 'status').
            None: On any failure. Check log for reason. Failure causes include:
                - kill_switch: Trading halted via data/HALT_TRADING
                - circuit_breaker: Too many consecutive API failures
                - daily_trade_limit: Max trades per day reached
                - daily_loss_limit: Max daily loss reached
                - dedup: Same ticker traded recently (cooldown)
                - cost_cap: Order cost exceeds max trade amount
                - balance: Insufficient available balance
                - stale_data: Market data too old
                - api_error: Kalshi API returned an error
                - allocator_denied: Capital allocator rejected the request
        """
```

**Step 2: Commit**

```bash
git add src/kalshi/kalshi_auth.py
git commit -m "docs(auth): document place_order return type and failure reasons"
```

---

## Phase 3: Config & Naming Cleanup (Tasks 9-12)

### Task 9: Remove Dead Config Keys (L9)

9 config keys in `bots-config.json` are never read by any code.

**Files:**
- Modify: `config/bots-config.json`

**Step 1: Verify each key is truly dead**

Run grep for each suspected dead key:

```bash
grep -rn "midRangeEdgeThreshold" src/ scripts/
grep -rn "midRangeLow" src/ scripts/
grep -rn "midRangeHigh" src/ scripts/
grep -rn "useJumpDiffusion" src/ scripts/
grep -rn "jumpDiffusion" src/ scripts/
grep -rn "minTradesForEmpirical" src/ scripts/
grep -rn "empiricalWeightCap" src/ scripts/
grep -rn "regime.*enabled" src/ scripts/
grep -rn "regime.*maxAgeHours" src/ scripts/
```

**Step 2: Remove confirmed dead keys**

Remove each key that has zero code references from `config/bots-config.json`.

**Step 3: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All PASS

**Step 4: Commit**

```bash
git add config/bots-config.json
git commit -m "config: remove 9 orphaned config keys that no code reads"
```

---

### Task 10: Remove Dead `mode` Config (M22)

Both `kalshi-config.json` and `kalshi-monitor-config.json` have `"mode": "demo"` that is purely cosmetic — actual mode comes from `KALSHI_MODE` env var.

**Files:**
- Modify: `config/kalshi-config.json`
- Modify: `config/kalshi-monitor-config.json`
- Modify: `src/kalshi/weather-bot.py` (remove cosmetic mode log)
- Modify: `src/kalshi/source-monitor.py` (remove cosmetic mode log)

**Step 1: Remove `mode` from config files**

**Step 2: Remove `config['mode']` references in weather-bot and source-monitor**

These are cosmetic log lines — replace with the actual mode from env:

```python
log.info("Mode: %s", os.environ.get("KALSHI_MODE", "demo"))
```

**Step 3: Run tests**

Run: `pytest tests/test_weather.py tests/test_source_monitor.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add config/kalshi-config.json config/kalshi-monitor-config.json src/kalshi/weather-bot.py src/kalshi/source-monitor.py
git commit -m "config: remove dead 'mode' keys (actual mode is KALSHI_MODE env var)"
```

---

### Task 11: Standardize Config Section Naming Convention (M24)

Config sections mix `snake_case` (`position_monitor`) and `camelCase`/`flat` (`crypto`).

**Files:**
- Modify: `config/bots-config.json` (add snake_case aliases)
- Modify: bot files that read config (add fallback)

**NOTE:** This is a backward-compatible migration. Add snake_case aliases alongside existing keys. Do NOT remove existing keys yet.

**Step 1: Add aliases in bots-config.json**

For sections that aren't already snake_case, add aliases. For example, if `crypto` should be `crypto_bot`, add both. However, the simplest fix is to document the convention: single-word sections use the word directly (`crypto`, `weather`, `economics`), multi-word sections use snake_case (`position_monitor`, `cross_platform_arb`, `market_maker`).

**Step 2: Add a comment at the top of bots-config.json**

Since JSON doesn't support comments, add a `"_naming_convention"` key:

```json
{
    "_naming_convention": "Single-word: flat (crypto, weather). Multi-word: snake_case (position_monitor). All values in dollars unless key ends with 'Cents'. All times in seconds unless key ends with 'Hours' or 'Minutes'.",
    ...
}
```

**Step 3: Commit**

```bash
git add config/bots-config.json
git commit -m "docs(config): document naming convention for config keys"
```

---

### Task 12: Fix Dashboard Trade File List Duplication (M21)

Dashboard maintains its own `TRADE_FILES` list that diverges from `trade_files.py`.

**Files:**
- Modify: `scripts/dashboard.py:86-95`
- Read: `src/kalshi/trade_files.py`

**Step 1: Read trade_files.py to understand the canonical list**

**Step 2: Import and use the canonical list in dashboard**

```python
# In dashboard.py, replace hardcoded TRADE_FILES with:
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
from trade_files import TRADE_FILES as _CANONICAL_FILES

TRADE_FILES = [
    {"path": PROJECT_DIR / "data" / f["filename"], "bot": f["bot"]}
    for f in _CANONICAL_FILES
]
```

**Step 3: Run dashboard tests (if any)**

Run: `pytest tests/test_dashboard_health.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add scripts/dashboard.py
git commit -m "refactor(dashboard): use canonical TRADE_FILES from trade_files.py"
```

---

## Phase 4: Dead Code Removal (Tasks 13-16)

### Task 13: Remove Unused Imports from Bot Files (L10)

`weather-bot.py`, `entertainment-bot.py`, `source-monitor.py` import `load_trades` and `save_trade` which are now handled by `TradeManager`.

**Files:**
- Modify: `src/kalshi/weather-bot.py:9`
- Modify: `src/kalshi/entertainment-bot.py:10`
- Modify: `src/kalshi/source-monitor.py:15`

**Step 1: Remove unused imports**

Verify with grep that these functions are not called in each file:

```bash
grep -n "load_trades\|save_trade" src/kalshi/weather-bot.py
grep -n "load_trades\|save_trade" src/kalshi/entertainment-bot.py
grep -n "load_trades\|save_trade" src/kalshi/source-monitor.py
```

Remove from import lines if confirmed unused.

Also remove `ensemble_weather_probability` from `weather-bot.py` if replaced by `ensemble_weather_probability_v2`.

**Step 2: Run tests**

Run: `pytest tests/test_weather.py tests/test_entertainment.py tests/test_source_monitor.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add src/kalshi/weather-bot.py src/kalshi/entertainment-bot.py src/kalshi/source-monitor.py
git commit -m "cleanup: remove unused load_trades/save_trade imports from bot files"
```

---

### Task 14: Deduplicate `_norm_cdf` Implementations (L8)

`simulation.py` and `scenario_engine.py` each redefine `_norm_cdf` / `_norm_pdf` that already exist in `probability.py`.

**Files:**
- Modify: `src/kalshi/simulation.py:25`
- Modify: `src/kalshi/scenario_engine.py:20`

**Step 1: Replace local definitions with imports**

In `simulation.py`:
```python
from probability import _norm_cdf, _norm_pdf
```
Remove the local definitions.

In `scenario_engine.py`:
```python
from probability import _norm_cdf
```
Remove the local definition (keep the fallback if it's guarded by a try/except for standalone usage).

**Step 2: Run tests**

Run: `pytest tests/test_simulation.py tests/test_scenario_engine.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add src/kalshi/simulation.py src/kalshi/scenario_engine.py
git commit -m "cleanup: deduplicate _norm_cdf/_norm_pdf, import from probability.py"
```

---

### Task 15: Remove Deprecated `edge_after_fees` (L11)

No bot code calls this function. Only tests test it.

**Files:**
- Modify: `src/kalshi/probability.py:1080-1092`
- Modify: tests that reference it (update to test the current approach)

**Step 1: Verify no production code calls it**

```bash
grep -rn "edge_after_fees" src/ scripts/
```

Should only return probability.py definition and test files.

**Step 2: Remove the function and alias**

Delete lines 1080-1092 from `probability.py`.

**Step 3: Update or remove tests that test the deprecated function**

**Step 4: Run tests**

Run: `pytest tests/test_probability.py -v`
Expected: All PASS (after removing deprecated tests)

**Step 5: Commit**

```bash
git add src/kalshi/probability.py tests/test_probability.py
git commit -m "cleanup: remove deprecated edge_after_fees function"
```

---

### Task 16: Fix `parse_crypto_ticker` Return Type Consistency (M17)

Returns dicts with 4 different key sets depending on code path.

**Files:**
- Modify: `src/kalshi/ticker_utils.py:48-171`
- Test: `tests/test_ticker_utils.py`

**Step 1: Write tests documenting expected return keys**

```python
# In tests/test_ticker_utils.py — add

class TestCryptoTickerReturnConsistency:
    """All parse_crypto_ticker return paths must include the same keys."""

    REQUIRED_KEYS = {"asset", "date", "threshold", "direction", "market_type"}

    def test_15min_ticker_has_all_keys(self):
        from ticker_utils import parse_crypto_ticker
        result = parse_crypto_ticker("KXBTC-26MAR06-T68000-B15M")
        if result:
            missing = self.REQUIRED_KEYS - set(result.keys())
            assert not missing, f"15-min ticker missing keys: {missing}"

    def test_monthly_ticker_has_all_keys(self):
        from ticker_utils import parse_crypto_ticker
        result = parse_crypto_ticker("KXBTCM-26MAR-MAX-T75000")
        if result:
            missing = self.REQUIRED_KEYS - set(result.keys())
            assert not missing, f"Monthly ticker missing keys: {missing}"

    def test_standard_ticker_has_all_keys(self):
        from ticker_utils import parse_crypto_ticker
        result = parse_crypto_ticker("KXBTC-26MAR06-T68000")
        if result:
            missing = self.REQUIRED_KEYS - set(result.keys())
            assert not missing, f"Standard ticker missing keys: {missing}"
```

**Step 2: Run tests to see which fail**

Run: `pytest tests/test_ticker_utils.py::TestCryptoTickerReturnConsistency -v`

**Step 3: Fix return paths to always include all keys**

Ensure every return path includes all keys in `REQUIRED_KEYS`, using `None` for absent values:

```python
# Example: add missing keys with None defaults
result = {
    "asset": asset,
    "date": date_str,
    "threshold": threshold,
    "direction": direction,
    "market_type": market_type or "standard",  # always present
    "settlement_hour": settlement_hour,  # None if not applicable
}
```

**Step 4: Run tests**

Run: `pytest tests/test_ticker_utils.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/kalshi/ticker_utils.py tests/test_ticker_utils.py
git commit -m "fix(ticker_utils): consistent return keys from parse_crypto_ticker"
```

---

## Phase 5: Dashboard Resilience (Tasks 17-18)

### Task 17: Fix Dashboard Silent Exception Swallowing (M12)

Dashboard has 6 `except Exception: pass` blocks.

**Files:**
- Modify: `scripts/dashboard.py`

**Step 1: Find all silent exception blocks**

```bash
grep -n "except.*pass" scripts/dashboard.py
```

**Step 2: Replace each `pass` with `logger.warning`**

```python
# Before:
except Exception:
    pass

# After:
except Exception as e:
    logger.warning("Failed to ...: %s", e)
```

**Step 3: Fix `_kalshi_available` permanent failure**

Add a TTL to the API availability check so it retries after 60 seconds:

```python
_kalshi_available = None
_kalshi_checked_at = 0

def get_kalshi_client():
    global _kalshi_available, _kalshi_checked_at
    if _kalshi_available is False and time.time() - _kalshi_checked_at < 60:
        return None
    try:
        client = KalshiClient()
        _kalshi_available = True
        return client
    except Exception as e:
        logger.warning("Kalshi API unavailable: %s", e)
        _kalshi_available = False
        _kalshi_checked_at = time.time()
        return None
```

**Step 4: Commit**

```bash
git add scripts/dashboard.py
git commit -m "fix(dashboard): log warnings instead of silent pass, add API retry"
```

---

### Task 18: Fix Dashboard Bot Attribution Heuristic (M19)

`_infer_bot_from_ticker()` misattributes some tickers.

**Files:**
- Modify: `scripts/dashboard.py`

**Step 1: Read the current heuristic**

**Step 2: Add missing prefix mappings**

```python
def _infer_bot_from_ticker(ticker):
    """Infer which bot placed a trade based on ticker prefix."""
    prefixes = {
        "KXHIGH": "weather",
        "KXBTC": "crypto",
        "KXETH": "crypto",
        "KXCPI": "economics",
        "KXGDP": "economics",
        "KXJOBS": "economics",
        "KXFED": "economics",
        "KXGAS": "economics",
        "KXINFL": "economics",
        # Add other known prefixes as bots expand
    }
    for prefix, bot in prefixes.items():
        if ticker.startswith(prefix):
            return bot
    return "strategy"  # default fallback
```

**Step 3: Commit**

```bash
git add scripts/dashboard.py
git commit -m "fix(dashboard): expand bot attribution ticker prefixes"
```

---

## Verification Checklist

After all 18 tasks:

```bash
# Full test suite
pytest tests/ -v

# No more duplicated fake_auth stubs (should only be in conftest.py)
grep -rn "fake_auth\|types.ModuleType.*kalshi_auth" tests/ | grep -v conftest.py | grep -v ".pyc"

# No orphaned config keys
grep -rn "midRangeEdgeThreshold\|useJumpDiffusion\|minTradesForEmpirical" config/ src/

# No duplicated _norm_cdf
grep -rn "def _norm_cdf" src/
# Should return only probability.py

# Dashboard uses canonical trade files
grep -n "TRADE_FILES" scripts/dashboard.py
# Should show import from trade_files.py, not hardcoded list
```
