#!/usr/bin/env python3
"""Sport Autoresearch Orchestrator — autonomous research pipeline.

Starts all components and coordinates the research lifecycle:
  1. Dashboard (FastAPI on :3457) — background thread
  2. Research agent (Claude Code subprocess) — managed process
  3. Director loop (statistical checks) — foreground, every 5 min

Director actions:
  CONTINUE              → log and keep going
  ALERT                 → stop agent, print warning (metric gaming)
  COLLECT_DATA          → stop agent, run batch_collector, restart
  CONVERGED_EXPORT      → verify improvement, auto-export, stop
  CONVERGED_NO_IMPROVEMENT → reset experiment.py, restart with hints

Usage:
    python3 scripts/orchestrator.py                  # Full pipeline
    python3 scripts/orchestrator.py --no-agent       # Dashboard + director only
    python3 scripts/orchestrator.py --collect-first   # Collect data before starting
    python3 scripts/orchestrator.py --check-interval 120  # Director checks every 2m
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT_ROOT))

from director_checks import run_checks, write_state, append_log, parse_results_tsv

# ── Paths ────────────────────────────────────────────────────────────

DATA_DIR = PROJECT_ROOT / "data"
PID_FILE = DATA_DIR / "research-agent.pid"
COLLECTION_SUMMARY = DATA_DIR / "processed" / "collection_summary.json"
EXPERIMENT_PY = PROJECT_ROOT / "experiment.py"
RESULTS_TSV = PROJECT_ROOT / "results.tsv"
RESEARCH_PROGRAM = PROJECT_ROOT / "research_program.md"
EXPORT_HISTORY = DATA_DIR / "export-history.json"
ORCHESTRATOR_STATE = DATA_DIR / "orchestrator-state.json"
HINTS_FILE = DATA_DIR / "director-hints.txt"
EXPORT_OUTPUT = PROJECT_ROOT / ".." / "kalshi-trading" / "config" / "oracle-calibration.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── ANSI colors ──────────────────────────────────────────────────────

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"

# ── Globals ──────────────────────────────────────────────────────────

_agent_proc: subprocess.Popen | None = None
_agent_log_file = None
_shutdown = threading.Event()


# ── Dashboard ────────────────────────────────────────────────────────


def start_dashboard() -> threading.Thread:
    """Start the FastAPI dashboard in a background daemon thread."""
    def _run():
        import uvicorn
        spec = importlib.util.spec_from_file_location(
            "dashboard", str(PROJECT_ROOT / "scripts" / "research-dashboard.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        uvicorn.run(mod.app, host="0.0.0.0", port=3457, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
    return t


# ── Research Agent ───────────────────────────────────────────────────


def start_agent() -> subprocess.Popen | None:
    """Launch the Claude Code research agent as a managed subprocess."""
    global _agent_proc, _agent_log_file

    if not RESEARCH_PROGRAM.exists():
        _print_status("ERROR", f"research_program.md not found at {RESEARCH_PROGRAM}", _RED)
        return None

    prompt = RESEARCH_PROGRAM.read_text()

    # Add hints if director wrote them
    if HINTS_FILE.exists():
        try:
            hints = HINTS_FILE.read_text().strip()
            if hints:
                prompt += f"\n\n## Director Hints\n\n{hints}\n"
        except OSError:
            pass

    log_path = PROJECT_ROOT / "run.log"
    _agent_log_file = open(log_path, "a")

    try:
        proc = subprocess.Popen(
            [
                "claude", "--print",
                "-p", prompt,
                "--allowedTools", "Edit,Bash,Read,Glob,Grep",
            ],
            stdout=_agent_log_file,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
        _agent_proc = proc

        # Write PID file for dashboard
        PID_FILE.write_text(str(proc.pid))

        _print_status("Agent", f"started (PID {proc.pid})", _GREEN)
        return proc
    except FileNotFoundError:
        _print_status("ERROR", "claude CLI not found — install Claude Code first", _RED)
        return None
    except Exception as e:
        _print_status("ERROR", f"Failed to start agent: {e}", _RED)
        return None


def stop_agent(reason: str = "") -> None:
    """Gracefully stop the research agent."""
    global _agent_proc, _agent_log_file

    if _agent_proc is None:
        return

    pid = _agent_proc.pid
    msg = f"stopping (PID {pid})"
    if reason:
        msg += f" — {reason}"
    _print_status("Agent", msg, _YELLOW)

    try:
        _agent_proc.terminate()
        try:
            _agent_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _agent_proc.kill()
            _agent_proc.wait(timeout=5)
    except (ProcessLookupError, OSError):
        pass

    _agent_proc = None

    if _agent_log_file:
        try:
            _agent_log_file.close()
        except OSError:
            pass
        _agent_log_file = None

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def is_agent_alive() -> bool:
    """Check if the research agent process is still running."""
    if _agent_proc is None:
        return False
    return _agent_proc.poll() is None


# ── Data Collection ──────────────────────────────────────────────────


def is_data_stale(max_age_hours: float = 48.0, min_players: int = 30) -> bool:
    """Check if data needs refreshing."""
    if not COLLECTION_SUMMARY.exists():
        return True
    try:
        summary = json.loads(COLLECTION_SUMMARY.read_text())
        age_hours = (time.time() - COLLECTION_SUMMARY.stat().st_mtime) / 3600
        n_players = summary.get("players_collected", 0) + summary.get("players_cached", 0)
        if n_players == 0:
            n_players = len(summary.get("players", {}))
        return age_hours > max_age_hours or n_players < min_players
    except (json.JSONDecodeError, OSError):
        return True


def run_collection(max_players: int = 50, min_minutes: float = 20.0) -> bool:
    """Run batch_collector synchronously. Returns True on success."""
    _print_status("Collect", f"starting (max {max_players} players, >= {min_minutes} min)", _CYAN)

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "collect.batch_collector",
                "--min-minutes", str(min_minutes),
                "--max-players", str(max_players),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
        )
        if result.returncode == 0:
            _print_status("Collect", "complete", _GREEN)
            return True
        else:
            _print_status("Collect", f"failed (exit {result.returncode})", _RED)
            if result.stderr:
                for line in result.stderr.strip().split("\n")[-3:]:
                    _print_status("Collect", f"  {line}", _DIM)
            return False
    except subprocess.TimeoutExpired:
        _print_status("Collect", "timed out (10 min limit)", _RED)
        return False
    except Exception as e:
        _print_status("Collect", f"error: {e}", _RED)
        return False


# ── Export ────────────────────────────────────────────────────────────


def run_export() -> bool:
    """Run export_calibration and log the diff."""
    # Read previous calibration for diff
    old_cal = {}
    export_path = EXPORT_OUTPUT.resolve()
    if export_path.exists():
        try:
            old_cal = json.loads(export_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    _print_status("Export", f"writing to {export_path}", _CYAN)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "export.export_calibration", "-o", str(export_path)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            _print_status("Export", f"failed: {result.stderr}", _RED)
            return False
    except Exception as e:
        _print_status("Export", f"error: {e}", _RED)
        return False

    # Read new calibration for diff
    new_cal = {}
    try:
        new_cal = json.loads(export_path.read_text())
    except (json.JSONDecodeError, OSError):
        pass

    # Log the diff
    _log_export_diff(old_cal, new_cal)
    _print_status("Export", "complete", _GREEN)
    return True


def _log_export_diff(old_cal: dict, new_cal: dict) -> None:
    """Append export diff to export-history.json."""
    old_b = old_cal.get("book_b", {})
    new_b = new_cal.get("book_b", {})
    old_metrics = old_cal.get("backtest_metrics", {})
    new_metrics = new_cal.get("backtest_metrics", {})

    changes = {}
    all_keys = set(list(old_b.keys()) + list(new_b.keys()))
    for k in sorted(all_keys):
        ov = old_b.get(k)
        nv = new_b.get(k)
        if ov != nv:
            changes[k] = {"old": ov, "new": nv}

    entry = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "old_brier": old_metrics.get("brier_score"),
        "new_brier": new_metrics.get("brier_score"),
        "param_changes": changes,
        "new_metrics": new_metrics,
    }

    history = []
    if EXPORT_HISTORY.exists():
        try:
            history = json.loads(EXPORT_HISTORY.read_text())
            if not isinstance(history, list):
                history = []
        except (json.JSONDecodeError, OSError):
            history = []

    history.append(entry)
    EXPORT_HISTORY.write_text(json.dumps(history, indent=2) + "\n")

    # Print changes
    if changes:
        _print_status("Export", f"{len(changes)} param(s) changed:", _CYAN)
        for k, v in changes.items():
            _print_status("Export", f"  {k}: {v['old']} -> {v['new']}", _DIM)
    else:
        _print_status("Export", "no param changes (metrics update only)", _DIM)

    brier_old = old_metrics.get("brier_score")
    brier_new = new_metrics.get("brier_score")
    if brier_old and brier_new:
        delta = float(brier_old) - float(brier_new)
        _print_status("Export", f"Brier: {brier_old} -> {brier_new} (delta: {delta:+.4f})", _GREEN)


# ── Run-quality gating ───────────────────────────────────────────────


def verify_improvement() -> bool:
    """Re-run baseline and best experiment 3 times to verify improvement is real."""
    _print_status("Verify", "running 3x baseline + 3x current to confirm improvement", _CYAN)

    baseline_scores = []
    current_scores = []

    for i in range(3):
        # Baseline
        result = subprocess.run(
            [sys.executable, "run_experiment.py", "--baseline", "--no-log"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        for line in result.stdout.split("\n"):
            if line.strip().startswith("brier_score:"):
                try:
                    baseline_scores.append(float(line.split()[-1]))
                except ValueError:
                    pass

        # Current params
        result = subprocess.run(
            [sys.executable, "run_experiment.py", "--no-log", "--desc", "verify"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        for line in result.stdout.split("\n"):
            if line.strip().startswith("brier_score:"):
                try:
                    current_scores.append(float(line.split()[-1]))
                except ValueError:
                    pass

    if not baseline_scores or not current_scores:
        _print_status("Verify", "failed to collect scores", _RED)
        return False

    avg_baseline = sum(baseline_scores) / len(baseline_scores)
    avg_current = sum(current_scores) / len(current_scores)
    improvement = avg_baseline - avg_current
    consistent = all(c < avg_baseline for c in current_scores)

    _print_status("Verify", f"baseline avg: {avg_baseline:.4f}, current avg: {avg_current:.4f}", _CYAN)
    _print_status("Verify", f"improvement: {improvement:+.4f}, consistent: {consistent}", _CYAN)

    if improvement > 0 and consistent:
        _print_status("Verify", "PASS — improvement is real and consistent", _GREEN)
        return True
    else:
        _print_status("Verify", "FAIL — improvement not consistent across runs", _RED)
        return False


# ── Experiment reset ─────────────────────────────────────────────────


def reset_experiment_to_defaults() -> None:
    """Reset experiment.py to default values using run_experiment.py DEFAULTS."""
    _print_status("Reset", "resetting experiment.py to defaults", _YELLOW)
    subprocess.run(
        ["git", "checkout", "experiment.py"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
    )


def write_hints(hint_text: str) -> None:
    """Write director hints for the next agent run."""
    HINTS_FILE.write_text(hint_text + "\n")
    _print_status("Hints", f"wrote {len(hint_text)} chars to director-hints.txt", _DIM)


# ── Status display ───────────────────────────────────────────────────


def _print_status(component: str, message: str, color: str = _RESET) -> None:
    """Print a timestamped status line."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{_DIM}[{ts}]{_RESET} {color}{_BOLD}{component:>10}{_RESET}  {message}")


def _print_live_status(state: dict) -> None:
    """Print a compact one-line status summary."""
    conv = state.get("convergence", {})
    rec = state.get("recommendation", {})
    total = state.get("total_experiments", 0)
    best = conv.get("best_brier")
    since = conv.get("experiments_since_improvement", 0)
    action = rec.get("action", "?")

    # Count results.tsv lines for experiment count
    best_str = f"{best:.4f}" if best else "N/A"
    agent_status = f"{_GREEN}running{_RESET}" if is_agent_alive() else f"{_RED}stopped{_RESET}"

    action_colors = {
        "CONTINUE": _GREEN,
        "COLLECT_DATA": _YELLOW,
        "ALERT": _RED,
        "CONVERGED_EXPORT": _BLUE,
        "CONVERGED_NO_IMPROVEMENT": _YELLOW,
    }
    ac = action_colors.get(action, _RESET)

    print(
        f"\n  {_DIM}experiments:{_RESET} {total}  "
        f"{_DIM}best:{_RESET} {best_str}  "
        f"{_DIM}since improvement:{_RESET} {since}  "
        f"{_DIM}agent:{_RESET} {agent_status}  "
        f"{_DIM}action:{_RESET} {ac}{action}{_RESET}"
    )


def _print_banner() -> None:
    """Print startup banner."""
    print()
    print(f"{_BOLD}{'='*60}{_RESET}")
    print(f"{_BOLD}  Sport Autoresearch Orchestrator{_RESET}")
    print(f"{'='*60}")
    print(f"  Dashboard:   {_CYAN}http://localhost:3457{_RESET}")
    print(f"  Project:     {PROJECT_ROOT}")
    print(f"  Results:     {RESULTS_TSV}")
    print(f"{'='*60}")
    print()


# ── Director loop ────────────────────────────────────────────────────


def director_loop(check_interval: int, no_agent: bool) -> None:
    """Main director loop — runs until shutdown signal."""
    restart_count = 0
    max_restarts = 3  # max agent restarts before giving up

    while not _shutdown.is_set():
        # Check if agent died unexpectedly
        if not no_agent and _agent_proc is not None and not is_agent_alive():
            exit_code = _agent_proc.returncode
            _print_status("Agent", f"exited (code {exit_code})", _YELLOW)
            stop_agent("process exited")
            if restart_count < max_restarts:
                _print_status("Agent", "restarting...", _CYAN)
                start_agent()
                restart_count += 1
            else:
                _print_status("Agent", f"max restarts ({max_restarts}) reached, not restarting", _RED)

        # Run director checks
        try:
            state = run_checks()
            write_state(state)
            append_log({
                "timestamp": state["checked_at"],
                "action": state["recommendation"]["action"],
                "reason": state["recommendation"]["reason"],
                "best_brier": state["convergence"].get("best_brier"),
                "total_experiments": state["total_experiments"],
            })
        except Exception as e:
            _print_status("Director", f"check failed: {e}", _RED)
            _shutdown.wait(check_interval)
            continue

        action = state["recommendation"]["action"]
        reason = state["recommendation"]["reason"]
        _print_live_status(state)
        _print_status("Director", f"{action} — {reason}", _CYAN)

        # Act on recommendation
        if action == "CONTINUE":
            pass  # keep going

        elif action == "ALERT":
            stop_agent("metric gaming detected")
            _print_status("Director", "ALERT: metric gaming detected. Investigate manually.", _RED)
            _print_status("Director", "stopping orchestrator", _RED)
            _shutdown.set()
            break

        elif action == "COLLECT_DATA":
            stop_agent("need more data")
            if run_collection():
                if not no_agent:
                    _print_status("Agent", "restarting after data collection", _CYAN)
                    start_agent()
                    restart_count = 0
            else:
                _print_status("Director", "collection failed — stopping", _RED)
                _shutdown.set()
                break

        elif action == "CONVERGED_EXPORT":
            stop_agent("converged with improvement")
            _print_status("Director", "verifying improvement before export...", _CYAN)
            if verify_improvement():
                if run_export():
                    _print_status("Director", "research complete — calibration exported", _GREEN)
                else:
                    _print_status("Director", "export failed", _RED)
            else:
                _print_status("Director", "improvement not verified — skipping export", _YELLOW)
            _shutdown.set()
            break

        elif action == "CONVERGED_NO_IMPROVEMENT":
            if restart_count >= max_restarts:
                _print_status("Director", "max restarts reached — research exhausted", _YELLOW)
                _print_status("Director", "consider: more data, different model structure, or accept baseline", _DIM)
                stop_agent("exhausted")
                _shutdown.set()
                break

            stop_agent("converged without improvement — resetting for fresh approach")
            reset_experiment_to_defaults()
            restart_count += 1

            # Write hints for next run
            hint_lines = [
                f"## Director Hint ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
                "",
                f"Previous approach exhausted after {state['total_experiments']} experiments.",
                f"Best Brier achieved: {state['convergence'].get('best_brier', 'N/A')}",
                f"Baseline Brier: {state['ci_overlap'].get('baseline_brier', 'N/A')}",
                "",
                "Suggestions for this restart:",
                "1. Try USE_STAT_SPACE=True (fundamentally different evaluation path)",
                "2. Try zeroing ALL adjustments (MATCHUP=0, VENUE=0, B2B=0)",
                "3. Try extreme recency (LAST5=5.0, LAST10=0.5, SEASON=0.1)",
                "4. If no single param helps, the model may be at its ceiling for this data",
            ]
            write_hints("\n".join(hint_lines))

            if not no_agent:
                _print_status("Agent", f"restart {restart_count}/{max_restarts} with fresh params + hints", _CYAN)
                start_agent()

        # Wait for next check — show countdown and tail agent logs
        _wait_with_visibility(check_interval, check_interval)


def _write_orchestrator_state(
    check_interval: int,
    next_check_at: str,
    experiment_durations: list[float],
) -> None:
    """Write timing state for dashboard consumption."""
    avg_dur = 0
    if experiment_durations:
        recent = experiment_durations[-10:]
        avg_dur = sum(recent) / len(recent)

    state = {
        "check_interval": check_interval,
        "next_check_at": next_check_at,
        "agent_started_at": datetime.fromtimestamp(
            _agent_proc.pid and time.time()  # approximate
        ).isoformat(timespec="seconds") if _agent_proc else "",
        "experiments_this_session": len(experiment_durations),
        "avg_experiment_seconds": round(avg_dur, 1),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        ORCHESTRATOR_STATE.write_text(json.dumps(state, indent=2) + "\n")
    except OSError:
        pass


def _wait_with_visibility(seconds: int, check_interval: int = 300) -> None:
    """Wait with live countdown, experiment timing, and agent log tailing."""
    from datetime import timedelta
    log_path = PROJECT_ROOT / "run.log"
    last_log_size = log_path.stat().st_size if log_path.exists() else 0
    last_results_count = _count_results()
    next_check_at = (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
    last_experiment_time = time.time()
    experiment_durations: list[float] = []

    for remaining in range(seconds, 0, -1):
        if _shutdown.is_set():
            return

        # Every 3 seconds, check for new agent activity
        if remaining % 3 == 0:
            current_count = _count_results()
            if current_count > last_results_count:
                now = time.time()
                duration = now - last_experiment_time
                experiment_durations.append(duration)
                last_experiment_time = now
                last_results_count = current_count
                _show_latest_experiment(experiment_durations)

            # Check for new log output
            if log_path.exists():
                try:
                    current_size = log_path.stat().st_size
                    if current_size > last_log_size:
                        _show_new_log_lines(log_path, last_log_size, current_size)
                        last_log_size = current_size
                except OSError:
                    pass

            # Check if agent died
            if _agent_proc is not None and not is_agent_alive():
                return

        # Update status line every 5 seconds
        if remaining % 5 == 0 and remaining > 0:
            _print_countdown(remaining, last_results_count, experiment_durations, last_experiment_time)

        # Write orchestrator state every 10 seconds for dashboard
        if remaining % 10 == 0:
            _write_orchestrator_state(check_interval, next_check_at, experiment_durations)

        _shutdown.wait(1)

    print(" " * 100, end="\r")


def _print_countdown(
    remaining: int,
    exp_count: int,
    durations: list[float],
    last_exp_time: float,
) -> None:
    """Print live status line with director countdown + experiment ETA."""
    agent_status = f"{_GREEN}running{_RESET}" if is_agent_alive() else f"{_RED}stopped{_RESET}"
    mm, ss = divmod(remaining, 60)

    # Estimate next experiment time
    exp_eta = ""
    if durations and is_agent_alive():
        avg_dur = sum(durations[-5:]) / len(durations[-5:])  # rolling avg of last 5
        elapsed_since = time.time() - last_exp_time
        eta_seconds = max(0, avg_dur - elapsed_since)
        if eta_seconds > 0:
            exp_eta = f"  {_DIM}next exp ~{int(eta_seconds)}s{_RESET}"
        else:
            exp_eta = f"  {_DIM}exp due now{_RESET}"

    line = (
        f"  {_DIM}director:{_RESET} {mm}m{ss:02d}s  "
        f"{_DIM}agent:{_RESET} {agent_status}  "
        f"{_DIM}experiments:{_RESET} {exp_count}"
        f"{exp_eta}"
    )
    # Pad to clear previous line content
    print(f"{line:<100}", end="\r")
    sys.stdout.flush()


def _count_results() -> int:
    """Count non-header lines in results.tsv."""
    if not RESULTS_TSV.exists():
        return 0
    try:
        return max(0, len(RESULTS_TSV.read_text().strip().split("\n")) - 1)
    except OSError:
        return 0


def _show_latest_experiment(durations: list[float]) -> None:
    """Print the latest experiment result with timestamp and timing."""
    try:
        lines = RESULTS_TSV.read_text().strip().split("\n")
        if len(lines) < 2:
            return
        last = lines[-1].split("\t")
        if len(last) < 10:
            return
        commit = last[0][:7]
        brier = last[1]
        profit = last[3]
        status = last[8]
        desc = last[9][:45]
        n_exp = len(lines) - 1

        color = _GREEN if status == "run" else _RED
        dur_str = ""
        if durations:
            dur_str = f"  {_DIM}({durations[-1]:.0f}s){_RESET}"

        print(" " * 100, end="\r")  # clear countdown
        _print_status(
            f"#{n_exp}",
            f"{color}{commit}{_RESET}  brier={brier}  profit={profit}%{dur_str}  {desc}",
            _CYAN,
        )
    except (OSError, IndexError):
        pass


def _show_new_log_lines(log_path: Path, old_size: int, new_size: int) -> None:
    """Show relevant new lines from run.log (filter noise)."""
    try:
        with open(log_path, "r") as f:
            f.seek(old_size)
            new_text = f.read(new_size - old_size)
        for line in new_text.strip().split("\n"):
            line = line.strip()
            if any(line.startswith(k) for k in ("brier_score:", "expected_profit", "CRASH")):
                print(" " * 100, end="\r")
                _print_status("Agent", f"  {line}", _DIM)
    except OSError:
        pass


# ── Signal handling ──────────────────────────────────────────────────


def _handle_signal(signum, frame):
    """Handle Ctrl+C gracefully."""
    print()
    _print_status("Shutdown", "received interrupt, cleaning up...", _YELLOW)
    _shutdown.set()
    stop_agent("shutdown")
    sys.exit(0)


# ── Main ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Sport Autoresearch Orchestrator — autonomous research pipeline"
    )
    parser.add_argument(
        "--no-agent", action="store_true",
        help="Start dashboard + director only (no research agent)",
    )
    parser.add_argument(
        "--collect-first", action="store_true",
        help="Run batch_collector before starting research",
    )
    parser.add_argument(
        "--check-interval", type=int, default=300,
        help="Seconds between director checks (default: 300 = 5 min)",
    )
    parser.add_argument(
        "--max-players", type=int, default=50,
        help="Max players for data collection (default: 50)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    _print_banner()

    # Step 1: Start dashboard
    _print_status("Dashboard", "starting on http://localhost:3457", _CYAN)
    start_dashboard()
    time.sleep(1)  # let uvicorn bind

    # Step 2: Auto-collect if data is stale (or --collect-first)
    if args.collect_first or is_data_stale():
        stale_reason = "stale" if is_data_stale() else "requested"
        _print_status("Collect", f"data is {stale_reason}, collecting before research", _YELLOW)
        if not run_collection(max_players=args.max_players):
            _print_status("Collect", "failed — continuing with existing data", _YELLOW)

    # Step 3: Run baseline to establish beat-target
    _print_status("Baseline", "running baseline experiment...", _CYAN)
    result = subprocess.run(
        [sys.executable, "run_experiment.py", "--baseline", "--no-log", "--desc", "orchestrator baseline"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    for line in result.stdout.split("\n"):
        if any(line.strip().startswith(k) for k in ("brier_score:", "sample_size:", "line_offsets:")):
            _print_status("Baseline", f"  {line.strip()}", _DIM)

    # Step 4: Start research agent
    if not args.no_agent:
        start_agent()
        time.sleep(2)  # let agent start

    # Step 5: Director loop (foreground — blocks until shutdown)
    _print_status("Director", f"starting checks every {args.check_interval}s", _CYAN)
    try:
        director_loop(args.check_interval, args.no_agent)
    except KeyboardInterrupt:
        pass
    finally:
        stop_agent("orchestrator exiting")
        _print_status("Shutdown", "orchestrator stopped", _YELLOW)


if __name__ == "__main__":
    main()
