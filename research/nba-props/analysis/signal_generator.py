"""Signal generator — finds trading edges by comparing model vs Kalshi prices.

Matches Real Sports player data against live Kalshi orderbooks to identify
player prop markets where our model disagrees with the market price.

Usage:
    python3 -m analysis.signal_generator
    python3 -m analysis.signal_generator --min-edge 0.06 --max-spread 0.10
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from analysis.backtester import _weighted_hit_rate, _compute_prob_space_shift

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMELOGS_DIR = _PROJECT_ROOT / "data" / "processed" / "gamelogs"

STAT_MAP = {
    "KXNBAPTS": "points",
    "KXNBAREB": "rebounds",
    "KXNBAAST": "assists",
    "KXNBA3PT": "three_pointers",
    "KXNBASTL": "steals",
    "KXNBABLK": "blocks",
}


def _load_gamelogs() -> dict[str, dict]:
    """Load all player gamelogs indexed by lowercase name."""
    players = {}
    for f in GAMELOGS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            name = data.get("player_name", "").lower()
            if name and data.get("gamelogs"):
                players[name] = data
        except (json.JSONDecodeError, OSError):
            continue
    return players


def _parse_title(title: str) -> dict | None:
    """Parse 'Player Name: N+ stat' from market title."""
    match = re.match(r"(.+?):\s+(\d+)\+\s+(.+)", title)
    if not match:
        return None
    return {
        "player": match.group(1).strip(),
        "line": int(match.group(2)),
        "stat_raw": match.group(3).strip().lower(),
    }


def _fuzzy_match(kalshi_name: str, player_names: set[str]) -> str | None:
    kn = kalshi_name.lower().strip()
    if kn in player_names:
        return kn
    parts = kn.split()
    if len(parts) >= 2:
        last = parts[-1]
        matches = [n for n in player_names if n.endswith(last)]
        if len(matches) == 1:
            return matches[0]
    return None


def _get_orderbook(ticker: str) -> dict:
    """Fetch live orderbook from production Kalshi (unauthenticated)."""
    resp = requests.get(f"{KALSHI_BASE}/markets/{ticker}/orderbook", timeout=10)
    if resp.status_code != 200:
        return {}
    ob = resp.json().get("orderbook_fp", {})
    yes_bids = ob.get("yes_dollars", [])
    no_bids = ob.get("no_dollars", [])

    result = {"yes_bid": 0.0, "yes_ask": 0.0, "spread": 1.0, "midpoint": 0.5,
              "yes_bid_depth": 0, "yes_ask_depth": 0}

    if yes_bids:
        result["yes_bid"] = float(yes_bids[-1][0])
        result["yes_bid_depth"] = int(float(yes_bids[-1][1]))
    if no_bids:
        result["yes_ask"] = round(1.0 - float(no_bids[-1][0]), 4)
        result["yes_ask_depth"] = int(float(no_bids[-1][1]))
    if result["yes_bid"] > 0 and result["yes_ask"] > 0:
        result["spread"] = round(result["yes_ask"] - result["yes_bid"], 4)
        result["midpoint"] = round((result["yes_bid"] + result["yes_ask"]) / 2, 4)

    return result


def generate_signals(
    params: dict | None = None,
    min_edge: float = 0.06,
    max_spread: float = 0.10,
    min_depth: int = 5,
) -> list[dict]:
    """Generate trading signals for tonight's NBA prop markets.

    Returns list of signals sorted by edge size (largest first).
    """
    if params is None:
        params = {}

    players = _load_gamelogs()
    player_names = set(players.keys())

    # Fetch open NBA prop markets from production
    all_markets = []
    for series_ticker, stat_type in STAT_MAP.items():
        resp = requests.get(f"{KALSHI_BASE}/markets", params={
            "series_ticker": series_ticker, "status": "open", "limit": 200,
        }, timeout=15)
        if resp.status_code == 200:
            markets = resp.json().get("markets", [])
            for m in markets:
                m["_stat_type"] = stat_type
                m["_series"] = series_ticker
            all_markets.extend(markets)

    signals = []
    matched = 0
    skipped_no_player = 0
    skipped_no_depth = 0

    for market in all_markets:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        parsed = _parse_title(title)
        if not parsed:
            continue

        stat_type = market["_stat_type"]
        line = parsed["line"]
        player_name = parsed["player"]

        # Match player
        gl_name = _fuzzy_match(player_name, player_names)
        if not gl_name:
            skipped_no_player += 1
            continue

        gamelogs = players[gl_name].get("gamelogs", [])
        if len(gamelogs) < 5:
            continue

        # Compute model probability
        sorted_gl = sorted(gamelogs, key=lambda g: g.get("game_date", ""), reverse=True)
        values = []
        for gl in sorted_gl:
            try:
                values.append(float(gl.get(stat_type, 0)))
            except (ValueError, TypeError):
                values.append(0.0)

        model_prob = _weighted_hit_rate(values, float(line), params)

        # Get live orderbook
        ob = _get_orderbook(ticker)
        if ob["yes_bid"] == 0 and ob["yes_ask"] == 0:
            skipped_no_depth += 1
            continue

        midpoint = ob["midpoint"]
        spread = ob["spread"]

        # Compute edge
        edge_yes = model_prob - ob["yes_ask"]  # edge if we buy YES
        edge_no = (1 - model_prob) - (1 - ob["yes_bid"])  # edge if we buy NO

        # Determine trade direction
        if edge_yes >= min_edge and spread <= max_spread:
            signals.append({
                "ticker": ticker,
                "player": player_name,
                "stat": stat_type,
                "line": line,
                "side": "YES",
                "model_prob": round(model_prob, 3),
                "market_price": ob["yes_ask"],
                "edge": round(edge_yes, 3),
                "spread": spread,
                "depth": ob["yes_ask_depth"],
                "cost_per_contract": ob["yes_ask"],
            })
        elif edge_no >= min_edge and spread <= max_spread:
            signals.append({
                "ticker": ticker,
                "player": player_name,
                "stat": stat_type,
                "line": line,
                "side": "NO",
                "model_prob": round(1 - model_prob, 3),
                "market_price": 1 - ob["yes_bid"],
                "edge": round(edge_no, 3),
                "spread": spread,
                "depth": ob["yes_bid_depth"],
                "cost_per_contract": 1 - ob["yes_bid"],
            })

        matched += 1

    signals.sort(key=lambda s: -s["edge"])

    return signals


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate NBA prop trading signals")
    parser.add_argument("--min-edge", type=float, default=0.06)
    parser.add_argument("--max-spread", type=float, default=0.10)
    parser.add_argument("--min-depth", type=int, default=5)
    args = parser.parse_args()

    print("Generating signals against live Kalshi orderbooks...\n")
    signals = generate_signals(
        min_edge=args.min_edge,
        max_spread=args.max_spread,
        min_depth=args.min_depth,
    )

    if not signals:
        print("No signals found meeting criteria.")
        print(f"  Min edge: {args.min_edge}, Max spread: {args.max_spread}")
        return

    print(f"{'='*90}")
    print(f"  TRADING SIGNALS — {len(signals)} opportunities")
    print(f"{'='*90}")
    print(f"  {'Player':<22} {'Stat':>8} {'Line':>4} {'Side':>4} {'Model':>6} {'Mkt':>5} "
          f"{'Edge':>5} {'Spread':>6} {'Depth':>5}")
    print(f"  {'-'*22} {'-'*8} {'-'*4} {'-'*4} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*5}")

    total_edge = 0
    for s in signals:
        print(f"  {s['player']:<22} {s['stat']:>8} {s['line']:>4}+ {s['side']:>4} "
              f"{s['model_prob']:>5.0%} {s['market_price']:>5.2f} "
              f"{s['edge']:>+5.0%} {s['spread']:>6.2f} {s['depth']:>5}")
        total_edge += s["edge"]

    print(f"{'='*90}")
    print(f"  Average edge: {total_edge/len(signals):+.1%}")
    print(f"  Total signals: {len(signals)}")

    # Save signals
    output_path = _PROJECT_ROOT / "data" / "signals.json"
    with open(output_path, "w") as f:
        json.dump({"signals": signals, "count": len(signals)}, f, indent=2)
    print(f"  Saved to: {output_path}")


if __name__ == "__main__":
    main()
