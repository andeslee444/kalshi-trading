# Supervisor Process Deduplication Fix

## Problem

The supervisor (`scripts/supervisor.py`) relies solely on PID files (`data/pids/<name>.pid`) to track bot processes. This causes duplicate bot instances in several scenarios:

1. **Orphaned processes after supervisor restart**: Bots are started with `start_new_session=True` (line 182), so they survive supervisor restarts as orphans under PID 1. The new supervisor writes a new PID file, losing track of the old process.

2. **No process-name-based scan**: `_adopt_or_kill_orphans()` (line 362) only checks existing PID files. It never scans for running processes by command pattern (e.g., `pgrep -f weather-bot.py`), so orphans without PID files are invisible.

3. **Crash loop races**: When bots crash instantly (e.g., import errors), the supervisor rapidly cycles start → crash → restart. PID files get overwritten before old processes are fully reaped, leaving zombies.

4. **No supervisor singleton guard**: Nothing prevents two `npm run supervisor` instances from running simultaneously, each spawning its own set of bots.

## Evidence

In production we observed:
- 5 instances of `source-monitor.py` running simultaneously
- 4 instances of `entertainment-bot.py`
- 3 instances of `cross-platform-arb.py`, `hdd-scraper.py`
- 2 instances of most other bots
- All orphans had PPID=1 (reparented after supervisor restart)
- Restart counts in the 650-670 range from a scipy import crash loop

## Requirements

### 1. Process-name scan before start
Before starting any bot, scan for existing processes matching the bot's command (e.g., `pgrep -f "python3 src/kalshi/weather-bot.py"` or equivalent using `psutil`/`os`). Kill any found orphans before launching a new instance. This should happen in:
- `BotProcess.start()` — before launching subprocess
- `Supervisor._adopt_or_kill_orphans()` — on supervisor startup

### 2. Supervisor singleton lock
Ensure only one supervisor instance runs at a time. Use a lockfile (`data/pids/supervisor.lock`) with `fcntl.flock()` or similar. If another supervisor is already running, exit with a clear error message.

### 3. Robust orphan cleanup on startup
Enhance `_adopt_or_kill_orphans()` to:
- Scan for ALL running processes matching any bot command pattern
- Kill duplicates (keep none — fresh start is safer than adopting stale processes)
- Clean up all PID files before starting fresh

### 4. Guard against crash loop PID races
In `check_and_restart()`, before calling `start()`, verify no process matching the bot's command is already running. The crash rate limiter (MAX_CRASHES=5 in CRASH_WINDOW=600s) already exists but doesn't prevent PID file races within the window.

## Architecture Constraints

- Keep changes within `scripts/supervisor.py` — don't modify bot code
- Use only stdlib (no `psutil`) since the project doesn't use it. Use `subprocess.check_output(["pgrep", "-f", pattern])` or parse `/proc` / `ps aux`
- macOS compatibility required (Darwin) — `pgrep -f` works on macOS
- The `pgrep` pattern must be specific enough to not match the supervisor itself or grep processes
- Bots are launched with `start_new_session=True` — they form their own process groups
- PID files remain the primary tracking mechanism; process scanning is a safety net

## Testing

Existing tests are in `tests/test_supervisor.py`. Add tests for:
- `_kill_orphans_by_pattern()` or equivalent helper
- Singleton lock acquisition and conflict detection
- `start()` killing existing process before launching new one
- No false positives (supervisor doesn't kill itself, grep doesn't match itself)

Run tests with: `pytest tests/test_supervisor.py -v`
