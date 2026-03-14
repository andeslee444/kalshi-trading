#!/usr/bin/env python3
"""Kalshi Trading Performance Analytics — Reads trade logs from all bots and prints a summary.

Usage:
    python3 scripts/analyze-performance.py            # Human-readable table
    python3 scripts/analyze-performance.py --json     # Machine-readable JSON output
    python3 scripts/analyze-performance.py --reconcile        # Include win rate & P&L from Kalshi API
    python3 scripts/analyze-performance.py --reconcile --json # Reconciliation as JSON
    python3 scripts/analyze-performance.py --save             # Save metrics to data/performance-metrics.json
"""

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
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
# Import from canonical trade_files module (shared across all scripts)
from trade_files import TRADE_FILES as _CANONICAL_TRADE_FILES
from event_ledger import DEFAULT_LEDGER_PATH, get_event_ledger

DATA_DIR = PROJECT_DIR / "data"
TRADE_FILES = [
    {"label": tf["label"], "bot": tf["bot"], "path": DATA_DIR / tf["filename"]}
    for tf in _CANONICAL_TRADE_FILES
]


# ─── Helpers ───

def load_trades_safe(filepath: Path, *, use_ledger=False, ledger=None) -> list | None:
    """Load a JSON trade file.  Returns list on success, None on missing/corrupt."""
    if use_ledger and ledger is not None:
        return ledger.get_trade_records(filepath)
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
            wr_str = f"{bot['win_rate'] * 100:.1f}%"
            parts = []
            if bot.get("yes_win_rate") is not None:
                parts.append(f"YES: {bot['yes_win_rate'] * 100:.1f}%")
            if bot.get("no_win_rate") is not None:
                parts.append(f"NO: {bot['no_win_rate'] * 100:.1f}%")
            if parts:
                wr_str += f" ({', '.join(parts)})"
            print(f"    Win rate: {wr_str}")
        print(f"    P&L: ${bot['pnl_cents'] / 100:.2f}")
        print(f"    Unmatched local trades: {bot['unmatched']}")

    agg = rec["aggregate"]
    print()
    print("  --- Aggregate ---")
    total_settled = agg["wins"] + agg["losses"]
    print(f"    Total settled: {total_settled}  (wins={agg['wins']}, losses={agg['losses']})")
    if total_settled > 0:
        wr_str = f"{agg['win_rate'] * 100:.1f}%"
        parts = []
        if agg.get("yes_win_rate") is not None:
            parts.append(f"YES: {agg['yes_win_rate'] * 100:.1f}%")
        if agg.get("no_win_rate") is not None:
            parts.append(f"NO: {agg['no_win_rate'] * 100:.1f}%")
        if parts:
            wr_str += f" ({', '.join(parts)})"
        print(f"    Win rate: {wr_str}")
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
        path = "/portfolio/settlements?limit=100"
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
        path = "/portfolio/fills?limit=100"
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
    # (e.g. multiple contracts), sum the profit (revenue - cost).
    # NOTE: Kalshi's "revenue" field is gross payout (cost back + profit),
    # NOT net profit. We must subtract total cost to get actual P&L.
    ticker_profit: dict[str, int] = defaultdict(int)
    ticker_settled: dict[str, str] = {}  # ticker → earliest settled_time date
    for s in settlements:
        ticker = s.get("ticker", "")
        revenue = s.get("revenue", 0)
        try:
            revenue = int(revenue)
        except (TypeError, ValueError):
            revenue = 0
        cost = int(s.get("yes_total_cost", 0)) + int(s.get("no_total_cost", 0))
        ticker_profit[ticker] += revenue - cost

        st = s.get("settled_time", "")
        if st and isinstance(st, str):
            day = st[:10]
            existing = ticker_settled.get(ticker)
            if existing is None or day < existing:
                ticker_settled[ticker] = day

    # Daily P&L from settlements (for Sharpe) — uses profit, not raw revenue
    daily_pnl: dict[str, int] = defaultdict(int)
    for s in settlements:
        ticker = s.get("ticker", "")
        revenue = s.get("revenue", 0)
        try:
            revenue = int(revenue)
        except (TypeError, ValueError):
            revenue = 0
        cost = int(s.get("yes_total_cost", 0)) + int(s.get("no_total_cost", 0))
        st = s.get("settled_time", "")
        if st and isinstance(st, str):
            day = st[:10]
            daily_pnl[day] += revenue - cost

    # Per-bot reconciliation with dedup by order_id
    # Track counted order_ids to prevent double-counting when the same ticker
    # appears in multiple bots' trade logs
    counted_order_ids: set[str] = set()
    counted_tickers: set[str] = set()  # fallback dedup for old records without order_id

    per_bot: list[dict] = []
    agg_wins = 0
    agg_losses = 0
    agg_yes_wins = 0
    agg_yes_losses = 0
    agg_no_wins = 0
    agg_no_losses = 0
    agg_pnl = 0

    for bot_entry in local_trades_by_bot:
        label = bot_entry["label"]
        trades = bot_entry.get("trades") or []
        wins = 0
        losses = 0
        yes_wins = 0
        yes_losses = 0
        no_wins = 0
        no_losses = 0
        pnl_cents = 0
        unmatched = 0

        for t in trades:
            ticker = t.get("ticker", "")
            order_id = t.get("order_id", "")
            side = t.get("side", "")

            # Dedup: skip if this exact order_id was already counted,
            # or if this ticker's revenue was already claimed by any trade
            if order_id and order_id in counted_order_ids:
                continue
            if ticker in counted_tickers:
                continue

            if ticker in ticker_profit:
                prof = ticker_profit[ticker]
                pnl_cents += prof
                if prof > 0:
                    wins += 1
                    if side == "yes":
                        yes_wins += 1
                    elif side == "no":
                        no_wins += 1
                else:
                    losses += 1
                    if side == "yes":
                        yes_losses += 1
                    elif side == "no":
                        no_losses += 1
                # Mark both order_id and ticker as counted
                if order_id:
                    counted_order_ids.add(order_id)
                counted_tickers.add(ticker)
            else:
                unmatched += 1

        total_settled = wins + losses
        win_rate = (wins / total_settled) if total_settled > 0 else 0.0

        # Per-side win rates
        yes_total = yes_wins + yes_losses
        no_total = no_wins + no_losses
        yes_win_rate = (yes_wins / yes_total) if yes_total > 0 else None
        no_win_rate = (no_wins / no_total) if no_total > 0 else None

        per_bot.append({
            "label": label,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "yes_wins": yes_wins,
            "yes_losses": yes_losses,
            "yes_win_rate": round(yes_win_rate, 4) if yes_win_rate is not None else None,
            "no_wins": no_wins,
            "no_losses": no_losses,
            "no_win_rate": round(no_win_rate, 4) if no_win_rate is not None else None,
            "pnl_cents": pnl_cents,
            "unmatched": unmatched,
        })

        agg_wins += wins
        agg_losses += losses
        agg_yes_wins += yes_wins
        agg_yes_losses += yes_losses
        agg_no_wins += no_wins
        agg_no_losses += no_losses
        agg_pnl += pnl_cents

    agg_total_settled = agg_wins + agg_losses
    agg_win_rate = (agg_wins / agg_total_settled) if agg_total_settled > 0 else 0.0

    # Per-side aggregate win rates
    agg_yes_total = agg_yes_wins + agg_yes_losses
    agg_no_total = agg_no_wins + agg_no_losses
    agg_yes_win_rate = (agg_yes_wins / agg_yes_total) if agg_yes_total > 0 else None
    agg_no_win_rate = (agg_no_wins / agg_no_total) if agg_no_total > 0 else None

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
            "yes_wins": agg_yes_wins,
            "yes_losses": agg_yes_losses,
            "yes_win_rate": round(agg_yes_win_rate, 4) if agg_yes_win_rate is not None else None,
            "no_wins": agg_no_wins,
            "no_losses": agg_no_losses,
            "no_win_rate": round(agg_no_win_rate, 4) if agg_no_win_rate is not None else None,
            "pnl_cents": agg_pnl,
            "sharpe": sharpe,
        },
    }


# ─── Performance metrics computation ───

def _compute_sharpe(daily_pnl_values):
    """Compute annualized Sharpe ratio from a list of daily P&L values (cents)."""
    if len(daily_pnl_values) < 2:
        return None
    mean_pnl = sum(daily_pnl_values) / len(daily_pnl_values)
    variance = sum((v - mean_pnl) ** 2 for v in daily_pnl_values) / (len(daily_pnl_values) - 1)
    std_pnl = math.sqrt(variance)
    if std_pnl <= 0:
        return None
    return round((mean_pnl / std_pnl) * math.sqrt(252), 4)


def _iso_week(date_str):
    """Convert a YYYY-MM-DD date string to ISO week string YYYY-WNN."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except (ValueError, TypeError):
        return None


def compute_performance_metrics(reconciliation):
    """Compute comprehensive P&L metrics from reconciliation data.

    Returns a dict suitable for saving to data/performance-metrics.json with:
    - per_bot: per-bot metrics with daily P&L, rolling Sharpe, gross/net breakdown
    - aggregate: aggregate metrics across all bots
    - timestamp: ISO timestamp
    """
    if not reconciliation:
        return None

    per_bot_metrics = {}
    agg_daily_gross = defaultdict(int)  # date -> gross P&L cents
    agg_daily_fees = defaultdict(int)   # date -> fee cents
    agg_total_gross = 0
    agg_total_fees = 0
    agg_total_trades = 0
    agg_wins = 0
    agg_losses = 0

    for bot_entry in reconciliation.get("per_bot", []):
        label = bot_entry["label"]
        wins = bot_entry.get("wins", 0)
        losses = bot_entry.get("losses", 0)
        pnl_cents = bot_entry.get("pnl_cents", 0)
        fee_cents = bot_entry.get("fee_cents", 0)
        trade_count = wins + losses
        win_rate = round(wins / trade_count, 4) if trade_count > 0 else 0.0

        # Daily P&L from per-bot daily_pnl if available
        daily_pnl = bot_entry.get("daily_pnl", {})

        # Gross = pnl_cents (revenue - cost), Net = gross - fees
        gross_pnl = pnl_cents
        net_pnl = pnl_cents - fee_cents

        # All-time Sharpe from daily P&L
        daily_values = list(daily_pnl.values()) if daily_pnl else []
        sharpe_all_time = _compute_sharpe(daily_values) if daily_values else None

        # Rolling 30-day Sharpe
        sharpe_rolling_30d = None
        if daily_pnl:
            sorted_days = sorted(daily_pnl.keys())
            if len(sorted_days) >= 2:
                last_30 = sorted_days[-30:] if len(sorted_days) >= 30 else sorted_days
                last_30_values = [daily_pnl[d] for d in last_30]
                sharpe_rolling_30d = _compute_sharpe(last_30_values)

        per_bot_metrics[label] = {
            "realized_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "fee_cents": fee_cents,
            "win_rate": win_rate,
            "wins": wins,
            "losses": losses,
            "trade_count": trade_count,
            "sharpe_all_time": sharpe_all_time,
            "sharpe_rolling_30d": sharpe_rolling_30d,
            "daily_pnl": daily_pnl,
        }

        # Accumulate aggregate daily
        for day, val in daily_pnl.items():
            agg_daily_gross[day] += val
        agg_total_gross += gross_pnl
        agg_total_fees += fee_cents
        agg_total_trades += trade_count
        agg_wins += wins
        agg_losses += losses

    # Aggregate metrics
    agg_daily_values = [agg_daily_gross[d] for d in sorted(agg_daily_gross)]
    agg_sharpe_all_time = _compute_sharpe(agg_daily_values) if agg_daily_values else None

    agg_sharpe_rolling_30d = None
    if agg_daily_gross:
        sorted_days = sorted(agg_daily_gross.keys())
        if len(sorted_days) >= 2:
            last_30 = sorted_days[-30:] if len(sorted_days) >= 30 else sorted_days
            last_30_values = [agg_daily_gross[d] for d in last_30]
            agg_sharpe_rolling_30d = _compute_sharpe(last_30_values)

    # Weekly P&L
    agg_weekly = defaultdict(int)
    for day, val in agg_daily_gross.items():
        week = _iso_week(day)
        if week:
            agg_weekly[week] += val

    # Cumulative P&L
    agg_cumulative = {}
    running = 0
    for day in sorted(agg_daily_gross):
        running += agg_daily_gross[day]
        agg_cumulative[day] = running

    aggregate = {
        "realized_pnl": agg_total_gross,
        "net_pnl": agg_total_gross - agg_total_fees,
        "fee_cents": agg_total_fees,
        "win_rate": round(agg_wins / agg_total_trades, 4) if agg_total_trades > 0 else 0.0,
        "wins": agg_wins,
        "losses": agg_losses,
        "trade_count": agg_total_trades,
        "sharpe_all_time": agg_sharpe_all_time,
        "sharpe_rolling_30d": agg_sharpe_rolling_30d,
        "daily_pnl": dict(sorted(agg_daily_gross.items())),
        "weekly_pnl": dict(sorted(agg_weekly.items())),
        "cumulative_pnl": agg_cumulative,
    }

    return {
        "per_bot": per_bot_metrics,
        "aggregate": aggregate,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _print_performance_metrics(metrics):
    """Print performance metrics in a human-readable table."""
    if not metrics:
        return

    print()
    print("=" * 70)
    print("P&L ANALYTICS")
    print("=" * 70)

    # Per-bot table
    per_bot = metrics.get("per_bot", {})
    if per_bot:
        print(f"\n  {'Bot':<22} {'Gross':>8}  {'Net':>8}  {'Fees':>6}  {'WR':>6}  {'Sharpe':>7}  {'30d':>7}")
        print("  " + "-" * 72)
        for label, stats in per_bot.items():
            gross = f"${stats['realized_pnl'] / 100:.2f}"
            net = f"${stats['net_pnl'] / 100:.2f}"
            fees = f"${stats['fee_cents'] / 100:.2f}"
            wr = f"{stats['win_rate'] * 100:.1f}%" if stats['trade_count'] > 0 else "N/A"
            sharpe = f"{stats['sharpe_all_time']:.2f}" if stats['sharpe_all_time'] is not None else "N/A"
            s30d = f"{stats['sharpe_rolling_30d']:.2f}" if stats['sharpe_rolling_30d'] is not None else "N/A"
            print(f"  {label:<22} {gross:>8}  {net:>8}  {fees:>6}  {wr:>6}  {sharpe:>7}  {s30d:>7}")

    # Aggregate
    agg = metrics.get("aggregate", {})
    if agg:
        print()
        print("  --- Aggregate ---")
        print(f"    Gross P&L:  ${agg.get('realized_pnl', 0) / 100:.2f}")
        print(f"    Net P&L:    ${agg.get('net_pnl', 0) / 100:.2f}")
        print(f"    Fees:       ${agg.get('fee_cents', 0) / 100:.2f}")
        wr = agg.get('win_rate', 0)
        print(f"    Win rate:   {wr * 100:.1f}% ({agg.get('wins', 0)}W/{agg.get('losses', 0)}L)")
        sharpe = agg.get("sharpe_all_time")
        s30d = agg.get("sharpe_rolling_30d")
        print(f"    Sharpe (all-time):  {sharpe:.2f}" if sharpe is not None else "    Sharpe (all-time):  N/A")
        print(f"    Sharpe (30-day):    {s30d:.2f}" if s30d is not None else "    Sharpe (30-day):    N/A")


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
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save performance metrics to data/performance-metrics.json (requires --reconcile).",
    )
    parser.add_argument(
        "--use-ledger",
        action="store_true",
        help="Read local trade history from the SQLite event ledger instead of JSON trade logs.",
    )
    args = parser.parse_args()
    ledger = get_event_ledger(path=DEFAULT_LEDGER_PATH) if args.use_ledger else None

    results = []

    for tf in TRADE_FILES:
        label = tf["label"]
        filepath = tf["path"]
        # Use a relative-looking path for display
        try:
            rel_path = str(filepath.relative_to(PROJECT_DIR))
        except ValueError:
            rel_path = str(filepath)

        trades = load_trades_safe(filepath, use_ledger=args.use_ledger, ledger=ledger)
        if trades is None or len(trades) == 0:
            results.append({"label": label, "rel_path": rel_path, "stats": None})
        else:
            stats = analyze_trades(trades)
            results.append({"label": label, "rel_path": rel_path, "stats": stats})

    # Reconciliation (optional — requires API credentials)
    reconciliation = None
    performance_metrics = None
    if args.reconcile:
        client = _get_client()
        settlements = fetch_settlements(client)
        fills = fetch_fills(client)

        local_trades_by_bot = []
        for tf in TRADE_FILES:
            trades = load_trades_safe(tf["path"], use_ledger=args.use_ledger, ledger=ledger)
            local_trades_by_bot.append({
                "label": tf["label"],
                "trades": trades or [],
            })

        reconciliation = reconcile_trades(local_trades_by_bot, settlements, fills)

        # Enhance reconciliation with per-bot daily P&L and fee data from settlements
        _enrich_reconciliation_with_daily_pnl(reconciliation, settlements)

        # Compute performance metrics
        performance_metrics = compute_performance_metrics(reconciliation)

    if args.save:
        if performance_metrics:
            from kalshi_auth import _atomic_write_json
            save_path = PROJECT_DIR / "data" / "performance-metrics.json"
            save_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(save_path, performance_metrics)
            print(f"Performance metrics saved to {save_path}", file=sys.stderr)
        else:
            print("Warning: --save requires --reconcile to generate performance data", file=sys.stderr)

    if args.json:
        print_json_report(results, reconciliation)
    else:
        print_text_report(results, reconciliation)
        if performance_metrics:
            _print_performance_metrics(performance_metrics)


def _enrich_reconciliation_with_daily_pnl(reconciliation, settlements):
    """Add per-bot daily_pnl and fee_cents to reconciliation data.

    This enriches the reconciliation output so compute_performance_metrics()
    can compute daily/rolling Sharpe per bot.
    """
    if not reconciliation or not settlements:
        return

    # Build ticker -> (bot_label, settled_day, profit, fee) mapping
    # First, build ticker -> bot_label from per_bot entries
    bot_label_by_ticker = {}
    for bot_entry in reconciliation.get("per_bot", []):
        label = bot_entry["label"]
        # We don't have direct ticker access here, so we track by the
        # reconciliation loop's revenue attribution
        pass

    # Build daily P&L from settlements grouped by settled_time
    # We need to match settlements to bots using the ticker -> bot mapping
    from trade_files import TRADE_FILES as _canonical
    ticker_to_label = {}
    for tf in _canonical:
        filepath = DATA_DIR / tf["filename"]
        trades = load_trades_safe(filepath)
        if trades:
            for t in trades:
                ticker = t.get("ticker", "")
                if ticker:
                    ticker_to_label[ticker] = tf["label"]

    # Per-bot daily P&L accumulation
    per_bot_daily = defaultdict(lambda: defaultdict(int))
    per_bot_fees = defaultdict(int)

    for s in settlements:
        ticker = s.get("ticker", s.get("market_ticker", ""))
        revenue = s.get("revenue", 0)
        try:
            revenue = int(revenue)
        except (TypeError, ValueError):
            revenue = 0
        # Kalshi "revenue" is gross payout; subtract cost to get profit
        cost = int(s.get("yes_total_cost", 0)) + int(s.get("no_total_cost", 0))
        profit = revenue - cost

        # Fee extraction
        try:
            fee_cents = round(float(s.get("fee_cost", "0")) * 100)
        except (TypeError, ValueError):
            fee_cents = 0

        st = s.get("settled_time", "")
        day = st[:10] if st and isinstance(st, str) else None

        label = ticker_to_label.get(ticker, "Unknown")
        if day:
            per_bot_daily[label][day] += profit
        per_bot_fees[label] += fee_cents

    # Merge into reconciliation per_bot entries
    for bot_entry in reconciliation.get("per_bot", []):
        label = bot_entry["label"]
        bot_entry["daily_pnl"] = dict(sorted(per_bot_daily.get(label, {}).items()))
        bot_entry["fee_cents"] = per_bot_fees.get(label, 0)


if __name__ == "__main__":
    main()
