#!/usr/bin/env python3
"""Sport Autoresearch Dashboard — FastAPI web app on port 3457.

Serves a single-page research monitoring dashboard with experiment results,
sensitivity analysis, director status, and research agent controls.

Usage:
    python3 scripts/research-dashboard.py
    # or: uvicorn scripts.research-dashboard:app --port 3457
"""

from __future__ import annotations

import ast
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from statistics import correlation as _corr

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

# ── Paths ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_TSV = PROJECT_ROOT / "results.tsv"
EXPERIMENT_PY = PROJECT_ROOT / "experiment.py"
DATA_DIR = PROJECT_ROOT / "data"
GAMELOGS_DIR = DATA_DIR / "processed" / "gamelogs"
DIRECTOR_STATE = DATA_DIR / "director-state.json"
DIRECTOR_LOG = DATA_DIR / "director-log.json"
PID_FILE = DATA_DIR / "research-agent.pid"
COLLECTION_SUMMARY = DATA_DIR / "processed" / "collection_summary.json"
RESEARCH_PROGRAM = PROJECT_ROOT / "research_program.md"
HTML_PATH = Path(__file__).resolve().parent / "research-dashboard.html"

# Ensure data dir exists
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Add project to path so we can import director_checks
sys.path.insert(0, str(Path(__file__).resolve().parent))
from director_checks import parse_results_tsv, run_checks  # noqa: E402

# Import DEFAULTS from run_experiment for sensitivity analysis
sys.path.insert(0, str(PROJECT_ROOT))
from run_experiment import DEFAULTS  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────


def _parse_py_assignments(text: str) -> dict:
    """Parse NAME = value assignments from Python source text.

    Uses ast.literal_eval for safe value parsing (no code execution).
    """
    params = {}
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        match = re.match(r"^([A-Z_]+)\s*=\s*(.+?)(?:\s*#.*)?$", line)
        if match:
            name, raw_val = match.group(1), match.group(2).strip()
            try:
                val = ast.literal_eval(raw_val)
                params[name] = val
            except (ValueError, SyntaxError):
                params[name] = raw_val
    return params


# ── App ──────────────────────────────────────────────────────────────

app = FastAPI(title="Sport Autoresearch Dashboard", version="1.0.0")


# ── HTML serving ─────────────────────────────────────────────────────


@app.get("/")
async def serve_dashboard():
    """Serve the main dashboard HTML page."""
    if not HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="Dashboard HTML not found")
    return FileResponse(HTML_PATH, media_type="text/html")


# ── Data APIs ────────────────────────────────────────────────────────


@app.get("/api/results")
async def get_results():
    """Parse results.tsv and return as JSON list with commit timestamps."""
    rows = parse_results_tsv(RESULTS_TSV)

    # Batch-fetch commit timestamps via git log
    commits = [r.get("commit", "") for r in rows if r.get("commit")]
    commit_times = _get_commit_timestamps(commits)

    for i, row in enumerate(rows):
        row["index"] = i + 1
        row["timestamp"] = commit_times.get(row.get("commit", ""), "")
    return JSONResponse(content=rows)


def _get_commit_timestamps(commits: list[str]) -> dict[str, str]:
    """Get ISO timestamps for a list of git commit hashes."""
    if not commits:
        return {}
    try:
        result = subprocess.run(
            ["git", "log", "--format=%h %aI", "--all", "-200"],
            capture_output=True, text=True, timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            return {}
        mapping = {}
        for line in result.stdout.strip().split("\n"):
            parts = line.split(" ", 1)
            if len(parts) == 2:
                short_hash, iso_time = parts
                mapping[short_hash] = iso_time
        return mapping
    except (subprocess.TimeoutExpired, Exception):
        return {}


@app.get("/api/baseline")
async def get_baseline():
    """Run baseline metrics via run_experiment --baseline --no-log."""
    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "run_experiment.py"), "--baseline", "--no-log"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        # Parse the output between --- markers
        output = result.stdout
        metrics = {}
        in_block = False
        for line in output.split("\n"):
            if line.strip() == "---":
                in_block = not in_block
                continue
            if in_block and ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().split()[0]  # take first token (ignore LOCKED etc)
                try:
                    metrics[key] = float(val)
                except ValueError:
                    metrics[key] = val
        return JSONResponse(content={
            "status": "ok" if result.returncode == 0 else "error",
            "metrics": metrics,
            "stderr": result.stderr[:500] if result.stderr else "",
        })
    except subprocess.TimeoutExpired:
        return JSONResponse(content={"status": "error", "message": "Baseline timed out (120s)"})
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


@app.get("/api/experiment")
async def get_experiment():
    """Read current experiment.py params as JSON."""
    if not EXPERIMENT_PY.exists():
        raise HTTPException(status_code=404, detail="experiment.py not found")

    params = _parse_py_assignments(EXPERIMENT_PY.read_text())

    # Mark which differ from defaults
    changed = {}
    for k, v in params.items():
        if k in DEFAULTS and params[k] != DEFAULTS[k]:
            changed[k] = {"current": params[k], "default": DEFAULTS[k]}

    return JSONResponse(content={"params": params, "changed": changed, "defaults": DEFAULTS})


@app.get("/api/sensitivity")
async def get_sensitivity():
    """Compute param sensitivity from results.tsv data.

    For each param that varies across rows, compute correlation with brier_score.
    """
    rows = parse_results_tsv(RESULTS_TSV)
    valid = [r for r in rows if r.get("brier_score", -1) > 0 and r.get("status") != "crash"]
    if len(valid) < 3:
        return JSONResponse(content={"params": [], "message": "Too few valid experiments"})

    # Extract param values from description field
    param_values: dict[str, list[tuple[float, float]]] = {}

    for row in valid:
        desc = row.get("description", "")
        brier = row["brier_score"]

        # Parse "PARAM_NAME=value" patterns from description
        for match in re.finditer(r"([A-Z_]+)=(-?[\d.]+)", desc):
            name = match.group(1)
            try:
                val = float(match.group(2))
            except ValueError:
                continue
            if name not in param_values:
                param_values[name] = []
            param_values[name].append((val, brier))

    sensitivity = []
    for param, points in param_values.items():
        if len(points) < 3:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        # Check for variance (correlation needs non-constant data)
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            continue
        try:
            r = _corr(xs, ys)
        except Exception:
            r = 0.0

        sensitivity.append({
            "param": param,
            "correlation": round(r, 4),
            "n_points": len(points),
            "value_range": [min(xs), max(xs)],
            "brier_range": [min(ys), max(ys)],
            "scatter": [{"x": round(x, 4), "y": round(y, 4)} for x, y in points],
            "sensitive": abs(r) > 0.3,
        })

    # Sort by absolute correlation descending
    sensitivity.sort(key=lambda s: abs(s["correlation"]), reverse=True)

    return JSONResponse(content={"params": sensitivity})


@app.get("/api/data-status")
async def get_data_status():
    """Count gamelog files, total games, freshness."""
    n_players = 0
    n_games = 0
    latest_ts = None
    player_roster = []

    if GAMELOGS_DIR.exists():
        for f in sorted(GAMELOGS_DIR.glob("*.json")):
            if f.name.startswith("."):
                continue
            n_players += 1
            try:
                data = json.loads(f.read_text())
                if isinstance(data, dict):
                    games = data.get("gamelogs", [])
                    n_games += len(games)
                    collected = data.get("collected_at", "")
                    if collected and (latest_ts is None or collected > latest_ts):
                        latest_ts = collected
                    # Extract player info for roster
                    avg_pts = avg_reb = avg_ast = 0.0
                    if games:
                        pts = [g.get("points", g.get("pts", 0)) or 0 for g in games]
                        reb = [g.get("rebounds", g.get("reb", 0)) or 0 for g in games]
                        ast = [g.get("assists", g.get("ast", 0)) or 0 for g in games]
                        avg_pts = sum(pts) / len(pts) if pts else 0
                        avg_reb = sum(reb) / len(reb) if reb else 0
                        avg_ast = sum(ast) / len(ast) if ast else 0
                    player_roster.append({
                        "player_id": data.get("player_id", f.stem),
                        "player_name": data.get("player_name", f.stem),
                        "team": data.get("team", ""),
                        "game_count": len(games),
                        "avg_points": round(avg_pts, 1),
                        "avg_rebounds": round(avg_reb, 1),
                        "avg_assists": round(avg_ast, 1),
                    })
                elif isinstance(data, list):
                    n_games += len(data)
            except (json.JSONDecodeError, OSError):
                pass

    # Check collection summary freshness
    collection_ts = None
    if COLLECTION_SUMMARY.exists():
        try:
            summary = json.loads(COLLECTION_SUMMARY.read_text())
            collection_ts = summary.get("completed_at", summary.get("timestamp", None))
        except (json.JSONDecodeError, OSError):
            pass

    return JSONResponse(content={
        "n_players": n_players,
        "n_games": n_games,
        "latest_collection": latest_ts,
        "collection_summary_ts": collection_ts,
        "player_roster": player_roster,
    })


@app.get("/api/git-log")
async def get_git_log():
    """Last 20 git log entries."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        entries = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split(" ", 1)
                entries.append({
                    "commit": parts[0],
                    "message": parts[1] if len(parts) > 1 else "",
                })
        return JSONResponse(content={"entries": entries})
    except Exception as e:
        return JSONResponse(content={"entries": [], "error": str(e)})


@app.get("/api/director")
async def get_director_state():
    """Read data/director-state.json."""
    if not DIRECTOR_STATE.exists():
        return JSONResponse(content={"status": "no_data", "message": "No director state yet"})
    try:
        state = json.loads(DIRECTOR_STATE.read_text())
        return JSONResponse(content=state)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read director state: {e}")


@app.get("/api/director/log")
async def get_director_log():
    """Read data/director-log.json."""
    if not DIRECTOR_LOG.exists():
        return JSONResponse(content=[])
    try:
        log = json.loads(DIRECTOR_LOG.read_text())
        return JSONResponse(content=log)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to read director log: {e}")


@app.get("/api/research/status")
async def get_research_status():
    """Check if research agent is running (PID file check)."""
    if not PID_FILE.exists():
        return JSONResponse(content={"running": False, "pid": None})

    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return JSONResponse(content={"running": False, "pid": None, "error": "Invalid PID file"})

    # Check if process is alive
    try:
        os.kill(pid, 0)
        return JSONResponse(content={"running": True, "pid": pid})
    except OSError:
        # Process is dead, clean up stale PID file
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return JSONResponse(content={"running": False, "pid": None, "stale_pid": pid})


@app.get("/api/params/{commit}")
async def get_params_at_commit(commit: str):
    """Show experiment.py at a specific git commit."""
    # Sanitize commit hash (alphanumeric only)
    if not re.match(r"^[a-f0-9]{4,40}$", commit):
        raise HTTPException(status_code=400, detail="Invalid commit hash")

    try:
        result = subprocess.run(
            ["git", "show", f"{commit}:experiment.py"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode != 0:
            raise HTTPException(status_code=404, detail=f"Cannot read experiment.py at {commit}")

        params = _parse_py_assignments(result.stdout)

        # Diff from defaults
        changed = {k: v for k, v in params.items() if k in DEFAULTS and v != DEFAULTS[k]}

        return JSONResponse(content={"commit": commit, "params": params, "changed": changed})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Action endpoints ─────────────────────────────────────────────────


@app.post("/api/research/start")
async def start_research():
    """Launch research agent subprocess, save PID."""
    # Check if already running
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return JSONResponse(
                content={"status": "already_running", "pid": pid},
                status_code=409,
            )
        except (OSError, ValueError):
            # Stale PID, clean up
            try:
                PID_FILE.unlink()
            except OSError:
                pass

    if not RESEARCH_PROGRAM.exists():
        raise HTTPException(status_code=404, detail="research_program.md not found")

    prompt_text = RESEARCH_PROGRAM.read_text()

    try:
        proc = subprocess.Popen(
            [
                "claude",
                "--print",
                "-p",
                prompt_text,
                "--allowedTools",
                "Edit,Bash,Read,Glob,Grep",
            ],
            stdout=open(PROJECT_ROOT / "run.log", "a"),
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
        PID_FILE.write_text(str(proc.pid))
        return JSONResponse(content={"status": "started", "pid": proc.pid})
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="'claude' CLI not found in PATH")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start research agent: {e}")


@app.post("/api/research/stop")
async def stop_research():
    """Kill research agent subprocess by PID."""
    if not PID_FILE.exists():
        return JSONResponse(content={"status": "not_running"})

    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        PID_FILE.unlink(missing_ok=True)
        return JSONResponse(content={"status": "invalid_pid"})

    try:
        # Send SIGTERM first, then SIGKILL if needed
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return JSONResponse(content={"status": "stopped", "pid": pid})
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        return JSONResponse(content={"status": "already_dead", "pid": pid})
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"No permission to kill PID {pid}")


@app.post("/api/director/check")
async def trigger_director_check():
    """Trigger director analysis now."""
    try:
        state = run_checks()
        # Write state and log (same as CLI)
        from director_checks import write_state, append_log

        write_state(state)
        append_log({
            "timestamp": state["checked_at"],
            "action": state["recommendation"]["action"],
            "reason": state["recommendation"]["reason"],
            "best_brier": state["convergence"].get("best_brier"),
            "total_experiments": state["total_experiments"],
        })
        return JSONResponse(content=state)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Director check failed: {e}")


@app.post("/api/collect")
async def start_collection():
    """Launch batch_collector subprocess."""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "collect.batch_collector",
                "--min-minutes",
                "20",
                "--max-players",
                "60",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(PROJECT_ROOT),
        )
        return JSONResponse(content={
            "status": "started",
            "pid": proc.pid,
            "message": "Batch collector running (--min-minutes 20 --max-players 60)",
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start collector: {e}")


# ── Orchestrator status ──────────────────────────────────────────────

ORCHESTRATOR_STATE = DATA_DIR / "orchestrator-state.json"


@app.get("/api/orchestrator/status")
async def get_orchestrator_status():
    """Return orchestrator timing info for countdown display."""
    state = {}
    if ORCHESTRATOR_STATE.exists():
        try:
            state = json.loads(ORCHESTRATOR_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Also read director state for last check time
    director_state = {}
    if DIRECTOR_STATE.exists():
        try:
            director_state = json.loads(DIRECTOR_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    return JSONResponse(content={
        "check_interval": state.get("check_interval", 300),
        "last_check": director_state.get("checked_at", ""),
        "next_check_at": state.get("next_check_at", ""),
        "agent_started_at": state.get("agent_started_at", ""),
        "experiments_this_session": state.get("experiments_this_session", 0),
        "avg_experiment_seconds": state.get("avg_experiment_seconds", 0),
    })


# ── Agent log ────────────────────────────────────────────────────

AGENT_LOG = PROJECT_ROOT / "run.log"


def _parse_stream_json_line(line: str) -> str | None:
    """Parse a stream-json line into a readable string."""
    line = line.strip()
    if not line:
        return None
    if not line.startswith("{"):
        return line
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return line

    etype = event.get("type", "")
    if etype == "assistant":
        msg = event.get("message", {})
        content = msg.get("content", [])
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "thinking":
                    text = block.get("thinking", "")
                    if text:
                        parts.append(f"[THINKING] {text}")
                elif btype == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append(text)
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    if name in ("Read", "Glob", "Grep"):
                        target = inp.get("file_path") or inp.get("pattern") or inp.get("path", "")
                        parts.append(f"[TOOL] {name}: {target}")
                    elif name == "Edit":
                        parts.append(f"[TOOL] Edit: {inp.get('file_path', '')}")
                    elif name == "Bash":
                        parts.append(f"[TOOL] Bash: {inp.get('command', '')[:200]}")
                    else:
                        parts.append(f"[TOOL] {name}")
        return "\n".join(parts) if parts else None
    elif etype == "result":
        result = event.get("result", "")
        subtype = event.get("subtype", "")
        if subtype == "success" and result:
            return f"[RESULT] {str(result)[:300]}"
        elif subtype == "error":
            return f"[ERROR] {result}"
        return None
    elif etype == "system":
        return None
    return None


@app.get("/api/agent-log")
async def get_agent_log(lines: int = 200, offset: int = 0):
    """Return parsed agent log lines from run.log."""
    if not AGENT_LOG.exists():
        return JSONResponse(content={"lines": [], "offset": 0, "size": 0})
    try:
        size = AGENT_LOG.stat().st_size
        if offset > 0:
            if offset >= size:
                return JSONResponse(content={"lines": [], "offset": size, "size": size, "incremental": True})
            with open(AGENT_LOG, "r", errors="replace") as f:
                f.seek(offset)
                new_text = f.read()
            raw_lines = new_text.rstrip("\n").split("\n") if new_text.strip() else []
            parsed = []
            for raw in raw_lines:
                result = _parse_stream_json_line(raw)
                if result:
                    parsed.extend(result.split("\n"))
            return JSONResponse(content={"lines": parsed, "offset": size, "size": size, "incremental": True})
        with open(AGENT_LOG, "r", errors="replace") as f:
            all_lines = f.readlines()
        raw_tail = [l.rstrip("\n") for l in all_lines[-lines:]]
        parsed = []
        for raw in raw_tail:
            result = _parse_stream_json_line(raw)
            if result:
                parsed.extend(result.split("\n"))
        return JSONResponse(content={"lines": parsed[-lines:], "offset": size, "size": size, "incremental": False})
    except OSError as e:
        return JSONResponse(content={"lines": [], "offset": 0, "error": str(e)})


# ── Main ─────────────────────────────────────────────────────────────


def main():
    print(f"Starting Sport Autoresearch Dashboard on http://localhost:3457")
    print(f"Project root: {PROJECT_ROOT}")
    print(f"Results TSV:  {RESULTS_TSV}")
    uvicorn.run(app, host="127.0.0.1", port=3457, log_level="info")


if __name__ == "__main__":
    main()
