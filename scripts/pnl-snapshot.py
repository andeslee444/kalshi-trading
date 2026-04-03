#!/usr/bin/env python3
"""P&L Snapshot — Dual-source verified financial summary.

Fetches authoritative data from Kalshi API, loads local trade logs,
cross-references both sources, writes data/financial-snapshot.json.

Usage:
    python3 scripts/pnl-snapshot.py              # generate snapshot
    python3 scripts/pnl-snapshot.py --print       # print to stdout instead of file
    python3 scripts/pnl-snapshot.py --pull-s3     # pull trade logs from S3 first
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ─── Project paths ───
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from artifact_contracts import normalize_financial_snapshot
from trade_files import TRADE_FILES as _CANONICAL_FILES

DATA_DIR = PROJECT_DIR / "data"
DEPOSITS_PATH = DATA_DIR / "deposits.json"
SNAPSHOT_PATH = DATA_DIR / "financial-snapshot.json"
UNATTRIBUTED_WEATHER_BOT = "unattributed-weather"
DEMO_WEATHER_HISTORY_BOT = "demo-weather-history"
BOT_DISPLAY_MAP = {
    DEMO_WEATHER_HISTORY_BOT: "demo-weather history",
    UNATTRIBUTED_WEATHER_BOT: "legacy automated weather history",
}
BOT_REPORTING_NOTES = {
    DEMO_WEATHER_HISTORY_BOT: (
        "Known demo-trader weather activity matched from data/demo-trades-log.json; "
        "exclude from canonical weather-family bot P&L."
    ),
    UNATTRIBUTED_WEATHER_BOT: (
        "API-only KXHIGH settlements/fills with no canonical local-order match. "
        "Current evidence suggests this is older automated weather activity outside "
        "today's canonical weather trade logs, not manual trading."
    ),
}


# ─── Pure computation functions (no I/O, fully testable) ───

def compute_realized_pnl(settlements):
    """Compute realized P&L from Kalshi API settlement records.

    Uses the authoritative formula: profit = revenue - yes_total_cost - no_total_cost
    (Kalshi's 'revenue' is gross payout, NOT net profit.)

    Returns dict with total_cents, fees, wins/losses, by_day, by_bot breakdown.
    """
    total_cents = 0
    total_fees_cents = 0
    wins = 0
    losses = 0
    by_day = defaultdict(int)

    for s in settlements:
        revenue = _safe_int(s.get("revenue", 0))
        yes_cost = _safe_int(s.get("yes_total_cost", 0))
        no_cost = _safe_int(s.get("no_total_cost", 0))
        profit = revenue - yes_cost - no_cost

        # Fee: Kalshi returns dollars as string (e.g. "0.04")
        try:
            fee_cents = round(float(s.get("fee_cost", "0") or "0") * 100)
        except (TypeError, ValueError):
            fee_cents = 0

        total_cents += profit
        total_fees_cents += fee_cents

        if profit > 0:
            wins += 1
        elif profit < 0:
            losses += 1

        # Daily breakdown by settlement date
        settled_time = s.get("settled_time", "")
        if settled_time and isinstance(settled_time, str) and len(settled_time) >= 10:
            day = settled_time[:10]
            by_day[day] += profit

    count = wins + losses
    return {
        "total_cents": total_cents,
        "total_fees_cents": total_fees_cents,
        "net_after_fees_cents": total_cents - total_fees_cents,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / count, 4) if count > 0 else 0.0,
        "by_day": dict(sorted(by_day.items())),
        "source": "kalshi_api_settlements",
    }


def _is_executed_status(status):
    normalized = (status or "").lower()
    return normalized in ("executed", "filled")


def _safe_fee_cents(value):
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _safe_api_fee_cents(value):
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _is_buy_action(value):
    if value is None:
        return True
    normalized = str(value).strip().lower()
    if normalized in ("", "buy", "none", "null"):
        return True
    return False


def _fill_count(fill):
    count = fill.get("count")
    if count in (None, "", 0):
        count = fill.get("count_fp")
    return _safe_int(count)


def _dollars_to_cents(value):
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _fill_price_cents(fill, side):
    if side == "yes":
        direct = fill.get("yes_price")
        if direct not in (None, ""):
            return _safe_int(direct)
        return _dollars_to_cents(fill.get("yes_price_dollars"))
    direct = fill.get("no_price")
    if direct not in (None, ""):
        return _safe_int(direct)
    return _dollars_to_cents(fill.get("no_price_dollars"))


def _weather_city_from_ticker(ticker):
    ticker = (ticker or "").upper()
    if not ticker.startswith("KXHIGH"):
        return None
    suffix = ticker[len("KXHIGH"):].split("-", 1)[0]
    return suffix or None


def _trade_city(trade):
    if not isinstance(trade, dict):
        return None
    city = trade.get("city")
    if city:
        return str(city).upper()
    return _weather_city_from_ticker(trade.get("ticker", ""))


def _init_rollup():
    return {"pnl_cents": 0, "wins": 0, "losses": 0, "fees_cents": 0}


def _accumulate_rollup(stats, pnl_cents, fee_cents):
    stats["pnl_cents"] += pnl_cents
    stats["fees_cents"] += fee_cents
    if pnl_cents > 0:
        stats["wins"] += 1
    elif pnl_cents < 0:
        stats["losses"] += 1


def _finalize_rollups(group):
    for stats in group.values():
        total = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(stats["wins"] / total, 4) if total > 0 else 0.0


def _init_local_reconciliation_stats():
    return {
        "local_buy_orders": 0,
        "executed_local_buy_orders": 0,
        "nonexecuted_local_buy_orders": 0,
        "orders_with_fills": 0,
        "unmatched_local_buy_orders_without_fills": 0,
        "unmatched_executed_buy_orders_without_fills": 0,
        "api_fills_without_local_order": 0,
        "filled_orders_without_settlement": 0,
        "settled_local_buy_orders": 0,
        "settled_nonexecuted_buy_orders": 0,
        "executed_settled_orders": 0,
        "executed_settled_orders_with_fill_and_settlement": 0,
        "executed_settled_orders_without_fill": 0,
    }


def _finalize_local_reconciliation_stats(group):
    for bot, stats in group.items():
        executed_settled = stats.get("executed_settled_orders", 0)
        covered = stats.get("executed_settled_orders_with_fill_and_settlement", 0)
        stats["executed_settled_fill_coverage"] = (
            round(covered / executed_settled, 4) if executed_settled > 0 else None
        )
        strict_local_basis = bool(
            executed_settled > 0
            and covered == executed_settled
            and stats.get("unmatched_executed_buy_orders_without_fills", 0) == 0
            and stats.get("api_fills_without_local_order", 0) == 0
        )
        # Weather-family bots can still use the local joined basis once every
        # executed-and-settled canonical order is covered. They carry a known
        # tail of older demo/manual/orphan KXHIGH fills that should remain
        # visible in reconciliation stats, but should not override the
        # canonical local weather logs.
        relaxed_weather_family_basis = bool(
            bot in {"weather", "source-monitor"}
            and executed_settled > 0
            and covered == executed_settled
            and stats.get("unmatched_executed_buy_orders_without_fills", 0) == 0
        )
        stats["eligible_local_join_basis"] = strict_local_basis or relaxed_weather_family_basis


def compute_realized_pnl_by_bot_api(settlements, local_trades, demo_weather_refs=None):
    """Legacy per-bot attribution from API settlements + local ticker mapping."""
    ticker_to_bot = {}
    for trade in local_trades:
        ticker = trade.get("ticker", "")
        bot = trade.get("source_bot", "")
        if ticker and bot:
            ticker_to_bot[ticker] = bot

    by_bot = defaultdict(_init_rollup)
    for settlement in settlements:
        ticker = settlement.get("ticker", "") or settlement.get("market_ticker", "")
        revenue = _safe_int(settlement.get("revenue", 0))
        cost = _safe_int(settlement.get("yes_total_cost", 0)) + _safe_int(settlement.get("no_total_cost", 0))
        profit = revenue - cost
        fee = _safe_api_fee_cents(settlement.get("fee_cost"))
        bot = ticker_to_bot.get(
            ticker,
            _infer_unmatched_api_bot(ticker, demo_weather_refs=demo_weather_refs),
        )
        _accumulate_rollup(by_bot[bot], profit, fee)

    result = dict(by_bot)
    _finalize_rollups(result)
    return result


def compute_realized_pnl_by_bot_local(local_trades, fills, settlements, demo_weather_refs=None):
    """Per-bot realized P&L from local buy orders joined to fills and outcomes."""
    settlement_result_by_ticker = {}
    for settlement in settlements:
        ticker = settlement.get("ticker", "") or settlement.get("market_ticker", "")
        market_result = str(settlement.get("market_result") or settlement.get("result") or "").lower()
        if ticker and market_result in ("yes", "no"):
            settlement_result_by_ticker[ticker] = market_result

    local_orders = {}
    for trade in local_trades:
        if not _is_buy_action(trade.get("action")):
            continue
        order_id = trade.get("order_id")
        if not order_id:
            continue
        if order_id in local_orders:
            continue
        local_orders[order_id] = {
            "ticker": trade.get("ticker", ""),
            "side": str(trade.get("side", "")).lower(),
            "source_bot": trade.get("source_bot") or _infer_bot(trade.get("ticker", "")),
            "city": _trade_city(trade),
            "source_type": str(trade.get("source_type", "")).lower() or None,
            "fee_cents": _safe_fee_cents(trade.get("fee_cents")),
            "status": str(trade.get("status", "")).lower(),
            "settlement_result": trade.get("settlement_result"),
        }

    fills_by_order = defaultdict(lambda: {
        "ticker": None,
        "side": None,
        "count": 0,
        "cost_cents": 0,
    })
    for fill in fills:
        if not _is_buy_action(fill.get("action")):
            continue
        order_id = fill.get("order_id")
        if not order_id:
            continue
        side = str(fill.get("side", "")).lower()
        if side not in ("yes", "no"):
            continue
        count = _fill_count(fill)
        if count <= 0:
            continue
        price = _fill_price_cents(fill, side)
        bucket = fills_by_order[order_id]
        if bucket["ticker"] is None:
            bucket["ticker"] = fill.get("ticker", "") or fill.get("market_ticker", "")
        if bucket["side"] is None:
            bucket["side"] = side
        bucket["count"] += count
        bucket["cost_cents"] += _safe_int(price) * count

    by_bot = defaultdict(_init_rollup)
    by_bot_reconciliation = defaultdict(_init_local_reconciliation_stats)
    matched_orders = 0
    unmatched_fills_without_local_trade = 0
    unmatched_filled_orders_without_settlement = 0

    for order_id, meta in local_orders.items():
        bot = meta.get("source_bot") or _infer_bot(meta.get("ticker", ""))
        bot_stats = by_bot_reconciliation[bot]
        bot_stats["local_buy_orders"] += 1
        executed = _is_executed_status(meta.get("status"))
        if executed:
            bot_stats["executed_local_buy_orders"] += 1
        else:
            bot_stats["nonexecuted_local_buy_orders"] += 1
        if order_id in fills_by_order:
            bot_stats["orders_with_fills"] += 1
        else:
            bot_stats["unmatched_local_buy_orders_without_fills"] += 1
            if executed:
                bot_stats["unmatched_executed_buy_orders_without_fills"] += 1

        settled = bool(
            meta.get("settlement_result") in ("won", "lost")
            or (meta.get("ticker") or "") in settlement_result_by_ticker
        )
        if settled:
            bot_stats["settled_local_buy_orders"] += 1
            if not executed:
                bot_stats["settled_nonexecuted_buy_orders"] += 1
            else:
                bot_stats["executed_settled_orders"] += 1
                if order_id not in fills_by_order:
                    bot_stats["executed_settled_orders_without_fill"] += 1

    for order_id, fill_row in fills_by_order.items():
        local = local_orders.get(order_id)
        if not local:
            unmatched_fills_without_local_trade += 1
            bot = _infer_unmatched_api_bot(
                fill_row.get("ticker", ""),
                order_id=order_id,
                demo_weather_refs=demo_weather_refs,
            )
            by_bot_reconciliation[bot]["api_fills_without_local_order"] += 1
            continue

        ticker = local.get("ticker") or fill_row.get("ticker") or ""
        market_result = settlement_result_by_ticker.get(ticker)
        if market_result not in ("yes", "no"):
            unmatched_filled_orders_without_settlement += 1
            bot = local.get("source_bot") or _infer_bot(ticker)
            by_bot_reconciliation[bot]["filled_orders_without_settlement"] += 1
            continue

        side = local.get("side") or fill_row.get("side")
        if side not in ("yes", "no"):
            continue

        won = (side == market_result)
        pnl_cents = (100 * fill_row["count"] - fill_row["cost_cents"]) if won else -fill_row["cost_cents"]
        fee_cents = local.get("fee_cents", 0)
        bot = local.get("source_bot") or _infer_bot(ticker)
        _accumulate_rollup(by_bot[bot], pnl_cents, fee_cents)
        if _is_executed_status(local.get("status")):
            by_bot_reconciliation[bot]["executed_settled_orders_with_fill_and_settlement"] += 1
        matched_orders += 1

        city = local.get("city")
        if city:
            city_map = by_bot[bot].setdefault("by_city", {})
            stats = city_map.setdefault(city, _init_rollup())
            _accumulate_rollup(stats, pnl_cents, fee_cents)

    result = dict(by_bot)
    for stats in result.values():
        city_map = stats.get("by_city")
        if isinstance(city_map, dict):
            _finalize_rollups(city_map)
    _finalize_rollups(result)
    _finalize_local_reconciliation_stats(by_bot_reconciliation)

    local_buy_orders = len(local_orders)
    unmatched_local_buy_orders_without_fills = sum(1 for order_id in local_orders if order_id not in fills_by_order)
    unmatched_executed_buy_orders_without_fills = sum(
        1
        for order_id, meta in local_orders.items()
        if order_id not in fills_by_order and _is_executed_status(meta.get("status"))
    )

    return {
        "basis": "local_buy_orders_joined_to_api_fills_and_settlement_outcomes",
        "by_bot": result,
        "matched_orders": matched_orders,
        "local_buy_orders": local_buy_orders,
        "unmatched_local_buy_orders_without_fills": unmatched_local_buy_orders_without_fills,
        "unmatched_executed_buy_orders_without_fills": unmatched_executed_buy_orders_without_fills,
        "unmatched_fills_without_local_trade": unmatched_fills_without_local_trade,
        "unmatched_filled_orders_without_settlement": unmatched_filled_orders_without_settlement,
        "by_bot_reconciliation": dict(by_bot_reconciliation),
    }


def _select_canonical_by_bot(by_bot_api, by_bot_local, by_bot_local_reconciliation, local_basis):
    canonical = {}
    basis_map = {}
    for bot in sorted(set(by_bot_api) | set(by_bot_local)):
        local_payload = by_bot_local.get(bot)
        api_payload = by_bot_api.get(bot)
        local_stats = by_bot_local_reconciliation.get(bot, {}) if isinstance(by_bot_local_reconciliation, dict) else {}
        use_local = bool(local_payload and local_stats.get("eligible_local_join_basis"))
        if use_local:
            canonical[bot] = local_payload
            basis_map[bot] = local_basis
        elif api_payload is not None:
            canonical[bot] = api_payload
            basis_map[bot] = "kalshi_api_settlements"
        elif local_payload is not None:
            canonical[bot] = local_payload
            basis_map[bot] = local_basis
    return canonical, basis_map


def compute_unrealized_pnl(positions, fills):
    """Compute unrealized P&L from open positions and historical fills.

    WARNING: This calculation is unreliable. Kalshi's market_exposure field
    returns cost basis, not current market value, so most positions show
    unrealized = $0. Use balance_check.implied_unrealized_cents instead.

    Returns dict with total unrealized and per-position breakdown.
    """
    # Build ticker → cost basis from fills
    ticker_cost = defaultdict(int)
    for f in fills:
        ticker = f.get("ticker", "")
        side = (f.get("side", "") or "").lower()
        price = _fill_price_cents(f, side)
        count = _fill_count(f)
        action = (f.get("action", "") or "").lower()
        if action == "buy":
            ticker_cost[ticker] += (price or 0) * count
        elif action == "sell":
            ticker_cost[ticker] -= (price or 0) * count

    result_positions = []
    total_unrealized = 0

    for p in positions:
        if p.get("position", 0) == 0:
            continue
        ticker = p.get("ticker", "")
        current_value = p.get("market_exposure", 0)
        cost = ticker_cost.get(ticker, 0)
        unrealized = current_value - cost

        result_positions.append({
            "ticker": ticker,
            "position": p.get("position", 0),
            "cost_cents": cost,
            "current_value_cents": current_value,
            "unrealized_cents": unrealized,
        })
        total_unrealized += unrealized

    return {
        "total_cents": total_unrealized,
        "positions": result_positions,
        "source": "kalshi_api_positions_and_fills",
    }


def verify_settlements(api_settlements, local_trades):
    """Cross-verify API settlements against local trade logs.

    Runs multiple checks and returns structured verification report.
    """
    checks = []

    # Build ticker sets (exclude sell/exit trades from local)
    api_tickers = {s.get("ticker", "") or s.get("market_ticker", "")
                   for s in api_settlements}
    local_buy_trades = [t for t in local_trades
                        if _is_buy_action(t.get("action"))]
    local_tickers = {t.get("ticker", "") for t in local_buy_trades
                     if t.get("ticker")}

    # Check 1: Settlement count match
    api_count = len(api_tickers)
    local_settled_tickers = local_tickers & api_tickers
    local_count = len(local_settled_tickers)
    count_match = api_count == local_count
    checks.append({
        "check": "settlement_count_match",
        "api": api_count,
        "local": local_count,
        "status": "ok" if count_match else "warning",
        "detail": (f"{api_count - local_count} API settlements have no matching "
                   f"local trade" if not count_match else ""),
    })

    # Check 2: P&L agreement per ticker
    # Build API P&L by ticker
    api_pnl_by_ticker = {}
    for s in api_settlements:
        ticker = s.get("ticker", "") or s.get("market_ticker", "")
        revenue = _safe_int(s.get("revenue", 0))
        cost = _safe_int(s.get("yes_total_cost", 0)) + _safe_int(s.get("no_total_cost", 0))
        api_pnl_by_ticker[ticker] = revenue - cost

    # Build local P&L by ticker from settlement_result + cost_cents + count.
    # NOTE: Do NOT use settlement_revenue_cents — it has inconsistent semantics
    # (reconcile-trades.py stores gross payout, backfill-settlements.py stores
    # net profit). Instead, derive P&L from settlement outcome directly.
    local_pnl_by_ticker = defaultdict(int)
    for t in local_buy_trades:
        ticker = t.get("ticker", "")
        result = t.get("settlement_result")
        if result is not None and ticker:
            count = t.get("count", 1) or 1
            cost = t.get("cost_cents", 0) or 0
            if result == "won":
                local_pnl_by_ticker[ticker] += 100 * count - cost
            elif result == "lost":
                local_pnl_by_ticker[ticker] += -cost

    # Compare where both exist
    common_tickers = set(api_pnl_by_ticker) & set(local_pnl_by_ticker)
    total_api_pnl = sum(api_pnl_by_ticker.get(t, 0) for t in common_tickers)
    total_local_pnl = sum(local_pnl_by_ticker.get(t, 0) for t in common_tickers)
    delta = abs(total_api_pnl - total_local_pnl)
    checks.append({
        "check": "pnl_agreement",
        "api_cents": total_api_pnl,
        "local_cents": total_local_pnl,
        "delta_cents": delta,
        "tickers_compared": len(common_tickers),
        "status": "ok" if delta == 0 else "warning",
    })

    # Check 3: Orphan detection
    unmatched_api = sorted(api_tickers - local_tickers)
    unmatched_local = sorted(local_tickers - api_tickers)

    checks.append({
        "check": "orphan_settlements",
        "count": len(unmatched_api),
        "status": "ok" if not unmatched_api else "warning",
    })
    checks.append({
        "check": "orphan_local_trades",
        "count": len(unmatched_local),
        "status": "ok" if not unmatched_local else "info",
        "detail": "Local trades not yet settled" if unmatched_local else "",
    })

    # Overall status
    statuses = [c["status"] for c in checks]
    if "error" in statuses:
        overall = "errors"
    elif "warning" in statuses:
        overall = "warnings"
    else:
        overall = "ok"

    return {
        "status": overall,
        "checks": checks,
        "unmatched_api_settlements": unmatched_api,
        "unmatched_local_trades": unmatched_local,
    }


def load_deposits(deposits_path):
    """Load manually-tracked deposit/withdrawal ledger.

    Returns dict with tracked, totals, and optional ROI.
    File format: JSON array of {date, type, amount_cents, note}.
    """
    path = Path(deposits_path) if deposits_path else None
    if not path or not path.exists():
        return {"tracked": False, "source": "data/deposits.json"}

    try:
        data = json.loads(path.read_text().strip())
        if not isinstance(data, list):
            return {"tracked": False, "source": str(path)}
    except (json.JSONDecodeError, ValueError, OSError):
        return {"tracked": False, "source": str(path)}

    total_deposited = 0
    total_withdrawn = 0
    for entry in data:
        amount = entry.get("amount_cents", 0)
        if entry.get("type") == "deposit":
            total_deposited += amount
        elif entry.get("type") == "withdrawal":
            total_withdrawn += amount

    return {
        "tracked": True,
        "total_deposited_cents": total_deposited,
        "total_withdrawn_cents": total_withdrawn,
        "net_funded_cents": total_deposited - total_withdrawn,
        "entries": len(data),
        "source": str(path),
    }


def build_snapshot(balance_cents, portfolio_value_cents, settlements, fills,
                   positions, local_trades, deposits_path, demo_weather_refs=None):
    """Assemble the complete financial snapshot from all data sources."""
    realized = compute_realized_pnl(settlements)
    unrealized = compute_unrealized_pnl(positions, fills)
    verification = verify_settlements(settlements, local_trades)
    deposits = load_deposits(deposits_path)
    by_bot_api = compute_realized_pnl_by_bot_api(
        settlements,
        local_trades,
        demo_weather_refs=demo_weather_refs,
    )
    by_bot_local = compute_realized_pnl_by_bot_local(
        local_trades,
        fills,
        settlements,
        demo_weather_refs=demo_weather_refs,
    )
    local_by_bot_total_cents = sum(
        stats.get("pnl_cents", 0)
        for stats in by_bot_local["by_bot"].values()
        if isinstance(stats, dict)
    )
    local_by_bot_is_complete = (
        by_bot_local.get("matched_orders", 0) > 0
        and by_bot_local.get("unmatched_executed_buy_orders_without_fills", 0) == 0
        and by_bot_local.get("unmatched_fills_without_local_trade", 0) == 0
        and by_bot_local.get("unmatched_filled_orders_without_settlement", 0) == 0
        and local_by_bot_total_cents == realized.get("total_cents", 0)
    )
    canonical_by_bot, canonical_basis_map = _select_canonical_by_bot(
        by_bot_api,
        by_bot_local["by_bot"],
        by_bot_local.get("by_bot_reconciliation", {}),
        by_bot_local["basis"],
    )
    basis_values = set(canonical_basis_map.values())
    if not canonical_basis_map:
        canonical_basis = "kalshi_api_settlements_fallback_incomplete_local_fill_coverage"
    elif len(basis_values) == 1:
        canonical_basis = next(iter(basis_values))
    else:
        canonical_basis = "hybrid_per_bot_local_or_api"
    realized["by_bot"] = canonical_by_bot
    realized["by_bot_basis"] = canonical_basis
    realized["by_bot_basis_map"] = canonical_basis_map
    realized["by_bot_display_map"] = {
        bot: BOT_DISPLAY_MAP[bot]
        for bot in BOT_DISPLAY_MAP
        if bot in canonical_by_bot or bot in by_bot_api
    }
    realized["by_bot_reporting_notes"] = {
        bot: BOT_REPORTING_NOTES[bot]
        for bot in BOT_REPORTING_NOTES
        if bot in canonical_by_bot or bot in by_bot_api
    }
    realized["by_bot_api_settlements"] = by_bot_api
    realized["by_bot_local_joined_fills"] = by_bot_local["by_bot"]
    realized["by_bot_local_reconciliation"] = by_bot_local.get("by_bot_reconciliation", {})
    realized["by_bot_reconciliation"] = {
        "api_account_total_cents": realized.get("total_cents", 0),
        "local_by_bot_total_cents": local_by_bot_total_cents,
        "local_by_bot_is_complete": local_by_bot_is_complete,
        "matched_orders": by_bot_local.get("matched_orders", 0),
        "local_buy_orders": by_bot_local.get("local_buy_orders", 0),
        "unmatched_local_buy_orders_without_fills": by_bot_local.get("unmatched_local_buy_orders_without_fills", 0),
        "unmatched_executed_buy_orders_without_fills": by_bot_local.get("unmatched_executed_buy_orders_without_fills", 0),
        "unmatched_fills_without_local_trade": by_bot_local.get("unmatched_fills_without_local_trade", 0),
        "unmatched_filled_orders_without_settlement": by_bot_local.get("unmatched_filled_orders_without_settlement", 0),
    }

    nav_cents = balance_cents + portfolio_value_cents

    # Balance check: derive true P&L from NAV vs deposits (ground truth).
    # Kalshi's market_exposure field returns cost basis, not current value,
    # so compute_unrealized_pnl is unreliable. The balance equation is authoritative.
    balance_check = _build_balance_check(nav_cents, realized, deposits)

    # ROI: use true total P&L (NAV - deposits) when available, not just realized
    if deposits.get("tracked") and deposits.get("net_funded_cents", 0) > 0:
        net_funded = deposits["net_funded_cents"]
        true_total_pnl = nav_cents - net_funded
        deposits["roi_pct"] = round(true_total_pnl / net_funded * 100, 2)

    return normalize_financial_snapshot({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_used": ["kalshi_api", "local_trade_logs"],
        "account": {
            "balance_cents": balance_cents,
            "portfolio_value_cents": portfolio_value_cents,
            "nav_cents": nav_cents,
        },
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "balance_check": balance_check,
        "verification": verification,
        "deposits": deposits,
    })


def _build_balance_check(nav_cents, realized, deposits):
    """Cross-check P&L using the balance equation: true_pnl = NAV - deposits.

    Kalshi's market_exposure field returns cost basis, not current market value,
    so compute_unrealized_pnl is unreliable. This function derives the true
    unrealized P&L from the authoritative NAV and deposit data.
    """
    if not deposits.get("tracked"):
        return {"available": False, "reason": "deposits not tracked"}

    net_funded = deposits.get("net_funded_cents", 0)
    realized_net = realized.get("net_after_fees_cents", 0)

    true_total_pnl = nav_cents - net_funded
    implied_unrealized = true_total_pnl - realized_net

    return {
        "available": True,
        "nav_cents": nav_cents,
        "net_funded_cents": net_funded,
        "true_total_pnl_cents": true_total_pnl,
        "realized_net_cents": realized_net,
        "implied_unrealized_cents": implied_unrealized,
    }


def _infer_bot(ticker):
    """Best-effort bot attribution from ticker prefix."""
    t = (ticker or "").upper()
    if t.startswith("KXHIGH"):
        return "weather"
    if t.startswith(("KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP")):
        return "crypto"
    if t.startswith(("KXCPI", "KXGDP", "KXJOBS", "KXFED", "KXECONSTAT")):
        return "economics"
    if t.startswith(("KXALBUM", "KX1ALBUM")):
        return "entertainment"
    return "other"


def _infer_unmatched_api_bot(ticker, *, order_id=None, demo_weather_refs=None):
    """Attribute API-only rows conservatively when no local order exists."""
    t = (ticker or "").upper()
    if t.startswith("KXHIGH"):
        refs = demo_weather_refs or {}
        demo_tickers = refs.get("tickers", set())
        demo_order_ids = refs.get("order_ids", set())
        if t in demo_tickers or (order_id and str(order_id) in demo_order_ids):
            return DEMO_WEATHER_HISTORY_BOT
        return UNATTRIBUTED_WEATHER_BOT
    return _infer_bot(ticker)


def _safe_int(val):
    """Convert to int safely."""
    try:
        return int(round(float(val)))
    except (TypeError, ValueError):
        return 0


# ─── I/O functions (not tested in unit tests) ───

def _load_local_trades():
    """Load all local trade logs using canonical trade_files list."""
    all_trades = []
    for tf in _CANONICAL_FILES:
        filepath = DATA_DIR / tf["filename"]
        if not filepath.exists():
            continue
        try:
            text = filepath.read_text().strip()
            if not text:
                continue
            trades = json.loads(text)
            if isinstance(trades, list):
                for t in trades:
                    if not t.get("source_bot"):
                        t["source_bot"] = tf["bot"]
                    all_trades.append(t)
        except (json.JSONDecodeError, ValueError, OSError):
            continue
    return all_trades


def _load_demo_weather_refs():
    """Load known demo weather rows so API-only weather history can be split."""
    path = DATA_DIR / "demo-trades-log.json"
    if not path.exists():
        return {"tickers": set(), "order_ids": set()}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return {"tickers": set(), "order_ids": set()}

    if isinstance(payload, dict):
        rows = payload.get("trades", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    tickers = set()
    order_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker", "")).upper()
        if not ticker.startswith("KXHIGH"):
            continue
        tickers.add(ticker)
        order_id = row.get("order_id")
        if order_id:
            order_ids.add(str(order_id))
    return {"tickers": tickers, "order_ids": order_ids}


def _fetch_api_data():
    """Fetch all required data from Kalshi API.

    Raises RuntimeError if balance fetch fails (cannot produce valid snapshot).
    Settlement/fill/position fetches log warnings on failure but continue.
    """
    from kalshi_auth import KalshiClient, _atomic_write_json
    client = KalshiClient()

    # Balance (required — cannot produce snapshot without it)
    balance_data = client.get("/portfolio/balance")
    balance_cents = balance_data.get("balance", 0)
    portfolio_value_cents = balance_data.get("portfolio_value", 0)

    # Settlements (paginated)
    settlements = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/settlements?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = client.get(path)
        except Exception as e:
            print(f"  WARNING: settlement fetch failed: {e}")
            break
        batch = data.get("settlements", [])
        settlements.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    # Fills (paginated)
    fills = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/fills?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = client.get(path)
        except Exception as e:
            print(f"  WARNING: fills fetch failed: {e}")
            break
        batch = data.get("fills", [])
        fills.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    # Positions
    try:
        pos_data = client.get("/portfolio/positions")
        positions = [p for p in pos_data.get("market_positions", [])
                     if p.get("position", 0) != 0]
    except Exception as e:
        print(f"  WARNING: positions fetch failed: {e}")
        positions = []

    return balance_cents, portfolio_value_cents, settlements, fills, positions


def main():
    parser = argparse.ArgumentParser(
        description="Generate dual-source verified P&L snapshot."
    )
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="Print to stdout instead of writing file")
    parser.add_argument("--pull-s3", action="store_true",
                        help="Pull trade logs from S3 before generating snapshot")
    args = parser.parse_args()

    if args.pull_s3:
        print("Pulling trade logs from S3...")
        subprocess.run(
            ["bash", str(PROJECT_DIR / "scripts" / "s3-sync.sh"), "download"],
            check=True,
        )

    print("Fetching data from Kalshi API...")
    balance, portfolio_value, settlements, fills, positions = _fetch_api_data()
    print(f"  {len(settlements)} settlements, {len(fills)} fills, {len(positions)} open positions")

    print("Loading local trade logs...")
    local_trades = _load_local_trades()
    print(f"  {len(local_trades)} local trade records")
    demo_weather_refs = _load_demo_weather_refs()

    deposits_path = DEPOSITS_PATH if DEPOSITS_PATH.exists() else None

    snapshot = build_snapshot(
        balance_cents=balance,
        portfolio_value_cents=portfolio_value,
        settlements=settlements,
        fills=fills,
        positions=positions,
        local_trades=local_trades,
        deposits_path=deposits_path,
        demo_weather_refs=demo_weather_refs,
    )

    if args.print_only:
        print(json.dumps(snapshot, indent=2))
    else:
        from kalshi_auth import _atomic_write_json
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(SNAPSHOT_PATH, snapshot)
        print(f"Snapshot written to {SNAPSHOT_PATH}")

        # Print verification summary
        v = snapshot["verification"]
        status = v["status"].upper()
        print(f"\nVerification: {status}")
        for c in v["checks"]:
            icon = "+" if c["status"] == "ok" else "!" if c["status"] == "warning" else "x"
            detail = f" -- {c.get('detail', '')}" if c.get("detail") else ""
            print(f"  {icon} {c['check']}{detail}")

        pnl = snapshot["realized_pnl"]
        print(f"\nRealized P&L: ${pnl['total_cents'] / 100:+.2f} "
              f"({pnl['wins']}W/{pnl['losses']}L, "
              f"{pnl['win_rate'] * 100:.1f}% WR)")
        print(f"Fees: ${pnl['total_fees_cents'] / 100:.2f}")
        print(f"NAV: ${snapshot['account']['nav_cents'] / 100:.2f}")

        bc = snapshot.get("balance_check", {})
        if bc.get("available"):
            print(f"\nBalance Check (ground truth):")
            print(f"  Deposits: ${bc['net_funded_cents'] / 100:.2f}")
            print(f"  True Total P&L: ${bc['true_total_pnl_cents'] / 100:+.2f}")
            print(f"  Realized (net): ${bc['realized_net_cents'] / 100:+.2f}")
            print(f"  Implied Unrealized: ${bc['implied_unrealized_cents'] / 100:+.2f}")
            if snapshot["deposits"].get("roi_pct") is not None:
                print(f"  ROI: {snapshot['deposits']['roi_pct']:+.2f}%")


if __name__ == "__main__":
    main()
