"""Order monitoring helpers extracted from kalshi_auth."""

from __future__ import annotations

import logging
import time


class OrderMonitor:
    """Tracks pending orders and manages their lifecycle."""

    def __init__(self, client, log=None, max_age_seconds=300, check_interval=30):
        self.client = client
        self.log = log or logging.getLogger("order-monitor")
        self.max_age_seconds = max_age_seconds
        self.check_interval = check_interval
        self._pending = {}
        self._last_check = 0

    def track(self, order_id, ticker, side, price_cents, count):
        """Register a newly placed order for monitoring."""
        self._pending[order_id] = {
            "placed_at": time.time(),
            "ticker": ticker,
            "side": side,
            "price": price_cents,
            "count": count,
        }

    def check_orders(self):
        """Poll order statuses and handle stale orders."""
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {}
        self._last_check = now

        if not self._pending:
            return {}

        changes = {}

        try:
            data = self.client.get("/portfolio/orders?status=resting")
            resting_ids = {o.get("order_id") for o in data.get("orders", [])}
        except Exception as e:
            self.log.warning("OrderMonitor: failed to fetch resting orders: %s", e)
            return {}

        stale_ids = []
        for order_id, info in list(self._pending.items()):
            age = now - info["placed_at"]

            if order_id not in resting_ids:
                self.log.info(
                    "OrderMonitor: %s on %s no longer resting (filled/canceled after %.0fs)",
                    order_id[:12],
                    info["ticker"],
                    age,
                )
                changes[order_id] = {"status": "filled_or_canceled", "action": "removed"}
                del self._pending[order_id]
            elif age > self.max_age_seconds:
                stale_ids.append(order_id)

        for order_id in stale_ids:
            info = self._pending[order_id]
            success = self.cancel_order(order_id)
            if success:
                self.log.info(
                    "OrderMonitor: canceled stale order %s on %s (age %.0fs > %ds)",
                    order_id[:12],
                    info["ticker"],
                    now - info["placed_at"],
                    self.max_age_seconds,
                )
                changes[order_id] = {"status": "canceled_stale", "action": "canceled"}
                del self._pending[order_id]

        return changes

    def cancel_order(self, order_id):
        """Cancel a specific order. Returns True on success."""
        try:
            self.client.delete(f"/portfolio/orders/{order_id}")
            return True
        except Exception as e:
            self.log.warning("OrderMonitor: failed to cancel %s: %s", order_id[:12], e)
            return False

    def get_pending_count(self):
        """Return number of orders still being tracked."""
        return len(self._pending)

    def get_pending_capital(self):
        """Return total cents locked in pending orders."""
        return sum(info["price"] * info["count"] for info in self._pending.values())


__all__ = ["OrderMonitor"]
