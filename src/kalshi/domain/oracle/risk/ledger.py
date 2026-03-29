"""Oracle-internal risk ledger.

Tracks open positions in memory with JSON persistence for crash recovery.
Provides per-book, per-game, and per-player exposure queries for limit checks.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from domain.oracle.models import Book, OraclePosition

_log = logging.getLogger("oracle.risk.ledger")


class OracleRiskLedger:
    """In-memory position tracker with JSON persistence."""

    def __init__(self, state_path: Path):
        self._positions: dict[str, OraclePosition] = {}  # ticker -> position
        self._daily_pnl_cents: float = 0.0
        self._cumulative_pnl_cents: float = 0.0
        self._book_pnl_cents: dict[str, float] = {}  # book name -> cumulative cents
        self._consecutive_failures: int = 0
        self._daily_date: str = ""  # YYYY-MM-DD of current daily window
        self._state_path = state_path
        self._load()

    # ── Position management ──

    def add_position(self, pos: OraclePosition) -> None:
        self._positions[pos.ticker] = pos
        self._persist()

    def remove_position(self, ticker: str) -> Optional[OraclePosition]:
        pos = self._positions.pop(ticker, None)
        if pos:
            self._persist()
        return pos

    def get_position(self, ticker: str) -> Optional[OraclePosition]:
        return self._positions.get(ticker)

    def has_position(self, ticker: str) -> bool:
        return ticker in self._positions

    # ── Exposure queries ──

    def total_exposure_cents(self) -> int:
        return sum(p.exposure_cents for p in self._positions.values())

    def book_exposure_cents(self, book: Book) -> int:
        return sum(
            p.exposure_cents for p in self._positions.values()
            if p.book == book
        )

    def game_exposure_cents(self, game_id: str) -> int:
        return sum(
            p.exposure_cents for p in self._positions.values()
            if p.game_id == game_id
        )

    def book_game_exposure_cents(self, book: Book, game_id: str) -> int:
        """Exposure for a specific book on a specific game."""
        return sum(
            p.exposure_cents for p in self._positions.values()
            if p.book == book and p.game_id == game_id
        )

    def player_exposure_cents(self, player_id: int) -> int:
        return sum(
            p.exposure_cents for p in self._positions.values()
            if p.player_id == player_id
        )

    def position_count(self) -> int:
        return len(self._positions)

    def book_position_count(self, book: Book) -> int:
        return sum(1 for p in self._positions.values() if p.book == book)

    def player_position_count(self, player_id: int) -> int:
        return sum(
            1 for p in self._positions.values()
            if p.player_id == player_id
        )

    def game_position_count(self, game_id: str) -> int:
        return sum(
            1 for p in self._positions.values()
            if p.game_id == game_id
        )

    def positions_for_book(self, book: Book) -> list[OraclePosition]:
        return [p for p in self._positions.values() if p.book == book]

    def all_positions(self) -> list[OraclePosition]:
        return list(self._positions.values())

    # ── P&L tracking ──

    @property
    def daily_pnl_cents(self) -> float:
        return self._daily_pnl_cents

    @property
    def cumulative_pnl_cents(self) -> float:
        return self._cumulative_pnl_cents

    def record_pnl(self, pnl_cents: float, book: Book | None = None) -> None:
        self._daily_pnl_cents += pnl_cents
        self._cumulative_pnl_cents += pnl_cents
        if book is not None:
            self._book_pnl_cents[book.name] = (
                self._book_pnl_cents.get(book.name, 0.0) + pnl_cents
            )
        self._persist()

    def book_pnl_cents(self, book: Book) -> float:
        """Cumulative P&L for a specific book."""
        return self._book_pnl_cents.get(book.name, 0.0)

    def reset_daily_pnl(self) -> None:
        self._daily_pnl_cents = 0.0
        self._persist()

    def maybe_reset_daily(self) -> bool:
        """Reset daily PnL if the date has changed. Returns True if reset."""
        import datetime
        today = datetime.date.today().isoformat()
        if self._daily_date and self._daily_date != today:
            self._daily_pnl_cents = 0.0
            self._daily_date = today
            self._persist()
            return True
        if not self._daily_date:
            self._daily_date = today
            self._persist()
        return False

    def settle(self, ticker: str, pnl_cents: float) -> None:
        """Settle a position: remove it and record P&L atomically."""
        pos = self._positions.pop(ticker, None)
        book = pos.book if pos else None
        self._daily_pnl_cents += pnl_cents
        self._cumulative_pnl_cents += pnl_cents
        if book is not None:
            self._book_pnl_cents[book.name] = (
                self._book_pnl_cents.get(book.name, 0.0) + pnl_cents
            )
        self._persist()

    # ── Kill switch tracking ──

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def record_order_success(self) -> None:
        self._consecutive_failures = 0
        self._persist()

    def record_order_failure(self) -> None:
        self._consecutive_failures += 1
        self._persist()

    # ── Spread side tracking (correlation guard) ──

    def has_opposite_spread_side(self, game_id: str, side: str) -> bool:
        """Check if we already have an opposite-side position on this game."""
        opposite = "no" if side == "yes" else "yes"
        for p in self._positions.values():
            if p.game_id == game_id and p.side == opposite:
                return True
        return False

    # ── Persistence ──

    def _persist(self) -> None:
        state = {
            "positions": {
                ticker: pos.to_dict()
                for ticker, pos in self._positions.items()
            },
            "daily_pnl_cents": self._daily_pnl_cents,
            "cumulative_pnl_cents": self._cumulative_pnl_cents,
            "book_pnl_cents": self._book_pnl_cents,
            "consecutive_failures": self._consecutive_failures,
            "daily_date": self._daily_date,
            "last_updated": time.time(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(self._state_path)
        except OSError as exc:
            _log.warning("Failed to persist risk ledger: %s", exc)

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            if not isinstance(data, dict):
                return
            self._daily_pnl_cents = data.get("daily_pnl_cents", 0.0)
            self._cumulative_pnl_cents = data.get("cumulative_pnl_cents", 0.0)
            self._book_pnl_cents = data.get("book_pnl_cents", {})
            self._consecutive_failures = data.get("consecutive_failures", 0)
            self._daily_date = data.get("daily_date", "")
            for ticker, pos_dict in data.get("positions", {}).items():
                try:
                    self._positions[ticker] = OraclePosition.from_dict(pos_dict)
                except (KeyError, ValueError) as exc:
                    _log.warning("Skipping corrupt position %s: %s", ticker, exc)
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Failed to load risk ledger: %s", exc)

    def to_summary(self) -> dict:
        """Summary dict for health/dashboard reporting."""
        return {
            "position_count": self.position_count(),
            "total_exposure_cents": self.total_exposure_cents(),
            "book_a_positions": self.book_position_count(Book.A),
            "book_b_positions": self.book_position_count(Book.B),
            "book_c_positions": self.book_position_count(Book.C),
            "daily_pnl_cents": self._daily_pnl_cents,
            "cumulative_pnl_cents": self._cumulative_pnl_cents,
            "book_pnl_cents": dict(self._book_pnl_cents),
            "consecutive_failures": self._consecutive_failures,
        }
