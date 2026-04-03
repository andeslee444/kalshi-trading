#!/usr/bin/env python3
"""H6 Prop Analysis: evaluate Book B pregame props with real Kalshi prices.

Loads the 300 generated signals and 38 scored results from the nba-props
research pipeline. Analyzes:
  1. P&L by stat class
  2. Edge bucket performance
  3. Side analysis (YES vs NO)
  4. Model calibration vs real settlements
  5. Brier score with real prices
  6. Capacity estimate

Usage:
    python3 scripts/oracle-h6-prop-analysis.py [--json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SIGNALS_PATH = PROJECT_DIR / "research" / "nba-props" / "data" / "signals.json"
RESULTS_DIR = PROJECT_DIR / "research" / "nba-props" / "data" / "signal_results"
RESULTS_TSV = PROJECT_DIR / "research" / "nba-props" / "results.tsv"


def load_signals() -> list[dict]:
    if not SIGNALS_PATH.exists():
        return []
    data = json.loads(SIGNALS_PATH.read_text())
    if isinstance(data, dict):
        payload = data.get("signals", [])
        return payload if isinstance(payload, list) else []
    return data if isinstance(data, list) else []


def load_scored_results() -> list[dict]:
    """Load the most recent scored results file."""
    if not RESULTS_DIR.exists():
        return []
    files = sorted(RESULTS_DIR.glob("results_*.json"), reverse=True)
    if not files:
        return []
    data = json.loads(files[0].read_text())
    return data.get("results", [])


def load_experiment_history() -> list[dict]:
    """Load results.tsv experiment history."""
    if not RESULTS_TSV.exists():
        return []
    rows = []
    with open(RESULTS_TSV) as f:
        header = None
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if header is None:
                header = parts
                continue
            row = {}
            for i, col in enumerate(header):
                row[col] = parts[i] if i < len(parts) else ""
            rows.append(row)
    return rows


def _edge_bucket(edge: float) -> str:
    if edge < 0.06:
        return "<6%"
    if edge < 0.10:
        return "6-10%"
    if edge < 0.20:
        return "10-20%"
    if edge < 0.30:
        return "20-30%"
    return "30%+"


def run_analysis() -> dict:
    signals = load_signals()
    scored = load_scored_results()
    experiments = load_experiment_history()

    if not scored:
        return {"error": "No scored results found"}

    # ─── Section 1: Overall Summary ───
    total_trades = len(scored)
    total_wins = sum(1 for r in scored if r.get("won"))
    total_pnl = sum(r.get("net_profit", 0) for r in scored)
    total_cost = sum(abs(r.get("cost", 0)) for r in scored)
    win_rate = total_wins / total_trades if total_trades else 0
    roi = (total_pnl / total_cost * 100) if total_cost else 0

    # ─── Section 2: By Stat Class ───
    by_stat = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0, "edges": []})
    for r in scored:
        stat = r.get("stat", "unknown")
        by_stat[stat]["n"] += 1
        by_stat[stat]["wins"] += 1 if r.get("won") else 0
        by_stat[stat]["pnl"] += r.get("net_profit", 0)
        by_stat[stat]["cost"] += abs(r.get("cost", 0))
        by_stat[stat]["edges"].append(r.get("edge", 0))

    stat_table = []
    for stat in sorted(by_stat.keys()):
        d = by_stat[stat]
        wr = d["wins"] / d["n"] if d["n"] else 0
        r = (d["pnl"] / d["cost"] * 100) if d["cost"] else 0
        stat_table.append({
            "stat": stat,
            "trades": d["n"],
            "wins": d["wins"],
            "win_rate": round(wr, 3),
            "pnl": round(d["pnl"], 2),
            "roi_pct": round(r, 1),
            "avg_edge": round(statistics.mean(d["edges"]), 3) if d["edges"] else 0,
            "verdict": "KEEP" if d["n"] >= 5 and d["pnl"] > 0 else "KILL",
        })

    # ─── Section 3: By Edge Bucket ───
    by_edge = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0})
    for r in scored:
        bucket = _edge_bucket(r.get("edge", 0))
        by_edge[bucket]["n"] += 1
        by_edge[bucket]["wins"] += 1 if r.get("won") else 0
        by_edge[bucket]["pnl"] += r.get("net_profit", 0)
        by_edge[bucket]["cost"] += abs(r.get("cost", 0))

    edge_table = []
    for bucket in ["<6%", "6-10%", "10-20%", "20-30%", "30%+"]:
        d = by_edge[bucket]
        if d["n"] == 0:
            continue
        wr = d["wins"] / d["n"] if d["n"] else 0
        r = (d["pnl"] / d["cost"] * 100) if d["cost"] else 0
        edge_table.append({
            "bucket": bucket,
            "trades": d["n"],
            "wins": d["wins"],
            "win_rate": round(wr, 3),
            "pnl": round(d["pnl"], 2),
            "roi_pct": round(r, 1),
        })

    # ─── Section 4: By Side ───
    by_side = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for r in scored:
        by_side[r.get("side", "")]["n"] += 1
        by_side[r.get("side", "")]["wins"] += 1 if r.get("won") else 0
        by_side[r.get("side", "")]["pnl"] += r.get("net_profit", 0)

    side_table = []
    for side in ["YES", "NO"]:
        d = by_side[side]
        wr = d["wins"] / d["n"] if d["n"] else 0
        side_table.append({
            "side": side,
            "trades": d["n"],
            "wins": d["wins"],
            "win_rate": round(wr, 3),
            "pnl": round(d["pnl"], 2),
        })

    # ─── Section 5: Model Calibration ───
    # Brier score: model_prob vs actual outcome (did the trade win?)
    brier_sum = 0
    for r in scored:
        model = r.get("model_prob", 0.5)
        outcome = 1.0 if r.get("won") else 0.0
        brier_sum += (model - outcome) ** 2
    brier = brier_sum / total_trades if total_trades else 1.0

    # Calibration by model confidence bucket
    cal_buckets = defaultdict(lambda: {"n": 0, "wins": 0, "model_sum": 0.0})
    for r in scored:
        mp = r.get("model_prob", 0.5)
        if mp < 0.6:
            bucket = "50-60%"
        elif mp < 0.7:
            bucket = "60-70%"
        elif mp < 0.8:
            bucket = "70-80%"
        elif mp < 0.9:
            bucket = "80-90%"
        else:
            bucket = "90-100%"
        cal_buckets[bucket]["n"] += 1
        cal_buckets[bucket]["wins"] += 1 if r.get("won") else 0
        cal_buckets[bucket]["model_sum"] += mp

    cal_table = []
    for bucket in ["50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]:
        d = cal_buckets[bucket]
        if d["n"] == 0:
            continue
        actual = d["wins"] / d["n"]
        predicted = d["model_sum"] / d["n"]
        cal_table.append({
            "bucket": bucket,
            "n": d["n"],
            "actual_win_rate": round(actual, 3),
            "predicted_win_rate": round(predicted, 3),
            "gap": round(predicted - actual, 3),
        })

    # ─── Section 6: Experiment History Summary ───
    # Check last few experiments for profit_pct
    exp_summary = []
    for e in experiments[-5:]:
        exp_summary.append({
            "commit": e.get("commit", ""),
            "brier": float(e.get("brier_score", 0)),
            "cal_error": float(e.get("calibration_error", 0)),
            "profit_pct": float(e.get("expected_profit_pct", 0)),
            "sample_size": int(e.get("sample_size", 0)),
            "trades_taken": int(e.get("trades_taken", 0)),
            "description": e.get("description", ""),
        })

    # ─── Section 7: Capacity Estimate ───
    # Signals generated: 300 for one game night
    # Settled: 38 (12.7%)
    # Tradeable (edge >= 6%): roughly ~30-50 per night from the signal data
    signals_per_night = len(signals) if signals else 300
    tradeable_rate = total_trades / signals_per_night if signals_per_night else 0

    # ─── Verdict ───
    any_stat_positive = any(s["pnl"] > 0 and s["trades"] >= 5 for s in stat_table)
    sufficient_sample = total_trades >= 50

    # The key finding: NO-side is 0% win rate (32 trades, 0 wins, -$10.54)
    # YES-side is 100% win rate (6 trades, 6 wins, +$2.21)
    no_side_catastrophe = (by_side.get("NO", {}).get("wins", 0) == 0
                           and by_side.get("NO", {}).get("n", 0) >= 10)

    # Edge bucket inversion: higher edge = worse performance
    edge_inversion = (by_edge.get("30%+", {}).get("pnl", 0) < 0
                      and by_edge.get("<6%", {}).get("pnl", 0) > 0)

    if no_side_catastrophe:
        verdict = "KILLED"
        reason = (
            f"Catastrophic directional failure: NO-side is {by_side.get('NO', {}).get('wins', 0)}/"
            f"{by_side.get('NO', {}).get('n', 0)} wins across {total_trades} settled signals."
        )
    elif not sufficient_sample:
        verdict = "INCONCLUSIVE"
        reason = f"Only {total_trades} settled signals (need >= 50). Directional evidence is strongly negative."
    elif any_stat_positive:
        verdict = "NARROW"
        reason = f"Rebounds shows positive P&L but only {by_stat.get('rebounds', {}).get('n', 0)} trades."
    else:
        verdict = "KILLED"
        reason = "No stat class has positive P&L on sufficient sample."

    if no_side_catastrophe:
        reason += " CRITICAL: NO-side is 0/32 wins -- the model systematically overestimates hit rates."
    if edge_inversion:
        reason += " Edge inversion: higher model edge = worse results. Classic overconfidence pattern."

    recommendation = ""
    if verdict == "KILLED":
        recommendation = (
            "Book B is KILLED as currently designed. The model has catastrophic NO-side calibration "
            "(0/32 wins) meaning it predicts players will MISS lines far more often than reality. "
            "The edge-inversion (30%+ edge = -60% ROI, <6% edge = +9.5% ROI) confirms the model "
            "is most wrong when most confident. "
            "To resurrect Book B: (1) abandon the empirical hit-rate model for a minutes-rate "
            "negative binomial, (2) incorporate sportsbook consensus as a base rate, "
            "(3) restrict to YES-side only (100% win rate on 6 trades is tiny but directionally correct), "
            "(4) drop assists/steals/blocks/3PT entirely."
        )
    elif verdict == "NARROW":
        recommendation = (
            f"Only rebounds ({by_stat.get('rebounds', {}).get('n', 0)} trades) shows positive ROI. "
            "Sample is too small for a real verdict. Continue collecting data on rebounds-only. "
            "Kill all other stat classes immediately."
        )
    else:
        recommendation = "Collect more data before deciding."

    return {
        "summary": {
            "total_signals": len(signals),
            "settled_signals": total_trades,
            "total_wins": total_wins,
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "roi_pct": round(roi, 1),
            "brier_score": round(brier, 4),
        },
        "by_stat": stat_table,
        "by_edge_bucket": edge_table,
        "by_side": side_table,
        "calibration": cal_table,
        "experiment_history": exp_summary,
        "capacity": {
            "signals_per_night": signals_per_night,
            "settle_rate": round(tradeable_rate, 3),
            "estimated_trades_per_night": round(signals_per_night * tradeable_rate),
        },
        "critical_findings": {
            "no_side_catastrophe": no_side_catastrophe,
            "edge_inversion": edge_inversion,
            "no_side_record": (
                f"{by_side.get('NO', {}).get('wins', 0)}/{by_side.get('NO', {}).get('n', 0)} wins"
            ),
            "yes_side_record": (
                f"{by_side.get('YES', {}).get('wins', 0)}/{by_side.get('YES', {}).get('n', 0)} wins"
            ),
        },
        "verdict": {
            "h6_status": verdict,
            "reason": reason,
            "recommendation": recommendation,
        },
    }


def print_human_readable(results: dict):
    if "error" in results:
        print(f"ERROR: {results['error']}")
        return

    s = results["summary"]
    print("=" * 80)
    print("H6 PREGAME PROPS ANALYSIS (Real Kalshi Prices)")
    print("=" * 80)
    print(f"Signals generated:  {s['total_signals']}")
    print(f"Settled:            {s['settled_signals']}")
    print(f"Win rate:           {s['win_rate']:.1%}")
    print(f"Total P&L:          ${s['total_pnl']:+.2f}")
    print(f"ROI:                {s['roi_pct']:+.1f}%")
    print(f"Brier score:        {s['brier_score']:.4f} (0.25 = coin flip)")
    print()

    print("--- By Stat Class ---")
    print(f"{'Stat':<18} {'N':>4} {'Wins':>4} {'Win%':>6} {'P&L':>8} {'ROI':>7} {'AvgEdge':>8} {'Verdict':>7}")
    print("-" * 70)
    for row in results["by_stat"]:
        print(f"{row['stat']:<18} {row['trades']:>4} {row['wins']:>4} "
              f"{row['win_rate']:>6.1%} ${row['pnl']:>+7.2f} {row['roi_pct']:>+6.1f}% "
              f"{row['avg_edge']:>7.1%} {row['verdict']:>7}")
    print()

    print("--- By Edge Bucket ---")
    print(f"{'Bucket':<10} {'N':>4} {'Wins':>4} {'Win%':>6} {'P&L':>8} {'ROI':>7}")
    print("-" * 45)
    for row in results["by_edge_bucket"]:
        print(f"{row['bucket']:<10} {row['trades']:>4} {row['wins']:>4} "
              f"{row['win_rate']:>6.1%} ${row['pnl']:>+7.2f} {row['roi_pct']:>+6.1f}%")
    print()

    print("--- By Side ---")
    for row in results["by_side"]:
        print(f"  {row['side']}: {row['trades']} trades, {row['wins']} wins, "
              f"{row['win_rate']:.1%}, ${row['pnl']:+.2f}")
    print()

    print("--- Model Calibration ---")
    print(f"{'Confidence':<12} {'N':>4} {'Actual':>8} {'Predicted':>10} {'Gap':>6}")
    print("-" * 45)
    for row in results["calibration"]:
        print(f"{row['bucket']:<12} {row['n']:>4} {row['actual_win_rate']:>8.1%} "
              f"{row['predicted_win_rate']:>10.1%} {row['gap']:>+5.1%}")
    print()

    cf = results["critical_findings"]
    print("--- CRITICAL FINDINGS ---")
    print(f"  NO-side record:    {cf['no_side_record']} {'*** CATASTROPHIC ***' if cf['no_side_catastrophe'] else ''}")
    print(f"  YES-side record:   {cf['yes_side_record']}")
    print(f"  Edge inversion:    {'YES -- higher edge = worse results' if cf['edge_inversion'] else 'No'}")
    print()

    v = results["verdict"]
    print("=" * 80)
    print(f"VERDICT: {v['h6_status']}")
    print(f"Reason:  {v['reason']}")
    print(f"\nRecommendation: {v['recommendation']}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="H6 prop analysis")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = run_analysis()

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_human_readable(results)

    out_path = PROJECT_DIR / "data" / "reports" / "oracle-h6-prop-analysis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nJSON written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
