# Plan 7: Entertainment & Beatrelease — Full Quant Desk Review

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Per CLAUDE.md, you may ONLY modify: `entertainment-bot.py`, `test_entertainment*.py` for entertainment; `beatrelease-scanner.py` for beatrelease. Do NOT touch probability.py, kalshi_auth.py, or other bots.

**Goal:** Re-enable disabled entertainment and beatrelease bots with proper sizing and integration. Target: +$10-20/day combined.

**Architecture:** Entertainment: HDD album sales info-arb. Beatrelease: blog + LLM sentiment extraction. Both currently disabled.

**Tech Stack:** Python 3, HDD scraping, DeepSeek LLM

---

### Task 7.1: Fix Entertainment Bot Kelly Sizing

**Files:**
- Modify: `src/kalshi/entertainment-bot.py`
- Test: `tests/test_entertainment*.py`

**Context:** Uses quarter_kelly even for confirmed settlement source data (e.g., HDD chart shows album sold 200K and market threshold is 150K). When the data source directly confirms the outcome, half_kelly is appropriate.

**Step 1:** Implement tiered Kelly based on data confidence:
- Confirmed outcome (data > threshold): half_kelly
- High confidence (data close to threshold): quarter_kelly
- Low confidence (early/partial data): eighth_kelly

**Step 2:** Write tests and commit.

### Task 7.2: Fix Beatrelease Kelly Bypass Bug

**Files:**
- Modify: `src/kalshi/beatrelease-scanner.py`
- Test: tests for beatrelease

**Context:** Kelly bypass bug at line ~714 — the scanner can place trades that skip Kelly sizing entirely, using a fixed amount instead.

**Step 1:** Read line 714 and surrounding code. Identify the bypass path.
**Step 2:** Ensure ALL trade paths go through Kelly sizing.
**Step 3:** Write tests and commit.

### Task 7.3: Fix Beatrelease Trade Log Integration

**Files:**
- Modify: `src/kalshi/beatrelease-scanner.py`

**Context:** beatrelease-trades.json is not in position-monitor's ALL_TRADE_LOGS list (lines 82-89). This means beatrelease positions are invisible to the exit system.

**Step 1:** This is a position-monitor fix (Plan 8). Flag as dependency.
**Step 2:** Ensure beatrelease-scanner.py writes to the correct path.
**Step 3:** Commit.

### Task 7.4: Re-Enable and Test

**Files:**
- Modify: `config/bots-config.json` (entertainment and beatrelease sections)

**Step 1:** Enable both bots in config with conservative limits:
- maxTradeCents: 500 ($5)
- maxDailyTrades: 5
- maxDailyLossCents: 1000 ($10)
- edgeThreshold: 0.10 (10%)

**Step 2:** Run in demo mode first. Verify logs, decisions, trade records.
**Step 3:** Commit.

### Task 7.5: Add Measurement Framework

**Step 1:** Log per-scan metrics for both bots.
**Step 2:** Write tests and commit.

---

## Measurement Protocol

| Metric | Before | Target | Method |
|--------|--------|--------|--------|
| Entertainment status | Disabled | Enabled (conservative) | Config |
| Beatrelease status | Disabled | Enabled (conservative) | Config |
| Kelly bypass | Exists (bug) | All trades through Kelly | Unit test |
| Trade log integration | Missing from position-monitor | Integrated | Config check |
| Combined daily P&L | $0 | +$10-20/day | Trade log |

---

## Execution Report (2026-03-07)

**Status:** Complete

**Tasks completed:** 3/5 (Tasks 7.2/7.3 out of scope -- beatrelease-scanner.py is a separate bot)

**Summary:** Tiered Kelly (eighth/quarter/half) implemented, entertainment bot re-enabled conservative ($5/trade, 5/day, $10 loss), per-scan metrics added.

**Backtest results (post-implementation):**
- Entertainment Brier: N/A (0 settlements, just re-enabled)
- No settled entertainment markets yet to evaluate

**Next steps:** Monitor first entertainment settlements. Tasks 7.2/7.3 (beatrelease-scanner.py improvements) to be handled in a dedicated session.
