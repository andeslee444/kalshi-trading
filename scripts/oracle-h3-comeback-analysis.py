#!/usr/bin/env python3
"""H3 Comeback Overpricing Analysis: empirical trailing-team win rates from stored feeds.

Uses 201 stored Real game feeds to compute:
  1. Book C parameter sweep (12-combination clutch_comeback grid)
  2. Empirical trailing-team win rates by margin/time bucket
  3. Comparison against the Book C model probability
  4. Theoretical overpricing estimates

Usage:
    python3 scripts/oracle-h3-comeback-analysis.py [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from domain.oracle.book_c_research import (
    evaluate_clutch_comeback_buckets,
    load_processed_game_feeds,
    summarize_book_c_dataset,
    summarize_book_c_parameter_sweep,
)


def _model_trailing_prob(margin: float, clock_seconds: float) -> float:
    """Reproduce Book C clutch_comeback model probability for trailing team.

    From book_c.py detect_clutch_comeback():
        time_factor = clamp(clock_seconds / 120.0, 0, 1)
        model_prob_trailing = clamp(0.08 + 0.42*time_factor - 0.075*margin, 0.01, 0.45)
    """
    time_factor = max(0.0, min(1.0, clock_seconds / 120.0))
    prob = 0.08 + (0.42 * time_factor) - (0.075 * margin)
    return max(0.01, min(0.45, prob))


def _bucket_midpoints() -> dict[tuple[str, str], tuple[float, float]]:
    """Return (margin_midpoint, time_midpoint_seconds) for each bucket."""
    return {
        ("2:00-1:00", "1"): (1.0, 90.0),
        ("2:00-1:00", "2"): (2.0, 90.0),
        ("2:00-1:00", "3-5"): (4.0, 90.0),
        ("2:00-1:00", "6-8"): (7.0, 90.0),
        ("1:00-0:30", "1"): (1.0, 45.0),
        ("1:00-0:30", "2"): (2.0, 45.0),
        ("1:00-0:30", "3-5"): (4.0, 45.0),
        ("1:00-0:30", "6-8"): (7.0, 45.0),
        ("0:30-0:10", "1"): (1.0, 20.0),
        ("0:30-0:10", "2"): (2.0, 20.0),
        ("0:30-0:10", "3-5"): (4.0, 20.0),
        ("0:30-0:10", "6-8"): (7.0, 20.0),
    }


def run_analysis() -> dict:
    """Run full H3 comeback overpricing analysis."""
    feeds_path = PROJECT_DIR / "research" / "nba-props" / "data" / "processed" / "game_feeds"
    feeds = load_processed_game_feeds(feeds_path)

    if not feeds:
        return {"error": f"No game feeds found at {feeds_path}"}

    print(f"Loaded {len(feeds)} game feeds", file=sys.stderr)

    # 1. Dataset summary (includes clutch buckets)
    dataset = summarize_book_c_dataset(feeds)

    # 2. Parameter sweep
    sweep = summarize_book_c_parameter_sweep(feeds)

    # 3. Clutch comeback buckets with empirical win rates
    buckets = evaluate_clutch_comeback_buckets(feeds)

    # 4. Enrich buckets with model comparison
    midpoints = _bucket_midpoints()
    enriched_buckets = []
    total_opportunities = 0
    total_overpriced_opps = 0
    overpricing_5pp_buckets = []

    for b in buckets:
        key = (b["time_bucket"], b["margin_bucket"])
        margin_mid, time_mid = midpoints.get(key, (3.0, 60.0))

        model_trailing_prob = round(_model_trailing_prob(margin_mid, time_mid), 3)
        empirical_trailing = b["trailing_win_rate"]
        empirical_leader = b["leader_hold_rate"]

        # Overpricing = how much more the market/model prices the trailing team
        # vs empirical reality. Positive = trailing team is overpriced (alpha for fading).
        overpricing_vs_model = None
        if empirical_trailing is not None:
            overpricing_vs_model = round(model_trailing_prob - empirical_trailing, 3)

        enriched = {
            **b,
            "model_trailing_prob": model_trailing_prob,
            "model_leader_prob": round(1.0 - model_trailing_prob, 3),
            "overpricing_vs_model_pp": (
                round(overpricing_vs_model * 100, 1) if overpricing_vs_model is not None else None
            ),
            "bucket_margin_mid": margin_mid,
            "bucket_time_mid_seconds": time_mid,
        }
        enriched_buckets.append(enriched)
        total_opportunities += b["opportunities"]

        if overpricing_vs_model is not None and overpricing_vs_model >= 0.05:
            total_overpriced_opps += b["opportunities"]
            overpricing_5pp_buckets.append(enriched)

    # 5. Cross-reference with H1 live data summary
    h1_note = (
        "H1 live data shows clutch_entry at t+5s: mean markout +0.535c, "
        "CI [+0.043, +0.875], N=43 across 5 games. "
        "This is the ONLY event class with positive markout in the H1 analysis."
    )

    # 6. Sweep best parameters
    cc_sweep = sweep.get("sweeps", {}).get("clutch_comeback", {})
    best_cc = cc_sweep.get("best", {})

    # 7. Verdict
    has_overpricing = len(overpricing_5pp_buckets) > 0
    has_volume = total_opportunities >= 200

    if has_overpricing and has_volume:
        verdict = "STAGE_1_PASS"
        reason = (
            f"Found {len(overpricing_5pp_buckets)} bucket(s) with >= 5pp overpricing "
            f"across {total_overpriced_opps} opportunities. "
            f"Total opportunities: {total_opportunities} (>= 200 threshold met)."
        )
        recommendation = (
            "H3 passes Stage 1. Proceed to Stage 2: shadow-trade clutch_comeback signals "
            "restricted to the overpriced buckets. The H1 live data corroborates this -- "
            "clutch_entry is the only positive-markout event class."
        )
    elif has_overpricing and not has_volume:
        verdict = "INCONCLUSIVE_LOW_VOLUME"
        reason = (
            f"Found overpricing in {len(overpricing_5pp_buckets)} bucket(s) "
            f"but only {total_opportunities} total opportunities (< 200 threshold)."
        )
        recommendation = "Collect more game feeds and re-analyze."
    elif not has_overpricing and total_opportunities >= 50:
        verdict = "KILLED"
        reason = (
            f"No bucket shows >= 5pp overpricing across {total_opportunities} opportunities. "
            "The empirical trailing-team win rates are close to or above the model predictions."
        )
        recommendation = (
            "H3 does not show tradeable comeback overpricing at the current model's parameters. "
            "Consider: (1) the model's trailing-team probability may already be well-calibrated, "
            "or (2) Real game feeds may not capture the same market conditions as Kalshi late-game quotes."
        )
    else:
        verdict = "INSUFFICIENT_DATA"
        reason = f"Only {total_opportunities} total opportunities, too few to draw conclusions."
        recommendation = "Collect substantially more game feeds before re-evaluating."

    return {
        "summary": {
            "total_feeds": len(feeds),
            "total_clutch_opportunities": total_opportunities,
            "buckets_with_data": len([b for b in enriched_buckets if b["opportunities"] > 0]),
            "overpricing_5pp_buckets": len(overpricing_5pp_buckets),
            "overpriced_opportunities": total_overpriced_opps,
        },
        "empirical_win_rate_table": enriched_buckets,
        "parameter_sweep": {
            "clutch_comeback_best": best_cc,
            "clutch_comeback_rows": cc_sweep.get("rows", []),
        },
        "dataset_summary": {
            "total_games": dataset.get("total_games"),
            "supported_signals": dataset.get("supported_signals", []),
        },
        "h1_cross_reference": h1_note,
        "verdict": {
            "h3_status": verdict,
            "reason": reason,
            "recommendation": recommendation,
            "overpricing_5pp_buckets": overpricing_5pp_buckets,
        },
    }


def print_human_readable(results: dict):
    """Print human-readable summary."""
    if "error" in results:
        print(f"ERROR: {results['error']}")
        return

    s = results["summary"]
    print("=" * 80)
    print("H3 COMEBACK OVERPRICING ANALYSIS")
    print("=" * 80)
    print(f"Game feeds analyzed:     {s['total_feeds']}")
    print(f"Clutch opportunities:    {s['total_clutch_opportunities']}")
    print(f"Buckets with data:       {s['buckets_with_data']}")
    print()

    # Empirical win rate table
    print("--- Empirical Trailing-Team Win Rate by Bucket ---")
    print(f"{'Time':<12} {'Margin':<8} {'N':>5} {'Games':>5} {'Trail%':>7} {'Lead%':>7} "
          f"{'Model%':>7} {'Overprice':>10}")
    print("-" * 75)
    for b in results["empirical_win_rate_table"]:
        if b["opportunities"] == 0:
            continue
        op = b.get("overpricing_vs_model_pp")
        op_str = f"{op:>+7.1f}pp" if op is not None else "    n/a"
        flag = " <<<" if op is not None and op >= 5.0 else ""
        print(f"{b['time_bucket']:<12} {b['margin_bucket']:<8} {b['opportunities']:>5} "
              f"{b['games']:>5} {b['trailing_win_rate']:>7.1%} {b['leader_hold_rate']:>7.1%} "
              f"{b['model_trailing_prob']:>7.1%} {op_str}{flag}")
    print()

    # Parameter sweep best
    best = results["parameter_sweep"]["clutch_comeback_best"]
    if best:
        print("--- Best Clutch Comeback Parameter Set ---")
        print(f"  Window: {best.get('window', 'n/a')}")
        print(f"  Games:  {best.get('games_with_signal', 0)} / {best.get('games_share', 0):.1%}")
        print(f"  Opps:   {best.get('opportunities', 0)}")
        print(f"  Score:  {best.get('priority_score', 0):.3f}")
        print(f"  Rec:    {best.get('recommendation', 'n/a')}")
        print()

    # H1 cross-reference
    print("--- H1 Cross-Reference ---")
    print(f"  {results['h1_cross_reference']}")
    print()

    # Verdict
    v = results["verdict"]
    print("=" * 80)
    print(f"VERDICT: {v['h3_status']}")
    print(f"Reason:  {v['reason']}")
    if v["overpricing_5pp_buckets"]:
        print("Overpriced buckets:")
        for b in v["overpricing_5pp_buckets"]:
            print(f"  - {b['time_bucket']} / margin {b['margin_bucket']}: "
                  f"{b['overpricing_vs_model_pp']:+.1f}pp ({b['opportunities']} opps)")
    print(f"\nRecommendation: {v['recommendation']}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="H3 comeback overpricing analysis")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    results = run_analysis()

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_human_readable(results)

    # Write JSON
    out_path = PROJECT_DIR / "data" / "reports" / "oracle-h3-comeback-analysis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nJSON written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
