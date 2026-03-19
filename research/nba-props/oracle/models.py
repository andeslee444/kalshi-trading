from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class Book(Enum):
    A = "game_divergence"
    B = "pregame_props"
    C = "live_events"


class GameState(Enum):
    COMPETITIVE = "competitive"
    BLOWOUT = "blowout"
    CLUTCH = "clutch"
    OT_LIKELY = "ot_likely"
    OVERTIME = "overtime"
    PREGAME = "pregame"
    HALFTIME = "halftime"
    FINAL = "final"

    @classmethod
    def classify(cls, margin: int, period: str, clock_seconds: int) -> GameState:
        abs_margin = abs(margin)
        is_q4 = period == "Q4"
        is_q3_or_later = period in ("Q3", "Q4")

        if is_q4 and clock_seconds <= 120 and abs_margin <= 2:
            return cls.OT_LIKELY
        if is_q4 and clock_seconds <= 300 and abs_margin <= 6:
            return cls.CLUTCH
        if is_q3_or_later and abs_margin >= 20:
            return cls.BLOWOUT
        return cls.COMPETITIVE


@dataclass
class Signal:
    book: Book
    ticker: str
    side: str  # "yes" or "no"
    edge: float
    model_prob: float
    kalshi_price: float
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.edge < 0:
            raise ValueError(f"Edge must be non-negative, got {self.edge}")
        if self.side not in ("yes", "no"):
            raise ValueError(f"Side must be 'yes' or 'no', got {self.side}")


@dataclass
class Position:
    book: Book
    ticker: str
    side: str
    contracts: int
    fill_price: float
    signal_timestamp: float = field(default_factory=time.time)
    fill_timestamp: float = field(default_factory=time.time)
    game_id: Optional[str] = None
    player_id: Optional[int] = None
    spread_game_id: Optional[str] = None

    @property
    def cost(self) -> float:
        return round(self.contracts * self.fill_price, 2)


@dataclass
class Game:
    game_id: int
    sport: str
    home_team: str
    away_team: str
    status: str  # "scheduled", "live", "final"
    home_score: int = 0
    away_score: int = 0
    period: str = ""
    clock: str = ""
    real_volume: int = 0


@dataclass
class Market:
    ticker: str
    series_ticker: str
    title: str
    yes_price: int  # cents (1-99)
    no_price: int
    volume: int = 0
    yes_bid: int = 0
    yes_ask: int = 0
    status: str = "open"


@dataclass
class Player:
    player_id: int
    name: str
    team: str
    status: str = "Active"  # Active, Out, Questionable
    minutes_avg_last5: float = 0.0
    season_per_minute: dict = field(default_factory=dict)


@dataclass
class PlayerSplits:
    last_5: list[float]
    last_10: list[float]
    season_avg: float
    home_avg: float
    away_avg: float
    per_opponent: dict = field(default_factory=dict)

    def hit_rate(self, line: float, n: int = 5) -> float:
        values = self.last_5 if n <= 5 else self.last_10[:n]
        if not values:
            return 0.0
        return sum(1 for v in values if v > line) / len(values)


@dataclass
class StatTracker:
    player_name: str
    stat_type: str
    line: float
    over_odds: Optional[float] = None
    under_odds: Optional[float] = None


@dataclass
class QuoteSnapshot:
    ticker: str
    yes_bid: int
    yes_ask: int
    bid_depth: int
    ask_depth: int
    timestamp: float = field(default_factory=time.time)

    @property
    def spread(self) -> int:
        return self.yes_ask - self.yes_bid

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp
