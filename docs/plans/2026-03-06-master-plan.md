# Kalshi Trading System — Master Optimization Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement each sub-plan task-by-task. Respect CLAUDE.md file ownership rules — shared infrastructure changes require a dedicated session, bot-specific changes stay in bot sessions.

**Goal:** Maximize P&L to ~3% daily portfolio return (~$150/day on $5K portfolio) with balanced risk, world-class model quality, and full observability.

**Current State (2026-03-07 18:37 UTC, from `data/financial-snapshot.json` — authoritative source):**
- Deposited: $5,000.00 ($500 initial + $4,500 top-up)
- NAV: $4,997.45 (balance + positions)
- **True Total P&L: -$2.55** (NAV minus deposits — nearly break-even after Plan 0+1 fixes)
- Realized P&L: +$81.37 net after fees (98W/61L, 61.6% WR)
- Weather: +$53.40 (34W/21L, 62% WR) — best bot
- Source-monitor: +$20.85 (4W/3L, 57% WR) — NWS info-arb
- Crypto: +$2.99 (46W/28L, 62% WR) — high volume, thin edge
- Other: -$4.88 (11W/5L) — unattributed bot trades
- Strategy/Entertainment/Beatrelease/Economics: $0 realized (no settled trades yet)
- **34 orphan API settlements** with no local trade log — ALL from bot processes (no manual trading ever occurred). Zombie processes executed real trades without local logging. Breakdown: 8 weather, 10 album sales, 16 sports/other. Root cause identified (crash between API call and log write); WAL fix deployed in Plan 1.
- Ops issues resolved: 72 zombies killed (Plan 0), OU model emergency-disabled, calibration refreshed (212 settlements)
- **Strategy-trader Brier 0.8325 — DISABLED pending Plan 5 model fix (worse than random)**
- **Run `npm run snapshot` for latest numbers — do NOT compute P&L from trade logs**

**Architecture:** 8 sub-plans executed in order. Plan 0 (operational triage) first, then Plan 1 (shared infrastructure), then Plans 2-8 (per-bot full quant desk reviews). Each bot plan covers 7 dimensions: data acquisition, signal/model, edge/sizing, execution, exit management, risk controls, measurement framework.

**Daily Target:** 3% of portfolio (~$150/day). This requires deploying more capital with better edge detection, not just higher risk.

---

## Plan Index

| Plan | Scope | Est. Impact | Status |
|------|-------|-------------|--------|
| [Plan 0: Operational Triage](./2026-03-06-plan0-operational-triage.md) | Kill zombies, fix supervisor, single-instance enforcement | Prerequisite | **Complete** ✅ |
| [Plan 1: Shared Infrastructure](./2026-03-06-plan1-shared-infrastructure.md) | probability.py, kalshi_auth.py, capital_allocator.py | Foundation | **Complete** ✅ (Task 1.11 rate limiter deferred) |
| [Plan 2: Forecast Weather](./2026-03-06-plan2-weather-bot.md) | Open-Meteo ensemble optimization, day-0 dedup fix, limit orders | +$50-80/day potential | **Complete** ✅ + Phase 4 shadow-review follow-up |
| [Plan 3: Crypto Bot](./2026-03-06-plan3-crypto-bot.md) | OU fix, GARCH stabilization, Kelly stack reduction | +$30-50/day potential | **Complete** ✅ (OU infra ready, re-enable after backtest validation) |
| [Plan 4: Economics Bot](./2026-03-06-plan4-economics-bot.md) | GDP sigma fix, concentration limits, belief filter tuning | Risk reduction + edge | **Complete** ✅ |
| [Plan 5: Strategy Trader](./2026-03-06-plan5-strategy-trader.md) | **DISABLED** — CPU fix, edge formula rewrite, copula calibration | +$10-20/day potential | **Complete** ✅ (bot remains disabled pending Brier < 0.35 on new settlements) |
| [Plan 6: Source Monitor](./2026-03-06-plan6-source-monitor.md) | NWS weather info-arb, timezone fix, data freshness, edge optimization | +$20-30/day potential | **Complete** ✅ |
| [Plan 7: Entertainment/Beat](./2026-03-06-plan7-entertainment-beat.md) | Re-enable with proper Kelly, fix trade log integration | +$10-20/day potential | **Complete** ✅ |
| [Plan 8: Position Monitor](./2026-03-06-plan8-position-monitor.md) | Decision path fix, model-shift exit, stop-loss calibration | Loss prevention | **Complete** ✅ |
| [Plan 9: Data Quality & S3 Sync](./2026-03-06-plan9-data-quality-and-s3-sync.md) | Pre-upload validation, sync scope, integrity reports | Data reliability | **Complete** ✅ |
| [Plan 10: Weekly Self-Improvement](./2026-03-07-weekly-self-improvement.md) | Auto-calibrate all bots weekly, regression gate, auto-apply | Compounding edge | **Complete** ✅ |

Observation-window follow-up:

- Weather family Phase 4 safe-now worklist:
  - [2026-03-22-weather-observation-window-worklist.md](./2026-03-22-weather-observation-window-worklist.md)
- Weather shadow refresh orchestrator:
  - `python3 scripts/weather-shadow-refresh.py`
  - use `--refresh-shadow-prior` only for shadow outputs under `data/shadow/**`
- Weather family ranked post-window promotion list:
  - [2026-03-22-weather-april1-promotion-list.md](./2026-03-22-weather-april1-promotion-list.md)
- Weather-family March 29 implementation audit note:
  - [2026-03-29-weather-family-calibration-audit.md](./2026-03-29-weather-family-calibration-audit.md)
- Weather-family April 2 outage-recovery audit note:
  - [2026-04-02-weather-outage-recovery-audit.md](./2026-04-02-weather-outage-recovery-audit.md)
- Weather-family April 3 Open-Meteo follow-on:
  - NBM promoted into the live forecast-weather prior
  - AIFS explicitly held back after zero usable training pairs in end-to-end tests
  - `python3 scripts/weather-intraday-feature-audit.py` added as a research-only same-day HRRR 15-minute regime surface
- Event-ledger post-Phase-4 storage retention plan:
  - [2026-03-22-ledger-storage-retention-plan.md](./2026-03-22-ledger-storage-retention-plan.md)

## Cross-Cutting Recommendations (A-F)

These are woven into each sub-plan:

### A. Measurement Framework
Every bot change includes pre/post measurement using `npm run snapshot` as the authoritative P&L source. Key metrics per bot: `realized_pnl.by_bot.{bot}.pnl_cents`, win rate, Brier score, edge-at-entry vs outcome, calibration drift. Per-scan metrics logged to `data/{bot}-metrics.json`. **Never compute P&L from trade logs — always use the snapshot.**

### B. Edge Decay Monitoring
Track edge-at-entry vs final settlement. If edges consistently decay (e.g., weather edges entered at 15% settle at 5%), the model is lagging the market. Each bot logs `edge_at_entry` and compares to `settlement_edge` post-settlement.

### C. Execution Quality
Measure slippage: order price vs fill price vs fair value. Track limit order fill rates. Each bot logs `order_price_cents`, `fill_price_cents`, `model_fair_value_cents` in trade records.

### D. Data Source Redundancy
Add fallback data sources. Weather family: forecast weather uses Open-Meteo + WeatherAPI, while NWS weather uses official NWS data and remains the separate observed-weather track. Crypto: Coinbase + Binance + Kraken. Economics: Cleveland Fed + Truflation + TIPS breakevens. Log which source was used per trade.

### E. Capital Deployment Efficiency
Current: ~$918 deployed over full history. Target: $500-1000/day deployed across all bots. Capital allocator needs to be more aggressive when edges are strong and more defensive when correlation is high.

### F. Correlation Dashboard
Real-time correlation monitoring across bots. If weather and economics are both long on "above" outcomes, the portfolio has directional risk. Capital allocator should enforce cross-bot correlation limits.

## Execution Order & Dependencies

```
Plan 0 (Ops Triage) ──> Plan 1 (Shared Infra) ──┬──> Plan 2 (Weather)
                                                  ├──> Plan 3 (Crypto)
                                                  ├──> Plan 4 (Economics)
                                                  ├──> Plan 5 (Strategy) ──┐
                                                  ├──> Plan 6 (Source Monitor)  │
                                                  ├──> Plan 7 (Entertainment)   │
                                                  ├──> Plan 8 (Position Monitor)│
                                                  ├──> Plan 9 (Data Quality)    │
                                                  └──> Plan 10 (Self-Improve) <─┘
```

Plans 2-8 can be executed in parallel after Plan 1, each in its own session respecting CLAUDE.md file ownership.

## Success Criteria

| Metric | Current | Target | Timeframe |
|--------|---------|--------|-----------|
| Daily P&L | ~$2.50/day avg realized | $150/day (3%) | 30 days |
| Win Rate | 62.6% | 72%+ | 30 days |
| Brier Score (weather) | 0.309 | <0.250 | 14 days |
| Max single-market exposure | $2,322 (CPI) | <$500 | Immediate (prevention) |
| Capital deployed/day | ~$30/day avg | $500-1000/day | 30 days |
| Zombie processes | 72 (found during Plan 0) | 0 | Plan 0 ✅ (72 killed) |
| Model staleness alerts | None | Real-time | Plan 1 ✅ (check_calibration_freshness) |
| Edge decay tracking | None | Per-trade | Plan 1 ✅ (edge_at_entry in golden record) |
| Crypto Brier | 0.3851 (coin-flip, OU corruption) | <0.250 | Plan 3 |
| Strategy Brier | 0.8325 (catastrophic overconfidence) | <0.350 | Plan 5 ✅ (model rewritten, awaiting new settlements) |
| Open-Meteo errors | 1,870 since Mar 6 | 0 | Plan 2 |
| Orphan trades | 34 (zombie period, no local log) | 0 (WAL) | Plan 1 ✅ (WAL deployed) |
| Calibrators in pipeline | 1 (weather only) | 4 (weather + crypto + CPI + strategy) | Plan 10 |
| Auto-calibration cadence | Manual | Weekly (Sunday 5 AM) with auto-apply | Plan 10 |
| Calibration history | Single backup | Versioned archive with rollback | Plan 10 |
| Per-bot regression gate | None | 5% max regression per bot | Plan 10 |

## PM Audit (2026-03-07 evening)

Plans 0 and 1 verified complete against codebase. Plans 2-10 reviewed for accuracy. 8 issues found and corrected in plans.

### PM Execution Review (2026-03-07 night)

All 10 plans executed. 71/71 tasks completed. All deferred items (strategy 5.1-5.5, beatrelease 7.2/7.3, OU-target) completed in follow-up session. ~1,100 new tests added.

**Brier scores unchanged** — expected. All 242 evaluated settlements are from pre-fix trades. Structural improvements only affect trades placed going forward. **First meaningful signal: 2026-03-14** when ~50-100 new trades settle under improved models.

**Discrepancy noted — Plan 6 timezone:** Plan said NWS timezone handling was "already correct." Developer found and fixed a separate bug (server time used for hour-of-day in sigma calculation, distinct from the date boundary `_local_today()` which WAS correct). Good catch. Plan 6 NOTE updated to reflect this was a real fix.

**Completed deferred items (2026-03-07 late):**

| Item | Status | Verified |
|------|--------|----------|
| ~~Plan 5 Tasks 5.1-5.5 (strategy model rewrite)~~ | ✅ Complete — sqrt time decay, 20% floor, category penalty, CorrelationAwareSizer, per-scan metrics | PM verified |
| ~~Beatrelease Tasks 7.2/7.3 (Kelly bypass + trade log)~~ | ✅ Complete — zero Kelly skips trade, position-monitor uses canonical trade_files.py | PM verified |
| ~~probability.py `ou_target` param for crypto~~ | ✅ Complete — param added, compute_ou_target() computes 24h VWAP, crypto-bot passes through | PM verified |

**Remaining open items:**

| Item | Blocker | Priority | Trigger |
|------|---------|----------|---------|
| Strategy bot re-enable | Brier 0.8325 on old data; need ≥20 new settlements with Brier < 0.35 | **HIGH** | 2026-03-14 checkpoint |
| Crypto OU re-enable | Infrastructure ready; need backtest showing Brier improvement over disabled baseline | **MEDIUM** | Run OU backtest on ≥50 settlements |
| Plan 1 Task 1.11 (API rate limiter) | Not blocking yet | **LOW** | When 429 error rate increases |
| LAX/MIA weather sigma tuning | Per-city Brier 0.53/0.59 | **MEDIUM** | Next calibration cycle (Sunday auto-cal) |
| Crypto overconfidence (0.93 pred → 0.50 actual) | Model structural issue | **MONITOR** | Review after 1 week of new settlements |

**Review checkpoint: 2026-03-14** — Run `npm run backtest` + `npm run snapshot`. Expected: weather Brier < 0.28, crypto Brier < 0.35. Strategy re-enable decision point. If regression, Sunday auto-calibration will flag via WhatsApp.

## Plan Review Findings (2026-03-07)

Comprehensive review of all plans against the actual codebase surfaced these issues, all now corrected in the plans:

### Critical Corrections Applied

1. **OU model was LIVE, now emergency-disabled** — `config/bots-config.json` line 94 had `"useOrnsteinUhlenbeck": true`. The OU target = strike price bug (probability.py ~1162) was actively corrupting every crypto probability estimate. Emergency disable applied during Plan 0 execution. Plan 3 Task 3.2 covers proper OU fix (VWAP target) for eventual re-enable.

2. **Plan 8 Task 8.1 bug didn't exist** — position-monitor line 498 already uses the correct path `kalshi-economics-trades-decisions.json`. Task changed from "fix bug" to "regression test."

3. **Plan 1 Task 1.3 test had wrong function signature** — `uncertainty_kelly` takes `(edge, price_cents, max_cost_cents, bankroll_cents, scenario_agreement, posterior_sigma)`, not a `sigma_mult` parameter. Test code corrected.

4. **Plan 5 Task 5.3 math was wrong** — Longshot edge at 5c is `0.57 * exp(-0.75)` = 26.9%, not 1.35%. Corrected in plan.

5. **Strategy-trader sleep already exists** — Line 842 has `time.sleep(SCAN_INTERVAL * 60)`. CPU spike likely from zombie processes, not missing sleep. Plan 0 Task 0.3 and Plan 5 Task 5.1 updated to diagnose after zombie cleanup.

6. **Economics bot concentration limits already exist** — `_check_concentration()` at line 146 with 15% family / 40% total caps (corrected from earlier 10%/20% estimate). Plan 1 Task 1.8 added portfolio-level (not per-bot) concentration as a complementary layer. Plan 4 Task 4.5 should verify existing limits, not rewrite.

7. **Duplicate task numbers** — Plan 3 had two Task 3.6 entries. Second renamed to 3.6b.

### Systemic Gaps Identified (tracked in plans)

- **~~No write-ahead logging~~** — ✅ Fixed in Plan 1 Task 1.10 (WAL in TradeManager).
- **No API rate limit coordination** — 8 concurrent bots may exceed Kalshi rate limits collectively. → **Plan 1 Task 1.11 (NEW)**.
- **Stop-loss calibration not reviewed** — all bots use 25-30c stop-loss without empirical validation. → **Plan 8 Task 8.7 (NEW)**.
- **No API outage graceful degradation** — position-monitor can't exit positions during outages. (Deferred — low frequency risk.)
- **~~Weather bot dedup fix requires shared module change~~** — Resolved: Plan 2 Task 2.3 implements cooldown locally in weather-bot.py (no shared module change needed).
- **Supervisor race condition on crash-restart** — Plan 0 Task 0.2 found duplicate bot instances after restart. Root cause undiagnosed. (Low priority — monitor.)

### Test Quality Assessment

- 29% of plan tasks (16/56) have test code; many are pseudocode or use wrong imports
- Plans 5 (Strategy) and 7 (Entertainment) have 0% test coverage in plan
- Tests should use conftest `load_bot_module()` pattern and `_reset_calibration()` for probability tests
- Implementation sessions should write real tests at execution time, not rely solely on plan pseudocode

---

## Risk Guardrails (Do NOT Change)

- Kill switch (`data/HALT_TRADING`) remains active
- Circuit breaker pattern stays
- No single trade >$10 without explicit override
- Half-Kelly maximum (never full Kelly)
- All changes prevent FUTURE concentration, do NOT cut existing positions
