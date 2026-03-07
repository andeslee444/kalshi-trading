# Plan 5: Strategy Trader — Full Quant Desk Review

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Per CLAUDE.md, you may ONLY modify: `strategy-trader.py`, `strategy_engine.py`, `test_strategy*.py`. Do NOT touch probability.py, kalshi_auth.py, or other bots.

**Goal:** Fix CPU spin, calibrate copula_scale, review longshot edge formula. Currently -$0.36 realized (5 settled, 60% WR). Target: +$10-20/day.

**Architecture:** Longshot bias exploitation (Becker 2025) + near-settlement arbitrage. One-shot mode.

**Tech Stack:** Python 3

---

### Task 5.1: Fix CPU Spin — Defer Allocator Calls Until After Sorting

**Files:**
- Modify: `src/kalshi/strategy-trader.py`
- Test: `tests/test_strategy*.py`

**Root Cause (diagnosed in Plan 0, Task 0.3):** CPU is NOT from zombies or missing sleep. The daemon loop sleep at line ~842 works correctly. The issue is in `find_longshot_sells()` (line ~82-258) and `find_longshot_buys()` (line ~260+):

- `find_longshot_sells()` iterates over ALL open Kalshi markets (thousands)
- For every candidate that passes the edge filter, it calls `allocator.request_budget()` (line ~195)
- Each `request_budget()` call: acquires exclusive file lock (`fcntl.LOCK_EX`), reads/parses `allocator-state.json` from disk, runs correlation engine checks (cluster limit, marginal VaR, tail risk multiplier)
- This produces thousands of "Tail risk reduction" log lines per scan
- But only the top 10 candidates are actually traded (`longshots[:10]` at line ~588)
- Result: 10-15 minutes of 100% CPU per scan cycle, mostly wasted on allocator calls for markets that will never be traded

**Fix:** Separate candidate scoring from budget allocation. Score all candidates first (edge, volume, hours), sort by edge×volume, then only call `request_budget()` for the top 20 candidates (2x the trade limit of 10 to allow for allocator denials).

**Step 1:** In `find_longshot_sells()`, remove the `allocator.request_budget()` call from the main loop. Instead, compute edge and Kelly sizing without the allocator, collect all candidates into a list, sort by `est_edge * log1p(volume)` (the existing sort key at line ~257).

**Step 2:** After sorting, iterate over only the top 20 candidates and call `allocator.request_budget()` for each. Move the budget check, correlation sizer cap check, and the Bayesian Kelly multiplier application into this post-sort loop. Candidates denied by the allocator are skipped; continue to the next candidate.

**Step 3:** Apply the same fix to `find_longshot_buys()`.

**Step 4:** Write a test that verifies the scan completes in <30 seconds (mock the allocator and market list with 5000 markets). Verify that `allocator.request_budget` is called ≤20 times, not thousands.

**Step 5:** Commit.

```bash
git add src/kalshi/strategy-trader.py tests/test_strategy*.py
git commit -m "perf(strategy): defer allocator calls to top-N candidates only (was O(all_markets))"
```

**Expected impact:** Scan time drops from 10-15 min to <30 seconds. CPU drops from 100% during scan to <5%.

### Task 5.2: Calibrate copula_scale

**Files:**
- Modify: `src/kalshi/strategy-trader.py`
- Test: `tests/test_strategy*.py`

**Context:** `copula_scale` is hardcoded to 1.0 at lines 186, 335. This means no correlation adjustment between markets. Need data-driven calibration.

**Step 1:** Analyze historical trades: are multi-leg positions correlated?
**Step 2:** Set copula_scale based on observed correlation (e.g., 0.7 for sports, 0.5 for cross-category).
**Step 3:** Write tests and commit.

### Task 5.3: Review Longshot Edge Formula (CRITICAL — Backtest Shows Catastrophic Overconfidence)

**Files:**
- Modify: `src/kalshi/strategy_engine.py`
- Test: `tests/test_strategy*.py`

**Context (from Plan 0 Task 0.6 backtest):** **Strategy "other" Brier score is 0.8325** — catastrophic overconfidence. The calibration curve shows: predicted 98.9% probability → actual outcome only 14.7%. The longshot model thinks it has near-certainty but is wrong 85% of the time. This is worse than random.

Edge formula: `amplitude * exp(-decay_rate * price)` with category-specific parameters (probability.py line ~1418). At 5c contracts with default params (0.57, 0.15): `0.57 * exp(-0.75)` = 26.9% edge. The formula may be fundamentally miscalibrated for some categories, or the time-decay factor is not aggressive enough.

The 16 orphan strategy-trader trades from the zombie period (11 sports, 2 politics, 2 quick-settle, 1 forex) provide additional evidence that the bot was actively placing bad trades across diverse categories.

**Step 1:** Backtest longshot formula against actual settlements. The 0.8325 Brier means the model is systematically wrong — understand WHICH categories are failing.
**Step 2:** Disable or heavily penalize categories with <50% WR.
**Step 3:** If no category is profitable, consider disabling longshot selling entirely until the model is fixed.
**Step 4:** Write tests and commit.

### Task 5.4: Fix Time Decay Floor

**Files:**
- Modify: `src/kalshi/strategy_engine.py`
- Test: `tests/test_strategy*.py`

**Context:** Time decay floors at 50%, meaning near-settlement markets can't decay below 50% edge reduction. Should go lower for markets that are clearly going to settle one way.

**Step 1:** Remove or lower the floor to 20%.
**Step 2:** Write tests and commit.

### Task 5.5: Add Measurement Framework

**Files:**
- Modify: `src/kalshi/strategy-trader.py`
- Output: `data/strategy-metrics.json`

**Step 1:** Log per-run metrics: markets scanned, longshot candidates, near-settlement candidates, trades placed, edge distribution.
**Step 2:** Write tests and commit.

---

## Measurement Protocol

| Metric | Before | Target | Method |
|--------|--------|--------|--------|
| CPU usage | 100% during scan (O(n) allocator calls on all markets) | <5% (defer allocator to top-N) | ps aux |
| copula_scale | Hardcoded 1.0 | Data-driven | Config |
| Longshot edge at 5c | ~27% (default params, pre-time-decay) | Validate with backtest | Backtest |
| Time decay floor | 50% | 20% | Unit test |
| Daily P&L | -$0.36 total | +$10-20/day | Trade log |
| Strategy Brier (backtest) | 0.8325 (catastrophic: 98.9% pred → 14.7% actual) | <0.350 | npm run backtest |
| Orphan strategy trades | 16 from zombie period (unlogged) | 0 (WAL from Plan 1) | snapshot |
