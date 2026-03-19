"""Empirical CDF analysis and probability computation.

Core library functions for building distributions from game logs and
comparing probability-space vs stat-space adjustment approaches.

Usage as library:
    from analysis.empirical_cdf import build_empirical_cdf, compare_approaches

Usage as CLI:
    python -m analysis.empirical_cdf
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional


DATA_DIR = Path(__file__).parent.parent / "data"
GAMELOGS_DIR = DATA_DIR / "processed" / "gamelogs"
DISTRIBUTIONS_DIR = DATA_DIR / "processed" / "distributions"


def build_empirical_cdf(values: list[float], line: float) -> dict:
    """Build empirical CDF from game-by-game stat values.

    Returns dict with distribution stats and hit rates.
    """
    if not values:
        return {"n": 0, "hit_rate": 0.0, "weighted_hit_rate": 0.0}

    sorted_vals = sorted(values)
    n = len(values)
    mean = sum(values) / n
    median = sorted_vals[n // 2]
    variance = sum((v - mean) ** 2 for v in values) / max(n - 1, 1)
    std = math.sqrt(variance)

    hit_rate = sum(1 for v in values if v > line) / n
    w_hr = weighted_hit_rate(values, line)

    return {
        "values": sorted_vals,
        "n": n,
        "mean": round(mean, 2),
        "median": median,
        "std": round(std, 2),
        "hit_rate": round(hit_rate, 4),
        "weighted_hit_rate": round(w_hr, 4),
        "line": line,
    }


def weighted_hit_rate(values: list[float], line: float) -> float:
    """Recency-weighted hit rate: last 5 = 2x, 6-10 = 1.5x, 11-20 = 1x.

    Values must be in chronological order (most recent first).
    """
    if not values:
        return 0.0

    weights = []
    for i in range(len(values)):
        if i < 5:
            weights.append(2.0)
        elif i < 10:
            weights.append(1.5)
        else:
            weights.append(1.0)

    weighted_hits = sum(w * (1.0 if v > line else 0.0) for w, v in zip(weights, values))
    weighted_total = sum(weights[:len(values)])
    if weighted_total <= 0:
        return 0.0
    return weighted_hits / weighted_total


def stat_space_hit_rate(values: list[float], line: float, shift: float) -> float:
    """Shift raw stat values then count hits (spec Section 4 approach).

    This preserves the non-normal shape of the empirical distribution.
    A +2.5 shift at a tight line changes hit rate more than a flat
    probability shift, because more values cross the threshold.
    """
    if not values:
        return 0.0
    adjusted = [v + shift for v in values]

    # Apply recency weighting to the shifted values
    weights = []
    for i in range(len(adjusted)):
        if i < 5:
            weights.append(2.0)
        elif i < 10:
            weights.append(1.5)
        else:
            weights.append(1.0)

    weighted_hits = sum(w * (1.0 if v > line else 0.0) for w, v in zip(weights, adjusted))
    weighted_total = sum(weights[:len(adjusted)])
    return weighted_hits / weighted_total if weighted_total > 0 else 0.0


def prob_space_hit_rate(values: list[float], line: float, prob_shift: float) -> float:
    """Count hits then add probability shift (current Oracle approach).

    Simpler but doesn't preserve distribution shape near the line.
    """
    base_hr = weighted_hit_rate(values, line)
    return max(0.0, min(1.0, base_hr + prob_shift))


def compare_approaches(
    values: list[float],
    line: float,
    shifts: dict[str, float],
) -> dict:
    """Compare probability-space vs stat-space approaches.

    shifts: dict mapping adjustment name to point-value shift.
        e.g., {"matchup": 2.5, "venue": -1.0, "b2b": -1.5}

    Returns comparison dict with both approaches and divergence.
    """
    total_shift = sum(shifts.values())

    # Stat-space: shift raw values, then count hits
    stat_hr = stat_space_hit_rate(values, line, total_shift)

    # Probability-space: count hits from raw values, then add prob shift.
    # Convert point shift to approximate probability shift:
    # A rough heuristic: divide by the std of the distribution.
    if values:
        mean = sum(values) / len(values)
        std = math.sqrt(sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1))
        prob_shift = total_shift / max(std, 1.0) * 0.15  # rough scaling
    else:
        prob_shift = total_shift * 0.03  # fallback

    prob_hr = prob_space_hit_rate(values, line, prob_shift)

    divergence = abs(stat_hr - prob_hr)
    base_hr = weighted_hit_rate(values, line)

    return {
        "base_hit_rate": round(base_hr, 4),
        "stat_space": {
            "hit_rate": round(stat_hr, 4),
            "method": "shift stat values, then count hits",
            "total_shift_pts": round(total_shift, 2),
        },
        "prob_space": {
            "hit_rate": round(prob_hr, 4),
            "method": "count hits, then shift probability",
            "prob_shift": round(prob_shift, 4),
        },
        "divergence": round(divergence, 4),
        "divergence_pct": round(divergence * 100, 2),
        "material": divergence > 0.02,  # >2% = would change trading decisions
    }


def load_gamelogs(player_id: int) -> list[dict]:
    """Load a player's game logs from the processed data directory."""
    path = GAMELOGS_DIR / f"{player_id}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def main():
    """CLI: Build CDFs and run comparisons for all collected players."""
    import argparse

    parser = argparse.ArgumentParser(description="Build empirical CDFs from game logs")
    parser.add_argument("--stat", default="pts", help="Stat field (pts, reb, ast)")
    parser.add_argument("--shift", type=float, default=2.5, help="Test shift magnitude (points)")
    args = parser.parse_args()

    DISTRIBUTIONS_DIR.mkdir(parents=True, exist_ok=True)

    if not GAMELOGS_DIR.exists():
        print(f"No game logs found at {GAMELOGS_DIR}")
        print("Run: python -m collect.real_sports --players 50 --games 20")
        return

    # Stat name mapping: CLI arg → possible field names in game log
    STAT_ALIASES = {
        "pts": ["pts", "points"],
        "reb": ["reb", "rebounds"],
        "ast": ["ast", "assists"],
        "stl": ["stl", "steals"],
        "blk": ["blk", "blocks"],
        "to": ["to", "turnovers"],
    }
    stat_fields = STAT_ALIASES.get(args.stat, [args.stat])

    player_files = list(GAMELOGS_DIR.glob("*.json"))
    if not player_files:
        print("No game log files found. Run the collector first.")
        return

    print(f"Building CDFs for {len(player_files)} players, stat={args.stat}")
    print(f"Test shift: {args.shift:+.1f} points")
    print()
    print(f"{'Player':>20} {'Games':>5} {'Mean':>6} {'HR@mean':>7} {'StatSpace':>9} {'ProbSpace':>9} {'Div%':>5} {'Mat':>4}")
    print("-" * 75)

    material_count = 0
    total_count = 0

    for pf in sorted(player_files):
        data = json.loads(pf.read_text())
        # Handle both raw list and wrapped dict formats
        if isinstance(data, list):
            logs = data
            player_name = pf.stem
        elif isinstance(data, dict):
            logs = data.get("gamelogs", data.get("games", []))
            player_name = data.get("player_name", pf.stem)
        else:
            continue

        # Extract stat values using alias lookup
        values = []
        for g in logs:
            if not isinstance(g, dict):
                continue
            for field in stat_fields:
                v = g.get(field, 0)
                if v and v > 0:
                    values.append(float(v))
                    break

        if len(values) < 5:
            continue

        total_count += 1
        mean = sum(values) / len(values)
        line = round(mean)  # test at mean

        cdf = build_empirical_cdf(values, line)
        comp = compare_approaches(values, line, {"test_shift": args.shift})

        # Save distribution
        dist_path = DISTRIBUTIONS_DIR / f"{pf.stem}_{args.stat}.json"
        dist_path.write_text(json.dumps({**cdf, "comparison": comp}, indent=2))

        is_material = comp["material"]
        if is_material:
            material_count += 1
        material = "YES" if is_material else ""
        print(
            f"{player_name:>20} {len(values):>5} {mean:>6.1f} "
            f"{cdf['hit_rate']:>7.3f} {comp['stat_space']['hit_rate']:>9.3f} "
            f"{comp['prob_space']['hit_rate']:>9.3f} {comp['divergence_pct']:>5.1f} "
            f"{material:>4}"
        )

    print()
    print(f"Summary: {material_count}/{total_count} players have material divergence (>2%)")
    if total_count > 0:
        print(f"Material rate: {material_count/total_count*100:.0f}%")


if __name__ == "__main__":
    main()
