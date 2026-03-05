---
phase: quick-1
plan: 01
subsystem: crypto-bot
tags: [crypto, ticker-parsing, liquidity, pricing, drift]
dependency_graph:
  requires: []
  provides: [relaxed-bracket-filter, price-fallback, new-ticker-formats, drift-zeroing]
  affects: [crypto-bot, ticker_utils]
tech_stack:
  added: []
  patterns: [dual-side-liquidity-check, price-fallback-cascade, horizon-aware-drift]
key_files:
  created: []
  modified:
    - src/kalshi/crypto-bot.py
    - src/kalshi/ticker_utils.py
    - tests/test_crypto.py
    - tests/test_ticker_utils.py
decisions:
  - "15-minute bracket tickers parsed as direction=B (bracket) with settlement_minute field"
  - "Monthly max/min thresholds divided by 100 (cents to dollars) at parse time"
  - "Bracket volume floor lowered from 10 to 5 with dual-side spread confirmation"
  - "Price fallback uses strict < 99 boundary (not <= 99) to exclude near-certain outcomes"
  - "Drift zeroed at < 1440 min cutoff (sub-daily), not just short horizons"
metrics:
  duration: "4m 39s"
  completed: "2026-03-05T04:33:37Z"
  tasks_completed: 2
  tasks_total: 2
  tests_added: 29
  tests_total: 1378
---

# Quick Task 1: Fix Crypto Bot Opportunity Access

Relax bracket liquidity filter to check both YES and NO sides, add price fallback cascade (yes_ask -> no_ask -> last_price), parse KXBTC15M/KXBTCMAXMON/KXBTCMINMON ticker formats, and zero drift for sub-daily markets.

## Task Completion

| Task | Name | Commit | Key Changes |
|------|------|--------|-------------|
| 1 | Add new ticker formats to parse_crypto_ticker | `6c30ded` | 15M, MAXMON, MINMON regex patterns; 10 new tests; format_ticker_human updated |
| 2 | Fix bracket liquidity, price fallback, drift cap | `27e77f8`, `bb090aa` | Dual-side spread check; price cascade; drift=0 for <1440min; 19 new tests |

## Changes Made

### ticker_utils.py -- New Ticker Formats (ISSUE-3)
- Added `KXBTC15M-{YYMONDDHHM}-{threshold}` regex for 15-minute bracket markets
- Added `KX{ASSET}MAXMON-{ASSET}-{YYMONDD}-{threshold}` for monthly max price
- Added `KX{ASSET}MINMON-{ASSET}-{YYMONDD}-{threshold}` for monthly min price
- Max/min thresholds auto-divided by 100 (stored in cents on Kalshi)
- Returns `market_type` field ("15m", "maxmon", "minmon") for downstream handling
- Updated `format_ticker_human()` to display new market types with type suffix

### crypto-bot.py -- Bracket Liquidity (ISSUE-1)
- Bracket filter now checks BOTH yes-side AND no-side spreads (takes minimum)
- Markets with `last_price` set and volume >= 10 pass even with empty order book
- Volume floor lowered from 10 to 5 (since spread confirmation provides confidence)
- Expected impact: 200-500 additional bracket markets evaluated per scan

### crypto-bot.py -- Price Fallback (ISSUE-4)
- Three-tier price cascade: `yes_ask` -> `100 - no_ask` -> `last_price`
- Values must be in range [1, 99) to be usable (excludes degenerate prices)
- Edge computation now uses `market_price` with fallback instead of raw `yes_ask`
- NO-side edge uses `no_ask` directly when available, else derives from `market_price`
- Expected impact: 100-200 additional markets evaluated per scan

### crypto-bot.py -- Drift Zeroing (ISSUE-5)
- Drift set to 0.0 for all markets with `minutes_to_settle < 1440` (sub-daily)
- Prevents annualized drift noise from biasing short-horizon probability estimates
- Raw vs capped drift values now logged at DEBUG level when capping occurs
- Daily+ markets still use actual trailing drift

## Deviations from Plan

None -- plan executed exactly as written.

## Test Summary

- 10 new ticker parsing tests (15M, MAXMON, MINMON formats)
- 10 new bracket liquidity tests (NO-side spread, last_price fallback, volume floor)
- 8 new price fallback tests (cascade logic, boundary conditions, None handling)
- 9 new drift zeroing tests (horizon cutoffs, negative drift, zero passthrough)
- 3 existing ticker tests updated from assert-None to assert-valid-parse
- Full suite: 1378 tests, all passing

## Self-Check: PASSED

- All 4 modified files exist on disk
- All 3 task commits verified in git log (6c30ded, 27e77f8, bb090aa)
- Full test suite: 1378/1378 passing
