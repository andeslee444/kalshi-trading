---
phase: 03-position-management
plan: 01
subsystem: trading
tags: [position-management, exits, take-profit, stop-loss, model-shift, kelly, whatsapp]

# Dependency graph
requires:
  - phase: 01-feedback-loop
    provides: "Trade log golden record, probability models, kalshi_fee_cents"
  - phase: 02-position-sizing
    provides: "Quarter-Kelly sizing, half_kelly_sell"
provides:
  - "sell_position with market order support (order_type parameter)"
  - "Per-bot exit config in bots-config.json (6 bots)"
  - "_get_exit_config() per-bot threshold routing"
  - "Partial take-profit exits (50% default fraction, limit orders)"
  - "Full stop-loss exits (market orders for urgent execution)"
  - "Multi-model model-shift routing via _compute_current_probability"
  - "WhatsApp notifications on every successful exit"
  - "30 tests for exit logic"
affects: [03-position-management, 04-bot-activation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-bot exit config pattern: BOT_CONFIG_MAP + _get_exit_config(source_bot)"
    - "Order type routing: limit for take-profit/model-shift, market for stop-loss"
    - "Partial exit pattern: take_profit_fraction * count, minimum 1 contract"
    - "Multi-model probability routing: _compute_current_probability dispatcher"

key-files:
  created:
    - tests/test_position_exits.py
  modified:
    - src/kalshi/kalshi_auth.py
    - src/kalshi/position-monitor.py
    - config/bots-config.json
    - tests/test_optimization.py

key-decisions:
  - "Market orders for stop-loss (urgent exit), limit orders for take-profit/model-shift (patient exit)"
  - "50% partial exit for take-profit (lock in gains, let remainder ride to settlement)"
  - "Crypto gets tighter thresholds (85c TP, 25c SL, 15pp model-shift) due to higher volatility"
  - "Entertainment/beatrelease model-shift deferred (no live data source to recompute probability)"
  - "Removed hardcoded info-arb skip logic; per-bot exit config handles naturally"

patterns-established:
  - "BOT_CONFIG_MAP: source_bot name to bots-config.json key mapping"
  - "_get_exit_config: per-bot threshold lookup with position_monitor fallback defaults"
  - "exit_signal dict includes order_type field for sell_position routing"

requirements-completed: [EXIT-01, EXIT-02, EXIT-03]

# Metrics
duration: 7min
completed: 2026-02-28
---

# Phase 3 Plan 1: Position Exit Execution Summary

**Per-bot take-profit (partial, limit), stop-loss (full, market), model-shift (multi-model routing) exits with WhatsApp alerts and 30 tests**

## Performance

- **Duration:** 7 min
- **Started:** 2026-02-28T03:08:22Z
- **Completed:** 2026-02-28T03:15:41Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- sell_position supports both limit and market order types via order_type parameter
- Per-bot exit thresholds in bots-config.json (6 bots configured with exit blocks)
- Take-profit exits sell partial fraction (50%) at current bid using limit orders
- Stop-loss exits sell full position using market orders for urgent execution
- Model-shift routes to correct probability model by source_bot (weather->NWS, others deferred)
- WhatsApp notification fires on every successful exit with entry/exit price and P&L
- 30 new tests covering all exit logic (take-profit, stop-loss, model-shift, config routing)

## Task Commits

Each task was committed atomically:

1. **Task 1: Add market order support and per-bot exit config** - `a05684f` (feat)
2. **Task 2: Implement per-bot exit routing, partial exits, market order stops, and WhatsApp alerts** - `4ebc84d` (feat)

## Files Created/Modified
- `src/kalshi/kalshi_auth.py` - sell_position gains order_type parameter; market orders omit price field
- `src/kalshi/position-monitor.py` - _get_exit_config, _compute_current_probability, rewritten evaluators, WhatsApp alerts
- `config/bots-config.json` - Added weather section, exit blocks in 6 bot configs
- `tests/test_position_exits.py` - 30 tests for exit logic (created)
- `tests/test_optimization.py` - Fixed fake_auth to include notify_whatsapp

## Decisions Made
- Market orders for stop-loss (urgent exit), limit orders for take-profit/model-shift (patient exit)
- 50% partial exit for take-profit to lock in gains while letting the remainder ride to settlement
- Crypto gets tighter thresholds (85c TP, 25c SL, 15pp model-shift) due to higher volatility
- Entertainment/beatrelease model-shift deferred -- no live data source to recompute probability
- Removed hardcoded info-arb skip logic; per-bot exit config handles this naturally (source-monitor can set high TP threshold)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed test_optimization.py fake_auth missing notify_whatsapp**
- **Found during:** Task 2 (position-monitor modification)
- **Issue:** test_optimization.py creates a fake kalshi_auth module for loading position-monitor, but the new notify_whatsapp import was missing from the fake
- **Fix:** Added `fake_auth.notify_whatsapp = lambda *a, **kw: None` to the fake module
- **Files modified:** tests/test_optimization.py
- **Verification:** All 4 TestSettlementAwareCleanup tests pass
- **Committed in:** 4ebc84d (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Minor fix to existing test fixture to accommodate new import. No scope creep.

## Issues Encountered
- Xcode license agreement expired, blocking git operations. Resolved by using /Library/Developer/CommandLineTools/usr/bin/git directly.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Take-profit, stop-loss, and model-shift exits are fully implemented and tested
- Plan 03-02 (trailing stops, stale order TTL, dashboard panel) can proceed
- Trailing stop evaluator exists but uses global config; 03-02 will add per-bot trailing config

## Self-Check: PASSED

- All 5 files FOUND
- Both commits (a05684f, 4ebc84d) FOUND
- 30 tests collected and passing
- 754 full test suite passes (no regressions)

---
*Phase: 03-position-management*
*Completed: 2026-02-28*
