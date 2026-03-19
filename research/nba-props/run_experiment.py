#!/usr/bin/env python3
"""Sports Research Experiment Harness — DO NOT MODIFY.

Loads parameters from experiment.py, runs the Book B backtest, and reports
metrics in autoresearch-compatible format. Results are auto-appended to
results.tsv for experiment tracking (including crashes).

Usage:
    python run_experiment.py                          # Standard run
    python run_experiment.py --verbose                # Include per-stat breakdown
    python run_experiment.py --baseline               # Run with default params
    python run_experiment.py --desc "try higher matchup"  # Log hypothesis
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure sportsmarket root is on path
sys.path.insert(0, str(Path(__file__).parent))

from analysis.backtester import backtest_book_b
from analysis.metrics import bootstrap_brier_ci

# Default parameter values — matches experiment.py's initial state.
# Used to detect which params the agent changed.
DEFAULTS = {
    "MATCHUP_MULTIPLIER": 0.15,
    "MATCHUP_CAP": 0.10,
    "VENUE_MULTIPLIER": 0.10,
    "VENUE_CAP": 0.05,
    "B2B_PENALTY": -0.05,
    "RECENCY_WEIGHT_LAST5": 2.0,
    "RECENCY_WEIGHT_LAST10": 1.5,
    "RECENCY_WEIGHT_SEASON": 1.0,
    "FALLBACK_SIGMA": 0.15,
    "USE_STAT_SPACE": False,
    "FOUL_TROUBLE_PROB": 0.25,
    "FOUL_TROUBLE_MINUTES_REDUCTION": 0.70,
    "BLOWOUT_PROB": 0.15,
    "BLOWOUT_MINUTES_THRESHOLD": 10.0,
    "BLOWOUT_MINUTES_REDUCTION": 0.50,
    "OT_MINUTES": 5.0,
    "OT_MARGIN_FACTOR_TIED": 1.0,
    "OT_MARGIN_FACTOR_CLOSE": 0.55,
    "OT_CLOCK_DECAY": 200.0,
    "OT_BASE_MULTIPLIER": 0.80,
    "OT_MAX_PROB": 0.85,
    "OT_OVER_PROB_BASE": 0.50,
    "OT_OVER_PROB_MULTIPLIER": 2.0,
    "OT_OVER_PROB_MIN": 0.40,
    "OT_OVER_PROB_MAX": 0.90,
    "OT_NO_OT_PROB_BASE": 0.30,
    "OT_NO_OT_PROB_MULTIPLIER": 2.0,
    "OT_NO_OT_PROB_MIN": 0.10,
    "OT_NO_OT_PROB_MAX": 0.80,
    "OT_SAFETY_MARGIN": 1.05,
    "OT_GAP_THRESHOLD": 0.30,
    "BOOK_A_MIN_EDGE": 0.15,
    "BOOK_B_MIN_EDGE": 0.10,
    "BOOK_C_MIN_EDGE": 0.12,
    "BOOK_A_SIZE_PCT": 0.02,
    "BOOK_B_SIZE_PCT": 0.015,
    "BOOK_C_SIZE_PCT": 0.01,
}

RESULTS_HEADER = (
    "commit\tbrier_score\tcalibration_error\texpected_profit_pct\t"
    "hit_rate\tsample_size\ttrades_taken\tbrier_95ci\tstatus\tdescription\n"
)


def get_git_commit() -> str:
    """Get current git commit short hash (7 chars), or 'nocommit' if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(Path(__file__).parent),
        )
        if result.returncode == 0:
            return result.stdout.strip()[:7]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "nocommit"


def load_experiment_params() -> dict:
    """Load current parameters from experiment.py (force reimport)."""
    if "experiment" in sys.modules:
        del sys.modules["experiment"]

    import experiment

    params = {}
    for key in DEFAULTS:
        params[key] = getattr(experiment, key, DEFAULTS[key])
    return params


def find_changed_params(params: dict) -> list[str]:
    """Find which params differ from defaults."""
    return [k for k, v in DEFAULTS.items() if params.get(k) != v]


def append_results(
    commit: str,
    results: dict,
    brier_ci: str,
    status: str,
    description: str,
) -> None:
    """Auto-append to results.tsv. Logs ALL experiments including crashes."""
    results_file = Path(__file__).parent / "results.tsv"
    if not results_file.exists():
        results_file.write_text(RESULTS_HEADER)

    # Sanitize description (remove tabs/newlines)
    desc = description.replace("\t", " ").replace("\n", " ").strip()

    line = (
        f"{commit}\t"
        f"{results.get('brier_score', -1):.4f}\t"
        f"{results.get('calibration_error', -1):.4f}\t"
        f"{results.get('expected_profit_pct', 0):.1f}\t"
        f"{results.get('hit_rate', 0):.3f}\t"
        f"{results.get('sample_size', 0)}\t"
        f"{results.get('trades_taken', 0)}\t"
        f"{brier_ci}\t"
        f"{status}\t"
        f"{desc}\n"
    )
    with open(results_file, "a") as f:
        f.write(line)


def main():
    parser = argparse.ArgumentParser(description="Run sports research experiment")
    parser.add_argument("--verbose", "-v", action="store_true", help="Per-stat breakdown")
    parser.add_argument("--baseline", action="store_true", help="Use default params")
    parser.add_argument("--no-log", action="store_true", help="Don't append to results.tsv")
    parser.add_argument("--desc", "-d", default="", help="Experiment description/hypothesis")
    args = parser.parse_args()

    commit = get_git_commit()

    if args.baseline:
        params = dict(DEFAULTS)
        description = args.desc or "baseline"
    else:
        params = load_experiment_params()
        description = args.desc

    # Defense-in-depth: inject locked eval-scope params into the dict.
    # These are ALSO hardcoded in backtester.py, but git operations by the
    # research agent can revert backtester.py. This injection ensures the
    # harness always produces consistent results regardless of backtester state.
    params["TEST_LINE_OFFSETS"] = [-2, 0, 2]
    params["USE_EXTENDED_STATS"] = True

    changed = find_changed_params(params)
    if not description and changed:
        description = ",".join(changed)
    elif not description:
        description = "(baseline)"

    start = time.time()
    try:
        results = backtest_book_b(params)

        # Safety check: sample_size must be consistent (eval scope is locked).
        # If it deviates, the backtester lock was reverted by a git operation.
        expected_min_samples = 2000  # with 42 players + 9 stats + 3 offsets, expect ~8000+
        if results["sample_size"] < expected_min_samples:
            print(
                f"WARNING: sample_size {results['sample_size']} < {expected_min_samples}. "
                f"Eval scope may be unlocked. Check analysis/backtester.py.",
                file=sys.stderr,
            )

        status = "run"
    except Exception as e:
        results = {"brier_score": -1, "calibration_error": -1, "expected_profit_pct": 0,
                    "hit_rate": 0, "sample_size": 0, "trades_taken": 0}
        status = "crash"
        print(f"CRASH: {e}", file=sys.stderr)
        if not args.no_log:
            append_results(commit, results, "N/A", "crash", f"CRASH: {e}")
        sys.exit(1)
    elapsed = time.time() - start

    # Bootstrap 95% CI for Brier score
    brier_lo, brier_hi = bootstrap_brier_ci(
        results.get("_predictions", []),
        results.get("_outcomes", []),
    )
    brier_ci = f"{brier_lo:.4f}-{brier_hi:.4f}" if brier_lo is not None else "N/A"

    # Print metrics in autoresearch format (parseable by grep)
    print("---")
    print(f"brier_score:         {results['brier_score']:.4f}")
    print(f"brier_95ci:          {brier_ci}")
    print(f"calibration_error:   {results['calibration_error']:.4f}")
    print(f"expected_profit_pct: {results['expected_profit_pct']:.1f}")
    print(f"hit_rate:            {results['hit_rate']:.3f}")
    print(f"sample_size:         {results['sample_size']}")
    print(f"trades_taken:        {results['trades_taken']}")
    print(f"elapsed_seconds:     {elapsed:.1f}")
    print(f"commit:              {commit}")
    print(f"use_stat_space:      {params.get('USE_STAT_SPACE', False)}")
    if changed:
        print(f"params_changed:      {', '.join(changed)}")
    else:
        print("params_changed:      (baseline)")
    print("---")

    if args.verbose and results.get("per_stat"):
        print("\nPer-stat breakdown:")
        print(f"  {'stat':>25}  {'brier':>7}  {'hit':>5}  {'profit%':>8}  {'n':>5}")
        print(f"  {'-'*25}  {'-'*7}  {'-'*5}  {'-'*8}  {'-'*5}")
        for stat, data in sorted(results["per_stat"].items()):
            print(
                f"  {stat:>25}  {data['brier_score']:.4f}  "
                f"{data['hit_rate']:.3f}  "
                f"{data['expected_profit_pct']:>7.1f}%  "
                f"{data['sample_size']:>5}"
            )

    if not args.no_log:
        append_results(commit, results, brier_ci, status, description)


if __name__ == "__main__":
    main()
