"""Book C quote quality validation from orderbook data.

Before executing any Book C live signal, we verify:
1. Spread <= max (default 8 cents)
2. Depth >= min (default 5 contracts on each side)
3. Quote age <= max (default 5 seconds)

If any check fails, the signal is rejected to avoid poor fills.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from domain.oracle.models import QuoteSnapshot

_log = logging.getLogger("oracle.execution.quote")


@dataclass
class QuoteCheckResult:
    """Result of an orderbook quality check."""
    passed: bool
    reason: str = ""
    spread: int = 0
    min_depth: int = 0
    age_seconds: float = 0.0


def _price_to_cents(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if 0.0 <= value <= 1.0:
            return int(Decimal(str(value * 100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    text = str(value).strip()
    if "." in text:
        return int((Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int(text)


def _size_to_int(value) -> int:
    if value in (None, ""):
        return 0
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def check_quote_quality(
    quote: QuoteSnapshot,
    max_spread: int = 8,
    min_depth: int = 5,
    max_age_seconds: float = 5.0,
) -> QuoteCheckResult:
    """Validate orderbook quote quality for Book C execution.

    quote: QuoteSnapshot from get_orderbook().
    max_spread: Maximum bid-ask spread in cents.
    min_depth: Minimum contracts on each side.
    max_age_seconds: Maximum quote age.

    Returns QuoteCheckResult with passed=True if all checks pass.
    """
    spread = quote.spread
    min_side_depth = min(quote.bid_depth, quote.ask_depth)
    age = quote.age_seconds

    if spread > max_spread:
        return QuoteCheckResult(
            passed=False,
            reason=f"Spread {spread}c > max {max_spread}c",
            spread=spread,
            min_depth=min_side_depth,
            age_seconds=age,
        )

    if min_side_depth < min_depth:
        return QuoteCheckResult(
            passed=False,
            reason=f"Depth {min_side_depth} < min {min_depth}",
            spread=spread,
            min_depth=min_side_depth,
            age_seconds=age,
        )

    if age > max_age_seconds:
        return QuoteCheckResult(
            passed=False,
            reason=f"Quote age {age:.1f}s > max {max_age_seconds}s",
            spread=spread,
            min_depth=min_side_depth,
            age_seconds=age,
        )

    return QuoteCheckResult(
        passed=True,
        spread=spread,
        min_depth=min_side_depth,
        age_seconds=age,
    )


def quote_from_orderbook(ticker: str, orderbook: dict) -> QuoteSnapshot:
    """Create a QuoteSnapshot from Kalshi orderbook API response.

    orderbook: Response from GET /markets/{ticker}/orderbook.
    Expected formats:
    {
        "orderbook": {
            "yes": [[price, quantity], ...],
            "no": [[price, quantity], ...]
        }
    }
    or
    {
        "orderbook_fp": {
            "yes_dollars": [["0.5800", "100.00"], ...],
            "no_dollars": [["0.4100", "95.00"], ...]
        }
    }
    """
    if "orderbook_fp" in orderbook:
        book = orderbook.get("orderbook_fp", {})
        yes_levels = book.get("yes_dollars", [])
        no_levels = book.get("no_dollars", [])
    else:
        book = orderbook.get("orderbook", orderbook)
        yes_levels = book.get("yes", [])
        no_levels = book.get("no", [])

    # Best bid = highest YES bid; best ask = lowest YES ask (100 - best NO bid)
    yes_bid = _price_to_cents(yes_levels[0][0]) if yes_levels else 0
    yes_bid_depth = _size_to_int(yes_levels[0][1]) if yes_levels else 0

    no_bid = _price_to_cents(no_levels[0][0]) if no_levels else 0
    no_bid_depth = _size_to_int(no_levels[0][1]) if no_levels else 0

    yes_ask = 100 - no_bid if no_bid else 100
    ask_depth = no_bid_depth

    return QuoteSnapshot(
        ticker=ticker,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        bid_depth=yes_bid_depth,
        ask_depth=ask_depth,
        timestamp=time.time(),
    )
