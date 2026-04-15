#!/usr/bin/env python3
"""Automated trade reconciliation — annotates trade records with settlement outcomes.

Walks all trade log files, matches each trade to API settlement data and fill info,
and writes settlement_result, settlement_revenue_cents, fill_price_cents, and
realized_edge back into the trade record.

Idempotent: normalizes existing records in place and clears stale settlement
annotations from unfilled orders.
Writes go through TradeStore so file format stays unchanged.

Usage:
    python3 scripts/reconcile-trades.py              # annotate all trade files
    python3 scripts/reconcile-trades.py --dry-run    # show what would change, don't write
"""

import argparse
import sys
import time
from pathlib import Path

# Add src/kalshi to path for imports
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src" / "kalshi")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from event_ledger import get_event_ledger
from kalshi_auth import KalshiClient, setup_logging
from settlement_utils import (
    allocate_integer_total,
    realized_edge_for_trade,
    settlement_payout_cents,
    settlement_result_for_trade,
    trade_has_filled_exposure,
)
from storage import TradeStore
from trade_files import ALL_TRADE_PATHS

log = setup_logging("reconcile")
ledger = get_event_ledger(logger=log)

# Use canonical trade file list from trade_files module
TRADE_FILES = ALL_TRADE_PATHS


def _get_with_retries(client, path, *, attempts=4, base_sleep_seconds=0.5):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return client.get(path)
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                raise
            delay = base_sleep_seconds * attempt
            log.warning(
                "Retrying %s after error (%d/%d): %s",
                path,
                attempt,
                attempts,
                exc,
            )
            time.sleep(delay)
    raise last_error


def _safe_int(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _dollars_to_cents(value):
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _fill_count(fill):
    count = fill.get("count_fp")
    if count in (None, ""):
        count = fill.get("count")
    return _safe_int(count)


def _fill_price_cents(fill):
    side = str(fill.get("side", "") or "").lower()
    if side == "yes":
        direct = fill.get("yes_price")
        if direct not in (None, ""):
            return _safe_int(direct)
        return _dollars_to_cents(fill.get("yes_price_dollars"))
    if side == "no":
        direct = fill.get("no_price")
        if direct not in (None, ""):
            return _safe_int(direct)
        return _dollars_to_cents(fill.get("no_price_dollars"))

    direct = fill.get("yes_price")
    if direct not in (None, ""):
        return _safe_int(direct)
    direct = fill.get("no_price")
    if direct not in (None, ""):
        return _safe_int(direct)
    if fill.get("yes_price_dollars") not in (None, ""):
        return _dollars_to_cents(fill.get("yes_price_dollars"))
    return _dollars_to_cents(fill.get("no_price_dollars"))


def _fetch_all_settlements(client):
    """Fetch all settled positions from the API (paginated).

    Returns dict: {ticker: {"revenue_cents": int, "yes_won": bool, "settled_time": str}}
    """
    settlements = {}
    cursor = None
    for _ in range(50):
        path = "/portfolio/settlements?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = _get_with_retries(client, path)
        except Exception as e:
            log.warning("Failed to fetch settlements: %s", e)
            break
        for s in data.get("settlements", []):
            ticker = s.get("ticker", s.get("market_ticker", ""))
            if ticker:
                settlements[ticker] = {
                    "revenue_cents": s.get("revenue", 0),
                    "yes_won": s.get("market_result", "") == "yes",
                    "settled_time": s.get("settled_time", ""),
                    "yes_count": _fill_count({"count_fp": s.get("yes_count_fp"), "count": s.get("yes_count")}),
                    "no_count": _fill_count({"count_fp": s.get("no_count_fp"), "count": s.get("no_count")}),
                    "yes_total_cost_cents": _dollars_to_cents(s.get("yes_total_cost_dollars"))
                    if s.get("yes_total_cost_dollars") not in (None, "")
                    else _safe_int(s.get("yes_total_cost", 0)),
                    "no_total_cost_cents": _dollars_to_cents(s.get("no_total_cost_dollars"))
                    if s.get("no_total_cost_dollars") not in (None, "")
                    else _safe_int(s.get("no_total_cost", 0)),
                }
        cursor = data.get("cursor")
        if not cursor or not data.get("settlements"):
            break
    return settlements


def _fetch_all_fills(client):
    """Fetch all order fills from the API (paginated).

    Returns dict: {order_id: {"fill_price_cents": int, "fill_count": int, "fill_cost_cents": int}}
    """
    fills = {}
    cursor = None
    for _ in range(50):
        path = "/portfolio/fills?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = _get_with_retries(client, path)
        except Exception as e:
            log.warning("Failed to fetch fills: %s", e)
            break
        for f in data.get("fills", []):
            order_id = f.get("order_id", "")
            if order_id:
                fill_count = _fill_count(f)
                if fill_count <= 0:
                    continue
                fill_price_cents = _fill_price_cents(f)
                existing = fills.setdefault(order_id, {
                    "fill_price_cents": None,
                    "fill_count": 0,
                    "fill_cost_cents": 0,
                })
                existing["fill_count"] += fill_count
                existing["fill_cost_cents"] += fill_price_cents * fill_count
                existing["fill_price_cents"] = int(
                    round(existing["fill_cost_cents"] / existing["fill_count"])
                )
        cursor = data.get("cursor")
        if not cursor or not data.get("fills"):
            break
    return fills


def _annotate_trade(trade, settlements, fills):
    """Annotate a single trade record with settlement/fill data.

    Returns True if the record was modified.
    Skips sell (exit) records — settlement belongs to the original buy.
    """
    # Skip sell (exit) records — legacy records without action are assumed buys
    if trade.get("action", "buy") != "buy":
        return False

    ticker = trade.get("ticker", "")
    order_id = trade.get("order_id", "")
    modified = False
    has_exposure, contract_count, fill_price_cents = trade_has_filled_exposure(trade, fills=fills)

    # Match fill metadata.
    fill = fills.get(order_id) if order_id else None
    fill_count = fill.get("fill_count") if fill else None
    fill_cost_cents = fill.get("fill_cost_cents") if fill else None
    if fill_price_cents is not None and trade.get("fill_price_cents") != fill_price_cents:
        trade["fill_price_cents"] = fill_price_cents
        modified = True
    if fill_price_cents is None and not has_exposure and trade.get("fill_price_cents") is not None:
        trade["fill_price_cents"] = None
        modified = True
    if fill_count is not None and trade.get("fill_count") != fill_count:
        trade["fill_count"] = fill_count
        modified = True
    if fill_count is None and not has_exposure and trade.get("fill_count") is not None:
        trade["fill_count"] = None
        modified = True
    if fill_cost_cents is not None and trade.get("cost_cents") != fill_cost_cents:
        trade["cost_cents"] = fill_cost_cents
        modified = True

    settlement_result = trade.get("settlement_result")
    settlement_revenue_cents = trade.get("settlement_revenue_cents")
    realized_edge = trade.get("realized_edge")

    if ticker in settlements and has_exposure:
        yes_won = settlements[ticker]["yes_won"]
        settlement_result = settlement_result_for_trade(trade.get("side", "yes"), yes_won)
        settlement_revenue_cents = settlement_payout_cents(settlement_result, contract_count)
        realized_edge = realized_edge_for_trade(
            trade,
            settlement_result,
            fill_price_cents=trade.get("fill_price_cents"),
        )
    elif not has_exposure:
        settlement_result = None
        settlement_revenue_cents = None
        realized_edge = None

    if trade.get("settlement_result") != settlement_result:
        trade["settlement_result"] = settlement_result
        modified = True
    if trade.get("settlement_revenue_cents") != settlement_revenue_cents:
        trade["settlement_revenue_cents"] = settlement_revenue_cents
        modified = True
    if trade.get("realized_edge") != realized_edge:
        trade["realized_edge"] = realized_edge
        modified = True

    return modified


def _apply_authoritative_settlement_totals(group, settlement, side):
    target_count = settlement.get(f"{side}_count", 0) or 0
    target_cost_cents = settlement.get(f"{side}_total_cost_cents", 0) or 0
    if target_count <= 0:
        return set()

    exact_rows = []
    unresolved_rows = []
    for entry in group:
        fill = entry["fill"]
        if fill and (fill.get("fill_count") or 0) > 0:
            exact_rows.append(entry)
        else:
            unresolved_rows.append(entry)

    fixed_count = sum((entry["fill"] or {}).get("fill_count", 0) or 0 for entry in exact_rows)
    fixed_cost = sum((entry["fill"] or {}).get("fill_cost_cents", 0) or 0 for entry in exact_rows)
    if fixed_count > target_count or fixed_cost > target_cost_cents or not unresolved_rows:
        return set()

    remaining_count = target_count - fixed_count
    remaining_cost = target_cost_cents - fixed_cost
    if remaining_count < len(unresolved_rows):
        return set()

    if len(unresolved_rows) == 1:
        count_allocations = [remaining_count]
    else:
        extra_counts = allocate_integer_total(
            remaining_count - len(unresolved_rows),
            [max(entry["base_count"], 1) for entry in unresolved_rows],
        )
        count_allocations = [1 + value for value in extra_counts]

    cost_weights = []
    for entry, count in zip(unresolved_rows, count_allocations):
        price = entry["price_cents"]
        cost_weights.append(max(price, 1) * max(count, 0))
    cost_allocations = allocate_integer_total(remaining_cost, cost_weights)

    modified_trades = set()
    for entry, count_value, cost_value in zip(unresolved_rows, count_allocations, cost_allocations):
        trade = entry["trade"]
        fill_price_cents = int(round(cost_value / count_value)) if count_value > 0 else None
        settlement_result = settlement_result_for_trade(side, settlement["yes_won"])
        settlement_revenue_cents = settlement_payout_cents(settlement_result, count_value)
        realized_edge = realized_edge_for_trade(
            trade,
            settlement_result,
            fill_price_cents=fill_price_cents,
        )

        if trade.get("fill_count") != count_value:
            trade["fill_count"] = count_value
            modified_trades.add(id(trade))
        if trade.get("cost_cents") != cost_value:
            trade["cost_cents"] = cost_value
            modified_trades.add(id(trade))
        if trade.get("fill_price_cents") != fill_price_cents:
            trade["fill_price_cents"] = fill_price_cents
            modified_trades.add(id(trade))
        if trade.get("settlement_result") != settlement_result:
            trade["settlement_result"] = settlement_result
            modified_trades.add(id(trade))
        if trade.get("settlement_revenue_cents") != settlement_revenue_cents:
            trade["settlement_revenue_cents"] = settlement_revenue_cents
            modified_trades.add(id(trade))
        if trade.get("realized_edge") != realized_edge:
            trade["realized_edge"] = realized_edge
            modified_trades.add(id(trade))
    return modified_trades


def _normalize_settled_groups(all_file_trades, settlements, fills):
    groups = {}
    for trade_file, _store, trades in all_file_trades:
        for trade in trades:
            if trade.get("action", "buy") != "buy":
                continue
            ticker = trade.get("ticker", "")
            if ticker not in settlements:
                continue
            side = str(trade.get("side", "yes") or "yes").lower()
            if side not in ("yes", "no"):
                continue
            fill = fills.get(trade.get("order_id")) if trade.get("order_id") else None
            has_exposure, contract_count, fill_price_cents = trade_has_filled_exposure(trade, fills=fills)
            if not has_exposure:
                continue
            groups.setdefault((ticker, side), []).append({
                "trade": trade,
                "trade_file": trade_file,
                "fill": fill,
                "base_count": contract_count or _safe_int(trade.get("count")) or 1,
                "price_cents": fill_price_cents or trade.get("fill_price_cents") or trade.get("price_cents") or 50,
            })

    modified_trades = set()
    for (ticker, side), group in groups.items():
        modified_trades.update(_apply_authoritative_settlement_totals(group, settlements[ticker], side))
    return modified_trades


def reconcile_all(dry_run=False):
    """Walk all trade files, match to API settlements/fills, annotate records."""
    client = KalshiClient()

    log.info("Fetching settlements and fills from API...")
    settlements = _fetch_all_settlements(client)
    fills = _fetch_all_fills(client)
    log.info("  %d settlements, %d fills fetched", len(settlements), len(fills))

    all_file_trades = []
    modified_trade_ids = set()
    total_annotated = 0
    total_skipped = 0

    for trade_file in TRADE_FILES:
        if not trade_file.exists():
            continue

        store = TradeStore(trade_file, logger=log)
        trades = store.load()
        if not trades:
            continue
        all_file_trades.append((trade_file, store, trades))

    for _trade_file, _store, trades in all_file_trades:
        for trade in trades:
            if _annotate_trade(trade, settlements, fills):
                modified_trade_ids.add(id(trade))
                total_annotated += 1
            else:
                total_skipped += 1

    normalized = _normalize_settled_groups(all_file_trades, settlements, fills)
    if normalized:
        modified_trade_ids.update(normalized)
        log.info(
            "  normalized %d settled rows from authoritative settlement side totals",
            len(normalized),
        )
        total_annotated += len(normalized)

    for trade_file, store, trades in all_file_trades:
        file_modified = sum(1 for trade in trades if id(trade) in modified_trade_ids)

        if file_modified > 0:
            log.info("  %s: %d/%d records annotated", trade_file.name, file_modified, len(trades))
            if not dry_run:
                try:
                    for trade in trades:
                        if trade.get("action", "buy") != "buy":
                            continue
                        if trade.get("order_id") and trade.get("fill_price_cents") is not None:
                            fill_count = trade.get("fill_count") or trade.get("count")
                            if fill_count:
                                ledger.record_fill({
                                    "timestamp": trade.get("timestamp"),
                                    "ticker": trade.get("ticker"),
                                    "order_id": trade.get("order_id"),
                                    "fill_price_cents": trade.get("fill_price_cents"),
                                    "fill_count": fill_count,
                                    "source_bot": trade.get("source_bot"),
                                }, source_path=trade_file)
                        if trade.get("settlement_result") is not None:
                            ledger.record_settlement(trade, source_path=trade_file)
                except Exception as e:
                    log.warning("Failed to dual-write reconcile events for %s: %s", trade_file.name, e)
                store.save(trades)

    log.info("Reconciliation complete: %d annotated, %d skipped (unchanged or no match)",
             total_annotated, total_skipped)
    if dry_run:
        log.info("DRY RUN — no files were modified")

    return total_annotated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reconcile trade records with API data")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    reconcile_all(dry_run=args.dry_run)
