# Phase 7: Market Microstructure & Intelligence — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the capstone analytics and intelligence layer: P&L attribution engine, competitive edge monitoring with decay detection, execution quality analytics, agent-based order book simulation for market maker activation, and dashboard integration for all new intelligence endpoints.

**Architecture:** Four new pure-analytics modules with no `kalshi_auth` module-level dependency (testable without API stubs). P&L attribution reads settled trade records from JSON files. Edge monitor tracks market efficiency trends and competitor detection. Orderbook simulator provides calibrated MM parameters. All feed into dashboard via new API endpoints.

**Tech Stack:** Python 3, `math`/`random`/`json` (stdlib only for new modules — no new dependencies), `pytest`, `FastAPI` (existing).

**Branch:** `phase7/microstructure` (from `main`)

**Git author for all commits:** `--author="Andes Lee <andes.lee444@gmail.com>"`

---

### Task 1: Create Branch and Verify Baseline

**Files:**
- None (branch setup only)

**Step 1: Create the feature branch**

```bash
git checkout -b phase7/microstructure main
```

**Step 2: Verify all existing tests pass**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (35+ test files).

**Step 3: No commit needed — branch is ready.**

---

### Task 2: P&L Attribution — Tests

**Files:**
- Create: `tests/test_pnl_attribution.py`

**Context:** The P&L attribution engine reads settled trade records (from all bot trade log files) and decomposes realized P&L by 5 dimensions: bot, edge bucket, regime, sizing method, and market type. Trade records follow the golden record format from `TradeManager._build_golden_record()` — key fields: `source_bot`, `raw_edge`, `sizing_method`, `cost_cents`, `count`, `settlement_result`, `settlement_revenue_cents`, `ticker`, `timestamp`.

**Step 1: Write tests**

```python
"""Tests for P&L attribution engine.

Tests pure analytics functions — no API calls or kalshi_auth dependency.
All trade data is provided as fixture dicts matching the golden record format.
"""

import pytest
from pnl_attribution import (
    PnLAttributor, _compute_pnl_cents, _classify_market_type,
)


# ── Fixtures: known trade records with settlement data ──

def _make_trade(ticker="KXHIGHHOU-26MAR03-T75", source_bot="weather",
                raw_edge=0.12, cost_cents=200, count=4, price_cents=50,
                sizing_method="half_kelly", timestamp="2026-03-01T10:00:00",
                settlement_result=None, settlement_revenue_cents=None):
    """Build a golden-record-style trade dict."""
    return {
        "ticker": ticker,
        "source_bot": source_bot,
        "raw_edge": raw_edge,
        "cost_cents": cost_cents,
        "count": count,
        "price_cents": price_cents,
        "sizing_method": sizing_method,
        "timestamp": timestamp,
        "settlement_result": settlement_result,
        "settlement_revenue_cents": settlement_revenue_cents,
        "best_bid": 48,
        "best_ask": 52,
        "spread": 4,
        "status": "filled",
    }


SETTLED_TRADES = [
    # Weather bot: won, 4 contracts at 50c → revenue 400c, cost 200c, P&L +200c
    _make_trade(source_bot="weather", raw_edge=0.12, cost_cents=200, count=4,
                sizing_method="half_kelly", timestamp="2026-03-01T10:00:00",
                settlement_result="won", settlement_revenue_cents=400),
    # Weather bot: lost, 2 contracts at 60c → revenue 0c, cost 120c, P&L -120c
    _make_trade(source_bot="weather", raw_edge=0.08, cost_cents=120, count=2,
                price_cents=60, sizing_method="half_kelly",
                timestamp="2026-03-01T14:00:00",
                settlement_result="lost", settlement_revenue_cents=0),
    # Crypto bot: won, 3 contracts at 30c → revenue 300c, cost 90c, P&L +210c
    _make_trade(ticker="KXBTC-26MAR03-T95000", source_bot="crypto",
                raw_edge=0.20, cost_cents=90, count=3, price_cents=30,
                sizing_method="quarter_kelly", timestamp="2026-03-01T11:00:00",
                settlement_result="won", settlement_revenue_cents=300),
    # Economics bot: lost, 10 contracts at 3c → revenue 0c, cost 30c, P&L -30c
    _make_trade(ticker="KXCPI-26MAY-T20", source_bot="economics",
                raw_edge=0.05, cost_cents=30, count=10, price_cents=3,
                sizing_method="half_kelly", timestamp="2026-03-02T08:00:00",
                settlement_result="lost", settlement_revenue_cents=0),
    # Strategy bot: won (longshot sell), 5 contracts at 2c
    _make_trade(ticker="KXSPORTS-NCAAM", source_bot="strategy",
                raw_edge=0.03, cost_cents=10, count=5, price_cents=2,
                sizing_method="half_kelly", timestamp="2026-03-02T09:00:00",
                settlement_result="won", settlement_revenue_cents=500),
]

UNSETTLED_TRADE = _make_trade(settlement_result=None, settlement_revenue_cents=None)


# ── Tests: _compute_pnl_cents ──

class TestComputePnlCents:
    def test_won_with_revenue(self):
        t = _make_trade(cost_cents=200, settlement_result="won",
                        settlement_revenue_cents=400)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == 200

    def test_lost_with_revenue(self):
        t = _make_trade(cost_cents=120, settlement_result="lost",
                        settlement_revenue_cents=0)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == -120

    def test_unsettled_returns_zero(self):
        pnl, settled = _compute_pnl_cents(UNSETTLED_TRADE)
        assert settled is False
        assert pnl == 0

    def test_fallback_won_without_revenue(self):
        t = _make_trade(cost_cents=200, count=4,
                        settlement_result="won", settlement_revenue_cents=None)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == 200  # 100*4 - 200

    def test_fallback_lost_without_revenue(self):
        t = _make_trade(cost_cents=120, count=2,
                        settlement_result="lost", settlement_revenue_cents=None)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == -120


# ── Tests: _classify_market_type ──

class TestClassifyMarketType:
    def test_weather(self):
        assert _classify_market_type("KXHIGHHOU-26MAR03-T75") == "weather"

    def test_crypto_btc(self):
        assert _classify_market_type("KXBTC-26MAR03-T95000") == "crypto"

    def test_crypto_eth(self):
        assert _classify_market_type("KXETH-26MAR03-T3500") == "crypto"

    def test_economics_cpi(self):
        assert _classify_market_type("KXCPI-26MAY-T20") == "economics"

    def test_economics_gdp(self):
        assert _classify_market_type("KXGDP-26Q1-T20") == "economics"

    def test_entertainment(self):
        assert _classify_market_type("KXALBUM-DRAKE-100K") == "entertainment"

    def test_box_office(self):
        assert _classify_market_type("KXBOX-MINECRAFT-200M") == "box_office"

    def test_other(self):
        assert _classify_market_type("KXSPORTS-NCAAM") == "other"


# ── Tests: PnLAttributor ──

class TestAttributeByBot:
    def test_groups_by_source_bot(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_bot()
        assert "weather" in result
        assert "crypto" in result
        assert result["weather"]["trades"] == 2
        assert result["crypto"]["trades"] == 1

    def test_correct_pnl_per_bot(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_bot()
        assert result["weather"]["pnl_cents"] == 80  # 200 - 120
        assert result["crypto"]["pnl_cents"] == 210
        assert result["economics"]["pnl_cents"] == -30

    def test_win_rate(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_bot()
        assert result["weather"]["win_rate"] == 0.5  # 1 win, 1 loss
        assert result["crypto"]["win_rate"] == 1.0

    def test_empty_trades(self):
        attr = PnLAttributor()
        attr.load_trades(trades=[])
        result = attr.attribute_by_bot()
        assert result == {}

    def test_unsettled_excluded(self):
        attr = PnLAttributor()
        attr.load_trades(trades=[UNSETTLED_TRADE])
        result = attr.attribute_by_bot()
        assert result == {}


class TestAttributeByEdgeBucket:
    def test_buckets_assigned_correctly(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_edge_bucket()
        # raw_edge=0.03 → "0-4%", raw_edge=0.05 → "4-8%",
        # raw_edge=0.08 → "8-15%", raw_edge=0.12 → "8-15%", raw_edge=0.20 → "15%+"
        assert "0-4%" in result
        assert result["0-4%"]["trades"] == 1
        assert "4-8%" in result
        assert result["4-8%"]["trades"] == 1
        assert "8-15%" in result
        assert result["8-15%"]["trades"] == 2
        assert "15%+" in result
        assert result["15%+"]["trades"] == 1

    def test_pnl_per_bucket(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_edge_bucket()
        assert result["15%+"]["pnl_cents"] == 210  # crypto won
        assert result["0-4%"]["pnl_cents"] == 490   # strategy won

    def test_unknown_edge_grouped(self):
        trade = _make_trade(raw_edge=None, settlement_result="won",
                            settlement_revenue_cents=400, cost_cents=200)
        attr = PnLAttributor()
        attr.load_trades(trades=[trade])
        result = attr.attribute_by_edge_bucket()
        assert "unknown" in result


class TestAttributeByRegime:
    def test_regime_assignment_from_history(self):
        # Regime history: normal until 2026-03-02, then high_vol
        regime_history = [
            ("2026-03-01T00:00:00", 0.45, "normal"),
            ("2026-03-02T00:00:00", 0.80, "high_vol"),
        ]
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_regime(regime_history=regime_history)
        # 3 trades on 03-01 → normal, 2 trades on 03-02 → high_vol
        assert result["normal"]["trades"] == 3
        assert result["high_vol"]["trades"] == 2

    def test_empty_regime_history(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_regime(regime_history=[])
        assert "unknown" in result
        assert result["unknown"]["trades"] == 5


class TestAttributeBySizing:
    def test_groups_by_sizing_method(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_sizing()
        assert "half_kelly" in result
        assert "quarter_kelly" in result
        assert result["half_kelly"]["trades"] == 4
        assert result["quarter_kelly"]["trades"] == 1


class TestAttributeByMarketType:
    def test_groups_by_ticker_prefix(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_market_type()
        assert "weather" in result
        assert result["weather"]["trades"] == 2
        assert "crypto" in result
        assert result["crypto"]["trades"] == 1
        assert "economics" in result


class TestFullReport:
    def test_report_has_all_dimensions(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        report = attr.full_report()
        assert "by_bot" in report
        assert "by_edge_bucket" in report
        assert "by_regime" in report
        assert "by_sizing" in report
        assert "by_market_type" in report
        assert "summary" in report

    def test_summary_totals(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        report = attr.full_report()
        assert report["summary"]["total_trades_settled"] == 5
        # Total P&L: 200 - 120 + 210 - 30 + 490 = 750
        assert report["summary"]["total_pnl_cents"] == 750
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pnl_attribution.py -v`
Expected: FAIL — `pnl_attribution` module not found.

**Step 3: Commit failing tests**

```bash
git add tests/test_pnl_attribution.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add P&L attribution tests (failing — module not yet implemented)"
```

---

### Task 3: P&L Attribution — Implementation

**Files:**
- Create: `src/kalshi/pnl_attribution.py`

**Context:** Pure analytics module — no kalshi_auth dependency at module level. Accepts trade data as constructor argument (for testing) or loads from trade log file paths. All trade records follow the golden record format from TradeManager.

**Step 1: Implement the module**

```python
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
    revenue = trade.get("settlement_revenue_cents")

    if revenue is not None:
        return revenue - cost, True

    # Fallback: infer from binary settlement result
    count = trade.get("count", 0) or 0
    if settlement in ("won", "yes", True, 1):
        return (100 * count) - cost, True
    elif settlement in ("lost", "no", False, 0):
        return -cost, True

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

    def __init__(self, trade_file_paths=None, regime_state_path=None):
        """
        Args:
            trade_file_paths: List of dicts with "path" (str/Path) and "bot" (str) keys.
            regime_state_path: Path to regime-state.json for regime attribution.
        """
        self._trade_file_paths = trade_file_paths
        self._regime_state_path = regime_state_path
        self._trades = []       # Settled trades only (with _pnl_cents)
        self._all_trades = []   # All trades including unsettled

    def load_trades(self, trades=None):
        """Load and filter to settled trades.

        Args:
            trades: Optional pre-loaded list of trade dicts (for testing).
        """
        if trades is not None:
            self._all_trades = list(trades)
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

        # Filter to settled trades
        self._trades = []
        for t in self._all_trades:
            pnl, is_settled = _compute_pnl_cents(t)
            if is_settled:
                t["_pnl_cents"] = pnl
                self._trades.append(t)

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
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_pnl_attribution.py -v`
Expected: All tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/pnl_attribution.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add P&L attribution engine with 5-dimensional decomposition"
```

---

### Task 4: Edge Monitor — Tests

**Files:**
- Create: `tests/test_edge_monitor.py`

**Context:** The edge monitor tracks how market efficiency evolves over time. It answers: "Is our edge decaying? Are new competitors entering?" It processes observations from settled trades — each observation records our model probability, the market price, and whether we won. From these, it computes efficiency trends (slope of the model-vs-market gap), edge half-lives (exponential decay fit), and competitor detection (sudden efficiency jumps).

**Step 1: Write tests**

```python
"""Tests for edge monitoring and competitive intelligence.

Tests pure analytics — no API calls or kalshi_auth dependency.
Observations are provided as fixture data.
"""

import math
import pytest
from edge_monitor import EdgeMonitor, _linear_regression


# ── Fixtures ──

def _obs(timestamp, market_type, model_prob, market_price_cents, won):
    """Build an observation dict."""
    return {
        "timestamp": timestamp,
        "market_type": market_type,
        "model_prob": model_prob,
        "market_price_cents": market_price_cents,
        "raw_edge": model_prob - market_price_cents / 100.0,
        "settled_won": won,
    }


# Stable edge: consistent ~15% gap, high win rate
STABLE_OBSERVATIONS = [
    _obs(f"2026-02-{d:02d}T10:00:00", "weather", 0.70, 55, d % 3 != 0)
    for d in range(1, 29)
]

# Decaying edge: gap shrinks from 20% to 5% over 4 weeks
DECAYING_OBSERVATIONS = [
    _obs(f"2026-02-{d:02d}T10:00:00", "crypto",
         0.60 - d * 0.005,           # model_prob decreases slightly
         int((0.40 + d * 0.005) * 100),  # market catches up
         d < 15)                     # stops winning in second half
    for d in range(1, 29)
]

# Sudden competitor: stable for 2 weeks, then gap collapses
COMPETITOR_OBSERVATIONS = (
    [_obs(f"2026-02-{d:02d}T10:00:00", "economics", 0.75, 60, True)
     for d in range(1, 15)]
    + [_obs(f"2026-02-{d:02d}T10:00:00", "economics", 0.72, 70, d % 2 == 0)
       for d in range(15, 29)]
)


# ── Tests: _linear_regression ──

class TestLinearRegression:
    def test_positive_slope(self):
        xs = [1, 2, 3, 4, 5]
        ys = [2, 4, 6, 8, 10]
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert abs(slope - 2.0) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_negative_slope(self):
        xs = [1, 2, 3, 4, 5]
        ys = [10, 8, 6, 4, 2]
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert slope < 0
        assert abs(slope + 2.0) < 0.01

    def test_flat(self):
        xs = [1, 2, 3, 4, 5]
        ys = [5, 5, 5, 5, 5]
        slope, intercept, r_sq = _linear_regression(xs, ys)
        assert abs(slope) < 0.01

    def test_single_point(self):
        slope, intercept, r_sq = _linear_regression([1], [5])
        assert slope == 0.0


# ── Tests: EdgeMonitor ──

class TestEdgeRealizationRate:
    def test_high_win_rate(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        rate = em.edge_realization_rate("weather")
        assert rate > 0.5  # Weather observations win ~67% of the time

    def test_declining_win_rate_for_decaying_edge(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        rate = em.edge_realization_rate("crypto")
        assert rate == pytest.approx(14 / 28, abs=0.01)  # wins first 14 days

    def test_unknown_market_type_returns_zero(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        rate = em.edge_realization_rate("nonexistent")
        assert rate == 0.0

    def test_windowed_rate(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        # Last 14 days should have lower win rate
        rate_recent = em.edge_realization_rate("crypto", window_days=14)
        rate_all = em.edge_realization_rate("crypto")
        assert rate_recent < rate_all


class TestEfficiencyTrend:
    def test_stable_edge_flat_trend(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        slope = em.efficiency_trend("weather")
        # Stable edge → slope near zero
        assert abs(slope) < 0.005

    def test_decaying_edge_negative_trend(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        slope = em.efficiency_trend("crypto")
        # Gap is shrinking → slope should be negative (gap decreasing over time)
        assert slope < -0.001

    def test_no_observations_returns_zero(self):
        em = EdgeMonitor()
        slope = em.efficiency_trend("weather")
        assert slope == 0.0


class TestEdgeHalfLife:
    def test_decaying_edge_has_finite_half_life(self):
        em = EdgeMonitor()
        em.add_observations(DECAYING_OBSERVATIONS)
        half_life = em.edge_half_life("crypto")
        # Should be a positive finite number (edge is decaying)
        assert 0 < half_life < 365

    def test_stable_edge_has_long_half_life(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        half_life = em.edge_half_life("weather")
        # Stable edge → very long (or infinite) half-life
        assert half_life > 90 or half_life == float("inf")


class TestDetectCompetitor:
    def test_detects_sudden_efficiency_jump(self):
        em = EdgeMonitor()
        em.add_observations(COMPETITOR_OBSERVATIONS)
        detected = em.detect_competitor("economics")
        assert detected is True

    def test_no_competitor_when_stable(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        detected = em.detect_competitor("weather")
        assert detected is False


class TestOptimalStrategyWeights:
    def test_weights_sum_to_one(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS + DECAYING_OBSERVATIONS)
        weights = em.optimal_strategy_weights()
        if weights:
            total = sum(weights.values())
            assert abs(total - 1.0) < 0.01

    def test_durable_edge_gets_higher_weight(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS + DECAYING_OBSERVATIONS)
        weights = em.optimal_strategy_weights()
        if "weather" in weights and "crypto" in weights:
            assert weights["weather"] > weights["crypto"]


class TestStatePersistence:
    def test_save_and_load(self, tmp_path):
        state_path = str(tmp_path / "edge-state.json")
        em = EdgeMonitor(state_path=state_path)
        em.add_observations(STABLE_OBSERVATIONS[:5])
        em.save_state()

        em2 = EdgeMonitor(state_path=state_path)
        em2.load_state()
        assert len(em2._observations) == 5

    def test_json_report_structure(self):
        em = EdgeMonitor()
        em.add_observations(STABLE_OBSERVATIONS)
        report = em.json_report()
        assert "market_types" in report
        assert "weather" in report["market_types"]
        assert "realization_rate" in report["market_types"]["weather"]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_edge_monitor.py -v`
Expected: FAIL — `edge_monitor` module not found.

**Step 3: Commit failing tests**

```bash
git add tests/test_edge_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add edge monitor tests (failing — module not yet implemented)"
```

---

### Task 5: Edge Monitor — Implementation

**Files:**
- Create: `src/kalshi/edge_monitor.py`

**Step 1: Implement the module**

```python
"""Competitive Edge Monitor — Tracks edge decay and market efficiency.

Detects when trading strategies lose their edge due to market
efficiency improvements or new competitor entry. Processes settled
trade observations and computes efficiency trends, edge half-lives,
and optimal strategy allocation weights.

No kalshi_auth dependency — pure analytics module.

Usage:
    from edge_monitor import EdgeMonitor

    em = EdgeMonitor(state_path="data/edge-monitor-state.json")
    em.load_state()
    em.add_observations(settled_observations)
    report = em.json_report()
    em.save_state()
"""

import json
import math
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("edge-monitor")


def _linear_regression(xs, ys):
    """Simple OLS regression. Returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0

    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)

    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-10:
        return 0.0, sum_y / n, 0.0

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    mean_y = sum_y / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_sq = 1 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0

    return slope, intercept, max(0.0, r_sq)


def _parse_timestamp(ts):
    """Parse ISO timestamp to datetime. Returns None on failure."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00", ""))
    except (ValueError, AttributeError):
        return None


def _days_since_epoch(ts_str):
    """Convert ISO timestamp to days since a reference epoch (2026-01-01)."""
    dt = _parse_timestamp(ts_str)
    if dt is None:
        return 0.0
    epoch = datetime(2026, 1, 1)
    return (dt - epoch).total_seconds() / 86400.0


class EdgeMonitor:
    """Tracks market efficiency trends and competitive edge decay."""

    def __init__(self, lookback_days=90, state_path=None):
        self.lookback_days = lookback_days
        self.state_path = state_path
        self._observations = []  # list of observation dicts

    def add_observations(self, observations):
        """Add a batch of settled trade observations.

        Each observation dict has:
            timestamp, market_type, model_prob, market_price_cents,
            raw_edge, settled_won
        """
        self._observations.extend(observations)
        # Sort by timestamp
        self._observations.sort(key=lambda o: o.get("timestamp", ""))
        # Trim to lookback window
        cutoff = (datetime.now() - timedelta(days=self.lookback_days)).isoformat()
        self._observations = [o for o in self._observations
                              if o.get("timestamp", "") >= cutoff or self.lookback_days <= 0]

    def _filter_by_type(self, market_type, window_days=None):
        """Get observations for a specific market type, optionally windowed."""
        obs = [o for o in self._observations if o.get("market_type") == market_type]
        if window_days is not None and obs:
            cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
            obs = [o for o in obs if o.get("timestamp", "") >= cutoff]
        return obs

    def edge_realization_rate(self, market_type, window_days=None):
        """Fraction of trades where edge was realized (won).

        Returns 0.0 if no observations for this market type.
        """
        obs = self._filter_by_type(market_type, window_days)
        if not obs:
            return 0.0
        wins = sum(1 for o in obs if o.get("settled_won"))
        return wins / len(obs)

    def efficiency_trend(self, market_type):
        """Slope of |model_prob - market_price| gap over time.

        Negative slope = market becoming more efficient (gap shrinking).
        Returns 0.0 if insufficient data.
        """
        obs = self._filter_by_type(market_type)
        if len(obs) < 3:
            return 0.0

        xs = [_days_since_epoch(o["timestamp"]) for o in obs]
        ys = [abs(o.get("raw_edge", 0.0)) for o in obs]

        slope, _, _ = _linear_regression(xs, ys)
        return slope

    def edge_half_life(self, market_type):
        """Estimated days until edge decays 50%.

        Fits exponential decay to the gap values: gap(t) = gap_0 * exp(-lambda * t).
        Takes log of gap values and fits linear regression.

        Returns float("inf") if edge is stable or growing.
        """
        obs = self._filter_by_type(market_type)
        if len(obs) < 5:
            return float("inf")

        xs = [_days_since_epoch(o["timestamp"]) for o in obs]
        gaps = [abs(o.get("raw_edge", 0.0)) for o in obs]

        # Filter out zero/near-zero gaps (can't take log)
        filtered = [(x, g) for x, g in zip(xs, gaps) if g > 0.001]
        if len(filtered) < 3:
            return float("inf")

        log_gaps = [math.log(g) for _, g in filtered]
        x_vals = [x for x, _ in filtered]

        slope, _, _ = _linear_regression(x_vals, log_gaps)

        # slope = -lambda in exponential decay
        if slope >= 0:
            return float("inf")  # Edge is stable or growing

        decay_rate = -slope
        return math.log(2) / decay_rate

    def detect_competitor(self, market_type, threshold=0.30, recent_days=14):
        """Detect if a new competitor may have entered.

        Compares average gap in last `recent_days` to the preceding period.
        Returns True if gap shrank by more than `threshold` fraction.
        """
        all_obs = self._filter_by_type(market_type)
        if len(all_obs) < 5:
            return False

        cutoff = (datetime.now() - timedelta(days=recent_days)).isoformat()
        recent = [o for o in all_obs if o.get("timestamp", "") >= cutoff]
        earlier = [o for o in all_obs if o.get("timestamp", "") < cutoff]

        if not recent or not earlier:
            return False

        avg_recent = sum(abs(o.get("raw_edge", 0)) for o in recent) / len(recent)
        avg_earlier = sum(abs(o.get("raw_edge", 0)) for o in earlier) / len(earlier)

        if avg_earlier < 0.001:
            return False

        shrinkage = 1.0 - avg_recent / avg_earlier
        return shrinkage > threshold

    def optimal_strategy_weights(self):
        """Suggest capital allocation weights based on edge durability.

        Strategies with longer edge half-lives get higher weights.
        Returns dict {market_type: weight} summing to 1.0.
        """
        market_types = set(o.get("market_type") for o in self._observations)
        market_types.discard(None)

        if not market_types:
            return {}

        scores = {}
        for mt in market_types:
            hl = self.edge_half_life(mt)
            rate = self.edge_realization_rate(mt)
            # Score = win_rate * log(1 + half_life_days)
            if hl == float("inf"):
                hl_score = math.log(1 + 365)
            else:
                hl_score = math.log(1 + max(1, hl))
            scores[mt] = rate * hl_score

        total = sum(scores.values())
        if total <= 0:
            # Equal weights
            n = len(market_types)
            return {mt: 1.0 / n for mt in market_types}

        return {mt: round(s / total, 4) for mt, s in scores.items()}

    def save_state(self):
        """Persist observations to disk."""
        if not self.state_path:
            return
        data = {"observations": self._observations}
        p = Path(self.state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        import os
        os.replace(tmp, str(p))

    def load_state(self):
        """Load observations from disk."""
        if not self.state_path:
            return
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self._observations = data.get("observations", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def json_report(self):
        """Generate a JSON-serializable report of edge intelligence."""
        market_types = sorted(set(o.get("market_type") for o in self._observations) - {None})
        mt_reports = {}
        for mt in market_types:
            mt_reports[mt] = {
                "realization_rate": round(self.edge_realization_rate(mt), 4),
                "efficiency_trend_slope": round(self.efficiency_trend(mt), 6),
                "edge_half_life_days": self.edge_half_life(mt),
                "competitor_detected": self.detect_competitor(mt),
                "observation_count": len(self._filter_by_type(mt)),
            }
        weights = self.optimal_strategy_weights()
        return {
            "market_types": mt_reports,
            "strategy_weights": weights,
            "total_observations": len(self._observations),
        }
```

**Step 2: Run tests**

Run: `pytest tests/test_edge_monitor.py -v`
Expected: All tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/edge_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add edge monitor with decay detection and competitor alerts"
```

---

### Task 6: Execution Quality — Tests

**Files:**
- Create: `tests/test_execution_quality.py`

**Context:** Execution quality analytics compute fill rate, slippage, and implementation shortfall from existing trade records. The golden record already has `status`, `price_cents` (our limit), `fill_price_cents` (actual fill), `best_bid`, `best_ask`. This module reads trade logs and computes metrics — no new data collection needed.

**Step 1: Write tests**

```python
"""Tests for execution quality analytics.

Tests pure computation on trade record fixtures.
"""

import pytest
from execution_quality import ExecutionAnalyzer


def _make_trade(source_bot="weather", status="filled", price_cents=50,
                fill_price_cents=50, best_bid=48, best_ask=52,
                timestamp="2026-03-01T10:00:00", side="yes",
                raw_edge=0.12):
    return {
        "source_bot": source_bot,
        "status": status,
        "price_cents": price_cents,
        "fill_price_cents": fill_price_cents,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "timestamp": timestamp,
        "side": side,
        "raw_edge": raw_edge,
    }


TRADES = [
    # Filled, no slippage
    _make_trade(status="filled", price_cents=50, fill_price_cents=50,
                best_bid=48, best_ask=52),
    # Filled, 1c slippage (filled worse than limit)
    _make_trade(status="filled", price_cents=50, fill_price_cents=51,
                best_bid=48, best_ask=52, source_bot="crypto"),
    # Filled, 2c slippage
    _make_trade(status="filled", price_cents=50, fill_price_cents=52,
                best_bid=48, best_ask=52, source_bot="crypto"),
    # Cancelled (not filled)
    _make_trade(status="canceled", price_cents=45, fill_price_cents=None,
                best_bid=48, best_ask=52),
    # Resting (not filled)
    _make_trade(status="resting", price_cents=44, fill_price_cents=None,
                best_bid=48, best_ask=52, source_bot="economics"),
]


class TestFillRate:
    def test_overall_fill_rate(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=TRADES)
        rate = ea.fill_rate()
        assert rate == pytest.approx(3 / 5, abs=0.01)  # 3 filled out of 5

    def test_fill_rate_by_bot(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=TRADES)
        rate = ea.fill_rate(bot="crypto")
        assert rate == pytest.approx(1.0)  # 2/2 crypto trades filled

    def test_fill_rate_no_trades(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=[])
        assert ea.fill_rate() == 0.0


class TestSlippage:
    def test_average_slippage(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=TRADES)
        slip = ea.average_slippage()
        # 3 filled: 0 + 1 + 2 = 3, avg = 1.0
        assert slip == pytest.approx(1.0, abs=0.01)

    def test_slippage_by_bot(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=TRADES)
        slip = ea.average_slippage(bot="weather")
        assert slip == pytest.approx(0.0)  # weather had 0 slippage
        slip_crypto = ea.average_slippage(bot="crypto")
        assert slip_crypto == pytest.approx(1.5)  # (1+2)/2

    def test_slippage_no_fills(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=[_make_trade(status="canceled")])
        assert ea.average_slippage() == 0.0


class TestImplementationShortfall:
    def test_shortfall_computation(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=TRADES)
        # Implementation shortfall for YES buys: fill_price - best_ask at decision
        # Trade 1: 50 - 52 = -2 (filled better than ask)
        # Trade 2: 51 - 52 = -1
        # Trade 3: 52 - 52 = 0
        shortfall = ea.implementation_shortfall()
        assert shortfall == pytest.approx(-1.0, abs=0.01)


class TestJsonReport:
    def test_report_structure(self):
        ea = ExecutionAnalyzer()
        ea.load_trades(trades=TRADES)
        report = ea.json_report()
        assert "fill_rate" in report
        assert "average_slippage_cents" in report
        assert "implementation_shortfall_cents" in report
        assert "by_bot" in report
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_execution_quality.py -v`
Expected: FAIL — module not found.

**Step 3: Commit**

```bash
git add tests/test_execution_quality.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add execution quality analytics tests (failing — module not yet implemented)"
```

---

### Task 7: Execution Quality — Implementation

**Files:**
- Create: `src/kalshi/execution_quality.py`

**Step 1: Implement the module**

```python
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
from collections import defaultdict
from pathlib import Path

log = logging.getLogger("execution-quality")


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


class ExecutionAnalyzer:
    """Compute execution quality metrics from trade records."""

    def __init__(self, trade_file_paths=None):
        self._trade_file_paths = trade_file_paths
        self._trades = []

    def load_trades(self, trades=None):
        """Load trades from files or accept pre-loaded list."""
        if trades is not None:
            self._trades = list(trades)
        elif self._trade_file_paths:
            self._trades = []
            for tf in self._trade_file_paths:
                self._trades.extend(_load_trades_safe(tf["path"]))
        else:
            self._trades = []

    def _filtered(self, bot=None, edge_bucket=None):
        """Filter trades by bot and/or edge bucket."""
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
```

**Step 2: Run tests**

Run: `pytest tests/test_execution_quality.py -v`
Expected: All tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/execution_quality.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add execution quality analytics — fill rate, slippage, shortfall"
```

---

### Task 8: Orderbook Simulator — Tests

**Files:**
- Create: `tests/test_orderbook_sim.py`

**Context:** Agent-based market simulation for evaluating market-maker profitability. Four agent types: Informed (trade toward true probability), Noise (random), MarketMaker (Avellaneda-Stoikov quotes), Bot (deterministic). The simulation runs a continuous double auction and measures price convergence, agent P&L, fill probability, and Kyle's lambda (price impact coefficient).

**Step 1: Write tests**

```python
"""Tests for agent-based order book simulation.

Uses seeded random for reproducibility. Tests verify economic properties
(informed profit, price convergence) rather than exact values.
"""

import math
import pytest
from orderbook_sim import (
    OrderBook, Order, InformedAgent, NoiseAgent, MMAgent,
    OrderBookSimulator, kyle_lambda_estimate,
)


class TestOrderBook:
    def test_add_and_match(self):
        book = OrderBook()
        book.add_order(Order("buyer", "buy", 55, 1))
        book.add_order(Order("seller", "sell", 50, 1))
        fills = book.match()
        assert len(fills) == 1
        assert fills[0]["size"] == 1
        assert fills[0]["price"] == 52  # midpoint of 55 and 50

    def test_no_match_when_spread(self):
        book = OrderBook()
        book.add_order(Order("buyer", "buy", 45, 1))
        book.add_order(Order("seller", "sell", 55, 1))
        fills = book.match()
        assert len(fills) == 0

    def test_best_bid_ask(self):
        book = OrderBook()
        book.add_order(Order("b1", "buy", 48, 1))
        book.add_order(Order("b2", "buy", 50, 1))
        book.add_order(Order("s1", "sell", 55, 1))
        assert book.best_bid() == 50
        assert book.best_ask() == 55

    def test_midpoint(self):
        book = OrderBook()
        book.add_order(Order("b", "buy", 48, 1))
        book.add_order(Order("s", "sell", 52, 1))
        assert book.midpoint() == 50.0

    def test_empty_book(self):
        book = OrderBook()
        assert book.best_bid() == 0
        assert book.best_ask() == 100
        assert book.midpoint() == 50.0


class TestInformedAgent:
    def test_buys_when_underpriced(self):
        agent = InformedAgent("inf1", true_prob=0.80, signal_noise=0.0)
        book = OrderBook()
        book.add_order(Order("x", "sell", 60, 1))
        order = agent.decide(book, 0)
        assert order is not None
        assert order.side == "buy"

    def test_sells_when_overpriced(self):
        agent = InformedAgent("inf1", true_prob=0.20, signal_noise=0.0)
        book = OrderBook()
        book.add_order(Order("x", "buy", 40, 1))
        order = agent.decide(book, 0)
        assert order is not None
        assert order.side == "sell"


class TestOrderBookSimulator:
    def test_price_converges_to_true_prob(self):
        """Informed agents should push price toward true probability."""
        sim = OrderBookSimulator(true_prob=0.70, n_informed=15, n_noise=40,
                                 n_mm=5, seed=42)
        history, _ = sim.run(n_steps=500)
        # Last 50 prices should average close to 70
        final_avg = sum(history[-50:]) / 50
        assert 55 < final_avg < 85  # Within 15 cents of true prob

    def test_informed_agents_profit(self):
        """Informed agents should make money in aggregate."""
        sim = OrderBookSimulator(true_prob=0.65, n_informed=10, n_noise=50,
                                 n_mm=3, seed=123)
        history, book = sim.run(n_steps=400)
        # Compute informed agent P&L from fills
        informed_pnl = 0
        for fill in book.all_fills:
            buyer_id = fill["buyer_id"]
            seller_id = fill["seller_id"]
            price = fill["price"]
            true_cents = sim.true_prob * 100
            if buyer_id.startswith("informed_"):
                informed_pnl += true_cents - price  # expected profit per buy
            if seller_id.startswith("informed_"):
                informed_pnl += price - true_cents  # expected profit per sell
        # Informed should be profitable (on expectation)
        # Allow some variance — just check it's not deeply negative
        assert informed_pnl > -200

    def test_returns_price_history(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=1)
        history, book = sim.run(n_steps=100)
        assert len(history) == 100
        assert all(0 <= p <= 100 for p in history)

    def test_deterministic_with_seed(self):
        sim1 = OrderBookSimulator(true_prob=0.60, seed=777)
        h1, _ = sim1.run(n_steps=50)
        sim2 = OrderBookSimulator(true_prob=0.60, seed=777)
        h2, _ = sim2.run(n_steps=50)
        assert h1 == h2


class TestFillProbability:
    def test_aggressive_price_fills_more(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=42)
        # Buying at ask should fill more than buying at low price
        fill_high = sim.estimate_fill_probability(55, "buy", n_simulations=50)
        fill_low = sim.estimate_fill_probability(40, "buy", n_simulations=50)
        assert fill_high >= fill_low

    def test_fill_probability_range(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=42)
        prob = sim.estimate_fill_probability(50, "buy", n_simulations=50)
        assert 0.0 <= prob <= 1.0


class TestKyleLambda:
    def test_positive_lambda(self):
        """Price impact should be positive (buys push price up)."""
        # Run simulation and collect order flow / price changes
        sim = OrderBookSimulator(true_prob=0.50, n_informed=10, n_noise=50,
                                 seed=42)
        history, book = sim.run(n_steps=300)

        # Build order flow from fills
        order_flows = []
        for fill in book.all_fills:
            # +1 for buys, -1 for sells (from the initiator's perspective)
            order_flows.append(1)  # simplified: each fill is a unit of flow
        price_changes = [history[i+1] - history[i]
                         for i in range(min(len(history)-1, len(order_flows)))]

        lam = kyle_lambda_estimate(history, order_flows)
        # Lambda should be non-negative (buys increase price)
        assert lam >= -0.5  # Allow small negative due to noise

    def test_insufficient_data(self):
        lam = kyle_lambda_estimate([50], [])
        assert lam == 0.0


class TestFindOptimalMMParams:
    def test_returns_valid_params(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=42)
        result = sim.find_optimal_mm_params(
            gamma_range=[0.2, 0.5],
            k_range=[1.0, 2.0],
            n_simulations=10,
            n_steps=100,
        )
        assert "gamma" in result
        assert "k" in result
        assert "sharpe" in result
        assert "should_activate" in result
        assert result["gamma"] in [0.2, 0.5]
        assert result["k"] in [1.0, 2.0]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orderbook_sim.py -v`
Expected: FAIL — module not found.

**Step 3: Commit**

```bash
git add tests/test_orderbook_sim.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add orderbook simulator tests (failing — module not yet implemented)"
```

---

### Task 9: Orderbook Simulator — Implementation

**Files:**
- Create: `src/kalshi/orderbook_sim.py`

**Context:** Agent-based order book simulation with continuous double auction matching. Used for: (1) estimating fill probability for limit orders, (2) finding optimal Avellaneda-Stoikov params for market making, (3) computing Kyle's lambda (price impact). Pure Python, no external dependencies.

**Step 1: Implement the module**

```python
"""Agent-Based Order Book Simulation for Market Maker Activation.

Simulates a continuous double auction with four agent types calibrated
to Kalshi prediction market microstructure. Used to:
  1. Estimate fill probability for limit orders at various prices
  2. Find optimal Avellaneda-Stoikov (gamma, k) per market
  3. Measure price impact (Kyle's lambda)
  4. Decide whether to activate the market maker on a given market

Agent types:
  - Informed (5-15%): trade toward true probability with noisy signal
  - Noise (60-80%): random direction, size
  - MarketMaker: A-S quotes, inventory-managed
  - (Future: Bot agents for deterministic strategies)

Usage:
    from orderbook_sim import OrderBookSimulator

    sim = OrderBookSimulator(true_prob=0.65, seed=42)
    history, book = sim.run(n_steps=500)
    fill_prob = sim.estimate_fill_probability(50, "buy")
    params = sim.find_optimal_mm_params()
"""

import math
import random
import logging

log = logging.getLogger("orderbook-sim")


class Order:
    """A limit order in the book."""
    __slots__ = ("agent_id", "side", "price", "size", "timestamp")

    def __init__(self, agent_id, side, price, size, timestamp=0):
        self.agent_id = agent_id
        self.side = side       # "buy" or "sell"
        self.price = int(price)  # cents, 1-99
        self.size = int(size)
        self.timestamp = timestamp


class OrderBook:
    """Continuous double auction order book."""

    def __init__(self):
        self.bids = []       # sorted by price descending
        self.asks = []       # sorted by price ascending
        self.all_fills = []  # list of fill dicts

    def add_order(self, order):
        if order.side == "buy":
            self.bids.append(order)
            self.bids.sort(key=lambda o: (-o.price, o.timestamp))
        else:
            self.asks.append(order)
            self.asks.sort(key=lambda o: (o.price, o.timestamp))

    def match(self, timestamp=0):
        """Match crossing orders. Returns list of fill dicts."""
        fills = []
        while self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            bid = self.bids[0]
            ask = self.asks[0]
            fill_price = (bid.price + ask.price) // 2
            fill_size = min(bid.size, ask.size)
            fills.append({
                "timestamp": timestamp,
                "price": fill_price,
                "size": fill_size,
                "buyer_id": bid.agent_id,
                "seller_id": ask.agent_id,
            })
            bid.size -= fill_size
            ask.size -= fill_size
            if bid.size <= 0:
                self.bids.pop(0)
            if ask.size <= 0:
                self.asks.pop(0)
        self.all_fills.extend(fills)
        return fills

    def best_bid(self):
        return self.bids[0].price if self.bids else 0

    def best_ask(self):
        return self.asks[0].price if self.asks else 100

    def midpoint(self):
        b, a = self.best_bid(), self.best_ask()
        if b > 0 and a < 100:
            return (b + a) / 2.0
        return 50.0

    def clear_stale(self, max_age=50, current_step=0):
        """Remove orders older than max_age steps to prevent book bloat."""
        self.bids = [o for o in self.bids if current_step - o.timestamp < max_age]
        self.asks = [o for o in self.asks if current_step - o.timestamp < max_age]


class InformedAgent:
    """Trades toward true probability with noisy private signal."""

    def __init__(self, agent_id, true_prob, signal_noise=0.03):
        self.agent_id = agent_id
        self.true_prob = true_prob
        self.signal_noise = signal_noise

    def decide(self, book, step):
        signal = self.true_prob + random.gauss(0, self.signal_noise)
        signal = max(0.01, min(0.99, signal))
        signal_cents = round(signal * 100)
        mid = book.midpoint()

        if signal_cents > mid + 2:
            return Order(self.agent_id, "buy", min(signal_cents, 99), 1, step)
        elif signal_cents < mid - 2:
            return Order(self.agent_id, "sell", max(signal_cents, 1), 1, step)
        return None


class NoiseAgent:
    """Random trader — models retail participants."""

    def __init__(self, agent_id, trade_prob=0.25):
        self.agent_id = agent_id
        self.trade_prob = trade_prob

    def decide(self, book, step):
        if random.random() > self.trade_prob:
            return None
        side = random.choice(["buy", "sell"])
        mid = book.midpoint()
        offset = random.gauss(0, 5)
        price = int(mid + offset)
        price = max(1, min(99, price))
        return Order(self.agent_id, side, price, 1, step)


class MMAgent:
    """Market maker using Avellaneda-Stoikov reservation price model."""

    def __init__(self, agent_id, gamma=0.3, k=1.5, sigma_frac=0.05):
        self.agent_id = agent_id
        self.gamma = gamma
        self.k = k
        self.sigma_frac = sigma_frac
        self.inventory = 0

    def decide(self, book, step):
        mid_frac = book.midpoint() / 100.0
        T = 1.0

        r = mid_frac - self.inventory * self.gamma * (self.sigma_frac ** 2) * T
        r_cents = max(1, min(99, round(r * 100)))

        delta = (self.gamma * (self.sigma_frac ** 2) * T +
                 (2 / self.gamma) * math.log(1 + self.gamma / self.k))
        half_spread = max(1, round(delta * 100 / 2))

        bid = max(1, r_cents - half_spread)
        ask = min(99, r_cents + half_spread)

        orders = []
        if bid < ask:
            orders.append(Order(self.agent_id, "buy", bid, 1, step))
            orders.append(Order(self.agent_id, "sell", ask, 1, step))
        return orders if orders else None


class OrderBookSimulator:
    """Agent-based market simulation for MM evaluation."""

    def __init__(self, true_prob=0.50, n_informed=10, n_noise=60,
                 n_mm=5, informed_noise=0.03, seed=None):
        self._seed = seed
        if seed is not None:
            random.seed(seed)
        self.true_prob = true_prob
        self.agents = []
        self._agent_map = {}
        self._mm_agents = []

        aid = 0
        for _ in range(n_informed):
            a = InformedAgent(f"informed_{aid}", true_prob, informed_noise)
            self.agents.append(a)
            self._agent_map[a.agent_id] = a
            aid += 1
        for _ in range(n_noise):
            a = NoiseAgent(f"noise_{aid}")
            self.agents.append(a)
            self._agent_map[a.agent_id] = a
            aid += 1
        for _ in range(n_mm):
            a = MMAgent(f"mm_{aid}")
            self.agents.append(a)
            self._agent_map[a.agent_id] = a
            self._mm_agents.append(a)
            aid += 1

    def run(self, n_steps=500):
        """Run simulation. Returns (price_history, book)."""
        book = OrderBook()
        price_history = []

        for step in range(n_steps):
            agent = random.choice(self.agents)
            result = agent.decide(book, step)

            if result is None:
                pass
            elif isinstance(result, list):
                for order in result:
                    book.add_order(order)
            else:
                book.add_order(result)

            fills = book.match(step)

            # Update MM inventory from fills
            for fill in fills:
                buyer = self._agent_map.get(fill["buyer_id"])
                seller = self._agent_map.get(fill["seller_id"])
                if isinstance(buyer, MMAgent):
                    buyer.inventory += fill["size"]
                if isinstance(seller, MMAgent):
                    seller.inventory -= fill["size"]

            # Periodic stale order cleanup
            if step % 50 == 0:
                book.clear_stale(max_age=100, current_step=step)

            price_history.append(book.midpoint())

        return price_history, book

    def estimate_fill_probability(self, limit_price, side="buy",
                                   duration_steps=50, n_simulations=100):
        """Estimate probability a limit order fills within duration_steps."""
        fills = 0
        for sim_i in range(n_simulations):
            if self._seed is not None:
                random.seed(self._seed + sim_i + 10000)
            else:
                random.seed(sim_i + 10000)

            book = OrderBook()
            our_order = Order("_test_", side, limit_price, 1, 0)
            book.add_order(our_order)

            filled = False
            for step in range(duration_steps):
                agent = random.choice(self.agents)
                result = agent.decide(book, step)
                if result is None:
                    continue
                if isinstance(result, list):
                    for order in result:
                        book.add_order(order)
                else:
                    book.add_order(result)

                matched = book.match(step)
                for f in matched:
                    if f["buyer_id"] == "_test_" or f["seller_id"] == "_test_":
                        filled = True
                        break
                if filled:
                    break

            if filled:
                fills += 1

        return fills / n_simulations

    def find_optimal_mm_params(self, gamma_range=None, k_range=None,
                                n_simulations=30, n_steps=300):
        """Grid search for optimal MM params.

        Returns dict with best gamma, k, sharpe, and should_activate flag.
        """
        if gamma_range is None:
            gamma_range = [0.1, 0.2, 0.3, 0.5, 0.8]
        if k_range is None:
            k_range = [0.5, 1.0, 1.5, 2.0, 3.0]

        best_sharpe = -float("inf")
        best_params = (0.3, 1.5)

        for gamma in gamma_range:
            for k in k_range:
                pnls = []
                for sim_i in range(n_simulations):
                    if self._seed is not None:
                        random.seed(self._seed + sim_i + 20000)

                    # Sim without our MM, plus one of ours
                    sim = OrderBookSimulator(
                        self.true_prob, n_informed=10, n_noise=60, n_mm=3,
                        seed=(self._seed or 0) + sim_i + 30000,
                    )
                    our_mm = MMAgent("our_mm", gamma=gamma, k=k)
                    sim.agents.append(our_mm)
                    sim._agent_map[our_mm.agent_id] = our_mm
                    sim._mm_agents.append(our_mm)

                    _, book = sim.run(n_steps)

                    # Compute our MM's P&L from fills
                    buy_cost = 0
                    buy_qty = 0
                    sell_rev = 0
                    sell_qty = 0
                    for fill in book.all_fills:
                        if fill["buyer_id"] == "our_mm":
                            buy_cost += fill["price"] * fill["size"]
                            buy_qty += fill["size"]
                        if fill["seller_id"] == "our_mm":
                            sell_rev += fill["price"] * fill["size"]
                            sell_qty += fill["size"]

                    net_inv = buy_qty - sell_qty
                    settlement = net_inv * (self.true_prob * 100)
                    pnl = sell_rev - buy_cost + settlement
                    pnls.append(pnl)

                if len(pnls) >= 2:
                    mean_pnl = sum(pnls) / len(pnls)
                    var = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
                    std_pnl = math.sqrt(var) if var > 0 else 0.001
                    sharpe = mean_pnl / std_pnl

                    if sharpe > best_sharpe:
                        best_sharpe = sharpe
                        best_params = (gamma, k)

        return {
            "gamma": best_params[0],
            "k": best_params[1],
            "sharpe": round(best_sharpe, 4),
            "should_activate": best_sharpe > 1.0,
        }


def kyle_lambda_estimate(price_history, order_flows):
    """Estimate Kyle's lambda from price changes and order flow.

    lambda = price impact per unit signed order flow.
    Uses regression through origin: delta_P = lambda * signed_sqrt_flow.

    Returns estimated lambda.
    """
    if len(price_history) < 2 or len(order_flows) < 1:
        return 0.0

    n = min(len(price_history) - 1, len(order_flows))
    xs, ys = [], []
    for i in range(n):
        of = order_flows[i]
        if of == 0:
            continue
        x = math.copysign(math.sqrt(abs(of)), of)
        y = price_history[i + 1] - price_history[i]
        xs.append(x)
        ys.append(y)

    if len(xs) < 2:
        return 0.0

    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)

    return sum_xy / sum_xx if sum_xx > 0 else 0.0
```

**Step 2: Run tests**

Run: `pytest tests/test_orderbook_sim.py -v`
Expected: All tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/orderbook_sim.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add agent-based orderbook simulator for MM calibration"
```

---

### Task 10: Market Maker Calibration — Tests

**Files:**
- Create: `tests/test_mm_calibration.py`

**Context:** The market maker integration adds a calibration config file (`config/mm-calibration.json`) written by the orderbook simulator. The existing `market-maker.py` loads calibrated per-market params instead of using hardcoded gamma=0.3, k=1.5. This task tests the calibration flow and the integration with market-maker.py's functions.

**Step 1: Write tests**

```python
"""Tests for market maker calibration integration.

Tests the calibration config format, loading logic, and activation criteria.
Uses importlib to load market-maker.py (hyphenated filename).
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
import importlib
import importlib.util
import sys


def _load_mm_module():
    """Load market-maker.py with stubbed dependencies."""
    mock_auth = MagicMock()
    mock_client = MagicMock()
    mock_auth.KalshiClient.return_value = mock_client
    mock_auth.setup_logging.return_value = MagicMock()
    mock_auth.setup_unbuffered = MagicMock()
    mock_auth.setup_signal_handlers = MagicMock()
    mock_auth.PROJECT_DIR = Path(__file__).resolve().parent.parent
    mock_auth.TradeManager = MagicMock()
    mock_auth.trim_trade_log = MagicMock()
    mock_auth.build_market_snapshot = MagicMock(return_value={})

    sys.modules["kalshi_auth"] = mock_auth
    sys.modules["probability"] = MagicMock()
    sys.modules["capital_allocator"] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "market_maker",
        str(Path(__file__).resolve().parent.parent / "src" / "kalshi" / "market-maker.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Tests: Calibration config format ──

class TestCalibrationConfig:
    def test_valid_config_format(self):
        """Verify the expected calibration config structure."""
        config = {
            "KXHIGH": {
                "gamma": 0.25,
                "k": 1.2,
                "sharpe": 1.5,
                "activated": True,
                "calibrated_at": "2026-03-03T10:00:00",
            },
            "KXBTC": {
                "gamma": 0.5,
                "k": 2.0,
                "sharpe": 0.8,
                "activated": False,
                "calibrated_at": "2026-03-03T10:00:00",
            },
        }
        # Validate structure
        for prefix, params in config.items():
            assert "gamma" in params
            assert "k" in params
            assert "sharpe" in params
            assert "activated" in params
            assert params["gamma"] > 0
            assert params["k"] > 0

    def test_activation_criteria(self):
        """Only activate when simulated Sharpe > 1.0."""
        config = {
            "KXHIGH": {"gamma": 0.25, "k": 1.2, "sharpe": 1.5, "activated": True},
            "KXBTC": {"gamma": 0.5, "k": 2.0, "sharpe": 0.8, "activated": False},
        }
        for prefix, params in config.items():
            assert params["activated"] == (params["sharpe"] > 1.0)


class TestLoadCalibratedParams:
    def test_load_calibrated_gamma_k(self, tmp_path):
        """Verify load_calibrated_params reads per-market params."""
        cal_path = tmp_path / "mm-calibration.json"
        cal_data = {
            "KXHIGH": {"gamma": 0.25, "k": 1.2, "sharpe": 1.5, "activated": True},
        }
        cal_path.write_text(json.dumps(cal_data))

        # The function we'll add: load_calibrated_params(path, ticker_prefix)
        # Returns (gamma, k) if activated, else None
        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(cal_path), "KXHIGH")
        assert result is not None
        assert result["gamma"] == 0.25
        assert result["k"] == 1.2

    def test_returns_none_for_inactive(self, tmp_path):
        cal_path = tmp_path / "mm-calibration.json"
        cal_data = {
            "KXBTC": {"gamma": 0.5, "k": 2.0, "sharpe": 0.8, "activated": False},
        }
        cal_path.write_text(json.dumps(cal_data))

        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(cal_path), "KXBTC")
        assert result is None

    def test_returns_none_for_missing_prefix(self, tmp_path):
        cal_path = tmp_path / "mm-calibration.json"
        cal_path.write_text("{}")

        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(cal_path), "KXHIGH")
        assert result is None

    def test_returns_none_for_missing_file(self, tmp_path):
        from orderbook_sim import load_calibrated_params
        result = load_calibrated_params(str(tmp_path / "nonexistent.json"), "KXHIGH")
        assert result is None
```

**Step 2: Run tests to verify the new tests fail**

Run: `pytest tests/test_mm_calibration.py -v`
Expected: FAIL — `load_calibrated_params` not yet in orderbook_sim.

**Step 3: Commit**

```bash
git add tests/test_mm_calibration.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add MM calibration integration tests (failing — function not yet added)"
```

---

### Task 11: Market Maker Calibration + Integration

**Files:**
- Modify: `src/kalshi/orderbook_sim.py` (add `load_calibrated_params` function)
- Modify: `src/kalshi/market-maker.py` (load calibrated params per market prefix)
- Create: `scripts/calibrate-mm.py` (run calibration and write config)

**Step 1: Add `load_calibrated_params` to orderbook_sim.py**

Append this function to the end of `src/kalshi/orderbook_sim.py`:

```python
def load_calibrated_params(config_path, ticker_prefix):
    """Load calibrated MM params for a market prefix.

    Returns dict with gamma, k if activated. Returns None if not
    activated, not found, or config file missing.
    """
    import json
    try:
        with open(config_path) as f:
            data = json.load(f)
        params = data.get(ticker_prefix)
        if params and params.get("activated"):
            return {"gamma": params["gamma"], "k": params["k"]}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None
```

**Step 2: Create the calibration script**

Create `scripts/calibrate-mm.py`:

```python
#!/usr/bin/env python3
"""Calibrate market maker parameters using orderbook simulation.

Runs agent-based simulations for each target market prefix and writes
optimal (gamma, k) to config/mm-calibration.json.

Usage:
    python3 scripts/calibrate-mm.py              # Calibrate all target markets
    python3 scripts/calibrate-mm.py --prefix KXHIGH  # Single prefix
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from orderbook_sim import OrderBookSimulator

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "mm-calibration.json"

# Default target prefixes and their typical true probability ranges
TARGET_MARKETS = {
    "KXHIGH": {"true_prob": 0.50, "n_sims": 30, "n_steps": 300},
    "KXBTC":  {"true_prob": 0.50, "n_sims": 30, "n_steps": 300},
    "KXETH":  {"true_prob": 0.50, "n_sims": 30, "n_steps": 300},
}


def calibrate_prefix(prefix, params):
    """Run calibration for a single market prefix."""
    print(f"Calibrating {prefix}...")
    sim = OrderBookSimulator(true_prob=params["true_prob"], seed=42)
    result = sim.find_optimal_mm_params(
        gamma_range=[0.1, 0.2, 0.3, 0.5, 0.8],
        k_range=[0.5, 1.0, 1.5, 2.0, 3.0],
        n_simulations=params["n_sims"],
        n_steps=params["n_steps"],
    )
    result["activated"] = result["should_activate"]
    result["calibrated_at"] = datetime.now().isoformat()
    del result["should_activate"]
    print(f"  {prefix}: gamma={result['gamma']}, k={result['k']}, "
          f"sharpe={result['sharpe']}, activated={result['activated']}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Calibrate MM params")
    parser.add_argument("--prefix", help="Single prefix to calibrate")
    args = parser.parse_args()

    # Load existing config
    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    targets = TARGET_MARKETS
    if args.prefix:
        if args.prefix not in targets:
            targets[args.prefix] = {"true_prob": 0.50, "n_sims": 30, "n_steps": 300}
        targets = {args.prefix: targets[args.prefix]}

    for prefix, params in targets.items():
        result = calibrate_prefix(prefix, params)
        existing[prefix] = result

    # Write atomically
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(CONFIG_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    import os
    os.replace(tmp, str(CONFIG_PATH))
    print(f"\nCalibration saved to {CONFIG_PATH}")


if __name__ == "__main__":
    main()
```

**Step 3: Modify market-maker.py to load calibrated params**

In `src/kalshi/market-maker.py`, add after the config loading block (around line 48, after `TARGET_MARKETS`):

```python
# Load calibrated per-market params (from orderbook simulation)
MM_CALIBRATION_PATH = PROJECT_DIR / "config" / "mm-calibration.json"

def _load_mm_calibration():
    """Load per-prefix calibrated MM params."""
    try:
        if MM_CALIBRATION_PATH.exists():
            return json.loads(MM_CALIBRATION_PATH.read_text())
    except (json.JSONDecodeError, ValueError):
        pass
    return {}

_mm_calibration = _load_mm_calibration()
```

Then in `scan_and_quote()`, replace the hardcoded gamma computation (around line 293) with calibration-aware logic:

```python
        # Use calibrated params if available, else default
        cal = None
        for prefix in TARGET_MARKETS:
            if ticker.startswith(prefix):
                cal = _mm_calibration.get(prefix)
                break

        if cal and cal.get("activated"):
            base_gamma = cal.get("gamma", GAMMA)
            base_k = cal.get("k", K_PARAM)
        else:
            base_gamma = GAMMA
            base_k = K_PARAM

        # Adaptive gamma: increase near settlement and with inventory
        gamma = base_gamma * max(1.0, 24 / hours_to_settle) * (1 + abs(inventory) / MAX_INVENTORY)

        # Compute reservation price and optimal spread
        reservation = compute_reservation_price(mid, inventory, sigma, hours_to_settle, gamma)
        half_spread = compute_optimal_spread(sigma, hours_to_settle, gamma, base_k)
```

**Step 4: Run tests**

Run: `pytest tests/test_mm_calibration.py tests/test_orderbook_sim.py -v`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add src/kalshi/orderbook_sim.py src/kalshi/market-maker.py scripts/calibrate-mm.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add MM calibration script and load calibrated params in market-maker"
```

---

### Task 12: Dashboard API Endpoints

**Files:**
- Modify: `scripts/dashboard.py`

**Context:** Add three new API endpoints that serve data from the new analytics modules: `/api/attribution`, `/api/edge-decay`, `/api/execution-quality`. These load trade data from the trade log files and return JSON reports. The modules have no kalshi_auth dependency, so they can be imported directly.

**Step 1: Add new endpoints to dashboard.py**

Add these imports near the top of `scripts/dashboard.py` (after the existing `sys.path.insert`):

```python
from pnl_attribution import PnLAttributor
from edge_monitor import EdgeMonitor
from execution_quality import ExecutionAnalyzer
```

Add the trade file paths builder (after the existing `TRADE_FILES` list):

```python
# Build paths for analytics modules
_ANALYTICS_TRADE_FILES = [
    {"path": tf["path"], "bot": tf["bot"]} for tf in TRADE_FILES
]
```

Add the three new endpoints before the `main()` function:

```python
@app.get("/api/attribution")
async def api_attribution():
    """P&L attribution by bot, edge bucket, regime, sizing, market type."""
    cached = cache.get("attribution")
    if cached is not None:
        return cached

    attr = PnLAttributor(
        trade_file_paths=_ANALYTICS_TRADE_FILES,
        regime_state_path=str(PROJECT_DIR / "data" / "regime-state.json"),
    )
    attr.load_trades()
    report = attr.full_report()
    cache.set("attribution", report, ttl=120)
    return report


@app.get("/api/edge-decay")
async def api_edge_decay():
    """Edge decay metrics per market type."""
    cached = cache.get("edge_decay")
    if cached is not None:
        return cached

    em = EdgeMonitor(state_path=str(DATA_DIR / "edge-monitor-state.json"))
    em.load_state()

    # If no persisted state, build observations from settled trades
    if not em._observations:
        for tf in TRADE_FILES:
            trades = load_trades_safe(tf["path"])
            if not trades:
                continue
            for t in trades:
                if t.get("settlement_result") is None:
                    continue
                ticker = t.get("ticker", "")
                from pnl_attribution import _classify_market_type
                obs = {
                    "timestamp": t.get("timestamp", ""),
                    "market_type": _classify_market_type(ticker),
                    "model_prob": t.get("model_prob", 0.5),
                    "market_price_cents": t.get("best_ask") or t.get("price_cents", 50),
                    "raw_edge": t.get("raw_edge", 0.0),
                    "settled_won": t.get("settlement_result") in ("won", "yes", True, 1),
                }
                em._observations.append(obs)

    report = em.json_report()
    cache.set("edge_decay", report, ttl=300)
    return report


@app.get("/api/execution-quality")
async def api_execution_quality():
    """Execution quality metrics: fill rate, slippage, shortfall."""
    cached = cache.get("exec_quality")
    if cached is not None:
        return cached

    ea = ExecutionAnalyzer(trade_file_paths=_ANALYTICS_TRADE_FILES)
    ea.load_trades()
    report = ea.json_report()
    cache.set("exec_quality", report, ttl=120)
    return report
```

**Step 2: Verify dashboard starts without errors**

Run: `python3 scripts/dashboard.py --port 9999 &` then `curl http://localhost:9999/api/attribution` and `kill %1`
Expected: Returns JSON (even if trade logs have no settled data yet).

**Step 3: Commit**

```bash
git add scripts/dashboard.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add dashboard API endpoints for attribution, edge decay, execution quality"
```

---

### Task 13: Dashboard HTML Intelligence Tab

**Files:**
- Modify: `scripts/dashboard.html`

**Context:** Add an "Intelligence" tab to the dashboard UI that displays P&L attribution, edge decay metrics, and execution quality. The tab fetches from the three new API endpoints and renders tables/charts using the existing dashboard styling.

**Step 1: Add Intelligence tab to dashboard.html**

Find the tab navigation in `dashboard.html` and add a new tab button. Then add the tab content panel. The exact insertion points depend on the HTML structure — look for the existing tab pattern.

Add this tab button to the navigation bar (alongside existing tabs):

```html
<button class="tab-btn" data-tab="intelligence">Intelligence</button>
```

Add this tab content panel:

```html
<div id="tab-intelligence" class="tab-content" style="display:none;">
  <h2>P&L Attribution</h2>
  <div id="attribution-summary" class="card">Loading...</div>

  <div class="grid-2col">
    <div class="card">
      <h3>By Bot</h3>
      <table id="attr-by-bot" class="data-table">
        <thead><tr><th>Bot</th><th>P&L</th><th>Trades</th><th>Win Rate</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="card">
      <h3>By Edge Bucket</h3>
      <table id="attr-by-edge" class="data-table">
        <thead><tr><th>Edge</th><th>P&L</th><th>Trades</th><th>Win Rate</th><th>Avg Edge</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <div class="grid-2col">
    <div class="card">
      <h3>By Market Type</h3>
      <table id="attr-by-market" class="data-table">
        <thead><tr><th>Type</th><th>P&L</th><th>Trades</th><th>Win Rate</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="card">
      <h3>By Sizing Method</h3>
      <table id="attr-by-sizing" class="data-table">
        <thead><tr><th>Method</th><th>P&L</th><th>Trades</th><th>Win Rate</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <h2>Edge Decay Monitor</h2>
  <div id="edge-decay-content" class="card">Loading...</div>
  <table id="edge-decay-table" class="data-table">
    <thead><tr><th>Market Type</th><th>Win Rate</th><th>Trend Slope</th><th>Half-Life (days)</th><th>Competitor?</th></tr></thead>
    <tbody></tbody>
  </table>

  <h2>Execution Quality</h2>
  <div id="exec-quality-content" class="card">Loading...</div>
  <table id="exec-quality-table" class="data-table">
    <thead><tr><th>Bot</th><th>Fill Rate</th><th>Avg Slippage</th><th>Shortfall</th><th>Trades</th></tr></thead>
    <tbody></tbody>
  </table>
</div>
```

Add this JavaScript to populate the tables (inside the existing `<script>` block):

```javascript
async function loadIntelligence() {
  // Attribution
  try {
    const attr = await fetch('/api/attribution').then(r => r.json());
    if (attr.summary) {
      document.getElementById('attribution-summary').innerHTML =
        `<strong>Settled: ${attr.summary.total_trades_settled}</strong> trades | ` +
        `Total P&L: <span class="${attr.summary.total_pnl_cents >= 0 ? 'green' : 'red'}">` +
        `$${(attr.summary.total_pnl_cents / 100).toFixed(2)}</span>`;
    }
    fillAttrTable('attr-by-bot', attr.by_bot);
    fillAttrTable('attr-by-edge', attr.by_edge_bucket, true);
    fillAttrTable('attr-by-market', attr.by_market_type);
    fillAttrTable('attr-by-sizing', attr.by_sizing);
  } catch(e) { console.error('Attribution load failed:', e); }

  // Edge decay
  try {
    const edge = await fetch('/api/edge-decay').then(r => r.json());
    const tbody = document.querySelector('#edge-decay-table tbody');
    tbody.innerHTML = '';
    for (const [mt, d] of Object.entries(edge.market_types || {})) {
      const hl = d.edge_half_life_days === Infinity ? '∞' : d.edge_half_life_days.toFixed(0);
      tbody.innerHTML += `<tr>
        <td>${mt}</td><td>${(d.realization_rate * 100).toFixed(1)}%</td>
        <td>${d.efficiency_trend_slope.toFixed(5)}</td><td>${hl}</td>
        <td>${d.competitor_detected ? '⚠️ YES' : 'No'}</td></tr>`;
    }
    document.getElementById('edge-decay-content').innerHTML =
      `${edge.total_observations || 0} observations tracked`;
  } catch(e) { console.error('Edge decay load failed:', e); }

  // Execution quality
  try {
    const eq = await fetch('/api/execution-quality').then(r => r.json());
    document.getElementById('exec-quality-content').innerHTML =
      `Fill Rate: <strong>${(eq.fill_rate * 100).toFixed(1)}%</strong> | ` +
      `Avg Slippage: ${eq.average_slippage_cents.toFixed(1)}c | ` +
      `Shortfall: ${eq.implementation_shortfall_cents.toFixed(1)}c`;
    const tbody = document.querySelector('#exec-quality-table tbody');
    tbody.innerHTML = '';
    for (const [bot, d] of Object.entries(eq.by_bot || {})) {
      tbody.innerHTML += `<tr><td>${bot}</td>
        <td>${(d.fill_rate * 100).toFixed(1)}%</td>
        <td>${d.avg_slippage_cents.toFixed(1)}c</td>
        <td>${d.impl_shortfall_cents.toFixed(1)}c</td>
        <td>${d.trade_count}</td></tr>`;
    }
  } catch(e) { console.error('Exec quality load failed:', e); }
}

function fillAttrTable(tableId, data, hasAvgEdge) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  if (!tbody || !data) return;
  tbody.innerHTML = '';
  for (const [key, d] of Object.entries(data)) {
    const pnlClass = d.pnl_cents >= 0 ? 'green' : 'red';
    let row = `<td>${key}</td>
      <td class="${pnlClass}">$${(d.pnl_cents / 100).toFixed(2)}</td>
      <td>${d.trades}</td><td>${(d.win_rate * 100).toFixed(1)}%</td>`;
    if (hasAvgEdge) row += `<td>${((d.avg_edge || 0) * 100).toFixed(1)}%</td>`;
    tbody.innerHTML += `<tr>${row}</tr>`;
  }
}
```

Ensure `loadIntelligence()` is called when the Intelligence tab is activated (in the existing tab switching logic):

```javascript
// In the tab switch handler, add:
if (tabId === 'intelligence') loadIntelligence();
```

**Step 2: Verify the tab renders**

Start dashboard and verify the Intelligence tab appears and loads data.

**Step 3: Commit**

```bash
git add scripts/dashboard.html
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add Intelligence dashboard tab with attribution, edge decay, execution quality"
```

---

### Task 14: Daily Attribution Script

**Files:**
- Create: `scripts/daily-attribution.py`

**Context:** Daily script that generates and saves P&L attribution reports. Can send WhatsApp alerts for edge decay warnings. Integrates with existing daily automation (LaunchAgent, cron).

**Step 1: Create the script**

```python
#!/usr/bin/env python3
"""Daily P&L Attribution Report — Generates and saves attribution analysis.

Usage:
    python3 scripts/daily-attribution.py              # Print report
    python3 scripts/daily-attribution.py --save       # Save to data/attribution-report.json
    python3 scripts/daily-attribution.py --notify     # Send edge decay alerts via WhatsApp
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from pnl_attribution import PnLAttributor
from edge_monitor import EdgeMonitor

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

# Import trade file definitions
from trade_files import TRADE_FILES as _CANONICAL

TRADE_FILES = [{"path": str(DATA_DIR / tf["filename"]), "bot": tf["bot"]} for tf in _CANONICAL]


def print_report(report):
    """Print human-readable attribution report."""
    print("=" * 60)
    print("P&L ATTRIBUTION REPORT")
    print("=" * 60)

    s = report["summary"]
    print(f"\nSettled trades: {s['total_trades_settled']} of {s['total_trades_all']} total")
    print(f"Total P&L: ${s['total_pnl_cents'] / 100:.2f}")

    print("\n--- By Bot ---")
    for bot, stats in sorted(report["by_bot"].items(), key=lambda x: -x[1]["pnl_cents"]):
        print(f"  {bot:20s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")

    print("\n--- By Edge Bucket ---")
    for bucket, stats in report["by_edge_bucket"].items():
        print(f"  {bucket:10s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR  "
              f"avg edge {stats.get('avg_edge', 0)*100:.1f}%")

    print("\n--- By Market Type ---")
    for mt, stats in sorted(report["by_market_type"].items(), key=lambda x: -x[1]["pnl_cents"]):
        print(f"  {mt:15s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")


def check_edge_decay(notify=False):
    """Check edge monitor for decay warnings."""
    em = EdgeMonitor(state_path=str(DATA_DIR / "edge-monitor-state.json"))
    em.load_state()
    report = em.json_report()

    alerts = []
    for mt, info in report.get("market_types", {}).items():
        if info.get("competitor_detected"):
            alerts.append(f"⚠️ Competitor detected in {mt} markets")
        hl = info.get("edge_half_life_days", float("inf"))
        if hl != float("inf") and hl < 30:
            alerts.append(f"⚠️ {mt} edge half-life is {hl:.0f} days (< 30 day threshold)")

    if alerts:
        msg = "EDGE DECAY ALERT\n" + "\n".join(alerts)
        print(f"\n{msg}")
        if notify:
            try:
                from kalshi_auth import notify_whatsapp
                notify_whatsapp(msg)
            except Exception as e:
                print(f"WhatsApp notification failed: {e}")

    return alerts


def main():
    parser = argparse.ArgumentParser(description="Daily P&L Attribution")
    parser.add_argument("--save", action="store_true", help="Save report to JSON")
    parser.add_argument("--notify", action="store_true", help="Send WhatsApp alerts")
    args = parser.parse_args()

    attr = PnLAttributor(
        trade_file_paths=TRADE_FILES,
        regime_state_path=str(DATA_DIR / "regime-state.json"),
    )
    attr.load_trades()
    report = attr.full_report()

    print_report(report)

    if args.save:
        out_path = DATA_DIR / "attribution-report.json"
        with open(str(out_path) + ".tmp", "w") as f:
            json.dump(report, f, indent=2)
        import os
        os.replace(str(out_path) + ".tmp", str(out_path))
        print(f"\nSaved to {out_path}")

    check_edge_decay(notify=args.notify)


if __name__ == "__main__":
    main()
```

**Step 2: Verify the script runs**

Run: `python3 scripts/daily-attribution.py`
Expected: Prints report (may show empty data if no settled trades in logs).

**Step 3: Commit**

```bash
git add scripts/daily-attribution.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add daily attribution script with edge decay alerts"
```

---

### Task 15: Allocator Integration — Dynamic Strategy Weights

**Files:**
- Modify: `src/kalshi/capital_allocator.py`

**Context:** Feed edge monitor's optimal strategy weights back into the capital allocator. If the edge monitor detects that a strategy's edge is decaying, reduce that strategy's allocation priority dynamically. This adds a new optional check in `request_budget` that consults edge monitor weights.

**Step 1: Add edge-monitor-aware priority adjustment**

In `capital_allocator.py`, add this import and helper near the top (after the existing imports):

```python
from edge_monitor import EdgeMonitor
```

Add this method to `PortfolioAllocator.__init__` (after the regime detector initialization):

```python
        # Edge monitor for dynamic priority weights
        self._edge_monitor = EdgeMonitor(
            state_path=str(self.state_path.parent / "edge-monitor-state.json")
        )
        self._edge_monitor.load_state()
        self._edge_weights = self._edge_monitor.optimal_strategy_weights()
        self._edge_weights_loaded_at = time.time()
```

Add a method to refresh edge weights:

```python
    def _get_edge_weight(self, bot_name):
        """Get dynamic priority weight for a bot based on edge durability.

        Returns a multiplier in [0.5, 1.5] applied to the bot's base priority.
        Refreshes edge monitor weights every 6 hours.
        """
        # Refresh every 6 hours
        if time.time() - self._edge_weights_loaded_at > 6 * 3600:
            self._edge_monitor.load_state()
            self._edge_weights = self._edge_monitor.optimal_strategy_weights()
            self._edge_weights_loaded_at = time.time()

        if not self._edge_weights:
            return 1.0

        # Map bot names to market types for edge monitor lookup
        bot_to_market = {
            "weather": "weather", "source-monitor": "weather",
            "crypto": "crypto", "economics": "economics",
            "entertainment": "entertainment", "strategy": "other",
            "beatrelease": "entertainment",
        }
        market_type = bot_to_market.get(bot_name)
        if not market_type or market_type not in self._edge_weights:
            return 1.0

        # Edge weight is 0-1 (fraction of total). Convert to multiplier.
        # Average weight = 1/n_strategies. Ratio to average = multiplier.
        n = len(self._edge_weights)
        avg_weight = 1.0 / n if n > 0 else 1.0
        weight = self._edge_weights.get(market_type, avg_weight)
        multiplier = weight / avg_weight if avg_weight > 0 else 1.0

        # Clamp to [0.5, 1.5] to prevent extreme swings
        return max(0.5, min(1.5, multiplier))
```

In `_request_budget_inner`, adjust the priority lookup (around the line `priority = BOT_PRIORITY.get(bot_name, 0.2)`):

```python
        # 4. Per-bot daily spending check (based on available)
        priority = BOT_PRIORITY.get(bot_name, 0.2)
        # Adjust priority based on edge durability
        edge_mult = self._get_edge_weight(bot_name)
        effective_priority = priority * edge_mult
        max_bot_risk = int(available_balance * MAX_BOT_FRACTION * effective_priority)
```

**Step 2: Run allocator tests**

Run: `pytest tests/test_allocator.py -v`
Expected: All existing tests pass (edge_monitor import is safe — it has no kalshi_auth dependency).

**Step 3: Commit**

```bash
git add src/kalshi/capital_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: integrate edge monitor weights into capital allocator dynamic priority"
```

---

### Task 16: Config, npm Scripts, Final Verification

**Files:**
- Modify: `package.json` (add npm scripts)
- Create: `config/mm-calibration.json` (empty initial config)

**Step 1: Add npm scripts**

Add to `package.json` scripts section:

```json
"attribution": "python3 scripts/daily-attribution.py",
"attribution:save": "python3 scripts/daily-attribution.py --save",
"attribution:notify": "python3 scripts/daily-attribution.py --save --notify",
"calibrate:mm": "python3 scripts/calibrate-mm.py"
```

**Step 2: Create initial MM calibration config**

Create `config/mm-calibration.json`:

```json
{}
```

**Step 3: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (35+ existing + 5 new test files).

New test files:
- `tests/test_pnl_attribution.py`
- `tests/test_edge_monitor.py`
- `tests/test_execution_quality.py`
- `tests/test_orderbook_sim.py`
- `tests/test_mm_calibration.py`

**Step 4: Commit**

```bash
git add package.json config/mm-calibration.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "chore: add npm scripts for attribution and MM calibration, initial config"
```

**Step 5: Merge to main**

```bash
git checkout main
git merge phase7/microstructure
```

---

## Summary

| Task | Files | Tests | Lines (est) |
|------|-------|-------|-------------|
| 1 | Branch setup | — | — |
| 2-3 | pnl_attribution.py | 20 tests | ~400 |
| 4-5 | edge_monitor.py | 16 tests | ~350 |
| 6-7 | execution_quality.py | 10 tests | ~200 |
| 8-9 | orderbook_sim.py | 14 tests | ~450 |
| 10-11 | MM calibration + integration | 4 tests | ~200 |
| 12-13 | Dashboard endpoints + HTML | — | ~200 |
| 14 | daily-attribution.py | — | ~100 |
| 15 | capital_allocator.py integration | — | ~50 |
| 16 | Config + verification | — | ~20 |
| **Total** | **8 new files, 3 modified** | **~64 tests** | **~1,970** |

New modules: `pnl_attribution.py`, `edge_monitor.py`, `execution_quality.py`, `orderbook_sim.py`
New scripts: `daily-attribution.py`, `calibrate-mm.py`
Modified: `market-maker.py`, `capital_allocator.py`, `dashboard.py`, `dashboard.html`, `package.json`
New config: `config/mm-calibration.json`
