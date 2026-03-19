#!/usr/bin/env python3
"""Export winning parameters to kalshi-trading Oracle config.

Reads the current experiment.py parameters (which the research agent has
optimized) and writes them as a JSON config that Oracle can load at startup,
overriding its hardcoded defaults.

Optionally reads results.tsv to embed the best backtest metrics in the
export for audit trail.

Usage:
    python -m export.export_calibration
    python -m export.export_calibration --output ../kalshi-trading/config/oracle-calibration.json
    python -m export.export_calibration --dry-run
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_OUTPUT = "../../config/oracle-calibration.json"

BOOK_B_PARAMS = [
    "MATCHUP_MULTIPLIER",
    "MATCHUP_CAP",
    "VENUE_MULTIPLIER",
    "VENUE_CAP",
    "B2B_PENALTY",
    "RECENCY_WEIGHT_LAST5",
    "RECENCY_WEIGHT_LAST10",
    "RECENCY_WEIGHT_SEASON",
    "FALLBACK_SIGMA",
    "USE_STAT_SPACE",
]

BOOK_C_PARAMS = [
    "FOUL_TROUBLE_PROB",
    "FOUL_TROUBLE_MINUTES_REDUCTION",
    "BLOWOUT_PROB",
    "BLOWOUT_MINUTES_THRESHOLD",
    "BLOWOUT_MINUTES_REDUCTION",
    "OT_MINUTES",
    "OT_MARGIN_FACTOR_TIED",
    "OT_MARGIN_FACTOR_CLOSE",
    "OT_CLOCK_DECAY",
    "OT_BASE_MULTIPLIER",
    "OT_MAX_PROB",
    "OT_OVER_PROB_BASE",
    "OT_OVER_PROB_MULTIPLIER",
    "OT_OVER_PROB_MIN",
    "OT_OVER_PROB_MAX",
    "OT_NO_OT_PROB_BASE",
    "OT_NO_OT_PROB_MULTIPLIER",
    "OT_NO_OT_PROB_MIN",
    "OT_NO_OT_PROB_MAX",
    "OT_SAFETY_MARGIN",
    "OT_GAP_THRESHOLD",
]

EDGE_PARAMS = ["BOOK_A_MIN_EDGE", "BOOK_B_MIN_EDGE", "BOOK_C_MIN_EDGE"]
SIZE_PARAMS = ["BOOK_A_SIZE_PCT", "BOOK_B_SIZE_PCT", "BOOK_C_SIZE_PCT"]


def load_experiment(path: Path) -> dict:
    """Load parameter values from experiment.py via importlib."""
    spec = importlib.util.spec_from_file_location("experiment", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module spec from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    params = {}
    for key in BOOK_B_PARAMS + BOOK_C_PARAMS + EDGE_PARAMS + SIZE_PARAMS:
        if hasattr(mod, key):
            params[key] = getattr(mod, key)
    return params


def load_best_result(results_path: Path) -> dict | None:
    """Read results.tsv and find the row with best brier_score."""
    if not results_path.exists():
        return None

    lines = results_path.read_text().strip().split("\n")
    if len(lines) < 2:
        return None

    header = lines[0].split("\t")
    brier_idx = header.index("brier_score") if "brier_score" in header else 1

    # Find status/description column indices for filtering
    status_idx = header.index("status") if "status" in header else -1
    desc_idx = header.index("description") if "description" in header else -1
    # Legacy format: params_changed column
    pc_idx = header.index("params_changed") if "params_changed" in header else -1

    best_score = float("inf")
    best_row = None
    for line in lines[1:]:
        cols = line.split("\t")
        # Skip baseline rows (would pair default metrics with optimized params)
        if pc_idx >= 0 and len(cols) > pc_idx and cols[pc_idx].strip() == "(baseline)":
            continue
        if desc_idx >= 0 and len(cols) > desc_idx and cols[desc_idx].strip() == "baseline":
            continue
        try:
            score = float(cols[brier_idx])
            if score < best_score:
                best_score = score
                best_row = dict(zip(header, cols))
        except (ValueError, IndexError):
            continue

    return best_row


def main():
    parser = argparse.ArgumentParser(
        description="Export calibrated params to kalshi-trading"
    )
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument("--experiment", "-e", default="experiment.py")
    parser.add_argument("--results", "-r", default="results.tsv")
    parser.add_argument("--dry-run", action="store_true", help="Print but don't write")
    args = parser.parse_args()

    root = Path(__file__).parent.parent
    experiment_path = root / args.experiment
    results_path = root / args.results
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = root / output_path

    if not experiment_path.exists():
        print(f"ERROR: {experiment_path} not found", file=sys.stderr)
        sys.exit(1)

    params = load_experiment(experiment_path)
    best = load_best_result(results_path)

    calibration = {
        "version": 1,
        "source": "sportsmarket/autoresearch",
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "book_b": {k: params[k] for k in BOOK_B_PARAMS if k in params},
        "book_c": {k: params[k] for k in BOOK_C_PARAMS if k in params},
        "edge_thresholds": {k: params[k] for k in EDGE_PARAMS if k in params},
        "sizing": {k: params[k] for k in SIZE_PARAMS if k in params},
    }

    if best:
        calibration["backtest_metrics"] = {
            "brier_score": float(best.get("brier_score", 0)),
            "calibration_error": float(best.get("calibration_error", 0)),
            "expected_profit_pct": float(best.get("expected_profit_pct", 0)),
            "sample_size": int(best.get("sample_size", 0)),
            "timestamp": best.get("timestamp", ""),
        }

    output_json = json.dumps(calibration, indent=2)

    if args.dry_run:
        print(output_json)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_json + "\n")
    print(f"Exported calibration to: {output_path}")
    if best:
        print(f"Best brier_score: {best.get('brier_score', 'N/A')}")
        print(f"Best expected_profit: {best.get('expected_profit_pct', 'N/A')}%")


if __name__ == "__main__":
    main()
