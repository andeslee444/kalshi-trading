"""Book A: Game-level price divergence.

Compares Real Sports crowd-implied win probability against Kalshi price.
Trades when the gap (edge) exceeds the configured minimum (default 15%).

Correctly handles both YES and NO sides:
- YES side: edge = model_prob - (kalshi_yes_price / 100)
- NO side:  edge = (1 - model_prob) - (kalshi_no_price / 100)
  where kalshi_no_price = 100 - kalshi_yes_price

This addresses original plan Bug #3 (incorrect NO-side pricing).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from domain.oracle.models import Book, Signal

_log = logging.getLogger("oracle.book_a")

# Track edge history per ticker for convergence detection (spec Section 3)
# If edge narrows over 3 consecutive polls, skip — the market is pricing in.
_edge_history: dict[str, list[float]] = defaultdict(list)
_CONVERGENCE_WINDOW = 3


def is_edge_converging(ticker: str, current_edge: float) -> bool:
    """Check if edge has been narrowing over the last 3 polls.

    Returns True if edge is converging (should skip), False if stable/widening.
    """
    history = _edge_history[ticker]
    history.append(abs(current_edge))

    # Keep only last N entries
    if len(history) > _CONVERGENCE_WINDOW + 1:
        _edge_history[ticker] = history[-(_CONVERGENCE_WINDOW + 1):]
        history = _edge_history[ticker]

    if len(history) < _CONVERGENCE_WINDOW:
        return False

    # Check if each successive edge is strictly smaller
    recent = history[-_CONVERGENCE_WINDOW:]
    for i in range(1, len(recent)):
        if recent[i] >= recent[i - 1]:
            return False
    return True


def reset_edge_history(ticker: str = "") -> None:
    """Clear edge history (for testing or daily reset)."""
    if ticker:
        _edge_history.pop(ticker, None)
    else:
        _edge_history.clear()


@dataclass
class DivergenceOpportunity:
    """A detected price divergence between Real and Kalshi."""
    ticker: str
    game_id: str
    side: str  # "yes" or "no"
    real_prob: float  # Real's implied probability (0-1)
    kalshi_price_cents: int  # Kalshi price in cents
    edge: float  # Absolute edge (positive = trade)
    volume: int  # Real Sports volume


def scan_divergence(
    real_prob: float,
    kalshi_yes_price_cents: int,
    ticker: str,
    game_id: str,
    real_volume: int,
    min_edge: float = 0.15,
    min_volume: int = 200000,
    game_status: str = "scheduled",
) -> Optional[DivergenceOpportunity]:
    """Scan for game-level price divergence.

    real_prob: Real Sports implied probability for the home/favored team (0-1).
    kalshi_yes_price_cents: Kalshi YES price in cents (1-99).
    ticker: Kalshi market ticker.
    game_id: Canonical game ID (e.g., "LAL-HOU-20260318").
    real_volume: Real Sports market volume.
    min_edge: Minimum edge to trigger (default 15%).
    min_volume: Minimum Real volume for signal reliability.

    Returns DivergenceOpportunity if edge found, None otherwise.
    """
    # Gate: no Book A trades on live games (defer to Book C per spec Section 3)
    if game_status in ("live", "in_progress", "Live"):
        return None

    if real_volume < min_volume:
        return None

    if kalshi_yes_price_cents <= 5 or kalshi_yes_price_cents >= 95:
        return None

    if not (0.01 <= real_prob <= 0.99):
        return None

    kalshi_yes_prob = kalshi_yes_price_cents / 100.0
    kalshi_no_prob = 1.0 - kalshi_yes_prob

    real_no_prob = 1.0 - real_prob

    # Check YES side: Real thinks more likely than Kalshi
    yes_edge = real_prob - kalshi_yes_prob
    # Check NO side: Real thinks less likely than Kalshi (buy NO)
    no_edge = real_no_prob - kalshi_no_prob

    # Pick the side with the bigger edge (only one can be positive)
    best_edge = max(yes_edge, no_edge)
    if best_edge < min_edge:
        return None

    # Edge convergence check (spec Section 3):
    # If edge has been narrowing over 3 consecutive polls, the market is
    # pricing in — skip to avoid chasing a closing gap.
    if is_edge_converging(ticker, best_edge):
        _log.debug("Edge converging for %s (%.1f%%), skipping", ticker, best_edge * 100)
        return None

    if yes_edge >= min_edge:
        return DivergenceOpportunity(
            ticker=ticker,
            game_id=game_id,
            side="yes",
            real_prob=real_prob,
            kalshi_price_cents=kalshi_yes_price_cents,
            edge=yes_edge,
            volume=real_volume,
        )

    return DivergenceOpportunity(
        ticker=ticker,
        game_id=game_id,
        side="no",
        real_prob=real_prob,
        kalshi_price_cents=100 - kalshi_yes_price_cents,
        edge=no_edge,
        volume=real_volume,
    )


def divergence_to_signal(opp: DivergenceOpportunity) -> Signal:
    """Convert a DivergenceOpportunity to a tradeable Signal."""
    model_prob = opp.real_prob if opp.side == "yes" else (1.0 - opp.real_prob)
    return Signal(
        book=Book.A,
        ticker=opp.ticker,
        side=opp.side,
        edge=opp.edge,
        model_prob=model_prob,
        kalshi_price=opp.kalshi_price_cents / 100.0,
        metadata={
            "game_id": opp.game_id,
            "real_prob": opp.real_prob,
            "real_volume": opp.volume,
            "signal_type": "game_divergence",
        },
    )


def evaluate_games(
    real_markets: list[dict],
    kalshi_markets: list[dict],
    match_fn,
    min_edge: float = 0.15,
    min_volume: int = 200000,
) -> list[DivergenceOpportunity]:
    """Evaluate all matched games for divergence opportunities.

    real_markets: Parsed Real Sports game markets (from parse_game_market_response).
    kalshi_markets: Raw Kalshi market dicts.
    match_fn: Function(real_home, real_away, game_date, kalshi_markets) -> list[dict].

    Returns list of DivergenceOpportunity (may be empty).
    """
    opportunities = []

    for real_game in real_markets:
        home_team = real_game.get("home_team", "")
        away_team = real_game.get("away_team", "")
        home_pct = real_game.get("home_pct", 0)
        volume = real_game.get("volume", 0)

        if not home_team or not away_team or home_pct <= 0:
            continue

        real_prob = home_pct / 100.0

        # Match to Kalshi markets
        matched = match_fn(home_team, away_team, kalshi_markets)

        for kalshi_market in matched:
            ticker = kalshi_market.get("ticker", "")
            yes_price = kalshi_market.get("yes_price", 0)
            game_id = real_game.get("game_id", "")

            opp = scan_divergence(
                real_prob=real_prob,
                kalshi_yes_price_cents=yes_price,
                ticker=ticker,
                game_id=str(game_id),
                real_volume=volume,
                min_edge=min_edge,
                min_volume=min_volume,
                game_status=real_game.get("status", "scheduled"),
            )
            if opp:
                opportunities.append(opp)

    return opportunities
