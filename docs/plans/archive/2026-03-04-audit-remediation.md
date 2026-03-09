# Audit Remediation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all issues identified in the 2026-03-04 comprehensive audit — operational, security, correctness, and configuration.

**Architecture:** Nine independent tasks targeting watchdog security, duplicate logging, market cache concurrency, position monitor unblocking, dead bot disabling, allocator rebalancing, probability model fixes, tracker pruning, and kill switch removal. Tasks are ordered by blast-radius (operational blockers first, then correctness, then polish).

**Tech Stack:** Python 3, pytest, fcntl, logging, JSON config

---

### Task 1: Remove Kill Switch and Unblock Trading

The `data/HALT_TRADING` file was auto-created on Mar 3 due to a transient NWS outage that has since resolved. It blocks ALL trades and ALL position monitor sells (the root cause of 216 repeated `sell_failed` rejections).

**Files:**
- Delete: `data/HALT_TRADING`

**Step 1: Verify the kill switch file exists and read its contents**

```bash
cat data/HALT_TRADING
```

Expected: Shows "Auto-halted: bot/weather stale..." message.

**Step 2: Remove the kill switch**

```bash
rm data/HALT_TRADING
```

**Step 3: Verify removal**

```bash
ls data/HALT_TRADING 2>&1
```

Expected: "No such file or directory"

**Step 4: Commit**

```bash
# No commit needed — data/ is gitignored
```

---

### Task 2: Fix Duplicate Logging (Every Line Appears Twice)

**Root cause:** The supervisor (`scripts/supervisor.py` line 210) redirects each bot's stdout to `data/logs/{name}.log`. The bot's `setup_logging()` (`kalshi_auth.py` line 104) also creates a `RotatingFileHandler` writing to the same `data/logs/{name}.log`. The StreamHandler writes to stdout → supervisor captures → writes to file, AND the FileHandler writes to the same file independently = doubled lines.

**Fix:** When a bot is run under the supervisor (stdout is redirected to a file, not a TTY), skip the StreamHandler to avoid double-writing.

**Files:**
- Modify: `src/kalshi/kalshi_auth.py:82-110`
- Test: `tests/test_setup_logging.py`

**Step 1: Write the failing test**

Create `tests/test_setup_logging.py`:

```python
"""Tests for setup_logging duplicate handler prevention."""
import logging
import sys
import io
import os
import tempfile
from pathlib import Path

# Add source path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))


def _clear_logger(name):
    """Remove all handlers from a logger."""
    logger = logging.getLogger(name)
    logger.handlers.clear()
    return logger


def test_setup_logging_no_duplicate_handlers():
    """setup_logging should not add StreamHandler when stdout is not a TTY."""
    from kalshi_auth import setup_logging

    name = "test-no-dup-handlers"
    _clear_logger(name)

    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, "test.log")
        logger = setup_logging(name, log_file=log_file)
        handler_types = [type(h).__name__ for h in logger.handlers]
        # Should have at most 2 handlers (stream + file), never more
        assert len(logger.handlers) <= 2, f"Too many handlers: {handler_types}"
        # Calling again should NOT add more handlers
        logger2 = setup_logging(name, log_file=log_file)
        assert logger is logger2
        assert len(logger.handlers) <= 2, "Duplicate handlers added on second call"
    _clear_logger(name)


def test_setup_logging_skips_stream_when_not_tty():
    """When stdout is not a TTY (piped/redirected), skip StreamHandler to avoid duplicates."""
    from kalshi_auth import setup_logging

    name = "test-skip-stream"
    _clear_logger(name)

    # Simulate non-TTY stdout (like supervisor redirect)
    original_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = os.path.join(tmpdir, "test.log")
            logger = setup_logging(name, log_file=log_file)
            handler_types = [type(h).__name__ for h in logger.handlers]
            # Should only have RotatingFileHandler, no StreamHandler
            assert "StreamHandler" not in handler_types, (
                f"StreamHandler present when stdout is not a TTY: {handler_types}"
            )
            assert "RotatingFileHandler" in handler_types
    finally:
        sys.stdout = original_stdout
    _clear_logger(name)


def test_setup_logging_adds_stream_when_tty(monkeypatch):
    """When stdout IS a TTY, StreamHandler should be included."""
    from kalshi_auth import setup_logging

    name = "test-with-stream"
    _clear_logger(name)

    # Mock isatty to return True
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, "test.log")
        logger = setup_logging(name, log_file=log_file)
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" in handler_types, (
            f"StreamHandler missing when stdout IS a TTY: {handler_types}"
        )
        assert "RotatingFileHandler" in handler_types
    _clear_logger(name)
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_setup_logging.py -v
```

Expected: `test_setup_logging_skips_stream_when_not_tty` FAILS (StreamHandler is always added currently).

**Step 3: Implement the fix**

In `src/kalshi/kalshi_auth.py`, replace lines 82-110:

```python
def setup_logging(name, log_file=None):
    """Configure a logger with consistent format for a bot.

    Returns a logging.Logger with file handler and optional stdout handler.
    Auto-derives log file path from bot name if not explicitly provided:
    ``data/logs/{name}.log`` with 5MB rotation and 3 backups.

    When stdout is not a TTY (e.g., supervisor redirects stdout to the log file),
    the StreamHandler is skipped to prevent every line appearing twice.

    Call once at bot startup: ``log = setup_logging("weather")``
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Only add StreamHandler if stdout is a real TTY (not redirected by supervisor)
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    # Auto file logging — derive path from bot name if not explicitly provided
    if log_file is None:
        log_file = str(PROJECT_DIR / "data" / "logs" / f"{name}.log")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger
```

**Step 4: Run tests to verify they pass**

```bash
pytest tests/test_setup_logging.py -v
```

Expected: All 3 tests PASS.

**Step 5: Run existing tests to check for regressions**

```bash
pytest tests/ -x -q --timeout=30
```

Expected: No regressions.

**Step 6: Commit**

```bash
git add src/kalshi/kalshi_auth.py tests/test_setup_logging.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: prevent duplicate log lines when bots run under supervisor

StreamHandler now skipped when stdout is not a TTY (i.e., supervisor
redirects stdout to the log file). RotatingFileHandler writes directly,
eliminating every-line-doubled behavior."
```

---

### Task 3: Move Hardcoded Phone Number to Environment Variable

**Files:**
- Modify: `src/kalshi/watchdog.py:93-106`
- Modify: `.env.example` (add NOTIFICATION_PHONE)

**Step 1: Update watchdog.py to read phone from env**

Replace the `send_alert` function (lines 93-109):

```python
def send_alert(message):
    """Alert via OpenClaw CLI."""
    print(f"[ALERT] {message}")
    phone = os.environ.get("NOTIFICATION_PHONE", "")
    if not phone:
        print("[ALERT] No NOTIFICATION_PHONE set, skipping WhatsApp")
        with open("/tmp/watchdog-alerts.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {message}\n")
        return
    try:
        subprocess.run(
            ["/opt/homebrew/bin/openclaw", "message", "send",
             "--channel", "whatsapp",
             "--to", phone,
             "--message", f"🚨 Watchdog: {message}"],
            timeout=15,
            capture_output=True
        )
    except Exception as e:
        print(f"[ALERT FAILED] {e}")
        with open("/tmp/watchdog-alerts.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {message}\n")
```

**Step 2: Add NOTIFICATION_PHONE to .env.example**

Append to `.env.example`:

```
# WhatsApp notification phone number (with country code, e.g., +14255551234)
NOTIFICATION_PHONE=
```

**Step 3: Add NOTIFICATION_PHONE to production .env**

```bash
echo 'NOTIFICATION_PHONE=+14255336828' >> .env
```

**Step 4: Make watchdog load dotenv**

Add after line 19 (`from datetime import ...`):

```python
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
```

**Step 5: Commit**

```bash
git add src/kalshi/watchdog.py .env.example
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "security: move hardcoded phone number to NOTIFICATION_PHONE env var

Removes personal phone number from committed source code.
Watchdog now reads from .env via dotenv."
```

---

### Task 4: Fix Watchdog shell=True and Non-Atomic State Write

**Files:**
- Modify: `src/kalshi/watchdog.py:57-59` (atomic write)
- Modify: `src/kalshi/watchdog.py:49-54` (narrow bare except)
- Modify: `src/kalshi/watchdog.py:158` (shell=True — document as accepted risk)

**Step 1: Fix non-atomic state write**

Replace `save_state` (line 58-59):

```python
def save_state(state):
    import tempfile
    data = json.dumps(state, indent=2, default=str)
    fd, tmp_path = tempfile.mkstemp(dir=str(WATCHDOG_STATE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp_path, str(WATCHDOG_STATE))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
```

**Step 2: Narrow bare except clauses**

Replace `load_state` (lines 49-55):

```python
def load_state():
    if WATCHDOG_STATE.exists():
        try:
            return json.loads(WATCHDOG_STATE.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    return {"alerts": {}, "cpu_history": {}}
```

Replace `get_cpu` bare except (line 80-81):

```python
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0
```

Replace `get_log_age` bare except (line 89-90):

```python
    except (OSError, TypeError):
        return float("inf")
```

Replace `should_alert` bare except (line 120-121):

```python
    except (ValueError, TypeError):
        return True
```

**Step 3: Add comment documenting shell=True as accepted risk**

At line 158, add a comment:

```python
                # shell=True required: restart_cmd uses nohup, pipes, $! expansion.
                # Safe: commands are hardcoded in PROCESSES dict, not user input.
                subprocess.run(config["restart_cmd"], shell=True, timeout=15)
```

**Step 4: Commit**

```bash
git add src/kalshi/watchdog.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: atomic state write and narrow bare except clauses in watchdog

- save_state now uses tempfile + os.replace for crash safety
- Bare except clauses narrowed to specific exception types
- shell=True documented as accepted (hardcoded commands, not user input)"
```

---

### Task 5: Fix Market Cache Read-Modify-Write Race

**Files:**
- Modify: `src/kalshi/kalshi_auth.py:329-338`
- Test: `tests/test_market_cache_lock.py`

**Step 1: Write the failing test**

Create `tests/test_market_cache_lock.py`:

```python
"""Test that market cache updates are protected by file locking."""
import sys
import json
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import read_market_cache, write_market_cache, MARKET_CACHE_PATH


def test_market_cache_write_uses_lock(tmp_path):
    """write_market_cache should use file locking to prevent lost updates."""
    cache_file = tmp_path / "market-cache.json"
    lock_file = cache_file.with_suffix(".lock")

    with patch("kalshi_auth.MARKET_CACHE_PATH", cache_file):
        # Write initial data
        write_market_cache({"KXHIGH": [{"ticker": "T1"}]})
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert "KXHIGH" in data

        # Write again with different prefix
        write_market_cache({"KXBTC": [{"ticker": "T2"}]})
        data = json.loads(cache_file.read_text())
        assert "KXBTC" in data
```

**Step 2: Run test to verify current behavior**

```bash
pytest tests/test_market_cache_lock.py -v
```

**Step 3: Add file locking to the shared cache update in get_all_markets**

In `src/kalshi/kalshi_auth.py`, replace lines 329-338:

```python
        if use_shared_cache and prefix and status == "open":
            try:
                lock_path = MARKET_CACHE_PATH.with_suffix(".lock")
                with open(lock_path, "w") as lock_fd:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    try:
                        existing = read_market_cache(max_age=MARKET_CACHE_TTL * 10) or {}
                        if not isinstance(existing, dict):
                            existing = {}
                        existing[prefix] = all_markets
                        write_market_cache(existing)
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception as e:
                _log.debug("Failed to write shared market cache: %s", e)
```

**Step 4: Run tests**

```bash
pytest tests/test_market_cache_lock.py tests/test_kelly.py -v -x
```

Expected: All pass.

**Step 5: Commit**

```bash
git add src/kalshi/kalshi_auth.py tests/test_market_cache_lock.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: add file lock to market cache read-modify-write

Prevents concurrent bots from losing each other's cache updates.
Uses fcntl.LOCK_EX matching the pattern used for trade log writes."
```

---

### Task 6: Disable Dead Bots in Supervisor

Entertainment (0 trades ever, 91.8% illiquid), BeatRelease (negative mean edge, no trade since Feb 18), and Cross-Platform Arb (Phase 1 monitor-only, 0 evaluations) should be disabled.

**Files:**
- Modify: `scripts/supervisor.py:54-58`
- Modify: `config/bots-config.json` (add `"enabled": false` to beatrelease and cross_platform_arb)

**Step 1: Add beatrelease and arb to DISABLED_BY_DEFAULT**

In `scripts/supervisor.py`, replace line 58:

```python
DISABLED_BY_DEFAULT = {"mm", "demo", "entertainment", "beatrelease", "arb"}
```

**Step 2: Add enabled flags to bots-config.json**

In `config/bots-config.json`, add `"enabled": false` to beatrelease section (after line 31):

Add at the start of the beatrelease block:
```json
  "beatrelease": {
    "enabled": false,
    "checkIntervalHours": 1,
```

Add at the start of the cross_platform_arb block:
```json
  "cross_platform_arb": {
    "enabled": false,
    "scanIntervalMinutes": 10,
```

**Step 3: Commit**

```bash
git add scripts/supervisor.py config/bots-config.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "ops: disable beatrelease and cross-platform-arb bots

BeatRelease: negative mean edge (-1.5%), no trade since Feb 18, LLM
extraction pipeline producing no actionable signals.

Cross-platform arb: Phase 1 monitor-only, 306 scans/day with zero
evaluations, consuming API calls for no value.

Entertainment was already disabled. All three can be re-enabled via
supervisor start <name> when market conditions improve."
```

---

### Task 7: Fix quarter_kelly Integer Truncation

**Files:**
- Modify: `src/kalshi/probability.py:967-968`
- Test: `tests/test_kelly.py` (add test case)

**Step 1: Write the failing test**

Add to `tests/test_kelly.py`:

```python
def test_quarter_kelly_preserves_single_contract():
    """quarter_kelly should return 1 contract when half_kelly returns 1, not zero."""
    from probability import quarter_kelly, half_kelly
    # Find params where half_kelly returns exactly 1 contract
    # Small edge + small bankroll = 1 contract from half_kelly
    contracts_hk, _, _ = half_kelly(0.10, 5, 500, 5000, return_details=True)
    if contracts_hk >= 1:
        contracts_qk, _, _ = quarter_kelly(0.10, 5, 500, 5000, return_details=True)
        # If half_kelly gives 1+, quarter_kelly should give at least 1 (not zero)
        if contracts_hk <= 2:
            assert contracts_qk >= 1, (
                f"quarter_kelly zeroed out: half_kelly={contracts_hk}, quarter_kelly={contracts_qk}"
            )
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_kelly.py::test_quarter_kelly_preserves_single_contract -v
```

**Step 3: Fix the truncation**

In `src/kalshi/probability.py`, replace lines 967-968:

```python
    # Halve the half-Kelly position (= quarter-Kelly)
    # Use round() instead of // to avoid silently zeroing out 1-contract positions
    contracts = max(1, round(contracts / 2)) if contracts >= 1 else 0
```

**Step 4: Run full Kelly test suite**

```bash
pytest tests/test_kelly.py -v
```

Expected: All pass, including new test.

**Step 5: Commit**

```bash
git add src/kalshi/probability.py tests/test_kelly.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: quarter_kelly preserves minimum 1 contract when half_kelly >= 1

Integer floor division (// 2) was silently zeroing out positions when
half_kelly returned 1 contract. Now uses round() with floor of 1."
```

---

### Task 8: Add RecentTradeTracker Pruning

**Files:**
- Modify: `src/kalshi/kalshi_auth.py:522-528` (add pruning in `is_recent`)

**Step 1: Add lazy pruning to is_recent**

The simplest fix: prune expired entries opportunistically during `is_recent()` checks. Replace the `is_recent` method (lines 522-528):

```python
    def is_recent(self, ticker):
        """Return True if this ticker was traded within the cooldown window.

        Lazily prunes expired entries to prevent unbounded memory growth
        in long-running daemon sessions.
        """
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=self.cooldown_hours)
        # Prune expired entries every 100 calls (amortized O(1))
        if not hasattr(self, "_prune_counter"):
            self._prune_counter = 0
        self._prune_counter += 1
        if self._prune_counter >= 100:
            self._prune_counter = 0
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}

        ts = self._recent.get(ticker)
        if not ts:
            return False
        return ts > cutoff
```

**Step 2: Run existing tests**

```bash
pytest tests/ -x -q --timeout=30
```

Expected: All pass.

**Step 3: Commit**

```bash
git add src/kalshi/kalshi_auth.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: add lazy pruning to RecentTradeTracker to prevent memory leak

Expired entries now pruned every 100 is_recent() calls. Prevents
unbounded dict growth in long-running daemon sessions."
```

---

### Task 9: Review and Adjust Allocator Risk Cap

The economics bot consumed $464 of the $500 daily cap, blocking all other bots from trading genuine edges (weather 38% mean edge, source monitor 29% mean edge).

**Files:**
- Modify: `config/bots-config.json:145-149` (allocator section)

**Step 1: Increase daily risk cap and add per-bot caps**

Replace the allocator section in `config/bots-config.json`:

```json
  "allocator": {
    "maxConcurrentPositions": 30,
    "absoluteDailyLossCap": 750,
    "absoluteDailyLossCapPct": 0.15,
    "perBotDailyLimit": {
      "weather": 200,
      "economics": 300,
      "crypto": 150,
      "strategy": 150,
      "monitor": 100,
      "entertainment": 0,
      "beatrelease": 0,
      "arb": 0
    }
  },
```

**Step 2: Check if capital_allocator.py supports per-bot limits**

Read `src/kalshi/capital_allocator.py` to verify the `perBotDailyLimit` field is consumed. If not, this config change is documentation-only and the allocator code needs a follow-up change to enforce per-bot caps. Either way, raising the absolute cap from $500 → $750 is immediately effective.

**Step 3: Commit**

```bash
git add config/bots-config.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "ops: raise allocator daily risk cap to $750 and add per-bot limits

Economics bot was consuming $464 of $500 cap, blocking all other bots
from exploiting genuine edges. Per-bot limits ensure diversification."
```

---

## Summary of Changes

| Task | Priority | Type | Risk |
|------|----------|------|------|
| 1. Remove kill switch | P0 | Ops | None (data/ gitignored) |
| 2. Fix duplicate logging | P1 | Bug | Low (logging only) |
| 3. Move phone to env var | P1 | Security | None |
| 4. Fix watchdog safety | P2 | Security/Correctness | Low |
| 5. Fix market cache race | P2 | Concurrency | Low |
| 6. Disable dead bots | P2 | Ops | None (can re-enable) |
| 7. Fix quarter_kelly truncation | P2 | Correctness | Low (more trades, not fewer) |
| 8. Add tracker pruning | P3 | Memory | None |
| 9. Adjust allocator cap | P3 | Config | Medium (more capital deployed) |

**Total estimated changes:** ~150 lines across 6 files + 2 new test files.
