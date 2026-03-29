"""Kalshi fee schedule for Oracle.

IMPORTANT: Kalshi fees are profit-based, not payout-based.
Fee = 0.07 * profit, where profit = payout - cost (only charged if positive).
This differs from the per-contract formula in domain/shared/sizing.py which uses
fee = 0.07 * P * (1-P) * 100 as an approximation.

For Oracle's fixed-fractional sizing, we compute exact post-fee profit.
"""

from __future__ import annotations

FEE_RATE = 0.07  # 7% of profit


def kalshi_fee_on_profit(profit_cents: float) -> float:
    """Compute Kalshi fee on a winning trade's profit.

    Fee is only charged on profit (payout - cost), not on the full payout.
    If profit <= 0, no fee is charged.
    """
    if profit_cents <= 0:
        return 0.0
    return profit_cents * FEE_RATE


def net_profit_cents(payout_cents: float, cost_cents: float) -> float:
    """Compute net profit after Kalshi fee.

    payout_cents: total payout if contract settles in your favor (contracts * 100)
    cost_cents: total cost paid (contracts * entry_price_cents)

    Returns net profit (can be negative for losing trades — no fee charged).
    """
    gross_profit = payout_cents - cost_cents
    if gross_profit <= 0:
        return gross_profit
    fee = kalshi_fee_on_profit(gross_profit)
    return gross_profit - fee


def expected_value_cents(
    model_prob: float, entry_price_cents: int, contracts: int = 1,
) -> float:
    """Expected value per trade accounting for Kalshi fees.

    For YES side:
      win: payout = 100 * contracts, cost = price * contracts
      lose: payout = 0, cost = price * contracts

    Returns expected net P&L in cents.
    """
    cost = entry_price_cents * contracts
    payout = 100 * contracts

    # Win scenario: gross profit = payout - cost, fee = 7% of profit
    gross_win = payout - cost
    fee_win = kalshi_fee_on_profit(gross_win)
    net_win = gross_win - fee_win

    # Lose scenario: lose entire cost, no fee
    net_lose = -cost

    return model_prob * net_win + (1 - model_prob) * net_lose
