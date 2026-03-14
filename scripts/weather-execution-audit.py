#!/usr/bin/env python3
"""Summarize weather trade execution quality by city and execution style."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_weather_trades(path):
    if not path.exists():
        return []
    trades = json.loads(path.read_text())
    return [t for t in trades if t.get("source_bot") == "weather"]


def summarize(trades):
    city_rows = defaultdict(lambda: {
        "trades": 0,
        "executed": 0,
        "resting": 0,
        "maker": 0,
        "maker_executed": 0,
        "taker_executed": 0,
        "total_edge": 0.0,
        "bias_records": 0,
        "total_abs_bias": 0.0,
        "bias_conflict": 0,
        "bias_capped": 0,
    })
    overall = {
        "trades": 0,
        "executed": 0,
        "resting": 0,
        "maker": 0,
        "maker_executed": 0,
        "taker_executed": 0,
        "total_edge": 0.0,
        "bias_records": 0,
        "total_abs_bias": 0.0,
        "bias_conflict": 0,
        "bias_capped": 0,
    }

    for trade in trades:
        city = trade.get("city") or "UNKNOWN"
        row = city_rows[city]
        status = trade.get("status")
        style = trade.get("execution_style") or "legacy"
        edge = float(trade.get("edge") or 0.0)
        bias_applied = trade.get("bias_applied_f")
        bias_conflict = bool(trade.get("bias_conflict"))
        bias_capped = bool(trade.get("bias_capped"))

        for target in (row, overall):
            target["trades"] += 1
            target["total_edge"] += edge
            if status == "executed":
                target["executed"] += 1
            if status == "resting":
                target["resting"] += 1
            if style == "maker":
                target["maker"] += 1
                if status == "executed":
                    target["maker_executed"] += 1
            elif status == "executed":
                target["taker_executed"] += 1
            if isinstance(bias_applied, (int, float)):
                target["bias_records"] += 1
                target["total_abs_bias"] += abs(float(bias_applied))
                if bias_conflict:
                    target["bias_conflict"] += 1
                if bias_capped:
                    target["bias_capped"] += 1

    def finalize(stats):
        trades = max(1, stats["trades"])
        maker = max(1, stats["maker"])
        executed = max(1, stats["executed"])
        bias_records = max(1, stats["bias_records"])
        return {
            **stats,
            "avg_edge": round(stats["total_edge"] / trades, 3),
            "maker_share": round(stats["maker"] / trades, 3),
            "fill_rate": round(stats["executed"] / trades, 3),
            "maker_fill_rate": round(stats["maker_executed"] / maker, 3),
            "maker_exec_share": round(stats["maker_executed"] / executed, 3) if stats["executed"] else 0.0,
            "avg_abs_bias_applied": round(stats["total_abs_bias"] / bias_records, 3) if stats["bias_records"] else None,
            "bias_conflict_share": round(stats["bias_conflict"] / bias_records, 3) if stats["bias_records"] else None,
            "bias_cap_share": round(stats["bias_capped"] / bias_records, 3) if stats["bias_records"] else None,
        }

    return finalize(overall), {city: finalize(stats) for city, stats in sorted(city_rows.items())}


def print_summary(overall, per_city):
    print("OVERALL")
    print(json.dumps(overall, indent=2, sort_keys=True))
    print("\nPER CITY")
    for city, stats in per_city.items():
        print(
            f"{city:>5} trades={stats['trades']:>2} exec={stats['executed']:>2} rest={stats['resting']:>2} "
            f"maker={stats['maker']:>2} fill={stats['fill_rate']:.2f} maker_fill={stats['maker_fill_rate']:.2f} "
            f"avg_edge={stats['avg_edge']:.3f} avg_abs_bias={stats['avg_abs_bias_applied']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Weather execution audit")
    parser.add_argument(
        "--trades-path",
        default=str(PROJECT_DIR / "data" / "kalshi-trades.json"),
        help="Path to trade log",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    trades = load_weather_trades(Path(args.trades_path))
    overall, per_city = summarize(trades)
    if args.json:
        print(json.dumps({"overall": overall, "per_city": per_city}, indent=2, sort_keys=True))
        return
    print_summary(overall, per_city)


if __name__ == "__main__":
    main()
