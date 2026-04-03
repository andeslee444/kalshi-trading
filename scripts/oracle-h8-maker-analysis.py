#!/usr/bin/env python3
"""H8 Maker-vs-Taker Analysis: passive fill probability and markout estimation.

The H1 analysis showed taker markouts of -0.76c on game markets (the spread
crossing destroys the information edge). H8 tests whether passive limit orders
(posting at the bid or ask instead of crossing) could recover the spread cost.

Key questions:
1. How often does the bid/ask get lifted within 5s of a source event?
2. What is the simulated passive markout assuming queue position?
3. What is the fill probability by event class?
4. What is the net EV under passive execution assumptions?

Uses the existing quote snapshot pairs in the alpha ledger.

Usage:
    python3 scripts/oracle-h8-maker-analysis.py [--json]
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


def _mean(values: list) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _median(values: list) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _bootstrap_ci(values: list[float], cluster_keys: list[str] | None = None,
                  samples: int = 500, seed: int = 42) -> dict:
    if not values:
        return {"mean": None, "ci_low": None, "ci_high": None, "n": 0}
    clusters: dict[str, list[float]] = defaultdict(list)
    if cluster_keys and len(cluster_keys) == len(values):
        for v, k in zip(values, cluster_keys):
            clusters[k].append(v)
    else:
        clusters["all"] = values
    rng = random.Random(seed)
    keys = list(clusters)
    nc = len(keys)
    draws = []
    for _ in range(samples):
        s = [keys[rng.randrange(nc)] for _ in range(nc)]
        d = [v for k in s for v in clusters[k]]
        if d:
            draws.append(sum(d) / len(d))
    draws.sort()
    lo = max(0, int(len(draws) * 0.025))
    hi = min(len(draws) - 1, int(len(draws) * 0.975))
    return {
        "mean": _mean(values),
        "ci_low": round(draws[lo], 3) if draws else None,
        "ci_high": round(draws[hi], 3) if draws else None,
        "n": len(values),
        "clusters": nc,
    }


def load_maker_opportunities(db: sqlite3.Connection) -> list[dict]:
    """Load paired baseline->followup snapshots and compute passive markouts.

    A passive (maker) entry means:
    - YES maker: post a limit buy at baseline YES bid. Get filled when someone
      sells into your bid. Then mark-to-market at the followup midpoint.
      Passive YES markout = followup_mid - baseline_bid
    - NO maker: post a limit buy at baseline NO bid (= 100 - baseline_ask).
      Passive NO markout = (100 - followup_mid) - (100 - baseline_ask)
                         = baseline_ask - followup_mid

    Fill probability proxy: did the price cross through your limit within the
    horizon window? If followup_ask <= baseline_bid, a seller hit your bid.
    """
    cur = db.cursor()
    cur.execute("""
        SELECT event_id, payload_json FROM events
        WHERE event_type = 'market_snapshot' AND payload_json IS NOT NULL
    """)

    by_source_ticker: dict[tuple[str, str], dict[float, dict]] = defaultdict(dict)
    for row in cur.fetchall():
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

    # Load source metadata
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

    print(f"Loaded {len(by_source_ticker)} source-ticker pairs", file=sys.stderr)

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

        if b_bid <= 0 or b_ask <= 0 or b_spread <= 0 or b_spread > 8:
            continue

        # Only game markets (props are too thin)
        ticker_upper = ticker.upper()
        is_game = ticker_upper.startswith("KXNBAGAME")
        if not is_game:
            continue

        src = source_meta.get(source_id, {})
        event_class = (baseline.get("derived_event_class")
                       or src.get("derived_event_class")
                       or "unclassified")
        game_id = str(baseline.get("game_id") or src.get("game_id") or "")
        period = baseline.get("period") or src.get("period") or ""

        b_mid = (b_bid + b_ask) / 2.0

        for h, quote in horizons.items():
            if h == 0.0 or h == 0:
                continue

            q_bid = quote.get("yes_bid_cents", 0)
            q_ask = quote.get("yes_ask_cents", 0)
            if q_bid <= 0 or q_ask <= 0:
                continue
            q_mid = (q_bid + q_ask) / 2.0

            # Passive YES entry: post bid at b_bid, fill when price drops
            # Markout = followup_mid - entry_price (b_bid)
            passive_yes_markout = q_mid - b_bid
            # Fill proxy: did followup ask drop to or below our bid?
            passive_yes_filled = q_ask <= b_bid

            # Passive NO entry: post at 100 - b_ask (NO bid)
            # Entry cost = 100 - b_ask. Markout = (100 - q_mid) - (100 - b_ask) = b_ask - q_mid
            passive_no_markout = b_ask - q_mid
            # Fill proxy: did followup bid rise to or above our ask?
            passive_no_filled = q_bid >= b_ask

            # Taker markouts for comparison
            taker_yes_markout = q_bid - b_ask
            taker_no_markout = b_bid - q_ask

            # Best passive: assume we take the direction with higher markout
            best_passive = max(passive_yes_markout, passive_no_markout)
            any_passive_filled = passive_yes_filled or passive_no_filled

            opportunities.append({
                "source_event_id": source_id,
                "ticker": ticker,
                "horizon_seconds": float(h),
                "event_class": event_class,
                "game_id": game_id,
                "period": period,
                "baseline_bid": b_bid,
                "baseline_ask": b_ask,
                "baseline_spread": b_spread,
                "baseline_mid": b_mid,
                "followup_bid": q_bid,
                "followup_ask": q_ask,
                "followup_mid": q_mid,
                "passive_yes_markout": passive_yes_markout,
                "passive_no_markout": passive_no_markout,
                "best_passive_markout": best_passive,
                "passive_yes_filled": passive_yes_filled,
                "passive_no_filled": passive_no_filled,
                "any_passive_filled": any_passive_filled,
                "taker_yes_markout": taker_yes_markout,
                "taker_no_markout": taker_no_markout,
                "best_taker_markout": max(taker_yes_markout, taker_no_markout),
                "mid_change": q_mid - b_mid,
            })

    return opportunities


def analyze(opps: list[dict], label: str) -> dict:
    if not opps:
        return {"label": label, "n": 0}

    passive_markouts = [o["best_passive_markout"] for o in opps]
    taker_markouts = [o["best_taker_markout"] for o in opps]
    fill_rates = [1.0 if o["any_passive_filled"] else 0.0 for o in opps]
    cluster_keys = [o["game_id"] for o in opps]

    # Conditional markout: only for filled opportunities
    filled_opps = [o for o in opps if o["any_passive_filled"]]
    filled_markouts = [o["best_passive_markout"] for o in filled_opps] if filled_opps else []

    return {
        "label": label,
        "n": len(opps),
        "games": len(set(o["game_id"] for o in opps)),
        "passive_fill_rate": round(sum(fill_rates) / len(fill_rates), 3),
        "passive_markout_mean": _mean(passive_markouts),
        "passive_markout_median": _median(passive_markouts),
        "passive_positive_rate": round(sum(1 for m in passive_markouts if m > 0) / len(passive_markouts), 3),
        "taker_markout_mean": _mean(taker_markouts),
        "taker_markout_median": _median(taker_markouts),
        "maker_vs_taker_advantage": round(
            (_mean(passive_markouts) or 0) - (_mean(taker_markouts) or 0), 3
        ),
        "bootstrap_passive": _bootstrap_ci(
            [float(m) for m in passive_markouts], cluster_keys=cluster_keys
        ),
        "bootstrap_taker": _bootstrap_ci(
            [float(m) for m in taker_markouts], cluster_keys=cluster_keys
        ),
        "filled_count": len(filled_opps),
        "filled_markout_mean": _mean(filled_markouts) if filled_markouts else None,
        "filled_markout_median": _median(filled_markouts) if filled_markouts else None,
        "bootstrap_filled": (
            _bootstrap_ci([float(m) for m in filled_markouts],
                          cluster_keys=[o["game_id"] for o in filled_opps])
            if filled_opps else {"mean": None, "ci_low": None, "ci_high": None, "n": 0}
        ),
    }


def run_analysis(ledger_path: Path) -> dict:
    db = sqlite3.connect(str(ledger_path))
    opps = load_maker_opportunities(db)
    db.close()

    print(f"Total game-market passive opportunities: {len(opps)}", file=sys.stderr)

    t5 = [o for o in opps if o["horizon_seconds"] == 5.0]
    t3 = [o for o in opps if o["horizon_seconds"] == 3.0]
    t1 = [o for o in opps if o["horizon_seconds"] == 1.0]

    results = {
        "summary": {
            "total_opportunities": len(opps),
            "game_markets_only": True,
            "t5": len(t5), "t3": len(t3), "t1": len(t1),
            "games": len(set(o["game_id"] for o in opps if o["game_id"])),
        },
        "sections": {},
    }

    primary = t5 if t5 else t3 if t3 else t1
    primary_label = "t+5s" if t5 else "t+3s" if t3 else "t+1s"

    results["sections"]["overall"] = analyze(primary, f"all game mkts ({primary_label})")

    # By event class
    ec_results = {}
    for ec in sorted(set(o["event_class"] for o in primary)):
        ec_opps = [o for o in primary if o["event_class"] == ec]
        ec_results[ec] = analyze(ec_opps, f"{ec} ({primary_label})")
    results["sections"]["by_event_class"] = ec_results

    # By spread bucket
    spread_results = {}
    for bucket, lo, hi in [("1c", 0, 1), ("2c", 2, 2), ("3-4c", 3, 4), ("5-8c", 5, 8)]:
        bucket_opps = [o for o in primary if lo <= o["baseline_spread"] <= hi]
        if bucket_opps:
            spread_results[bucket] = analyze(bucket_opps, f"spread={bucket} ({primary_label})")
    results["sections"]["by_spread"] = spread_results

    # Horizon progression
    hp = {}
    for label, h_opps in [("t+1s", t1), ("t+3s", t3), ("t+5s", t5)]:
        if h_opps:
            pm = [o["best_passive_markout"] for o in h_opps]
            tm = [o["best_taker_markout"] for o in h_opps]
            fr = [1.0 if o["any_passive_filled"] else 0.0 for o in h_opps]
            hp[label] = {
                "n": len(h_opps),
                "passive_mean": _mean(pm),
                "taker_mean": _mean(tm),
                "fill_rate": round(sum(fr) / len(fr), 3),
                "advantage": round((_mean(pm) or 0) - (_mean(tm) or 0), 3),
            }
    results["sections"]["horizon_progression"] = hp

    # Verdict
    overall = results["sections"]["overall"]
    ci = overall.get("bootstrap_passive", {})
    ci_above_zero = ci.get("ci_low") is not None and ci["ci_low"] > 0

    ec_positive = []
    for ec, data in ec_results.items():
        ec_ci = data.get("bootstrap_passive", {})
        if ec_ci.get("ci_low") is not None and ec_ci["ci_low"] > 0:
            ec_positive.append({
                "event_class": ec,
                "n": data["n"],
                "passive_mean": data["passive_markout_mean"],
                "ci": [ec_ci["ci_low"], ec_ci["ci_high"]],
                "fill_rate": data["passive_fill_rate"],
            })

    fill_rate = overall.get("passive_fill_rate", 0)
    advantage = overall.get("maker_vs_taker_advantage", 0)

    if ci_above_zero:
        verdict = "PASS"
        reason = (
            f"Passive markout CI above zero: [{ci['ci_low']}, {ci['ci_high']}]. "
            f"Maker advantage over taker: {advantage:+.1f}c. Fill rate: {fill_rate:.1%}."
        )
    elif ec_positive:
        verdict = "PARTIAL_PASS"
        reason = (
            f"Overall passive CI crosses zero, but {len(ec_positive)} event class(es) show "
            f"positive passive markout with CI above zero."
        )
    else:
        verdict = "KILLED"
        reason = "No event class shows positive passive markout with CI above zero."

    results["verdict"] = {
        "h8_status": verdict,
        "reason": reason,
        "maker_vs_taker_advantage_cents": advantage,
        "passive_fill_rate": fill_rate,
        "positive_event_classes": ec_positive,
        "recommendation": (
            f"Passive execution recovers {advantage:+.1f}c over taker. "
            + ("Proceed to passive shadow trading. " if verdict in ("PASS", "PARTIAL_PASS") else "")
            + f"Fill rate of {fill_rate:.1%} means {fill_rate*100:.0f}% of posted orders would fill."
        ),
    }

    return results


def print_human_readable(results: dict):
    s = results["summary"]
    print("=" * 80)
    print("H8 MAKER-VS-TAKER ANALYSIS (Game Markets)")
    print("=" * 80)
    print(f"Opportunities:  {s['total_opportunities']:,}")
    print(f"Games:          {s['games']}")
    print(f"t+5s: {s['t5']:,}  t+3s: {s['t3']:,}  t+1s: {s['t1']:,}")
    print()

    def _p(data: dict, indent: str = "  "):
        if data.get("n", 0) == 0:
            print(f"{indent}(no data)")
            return
        pci = data.get("bootstrap_passive", {})
        tci = data.get("bootstrap_taker", {})
        pci_str = f"[{pci.get('ci_low','?')}, {pci.get('ci_high','?')}]"
        tci_str = f"[{tci.get('ci_low','?')}, {tci.get('ci_high','?')}]"
        flag = " <<<" if pci.get("ci_low") is not None and pci["ci_low"] > 0 else ""
        print(f"{indent}N={data['n']:>5}  fill%={data.get('passive_fill_rate',0):>5.1%}  "
              f"passive={data.get('passive_markout_mean','?'):>6}c CI={pci_str}  "
              f"taker={data.get('taker_markout_mean','?'):>6}c CI={tci_str}  "
              f"adv={data.get('maker_vs_taker_advantage',0):>+5.1f}c{flag}")

    print("--- Overall ---")
    _p(results["sections"]["overall"])
    print()

    print("--- By Event Class ---")
    for ec, data in results["sections"]["by_event_class"].items():
        print(f"  {ec}:")
        _p(data, "    ")
    print()

    print("--- By Spread ---")
    for sp, data in results["sections"]["by_spread"].items():
        print(f"  {sp}:")
        _p(data, "    ")
    print()

    print("--- Horizon Progression ---")
    for h, data in results["sections"]["horizon_progression"].items():
        print(f"  {h}: N={data['n']:>5}  passive={data['passive_mean']:>6}c  "
              f"taker={data['taker_mean']:>6}c  fill%={data['fill_rate']:>5.1%}  "
              f"adv={data['advantage']:>+5.1f}c")
    print()

    v = results["verdict"]
    print("=" * 80)
    print(f"VERDICT: {v['h8_status']}")
    print(f"Reason:  {v['reason']}")
    if v["positive_event_classes"]:
        for ec in v["positive_event_classes"]:
            print(f"  + {ec['event_class']}: N={ec['n']}  passive={ec['passive_mean']}c  "
                  f"CI={ec['ci']}  fill%={ec['fill_rate']:.1%}")
    print(f"\nRecommendation: {v['recommendation']}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="H8 maker-vs-taker analysis")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args()

    if not args.ledger.exists():
        print(f"Ledger not found: {args.ledger}", file=sys.stderr)
        sys.exit(1)

    results = run_analysis(args.ledger)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_human_readable(results)

    out_path = PROJECT_DIR / "data" / "reports" / "oracle-h8-maker-analysis.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nJSON written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
