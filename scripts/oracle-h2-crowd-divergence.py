#!/usr/bin/env python3
"""H2 Crowd Divergence Analysis: Real Sports crowd probability vs Kalshi game price.

Tests whether Real Sports crowd win probabilities diverge from Kalshi game
market prices, and whether that divergence predicts Kalshi price movement.

Data sources:
1. Real-time: fetch current crowd probs + Kalshi orderbooks for live/upcoming games
2. Alpha ledger: pair crowd_probability snapshots with market_snapshot quotes (new capture)
3. Historical: crowd probabilityHistory timeseries from the per-game endpoint

Usage:
    python3 scripts/oracle-h2-crowd-divergence.py [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from dotenv import load_dotenv
load_dotenv(PROJECT_DIR / ".env")


def _mean(values: list) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _median(values: list) -> float | None:
    return round(statistics.median(values), 4) if values else None


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
        "ci_low": round(draws[lo], 4) if draws else None,
        "ci_high": round(draws[hi], 4) if draws else None,
        "n": len(values),
        "clusters": nc,
    }


async def fetch_live_divergence() -> list[dict]:
    """Fetch current crowd probabilities and Kalshi prices for all games."""
    from domain.oracle.real_sports_client import RealSportsClient, RealSportsConfig
    from domain.oracle.market_mapper import normalize_team, match_game_markets
    from infra.kalshi_client import KalshiClient

    real_config = RealSportsConfig.from_bots_config({
        "realSports": {
            "baseUrl": "https://web.realapp.com",
            "wsUrl": "https://web.realsports.io",
        }
    })
    real_client = RealSportsClient(real_config)
    kalshi_client = KalshiClient()

    # Fetch crowd markets (per-game for richer data)
    home = await real_client.get_home_feed("nba")
    games = home.get("latestDayContent", {}).get("games", []) if isinstance(home, dict) else []

    kalshi_markets = kalshi_client.get_all_markets("KXNBAGAME", "open", max_pages=5)

    results = []
    for g in games:
        game_id = g.get("id") or g.get("gameId")
        if not game_id:
            continue
        status = g.get("status", "unknown")
        home_team = g.get("homeTeam", {}).get("name", "")
        away_team = g.get("awayTeam", {}).get("name", "")

        # Fetch per-game crowd markets
        per_game = await real_client._get(f"/predictions/game/nba/{game_id}/markets")
        crowd_markets = per_game.get("markets", []) if isinstance(per_game, dict) else []

        game_winner = None
        for m in crowd_markets:
            if m.get("label") == "Game Winner":
                game_winner = m
                break

        if not game_winner:
            continue

        outcomes = game_winner.get("outcomes", [])
        if len(outcomes) != 2:
            continue

        # Extract crowd probabilities
        crowd_probs = {}
        for o in outcomes:
            crowd_probs[o.get("key", "")] = o.get("probability", 0)

        volume = game_winner.get("volumeDisplay", "0")
        history = game_winner.get("probabilityHistory", [])

        # Match to Kalshi markets
        import datetime as dt
        scheduled_day = g.get("dateTime", "")
        game_date = None
        if scheduled_day:
            try:
                game_date = dt.datetime.fromisoformat(scheduled_day.replace("Z", "+00:00")).date()
            except ValueError:
                pass

        matched = []
        if game_date:
            matched = match_game_markets(home_team, away_team, game_date, kalshi_markets)

        for kalshi_mkt in matched:
            ticker = kalshi_mkt.get("ticker", "")
            yes_price = kalshi_mkt.get("yes_price", 0)
            if yes_price <= 0:
                # Try to get from orderbook
                try:
                    ob = kalshi_client.get_orderbook(ticker)
                    fp = ob.get("orderbook_fp", ob.get("orderbook", {}))
                    yes_bids = fp.get("yes_dollars", [])
                    yes_asks = fp.get("no_dollars", [])  # no_dollars = yes_asks in Kalshi format
                    if yes_bids:
                        yes_price = int(float(yes_bids[0][0]) * 100)
                except Exception:
                    continue

            if yes_price <= 0 or yes_price >= 100:
                continue

            kalshi_implied = yes_price / 100.0

            # Determine which team this ticker is for
            home_code = normalize_team(home_team)
            away_code = normalize_team(away_team)
            ticker_team = None
            if home_code and home_code in ticker:
                ticker_team = home_code
                crowd_prob = crowd_probs.get(home_code, 0)
            elif away_code and away_code in ticker:
                ticker_team = away_code
                crowd_prob = crowd_probs.get(away_code, 0)
            else:
                continue

            divergence = crowd_prob - kalshi_implied

            results.append({
                "game_id": game_id,
                "ticker": ticker,
                "team": ticker_team,
                "home_team": home_team,
                "away_team": away_team,
                "status": status,
                "crowd_prob": round(crowd_prob, 4),
                "kalshi_implied": round(kalshi_implied, 4),
                "divergence": round(divergence, 4),
                "abs_divergence": round(abs(divergence), 4),
                "crowd_volume": volume,
                "crowd_history_length": len(history),
            })

    await real_client.close()
    return results


def analyze_divergence(rows: list[dict]) -> dict:
    """Analyze crowd-Kalshi divergence patterns."""
    if not rows:
        return {"n": 0, "error": "No divergence data"}

    divergences = [r["divergence"] for r in rows]
    abs_divs = [r["abs_divergence"] for r in rows]
    game_ids = [str(r["game_id"]) for r in rows]

    # By status
    by_status = defaultdict(list)
    for r in rows:
        by_status[r["status"]].append(r)

    status_analysis = {}
    for status, items in by_status.items():
        divs = [r["divergence"] for r in items]
        status_analysis[status] = {
            "n": len(items),
            "mean_divergence": _mean(divs),
            "median_divergence": _median(divs),
            "mean_abs_divergence": _mean([abs(d) for d in divs]),
            "positive_rate": round(sum(1 for d in divs if d > 0) / len(divs), 3),
        }

    # By divergence direction and magnitude
    overpriced_on_kalshi = [r for r in rows if r["divergence"] < -0.05]
    underpriced_on_kalshi = [r for r in rows if r["divergence"] > 0.05]
    aligned = [r for r in rows if abs(r["divergence"]) <= 0.05]

    return {
        "n": len(rows),
        "games": len(set(game_ids)),
        "mean_divergence": _mean(divergences),
        "median_divergence": _median(divergences),
        "mean_abs_divergence": _mean(abs_divs),
        "std_divergence": round(statistics.stdev(divergences), 4) if len(divergences) > 1 else None,
        "max_divergence": round(max(divergences), 4),
        "min_divergence": round(min(divergences), 4),
        "bootstrap": _bootstrap_ci(divergences, cluster_keys=game_ids),
        "overpriced_on_kalshi": len(overpriced_on_kalshi),
        "underpriced_on_kalshi": len(underpriced_on_kalshi),
        "aligned_within_5pct": len(aligned),
        "by_status": status_analysis,
    }


def print_human_readable(rows: list[dict], analysis: dict):
    print("=" * 80)
    print("H2 CROWD DIVERGENCE ANALYSIS (Real Sports vs Kalshi)")
    print("=" * 80)

    if analysis.get("n", 0) == 0:
        print("No divergence data available.")
        return

    a = analysis
    print(f"Markets compared:        {a['n']}")
    print(f"Games:                   {a['games']}")
    print(f"Mean divergence:         {a['mean_divergence']:+.2%}")
    print(f"Mean abs divergence:     {a['mean_abs_divergence']:.2%}")
    print(f"Std divergence:          {a.get('std_divergence', 0):.2%}")
    ci = a.get("bootstrap", {})
    print(f"Bootstrap CI:            [{ci.get('ci_low', '?')}, {ci.get('ci_high', '?')}]")
    print(f"Overpriced on Kalshi:    {a['overpriced_on_kalshi']} (crowd says < Kalshi price)")
    print(f"Underpriced on Kalshi:   {a['underpriced_on_kalshi']} (crowd says > Kalshi price)")
    print(f"Aligned (within 5%):     {a['aligned_within_5pct']}")
    print()

    print("--- By Game Status ---")
    for status, data in a.get("by_status", {}).items():
        print(f"  {status}: N={data['n']}  mean_div={data['mean_divergence']:+.2%}  "
              f"abs_div={data['mean_abs_divergence']:.2%}")
    print()

    print("--- Individual Markets ---")
    print(f"{'Ticker':<45} {'Status':<15} {'Crowd':>7} {'Kalshi':>7} {'Div':>8} {'Volume':>8}")
    print("-" * 95)
    for r in sorted(rows, key=lambda x: -abs(x["divergence"])):
        flag = " <<<" if abs(r["divergence"]) >= 0.08 else ""
        print(f"{r['ticker']:<45} {r['status']:<15} {r['crowd_prob']:>7.1%} "
              f"{r['kalshi_implied']:>7.1%} {r['divergence']:>+7.1%} {r['crowd_volume']:>8}{flag}")
    print()

    # Verdict
    abs_div = a["mean_abs_divergence"] or 0
    n = a["n"]
    print("=" * 80)
    if abs_div >= 0.05 and n >= 4:
        print("VERDICT: PROMISING -- meaningful divergence exists between crowd and Kalshi")
        print(f"Mean absolute divergence of {abs_div:.1%} suggests the two markets price differently.")
        print("Next: accumulate divergence data over multiple game nights and test for CLV.")
    elif abs_div >= 0.03:
        print("VERDICT: MARGINAL -- small divergence, may or may not be tradeable")
        print("Need more data to determine if this is noise or signal.")
    else:
        print("VERDICT: NO DIVERGENCE -- crowd and Kalshi prices are well-aligned")
        print("Book A has no edge if the two markets agree.")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="H2 crowd divergence analysis")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = asyncio.run(fetch_live_divergence())
    analysis = analyze_divergence(rows)

    if args.json:
        print(json.dumps({"rows": rows, "analysis": analysis}, indent=2, default=str))
    else:
        print_human_readable(rows, analysis)

    out_path = PROJECT_DIR / "data" / "reports" / "oracle-h2-crowd-divergence.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"rows": rows, "analysis": analysis}, indent=2, default=str))
    print(f"\nJSON written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
