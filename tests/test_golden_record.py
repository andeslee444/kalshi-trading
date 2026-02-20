"""Tests for the golden trade record schema, limit tier classification,
caps tracking, Kelly return_details, and reconciliation annotation.

Fix 1: Validates the extended trade record and reconciliation pipeline.
"""

import datetime
import json
import tempfile
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from probability import half_kelly, half_kelly_sell, quarter_kelly, high_conviction_kelly


class TestLimitTierClassification:
    """Test TradeManager._classify_limit_tier()."""

    def _classify(self, edge):
        from kalshi_auth import TradeManager
        return TradeManager._classify_limit_tier(edge)

    def test_high_edge_is_urgent(self):
        assert self._classify(0.20) == "urgent"

    def test_none_edge_is_urgent(self):
        assert self._classify(None) == "urgent"

    def test_medium_edge_is_balanced(self):
        assert self._classify(0.10) == "balanced"

    def test_low_edge_is_patient(self):
        assert self._classify(0.05) == "patient"

    def test_boundary_15_is_urgent(self):
        assert self._classify(0.15) == "urgent"

    def test_boundary_08_is_balanced(self):
        assert self._classify(0.08) == "balanced"


class TestKellyReturnDetails:
    """Test return_details parameter on Kelly sizing functions."""

    def test_half_kelly_returns_details(self):
        result = half_kelly(0.20, 30, 500, bankroll_cents=10000, return_details=True)
        assert len(result) == 3
        contracts, risk, details = result
        assert contracts > 0
        assert isinstance(details, dict)
        assert "kelly_fraction" in details
        assert "bankroll_used" in details
        assert details["bankroll_used"] == 10000
        assert details["kelly_fraction"] > 0

    def test_half_kelly_without_details_unchanged(self):
        result = half_kelly(0.20, 30, 500, bankroll_cents=10000)
        assert len(result) == 2
        contracts, risk = result
        assert contracts > 0

    def test_half_kelly_zero_edge_details(self):
        result = half_kelly(0, 30, 500, return_details=True)
        contracts, risk, details = result
        assert contracts == 0
        assert details["kelly_fraction"] == 0.0

    def test_half_kelly_sell_returns_details(self):
        result = half_kelly_sell(0.10, 10, 500, bankroll_cents=10000, return_details=True)
        assert len(result) == 3
        contracts, risk, details = result
        assert "kelly_fraction" in details

    def test_quarter_kelly_returns_details(self):
        result = quarter_kelly(0.20, 30, 500, bankroll_cents=10000, return_details=True)
        assert len(result) == 3
        contracts, risk, details = result
        assert "kelly_fraction" in details
        # Quarter Kelly fraction should be smaller than half Kelly
        hk_result = half_kelly(0.20, 30, 500, bankroll_cents=10000, return_details=True)
        assert details["kelly_fraction"] <= hk_result[2]["kelly_fraction"]

    def test_high_conviction_returns_details(self):
        result = high_conviction_kelly(0.20, 30, 500, bankroll_cents=100000, return_details=True)
        assert len(result) == 3
        contracts, risk, details = result
        assert "kelly_fraction" in details
        assert details["kelly_fraction"] > 0


class TestGoldenRecordBuild:
    """Test TradeManager._build_golden_record()."""

    def _make_tm(self):
        from kalshi_auth import TradeManager
        mock_client = MagicMock()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b"[]")
            trades_path = Path(f.name)
        tm = TradeManager.__new__(TradeManager)
        tm.client = mock_client
        tm.trades_path = trades_path
        tm.config = {"maxTradeAmount": 10, "maxDailyTrades": 10, "maxDailyLoss": 50}
        tm.log = MagicMock()
        tm.log.name = "test-bot"
        tm.kill_switch_path = Path("/tmp/nonexistent_halt")
        tm.tracker = MagicMock()
        tm.breaker = MagicMock()
        tm._daily_trades = 0
        tm._daily_spend_cents = 0
        tm._daily_date = None
        return tm

    def test_record_has_settlement_placeholders(self):
        tm = self._make_tm()
        record = tm._build_golden_record(
            "KXHIGHMIA-26FEB16-T86", "no", 70, 3, 210,
            "test reasoning", {"order_id": "abc123", "status": "resting"},
            market_snapshot={"yes_bid": 28, "yes_ask": 32},
            raw_edge=0.12,
            market_close_time="2026-02-16T23:00:00Z",
        )
        assert record["settlement_result"] is None
        assert record["settlement_revenue_cents"] is None
        assert record["fill_price_cents"] is None

    def test_record_flattens_snapshot(self):
        tm = self._make_tm()
        record = tm._build_golden_record(
            "KXHIGHNY-26FEB16-T40", "yes", 50, 2, 100,
            "test", {"order_id": "x", "status": "ok"},
            market_snapshot={"yes_bid": 48, "yes_ask": 52},
        )
        assert record["best_bid"] == 48
        assert record["best_ask"] == 52
        assert record["spread"] == 4

    def test_record_computes_time_to_settle(self):
        tm = self._make_tm()
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = tm._build_golden_record(
            "KXHIGHCHI-26FEB16-T30", "no", 70, 1, 70,
            "test", {"order_id": "y", "status": "ok"},
            market_close_time=future,
        )
        assert "time_to_settle_minutes" in record
        assert 100 < record["time_to_settle_minutes"] < 140  # ~120 minutes

    def test_record_includes_caps(self):
        tm = self._make_tm()
        record = tm._build_golden_record(
            "KXHIGHLAX-26FEB16-T75", "yes", 50, 1, 50,
            "test", {"order_id": "z", "status": "ok"},
            caps_applied=["cost_cap", "daily_loss"],
        )
        assert "cost_cap" in record["caps_applied"]
        assert "daily_loss" in record["caps_applied"]

    def test_limit_price_rule_from_edge(self):
        tm = self._make_tm()
        record = tm._build_golden_record(
            "KXHIGHDEN-26FEB16-T50", "no", 60, 1, 60,
            "test", {"order_id": "w", "status": "ok"},
            raw_edge=0.12,
        )
        assert record["limit_price_rule"] == "balanced"


class TestReconcileAnnotation:
    """Test _annotate_trade logic from reconcile script."""

    def test_annotation_sets_settlement_result(self):
        """Simulate annotating a trade with settlement data."""
        trade = {
            "ticker": "KXHIGHNY-26FEB16-T40",
            "side": "no",
            "order_id": "ord_123",
            "price_cents": 70,
            "model_prob": 0.25,
            "settlement_result": None,
        }
        settlements = {
            "KXHIGHNY-26FEB16-T40": {
                "revenue_cents": 30,
                "yes_won": False,
                "settled_time": "2026-02-16T23:30:00Z",
            }
        }
        fills = {
            "ord_123": {"fill_price_cents": 69, "fill_count": 1}
        }

        # Import annotate function
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from importlib import import_module
        spec = __import__("importlib").util.spec_from_file_location(
            "reconcile",
            str(Path(__file__).resolve().parent.parent / "scripts" / "reconcile-trades.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        modified = mod._annotate_trade(trade, settlements, fills)
        assert modified is True
        assert trade["settlement_result"] == "won"  # NO side, yes didn't win -> we won
        assert trade["fill_price_cents"] == 69
        assert trade["realized_edge"] is not None

    def test_annotation_idempotent(self):
        """Already-annotated trades should be skipped."""
        trade = {
            "ticker": "KXHIGHNY-26FEB16-T40",
            "settlement_result": "won",
        }
        settlements = {}
        fills = {}

        spec = __import__("importlib").util.spec_from_file_location(
            "reconcile",
            str(Path(__file__).resolve().parent.parent / "scripts" / "reconcile-trades.py"),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        modified = mod._annotate_trade(trade, settlements, fills)
        assert modified is False
