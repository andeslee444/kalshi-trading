#!/usr/bin/env python3
"""Build a derived shadow artifact that ranks weather promotion candidates.

This script is Phase-4 safe: it reads existing local artifacts only and writes
an optional non-canonical promotion review artifact under data/.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_OBSERVATION_PACK_PATH = DATA_DIR / "weather-observation-pack.json"
DEFAULT_CITY_AUDIT_PATH = DATA_DIR / "weather-city-audit.json"
DEFAULT_OUTPUT_PATH = DATA_DIR / "weather-promotion-candidates.json"

sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))
from kalshi_auth import atomic_write_json  # noqa: E402


DEFAULT_THRESHOLDS = {
    "min_expand_pnl_cents": 50000,
    "min_expand_settled": 15,
    "min_expand_win_rate": 0.67,
    "min_tighten_pnl_cents": 2000,
    "min_tighten_settled": 10,
    "shadow_only_settled_max": 4,
}

ACTION_PRIORITY = {
    "expand": 0,
    "expand_after_refresh": 1,
    "hold": 2,
    "tighten": 3,
    "shadow_only": 4,
}


def _load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _safe_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _city_index(pack):
    pnl_rows = pack.get("city_pnl", {}).get("by_city", []) if isinstance(pack, dict) else []
    conflict_rows = pack.get("city_bias", {}).get("conflicts", []) if isinstance(pack, dict) else []
    pnl_map = {row.get("city"): row for row in pnl_rows if row.get("city")}
    conflict_map = {row.get("city"): row for row in conflict_rows if row.get("city")}
    return pnl_map, conflict_map


def _audit_index(rows):
    if not rows:
        return {}
    return {row.get("city"): row for row in rows if row.get("city")}


def _merge_city_record(city, pnl_row, conflict_row, audit_row):
    conflict_source = conflict_row or audit_row or {}
    record = {
        "city": city,
        "pnl_cents": _safe_int(pnl_row.get("pnl_cents") if pnl_row else 0),
        "settled": _safe_int(pnl_row.get("settled") if pnl_row else 0),
        "trades": _safe_int(pnl_row.get("trades") if pnl_row else 0),
        "win_rate": _safe_float(pnl_row.get("win_rate") if pnl_row else None),
        "fees_cents": _safe_int(pnl_row.get("fees_cents") if pnl_row else 0),
        "weather_pnl_cents": _safe_int(conflict_source.get("weather_pnl_cents"), default=0),
        "weather_trades": _safe_int(conflict_source.get("weather_trades"), default=0),
        "bias_conflict": bool(conflict_source.get("bias_conflict", False)),
        "sign_flip": bool(conflict_source.get("sign_flip", False)),
        "gap_f": _safe_float(conflict_source.get("gap_f")),
        "historical_bias_f": _safe_float(conflict_source.get("historical_bias_f") or conflict_source.get("hist_bias_f")),
        "live_bias_f": _safe_float(conflict_source.get("live_bias_f")),
        "live_samples": _safe_int(conflict_source.get("live_samples") or conflict_source.get("live_n"), default=0),
        "live_confidence": _safe_float(conflict_source.get("live_confidence"), default=0.0),
        "guarded_bias_f": _safe_float((audit_row or {}).get("guarded_bias_f")),
        "blend_alpha": _safe_float((audit_row or {}).get("blend_alpha")),
        "bias_capped": bool((audit_row or {}).get("bias_capped", False)),
        "trade_count": _safe_int(
            (audit_row or {}).get("trade_count"),
            default=_safe_int(pnl_row.get("trades") if pnl_row else (conflict_row or {}).get("weather_trades"), 0),
        ),
        "executed_count": _safe_int((audit_row or {}).get("executed_count"), default=0),
        "resting_count": _safe_int((audit_row or {}).get("resting_count"), default=0),
        "maker_count": _safe_int((audit_row or {}).get("maker_count"), default=0),
    }
    record["settled"] = record["settled"] or record["trade_count"]
    record["trades"] = record["trades"] or record["trade_count"]
    return record


def classify_city(record, thresholds=None):
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    settled = record.get("settled", 0)
    pnl_cents = record.get("pnl_cents", 0)
    win_rate = record.get("win_rate")
    has_conflict = bool(record.get("bias_conflict") or record.get("sign_flip"))

    if settled <= thresholds["shadow_only_settled_max"]:
        return "shadow_only"
    if (
        pnl_cents >= thresholds["min_expand_pnl_cents"]
        and settled >= thresholds["min_expand_settled"]
        and win_rate is not None
        and win_rate >= thresholds["min_expand_win_rate"]
    ):
        if has_conflict:
            return "expand_after_refresh"
        return "expand"
    if pnl_cents <= thresholds["min_tighten_pnl_cents"] and settled >= thresholds["min_tighten_settled"]:
        return "tighten"
    return "hold"


def _candidate_reason(record, action):
    pnl_dollars = record.get("pnl_cents", 0) / 100.0
    settled = record.get("settled", 0)
    win_rate = record.get("win_rate")
    if action == "expand":
        return (
            f"strong realized pnl ${pnl_dollars:+.2f}, "
            f"{settled} settled trades"
            + (f", win rate {win_rate:.1%}" if win_rate is not None else "")
        )
    if action == "expand_after_refresh":
        return (
            f"strong realized pnl ${pnl_dollars:+.2f}, "
            f"{settled} settled trades, but live-vs-historical bias conflict "
            "still requires stale-prior refresh and shadow-pack confirmation"
        )
    if action == "tighten":
        return (
            f"weak realized pnl ${pnl_dollars:+.2f}, "
            f"{settled} settled trades"
            + (f", win rate {win_rate:.1%}" if win_rate is not None else "")
        )
    if action == "shadow_only":
        return f"insufficient settled sample ({settled})"
    return f"keep current posture pending fresh shadow pack (${pnl_dollars:+.2f}, {settled} settled)"


def _action_sort_key(record):
    action = record["recommended_action"]
    if action == "expand":
        return (
            ACTION_PRIORITY[action],
            -record.get("pnl_cents", 0),
            -record.get("settled", 0),
            -(record.get("win_rate") or 0.0),
            record["city"],
        )
    if action == "expand_after_refresh":
        return (
            ACTION_PRIORITY[action],
            -record.get("pnl_cents", 0),
            -record.get("settled", 0),
            -(record.get("win_rate") or 0.0),
            record["city"],
        )
    if action == "hold":
        return (
            ACTION_PRIORITY[action],
            -record.get("pnl_cents", 0),
            -record.get("settled", 0),
            -(record.get("win_rate") or 0.0),
            record["city"],
        )
    if action == "tighten":
        return (
            ACTION_PRIORITY[action],
            record.get("pnl_cents", 0),
            record.get("win_rate") if record.get("win_rate") is not None else 1.0,
            -(record.get("gap_f") or 0.0),
            record["city"],
        )
    return (
        ACTION_PRIORITY[action],
        -record.get("settled", 0),
        -record.get("trade_count", 0),
        record["city"],
    )


def build_promotion_artifact(
    observation_pack,
    city_audit_rows=None,
    thresholds=None,
    now=None,
    observation_pack_path=DEFAULT_OBSERVATION_PACK_PATH,
    city_audit_path=DEFAULT_CITY_AUDIT_PATH,
):
    if not isinstance(observation_pack, dict):
        observation_pack = {}
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    now = now or datetime.now(timezone.utc)

    pnl_map, conflict_map = _city_index(observation_pack)
    audit_map = _audit_index(city_audit_rows or [])
    cities = sorted(set(pnl_map) | set(conflict_map) | set(audit_map))

    ranked = []
    for city in cities:
        record = _merge_city_record(city, pnl_map.get(city), conflict_map.get(city), audit_map.get(city))
        record["recommended_action"] = classify_city(record, thresholds=thresholds)
        record["promotion_reason"] = _candidate_reason(record, record["recommended_action"])
        ranked.append(record)

    ranked.sort(key=_action_sort_key)
    for idx, record in enumerate(ranked, start=1):
        record["rank"] = idx

    action_counts = Counter(record["recommended_action"] for record in ranked)
    artifact = {
        "artifact_type": "weather_promotion_candidates",
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "source_artifacts": {
            "weather_observation_pack": observation_pack.get("generated_at"),
            "weather_observation_pack_path": str(observation_pack_path),
            "weather_city_audit_path": str(city_audit_path),
        },
        "thresholds": thresholds,
        "summary": {
            "counts_by_action": dict(sorted(action_counts.items())),
            "top_expansion_candidates": [row["city"] for row in ranked if row["recommended_action"] == "expand"][:3],
            "top_expand_after_refresh_candidates": [
                row["city"] for row in ranked if row["recommended_action"] == "expand_after_refresh"
            ][:3],
            "top_tighten_candidates": [row["city"] for row in ranked if row["recommended_action"] == "tighten"][:3],
            "top_shadow_only_candidates": [row["city"] for row in ranked if row["recommended_action"] == "shadow_only"][:6],
        },
        "ranked_cities": ranked,
    }
    return artifact


def _load_city_audit_rows(path):
    payload = _load_json(path, default={})
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return payload["rows"]
    if isinstance(payload, list):
        return payload
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build weather promotion candidates from local artifacts")
    parser.add_argument(
        "--observation-pack-path",
        default=str(DEFAULT_OBSERVATION_PACK_PATH),
        help="Path to weather-observation-pack.json",
    )
    parser.add_argument(
        "--city-audit-path",
        default=str(DEFAULT_CITY_AUDIT_PATH),
        help="Optional path to a saved weather-city-audit JSON artifact",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output path for the derived promotion artifact",
    )
    parser.add_argument("--save", action="store_true", help="Write the derived artifact to disk")
    parser.add_argument("--json", action="store_true", help="Print the derived artifact as JSON")
    parser.add_argument("--min-expand-pnl-cents", type=int, default=DEFAULT_THRESHOLDS["min_expand_pnl_cents"])
    parser.add_argument("--min-expand-settled", type=int, default=DEFAULT_THRESHOLDS["min_expand_settled"])
    parser.add_argument("--min-expand-win-rate", type=float, default=DEFAULT_THRESHOLDS["min_expand_win_rate"])
    parser.add_argument("--min-tighten-pnl-cents", type=int, default=DEFAULT_THRESHOLDS["min_tighten_pnl_cents"])
    parser.add_argument("--min-tighten-settled", type=int, default=DEFAULT_THRESHOLDS["min_tighten_settled"])
    parser.add_argument("--shadow-only-settled-max", type=int, default=DEFAULT_THRESHOLDS["shadow_only_settled_max"])
    args = parser.parse_args(argv)

    observation_pack = _load_json(Path(args.observation_pack_path), default={})
    city_audit_rows = _load_city_audit_rows(Path(args.city_audit_path))
    thresholds = {
        "min_expand_pnl_cents": args.min_expand_pnl_cents,
        "min_expand_settled": args.min_expand_settled,
        "min_expand_win_rate": args.min_expand_win_rate,
        "min_tighten_pnl_cents": args.min_tighten_pnl_cents,
        "min_tighten_settled": args.min_tighten_settled,
        "shadow_only_settled_max": args.shadow_only_settled_max,
    }
    artifact = build_promotion_artifact(
        observation_pack,
        city_audit_rows=city_audit_rows,
        thresholds=thresholds,
        observation_pack_path=Path(args.observation_pack_path),
        city_audit_path=Path(args.city_audit_path),
    )

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = PROJECT_DIR / output_path

    if args.save:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output_path, artifact)

    if args.json:
        print(json.dumps(artifact, indent=2, sort_keys=True))
        return

    print(f"Derived weather promotion candidates for {len(artifact['ranked_cities'])} cities")
    if args.save:
        print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()
