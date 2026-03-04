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
