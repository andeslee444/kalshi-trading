# Kalshi Trading System — Master Optimization Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement each sub-plan task-by-task. Respect CLAUDE.md file ownership rules — shared infrastructure changes require a dedicated session, bot-specific changes stay in bot sessions.

**Goal:** Maximize P&L to ~3% daily portfolio return (~$150/day on $5K portfolio) with balanced risk, world-class model quality, and full observability.

**Current State (2026-03-07, from `data/financial-snapshot.json` — authoritative source):**
- Deposited: $5,000.00 ($500 initial + $4,500 top-up)
- NAV: $4,982.08 (balance + positions)
- **True Total P&L: -$17.92** (NAV minus deposits — slightly underwater)
- Realized P&L: +$72.49 gross, +$50.25 net of $22.24 fees (97W/58L, 62.6% WR, 208 settlements)
- Weather: +$53.40 (34W/21L, 62% WR) — best bot
- Source-monitor: +$20.85 (4W/3L, 57% WR) — NWS info-arb
- Crypto: +$2.99 (46W/28L, 62% WR) — high volume, thin edge
- Other: -$4.88 (11W/5L) — unattributed bot trades
- Strategy/Entertainment/Beatrelease/Economics: $0 realized (no settled trades yet)
- Implied unrealized: -$68.17 (open positions are net losing)
- **34 orphan API settlements** with no local trade log — ALL from bot processes (no manual trading ever occurred). Zombie processes executed real trades without local logging. Breakdown: 8 weather, 10 album sales, 16 sports/other. This is a critical data integrity gap.
- Critical ops issues: ~30 zombie weather-bot processes, strategy-trader at 99.2% CPU, stale heartbeats
- **Run `npm run snapshot` for latest numbers — do NOT compute P&L from trade logs**

**Architecture:** 8 sub-plans executed in order. Plan 0 (operational triage) first, then Plan 1 (shared infrastructure), then Plans 2-8 (per-bot full quant desk reviews). Each bot plan covers 7 dimensions: data acquisition, signal/model, edge/sizing, execution, exit management, risk controls, measurement framework.

**Daily Target:** 3% of portfolio (~$150/day). This requires deploying more capital with better edge detection, not just higher risk.

---

## Plan Index

| Plan | Scope | Est. Impact | Status |
|------|-------|-------------|--------|
| [Plan 0: Operational Triage](./2026-03-06-plan0-operational-triage.md) | Kill zombies, fix supervisor, single-instance enforcement | Prerequisite | Pending |
| [Plan 1: Shared Infrastructure](./2026-03-06-plan1-shared-infrastructure.md) | probability.py, kalshi_auth.py, capital_allocator.py | Foundation | Pending |
| [Plan 2: Weather Bot](./2026-03-06-plan2-weather-bot.md) | Ensemble optimization, day-0 dedup fix, limit orders | +$50-80/day potential | Pending |
| [Plan 3: Crypto Bot](./2026-03-06-plan3-crypto-bot.md) | OU fix, GARCH stabilization, Kelly stack reduction | +$30-50/day potential | Pending |
| [Plan 4: Economics Bot](./2026-03-06-plan4-economics-bot.md) | GDP sigma fix, concentration limits, belief filter tuning | Risk reduction + edge | Pending |
| [Plan 5: Strategy Trader](./2026-03-06-plan5-strategy-trader.md) | CPU fix, copula_scale calibration, edge formula review | +$10-20/day potential | Pending |
| [Plan 6: Source Monitor](./2026-03-06-plan6-source-monitor.md) | Timezone fix, NWS sigma model, data freshness | +$20-30/day potential | Pending |
| [Plan 7: Entertainment/Beat](./2026-03-06-plan7-entertainment-beat.md) | Re-enable with proper Kelly, fix trade log integration | +$10-20/day potential | Pending |
| [Plan 8: Position Monitor](./2026-03-06-plan8-position-monitor.md) | Decision path fix, model-shift exit improvement | Loss prevention | Pending |
| [Plan 9: Data Quality & S3 Sync](./2026-03-06-plan9-data-quality-and-s3-sync.md) | Pre-upload validation, sync scope, integrity reports | Data reliability | Pending |
| [Plan 10: Weekly Self-Improvement](./2026-03-07-weekly-self-improvement.md) | Auto-calibrate all bots weekly, regression gate, auto-apply | Compounding edge | Pending |

## Cross-Cutting Recommendations (A-F)

These are woven into each sub-plan:

### A. Measurement Framework
Every bot change includes pre/post measurement using `npm run snapshot` as the authoritative P&L source. Key metrics per bot: `realized_pnl.by_bot.{bot}.pnl_cents`, win rate, Brier score, edge-at-entry vs outcome, calibration drift. Per-scan metrics logged to `data/{bot}-metrics.json`. **Never compute P&L from trade logs — always use the snapshot.**

### B. Edge Decay Monitoring
Track edge-at-entry vs final settlement. If edges consistently decay (e.g., weather edges entered at 15% settle at 5%), the model is lagging the market. Each bot logs `edge_at_entry` and compares to `settlement_edge` post-settlement.

### C. Execution Quality
Measure slippage: order price vs fill price vs fair value. Track limit order fill rates. Each bot logs `order_price_cents`, `fill_price_cents`, `model_fair_value_cents` in trade records.

### D. Data Source Redundancy
Add fallback data sources. Weather: Open-Meteo + NWS + WeatherAPI. Crypto: Coinbase + Binance + Kraken. Economics: Cleveland Fed + Truflation + TIPS breakevens. Log which source was used per trade.

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
| Zombie processes | 72 (found during Plan 0) | 0 | Plan 0 ✅ |
| Model staleness alerts | None | Real-time | Plan 1 |
| Edge decay tracking | None | Per-trade | Plan 1 |
| Crypto Brier | 0.3851 (coin-flip, OU corruption) | <0.250 | Plan 3 |
| Strategy Brier | 0.8325 (catastrophic overconfidence) | <0.350 | Plan 5 |
| Open-Meteo errors | 1,870 since Mar 6 | 0 | Plan 2 |
| Orphan trades | 32 (zombie period, no local log) | 0 (WAL) | Plan 1 |
| Calibrators in pipeline | 1 (weather only) | 4 (weather + crypto + CPI + strategy) | Plan 10 |
| Auto-calibration cadence | Manual | Weekly (Sunday 5 AM) with auto-apply | Plan 10 |
| Calibration history | Single backup | Versioned archive with rollback | Plan 10 |
| Per-bot regression gate | None | 5% max regression per bot | Plan 10 |

## Plan Review Findings (2026-03-07)

Comprehensive review of all plans against the actual codebase surfaced these issues, all now corrected in the plans:

### Critical Corrections Applied

1. **OU model is LIVE, not dormant** — `config/bots-config.json` line 96 has `"useOrnsteinUhlenbeck": true`. The OU target = strike price bug (probability.py ~1143) is actively corrupting every crypto probability estimate. Plan 3 Task 3.2 elevated to highest priority with emergency disable option.

2. **Plan 8 Task 8.1 bug didn't exist** — position-monitor line 498 already uses the correct path `kalshi-economics-trades-decisions.json`. Task changed from "fix bug" to "regression test."

3. **Plan 1 Task 1.3 test had wrong function signature** — `uncertainty_kelly` takes `(edge, price_cents, max_cost_cents, bankroll_cents, scenario_agreement, posterior_sigma)`, not a `sigma_mult` parameter. Test code corrected.

4. **Plan 5 Task 5.3 math was wrong** — Longshot edge at 5c is `0.57 * exp(-0.75)` = 26.9%, not 1.35%. Corrected in plan.

5. **Strategy-trader sleep already exists** — Line 842 has `time.sleep(SCAN_INTERVAL * 60)`. CPU spike likely from zombie processes, not missing sleep. Plan 0 Task 0.3 and Plan 5 Task 5.1 updated to diagnose after zombie cleanup.

6. **Economics bot concentration limits already exist** — `_check_concentration()` at line 146 with 10% family / 20% total caps. Plan 1 Task 1.8 updated to add portfolio-level (not per-bot) concentration as a complementary layer. Plan 4 Task 4.5 should verify existing limits, not rewrite.

7. **Duplicate task numbers** — Plan 3 had two Task 3.6 entries. Second renamed to 3.6b.

### Systemic Gaps Identified (not yet in plans)

- **No write-ahead logging** — crash between API order and local log write causes orphan trades. Root cause of the 34-orphan incident.
- **No API rate limit coordination** — 8 concurrent bots may exceed Kalshi rate limits collectively.
- **Stop-loss calibration not reviewed** — all bots use 25-30c stop-loss without empirical validation.
- **No API outage graceful degradation** — position-monitor can't exit positions during outages.
- **Weather bot dedup fix requires shared module change** — Plan 2 Task 2.3 needs TradeManager per-ticker cooldowns, which is a CLAUDE.md file ownership violation for a weather bot session.

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
