"""Execution Quality Analytics — Fill rate, slippage, and implementation shortfall.

Computes execution metrics from existing trade records (golden record format).
No new data collection needed — all fields already in trade logs.

Usage:
    from execution_quality import ExecutionAnalyzer

    ea = ExecutionAnalyzer()
    ea.load_trades(trades=all_trades)
    report = ea.json_report()
"""

import json
import logging
from pathlib import Path

from event_ledger import get_event_ledger
from pnl_attribution import _load_trades_safe

log = logging.getLogger("execution-quality")


class ExecutionAnalyzer:
    """Compute execution quality metrics from trade records."""

    def __init__(self, trade_file_paths=None, ledger_path=None):
        self._trade_file_paths = trade_file_paths
        self._ledger_path = ledger_path
        self._trades = []

    def load_trades(self, trades=None):
        """Load trades from files or accept pre-loaded list."""
        if trades is not None:
            self._trades = list(trades)
        elif self._ledger_path and self._trade_file_paths:
            ledger = get_event_ledger(path=self._ledger_path, logger=log)
            self._trades = []
            for tf in self._trade_file_paths:
                self._trades.extend(ledger.get_trade_records(tf["path"]))
        elif self._trade_file_paths:
            self._trades = []
            for tf in self._trade_file_paths:
                self._trades.extend(_load_trades_safe(tf["path"]))
        else:
            self._trades = []

    def _filtered(self, bot=None):
        """Filter trades by bot."""
        trades = self._trades
        if bot:
            trades = [t for t in trades if t.get("source_bot") == bot]
        return trades

    def fill_rate(self, bot=None):
        """Fraction of orders that were filled.

        Counts "filled" (or "resting" that became filled) vs total orders.
        """
        trades = self._filtered(bot)
        if not trades:
            return 0.0
        filled = sum(1 for t in trades
                     if (t.get("status") or "").lower() in ("filled", "complete"))
        return filled / len(trades)

    def average_slippage(self, bot=None):
        """Average slippage in cents: fill_price - limit_price.

        Only includes filled trades with both prices available.
        Positive = filled worse than limit. Returns 0.0 if no data.
        """
        trades = self._filtered(bot)
        slippages = []
        for t in trades:
            fill = t.get("fill_price_cents")
            limit = t.get("price_cents")
            if fill is not None and limit is not None:
                slippages.append(fill - limit)
        return sum(slippages) / len(slippages) if slippages else 0.0

    def implementation_shortfall(self, bot=None):
        """Average shortfall: fill_price - decision_price (best_ask for buys).

        Measures how much worse we did vs the market at decision time.
        Negative = we got a better price than the ask. Returns 0.0 if no data.
        """
        trades = self._filtered(bot)
        shortfalls = []
        for t in trades:
            fill = t.get("fill_price_cents")
            if fill is None:
                continue
            side = (t.get("side") or "").lower()
            if side == "yes":
                decision_price = t.get("best_ask")
            elif side == "no":
                decision_price = t.get("best_bid")
                if decision_price:
                    decision_price = 100 - decision_price
            else:
                continue
            if decision_price is not None:
                shortfalls.append(fill - decision_price)
        return sum(shortfalls) / len(shortfalls) if shortfalls else 0.0

    def fill_rate_by_bot(self):
        """Fill rate per bot."""
        bots = set(t.get("source_bot") for t in self._trades) - {None}
        return {bot: round(self.fill_rate(bot=bot), 4) for bot in sorted(bots)}

    def json_report(self):
        """Full execution quality report."""
        bots = set(t.get("source_bot") for t in self._trades) - {None}
        by_bot = {}
        for bot in sorted(bots):
            by_bot[bot] = {
                "fill_rate": round(self.fill_rate(bot=bot), 4),
                "avg_slippage_cents": round(self.average_slippage(bot=bot), 2),
                "impl_shortfall_cents": round(self.implementation_shortfall(bot=bot), 2),
                "trade_count": len(self._filtered(bot=bot)),
            }
        return {
            "fill_rate": round(self.fill_rate(), 4),
            "average_slippage_cents": round(self.average_slippage(), 2),
            "implementation_shortfall_cents": round(self.implementation_shortfall(), 2),
            "total_trades": len(self._trades),
            "by_bot": by_bot,
        }
