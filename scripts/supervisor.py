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
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import setup_logging, check_kill_switch, notify_webhook, per_bot_halt_path

log = setup_logging("supervisor")
PROJECT_DIR = Path(__file__).resolve().parent.parent
PID_DIR = PROJECT_DIR / "data" / "pids"
PID_DIR.mkdir(parents=True, exist_ok=True)
HEALTH_STATE_PATH = PROJECT_DIR / "data" / "health-state.json"
SUPERVISOR_STATE_PATH = PID_DIR / "supervisor-state.json"

# Bot definitions — maps name -> command (derived from package.json)
BOT_COMMANDS = {
    "weather":       ["python3", "src/kalshi/weather-bot.py"],
    "entertainment": ["python3", "src/kalshi/entertainment-bot.py"],
    "crypto":        ["python3", "src/kalshi/crypto-bot.py"],
    "economics":     ["python3", "src/kalshi/economics-bot.py"],
    "positions":     ["python3", "src/kalshi/position-monitor.py"],
    "monitor":       ["python3", "src/kalshi/source-monitor.py"],
    "strategy":      ["python3", "src/kalshi/strategy-trader.py"],
    "hdd":           ["python3", "src/kalshi/hdd-scraper.py"],
    "arb":           ["python3", "src/kalshi/cross-platform-arb.py"],
    "mm":            ["python3", "src/kalshi/market-maker.py"],
    "beatrelease":   ["python3", "src/kalshi/beatrelease-scanner.py"],
}

# Daemon bots auto-restart on crash; one-shot bots do not
DAEMON_BOTS = {"weather", "crypto", "economics", "positions", "monitor", "beatrelease", "arb", "entertainment"}
ONESHOT_BOTS = {"strategy", "hdd"}

# Disabled by default (can be started explicitly)
DISABLED_BY_DEFAULT = {"mm", "demo", "entertainment", "beatrelease", "arb"}

# Crash rate limiting
MAX_CRASHES = 5
CRASH_WINDOW = 600  # 10 minutes

CHECK_INTERVAL = 30  # seconds

# Heartbeat staleness detection
HEARTBEAT_NAMES = {
    "weather": "weather",
    "entertainment": "entertainment",
    "crypto": "crypto",
    "economics": "economics",
    "positions": "position-monitor",
    "monitor": "source-monitor",
    "arb": "cross-platform-arb",
    "mm": "market-maker",
    "beatrelease": "beatrelease",
}

# Expected scan intervals in minutes (from bots-config / kalshi-config)
BOT_SCAN_INTERVALS = {
    "weather": 30,
    "entertainment": 15,
    "crypto": 5,
    "economics": 360,
    "positions": 15,
    "monitor": 10,
    "arb": 10,
    "beatrelease": 60,
}

HEARTBEAT_GRACE_PERIOD = 300  # 5 min startup grace before checking heartbeats


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
            if age_min > stale_threshold_min:
                return True, age_min
        except (ValueError, TypeError):
            pass

        return False, None

    def is_running(self):
        """Check if bot is running via PID file + os.kill probe."""
        pid = self._read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            # Stale PID file
            self._remove_pid()
            return False

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
        """Stop the bot process (SIGTERM to process group, then SIGKILL after 10s)."""
        pid = self._read_pid()
        if pid is None:
            return

        log.info(f"  Stopping {self.name} (PID {pid})...")
        try:
            # Kill entire process group (bots use start_new_session=True)
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                self._remove_pid()
                return

        # Wait up to 10s for graceful shutdown
        for _ in range(20):
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

        self._remove_pid()
        self.process = None
        log.info(f"  {self.name} stopped")

    def check_oneshot_completion(self):
        """Check if one-shot bot wrote a completion/error marker."""
        marker_path = PROJECT_DIR / "data" / f"{self.name}-last-run.json"
        if not marker_path.exists():
            return
        try:
            data = json.loads(marker_path.read_text())
            status = data.get("status")
            if status == "error":
                notify_webhook(f"One-shot bot {self.name} failed: {data.get('error', '?')}", level="warning")
        except Exception:
            pass

    def check_and_restart(self, health_data=None):
        """Check if daemon crashed or hung and auto-restart with rate limiting.

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
            # Double-check: maybe process is running but PID file is stale
            if _find_bot_processes(self.cmd):
                log.info(f"  {self.name} PID file stale but process found by pgrep, skipping restart")
                needs_restart = False
            else:
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
        """Acquire singleton lock. Exit if another supervisor is running."""
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
        """Start specified bots (or all enabled)."""
        targets = self._resolve_names(names)
        if not targets:
            return
        log.info(f"Starting {len(targets)} bot(s)...")
        for name in targets:
            self.bots[name].start()
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

    def status(self):
        """Print status table for all bots."""
        health = self._load_health()

        print(f"\n{'Bot':<16} {'Status':<10} {'PID':<8} {'Uptime':<8} {'Restarts':<10} {'Last Heartbeat'}")
        print("-" * 75)

        for name in sorted(self.bots):
            bot = self.bots[name]
            pid = bot._read_pid()
            running = bot.is_running()

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

        print()

    def run(self):
        """Start all bots and enter foreground monitor loop."""
        # Signal handling for graceful shutdown
        def _shutdown(sig, frame):
            log.info(f"\nReceived signal {sig}, shutting down...")
            self._running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

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
