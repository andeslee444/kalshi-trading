"""Book B: Pregame player props.

Uses empirical distribution from game logs to price player prop markets.
Applies adjustments for matchup, venue, B2B, pace, and teammate injury.

Pipeline:
1. Fetch player splits from Real Sports (last 5/10 games, home/away, per-opponent)
2. Compute weighted hit rate (how often player clears the line)
3. Apply adjustments: matchup_shift, venue_shift, B2B_penalty, pace_factor, teammate_out
4. Compare adjusted probability to Kalshi price
5. Trade if edge > threshold (default 10%)

Gates:
- Player must average >= 20 minutes in last 5 games (filter bench players)
- No trades within 15 minutes of game start (lineups may change)
- Per-player/per-game exposure limits enforced by risk/internal_limits.py
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from domain.oracle.models import Book, Signal, PlayerSplits

_log = logging.getLogger("oracle.book_b")

# ─── Calibration loading ───

_ORACLE_CAL_PATH = Path(__file__).resolve().parents[5] / "config" / "oracle-calibration.json"
_oracle_cal = None


def _load_oracle_calibration():
    """Lazy-load oracle-calibration.json. Falls back to hardcoded defaults."""
    global _oracle_cal
    if _oracle_cal is not None:
        return _oracle_cal
    try:
        if _ORACLE_CAL_PATH.exists():
            _oracle_cal = json.loads(_ORACLE_CAL_PATH.read_text())
            if not isinstance(_oracle_cal, dict):
                _log.warning("oracle-calibration.json has wrong schema, using defaults")
                _oracle_cal = {}
            else:
                _log.info("Oracle calibration loaded from %s", _ORACLE_CAL_PATH.name)
        else:
            _oracle_cal = {}
            _log.info("Oracle calibration: using hardcoded defaults (no oracle-calibration.json)")
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("Failed to load oracle-calibration.json: %s", e)
        _oracle_cal = {}
    return _oracle_cal


def _reset_oracle_calibration():
    """Reset cached calibration (for testing)."""
    global _oracle_cal
    _oracle_cal = None


@dataclass
class PropOpportunity:
    """A detected player prop mispricing."""
    ticker: str
    player_name: str
    player_id: Optional[int]
    team: str
    stat_type: str
    line: float
    direction: str  # "over" or "under"
    model_prob: float
    kalshi_price_cents: int
    edge: float
    game_id: Optional[str] = None
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


def weighted_hit_rate(
    splits: PlayerSplits,
    line: float,
) -> float:
    """Compute probability of clearing a line using empirical distribution.

    Spec Section 4: Build from game-by-game stat values with recency weighting.
    Last 5 games get 2x weight, last 6-10 get 1.5x, last 11-20 get 1x.
    This preserves the non-normal shape of the actual distribution.
    """
    # Use last_10 as the broadest available sample (ideally 20, but PlayerSplits has 10)
    values = list(splits.last_10) if splits.last_10 else list(splits.last_5)
    if not values:
        # Fallback to season average estimate
        if splits.season_avg <= 0:
            return 0.5
        z = (splits.season_avg - line) / max(splits.season_avg * 0.15, 1.0)
        return max(0.01, min(0.99, 1.0 / (1.0 + math.exp(-z * 1.5))))

    # Recency weighting: last 5 = 2x, last 6-10 = 1.5x, rest = 1x
    cal = _load_oracle_calibration().get("book_b", {})
    w5 = cal.get("RECENCY_WEIGHT_LAST5", 2.0)
    w10 = cal.get("RECENCY_WEIGHT_LAST10", 1.5)
    ws = cal.get("RECENCY_WEIGHT_SEASON", 1.0)
    weights = []
    for i in range(len(values)):
        if i < 5:
            weights.append(w5)
        elif i < 10:
            weights.append(w10)
        else:
            weights.append(ws)

    weighted_hits = sum(w * (1.0 if v > line else 0.0) for w, v in zip(weights, values))
    weighted_total = sum(weights[:len(values)])

    if weighted_total <= 0:
        return 0.5

    return max(0.01, min(0.99, weighted_hits / weighted_total))


def matchup_shift(
    splits: PlayerSplits,
    opponent: str,
    line: float,
) -> float:
    """Adjustment for opponent-specific performance.

    Returns shift in probability (-0.1 to +0.1).
    """
    opp_avg = splits.per_opponent.get(opponent)
    if opp_avg is None:
        return 0.0

    # How much does opponent performance differ from season average?
    if splits.season_avg <= 0:
        return 0.0

    ratio = opp_avg / splits.season_avg
    # Clamp adjustment to +-cap
    cal = _load_oracle_calibration().get("book_b", {})
    multiplier = cal.get("MATCHUP_MULTIPLIER", 0.15)
    cap = cal.get("MATCHUP_CAP", 0.10)
    shift = (ratio - 1.0) * multiplier
    return max(-cap, min(cap, shift))


def venue_shift(
    splits: PlayerSplits,
    is_home: bool,
) -> float:
    """Adjustment for home/away performance.

    Returns shift in probability (-0.05 to +0.05).
    """
    if splits.season_avg <= 0:
        return 0.0

    venue_avg = splits.home_avg if is_home else splits.away_avg
    if venue_avg <= 0:
        return 0.0

    ratio = venue_avg / splits.season_avg
    cal = _load_oracle_calibration().get("book_b", {})
    multiplier = cal.get("VENUE_MULTIPLIER", 0.10)
    cap = cal.get("VENUE_CAP", 0.05)
    shift = (ratio - 1.0) * multiplier
    return max(-cap, min(cap, shift))


def b2b_penalty(is_back_to_back: bool) -> float:
    """Adjustment for back-to-back games.

    Players tend to underperform on the second night of a B2B.
    Returns negative shift (0 to -0.05).
    """
    cal = _load_oracle_calibration().get("book_b", {})
    penalty = cal.get("B2B_PENALTY", -0.05)
    return penalty if is_back_to_back else 0.0


def pace_factor(
    team_pace: float,
    league_avg_pace: float = 100.0,
) -> float:
    """Adjustment for team pace (possessions per game).

    Faster pace = more counting stats = higher over probability.
    Returns shift (-0.05 to +0.05).
    """
    if league_avg_pace <= 0:
        return 0.0
    ratio = team_pace / league_avg_pace
    shift = (ratio - 1.0) * 0.10
    return max(-0.05, min(0.05, shift))


def teammate_out_shift(
    key_teammate_out: bool,
    stat_type: str = "",
) -> float:
    """Adjustment when a key teammate is out (spec Section 4, step 4c).

    Stat-specific: points get +3% (teammate out = more usage),
    assists get -2% (fewer assist opportunities), others +1.5%.
    """
    if not key_teammate_out:
        return 0.0
    cal = _load_oracle_calibration().get("book_b", {})
    if stat_type in ("points", "pts"):
        return cal.get("TEAMMATE_OUT_SHIFT_PTS", 0.03)
    if stat_type in ("assists", "ast"):
        return cal.get("TEAMMATE_OUT_SHIFT_AST", -0.02)
    return cal.get("TEAMMATE_OUT_SHIFT_OTHER", 0.015)


def external_odds_blend(
    model_prob: float,
    external_prob: float,
    blend_weight: float = 0.3,
) -> float:
    """Blend model probability with external sportsbook odds.

    When external odds are available (e.g., DraftKings, FanDuel prop lines),
    blend them into the model probability to reduce calibration error.

    Args:
        model_prob: Our model's probability estimate.
        external_prob: Implied probability from external sportsbook.
        blend_weight: Weight given to external odds (0-1). Default 0.3.

    Returns:
        Blended probability in [0.01, 0.99].
    """
    if external_prob <= 0 or external_prob >= 1:
        return model_prob
    blended = (1 - blend_weight) * model_prob + blend_weight * external_prob
    return max(0.01, min(0.99, blended))


def is_within_no_trade_window(
    minutes_to_start: float,
    no_trade_window_min: float = 15.0,
) -> bool:
    """Check if we're within the no-trade window before game start.

    Book B should not trade props within N minutes of game start because
    lineups may change (late scratches, injury upgrades).
    """
    return 0 <= minutes_to_start < no_trade_window_min


def compute_adjusted_prob(
    splits: PlayerSplits,
    line: float,
    opponent: str = "",
    is_home: bool = True,
    is_b2b: bool = False,
    team_pace: float = 100.0,
    league_avg_pace: float = 100.0,
    key_teammate_out: bool = False,
    stat_type: str = "",
) -> float:
    """Compute fully adjusted probability of clearing a prop line.

    Returns probability in [0.01, 0.99].
    """
    base = weighted_hit_rate(splits, line)

    adjustments = (
        matchup_shift(splits, opponent, line)
        + venue_shift(splits, is_home)
        + b2b_penalty(is_b2b)
        + pace_factor(team_pace, league_avg_pace)
        + teammate_out_shift(key_teammate_out, stat_type)
    )

    adjusted = base + adjustments
    return max(0.01, min(0.99, adjusted))


def evaluate_prop(
    ticker: str,
    player_name: str,
    player_id: Optional[int],
    team: str,
    stat_type: str,
    line: float,
    direction: str,
    kalshi_price_cents: int,
    splits: PlayerSplits,
    min_edge: float = 0.10,
    min_minutes_avg: float = 20.0,
    minutes_avg_last5: float = 0.0,
    opponent: str = "",
    is_home: bool = True,
    is_b2b: bool = False,
    team_pace: float = 100.0,
    league_avg_pace: float = 100.0,
    key_teammate_out: bool = False,
    game_id: Optional[str] = None,
    minutes_to_start: float = -1.0,
    no_trade_window_min: float = 15.0,
    ext_prob: float = 0.0,
    ext_blend_weight: float = 0.3,
    player_status: str = "Active",
    key_teammate_questionable: bool = False,
) -> Optional[PropOpportunity]:
    """Evaluate a single player prop for trading opportunity.

    IMPORTANT: kalshi_price_cents must match the direction parameter.
    If direction="over", pass the price of the OVER contract.
    If direction="under", pass the price of the UNDER contract.
    Misalignment produces incorrect edge calculations.

    Lineup gates (spec Section 4):
    - player_status="Questionable" → NO TRADE
    - key_teammate_questionable=True → NO TRADE (usage distribution unknown)
    - minutes_avg_last5 < 20 → NO TRADE (bench player variance)
    - minutes_to_start < 15 → NO TRADE (late lineup changes)
    - Rotation player (20-28 min avg) → 50% position size reduction (via metadata)

    Returns PropOpportunity if edge found, None otherwise.
    """
    # Gate: Questionable player status (spec Section 4, rule 1)
    if player_status == "Questionable":
        return None

    # Gate: key teammate Questionable (spec Section 4, rule 2)
    if key_teammate_questionable:
        return None

    # Gate: minimum minutes (spec Section 4, rule 4)
    if minutes_avg_last5 > 0 and minutes_avg_last5 < min_minutes_avg:
        return None

    # Gate: no-trade window before game start (spec Section 4, rule 3)
    if minutes_to_start >= 0 and is_within_no_trade_window(
        minutes_to_start, no_trade_window_min
    ):
        return None

    if kalshi_price_cents <= 5 or kalshi_price_cents >= 95:
        return None

    over_prob = compute_adjusted_prob(
        splits, line, opponent, is_home, is_b2b,
        team_pace, league_avg_pace, key_teammate_out,
        stat_type=stat_type,
    )

    if direction == "over":
        model_prob = over_prob
    else:
        model_prob = 1.0 - over_prob

    # Blend with external sportsbook odds if available
    if ext_prob > 0:
        model_prob = external_odds_blend(model_prob, ext_prob, ext_blend_weight)

    kalshi_implied = kalshi_price_cents / 100.0
    edge = model_prob - kalshi_implied

    if edge < min_edge:
        return None

    # Rotation player sizing reduction (spec Section 4, rule 5):
    # 20-28 min avg = rotation player → 50% size reduction
    # >28 min = presumed starter → full size
    is_rotation_player = (0 < minutes_avg_last5 < 28) and minutes_avg_last5 >= min_minutes_avg
    sizing_multiplier = 0.5 if is_rotation_player else 1.0

    return PropOpportunity(
        ticker=ticker,
        player_name=player_name,
        player_id=player_id,
        team=team,
        stat_type=stat_type,
        line=line,
        direction=direction,
        model_prob=model_prob,
        kalshi_price_cents=kalshi_price_cents,
        edge=edge,
        game_id=game_id,
        metadata={
            "over_prob": over_prob,
            "opponent": opponent,
            "is_home": is_home,
            "is_b2b": is_b2b,
            "team_pace": team_pace,
            "key_teammate_out": key_teammate_out,
            "minutes_avg_last5": minutes_avg_last5,
            "minutes_to_start": minutes_to_start,
            "ext_prob": ext_prob,
            "player_status": player_status,
            "is_rotation_player": is_rotation_player,
            "sizing_multiplier": sizing_multiplier,
        },
    )


def prop_to_signal(opp: PropOpportunity) -> Signal:
    """Convert a PropOpportunity to a tradeable Signal."""
    side = "yes"  # buying YES on the direction (over or under)
    return Signal(
        book=Book.B,
        ticker=opp.ticker,
        side=side,
        edge=opp.edge,
        model_prob=opp.model_prob,
        kalshi_price=opp.kalshi_price_cents / 100.0,
        metadata={
            "player_name": opp.player_name,
            "player_id": opp.player_id,
            "stat_type": opp.stat_type,
            "line": opp.line,
            "direction": opp.direction,
            "game_id": opp.game_id,
            "signal_type": "pregame_prop",
            **(opp.metadata or {}),
        },
    )
