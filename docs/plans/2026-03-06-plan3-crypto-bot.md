# Plan 3: Crypto Bot — Full Quant Desk Review

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Per CLAUDE.md, you may ONLY modify: `crypto-bot.py`, `crypto_models.py`, `vol_forecaster.py`, `regime_detector.py`, `particle_filter.py`, `test_crypto*.py`, `test_heston.py`, `test_vol_forecaster.py`. Do NOT touch probability.py, kalshi_auth.py, or other bots.

**Goal:** Optimize crypto bot from +$91.99 realized (74 settled, 74% WR, 21% ROI) to target +$30-50/day. Fix OU target bug, stabilize GARCH, reduce Kelly stack crushing.

**Architecture:** Log-normal/GBM model for BTC/ETH with Heston stochastic vol, particle filter for Bayesian belief, regime detection. 5-minute scan interval.

**Tech Stack:** Python 3, Coinbase API, Deribit IV API, particle filter

---

## 1. Data Acquisition

### Task 3.1: Add Fallback Price Sources (Recommendation D)

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Test: `tests/test_crypto*.py`

**Context:** Sole dependency on Coinbase for spot price. Add Binance and/or Kraken as fallbacks.

**Step 1: Implement multi-source price fetching**

```python
PRICE_SOURCES = [
    ("coinbase", fetch_coinbase_price),
    ("binance", fetch_binance_price),
]

def get_spot_price(symbol):
    for name, fetcher in PRICE_SOURCES:
        try:
            price = fetcher(symbol)
            if price and price > 0:
                return price, name
        except Exception as e:
            log.warning(f"Price source {name} failed: {e}")
    raise RuntimeError(f"All price sources failed for {symbol}")
```

**Step 2: Cross-validate sources**

When both sources are available, flag if they disagree by >0.5% — suggests stale data.

**Step 3: Write tests and commit**

---

## 2. Signal & Model Quality

### Task 3.2: Fix OU Mean-Reversion Target

**Files:**
- Modify: `src/kalshi/crypto-bot.py` (or `crypto_models.py`)
- Test: `tests/test_crypto*.py`

**Context:** **CRITICAL — OU IS ENABLED IN PRODUCTION** (`config/bots-config.json` line 96: `"useOrnsteinUhlenbeck": true`). The mean-reversion target is set to the STRIKE PRICE of the market being evaluated (probability.py line ~1143), which is mathematically nonsensical — it pulls price toward each strike simultaneously. Every crypto probability estimate is currently corrupted by this bug. Fix immediately OR disable OU in config as an emergency measure.

**EMERGENCY OPTION:** If this task can't be implemented quickly, set `"useOrnsteinUhlenbeck": false` in `config/bots-config.json` to stop the bleeding. The GBM model without OU is more correct than OU with a wrong target.

**Step 1: Write failing test**

```python
def test_ou_target_is_not_strike_price():
    """OU mean-reversion target should be independent of strike price."""
    # Two different strike prices should produce different probabilities
    # but use the SAME mean-reversion target
    prob_80k = crypto_probability_with_ou(
        current=82000, strike=80000, direction="above", hours=4
    )
    prob_90k = crypto_probability_with_ou(
        current=82000, strike=90000, direction="above", hours=4
    )
    # If OU target = strike, the model would pull price toward each strike differently
    # With proper target (e.g., 82000 VWAP), both use same drift
    assert prob_80k > prob_90k  # Basic sanity: above 80K more likely than above 90K
```

**Step 2: Fix OU target to use rolling VWAP or SMA**

```python
def get_ou_target(symbol, lookback_hours=24):
    """Use 24h VWAP as mean-reversion target, NOT the strike price."""
    # Fetch historical prices for lookback period
    prices = fetch_historical_prices(symbol, lookback_hours)
    return np.mean(prices)  # Or VWAP if volume data available
```

**Step 3: Run tests and commit**

### Task 3.3: Stabilize GARCH After Flash Crashes

**Files:**
- Modify: `src/kalshi/vol_forecaster.py`
- Test: `tests/test_vol_forecaster.py`

**Context:** GARCH model can explode after flash crashes, producing unreasonably high vol estimates that persist for hours. This causes the model to see huge uncertainty and skip all trades.

**Step 1: Write failing test**

```python
def test_garch_bounded_after_extreme_return():
    """GARCH vol should be bounded even after a 10% flash crash."""
    returns = [0.001] * 100 + [-0.10]  # Normal returns then crash
    vol = garch_forecast(returns)
    assert vol < 2.0, f"GARCH vol after crash should be bounded, got {vol}"
    assert vol > 0.5, f"GARCH vol after crash should still reflect elevated risk"
```

**Step 2: Add vol ceiling and decay**

```python
MAX_GARCH_VOL = 2.0  # Cap annualized vol at 200%
GARCH_DECAY_RATE = 0.95  # Faster decay after spikes

def garch_forecast(returns, max_vol=MAX_GARCH_VOL):
    raw_vol = compute_garch(returns)
    return min(raw_vol, max_vol)
```

**Step 3: Run tests and commit**

### Task 3.4: Improve Short-Horizon Realized Vol

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Test: `tests/test_crypto*.py`

**Context:** Short-horizon (5-min) realized vol is noisy due to microstructure noise. Use a longer lookback (1h) for RV estimation or apply a Parkinson/Yang-Zhang estimator.

**Step 1: Implement Yang-Zhang vol estimator**

Yang-Zhang uses OHLC data (open, high, low, close) and is more efficient than close-to-close:

```python
def yang_zhang_vol(ohlc_data, lookback=24):
    """Yang-Zhang volatility estimator using OHLC data."""
    # More sample-efficient than close-to-close
    # Reduces microstructure noise
    ...
```

**Step 2: Write tests and commit**

---

## 3. Edge & Sizing

### Task 3.5: Fix Correlation Multiplier Bugs (CRITICAL)

**Files:**
- Modify: `src/kalshi/crypto-bot.py` (line ~773-787)
- Test: `tests/test_crypto*.py`

**Context (corrected by PM audit):** Two bugs in the correlation multiplier at line ~784:

1. **`abs(rho)` treats negative correlation as concentration risk** — The code uses `max_corr = max(max_corr, abs(rho))`, which means strong NEGATIVE correlation (rho=-0.8, a diversification BENEFIT) is treated identically to strong positive correlation (rho=+0.8, actual concentration risk). Negative correlation should INCREASE position size or at minimum not reduce it.

2. **No floor clamp** — The formula `corr_mult = 1.0 - 0.3 * (max_corr - 0.5) / 0.5` yields 0.7 at max_corr=1.0 (not negative as previously described), but should still have a floor clamp for safety.

**Step 1: Write failing test**

```python
def test_negative_correlation_not_penalized():
    """Negative correlation (diversification) should not reduce position size."""
    # Positive correlation = concentration risk = reduce
    mult_pos = compute_correlation_multiplier(max_positive_corr=0.8)
    # Negative correlation = diversification benefit = don't reduce
    mult_neg = compute_correlation_multiplier(max_positive_corr=0.0)
    assert mult_neg >= mult_pos, "Diversified portfolio should not be penalized"

def test_correlation_multiplier_bounded():
    """Correlation multiplier should be in [0.1, 1.0]."""
    for corr in [0.0, 0.3, 0.5, 0.7, 0.9, 1.0]:
        mult = compute_correlation_multiplier(max_positive_corr=corr)
        assert mult >= 0.1, f"Mult too low at corr={corr}: {mult}"
        assert mult <= 1.0, f"Mult > 1 at corr={corr}: {mult}"
```

**Step 2: Fix both bugs**

```python
# Bug 1: Only penalize POSITIVE correlation (concentration risk)
# Negative correlation = diversification, skip
max_pos_corr = 0.0
for other_asset in spot_prices:
    if other_asset != opp["asset"]:
        rho = dcc_tracker.pair_correlation(opp["asset"], other_asset)
        if rho is not None and rho > 0:  # Only positive correlation = risk
            max_pos_corr = max(max_pos_corr, rho)

# Bug 2: Add floor clamp
if max_pos_corr > 0.5:
    corr_mult = max(0.1, 1.0 - 0.3 * (max_pos_corr - 0.5) / 0.5)
```

**Step 3: Run tests and commit**

---

### Task 3.6: Fix Kelly Stack Crushing

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Test: `tests/test_crypto*.py`

**Context:** 4 multiplicative Kelly reductions: quarter_kelly base, particle filter CI (ci_kelly_multiplier), uncertainty_kelly, regime detector, vol forecaster confidence. Product can be <1/64th of optimal.

**Step 1: Write test documenting current crushing**

```python
def test_kelly_not_crushed_below_floor():
    """Final position size should be at least 25% of base Kelly."""
    # Simulate: 20% edge, 30c contract, moderate uncertainty
    base = quarter_kelly(0.20, 30, 500, 50000)
    # Apply all reductions
    final = apply_all_reductions(base, ci=0.7, uncertainty=0.8, regime=0.9, vol_conf=0.85)
    assert final >= base * 0.25, f"Kelly crushed to {final/base:.0%} of base"
```

**Step 2: Use apply_kelly_multipliers from Plan 1**

Replace manual multiplication chain with:
```python
from probability import apply_kelly_multipliers
final = apply_kelly_multipliers(base, [ci_mult, uncertainty_mult, regime_mult, vol_mult], floor_pct=0.25)
```

**Step 3: Run tests and commit**

### Task 3.6b: Review Heston 0.1% Floor

**Files:**
- Modify: `src/kalshi/crypto_models.py` or `probability.py` via flag
- Test: `tests/test_heston.py`

**Context:** Heston model has a 0.1% probability floor. For far OTM contracts this creates phantom edges. If Heston says 0.1% and market is at 1c, the model sees 0% edge when it should see "no information."

**Step 1: Audit where the floor is used and what happens if removed**

**Step 2: Replace floor with a "no-opinion" signal**

Instead of 0.1%, return None when Heston can't distinguish from zero. Let the bot skip rather than trade on a phantom floor.

**Step 3: Write tests and commit**

---

## 4. Execution

### Task 3.7: Add Limit Orders for Wide-Spread Markets

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Test: `tests/test_crypto*.py`

**Context:** 5-minute scan interval means prices can move between evaluation and execution. For wide-spread markets, use limit orders at model fair value.

**Step 1: Implement spread-aware order type selection**

**Step 2: Write tests and commit**

---

## 5. Exit Management

### Task 3.8: Improve Model-Shift Exit Triggers

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Test: `tests/test_crypto*.py`

**Context:** Position monitor does generic exits. Crypto markets are fast-moving — the bot itself should flag when its probability has shifted significantly since entry.

**Step 1: Track entry probability and current probability**

```python
for position in open_positions:
    entry_prob = position.get("model_fair_value_cents", 50) / 100
    current_prob = calculate_current_probability(position)
    prob_shift = abs(current_prob - entry_prob)
    if prob_shift > 0.20:  # 20pp shift
        log.warning(f"Model shift: {position['ticker']} moved {prob_shift:.0%}")
        # Flag for exit consideration
```

**Step 2: Write tests and commit**

---

## 6. Risk Controls

### Task 3.9: Add Regime-Aware Position Limits

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Test: `tests/test_crypto*.py`

**Context:** Crypto has distinct regimes (trending, mean-reverting, high-vol). Position limits should tighten in high-vol regimes.

**Step 1: Implement regime-aware limits**

```python
def get_position_limit(regime):
    if regime == "high_vol":
        return MAX_POSITION * 0.5  # Half position in high vol
    elif regime == "trending":
        return MAX_POSITION * 0.75  # 75% in trending (model less reliable)
    return MAX_POSITION  # Full in mean-reverting
```

**Step 2: Write tests and commit**

---

## 7. Measurement Framework

### Task 3.10: Create Crypto Bot Metrics

**Files:**
- Modify: `src/kalshi/crypto-bot.py`
- Output: `data/crypto-metrics.json`

**Step 1: Log per-scan metrics**

```python
scan_metrics = {
    "timestamp": datetime.utcnow().isoformat(),
    "btc_price": btc_price,
    "eth_price": eth_price,
    "garch_vol": garch_vol,
    "regime": current_regime,
    "markets_scanned": len(markets),
    "trades_placed": len(trades),
    "particle_filter_ci": ci_width,
    "kelly_reductions_applied": reduction_factors
}
```

**Step 2: Write tests and commit**

---

## Measurement Protocol

| Metric | Before | Target | Method |
|--------|--------|--------|--------|
| Win Rate | 74% | 76%+ | Settlement analysis |
| Daily P&L | ~$3/day | $30-50/day | Trade log |
| Kelly floor | Can crush to ~1/64 | 25% minimum | Unit test |
| OU target | = strike price (LIVE — **emergency disabled Mar 7**) | 24h VWAP or keep disabled | Unit test + config check |
| Crypto Brier (backtest) | 0.3851 (coin-flip calibration: 0-20% pred → 50% actual, 80-100% pred → 50% actual) | <0.250 | npm run backtest |
| Correlation multiplier | Uses abs(rho) — penalizes diversification; no floor | Only penalize positive corr; clamped [0.1, 1.0] | Unit test |
| GARCH after crash | Explodes | Bounded at 200% | Unit test |
| RV estimator | Close-to-close (noisy) | Yang-Zhang | Unit test |
| Price sources | 1 (Coinbase) | 2+ | Health check |

---

## Execution Report (2026-03-07)

**Status:** Complete

**Tasks completed:** 10/10

**Summary:** abs(rho) fixed, GARCH ceiling 200%, Kelly floor 25%, Binance fallback, Yang-Zhang vol, no-opinion detection, spread pricing, model-shift exits, regime limits. Crypto Brier 0.3851 (target <0.250). OU remains disabled.

**Backtest results (post-implementation):**
- Crypto Brier: 0.3851 (127 settlements)
- Overconfident at extremes (0.93 pred vs 0.50 actual in 0.8-1.0 bin)
- Actual P&L $1,619 vs flat -$57 vs Kelly $1,351

**Next steps:** SHARED MODULE CHANGE NEEDED: probability.py -- add ou_target parameter to crypto_price_probability() to allow re-enabling OU mean-reversion with proper target (24h VWAP instead of strike price).
