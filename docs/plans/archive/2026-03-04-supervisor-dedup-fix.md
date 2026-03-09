# Supervisor Process Deduplication Fix

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Prevent duplicate bot instances by adding process-name scanning, singleton locking, and robust orphan cleanup to the supervisor.

**Architecture:** Add `_find_bot_processes(cmd)` helper using `pgrep -f` to find PIDs by command pattern. Add `fcntl.flock()` singleton guard. Integrate process scanning into `start()`, `_adopt_or_kill_orphans()`, and `check_and_restart()`. All stdlib, macOS-compatible.

**Tech Stack:** Python 3 stdlib (`subprocess.check_output`, `fcntl.flock`, `pgrep`)

---

### Task 1: Add `_find_bot_processes()` helper + tests

**Files:**
- Modify: `scripts/supervisor.py` (add module-level helper after line 91)
- Test: `tests/test_supervisor.py`

**Step 1: Write failing tests for `_find_bot_processes()`**

Add to `tests/test_supervisor.py`:

```python
class TestFindBotProcesses:

    def test_finds_matching_pids(self):
        """Should return PIDs from pgrep output."""
        output = b"1234\n5678\n"
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [1234, 5678]

    def test_excludes_own_pid(self):
        """Should exclude the supervisor's own PID from results."""
        output = b"1234\n9999\n5678\n"
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [1234, 5678]

    def test_returns_empty_when_no_matches(self):
        """pgrep exits non-zero when no matches — should return empty list."""
        with patch.object(supervisor.subprocess, "check_output",
                          side_effect=subprocess.CalledProcessError(1, "pgrep")), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == []

    def test_handles_blank_lines(self):
        """Should handle trailing newlines and blank lines."""
        output = b"1234\n\n"
        with patch.object(supervisor.subprocess, "check_output", return_value=output), \
             patch.object(supervisor.os, "getpid", return_value=9999):
            pids = supervisor._find_bot_processes(["python3", "src/kalshi/weather-bot.py"])
        assert pids == [1234]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_supervisor.py::TestFindBotProcesses -v`
Expected: FAIL — `supervisor` module has no `_find_bot_processes`

**Step 3: Implement `_find_bot_processes()`**

Add after line 91 in `scripts/supervisor.py` (after `HEARTBEAT_GRACE_PERIOD`):

```python
def _find_bot_processes(cmd):
    """Find PIDs of running processes matching a bot command.

    Uses pgrep -f with the full command string. Excludes the current
    process (supervisor) to avoid false positives.

    Returns list of integer PIDs (may be empty).
    """
    pattern = " ".join(cmd)
    my_pid = os.getpid()
    try:
        output = subprocess.check_output(["pgrep", "-f", pattern])
        pids = []
        for line in output.decode().strip().split("\n"):
            line = line.strip()
            if line:
                pid = int(line)
                if pid != my_pid:
                    pids.append(pid)
        return pids
    except subprocess.CalledProcessError:
        return []
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_supervisor.py::TestFindBotProcesses -v`
Expected: 4 PASS

**Step 5: Commit**

```bash
git add scripts/supervisor.py tests/test_supervisor.py
git commit -m "feat(supervisor): add _find_bot_processes helper for pgrep-based process scanning"
```

---

### Task 2: Add singleton lock + tests

**Files:**
- Modify: `scripts/supervisor.py` (add `fcntl` import, lock in `Supervisor.__init__` or `run()`)
- Test: `tests/test_supervisor.py`

**Step 1: Write failing tests for singleton lock**

Add to `tests/test_supervisor.py`:

```python
class TestSingletonLock:

    def test_acquire_lock_succeeds(self, tmp_path):
        """First supervisor should acquire lock successfully."""
        lock_path = tmp_path / "supervisor.lock"
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0
        sup._lock_file = None
        with patch.object(supervisor, "PID_DIR", tmp_path):
            sup._acquire_lock()
        assert sup._lock_file is not None
        sup._release_lock()

    def test_acquire_lock_fails_when_held(self, tmp_path):
        """Second supervisor should fail to acquire lock."""
        import fcntl
        lock_path = tmp_path / "supervisor.lock"

        # Hold the lock from "another process"
        held = open(lock_path, "w")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0
        sup._lock_file = None
        with patch.object(supervisor, "PID_DIR", tmp_path):
            with pytest.raises(SystemExit):
                sup._acquire_lock()

        held.close()

    def test_release_lock(self, tmp_path):
        """Lock should be released cleanly."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {}
        sup._running = True
        sup._last_calibration_check = 0
        sup._lock_file = None
        with patch.object(supervisor, "PID_DIR", tmp_path):
            sup._acquire_lock()
            sup._release_lock()
        assert sup._lock_file is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_supervisor.py::TestSingletonLock -v`
Expected: FAIL — no `_acquire_lock` method

**Step 3: Implement singleton lock**

Add `import fcntl` to the imports at the top of `scripts/supervisor.py` (after `import signal`).

Add methods to `Supervisor` class:

```python
def _acquire_lock(self):
    """Acquire singleton lock. Exit if another supervisor is running."""
    import fcntl
    lock_path = PID_DIR / "supervisor.lock"
    self._lock_file = open(lock_path, "w")
    try:
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()
    except (IOError, OSError):
        log.error("Another supervisor is already running. Exiting.")
        self._lock_file.close()
        self._lock_file = None
        sys.exit(1)

def _release_lock(self):
    """Release singleton lock."""
    if self._lock_file:
        try:
            import fcntl
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
        except Exception:
            pass
        self._lock_file = None
```

Initialize `self._lock_file = None` in `__init__`.

Call `self._acquire_lock()` at the start of `run()` (before `_adopt_or_kill_orphans`).
Call `self._release_lock()` at the end of `run()` (after "Supervisor stopped." log).

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_supervisor.py::TestSingletonLock -v`
Expected: 3 PASS

**Step 5: Commit**

```bash
git add scripts/supervisor.py tests/test_supervisor.py
git commit -m "feat(supervisor): add singleton lock to prevent duplicate supervisor instances"
```

---

### Task 3: Kill orphans by pattern in `_adopt_or_kill_orphans()` + tests

**Files:**
- Modify: `scripts/supervisor.py` — rewrite `_adopt_or_kill_orphans()`
- Test: `tests/test_supervisor.py`

**Step 1: Write failing tests**

Add to `tests/test_supervisor.py`:

```python
class TestKillOrphansByPattern:

    def test_kills_orphan_found_by_pgrep(self):
        """Orphan processes found by pattern should be killed."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(supervisor, "_find_bot_processes", return_value=[1234, 5678]) as mock_find, \
             patch.object(supervisor.os, "kill") as mock_kill, \
             patch.object(supervisor.time, "sleep"):
            sup._adopt_or_kill_orphans()

        # Should have tried to kill both orphans
        kill_calls = [c for c in mock_kill.call_args_list if c[0][1] == signal.SIGTERM]
        assert len(kill_calls) == 2

    def test_cleans_pid_files(self):
        """All PID files should be cleaned up during orphan cleanup."""
        sup = Supervisor.__new__(Supervisor)
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])
        sup.bots = {"weather": bot}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(supervisor, "_find_bot_processes", return_value=[]), \
             patch.object(bot, "_remove_pid") as mock_remove:
            sup._adopt_or_kill_orphans()

        mock_remove.assert_called_once()

    def test_handles_already_dead_process(self):
        """Should not crash if orphan dies between pgrep and kill."""
        sup = Supervisor.__new__(Supervisor)
        sup.bots = {"weather": BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])}
        sup._running = True
        sup._last_calibration_check = 0

        with patch.object(supervisor, "_find_bot_processes", return_value=[1234]), \
             patch.object(supervisor.os, "kill", side_effect=ProcessLookupError), \
             patch.object(supervisor.time, "sleep"):
            sup._adopt_or_kill_orphans()  # should not raise
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_supervisor.py::TestKillOrphansByPattern -v`
Expected: FAIL

**Step 3: Rewrite `_adopt_or_kill_orphans()`**

Replace the existing `_adopt_or_kill_orphans` method in `Supervisor`:

```python
def _adopt_or_kill_orphans(self):
    """Kill any orphan bot processes and clean up PID files.

    Called on startup before start_bots(). Scans for ALL running
    processes matching each bot's command pattern and kills them.
    Fresh start is safer than adopting stale processes.
    """
    for name, bot in self.bots.items():
        # Kill any running processes matching this bot's command
        orphan_pids = _find_bot_processes(bot.cmd)
        for pid in orphan_pids:
            log.warning(f"  Killing orphan {name} (PID {pid})")
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
        # Brief wait for SIGTERM, then SIGKILL stragglers
        if orphan_pids:
            time.sleep(2)
            for pid in orphan_pids:
                try:
                    os.kill(pid, 0)  # check if still alive
                    log.warning(f"  Force-killing orphan {name} (PID {pid})")
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        # Clean up PID file regardless
        bot._remove_pid()
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_supervisor.py::TestKillOrphansByPattern -v`
Expected: 3 PASS

Also run old orphan tests (some will need updating since behavior changed):
Run: `pytest tests/test_supervisor.py::TestOrphanAdoption -v`

**Step 5: Update old orphan adoption tests**

The old `TestOrphanAdoption` tests assumed adoption behavior. Update them to match the new kill-all-orphans behavior:

- `test_orphan_adoption_restores_state` — remove or update (no longer adopts)
- `test_orphan_adoption_fallback_to_current_time` — remove (no longer adopts)
- `test_orphan_dead_process_cleans_pid` — keep (still cleans PID files)
- `test_orphan_no_pid_file_skipped` — update (PID files are always cleaned now, but pgrep scan still runs)

**Step 6: Run full test suite**

Run: `pytest tests/test_supervisor.py -v`
Expected: All pass

**Step 7: Commit**

```bash
git add scripts/supervisor.py tests/test_supervisor.py
git commit -m "feat(supervisor): kill orphan processes by pgrep pattern on startup"
```

---

### Task 4: Pre-start dedup in `BotProcess.start()` + tests

**Files:**
- Modify: `scripts/supervisor.py` — update `BotProcess.start()`
- Test: `tests/test_supervisor.py`

**Step 1: Write failing test**

Add to `tests/test_supervisor.py`:

```python
class TestStartKillsExisting:

    def test_start_kills_existing_process_before_launch(self):
        """start() should kill any existing process matching this bot before launching."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])

        popen_mock = MagicMock()
        popen_mock.pid = 9999

        with patch.object(supervisor, "_find_bot_processes", return_value=[1234]) as mock_find, \
             patch.object(supervisor.os, "kill") as mock_kill, \
             patch.object(supervisor.time, "sleep"), \
             patch.object(supervisor.subprocess, "Popen", return_value=popen_mock), \
             patch.object(bot, "_write_pid"), \
             patch("builtins.open", MagicMock()):
            bot.start()

        # Should have killed the existing process
        mock_kill.assert_any_call(1234, signal.SIGTERM)

    def test_start_proceeds_when_no_existing_process(self):
        """start() should work normally when no existing process found."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])

        popen_mock = MagicMock()
        popen_mock.pid = 9999

        with patch.object(supervisor, "_find_bot_processes", return_value=[]), \
             patch.object(supervisor.subprocess, "Popen", return_value=popen_mock), \
             patch.object(bot, "_write_pid"), \
             patch("builtins.open", MagicMock()):
            result = bot.start()

        assert result is True
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_supervisor.py::TestStartKillsExisting -v`
Expected: FAIL (start() doesn't call `_find_bot_processes` yet)

**Step 3: Update `BotProcess.start()`**

Replace the beginning of `start()` method:

```python
def start(self):
    """Start the bot process, killing any existing instances first."""
    # Kill any existing processes matching this bot's command
    existing = _find_bot_processes(self.cmd)
    for pid in existing:
        log.warning(f"  Killing existing {self.name} (PID {pid}) before start")
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if existing:
        time.sleep(1)
        for pid in existing:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    log.info(f"  Starting {self.name}...")
    # ... rest of method unchanged from the try: block onward
```

Remove the old `is_running()` guard at the top — the pgrep scan supersedes it.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_supervisor.py::TestStartKillsExisting -v`
Expected: 2 PASS

**Step 5: Commit**

```bash
git add scripts/supervisor.py tests/test_supervisor.py
git commit -m "feat(supervisor): kill existing processes by pattern before starting bot"
```

---

### Task 5: Guard `check_and_restart()` against PID races + tests

**Files:**
- Modify: `scripts/supervisor.py` — update `check_and_restart()`
- Test: `tests/test_supervisor.py`

**Step 1: Write failing test**

Add to `tests/test_supervisor.py`:

```python
class TestCheckAndRestartDedup:

    def test_skips_restart_if_process_already_running_by_pattern(self):
        """If pgrep finds the bot running, don't restart even if PID file is stale."""
        bot = BotProcess("weather", ["python3", "src/kalshi/weather-bot.py"])
        bot.recent_crashes = []

        with patch.object(bot, "is_running", return_value=False), \
             patch.object(supervisor, "_find_bot_processes", return_value=[1234]), \
             patch.object(bot, "start") as mock_start:
            result = bot.check_and_restart()

        # Should NOT have called start since process is already running
        mock_start.assert_not_called()
        assert result is False
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_supervisor.py::TestCheckAndRestartDedup -v`
Expected: FAIL

**Step 3: Update `check_and_restart()`**

In the `check_and_restart` method, after determining `needs_restart = True` from `not self.is_running()`, add a pgrep check before proceeding to restart:

```python
if not self.is_running():
    # Double-check: maybe process is running but PID file is stale
    if _find_bot_processes(self.cmd):
        log.info(f"  {self.name} PID file stale but process found by pgrep, skipping restart")
        needs_restart = False
    else:
        needs_restart = True
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_supervisor.py::TestCheckAndRestartDedup -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `pytest tests/test_supervisor.py -v`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/supervisor.py tests/test_supervisor.py
git commit -m "feat(supervisor): guard check_and_restart against PID file races with pgrep"
```

---

### Task 6: Final integration test + full suite verification

**Files:**
- Test: `tests/test_supervisor.py`

**Step 1: Run full test suite**

Run: `pytest tests/test_supervisor.py -v`
Expected: All pass

**Step 2: Run broader test suite to check for regressions**

Run: `pytest tests/ -v`
Expected: All pass

**Step 3: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "test(supervisor): complete process deduplication test coverage"
```
