"""Tests for Oracle execution: quote check and fill monitor."""

import time
from domain.oracle.models import QuoteSnapshot
from domain.oracle.execution.quote_check import (
    check_quote_quality,
    quote_from_orderbook,
)
from domain.oracle.execution.fill_monitor import FillMonitor


# ── Quote Check ──

def test_quote_passes():
    q = QuoteSnapshot(
        ticker="T1", yes_bid=48, yes_ask=52,
        bid_depth=10, ask_depth=8,
    )
    result = check_quote_quality(q, max_spread=8, min_depth=5, max_age_seconds=5)
    assert result.passed


def test_quote_spread_too_wide():
    q = QuoteSnapshot(
        ticker="T1", yes_bid=40, yes_ask=55,
        bid_depth=10, ask_depth=8,
    )
    result = check_quote_quality(q, max_spread=8)
    assert not result.passed
    assert "Spread" in result.reason


def test_quote_depth_too_low():
    q = QuoteSnapshot(
        ticker="T1", yes_bid=48, yes_ask=52,
        bid_depth=2, ask_depth=8,
    )
    result = check_quote_quality(q, min_depth=5)
    assert not result.passed
    assert "Depth" in result.reason


def test_quote_too_old():
    q = QuoteSnapshot(
        ticker="T1", yes_bid=48, yes_ask=52,
        bid_depth=10, ask_depth=8,
        timestamp=time.time() - 10,
    )
    result = check_quote_quality(q, max_age_seconds=5)
    assert not result.passed
    assert "age" in result.reason


def test_quote_from_orderbook():
    orderbook = {
        "orderbook": {
            "yes": [[48, 25], [47, 50]],
            "no": [[52, 30], [53, 20]],
        }
    }
    q = quote_from_orderbook("T1", orderbook)
    assert q.ticker == "T1"
    assert q.yes_bid == 48
    assert q.yes_ask == 48  # 100 - 52
    assert q.bid_depth == 25
    assert q.ask_depth == 30


def test_quote_from_empty_orderbook():
    orderbook = {"orderbook": {"yes": [], "no": []}}
    q = quote_from_orderbook("T1", orderbook)
    assert q.yes_bid == 0
    assert q.yes_ask == 100


def test_quote_from_orderbook_fp():
    orderbook = {
        "orderbook_fp": {
            "yes_dollars": [["0.5800", "117933.00"]],
            "no_dollars": [["0.4100", "190985.00"]],
        }
    }
    q = quote_from_orderbook("T1", orderbook)
    assert q.yes_bid == 58
    assert q.yes_ask == 59
    assert q.bid_depth == 117933
    assert q.ask_depth == 190985


# ── Fill Monitor ──

def test_fill_monitor_basic():
    fm = FillMonitor(fill_timeout_seconds=15, max_reprices=2)
    assert not fm.is_disabled

    attempt = fm.start_fill("T1", 50)
    assert attempt.submitted_price_cents == 50

    fm.record_fill(attempt, 51)
    assert attempt.slippage_cents == 1
    assert attempt.fill_time_seconds > 0


def test_fill_monitor_cancel():
    fm = FillMonitor()
    attempt = fm.start_fill("T1", 50)
    fm.record_cancel(attempt, "timeout")
    assert attempt.canceled
    assert attempt.timed_out


def test_fill_monitor_reprice():
    fm = FillMonitor(max_reprices=2)
    attempt = fm.start_fill("T1", 50)
    fm.record_reprice(attempt, 51)
    assert attempt.reprices == 1
    assert attempt.submitted_price_cents == 51


def test_fill_monitor_auto_disable():
    fm = FillMonitor(
        slippage_disable_threshold_cents=2,
        slippage_disable_min_trades=3,
    )
    # Record 3 trades with 5c slippage each
    for i in range(3):
        attempt = fm.start_fill(f"T{i}", 50)
        fm.record_fill(attempt, 55)  # 5c slippage

    assert fm.is_disabled


def test_fill_monitor_stats():
    fm = FillMonitor()
    attempt = fm.start_fill("T1", 50)
    fm.record_fill(attempt, 51)

    stats = fm.stats()
    assert stats["total_attempts"] == 1
    assert stats["fills"] == 1
    assert stats["cancels"] == 0
    assert stats["avg_slippage_cents"] == 1.0


def test_fill_monitor_reset_disable():
    fm = FillMonitor(slippage_disable_threshold_cents=1, slippage_disable_min_trades=1)
    attempt = fm.start_fill("T1", 50)
    fm.record_fill(attempt, 55)
    assert fm.is_disabled

    fm.reset_disable()
    assert not fm.is_disabled


# ── should_reprice / should_cancel ──

def test_should_reprice_before_timeout():
    fm = FillMonitor(fill_timeout_seconds=15.0, max_reprices=2)
    attempt = fm.start_fill("T1", 50)
    # Immediately after start — should NOT reprice (not timed out)
    assert not fm.should_reprice(attempt)


def test_should_reprice_after_timeout():
    fm = FillMonitor(fill_timeout_seconds=0.01, max_reprices=2)
    attempt = fm.start_fill("T1", 50)
    import time
    time.sleep(0.02)
    assert fm.should_reprice(attempt)


def test_should_reprice_exhausted():
    fm = FillMonitor(fill_timeout_seconds=0.01, max_reprices=2)
    attempt = fm.start_fill("T1", 50)
    attempt.reprices = 2  # already used all reprices
    assert not fm.should_reprice(attempt)


def test_should_cancel_before_exhausted():
    fm = FillMonitor(fill_timeout_seconds=15.0, max_reprices=2)
    attempt = fm.start_fill("T1", 50)
    # 0 reprices, should not cancel
    assert not fm.should_cancel(attempt)


def test_should_cancel_after_exhausted_and_timeout():
    fm = FillMonitor(fill_timeout_seconds=0.01, max_reprices=2)
    attempt = fm.start_fill("T1", 50)
    attempt.reprices = 2
    import time
    time.sleep(0.04)  # past timeout * (2+1)
    assert fm.should_cancel(attempt)
