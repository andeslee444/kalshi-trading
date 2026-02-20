#!/usr/bin/env python3
"""Kalshi Trading Performance Analytics — Reads trade logs from all bots and prints a summary.

Usage:
    python3 scripts/analyze-performance.py            # Human-readable table
    python3 scripts/analyze-performance.py --json     # Machine-readable JSON output
    python3 scripts/analyze-performance.py --reconcile        # Include win rate & P&L from Kalshi API
    python3 scripts/analyze-performance.py --reconcile --json # Reconciliation as JSON
"""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ─── Shared project root ───
# Mirrors PROJECT_DIR from kalshi_auth.py but computed locally so we avoid
# importing kalshi_auth (which pulls in requests + cryptography).
# The sys.path insert is kept so that kalshi_auth *could* be imported by
# other scripts launched from the same entry point, matching the project
# convention established in trade-cycle-2.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
PROJECT_DIR = Path(__file__).resolve().parent.parent

# ─── Trade file definitions ───
# Each entry: (label, relative path from PROJECT_DIR, field mapping notes)
TRADE_FILES = [
    {
        "label": "Weather Bot",
        "path": PROJECT_DIR / "data" / "kalshi-trades.json",
    },
    {
        "label": "Strategy Trader",
        "path": PROJECT_DIR / "data" / "kalshi-strategy-trades.json",
    },
    {
        "label": "Entertainment Bot",
        "path": PROJECT_DIR / "data" / "kalshi-entertainment-trades.json",
    },
    {
        "label": "BeatRelease Scanner",
        "path": PROJECT_DIR / "data" / "beatrelease-trades.json",
    },
    {
        "label": "Source Monitor",
        "path": PROJECT_DIR / "data" / "kalshi-monitor-trades.json",
    },
    {
        "label": "Position Monitor",
        "path": PROJECT_DIR / "data" / "kalshi-position-trades.json",
    },
    {
        "label": "Economics Bot",
        "path": PROJECT_DIR / "data" / "kalshi-economics-trades.json",
    },
    {
        "label": "Crypto Bot",
        "path": PROJECT_DIR / "data" / "kalshi-crypto-trades.json",
    },
    {
        "label": "Cross-Platform Arb",
        "path": PROJECT_DIR / "data" / "kalshi-arb-trades.json",
    },
    {
        "label": "Market Maker",
        "path": PROJECT_DIR / "data" / "kalshi-mm-trades.json",
    },
]


# ─── Helpers ───

def load_trades_safe(filepath: Path) -> list | None:
    """Load a JSON trade file.  Returns list on success, None on missing/corrupt."""
    if not filepath.exists():
        return None
    try:
        text = filepath.read_text().strip()
        if not text:
            return None
        data = json.loads(text)
        if not isinstance(data, list):
            return None
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def extract_side(trade: dict) -> str:
    """Normalise the side/direction field across bot formats."""
    # Direct 'side' field (weather, entertainment, beatrelease)
    side = trade.get("side", "")
    if side:
        return side.upper()
    # Strategy trader uses 'direction' like "BUY NO (= SELL YES)"
    direction = trade.get("direction", "")
    if direction:
        d = direction.upper()
        if "NO" in d:
            return "NO"
        if "YES" in d:
            return "YES"
    return "unknown"


def extract_risk_cents(trade: dict) -> int:
    """Best-effort extraction of risk/cost in cents across all formats."""
    # Explicit risk_cents field (strategy trader, trade-cycle-2)
    if "risk_cents" in trade:
        try:
            return int(trade["risk_cents"])
        except (TypeError, ValueError):
            pass
    # Entertainment bot stores cost_cents directly
    if "cost_cents" in trade:
        try:
            return int(trade["cost_cents"])
        except (TypeError, ValueError):
            pass
    # Weather bot: price * count
    price = trade.get("price", 0) or 0
    count = trade.get("count", 0) or 0
    if price and count:
        try:
            return int(price) * int(count)
        except (TypeError, ValueError):
            pass
    # BeatRelease: price * quantity
    quantity = trade.get("quantity", 0) or 0
    if price and quantity:
        try:
            return int(price) * int(quantity)
        except (TypeError, ValueError):
            pass
    # Strategy trader fallback: no_price * contracts
    no_price = trade.get("no_price", 0) or 0
    contracts = trade.get("contracts", 0) or 0
    if no_price and contracts:
        try:
            return int(no_price) * int(contracts)
        except (TypeError, ValueError):
            pass
    # Entertainment bot fallback: price_cents * count
    price_cents = trade.get("price_cents", 0) or 0
    if price_cents and count:
        try:
            return int(price_cents) * int(count)
        except (TypeError, ValueError):
            pass
    return 0


def extract_timestamp(trade: dict) -> str | None:
    """Return the timestamp string or None."""
    ts = trade.get("timestamp")
    if ts and isinstance(ts, str):
        return ts[:10]  # YYYY-MM-DD
    return None


def analyze_trades(trades: list) -> dict:
    """Compute analytics for a list of trade dicts."""
    total = len(trades)
    status_counter: Counter = Counter()
    side_counter: Counter = Counter()
    strategy_counter: Counter = Counter()
    total_risk_cents = 0
    dates: list[str] = []

    for t in trades:
        # Status
        status = t.get("status", "unknown") or "unknown"
        status_counter[status.lower()] += 1

        # Side
        side = extract_side(t)
        side_counter[side] += 1

        # Risk
        total_risk_cents += extract_risk_cents(t)

        # Strategy
        strategy = t.get("strategy")
        if strategy:
            strategy_counter[strategy] += 1

        # Dates
        ts = extract_timestamp(t)
        if ts:
            dates.append(ts)

    dates.sort()
    date_range = None
    if dates:
        date_range = {"earliest": dates[0], "latest": dates[-1]}

    return {
        "total": total,
        "by_status": dict(status_counter.most_common()),
        "by_side": dict(side_counter.most_common()),
        "by_strategy": dict(strategy_counter.most_common()) if strategy_counter else None,
        "total_risk_cents": total_risk_cents,
        "date_range": date_range,
    }


# ─── Formatters ───

def format_status(by_status: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in by_status.items())


def format_side(by_side: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in by_side.items())


def format_strategy(by_strategy: dict | None) -> str | None:
    if not by_strategy:
        return None
    return ", ".join(f"{k}={v}" for k, v in by_strategy.items())


def print_text_report(results: list[dict], reconciliation: dict | None = None):
    """Print human-readable formatted report."""
    print("=" * 60)
    print("KALSHI TRADING PERFORMANCE ANALYSIS")
    print("=" * 60)

    any_data = False
    agg_total = 0
    agg_risk = 0

    for r in results:
        label = r["label"]
        rel_path = r["rel_path"]
        stats = r.get("stats")

        print(f"\n--- {label} ({rel_path}) ---")

        if stats is None:
            print("  No trades found.")
            continue

        any_data = True
        agg_total += stats["total"]
        agg_risk += stats["total_risk_cents"]

        print(f"  Trades: {stats['total']}")

        dr = stats["date_range"]
        if dr:
            print(f"  Date range: {dr['earliest']} to {dr['latest']}")

        print(f"  By side: {format_side(stats['by_side'])}")
        print(f"  Total risk: ${stats['total_risk_cents'] / 100:.2f}")
        print(f"  By status: {format_status(stats['by_status'])}")

        strat_str = format_strategy(stats["by_strategy"])
        if strat_str:
            print(f"  By strategy: {strat_str}")

    if not any_data:
        print("\nNo trade data found.")
        if not reconciliation:
            return

    print()
    print("=" * 60)
    print("AGGREGATE SUMMARY")
    print("=" * 60)
    print(f"  Total trades across all bots: {agg_total}")
    print(f"  Total risk: ${agg_risk / 100:.2f}")

    if reconciliation:
        _print_text_reconciliation(reconciliation)


def _print_text_reconciliation(rec: dict):
    """Append reconciliation section to text report."""
    print()
    print("=" * 60)
    print("RECONCILIATION (from Kalshi API)")
    print("=" * 60)

    for bot in rec.get("per_bot", []):
        label = bot["label"]
        print(f"\n  --- {label} ---")
        print(f"    Settled: {bot['wins'] + bot['losses']}  (wins={bot['wins']}, losses={bot['losses']})")
        if bot["wins"] + bot["losses"] > 0:
            print(f"    Win rate: {bot['win_rate'] * 100:.1f}%")
        print(f"    P&L: ${bot['pnl_cents'] / 100:.2f}")
        print(f"    Unmatched local trades: {bot['unmatched']}")

    agg = rec["aggregate"]
    print()
    print("  --- Aggregate ---")
    total_settled = agg["wins"] + agg["losses"]
    print(f"    Total settled: {total_settled}  (wins={agg['wins']}, losses={agg['losses']})")
    if total_settled > 0:
        print(f"    Win rate: {agg['win_rate'] * 100:.1f}%")
    print(f"    Total P&L: ${agg['pnl_cents'] / 100:.2f}")
    if agg["sharpe"] is not None:
        print(f"    Sharpe ratio (annualised): {agg['sharpe']:.2f}")
    else:
        print("    Sharpe ratio: N/A (need >=2 trading days)")


def print_json_report(results: list[dict], reconciliation: dict | None = None):
    """Print machine-readable JSON report."""
    output: dict = {"bots": [], "aggregate": {"total_trades": 0, "total_risk_cents": 0}}

    for r in results:
        entry = {
            "label": r["label"],
            "path": r["rel_path"],
            "stats": r.get("stats"),
        }
        output["bots"].append(entry)
        if r.get("stats"):
            output["aggregate"]["total_trades"] += r["stats"]["total"]
            output["aggregate"]["total_risk_cents"] += r["stats"]["total_risk_cents"]

    output["aggregate"]["total_risk_dollars"] = round(
        output["aggregate"]["total_risk_cents"] / 100, 2
    )

    if reconciliation:
        output["reconciliation"] = reconciliation

    print(json.dumps(output, indent=2))


# ─── Reconciliation ───

def _get_client():
    """Lazy import of KalshiClient — only needed when --reconcile is used."""
    from kalshi_auth import KalshiClient
    return KalshiClient()


def fetch_settlements(client) -> list[dict]:
    """Paginate /portfolio/settlements and return all settlement records."""
    all_settlements: list[dict] = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/settlements?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        batch = data.get("settlements", [])
        all_settlements.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return all_settlements


def fetch_fills(client) -> list[dict]:
    """Paginate /portfolio/fills and return all fill records."""
    all_fills: list[dict] = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/fills?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        batch = data.get("fills", [])
        all_fills.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return all_fills


def reconcile_trades(
    local_trades_by_bot: list[dict],
    settlements: list[dict],
    fills: list[dict],
) -> dict:
    """Reconcile local trade records against Kalshi settlements and fills.

    Args:
        local_trades_by_bot: list of {"label": str, "trades": list[dict]} entries.
            Each trade dict must have a "ticker" key.
        settlements: raw settlement records from the Kalshi API.
            Each has "ticker", "revenue" (net P&L in cents), and optionally
            "settled_time" (ISO string).
        fills: raw fill records from the Kalshi API (used for daily P&L grouping).
            Each has "ticker", "created_time" (ISO string).

    Returns:
        dict with "per_bot" (list) and "aggregate" keys.
    """
    # Build ticker → settlement map.  If a ticker appears multiple times
    # (e.g. multiple contracts), sum the revenues.
    ticker_revenue: dict[str, int] = defaultdict(int)
    ticker_settled: dict[str, str] = {}  # ticker → earliest settled_time date
    for s in settlements:
        ticker = s.get("ticker", "")
        revenue = s.get("revenue", 0)
        try:
            revenue = int(revenue)
        except (TypeError, ValueError):
            revenue = 0
        ticker_revenue[ticker] += revenue

        st = s.get("settled_time", "")
        if st and isinstance(st, str):
            day = st[:10]
            existing = ticker_settled.get(ticker)
            if existing is None or day < existing:
                ticker_settled[ticker] = day

    # Daily P&L from settlements (for Sharpe)
    daily_pnl: dict[str, int] = defaultdict(int)
    for s in settlements:
        ticker = s.get("ticker", "")
        revenue = s.get("revenue", 0)
        try:
            revenue = int(revenue)
        except (TypeError, ValueError):
            revenue = 0
        st = s.get("settled_time", "")
        if st and isinstance(st, str):
            day = st[:10]
            daily_pnl[day] += revenue

    # Per-bot reconciliation
    per_bot: list[dict] = []
    agg_wins = 0
    agg_losses = 0
    agg_pnl = 0

    for bot_entry in local_trades_by_bot:
        label = bot_entry["label"]
        trades = bot_entry.get("trades") or []
        wins = 0
        losses = 0
        pnl_cents = 0
        unmatched = 0

        for t in trades:
            ticker = t.get("ticker", "")
            if ticker in ticker_revenue:
                rev = ticker_revenue[ticker]
                pnl_cents += rev
                if rev > 0:
                    wins += 1
                else:
                    losses += 1
            else:
                unmatched += 1

        total_settled = wins + losses
        win_rate = (wins / total_settled) if total_settled > 0 else 0.0

        per_bot.append({
            "label": label,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "pnl_cents": pnl_cents,
            "unmatched": unmatched,
        })

        agg_wins += wins
        agg_losses += losses
        agg_pnl += pnl_cents

    agg_total_settled = agg_wins + agg_losses
    agg_win_rate = (agg_wins / agg_total_settled) if agg_total_settled > 0 else 0.0

    # Sharpe ratio: (mean daily P&L / std daily P&L) * sqrt(252)
    sharpe = None
    if len(daily_pnl) >= 2:
        pnl_values = list(daily_pnl.values())
        mean_pnl = sum(pnl_values) / len(pnl_values)
        variance = sum((v - mean_pnl) ** 2 for v in pnl_values) / (len(pnl_values) - 1)
        std_pnl = math.sqrt(variance)
        if std_pnl > 0:
            sharpe = round((mean_pnl / std_pnl) * math.sqrt(252), 4)

    return {
        "per_bot": per_bot,
        "aggregate": {
            "wins": agg_wins,
            "losses": agg_losses,
            "win_rate": round(agg_win_rate, 4),
            "pnl_cents": agg_pnl,
            "sharpe": sharpe,
        },
    }


# ─── Main ───

def main():
    parser = argparse.ArgumentParser(
        description="Analyze Kalshi trading performance across all bots."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON instead of formatted text.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Query Kalshi API for settlements/fills and compute win rate, P&L, and Sharpe ratio.",
    )
    args = parser.parse_args()

    results = []

    for tf in TRADE_FILES:
        label = tf["label"]
        filepath = tf["path"]
        # Use a relative-looking path for display
        try:
            rel_path = str(filepath.relative_to(PROJECT_DIR))
        except ValueError:
            rel_path = str(filepath)

        trades = load_trades_safe(filepath)
        if trades is None or len(trades) == 0:
            results.append({"label": label, "rel_path": rel_path, "stats": None})
        else:
            stats = analyze_trades(trades)
            results.append({"label": label, "rel_path": rel_path, "stats": stats})

    # Reconciliation (optional — requires API credentials)
    reconciliation = None
    if args.reconcile:
        client = _get_client()
        settlements = fetch_settlements(client)
        fills = fetch_fills(client)

        local_trades_by_bot = []
        for tf in TRADE_FILES:
            trades = load_trades_safe(tf["path"])
            local_trades_by_bot.append({
                "label": tf["label"],
                "trades": trades or [],
            })

        reconciliation = reconcile_trades(local_trades_by_bot, settlements, fills)

    if args.json:
        print_json_report(results, reconciliation)
    else:
        print_text_report(results, reconciliation)


if __name__ == "__main__":
    main()
