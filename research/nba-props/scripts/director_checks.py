#!/usr/bin/env python3
"""Research Director — automated statistical analysis of experiment results.

Pure Python checks for convergence, metric gaming, data sufficiency, and
CI overlap. No LLM calls. Reads results.tsv and writes assessments to
data/director-state.json and data/director-log.json.

Usage:
    python3 scripts/director_checks.py                # Full analysis
    python3 scripts/director_checks.py --json          # JSON output only
    python3 scripts/director_checks.py --last 20       # Analyze last 20 experiments
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_TSV = PROJECT_ROOT / "results.tsv"
STATE_PATH = PROJECT_ROOT / "data" / "director-state.json"
LOG_PATH = PROJECT_ROOT / "data" / "director-log.json"
GAMELOGS_DIR = PROJECT_ROOT / "data" / "processed" / "gamelogs"


# ── Results parsing ──────────────────────────────────────────────────


def parse_results_tsv(path: Path = RESULTS_TSV) -> list[dict]:
    """Parse results.tsv into list of dicts."""
    if not path.exists():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    lines = text.split("\n")
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        cols = line.split("\t")
        row = {}
        for i, h in enumerate(header):
            row[h] = cols[i] if i < len(cols) else ""
        # Parse numeric fields
        for field in ("brier_score", "calibration_error", "expected_profit_pct", "hit_rate"):
            try:
                row[field] = float(row.get(field, -1))
            except (ValueError, TypeError):
                row[field] = -1.0
        for field in ("sample_size", "trades_taken"):
            try:
                row[field] = int(row.get(field, 0))
            except (ValueError, TypeError):
                row[field] = 0
        rows.append(row)
    return rows


# ── Bootstrap CI ─────────────────────────────────────────────────────


def approximate_brier_ci(
    brier: float,
    sample_size: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Approximate CI for a Brier score using normal approximation.

    For a mean of squared errors over N independent samples, the standard
    error is approximately sqrt(var(squared_errors) / N). For calibrated
    predictions, the variance of (p-o)^2 can be bounded by the Brier score
    itself: Var ~ B*(1-B) where B is the Brier score (analogous to
    Bernoulli variance). This gives a conservative CI.

    For z_{0.975} = 1.96, the 95% CI is: B ± 1.96 * sqrt(B*(1-B)/N).
    """
    if sample_size < 10 or brier < 0:
        return 0.0, 1.0

    # Clamp brier to valid range for variance computation
    b = max(0.001, min(0.999, brier))
    n = sample_size

    # Standard error of the Brier score
    se = math.sqrt(b * (1 - b) / n)

    # z-score for the confidence level
    # For 95% CI, z = 1.96. For others, use the inverse normal approximation.
    alpha = (1 - confidence) / 2
    # Rational approximation to inverse normal CDF (Abramowitz & Stegun 26.2.23)
    t = math.sqrt(-2.0 * math.log(alpha))
    z = t - (2.515517 + 0.802853 * t + 0.010328 * t * t) / (
        1.0 + 1.432788 * t + 0.189269 * t * t + 0.001308 * t * t * t
    )

    lo = max(0.0, brier - z * se)
    hi = min(1.0, brier + z * se)
    return lo, hi


# ── Statistical checks ───────────────────────────────────────────────


def check_convergence(rows: list[dict], window: int = 15) -> dict:
    """Check if best Brier hasn't improved in `window` experiments.

    Returns dict with converged (bool), experiments_since_improvement, best_brier.
    """
    valid = [r for r in rows if r["brier_score"] > 0 and r.get("status") != "crash"]
    if len(valid) < window:
        return {
            "converged": False,
            "experiments_since_improvement": 0,
            "best_brier": valid[-1]["brier_score"] if valid else None,
            "total_experiments": len(valid),
            "message": f"Too few experiments ({len(valid)}) to assess convergence (need {window})",
        }

    best_overall = min(r["brier_score"] for r in valid)
    # Find when we last improved
    running_best = float("inf")
    last_improvement_idx = 0
    for i, r in enumerate(valid):
        if r["brier_score"] < running_best:
            running_best = r["brier_score"]
            last_improvement_idx = i

    experiments_since = len(valid) - 1 - last_improvement_idx

    return {
        "converged": experiments_since >= window,
        "experiments_since_improvement": experiments_since,
        "best_brier": best_overall,
        "total_experiments": len(valid),
        "message": (
            f"CONVERGED: No improvement in {experiments_since} experiments"
            if experiments_since >= window
            else f"Last improvement {experiments_since} experiments ago (threshold: {window})"
        ),
    }


def check_metric_gaming(rows: list[dict]) -> dict:
    """Detect eval-scope manipulation (sample_size changes between experiments).

    With locked eval params, sample_size should be constant. Only checks
    the most recent contiguous run of experiments — ignores v1 history
    where sample_size legitimately varied before eval-scope locking.
    """
    valid = [r for r in rows if r["sample_size"] > 0 and r.get("status") != "crash"]
    if len(valid) < 2:
        return {"gaming_detected": False, "message": "Too few experiments to check"}

    # Find the most recent contiguous run with consistent sample_size.
    # Walk backward from the latest experiment: as long as sample_size
    # matches the latest, include it. Stop at the first mismatch.
    latest_size = valid[-1]["sample_size"]
    recent_run_start = len(valid) - 1
    for i in range(len(valid) - 2, -1, -1):
        if valid[i]["sample_size"] == latest_size:
            recent_run_start = i
        else:
            break

    recent_run = valid[recent_run_start:]
    pre_v2_count = recent_run_start

    # Within the recent run, check for any deviations (should be zero)
    sample_sizes = [r["sample_size"] for r in recent_run]
    mode_size = latest_size
    deviations = [
        {"index": i + recent_run_start, "sample_size": s, "description": recent_run[i].get("description", "")}
        for i, s in enumerate(sample_sizes)
        if s != mode_size
    ]

    # Check for sudden Brier drops (>0.05 in one experiment) within recent run
    sudden_drops = []
    for i in range(1, len(recent_run)):
        prev = recent_run[i - 1]["brier_score"]
        curr = recent_run[i]["brier_score"]
        if prev - curr > 0.05:
            sudden_drops.append({
                "index": i + recent_run_start,
                "prev_brier": prev,
                "curr_brier": curr,
                "drop": prev - curr,
                "description": recent_run[i].get("description", ""),
            })

    gaming = bool(deviations)

    msg_parts = []
    if gaming:
        msg_parts.append(f"ALERT: Sample size changed in {len(deviations)} experiments (expected {mode_size})")
    else:
        msg_parts.append(f"Clean: sample_size stable at {mode_size} ({len(recent_run)} experiments)")
    if pre_v2_count > 0:
        msg_parts.append(f"Ignored {pre_v2_count} pre-v2 experiments with different sample sizes")

    return {
        "gaming_detected": gaming,
        "expected_sample_size": mode_size,
        "recent_run_size": len(recent_run),
        "pre_v2_ignored": pre_v2_count,
        "sample_size_deviations": deviations,
        "sudden_brier_drops": sudden_drops,
        "message": ". ".join(msg_parts),
    }


def check_ci_overlap(rows: list[dict], last_n: int = 5) -> dict:
    """Check if recent kept experiments have significantly different Brier from baseline.

    Computes approximate CIs and checks for overlap with the baseline.
    """
    valid = [r for r in rows if r["brier_score"] > 0 and r.get("status") != "crash"]
    if not valid:
        return {"significant": False, "message": "No valid experiments"}

    # Find baseline (first run or lowest Brier with "(baseline)" description)
    baseline = None
    for r in valid:
        desc = r.get("description", "")
        if "baseline" in desc.lower():
            baseline = r
            break
    if baseline is None:
        baseline = valid[0]

    baseline_brier = baseline["brier_score"]
    baseline_n = baseline["sample_size"]
    bl_lo, bl_hi = approximate_brier_ci(baseline_brier, baseline_n)

    # Recent experiments
    recent = valid[-last_n:]
    best_recent = min(recent, key=lambda r: r["brier_score"])
    br_brier = best_recent["brier_score"]
    br_n = best_recent["sample_size"]
    br_lo, br_hi = approximate_brier_ci(br_brier, br_n)

    # CIs overlap if one's lower bound is below the other's upper bound
    overlaps = br_lo <= bl_hi and bl_lo <= br_hi
    improvement = baseline_brier - br_brier

    return {
        "significant": not overlaps and improvement > 0,
        "baseline_brier": baseline_brier,
        "baseline_ci": [bl_lo, bl_hi],
        "best_recent_brier": br_brier,
        "best_recent_ci": [br_lo, br_hi],
        "improvement": improvement,
        "ci_overlap": overlaps,
        "message": (
            f"Significant improvement: {improvement:.4f} (CIs don't overlap)"
            if not overlaps and improvement > 0
            else f"No significant improvement: CIs overlap (improvement {improvement:.4f})"
        ),
    }


def check_data_sufficiency(
    rows: list[dict], target_ci_width: float = 0.02
) -> dict:
    """Check if we have enough data for the desired statistical precision.

    Estimates required sample size for detectable effect size.
    """
    valid = [r for r in rows if r["brier_score"] > 0 and r.get("status") != "crash"]
    if not valid:
        return {"sufficient": False, "message": "No experiments"}

    latest = valid[-1]
    current_n = latest["sample_size"]
    current_brier = latest["brier_score"]

    # Parse CI from brier_95ci field (format: "0.1234-0.5678")
    ci_str = latest.get("brier_95ci", "")
    ci_width = None
    if "-" in str(ci_str) and ci_str != "N/A":
        parts = ci_str.split("-")
        if len(parts) == 2:
            try:
                ci_lo, ci_hi = float(parts[0]), float(parts[1])
                ci_width = ci_hi - ci_lo
            except ValueError:
                pass

    if ci_width is None:
        # Approximate CI width from bootstrap
        lo, hi = approximate_brier_ci(current_brier, current_n)
        ci_width = hi - lo

    # Estimate required N for target CI width
    # CI width scales as ~1/sqrt(N), so N_required = N_current * (ci_width / target)^2
    if ci_width > 0:
        n_required = int(current_n * (ci_width / target_ci_width) ** 2)
    else:
        n_required = current_n

    # Count current data
    n_players = 0
    n_games = 0
    if GAMELOGS_DIR.exists():
        for f in GAMELOGS_DIR.glob("*.json"):
            if f.name.startswith("."):
                continue
            n_players += 1
            try:
                data = json.loads(f.read_text())
                if isinstance(data, dict):
                    n_games += len(data.get("gamelogs", []))
                elif isinstance(data, list):
                    n_games += len(data)
            except (json.JSONDecodeError, OSError):
                pass

    return {
        "sufficient": ci_width <= target_ci_width,
        "current_ci_width": ci_width,
        "target_ci_width": target_ci_width,
        "current_sample_size": current_n,
        "estimated_n_required": n_required,
        "n_players": n_players,
        "n_games": n_games,
        "message": (
            f"Sufficient: CI width {ci_width:.4f} <= target {target_ci_width}"
            if ci_width <= target_ci_width
            else f"Need more data: CI width {ci_width:.4f} > target {target_ci_width}. "
            f"~{n_required} samples needed (have {current_n}). "
            f"Currently {n_players} players, {n_games} games."
        ),
    }


def compute_recommendation(
    convergence: dict,
    gaming: dict,
    ci_overlap: dict,
    data_sufficiency: dict,
) -> dict:
    """Synthesize checks into a single recommendation."""
    if gaming["gaming_detected"]:
        return {
            "action": "ALERT",
            "reason": "Metric gaming detected — sample size changed between experiments",
            "detail": gaming["message"],
        }

    if convergence["converged"]:
        if ci_overlap["significant"]:
            return {
                "action": "CONVERGED_EXPORT",
                "reason": "Research converged with significant improvement — ready to export",
                "detail": f"Best Brier: {convergence['best_brier']:.4f}, "
                f"improvement: {ci_overlap['improvement']:.4f}",
            }
        else:
            return {
                "action": "CONVERGED_NO_IMPROVEMENT",
                "reason": "Research converged but no significant improvement over baseline",
                "detail": ci_overlap["message"],
            }

    if not data_sufficiency["sufficient"]:
        return {
            "action": "COLLECT_DATA",
            "reason": "CI too wide for meaningful comparison — collect more player data",
            "detail": data_sufficiency["message"],
        }

    return {
        "action": "CONTINUE",
        "reason": "Research is productive — let agent keep running",
        "detail": f"Best Brier: {convergence['best_brier']:.4f}, "
        f"{convergence['experiments_since_improvement']} experiments since last improvement",
    }


# ── State persistence ────────────────────────────────────────────────


def write_state(state: dict) -> None:
    """Write director state to JSON."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def append_log(entry: dict) -> None:
    """Append a decision entry to the director log."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = []
    if LOG_PATH.exists():
        try:
            log = json.loads(LOG_PATH.read_text())
            if not isinstance(log, list):
                log = []
        except (json.JSONDecodeError, OSError):
            log = []
    log.append(entry)
    # Keep last 200 entries
    if len(log) > 200:
        log = log[-200:]
    LOG_PATH.write_text(json.dumps(log, indent=2) + "\n")


# ── Main ─────────────────────────────────────────────────────────────


def _filter_v2_rows(rows: list[dict]) -> list[dict]:
    """Extract the most recent contiguous run with consistent sample_size.

    This filters out v1-era experiments where sample_size varied due to
    eval-scope manipulation. In v2, sample_size is locked and constant.
    """
    valid = [r for r in rows if r["sample_size"] > 0 and r.get("status") != "crash"]
    if not valid:
        return rows

    latest_size = valid[-1]["sample_size"]
    cutoff = len(valid) - 1
    for i in range(len(valid) - 2, -1, -1):
        if valid[i]["sample_size"] == latest_size:
            cutoff = i
        else:
            break

    # Return the original rows (including crashes) from the cutoff point
    if cutoff == 0:
        return rows  # all same size, no filtering needed

    # Find the index in the original rows list that corresponds to cutoff
    valid_idx = 0
    for i, r in enumerate(rows):
        if r["sample_size"] > 0 and r.get("status") != "crash":
            if valid_idx == cutoff:
                return rows[i:]
            valid_idx += 1

    return rows


def run_checks(last_n: int = 0) -> dict:
    """Run all director checks and return full assessment."""
    all_rows = parse_results_tsv()
    if last_n > 0:
        all_rows = all_rows[-last_n:]

    # Gaming check uses ALL rows (to detect v1→v2 boundary)
    gaming = check_metric_gaming(all_rows)

    # All other checks use only v2-era experiments
    v2_rows = _filter_v2_rows(all_rows)
    convergence = check_convergence(v2_rows)
    ci_overlap = check_ci_overlap(v2_rows)
    data_sufficiency = check_data_sufficiency(v2_rows)
    recommendation = compute_recommendation(convergence, gaming, ci_overlap, data_sufficiency)

    state = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "total_experiments": len(all_rows),
        "v2_experiments": len(v2_rows),
        "convergence": convergence,
        "metric_gaming": gaming,
        "ci_overlap": ci_overlap,
        "data_sufficiency": data_sufficiency,
        "recommendation": recommendation,
    }

    return state


def main():
    parser = argparse.ArgumentParser(description="Research Director — statistical analysis")
    parser.add_argument("--json", action="store_true", help="JSON output only")
    parser.add_argument("--last", type=int, default=0, help="Analyze last N experiments only")
    parser.add_argument("--no-save", action="store_true", help="Don't write state/log files")
    args = parser.parse_args()

    state = run_checks(last_n=args.last)

    if not args.no_save:
        write_state(state)
        append_log({
            "timestamp": state["checked_at"],
            "action": state["recommendation"]["action"],
            "reason": state["recommendation"]["reason"],
            "best_brier": state["convergence"].get("best_brier"),
            "total_experiments": state["total_experiments"],
        })

    if args.json:
        print(json.dumps(state, indent=2))
        return

    # Human-readable output
    rec = state["recommendation"]
    conv = state["convergence"]
    data = state["data_sufficiency"]
    ci = state["ci_overlap"]

    print("=" * 60)
    print("RESEARCH DIRECTOR ASSESSMENT")
    print("=" * 60)
    print()
    print(f"  Recommendation:   {rec['action']}")
    print(f"  Reason:           {rec['reason']}")
    print()
    print(f"  Total experiments: {state['total_experiments']}")
    print(f"  Best Brier:        {conv.get('best_brier', 'N/A')}")
    print(f"  Since improvement: {conv.get('experiments_since_improvement', 'N/A')} experiments")
    print(f"  Converged:         {conv.get('converged', False)}")
    print()
    print(f"  CI width:          {data.get('current_ci_width', 'N/A')}")
    print(f"  Data sufficient:   {data.get('sufficient', False)}")
    print(f"  Players:           {data.get('n_players', 0)}")
    print(f"  Games:             {data.get('n_games', 0)}")
    print()
    print(f"  Significant improvement: {ci.get('significant', False)}")
    print(f"  Baseline Brier:    {ci.get('baseline_brier', 'N/A')}")
    print(f"  Best recent:       {ci.get('best_recent_brier', 'N/A')}")
    print()
    print(f"  Metric gaming:     {'ALERT' if state['metric_gaming']['gaming_detected'] else 'Clean'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
