"""Book C fill monitoring: timeout, repricing, slippage tracking.

For Book C live signals, we need fast execution:
- 15-second fill timeout
- Max 2 reprices before canceling
- Slippage tracking with auto-disable threshold

If a Book C order isn't filled within 15s, we reprice (adjust limit price
toward market). After 2 reprices, we cancel. If observed slippage across
recent trades exceeds the threshold, Book C auto-disables.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import deque

_log = logging.getLogger("oracle.execution.fill")


@dataclass
class FillAttempt:
    """Track a single order fill attempt."""
    ticker: str
    submitted_price_cents: int
    filled_price_cents: int = 0
    reprices: int = 0
    submitted_at: float = field(default_factory=time.time)
    filled_at: float = 0.0
    canceled: bool = False
    timed_out: bool = False

    @property
    def slippage_cents(self) -> int:
        """Difference between submitted and filled price (positive = adverse)."""
        if self.filled_price_cents <= 0:
            return 0
        return abs(self.filled_price_cents - self.submitted_price_cents)

    @property
    def fill_time_seconds(self) -> float:
        if self.filled_at <= 0:
            return 0.0
        return self.filled_at - self.submitted_at


class FillMonitor:
    """Monitors Book C order fills and tracks slippage for auto-disable."""

    def __init__(
        self,
        fill_timeout_seconds: float = 15.0,
        max_reprices: int = 2,
        slippage_disable_threshold_cents: int = 3,
        slippage_disable_min_trades: int = 20,
        history_size: int = 100,
    ):
        self._timeout = fill_timeout_seconds
        self._max_reprices = max_reprices
        self._slippage_threshold = slippage_disable_threshold_cents
        self._slippage_min_trades = slippage_disable_min_trades
        self._history: deque[FillAttempt] = deque(maxlen=history_size)
        self._disabled = False

    @property
    def is_disabled(self) -> bool:
        return self._disabled

    def reset_disable(self) -> None:
        self._disabled = False

    def start_fill(self, ticker: str, price_cents: int) -> FillAttempt:
        """Start tracking a new fill attempt."""
        attempt = FillAttempt(
            ticker=ticker,
            submitted_price_cents=price_cents,
        )
        return attempt

    def should_reprice(self, attempt: FillAttempt) -> bool:
        """Check if we should reprice (timed out but reprices still available)."""
        if attempt.reprices >= self._max_reprices:
            return False  # no more reprices, should cancel instead
        elapsed = time.time() - attempt.submitted_at
        # Reprice after each timeout window (15s per attempt)
        if elapsed >= self._timeout * (attempt.reprices + 1):
            return True
        return False

    def should_cancel(self, attempt: FillAttempt) -> bool:
        """Check if we should cancel the order (reprices exhausted + timed out)."""
        if attempt.reprices >= self._max_reprices:
            elapsed = time.time() - attempt.submitted_at
            # Cancel after final timeout window
            return elapsed >= self._timeout * (attempt.reprices + 1)
        return False

    def record_reprice(self, attempt: FillAttempt, new_price_cents: int) -> None:
        """Record a reprice event."""
        attempt.reprices += 1
        attempt.submitted_price_cents = new_price_cents
        _log.info(
            "Repriced %s: attempt %d/%d, new price %dc",
            attempt.ticker, attempt.reprices, self._max_reprices, new_price_cents,
        )

    def record_fill(self, attempt: FillAttempt, filled_price_cents: int) -> None:
        """Record a successful fill."""
        attempt.filled_price_cents = filled_price_cents
        attempt.filled_at = time.time()
        self._history.append(attempt)
        self._check_slippage()

    def record_cancel(self, attempt: FillAttempt, reason: str = "timeout") -> None:
        """Record an order cancellation."""
        attempt.canceled = True
        attempt.timed_out = reason == "timeout"
        self._history.append(attempt)

    def _check_slippage(self) -> None:
        """Check if average slippage exceeds threshold, auto-disable if so."""
        filled = [a for a in self._history if a.filled_price_cents > 0]
        if len(filled) < self._slippage_min_trades:
            return

        avg_slippage = sum(a.slippage_cents for a in filled) / len(filled)
        if avg_slippage > self._slippage_threshold:
            _log.warning(
                "Book C auto-disabled: avg slippage %.1fc > threshold %dc "
                "over %d trades",
                avg_slippage, self._slippage_threshold, len(filled),
            )
            self._disabled = True

    def stats(self) -> dict:
        """Return fill monitoring statistics."""
        filled = [a for a in self._history if a.filled_price_cents > 0]
        canceled = [a for a in self._history if a.canceled]

        return {
            "total_attempts": len(self._history),
            "fills": len(filled),
            "cancels": len(canceled),
            "avg_slippage_cents": (
                sum(a.slippage_cents for a in filled) / len(filled)
                if filled else 0.0
            ),
            "avg_fill_time_seconds": (
                sum(a.fill_time_seconds for a in filled) / len(filled)
                if filled else 0.0
            ),
            "disabled": self._disabled,
        }
