"""Oracle-internal limit enforcement.

Enforces ALL spec limits before trade execution. These checks happen BEFORE
calling allocator.request_budget() — the allocator provides portfolio-level
coordination; Oracle's internal limits enforce spec-level granularity.

Limit hierarchy:
1. Kill switches (immediate halt — consecutive failures, position mismatch)
2. Single-book drawdown kill switch (>10% per book)
3. Daily/cumulative stop-loss
4. Global position limits
5. Per-book limits (positions, exposure)
6. Per-market limits (positionSizePct)
7. Per-game limits (all books)
8. Per-player limits (book-aware: B=2.5%, C=1.5%)
9. Correlation guards (props/player, props/game, no opposite spread sides)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from domain.oracle.models import Book, OraclePosition
from domain.oracle.risk.ledger import OracleRiskLedger

_log = logging.getLogger("oracle.risk.limits")


@dataclass
class LimitCheckResult:
    """Result of a limit check."""
    allowed: bool
    reason: str = ""

    @staticmethod
    def ok() -> LimitCheckResult:
        return LimitCheckResult(allowed=True)

    @staticmethod
    def denied(reason: str) -> LimitCheckResult:
        return LimitCheckResult(allowed=False, reason=reason)


def check_kill_switches(
    ledger: OracleRiskLedger, config: dict,
    api_position_count: Optional[int] = None,
) -> LimitCheckResult:
    """Check kill switch conditions (immediate halt)."""
    ks = config.get("killSwitches", {})

    max_failures = ks.get("consecutiveOrderFailures", 3)
    if ledger.consecutive_failures >= max_failures:
        return LimitCheckResult.denied(
            f"Kill switch: {ledger.consecutive_failures} consecutive order failures "
            f"(threshold: {max_failures})"
        )

    # Position mismatch: ledger vs Kalshi API
    if api_position_count is not None:
        max_mismatch = ks.get("positionMismatchContracts", 2)
        mismatch = abs(ledger.position_count() - api_position_count)
        if mismatch > max_mismatch:
            return LimitCheckResult.denied(
                f"Kill switch: position mismatch {mismatch} "
                f"(ledger={ledger.position_count()}, api={api_position_count}, "
                f"threshold={max_mismatch})"
            )

    return LimitCheckResult.ok()


def check_stop_loss(
    ledger: OracleRiskLedger, bankroll_cents: int, config: dict,
) -> LimitCheckResult:
    """Check daily and cumulative stop-loss limits."""
    risk = config.get("risk", {})

    # Daily stop-loss: -5% of bankroll
    daily_stop_pct = risk.get("dailyStopLossPct", 0.05)
    daily_limit_cents = bankroll_cents * daily_stop_pct
    if ledger.daily_pnl_cents < -daily_limit_cents:
        return LimitCheckResult.denied(
            f"Daily stop-loss: P&L {ledger.daily_pnl_cents / 100:.2f} "
            f"< limit -{daily_limit_cents / 100:.2f}"
        )

    # Cumulative drawdown: -15% -> halt until manual reset
    max_dd_pct = risk.get("maxDrawdownPct", 0.15)
    dd_limit_cents = bankroll_cents * max_dd_pct
    if ledger.cumulative_pnl_cents < -dd_limit_cents:
        return LimitCheckResult.denied(
            f"Cumulative drawdown halt: P&L {ledger.cumulative_pnl_cents / 100:.2f} "
            f"< limit -{dd_limit_cents / 100:.2f}"
        )

    return LimitCheckResult.ok()


def check_global_limits(
    ledger: OracleRiskLedger, bankroll_cents: int, config: dict,
) -> LimitCheckResult:
    """Check global position and exposure limits."""
    risk = config.get("risk", {})

    max_positions = risk.get("maxSimultaneousPositions", 10)
    if ledger.position_count() >= max_positions:
        return LimitCheckResult.denied(
            f"Max positions: {ledger.position_count()} >= {max_positions}"
        )

    max_exposure_pct = risk.get("maxTotalExposurePct", 0.18)
    max_exposure_cents = bankroll_cents * max_exposure_pct
    if ledger.total_exposure_cents() >= max_exposure_cents:
        return LimitCheckResult.denied(
            f"Max exposure: {ledger.total_exposure_cents()} cents "
            f">= {max_exposure_cents:.0f} cents"
        )

    return LimitCheckResult.ok()


def check_book_limits(
    book: Book, ledger: OracleRiskLedger, bankroll_cents: int,
    config: dict,
) -> LimitCheckResult:
    """Check per-book position and exposure limits."""
    books_cfg = config.get("books", {})
    book_cfg = books_cfg.get(book.name, {})

    if not book_cfg.get("enabled", True):
        return LimitCheckResult.denied(f"Book {book.name} is disabled")

    max_positions = book_cfg.get("maxPositions", 4)
    if ledger.book_position_count(book) >= max_positions:
        return LimitCheckResult.denied(
            f"Book {book.name} max positions: "
            f"{ledger.book_position_count(book)} >= {max_positions}"
        )

    max_exposure_pct = book_cfg.get("maxExposurePct", 0.08)
    max_exposure_cents = bankroll_cents * max_exposure_pct
    if ledger.book_exposure_cents(book) >= max_exposure_cents:
        return LimitCheckResult.denied(
            f"Book {book.name} max exposure: "
            f"{ledger.book_exposure_cents(book)} cents >= {max_exposure_cents:.0f}"
        )

    return LimitCheckResult.ok()


def check_game_limits(
    book: Book, game_id: Optional[str], ledger: OracleRiskLedger,
    bankroll_cents: int, config: dict,
) -> LimitCheckResult:
    """Check per-game exposure limits (Books A, B)."""
    if not game_id:
        return LimitCheckResult.ok()

    risk = config.get("risk", {})
    max_props_per_game = risk.get("maxPropsPerGame", 4)
    if ledger.game_position_count(game_id) >= max_props_per_game:
        return LimitCheckResult.denied(
            f"Max props per game {game_id}: "
            f"{ledger.game_position_count(game_id)} >= {max_props_per_game}"
        )

    # Per-book per-game exposure (each book's limit checked independently)
    books_cfg = config.get("books", {})
    book_cfg = books_cfg.get(book.name, {})
    max_per_game_pct = book_cfg.get("maxPerGamePct")
    if max_per_game_pct is not None:
        max_per_game_cents = bankroll_cents * max_per_game_pct
        book_game_exp = ledger.book_game_exposure_cents(book, game_id)
        if book_game_exp >= max_per_game_cents:
            return LimitCheckResult.denied(
                f"Book {book.name} max per-game exposure for {game_id}: "
                f"{book_game_exp} >= {max_per_game_cents:.0f}"
            )

    return LimitCheckResult.ok()


def check_player_limits(
    book: Book, player_id: Optional[int], ledger: OracleRiskLedger,
    bankroll_cents: int, config: dict,
) -> LimitCheckResult:
    """Check per-player exposure and correlation limits.

    Spec Section 6: per-player limits vary by book:
      Book B: 2.5% per player
      Book C: 1.5% per player
    """
    if not player_id:
        return LimitCheckResult.ok()

    risk = config.get("risk", {})
    books_cfg = config.get("books", {})
    book_cfg = books_cfg.get(book.name, {})

    # Per-player exposure (book-specific, falls back to Book B default)
    max_per_player_pct = book_cfg.get("maxPerPlayerPct")
    if max_per_player_pct is not None:
        max_per_player_cents = bankroll_cents * max_per_player_pct
        if ledger.player_exposure_cents(player_id) >= max_per_player_cents:
            return LimitCheckResult.denied(
                f"Book {book.name} max per-player exposure for player {player_id}: "
                f"{ledger.player_exposure_cents(player_id)} >= {max_per_player_cents:.0f}"
            )

    max_props = risk.get("maxPropsPerPlayer", 2)
    if ledger.player_position_count(player_id) >= max_props:
        return LimitCheckResult.denied(
            f"Max props per player {player_id}: "
            f"{ledger.player_position_count(player_id)} >= {max_props}"
        )

    return LimitCheckResult.ok()


def check_per_market_limits(
    book: Book, ticker: str, ledger: OracleRiskLedger,
    bankroll_cents: int, config: dict, proposed_cost_cents: int,
) -> LimitCheckResult:
    """Check per-market (single ticker) exposure limits.

    Spec Section 6: per-market limits per book:
      Book A: 2%, Book B: 1.5%, Book C: 1%
    """
    books_cfg = config.get("books", {})
    book_cfg = books_cfg.get(book.name, {})
    # Conservative fallback: 2% if key missing (fail-safe, not fail-open)
    per_market_pct = book_cfg.get("positionSizePct", 0.02)

    max_cents = bankroll_cents * per_market_pct
    existing = ledger.get_position(ticker)
    current_exposure = existing.exposure_cents if existing else 0
    if current_exposure + proposed_cost_cents > max_cents:
        return LimitCheckResult.denied(
            f"Book {book.name} per-market limit for {ticker}: "
            f"{current_exposure + proposed_cost_cents} > {max_cents:.0f}"
        )
    return LimitCheckResult.ok()


def check_cross_book_sizing(
    book: Book, game_id: Optional[str], ledger: OracleRiskLedger,
) -> float:
    """Check for cross-book sizing reduction.

    Spec Section 6: If Book A holds a game-level position and Book B/C holds
    player props in the same game, reduce Book B/C sizing by 40%.

    Returns sizing multiplier (1.0 or 0.6).
    """
    if not game_id or book == Book.A:
        return 1.0

    # Check if Book A has any position on this game
    for pos in ledger.positions_for_book(Book.A):
        if pos.game_id == game_id:
            return 0.6  # 40% reduction per spec
    return 1.0


def check_correlation_guards(
    game_id: Optional[str], side: str,
    ledger: OracleRiskLedger, config: dict,
) -> LimitCheckResult:
    """Check correlation guards (no opposite sides on spread)."""
    risk = config.get("risk", {})

    if game_id and risk.get("noOppositeSidesOnSpread", True):
        if ledger.has_opposite_spread_side(game_id, side):
            return LimitCheckResult.denied(
                f"Correlation guard: opposite side already exists for game {game_id}"
            )

    return LimitCheckResult.ok()


def check_single_book_drawdown(
    book: Book, ledger: OracleRiskLedger,
    bankroll_cents: int, config: dict,
) -> LimitCheckResult:
    """Check single-book drawdown kill switch."""
    ks = config.get("killSwitches", {})
    max_dd_pct = ks.get("singleBookDrawdownPct", 0.10)

    book_pnl = ledger.book_pnl_cents(book)
    dd_limit_cents = bankroll_cents * max_dd_pct
    if book_pnl < -dd_limit_cents:
        return LimitCheckResult.denied(
            f"Kill switch: Book {book.name} drawdown "
            f"{book_pnl / 100:.2f} < limit -{dd_limit_cents / 100:.2f}"
        )

    return LimitCheckResult.ok()


def check_all_limits(
    book: Book,
    ledger: OracleRiskLedger,
    bankroll_cents: int,
    config: dict,
    game_id: Optional[str] = None,
    player_id: Optional[int] = None,
    side: str = "yes",
    api_position_count: Optional[int] = None,
    ticker: str = "",
    proposed_cost_cents: int = 0,
) -> LimitCheckResult:
    """Run all limit checks in priority order. Returns first failure or ok."""
    checks = [
        lambda: check_kill_switches(ledger, config, api_position_count),
        lambda: check_single_book_drawdown(book, ledger, bankroll_cents, config),
        lambda: check_stop_loss(ledger, bankroll_cents, config),
        lambda: check_global_limits(ledger, bankroll_cents, config),
        lambda: check_book_limits(book, ledger, bankroll_cents, config),
        lambda: check_per_market_limits(book, ticker, ledger, bankroll_cents, config, proposed_cost_cents),
        lambda: check_game_limits(book, game_id, ledger, bankroll_cents, config),
        lambda: check_player_limits(book, player_id, ledger, bankroll_cents, config),
        lambda: check_correlation_guards(game_id, side, ledger, config),
    ]

    for check in checks:
        result = check()
        if not result.allowed:
            return result

    return LimitCheckResult.ok()
