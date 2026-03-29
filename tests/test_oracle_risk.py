"""Tests for Oracle risk: ledger, limits, fees, sizing."""

import json
import tempfile
from pathlib import Path

import pytest

from domain.oracle.models import Book, OraclePosition
from domain.oracle.risk.fees import (
    kalshi_fee_on_profit,
    net_profit_cents,
    expected_value_cents,
)
from domain.oracle.risk.sizing import (
    compute_contracts,
    compute_risk_cents,
    contracts_for_book,
    BOOK_SIZE_PCT,
)
from domain.oracle.risk.ledger import OracleRiskLedger
from domain.oracle.risk.internal_limits import (
    check_kill_switches,
    check_stop_loss,
    check_global_limits,
    check_book_limits,
    check_game_limits,
    check_player_limits,
    check_per_market_limits,
    check_cross_book_sizing,
    check_correlation_guards,
    check_single_book_drawdown,
    check_all_limits,
    LimitCheckResult,
)


# ── Fees ──

class TestFees:
    def test_fee_on_positive_profit(self):
        assert abs(kalshi_fee_on_profit(100) - 7.0) < 0.01  # 7% of 100

    def test_fee_on_zero_profit(self):
        assert kalshi_fee_on_profit(0) == 0.0

    def test_fee_on_negative_profit(self):
        assert kalshi_fee_on_profit(-50) == 0.0

    def test_net_profit_winning_trade(self):
        # Buy at 40c, win: payout=100, cost=40, gross=60, fee=4.2, net=55.8
        net = net_profit_cents(100, 40)
        assert abs(net - 55.8) < 0.01

    def test_net_profit_losing_trade(self):
        # Lose: payout=0, cost=40, gross=-40, no fee
        net = net_profit_cents(0, 40)
        assert net == -40

    def test_expected_value_positive_edge(self):
        # 70% model prob, 50c price: should be positive EV
        ev = expected_value_cents(0.70, 50, contracts=1)
        assert ev > 0

    def test_expected_value_no_edge(self):
        # 50% model prob, 50c price: negative EV (fees)
        ev = expected_value_cents(0.50, 50, contracts=1)
        assert ev < 0  # fees drag it negative


# ── Sizing ──

class TestSizing:
    def test_compute_contracts_basic(self):
        # $100 bankroll, 2% position, 50c price -> budget=$2 -> 4 contracts
        contracts = compute_contracts(10000, 0.02, 50)
        assert contracts == 4

    def test_compute_contracts_zero_bankroll(self):
        assert compute_contracts(0, 0.02, 50) == 0

    def test_compute_contracts_zero_price(self):
        assert compute_contracts(10000, 0.02, 0) == 0

    def test_compute_contracts_price_100(self):
        assert compute_contracts(10000, 0.02, 100) == 0

    def test_compute_contracts_max_cap(self):
        # Huge bankroll, small price -> capped at max_contracts
        contracts = compute_contracts(10000000, 0.10, 1, max_contracts=50)
        assert contracts == 50

    def test_compute_risk_cents(self):
        assert compute_risk_cents(10, 50) == 500

    def test_contracts_for_book_a(self):
        # Book A = 2%: $50 bankroll at 25c -> budget=$1 -> 4 contracts
        assert contracts_for_book("A", 5000, 25) == 4

    def test_contracts_for_book_b(self):
        # Book B = 1.5%: $50 bankroll at 25c -> budget=$0.75 -> 3 contracts
        assert contracts_for_book("B", 5000, 25) == 3

    def test_contracts_for_book_c(self):
        # Book C = 1%: $50 bankroll at 25c -> budget=$0.50 -> 2 contracts
        assert contracts_for_book("C", 5000, 25) == 2

    def test_book_size_pct_values(self):
        assert BOOK_SIZE_PCT["A"] == 0.02
        assert BOOK_SIZE_PCT["B"] == 0.015
        assert BOOK_SIZE_PCT["C"] == 0.01


# ── Ledger ──

def _make_ledger(tmpdir):
    path = Path(tmpdir) / "risk-state.json"
    return OracleRiskLedger(path)


def _make_pos(book=Book.A, ticker="T1", contracts=10, price=50,
              game_id=None, player_id=None):
    return OraclePosition(
        book=book, ticker=ticker, side="yes",
        contracts=contracts, entry_price_cents=price,
        game_id=game_id, player_id=player_id,
    )


class TestLedger:
    def test_add_and_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(ticker="T1"))
            assert ledger.position_count() == 1
            assert ledger.has_position("T1")
            assert ledger.total_exposure_cents() == 500  # 10 * 50

    def test_remove_position(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(ticker="T1"))
            removed = ledger.remove_position("T1")
            assert removed is not None
            assert ledger.position_count() == 0

    def test_book_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(book=Book.A, ticker="T1"))
            ledger.add_position(_make_pos(book=Book.B, ticker="T2"))
            assert ledger.book_exposure_cents(Book.A) == 500
            assert ledger.book_exposure_cents(Book.B) == 500
            assert ledger.book_exposure_cents(Book.C) == 0

    def test_game_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(ticker="T1", game_id="G1"))
            ledger.add_position(_make_pos(ticker="T2", game_id="G1"))
            ledger.add_position(_make_pos(ticker="T3", game_id="G2"))
            assert ledger.game_exposure_cents("G1") == 1000
            assert ledger.game_exposure_cents("G2") == 500

    def test_player_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(ticker="T1", player_id=42))
            ledger.add_position(_make_pos(ticker="T2", player_id=42))
            assert ledger.player_exposure_cents(42) == 1000
            assert ledger.player_exposure_cents(99) == 0

    def test_pnl_tracking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(100)
            ledger.record_pnl(-50)
            assert ledger.daily_pnl_cents == 50
            assert ledger.cumulative_pnl_cents == 50

    def test_daily_pnl_reset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(100)
            ledger.reset_daily_pnl()
            assert ledger.daily_pnl_cents == 0
            assert ledger.cumulative_pnl_cents == 100  # cumulative persists

    def test_settle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(ticker="T1"))
            ledger.settle("T1", pnl_cents=200)
            assert not ledger.has_position("T1")
            assert ledger.daily_pnl_cents == 200

    def test_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_order_failure()
            ledger.record_order_failure()
            assert ledger.consecutive_failures == 2
            ledger.record_order_success()
            assert ledger.consecutive_failures == 0

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk-state.json"
            ledger1 = OracleRiskLedger(path)
            ledger1.add_position(_make_pos(ticker="T1", game_id="G1"))
            ledger1.record_pnl(150)
            ledger1.record_order_failure()

            # Load fresh
            ledger2 = OracleRiskLedger(path)
            assert ledger2.position_count() == 1
            assert ledger2.has_position("T1")
            assert ledger2.daily_pnl_cents == 150
            assert ledger2.consecutive_failures == 1

    def test_opposite_spread_side(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            pos = OraclePosition(
                book=Book.A, ticker="T1", side="yes",
                contracts=5, entry_price_cents=50, game_id="G1",
            )
            ledger.add_position(pos)
            assert ledger.has_opposite_spread_side("G1", "no")
            assert not ledger.has_opposite_spread_side("G1", "yes")

    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(book=Book.A, ticker="T1"))
            ledger.add_position(_make_pos(book=Book.B, ticker="T2"))
            summary = ledger.to_summary()
            assert summary["position_count"] == 2
            assert summary["book_a_positions"] == 1
            assert summary["book_b_positions"] == 1


# ── Internal Limits ──

ORACLE_CONFIG = {
    "risk": {
        "maxTotalExposurePct": 0.18,
        "maxSimultaneousPositions": 10,
        "dailyStopLossPct": 0.05,
        "maxDrawdownPct": 0.15,
        "maxPropsPerPlayer": 2,
        "maxPropsPerGame": 4,
        "noOppositeSidesOnSpread": True,
    },
    "books": {
        "A": {"enabled": True, "maxPositions": 4, "maxExposurePct": 0.08,
               "maxPerGamePct": 0.04, "positionSizePct": 0.02},
        "B": {"enabled": True, "maxPositions": 6, "maxExposurePct": 0.09,
               "maxPerGamePct": 0.04, "maxPerPlayerPct": 0.025, "positionSizePct": 0.015},
        "C": {"enabled": True, "maxPositions": 3, "maxExposurePct": 0.03,
               "maxPerGamePct": 0.02, "maxPerPlayerPct": 0.015, "positionSizePct": 0.01},
    },
    "killSwitches": {
        "consecutiveOrderFailures": 3,
        "singleBookDrawdownPct": 0.10,
    },
}

BANKROLL = 500000  # $5,000 in cents


class TestInternalLimits:
    def test_kill_switch_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for _ in range(3):
                ledger.record_order_failure()
            result = check_kill_switches(ledger, ORACLE_CONFIG)
            assert not result.allowed
            assert "consecutive order failures" in result.reason

    def test_kill_switch_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            result = check_kill_switches(ledger, ORACLE_CONFIG)
            assert result.allowed

    def test_daily_stop_loss(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(-26000)  # -$260, exceeds 5% of $5K = $250
            result = check_stop_loss(ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed
            assert "Daily stop-loss" in result.reason

    def test_cumulative_drawdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(-80000)  # -$800, exceeds 15% of $5K = $750
            result = check_stop_loss(ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed

    def test_max_positions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for i in range(10):
                ledger.add_position(_make_pos(ticker=f"T{i}", contracts=1, price=10))
            result = check_global_limits(ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed
            assert "Max positions" in result.reason

    def test_max_total_exposure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            # 18% of $5K = $900 = 90000 cents
            ledger.add_position(_make_pos(ticker="T1", contracts=100, price=950))
            result = check_global_limits(ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed
            assert "Max exposure" in result.reason

    def test_book_position_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for i in range(4):
                ledger.add_position(_make_pos(
                    book=Book.A, ticker=f"A{i}", contracts=1, price=10,
                ))
            result = check_book_limits(Book.A, ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed

    def test_book_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            config = {**ORACLE_CONFIG, "books": {"A": {"enabled": False}}}
            result = check_book_limits(Book.A, ledger, BANKROLL, config)
            assert not result.allowed
            assert "disabled" in result.reason

    def test_per_game_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for i in range(4):
                ledger.add_position(_make_pos(
                    ticker=f"G{i}", game_id="G1", contracts=1, price=10,
                ))
            result = check_game_limits(Book.A, "G1", ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed

    def test_per_game_limit_per_book_isolation(self):
        """Book B exposure on a game should not block Book A on the same game."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            # Book B has 3% of bankroll on game G1 (15000 / 500000)
            ledger.add_position(_make_pos(
                book=Book.B, ticker="B1", game_id="G1",
                contracts=30, price=50,  # 1500 cents exposure
            ))
            # Book A has no exposure on G1 -> should pass its 4% per-game limit
            result = check_game_limits(Book.A, "G1", ledger, BANKROLL, ORACLE_CONFIG)
            assert result.allowed

    def test_per_player_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for i in range(2):
                ledger.add_position(_make_pos(
                    book=Book.B, ticker=f"P{i}", player_id=42,
                    contracts=1, price=10,
                ))
            result = check_player_limits(Book.B, 42, ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed

    def test_correlation_guard_opposite_side(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            pos = OraclePosition(
                book=Book.A, ticker="T1", side="yes",
                contracts=5, entry_price_cents=50, game_id="G1",
            )
            ledger.add_position(pos)
            result = check_correlation_guards("G1", "no", ledger, ORACLE_CONFIG)
            assert not result.allowed

    def test_check_all_limits_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            result = check_all_limits(
                Book.A, ledger, BANKROLL, ORACLE_CONFIG,
                game_id="G1", side="yes",
            )
            assert result.allowed

    def test_check_all_limits_stops_at_first_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for _ in range(3):
                ledger.record_order_failure()
            result = check_all_limits(
                Book.A, ledger, BANKROLL, ORACLE_CONFIG,
                game_id="G1", side="yes",
            )
            assert not result.allowed
            assert "Kill switch" in result.reason

    def test_no_game_id_skips_game_limits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            result = check_game_limits(Book.A, None, ledger, BANKROLL, ORACLE_CONFIG)
            assert result.allowed

    def test_no_player_id_skips_player_limits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            result = check_player_limits(Book.B, None, ledger, BANKROLL, ORACLE_CONFIG)
            assert result.allowed

    def test_single_book_drawdown_triggers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            # Book A loses >10% of $5K = $500 = 50000 cents
            ledger.record_pnl(-55000, book=Book.A)
            result = check_single_book_drawdown(Book.A, ledger, BANKROLL, ORACLE_CONFIG)
            assert not result.allowed
            assert "Book A drawdown" in result.reason

    def test_single_book_drawdown_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(-10000, book=Book.A)  # -$100, under 10% of $5K
            result = check_single_book_drawdown(Book.A, ledger, BANKROLL, ORACLE_CONFIG)
            assert result.allowed

    def test_single_book_drawdown_other_book_unaffected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(-55000, book=Book.A)  # Book A in drawdown
            result = check_single_book_drawdown(Book.B, ledger, BANKROLL, ORACLE_CONFIG)
            assert result.allowed  # Book B is fine

    def test_position_mismatch_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for i in range(5):
                ledger.add_position(_make_pos(ticker=f"T{i}"))
            # API says only 1 position — mismatch of 4 > threshold 2
            result = check_kill_switches(ledger, ORACLE_CONFIG, api_position_count=1)
            assert not result.allowed
            assert "position mismatch" in result.reason

    def test_position_mismatch_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for i in range(3):
                ledger.add_position(_make_pos(ticker=f"T{i}"))
            # API says 2 — mismatch of 1, within threshold of 2
            result = check_kill_switches(ledger, ORACLE_CONFIG, api_position_count=2)
            assert result.allowed

    def test_position_mismatch_skipped_when_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            for i in range(5):
                ledger.add_position(_make_pos(ticker=f"T{i}"))
            # No API count provided — skip mismatch check
            result = check_kill_switches(ledger, ORACLE_CONFIG, api_position_count=None)
            assert result.allowed


class TestLedgerBookPnl:
    def test_book_pnl_tracking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(500, book=Book.A)
            ledger.record_pnl(-200, book=Book.B)
            ledger.record_pnl(100, book=Book.A)
            assert ledger.book_pnl_cents(Book.A) == 600
            assert ledger.book_pnl_cents(Book.B) == -200
            assert ledger.book_pnl_cents(Book.C) == 0
            assert ledger.daily_pnl_cents == 400  # aggregate still works

    def test_book_pnl_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "risk-state.json"
            ledger1 = OracleRiskLedger(path)
            ledger1.record_pnl(-300, book=Book.C)
            ledger2 = OracleRiskLedger(path)
            assert ledger2.book_pnl_cents(Book.C) == -300

    def test_settle_records_book_pnl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(book=Book.A, ticker="T1"))
            ledger.settle("T1", pnl_cents=200)
            assert ledger.book_pnl_cents(Book.A) == 200

    def test_book_game_exposure(self):
        """book_game_exposure_cents filters by both book and game."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(book=Book.A, ticker="T1", game_id="G1"))
            ledger.add_position(_make_pos(book=Book.B, ticker="T2", game_id="G1"))
            # Aggregate: 1000, per-book: 500 each
            assert ledger.game_exposure_cents("G1") == 1000
            assert ledger.book_game_exposure_cents(Book.A, "G1") == 500
            assert ledger.book_game_exposure_cents(Book.B, "G1") == 500
            assert ledger.book_game_exposure_cents(Book.C, "G1") == 0

    def test_daily_reset_via_maybe_reset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.record_pnl(500)
            # Force a stale date
            ledger._daily_date = "2020-01-01"
            ledger._persist()
            assert ledger.maybe_reset_daily()
            assert ledger.daily_pnl_cents == 0
            assert ledger.cumulative_pnl_cents == 500  # cumulative preserved


class TestPerMarketLimits:
    def test_per_market_blocks_when_over_limit(self):
        """Per-market limit denies when existing + proposed exceeds positionSizePct."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            # Book A positionSizePct=0.02, bankroll=$5K -> max=10000c
            # Add existing position worth 8000c
            ledger.add_position(_make_pos(
                book=Book.A, ticker="KXNBA-T1", contracts=160, price=50,
            ))
            # Propose another 3000c -> total 11000c > 10000c limit
            result = check_per_market_limits(
                Book.A, "KXNBA-T1", ledger, BANKROLL, ORACLE_CONFIG, 3000,
            )
            assert not result.allowed
            assert "per-market" in result.reason

    def test_per_market_allows_under_limit(self):
        """Per-market limit allows when existing + proposed is under positionSizePct."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            # No existing position, propose 5000c < 10000c limit
            result = check_per_market_limits(
                Book.A, "KXNBA-T1", ledger, BANKROLL, ORACLE_CONFIG, 5000,
            )
            assert result.allowed

    def test_per_market_uses_safe_default_when_no_config(self):
        """Per-market check uses 2% fallback when positionSizePct is missing (fail-safe)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            config = {"books": {"A": {}}}
            # 2% of $5K = 10000c. Proposing 99999c should be denied with fallback.
            result = check_per_market_limits(
                Book.A, "KXNBA-T1", ledger, BANKROLL, config, 99999,
            )
            assert not result.allowed  # fail-safe: denies with conservative default


class TestCrossBookSizing:
    def test_cross_book_reduction_when_book_a_holds_game(self):
        """Book B/C gets 40% reduction when Book A has position on same game."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(
                book=Book.A, ticker="KXNBA-A1", game_id="G1",
            ))
            mult = check_cross_book_sizing(Book.B, "G1", ledger)
            assert mult == 0.6

    def test_no_reduction_for_book_a(self):
        """Book A never gets cross-book reduction (it IS Book A)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(
                book=Book.A, ticker="KXNBA-A1", game_id="G1",
            ))
            mult = check_cross_book_sizing(Book.A, "G1", ledger)
            assert mult == 1.0

    def test_no_reduction_when_no_game_id(self):
        """No reduction when game_id is None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            mult = check_cross_book_sizing(Book.B, None, ledger)
            assert mult == 1.0

    def test_no_reduction_when_book_a_has_different_game(self):
        """No reduction when Book A position is on a different game."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(
                book=Book.A, ticker="KXNBA-A1", game_id="G2",
            ))
            mult = check_cross_book_sizing(Book.B, "G1", ledger)
            assert mult == 1.0

    def test_cross_book_reduction_for_book_c(self):
        """Book C also gets the 40% reduction when Book A holds same game."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _make_ledger(tmpdir)
            ledger.add_position(_make_pos(
                book=Book.A, ticker="KXNBA-A1", game_id="G1",
            ))
            mult = check_cross_book_sizing(Book.C, "G1", ledger)
            assert mult == 0.6
