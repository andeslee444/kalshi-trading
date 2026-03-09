# P&L Snapshot Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a dual-source verified P&L snapshot that cross-references Kalshi API settlements against local trade logs, writes `data/financial-snapshot.json`, and serves it via dashboard.

**Architecture:** Standalone `scripts/pnl-snapshot.py` with pure verification functions (testable without API). Fetches API data, loads local trade logs via `trade_files.py`, runs verification checks, writes atomic JSON. Dashboard reads and serves the file.

**Tech Stack:** Python 3, existing `kalshi_auth.KalshiClient`, `trade_files.py`, `_atomic_write_json`. No new dependencies.

---

### Task 1: Create test file with pure function tests

**Files:**
- Create: `tests/test_pnl_snapshot.py`

**Step 1: Write test fixtures and tests for `compute_realized_pnl`**

This function takes raw API settlements and returns structured P&L. It's the core
calculation that replaces the 3 different methods currently in the codebase.

```python
"""Tests for pnl-snapshot verified financial summary.

Tests pure computation and verification functions — no API calls.
"""

import pytest
from pnl_snapshot import (
    compute_realized_pnl,
    compute_unrealized_pnl,
    verify_settlements,
    build_snapshot,
    load_deposits,
)


# ── Fixtures: API settlement records (matching Kalshi /portfolio/settlements format) ──

def _make_settlement(ticker="KXHIGHHOU-26MAR03-T75", revenue=400,
                     yes_total_cost=200, no_total_cost=0, fee_cost="0.04",
                     market_result="yes", settled_time="2026-03-01T16:00:00Z"):
    """Build a Kalshi API settlement record."""
    return {
        "ticker": ticker,
        "market_ticker": ticker,
        "revenue": revenue,
        "yes_total_cost": yes_total_cost,
        "no_total_cost": no_total_cost,
        "fee_cost": fee_cost,
        "market_result": market_result,
        "settled_time": settled_time,
    }


def _make_local_trade(ticker="KXHIGHHOU-26MAR03-T75", source_bot="weather",
                      side="yes", price_cents=50, count=4, cost_cents=200,
                      order_id="ord-123", settlement_result=None,
                      settlement_revenue_cents=None, action="buy"):
    """Build a local trade log record."""
    return {
        "ticker": ticker,
        "source_bot": source_bot,
        "side": side,
        "price_cents": price_cents,
        "count": count,
        "cost_cents": cost_cents,
        "order_id": order_id,
        "action": action,
        "settlement_result": settlement_result,
        "settlement_revenue_cents": settlement_revenue_cents,
        "timestamp": "2026-03-01T10:00:00",
        "status": "filled",
    }


def _make_fill(ticker="KXHIGHHOU-26MAR03-T75", order_id="ord-123",
               side="yes", yes_price=50, no_price=50, count=4,
               created_time="2026-03-01T10:00:05Z"):
    """Build a Kalshi API fill record."""
    return {
        "ticker": ticker,
        "order_id": order_id,
        "side": side,
        "action": "buy",
        "yes_price": yes_price,
        "no_price": no_price,
        "count": count,
        "created_time": created_time,
        "is_taker": True,
    }


def _make_position(ticker="KXHIGHHOU-26MAR10-T80", position=3,
                   market_exposure=150):
    """Build a Kalshi API position record."""
    return {
        "ticker": ticker,
        "position": position,
        "market_exposure": market_exposure,
        "resting_orders_count": 0,
    }


SAMPLE_SETTLEMENTS = [
    _make_settlement(ticker="KXHIGHHOU-26MAR03-T75", revenue=400,
                     yes_total_cost=200, no_total_cost=0, fee_cost="0.04",
                     market_result="yes", settled_time="2026-03-01T16:00:00Z"),
    _make_settlement(ticker="KXBTC-26MAR03-T95000", revenue=0,
                     yes_total_cost=90, no_total_cost=0, fee_cost="0.02",
                     market_result="no", settled_time="2026-03-01T17:00:00Z"),
    _make_settlement(ticker="KXCPI-26MAY-T20", revenue=300,
                     yes_total_cost=30, no_total_cost=0, fee_cost="0.01",
                     market_result="yes", settled_time="2026-03-02T14:00:00Z"),
]

SAMPLE_LOCAL_TRADES = [
    _make_local_trade(ticker="KXHIGHHOU-26MAR03-T75", source_bot="weather",
                      order_id="ord-1", cost_cents=200),
    _make_local_trade(ticker="KXBTC-26MAR03-T95000", source_bot="crypto",
                      order_id="ord-2", cost_cents=90),
    _make_local_trade(ticker="KXCPI-26MAY-T20", source_bot="economics",
                      order_id="ord-3", cost_cents=30),
]


# ── Tests: compute_realized_pnl ──

class TestComputeRealizedPnl:
    def test_basic_pnl_from_settlements(self):
        result = compute_realized_pnl(SAMPLE_SETTLEMENTS)
        # Settlement 1: 400 - 200 - 0 = +200
        # Settlement 2: 0 - 90 - 0 = -90
        # Settlement 3: 300 - 30 - 0 = +270
        assert result["total_cents"] == 380

    def test_win_loss_counts(self):
        result = compute_realized_pnl(SAMPLE_SETTLEMENTS)
        assert result["wins"] == 2  # profit > 0
        assert result["losses"] == 1  # profit < 0

    def test_win_rate(self):
        result = compute_realized_pnl(SAMPLE_SETTLEMENTS)
        assert result["win_rate"] == pytest.approx(2 / 3, abs=0.001)

    def test_fee_extraction(self):
        result = compute_realized_pnl(SAMPLE_SETTLEMENTS)
        # "0.04" + "0.02" + "0.01" = $0.07 = 7 cents
        assert result["total_fees_cents"] == 7

    def test_net_after_fees(self):
        result = compute_realized_pnl(SAMPLE_SETTLEMENTS)
        assert result["net_after_fees_cents"] == 380 - 7

    def test_daily_breakdown(self):
        result = compute_realized_pnl(SAMPLE_SETTLEMENTS)
        assert "2026-03-01" in result["by_day"]
        assert "2026-03-02" in result["by_day"]
        # Day 1: +200 + (-90) = +110
        assert result["by_day"]["2026-03-01"] == 110
        # Day 2: +270
        assert result["by_day"]["2026-03-02"] == 270

    def test_empty_settlements(self):
        result = compute_realized_pnl([])
        assert result["total_cents"] == 0
        assert result["wins"] == 0
        assert result["losses"] == 0

    def test_fee_with_bad_string(self):
        """fee_cost can be empty string or missing."""
        s = _make_settlement(fee_cost="")
        result = compute_realized_pnl([s])
        assert result["total_fees_cents"] == 0

    def test_source_label(self):
        result = compute_realized_pnl(SAMPLE_SETTLEMENTS)
        assert result["source"] == "kalshi_api_settlements"


# ── Tests: compute_unrealized_pnl ──

class TestComputeUnrealizedPnl:
    def test_basic_unrealized(self):
        positions = [_make_position(position=3, market_exposure=150)]
        fills = [_make_fill(ticker="KXHIGHHOU-26MAR10-T80", count=3, yes_price=40)]
        result = compute_unrealized_pnl(positions, fills)
        assert len(result["positions"]) == 1
        p = result["positions"][0]
        assert p["ticker"] == "KXHIGHHOU-26MAR10-T80"
        assert p["cost_cents"] == 120  # 40 * 3
        assert p["current_value_cents"] == 150  # market_exposure
        assert p["unrealized_cents"] == 30

    def test_no_positions(self):
        result = compute_unrealized_pnl([], [])
        assert result["total_cents"] == 0
        assert result["positions"] == []

    def test_source_label(self):
        result = compute_unrealized_pnl([], [])
        assert result["source"] == "kalshi_api_positions_and_fills"


# ── Tests: verify_settlements ──

class TestVerifySettlements:
    def test_all_matched(self):
        """All API settlements have matching local trades."""
        result = verify_settlements(SAMPLE_SETTLEMENTS, SAMPLE_LOCAL_TRADES)
        settlement_check = next(c for c in result["checks"]
                                if c["check"] == "settlement_count_match")
        assert settlement_check["status"] == "ok"
        assert result["unmatched_api_settlements"] == []
        assert result["unmatched_local_trades"] == []

    def test_orphan_api_settlement(self):
        """API has settlement that no local trade matches."""
        extra = _make_settlement(ticker="KXORPHAN-123", revenue=100,
                                 yes_total_cost=50, no_total_cost=0)
        result = verify_settlements(
            SAMPLE_SETTLEMENTS + [extra],
            SAMPLE_LOCAL_TRADES,
        )
        assert "KXORPHAN-123" in result["unmatched_api_settlements"]

    def test_orphan_local_trade(self):
        """Local trade has no matching API settlement."""
        extra = _make_local_trade(ticker="KXLOCAL-999", order_id="ord-99")
        result = verify_settlements(
            SAMPLE_SETTLEMENTS,
            SAMPLE_LOCAL_TRADES + [extra],
        )
        assert "KXLOCAL-999" in result["unmatched_local_trades"]

    def test_pnl_agreement_check(self):
        """API P&L matches local P&L when settlement_revenue_cents is set."""
        local = [
            _make_local_trade(ticker="KXHIGHHOU-26MAR03-T75", cost_cents=200,
                              settlement_result="won", settlement_revenue_cents=400,
                              order_id="ord-1"),
        ]
        api = [
            _make_settlement(ticker="KXHIGHHOU-26MAR03-T75", revenue=400,
                             yes_total_cost=200, no_total_cost=0),
        ]
        result = verify_settlements(api, local)
        pnl_check = next(c for c in result["checks"]
                         if c["check"] == "pnl_agreement")
        assert pnl_check["status"] == "ok"
        assert pnl_check["delta_cents"] == 0

    def test_pnl_disagreement_flagged(self):
        """Mismatch between API and local P&L is flagged."""
        local = [
            _make_local_trade(ticker="KXHIGHHOU-26MAR03-T75", cost_cents=200,
                              settlement_result="won", settlement_revenue_cents=350,
                              order_id="ord-1"),
        ]
        api = [
            _make_settlement(ticker="KXHIGHHOU-26MAR03-T75", revenue=400,
                             yes_total_cost=200, no_total_cost=0),
        ]
        result = verify_settlements(api, local)
        pnl_check = next(c for c in result["checks"]
                         if c["check"] == "pnl_agreement")
        assert pnl_check["status"] == "warning"
        assert pnl_check["delta_cents"] != 0

    def test_overall_status_ok_when_all_pass(self):
        result = verify_settlements(SAMPLE_SETTLEMENTS, SAMPLE_LOCAL_TRADES)
        assert result["status"] in ("ok", "warnings")

    def test_sell_trades_excluded_from_matching(self):
        """Sell (exit) trades should not count as orphans."""
        local = SAMPLE_LOCAL_TRADES + [
            _make_local_trade(ticker="KXSELL-TICKER", action="sell", order_id="ord-sell"),
        ]
        result = verify_settlements(SAMPLE_SETTLEMENTS, local)
        assert "KXSELL-TICKER" not in result["unmatched_local_trades"]


# ── Tests: load_deposits ──

class TestLoadDeposits:
    def test_missing_file_returns_untracked(self, tmp_path):
        result = load_deposits(tmp_path / "nonexistent.json")
        assert result["tracked"] is False

    def test_valid_deposits(self, tmp_path):
        import json
        deposits_file = tmp_path / "deposits.json"
        deposits_file.write_text(json.dumps([
            {"date": "2026-02-15", "type": "deposit", "amount_cents": 50000},
            {"date": "2026-03-01", "type": "withdrawal", "amount_cents": 5000},
        ]))
        result = load_deposits(deposits_file)
        assert result["tracked"] is True
        assert result["total_deposited_cents"] == 50000
        assert result["total_withdrawn_cents"] == 5000


# ── Tests: build_snapshot (integration of all pieces) ──

class TestBuildSnapshot:
    def test_snapshot_has_all_sections(self):
        snapshot = build_snapshot(
            balance_cents=48500,
            portfolio_value_cents=1200,
            settlements=SAMPLE_SETTLEMENTS,
            fills=[],
            positions=[],
            local_trades=SAMPLE_LOCAL_TRADES,
            deposits_path=None,
        )
        assert "generated_at" in snapshot
        assert "sources_used" in snapshot
        assert "account" in snapshot
        assert "realized_pnl" in snapshot
        assert "unrealized_pnl" in snapshot
        assert "verification" in snapshot
        assert "deposits" in snapshot

    def test_account_section(self):
        snapshot = build_snapshot(
            balance_cents=48500,
            portfolio_value_cents=1200,
            settlements=[], fills=[], positions=[],
            local_trades=[], deposits_path=None,
        )
        assert snapshot["account"]["balance_cents"] == 48500
        assert snapshot["account"]["portfolio_value_cents"] == 1200
        assert snapshot["account"]["nav_cents"] == 49700

    def test_sources_used(self):
        snapshot = build_snapshot(
            balance_cents=0, portfolio_value_cents=0,
            settlements=[], fills=[], positions=[],
            local_trades=[], deposits_path=None,
        )
        assert "kalshi_api" in snapshot["sources_used"]
        assert "local_trade_logs" in snapshot["sources_used"]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pnl_snapshot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pnl_snapshot'`

---

### Task 2: Implement pure computation functions

**Files:**
- Create: `scripts/pnl-snapshot.py`

**Step 1: Write the module with all pure functions**

```python
#!/usr/bin/env python3
"""P&L Snapshot — Dual-source verified financial summary.

Fetches authoritative data from Kalshi API, loads local trade logs,
cross-references both sources, writes data/financial-snapshot.json.

Usage:
    python3 scripts/pnl-snapshot.py              # generate snapshot
    python3 scripts/pnl-snapshot.py --print       # print to stdout instead of file
    python3 scripts/pnl-snapshot.py --pull-s3     # pull trade logs from S3 first
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ─── Project paths ───
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from trade_files import TRADE_FILES as _CANONICAL_FILES

DATA_DIR = PROJECT_DIR / "data"
DEPOSITS_PATH = DATA_DIR / "deposits.json"
SNAPSHOT_PATH = DATA_DIR / "financial-snapshot.json"


# ─── Pure computation functions (no I/O, fully testable) ───

def compute_realized_pnl(settlements):
    """Compute realized P&L from Kalshi API settlement records.

    Uses the authoritative formula: profit = revenue - yes_total_cost - no_total_cost
    (Kalshi's 'revenue' is gross payout, NOT net profit.)

    Returns dict with total_cents, fees, wins/losses, by_day, by_bot breakdown.
    """
    total_cents = 0
    total_fees_cents = 0
    wins = 0
    losses = 0
    by_day = defaultdict(int)

    for s in settlements:
        revenue = _safe_int(s.get("revenue", 0))
        yes_cost = _safe_int(s.get("yes_total_cost", 0))
        no_cost = _safe_int(s.get("no_total_cost", 0))
        profit = revenue - yes_cost - no_cost

        # Fee: Kalshi returns dollars as string (e.g. "0.04")
        try:
            fee_cents = round(float(s.get("fee_cost", "0") or "0") * 100)
        except (TypeError, ValueError):
            fee_cents = 0

        total_cents += profit
        total_fees_cents += fee_cents

        if profit > 0:
            wins += 1
        elif profit < 0:
            losses += 1

        # Daily breakdown by settlement date
        settled_time = s.get("settled_time", "")
        if settled_time and isinstance(settled_time, str) and len(settled_time) >= 10:
            day = settled_time[:10]
            by_day[day] += profit

    count = wins + losses
    return {
        "total_cents": total_cents,
        "total_fees_cents": total_fees_cents,
        "net_after_fees_cents": total_cents - total_fees_cents,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / count, 4) if count > 0 else 0.0,
        "by_day": dict(sorted(by_day.items())),
        "source": "kalshi_api_settlements",
    }


def compute_unrealized_pnl(positions, fills):
    """Compute unrealized P&L from open positions and historical fills.

    Positions provide current market_exposure (value at current prices).
    Fills provide cost basis (what was paid to enter).

    Returns dict with total unrealized and per-position breakdown.
    """
    # Build ticker → cost basis from fills
    ticker_cost = defaultdict(int)
    for f in fills:
        ticker = f.get("ticker", "")
        side = (f.get("side", "") or "").lower()
        price = f.get("yes_price", 0) if side == "yes" else f.get("no_price", 0)
        count = f.get("count", 0) or 0
        action = (f.get("action", "") or "").lower()
        if action == "buy":
            ticker_cost[ticker] += (price or 0) * count
        elif action == "sell":
            ticker_cost[ticker] -= (price or 0) * count

    result_positions = []
    total_unrealized = 0

    for p in positions:
        if p.get("position", 0) == 0:
            continue
        ticker = p.get("ticker", "")
        current_value = p.get("market_exposure", 0)
        cost = ticker_cost.get(ticker, 0)
        unrealized = current_value - cost

        result_positions.append({
            "ticker": ticker,
            "position": p.get("position", 0),
            "cost_cents": cost,
            "current_value_cents": current_value,
            "unrealized_cents": unrealized,
        })
        total_unrealized += unrealized

    return {
        "total_cents": total_unrealized,
        "positions": result_positions,
        "source": "kalshi_api_positions_and_fills",
    }


def verify_settlements(api_settlements, local_trades):
    """Cross-verify API settlements against local trade logs.

    Runs multiple checks and returns structured verification report.
    """
    checks = []

    # Build ticker sets (exclude sell/exit trades from local)
    api_tickers = {s.get("ticker", "") or s.get("market_ticker", "")
                   for s in api_settlements}
    local_buy_trades = [t for t in local_trades
                        if t.get("action", "buy") == "buy"]
    local_tickers = {t.get("ticker", "") for t in local_buy_trades
                     if t.get("ticker")}

    # Check 1: Settlement count match
    api_count = len(api_tickers)
    local_settled_tickers = local_tickers & api_tickers
    local_count = len(local_settled_tickers)
    count_match = api_count == local_count
    checks.append({
        "check": "settlement_count_match",
        "api": api_count,
        "local": local_count,
        "status": "ok" if count_match else "warning",
        "detail": (f"{api_count - local_count} API settlements have no matching "
                   f"local trade" if not count_match else ""),
    })

    # Check 2: P&L agreement per ticker
    # Build API P&L by ticker
    api_pnl_by_ticker = {}
    for s in api_settlements:
        ticker = s.get("ticker", "") or s.get("market_ticker", "")
        revenue = _safe_int(s.get("revenue", 0))
        cost = _safe_int(s.get("yes_total_cost", 0)) + _safe_int(s.get("no_total_cost", 0))
        api_pnl_by_ticker[ticker] = revenue - cost

    # Build local P&L by ticker (only where settlement_revenue_cents exists)
    local_pnl_by_ticker = {}
    for t in local_buy_trades:
        ticker = t.get("ticker", "")
        sr = t.get("settlement_revenue_cents")
        cost = t.get("cost_cents", 0) or 0
        if sr is not None and ticker:
            local_pnl_by_ticker[ticker] = sr - cost

    # Compare where both exist
    common_tickers = set(api_pnl_by_ticker) & set(local_pnl_by_ticker)
    total_api_pnl = sum(api_pnl_by_ticker.get(t, 0) for t in common_tickers)
    total_local_pnl = sum(local_pnl_by_ticker.get(t, 0) for t in common_tickers)
    delta = abs(total_api_pnl - total_local_pnl)
    checks.append({
        "check": "pnl_agreement",
        "api_cents": total_api_pnl,
        "local_cents": total_local_pnl,
        "delta_cents": delta,
        "tickers_compared": len(common_tickers),
        "status": "ok" if delta == 0 else "warning",
    })

    # Check 3: Orphan detection
    unmatched_api = sorted(api_tickers - local_tickers)
    unmatched_local = sorted(local_tickers - api_tickers)

    checks.append({
        "check": "orphan_settlements",
        "count": len(unmatched_api),
        "status": "ok" if not unmatched_api else "warning",
    })
    checks.append({
        "check": "orphan_local_trades",
        "count": len(unmatched_local),
        "status": "ok" if not unmatched_local else "info",
        "detail": "Local trades not yet settled" if unmatched_local else "",
    })

    # Overall status
    statuses = [c["status"] for c in checks]
    if "error" in statuses:
        overall = "errors"
    elif "warning" in statuses:
        overall = "warnings"
    else:
        overall = "ok"

    return {
        "status": overall,
        "checks": checks,
        "unmatched_api_settlements": unmatched_api,
        "unmatched_local_trades": unmatched_local,
    }


def load_deposits(deposits_path):
    """Load manually-tracked deposit/withdrawal ledger.

    Returns dict with tracked, totals, and optional ROI.
    File format: JSON array of {date, type, amount_cents, note}.
    """
    path = Path(deposits_path) if deposits_path else None
    if not path or not path.exists():
        return {"tracked": False, "source": "data/deposits.json"}

    try:
        data = json.loads(path.read_text().strip())
        if not isinstance(data, list):
            return {"tracked": False, "source": str(path)}
    except (json.JSONDecodeError, ValueError, OSError):
        return {"tracked": False, "source": str(path)}

    total_deposited = 0
    total_withdrawn = 0
    for entry in data:
        amount = entry.get("amount_cents", 0)
        if entry.get("type") == "deposit":
            total_deposited += amount
        elif entry.get("type") == "withdrawal":
            total_withdrawn += amount

    return {
        "tracked": True,
        "total_deposited_cents": total_deposited,
        "total_withdrawn_cents": total_withdrawn,
        "net_funded_cents": total_deposited - total_withdrawn,
        "entries": len(data),
        "source": str(path),
    }


def build_snapshot(balance_cents, portfolio_value_cents, settlements, fills,
                   positions, local_trades, deposits_path):
    """Assemble the complete financial snapshot from all data sources."""
    realized = compute_realized_pnl(settlements)
    unrealized = compute_unrealized_pnl(positions, fills)
    verification = verify_settlements(settlements, local_trades)
    deposits = load_deposits(deposits_path)

    # Bot attribution from API settlements + local trade ticker→bot mapping
    ticker_to_bot = {}
    for t in local_trades:
        ticker = t.get("ticker", "")
        bot = t.get("source_bot", "")
        if ticker and bot:
            ticker_to_bot[ticker] = bot

    by_bot = defaultdict(lambda: {"pnl_cents": 0, "wins": 0, "losses": 0, "fees_cents": 0})
    for s in settlements:
        ticker = s.get("ticker", "") or s.get("market_ticker", "")
        revenue = _safe_int(s.get("revenue", 0))
        cost = _safe_int(s.get("yes_total_cost", 0)) + _safe_int(s.get("no_total_cost", 0))
        profit = revenue - cost
        try:
            fee = round(float(s.get("fee_cost", "0") or "0") * 100)
        except (TypeError, ValueError):
            fee = 0

        bot = ticker_to_bot.get(ticker, _infer_bot(ticker))
        by_bot[bot]["pnl_cents"] += profit
        by_bot[bot]["fees_cents"] += fee
        if profit > 0:
            by_bot[bot]["wins"] += 1
        elif profit < 0:
            by_bot[bot]["losses"] += 1

    # Add win_rate to each bot
    for stats in by_bot.values():
        total = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(stats["wins"] / total, 4) if total > 0 else 0.0

    realized["by_bot"] = dict(by_bot)

    # ROI calculation if deposits tracked
    if deposits.get("tracked") and deposits.get("net_funded_cents", 0) > 0:
        deposits["roi_pct"] = round(
            realized["total_cents"] / deposits["net_funded_cents"] * 100, 2
        )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_used": ["kalshi_api", "local_trade_logs"],
        "account": {
            "balance_cents": balance_cents,
            "portfolio_value_cents": portfolio_value_cents,
            "nav_cents": balance_cents + portfolio_value_cents,
        },
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "verification": verification,
        "deposits": deposits,
    }


def _infer_bot(ticker):
    """Best-effort bot attribution from ticker prefix."""
    t = (ticker or "").upper()
    if t.startswith("KXHIGH"):
        return "weather"
    if t.startswith(("KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP")):
        return "crypto"
    if t.startswith(("KXCPI", "KXGDP", "KXJOBS", "KXFED")):
        return "economics"
    if t.startswith(("KXALBUM", "KX1ALBUM")):
        return "entertainment"
    return "other"


def _safe_int(val):
    """Convert to int safely."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


# ─── I/O functions (not tested in unit tests) ───

def _load_local_trades():
    """Load all local trade logs using canonical trade_files list."""
    all_trades = []
    for tf in _CANONICAL_FILES:
        filepath = DATA_DIR / tf["filename"]
        if not filepath.exists():
            continue
        try:
            text = filepath.read_text().strip()
            if not text:
                continue
            trades = json.loads(text)
            if isinstance(trades, list):
                for t in trades:
                    if not t.get("source_bot"):
                        t["source_bot"] = tf["bot"]
                    all_trades.append(t)
        except (json.JSONDecodeError, ValueError, OSError):
            continue
    return all_trades


def _fetch_api_data():
    """Fetch all required data from Kalshi API."""
    from kalshi_auth import KalshiClient, _atomic_write_json
    client = KalshiClient()

    # Balance
    balance_data = client.get("/portfolio/balance")
    balance_cents = balance_data.get("balance", 0)
    portfolio_value_cents = balance_data.get("portfolio_value", 0)

    # Settlements (paginated)
    settlements = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/settlements?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        batch = data.get("settlements", [])
        settlements.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    # Fills (paginated)
    fills = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/fills?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        batch = data.get("fills", [])
        fills.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    # Positions
    pos_data = client.get("/portfolio/positions")
    positions = [p for p in pos_data.get("market_positions", [])
                 if p.get("position", 0) != 0]

    return balance_cents, portfolio_value_cents, settlements, fills, positions


def main():
    parser = argparse.ArgumentParser(
        description="Generate dual-source verified P&L snapshot."
    )
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="Print to stdout instead of writing file")
    parser.add_argument("--pull-s3", action="store_true",
                        help="Pull trade logs from S3 before generating snapshot")
    args = parser.parse_args()

    if args.pull_s3:
        print("Pulling trade logs from S3...")
        subprocess.run(
            ["bash", str(PROJECT_DIR / "scripts" / "s3-sync.sh"), "download"],
            check=True,
        )

    print("Fetching data from Kalshi API...")
    balance, portfolio_value, settlements, fills, positions = _fetch_api_data()
    print(f"  {len(settlements)} settlements, {len(fills)} fills, {len(positions)} open positions")

    print("Loading local trade logs...")
    local_trades = _load_local_trades()
    print(f"  {len(local_trades)} local trade records")

    deposits_path = DEPOSITS_PATH if DEPOSITS_PATH.exists() else None

    snapshot = build_snapshot(
        balance_cents=balance,
        portfolio_value_cents=portfolio_value,
        settlements=settlements,
        fills=fills,
        positions=positions,
        local_trades=local_trades,
        deposits_path=deposits_path,
    )

    if args.print_only:
        print(json.dumps(snapshot, indent=2))
    else:
        from kalshi_auth import _atomic_write_json
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(SNAPSHOT_PATH, snapshot)
        print(f"Snapshot written to {SNAPSHOT_PATH}")

        # Print verification summary
        v = snapshot["verification"]
        status = v["status"].upper()
        print(f"\nVerification: {status}")
        for c in v["checks"]:
            icon = "✓" if c["status"] == "ok" else "⚠" if c["status"] == "warning" else "✗"
            detail = f" — {c.get('detail', '')}" if c.get("detail") else ""
            print(f"  {icon} {c['check']}{detail}")

        pnl = snapshot["realized_pnl"]
        print(f"\nRealized P&L: ${pnl['total_cents'] / 100:+.2f} "
              f"({pnl['wins']}W/{pnl['losses']}L, "
              f"{pnl['win_rate'] * 100:.1f}% WR)")
        print(f"Fees: ${pnl['total_fees_cents'] / 100:.2f}")
        print(f"NAV: ${snapshot['account']['nav_cents'] / 100:.2f}")


if __name__ == "__main__":
    main()
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_pnl_snapshot.py -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add scripts/pnl-snapshot.py tests/test_pnl_snapshot.py
git commit -m "feat: add pnl-snapshot with dual-source P&L verification

Cross-references Kalshi API settlements against local trade logs.
Writes data/financial-snapshot.json with verified P&L, verification
checks, and optional deposit tracking."
```

---

### Task 3: Add dashboard endpoint, S3 sync, and npm script

**Files:**
- Modify: `scripts/dashboard.py` (add `/api/snapshot` endpoint near line 845)
- Modify: `scripts/s3-sync.sh` (add to sync_filters near line 31)
- Modify: `package.json` (add snapshot script)
- Modify: `scripts/daily-automation.sh` (add step after reconciliation)

**Step 1: Add dashboard endpoint**

In `scripts/dashboard.py`, after the `/api/performance` endpoint (around line 847), add:

```python
@app.get("/api/snapshot")
async def api_snapshot():
    """Return verified financial snapshot (generated by pnl-snapshot.py)."""
    data = load_json_safe(DATA_DIR / "financial-snapshot.json")
    if data is None:
        return {"error": "No snapshot found. Run: npm run snapshot"}
    return data
```

**Step 2: Add to S3 sync filters**

In `scripts/s3-sync.sh`, in the `sync_filters()` function, after line 31 (`--include=performance-metrics.json`), add:

```bash
  echo "--include=financial-snapshot.json"
```

**Step 3: Add npm script**

In `package.json`, add to the `scripts` section:

```json
"snapshot": "python3 scripts/pnl-snapshot.py"
```

**Step 4: Add to daily automation**

In `scripts/daily-automation.sh`, after the reconciliation step (line 54) and before the daily report (line 57), add:

```bash
# Step 3: Generate verified P&L snapshot
echo "$(date): Generating P&L snapshot..." >> "$LOG_FILE"
python3 scripts/pnl-snapshot.py >> "$LOG_FILE" 2>&1 || true
```

(Renumber subsequent steps in comments.)

**Step 5: Commit**

```bash
git add scripts/dashboard.py scripts/s3-sync.sh package.json scripts/daily-automation.sh
git commit -m "feat: integrate pnl-snapshot with dashboard, S3 sync, and daily automation

- /api/snapshot endpoint serves pre-computed snapshot
- S3 sync includes financial-snapshot.json
- npm run snapshot shortcut
- Added to daily automation pipeline"
```

---

### Task 4: Create deposits.json template and update .gitignore

**Files:**
- Verify: `.gitignore` already excludes `data/` (it should via existing rules)
- Modify: `.env.example` (document STARTING_BALANCE_CENTS)

**Step 1: Verify data/ is gitignored**

Run: `grep -n "data/" .gitignore`
Expected: Should show `data/` is excluded

**Step 2: Create example deposits file**

The actual `data/deposits.json` is gitignored (lives in `data/`), but document
the format in the design doc (already done) and in the script's docstring.

**Step 3: Commit if any changes**

```bash
git add -A
git commit -m "docs: document deposits.json format for P&L snapshot"
```

---

### Task 5: Manual integration test

**Step 1: Run the snapshot script**

Run: `python3 scripts/pnl-snapshot.py --print`
Expected: JSON output with all sections, or clear error if API credentials not configured

**Step 2: Run without --print to write file**

Run: `python3 scripts/pnl-snapshot.py`
Expected: `data/financial-snapshot.json` created with verification summary on stdout

**Step 3: Verify dashboard serves it**

Run: `python3 scripts/dashboard.py &` then `curl localhost:3456/api/snapshot`
Expected: Returns the snapshot JSON

**Step 4: Run full test suite for snapshot tests**

Run: `pytest tests/test_pnl_snapshot.py -v`
Expected: All tests pass
