#!/usr/bin/env python3
"""H8 maker-vs-taker analysis with deployable directional policies.

The original H8 draft overstated the opportunity by:
1. choosing the better side ex post, and
2. averaging passive markouts even when the posted order never filled.

This version reports deployable policies explicitly:
  - passive_yes: always post a YES bid
  - passive_no: always post a NO bid
  - taker_yes: always cross to buy YES
  - taker_no: always cross to buy NO

For passive policies, the primary metric is per-attempt EV:
    markout_if_filled else 0

The ex-post best-side result is retained only as a non-tradeable upper bound.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = PROJECT_DIR / "data" / "oracle-alpha-ledger.sqlite3"


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def _bootstrap_ci(
    values: list[float],
    *,
    cluster_keys: list[str] | None = None,
    samples: int = 500,
    seed: int = 42,
) -> dict:
    if not values:
        return {"mean": None, "ci_low": None, "ci_high": None, "n": 0}

    clusters: dict[str, list[float]] = defaultdict(list)
    if cluster_keys and len(cluster_keys) == len(values):
        for value, key in zip(values, cluster_keys):
            clusters[key].append(value)
    else:
        clusters["all"] = values

    rng = random.Random(seed)
    cluster_names = list(clusters)
    cluster_count = len(cluster_names)
    draws = []
    for _ in range(samples):
        sampled = [cluster_names[rng.randrange(cluster_count)] for _ in range(cluster_count)]
        sample_values = [value for key in sampled for value in clusters[key]]
        if sample_values:
            draws.append(sum(sample_values) / len(sample_values))

    draws.sort()
    lo = max(0, int(len(draws) * 0.025))
    hi = min(len(draws) - 1, int(len(draws) * 0.975))
    return {
        "mean": _mean(values),
        "ci_low": round(draws[lo], 3) if draws else None,
        "ci_high": round(draws[hi], 3) if draws else None,
        "n": len(values),
        "clusters": cluster_count,
    }


def load_maker_opportunities(db: sqlite3.Connection) -> list[dict]:
    """Load baseline/follow-up quote pairs and compute policy-level markouts.

    Passive YES:
      - post a YES bid at baseline bid
      - fill proxy: follow-up ask <= baseline bid
      - filled markout: follow-up midpoint - baseline bid
      - attempt EV: filled markout if filled else 0

    Passive NO:
      - post a NO bid at baseline NO bid (= 100 - baseline YES ask)
      - fill proxy: follow-up YES bid >= baseline YES ask
      - filled markout: baseline YES ask - follow-up midpoint
      - attempt EV: filled markout if filled else 0
    """
    cur = db.cursor()
    cur.execute(
        """
        SELECT event_id, payload_json FROM events
        WHERE event_type = 'market_snapshot' AND payload_json IS NOT NULL
        """
    )

    by_source_ticker: dict[tuple[str, str], dict[float, dict]] = defaultdict(dict)
    for _, payload_json in cur.fetchall():
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        source_id = payload.get("source_event_id", "")
        ticker = payload.get("ticker", "")
        horizon = payload.get("horizon_seconds", -1)
        if not source_id or not ticker or horizon < 0:
            continue
        by_source_ticker[(source_id, ticker)][float(horizon)] = payload

    cur.execute(
        """
        SELECT event_id, payload_json FROM events
        WHERE event_type = 'source_observation'
          AND payload_json LIKE '%"record_kind":"source_event"%'
        """
    )
    source_meta: dict[str, dict] = {}
    for _, payload_json in cur.fetchall():
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        event_id = payload.get("event_id", "")
        if event_id:
            source_meta[event_id] = payload

    print(f"Loaded {len(by_source_ticker)} source-ticker pairs", file=sys.stderr)

    opportunities = []
    for (source_id, ticker), horizons in by_source_ticker.items():
        baseline = horizons.get(0.0) or horizons.get(0)
        if not baseline:
            continue

        baseline_bid = baseline.get("yes_bid_cents", 0)
        baseline_ask = baseline.get("yes_ask_cents", 0)
        baseline_spread = baseline.get("spread_cents", 99)
        if baseline_bid <= 0 or baseline_ask <= 0 or baseline_spread <= 0 or baseline_spread > 8:
            continue

        if not str(ticker).upper().startswith("KXNBAGAME"):
            continue

        source = source_meta.get(source_id, {})
        event_class = (
            baseline.get("derived_event_class")
            or source.get("derived_event_class")
            or "unclassified"
        )
        game_id = str(baseline.get("game_id") or source.get("game_id") or "")
        period = baseline.get("period") or source.get("period") or ""
        baseline_mid = (baseline_bid + baseline_ask) / 2.0

        for horizon, quote in horizons.items():
            if horizon == 0.0:
                continue
            followup_bid = quote.get("yes_bid_cents", 0)
            followup_ask = quote.get("yes_ask_cents", 0)
            if followup_bid <= 0 or followup_ask <= 0:
                continue
            followup_mid = (followup_bid + followup_ask) / 2.0

            passive_yes_markout = followup_mid - baseline_bid
            passive_yes_filled = followup_ask <= baseline_bid
            passive_yes_attempt_ev = passive_yes_markout if passive_yes_filled else 0.0

            passive_no_markout = baseline_ask - followup_mid
            passive_no_filled = followup_bid >= baseline_ask
            passive_no_attempt_ev = passive_no_markout if passive_no_filled else 0.0

            taker_yes_markout = followup_bid - baseline_ask
            taker_no_markout = baseline_bid - followup_ask

            opportunities.append(
                {
                    "source_event_id": source_id,
                    "ticker": ticker,
                    "horizon_seconds": float(horizon),
                    "event_class": event_class,
                    "game_id": game_id,
                    "period": period,
                    "baseline_bid": baseline_bid,
                    "baseline_ask": baseline_ask,
                    "baseline_spread": baseline_spread,
                    "baseline_mid": baseline_mid,
                    "followup_bid": followup_bid,
                    "followup_ask": followup_ask,
                    "followup_mid": followup_mid,
                    "passive_yes_markout": passive_yes_markout,
                    "passive_yes_filled": passive_yes_filled,
                    "passive_yes_attempt_ev": passive_yes_attempt_ev,
                    "passive_no_markout": passive_no_markout,
                    "passive_no_filled": passive_no_filled,
                    "passive_no_attempt_ev": passive_no_attempt_ev,
                    "taker_yes_attempt_ev": taker_yes_markout,
                    "taker_no_attempt_ev": taker_no_markout,
                    "ex_post_best_attempt_ev": max(passive_yes_attempt_ev, passive_no_attempt_ev),
                }
            )

    return opportunities


def analyze_policy(
    opps: list[dict],
    *,
    label: str,
    attempt_key: str,
    fill_key: str | None = None,
    markout_key: str | None = None,
) -> dict:
    if not opps:
        return {"label": label, "n": 0}

    attempt_values = [float(o[attempt_key]) for o in opps]
    cluster_keys = [o["game_id"] for o in opps]

    fill_rate = 1.0
    filled_opps = opps
    if fill_key is not None:
        fill_values = [1.0 if o[fill_key] else 0.0 for o in opps]
        fill_rate = sum(fill_values) / len(fill_values)
        filled_opps = [o for o in opps if o[fill_key]]

    filled_markouts: list[float] = []
    if markout_key is not None and filled_opps:
        filled_markouts = [float(o[markout_key]) for o in filled_opps]

    positive_attempts = sum(1 for value in attempt_values if value > 0)
    return {
        "label": label,
        "n": len(opps),
        "games": len(set(o["game_id"] for o in opps if o["game_id"])),
        "fill_rate": round(fill_rate, 3),
        "attempt_ev_mean": _mean(attempt_values),
        "attempt_ev_median": _median(attempt_values),
        "attempt_positive_rate": round(positive_attempts / len(attempt_values), 3),
        "bootstrap_attempt_ev": _bootstrap_ci(attempt_values, cluster_keys=cluster_keys),
        "filled_count": len(filled_opps),
        "filled_markout_mean": _mean(filled_markouts) if filled_markouts else None,
        "filled_markout_median": _median(filled_markouts) if filled_markouts else None,
        "bootstrap_filled_markout": (
            _bootstrap_ci(
                filled_markouts,
                cluster_keys=[o["game_id"] for o in filled_opps],
            )
            if filled_markouts
            else {"mean": None, "ci_low": None, "ci_high": None, "n": 0}
        ),
    }


def _policy_pack(opps: list[dict], label_prefix: str) -> dict:
    return {
        "passive_yes": analyze_policy(
            opps,
            label=f"{label_prefix} passive_yes",
            attempt_key="passive_yes_attempt_ev",
            fill_key="passive_yes_filled",
            markout_key="passive_yes_markout",
        ),
        "passive_no": analyze_policy(
            opps,
            label=f"{label_prefix} passive_no",
            attempt_key="passive_no_attempt_ev",
            fill_key="passive_no_filled",
            markout_key="passive_no_markout",
        ),
        "taker_yes": analyze_policy(
            opps,
            label=f"{label_prefix} taker_yes",
            attempt_key="taker_yes_attempt_ev",
        ),
        "taker_no": analyze_policy(
            opps,
            label=f"{label_prefix} taker_no",
            attempt_key="taker_no_attempt_ev",
        ),
        "ex_post_upper_bound": analyze_policy(
            opps,
            label=f"{label_prefix} ex_post_upper_bound",
            attempt_key="ex_post_best_attempt_ev",
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
    primary = t5 if t5 else t3 if t3 else t1
    primary_label = "t+5s" if t5 else "t+3s" if t3 else "t+1s"

    results = {
        "summary": {
            "total_opportunities": len(opps),
            "game_markets_only": True,
            "t5": len(t5),
            "t3": len(t3),
            "t1": len(t1),
            "games": len(set(o["game_id"] for o in opps if o["game_id"])),
            "primary_horizon": primary_label,
        },
        "methodology_warnings": [
            "Prior H8 draft chose the better side ex post and was not deployable.",
            "Passive per-attempt EV now uses 0 for unfilled orders instead of unrealized markout.",
            "Ex-post upper bound is retained only as a non-tradeable diagnostic ceiling.",
        ],
        "sections": {},
    }

    results["sections"]["overall"] = _policy_pack(primary, f"overall ({primary_label})")

    by_event_class = {}
    for event_class in sorted(set(o["event_class"] for o in primary)):
        event_opps = [o for o in primary if o["event_class"] == event_class]
        by_event_class[event_class] = _policy_pack(event_opps, f"{event_class} ({primary_label})")
    results["sections"]["by_event_class"] = by_event_class

    by_spread = {}
    for bucket, low, high in [("1c", 0, 1), ("2c", 2, 2), ("3-4c", 3, 4), ("5-8c", 5, 8)]:
        spread_opps = [o for o in primary if low <= o["baseline_spread"] <= high]
        if spread_opps:
            by_spread[bucket] = _policy_pack(spread_opps, f"spread={bucket} ({primary_label})")
    results["sections"]["by_spread"] = by_spread

    horizon_progression = {}
    for horizon_label, horizon_opps in [("t+1s", t1), ("t+3s", t3), ("t+5s", t5)]:
        if not horizon_opps:
            continue
        pack = _policy_pack(horizon_opps, horizon_label)
        horizon_progression[horizon_label] = {
            policy_name: {
                "n": policy_result["n"],
                "attempt_ev_mean": policy_result["attempt_ev_mean"],
                "fill_rate": policy_result["fill_rate"],
            }
            for policy_name, policy_result in pack.items()
        }
    results["sections"]["horizon_progression"] = horizon_progression

    directional_positive = []
    scoped_policies = [("overall", results["sections"]["overall"])]
    scoped_policies.extend(
        (f"event_class:{event_class}", policy_pack)
        for event_class, policy_pack in results["sections"]["by_event_class"].items()
    )
    for scope_name, policy_pack in scoped_policies:
        for policy_name in ("passive_yes", "passive_no", "taker_yes", "taker_no"):
            policy = policy_pack[policy_name]
            ci = policy.get("bootstrap_attempt_ev", {})
            if ci.get("ci_low") is not None and ci["ci_low"] > 0:
                directional_positive.append(
                    {
                        "scope": scope_name,
                        "policy": policy_name,
                        "n": policy["n"],
                        "attempt_ev_mean": policy["attempt_ev_mean"],
                        "ci": [ci["ci_low"], ci["ci_high"]],
                        "fill_rate": policy["fill_rate"],
                    }
                )

    upper_bound = results["sections"]["overall"]["ex_post_upper_bound"]
    upper_bound_ci = upper_bound.get("bootstrap_attempt_ev", {})
    if directional_positive:
        status = "RESEARCH_ONLY"
        reason = (
            "Some fixed-side policies show positive per-attempt EV, but that supports at most a "
            "separate maker strategy. It does not validate Oracle's directional passive execution."
        )
    else:
        status = "INVALIDATED"
        reason = (
            "No deployable fixed-side policy shows a positive per-attempt EV CI above zero. "
            "The prior H8 PASS depended on ex-post side selection."
        )

    results["verdict"] = {
        "h8_status": status,
        "reason": reason,
        "directional_positive_policies": directional_positive,
        "ex_post_upper_bound": {
            "attempt_ev_mean": upper_bound.get("attempt_ev_mean"),
            "ci": [upper_bound_ci.get("ci_low"), upper_bound_ci.get("ci_high")],
            "n": upper_bound.get("n"),
        },
        "recommendation": (
            "Keep Oracle passiveExecution OFF. If pursuing H8 further, treat it as a separate "
            "market-making research track and validate real demo fills/cancels before any deployment."
        ),
    }

    return results


def print_human_readable(results: dict) -> None:
    summary = results["summary"]
    overall = results["sections"]["overall"]

    print("=" * 80)
    print("H8 MAKER-VS-TAKER ANALYSIS (Deployable Policies)")
    print("=" * 80)
    print(f"Opportunities:  {summary['total_opportunities']:,}")
    print(f"Games:          {summary['games']}")
    print(f"Primary:        {summary['primary_horizon']}")
    print(f"t+5s: {summary['t5']:,}  t+3s: {summary['t3']:,}  t+1s: {summary['t1']:,}")
    print()

    print("--- Methodology ---")
    for warning in results.get("methodology_warnings", []):
        print(f"  - {warning}")
    print()

    def _print_policy(name: str, policy: dict, *, indent: str = "  ") -> None:
        ci = policy.get("bootstrap_attempt_ev", {})
        ci_str = f"[{ci.get('ci_low', '?')}, {ci.get('ci_high', '?')}]"
        print(
            f"{indent}{name:<18} N={policy.get('n', 0):>5}  "
            f"attempt_ev={policy.get('attempt_ev_mean', '?'):>6}c  "
            f"CI={ci_str}  fill%={policy.get('fill_rate', 0):>5.1%}  "
            f"filled_markout={policy.get('filled_markout_mean', '?')}"
        )

    print("--- Overall ---")
    for name, policy in overall.items():
        _print_policy(name, policy)
    print()

    print("--- By Event Class ---")
    for event_class, policies in results["sections"]["by_event_class"].items():
        print(f"  {event_class}:")
        for name, policy in policies.items():
            _print_policy(name, policy, indent="    ")
    print()

    print("--- Horizon Progression ---")
    for horizon_label, pack in results["sections"]["horizon_progression"].items():
        print(f"  {horizon_label}:")
        for name, policy in pack.items():
            print(
                f"    {name:<18} attempt_ev={policy.get('attempt_ev_mean', '?'):>6}c  "
                f"fill%={policy.get('fill_rate', 0):>5.1%}"
            )
    print()

    verdict = results["verdict"]
    print("=" * 80)
    print(f"VERDICT: {verdict['h8_status']}")
    print(f"Reason:  {verdict['reason']}")
    if verdict["directional_positive_policies"]:
        print("Directional positive policies:")
        for policy in verdict["directional_positive_policies"]:
            print(
                f"  - {policy['scope']} / {policy['policy']}: "
                f"attempt_ev={policy['attempt_ev_mean']}c  CI={policy['ci']}  "
                f"fill%={policy['fill_rate']:.1%}"
            )
    print(
        "Ex-post upper bound: "
        f"{verdict['ex_post_upper_bound']['attempt_ev_mean']}c "
        f"CI={verdict['ex_post_upper_bound']['ci']}"
    )
    print(f"\nRecommendation: {verdict['recommendation']}")
    print("=" * 80)


def main() -> None:
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
