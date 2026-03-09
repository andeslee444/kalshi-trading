# Strategy Bot Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 5 compounding bugs that produce a catastrophic 0.833 Brier score for the strategy bot (actual win rate is 91%, backtest reports 15%).

**Architecture:** Fix outcome determination across all backtest reeval functions (system-wide), fix strategy-specific formula and filter, fix Bayesian updater contamination in the live bot, reset corrupt state, add tournament market filter.

**Tech Stack:** Python, pytest, Kalshi REST API

---

### Task 1: Add `_determine_outcome()` helper to backtest.py

**Files:**
- Modify: `scripts/backtest.py:287` (insert before `reeval_strategy_trade`)

**Step 1: Add the outcome helper**

Insert this function at line 287 (before `reeval_strategy_trade`):

```python
def _determine_outcome(trade, settlement_revenue, side):
    """Determine trade outcome, preferring local settlement_result over API revenue.

    API revenue is 0 for positions exited before settlement (by position-monitor),
    which the old logic incorrectly treated as losses. The local settlement_result
    field (set by reconcile-trades.py) is authoritative.
    """
    local_result = trade.get("settlement_result")
    if local_result == "won":
        return 1
    if local_result == "lost":
        return 0
    # Fallback: API revenue (unreliable for pre-exit trades)
    if side == "yes":
        return 1 if settlement_revenue > 0 else 0
    elif side == "no":
        return 0 if settlement_revenue > 0 else 1
    return None
```

**Step 2: Update `reeval_weather_trade()` to use it**

In `reeval_weather_trade()` (~line 230-236), replace:
```python
    side = trade.get("side", "").lower()
    if side == "yes":
        actual = 1 if settlement_revenue > 0 else 0
    elif side == "no":
        actual = 0 if settlement_revenue > 0 else 1
    else:
        return None
```
with:
```python
    side = trade.get("side", "").lower()
    actual = _determine_outcome(trade, settlement_revenue, side)
    if actual is None:
        return None
```

**Step 3: Update `reeval_entertainment_trade()` to use it**

In `reeval_entertainment_trade()` (~line 334-341), replace:
```python
    if side == "yes":
        actual = 1 if settlement_revenue > 0 else 0
        predicted = conf_val
    elif side == "no":
        actual = 0 if settlement_revenue > 0 else 1
        predicted = 1 - conf_val
    else:
        return None
```
with:
```python
    actual = _determine_outcome(trade, settlement_revenue, side)
    if actual is None:
        return None
    if side == "yes":
        predicted = conf_val
    else:
        predicted = 1 - conf_val
```

**Step 4: Update `reeval_crypto_trade()` to use it**

In `reeval_crypto_trade()` (~line 361-369), replace:
```python
    if side == "yes":
        actual = 1 if settlement_revenue > 0 else 0
        predicted = model_prob
    elif side == "no":
        actual = 0 if settlement_revenue > 0 else 1
        predicted = 1 - model_prob
    else:
        return None
```
with:
```python
    actual = _determine_outcome(trade, settlement_revenue, side)
    if actual is None:
        return None
    if side == "yes":
        predicted = model_prob
    else:
        predicted = 1 - model_prob
```

**Step 5: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('scripts/backtest.py', doraise=True); print('OK')"`
Expected: OK

**Step 6: Commit**

```bash
git add scripts/backtest.py
git commit -m "fix(backtest): use local settlement_result for outcome determination

API revenue is 0 for positions exited before settlement, causing 25%
of trades to be misclassified as losses. Prefer reconciled local
settlement_result field across all reeval functions."
```

---

### Task 2: Rewrite `reeval_strategy_trade()` — formula fix + price filter + buy-side support

**Files:**
- Modify: `scripts/backtest.py:27-33` (add `longshot_edge` to imports)
- Modify: `scripts/backtest.py:288-318` (rewrite function)

**Step 1: Add `longshot_edge` to imports**

In the `from probability import` block (~line 27-33), add `longshot_edge`:

```python
from probability import (
    weather_probability,
    crypto_price_probability,
    half_kelly,
    half_kelly_sell,
    quarter_kelly,
    longshot_edge,
)
```

**Step 2: Rewrite `reeval_strategy_trade()`**

Replace the entire function (lines 288-318) with:

```python
def reeval_strategy_trade(trade, settlement_revenue):
    """Re-evaluate a strategy/longshot trade. Returns dict or None."""
    ticker = trade.get("ticker", "")
    strategy = trade.get("strategy", "longshot_sell")
    yes_price = trade.get("yes_price_at_entry") or trade.get("yes_price", 0)

    if strategy == "longshot_buy":
        # Buy-side: YES price is 70-99c, use NO-equivalent for edge
        if not yes_price or yes_price < 70 or yes_price > 99:
            return None
        no_price = 100 - yes_price
        edge_price = no_price  # edge is computed on the longshot (NO) side
    else:
        # Sell-side: YES price is 1-30c
        if not yes_price or yes_price <= 0 or yes_price > 30:
            return None
        edge_price = yes_price

    # Edge: prefer stored edge (captures Bayesian adjustments at trade time),
    # fall back to longshot_edge() from probability.py
    stored_edge = trade.get("edge")
    if stored_edge and stored_edge > 0:
        est_edge = stored_edge
    else:
        est_edge = longshot_edge(edge_price, ticker=ticker)

    # Outcome: prefer local settlement_result over API revenue
    side = "yes" if strategy == "longshot_buy" else "no"
    actual_win = _determine_outcome(trade, settlement_revenue, side)
    if actual_win is None:
        return None

    # Predicted win probability
    implied = edge_price / 100.0
    p_true = max(0.001, min(0.999, implied - est_edge))
    if strategy == "longshot_sell":
        predicted_win = 1 - p_true  # seller wins when event doesn't happen
    else:
        predicted_win = 1 - p_true  # buyer wins when longshot (NO) doesn't happen

    # Re-compute Kelly sizing
    if strategy == "longshot_buy":
        contracts, risk = half_kelly(est_edge, yes_price, 500)
    else:
        contracts, risk = half_kelly_sell(est_edge, edge_price, 500)

    return {
        "ticker": ticker,
        "predicted": predicted_win,
        "actual": actual_win,
        "side": side,
        "revenue": settlement_revenue,
        "price": yes_price if strategy == "longshot_buy" else 100 - yes_price,
        "kelly_contracts": contracts,
    }
```

**Step 3: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('scripts/backtest.py', doraise=True); print('OK')"`
Expected: OK

**Step 4: Run backtest and verify Brier improvement**

Run: `npm run backtest 2>&1 | grep -E "strategy|Brier|Win Rate"`
Expected: strategy Brier should drop from 0.8325 significantly (target < 0.40)

**Step 5: Commit**

```bash
git add scripts/backtest.py
git commit -m "fix(backtest): rewrite strategy reeval — correct formula, buy-side, price filter

- Use longshot_edge() from probability.py instead of raw overpricing ratio
- Prefer stored edge from trade record (Bayesian adjustments at trade time)
- Handle longshot_buy trades (YES 70-99c) correctly
- Expand price filter from >15 to >30 matching live bot's sellMaxPrice"
```

---

### Task 3: Fix `check_settled_trades()` in strategy-trader.py

**Files:**
- Modify: `src/kalshi/strategy-trader.py:457-479`

**Step 1: Fix the settlement loop**

Replace lines 457-479 (the `for s in settled_list:` loop) with:

```python
                for s in settled_list:
                    ticker = s.get("ticker", "")
                    if not ticker:
                        continue
                    # Only process settlements for OUR strategy trades
                    trade_rec = our_trades.get(ticker)
                    if not trade_rec:
                        continue

                    category = classify_ticker_category(ticker)
                    strategy = trade_rec.get("strategy", "")
                    price_cents = trade_rec.get("yes_price_at_entry", 5)

                    # Determine outcome: prefer local settlement_result
                    local_result = trade_rec.get("settlement_result")
                    if local_result == "won":
                        won = True
                    elif local_result == "lost":
                        won = False
                    else:
                        won = s.get("revenue", 0) > 0

                    # Fix side conflation: for buy-side trades (YES 70-99c),
                    # convert to NO-equivalent price (1-30c) before bucketing
                    if strategy == "longshot_buy" and price_cents > 30:
                        price_cents = 100 - price_cents  # NO-equivalent for bucketing
                        # For buy-side: "won" means YES resolved (buyer wins)
                        # For the Becker model, this means the longshot (NO side) LOST
                        won = not won  # flip: buyer winning = seller losing

                    # Clamp to valid bucket range
                    price_cents = max(1, min(30, price_cents))
                    edge_estimator.update_posterior(category, price_cents, won)
```

**Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/kalshi/strategy-trader.py', doraise=True); print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add src/kalshi/strategy-trader.py
git commit -m "fix(strategy): filter settlements to own trades, use local outcome

check_settled_trades() was processing ALL API settlements (crypto,
weather, etc.) and feeding them into the longshot bias Bayesian model.
Now only processes settlements with matching strategy trade records.
Also uses local settlement_result for outcome determination."
```

---

### Task 4: Add tournament market filter to strategy-trader.py

**Files:**
- Modify: `src/kalshi/strategy-trader.py:83-117` (add filter in `find_longshot_sells`)

**Step 1: Add TOURNAMENT_PREFIXES constant**

Add near the top of the file, after the config loading section (around line 82, before `find_longshot_sells`):

```python
# Multi-outcome futures where longshot bias model doesn't apply
TOURNAMENT_PREFIXES = ("KXMARMAD-",)
```

**Step 2: Add filter in `find_longshot_sells()`**

Inside `find_longshot_sells()`, after the `yes_ask <= 0 or yes_ask > _sell_max_price` check (around line 104), add:

```python
        # Skip tournament/championship futures (multi-outcome, violates longshot bias premise)
        if any(ticker.startswith(p) for p in TOURNAMENT_PREFIXES):
            trade_manager.log_decision(ticker, "no", "skipped", "tournament_market",
                                       yes_ask=yes_ask)
            continue
```

**Step 3: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/kalshi/strategy-trader.py', doraise=True); print('OK')"`
Expected: OK

**Step 4: Commit**

```bash
git add src/kalshi/strategy-trader.py
git commit -m "fix(strategy): skip tournament championship futures from longshot sells

KXMARMAD (NCAA tournament champion) markets are multi-outcome futures
where exactly 1 of N teams wins. The Becker longshot bias model assumes
independent binary events and doesn't apply here."
```

---

### Task 5: Reset bayes-params.json

**Files:**
- Modify: `config/bayes-params.json`

**Step 1: Reset to empty defaults**

Write a clean bayes-params.json with just the kappa value and empty categories:

```json
{
  "kappa": 30,
  "categories": {}
}
```

**Step 2: Commit**

```bash
git add config/bayes-params.json
git commit -m "fix(strategy): reset corrupt bayes-params.json

State was corrupted by two bugs: (1) all bot settlements were being
fed into the strategy Bayesian model, (2) outcome determination used
broken API revenue logic. Reset to rebuild from correct data."
```

---

### Task 6: Run backtest, verify success criteria, push

**Step 1: Run full backtest**

Run: `npm run backtest 2>&1 | tail -40`

Verify:
- Strategy Brier score < 0.40 (was 0.833)
- Strategy win rate reflects actual (~91%)
- Weather and crypto Brier scores unchanged (0.314, 0.388)

**Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=line 2>&1 | tail -5`
Expected: All tests pass (pre-existing failures OK)

**Step 3: Push**

Run: `git push origin plan2-consistency-testing-maintainability`

**Step 4: Restart supervisor**

Kill existing supervisor and bots, then restart:
```bash
pkill -f "supervisor.py run"
pkill -f "strategy-trader.py"
sleep 2
nohup python3 -u scripts/supervisor.py run > data/logs/supervisor.log 2>&1 &
sleep 5
python3 scripts/supervisor.py status
```
