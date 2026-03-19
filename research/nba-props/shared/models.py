"""Research-specific data models for sportsmarket analysis."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GameLog:
    """Single game stat line for a player."""
    player_id: int
    player_name: str
    game_date: str
    opponent: str
    home: bool
    minutes: float
    points: int = 0
    rebounds: int = 0
    assists: int = 0
    steals: int = 0
    blocks: int = 0
    turnovers: int = 0
    three_pointers: int = 0
    field_goals_made: int = 0
    field_goals_attempted: int = 0
    free_throws_made: int = 0
    free_throws_attempted: int = 0

    def stat(self, stat_type: str) -> float:
        """Get a stat value by type string (e.g., 'points', 'rebounds+assists')."""
        combo = stat_type.lower().replace(" ", "")
        total = 0.0
        for part in combo.split("+"):
            val = getattr(self, part, None)
            if val is None:
                raise ValueError(f"Unknown stat type: {part}")
            total += val
        return total


@dataclass
class EmpiricalDistribution:
    """Empirical CDF for a player-stat combination."""
    player_id: int
    player_name: str
    stat_type: str
    values: list[float] = field(default_factory=list)
    sample_size: int = 0

    def prob_over(self, line: float) -> float:
        """Empirical probability of going over a line."""
        if not self.values:
            return 0.5
        return sum(1 for v in self.values if v > line) / len(self.values)

    def prob_under(self, line: float) -> float:
        """Empirical probability of going under a line."""
        return 1.0 - self.prob_over(line)


@dataclass
class CalibrationResult:
    """Result of calibrating a model against historical data."""
    stat_type: str
    sample_size: int
    brier_score: float
    hit_rate: float
    avg_edge: float
    parameters: dict = field(default_factory=dict)
    notes: str = ""
