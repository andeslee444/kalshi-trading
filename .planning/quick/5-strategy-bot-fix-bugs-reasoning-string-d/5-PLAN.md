---
phase: quick-5
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/strategy-trader.py
  - src/kalshi/probability.py
  - tests/test_strategy_bugs.py
autonomous: true
requirements: [BUG-2, BUG-3, BUG-4, IMP-1]

must_haves:
  truths:
    - "Reasoning string uses correct implied_prob based on sell_price, not yes_ask"
    - "No negative true probabilities in reasoning strings for any valid trade"
    - "find_near_settlement dead code is removed from strategy-trader.py"
    - "compute_limit_price NO-side returns correct within-spread prices for low-edge trades"
    - "Settlement reconciliation has been run"
  artifacts:
    - path: "src/kalshi/strategy-trader.py"
      provides: "Bug-fixed strategy trader without dead code"
      contains: "sell_price / 100.0"
    - path: "src/kalshi/probability.py"
      provides: "Fixed compute_limit_price NO-side logic"
      contains: "compute_limit_price"
    - path: "tests/test_strategy_bugs.py"
      provides: "Targeted regression tests for BUG-2, BUG-3, BUG-4"
      min_lines: 40
  key_links:
    - from: "src/kalshi/strategy-trader.py"
      to: "src/kalshi/probability.py"
      via: "longshot_edge, compute_limit_price imports"
      pattern: "from probability import.*longshot_edge.*compute_limit_price"
---

<objective>
Fix three bugs in the strategy bot and run settlement reconciliation.

Purpose: The reasoning string uses wrong implied_prob (yes_ask instead of sell_price), dead code wastes scan time, and NO-side limit pricing places at full ask for low-edge trades. These degrade logging accuracy, bot performance, and fill quality.
Output: Patched strategy-trader.py, patched probability.py, regression tests, reconciliation run.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docs/plans/bot-improvements/04-strategy-bot.md
@src/kalshi/strategy-trader.py
@src/kalshi/probability.py
@tests/test_optimization.py (existing longshot_edge and compute_limit_price tests)

<interfaces>
<!-- Key functions the executor needs -->

From src/kalshi/probability.py:
```python
def longshot_edge(yes_price_cents, ticker="", hours_to_close=999):
    """Returns additive probability edge (implied_prob - true_prob).
    implied_prob = yes_price_cents / 100.0
    true_prob = implied_prob * (1.0 - overpricing_ratio)
    additive_edge = implied_prob - true_prob = implied_prob * overpricing_ratio
    """

def compute_limit_price(yes_bid, yes_ask, side, edge=None):
    """Returns price in cents within the spread based on edge tier.
    YES-side: works correctly with three tiers (high/medium/low edge).
    NO-side: converts to NO bid/ask then applies same tiers.
    BUG: NO-side for low-edge returns near-full-ask due to integer math.
    """
```

From src/kalshi/strategy-trader.py:
```python
def find_longshot_sells(markets, bankroll):
    """Lines 131-153: BUG-2 location — reasoning string uses yes_ask not sell_price"""

def find_near_settlement(markets):
    """Lines 159-195: BUG-3 location — dead code, no orders placed"""
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Fix BUG-2 (reasoning string), BUG-3 (dead code removal), BUG-4 (NO-side limit price)</name>
  <files>src/kalshi/strategy-trader.py, src/kalshi/probability.py, tests/test_strategy_bugs.py</files>
  <behavior>
    - Test: reasoning string implied_prob matches sell_price/100, not yes_ask/100 (e.g., sell_price=4, yes_ask=5 -> implied_prob should be 4.0%, not 5.0%)
    - Test: true_prob is never negative for any valid sell_price (1-15c) and est_edge from longshot_edge()
    - Test: find_near_settlement function no longer exists in strategy-trader.py
    - Test: compute_limit_price("no", edge=0.03) with yes_bid=3, yes_ask=10 returns within NO spread (90-97), NOT at 97 (full ask)
    - Test: compute_limit_price("no", edge=0.03) with yes_bid=86, yes_ask=92 returns within NO spread (8-14), strictly less than no_ask=14
    - Test: compute_limit_price NO-side still returns full ask for high edge (>=0.15)
  </behavior>
  <action>
    **BUG-2 fix in strategy-trader.py:**
    - Line 131: Change `implied_prob = yes_ask / 100.0` to `implied_prob = sell_price / 100.0`
    - This makes `true_prob = implied_prob - est_edge` correct because `est_edge` was recomputed at `sell_price` on line 92.
    - The reasoning string then correctly reports the true_prob from the Becker model at the actual entry price.

    **BUG-3 fix in strategy-trader.py:**
    - Delete the entire `find_near_settlement()` function (lines 159-195).
    - In `run_scan()`, remove the "Strategy 2" section that calls `find_near_settlement()` and logs its results (around lines 281-289).
    - Remove the `near_settle` variable from the performance log section (line 361).
    - Update the log line in `main()` that mentions "Near-Settlement" (line 416).

    **BUG-4 fix in probability.py:**
    - In `compute_limit_price()` NO-side (lines 1385-1395):
    - The current NO-side correctly computes no_bid and no_ask, but for low-edge the midpoint formula `(no_bid + no_ask) // 2 + 1` often rounds up to near-full-ask for tight spreads.
    - Replace the NO-side low-edge formula: instead of `(no_bid + no_ask) // 2 + 1`, use `no_bid + max(1, (no_ask - no_bid) // 3)` to place in the inner third of the spread (patient placement, mirrors YES-side intent of being closer to bid).
    - Medium edge: `no_bid + max(1, (no_ask - no_bid) * 2 // 3)` (outer third).
    - High edge / no edge: keep `no_ask` (unchanged, urgency).
    - Guard: if `no_ask <= no_bid` (no real spread), return `no_ask` (unchanged).

    **Tests in tests/test_strategy_bugs.py:**
    - Create new focused test file with regression tests for all three bugs.
    - For BUG-2: Mock find_longshot_sells logic to verify reasoning string uses sell_price, not yes_ask. Directly test the math: `implied_prob = sell_price / 100.0; true_prob = implied_prob - longshot_edge(sell_price, ...)` is always non-negative for sell_price 1-15.
    - For BUG-3: Use importlib to load strategy-trader.py, assert `find_near_settlement` is NOT in the module's namespace.
    - For BUG-4: Test compute_limit_price NO-side across edge tiers with various spreads, assert low-edge returns strictly less than no_ask when spread >= 3c.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -m pytest tests/test_strategy_bugs.py tests/test_optimization.py -x -v 2>&1 | tail -40</automated>
  </verify>
  <done>
    - Reasoning string in find_longshot_sells uses sell_price/100.0 for implied_prob (not yes_ask)
    - find_near_settlement function and all references removed from strategy-trader.py
    - compute_limit_price NO-side places within spread for low/medium edge trades
    - All new tests pass, all existing tests in test_optimization.py still pass
  </done>
</task>

<task type="auto">
  <name>Task 2: Run settlement reconciliation (IMP-1)</name>
  <files>(no code changes — operational task)</files>
  <action>
    Run settlement reconciliation to annotate trade records with settlement outcomes:
    ```bash
    cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading
    npm run reconcile
    npm run backfill
    ```
    These commands:
    - `reconcile`: Walks all trade log files, matches to API settlement/fill data, writes back settlement_result and realized_edge
    - `backfill`: Queries individual /markets/{ticker} endpoints for unsettled trades

    Note: These require valid API credentials. If auth fails (demo mode may not have settlement data), log the error and document the outcome. The task succeeds either way — the goal is to attempt reconciliation.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && npm run reconcile -- --dry-run 2>&1 | tail -20</automated>
  </verify>
  <done>
    - Reconciliation attempted (success or documented auth/data limitation)
    - If successful: trade logs annotated with settlement results
  </done>
</task>

</tasks>

<verification>
1. `python3 -m pytest tests/test_strategy_bugs.py -v` — all new regression tests pass
2. `python3 -m pytest tests/test_optimization.py -v` — existing tests unbroken
3. `python3 -m pytest tests/ -x --timeout=60` — full test suite passes
4. `grep -n "find_near_settlement" src/kalshi/strategy-trader.py` — returns nothing (dead code removed)
5. `grep -n "sell_price / 100.0" src/kalshi/strategy-trader.py` — confirms BUG-2 fix present
</verification>

<success_criteria>
- BUG-2: Reasoning string uses sell_price-based implied_prob, no negative true_prob values
- BUG-3: find_near_settlement function and all call sites removed from strategy-trader.py
- BUG-4: compute_limit_price NO-side returns within-spread prices for low-edge trades
- IMP-1: Settlement reconciliation attempted
- All tests pass (new + existing)
</success_criteria>

<output>
After completion, create `.planning/quick/5-strategy-bot-fix-bugs-reasoning-string-d/5-SUMMARY.md`
</output>
