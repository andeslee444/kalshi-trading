"""Fixed-fractional position sizing for Oracle.

The spec explicitly rejects Kelly sizing (Section 6): "v1 approach: Fixed
fractional sizing (1-2% per trade). Simple, robust, and won't blow up on
calibration errors."

Per-book sizing: Book A = 2%, Book B = 1.5%, Book C = 1%.
"""

from __future__ import annotations


def compute_contracts(
    bankroll_cents: int,
    position_size_pct: float,
    price_cents: int,
    max_contracts: int = 100,
) -> int:
    """Compute number of contracts using fixed-fractional sizing.

    bankroll_cents: total available bankroll in cents
    position_size_pct: fraction of bankroll to risk (e.g., 0.02 for 2%)
    price_cents: price per contract in cents (1-99)
    max_contracts: hard cap on contract count

    Returns number of contracts (0 if invalid inputs).
    """
    if bankroll_cents <= 0 or position_size_pct <= 0 or price_cents <= 0:
        return 0
    if price_cents >= 100:
        return 0

    budget_cents = bankroll_cents * position_size_pct
    contracts = int(budget_cents / price_cents)
    return min(max(contracts, 0), max_contracts)


def compute_risk_cents(contracts: int, price_cents: int) -> int:
    """Total capital at risk for a position."""
    return contracts * price_cents


BOOK_SIZE_PCT = {
    "A": 0.02,   # 2% of bankroll per Book A trade
    "B": 0.015,  # 1.5% per Book B trade
    "C": 0.01,   # 1% per Book C trade
}


def contracts_for_book(
    book: str,
    bankroll_cents: int,
    price_cents: int,
    max_contracts: int = 100,
) -> int:
    """Compute contracts using the book's default size percentage."""
    pct = BOOK_SIZE_PCT.get(book, 0.01)
    return compute_contracts(bankroll_cents, pct, price_cents, max_contracts)
