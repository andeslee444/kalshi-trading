# Consolidated Quant Desk Design — World-Class Prediction Market Trading

**Date**: 2026-03-02
**Goal**: Build institutional-grade prediction market trading system across two parallel tracks, consolidating all existing design docs into one roadmap.
**Bankroll**: $5,090 live ($3,187 cash + $1,904 in 31 open positions)
**Timeline**: 6-8 weeks aggressive, scaling to full $5K utilization
**Target**: $30-75/day P&L by end of roadmap

## Current State (What's Done)

### Completed Infrastructure (GSD Phases 1-6 + Quant Desk Phase 1)
- Settlement reconciliation pipeline with Brier scores (weather 0.309, crypto 0.037)
- Per-city sigma calibration in calibration.json
- Quarter-Kelly default sizing, calibration-gated weather tiering
- Position monitor with take-profit, stop-loss, trailing stops, model-shift exits
- All 6 bots activated with complete decision logging filter cascades
- Daily calibration pipeline with drift detection and WhatsApp alerts
- Crypto model validated (Brier 0.037, vol config near-optimal)
- Skip audit tool, bankroll-proportional risk limits ($5K), crypto bracket recovery
- Cross-platform arb execution enabled (3% min spread)
- Brier-guided edge threshold recommendations

### Current Portfolio (Live as of 2026-03-02)
| Category | Exposure | % of Equity | Notes |
|----------|----------|-------------|-------|
| CPI (May T2.0 + T2.1) | $1,720 | 33.8% | 71K contracts at 2-3c, bot-placed via economics-bot |
| Core CPI (Jun) | $8 | 0.2% | Small positions across T2.3-T2.6 |
| Sports (March Madness) | $54 | 1.1% | 8 longshot sells |
| Weather (Houston) | $39 | 0.8% | Short positions, Feb settlements |
| Crypto (BTC) | $7 | 0.1% | Small directional |
| Other (entertainment, sports, political) | $76 | 1.5% | Scattered longshot sells |
| **Total deployed** | **$1,904** | **37.4%** | |
| **Cash** | **$3,187** | **62.6%** | |

### Critical Finding: CPI Concentration Risk
The economics bot autonomously placed $1,720 in two correlated CPI bets across 4 scan cycles (Feb 28 - Mar 2). It computed 98% edge at 100% confidence from Cleveland Fed nowcast vs 2-3c market price. The "high-confidence trade" allocator path gave $500+ budget per cycle with no concentration limit. This is the #1 risk management gap — the correlation engine (Track 2, Phase 2) directly addresses this.

The CPI thesis may be correct (tariffs + sticky shelter → CPI stays above 2.0-2.1%), but the sizing is reckless without:
1. Geopolitics-informed conviction (Track 1) — is the macro environment actually supportive?
2. Correlation-adjusted position limits (Track 2) — T2.0 and T2.1 are ~99% correlated
3. Portfolio VaR awareness — what happens to the portfolio if CPI prints below 2.0%?

## Architecture: Two Parallel Tracks

### Execution Model
- **Track 1** runs on branch `track1/alpha-generation`
- **Track 2** runs on branch `track2/quant-infrastructure`
- Separate Claude instances can execute each track simultaneously
- Minimal file overlap (Track 1: bot files + configs, Track 2: new infra modules + allocator)
- Merge to main at each phase completion
- Tracks converge at Phase 7 (microstructure)

### Shared files requiring coordination
- `src/kalshi/capital_allocator.py` — Track 2 adds correlation limits; Track 1 doesn't touch it
- `src/kalshi/probability.py` — Track 1 adds macro adjustment functions; Track 2 adds particle filter integration (different functions, low conflict risk)
- `config/bots-config.json` — Both tracks may adjust configs; merge carefully

```
Week 1-2          Week 3-4           Week 5-6          Week 7-8
─────────────────────────────────────────────────────────────────
TRACK 1: Alpha Generation (branch: track1/alpha-generation)
┌─────────────┐  ┌──────────────┐  ┌──────────────┐
│ T1-Phase 1  │  │  T1-Phase 2  │  │  T1-Phase 3  │──┐
│ Macro/Geo-  │  │  Source Mon  │  │  New Strategy │  │
│ politics    │  │  Optimization│  │  Expansion    │  │
│ Engine      │  │  (all 10)    │  │  + Ops Harden │  │
└─────────────┘  └──────────────┘  └──────────────┘  │
                                                       │ CONVERGE
TRACK 2: Quant Infrastructure (branch: track2/quant-infrastructure)
┌─────────────┐  ┌──────────────┐  ┌──────────────┐  │
│ T2-Phase 1  │  │  T2-Phase 2  │  │  T2-Phase 3  │  │
│ Particle    │  │  Correlation  │  │  Advanced    │──┘
│ Filter      │  │  & Dependency│  │  Simulation   │  │
│ Engine      │  │  Layer       │  │  Engine       │  │
└─────────────┘  └──────────────┘  └──────────────┘  │
                                                       v
                                              ┌──────────────┐
                                              │   Phase 7    │
                                              │ Microstructure│
                                              │ & Attribution │
                                              └──────────────┘
```

## Phase Details

---

### T1-Phase 1: Macro/Geopolitics Engine (Week 1-2)

**Goal**: Build an autonomous macro sentiment engine that produces quantified CPI/GDP/Jobs bias adjustments from curated external data sources. Directly improves the economics bot's $1,720 CPI positions and all future macro trades.

**New module**: `src/kalshi/macro_engine.py`

```
MacroEngine
    __init__(config)
    fetch_all_sources()                    -> MacroSnapshot
    compute_cpi_bias(snapshot)             -> float (+/- adjustment to nowcast)
    compute_inflation_expectation()        -> float (market-implied)
    tariff_impact_estimate()               -> float (% CPI impact from trade policy)
    sentiment_score(source)                -> float [-1, +1]
    aggregate_signal()                     -> MacroSignal (bias, confidence, sources)
    serialize() / deserialize()            -> JSON persistence
```

**Data Sources (curated blog/newsletter scraping — no X API)**:

| Source | Data | Update Freq | Method |
|--------|------|-------------|--------|
| Kobeissi Letter (kobeissiletter.com) | Macro commentary, tariff analysis, CPI previews | Daily | RSS/blog scrape + DeepSeek LLM extraction |
| Truflation (truflation.com) | Real-time blockchain-based CPI tracking | Daily | Public API (free tier) |
| TIPS Breakeven Rates | Market-implied inflation expectations | Daily | FRED API (free, no auth) |
| Cleveland Fed Nowcast | CPI point estimate (already have) | Daily | Existing scraper |
| University of Michigan | Consumer inflation expectations | Monthly | FRED API |
| BLS Release Calendar | Exact CPI/Jobs/GDP release dates and actuals | Per release | HTML scrape |
| Reuters/AP Headlines | Breaking tariff/trade policy news | Hourly | RSS feed |
| Atlanta Fed GDPNow | Real-time GDP tracking estimate | Weekly | Public API |
| AAA Gas Prices | Daily national average (8% CPI weight) | Daily | Existing scraper |
| Import Price Index | Tariff pass-through signal | Monthly | BLS API |

**LLM-Powered Sentiment Extraction**:
- DeepSeek API (already have key from beatrelease scanner)
- Prompt: "Extract CPI/inflation outlook from this article. Output: direction (higher/lower/neutral), magnitude (1-5), confidence (0-1), key factors."
- Applied to Kobeissi blog posts and Reuters headlines
- Cached per-source with 4h TTL (same pattern as beatrelease scanner)

**Integration with economics-bot.py**:
```python
from macro_engine import MacroEngine

macro = MacroEngine(config)
snapshot = macro.fetch_all_sources()
signal = macro.aggregate_signal()

# Adjust nowcast with macro bias
adjusted_nowcast = cleveland_nowcast + signal.cpi_bias
adjusted_sigma = base_sigma * (1.0 - 0.3 * signal.confidence)  # Higher confidence = tighter sigma

# Use adjusted values in probability computation
prob = econ_nowcast_probability(adjusted_nowcast, adjusted_sigma, threshold, direction)
```

**Testing**: Unit tests for each source parser, mock LLM responses, integration test with fixture data, backtest macro-adjusted vs raw nowcast on historical CPI releases.

---

### T1-Phase 2: Source Monitor Optimization (Week 3-4)

**Goal**: Implement all 10 optimizations from the Feb 24 source monitor design doc.

**Incorporates**: `docs/plans/2026-02-24-source-monitor-optimization-design.md` (full scope)

| # | Optimization | Files |
|---|---|---|
| 1a | Data freshness validation (HDD 72h/168h gates, box office day-of-week) | source-monitor.py |
| 1b | Retry logic with exponential backoff (2 retries per source) | source-monitor.py |
| 1c | TMDb API for structured box office data (fallback to HTML scraping) | source-monitor.py, config |
| 2a | Time-decay sigma for album/box office (50% per 48h staleness) | probability.py |
| 2b | CI-based NWS edge thresholds (replace hour-17 step function) | probability.py, source-monitor.py |
| 3a | Entertainment bot consolidation (disable, absorb into source-monitor) | config, source-monitor.py |
| 3b | Cross-market consistency validation (monotonic probability check) | source-monitor.py |
| 3c | High-confidence sizing for info-arb (85% conf, 10% edge gate) | capital_allocator.py |
| 4a | Source monitor test suite (30+ test cases, 8 test groups) | tests/test_source_monitor.py |
| 4b | Settlement feedback loop (info-arb calibration in calibrate-sigma.py) | calibrate-sigma.py |

---

### T1-Phase 3: New Strategies + Operational Hardening (Week 5-6)

**Goal**: Expand into new alpha sources and harden daily operations.

**Incorporates**: GSD Phases 7 and 8

| Deliverable | Source |
|---|---|
| Box office trading bot (The Numbers/Box Office Mojo scraper → KXBOX/KXMOVIE markets) | GSD Phase 7 |
| Weather city expansion (verify against live Kalshi KXHIGH tickers) | GSD Phase 7 |
| Daily P&L report automation (cron, WhatsApp) | GSD Phase 8 |
| Data source health monitoring (detect stale/failed sources within 2 cycles) | GSD Phase 8 |
| HDD Sanity CMS endpoint health checks + auto-re-enable | GSD Phase 8 |

---

### T2-Phase 1: Particle Filter Engine (Week 1-2)

**Goal**: Replace stateless per-scan probability snapshots with Bayesian belief state that carries memory across scans.

**Incorporates**: Quant Desk Phase 2 (full spec in `2026-03-01-quant-desk-design.md`)

**Key deliverables**:
- `src/kalshi/particle_filter.py` — Sequential Monte Carlo with systematic resampling
- Per-bot configuration: crypto (process_vol=0.02, 5-min scans), weather (0.01, 30-min), economics (0.005, 6h), entertainment (0.005, 15-min)
- CI-aware sizing: narrow CI = normal Kelly, wide CI = reduced Kelly
- Trend detection: 3+ consecutive same-direction updates = `trend_confirmed` signal
- State persistence in `data/pf-state-{bot}.json` with staleness check
- Integration with crypto-bot (highest value — replaces raw model_prob with filtered_prob)

---

### T2-Phase 2: Correlation & Dependency Layer (Week 3-4)

**Goal**: Model portfolio-level risk to prevent CPI-style concentration blowups.

**Incorporates**: Quant Desk Phase 3 (full spec in design doc)

**Key deliverables**:
- `src/kalshi/correlation_engine.py` — Pairwise correlation, Student-t copula, portfolio VaR
- Layer 1: Empirical 30-day rolling correlation matrix
- Layer 2: t-copula for tail dependence (requires scipy — first scipy dependency)
- Layer 3: Hierarchical clustering → per-cluster concentration limits
- Integration with capital_allocator.py:
  - New check #6: cluster concentration limit
  - New check #7: marginal VaR threshold
  - New check #8: tail risk Kelly reduction (25% cut when tail dependence > 0.15)
- **This directly prevents another 34% single-factor concentration**

---

### T2-Phase 3: Advanced Simulation Engine (Week 5-6)

**Goal**: Importance sampling for tail-risk contracts, regime detection, jump-diffusion crypto.

**Incorporates**: Quant Desk Phase 4 (full spec in design doc)

**Key deliverables**:
- `src/kalshi/simulation.py` — Importance sampling + antithetic/control/stratified variance reduction
- `src/kalshi/regime_detector.py` — HMM-based 4-state vol regime (low/normal/high/crisis)
- Jump-diffusion model for crypto (`crypto_price_probability_jd()` in probability.py)
- Regime-adjusted Kelly: crisis = -50%, high vol = -25%
- Weather regime: ensemble spread tracking → adaptive sigma/sizing

---

### Phase 7: Market Microstructure & Intelligence (Week 7-8, both tracks converge)

**Goal**: Agent-based order book simulation, P&L attribution, competitive edge monitoring.

**Incorporates**: Quant Desk Phase 5 (full spec in design doc)

**Key deliverables**:
- `src/kalshi/orderbook_sim.py` — Agent-based market simulation for MM activation
- `src/kalshi/pnl_attribution.py` — P&L decomposition by component/bot/regime/edge bucket
- `src/kalshi/edge_monitor.py` — Edge decay detection, competitor alerts, optimal allocation
- Execution quality analytics: fill rate tracking, slippage, implementation shortfall
- Market maker activation: per-market calibrated Avellaneda-Stoikov params
- Dashboard integration: attribution tab, edge decay charts

---

## Success Criteria

| Metric | Current | Week 4 Target | Week 8 Target |
|--------|---------|---------------|---------------|
| Trade rate (trades/evaluated) | ~5% | ~15% | ~20% |
| Brier score (crypto) | 0.037 | 0.035 | 0.025 |
| Brier score (weather) | ~0.15 | 0.12 | 0.08 |
| Max single-factor concentration | 34% (CPI!) | <15% | <10% |
| Portfolio VaR accuracy | Not measured | Measured | Within 10% of realized |
| Edge decay detection | Not measured | Not measured | Detect within 7 days |
| Fill rate (limit orders) | ~50% est | ~60% | ~75% |
| Daily P&L (target) | ~$0 | $15-30/day | $30-75/day |

## Dependencies to Add

- `scipy` (T2-Phase 2+): t-copula fitting, optimization, statistical tests
- `feedparser` (T1-Phase 1): RSS feed parsing for Kobeissi/Reuters
- No other new dependencies

## Superseded Documents

This consolidated design supersedes and incorporates:
- `docs/plans/2026-02-26-quant-roadmap.md` — Original P0-P4 checklist (all P0/P1 items done)
- `docs/plans/2026-03-01-quant-desk-design.md` — Quant desk simulation phases 2-5 (fully incorporated)
- `docs/plans/2026-03-01-phase1-unblock-the-flow.md` — Phase 1 implementation plan (COMPLETE)
- `docs/plans/2026-02-24-source-monitor-optimization-design.md` — 10 optimizations (incorporated in T1-Phase 2)
- `docs/plans/2026-02-24-source-monitor-optimization-plan.md` — Implementation plan (incorporated in T1-Phase 2)
- `.planning/ROADMAP.md` — GSD phases 7-8 remaining (incorporated in T1-Phase 3)

---
*Last updated: 2026-03-02*
