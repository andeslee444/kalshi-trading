#!/usr/bin/env python3
"""Daily operations loop orchestrator — self-healing and self-improving.

Runs five stages in order:
  0. Refresh — regenerate daily artifacts
  1. Health Gate — decide if desk telemetry is trustworthy
  2. Explain — turn results into business understanding
  3. Diagnose — identify why returns were lower than expected
  4. Act — heal safe failures and queue reviewed changes

Writes a single normalized artifact: data/daily-ops-loop.json

Usage:
    python3 scripts/daily-ops-loop.py                # Full loop
    python3 scripts/daily-ops-loop.py --skip-refresh  # Skip Stage 0 (use existing artifacts)
    python3 scripts/daily-ops-loop.py --dry-run       # No auto-actions, report only
    python3 scripts/daily-ops-loop.py --json          # JSON output only (no text summary)
"""

import argparse
import datetime
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from ops.daily_loop_policy import (
    DailyLoopPolicy,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
)
from research.allocator_constraint_analysis import ConstraintAnalyzer
from execution_quality import ExecutionAnalyzer
from trade_files import ALL_TRADE_PATHS

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
SCRIPTS_DIR = PROJECT_DIR / "scripts"
ARTIFACT_PATH = DATA_DIR / "daily-ops-loop.json"

_log = logging.getLogger("daily-ops-loop")


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_json_safe(path):
    """Load a JSON file, returning None on any error."""
    try:
        p = Path(path)
        if not p.exists():
            return None
        text = p.read_text().strip()
        if not text:
            return None
        return json.loads(text)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


HISTORY_DIR = DATA_DIR / "ops-loop-history"


def _load_previous_loop():
    """Load the previous daily-ops-loop.json for follow-through tracking."""
    return _load_json_safe(ARTIFACT_PATH)


def _archive_previous_loop():
    """Archive the current artifact before overwriting, so recommendation
    tracking is durable across days (not lost by overwrite)."""
    if not ARTIFACT_PATH.exists():
        return
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        data = _load_json_safe(ARTIFACT_PATH)
        if data and data.get("generated_at"):
            ts = data["generated_at"][:10]  # YYYY-MM-DD
            archive_path = HISTORY_DIR / f"daily-ops-loop-{ts}.json"
            if not archive_path.exists():
                import shutil
                shutil.copy2(str(ARTIFACT_PATH), str(archive_path))
    except Exception as e:
        _log.warning("Failed to archive previous loop: %s", e)


# ── Stage 0: Refresh ────────────────────────────────────────────────

REFRESH_COMMANDS = [
    {
        "name": "parity",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "ledger-parity-report.py"), "--json", "--save"],
        "artifact": DATA_DIR / "ledger-parity-report.json",
        "critical": True,
    },
    {
        "name": "reconcile",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "reconcile-trades.py")],
        "artifact": None,
        "critical": True,
    },
    {
        "name": "source_scorecard",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "source-scorecard.py"), "--json", "--save"],
        "artifact": DATA_DIR / "source-catalog.json",
        "critical": False,
    },
    {
        "name": "attribution",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "daily-attribution.py"), "--save"],
        "artifact": DATA_DIR / "attribution-report.json",
        "critical": False,
    },
    {
        "name": "snapshot",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "pnl-snapshot.py")],
        "artifact": DATA_DIR / "financial-snapshot.json",
        "critical": True,
    },
    {
        "name": "performance",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "analyze-performance.py"), "--reconcile", "--save"],
        "artifact": DATA_DIR / "performance-metrics.json",
        "critical": False,
    },
    {
        "name": "missed_trades",
        "cmd": [sys.executable, str(SCRIPTS_DIR / "missed-trade-report.py"), "--limit", "20", "--save"],
        "artifact": DATA_DIR / "missed-trade-report.json",
        "critical": False,
    },
]


def run_refresh(skip=False, logger=print):
    """Stage 0: Run all refresh commands, capture results.

    Returns dict of {name: {"success": bool, "error": str|None, "duration_s": float}}
    """
    results = {}

    if skip:
        logger("[Stage 0] Skipped (--skip-refresh)")
        for cmd_spec in REFRESH_COMMANDS:
            results[cmd_spec["name"]] = {"success": True, "error": None, "duration_s": 0, "skipped": True}
        return results

    logger("[Stage 0] Refreshing daily artifacts...")
    for cmd_spec in REFRESH_COMMANDS:
        name = cmd_spec["name"]
        logger(f"  Running {name}...")
        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd_spec["cmd"],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(PROJECT_DIR),
            )
            duration = time.monotonic() - start
            if result.returncode != 0:
                error_msg = (result.stderr or result.stdout or "unknown error").strip()[-500:]
                results[name] = {"success": False, "error": error_msg, "duration_s": round(duration, 1)}
                logger(f"    FAILED ({duration:.1f}s): {error_msg[:100]}")
            else:
                results[name] = {"success": True, "error": None, "duration_s": round(duration, 1)}
                logger(f"    OK ({duration:.1f}s)")
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            results[name] = {"success": False, "error": "timeout (300s)", "duration_s": round(duration, 1)}
            logger(f"    TIMEOUT ({duration:.1f}s)")
        except Exception as e:
            duration = time.monotonic() - start
            results[name] = {"success": False, "error": str(e), "duration_s": round(duration, 1)}
            logger(f"    ERROR ({duration:.1f}s): {e}")

    return results


# ── Stage 1: Health Gate ────────────────────────────────────────────

def run_health_gate(policy, refresh_results, logger=print):
    """Stage 1: Evaluate health signals.

    Returns HealthVerdict.
    """
    logger("[Stage 1] Evaluating health gate...")

    # Load parity report
    parity_report = _load_json_safe(DATA_DIR / "ledger-parity-report.json") or {}

    # Load source summary from source-catalog.json (has pre-computed status field)
    # Falls back to health-state.json if catalog unavailable
    source_catalog = _load_json_safe(DATA_DIR / "source-catalog.json")
    if source_catalog and isinstance(source_catalog.get("sources"), list):
        # source-catalog.json stores sources as a list; convert to dict keyed by name
        source_summary = {"sources": {
            s["source_name"]: s for s in source_catalog["sources"]
            if isinstance(s, dict) and "source_name" in s
        }}
    else:
        health_state = _load_json_safe(DATA_DIR / "health-state.json") or {}
        source_summary = {"sources": health_state.get("sources", {})}

    # Load process summary from supervisor state or health state
    health_state = _load_json_safe(DATA_DIR / "health-state.json") or {}
    supervisor_state = _load_json_safe(DATA_DIR / "pids" / "supervisor-state.json") or {}
    process_summary = {"bots": health_state.get("bots", {})}
    # Merge supervisor state if available
    for bot_name, bot_data in supervisor_state.items():
        if isinstance(bot_data, dict) and bot_name not in ("artifact_type", "schema_version"):
            if bot_name not in process_summary["bots"]:
                process_summary["bots"][bot_name] = {}
            process_summary["bots"][bot_name].update(bot_data)

    # Snapshot result from refresh
    snapshot_refresh = refresh_results.get("snapshot", {})
    snapshot_result = {
        "success": snapshot_refresh.get("success", True),
        "error": snapshot_refresh.get("error"),
    }
    if snapshot_refresh.get("skipped"):
        snapshot_result["skipped"] = True
        snapshot_result["warning"] = "snapshot refresh was skipped; data may be stale"

    verdict = policy.evaluate_health(
        parity_report=parity_report,
        source_summary=source_summary,
        process_summary=process_summary,
        snapshot_result=snapshot_result,
    )

    status_icon = {"ok": "OK", "warning": "WARNING", "critical": "CRITICAL"}
    logger(f"  Overall: {status_icon.get(verdict.overall_status, verdict.overall_status)}")
    if verdict.incident_candidates:
        logger(f"  Incident candidates: {len(verdict.incident_candidates)}")
    if verdict.auto_actions:
        logger(f"  Auto-actions: {len(verdict.auto_actions)}")

    return verdict


# ── Stage 2: Explain ────────────────────────────────────────────────

def run_explain(logger=print):
    """Stage 2: Build trading outcome and execution quality from saved artifacts.

    Returns (trading_outcome, execution_quality) dicts.
    """
    logger("[Stage 2] Building daily explanation...")

    # Trading outcome from attribution and snapshot
    attribution = _load_json_safe(DATA_DIR / "attribution-report.json") or {}
    snapshot = _load_json_safe(DATA_DIR / "financial-snapshot.json") or {}

    realized_pnl = snapshot.get("realized_pnl", {})
    balance_check = snapshot.get("balance_check", {})

    # Extract today's realized P&L from the by_day breakdown (settlement date keyed)
    today_str = datetime.date.today().isoformat()
    by_day = realized_pnl.get("by_day", {})
    today_pnl = by_day.get(today_str, 0)

    trading_outcome = {
        "realized_pnl_cents": realized_pnl.get("total_cents", 0),
        "today_pnl_cents": today_pnl,
        "settled_trade_count": attribution.get("summary", {}).get("total_trades_settled", 0),
        "by_bot": attribution.get("by_bot", {}),
        "by_market_type": attribution.get("by_market_type", {}),
    }

    # Execution quality from ExecutionAnalyzer (uses actual trade records)
    try:
        ea = ExecutionAnalyzer(trade_file_paths=[{"path": str(p)} for p in ALL_TRADE_PATHS])
        ea.load_trades()
        eq_report = ea.json_report()
        execution_quality = {
            "fill_rate": eq_report.get("fill_rate"),
            "avg_slippage_cents": eq_report.get("average_slippage_cents"),
            "impl_shortfall_cents": eq_report.get("implementation_shortfall_cents"),
            "by_bot": {},
        }
        # Reshape by_bot to match what policy engine expects (trades, avg_slippage_cents, fill_rate)
        for bot_name, bot_stats in eq_report.get("by_bot", {}).items():
            execution_quality["by_bot"][bot_name] = {
                "trades": bot_stats.get("trade_count", 0),
                "avg_slippage_cents": bot_stats.get("avg_slippage_cents", 0),
                "fill_rate": bot_stats.get("fill_rate"),
            }
    except Exception as e:
        logger(f"  Execution quality analysis failed: {e}")
        execution_quality = {
            "fill_rate": None,
            "avg_slippage_cents": None,
            "impl_shortfall_cents": None,
            "by_bot": {},
        }

    logger(f"  Realized P&L: {trading_outcome['realized_pnl_cents']}c "
           f"({trading_outcome['settled_trade_count']} settled trades)")

    return trading_outcome, execution_quality


# ── Stage 3: Diagnose ──────────────────────────────────────────────

def run_diagnose(policy, attribution, execution_quality, logger=print):
    """Stage 3: Diagnose improvement opportunities.

    Returns (opportunities, constraint_report) tuple.
    """
    logger("[Stage 3] Diagnosing improvement opportunities...")

    # Missed trade report
    missed_report = _load_json_safe(DATA_DIR / "missed-trade-report.json") or {}

    # Constraint analysis
    constraint_analyzer = ConstraintAnalyzer()
    try:
        constraint_analyzer.load_budget_decisions(lookback_days=1)
        constraint_report = constraint_analyzer.full_report()
    except Exception as e:
        logger(f"  Constraint analysis failed: {e}")
        constraint_report = {"summary": {"total_decisions": 0, "total_denials": 0, "total_foregone_ev_cents": 0}}

    # Run policy evaluation
    opportunities = policy.evaluate_improvements(
        attribution=attribution,
        missed_trade_report=missed_report,
        execution_quality=execution_quality,
        constraint_analysis=constraint_report,
    )

    logger(f"  Found {len(opportunities)} improvement opportunity(ies)")
    summary = constraint_report.get("summary", {})
    cap_denials = summary.get("total_denials_capacity", summary.get("total_denials", 0))
    dedup_denials = summary.get("total_denials_dedup", 0)
    if cap_denials > 0 or dedup_denials > 0:
        logger(f"  Allocator: {cap_denials} capacity denials ({summary.get('total_foregone_ev_cents', 0):.0f}c foregone EV), "
               f"{dedup_denials} dedup denials (not capacity)")

    return opportunities, constraint_report


# ── Stage 4: Act ───────────────────────────────────────────────────

def run_act(policy, health_verdict, opportunities, dry_run=False, logger=print):
    """Stage 4: Classify and execute actions.

    Executes Class A auto-actions (bot restarts, incident open/update,
    artifact refresh) and persists recommendations for follow-through.

    Returns actions dict.
    """
    logger("[Stage 4] Classifying and executing actions...")

    actions = policy.classify_actions(health_verdict, opportunities)

    # Stamp all operator_required actions with created_at for follow-through tracking
    for action in actions.get("operator_required", []):
        action.setdefault("created_at", _utc_now_iso())

    if dry_run:
        logger("  [DRY RUN] No auto-actions executed")
        for action in actions.get("auto_executed", []):
            action["dry_run"] = True
        for ic in actions.get("incident_candidates", []):
            ic["dry_run"] = True
    else:
        # Execute Class A auto-actions
        for action in actions.get("auto_executed", []):
            action_type = action.get("type", "")
            if action_type == "restart_bot":
                target = action.get("target", "")
                logger(f"  Auto-restarting bot: {target}")
                success = _execute_bot_restart(target, logger=logger)
                action["executed"] = success
                action["executed_at"] = _utc_now_iso()
                if not success:
                    action["execution_error"] = "restart subprocess failed"
            elif action_type in ("refresh_artifact",):
                logger(f"  Auto-refreshing: {action.get('target', 'unknown')}")
                action["executed"] = True
                action["executed_at"] = _utc_now_iso()

        # Open/update incidents for incident candidates
        for ic in actions.get("incident_candidates", []):
            inc_type = ic.get("type", "unknown")
            inc_id = f"OPS-{inc_type.upper()}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')}"
            logger(f"  Opening incident: {inc_id} ({inc_type})")
            success = _execute_incident_open(
                incident_id=inc_id,
                summary=ic.get("summary", f"Auto-detected: {inc_type}"),
                severity=ic.get("severity", "warning"),
                logger=logger,
            )
            if success:
                ic["opened"] = True
                ic["incident_id"] = inc_id
                ic["opened_at"] = _utc_now_iso()
            else:
                ic["opened"] = False
                ic["open_error"] = f"Failed to open incident {inc_id}"

    logger(f"  Auto-executed: {len(actions.get('auto_executed', []))}")
    logger(f"  Operator required: {len(actions.get('operator_required', []))}")
    logger(f"  Incident candidates: {len(actions.get('incident_candidates', []))}")

    return actions


def _execute_bot_restart(bot_name, logger=print):
    """Execute a single bot restart via supervisor. Returns True on success."""
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "supervisor.py"), "restart", bot_name],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            logger(f"    Restart failed: {(result.stderr or result.stdout or '').strip()[:200]}")
            return False
        return True
    except Exception as e:
        logger(f"    Restart error: {e}")
        return False


def _execute_incident_open(incident_id, summary, severity, logger=print):
    """Open or update an incident via incident-workflow.py (idempotent). Returns True on success."""
    try:
        result = subprocess.run(
            [
                sys.executable, str(SCRIPTS_DIR / "incident-workflow.py"),
                "open", incident_id,
                "--summary", summary,
                "--severity", severity,
                "--note", "Auto-opened by daily ops loop",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            logger(f"    Incident open failed: {(result.stderr or result.stdout or '').strip()[:200]}")
            return False
        return True
    except Exception as e:
        logger(f"    Incident open error: {e}")
        return False


# ── Follow-Through ─────────────────────────────────────────────────

def _collect_historical_recommendations():
    """Collect undispositioned operator_required actions from archived loops.

    Scans ops-loop-history/ for all archived artifacts and collects any
    operator_required actions that were never dispositioned (accepted/rejected/deferred).
    This ensures recommendations aren't lost when daily artifacts overwrite each other.
    """
    all_actions = []
    if not HISTORY_DIR.exists():
        return all_actions
    for archive_path in sorted(HISTORY_DIR.glob("daily-ops-loop-*.json")):
        data = _load_json_safe(archive_path)
        if not data or "actions" not in data:
            continue
        generated_at = data.get("generated_at")
        for action in data["actions"].get("operator_required", []):
            if action.get("disposition") in ("accepted", "rejected", "deferred"):
                continue
            # Stamp source date for staleness tracking
            if not action.get("created_at"):
                action["created_at"] = generated_at
            all_actions.append(action)
    return all_actions


def run_follow_through(policy, logger=print):
    """Check follow-through on prior recommendations."""
    logger("[Follow-Through] Checking prior recommendations...")

    # Build a merged view: current artifact + historical undispositioned items
    previous_loop = _load_previous_loop()
    historical_actions = _collect_historical_recommendations()
    if historical_actions and previous_loop:
        # Merge historical actions into the previous loop's operator_required list,
        # deduplicating by description + affected_bots to avoid double-counting
        existing = previous_loop.setdefault("actions", {}).setdefault("operator_required", [])
        existing_keys = {
            (a.get("description", ""), tuple(a.get("affected_bots", [])))
            for a in existing
        }
        for action in historical_actions:
            key = (action.get("description", ""), tuple(action.get("affected_bots", [])))
            if key not in existing_keys:
                existing.append(action)
                existing_keys.add(key)
    elif historical_actions and not previous_loop:
        previous_loop = {"actions": {"operator_required": historical_actions}}

    # Load open incidents
    incident_data = _load_json_safe(DATA_DIR / "incident-reviews.json")
    open_incidents = []
    if isinstance(incident_data, dict):
        for inc in incident_data.get("incidents", []):
            if isinstance(inc, dict) and inc.get("status") not in ("closed", "resolved"):
                open_incidents.append(inc)
    elif isinstance(incident_data, list):
        for inc in incident_data:
            if isinstance(inc, dict) and inc.get("status") not in ("closed", "resolved"):
                open_incidents.append(inc)

    # Load open experiments
    experiment_data = _load_json_safe(DATA_DIR / "experiment-runs.json")
    open_experiments = []
    if isinstance(experiment_data, dict):
        for exp in experiment_data.get("experiments", []):
            if isinstance(exp, dict) and exp.get("stage") not in ("retired", "rolled_back"):
                open_experiments.append(exp)
    elif isinstance(experiment_data, list):
        for exp in experiment_data:
            if isinstance(exp, dict) and exp.get("stage") not in ("retired", "rolled_back"):
                open_experiments.append(exp)

    follow_through = policy.evaluate_follow_through(
        previous_loop=previous_loop,
        open_incidents=open_incidents,
        open_experiments=open_experiments,
    )

    stale_count = len(follow_through.get("stale_recommendations", []))
    stale_inc_count = len(follow_through.get("stale_incidents", []))
    if stale_count > 0:
        logger(f"  Stale recommendations: {stale_count}")
    if stale_inc_count > 0:
        logger(f"  Stale incidents: {stale_inc_count}")
    logger(f"  Open incidents: {len(follow_through.get('open_incidents', []))}")
    logger(f"  Open experiments: {len(follow_through.get('open_experiments', []))}")

    return follow_through


def _compute_capital_utilization():
    """Build capital utilization from allocator-state.json and financial-snapshot.json."""
    allocator_state = _load_json_safe(DATA_DIR / "allocator-state.json") or {}
    snapshot = _load_json_safe(DATA_DIR / "financial-snapshot.json") or {}

    bot_spend = allocator_state.get("bot_spend", {})
    total_deployed_cents = sum(
        v for v in bot_spend.values() if isinstance(v, (int, float))
    ) if bot_spend else 0
    balance_cents = snapshot.get("account", {}).get("balance_cents", 0)

    return {
        "total_deployed_cents": total_deployed_cents,
        "available_balance_cents": balance_cents,
        "utilization_pct": round(total_deployed_cents / balance_cents * 100, 1) if balance_cents > 0 else 0.0,
        "by_bot": {k: v for k, v in bot_spend.items()} if bot_spend else {},
    }


# ── Assemble & Write ───────────────────────────────────────────────

def assemble_artifact(refresh_results, health_verdict, trading_outcome,
                      execution_quality, constraint_report, opportunities,
                      actions, follow_through):
    """Assemble the canonical daily-ops-loop.json artifact."""

    phase4_window = {
        "mode": "observation",
        "parity_required_until": "2026-03-31",
    }

    missed_report = _load_json_safe(DATA_DIR / "missed-trade-report.json") or {}

    artifact = {
        "generated_at": _utc_now_iso(),
        "phase4_window": phase4_window,
        "refresh": refresh_results,
        "health": health_verdict.to_dict(),
        "trading_outcome": trading_outcome,
        "execution_quality": execution_quality,
        "missed_opportunity": {
            "top_missed_trades": missed_report.get("top_missed_trades", []),
            "reason_distribution": missed_report.get("reason_distribution", {}),
            "estimated_foregone_pnl_cents": constraint_report.get("summary", {}).get("total_foregone_ev_cents", 0),
        },
        "allocator": {
            "binding_constraints": constraint_report.get("denial_count_by_constraint", {}),
            "foregone_ev_by_constraint": constraint_report.get("foregone_ev_by_constraint", {}),
            "capital_utilization": _compute_capital_utilization(),
            "approval_rate": constraint_report.get("approval_rate", {}),
            "recommended_reallocations": constraint_report.get("reallocation_candidates", []),
            "dedup": constraint_report.get("dedup", {}),
            "opportunity_log": constraint_report.get("opportunity_log", {}),
        },
        "actions": actions,
        "improvements": [o.to_dict() for o in opportunities],
        "follow_through": follow_through,
    }

    return artifact


def save_artifact(artifact, path=None):
    """Write the daily-ops-loop.json artifact atomically."""
    out_path = Path(path) if path else ARTIFACT_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(artifact, indent=2, default=str) + "\n")
    os.replace(str(tmp_path), str(out_path))
    return out_path


# ── Text Summary ───────────────────────────────────────────────────

def print_summary(artifact):
    """Print a concise operator-facing text summary."""
    print()
    print("=" * 60)
    print("DAILY OPS LOOP SUMMARY")
    print(f"Generated: {artifact['generated_at']}")
    print("=" * 60)

    # Health
    health = artifact.get("health", {})
    status = health.get("overall_status", "unknown")
    print(f"\nHealth: {status.upper()}")
    for subsystem in ("parity", "sources", "processes", "snapshot"):
        sub = health.get(subsystem, {})
        sub_status = sub.get("status", "ok")
        if sub_status != "ok":
            print(f"  {subsystem}: {sub_status.upper()} — {sub.get('details', {})}")

    # Trading
    outcome = artifact.get("trading_outcome", {})
    pnl = outcome.get("realized_pnl_cents", 0)
    trades = outcome.get("settled_trade_count", 0)
    print(f"\nTrading: {pnl}c realized P&L, {trades} settled trades")
    by_bot = outcome.get("by_bot", {})
    for bot, stats in sorted(by_bot.items(), key=lambda x: x[1].get("pnl_cents", 0), reverse=True):
        if isinstance(stats, dict):
            print(f"  {bot}: {stats.get('pnl_cents', 0)}c ({stats.get('trades', 0)} trades, "
                  f"{stats.get('win_rate', 0):.0%} WR)")

    # Allocator constraints
    allocator = artifact.get("allocator", {})
    constraints = allocator.get("binding_constraints", {})
    if constraints:
        print("\nAllocator Constraints:")
        ev_map = allocator.get("foregone_ev_by_constraint", {})
        for constraint, count in sorted(constraints.items(), key=lambda x: x[1], reverse=True):
            ev = ev_map.get(constraint, 0)
            print(f"  {constraint}: {count} denials ({ev:.0f}c foregone EV)")

    # Improvements
    improvements = artifact.get("improvements", [])
    if improvements:
        print(f"\nImprovement Opportunities ({len(improvements)}):")
        for opp in improvements[:5]:
            print(f"  [{opp.get('severity', '?').upper()}] {opp.get('description', '')}")
            if opp.get("recommended_action"):
                print(f"    -> {opp['recommended_action']}")

    # Actions
    actions = artifact.get("actions", {})
    auto = actions.get("auto_executed", [])
    operator = actions.get("operator_required", [])
    incidents = actions.get("incident_candidates", [])
    if auto:
        print(f"\nAuto-Executed ({len(auto)}):")
        for a in auto:
            print(f"  {a.get('type', '?')}: {a.get('target', a.get('description', ''))}")
    if operator:
        print(f"\nOperator Required ({len(operator)}):")
        for a in operator[:5]:
            print(f"  {a.get('category', a.get('type', '?'))}: {a.get('description', a.get('recommended_action', ''))}")
    if incidents:
        print(f"\nIncident Candidates ({len(incidents)}):")
        for inc in incidents[:3]:
            print(f"  [{inc.get('severity', '?').upper()}] {inc.get('summary', '')}")

    # Follow-through
    ft = artifact.get("follow_through", {})
    stale_recs = ft.get("stale_recommendations", [])
    stale_incs = ft.get("stale_incidents", [])
    if stale_recs or stale_incs:
        print(f"\nFollow-Through:")
        if stale_recs:
            print(f"  {len(stale_recs)} stale recommendation(s) need attention")
        if stale_incs:
            print(f"  {len(stale_incs)} stale incident(s) need attention")

    print()
    print("=" * 60)


# ── Main ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily operations loop orchestrator")
    parser.add_argument("--skip-refresh", action="store_true", help="Skip Stage 0 artifact refresh")
    parser.add_argument("--dry-run", action="store_true", help="No auto-actions, report only")
    parser.add_argument("--json", action="store_true", dest="json_output", help="JSON output only")
    parser.add_argument("--output", type=str, default=None, help="Override output path")
    args = parser.parse_args()

    logger = (lambda msg: None) if args.json_output else print

    start_time = time.monotonic()
    policy = DailyLoopPolicy()

    # Stage 0: Refresh
    refresh_results = run_refresh(skip=args.skip_refresh, logger=logger)

    # Stage 1: Health Gate
    health_verdict = run_health_gate(policy, refresh_results, logger=logger)

    # Stage 2: Explain
    trading_outcome, execution_quality = run_explain(logger=logger)

    # Stage 3: Diagnose
    attribution = _load_json_safe(DATA_DIR / "attribution-report.json") or {}
    opportunities, constraint_report = run_diagnose(policy, attribution, execution_quality, logger=logger)

    # Stage 4: Act
    actions = run_act(policy, health_verdict, opportunities, dry_run=args.dry_run, logger=logger)

    # Follow-through
    follow_through = run_follow_through(policy, logger=logger)

    # Assemble artifact
    artifact = assemble_artifact(
        refresh_results, health_verdict, trading_outcome, execution_quality,
        constraint_report, opportunities, actions, follow_through,
    )
    artifact["loop_duration_s"] = round(time.monotonic() - start_time, 1)

    # Archive previous artifact before overwriting (durable follow-through)
    if not args.output:
        _archive_previous_loop()

    # Save
    out_path = save_artifact(artifact, path=args.output)
    logger(f"\nArtifact saved to {out_path}")

    if args.json_output:
        print(json.dumps(artifact, indent=2, default=str))
    else:
        print_summary(artifact)


if __name__ == "__main__":
    main()
