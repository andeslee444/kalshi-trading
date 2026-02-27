---
phase: 02-position-sizing
verified: 2026-02-27T23:55:00Z
status: passed
score: 11/11 must-haves verified
re_verification: false
---

# Phase 02: Position Sizing Verification Report

**Phase Goal:** All bots use correct position sizing math before being activated for live trading
**Verified:** 2026-02-27T23:55:00Z
**Status:** PASSED
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 1 | `quarter_kelly_sell()` exists and correctly halves `half_kelly_sell()` output | VERIFIED | `probability.py:863` — function exists, `test_halves_half_kelly_sell` passes |
| 2 | `get_status()` reports `available_balance` as `bankroll_cents`, not `total_balance` | VERIFIED | `capital_allocator.py:567` — `"bankroll_cents": available`; test passes |
| 3 | `edge_after_fees()` is prefixed with underscore to signal deprecation | VERIFIED | `probability.py:536` — `def _edge_after_fees`; alias `edge_after_fees = _edge_after_fees` at line 548 |
| 4 | All new sizing functions have passing test coverage | VERIFIED | 8 tests in `TestQuarterKellySell`, 2 in `TestBankrollStatusReporting` — all pass |
| 5 | Entertainment bot uses `quarter_kelly` for all 4 trade call sites | VERIFIED | Lines 358, 417, 525, 583 in `entertainment-bot.py` — all use `quarter_kelly` |
| 6 | Source monitor uses `quarter_kelly` for all 6 trade call sites | VERIFIED | Lines 262, 321, 624, 682, 970, 1045 in `source-monitor.py` — all use `quarter_kelly` |
| 7 | Economics bot uses `quarter_kelly` for its trade call site | VERIFIED | Line 798 in `economics-bot.py` — uses `quarter_kelly` |
| 8 | Cross-platform arb uses `quarter_kelly` for its trade call site | VERIFIED | Line 308 in `cross-platform-arb.py` — uses `quarter_kelly` |
| 9 | Strategy trader uses `quarter_kelly_sell` instead of `half_kelly_sell` | VERIFIED | Line 10 import, line 105 call, line 299 label — all reference `quarter_kelly_sell` |
| 10 | Weather bot preserves calibration-gated tiered sizing (`quarter_kelly` default, `half_kelly`/`high_conviction` when calibrated) | VERIFIED | Lines 326-357 in `weather-bot.py` — 4-tier calibration gate fully implemented |
| 11 | All `sizing_method` labels in trade records accurately reflect the new sizing function used | VERIFIED | All 4 migrated bots use `sizing_method="quarter_kelly"`; strategy trader uses `"quarter_kelly_sell"`; weather bot uses `sizing_label` variable that accurately names each tier |

**Score:** 11/11 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|---------|--------|---------|
| `src/kalshi/probability.py` | `quarter_kelly_sell`, `_edge_after_fees` | VERIFIED | `def quarter_kelly_sell` at line 863; `def _edge_after_fees` at line 536; alias at line 548 |
| `src/kalshi/capital_allocator.py` | Fixed `get_status()` reporting `available` as `bankroll_cents` | VERIFIED | Line 567: `"bankroll_cents": available`; line 568: `"total_balance_cents": total` |
| `tests/test_kelly.py` | `TestQuarterKellySell` class with 8 tests | VERIFIED | Class at line 192, 8 test methods — all pass |
| `tests/test_allocator.py` | `TestBankrollStatusReporting` class with 2 tests | VERIFIED | Class at line 158, 2 test methods — all pass |
| `src/kalshi/entertainment-bot.py` | Quarter-Kelly sized entertainment trades | VERIFIED | 4 call sites use `quarter_kelly`, 4 `sizing_method` labels updated |
| `src/kalshi/source-monitor.py` | Quarter-Kelly sized source monitor trades | VERIFIED | 6 call sites use `quarter_kelly`, 6 `sizing_method` labels updated |
| `src/kalshi/economics-bot.py` | Quarter-Kelly sized economics trades | VERIFIED | 1 call site uses `quarter_kelly`, 1 label updated |
| `src/kalshi/cross-platform-arb.py` | Quarter-Kelly sized cross-platform arb trades | VERIFIED | 1 call site uses `quarter_kelly`, 1 label updated |
| `src/kalshi/strategy-trader.py` | Quarter-Kelly sell-side sized strategy trades | VERIFIED | `quarter_kelly_sell` imported, called at line 105, labeled at line 299 |
| `src/kalshi/weather-bot.py` | Calibration-gated tiered sizing with `quarter_kelly` default | VERIFIED | `_load_calibration` imported at line 10; `is_calibrated` gate at lines 326-357 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `probability.py` | `tests/test_kelly.py` | `from probability import.*quarter_kelly_sell` | WIRED | Line 10: `from probability import half_kelly, half_kelly_sell, quarter_kelly_sell` |
| `capital_allocator.py` | `tests/test_allocator.py` | `bankroll_cents.*available` assertion | WIRED | `test_status_bankroll_is_available` asserts `status["bankroll_cents"] == 2000` (available) |
| `entertainment-bot.py` | `probability.py` | `from probability import.*quarter_kelly` | WIRED | Line 11 import confirmed; 4 call sites in function body |
| `source-monitor.py` | `probability.py` | `from probability import.*quarter_kelly` | WIRED | Line 17 import confirmed; 6 call sites in function body |
| `strategy-trader.py` | `probability.py` | `from probability import.*quarter_kelly_sell` | WIRED | Line 10 import confirmed; call site at line 105 |
| `weather-bot.py` | `config/calibration.json` | `_load_calibration` calibration gate check | WIRED | Line 10 imports `_load_calibration`; line 326 calls it; line 328 checks `per_city` key |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| SIZE-01 | 02-01 | All Kelly sizing functions use available balance (not total balance) as bankroll basis | SATISFIED | `get_status()` returns `available` as `bankroll_cents` (line 567); `TestBankrollStatusReporting` asserts this; REQUIREMENTS.md marks as `[x]` |
| SIZE-02 | 02-01 | Fee treatment is standardized across all bots (single pattern via `fee_cents` parameter) | SATISFIED | `_edge_after_fees` deprecated with underscore prefix + alias; all bots use `kalshi_fee_cents()` and pass result as `fee_cents=fee` to Kelly functions; no bot calls `edge_after_fees`; REQUIREMENTS.md marks as `[x]` |
| SIZE-03 | 02-01, 02-02 | Quarter-Kelly is the default sizing until models are calibration-validated | SATISFIED | 12 call sites across 4 bots migrated to `quarter_kelly`; strategy trader uses `quarter_kelly_sell`; weather bot defaults to `quarter_kelly` for uncalibrated cities; REQUIREMENTS.md marks as `[x]` |

No orphaned requirements found. REQUIREMENTS.md Phase 2 section contains exactly SIZE-01, SIZE-02, SIZE-03, all claimed by plan frontmatter, all verified.

### Anti-Patterns Found

No anti-patterns found in phase 02 modified files. No TODOs, FIXMEs, placeholders, or stub implementations detected across the 10 modified source files.

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | None found | — | — |

### Human Verification Required

No items require human verification. All goal-relevant behaviors (correct function existence, correct wiring, correct test coverage, correct sizing labels, correct calibration gate logic) are fully verifiable from the codebase.

### Additional Evidence

**Full test suite status:** 724 tests pass, 0 failures (`pytest tests/ -q`).

**Commits verified in git history:**
- `3e30aaf` — test: add failing tests for quarter_kelly_sell and get_status bankroll fix
- `4f7efc6` — feat: add quarter_kelly_sell, fix get_status bankroll, deprecate edge_after_fees
- `de4b7b8` — feat: migrate 4 bots from half_kelly to quarter_kelly default sizing
- `582063d` — feat: migrate strategy-trader to quarter_kelly_sell and add weather-bot calibration gate

**Zero `half_kelly` references remain** in the 4 migrated bots (`entertainment-bot.py`, `source-monitor.py`, `economics-bot.py`, `cross-platform-arb.py`) outside of comments.

**Test stubs updated:** `tests/test_economics.py` line 56 and `tests/test_arb.py` line 51 both define `fake_prob.quarter_kelly` (updated from `half_kelly`) — import chains correct.

### Gaps Summary

No gaps found. All 11 must-have truths verified. All 10 artifacts substantive and wired. All 6 key links confirmed. All 3 requirement IDs satisfied. Full test suite green.

---

_Verified: 2026-02-27T23:55:00Z_
_Verifier: Claude (gsd-verifier)_
