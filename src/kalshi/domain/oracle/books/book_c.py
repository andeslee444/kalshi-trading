"""Book C: Live event signals.

Three discrete signals from in-game events:
1. Foul trouble: SELL over if player has 4th foul before Q4 and is below line
2. OT likely: BUY over if tied with <2min in Q4, player projected to clear with OT
3. Blowout: SELL over if 20+ pt differential in Q3+, player below line (garbage time)
4. Clutch comeback: SELL the trailing team late when comeback odds are overpriced

Each signal requires orderbook quote quality check before execution.
Signals are derived from WebSocket events (GameUpdated, PlayerBoxScoreUpdated).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from domain.oracle.models import Book, GameState, Signal

_log = logging.getLogger("oracle.book_c")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class LiveSignal:
    """A detected live event trading signal."""
    signal_type: str  # "foul_trouble", "ot_likely", "blowout", "clutch_comeback"
    ticker: str
    side: str  # "yes" (buy over) or "no" (sell over / buy under)
    edge: float
    model_prob: float
    kalshi_price_cents: int
    player_name: str = ""
    game_id: Optional[str] = None
    player_id: Optional[int] = None
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


def detect_foul_trouble(
    fouls: int,
    period: str,
    clock_seconds: int,
    current_stat: float,
    line: float,
    kalshi_price_cents: int,
    ticker: str = "",
    player_name: str = "",
    game_id: Optional[str] = None,
    player_id: Optional[int] = None,
    min_edge: float = 0.12,
) -> Optional[LiveSignal]:
    """Detect foul trouble signal.

    SELL over (buy NO) if:
    - Player has 4+ fouls before Q4
    - Player is currently below the line
    - Expected minutes reduction reduces hit probability

    The logic: 4 fouls before Q4 means the player will likely sit for extended
    minutes in Q4 to avoid fouling out, reducing their counting stats.
    """
    # Only trigger for 4+ fouls before Q4
    if fouls < 4:
        return None
    if period not in ("Q1", "Q2", "Q3"):
        return None
    # Player must be below line (otherwise foul trouble helps under)
    if current_stat >= line:
        return None

    if kalshi_price_cents <= 5 or kalshi_price_cents >= 95:
        return None

    # Estimate reduced probability
    # With 4+ fouls pre-Q4, player loses ~8-12 minutes
    # This cuts counting stats by roughly 25-35%
    minutes_reduction_factor = 0.70  # ~30% reduction in remaining production
    projected_final = current_stat + (line - current_stat) * minutes_reduction_factor

    # If projected final is still below line, sell over
    if projected_final >= line:
        return None

    # Model probability of clearing line drops significantly
    # Rough estimate: probability drops to 20-30% range
    model_prob_over = 0.25  # conservative estimate for foul trouble
    kalshi_implied = kalshi_price_cents / 100.0

    # We're selling over (buying NO), so edge is on the under side
    model_prob_under = 1.0 - model_prob_over
    kalshi_no_implied = 1.0 - kalshi_implied
    edge = model_prob_under - kalshi_no_implied

    if edge < min_edge:
        return None

    return LiveSignal(
        signal_type="foul_trouble",
        ticker=ticker,
        side="no",  # Sell over = buy NO
        edge=edge,
        model_prob=model_prob_under,
        kalshi_price_cents=100 - kalshi_price_cents,
        player_name=player_name,
        game_id=game_id,
        player_id=player_id,
        metadata={
            "fouls": fouls,
            "period": period,
            "clock_seconds": clock_seconds,
            "current_stat": current_stat,
            "line": line,
            "projected_final": projected_final,
            "minutes_reduction_factor": minutes_reduction_factor,
        },
    )


def detect_ot_likely(
    margin: int,
    period: str,
    clock_seconds: int,
    current_stat: float,
    per_min_rate: float,
    line: float,
    kalshi_price_cents: int,
    ticker: str = "",
    player_name: str = "",
    game_id: Optional[str] = None,
    player_id: Optional[int] = None,
    min_edge: float = 0.12,
) -> Optional[LiveSignal]:
    """Detect overtime-likely signal.

    BUY over if:
    - Game is tied or within 2 points with <2min in Q4
    - Player is below line but projected to clear with OT minutes
    - OT = extra 5 minutes of production

    The logic: OT adds ~5 minutes of play. If player is close to the line,
    the extra minutes make the over more likely than the market prices.
    """
    game_state = GameState.classify(margin, period, clock_seconds)
    if game_state != GameState.OT_LIKELY:
        return None

    # Player should be below but close to line
    gap = line - current_stat
    if gap <= 0:
        return None  # already over
    if gap > line * 0.3:
        return None  # too far below to catch up even with OT

    if kalshi_price_cents <= 5 or kalshi_price_cents >= 95:
        return None

    # Project additional stats from remaining time + OT
    remaining_reg_minutes = clock_seconds / 60.0
    ot_minutes = 5.0
    projected_additional_reg = per_min_rate * remaining_reg_minutes
    projected_additional_ot = per_min_rate * (remaining_reg_minutes + ot_minutes)
    projected_final_reg = current_stat + projected_additional_reg
    projected_final_ot = current_stat + projected_additional_ot

    if projected_final_ot <= line * 1.05:
        return None  # can't clear even with OT (5% safety margin per spec Section 5)

    # Dynamic OT probability based on game state:
    #   - margin=0, clock<=30s: ~75% OT
    #   - margin=0, clock=120s: ~45% OT
    #   - margin=1-2, clock<=30s: ~35% OT
    #   - margin=1-2, clock=120s: ~20% OT
    # Linear decay with clock, step down for margin>0
    margin_factor = 1.0 if abs(margin) == 0 else 0.55
    clock_factor = max(0.3, 1.0 - (clock_seconds / 200.0))  # 0.3 at 2min, 1.0 at 0s
    ot_prob = min(0.85, margin_factor * clock_factor * 0.80)

    # Over probability conditioned on OT: based on projected distance to line
    ot_surplus = (projected_final_ot - line) / max(line, 1.0)
    over_prob_with_ot = min(0.90, max(0.40, 0.50 + ot_surplus * 2.0))

    # Over probability without OT: based on regulation projection
    reg_surplus = (projected_final_reg - line) / max(line, 1.0)
    over_prob_no_ot = min(0.80, max(0.10, 0.30 + reg_surplus * 2.0))

    model_prob = ot_prob * over_prob_with_ot + (1 - ot_prob) * over_prob_no_ot

    kalshi_implied = kalshi_price_cents / 100.0
    edge = model_prob - kalshi_implied

    if edge < min_edge:
        return None

    return LiveSignal(
        signal_type="ot_likely",
        ticker=ticker,
        side="yes",  # Buy over
        edge=edge,
        model_prob=model_prob,
        kalshi_price_cents=kalshi_price_cents,
        player_name=player_name,
        game_id=game_id,
        player_id=player_id,
        metadata={
            "margin": margin,
            "period": period,
            "clock_seconds": clock_seconds,
            "current_stat": current_stat,
            "line": line,
            "per_min_rate": per_min_rate,
            "projected_final_reg": projected_final_reg,
            "projected_final_ot": projected_final_ot,
            "ot_probability": round(ot_prob, 3),
            "over_prob_with_ot": round(over_prob_with_ot, 3),
            "over_prob_no_ot": round(over_prob_no_ot, 3),
        },
    )


def detect_blowout(
    margin: int,
    period: str,
    clock_seconds: int,
    current_stat: float,
    line: float,
    kalshi_price_cents: int,
    ticker: str = "",
    player_name: str = "",
    game_id: Optional[str] = None,
    player_id: Optional[int] = None,
    min_edge: float = 0.12,
    minutes_remaining: float = 0.0,
) -> Optional[LiveSignal]:
    """Detect blowout signal.

    SELL over (buy NO) if:
    - Score differential >= 20 points in Q3 or later
    - Player is currently below the line
    - Starters likely to be pulled early (garbage time)

    The logic: In blowouts, starters get pulled midway through Q4 (sometimes Q3).
    This cuts their remaining minutes and makes over less likely.
    """
    game_state = GameState.classify(margin, period, clock_seconds)
    if game_state != GameState.BLOWOUT:
        return None

    # Player must be below line
    if current_stat >= line:
        return None

    # Spec validation: projected remaining minutes < 10 in blowout.
    # In a blowout, starters lose ~50% of remaining regulation minutes.
    # Estimate from clock if caller doesn't provide explicit value.
    if minutes_remaining <= 0:
        # Derive from game clock: remaining regulation minutes * blowout reduction
        reg_minutes_left = clock_seconds / 60.0
        if period == "Q3":
            reg_minutes_left += 12.0  # Q4 still to play
        minutes_remaining = reg_minutes_left * 0.5  # starters play ~half in blowout
    if minutes_remaining > 10:
        return None

    if kalshi_price_cents <= 5 or kalshi_price_cents >= 95:
        return None

    # In a blowout, starters lose ~50% of remaining minutes
    # Model over probability drops significantly
    model_prob_over = 0.15  # low probability due to reduced minutes
    kalshi_implied = kalshi_price_cents / 100.0

    model_prob_under = 1.0 - model_prob_over
    kalshi_no_implied = 1.0 - kalshi_implied
    edge = model_prob_under - kalshi_no_implied

    if edge < min_edge:
        return None

    return LiveSignal(
        signal_type="blowout",
        ticker=ticker,
        side="no",  # Sell over = buy NO
        edge=edge,
        model_prob=model_prob_under,
        kalshi_price_cents=100 - kalshi_price_cents,
        player_name=player_name,
        game_id=game_id,
        player_id=player_id,
        metadata={
            "margin": margin,
            "period": period,
            "clock_seconds": clock_seconds,
            "current_stat": current_stat,
            "line": line,
            "game_state": game_state.value,
        },
    )


# Empirical trailing-team win rates from 200 stored game feeds (H3 analysis, 2026-04-02).
# Keyed by (time_bucket, margin_bucket) -> trailing_team_win_probability.
# Time buckets: "late" = 30-120s, "final" = 10-30s.
# Margin buckets: 1, 2, 3-5, 6-8.
_EMPIRICAL_TRAILING_WIN_RATE = {
    ("late", 1): 0.385,
    ("late", 2): 0.352,
    ("late", 3): 0.327,  # represents 3-5 margin
    ("late", 4): 0.327,
    ("late", 5): 0.327,
    ("late", 6): 0.227,  # represents 6-8 margin
    ("final", 1): 0.402,
    ("final", 2): 0.344,
    ("final", 3): 0.311,
    ("final", 4): 0.311,
    ("final", 5): 0.311,
    ("final", 6): 0.200,
}


def _empirical_trailing_prob(margin: int, clock_seconds: int) -> float:
    """Look up empirical trailing-team win probability from H3 research.

    Falls back to interpolation for margins 7-8 and edge cases.
    """
    time_bucket = "final" if clock_seconds <= 30 else "late"
    abs_margin = min(abs(margin), 6)
    prob = _EMPIRICAL_TRAILING_WIN_RATE.get((time_bucket, abs_margin))
    if prob is not None:
        return prob
    # Fallback: linear interpolation between margin 6 (22%) and 8 (15% est.)
    return _clamp(0.22 - 0.035 * (abs_margin - 6), 0.05, 0.45)


def detect_clutch_comeback(
    margin: int,
    period: str,
    clock_seconds: int,
    trailing_team_price_cents: int,
    ticker: str = "",
    trailing_team: str = "",
    leading_team: str = "",
    game_id: Optional[str] = None,
    min_edge: float = 0.08,
) -> Optional[LiveSignal]:
    """Detect late-game mispricing on the trailing team.

    Uses empirical trailing-team win rates (from H3 analysis of 200 games)
    to detect when Kalshi misprices the comeback probability. Trades both
    directions:
    - BUY YES on trailing team if Kalshi underprices (trailing_price < empirical)
    - BUY NO on trailing team if Kalshi overprices (trailing_price > empirical)

    Filters: Q4, 10-120s remaining, margin 1-6, price not extreme.
    """
    if period != "Q4":
        return None
    if clock_seconds < 10 or clock_seconds > 120:
        return None

    abs_margin = abs(margin)
    if abs_margin < 1 or abs_margin > 6:
        return None

    if trailing_team_price_cents <= 5 or trailing_team_price_cents >= 95:
        return None

    model_prob_trailing = _empirical_trailing_prob(abs_margin, clock_seconds)
    model_prob_leading = 1.0 - model_prob_trailing

    kalshi_trailing_implied = trailing_team_price_cents / 100.0

    # Detect mispricing in EITHER direction:
    # If Kalshi overprices trailing team -> fade (buy NO)
    # If Kalshi underprices trailing team -> buy YES on trailing
    overpricing = kalshi_trailing_implied - model_prob_trailing  # positive = overpriced
    underpricing = model_prob_trailing - kalshi_trailing_implied  # positive = underpriced

    if overpricing >= min_edge:
        # Trailing team overpriced: fade (buy NO on trailing = buy YES on leader)
        return LiveSignal(
            signal_type="clutch_comeback",
            ticker=ticker,
            side="no",
            edge=overpricing,
            model_prob=model_prob_leading,
            kalshi_price_cents=100 - trailing_team_price_cents,
            player_name=trailing_team,
            game_id=game_id,
            metadata={
                "margin": abs_margin,
                "period": period,
                "clock_seconds": clock_seconds,
                "trailing_team": trailing_team,
                "leading_team": leading_team,
                "trailing_team_model_prob": round(model_prob_trailing, 3),
                "leading_team_model_prob": round(model_prob_leading, 3),
                "trailing_team_market_prob": round(kalshi_trailing_implied, 3),
                "direction": "fade_trailing",
                "model_source": "empirical_h3_200games",
            },
        )

    if underpricing >= min_edge:
        # Trailing team underpriced: buy YES on trailing team
        return LiveSignal(
            signal_type="clutch_comeback",
            ticker=ticker,
            side="yes",
            edge=underpricing,
            model_prob=model_prob_trailing,
            kalshi_price_cents=trailing_team_price_cents,
            player_name=trailing_team,
            game_id=game_id,
            metadata={
                "margin": abs_margin,
                "period": period,
                "clock_seconds": clock_seconds,
                "trailing_team": trailing_team,
                "leading_team": leading_team,
                "trailing_team_model_prob": round(model_prob_trailing, 3),
                "leading_team_model_prob": round(model_prob_leading, 3),
                "trailing_team_market_prob": round(kalshi_trailing_implied, 3),
                "direction": "buy_trailing",
                "model_source": "empirical_h3_200games",
            },
        )

    return None


def should_cancel_signal(
    signal_type: str,
    margin: int,
    period: str,
    clock_seconds: int,
    fouls: int = 0,
) -> bool:
    """Check if a Book C signal's condition has reversed, requiring order cancellation.

    Spec Section 5, Cancel Discipline: "If the signal condition reverses
    (e.g., blowout narrows to < 15), cancel any unfilled Book C orders immediately."
    """
    if signal_type == "blowout":
        # Blowout reversal: margin narrowed below 15 (was >= 20 at signal time)
        return abs(margin) < 15

    if signal_type == "ot_likely":
        # OT reversal: margin widened beyond 2 (was <= 2 at signal time)
        # or game has moved past Q4
        if abs(margin) > 4:
            return True
        if period not in ("Q4", "OT"):
            return True
        return False

    if signal_type == "foul_trouble":
        # Foul trouble doesn't really "reverse" — once you have 4 fouls,
        # the damage to minutes is done. However, if the period advanced to
        # Q4 and the player hasn't fouled out, the coach may play them.
        # Cancel if we're deep into Q4 with no 5th foul (less impact).
        if period == "Q4" and clock_seconds < 180 and fouls < 5:
            return True
        return False

    if signal_type == "clutch_comeback":
        # Cancel if the game is no longer in the target late-game comeback window.
        if period != "Q4":
            return True
        if clock_seconds < 10 or clock_seconds > 120:
            return True
        if abs(margin) == 0:
            return True
        return False

    return False


def live_signal_to_signal(ls: LiveSignal) -> Signal:
    """Convert a LiveSignal to a tradeable Signal."""
    return Signal(
        book=Book.C,
        ticker=ls.ticker,
        side=ls.side,
        edge=ls.edge,
        model_prob=ls.model_prob,
        kalshi_price=ls.kalshi_price_cents / 100.0,
        metadata={
            "signal_type": ls.signal_type,
            "player_name": ls.player_name,
            "game_id": ls.game_id,
            "player_id": ls.player_id,
            **(ls.metadata or {}),
        },
    )
