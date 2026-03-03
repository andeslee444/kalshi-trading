"""HMM-based volatility regime detector.

Classifies market conditions into 4 states using online Bayesian
updates on realized volatility observations. No scipy required —
uses pure math for Gaussian likelihood and matrix operations.

States:
    low_vol  — calm markets (BTC ~15-30% ann. vol)
    normal   — typical conditions (BTC ~40-60%)
    high_vol — elevated uncertainty (BTC ~70-100%)
    crisis   — extreme stress / flash crash (BTC ~100%+)

Usage:
    from regime_detector import RegimeDetector, regime_kelly_multiplier

    rd = RegimeDetector()
    rd.update(realized_vol)  # e.g., 0.60 for 60% annualized
    regime = rd.current_regime()
    mult = regime_kelly_multiplier(rd)
"""

import math
import json
import datetime
from pathlib import Path

# Default emission parameters (log-normal: vol observations given regime)
# mean = center of vol range for that regime, std = observation noise
DEFAULT_EMISSION_PARAMS = {
    "low_vol":  {"mean": 0.20, "std": 0.08},
    "normal":   {"mean": 0.50, "std": 0.15},
    "high_vol": {"mean": 0.85, "std": 0.20},
    "crisis":   {"mean": 1.30, "std": 0.30},
}

# Default transition matrix (row = from, col = to)
# Regimes are sticky: high self-transition probability
DEFAULT_TRANSITION_MATRIX = [
    #  low    norm   high   crisis
    [0.90,  0.08,  0.015, 0.005],  # from low_vol
    [0.05,  0.85,  0.08,  0.02],   # from normal
    [0.02,  0.10,  0.80,  0.08],   # from high_vol
    [0.005, 0.05,  0.15,  0.795],  # from crisis
]

STATE_NAMES = ["low_vol", "normal", "high_vol", "crisis"]


def _gaussian_pdf(x, mean, std):
    """Gaussian probability density function."""
    if std <= 0:
        return 0.0
    z = (x - mean) / std
    return math.exp(-0.5 * z * z) / (std * math.sqrt(2 * math.pi))


class RegimeDetector:
    """HMM-based 4-state vol regime detector with online Bayes updates.

    Uses forward algorithm (prediction + update) without full Baum-Welch
    re-estimation — transition and emission parameters are fixed priors
    calibrated to crypto/weather vol characteristics.
    """

    def __init__(self, transition_matrix=None, emission_params=None):
        self.state_names = list(STATE_NAMES)
        self.n_states = len(self.state_names)
        self.transition_matrix = transition_matrix or [row[:] for row in DEFAULT_TRANSITION_MATRIX]
        self.emission_params = emission_params or {
            k: dict(v) for k, v in DEFAULT_EMISSION_PARAMS.items()
        }
        # Uniform initial belief
        self.belief = [1.0 / self.n_states] * self.n_states
        self.n_updates = 0
        self._history = []  # (timestamp, vol, regime) tuples

    def update(self, observed_vol):
        """Online Bayesian update: predict → observe → normalize.

        Args:
            observed_vol: Realized annualized vol as decimal (e.g., 0.60 = 60%).
        """
        # Prediction step: belief = T^T @ belief
        predicted = [0.0] * self.n_states
        for j in range(self.n_states):
            for i in range(self.n_states):
                predicted[j] += self.transition_matrix[i][j] * self.belief[i]

        # Observation step: weight by emission likelihood
        for j in range(self.n_states):
            state = self.state_names[j]
            params = self.emission_params[state]
            likelihood = _gaussian_pdf(observed_vol, params["mean"], params["std"])
            predicted[j] *= likelihood

        # Normalize
        total = sum(predicted)
        if total > 0:
            self.belief = [p / total for p in predicted]
        # else: keep previous belief (degenerate observation)

        self.n_updates += 1
        self._history.append((
            datetime.datetime.now().isoformat(),
            observed_vol,
            self.current_regime(),
        ))
        # Keep last 100 history entries
        if len(self._history) > 100:
            self._history = self._history[-100:]

    def current_regime(self):
        """Return the most likely regime (max belief state)."""
        max_idx = self.belief.index(max(self.belief))
        return self.state_names[max_idx]

    def regime_confidence(self):
        """Return confidence in current regime (max belief probability)."""
        return max(self.belief)

    def is_elevated_vol(self):
        """Return True if belief favors high_vol or crisis."""
        return (self.belief[2] + self.belief[3]) > 0.5

    def get_regime_index(self):
        """Return index of current regime (0=low, 1=normal, 2=high, 3=crisis)."""
        return self.belief.index(max(self.belief))

    def serialize(self):
        """Serialize detector state to JSON-compatible dict."""
        return {
            "belief": self.belief[:],
            "n_updates": self.n_updates,
            "transition_matrix": self.transition_matrix,
            "emission_params": self.emission_params,
            "history": self._history[-20:],  # Last 20 for debugging
            "last_updated": datetime.datetime.now().isoformat(),
        }

    @classmethod
    def deserialize(cls, data):
        """Reconstruct detector from serialized state."""
        rd = cls(
            transition_matrix=data.get("transition_matrix"),
            emission_params=data.get("emission_params"),
        )
        rd.belief = data.get("belief", [0.25] * 4)
        rd.n_updates = data.get("n_updates", 0)
        rd._history = data.get("history", [])
        return rd

    def save(self, path):
        """Save state to JSON file with atomic write."""
        from kalshi_auth import _atomic_write_json
        _atomic_write_json(path, self.serialize())

    def load(self, path, max_age_hours=24):
        """Load state from JSON file with staleness check.

        Returns True if state was loaded, False if stale/missing.
        """
        try:
            with open(path) as f:
                data = json.load(f)
            last_updated = data.get("last_updated")
            if last_updated:
                age = (datetime.datetime.now() -
                       datetime.datetime.fromisoformat(last_updated))
                if age.total_seconds() > max_age_hours * 3600:
                    return False
            loaded = RegimeDetector.deserialize(data)
            self.belief = loaded.belief
            self.n_updates = loaded.n_updates
            self._history = loaded._history
            return True
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return False


def regime_kelly_multiplier(detector):
    """Compute Kelly fraction multiplier based on current regime.

    Returns:
        float: Multiplier in [0.50, 1.10].
            low_vol  → 1.10 (slightly more aggressive in calm markets)
            normal   → 1.00 (baseline)
            high_vol → 0.75 (-25% Kelly reduction)
            crisis   → 0.50 (-50% Kelly reduction)

    The multiplier scales linearly with regime confidence:
    actual_mult = 1.0 + (regime_mult - 1.0) * confidence
    """
    regime = detector.current_regime()
    confidence = detector.regime_confidence()

    # Raw regime multipliers
    regime_multipliers = {
        "low_vol": 1.10,
        "normal": 1.00,
        "high_vol": 0.75,
        "crisis": 0.50,
    }

    raw = regime_multipliers.get(regime, 1.0)

    # Blend toward 1.0 based on confidence (low confidence → multiplier near 1.0)
    return 1.0 + (raw - 1.0) * confidence
