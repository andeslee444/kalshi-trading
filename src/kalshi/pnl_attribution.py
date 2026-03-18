"""P&L Attribution Engine — Decomposes realized P&L by multiple dimensions.

Reads settled trade records from all bot trade logs and computes P&L
attribution by: bot, edge bucket, regime, sizing method, and market type.

No kalshi_auth dependency — pure analytics module. Trade data is passed
in via constructor (for testing) or loaded from JSON file paths.

Usage:
    from pnl_attribution import PnLAttributor

    attr = PnLAttributor(trade_file_paths=[...])
    attr.load_trades()
    report = attr.full_report()
"""

import json
import logging
from collections import defaultdict
from pathlib import Path

from event_ledger import get_event_ledger

log = logging.getLogger("pnl-attribution")

# Default edge bucket boundaries (inclusive low, exclusive high)
DEFAULT_EDGE_BUCKETS = [
    (0.00, 0.04, "0-4%"),
    (0.04, 0.08, "4-8%"),
    (0.08, 0.15, "8-15%"),
    (0.15, 1.00, "15%+"),
]


def _load_trades_safe(filepath):
    """Load a JSON trade file. Returns list or empty list."""
    try:
        p = Path(filepath)
        if not p.exists():
            return []
        text = p.read_text().strip()
        if not text:
            return []
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError, OSError):
        return []


def _compute_pnl_cents(trade):
    """Compute realized P&L in cents for a settled trade.

    Returns (pnl_cents, is_settled) tuple.
    """
    settlement = trade.get("settlement_result")
    if settlement is None:
        return 0, False

    cost = trade.get("cost_cents", 0) or 0
    fill_count = trade.get("fill_count")
    if fill_count in (None, ""):
        fill_count = trade.get("count", 0) or 0
    else:
        try:
            fill_count = int(fill_count)
        except (TypeError, ValueError):
            fill_count = trade.get("count", 0) or 0
    revenue = trade.get("settlement_revenue_cents")

    if settlement in ("won", "yes", True, 1):
        return (100 * fill_count) - cost, True
    elif settlement in ("lost", "no", False, 0):
        return -cost, True
    if revenue is not None:
        return revenue - cost, True

    return 0, False


def _classify_market_type(ticker):
    """Classify a ticker into a market type category."""
    t = (ticker or "").upper()
    if t.startswith("KXHIGH"):
        return "weather"
    if t.startswith(("KXBTC", "KXETH", "KXSOL")):
        return "crypto"
    if t.startswith(("KXCPI", "KXCORECPI", "KXGDP", "KXJOBS", "KXPCE")):
        return "economics"
    if t.startswith(("KXALBUM", "KX1ALBUM")):
        return "entertainment"
    if t.startswith(("KXBOX", "KXMOVIE", "KXFILM")):
        return "box_office"
    return "other"


def _bucket_stats():
    """Return a fresh stats dict for accumulation."""
    return {"pnl_cents": 0, "trades": 0, "wins": 0, "losses": 0}


def _finalize_stats(group):
    """Add win_rate to each entry in a stats dict."""
    for stats in group.values():
        total = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(stats["wins"] / total, 4) if total > 0 else 0.0


class PnLAttributor:
    """P&L attribution engine — decomposes realized returns by multiple dimensions."""

    def __init__(self, trade_file_paths=None, regime_state_path=None, ledger_path=None):
        """
        Args:
            trade_file_paths: List of dicts with "path" (str/Path) and "bot" (str) keys.
            regime_state_path: Path to regime-state.json for regime attribution.
        """
        self._trade_file_paths = trade_file_paths
        self._regime_state_path = regime_state_path
        self._ledger_path = ledger_path
        self._trades = []       # Settled trades only (with _pnl_cents)
        self._all_trades = []   # All trades including unsettled

    def load_trades(self, trades=None):
        """Load and filter to settled trades.

        Args:
            trades: Optional pre-loaded list of trade dicts (for testing).
        """
        if trades is not None:
            self._all_trades = list(trades)
        elif self._ledger_path and self._trade_file_paths:
            ledger = get_event_ledger(path=self._ledger_path, logger=log)
            self._all_trades = []
            for tf in self._trade_file_paths:
                trades_for_path = ledger.get_trade_records(tf["path"])
                for t in trades_for_path:
                    if not t.get("source_bot"):
                        t["source_bot"] = tf.get("bot", "unknown")
                    self._all_trades.append(t)
        elif self._trade_file_paths:
            self._all_trades = []
            for tf in self._trade_file_paths:
                path = Path(tf["path"])
                bot_trades = _load_trades_safe(path)
                for t in bot_trades:
                    if not t.get("source_bot"):
                        t["source_bot"] = tf.get("bot", "unknown")
                    self._all_trades.append(t)
        else:
            self._all_trades = []

        # Filter to settled trades (copy dicts to avoid mutating caller's data)
        self._trades = []
        for t in self._all_trades:
            pnl, is_settled = _compute_pnl_cents(t)
            if is_settled:
                settled = dict(t)
                settled["_pnl_cents"] = pnl
                self._trades.append(settled)

    def _accumulate(self, key_fn):
        """Generic accumulation by a key function."""
        groups = defaultdict(_bucket_stats)
        for t in self._trades:
            key = key_fn(t)
            pnl = t.get("_pnl_cents", 0)
            groups[key]["pnl_cents"] += pnl
            groups[key]["trades"] += 1
            if pnl > 0:
                groups[key]["wins"] += 1
            elif pnl < 0:
                groups[key]["losses"] += 1
        result = dict(groups)
        _finalize_stats(result)
        return result

    def attribute_by_bot(self):
        """P&L decomposition by source bot."""
        return self._accumulate(lambda t: t.get("source_bot", "unknown"))

    def attribute_by_edge_bucket(self, buckets=None):
        """P&L decomposition by edge at entry."""
        buckets = buckets or DEFAULT_EDGE_BUCKETS

        def key_fn(t):
            edge = t.get("raw_edge")
            if edge is not None:
                abs_edge = abs(edge)
                for low, high, label in buckets:
                    if low <= abs_edge < high:
                        return label
            return "unknown"

        result = self._accumulate(key_fn)

        # Add avg_edge to each bucket
        edge_sums = defaultdict(float)
        for t in self._trades:
            key = key_fn(t)
            edge = t.get("raw_edge")
            if edge is not None:
                edge_sums[key] += abs(edge)
        for label, stats in result.items():
            stats["avg_edge"] = round(edge_sums[label] / stats["trades"], 4) if stats["trades"] > 0 else 0.0

        return {k: v for k, v in result.items() if v["trades"] > 0}

    def attribute_by_regime(self, regime_history=None):
        """P&L decomposition by volatility regime at trade time."""
        if regime_history is None:
            regime_history = self._load_regime_history()

        def key_fn(t):
            return self._match_regime(t.get("timestamp", ""), regime_history)

        return self._accumulate(key_fn)

    def attribute_by_sizing(self):
        """P&L decomposition by sizing method."""
        return self._accumulate(lambda t: t.get("sizing_method", "unknown") or "unknown")

    def attribute_by_market_type(self):
        """P&L decomposition by market type (weather, crypto, etc.)."""
        return self._accumulate(lambda t: _classify_market_type(t.get("ticker", "")))

    def full_report(self):
        """Generate complete attribution report with all dimensions."""
        return {
            "by_bot": self.attribute_by_bot(),
            "by_edge_bucket": self.attribute_by_edge_bucket(),
            "by_regime": self.attribute_by_regime(),
            "by_sizing": self.attribute_by_sizing(),
            "by_market_type": self.attribute_by_market_type(),
            "summary": {
                "total_trades_settled": len(self._trades),
                "total_trades_all": len(self._all_trades),
                "total_pnl_cents": sum(t.get("_pnl_cents", 0) for t in self._trades),
            },
        }

    def _load_regime_history(self):
        """Load regime history from regime-state.json."""
        if not self._regime_state_path:
            return []
        try:
            with open(self._regime_state_path) as f:
                data = json.load(f)
            return data.get("history", [])
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    @staticmethod
    def _match_regime(trade_timestamp, regime_history):
        """Find the regime active at trade_timestamp.

        Uses the most recent observation before or at the trade time.
        """
        if not regime_history or not trade_timestamp:
            return "unknown"
        best = "unknown"
        for ts, _vol, regime in regime_history:
            if ts <= trade_timestamp:
                best = regime
            else:
                break
        return best
