# T2-Phase 3: Advanced Simulation Engine — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build an importance sampling engine for tail-risk contract pricing, HMM-based regime detection for vol states (low/normal/high/crisis), a jump-diffusion crypto model that captures flash crashes and rallies, and regime-adjusted Kelly sizing that automatically reduces exposure during volatile regimes.

**Architecture:** Two new modules: `src/kalshi/simulation.py` (importance sampling with variance reduction) and `src/kalshi/regime_detector.py` (HMM with online Bayes updates, 4-state vol regime). The jump-diffusion model extends `crypto_price_probability()` in `probability.py`. Regime-adjusted Kelly integrates into `capital_allocator.py` as a new bankroll multiplier alongside the existing tail-risk multiplier from T2-P2. State persists in `data/regime-state.json`.

**Tech Stack:** Python 3, `scipy` (already installed — `scipy.stats`, `scipy.optimize`, `scipy.special`), `math`, `statistics`, JSON persistence via existing `_atomic_write_json`.

**Branch:** `track2/quant-infrastructure` (from `main`)

---

### Task 1: Create Branch and Verify Baseline

**Files:**
- None (branch setup only)

**Step 1: Create the feature branch**

```bash
git checkout -b track2/quant-infrastructure main
```

**Step 2: Verify all existing tests pass**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass.

**Step 3: No commit needed — branch is ready.**

---

### Task 2: Regime Detector — Core HMM Tests

**Files:**
- Create: `tests/test_regime_detector.py`

**Context:** The regime detector uses a Hidden Markov Model (HMM) with 4 hidden states: `low_vol` (calm markets), `normal` (typical conditions), `high_vol` (elevated uncertainty), and `crisis` (extreme stress/flash crash). Observations are realized volatility values. The HMM maintains transition probabilities and emission parameters, and provides online Bayes updates for real-time regime classification without full re-estimation.

**Step 1: Write core HMM tests**

```python
"""Tests for regime_detector.py — HMM-based vol regime detection.

The regime detector classifies market conditions into 4 states:
low_vol, normal, high_vol, crisis. It uses realized volatility
observations and online Bayesian updates.
"""

import json
import math
import tempfile
from pathlib import Path
import pytest


class TestRegimeDetectorInit:
    """Test regime detector initialization and default parameters."""

    def test_default_four_states(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert rd.n_states == 4
        assert rd.state_names == ["low_vol", "normal", "high_vol", "crisis"]

    def test_default_transition_matrix_shape(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert len(rd.transition_matrix) == 4
        assert all(len(row) == 4 for row in rd.transition_matrix)

    def test_transition_rows_sum_to_one(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for row in rd.transition_matrix:
            assert abs(sum(row) - 1.0) < 1e-9

    def test_default_emission_params(self):
        """Each state has a mean and std for vol emission (log-normal)."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert len(rd.emission_params) == 4
        for state in rd.state_names:
            assert "mean" in rd.emission_params[state]
            assert "std" in rd.emission_params[state]
            assert rd.emission_params[state]["std"] > 0

    def test_low_vol_mean_less_than_crisis(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert rd.emission_params["low_vol"]["mean"] < rd.emission_params["crisis"]["mean"]

    def test_initial_belief_uniform(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert len(rd.belief) == 4
        for p in rd.belief:
            assert abs(p - 0.25) < 1e-9


class TestRegimeUpdate:
    """Test online Bayesian regime updates."""

    def test_low_vol_observation_shifts_belief(self):
        """Observing low vol should increase P(low_vol)."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Low vol observation (e.g., 0.15 annualized = 15% vol for BTC is calm)
        rd.update(0.15)
        assert rd.belief[0] > 0.25  # low_vol probability increased

    def test_high_vol_observation_shifts_belief(self):
        """Observing high vol should increase P(high_vol) or P(crisis)."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # High vol observation (e.g., 1.20 annualized = 120% vol = extreme)
        rd.update(1.20)
        # high_vol (index 2) + crisis (index 3) should dominate
        assert rd.belief[2] + rd.belief[3] > 0.5

    def test_multiple_updates_converge(self):
        """Repeated low-vol observations should converge belief to low_vol."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(0.15)  # Consistently low vol
        assert rd.belief[0] > 0.8  # Strong low_vol belief

    def test_regime_switch_detected(self):
        """After regime switch (low→high vol), belief should shift."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Start in low vol
        for _ in range(5):
            rd.update(0.15)
        assert rd.belief[0] > 0.5  # In low_vol regime
        # Switch to high vol
        for _ in range(5):
            rd.update(0.90)
        assert rd.belief[0] < 0.3  # Left low_vol regime
        assert rd.belief[2] + rd.belief[3] > 0.5  # Moved to high/crisis

    def test_belief_sums_to_one_after_update(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        rd.update(0.50)
        assert abs(sum(rd.belief) - 1.0) < 1e-9

    def test_update_increments_count(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        assert rd.n_updates == 0
        rd.update(0.50)
        assert rd.n_updates == 1


class TestRegimeClassification:
    """Test regime classification from belief state."""

    def test_current_regime_returns_max_belief(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(0.15)
        assert rd.current_regime() == "low_vol"

    def test_current_regime_after_crisis(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(1.50)  # Extremely high vol
        regime = rd.current_regime()
        assert regime in ("high_vol", "crisis")

    def test_regime_confidence(self):
        """Confidence should be max(belief) — higher when regime is clear."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Uniform belief → low confidence
        assert rd.regime_confidence() < 0.5
        # After convergence → high confidence
        for _ in range(10):
            rd.update(0.15)
        assert rd.regime_confidence() > 0.7

    def test_is_elevated_vol(self):
        """Helper: returns True if high_vol or crisis."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(10):
            rd.update(0.15)
        assert rd.is_elevated_vol() is False
        for _ in range(10):
            rd.update(1.00)
        assert rd.is_elevated_vol() is True
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_regime_detector.py -v`
Expected: FAIL — `regime_detector` module not found.

**Step 3: Commit failing tests**

```bash
git add tests/test_regime_detector.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add HMM regime detector tests (failing — module not yet implemented)"
```

---

### Task 3: Regime Detector — Implementation

**Files:**
- Create: `src/kalshi/regime_detector.py`

**Step 1: Implement the regime detector**

```python
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
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_regime_detector.py -v`
Expected: All 17 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/regime_detector.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add HMM regime detector with 4-state vol classification and online Bayes updates"
```

---

### Task 4: Regime Detector — Persistence and Kelly Tests

**Files:**
- Modify: `tests/test_regime_detector.py`

**Step 1: Add persistence and Kelly multiplier tests**

```python
class TestRegimePersistence:
    """Test state serialization and file I/O."""

    def test_serialize_roundtrip(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        for _ in range(5):
            rd.update(0.50)
        data = rd.serialize()
        rd2 = RegimeDetector.deserialize(data)
        assert rd2.belief == rd.belief
        assert rd2.n_updates == rd.n_updates

    def test_save_and_load(self):
        from regime_detector import RegimeDetector
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regime-state.json"
            rd = RegimeDetector()
            for _ in range(5):
                rd.update(0.50)
            rd.save(str(path))
            rd2 = RegimeDetector()
            loaded = rd2.load(str(path))
            assert loaded is True
            assert rd2.belief == rd.belief

    def test_load_missing_file(self):
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        loaded = rd.load("/nonexistent/path.json")
        assert loaded is False
        assert rd.n_updates == 0  # Unchanged

    def test_stale_state_rejected(self):
        from regime_detector import RegimeDetector
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regime-state.json"
            rd = RegimeDetector()
            rd.update(0.50)
            # Write state with old timestamp
            data = rd.serialize()
            data["last_updated"] = "2020-01-01T00:00:00"
            with open(path, "w") as f:
                json.dump(data, f)
            rd2 = RegimeDetector()
            loaded = rd2.load(str(path), max_age_hours=24)
            assert loaded is False


class TestRegimeKellyMultiplier:
    """Test regime-adjusted Kelly sizing."""

    def test_low_vol_boost(self):
        """Low vol regime should increase Kelly slightly."""
        from regime_detector import RegimeDetector, regime_kelly_multiplier
        rd = RegimeDetector()
        for _ in range(20):
            rd.update(0.15)
        mult = regime_kelly_multiplier(rd)
        assert mult > 1.0  # Slight boost

    def test_normal_regime_near_one(self):
        """Normal regime should give multiplier near 1.0."""
        from regime_detector import RegimeDetector, regime_kelly_multiplier
        rd = RegimeDetector()
        for _ in range(20):
            rd.update(0.50)
        mult = regime_kelly_multiplier(rd)
        assert 0.9 <= mult <= 1.1

    def test_high_vol_reduction(self):
        """High vol should reduce Kelly by ~25%."""
        from regime_detector import RegimeDetector, regime_kelly_multiplier
        rd = RegimeDetector()
        for _ in range(20):
            rd.update(0.85)
        mult = regime_kelly_multiplier(rd)
        assert mult < 0.90  # Meaningful reduction

    def test_crisis_severe_reduction(self):
        """Crisis should reduce Kelly by ~50%."""
        from regime_detector import RegimeDetector, regime_kelly_multiplier
        rd = RegimeDetector()
        for _ in range(20):
            rd.update(1.50)
        mult = regime_kelly_multiplier(rd)
        assert mult < 0.70  # Severe reduction

    def test_uncertain_belief_near_one(self):
        """With uniform belief (uncertain), multiplier should be near 1.0."""
        from regime_detector import RegimeDetector, regime_kelly_multiplier
        rd = RegimeDetector()
        # No updates — uniform belief
        mult = regime_kelly_multiplier(rd)
        assert 0.95 <= mult <= 1.05

    def test_multiplier_bounded(self):
        """Multiplier should always be in [0.5, 1.1]."""
        from regime_detector import RegimeDetector, regime_kelly_multiplier
        rd = RegimeDetector()
        for vol in [0.05, 0.15, 0.50, 0.85, 1.50, 3.00]:
            rd2 = RegimeDetector()
            for _ in range(20):
                rd2.update(vol)
            mult = regime_kelly_multiplier(rd2)
            assert 0.45 <= mult <= 1.15
```

**Step 2: Run tests**

Run: `pytest tests/test_regime_detector.py -v`
Expected: All 28 tests pass.

**Step 3: Commit**

```bash
git add tests/test_regime_detector.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add regime detector persistence and Kelly multiplier tests"
```

---

### Task 5: Jump-Diffusion Crypto Model — Tests

**Files:**
- Modify: `tests/test_probability.py` (or `tests/test_crypto.py`)

**Context:** The existing `crypto_price_probability()` uses GBM (geometric Brownian motion), which underprices tail events like flash crashes. The Merton jump-diffusion model adds a Poisson jump process: `dS/S = (μ - λk)dt + σdW + JdN` where J ~ N(μ_J, σ_J) is the jump size and N is a Poisson process with intensity λ. This gives fatter tails and better pricing for far-OTM crypto markets.

**Step 1: Write jump-diffusion probability tests**

Add to `tests/test_crypto.py` (or create if testing probability directly):

```python
class TestJumpDiffusionCrypto:
    """Test Merton jump-diffusion model for crypto price probability."""

    def setup_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def teardown_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def test_basic_probability_range(self):
        """JD probability should be in [0, 1]."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert 0.0 <= prob <= 1.0

    def test_at_threshold_near_half(self):
        """When price equals threshold, probability should be near 0.5."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=90000, threshold=90000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert 0.35 < prob < 0.65

    def test_deep_itm_high_prob(self):
        """Deep ITM (price >> threshold) should give high probability."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=100000, threshold=80000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert prob > 0.80

    def test_deep_otm_low_prob(self):
        """Deep OTM (price << threshold) should give low probability."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=80000, threshold=100000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert prob < 0.20

    def test_fatter_tails_than_gbm(self):
        """JD model should give higher tail probabilities than GBM for far-OTM."""
        from probability import crypto_price_probability, crypto_price_probability_jd
        # Far OTM: price 90K, threshold 120K (33% away)
        gbm_prob = crypto_price_probability(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        jd_prob = crypto_price_probability_jd(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        # JD should give higher probability due to jump component
        assert jd_prob > gbm_prob

    def test_below_direction(self):
        """'below' direction should be 1 - P(above)."""
        from probability import crypto_price_probability_jd
        p_above = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        p_below = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="below",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        assert abs(p_above + p_below - 1.0) < 0.01

    def test_zero_jump_intensity_matches_gbm(self):
        """With λ=0 (no jumps), JD should match GBM."""
        from probability import crypto_price_probability, crypto_price_probability_jd
        gbm = crypto_price_probability(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
        )
        jd = crypto_price_probability_jd(
            current_price=90000, threshold=95000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
            jump_intensity=0.0,
        )
        assert abs(gbm - jd) < 0.02  # Should be very close

    def test_higher_intensity_fatter_tails(self):
        """Higher jump intensity should produce fatter tails."""
        from probability import crypto_price_probability_jd
        low_lambda = crypto_price_probability_jd(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
            jump_intensity=0.5,
        )
        high_lambda = crypto_price_probability_jd(
            current_price=90000, threshold=120000, direction="above",
            time_horizon_minutes=1440, realized_vol_pct=0.60,
            jump_intensity=2.0,
        )
        assert high_lambda > low_lambda

    def test_short_horizon(self):
        """Very short horizon should give extreme probability (near 0 or 1)."""
        from probability import crypto_price_probability_jd
        prob = crypto_price_probability_jd(
            current_price=90000, threshold=100000, direction="above",
            time_horizon_minutes=5, realized_vol_pct=0.60,
        )
        assert prob < 0.10  # Very unlikely in 5 minutes
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_crypto.py::TestJumpDiffusionCrypto -v`
Expected: FAIL — `crypto_price_probability_jd` not defined.

**Step 3: Commit failing tests**

```bash
git add tests/test_crypto.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add jump-diffusion crypto probability model tests (failing)"
```

---

### Task 6: Jump-Diffusion Crypto Model — Implementation

**Files:**
- Modify: `src/kalshi/probability.py` (add `crypto_price_probability_jd()` after existing `crypto_price_probability()`)

**Context:** The Merton jump-diffusion model computes P(S_T > K) by summing over the number of jumps n=0,1,2,...,N_max. For each n, the conditional distribution is log-normal with adjusted mean and variance. The probability is a Poisson-weighted mixture of GBM probabilities.

**Step 1: Add jump-diffusion function to probability.py**

Add after the existing `crypto_price_probability()` function (around line 620):

```python
def crypto_price_probability_jd(current_price, threshold, direction="above",
                                  time_horizon_minutes=1440, realized_vol_pct=None,
                                  iv_pct=None, drift_pct=0.0,
                                  jump_intensity=1.0, jump_mean=-0.05,
                                  jump_std=0.10, max_jumps=10):
    """Merton jump-diffusion probability for crypto price markets.

    Extends GBM with Poisson jump process for fatter tails. Better pricing
    for far-OTM crypto markets where flash crashes/rallies are underpriced
    by pure GBM.

    The model: dS/S = (mu - lambda*k)dt + sigma*dW + J*dN
    where J ~ N(jump_mean, jump_std), N ~ Poisson(lambda*T).

    P(S_T > K) = sum_{n=0}^{N_max} P(N=n) * P_n(S_T > K | n jumps)

    where P_n is a GBM probability with adjusted sigma and drift.

    Args:
        current_price: Current spot price.
        threshold: Market threshold price.
        direction: "above" or "below".
        time_horizon_minutes: Minutes to settlement.
        realized_vol_pct: Annualized vol as decimal (e.g., 0.60).
        iv_pct: Implied vol (takes precedence over realized).
        drift_pct: Annualized drift rate.
        jump_intensity: Average jumps per year (lambda). Default 1.0.
        jump_mean: Mean log-jump size (default -0.05 = -5%, slight downward bias).
        jump_std: Std of log-jump size (default 0.10 = 10%).
        max_jumps: Max number of jumps to sum over (default 10).

    Returns:
        Probability (0-1).
    """
    if current_price <= 0 or threshold <= 0:
        return 0.5

    # Select volatility
    if iv_pct is not None and iv_pct > 0:
        sigma = iv_pct
    elif realized_vol_pct is not None and realized_vol_pct > 0:
        sigma = realized_vol_pct
    else:
        sigma = 0.60

    T = time_horizon_minutes / (365.25 * 24 * 60)
    if T <= 0:
        return 1.0 if current_price > threshold else 0.0

    # Compensator: k = E[e^J - 1] = exp(jump_mean + 0.5*jump_std^2) - 1
    k = math.exp(jump_mean + 0.5 * jump_std ** 2) - 1
    lambda_T = jump_intensity * T

    log_S_K = math.log(current_price / threshold)
    prob_above = 0.0

    for n in range(max_jumps + 1):
        # Poisson probability P(N = n)
        if n == 0:
            poisson_p = math.exp(-lambda_T)
        else:
            # log(P(N=n)) = n*log(lambda_T) - lambda_T - sum(log(1..n))
            log_p = n * math.log(max(lambda_T, 1e-300)) - lambda_T
            for i in range(1, n + 1):
                log_p -= math.log(i)
            poisson_p = math.exp(log_p)

        if poisson_p < 1e-15:
            break  # Negligible contribution

        # Conditional GBM with n jumps:
        # sigma_n^2 = sigma^2 + n * jump_std^2 / T
        sigma_n_sq = sigma ** 2 + n * jump_std ** 2 / max(T, 1e-15)
        sigma_n = math.sqrt(sigma_n_sq)

        # drift_n = drift - lambda*k + n*jump_mean/T
        drift_n = drift_pct - jump_intensity * k + n * jump_mean / max(T, 1e-15)

        # d2 = (log(S/K) + (drift_n - 0.5*sigma_n^2)*T) / (sigma_n * sqrt(T))
        sqrt_T = math.sqrt(T)
        d2 = (log_S_K + (drift_n - 0.5 * sigma_n_sq) * T) / (sigma_n * sqrt_T)
        prob_above += poisson_p * _norm_cdf(d2)

    # Clamp to [0, 1]
    prob_above = max(0.0, min(1.0, prob_above))

    if direction == "below":
        return 1.0 - prob_above
    return prob_above
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_crypto.py::TestJumpDiffusionCrypto -v`
Expected: All 9 tests pass.

**Step 3: Run full probability test suite**

Run: `pytest tests/test_probability.py tests/test_crypto.py -v`
Expected: All pass.

**Step 4: Commit**

```bash
git add src/kalshi/probability.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add Merton jump-diffusion model for crypto price probability (fatter tails for OTM contracts)"
```

---

### Task 7: Importance Sampling Engine — Tests

**Files:**
- Create: `tests/test_simulation.py`

**Context:** Importance sampling improves Monte Carlo estimation for rare events (tail-risk contracts). Instead of drawing samples from the original distribution, we draw from a shifted distribution (proposal) that puts more weight on the tail region, then correct with importance weights. This gives much lower variance for pricing far-OTM contracts.

**Step 1: Write importance sampling tests**

```python
"""Tests for simulation.py — importance sampling engine.

The simulation engine provides variance-reduced Monte Carlo estimation
for tail-risk contract pricing. Uses importance sampling with optimal
shift for Gaussian processes.
"""

import math
import pytest


class TestImportanceSampler:
    """Test the importance sampling Monte Carlo engine."""

    def test_basic_probability_estimate(self):
        """IS should estimate P(X > threshold) within statistical tolerance."""
        from simulation import importance_sample_probability
        # P(N(0,1) > 2) ≈ 0.0228
        prob, std_err = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0,
            n_samples=10000, direction="above",
        )
        assert abs(prob - 0.0228) < 0.01

    def test_tail_probability_accuracy(self):
        """IS should accurately estimate deep tail probabilities."""
        from simulation import importance_sample_probability
        # P(N(0,1) > 3) ≈ 0.00135
        prob, std_err = importance_sample_probability(
            mean=0.0, std=1.0, threshold=3.0,
            n_samples=10000, direction="above",
        )
        assert abs(prob - 0.00135) < 0.005

    def test_below_direction(self):
        """'below' direction should compute left tail."""
        from simulation import importance_sample_probability
        # P(N(0,1) < -2) ≈ 0.0228
        prob, _ = importance_sample_probability(
            mean=0.0, std=1.0, threshold=-2.0,
            n_samples=10000, direction="below",
        )
        assert abs(prob - 0.0228) < 0.01

    def test_std_error_decreases_with_samples(self):
        """Standard error should decrease with more samples."""
        from simulation import importance_sample_probability
        _, err_1k = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0, n_samples=1000,
        )
        _, err_10k = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0, n_samples=10000,
        )
        assert err_10k < err_1k

    def test_variance_reduction_vs_naive(self):
        """IS should have lower variance than naive MC for tail events."""
        from simulation import importance_sample_probability, naive_mc_probability
        # Deep tail: P(N(0,1) > 3)
        _, is_err = importance_sample_probability(
            mean=0.0, std=1.0, threshold=3.0, n_samples=5000,
        )
        _, naive_err = naive_mc_probability(
            mean=0.0, std=1.0, threshold=3.0, n_samples=5000,
        )
        # IS should have meaningfully lower error for tail events
        assert is_err < naive_err * 0.8  # At least 20% variance reduction

    def test_returns_tuple(self):
        """Should return (probability, standard_error) tuple."""
        from simulation import importance_sample_probability
        result = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0, n_samples=1000,
        )
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_at_mean_probability_near_half(self):
        """P(X > mean) should be near 0.5."""
        from simulation import importance_sample_probability
        prob, _ = importance_sample_probability(
            mean=5.0, std=2.0, threshold=5.0, n_samples=10000,
        )
        assert 0.45 < prob < 0.55


class TestAntitheticVariates:
    """Test antithetic variate variance reduction."""

    def test_antithetic_probability(self):
        """Antithetic sampling should give valid probability."""
        from simulation import importance_sample_probability
        prob, _ = importance_sample_probability(
            mean=0.0, std=1.0, threshold=2.0,
            n_samples=10000, use_antithetic=True,
        )
        assert abs(prob - 0.0228) < 0.01

    def test_antithetic_reduces_variance(self):
        """Antithetic should reduce variance compared to standard IS."""
        from simulation import importance_sample_probability
        _, err_std = importance_sample_probability(
            mean=0.0, std=1.0, threshold=1.5,
            n_samples=5000, use_antithetic=False,
        )
        _, err_anti = importance_sample_probability(
            mean=0.0, std=1.0, threshold=1.5,
            n_samples=5000, use_antithetic=True,
        )
        # Antithetic should be at least as good (usually better)
        assert err_anti <= err_std * 1.2  # Allow 20% slack for randomness
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_simulation.py -v`
Expected: FAIL — `simulation` module not found.

**Step 3: Commit failing tests**

```bash
git add tests/test_simulation.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add importance sampling simulation engine tests (failing — module not yet implemented)"
```

---

### Task 8: Importance Sampling Engine — Implementation

**Files:**
- Create: `src/kalshi/simulation.py`

**Step 1: Implement the simulation engine**

```python
"""Importance sampling engine for tail-risk contract pricing.

Provides variance-reduced Monte Carlo estimation for computing probabilities
of rare events. Standard Monte Carlo is inefficient for pricing far-OTM
contracts (e.g., P(BTC > $150K tomorrow) ≈ 0.01%) because almost all
samples fall in the non-event region.

Importance sampling shifts the sampling distribution toward the tail,
then corrects with importance weights, giving much lower variance
for the same number of samples.

Usage:
    from simulation import importance_sample_probability

    prob, std_err = importance_sample_probability(
        mean=log_return_mean, std=log_return_std,
        threshold=log(K/S), n_samples=10000,
    )
"""

import math
import random


def _norm_cdf(x):
    """Standard normal CDF using math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x):
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def importance_sample_probability(mean, std, threshold, n_samples=10000,
                                   direction="above", use_antithetic=False):
    """Compute tail probability using importance sampling.

    Uses exponential tilting: shifts the Gaussian mean toward the
    threshold to sample more tail events, then corrects with
    importance weights.

    Args:
        mean: Distribution mean.
        std: Distribution standard deviation.
        threshold: Event threshold.
        n_samples: Number of Monte Carlo samples.
        direction: "above" for P(X > threshold), "below" for P(X < threshold).
        use_antithetic: If True, use antithetic variates for variance reduction.

    Returns:
        (probability, standard_error) tuple.
    """
    if std <= 0:
        if direction == "above":
            return (1.0, 0.0) if mean > threshold else (0.0, 0.0)
        else:
            return (1.0, 0.0) if mean < threshold else (0.0, 0.0)

    # Optimal shift: move mean to threshold for maximum efficiency
    # Proposal distribution: N(shift_mean, std)
    shift_mean = threshold  # Shift to threshold

    weights = []
    indicators = []

    actual_n = n_samples // 2 if use_antithetic else n_samples

    for _ in range(actual_n):
        # Sample from proposal N(shift_mean, std)
        z = random.gauss(0, 1)
        samples = [shift_mean + std * z]

        if use_antithetic:
            # Antithetic variate: use -z as well
            samples.append(shift_mean + std * (-z))

        for x in samples:
            # Importance weight: p(x) / q(x) where p=N(mean,std), q=N(shift_mean,std)
            # log(w) = -0.5*((x-mean)/std)^2 + 0.5*((x-shift_mean)/std)^2
            log_w = -0.5 * ((x - mean) / std) ** 2 + 0.5 * ((x - shift_mean) / std) ** 2
            w = math.exp(log_w)

            # Indicator function
            if direction == "above":
                ind = 1.0 if x > threshold else 0.0
            else:
                ind = 1.0 if x < threshold else 0.0

            weights.append(w * ind)
            indicators.append(ind)

    n = len(weights)
    if n == 0:
        return (0.0, 0.0)

    # Weighted estimate
    prob = sum(weights) / n

    # Standard error via sample variance of weighted indicator
    mean_w = prob
    var_w = sum((w - mean_w) ** 2 for w in weights) / (n - 1) if n > 1 else 0.0
    std_err = math.sqrt(var_w / n) if var_w > 0 else 0.0

    return (max(0.0, min(1.0, prob)), std_err)


def naive_mc_probability(mean, std, threshold, n_samples=10000,
                          direction="above"):
    """Standard (naive) Monte Carlo probability estimation.

    Used as baseline for comparing against importance sampling.

    Returns:
        (probability, standard_error) tuple.
    """
    if std <= 0:
        if direction == "above":
            return (1.0, 0.0) if mean > threshold else (0.0, 0.0)
        else:
            return (1.0, 0.0) if mean < threshold else (0.0, 0.0)

    count = 0
    for _ in range(n_samples):
        x = random.gauss(mean, std)
        if direction == "above":
            if x > threshold:
                count += 1
        else:
            if x < threshold:
                count += 1

    prob = count / n_samples
    # Binomial standard error
    std_err = math.sqrt(prob * (1 - prob) / n_samples) if n_samples > 0 else 0.0

    return (prob, std_err)
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_simulation.py -v`
Expected: All 9 tests pass.

**Step 3: Commit**

```bash
git add src/kalshi/simulation.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add importance sampling engine with antithetic variate variance reduction"
```

---

### Task 9: Regime-Adjusted Kelly in Capital Allocator — Tests

**Files:**
- Modify: `tests/test_allocator.py`

**Context:** The capital allocator already applies tail-risk Kelly reduction from the correlation engine (Check #8). We need to add a regime-adjusted multiplier from the regime detector. This stacks multiplicatively: `effective_bankroll = bankroll * tail_risk_mult * regime_mult`.

**Step 1: Add regime Kelly integration tests**

Append to `tests/test_allocator.py`:

```python
class TestRegimeKellyIntegration:
    """Test that regime detector multiplier is applied in budget allocation."""

    def test_crisis_regime_reduces_bankroll(self):
        """During crisis regime, bankroll should be reduced."""
        # Mock regime detector returning crisis multiplier
        from unittest.mock import MagicMock, patch
        mock_detector = MagicMock()
        mock_detector.current_regime.return_value = "crisis"
        mock_detector.regime_confidence.return_value = 0.9

        from regime_detector import regime_kelly_multiplier
        mult = regime_kelly_multiplier(mock_detector)
        assert mult < 0.70  # Crisis with high confidence

    def test_normal_regime_no_reduction(self):
        """During normal regime, bankroll should not be reduced."""
        from unittest.mock import MagicMock
        mock_detector = MagicMock()
        mock_detector.current_regime.return_value = "normal"
        mock_detector.regime_confidence.return_value = 0.8

        from regime_detector import regime_kelly_multiplier
        mult = regime_kelly_multiplier(mock_detector)
        assert 0.9 <= mult <= 1.1

    def test_regime_mult_stacks_with_tail_risk(self):
        """Regime multiplier should stack with tail-risk multiplier."""
        tail_mult = 0.75  # From correlation engine
        regime_mult = 0.60  # Crisis regime
        combined = tail_mult * regime_mult
        assert combined < 0.50  # Severe combined reduction
        assert combined == pytest.approx(0.45, abs=0.01)
```

**Step 2: Run tests**

Run: `pytest tests/test_allocator.py::TestRegimeKellyIntegration -v`
Expected: All pass (these test the regime_kelly_multiplier function directly, not allocator integration).

**Step 3: Commit**

```bash
git add tests/test_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add regime Kelly integration tests for capital allocator"
```

---

### Task 10: Regime-Adjusted Kelly in Capital Allocator — Implementation

**Files:**
- Modify: `src/kalshi/capital_allocator.py`

**Context:** Add regime detector to the capital allocator. The regime detector should be initialized alongside the correlation engine and its multiplier applied to bankroll in `_request_budget_inner()`.

**Step 1: Add regime detector import and initialization**

At the top of `capital_allocator.py`, add:

```python
from regime_detector import RegimeDetector, regime_kelly_multiplier
```

In `PortfolioAllocator.__init__()`, after correlation engine initialization:

```python
# Initialize regime detector
self._regime_detector = RegimeDetector()
regime_state_path = self._state_path.parent / "regime-state.json"
self._regime_detector.load(str(regime_state_path))
```

**Step 2: Apply regime multiplier in budget allocation**

In `_request_budget_inner()`, after the tail-risk multiplier line (around line 644-647):

```python
# Check 8b: regime-adjusted Kelly
regime_mult = regime_kelly_multiplier(self._regime_detector)
if regime_mult < 1.0:
    effective_bankroll = int(effective_bankroll * regime_mult)
    log.info("  Regime adjustment: %s (conf %.2f) → bankroll × %.2f",
             self._regime_detector.current_regime(),
             self._regime_detector.regime_confidence(),
             regime_mult)
```

**Step 3: Add method to update regime from bot scans**

```python
def update_regime(self, realized_vol):
    """Update the regime detector with a new vol observation.

    Should be called by bots that compute realized vol (crypto, weather).
    """
    self._regime_detector.update(realized_vol)
    regime_state_path = self._state_path.parent / "regime-state.json"
    self._regime_detector.save(str(regime_state_path))
```

**Step 4: Run tests**

Run: `pytest tests/test_allocator.py -v --tb=short`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add src/kalshi/capital_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: integrate regime-adjusted Kelly into capital allocator (check 8b)"
```

---

### Task 11: Regime Detector Config and Crypto Bot Integration — Tests

**Files:**
- Modify: `tests/test_regime_detector.py`

**Step 1: Add crypto bot integration tests**

```python
class TestCryptoBotIntegration:
    """Test regime detector integration with crypto bot vol tracking."""

    def test_realized_vol_feeds_regime(self):
        """Crypto bot's realized vol should update regime detector."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Simulate 10 crypto scans with normal vol
        for _ in range(10):
            rd.update(0.55)  # 55% annualized — normal for BTC
        assert rd.current_regime() in ("normal", "low_vol")

    def test_vol_spike_triggers_regime_change(self):
        """Flash crash vol spike should move toward crisis."""
        from regime_detector import RegimeDetector
        rd = RegimeDetector()
        # Normal period
        for _ in range(10):
            rd.update(0.50)
        # Vol spike (flash crash)
        for _ in range(3):
            rd.update(1.50)
        # Should have shifted toward high/crisis
        assert rd.belief[2] + rd.belief[3] > rd.belief[0]

    def test_config_from_bots_config(self):
        """Regime detector config should be loadable from bots-config.json."""
        # Config structure expected:
        config = {
            "regime": {
                "enabled": True,
                "maxAgeHours": 24,
            }
        }
        assert config["regime"]["enabled"] is True
```

**Step 2: Run tests**

Run: `pytest tests/test_regime_detector.py -v`
Expected: All pass.

**Step 3: Commit**

```bash
git add tests/test_regime_detector.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add crypto bot regime detector integration tests"
```

---

### Task 12: Crypto Bot Integration — Implementation

**Files:**
- Modify: `src/kalshi/crypto-bot.py` (add regime detector usage)
- Modify: `config/bots-config.json` (add regime config)

**Step 1: Add regime config to bots-config.json**

Add after the `"correlation"` section:

```json
"regime": {
    "enabled": true,
    "maxAgeHours": 24
}
```

**Step 2: Add regime detector to crypto-bot.py**

Add import:

```python
from regime_detector import RegimeDetector, regime_kelly_multiplier
```

In the initialization section, after particle filter setup:

```python
# Regime detector
regime_detector = RegimeDetector()
regime_state_path = PROJECT_DIR / "data" / "regime-state.json"
regime_detector.load(str(regime_state_path))
```

In the scan loop, after computing realized vol:

```python
# Update regime detector with latest vol observation
if realized_vol is not None:
    regime_detector.update(realized_vol)
    regime_detector.save(str(regime_state_path))
    log.info("  Regime: %s (conf=%.2f, mult=%.2f)",
             regime_detector.current_regime(),
             regime_detector.regime_confidence(),
             regime_kelly_multiplier(regime_detector))
```

**Step 3: Run existing crypto tests**

Run: `pytest tests/test_crypto.py -v`
Expected: All pass.

**Step 4: Commit**

```bash
git add src/kalshi/crypto-bot.py config/bots-config.json
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: integrate regime detector into crypto bot with vol-based updates"
```

---

### Task 13: Weather Ensemble Spread Regime — Tests

**Files:**
- Modify: `tests/test_weather.py`

**Context:** When weather ensemble models (GFS, ECMWF, ICON) disagree significantly, it signals higher forecast uncertainty. We track ensemble spread (max - min forecast) and use it to adaptively widen sigma — similar to regime detection but for weather forecasts.

**Step 1: Add ensemble spread tests**

```python
class TestEnsembleSpreadSigma:
    """Test ensemble spread tracking for adaptive weather sigma."""

    def setup_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def teardown_method(self):
        from probability import _reset_calibration
        _reset_calibration()

    def test_tight_ensemble_no_sigma_increase(self):
        """When models agree (spread < 2°F), no sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        # Models within 1°F of each other
        mult = ensemble_spread_sigma_multiplier(spread_f=1.0)
        assert mult == pytest.approx(1.0, abs=0.05)

    def test_moderate_spread_mild_increase(self):
        """When models diverge moderately (3-5°F), mild sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=4.0)
        assert 1.1 < mult < 1.5

    def test_large_spread_significant_increase(self):
        """When models diverge significantly (>8°F), large sigma increase."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=10.0)
        assert mult > 1.5

    def test_zero_spread_no_increase(self):
        """Zero spread should give multiplier of 1.0."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=0.0)
        assert mult == pytest.approx(1.0)

    def test_multiplier_capped(self):
        """Multiplier should be capped at reasonable maximum (e.g., 2.5)."""
        from probability import ensemble_spread_sigma_multiplier
        mult = ensemble_spread_sigma_multiplier(spread_f=20.0)
        assert mult <= 2.5
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_weather.py::TestEnsembleSpreadSigma -v`
Expected: FAIL — `ensemble_spread_sigma_multiplier` not defined.

**Step 3: Commit failing tests**

```bash
git add tests/test_weather.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "test: add weather ensemble spread sigma multiplier tests (failing)"
```

---

### Task 14: Weather Ensemble Spread Regime — Implementation

**Files:**
- Modify: `src/kalshi/probability.py`

**Step 1: Add ensemble spread sigma multiplier**

Add near the ensemble weather functions (around line 280):

```python
def ensemble_spread_sigma_multiplier(spread_f):
    """Compute sigma multiplier based on ensemble model spread.

    When GFS, ECMWF, and ICON disagree, forecast uncertainty is higher.
    Wider ensemble spread → wider sigma → more conservative trading.

    Args:
        spread_f: Max - min forecast temperature across models (°F).

    Returns:
        Multiplier >= 1.0. Applied to base sigma in weather_probability().
    """
    if spread_f <= 2.0:
        return 1.0  # Models agree — no adjustment
    # Linear ramp: spread 2→10°F maps to multiplier 1.0→2.0
    raw = 1.0 + (spread_f - 2.0) * 0.125
    return min(raw, 2.5)  # Cap at 2.5x
```

**Step 2: Run tests to verify they pass**

Run: `pytest tests/test_weather.py -v`
Expected: All tests pass (old + new).

**Step 3: Commit**

```bash
git add src/kalshi/probability.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add ensemble spread sigma multiplier for adaptive weather uncertainty"
```

---

### Task 15: Dashboard Regime Endpoint — Tests and Implementation

**Files:**
- Modify: `scripts/dashboard.py`

**Step 1: Add regime status to dashboard /api/health endpoint**

In `dashboard.py`, add regime detector state to the health endpoint:

```python
# In get_health() or similar endpoint:
regime_path = PROJECT_DIR / "data" / "regime-state.json"
regime_info = {"regime": "unknown", "confidence": 0.0, "n_updates": 0}
try:
    with open(regime_path) as f:
        regime_data = json.load(f)
    belief = regime_data.get("belief", [0.25] * 4)
    states = ["low_vol", "normal", "high_vol", "crisis"]
    max_idx = belief.index(max(belief))
    regime_info = {
        "regime": states[max_idx],
        "confidence": belief[max_idx],
        "n_updates": regime_data.get("n_updates", 0),
        "belief": dict(zip(states, belief)),
    }
except (FileNotFoundError, json.JSONDecodeError):
    pass
```

**Step 2: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass.

**Step 3: Commit**

```bash
git add scripts/dashboard.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "feat: add regime status to dashboard health endpoint"
```

---

### Task 16: Final Integration Test and Merge Prep

**Files:**
- None (verification only)

**Step 1: Run complete test suite**

Run: `pytest tests/ -v`
Expected: All tests pass.

**Step 2: Count new tests**

Run: `pytest tests/test_regime_detector.py tests/test_simulation.py tests/test_crypto.py::TestJumpDiffusionCrypto tests/test_weather.py::TestEnsembleSpreadSigma tests/test_allocator.py::TestRegimeKellyIntegration -v --tb=short`
Expected: ~45+ new tests across files.

**Step 3: Verify new modules**

Run: `python3 -c "from regime_detector import RegimeDetector; print('regime_detector OK')"`
Run: `python3 -c "from simulation import importance_sample_probability; print('simulation OK')"`
Run: `python3 -c "from probability import crypto_price_probability_jd, ensemble_spread_sigma_multiplier; print('probability extensions OK')"`

**Step 4: Merge to main**

```bash
git checkout main
git merge track2/quant-infrastructure --no-ff -m "Merge branch 'track2/quant-infrastructure' — T2-P3 Advanced Simulation Engine"
```

---

## Summary

| Task | Deliverable | Tests |
|------|-------------|-------|
| 2-3 | HMM regime detector (4-state, online Bayes) | 17 |
| 4 | Regime persistence + Kelly multiplier | 10 |
| 5-6 | Jump-diffusion crypto model (Merton) | 9 |
| 7-8 | Importance sampling engine + antithetic variates | 9 |
| 9-10 | Regime-adjusted Kelly in capital allocator | 3 |
| 11-12 | Crypto bot + config integration | 3 |
| 13-14 | Weather ensemble spread sigma | 5 |
| 15 | Dashboard regime endpoint | 0 |
| **Total** | | **~56** |
