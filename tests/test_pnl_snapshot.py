"""Tests for pnl-snapshot verified financial summary.

Tests pure computation and verification functions — no API calls.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# Load pnl-snapshot.py (hyphenated filename) as pnl_snapshot module
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "pnl_snapshot", str(_SCRIPTS_DIR / "pnl-snapshot.py"),
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["pnl_snapshot"] = _mod
_spec.loader.exec_module(_mod)

from pnl_snapshot import (
    compute_realized_pnl,
    compute_unrealized_pnl,
    verify_settlements,
    build_snapshot,
    load_deposits,
    _build_balance_check,
    _infer_bot,
    _safe_int,
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

    def test_pnl_agreement_from_settlement_result(self):
        """P&L computed from settlement_result + cost matches API P&L."""
        # Won: payout = 100 * 4 = 400, cost = 200, P&L = +200
        local = [
            _make_local_trade(ticker="KXHIGHHOU-26MAR03-T75", cost_cents=200,
                              count=4, settlement_result="won", order_id="ord-1"),
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

    def test_pnl_agreement_losing_trade(self):
        """Lost trade: local P&L = -cost matches API P&L = 0 - cost."""
        local = [
            _make_local_trade(ticker="KXBTC-26MAR03-T95000", cost_cents=90,
                              count=1, settlement_result="lost", order_id="ord-2"),
        ]
        api = [
            _make_settlement(ticker="KXBTC-26MAR03-T95000", revenue=0,
                             yes_total_cost=90, no_total_cost=0),
        ]
        result = verify_settlements(api, local)
        pnl_check = next(c for c in result["checks"]
                         if c["check"] == "pnl_agreement")
        assert pnl_check["status"] == "ok"
        assert pnl_check["delta_cents"] == 0

    def test_pnl_ignores_settlement_revenue_cents_semantic(self):
        """P&L comparison must not depend on settlement_revenue_cents.

        backfill-settlements.py stores net profit there, while
        reconcile-trades.py stores gross payout — verify both produce
        the same P&L comparison result.
        """
        # API: revenue=100, cost=90, P&L=+10
        api = [_make_settlement(ticker="T1", revenue=100, yes_total_cost=90,
                                no_total_cost=0)]
        # backfill semantic: settlement_revenue_cents=10 (net profit)
        local_backfill = [
            _make_local_trade(ticker="T1", cost_cents=90, count=1,
                              settlement_result="won",
                              settlement_revenue_cents=10, order_id="o1"),
        ]
        # reconcile semantic: settlement_revenue_cents=100 (gross payout)
        local_reconcile = [
            _make_local_trade(ticker="T1", cost_cents=90, count=1,
                              settlement_result="won",
                              settlement_revenue_cents=100, order_id="o1"),
        ]
        # Both should produce delta=0 since we derive from settlement_result
        for local in [local_backfill, local_reconcile]:
            result = verify_settlements(api, local)
            pnl_check = next(c for c in result["checks"]
                             if c["check"] == "pnl_agreement")
            assert pnl_check["delta_cents"] == 0, (
                f"P&L comparison should not depend on settlement_revenue_cents"
            )

    def test_pnl_disagreement_flagged(self):
        """Mismatch between API cost and local cost is flagged."""
        # API says cost=200 but local says cost=180 (different fill price)
        local = [
            _make_local_trade(ticker="KXHIGHHOU-26MAR03-T75", cost_cents=180,
                              count=4, settlement_result="won", order_id="ord-1"),
        ]
        api = [
            _make_settlement(ticker="KXHIGHHOU-26MAR03-T75", revenue=400,
                             yes_total_cost=200, no_total_cost=0),
        ]
        result = verify_settlements(api, local)
        pnl_check = next(c for c in result["checks"]
                         if c["check"] == "pnl_agreement")
        assert pnl_check["status"] == "warning"
        assert pnl_check["delta_cents"] == 20  # API: +200, local: +220

    def test_pnl_multiple_trades_same_ticker(self):
        """Multiple local trades for same ticker are summed correctly."""
        local = [
            _make_local_trade(ticker="T1", cost_cents=50, count=1,
                              settlement_result="won", order_id="o1"),
            _make_local_trade(ticker="T1", cost_cents=60, count=1,
                              settlement_result="won", order_id="o2"),
        ]
        # API: revenue=200 (2 contracts), total_cost=110
        api = [_make_settlement(ticker="T1", revenue=200,
                                yes_total_cost=110, no_total_cost=0)]
        result = verify_settlements(api, local)
        pnl_check = next(c for c in result["checks"]
                         if c["check"] == "pnl_agreement")
        # API P&L: 200 - 110 = 90
        # Local P&L: (100-50) + (100-60) = 50 + 40 = 90
        assert pnl_check["delta_cents"] == 0

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

    def test_net_funded_with_withdrawals(self, tmp_path):
        """net_funded = deposits - withdrawals, used for ROI."""
        import json
        deposits_file = tmp_path / "deposits.json"
        deposits_file.write_text(json.dumps([
            {"date": "2026-02-15", "type": "deposit", "amount_cents": 50000},
            {"date": "2026-03-01", "type": "deposit", "amount_cents": 10000},
            {"date": "2026-03-05", "type": "withdrawal", "amount_cents": 20000},
        ]))
        result = load_deposits(deposits_file)
        assert result["net_funded_cents"] == 40000  # 60000 - 20000


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

    def test_by_bot_attribution(self):
        snapshot = build_snapshot(
            balance_cents=0, portfolio_value_cents=0,
            settlements=SAMPLE_SETTLEMENTS, fills=[], positions=[],
            local_trades=SAMPLE_LOCAL_TRADES, deposits_path=None,
        )
        by_bot = snapshot["realized_pnl"]["by_bot"]
        assert "weather" in by_bot
        assert by_bot["weather"]["pnl_cents"] == 200  # 400 - 200
        assert by_bot["crypto"]["pnl_cents"] == -90   # 0 - 90
        assert by_bot["economics"]["pnl_cents"] == 270  # 300 - 30

    def test_roi_uses_nav_based_total_pnl(self, tmp_path):
        """ROI = (NAV - deposits) / deposits, NOT realized / deposits."""
        import json
        deposits_file = tmp_path / "deposits.json"
        deposits_file.write_text(json.dumps([
            {"date": "2026-02-15", "type": "deposit", "amount_cents": 50000},
        ]))
        snapshot = build_snapshot(
            balance_cents=48000, portfolio_value_cents=1500,
            settlements=SAMPLE_SETTLEMENTS, fills=[], positions=[],
            local_trades=SAMPLE_LOCAL_TRADES, deposits_path=deposits_file,
        )
        # NAV = 48000 + 1500 = 49500, deposits = 50000
        # true_total_pnl = 49500 - 50000 = -500
        # ROI = -500 / 50000 * 100 = -1.0%
        assert snapshot["deposits"]["tracked"] is True
        assert snapshot["deposits"]["roi_pct"] == pytest.approx(-1.0, abs=0.01)

    def test_balance_check_section_present(self, tmp_path):
        import json
        deposits_file = tmp_path / "deposits.json"
        deposits_file.write_text(json.dumps([
            {"date": "2026-02-15", "type": "deposit", "amount_cents": 50000},
        ]))
        snapshot = build_snapshot(
            balance_cents=48000, portfolio_value_cents=1500,
            settlements=SAMPLE_SETTLEMENTS, fills=[], positions=[],
            local_trades=SAMPLE_LOCAL_TRADES, deposits_path=deposits_file,
        )
        bc = snapshot["balance_check"]
        assert bc["available"] is True
        assert bc["nav_cents"] == 49500
        assert bc["net_funded_cents"] == 50000
        assert bc["true_total_pnl_cents"] == -500
        # realized_net = 380 - 7 = 373
        assert bc["realized_net_cents"] == 373
        # implied_unrealized = -500 - 373 = -873
        assert bc["implied_unrealized_cents"] == -873

    def test_balance_check_unavailable_without_deposits(self):
        snapshot = build_snapshot(
            balance_cents=48000, portfolio_value_cents=1500,
            settlements=[], fills=[], positions=[],
            local_trades=[], deposits_path=None,
        )
        bc = snapshot["balance_check"]
        assert bc["available"] is False


# ── Tests: _build_balance_check ──

class TestBuildBalanceCheck:
    def test_basic_balance_check(self):
        realized = {"net_after_fees_cents": 500}
        deposits = {"tracked": True, "net_funded_cents": 10000}
        result = _build_balance_check(nav_cents=10200, realized=realized, deposits=deposits)
        assert result["available"] is True
        assert result["true_total_pnl_cents"] == 200  # 10200 - 10000
        assert result["implied_unrealized_cents"] == -300  # 200 - 500

    def test_negative_pnl(self):
        realized = {"net_after_fees_cents": 100}
        deposits = {"tracked": True, "net_funded_cents": 50000}
        result = _build_balance_check(nav_cents=49800, realized=realized, deposits=deposits)
        assert result["true_total_pnl_cents"] == -200
        assert result["implied_unrealized_cents"] == -300  # -200 - 100

    def test_deposits_not_tracked(self):
        result = _build_balance_check(nav_cents=5000, realized={}, deposits={"tracked": False})
        assert result["available"] is False

    def test_all_fields_present(self):
        realized = {"net_after_fees_cents": 0}
        deposits = {"tracked": True, "net_funded_cents": 5000}
        result = _build_balance_check(nav_cents=5000, realized=realized, deposits=deposits)
        assert "nav_cents" in result
        assert "net_funded_cents" in result
        assert "true_total_pnl_cents" in result
        assert "realized_net_cents" in result
        assert "implied_unrealized_cents" in result


# ── Tests: helper functions ──

class TestInferBot:
    def test_weather(self):
        assert _infer_bot("KXHIGHHOU-26MAR03-T75") == "weather"

    def test_crypto_btc(self):
        assert _infer_bot("KXBTC-26MAR03-T95000") == "crypto"

    def test_crypto_eth(self):
        assert _infer_bot("KXETH-26MAR03-T3500") == "crypto"

    def test_economics(self):
        assert _infer_bot("KXCPI-26MAY-T20") == "economics"

    def test_economics_econstat_cpi(self):
        assert _infer_bot("KXECONSTATCPIYOY-26JUN-T2.0") == "economics"

    def test_economics_econstat_core(self):
        assert _infer_bot("KXECONSTATCORECPIYOY-26JUN-T2.3") == "economics"

    def test_entertainment(self):
        assert _infer_bot("KXALBUM-DRAKE-100K") == "entertainment"

    def test_entertainment_albumsales(self):
        assert _infer_bot("KXALBUMSALES-LUC-15000") == "entertainment"

    def test_other(self):
        assert _infer_bot("KXSPORTS-NCAAM") == "other"


class TestSafeInt:
    def test_normal_int(self):
        assert _safe_int(42) == 42

    def test_string_int(self):
        assert _safe_int("100") == 100

    def test_none(self):
        assert _safe_int(None) == 0

    def test_bad_string(self):
        assert _safe_int("abc") == 0
