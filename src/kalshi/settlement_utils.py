"""Shared helpers for settlement annotation and realized P&L."""

from __future__ import annotations


EXECUTED_TRADE_STATUSES = {
    "complete",
    "executed",
    "filled",
    "filled_or_canceled",
    "partially_filled",
    "partial_fill",
    "settled",
}


def _coerce_nonnegative_int(value):
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return 0
    return coerced if coerced > 0 else 0


def allocate_integer_total(total, weights):
    """Allocate an integer total across non-negative weights.

    Uses largest-remainder rounding and guarantees the allocations sum to total.
    """
    total = _coerce_nonnegative_int(total)
    normalized = []
    for weight in weights:
        try:
            normalized.append(max(float(weight or 0), 0.0))
        except (TypeError, ValueError):
            normalized.append(0.0)

    if total <= 0 or not normalized:
        return [0] * len(normalized)

    weight_sum = sum(normalized)
    if weight_sum <= 0:
        base = total // len(normalized)
        remainder = total % len(normalized)
        return [base + (1 if i < remainder else 0) for i in range(len(normalized))]

    raw = [total * weight / weight_sum for weight in normalized]
    floors = [int(value) for value in raw]
    remainder = total - sum(floors)
    if remainder > 0:
        ranked = sorted(
            range(len(raw)),
            key=lambda idx: (raw[idx] - floors[idx], normalized[idx], -idx),
            reverse=True,
        )
        for idx in ranked[:remainder]:
            floors[idx] += 1
    return floors


def is_winning_settlement(settlement_result):
    return settlement_result in ("won", "yes", True, 1)


def is_losing_settlement(settlement_result):
    return settlement_result in ("lost", "no", False, 0)


def resolved_contract_count(trade, fill_count=None):
    """Return the best available settled contract count for a trade."""
    candidates = [
        fill_count,
        trade.get("fill_count"),
        trade.get("count"),
    ]
    for candidate in candidates:
        count = _coerce_nonnegative_int(candidate)
        if count > 0:
            return count
    return 0


def settlement_result_for_trade(side, yes_won):
    """Convert a market-level YES outcome into the trade's P&L result."""
    if str(side or "yes").lower() == "yes":
        return "won" if yes_won else "lost"
    return "won" if not yes_won else "lost"


def settlement_payout_cents(settlement_result, contract_count):
    """Gross payout at settlement, excluding entry cost."""
    if contract_count <= 0:
        return None
    return 100 * contract_count if is_winning_settlement(settlement_result) else 0


def trade_has_filled_exposure(trade, fills=None):
    """Return whether a trade row represents executed exposure.

    Records with an order_id must have either fill data or an executed status.
    Legacy rows without an order_id are treated as executed if they have count.
    """
    if trade.get("action", "buy") != "buy":
        return False, 0, None

    order_id = trade.get("order_id", "")
    fill = fills.get(order_id) if order_id and fills else None
    fill_count = _coerce_nonnegative_int(fill.get("fill_count")) if fill else 0
    fill_price_cents = fill.get("fill_price_cents") if fill else None

    if fill_count > 0:
        return True, resolved_contract_count(trade, fill_count), fill_price_cents

    if order_id:
        status = str(trade.get("status", "") or "").strip().lower()
        if status in EXECUTED_TRADE_STATUSES:
            return True, resolved_contract_count(trade, fill_count), fill_price_cents
        return False, resolved_contract_count(trade, fill_count), fill_price_cents

    count = resolved_contract_count(trade, fill_count)
    return count > 0, count, fill_price_cents


def realized_edge_for_trade(trade, settlement_result, fill_price_cents=None):
    """Realized edge in the P(YES) frame used across the bots."""
    if not settlement_result or trade.get("model_prob") is None:
        return None

    fill_price = fill_price_cents
    if fill_price is None:
        fill_price = trade.get("fill_price_cents")
    if fill_price is None:
        fill_price = trade.get("price_cents")
    if fill_price is None:
        fill_price = 50

    side = str(trade.get("side", "yes") or "yes").lower()
    if side == "yes":
        actual = 1.0 if is_winning_settlement(settlement_result) else 0.0
        implied = fill_price / 100.0
    else:
        actual = 0.0 if is_winning_settlement(settlement_result) else 1.0
        implied = 1.0 - fill_price / 100.0
    return round(actual - implied, 4)


def compute_trade_pnl_cents(trade):
    """Compute realized P&L from a settled binary trade.

    Prefers explicit settlement_result plus contract count, which is stable across
    both historical payout semantics used in legacy artifacts.
    """
    settlement = trade.get("settlement_result")
    if settlement is None:
        return 0, False

    cost = int(trade.get("cost_cents", 0) or 0)
    count = resolved_contract_count(trade)

    if count > 0:
        payout = settlement_payout_cents(settlement, count)
        if payout is not None:
            return payout - cost, True

    revenue = trade.get("settlement_revenue_cents")
    if revenue is None:
        return 0, False

    try:
        revenue = int(revenue)
    except (TypeError, ValueError):
        return 0, False

    if is_winning_settlement(settlement):
        if revenue >= cost:
            return revenue - cost, True
        return revenue, True

    if is_losing_settlement(settlement):
        if revenue <= 0:
            return revenue if revenue < 0 else -cost, True
        return revenue - cost, True

    return 0, False
