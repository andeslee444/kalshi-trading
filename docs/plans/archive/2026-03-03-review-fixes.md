# Code Review Fixes — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all critical and important issues identified in the consolidated quant desk code review — 6 critical bugs, 16 important issues, and key structural improvements.

**Architecture:** Fixes span 10 source files and 5 test files across the correlation engine, regime detector, particle filter, macro engine, source monitor, capital allocator, and supervisor. All changes are backwards-compatible. No new dependencies.

**Tech Stack:** Python 3, pytest, existing project modules.

**Branch:** Work directly on `main` — these are bug fixes to already-merged code.

---

## Group 1: Critical Bug Fixes (Tasks 1-5)

### Task 1: Fix Self-Correlation Bug in Correlation Engine

**Files:**
- Modify: `src/kalshi/correlation_engine.py:149-153`
- Modify: `tests/test_correlation_engine.py`

**Context:** `get_ticker_correlation(same, same)` returns 0.90 (intra-factor) instead of 1.0. This underestimates VaR by ~5%.

**Step 1: Write the failing test**

Add to `tests/test_correlation_engine.py`:

```python
def test_same_ticker_correlation_is_one(self):
    """A ticker is perfectly correlated with itself."""
    engine = CorrelationEngine()
    corr = engine.get_ticker_correlation("KXCPI-26MAY-T20", "KXCPI-26MAY-T20")
    assert corr == 1.0

def test_same_factor_different_tickers_is_intra(self):
    """Two different tickers in same factor get intra-factor (0.90)."""
    engine = CorrelationEngine()
    corr = engine.get_ticker_correlation("KXCPI-26MAY-T20", "KXCPI-26MAY-T21")
    assert corr == 0.90
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_correlation_engine.py::test_same_ticker_correlation_is_one -v`
Expected: FAIL — returns 0.90 instead of 1.0.

**Step 3: Fix `get_ticker_correlation`**

In `src/kalshi/correlation_engine.py`, replace lines 149-153:

```python
    def get_ticker_correlation(self, ticker_a: str, ticker_b: str) -> float:
        """Get correlation between two tickers via their factor groups."""
        if ticker_a.upper() == ticker_b.upper():
            return 1.0
        fa = self.ticker_to_factor(ticker_a)
        fb = self.ticker_to_factor(ticker_b)
        return self.get_factor_correlation(fa, fb)
```

**Step 4: Run tests**

Run: `pytest tests/test_correlation_engine.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add src/kalshi/correlation_engine.py tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: self-correlation returns 1.0 instead of intra-factor 0.90

A ticker is perfectly correlated with itself by definition.
The previous behavior underestimated VaR by ~5%."
```

---

### Task 2: Fix Philadelphia City Code Mismatch

**Files:**
- Modify: `src/kalshi/correlation_engine.py:48-57`
- Modify: `tests/test_correlation_engine.py`

**Context:** Kalshi tickers use `PHIL` (e.g., `KXHIGHPHIL-26MAR3-T50`), but correlation engine maps `PHI` to `WEATHER_NE`. Philadelphia trades bypass Northeast regional correlation protection.

**Step 1: Write the failing test**

Add to `tests/test_correlation_engine.py`:

```python
def test_phil_ticker_maps_to_northeast(self):
    """KXHIGHPHIL tickers should map to WEATHER_NE factor."""
    engine = CorrelationEngine()
    factor = engine.ticker_to_factor("KXHIGHPHIL-26MAR3-T50")
    assert factor == "WEATHER_NE"

def test_phil_ny_correlated(self):
    """Philadelphia and New York should be correlated (same region)."""
    engine = CorrelationEngine()
    corr = engine.get_ticker_correlation("KXHIGHPHIL-26MAR3-T50", "KXHIGHNY-26MAR3-T50")
    assert corr == 0.90  # same factor = intra-factor
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_correlation_engine.py::test_phil_ticker_maps_to_northeast -v`
Expected: FAIL — returns `WEATHER_PHIL` instead of `WEATHER_NE`.

**Step 3: Add `PHIL` to `WEATHER_CITY_FACTORS`**

In `src/kalshi/correlation_engine.py`, replace lines 48-57:

```python
WEATHER_CITY_FACTORS = {
    "HOU": "WEATHER_SOUTH_TX",
    "AUS": "WEATHER_SOUTH_TX",
    "NY": "WEATHER_NE",
    "PHI": "WEATHER_NE",
    "PHIL": "WEATHER_NE",
    "MIA": "WEATHER_SE",
    "LAX": "WEATHER_W",
    "CHI": "WEATHER_MW",
    "DEN": "WEATHER_MT",
}
```

**Step 4: Run tests**

Run: `pytest tests/test_correlation_engine.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add src/kalshi/correlation_engine.py tests/test_correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: add PHIL city code to correlation engine

Kalshi uses PHIL (not PHI) for Philadelphia tickers.
Without this, Philly trades bypassed Northeast regional correlation limits."
```

---

### Task 3: Fix Negative Volatility in Regime Detector

**Files:**
- Modify: `src/kalshi/regime_detector.py:77-82`
- Modify: `tests/test_regime_detector.py`

**Context:** `update(vol=-0.5)` drives belief to 95.3% crisis, triggering 50% Kelly reduction. Negative vol is physically meaningless.

**Step 1: Write the failing test**

Add to `tests/test_regime_detector.py`:

```python
def test_negative_vol_ignored():
    """Negative volatility should be silently ignored."""
    rd = RegimeDetector()
    initial_belief = rd.belief[:]
    rd.update(-0.5)
    assert rd.belief == initial_belief
    assert rd.n_updates == 0  # should not count as an update
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_regime_detector.py::test_negative_vol_ignored -v`
Expected: FAIL — belief changes to crisis.

**Step 3: Add guard to `update()`**

In `src/kalshi/regime_detector.py`, replace lines 77-82:

```python
    def update(self, observed_vol):
        """Online Bayesian update: predict -> observe -> normalize.

        Args:
            observed_vol: Realized annualized vol as decimal (e.g., 0.60 = 60%).
                          Negative values are silently ignored.
        """
        if observed_vol < 0:
            return
```

**Step 4: Fix docstring on line 27**

Replace line 27:

```python
# Default emission parameters (Gaussian: vol observations given regime)
```

**Step 5: Run tests**

Run: `pytest tests/test_regime_detector.py -v`
Expected: All pass.

**Step 6: Commit**

```bash
git add src/kalshi/regime_detector.py tests/test_regime_detector.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: reject negative volatility in regime detector

Negative vol is physically meaningless but was driving the HMM
into crisis state, triggering erroneous 50% Kelly reduction.
Also fix docstring: emissions are Gaussian, not log-normal."
```

---

### Task 4: Fix Particle Filter Deserialize Length Validation

**Files:**
- Modify: `src/kalshi/particle_filter.py:234-247`
- Modify: `tests/test_particle_filter.py`

**Context:** If saved state has mismatched particles/weights array lengths (corruption), the filter silently operates with mismatched arrays, causing IndexError.

**Step 1: Write the failing test**

Add to `tests/test_particle_filter.py`:

```python
def test_deserialize_mismatched_lengths_resets():
    """Mismatched particles/weights should create a fresh filter."""
    state = {
        "config": {"n_particles": 200},
        "particles": [0.5] * 200,
        "weights": [0.005] * 150,  # 150 != 200
        "update_count": 10,
    }
    pf = ParticleFilter.deserialize(state)
    assert len(pf.particles) == len(pf.weights)
    assert pf._update_count == 0  # reset to fresh
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_particle_filter.py::test_deserialize_mismatched_lengths_resets -v`
Expected: FAIL — lengths are 200 and 150.

**Step 3: Add validation to `deserialize()`**

In `src/kalshi/particle_filter.py`, replace the `deserialize` method (lines 234-247):

```python
    @classmethod
    def deserialize(cls, state: dict) -> "ParticleFilter":
        """Reconstruct a filter from serialized state."""
        config_data = state.get("config", {})
        config = FilterConfig(**{k: v for k, v in config_data.items()
                                  if k in FilterConfig.__dataclass_fields__})
        pf = cls(config=config)
        particles = state.get("particles", pf.particles)
        weights = state.get("weights", pf.weights)
        if len(particles) != len(weights):
            _log.warning("Particle/weight length mismatch (%d vs %d), starting fresh",
                         len(particles), len(weights))
            return cls(config=config)
        pf.particles = particles
        pf.weights = weights
        pf._update_count = state.get("update_count", 0)
        pf._recent_directions = state.get("recent_directions", [])
        pf._last_prob = state.get("last_prob", 0.5)
        pf.n_particles = len(pf.particles)
        return pf
```

**Step 4: Run tests**

Run: `pytest tests/test_particle_filter.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add src/kalshi/particle_filter.py tests/test_particle_filter.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: validate particle/weight array lengths on deserialize

Mismatched arrays from state corruption could cause IndexError.
Now resets to fresh filter with a warning."
```

---

### Task 5: Add Entertainment Bot to Supervisor DISABLED_BY_DEFAULT

**Files:**
- Modify: `scripts/supervisor.py:57`
- Modify: `scripts/supervisor.py:53`

**Context:** Entertainment bot was consolidated into source-monitor (T1-P2), config sets `enabled: false`, but supervisor still starts it because `DISABLED_BY_DEFAULT` doesn't include `"entertainment"`.

**Step 1: Fix DISABLED_BY_DEFAULT**

In `scripts/supervisor.py`, replace line 57:

```python
DISABLED_BY_DEFAULT = {"mm", "demo", "entertainment"}
```

**Step 2: Remove entertainment from DAEMON_BOTS**

In `scripts/supervisor.py`, replace line 53:

```python
DAEMON_BOTS = {"weather", "crypto", "economics", "positions", "monitor", "beatrelease", "arb"}
```

**Step 3: Verify supervisor still works**

Run: `python3 scripts/supervisor.py status`
Expected: Entertainment shows as "disabled".

**Step 4: Commit**

```bash
git add scripts/supervisor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: disable entertainment bot in supervisor

Entertainment bot was consolidated into source-monitor in T1-P2,
but supervisor still started it. Add to DISABLED_BY_DEFAULT and
remove from DAEMON_BOTS to prevent duplicate trades."
```

---

## Group 2: Macro Engine Config & Cache Fixes (Tasks 6-7)

### Task 6: Wire Macro Engine Config Values

**Files:**
- Modify: `src/kalshi/macro_engine.py:314-338`
- Modify: `src/kalshi/economics-bot.py:588-597`
- Modify: `tests/test_macro_engine.py`

**Context:** `bots-config.json` declares `enabled`, `cacheTtlHours`, `biasClampPp`, `sigmaTighteningMax`, `maxSentimentArticles` but the code ignores them all.

**Step 1: Write failing test for `enabled` flag**

Add to `tests/test_macro_engine.py`:

```python
def test_disabled_engine_returns_empty_signal():
    """When macro.enabled is false, compute_signal returns zero-bias signal."""
    engine = MacroEngine(config={"enabled": False})
    signal = engine.compute_signal(cleveland_nowcast=2.8)
    assert signal.cpi_bias == 0.0
    assert signal.confidence == 0.0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_macro_engine.py::test_disabled_engine_returns_empty_signal -v`
Expected: FAIL — engine ignores enabled flag and makes network calls.

**Step 3: Wire config values in `MacroEngine.__init__`**

In `src/kalshi/macro_engine.py`, replace the `__init__` method and `MACRO_CACHE_TTL` constant:

Replace lines 315-316:
```python
MACRO_CACHE_TTL = 4 * 3600  # default, overridden by config
```

Replace lines 332-338:
```python
    def __init__(self, config: Optional[dict] = None):
        self._config = config or {}
        self._enabled = self._config.get("enabled", True)
        self._cache_ttl = self._config.get("cacheTtlHours", 4) * 3600
        self._bias_clamp = self._config.get("biasClampPp", 0.15)
        self._sigma_tightening_max = self._config.get("sigmaTighteningMax", 0.30)
        self._max_sentiment_articles = self._config.get("maxSentimentArticles", 5)
        self._fred = FREDClient(api_key=self._config.get("fredApiKey", ""))
        self._truflation = TruflationClient()
        self._rss = RSSFeedParser()
        self._deepseek_key = self._load_deepseek_key()
        self._sentiment = SentimentExtractor(api_key=self._deepseek_key)
```

**Step 4: Wire values into `compute_signal`**

In `compute_signal()`, add at the top (after `import datetime`):

```python
        if not self._enabled:
            return MacroSignal(timestamp=datetime.datetime.now().isoformat())
```

Replace the bias clamp line (line ~483):
```python
            signal.cpi_bias = max(-self._bias_clamp, min(self._bias_clamp, signal.cpi_bias))
```

Replace the `relevant[:3]` cap in RSS loop (line ~459):
```python
            for entry in relevant[:self._max_sentiment_articles]:
```

**Step 5: Wire config into `compute_sigma_multiplier`**

Replace line 407:
```python
        return 1.0 - self._sigma_tightening_max * ((confidence - 0.3) / 0.7)
```

**Step 6: Wire cache TTL in `load_cached_signal`**

Replace line 536:
```python
            if time.time() - data.get("cached_at", 0) > self._cache_ttl:
```

**Step 7: Run tests**

Run: `pytest tests/test_macro_engine.py -v`
Expected: All pass.

**Step 8: Commit**

```bash
git add src/kalshi/macro_engine.py tests/test_macro_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: wire macro engine config values from bots-config.json

Previously enabled, cacheTtlHours, biasClampPp, sigmaTighteningMax,
and maxSentimentArticles were declared in config but ignored.
Setting enabled: false now actually disables the macro engine."
```

---

### Task 7: Use Cached Signal as Fallback + Fix Nowcast Mutation

**Files:**
- Modify: `src/kalshi/economics-bot.py:588-597`

**Context:** (1) `load_cached_signal()` exists but is never called — a stale signal is better than no signal. (2) `nowcast["cpi_yoy"] += bias` mutates dict in-place, which would compound if called twice.

**Step 1: Fix the economics bot macro integration**

Replace lines 588-597 in `src/kalshi/economics-bot.py`:

```python
    # Macro adjustment (if available)
    macro_signal = None
    try:
        macro_signal = macro.compute_signal(cleveland_nowcast=nowcast.get("cpi_yoy") if nowcast else None)
    except Exception as e:
        log.warning(f"  Macro engine error (non-fatal): {e}")
        # Fallback to cached signal
        try:
            macro_signal = macro.load_cached_signal()
            if macro_signal:
                log.info(f"  Using cached macro signal (bias={macro_signal.cpi_bias:+.3f}%)")
        except Exception:
            pass

    if macro_signal and macro_signal.confidence > 0.2 and nowcast:
        if "cpi_yoy" in nowcast:
            adjusted_cpi = nowcast["cpi_yoy"] + macro_signal.cpi_bias
            nowcast["cpi_yoy"] = adjusted_cpi
            log.info(f"  Macro-adjusted CPI nowcast: {adjusted_cpi:.3f}% "
                     f"(bias={macro_signal.cpi_bias:+.3f}%, conf={macro_signal.confidence:.2f})")
```

**Step 2: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass.

**Step 3: Commit**

```bash
git add src/kalshi/economics-bot.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: use cached macro signal as fallback, avoid nowcast mutation

1. Falls back to load_cached_signal() when compute_signal() fails
2. Uses local variable for adjusted CPI instead of mutating nowcast dict"
```

---

## Group 3: Source Monitor Fixes (Tasks 8-9)

### Task 8: Extract NWS Edge Helper to Deduplicate Logic

**Files:**
- Modify: `src/kalshi/source-monitor.py:968-980, 1044-1055`
- Modify: `tests/test_source_monitor.py:313-374`

**Context:** CI-based min_edge logic is copy-pasted in YES-side and NO-side branches. Tests reimplement the logic instead of testing actual code.

**Step 1: Add helper function to source-monitor.py**

Add after the `_validate_market_cluster` function (around line 109):

```python
def _nws_min_edge(running_high, threshold, hour, is_bracket):
    """Compute minimum edge threshold for NWS trades based on CI confidence.

    Uses 99% CI of NWS observation error:
      very confident (margin > ci_99):    5% min edge
      moderate (margin > ci_99 * 0.5):   10% min edge
      uncertain:                         15% min edge
      brackets:                          20% min edge (always)
    """
    if is_bracket:
        return 0.20
    sigma = nws_sigma_for_hour(hour)
    ci_margin = abs(running_high - threshold)
    ci_99 = 2.576 * sigma
    if ci_margin > ci_99:
        return 0.05
    elif ci_margin > ci_99 * 0.5:
        return 0.10
    return 0.15
```

**Step 2: Replace YES-side duplicate (lines 968-980)**

Replace with:
```python
                    min_edge = _nws_min_edge(running_high, threshold, now.hour, is_bracket)
```

**Step 3: Replace NO-side duplicate (lines 1044-1055)**

Replace with:
```python
                    min_edge = _nws_min_edge(running_high, threshold, now.hour, is_bracket)
```

**Step 4: Fix margin variable shadowing (line 973)**

The `margin` on line 961 (`running_high - threshold`) is used in the reasoning string on line 1012. After extracting the helper, the CI computation no longer shadows it. Verify line 1012 still references the correct `margin` variable (it should — `margin` is still set on line 961).

**Step 5: Update tests to call actual code**

Replace `TestNWSEdgeThresholds` in `tests/test_source_monitor.py`:

```python
class TestNWSEdgeThresholds:
    """Test _nws_min_edge from source-monitor.py."""

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_very_confident_afternoon(self):
        sm = _load_source_monitor()
        assert sm._nws_min_edge(90, 85, 15, False) == 0.05

    def test_moderate_confidence_midday(self):
        sm = _load_source_monitor()
        assert sm._nws_min_edge(82, 80, 12, False) == 0.10

    def test_uncertain_morning(self):
        sm = _load_source_monitor()
        assert sm._nws_min_edge(81, 80, 8, False) == 0.15

    def test_bracket_always_020(self):
        sm = _load_source_monitor()
        assert sm._nws_min_edge(80, 80, 18, True) == 0.20

    def test_evening_confident(self):
        sm = _load_source_monitor()
        assert sm._nws_min_edge(85, 80, 18, False) == 0.05

    def test_overnight_uncertain(self):
        sm = _load_source_monitor()
        assert sm._nws_min_edge(83, 80, 4, False) == 0.15
```

**Step 6: Run tests**

Run: `pytest tests/test_source_monitor.py -v`
Expected: All pass.

**Step 7: Commit**

```bash
git add src/kalshi/source-monitor.py tests/test_source_monitor.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "refactor: extract _nws_min_edge helper, fix tests to call actual code

Deduplicates CI-based edge threshold logic from YES/NO branches.
Tests now call the actual function instead of reimplementing the logic."
```

---

### Task 9: Add NWS to Allocator Info-Arb Gate

**Files:**
- Modify: `src/kalshi/capital_allocator.py:667`
- Modify: `tests/test_allocator.py`

**Context:** NWS actual-temperature trades (highest confidence info-arb) pass `source_type="nws"` but the allocator only relaxes thresholds for `source_type == "info_arb"`. NWS observed data should get the same relaxed gate.

**Step 1: Write the failing test**

Add to `tests/test_allocator.py`:

```python
def test_nws_source_type_gets_relaxed_gate(mock_client):
    """NWS source_type should get same relaxed thresholds as info_arb."""
    alloc = _make_allocator(mock_client)
    # 87% confidence, 12% edge — above info_arb gate (85%/10%) but below default (90%/15%)
    result = alloc.request_budget("source-monitor", "KXHIGHHOU-26MAR3-T86",
                                   edge=0.12, confidence=0.87,
                                   bot_max_cost_cents=500, source_type="nws")
    assert result.approved
    # Verify it gets the high-confidence path (budget > bot_max_cost_cents)
    assert result.max_cost_cents >= 500
```

Note: Adapt the test helper (`_make_allocator`, `mock_client`) to match existing test patterns in the file.

**Step 2: Run test to verify it fails**

Expected: FAIL or the high-confidence path isn't triggered (87% < 90% default gate).

**Step 3: Fix the gate**

In `src/kalshi/capital_allocator.py`, replace line 667:

```python
        if source_type in ("info_arb", "nws"):
```

**Step 4: Run tests**

Run: `pytest tests/test_allocator.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add src/kalshi/capital_allocator.py tests/test_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: include NWS source_type in allocator info-arb relaxed gate

NWS actual temperature data is the highest-confidence info-arb signal
but was getting the strict 90%/15% thresholds instead of 85%/10%."
```

---

## Group 4: Position Floor & Structural (Tasks 10-12)

### Task 10: Add Minimum Position Floor in Capital Allocator

**Files:**
- Modify: `src/kalshi/capital_allocator.py:688-710`
- Modify: `tests/test_allocator.py`

**Context:** 8+ sequential Kelly reductions can compound to near-zero positions. Worst case: half-Kelly * 50% (CI) * 50% (crisis) * 75% (tail risk) = 9.4% of original. Need a floor.

**Step 1: Write the failing test**

Add to `tests/test_allocator.py`:

```python
def test_minimum_position_floor(mock_client):
    """Bankroll after all reductions should not drop below $1 (100 cents)."""
    alloc = _make_allocator(mock_client)
    # Force extreme reductions via regime + tail risk
    alloc._regime_detector.belief = [0.0, 0.0, 0.0, 1.0]  # 100% crisis
    alloc._regime_detector.n_updates = 10
    result = alloc.request_budget("crypto", "KXBTC-26MAR3-T95000",
                                   edge=0.10, confidence=0.70,
                                   bot_max_cost_cents=500)
    if result.approved:
        assert result.bankroll_cents >= 100  # $1 floor
```

**Step 2: Add floor logic**

In `src/kalshi/capital_allocator.py`, after the regime adjustment block (after line 703), add:

```python
        # Floor: bankroll after all adjustments should not drop below $1
        MIN_BANKROLL_CENTS = 100
        if bankroll < MIN_BANKROLL_CENTS:
            self.log.info("  Compound Kelly reductions pushed bankroll to $%.2f, applying $1 floor",
                         bankroll / 100)
            bankroll = MIN_BANKROLL_CENTS
```

**Step 3: Run tests**

Run: `pytest tests/test_allocator.py -v`
Expected: All pass.

**Step 4: Commit**

```bash
git add src/kalshi/capital_allocator.py tests/test_allocator.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "fix: add minimum bankroll floor after compound Kelly reductions

8+ sequential reductions (CI, regime, tail risk) could compound
to near-zero positions. Floor at $1 prevents death-by-1000-cuts."
```

---

### Task 11: Move scipy Imports to Module Level in Correlation Engine

**Files:**
- Modify: `src/kalshi/correlation_engine.py:259, 283`

**Context:** `scipy.stats.norm` and `scipy.stats.t` are imported inside method bodies, adding ~200ms overhead on first call and hiding dependency failures until runtime.

**Step 1: Move imports to top of file**

In `src/kalshi/correlation_engine.py`, add after line 20 (`from typing import ...`):

```python
from scipy.stats import norm as _norm_dist
from scipy.stats import t as _t_dist
```

**Step 2: Replace deferred import in `compute_portfolio_var` (line 259)**

Replace:
```python
        from scipy.stats import norm
        z = norm.ppf(conf)
```
With:
```python
        z = _norm_dist.ppf(conf)
```

**Step 3: Replace deferred import in `compute_tail_dependence` (line 283)**

Replace:
```python
        from scipy.stats import t as t_dist
```
And the usage:
```python
        lam = 2.0 * t_dist.cdf(arg, df=nu + 1)
```
With:
```python
        lam = 2.0 * _t_dist.cdf(arg, df=nu + 1)
```

**Step 4: Run tests**

Run: `pytest tests/test_correlation_engine.py -v`
Expected: All pass.

**Step 5: Commit**

```bash
git add src/kalshi/correlation_engine.py
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "refactor: move scipy imports to module level in correlation engine

Deferred imports added ~200ms on first call and hid dependency
failures until runtime. scipy is a declared project dependency."
```

---

### Task 12: Add Deprecation Headers to Superseded Docs

**Files:**
- Modify: `docs/plans/2026-02-26-quant-roadmap.md`
- Modify: `docs/plans/2026-03-01-quant-desk-design.md`
- Modify: `docs/plans/2026-03-01-phase1-unblock-the-flow.md`
- Modify: `docs/plans/2026-02-24-source-monitor-optimization-design.md`
- Modify: `docs/plans/2026-02-24-source-monitor-optimization-plan.md`

**Context:** 5 superseded documents remain in the repo without deprecation markers. Developers/agents opening them think they are current.

**Step 1: Add deprecation header to each file**

Prepend to each file (after the first `#` heading line):

```markdown
> **DEPRECATED**: This document is superseded by [Consolidated Quant Desk Design](2026-03-02-consolidated-quant-desk-design.md). Kept for historical reference only.
```

**Step 2: Commit**

```bash
git add docs/plans/2026-02-26-quant-roadmap.md docs/plans/2026-03-01-quant-desk-design.md docs/plans/2026-03-01-phase1-unblock-the-flow.md docs/plans/2026-02-24-source-monitor-optimization-design.md docs/plans/2026-02-24-source-monitor-optimization-plan.md
git commit --author="Andes Lee <andes.lee444@gmail.com>" -m "docs: add deprecation headers to superseded plan documents"
```

---

### Task 13: Final Verification

**Step 1: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests pass (230+ tests).

**Step 2: Verify no regressions**

Run: `pytest tests/ -v --tb=short 2>&1 | tail -5`
Expected: All passed, 0 failures.

**Step 3: Review git log**

Run: `git log --oneline -12`
Expected: 12 clean commits covering all fixes.

---

## Summary

| Task | Fix | Severity | Files |
|------|-----|----------|-------|
| 1 | Self-correlation returns 1.0 | CRITICAL | correlation_engine.py |
| 2 | PHIL city code mapping | CRITICAL | correlation_engine.py |
| 3 | Negative vol guard | CRITICAL | regime_detector.py |
| 4 | Deserialize length validation | CRITICAL | particle_filter.py |
| 5 | Entertainment bot in DISABLED_BY_DEFAULT | IMPORTANT | supervisor.py |
| 6 | Wire macro engine config | IMPORTANT | macro_engine.py |
| 7 | Cached signal fallback + nowcast mutation | IMPORTANT | economics-bot.py |
| 8 | Extract NWS edge helper | IMPORTANT | source-monitor.py |
| 9 | NWS in allocator info-arb gate | IMPORTANT | capital_allocator.py |
| 10 | Minimum position floor | IMPORTANT | capital_allocator.py |
| 11 | Module-level scipy imports | MINOR | correlation_engine.py |
| 12 | Deprecation headers | MINOR | docs/plans/*.md |
| 13 | Final verification | — | — |
