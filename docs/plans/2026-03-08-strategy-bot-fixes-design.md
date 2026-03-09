# Strategy Bot Fixes — Design Doc

**Date**: 2026-03-08
**Status**: Approved
**Context**: Strategy bot shows 0.833 Brier score (catastrophic). Investigation found 5 compounding bugs.

## Problem Summary

The strategy bot's backtest reports 14.7% win rate (5W/29L) and 0.833 Brier score. The actual win rate is 91.2% (31W/3L). Five bugs compound to produce this distortion.

## Bug Analysis

### Bug 1 (Critical): Outcome Determination Uses API Revenue

`reeval_strategy_trade()` in backtest.py uses `settlement_revenue > 0` to determine win/loss. But 25% of all API settlements have `revenue=0` because positions were exited by position-monitor before settlement. The backtest treats these as losses.

- 34 settled strategy trades: 31 won locally, but only 5 show API revenue > 0
- Backtest computes 5W/29L instead of 31W/3L
- Same bug affects `reeval_weather_trade()`, `reeval_crypto_trade()`, `reeval_entertainment_trade()`

### Bug 2: Formula Divergence Between Backtest and Live Bot

Backtest line 296: `est_edge = 0.57 * exp(-0.15 * price)` — raw overpricing **ratio**.
Live `longshot_edge()`: `additive_edge = implied_prob * overpricing_ratio` — the actual **edge**.

At 2c: backtest computes 42.2% edge vs correct 0.84% — a 50x error. This drives `p_true` negative, clamped to 0.001, yielding 99.9% predicted win probability.

### Bug 3: `yes_price > 15` Filter

Backtest line 292 drops all trades with `yes_price > 15`, but the live bot trades up to `sellMaxPrice` (30c). Artificially limits evaluation set.

### Bug 4: Bayesian Updater Processes All Settlements

`check_settled_trades()` in strategy-trader.py iterates over ALL API settlements (crypto, weather, entertainment) and feeds them into the longshot bias Bayesian model. This corrupts `bayes-params.json`:
- "crypto" category: 2011 wins / 2411 losses (from crypto bot, not strategy)
- "weather" category: 794 wins / 387 losses (from weather bot)
- "entertainment" category: 0 wins / 574 losses

Additionally, the outcome determination uses the same broken `revenue > 0` logic.

### Bug 5: Tournament Markets Violate Model Premise

Tournament championship futures (KXMARMAD-26-BYU etc.) are multi-outcome markets. The Becker longshot bias model assumes independent binary events and doesn't apply to "1 of 68 teams wins" markets.

## Fixes

### Fix 1: Outcome Determination (backtest.py)

Add `_determine_outcome()` helper that prefers local `settlement_result` over API revenue. Apply to all 4 `reeval_*` functions.

```python
def _determine_outcome(trade, settlement_revenue, side):
    local_result = trade.get("settlement_result")
    if local_result == "won":
        return 1
    if local_result == "lost":
        return 0
    if side == "yes":
        return 1 if settlement_revenue > 0 else 0
    elif side == "no":
        return 0 if settlement_revenue > 0 else 1
    return None
```

### Fix 2: Formula + Price Filter (backtest.py)

Rewrite `reeval_strategy_trade()`:
- Use stored `edge` field when available (captures Bayesian adjustments at trade time)
- Fall back to `longshot_edge()` from probability.py (guaranteed consistent)
- Handle both `longshot_sell` and `longshot_buy` strategies correctly
- Expand price filter from `> 15` to `> 30`

### Fix 3: Bayesian Updater (strategy-trader.py)

Fix `check_settled_trades()`:
- Skip settlements not in `our_trades` (only process strategy bot's own trades)
- Use local `settlement_result` for outcome, fallback to API revenue

### Fix 4: Reset Bayes State (bayes-params.json)

Reset to empty defaults. The model will rebuild from correct data on next scan.

### Fix 5: Tournament Market Filter (strategy-trader.py)

Add `TOURNAMENT_PREFIXES` list and skip matching tickers in `find_longshot_sells()`. Conservative: only KXMARMAD initially.

## Files Modified

| File | Changes |
|------|---------|
| `scripts/backtest.py` | `_determine_outcome()` helper, fix all 4 `reeval_*` functions, rewrite strategy formula, expand price filter |
| `src/kalshi/strategy-trader.py` | Fix `check_settled_trades()` settlement filter + outcome, add tournament prefix filter |
| `config/bayes-params.json` | Reset to empty defaults |

## Success Criteria

- Backtest strategy Brier score drops from 0.833 to < 0.40
- Strategy win rate in backtest matches local trade log (currently 91.2%)
- `bayes-params.json` only contains strategy bot settlements after next scan
- Tournament markets are excluded from longshot sell candidates
