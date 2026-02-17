"""Tests for the reconcile_trades() function in analyze-performance.py."""

import importlib.util
import math
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Import the module (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------

def _load_module():
    spec = importlib.util.spec_from_file_location(
        "analyze_performance",
        str(Path(__file__).resolve().parent.parent / "scripts" / "analyze-performance.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
reconcile_trades = _mod.reconcile_trades


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bot(label, tickers):
    """Build a local_trades_by_bot entry from a list of ticker strings."""
    return {"label": label, "trades": [{"ticker": t} for t in tickers]}


def _settlement(ticker, revenue, settled_time="2026-02-16T12:00:00Z"):
    return {"ticker": ticker, "revenue": revenue, "settled_time": settled_time}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReconcileEmpty:
    def test_empty_everything(self):
        result = reconcile_trades([], [], [])
        assert result["per_bot"] == []
        assert result["aggregate"]["wins"] == 0
        assert result["aggregate"]["losses"] == 0
        assert result["aggregate"]["pnl_cents"] == 0
        assert result["aggregate"]["sharpe"] is None

    def test_empty_settlements(self):
        bots = [_bot("Bot A", ["TICKER-1", "TICKER-2"])]
        result = reconcile_trades(bots, [], [])
        bot = result["per_bot"][0]
        assert bot["wins"] == 0
        assert bot["losses"] == 0
        assert bot["unmatched"] == 2

    def test_empty_local_trades(self):
        settlements = [_settlement("TICKER-1", 500)]
        bots = [_bot("Bot A", [])]
        result = reconcile_trades(bots, settlements, [])
        bot = result["per_bot"][0]
        assert bot["wins"] == 0
        assert bot["losses"] == 0
        assert bot["unmatched"] == 0


class TestReconcileSingleTrade:
    def test_single_win(self):
        bots = [_bot("Bot A", ["TICKER-1"])]
        settlements = [_settlement("TICKER-1", 500)]
        result = reconcile_trades(bots, settlements, [])

        bot = result["per_bot"][0]
        assert bot["wins"] == 1
        assert bot["losses"] == 0
        assert bot["win_rate"] == 1.0
        assert bot["pnl_cents"] == 500
        assert bot["unmatched"] == 0

    def test_single_loss(self):
        bots = [_bot("Bot A", ["TICKER-1"])]
        settlements = [_settlement("TICKER-1", -300)]
        result = reconcile_trades(bots, settlements, [])

        bot = result["per_bot"][0]
        assert bot["wins"] == 0
        assert bot["losses"] == 1
        assert bot["win_rate"] == 0.0
        assert bot["pnl_cents"] == -300

    def test_zero_revenue_counts_as_loss(self):
        bots = [_bot("Bot A", ["TICKER-1"])]
        settlements = [_settlement("TICKER-1", 0)]
        result = reconcile_trades(bots, settlements, [])

        bot = result["per_bot"][0]
        assert bot["wins"] == 0
        assert bot["losses"] == 1
        assert bot["pnl_cents"] == 0


class TestReconcileMixed:
    def test_wins_and_losses(self):
        bots = [_bot("Bot A", ["W1", "W2", "L1"])]
        settlements = [
            _settlement("W1", 200),
            _settlement("W2", 100),
            _settlement("L1", -150),
        ]
        result = reconcile_trades(bots, settlements, [])

        bot = result["per_bot"][0]
        assert bot["wins"] == 2
        assert bot["losses"] == 1
        assert bot["pnl_cents"] == 150
        assert bot["win_rate"] == pytest.approx(2 / 3, abs=0.001)

    def test_partial_match(self):
        """Some local trades have no settlement yet."""
        bots = [_bot("Bot A", ["SETTLED", "OPEN"])]
        settlements = [_settlement("SETTLED", 50)]
        result = reconcile_trades(bots, settlements, [])

        bot = result["per_bot"][0]
        assert bot["wins"] == 1
        assert bot["unmatched"] == 1
        assert bot["pnl_cents"] == 50


class TestReconcileMultiBot:
    def test_two_bots(self):
        bots = [
            _bot("Weather", ["WX-1", "WX-2"]),
            _bot("Strategy", ["ST-1"]),
        ]
        settlements = [
            _settlement("WX-1", 100),
            _settlement("WX-2", -50),
            _settlement("ST-1", 200),
        ]
        result = reconcile_trades(bots, settlements, [])

        assert len(result["per_bot"]) == 2

        wx = result["per_bot"][0]
        assert wx["label"] == "Weather"
        assert wx["wins"] == 1
        assert wx["losses"] == 1
        assert wx["pnl_cents"] == 50

        st = result["per_bot"][1]
        assert st["label"] == "Strategy"
        assert st["wins"] == 1
        assert st["losses"] == 0
        assert st["pnl_cents"] == 200

        agg = result["aggregate"]
        assert agg["wins"] == 2
        assert agg["losses"] == 1
        assert agg["pnl_cents"] == 250
        assert agg["win_rate"] == pytest.approx(2 / 3, abs=0.001)


class TestReconcileSharpe:
    def test_sharpe_none_with_one_day(self):
        bots = [_bot("Bot A", ["T1"])]
        settlements = [_settlement("T1", 100, "2026-02-16T12:00:00Z")]
        result = reconcile_trades(bots, settlements, [])
        assert result["aggregate"]["sharpe"] is None

    def test_sharpe_computed_with_two_days(self):
        bots = [_bot("Bot A", ["T1", "T2"])]
        settlements = [
            _settlement("T1", 100, "2026-02-16T12:00:00Z"),
            _settlement("T2", 200, "2026-02-17T12:00:00Z"),
        ]
        result = reconcile_trades(bots, settlements, [])

        # daily P&L: day1=100, day2=200
        # mean=150, std=sqrt(((100-150)^2+(200-150)^2)/1)=sqrt(5000)=70.71
        # sharpe = (150/70.71)*sqrt(252) = 2.121*15.875 ≈ 33.67
        sharpe = result["aggregate"]["sharpe"]
        assert sharpe is not None
        mean_pnl = 150
        std_pnl = math.sqrt(5000)
        expected = (mean_pnl / std_pnl) * math.sqrt(252)
        assert sharpe == pytest.approx(expected, abs=0.01)

    def test_sharpe_none_with_zero_std(self):
        """Equal daily P&L → std=0 → Sharpe is None."""
        bots = [_bot("Bot A", ["T1", "T2"])]
        settlements = [
            _settlement("T1", 100, "2026-02-16T12:00:00Z"),
            _settlement("T2", 100, "2026-02-17T12:00:00Z"),
        ]
        result = reconcile_trades(bots, settlements, [])
        assert result["aggregate"]["sharpe"] is None

    def test_sharpe_with_three_days(self):
        bots = [_bot("Bot A", ["T1", "T2", "T3"])]
        settlements = [
            _settlement("T1", 100, "2026-02-15T12:00:00Z"),
            _settlement("T2", -50, "2026-02-16T12:00:00Z"),
            _settlement("T3", 200, "2026-02-17T12:00:00Z"),
        ]
        result = reconcile_trades(bots, settlements, [])

        pnl_values = [100, -50, 200]
        mean_pnl = sum(pnl_values) / 3
        variance = sum((v - mean_pnl) ** 2 for v in pnl_values) / 2  # sample variance
        std_pnl = math.sqrt(variance)
        expected = (mean_pnl / std_pnl) * math.sqrt(252)
        assert result["aggregate"]["sharpe"] == pytest.approx(expected, abs=0.01)


class TestReconcileEdgeCases:
    def test_duplicate_settlement_ticker_sums_revenue(self):
        """Multiple settlement records for the same ticker are summed."""
        bots = [_bot("Bot A", ["T1"])]
        settlements = [
            _settlement("T1", 100, "2026-02-16T12:00:00Z"),
            _settlement("T1", 50, "2026-02-16T12:00:00Z"),
        ]
        result = reconcile_trades(bots, settlements, [])
        bot = result["per_bot"][0]
        assert bot["pnl_cents"] == 150
        assert bot["wins"] == 1

    def test_non_integer_revenue_handled(self):
        bots = [_bot("Bot A", ["T1"])]
        settlements = [{"ticker": "T1", "revenue": "not_a_number", "settled_time": "2026-02-16T12:00:00Z"}]
        result = reconcile_trades(bots, settlements, [])
        bot = result["per_bot"][0]
        assert bot["pnl_cents"] == 0
