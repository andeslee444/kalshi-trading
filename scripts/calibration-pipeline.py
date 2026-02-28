#!/usr/bin/env python3
"""Daily calibration pipeline orchestrator.

Runs reconcile -> backfill -> backtest -> calibrate as subprocesses,
manages per-bot/per-city Brier score baselines, detects drift,
writes timestamped logs, and sends daily WhatsApp summaries.

Usage:
    python3 scripts/calibration-pipeline.py              # Full run + WhatsApp
    python3 scripts/calibration-pipeline.py --dry-run     # Run stages, no WhatsApp
    python3 scripts/calibration-pipeline.py --update-baseline  # Re-snapshot baselines
"""

import argparse
import json
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

# ── Constants ────────────────────────────────────────────────────────────────
DRIFT_THRESHOLD = 0.10   # 10% degradation triggers drift alert
MIN_SAMPLES = 10         # minimum trades before drift detection activates

# (name, script_path, args, timeout_seconds)
STAGES = [
    ("reconcile", SCRIPTS_DIR / "reconcile-trades.py", [], 120),
    ("backfill",  SCRIPTS_DIR / "backfill-settlements.py", [], 180),
    ("backtest",  SCRIPTS_DIR / "backtest.py", ["--save"], 120),
    ("calibrate", SCRIPTS_DIR / "calibrate-sigma.py", ["--json"], 300),
]


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


def run_pipeline():
    """Run all pipeline stages sequentially.

    Returns dict with stages results and proposed_calibration (if available).
    """
    stages = {}
    proposed_calibration = None

    for name, script, args, timeout in STAGES:
        log.info(f"Running stage: {name}")
        result = run_stage(name, script, args, timeout)
        stages[name] = result

        status = "OK" if result["success"] else "FAILED"
        log.info(f"  {name}: {status} ({result['duration_s']}s)")
        if not result["success"]:
            log.warning(f"  {name} stderr: {result['stderr'][:300]}")

        # Parse calibrate stage JSON output
        if name == "calibrate" and result["success"] and result["stdout"].strip():
            try:
                proposed_calibration = json.loads(result["stdout"])
            except (json.JSONDecodeError, ValueError):
                log.warning("Could not parse calibrate JSON output")

    return {"stages": stages, "proposed_calibration": proposed_calibration}


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


# ── WhatsApp Summary ─────────────────────────────────────────────────────────

def format_whatsapp_summary(stage_results, drift_findings, any_stage_failed):
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

    return "\n".join(lines)


# ── Pipeline Log ─────────────────────────────────────────────────────────────

def write_pipeline_log(run_at, stage_results, drift_findings, whatsapp_sent, total_duration):
    """Write detailed pipeline log to data/logs/calibration/pipeline-YYYY-MM-DD.json."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    date_str = run_at.strftime("%Y-%m-%d")
    log_path = LOG_DIR / f"pipeline-{date_str}.json"

    log_data = {
        "run_at": run_at.isoformat(),
        "total_duration_s": round(total_duration, 1),
        "whatsapp_sent": whatsapp_sent,
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
    args = parser.parse_args()

    setup_unbuffered()
    run_at = datetime.now(timezone.utc)
    pipeline_start = time.monotonic()

    log.info("=" * 60)
    log.info("Calibration pipeline starting")
    log.info("=" * 60)

    # 1. Run all stages
    pipeline_result = run_pipeline()
    stage_results = pipeline_result["stages"]
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

    # 6. WhatsApp summary
    summary = format_whatsapp_summary(stage_results, drift_findings, any_stage_failed)
    whatsapp_sent = False

    if args.dry_run:
        log.info("Dry run -- WhatsApp suppressed")
    else:
        try:
            notify_whatsapp(summary, logger=log)
            whatsapp_sent = True
        except Exception as e:
            log.error(f"WhatsApp send failed: {e}")

    # 7. Write pipeline log
    total_duration = time.monotonic() - pipeline_start
    write_pipeline_log(run_at, stage_results, drift_findings, whatsapp_sent, total_duration)

    # 8. Print summary
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
