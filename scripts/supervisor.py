#!/usr/bin/env python3
"""Process supervisor for Kalshi trading bots.

Manages bot lifecycles: start/stop/restart individual or all bots,
auto-restart crashed daemons with rate limiting, unified status view.

Usage:
    python3 scripts/supervisor.py start                  # Start all enabled bots
    python3 scripts/supervisor.py start weather crypto    # Start specific bots
    python3 scripts/supervisor.py stop                    # Stop all
    python3 scripts/supervisor.py stop weather            # Stop specific
    python3 scripts/supervisor.py restart crypto          # Restart specific
    python3 scripts/supervisor.py status                  # Status table
    python3 scripts/supervisor.py run                     # Start all + monitor (foreground)
"""

import argparse
import datetime
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from bot_registry import (
    ALWAYS_DISABLED_BOT_IDS,
    BOT_COMMANDS as REGISTRY_BOT_COMMANDS,
    BOT_CONFIG_KEY_MAP,
    BOT_DEFAULT_SCAN_INTERVAL_MAP,
    BOT_HEALTH_KEY_MAP,
    BOT_LOG_NAME_MAP,
    DAEMON_BOT_IDS,
    ONESHOT_BOT_IDS,
)
from kalshi_auth import setup_logging, check_kill_switch, notify_webhook, per_bot_halt_path

log = setup_logging("supervisor")
PROJECT_DIR = Path(__file__).resolve().parent.parent
PID_DIR = PROJECT_DIR / "data" / "pids"
PID_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = PROJECT_DIR / "data" / "logs"
HEALTH_STATE_PATH = PROJECT_DIR / "data" / "health-state.json"
SUPERVISOR_STATE_PATH = PID_DIR / "supervisor-state.json"
WEATHER_VERIFICATION_PATH = PROJECT_DIR / "data" / "weather-verification.json"

BOT_COMMANDS = {name: list(cmd) for name, cmd in REGISTRY_BOT_COMMANDS.items()}

DAEMON_BOTS = set(DAEMON_BOT_IDS)
ONESHOT_BOTS = set(ONESHOT_BOT_IDS)

# Bots that are always disabled (experimental, unsafe, or utility one-shots)
_ALWAYS_DISABLED = set(ALWAYS_DISABLED_BOT_IDS)
CONFIG_KEY = {name: key for name, key in BOT_CONFIG_KEY_MAP.items() if key != name}

def _load_disabled_bots():
    """Read bots-config.json to determine which bots are disabled.

    Bots with "enabled": false are disabled. Bots without an "enabled" field
    are assumed enabled (core bots like weather/crypto don't have one).
    Falls back to a safe default set if config is unreadable.
    """
    config_path = PROJECT_DIR / "config" / "bots-config.json"
    fallback = set(_ALWAYS_DISABLED) | {"entertainment", "beatrelease", "arb", "strategy", "hdd-monitor"}
    try:
        with open(config_path) as f:
            config = json.load(f)
        disabled = set(_ALWAYS_DISABLED)
        for name in BOT_COMMANDS:
            cfg_key = CONFIG_KEY.get(name, name)
            bot_cfg = config.get(cfg_key, {})
            if isinstance(bot_cfg, dict) and "enabled" in bot_cfg and not bot_cfg["enabled"]:
                disabled.add(name)
        return disabled
    except Exception as e:
        log.warning(f"Could not read bots-config.json: {e} — using fallback disabled set")
        return fallback

DISABLED_BY_DEFAULT = _load_disabled_bots()

# Crash rate limiting
MAX_CRASHES = 5
CRASH_WINDOW = 600  # 10 minutes
RESTART_ALERT_THRESHOLD = 3

CHECK_INTERVAL = 30  # seconds

# Heartbeat staleness detection
HEARTBEAT_NAMES = {
    name: key for name, key in BOT_HEALTH_KEY_MAP.items() if name in BOT_COMMANDS
}

BOT_SCAN_INTERVALS = {
    name: minutes
    for name, minutes in BOT_DEFAULT_SCAN_INTERVAL_MAP.items()
    if name in BOT_COMMANDS
}

HEARTBEAT_GRACE_PERIOD = 300  # 5 min startup grace before checking heartbeats

BOT_LOG_NAMES = {
    name: log_name
    for name, log_name in BOT_LOG_NAME_MAP.items()
    if name in BOT_COMMANDS
}


def _find_bot_processes(cmd):
    """Find PIDs of running processes matching a bot command.

    For Python bots, matches on the script path rather than the literal
    interpreter string so it still finds workers launched as
    `/path/to/Python [-u] script.py`. Excludes the current process
    (supervisor) to avoid false positives.

    Returns list of integer PIDs (may be empty).
    """
    script_index = next((idx for idx, part in enumerate(cmd) if part.endswith(".py")), None)
    script_path = cmd[script_index] if script_index is not None else None
    script_args = cmd[script_index + 1:] if script_index is not None else []
    pattern = script_path or " ".join(cmd)
    my_pid = os.getpid()
    try:
        output = subprocess.check_output(["pgrep", "-fl", pattern], text=True)
        pids = []
        for line in output.strip().split("\n"):
            line = line.strip()
            if line:
                pid_str, _, command = line.partition(" ")
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)
                if pid != my_pid:
                    if script_path:
                        parts = command.split()
                        if not parts:
                            continue
                        exe_name = Path(parts[0]).name.lower()
                        if "python" not in exe_name:
                            continue
                        try:
                            path_index = parts.index(script_path, 1)
                        except ValueError:
                            continue
                        trailing_args = parts[path_index + 1:]
                        if script_args:
                            if trailing_args[:len(script_args)] != script_args:
                                continue
                        elif trailing_args:
                            continue
                    pids.append(pid)
        return pids
    except subprocess.CalledProcessError:
        return []


def _parse_iso_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return None


def _load_weather_actual_source_summary(lookback_days=30):
    """Load recent weather verification actual-source mix from disk."""
    if not WEATHER_VERIFICATION_PATH.exists():
        return None
    try:
        data = json.loads(WEATHER_VERIFICATION_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
    counts = {}
    total = 0
    for record in data.get("verified", []):
        record_date = _parse_iso_date(record.get("date"))
        if record_date is None or record_date < cutoff:
            continue
        source = record.get("actual_source") or "missing"
        counts[source] = counts.get(source, 0) + 1
        total += 1

    if total == 0:
        return {
            "lookback_days": lookback_days,
            "total": 0,
            "counts": {},
            "shares": {},
        }

    shares = {
        source: round(count / total, 3)
        for source, count in sorted(counts.items())
    }
    return {
        "lookback_days": lookback_days,
        "total": total,
        "counts": dict(sorted(counts.items())),
        "shares": shares,
    }


def _format_actual_source_summary(summary):
    """Human-readable weather actual-source mix."""
    if not summary or summary.get("total", 0) == 0:
        return None

    parts = []
    for source, count in summary.get("counts", {}).items():
        share = summary.get("shares", {}).get(source, 0.0)
        parts.append(f"{source}={count} ({share:.1%})")

    return (
        f"Weather actuals ({summary['lookback_days']}d): "
        f"total={summary['total']} | " + ", ".join(parts)
    )


def _bot_log_paths(name):
    """Possible log files for a bot (supervisor name + logger name)."""
    basenames = [name]
    mapped = BOT_LOG_NAMES.get(name)
    if mapped and mapped not in basenames:
        basenames.append(mapped)
    return [LOG_DIR / f"{base}.log" for base in basenames]


def _log_contains_duplicate_block(name, max_lines=50):
    """Check recent bot logs for singleton-guard duplicate-block messages."""
    for path in _bot_log_paths(name):
        if not path.exists():
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()[-max_lines:]
        except OSError:
            continue
        if any("Duplicate " in line and "launch blocked; exiting." in line for line in lines):
            return True
    return False


class BotProcess:
    """Tracks a single bot's process state."""

    def __init__(self, name, cmd):
        self.name = name
        self.cmd = cmd
        self.pid_file = PID_DIR / f"{name}.pid"
        self.process = None
        self.started_at = None
        self.restart_count = 0
        self.recent_crashes = []  # timestamps
        self.last_restart_alert_at = None
        self.per_bot_halted = False

    def is_heartbeat_stale(self, health_data):
        """Check if bot's heartbeat is stale (indicates hung process).

        Returns (is_stale, age_minutes) tuple.
        Stale = age > 3x expected scan interval.
        Returns (False, None) during grace period, for unknown bots, or if
        the bot has no heartbeat mapping.
        """
        if self.name not in HEARTBEAT_NAMES:
            return False, None

        # Grace period: don't check heartbeat within first 5 minutes of start
        if self.started_at is not None and (time.time() - self.started_at) < HEARTBEAT_GRACE_PERIOD:
            return False, None

        expected_interval = BOT_SCAN_INTERVALS.get(self.name, 30)
        stale_threshold_min = expected_interval * 3

        heartbeat_name = HEARTBEAT_NAMES[self.name]
        bot_health = health_data.get("bots", {}).get(heartbeat_name, {})
        hb = bot_health.get("last_heartbeat")

        if not hb:
            # No heartbeat recorded — stale if running > 2x interval
            if self.started_at is not None:
                running_min = (time.time() - self.started_at) / 60
                if running_min > expected_interval * 2:
                    return True, running_min
            return False, None

        try:
            from datetime import datetime
            hb_dt = datetime.fromisoformat(hb)
            if hb_dt.tzinfo is not None:
                now_dt = datetime.now(tz=hb_dt.tzinfo)
            else:
                now_dt = datetime.now()
            age_min = (now_dt - hb_dt).total_seconds() / 60

            # If heartbeat is old but bot was recently (re)started, it hasn't
            # had a chance to complete a scan yet — don't kill it prematurely
            if self.started_at is not None:
                running_min = (time.time() - self.started_at) / 60
                if running_min < stale_threshold_min:
                    return False, None

            if age_min > stale_threshold_min:
                return True, age_min
        except (ValueError, TypeError):
            pass

        return False, None

    def is_running(self):
        """Check if bot is running via PID file + os.kill probe."""
        if self.process is not None:
            ret = self.process.poll()
            if ret is not None:
                pid = self._read_pid()
                if pid is not None:
                    self._remove_pid_if_matches(pid)
                self.process = None
                return False

        pid = self._read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            # Some environments can observe a PID file and fresh heartbeats but
            # still be denied permission to probe the process directly.
            # Treat that as "likely running" instead of deleting the PID file.
            return True
        except ProcessLookupError:
            # Stale PID file
            self._remove_pid_if_matches(pid)
            return False

    def adopt_running_pid(self):
        """Adopt a single running process when the PID file is missing/stale."""
        if self._read_pid() is not None:
            return None
        pids = _find_bot_processes(self.cmd)
        if len(pids) != 1:
            return None
        pid = pids[0]
        self._write_pid(pid)
        if self.started_at is None:
            self.started_at = time.time()
        log.info(f"  {self.name} adopted running PID {pid} from pgrep")
        return pid

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
        try:
            log_dir = PROJECT_DIR / "data" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_file = open(log_dir / f"{self.name}.log", "a")

            try:
                self.process = subprocess.Popen(
                    self.cmd,
                    cwd=str(PROJECT_DIR),
                    stdout=stdout_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                stdout_file.close()  # child inherited the fd; close parent's copy
            self._write_pid(self.process.pid)
            self.started_at = time.time()
            log.info(f"  {self.name} started (PID {self.process.pid})")
            return True
        except Exception as e:
            log.error(f"  Failed to start {self.name}: {e}")
            return False

    def stop(self):
        """Stop the bot process (SIGUSR1 → SIGTERM → SIGKILL after 30s)."""
        pid = self._read_pid()
        if pid is None:
            return

        log.info(f"  Stopping {self.name} (PID {pid})...")

        # Send SIGUSR1 first to signal "finish current cycle, don't start new one"
        try:
            os.kill(pid, signal.SIGUSR1)
            time.sleep(1)  # Give bot a moment to set shutdown flag
        except (ProcessLookupError, PermissionError):
            pass

        try:
            # Kill entire process group (bots use start_new_session=True)
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                self._remove_pid()
                return

        # Wait up to 30s for graceful shutdown (bots may be mid-API-call)
        for _ in range(60):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except ProcessLookupError:
                break
        else:
            # Still running — force kill process group
            try:
                log.warning(f"  {self.name} didn't stop, sending SIGKILL")
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self._remove_pid_if_matches(pid)
        self.process = None
        log.info(f"  {self.name} stopped")

    def check_oneshot_completion(self):
        """Check if one-shot bot wrote a completion/error marker."""
        marker_path = PROJECT_DIR / "data" / f"{self.name}-last-run.json"
        if not marker_path.exists():
            return None
        try:
            data = json.loads(marker_path.read_text())
            status = data.get("status")
            if status == "error":
                notify_webhook(f"One-shot bot {self.name} failed: {data.get('error', '?')}", level="warning")
            return status
        except Exception:
            return None

    def handle_singleton_blocked_start(self):
        """Adopt an existing worker when this launch was rejected by a bot singleton."""
        if not _log_contains_duplicate_block(self.name):
            return False

        existing = _find_bot_processes(self.cmd)
        if not existing:
            return False

        if self.process is not None:
            self._remove_pid_if_matches(self.process.pid)
            self.process = None

        adopted_pid = self.adopt_running_pid()
        if adopted_pid is None and len(existing) == 1:
            adopted_pid = existing[0]
            self._write_pid(adopted_pid)
            if self.started_at is None:
                self.started_at = time.time()

        live_pids = ", ".join(str(pid) for pid in existing)
        log.warning(f"  {self.name} duplicate launch blocked by bot singleton; live PID(s): {live_pids}")
        return True

    def check_and_restart(self, health_data=None):
        """Check if daemon crashed or hung and auto-restart with rate limiting.

        Detection strategy:
        - First 60s after start: aggressively verify PID is alive (catches import
          errors, missing config, etc. without waiting for heartbeat grace period)
        - After grace period: also check heartbeat staleness for hung processes

        Args:
            health_data: Optional health-state.json dict for heartbeat checks.

        Returns True if restarted, False otherwise.
        """
        if self.name not in DAEMON_BOTS:
            # Check one-shot bot completion markers
            if self.name in ONESHOT_BOTS:
                self.check_oneshot_completion()
            return False

        needs_restart = False

        if not self.is_running():
            # Double-check: maybe process is running but PID file is stale/missing
            adopted_pid = self.adopt_running_pid()
            if adopted_pid is not None:
                needs_restart = False
            elif _find_bot_processes(self.cmd):
                log.info(f"  {self.name} PID file stale but process found by pgrep, skipping restart")
                needs_restart = False
            else:
                needs_restart = True
        elif self.started_at and (time.time() - self.started_at) < 60:
            # Early life check: verify the process is still alive via waitpid
            # (catches bots that crash right after start, before heartbeat grace)
            if self.process is not None:
                ret = self.process.poll()
                if ret is not None:
                    if self.handle_singleton_blocked_start():
                        return False
                    log.warning(f"  {self.name} exited early (code {ret}) — check data/logs/{self.name}.log")
                    self._remove_pid_if_matches(self.process.pid)
                    needs_restart = True
        elif health_data is not None:
            is_stale, age_min = self.is_heartbeat_stale(health_data)
            if is_stale:
                log.warning(f"  {self.name} heartbeat stale ({age_min:.0f}min), killing zombie")
                notify_webhook(f"Bot {self.name} heartbeat stale ({age_min:.0f}min) — killing zombie", level="warning")
                self.stop()
                needs_restart = True

        if not needs_restart:
            return False

        # Check crash rate
        now = time.time()
        self.recent_crashes = [t for t in self.recent_crashes if now - t < CRASH_WINDOW]

        if len(self.recent_crashes) >= MAX_CRASHES:
            log.error(f"  {self.name}: {MAX_CRASHES} crashes in {CRASH_WINDOW}s, not restarting")
            notify_webhook(f"Bot {self.name} crashed {MAX_CRASHES}x in {CRASH_WINDOW}s — auto-restart disabled", level="critical")
            return False

        self.recent_crashes.append(now)
        self.restart_count += 1
        log.warning(f"  {self.name} down, restarting (attempt #{self.restart_count})")
        if len(self.recent_crashes) >= RESTART_ALERT_THRESHOLD:
            should_alert = (
                self.last_restart_alert_at is None or
                (now - self.last_restart_alert_at) >= CRASH_WINDOW
            )
            if should_alert:
                self.last_restart_alert_at = now
                msg = (
                    f"Bot {self.name} restarted {len(self.recent_crashes)}x "
                    f"in {CRASH_WINDOW}s — investigate stability"
                )
                log.warning(f"  {msg}")
                notify_webhook(msg, level="warning")
        return self.start()

    def uptime_str(self):
        """Human-readable uptime."""
        pid = self._read_pid()
        if pid is None or not self.is_running():
            return "-"
        if self.started_at is None:
            return "?"
        elapsed = time.time() - self.started_at
        if elapsed < 60:
            return f"{elapsed:.0f}s"
        if elapsed < 3600:
            return f"{elapsed/60:.0f}m"
        return f"{elapsed/3600:.1f}h"

    def _read_pid(self):
        if not self.pid_file.exists():
            return None
        try:
            return int(self.pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def _write_pid(self, pid):
        self.pid_file.write_text(str(pid))

    def _remove_pid(self):
        try:
            self.pid_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _remove_pid_if_matches(self, pid):
        current = self._read_pid()
        if current == pid:
            self._remove_pid()


class Supervisor:
    """Manages all bot processes."""

    def __init__(self):
        self.bots = {
            name: BotProcess(name, cmd)
            for name, cmd in BOT_COMMANDS.items()
        }
        self._running = True
        self._last_calibration_check = 0
        self._lock_file = None
        self._load_state()

    def _load_state(self):
        """Restore started_at and restart_count for running bots from state file."""
        if not SUPERVISOR_STATE_PATH.exists():
            return
        try:
            state = json.loads(SUPERVISOR_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for name, bot in self.bots.items():
            bot_state = state.get(name)
            if bot_state and bot.is_running():
                bot.started_at = bot_state.get("started_at")
                bot.restart_count = bot_state.get("restart_count", 0)

    def _save_state(self):
        """Persist started_at and restart_count for all bots to state file."""
        state = {}
        for name, bot in self.bots.items():
            if bot.started_at is not None:
                state[name] = {
                    "started_at": bot.started_at,
                    "restart_count": bot.restart_count,
                }
        try:
            tmp = SUPERVISOR_STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(SUPERVISOR_STATE_PATH)
        except OSError as e:
            log.warning(f"Failed to save supervisor state: {e}")

    def _acquire_lock(self):
        """Acquire singleton lock. Exit if another supervisor is running.

        If the lock is held but the owning process is dead (stale lock from
        a crashed supervisor), we steal the lock instead of refusing to start.
        """
        lock_path = PID_DIR / "supervisor.lock"
        self._lock_file = open(lock_path, "w+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file.write(str(os.getpid()))
            self._lock_file.flush()
        except (IOError, OSError):
            # Lock is held — check if the holder is still alive
            stale = False
            old_pid = None
            try:
                self._lock_file.seek(0)
                content = self._lock_file.read().strip()
                if content:
                    old_pid = int(content)
                    os.kill(old_pid, 0)  # raises if process is dead
                else:
                    stale = True  # empty lock file = stale
            except (ValueError, ProcessLookupError):
                stale = True
            except PermissionError:
                pass  # process alive but owned by another user — not stale

            if stale:
                log.warning(f"Stale supervisor lock (PID {old_pid or '?'} is dead). Stealing lock.")
                # Close and reopen to get a fresh file descriptor
                self._lock_file.close()
                self._lock_file = open(lock_path, "w")
                try:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._lock_file.write(str(os.getpid()))
                    self._lock_file.flush()
                except (IOError, OSError):
                    # Lock is actually held (edge case: empty PID file but live holder)
                    log.error("Could not steal lock — another supervisor is still running. Exiting.")
                    self._lock_file.close()
                    self._lock_file = None
                    sys.exit(1)
            else:
                log.error("Another supervisor is already running. Exiting.")
                self._lock_file.close()
                self._lock_file = None
                sys.exit(1)

    def _release_lock(self):
        """Release singleton lock and remove lock file."""
        if self._lock_file:
            try:
                lock_path = PID_DIR / "supervisor.lock"
                lock_path.unlink(missing_ok=True)
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            self._lock_file = None

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

    def _resolve_names(self, names=None):
        """Resolve bot names, defaulting to all enabled if none specified."""
        if names:
            unknown = set(names) - set(self.bots)
            if unknown:
                log.error(f"Unknown bots: {', '.join(sorted(unknown))}")
                log.info(f"Available: {', '.join(sorted(self.bots))}")
                return []
            return list(names)
        return [n for n in self.bots if n not in DISABLED_BY_DEFAULT]

    def start_bots(self, names=None):
        """Start specified bots (or all enabled), with post-start verification."""
        targets = self._resolve_names(names)
        if not targets:
            return
        log.info(f"Starting {len(targets)} bot(s)...")
        for name in targets:
            self.bots[name].start()

        # Post-start verification: wait briefly, then check each bot is still alive.
        # Catches immediate crashes (import errors, config issues, missing deps).
        time.sleep(3)
        failed = []
        for name in targets:
            bot = self.bots[name]
            if not bot.is_running():
                if name in ONESHOT_BOTS and bot.check_oneshot_completion() == "ok":
                    continue
                if bot.handle_singleton_blocked_start():
                    continue
                failed.append(name)
                log.error(f"  {name} died immediately after start — check data/logs/{name}.log")
        if failed:
            notify_webhook(f"Bots died on startup: {', '.join(failed)}", level="critical")

        self._save_state()

    def stop_bots(self, names=None):
        """Stop specified bots (or all)."""
        targets = list(names) if names else list(self.bots.keys())
        log.info(f"Stopping {len(targets)} bot(s)...")
        for name in targets:
            if name in self.bots:
                self.bots[name].stop()

    def restart_bots(self, names):
        """Restart specified bots."""
        if not names:
            log.error("Specify bot name(s) to restart.")
            return
        for name in names:
            if name in self.bots:
                self.bots[name].stop()
                self.bots[name].start()
            else:
                log.error(f"Unknown bot: {name}")
        self._save_state()

    def status(self):
        """Print status table for all bots."""
        health = self._load_health()
        weather_summary_7d = _load_weather_actual_source_summary(lookback_days=7)
        weather_summary_30d = _load_weather_actual_source_summary(lookback_days=30)

        print(f"\n{'Bot':<16} {'Status':<10} {'PID':<8} {'Uptime':<8} {'Restarts':<10} {'Last Heartbeat'}")
        print("-" * 75)

        for name in sorted(self.bots):
            bot = self.bots[name]
            pid = bot._read_pid()
            running = bot.is_running()
            discovered_pids = []
            if not running:
                discovered_pids = _find_bot_processes(bot.cmd)
                if discovered_pids:
                    running = True
                    if pid is None and len(discovered_pids) == 1:
                        pid = discovered_pids[0]

            if name in DISABLED_BY_DEFAULT and not running:
                status = "disabled"
            elif running:
                status = "running"
            else:
                status = "stopped"

            pid_str = str(pid) if pid and running else "-"
            uptime = bot.uptime_str()
            restarts = str(bot.restart_count) if bot.restart_count else "-"

            # Last heartbeat from health-state.json (use HEARTBEAT_NAMES mapping)
            hb_name = HEARTBEAT_NAMES.get(name, name)
            bot_health = health.get("bots", {}).get(hb_name, {})
            heartbeat = bot_health.get("last_heartbeat", "-")
            if heartbeat != "-" and len(heartbeat) > 19:
                heartbeat = heartbeat[:19]  # trim timezone

            # Flag stale heartbeats
            is_stale, _ = bot.is_heartbeat_stale(health)
            if is_stale:
                heartbeat = f"{heartbeat} STALE"

            print(f"{name:<16} {status:<10} {pid_str:<8} {uptime:<8} {restarts:<10} {heartbeat}")

        for summary in (weather_summary_7d, weather_summary_30d):
            line = _format_actual_source_summary(summary)
            if line:
                print(line)

        print()

    def run(self):
        """Start all bots and enter foreground monitor loop."""
        # Signal handling for graceful shutdown
        def _shutdown(sig, frame):
            log.info(f"\nReceived signal {sig}, shutting down...")
            self._running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        def _reload(sig, frame):
            log.info("Received SIGHUP — scheduling bot restart cycle")
            self._reload_requested = True

        signal.signal(signal.SIGHUP, _reload)

        self._reload_requested = False
        self._acquire_lock()
        self._adopt_or_kill_orphans()
        self.start_bots()
        log.info(f"Supervisor running (checking every {CHECK_INTERVAL}s, Ctrl+C to stop)")

        kill_switch_active = False

        while self._running:
            time.sleep(CHECK_INTERVAL)
            if not self._running:
                break

            # Kill switch integration
            try:
                halted = check_kill_switch()
            except SystemExit:
                halted = True

            if halted and not kill_switch_active:
                log.warning("Kill switch active — stopping all bots")
                self.stop_bots()
                kill_switch_active = True
                continue

            if not halted and kill_switch_active:
                log.info("Kill switch removed — restarting bots")
                kill_switch_active = False
                self.start_bots()
                continue

            if kill_switch_active:
                continue

            # SIGHUP reload — re-read config and restart all bots with new code
            if self._reload_requested:
                global DISABLED_BY_DEFAULT
                old_disabled = DISABLED_BY_DEFAULT
                DISABLED_BY_DEFAULT = _load_disabled_bots()
                newly_enabled = old_disabled - DISABLED_BY_DEFAULT
                newly_disabled = DISABLED_BY_DEFAULT - old_disabled
                if newly_enabled:
                    log.info(f"Config reload: newly enabled bots: {', '.join(sorted(newly_enabled))}")
                if newly_disabled:
                    log.info(f"Config reload: newly disabled bots: {', '.join(sorted(newly_disabled))}")
                log.info("Reloading: stopping all bots...")
                self.stop_bots()
                log.info("Reloading: starting all bots with new code...")
                self.start_bots()
                self._reload_requested = False
                notify_webhook("Supervisor reload complete — all bots restarted", level="info")
                continue

            # Per-bot halt checking
            for name, bot in self.bots.items():
                halt_file = per_bot_halt_path(name)
                if halt_file.exists() and not bot.per_bot_halted:
                    log.warning("Per-bot halt active for %s — stopping", name)
                    bot.stop()
                    bot.per_bot_halted = True
                elif not halt_file.exists() and bot.per_bot_halted:
                    log.info("Per-bot halt removed for %s — restarting", name)
                    bot.per_bot_halted = False
                    bot.start()

            # Auto-restart crashed or hung daemons
            health = self._load_health()
            any_restarted = False
            for name, bot in self.bots.items():
                if name in DISABLED_BY_DEFAULT:
                    continue
                if bot.per_bot_halted:
                    continue
                if bot.check_and_restart(health_data=health):
                    any_restarted = True
            if any_restarted:
                self._save_state()

            # Check calibration staleness
            self.check_calibration_staleness()

        # Graceful shutdown
        log.info("Stopping all bots...")
        self.stop_bots()
        self._release_lock()
        log.info("Supervisor stopped.")

    def check_calibration_staleness(self):
        """Warn if calibration has zero samples but enough trades exist."""
        # Only check once per hour
        now = time.time()
        if now - self._last_calibration_check < 3600:
            return
        self._last_calibration_check = now

        cal_path = PROJECT_DIR / "config" / "calibration.json"
        if not cal_path.exists():
            return
        try:
            cal = json.loads(cal_path.read_text())
            n_weather = cal.get("weather", {}).get("n", 0)
            n_trades = cal.get("n_trades", 0)
            if n_weather == 0 and n_trades > 30:
                notify_webhook(
                    f"Calibration stale: {n_trades} trades logged but n=0 calibrated. Run npm run calibrate.",
                    level="warning"
                )
        except Exception:
            pass

    def _load_health(self):
        """Load health-state.json for heartbeat info."""
        if not HEALTH_STATE_PATH.exists():
            return {}
        try:
            return json.loads(HEALTH_STATE_PATH.read_text())
        except Exception:
            return {}


def main():
    parser = argparse.ArgumentParser(description="Kalshi bot process supervisor")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "run"],
                        help="Supervisor command")
    parser.add_argument("bots", nargs="*", help="Bot names (default: all enabled)")
    args = parser.parse_args()

    supervisor = Supervisor()

    if args.command == "start":
        supervisor.start_bots(args.bots or None)
    elif args.command == "stop":
        supervisor.stop_bots(args.bots or None)
    elif args.command == "restart":
        supervisor.restart_bots(args.bots)
    elif args.command == "status":
        supervisor.status()
    elif args.command == "run":
        supervisor.run()


if __name__ == "__main__":
    main()
