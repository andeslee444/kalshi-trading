"""Competitive Edge Monitor — Tracks edge decay and market efficiency.

Detects when trading strategies lose their edge due to market
efficiency improvements or new competitor entry. Processes settled
trade observations and computes efficiency trends, edge half-lives,
and optimal strategy allocation weights.

No kalshi_auth dependency — pure analytics module.

Usage:
    from edge_monitor import EdgeMonitor

    em = EdgeMonitor(state_path="data/edge-monitor-state.json")
    em.load_state()
    em.add_observations(settled_observations)
    report = em.json_report()
    em.save_state()
"""

import json
import math
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("edge-monitor")


def _linear_regression(xs, ys):
    """Simple OLS regression. Returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0

    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)

    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-10:
        return 0.0, sum_y / n, 0.0

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    mean_y = sum_y / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r_sq = 1 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0

    return slope, intercept, max(0.0, r_sq)


def _parse_timestamp(ts):
    """Parse ISO timestamp to naive datetime (assumes UTC). Returns None on failure."""
    try:
        # Strip timezone suffixes to get naive datetime for consistent comparison
        cleaned = ts.replace("Z", "")
        if "+" in cleaned:
            cleaned = cleaned[:cleaned.index("+")]
        return datetime.fromisoformat(cleaned)
    except (ValueError, AttributeError):
        return None


def _days_since_epoch(ts_str):
    """Convert ISO timestamp to days since a reference epoch (2026-01-01)."""
    dt = _parse_timestamp(ts_str)
    if dt is None:
        return 0.0
    epoch = datetime(2026, 1, 1)
    return (dt - epoch).total_seconds() / 86400.0


class EdgeMonitor:
    """Tracks market efficiency trends and competitive edge decay."""

    def __init__(self, lookback_days=90, state_path=None):
        self.lookback_days = lookback_days
        self.state_path = state_path
        self._observations = []  # list of observation dicts

    def add_observations(self, observations):
        """Add a batch of settled trade observations.

        Each observation dict has:
            timestamp, market_type, model_prob, market_price_cents,
            raw_edge, settled_won
        """
        self._observations.extend(observations)
        # Sort by timestamp
        self._observations.sort(key=lambda o: o.get("timestamp", ""))
        # Trim to lookback window
        cutoff = (datetime.now() - timedelta(days=self.lookback_days)).isoformat()
        self._observations = [o for o in self._observations
                              if o.get("timestamp", "") >= cutoff or self.lookback_days <= 0]

    def _filter_by_type(self, market_type, window_days=None):
        """Get observations for a specific market type, optionally windowed."""
        obs = [o for o in self._observations if o.get("market_type") == market_type]
        if window_days is not None and obs:
            cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
            obs = [o for o in obs if o.get("timestamp", "") >= cutoff]
        return obs

    def edge_realization_rate(self, market_type, window_days=None):
        """Fraction of trades where edge was realized (won).

        Returns 0.0 if no observations for this market type.
        """
        obs = self._filter_by_type(market_type, window_days)
        if not obs:
            return 0.0
        wins = sum(1 for o in obs if o.get("settled_won"))
        return wins / len(obs)

    def efficiency_trend(self, market_type):
        """Slope of |model_prob - market_price| gap over time.

        Negative slope = market becoming more efficient (gap shrinking).
        Returns 0.0 if insufficient data.
        """
        obs = self._filter_by_type(market_type)
        if len(obs) < 3:
            return 0.0

        xs = [_days_since_epoch(o["timestamp"]) for o in obs]
        ys = [abs(o.get("raw_edge", 0.0)) for o in obs]

        slope, _, _ = _linear_regression(xs, ys)
        return slope

    def edge_half_life(self, market_type):
        """Estimated days until edge decays 50%.

        Fits exponential decay to the gap values: gap(t) = gap_0 * exp(-lambda * t).
        Takes log of gap values and fits linear regression.

        Returns float("inf") if edge is stable or growing.
        """
        obs = self._filter_by_type(market_type)
        if len(obs) < 5:
            return float("inf")

        xs = [_days_since_epoch(o["timestamp"]) for o in obs]
        gaps = [abs(o.get("raw_edge", 0.0)) for o in obs]

        # Filter out zero/near-zero gaps (can't take log)
        filtered = [(x, g) for x, g in zip(xs, gaps) if g > 0.001]
        if len(filtered) < 3:
            return float("inf")

        log_gaps = [math.log(g) for _, g in filtered]
        x_vals = [x for x, _ in filtered]

        slope, _, _ = _linear_regression(x_vals, log_gaps)

        # slope = -lambda in exponential decay
        if slope >= 0:
            return float("inf")  # Edge is stable or growing

        decay_rate = -slope
        return math.log(2) / decay_rate

    def detect_competitor(self, market_type, threshold=0.30, recent_days=14):
        """Detect if a new competitor may have entered.

        Compares average gap in last `recent_days` to the preceding period.
        Returns True if gap shrank by more than `threshold` fraction.
        """
        all_obs = self._filter_by_type(market_type)
        if len(all_obs) < 5:
            return False

        cutoff = (datetime.now() - timedelta(days=recent_days)).isoformat()
        recent = [o for o in all_obs if o.get("timestamp", "") >= cutoff]
        earlier = [o for o in all_obs if o.get("timestamp", "") < cutoff]

        if not recent or not earlier:
            return False

        avg_recent = sum(abs(o.get("raw_edge", 0)) for o in recent) / len(recent)
        avg_earlier = sum(abs(o.get("raw_edge", 0)) for o in earlier) / len(earlier)

        if avg_earlier < 0.001:
            return False

        shrinkage = 1.0 - avg_recent / avg_earlier
        return shrinkage > threshold

    def optimal_strategy_weights(self):
        """Suggest capital allocation weights based on edge durability.

        Strategies with longer edge half-lives get higher weights.
        Returns dict {market_type: weight} summing to 1.0.
        """
        market_types = set(o.get("market_type") for o in self._observations)
        market_types.discard(None)

        if not market_types:
            return {}

        scores = {}
        for mt in market_types:
            hl = self.edge_half_life(mt)
            rate = self.edge_realization_rate(mt)
            # Score = win_rate * log(1 + half_life_days)
            if hl == float("inf"):
                hl_score = math.log(1 + 365)
            else:
                hl_score = math.log(1 + max(1, hl))
            scores[mt] = rate * hl_score

        total = sum(scores.values())
        if total <= 0:
            # Equal weights
            n = len(market_types)
            return {mt: 1.0 / n for mt in market_types}

        return {mt: round(s / total, 4) for mt, s in scores.items()}

    def save_state(self):
        """Persist observations to disk."""
        if not self.state_path:
            return
        data = {"observations": self._observations}
        p = Path(self.state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(p))

    def load_state(self):
        """Load observations from disk."""
        if not self.state_path:
            return
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            self._observations = data.get("observations", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def json_report(self):
        """Generate a JSON-serializable report of edge intelligence."""
        market_types = sorted(set(o.get("market_type") for o in self._observations) - {None})
        mt_reports = {}
        for mt in market_types:
            mt_reports[mt] = {
                "realization_rate": round(self.edge_realization_rate(mt), 4),
                "efficiency_trend_slope": round(self.efficiency_trend(mt), 6),
                "edge_half_life_days": self.edge_half_life(mt),
                "competitor_detected": self.detect_competitor(mt),
                "observation_count": len(self._filter_by_type(mt)),
            }
        weights = self.optimal_strategy_weights()
        return {
            "market_types": mt_reports,
            "strategy_weights": weights,
            "total_observations": len(self._observations),
        }
