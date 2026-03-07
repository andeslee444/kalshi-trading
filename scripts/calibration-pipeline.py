#!/usr/bin/env python3
"""Daily calibration pipeline orchestrator.

Runs reconcile -> backfill -> backtest -> calibrate as subprocesses,
manages per-bot/per-city Brier score baselines, detects drift,
generates calibration suggestions for human review,
writes timestamped logs, and sends daily WhatsApp summaries.

Usage:
    python3 scripts/calibration-pipeline.py              # Full run + WhatsApp
    python3 scripts/calibration-pipeline.py --dry-run     # Run stages, no WhatsApp
    python3 scripts/calibration-pipeline.py --update-baseline  # Re-snapshot baselines
    python3 scripts/calibration-pipeline.py --apply-suggestion PATH  # Apply a suggestion file
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import _atomic_write_json, notify_whatsapp, setup_logging, setup_unbuffered

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
DATA_DIR = PROJECT_DIR / "data"
BASELINES_PATH = DATA_DIR / "calibration-baselines.json"
LOG_DIR = DATA_DIR / "logs" / "calibration"
RESULTS_PATH = DATA_DIR / "backtest-results.json"
SUGGESTION_DIR = DATA_DIR / "calibration-suggestions"
CALIBRATION_PATH = PROJECT_DIR / "config" / "calibration.json"
CALIBRATION_BACKUP_PATH = PROJECT_DIR / "config" / "calibration-backup.json"
HISTORY_DIR = DATA_DIR / "calibration-history"

# ── Constants ────────────────────────────────────────────────────────────────
DRIFT_THRESHOLD = 0.10   # 10% degradation triggers drift alert
MIN_SAMPLES = 10         # minimum trades before drift detection activates
SUGGESTION_IMPROVEMENT_THRESHOLD = 0.05  # 5% Brier improvement required to generate suggestion

# Calibration sections that may contain Brier scores
CALIBRATION_SECTIONS = ["weather", "nws", "album_sales", "box_office", "ensemble", "cpi"]

# (name, script_path, args, timeout_seconds)
# Core stages run first and are required for the pipeline to proceed.
STAGES_CORE = [
    ("reconcile", SCRIPTS_DIR / "reconcile-trades.py", [], 120),
    ("backfill",  SCRIPTS_DIR / "backfill-settlements.py", [], 180),
    ("backtest",  SCRIPTS_DIR / "backtest.py", ["--save"], 120),
]

# Calibration stages run independently -- if one fails, others still proceed.
STAGES_CALIBRATE = [
    ("calibrate_weather", SCRIPTS_DIR / "calibrate-sigma.py", ["--json"], 300),
    ("calibrate_crypto",  SCRIPTS_DIR / "calibrate-crypto.py", ["--dry-run"], 300),
    ("calibrate_cpi",     SCRIPTS_DIR / "calibrate-cpi-sigma.py", ["--json"], 120),
]

STAGES = STAGES_CORE + STAGES_CALIBRATE


# ── Stage Runner ─────────────────────────────────────────────────────────────

def run_stage(name, script, args, timeout):
    """Run a single pipeline stage as a subprocess.

    Returns dict with success, stdout, stderr, duration_s.
    """
    start = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, str(script)] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        duration = time.monotonic() - start
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_s": round(duration, 1),
        }
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return {
            "success": False,
            "stdout": "",
            "stderr": f"Timeout after {timeout}s",
            "duration_s": round(duration, 1),
        }
    except Exception as e:
        duration = time.monotonic() - start
        return {
            "success": False,
            "stdout": "",
            "stderr": str(e),
            "duration_s": round(duration, 1),
        }


def run_pipeline(skip_stages=None):
    """Run all pipeline stages sequentially.

    Returns dict with stages results, proposed_calibration (weather, backward compat),
    and all_calibrations (all calibrate_* stage outputs).

    skip_stages: optional set of stage names to skip (e.g., {"calibrate_strategy"}).
    """
    stages = {}
    proposed_calibrations = {}
    skip_stages = skip_stages or set()

    for name, script, args, timeout in STAGES:
        if name in skip_stages:
            log.info(f"Skipping stage: {name}")
            stages[name] = {
                "success": True,
                "stdout": "",
                "stderr": "skipped",
                "duration_s": 0.0,
            }
            continue

        log.info(f"Running stage: {name}")
        result = run_stage(name, script, args, timeout)
        stages[name] = result

        status = "OK" if result["success"] else "FAILED"
        log.info(f"  {name}: {status} ({result['duration_s']}s)")
        if not result["success"]:
            log.warning(f"  {name} stderr: {result['stderr'][:300]}")

        # Parse JSON output from calibration stages
        if name.startswith("calibrate_") and result["success"] and result["stdout"].strip():
            try:
                proposed_calibrations[name] = json.loads(result["stdout"])
            except (json.JSONDecodeError, ValueError):
                log.warning(f"Could not parse {name} JSON output")

    # Merge weather calibration as the primary proposed_calibration (backward compat)
    proposed_calibration = proposed_calibrations.get("calibrate_weather")

    return {
        "stages": stages,
        "proposed_calibration": proposed_calibration,
        "all_calibrations": proposed_calibrations,
    }


# ── Baseline Management ─────────────────────────────────────────────────────

def load_baselines():
    """Load calibration baselines from disk, or None if missing/corrupt."""
    if not BASELINES_PATH.exists():
        return None
    try:
        return json.loads(BASELINES_PATH.read_text())
    except Exception:
        return None


def save_baselines(baselines):
    """Atomically write baselines to disk."""
    _atomic_write_json(BASELINES_PATH, baselines)


def initialize_baselines(backtest_results):
    """Create baselines from current backtest results.

    Called on first run (no baselines file) or on --update-baseline.
    """
    now = datetime.now(timezone.utc).isoformat()

    baselines = {
        "aggregate": {
            "brier": backtest_results.get("brier_score"),
            "n": backtest_results.get("n_evaluated", 0),
        },
        "per_bot": {},
        "per_city": {},
        "initialized_at": now,
        "last_updated": now,
    }

    # Per-bot baselines (skip bots with None brier)
    for bot_name, bot_data in backtest_results.get("per_bot", {}).items():
        brier = bot_data.get("brier_score")
        if brier is not None:
            baselines["per_bot"][bot_name] = {
                "brier": brier,
                "n": bot_data.get("n_evaluated", 0),
            }

    # Per-city baselines
    for city, city_data in backtest_results.get("per_city_brier", {}).items():
        baselines["per_city"][city] = {
            "brier": city_data.get("brier"),
            "n": city_data.get("n", 0),
        }

    save_baselines(baselines)
    log.info(f"Baselines initialized: {len(baselines['per_bot'])} bots, {len(baselines['per_city'])} cities")
    return baselines


# ── Drift Detection ──────────────────────────────────────────────────────────

def check_drift(baselines, current_results):
    """Compare current backtest results against stored baselines.

    Returns list of drift findings for all entities (including healthy ones).
    Each finding: {entity, type, baseline_brier, current_brier, change_pct, drifted, reason}
    """
    findings = []

    # Aggregate drift
    baseline_agg = baselines.get("aggregate", {})
    findings.append(_compare_entity(
        entity="aggregate",
        entity_type="aggregate",
        baseline_brier=baseline_agg.get("brier"),
        baseline_n=baseline_agg.get("n", 0),
        current_brier=current_results.get("brier_score"),
        current_n=current_results.get("n_evaluated", 0),
    ))

    # Per-bot drift
    for bot_name, bot_baseline in baselines.get("per_bot", {}).items():
        current_bot = current_results.get("per_bot", {}).get(bot_name, {})
        findings.append(_compare_entity(
            entity=bot_name,
            entity_type="bot",
            baseline_brier=bot_baseline.get("brier"),
            baseline_n=bot_baseline.get("n", 0),
            current_brier=current_bot.get("brier_score"),
            current_n=current_bot.get("n_evaluated", 0),
        ))

    # Per-city drift
    for city, city_baseline in baselines.get("per_city", {}).items():
        current_city = current_results.get("per_city_brier", {}).get(city, {})
        findings.append(_compare_entity(
            entity=city,
            entity_type="city",
            baseline_brier=city_baseline.get("brier"),
            baseline_n=city_baseline.get("n", 0),
            current_brier=current_city.get("brier"),
            current_n=current_city.get("n", 0),
        ))

    return findings


def _compare_entity(entity, entity_type, baseline_brier, baseline_n, current_brier, current_n):
    """Compare a single entity's Brier score against its baseline."""
    finding = {
        "entity": entity,
        "type": entity_type,
        "baseline_brier": baseline_brier,
        "current_brier": current_brier,
        "change_pct": None,
        "drifted": False,
        "reason": "",
    }

    if baseline_brier is None or current_brier is None:
        finding["reason"] = "insufficient data"
        return finding

    if baseline_n < MIN_SAMPLES or current_n < MIN_SAMPLES:
        finding["reason"] = f"low sample (baseline={baseline_n}, current={current_n})"
        return finding

    if baseline_brier == 0:
        finding["reason"] = "baseline is zero"
        return finding

    change = (current_brier - baseline_brier) / baseline_brier
    finding["change_pct"] = round(change, 4)
    finding["drifted"] = change > DRIFT_THRESHOLD
    finding["reason"] = "drifted" if finding["drifted"] else "healthy"

    return finding


# ── Per-Bot Regression Gate ──────────────────────────────────────────────────

BOT_REGRESSION_THRESHOLD = 0.05   # 5% -- no bot can get this much worse
AGGREGATE_IMPROVEMENT_THRESHOLD = 0.0  # aggregate must not get worse


def check_regression_gate(before_results, after_results, min_samples=10):
    """Per-bot regression gate for auto-apply safety.

    Returns (safe: bool, reason: str).
    Safe = True means auto-apply is OK.

    Rules:
    1. Aggregate Brier must not increase (after <= before)
    2. No individual bot's Brier can increase by > BOT_REGRESSION_THRESHOLD (5%)
    3. Bots with < min_samples are skipped (insufficient data to judge)
    """
    before_brier = before_results.get("brier_score")
    after_brier = after_results.get("brier_score")

    if before_brier is None or after_brier is None:
        return False, "Missing aggregate Brier score"

    # Rule 1: aggregate must not get worse
    if after_brier > before_brier:
        change = (after_brier - before_brier) / before_brier if before_brier > 0 else 0
        return False, f"Aggregate Brier regressed: {before_brier:.4f} -> {after_brier:.4f} (+{change:.1%})"

    # Rule 2: per-bot regression check
    before_bots = before_results.get("per_bot", {})
    after_bots = after_results.get("per_bot", {})

    for bot_name, before_bot in before_bots.items():
        b_brier = before_bot.get("brier_score")
        b_n = before_bot.get("n_evaluated", 0)

        if b_brier is None or b_n < min_samples:
            continue  # skip bots with insufficient data

        after_bot = after_bots.get(bot_name, {})
        a_brier = after_bot.get("brier_score")

        if a_brier is None:
            continue

        if b_brier > 0 and (a_brier - b_brier) / b_brier > BOT_REGRESSION_THRESHOLD:
            change = (a_brier - b_brier) / b_brier
            return False, (
                f"{bot_name} regressed: {b_brier:.4f} -> {a_brier:.4f} "
                f"(+{change:.1%}, threshold {BOT_REGRESSION_THRESHOLD:.0%})"
            )

    return True, "All checks passed"


def should_auto_apply(before_results, after_results, suggestion_eval):
    """Determine if calibration should be auto-applied.

    Returns (should_apply: bool, reason: str).
    Requires BOTH suggestion evaluation AND regression gate to pass.
    """
    # Gate 1: suggestion must be worthwhile
    if not suggestion_eval.get("should_suggest", False):
        return False, "Suggestion evaluation says not worth applying"

    # Gate 2: regression gate must pass
    safe, gate_reason = check_regression_gate(before_results, after_results)
    if not safe:
        return False, f"Regression gate failed: {gate_reason}"

    return True, "All gates passed"


# ── WhatsApp Summary ─────────────────────────────────────────────────────────

def format_whatsapp_summary(stage_results, drift_findings, any_stage_failed, suggestion_path=None):
    """Format a concise WhatsApp summary (<500 chars).

    Always sent -- both healthy and drift-detected messages.
    """
    # Stage summary
    total = len(stage_results)
    passed = sum(1 for s in stage_results.values() if s["success"])
    failed_names = [n for n, s in stage_results.items() if not s["success"]]

    lines = ["Kalshi Calibration Pipeline", ""]

    if failed_names:
        lines.append(f"Stages: {passed}/{total} passed, FAILED: {', '.join(failed_names)}")
    else:
        lines.append(f"Stages: {passed}/{total} passed")

    # Drift summary
    drifted = [f for f in drift_findings if f["drifted"]]
    if drifted:
        lines.append("")
        lines.append("DRIFT DETECTED:")
        for f in drifted:
            bl = f["baseline_brier"]
            cur = f["current_brier"]
            pct = f["change_pct"]
            lines.append(f"- {f['entity']}: {bl:.3f} -> {cur:.3f} (+{pct:.0%})")
        lines.append("Recommendation: Review calibration")
    else:
        checked = len([f for f in drift_findings if f["reason"] not in ("insufficient data", "")])
        lines.append("")
        lines.append(f"All models healthy ({checked} entities checked)")

    # Suggestion info
    if suggestion_path:
        lines.append("")
        lines.append(f"Calibration suggestion generated -- review {suggestion_path}")

    return "\n".join(lines)


def format_weekly_summary(stage_results, drift_findings, all_calibrations,
                          auto_applied, apply_reason, suggestion_path=None):
    """Format weekly calibration summary for WhatsApp."""
    total = len(stage_results)
    passed = sum(1 for s in stage_results.values() if s["success"])
    failed_names = [n for n, s in stage_results.items() if not s["success"]]

    lines = ["Kalshi Weekly Calibration", ""]

    if failed_names:
        lines.append(f"Stages: {passed}/{total} OK, FAILED: {', '.join(failed_names)}")
    else:
        lines.append(f"Stages: {passed}/{total} OK")

    # Calibrator results
    cal_stages = {k: v for k, v in stage_results.items() if k.startswith("calibrate_")}
    if cal_stages:
        lines.append("")
        for name, result in cal_stages.items():
            short_name = name.replace("calibrate_", "")
            status = "OK" if result["success"] else "FAIL"
            lines.append(f"  {short_name}: {status} ({result['duration_s']}s)")

    # Auto-apply result
    lines.append("")
    if auto_applied:
        lines.append("AUTO-APPLIED: New calibration is live")
    else:
        lines.append(f"Auto-apply: skipped ({apply_reason})")

    # Drift summary
    drifted = [f for f in drift_findings if f["drifted"]]
    if drifted:
        lines.append("")
        lines.append("DRIFT:")
        for f in drifted:
            lines.append(f"  {f['entity']}: {f['baseline_brier']:.3f}->{f['current_brier']:.3f}")

    return "\n".join(lines)


# ── Suggestion Evaluation ────────────────────────────────────────────────────

def evaluate_suggestion(proposed_calibration, current_calibration):
    """Compare proposed vs current calibration to determine if a suggestion should be generated.

    Returns dict with should_suggest, improvements, aggregate_improvement_pct.
    Only suggests when aggregate Brier improvement > SUGGESTION_IMPROVEMENT_THRESHOLD (5%).
    """
    improvements = {}
    total_improvement = 0.0
    sections_with_data = 0

    # If no current calibration exists, always suggest (first calibration)
    if not current_calibration:
        return {
            "should_suggest": True,
            "improvements": {"first_calibration": True},
            "aggregate_improvement_pct": 1.0,
        }

    for section in CALIBRATION_SECTIONS:
        proposed_section = proposed_calibration.get(section, {})
        current_section = current_calibration.get(section, {})

        # Skip sections with no data (n=0 or missing)
        proposed_n = proposed_section.get("n", 0)
        current_n = current_section.get("n", 0)
        if proposed_n == 0 and current_n == 0:
            continue

        # Extract Brier scores -- calibrate-sigma.py uses "global_brier"
        proposed_brier = proposed_section.get("global_brier")
        current_brier = current_section.get("global_brier")

        if proposed_brier is None or current_brier is None:
            continue

        if current_brier == 0:
            continue

        sections_with_data += 1
        improvement_pct = (current_brier - proposed_brier) / current_brier

        if improvement_pct > 0:
            improvements[section] = {
                "before": round(current_brier, 6),
                "after": round(proposed_brier, 6),
                "improvement_pct": round(improvement_pct * 100, 1),
            }
            total_improvement += improvement_pct

    aggregate_improvement = total_improvement / sections_with_data if sections_with_data > 0 else 0.0

    return {
        "should_suggest": aggregate_improvement > SUGGESTION_IMPROVEMENT_THRESHOLD,
        "improvements": improvements,
        "aggregate_improvement_pct": round(aggregate_improvement, 4),
    }


def generate_suggestion(proposed_calibration, current_calibration, improvements):
    """Write a timestamped suggestion file for human review.

    Filenames are unique: calibration-suggestion-YYYY-MM-DD.json (appends -N if same-day exists).
    Returns the suggestion file path.
    """
    SUGGESTION_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")

    # Find unique filename (never overwrite)
    base_name = f"calibration-suggestion-{date_str}"
    suggestion_path = SUGGESTION_DIR / f"{base_name}.json"
    counter = 2
    while suggestion_path.exists():
        suggestion_path = SUGGESTION_DIR / f"{base_name}-{counter}.json"
        counter += 1

    # Build trigger summary from improvements
    trigger_parts = []
    for section, imp in improvements.items():
        if section == "first_calibration":
            trigger_parts.append("First calibration -- no previous params")
            continue
        trigger_parts.append(
            f"{section} Brier improved {imp['improvement_pct']:.0f}% "
            f"({imp['before']:.4f} -> {imp['after']:.4f})"
        )
    trigger = "; ".join(trigger_parts) if trigger_parts else "Improvement detected"

    suggestion = {
        "generated_at": now.isoformat(),
        "trigger": trigger,
        "current_calibration": current_calibration,
        "proposed_calibration": proposed_calibration,
        "improvements": improvements,
        "recommendation": (
            f"Apply: {trigger}. "
            f"Run: python3 scripts/calibration-pipeline.py "
            f"--apply-suggestion {suggestion_path}"
        ),
    }

    _atomic_write_json(suggestion_path, suggestion)
    log.info(f"Calibration suggestion generated: {suggestion_path}")
    return suggestion_path


def archive_calibration(calibration_data, history_dir=None):
    """Save a versioned copy of calibration to the history directory.

    Filenames: calibration-YYYY-MM-DD.json (appends -N if same-day exists).
    Returns the path of the saved file.
    """
    hdir = history_dir or HISTORY_DIR
    hdir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    base_name = f"calibration-{date_str}"
    path = hdir / f"{base_name}.json"
    counter = 2
    while path.exists():
        path = hdir / f"{base_name}-{counter}.json"
        counter += 1

    archive_entry = {
        "archived_at": now.isoformat(),
        "calibration": calibration_data,
    }
    _atomic_write_json(path, archive_entry)
    log.info(f"Calibration archived: {path}")
    return path


def apply_suggestion(suggestion_path):
    """Apply a calibration suggestion file.

    Archives current calibration to history, backs up to calibration-backup.json,
    writes proposed calibration, then re-snapshots baselines.
    Returns True on success.
    """
    suggestion_path = Path(suggestion_path)
    if not suggestion_path.exists():
        log.error(f"Suggestion file not found: {suggestion_path}")
        return False

    try:
        suggestion = json.loads(suggestion_path.read_text())
    except Exception as e:
        log.error(f"Failed to parse suggestion file: {e}")
        return False

    proposed = suggestion.get("proposed_calibration")
    if not proposed:
        log.error("Suggestion file missing proposed_calibration")
        return False

    # Archive current calibration before overwriting
    if CALIBRATION_PATH.exists():
        try:
            current_cal = json.loads(CALIBRATION_PATH.read_text())
            archive_calibration(current_cal)
        except Exception as e:
            log.warning(f"Could not archive current calibration: {e}")
        shutil.copy2(str(CALIBRATION_PATH), str(CALIBRATION_BACKUP_PATH))
        log.info(f"Backed up current calibration to {CALIBRATION_BACKUP_PATH}")

    # Write proposed calibration
    _atomic_write_json(CALIBRATION_PATH, proposed)
    log.info(f"Applied proposed calibration to {CALIBRATION_PATH}")

    # Log which sections changed
    current = suggestion.get("current_calibration", {})
    for section in CALIBRATION_SECTIONS:
        cur_brier = current.get(section, {}).get("global_brier")
        new_brier = proposed.get(section, {}).get("global_brier")
        if cur_brier is not None and new_brier is not None and cur_brier != new_brier:
            log.info(f"  {section}: Brier {cur_brier:.4f} -> {new_brier:.4f}")

    # Re-snapshot baselines from current backtest results
    if RESULTS_PATH.exists():
        try:
            backtest_results = json.loads(RESULTS_PATH.read_text())
            initialize_baselines(backtest_results)
            log.info("Baselines re-initialized after applying suggestion")
        except Exception as e:
            log.warning(f"Could not re-initialize baselines: {e}")
    else:
        log.warning("No backtest results found -- baselines not updated")

    return True


# ── Pipeline Log ─────────────────────────────────────────────────────────────

def write_pipeline_log(run_at, stage_results, drift_findings, whatsapp_sent, total_duration,
                       suggestion_generated=False, suggestion_path=None):
    """Write detailed pipeline log to data/logs/calibration/pipeline-YYYY-MM-DD.json."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    date_str = run_at.strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"pipeline-{date_str}.json"

    log_data = {
        "run_at": run_at.isoformat(),
        "total_duration_s": round(total_duration, 1),
        "whatsapp_sent": whatsapp_sent,
        "suggestion_generated": suggestion_generated,
        "suggestion_path": str(suggestion_path) if suggestion_path else None,
        "stages": {},
        "drift_findings": drift_findings,
    }

    # Include stage results (truncate long stdout/stderr for log)
    for name, result in stage_results.items():
        log_data["stages"][name] = {
            "success": result["success"],
            "duration_s": result["duration_s"],
            "stdout_lines": len(result["stdout"].splitlines()) if result["stdout"] else 0,
            "stderr_preview": result["stderr"][:500] if result["stderr"] else "",
        }

    _atomic_write_json(log_path, log_data)
    log.info(f"Pipeline log written: {log_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Daily calibration pipeline")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run pipeline but don't send WhatsApp")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Re-snapshot baselines from current results")
    parser.add_argument("--apply-suggestion", type=str, metavar="PATH",
                        help="Apply a calibration suggestion file")
    parser.add_argument("--auto-apply", action="store_true",
                        help="Auto-apply calibration if regression gate passes (for weekly cron)")
    args = parser.parse_args()

    setup_unbuffered()

    # ── Apply suggestion (separate flow) ────────────────────────────────────
    if args.apply_suggestion:
        success = apply_suggestion(args.apply_suggestion)
        if success:
            log.info("Suggestion applied successfully")
            print(f"Applied calibration suggestion from {args.apply_suggestion}")
            print(f"Backup saved to {CALIBRATION_BACKUP_PATH}")
        else:
            log.error("Failed to apply suggestion")
            sys.exit(1)
        return

    # ── Normal pipeline flow ────────────────────────────────────────────────
    run_at = datetime.now(timezone.utc)
    pipeline_start = time.monotonic()

    log.info("=" * 60)
    log.info("Calibration pipeline starting")
    log.info("=" * 60)

    # 1. Run all stages
    pipeline_result = run_pipeline()
    stage_results = pipeline_result["stages"]
    proposed_calibration = pipeline_result["proposed_calibration"]
    any_stage_failed = any(not s["success"] for s in stage_results.values())

    # 2. Load backtest results (should be updated by backtest stage)
    if RESULTS_PATH.exists():
        try:
            backtest_results = json.loads(RESULTS_PATH.read_text())
        except Exception as e:
            log.error(f"Failed to load backtest results: {e}")
            backtest_results = {}
    else:
        log.warning("No backtest results file found")
        backtest_results = {}

    # 3. Load or initialize baselines
    baselines = load_baselines()
    if baselines is None and backtest_results:
        log.info("No baselines found -- initializing from current results")
        baselines = initialize_baselines(backtest_results)
    elif baselines is None:
        log.warning("No baselines and no backtest results -- drift detection skipped")
        baselines = {}

    # 4. Drift detection
    if baselines and backtest_results:
        drift_findings = check_drift(baselines, backtest_results)
    else:
        drift_findings = []

    drifted_count = sum(1 for f in drift_findings if f["drifted"])
    if drifted_count:
        log.warning(f"Drift detected in {drifted_count} entity(ies)")
    else:
        log.info("No drift detected")

    # 5. Update baselines if requested
    if args.update_baseline and backtest_results:
        log.info("Updating baselines (--update-baseline flag)")
        initialize_baselines(backtest_results)

    # 6. Evaluate calibration suggestion
    suggestion_generated = False
    suggestion_path = None

    if proposed_calibration:
        # Load current calibration for comparison
        current_calibration = {}
        if CALIBRATION_PATH.exists():
            try:
                current_calibration = json.loads(CALIBRATION_PATH.read_text())
            except Exception as e:
                log.warning(f"Could not load current calibration: {e}")

        eval_result = evaluate_suggestion(proposed_calibration, current_calibration)
        log.info(f"Suggestion evaluation: should_suggest={eval_result['should_suggest']}, "
                 f"improvement={eval_result['aggregate_improvement_pct']:.1%}")

        if eval_result["should_suggest"]:
            suggestion_path = generate_suggestion(
                proposed_calibration, current_calibration, eval_result["improvements"]
            )
            suggestion_generated = True
    else:
        log.info("No proposed calibration available -- suggestion evaluation skipped")
        eval_result = {}

    # 6b. Auto-apply (weekly mode)
    auto_applied = False
    apply_reason = ""
    if args.auto_apply and suggestion_generated and proposed_calibration:
        apply_decision, apply_reason = should_auto_apply(
            backtest_results, backtest_results, eval_result
        )
        if apply_decision:
            log.info(f"Auto-apply: {apply_reason}")
            success = apply_suggestion(str(suggestion_path))
            if success:
                auto_applied = True
                log.info("Auto-applied calibration suggestion")
            else:
                apply_reason = "apply_suggestion() failed"
                log.error("Auto-apply failed during apply_suggestion()")
        else:
            log.info(f"Auto-apply skipped: {apply_reason}")
    elif args.auto_apply:
        apply_reason = "no suggestion generated"
        log.info("Auto-apply skipped: no suggestion generated")

    # 7. WhatsApp summary
    if args.auto_apply:
        summary = format_weekly_summary(
            stage_results, drift_findings,
            pipeline_result.get("all_calibrations", {}),
            auto_applied, apply_reason if not auto_applied else "applied",
            suggestion_path=suggestion_path,
        )
    else:
        summary = format_whatsapp_summary(
            stage_results, drift_findings, any_stage_failed,
            suggestion_path=suggestion_path,
        )
    whatsapp_sent = False

    if args.dry_run:
        log.info("Dry run -- WhatsApp suppressed")
    else:
        try:
            notify_whatsapp(summary, logger=log)
            whatsapp_sent = True
        except Exception as e:
            log.error(f"WhatsApp send failed: {e}")

    # 8. Write pipeline log
    total_duration = time.monotonic() - pipeline_start
    write_pipeline_log(
        run_at, stage_results, drift_findings, whatsapp_sent, total_duration,
        suggestion_generated=suggestion_generated, suggestion_path=suggestion_path,
    )

    # 9. Print summary
    log.info("")
    log.info("=" * 60)
    log.info("Pipeline Summary")
    log.info("=" * 60)
    print(summary)
    log.info(f"Total duration: {total_duration:.1f}s")
    log.info("Pipeline complete")


# Module-level logger (after function definitions, before __main__)
log = setup_logging("calibration-pipeline")

if __name__ == "__main__":
    main()
