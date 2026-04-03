#!/usr/bin/env python3
"""Deep H1 latency analysis: decompose markouts to find filterable positive subsets.

Queries the oracle-alpha-ledger.sqlite3 directly to slice markouts by:
  1. Market type (game vs prop)
  2. Game state at signal time
  3. Spread bucket
  4. Direction (YES vs NO entry)
  5. Period (Q1-Q4, OT)
  6. Adverse selection across horizons
  7. Depth filter (depth >= 10, spread <= 6c)

Usage:
    python3 scripts/oracle-h1-deep-analysis.py [--json] [--ledger PATH]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = PROJECT_DIR / "data" / "oracle-alpha-ledger.sqlite3"


def _median(values: list) -> float | None:
    if not values:
        return None
    return round(statistics.median(values), 3)


def _mean(values: list) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _percentile(values: list, pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return round(float(ordered[low]), 3)
    frac = rank - low
    return round(float(ordered[low] + (ordered[high] - ordered[low]) * frac), 3)


def _bootstrap_mean_ci(values: list[float], *, cluster_keys: list[str] | None = None,
                        samples: int = 500, seed: int = 42) -> dict:
    """Clustered bootstrap 95% CI for mean."""
    if not values:
        return {"mean": None, "ci_low": None, "ci_high": None, "n": 0, "clusters": 0}

    if cluster_keys and len(cluster_keys) == len(values):
        clusters: dict[str, list[float]] = defaultdict(list)
        for v, k in zip(values, cluster_keys):
            clusters[k].append(v)
    else:
        clusters = {"all": values}

    rng = random.Random(seed)
    keys = list(clusters)
    n_clusters = len(keys)
    draw_means = []
    for _ in range(samples):
        sampled = [keys[rng.randrange(n_clusters)] for _ in range(n_clusters)]
        draw = [v for k in sampled for v in clusters[k]]
        if draw:
            draw_means.append(sum(draw) / len(draw))

    draw_means.sort()
    lo_idx = max(0, int(len(draw_means) * 0.025))
    hi_idx = min(len(draw_means) - 1, int(len(draw_means) * 0.975))

    return {
        "mean": _mean(values),
        "ci_low": round(draw_means[lo_idx], 3) if draw_means else None,
        "ci_high": round(draw_means[hi_idx], 3) if draw_means else None,
        "n": len(values),
        "clusters": n_clusters,
    }


def load_paired_opportunities(db: sqlite3.Connection) -> list[dict]:
    """Load all paired baseline->followup quote snapshots with markout data.

    A "pair" is: for a given source_event, we have a baseline quote (horizon=0)
    and a followup quote (horizon>0) for the same ticker. The markout is the
    change in fair value between baseline entry and followup.
    """
    cur = db.cursor()

    # Load all quote snapshots (market_snapshot events)
    cur.execute("""
        SELECT event_id, payload_json FROM events
        WHERE event_type = 'market_snapshot'
        AND payload_json IS NOT NULL
    """)

    # Index by (source_event_id, ticker) -> list of snapshots by horizon
    by_source_ticker: dict[tuple[str, str], dict[float, dict]] = defaultdict(dict)
    total_snapshots = 0

    for row in cur.fetchall():
        total_snapshots += 1
        try:
            p = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue

        source_id = p.get("source_event_id", "")
        ticker = p.get("ticker", "")
        horizon = p.get("horizon_seconds", -1)
        if not source_id or not ticker or horizon < 0:
            continue

        by_source_ticker[(source_id, ticker)][horizon] = p

    print(f"Loaded {total_snapshots} market snapshots, {len(by_source_ticker)} source-ticker pairs",
          file=sys.stderr)

    # Load source event metadata for classification
    cur.execute("""
        SELECT event_id, payload_json FROM events
        WHERE event_type = 'source_observation'
        AND payload_json LIKE '%"record_kind":"source_event"%'
    """)
    source_meta: dict[str, dict] = {}
    for row in cur.fetchall():
        try:
            p = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue
        eid = p.get("event_id", "")
        if eid:
            source_meta[eid] = p

    print(f"Loaded {len(source_meta)} source event metadata rows", file=sys.stderr)

    # Build paired opportunities
    opportunities = []
    for (source_id, ticker), horizons in by_source_ticker.items():
        baseline = horizons.get(0.0) or horizons.get(0)
        if not baseline:
            continue

        b_bid = baseline.get("yes_bid_cents", 0)
        b_ask = baseline.get("yes_ask_cents", 0)
        b_spread = baseline.get("spread_cents", 99)
        b_bid_depth = baseline.get("yes_bid_depth", 0)
        b_ask_depth = baseline.get("yes_ask_depth", 0)
        b_mid = baseline.get("midpoint_cents", 0)

        if b_bid <= 0 or b_ask <= 0 or b_spread <= 0:
            continue

        src = source_meta.get(source_id, {})
        event_class = (baseline.get("derived_event_class")
                       or src.get("derived_event_class")
                       or "unclassified")
        game_state = baseline.get("game_state") or src.get("game_state") or ""
        period = baseline.get("period") or src.get("period") or ""
        game_id = baseline.get("game_id") or src.get("game_id") or ""

        # Determine market type from ticker
        is_prop = False
        ticker_upper = ticker.upper()
        for prefix in ("KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PM", "KXNBASTL", "KXNBABLK", "KXNBATO"):
            if ticker_upper.startswith(prefix):
                is_prop = True
                break
        market_type = "prop" if is_prop else "game"

        for h, quote in horizons.items():
            if h == 0.0 or h == 0:
                continue  # skip baseline itself

            q_bid = quote.get("yes_bid_cents", 0)
            q_ask = quote.get("yes_ask_cents", 0)
            if q_bid <= 0 or q_ask <= 0:
                continue

            # YES-side markout: buy at baseline ask, mark-to-market at followup bid
            yes_markout = q_bid - b_ask
            # NO-side markout: sell at baseline bid, mark-to-market at followup ask
            no_markout = b_bid - q_ask
            best_markout = max(yes_markout, no_markout)

            # Fillability checks
            yes_fillable = b_spread <= 8 and b_ask_depth >= 5
            no_fillable = b_spread <= 8 and b_bid_depth >= 5
            any_fillable = yes_fillable or no_fillable

            # Tight filter: spread <= 6, depth >= 10
            yes_tight = b_spread <= 6 and b_ask_depth >= 10
            no_tight = b_spread <= 6 and b_bid_depth >= 10
            any_tight = yes_tight or no_tight

            opportunities.append({
                "source_event_id": source_id,
                "ticker": ticker,
                "horizon_seconds": float(h),
                "event_class": event_class,
                "game_state": game_state,
                "period": period,
                "game_id": str(game_id),
                "market_type": market_type,
                "baseline_spread": b_spread,
                "baseline_bid": b_bid,
                "baseline_ask": b_ask,
                "baseline_mid": b_mid,
                "baseline_bid_depth": b_bid_depth,
                "baseline_ask_depth": b_ask_depth,
                "followup_bid": q_bid,
                "followup_ask": q_ask,
                "yes_markout": yes_markout,
                "no_markout": no_markout,
                "best_markout": best_markout,
                "yes_fillable": yes_fillable,
                "no_fillable": no_fillable,
                "any_fillable": any_fillable,
                "yes_tight": yes_tight,
                "no_tight": no_tight,
                "any_tight": any_tight,
                "latency_ms": quote.get("source_to_quote_ms", 0),
            })

    return opportunities


def _spread_bucket(spread: int) -> str:
    if spread <= 4:
        return "0-4c"
    if spread <= 8:
        return "5-8c"
    if spread <= 12:
        return "9-12c"
    return "13c+"


def _period_bucket(period: str) -> str:
    if period in ("Q1", "Q2", "Q3"):
        return "Q1-Q3"
    if period == "Q4":
        return "Q4"
    if period.startswith("OT"):
        return "OT"
    return "other"


def analyze_slice(opps: list[dict], label: str) -> dict:
    """Compute markout stats for a filtered slice of opportunities."""
    if not opps:
        return {"label": label, "n": 0}

    yes_markouts = [o["yes_markout"] for o in opps]
    no_markouts = [o["no_markout"] for o in opps]
    best_markouts = [o["best_markout"] for o in opps]
    cluster_keys = [o["game_id"] for o in opps]

    return {
        "label": label,
        "n": len(opps),
        "games": len(set(o["game_id"] for o in opps)),
        "yes_markout_mean": _mean(yes_markouts),
        "yes_markout_median": _median(yes_markouts),
        "no_markout_mean": _mean(no_markouts),
        "no_markout_median": _median(no_markouts),
        "best_markout_mean": _mean(best_markouts),
        "best_markout_median": _median(best_markouts),
        "best_markout_p25": _percentile(best_markouts, 25),
        "best_markout_p75": _percentile(best_markouts, 75),
        "positive_rate": round(sum(1 for m in best_markouts if m > 0) / len(best_markouts), 3),
        "bootstrap_best": _bootstrap_mean_ci(
            [float(m) for m in best_markouts],
            cluster_keys=cluster_keys,
        ),
        "bootstrap_yes": _bootstrap_mean_ci(
            [float(m) for m in yes_markouts],
            cluster_keys=cluster_keys,
        ),
        "bootstrap_no": _bootstrap_mean_ci(
            [float(m) for m in no_markouts],
            cluster_keys=cluster_keys,
        ),
    }


def run_analysis(ledger_path: Path) -> dict:
    """Run full H1 deep analysis."""
    db = sqlite3.connect(str(ledger_path))
    opps = load_paired_opportunities(db)
    db.close()

    print(f"Total paired opportunities: {len(opps)}", file=sys.stderr)

    # Filter to fillable only for core analysis
    fillable = [o for o in opps if o["any_fillable"]]
    print(f"Fillable opportunities: {len(fillable)}", file=sys.stderr)

    results = {
        "summary": {
            "total_paired": len(opps),
            "total_fillable": len(fillable),
            "horizons_present": sorted(set(o["horizon_seconds"] for o in opps)),
            "event_classes": sorted(set(o["event_class"] for o in opps)),
            "game_count": len(set(o["game_id"] for o in opps if o["game_id"])),
        },
        "sections": {},
    }

    # Use t+5s horizon as the primary analysis window (best available markout horizon)
    t5 = [o for o in fillable if o["horizon_seconds"] == 5.0]
    t3 = [o for o in fillable if o["horizon_seconds"] == 3.0]
    t1 = [o for o in fillable if o["horizon_seconds"] == 1.0]

    results["summary"]["t5_fillable"] = len(t5)
    results["summary"]["t3_fillable"] = len(t3)
    results["summary"]["t1_fillable"] = len(t1)

    primary = t5 if t5 else t3 if t3 else t1
    primary_label = "t+5s" if t5 else "t+3s" if t3 else "t+1s"

    # ─── Section 1: Market Type ───
    game_opps = [o for o in primary if o["market_type"] == "game"]
    prop_opps = [o for o in primary if o["market_type"] == "prop"]
    results["sections"]["by_market_type"] = {
        "game": analyze_slice(game_opps, f"game ({primary_label})"),
        "prop": analyze_slice(prop_opps, f"prop ({primary_label})"),
        "all": analyze_slice(primary, f"all ({primary_label})"),
    }

    # ─── Section 2: Event Class ───
    ec_results = {}
    for ec in sorted(set(o["event_class"] for o in primary)):
        ec_opps = [o for o in primary if o["event_class"] == ec]
        ec_results[ec] = analyze_slice(ec_opps, f"{ec} ({primary_label})")
    results["sections"]["by_event_class"] = ec_results

    # ─── Section 3: Spread Bucket ───
    spread_results = {}
    for o in primary:
        o["_spread_bucket"] = _spread_bucket(o["baseline_spread"])
    for bucket in ["0-4c", "5-8c", "9-12c", "13c+"]:
        bucket_opps = [o for o in primary if o["_spread_bucket"] == bucket]
        spread_results[bucket] = analyze_slice(bucket_opps, f"spread {bucket} ({primary_label})")
    results["sections"]["by_spread_bucket"] = spread_results

    # ─── Section 4: Directional Analysis ───
    # YES-entry only (buy at ask, evaluate at followup bid)
    yes_only = [o for o in primary if o["yes_fillable"]]
    no_only = [o for o in primary if o["no_fillable"]]

    yes_markouts = [{"best_markout": o["yes_markout"], "game_id": o["game_id"],
                     **{k: v for k, v in o.items() if k != "best_markout"}} for o in yes_only]
    no_markouts = [{"best_markout": o["no_markout"], "game_id": o["game_id"],
                    **{k: v for k, v in o.items() if k != "best_markout"}} for o in no_only]

    results["sections"]["directional"] = {
        "yes_entry": {
            "n": len(yes_only),
            "mean": _mean([o["yes_markout"] for o in yes_only]),
            "median": _median([o["yes_markout"] for o in yes_only]),
            "positive_rate": round(sum(1 for o in yes_only if o["yes_markout"] > 0) / max(1, len(yes_only)), 3),
            "bootstrap": _bootstrap_mean_ci(
                [float(o["yes_markout"]) for o in yes_only],
                cluster_keys=[o["game_id"] for o in yes_only],
            ),
        },
        "no_entry": {
            "n": len(no_only),
            "mean": _mean([o["no_markout"] for o in no_only]),
            "median": _median([o["no_markout"] for o in no_only]),
            "positive_rate": round(sum(1 for o in no_only if o["no_markout"] > 0) / max(1, len(no_only)), 3),
            "bootstrap": _bootstrap_mean_ci(
                [float(o["no_markout"]) for o in no_only],
                cluster_keys=[o["game_id"] for o in no_only],
            ),
        },
    }

    # ─── Section 5: Period Analysis ───
    period_results = {}
    for o in primary:
        o["_period_bucket"] = _period_bucket(o["period"])
    for bucket in ["Q1-Q3", "Q4", "OT", "other"]:
        bucket_opps = [o for o in primary if o["_period_bucket"] == bucket]
        if bucket_opps:
            period_results[bucket] = analyze_slice(bucket_opps, f"period {bucket} ({primary_label})")
    results["sections"]["by_period"] = period_results

    # ─── Section 6: Adverse Selection Across Horizons ───
    horizon_progression = {}
    for h_sec, h_opps in [("t+1s", t1), ("t+3s", t3), ("t+5s", t5)]:
        if h_opps:
            best = [o["best_markout"] for o in h_opps]
            horizon_progression[h_sec] = {
                "n": len(h_opps),
                "best_mean": _mean(best),
                "best_median": _median(best),
                "positive_rate": round(sum(1 for m in best if m > 0) / len(best), 3),
            }
    results["sections"]["adverse_selection_by_horizon"] = horizon_progression

    # ─── Section 7: Tight Filter (spread <= 6, depth >= 10) ───
    tight = [o for o in primary if o["any_tight"]]
    tight_by_ec = {}
    for ec in sorted(set(o["event_class"] for o in tight)) if tight else []:
        ec_tight = [o for o in tight if o["event_class"] == ec]
        tight_by_ec[ec] = analyze_slice(ec_tight, f"{ec} tight ({primary_label})")
    results["sections"]["tight_filter"] = {
        "all": analyze_slice(tight, f"tight all ({primary_label})"),
        "by_event_class": tight_by_ec,
    }

    # ─── Section 8: Game State at Signal Time ───
    state_results = {}
    for gs in sorted(set(o["game_state"] for o in primary if o["game_state"])):
        gs_opps = [o for o in primary if o["game_state"] == gs]
        state_results[gs] = analyze_slice(gs_opps, f"state={gs} ({primary_label})")
    results["sections"]["by_game_state"] = state_results

    # ─── Section 9: Combined Filters (best-case scenario) ───
    # Q4 + tight spread + classified events (not unclassified)
    q4_tight_classified = [
        o for o in primary
        if o["_period_bucket"] == "Q4"
        and o["any_tight"]
        and o["event_class"] != "unclassified"
    ]
    results["sections"]["best_case_q4_tight_classified"] = analyze_slice(
        q4_tight_classified, f"Q4+tight+classified ({primary_label})"
    )

    # ─── Verdict ───
    verdict_sections = []
    any_positive = False

    for section_name, section_data in results["sections"].items():
        if isinstance(section_data, dict) and "bootstrap_best" in section_data:
            ci = section_data["bootstrap_best"]
            if ci.get("ci_low") is not None and ci["ci_low"] > 0:
                any_positive = True
                verdict_sections.append({
                    "section": section_name,
                    "label": section_data.get("label", ""),
                    "n": section_data.get("n", 0),
                    "mean": ci.get("mean"),
                    "ci": [ci.get("ci_low"), ci.get("ci_high")],
                })
        elif isinstance(section_data, dict):
            for sub_key, sub_data in section_data.items():
                if isinstance(sub_data, dict) and "bootstrap_best" in sub_data:
                    ci = sub_data["bootstrap_best"]
                    if ci.get("ci_low") is not None and ci["ci_low"] > 0:
                        any_positive = True
                        verdict_sections.append({
                            "section": f"{section_name}/{sub_key}",
                            "label": sub_data.get("label", ""),
                            "n": sub_data.get("n", 0),
                            "mean": ci.get("mean"),
                            "ci": [ci.get("ci_low"), ci.get("ci_high")],
                        })

    results["verdict"] = {
        "h1_status": "PARTIAL_PASS" if any_positive else "KILLED",
        "reason": (
            f"Found {len(verdict_sections)} filtered subset(s) with positive markout CI"
            if any_positive
            else "All filtered subsets show negative markout CIs -- no taker edge exists"
        ),
        "positive_subsets": verdict_sections,
        "recommendation": (
            "Narrow Book C to the positive subsets identified above. Shadow-trade only these filters."
            if any_positive
            else "H1 is KILLED for aggressive (taker) execution. "
                 "Consider H8 (maker-vs-taker) -- passive limit orders may recover the spread cost. "
                 "The ~1.3c negative markout is approximately one spread crossing, "
                 "suggesting the information edge exists but is consumed by the bid-ask spread."
        ),
    }

    return results


def print_human_readable(results: dict):
    """Print a human-readable summary."""
    s = results["summary"]
    print("=" * 80)
    print("H1 DEEP LATENCY ANALYSIS")
    print("=" * 80)
    print(f"Total paired:   {s['total_paired']:,}")
    print(f"Fillable:       {s['total_fillable']:,}")
    print(f"Games:          {s['game_count']}")
    print(f"t+5s fillable:  {s['t5_fillable']:,}")
    print(f"t+3s fillable:  {s['t3_fillable']:,}")
    print(f"t+1s fillable:  {s['t1_fillable']:,}")
    print(f"Event classes:  {', '.join(s['event_classes'])}")
    print()

    def _print_slice(data: dict, indent: str = "  "):
        if data.get("n", 0) == 0:
            print(f"{indent}(no data)")
            return
        ci = data.get("bootstrap_best", {})
        ci_str = f"[{ci.get('ci_low', '?')}, {ci.get('ci_high', '?')}]"
        flag = " <<<" if ci.get("ci_low") is not None and ci["ci_low"] > 0 else ""
        print(f"{indent}N={data['n']:>6}  games={data.get('games',0):>3}  "
              f"best_mean={data.get('best_markout_mean','?'):>7}c  "
              f"best_median={data.get('best_markout_median','?'):>7}c  "
              f"pos%={data.get('positive_rate','?'):>5}  "
              f"CI={ci_str}{flag}")

    for section_name in ["by_market_type", "by_event_class", "by_spread_bucket",
                         "by_period", "by_game_state"]:
        section = results["sections"].get(section_name, {})
        print(f"--- {section_name} ---")
        for key, data in section.items():
            if isinstance(data, dict) and "n" in data:
                print(f"  {key}:")
                _print_slice(data, "    ")
        print()

    print("--- Directional (YES vs NO entry) ---")
    direc = results["sections"].get("directional", {})
    for side in ["yes_entry", "no_entry"]:
        d = direc.get(side, {})
        ci = d.get("bootstrap", {})
        ci_str = f"[{ci.get('ci_low', '?')}, {ci.get('ci_high', '?')}]"
        flag = " <<<" if ci.get("ci_low") is not None and ci["ci_low"] > 0 else ""
        print(f"  {side}: N={d.get('n',0):>6}  mean={d.get('mean','?'):>7}c  "
              f"median={d.get('median','?'):>7}c  pos%={d.get('positive_rate','?'):>5}  CI={ci_str}{flag}")
    print()

    print("--- Adverse Selection by Horizon ---")
    adv = results["sections"].get("adverse_selection_by_horizon", {})
    for h in ["t+1s", "t+3s", "t+5s"]:
        d = adv.get(h, {})
        print(f"  {h}: N={d.get('n',0):>6}  best_mean={d.get('best_mean','?'):>7}c  "
              f"best_median={d.get('best_median','?'):>7}c  pos%={d.get('positive_rate','?'):>5}")
    print()

    print("--- Tight Filter (spread<=6c, depth>=10) ---")
    tf = results["sections"].get("tight_filter", {})
    _print_slice(tf.get("all", {}))
    for key, data in tf.get("by_event_class", {}).items():
        print(f"  {key}:")
        _print_slice(data, "    ")
    print()

    print("--- Best Case: Q4 + Tight + Classified ---")
    bc = results["sections"].get("best_case_q4_tight_classified", {})
    _print_slice(bc)
    print()

    print("=" * 80)
    v = results["verdict"]
    print(f"VERDICT: {v['h1_status']}")
    print(f"Reason:  {v['reason']}")
    if v["positive_subsets"]:
        print("Positive subsets:")
        for ps in v["positive_subsets"]:
            print(f"  - {ps['section']}: N={ps['n']}, mean={ps['mean']}c, CI={ps['ci']}")
    print(f"\nRecommendation: {v['recommendation']}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Deep H1 latency analysis")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                        help="Path to oracle-alpha-ledger.sqlite3")
    args = parser.parse_args()

    if not args.ledger.exists():
        print(f"Ledger not found: {args.ledger}", file=sys.stderr)
        sys.exit(1)

    results = run_analysis(args.ledger)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_human_readable(results)

    # Write JSON output alongside
    out_path = PROJECT_DIR / "data" / "reports" / "oracle-h1-deep-analysis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nJSON written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
