# Plan 6: Source Monitor — Full Quant Desk Review

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Per CLAUDE.md, you may ONLY modify: `source-monitor.py`, `hdd_parser.py`, `test_source*.py`. Do NOT touch probability.py, kalshi_auth.py, or other bots.

**Goal:** Optimize the observed-weather / source-monitor NWS track. NWS timezone and observation gating are already correctly implemented. Focus on edge threshold optimization, data freshness hardening, and measurement. Target: +$20-30/day.

**Architecture:** Real-time data fetching from NWS (weather actuals), HDD (album sales), Box Office Mojo. Trades when external data confirms outcome before market settles.

**Weather-family note:** this is the observed-weather subtrack of the broader weather family. It is currently the strongest realized weather alpha, so its calibration and promotion decisions should be reviewed together with forecast-weather changes.

**Audit note:** for the March 29, 2026 calibration refresh, source-monitor NWS execution update, and validation trail, use [2026-03-29-weather-family-calibration-audit.md](./2026-03-29-weather-family-calibration-audit.md).

**Recovery note:** for the April 2, 2026 outage-recovery hardening, reconciliation repair, and live supervisor cutover, use [2026-04-02-weather-outage-recovery-audit.md](./2026-04-02-weather-outage-recovery-audit.md).

**Tech Stack:** Python 3, NWS API, web scraping

**NOTE:** Pre-plan code review confirmed `_local_today()` date boundaries were correct (per-city `ZoneInfo`). However, during execution the developer found a separate bug: the **hour-of-day** used for the sigma model / pre-dawn gate was using server time, not per-city local time. This meant LAX at 6am PT was being evaluated as 9am ET. Both issues are now fixed and regression-tested.

---

### Task 6.1: Verify and Test NWS Timezone Correctness

**Files:**
- Test: `tests/test_source*.py`

**Context:** Code review confirmed source-monitor correctly uses per-city `ZoneInfo` for timezone-aware date boundaries. Write tests to lock in this behavior and prevent regressions.

**Step 1: Write regression test**

```python
def test_nws_uses_city_local_date():
    """NWS market matching should use city-local date, not server date."""
    from zoneinfo import ZoneInfo
    # At 11pm EST, it's still today in NYC but already tomorrow in UTC
    assert _local_today("NYC") uses ZoneInfo("America/New_York")
    # At 11pm PST, it's already tomorrow in EST
    # Verify LAX market uses Pacific time, not Eastern
```

**Step 2: Run tests and commit**

### Task 6.2: Optimize NWS Edge Thresholds

**Files:**
- Modify: `src/kalshi/source-monitor.py`
- Test: `tests/test_source*.py`

**Context:** NWS uses tiered edge thresholds based on confidence intervals (5%/10%/15%/20%). Analyze historical NWS trade outcomes to optimize these tiers.

**Step 1:** Backtest NWS trades: group by CI tier and compute WR/P&L per tier.
**Step 2:** Adjust thresholds based on backtest. If 5% min-edge trades are profitable, keep. If not, raise.
**Step 3:** Write tests and commit.

### Task 6.3: Harden Data Freshness Validation

**Files:**
- Modify: `src/kalshi/source-monitor.py`
- Test: `tests/test_source*.py`

**Context:** No validation that NWS data is current. If NWS API returns stale data (last observation from 2 hours ago), the bot might trade on outdated information.

**Step 1:** Check observation timestamp in NWS response.
**Step 2:** Skip trade if observation is >90 minutes old.
**Step 3:** Write tests and commit.

### Task 6.4: Add Measurement Framework

**Files:**
- Modify: `src/kalshi/source-monitor.py`
- Output: `data/source-monitor-metrics.json`

**Step 1:** Log per-scan: NWS data freshness, HDD data availability, trades placed, sigma values used, timezone corrections applied.
**Step 2:** Write tests and commit.

---

## Measurement Protocol

| Metric | Before | Target | Method |
|--------|--------|--------|--------|
| Timezone handling | Correct (verified) | Regression-tested | Unit test |
| Pre-dawn gate | Correct (8 AM local) | Regression-tested | Unit test |
| Data freshness check | None | 90-min max age | Unit test |
| Daily P&L | Unknown (mixed with weather) | +$20-30/day | Trade log |

---

## Execution Report (2026-03-07)

**Status:** Complete

**Tasks completed:** 4/4

**Summary:** Timezone regression tests added, per-city local hour fix (was using server time), NWS staleness tightened to 90min, per-scan metrics implemented.

**Backtest results (post-implementation):**
- No settlements yet to measure Brier score
- Source monitor trades are logged separately but no settled outcomes available

**Next steps:** Await first settlement cycle to establish baseline Brier for source-monitor trades.

## Weather-Family Follow-Up

- Use the shared weather family gate in [2026-03-22-weather-observation-window-worklist.md](./2026-03-22-weather-observation-window-worklist.md).
- Treat source-monitor NWS calibration as separate from forecast-weather calibration, but do not promote them independently when the change affects the same weather-family capital bucket.
- When reviewing weather promotions, compare source-monitor NWS performance against forecast-weather performance and rank them together by realized P&L, calibration quality, and execution quality.
- Current live source-monitor posture after the March 29 implementation:
  - refreshed `nws` calibration is live
  - conservative quarter-Kelly sizing is retained
  - the narrow threshold-NO liquidity override is the main live execution change under review
