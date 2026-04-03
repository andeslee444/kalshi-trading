#!/usr/bin/env python3
"""H2 crowd divergence analysis from live fetches or collected ledger data."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from dotenv import load_dotenv

from domain.oracle.alpha_capture import DEFAULT_ORACLE_ALPHA_LEDGER_PATH
from domain.oracle.execution.quote_check import quote_from_orderbook
from domain.oracle.nba_ticker_utils import parse_nba_ticker
from domain.oracle.real_sports_client import RealSportsClient, RealSportsConfig
from domain.shared.sizing import kalshi_fee_cents
from event_ledger import EVENT_TYPE_MARKET_SNAPSHOT, EVENT_TYPE_SOURCE_OBSERVATION, EventLedger
from infra.kalshi_client import KalshiClient

load_dotenv(PROJECT_DIR / ".env")


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


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
        "ci_low": round(draws[lo], 4) if draws else None,
        "ci_high": round(draws[hi], 4) if draws else None,
        "n": len(values),
        "clusters": cluster_count,
    }


def _coerce_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value):
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _hours_to_tip_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 1.0:
        return "<1h"
    if value < 2.0:
        return "1-2h"
    if value < 4.0:
        return "2-4h"
    if value < 8.0:
        return "4-8h"
    return "8h+"


def _build_divergence_row(crowd_row: dict, quote_row: dict) -> dict | None:
    ticker = str(quote_row.get("ticker") or "")
    parsed = parse_nba_ticker(ticker)
    if not parsed or parsed.get("type") != "game":
        return None

    pick = parsed.get("pick")
    team_a = str(crowd_row.get("team_a") or "")
    team_b = str(crowd_row.get("team_b") or "")
    if pick == team_a:
        crowd_prob = _coerce_float(crowd_row.get("team_a_prob"))
    elif pick == team_b:
        crowd_prob = _coerce_float(crowd_row.get("team_b_prob"))
    else:
        return None

    yes_bid = _coerce_int(quote_row.get("yes_bid_cents"))
    yes_ask = _coerce_int(quote_row.get("yes_ask_cents"))
    if crowd_prob is None or yes_bid is None or yes_ask is None:
        return None
    midpoint_cents = _coerce_int(quote_row.get("midpoint_cents"))
    if midpoint_cents is None:
        midpoint_cents = round((yes_bid + yes_ask) / 2)
    spread_cents = _coerce_int(quote_row.get("spread_cents"))
    if spread_cents is None:
        spread_cents = yes_ask - yes_bid
    midpoint_prob = midpoint_cents / 100.0
    divergence = crowd_prob - midpoint_prob
    fee_cents = kalshi_fee_cents(midpoint_cents)
    net_edge_after_cost = abs(divergence) - (spread_cents / 100.0) - (fee_cents / 100.0)

    observed_at = _parse_datetime(crowd_row.get("observed_at")) or _parse_datetime(quote_row.get("quote_timestamp_utc"))
    start_time = _parse_datetime(crowd_row.get("start_time")) or _parse_datetime(quote_row.get("start_time"))
    hours_to_tip = _coerce_float(crowd_row.get("hours_to_tip"))
    if hours_to_tip is None and observed_at is not None and start_time is not None:
        hours_to_tip = round((start_time - observed_at).total_seconds() / 3600.0, 4)

    return {
        "source": "ledger",
        "collector_cycle_id": crowd_row.get("collector_cycle_id") or quote_row.get("collector_cycle_id"),
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "game_id": str(crowd_row.get("game_id") or quote_row.get("game_id") or ""),
        "ticker": ticker,
        "team": pick,
        "status": crowd_row.get("game_status") or quote_row.get("game_status") or "unknown",
        "crowd_prob": round(crowd_prob, 4),
        "kalshi_implied": round(midpoint_prob, 4),
        "divergence": round(divergence, 4),
        "abs_divergence": round(abs(divergence), 4),
        "spread_cents": spread_cents,
        "fee_cents": round(fee_cents, 3),
        "net_edge_after_cost": round(net_edge_after_cost, 4),
        "survives_spread_and_fees": net_edge_after_cost > 0,
        "hours_to_tip": round(hours_to_tip, 4) if hours_to_tip is not None else None,
        "hours_to_tip_bucket": _hours_to_tip_bucket(hours_to_tip),
    }


def load_ledger_divergence(ledger_path: str | Path) -> list[dict]:
    ledger = EventLedger(ledger_path)
    source_rows = ledger._fetch_event_payloads(
        EVENT_TYPE_SOURCE_OBSERVATION,
        source_path=str(ledger_path),
    )
    quote_rows = ledger._fetch_event_payloads(
        EVENT_TYPE_MARKET_SNAPSHOT,
        source_path=str(ledger_path),
    )

    crowd_rows = [
        row
        for row in source_rows
        if row.get("snapshot_name") == "crowd_probability" and row.get("collector_mode") == "pregame"
    ]
    pregame_quotes = [
        row
        for row in quote_rows
        if row.get("collector_mode") == "pregame"
        and row.get("capture_mode") == "baseline"
    ]
    quotes_by_cycle_game: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for quote in pregame_quotes:
        cycle_id = str(quote.get("collector_cycle_id") or "")
        game_id = str(quote.get("game_id") or "")
        if not cycle_id or not game_id:
            continue
        quotes_by_cycle_game[(cycle_id, game_id)].append(quote)

    rows = []
    for crowd in crowd_rows:
        cycle_id = str(crowd.get("collector_cycle_id") or "")
        game_id = str(crowd.get("game_id") or "")
        if not cycle_id or not game_id:
            continue
        for quote in quotes_by_cycle_game.get((cycle_id, game_id), []):
            row = _build_divergence_row(crowd, quote)
            if row is not None:
                rows.append(row)
    return rows


async def fetch_live_divergence() -> list[dict]:
    """Fetch current crowd probabilities and Kalshi prices for all games."""
    from domain.oracle.market_mapper import match_game_markets, normalize_team

    real_config = RealSportsConfig.from_bots_config(
        {
            "realSports": {
                "baseUrl": "https://web.realapp.com",
                "wsUrl": "https://web.realsports.io",
            }
        }
    )
    real_client = RealSportsClient(real_config)
    if real_config.can_auto_login:
        ok = await real_client.login()
        if not ok:
            raise SystemExit("Real Sports auto-login failed for H2 live divergence fetch")
    kalshi_client = KalshiClient()

    home = await real_client.get_home_feed("nba")
    games = home.get("latestDayContent", {}).get("games", []) if isinstance(home, dict) else []
    crowd_markets = await real_client.get_game_markets("nba")
    crowd_by_game = {
        str(m.get("gameId") or m.get("id")): m
        for m in crowd_markets
        if isinstance(m, dict) and (m.get("gameId") or m.get("id"))
    }
    kalshi_markets = kalshi_client.get_all_markets("KXNBAGAME", "open", max_pages=5)

    results = []
    for game in games:
        game_id = str(game.get("id") or game.get("gameId") or "")
        if not game_id:
            continue
        market = crowd_by_game.get(game_id)
        if not market or market.get("label") != "Game Winner":
            continue
        outcomes = market.get("outcomes", [])
        if len(outcomes) != 2:
            continue
        crowd_probs = {outcome.get("key", ""): outcome.get("probability", 0) for outcome in outcomes}
        home_team = game.get("homeTeam", {}).get("name", "")
        away_team = game.get("awayTeam", {}).get("name", "")
        status = game.get("status", "unknown")

        scheduled_day = game.get("dateTime", "")
        game_date = None
        if scheduled_day:
            try:
                game_date = dt.datetime.fromisoformat(scheduled_day.replace("Z", "+00:00")).date()
            except ValueError:
                game_date = None
        if not game_date:
            continue

        for kalshi_market in match_game_markets(home_team, away_team, game_date, kalshi_markets):
            ticker = kalshi_market.get("ticker", "")
            parsed = parse_nba_ticker(ticker)
            if not parsed or parsed.get("type") != "game":
                continue
            pick = parsed.get("pick")
            crowd_prob = crowd_probs.get(pick)
            if crowd_prob is None:
                continue
            yes_price = kalshi_market.get("yes_price", 0)
            if yes_price <= 0:
                try:
                    orderbook = kalshi_client.get_orderbook(ticker)
                    quote = quote_from_orderbook(ticker, orderbook)
                    yes_price = round((quote.yes_bid + quote.yes_ask) / 2)
                except Exception:
                    continue
            if yes_price <= 0 or yes_price >= 100:
                continue

            divergence = crowd_prob - (yes_price / 100.0)
            team = normalize_team(home_team) if pick == normalize_team(home_team) else normalize_team(away_team)
            results.append(
                {
                    "source": "live",
                    "game_id": game_id,
                    "ticker": ticker,
                    "team": team,
                    "status": status,
                    "crowd_prob": round(crowd_prob, 4),
                    "kalshi_implied": round(yes_price / 100.0, 4),
                    "divergence": round(divergence, 4),
                    "abs_divergence": round(abs(divergence), 4),
                    "spread_cents": None,
                    "fee_cents": None,
                    "net_edge_after_cost": None,
                    "survives_spread_and_fees": None,
                    "hours_to_tip": None,
                    "hours_to_tip_bucket": "unknown",
                }
            )

    await real_client.close()
    return results


def analyze_divergence(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "error": "No divergence data"}

    divergences = [row["divergence"] for row in rows]
    abs_divs = [row["abs_divergence"] for row in rows]
    cluster_keys = [str(row.get("game_id") or row.get("ticker") or "na") for row in rows]
    net_edge_rows = [row["net_edge_after_cost"] for row in rows if row.get("net_edge_after_cost") is not None]

    by_status = defaultdict(list)
    by_hours_to_tip = defaultdict(list)
    for row in rows:
        by_status[str(row.get("status") or "unknown")].append(row)
        by_hours_to_tip[str(row.get("hours_to_tip_bucket") or "unknown")].append(row)

    def summarize_group(items: list[dict]) -> dict:
        values = [item["divergence"] for item in items]
        net_edges = [item["net_edge_after_cost"] for item in items if item.get("net_edge_after_cost") is not None]
        return {
            "n": len(items),
            "mean_divergence": _mean(values),
            "median_divergence": _median(values),
            "mean_abs_divergence": _mean([abs(value) for value in values]),
            "positive_rate": round(sum(1 for value in values if value > 0) / len(values), 3),
            "survives_spread_and_fees": sum(1 for item in items if item.get("survives_spread_and_fees") is True),
            "mean_net_edge_after_cost": _mean(net_edges),
        }

    overpriced_on_kalshi = [row for row in rows if row["divergence"] < -0.05]
    underpriced_on_kalshi = [row for row in rows if row["divergence"] > 0.05]
    aligned = [row for row in rows if abs(row["divergence"]) <= 0.05]

    return {
        "n": len(rows),
        "games": len(set(cluster_keys)),
        "mean_divergence": _mean(divergences),
        "median_divergence": _median(divergences),
        "mean_abs_divergence": _mean(abs_divs),
        "std_divergence": round(statistics.stdev(divergences), 4) if len(divergences) > 1 else None,
        "max_divergence": round(max(divergences), 4),
        "min_divergence": round(min(divergences), 4),
        "bootstrap": _bootstrap_ci(divergences, cluster_keys=cluster_keys),
        "mean_net_edge_after_cost": _mean(net_edge_rows),
        "survives_spread_and_fees": sum(1 for row in rows if row.get("survives_spread_and_fees") is True),
        "overpriced_on_kalshi": len(overpriced_on_kalshi),
        "underpriced_on_kalshi": len(underpriced_on_kalshi),
        "aligned_within_5pct": len(aligned),
        "by_status": {status: summarize_group(items) for status, items in sorted(by_status.items())},
        "by_hours_to_tip": {bucket: summarize_group(items) for bucket, items in sorted(by_hours_to_tip.items())},
    }


def print_human_readable(rows: list[dict], analysis: dict):
    print("=" * 80)
    print("H2 CROWD DIVERGENCE ANALYSIS")
    print("=" * 80)

    if analysis.get("n", 0) == 0:
        print("No divergence data available.")
        return

    print(f"Markets compared:        {analysis['n']}")
    print(f"Games:                   {analysis['games']}")
    print(f"Mean divergence:         {analysis['mean_divergence']:+.2%}")
    print(f"Mean abs divergence:     {analysis['mean_abs_divergence']:.2%}")
    print(f"Mean net edge after cost:{analysis.get('mean_net_edge_after_cost', 0):+.2%}")
    print(f"Survive spread+fees:     {analysis['survives_spread_and_fees']}")
    print()

    print("--- By Hours To Tip ---")
    for bucket, data in analysis.get("by_hours_to_tip", {}).items():
        print(
            f"  {bucket}: N={data['n']} mean_div={data['mean_divergence']:+.2%} "
            f"abs_div={data['mean_abs_divergence']:.2%} "
            f"post_cost={data.get('mean_net_edge_after_cost', 0):+.2%} "
            f"survive={data['survives_spread_and_fees']}"
        )
    print()

    print("--- Top Divergences ---")
    print(f"{'Ticker':<38} {'Tip':>6} {'Crowd':>7} {'Kalshi':>7} {'Div':>8} {'Net':>8}")
    print("-" * 82)
    for row in sorted(rows, key=lambda item: -abs(item["divergence"]))[:20]:
        print(
            f"{row['ticker']:<38} "
            f"{(row.get('hours_to_tip_bucket') or '-'):>6} "
            f"{row['crowd_prob']:>7.1%} "
            f"{row['kalshi_implied']:>7.1%} "
            f"{row['divergence']:>+7.1%} "
            f"{(row.get('net_edge_after_cost') or 0):>+7.1%}"
        )
    print()

    print("=" * 80)
    if analysis["survives_spread_and_fees"] > 0 and (analysis.get("mean_net_edge_after_cost") or 0) > 0:
        print("VERDICT: PROMISING -- some pregame crowd divergences survive spread and fee hurdles.")
    elif analysis["mean_abs_divergence"] >= 0.03:
        print("VERDICT: RESEARCH ONLY -- divergence exists but does not yet clear cost hurdles cleanly.")
    else:
        print("VERDICT: NO EDGE -- crowd and Kalshi remain too aligned after realistic costs.")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="H2 crowd divergence analysis")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--source",
        choices=("ledger", "live"),
        default="ledger",
        help="Analyze collected ledger data or fetch current live data",
    )
    parser.add_argument(
        "--ledger-path",
        default=str(DEFAULT_ORACLE_ALPHA_LEDGER_PATH),
        help="Path to the Oracle alpha ledger SQLite file",
    )
    args = parser.parse_args()

    if args.source == "live":
        rows = asyncio.run(fetch_live_divergence())
    else:
        rows = load_ledger_divergence(args.ledger_path)
    analysis = analyze_divergence(rows)
    payload = {"source": args.source, "rows": rows, "analysis": analysis}

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_human_readable(rows, analysis)

    out_path = PROJECT_DIR / "data" / "reports" / "oracle-h2-crowd-divergence.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nJSON written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
