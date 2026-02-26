# Architecture Patterns

**Domain:** Automated prediction market quant trading system (Kalshi)
**Researched:** 2026-02-26
**Overall Confidence:** MEDIUM-HIGH (based on existing codebase analysis, quant system patterns from multiple sources, and Kalshi-specific domain knowledge)

## Recommended Architecture

The system should evolve from its current "independent daemons sharing files" architecture to a **pipeline-oriented architecture with a centralized calibration feedback loop** while preserving the simplicity of single-machine, file-based deployment.

The key architectural insight: the existing system has the right components but lacks the *connective tissue* between them. Each bot operates independently with hardcoded or uncalibrated parameters. The improvements needed are not new components but new *data flows* connecting existing components into closed loops.

### Architecture Overview

```
                    EXTERNAL DATA SOURCES
                    |                    |
    [Weather APIs]  [NWS Stations]  [HDD/Box Office]  [Coinbase/Deribit]  [Cleveland Fed]
         |               |                |                   |                  |
         v               v                v                   v                  v
    +------------------------------------------------------------------------+
    |                     DATA INGESTION LAYER                               |
    |  (per-bot fetchers, normalized into common signal format)              |
    +------------------------------------------------------------------------+
         |
         v
    +------------------------------------------------------------------------+
    |                     SIGNAL GENERATION LAYER                            |
    |  probability.py models: weather CDF, info-arb, crypto GBM, econ CDF  |
    |  <-- calibration.json parameters feed in here                         |
    +------------------------------------------------------------------------+
         |
         v
    +------------------------------------------------------------------------+
    |                     RISK & SIZING LAYER                                |
    |  capital_allocator.py: portfolio limits, dedup, concentration         |
    |  probability.py: half_kelly, quarter_kelly, high_conviction_kelly     |
    +------------------------------------------------------------------------+
         |
         v
    +------------------------------------------------------------------------+
    |                     EXECUTION LAYER                                     |
    |  kalshi_auth.py: TradeManager (guardrails, atomic logging)            |
    |  KalshiClient: API calls, retry, circuit breaker                      |
    +------------------------------------------------------------------------+
         |
         v
    +------------------------------------------------------------------------+
    |                     POSITION MANAGEMENT LAYER                          |
    |  position-monitor.py: TP, SL, trailing stop, model-shift exits        |
    |  Reads entry records from all bot trade logs                          |
    +------------------------------------------------------------------------+
         |
         v
    +------------------------------------------------------------------------+
    |                     SETTLEMENT & RECONCILIATION                        |
    |  reconcile-trades.py: match trades to settlement outcomes             |
    |  backfill-settlements.py: query unsettled market endpoints            |
    +------------------------------------------------------------------------+
         |
         v
    +------------------------------------------------------------------------+
    |                     CALIBRATION FEEDBACK LOOP                          |
    |  backtest.py: Brier scores, model quality metrics                     |
    |  calibrate-sigma.py: grid-search optimal sigma parameters             |
    |  daily-backtest.py: drift detection + alerting                        |
    |  --> WRITES calibration.json --> feeds back to SIGNAL GENERATION      |
    +------------------------------------------------------------------------+
```

### Component Boundaries

| Component | Responsibility | Communicates With | State Owned |
|-----------|---------------|-------------------|-------------|
| **Data Ingestion** (per-bot fetchers) | Fetch external data, normalize, validate freshness | Signal Generation (passes data), Health Monitor (freshness timestamps) | `data/market-cache.json`, source-specific caches |
| **Signal Generation** (`probability.py`) | Convert raw data + market prices into probability estimates and edge calculations | Risk & Sizing (passes edge, probability), Calibration Loop (reads `calibration.json`) | `config/calibration.json` (read-only) |
| **Risk & Sizing** (`capital_allocator.py`, Kelly functions) | Portfolio-level capital coordination, position sizing, dedup, concentration limits | Execution (passes approved budget + contract count), all bots (via file-locked shared state) | `data/allocator-state.json` |
| **Execution** (`kalshi_auth.py` TradeManager) | Place orders with safety guardrails, atomic trade logging | Kalshi API (HTTP), Position Management (trade logs read by PM), Reconciliation (trade logs read by reconcile) | `data/*-trades.json`, `data/*-decisions.json` |
| **Position Management** (`position-monitor.py`) | Monitor open positions, execute exits (TP/SL/trailing/model-shift) | Kalshi API (positions, market data, NWS for model-shift), Execution (sell orders), all bot trade logs (entry price lookup) | `data/kalshi-position-trades.json`, `data/position-peaks.json` |
| **Reconciliation** (`reconcile-trades.py`, `backfill-settlements.py`) | Match trades to settlement outcomes, annotate trade records | Kalshi API (settlements, fills, individual markets), all trade log files (read + annotate) | Annotations within `data/*-trades.json` |
| **Calibration Loop** (`calibrate-sigma.py`, `backtest.py`, `daily-backtest.py`) | Evaluate model quality, optimize parameters, detect drift | Trade logs (read settled trades), Signal Generation (writes `calibration.json`) | `config/calibration.json`, `data/backtest-results.json` |
| **Observability** (`supervisor.py`, `dashboard.py`, `audit.py`) | Process management, monitoring, system health | All components (reads health state, trade logs, allocator state, decisions) | `data/health-state.json`, `data/scan-summaries.json` |

### Data Flow

**Primary Trading Loop (per scan cycle):**

1. Bot wakes on scan interval timer
2. Bot fetches external data (weather forecast, NWS actuals, album sales, spot prices, nowcasts)
3. Bot fetches Kalshi markets via `KalshiClient.get_all_markets(prefix=...)`
4. For each relevant market:
   a. Call `probability.py` model function with fetched data + market parameters
   b. Compute edge = model_probability - market_implied_probability
   c. If edge > threshold: call `allocator.request_budget()` for portfolio-level approval
   d. If approved: call Kelly sizing function with approved budget
   e. If contracts > 0: call `trade_manager.place_order()` (all safety checks inside)
   f. Log decision to `*-decisions.json` (trade or skip with reason)
5. Write `ScanSummary` to `scan-summaries.json`
6. Record heartbeat to `health-state.json`

**Calibration Feedback Loop (daily):**

```
reconcile-trades.py          backfill-settlements.py
       |                              |
       v                              v
  Annotated trade logs (settlement_result, realized_edge)
       |
       v
  backtest.py --save
       |
       v
  data/backtest-results.json (Brier scores per bot, per market type)
       |
       v
  daily-backtest.py
       |
       +--> Drift > 10%? --> WhatsApp alert + trigger recalibration
       |
       v
  calibrate-sigma.py --save
       |
       v
  config/calibration.json (updated sigma parameters)
       |
       v
  probability.py lazy-loads on next bot cycle
```

**Position Exit Loop (every 15 min):**

```
position-monitor.py
       |
       +--> Fetch /portfolio/positions
       +--> For each position:
       |       +--> Fetch market data (bid/ask)
       |       +--> Load entry record from all bot trade logs
       |       +--> Evaluate in priority order:
       |       |       1. Take-profit (bid - fee >= 80c, skip if confirmed info-arb)
       |       |       2. Stop-loss (bid <= entry * 0.6, or absolute 30c)
       |       |       3. Trailing stop (dropped 10c from peak, peak was profitable)
       |       |       4. Model-shift (NWS recompute, model prob < 35%)
       |       +--> Execute exit via trade_manager.sell_position()
       |
       +--> Process allocator supersede exits (better signal replaced position)
       +--> Cancel stale resting orders (>12h old or <2h to settlement)
```

## Patterns to Follow

### Pattern 1: Closed-Loop Calibration Pipeline

**What:** Every model parameter should be derived from historical settlement data, not hardcoded. The system should automatically reconcile trades, compute model quality metrics, recalibrate parameters, and detect degradation.

**When:** Always. This is the single most important architectural pattern for a prediction market quant system. Without it, you are flying blind -- every edge estimate is unvalidated theory.

**Why this matters for this system:** `calibration.json` is currently empty. All probability models run on hardcoded defaults (sigma intercept=2.0, slope=0.5). Zero settlement reconciliation has been done. Brier scores are null. The feedback loop exists as code but has never been executed end-to-end.

**Implementation:**

```python
# daily-calibration-pipeline.py (new script, runs daily via cron)
def run_calibration_pipeline():
    """Full closed-loop calibration: reconcile -> backtest -> recalibrate -> validate."""

    # Step 1: Annotate trades with settlement outcomes
    run_reconcile()          # reconcile-trades.py
    run_backfill()           # backfill-settlements.py

    # Step 2: Evaluate current model quality
    previous_brier = load_backtest_results()
    run_backtest()           # backtest.py --save
    current_brier = load_backtest_results()

    # Step 3: Recalibrate if enough settled trades exist
    if settled_trade_count >= MIN_CALIBRATION_TRADES:  # e.g., 50+
        run_calibration()    # calibrate-sigma.py --save

        # Step 4: Validate that recalibration improved (or didn't worsen) Brier
        run_backtest()       # re-run with new calibration
        new_brier = load_backtest_results()

        if new_brier > current_brier * 1.05:  # 5% worse
            rollback_calibration()
            alert("Calibration rollback: new params degraded model")

    # Step 5: Drift detection
    if drift_detected(previous_brier, current_brier):
        alert_whatsapp("Model drift detected: Brier degraded >10%")
```

**Confidence:** HIGH -- this pattern is universal across quantitative trading systems. The existing codebase has all the pieces (`reconcile-trades.py`, `backtest.py`, `calibrate-sigma.py`, `daily-backtest.py`); they just need to be connected into a single automated pipeline.

### Pattern 2: Speed-Tiered Data Ingestion

**What:** Different data sources have different latency characteristics and trading windows. The architecture should tier data sources by speed requirements and optimize the critical path for the highest-edge opportunities.

**When:** For information arbitrage strategies where the edge comes from getting data before the market prices it in.

**Tiers for this system:**

| Tier | Data Source | Latency Target | Trading Window | Current Interval |
|------|------------|----------------|----------------|------------------|
| **T0: Real-time** | NWS actual temps (DSM releases) | < 30 seconds | 5-10 minutes | 10-30 min (too slow) |
| **T1: Near-real-time** | Coinbase/Deribit spot + IV | < 60 seconds | Minutes | 5 min (adequate) |
| **T2: Timely** | HDD chart updates, box office actuals | < 5 minutes | Hours-days | 15 min (adequate) |
| **T3: Periodic** | Weather forecasts (GFS/ECMWF/ICON) | < 30 minutes | Hours-days | 30 min (adequate) |
| **T4: Slow** | Cleveland Fed nowcast, BeatRelease blog | < 1 hour | Days-weeks | 6h / 4h (adequate) |

**Critical path optimization for T0 (NWS speed edge):**

```python
# Instead of polling NWS every 10-30 minutes, poll every 60 seconds
# for cities with active markets near settlement (after 2 PM local)
class SpeedEdgeMonitor:
    def __init__(self):
        self.fast_poll_cities = set()  # cities in "speed mode"

    def should_fast_poll(self, city, hour_local):
        """Fast poll after 2 PM when NWS data is most actionable."""
        return hour_local >= 14 and city_has_active_near_threshold_markets(city)

    def get_poll_interval(self, city, hour_local):
        if self.should_fast_poll(city, hour_local):
            return 60   # 1 minute for speed edge
        return 600       # 10 minutes otherwise
```

**Confidence:** HIGH for the tiering concept. MEDIUM for specific latency targets (depends on actual market maker bot competition, which research suggests operates at sub-second speeds on Kalshi weather markets).

### Pattern 3: Portfolio-Level Risk Coordination via Shared State

**What:** Multiple independent bot processes coordinate risk through a shared state file with advisory file locking, rather than through a central orchestrator process.

**When:** When strategies are independent enough to run as separate processes but share capital and concentration constraints.

**Why this fits:** The current `PortfolioAllocator` with file-backed state and `fcntl` locking is the right pattern for this system. It avoids the complexity of a message broker or central orchestrator while still preventing double-trading and enforcing portfolio limits. This is a pragmatic choice for a single-machine deployment.

**The existing implementation is solid. Key improvements needed:**

1. **Correlation-aware risk budgets**: The `CITY_REGIONS` concept exists but needs expansion. Weather markets in the same region are correlated (a heat wave hits Houston and Austin together). The region-level exposure cap (15%) correctly limits this.

2. **Strategy-type diversification**: The `BOT_PRIORITY` and `MAX_BOT_FRACTION` correctly prevent any single strategy from consuming too much capital. The priority ordering (info-arb > model-based > statistical) reflects edge quality hierarchy.

3. **Signal supersede logic**: The "best signal wins" mechanism (new signal must be 1.5x quality to supersede) prevents flip-flopping while allowing higher-quality signals to take over positions.

**No architectural change needed here -- extend, don't replace.**

**Confidence:** HIGH -- the file-locked shared state pattern is proven for single-machine multi-process trading systems. The existing implementation handles the key concerns (atomicity, daily reset, dedup, concentration).

### Pattern 4: Entry-Aware Position Management

**What:** Exit decisions should reference entry conditions (price, model probability, signal source, strategy type) rather than using absolute thresholds alone.

**When:** Always for position management. The position monitor must understand *why* a position was opened to make correct exit decisions.

**Current implementation gap:** The position monitor loads entry records from all bot trade logs and uses entry-price-relative stops. However, the info-arb gate (skip take-profit for confirmed source-monitor positions with >95% model prob) is a good example of entry-aware exits. This pattern should be extended:

```python
# Exit behavior should vary by entry strategy
EXIT_PROFILES = {
    "info_arb": {
        # High-confidence data-driven entries: hold to settlement
        "skip_take_profit": True,   # already near-certain
        "stop_loss_pct": 0.50,      # wider stop (trust the data)
        "trailing_enabled": False,   # don't trail, hold to settle
        "model_shift_enabled": True, # but exit if data reverses
    },
    "weather_forecast": {
        # Model-based entries: exit before settlement if model shifts
        "skip_take_profit": False,
        "stop_loss_pct": 0.40,
        "trailing_enabled": True,
        "model_shift_enabled": True,
    },
    "longshot_sell": {
        # Statistical bias entries: hold to settlement (edge is at settlement)
        "skip_take_profit": False,   # take profit if available
        "stop_loss_pct": 0.60,       # wider stop (long-tailed)
        "trailing_enabled": False,
        "model_shift_enabled": False, # no model to shift
    },
    "crypto_gbm": {
        # High-vol model: tight exits, fast reaction
        "stop_loss_pct": 0.30,
        "trailing_enabled": True,
        "trailing_drop_cents": 8,
        "model_shift_enabled": True,
    },
}
```

**Confidence:** MEDIUM-HIGH -- entry-aware exits are standard in multi-strategy quant systems. The specific thresholds are LOW confidence (need calibration from actual settlement data).

### Pattern 5: Separation of Data Fetching from Signal Generation

**What:** Keep data ingestion separate from probability computation. A bot should fetch data once, then evaluate multiple markets against that data, rather than fetching data per-market.

**When:** Always. This is already followed in the weather bot (fetch forecasts once, evaluate all KXHIGH markets). Ensure all bots follow this pattern.

**Current gap:** The source monitor fetches NWS data per-city, which is correct. But the position monitor also fetches NWS data for model-shift evaluation, duplicating the fetch. These should share a data layer or cache.

```python
# Shared data cache with TTL (already exists as market-cache.json for Kalshi markets)
# Extend to external data sources:
class DataCache:
    def __init__(self, cache_path, default_ttl=300):
        self.cache_path = cache_path
        self.default_ttl = default_ttl

    def get(self, key, fetcher, ttl=None):
        """Return cached value if fresh, otherwise call fetcher and cache result."""
        cached = self._load(key)
        if cached and not self._is_stale(cached, ttl or self.default_ttl):
            return cached["value"]
        value = fetcher()
        self._save(key, value)
        return value
```

**Confidence:** HIGH -- this is standard software engineering. The existing market cache proves the pattern works.

## Anti-Patterns to Avoid

### Anti-Pattern 1: Calibration-Free Trading

**What:** Running probability models with hardcoded parameters that have never been validated against settlement outcomes.

**Why bad:** The system currently has zero settled Brier scores. Every claimed "edge" (weather 8-15%, crypto 8-80%, entertainment >20%) is unvalidated. The crypto model claims 8-80% edges while signal quality is 3-5% -- a 10-20x disconnect that suggests the model is miscalibrated. Trading on uncalibrated models is equivalent to gambling with a biased estimate of the odds.

**Instead:** Run the calibration pipeline (reconcile -> backtest -> calibrate) before expanding trading volume. Treat all edge estimates as suspect until Brier scores prove model quality. The existing scripts do this -- they just have never been executed.

### Anti-Pattern 2: Polling When You Should Be Pushing

**What:** Using long polling intervals (10-30 minutes) for data sources where the edge window is measured in seconds to minutes.

**Why bad:** NWS DSM releases create 5-10 minute trading windows. A 30-minute poll cycle means you miss most of these windows. Research shows that Kalshi weather market bots operate at sub-second latency, and DSM-reactive bots are common. A 10-minute NWS poll is not competitive.

**Instead:** For the NWS speed edge specifically: poll every 60 seconds during peak hours (2 PM - 8 PM local) for cities with active near-threshold markets. The cost is minimal (NWS API is free, rate limits are generous) and the potential edge capture is significant.

**Important caveat:** Do not attempt sub-second latency -- the system runs on a Mac Mini, not a colocated server. The achievable edge is "faster than other retail bots" (1-2 minute latency), not "faster than institutional HFT" (sub-millisecond). This is still valuable because most prediction market participants are manual traders.

### Anti-Pattern 3: Monolithic Bot That Does Everything

**What:** Combining data ingestion, signal generation, risk management, and execution into a single process.

**Why bad:** Failures cascade (one bad data source crashes the entire system), testing is impossible (can't test signal generation without API calls), and scaling is harder.

**Instead:** The current architecture already avoids this -- each bot is independent. Maintain this separation. The shared modules (`kalshi_auth.py`, `probability.py`, `capital_allocator.py`) are the right abstraction boundary.

### Anti-Pattern 4: Exit-Less Trading

**What:** Opening positions and holding them all to settlement without active management.

**Why bad:** The position monitor has zero exits ever. This means: (1) profitable positions that could have been locked in at 85c+ are held through settlement volatility, (2) losing positions bleed to zero instead of being cut at 40% loss, and (3) capital is trapped in positions that could be recycled into new opportunities.

**Instead:** The position monitor code is well-designed with the right exit hierarchy (take-profit -> stop-loss -> trailing -> model-shift). It just needs to be running and validated. Start it as a supervisor-managed daemon.

### Anti-Pattern 5: Per-Trade Kelly Without Portfolio Kelly

**What:** Sizing each trade independently with Kelly criterion without considering portfolio-level correlations and total exposure.

**Why bad:** A portfolio of 20 half-Kelly positions in correlated weather markets can have unacceptable aggregate risk even though each individual position is "correctly" sized. The current system partially addresses this with `MAX_CITY_FRACTION` (10%) and `MAX_REGION_FRACTION` (15%), but these are heuristic caps, not true portfolio Kelly.

**Instead:** Keep the current heuristic approach (it is practical and sufficient at current scale) but add monitoring of realized portfolio-level metrics (total drawdown, daily P&L variance, Sharpe ratio) to validate that the heuristic caps are producing acceptable aggregate risk.

## Scalability Considerations

| Concern | At Current Scale (5-10 bots) | At 20+ Strategies | At 100+ Markets/Day |
|---------|------------------------------|--------------------|-----------------------|
| **State coordination** | File-locked JSON (fine) | File-locked JSON (still fine) | Consider SQLite for atomic multi-key operations |
| **Data freshness** | Per-bot polling (adequate) | Shared data cache (reduce API calls) | Event-driven with central data service |
| **Calibration** | Daily batch (fine) | Daily batch (still fine) | Online learning (update after each settlement) |
| **Risk tracking** | Per-bot daily limits + allocator | Need correlation matrix | Need real-time portfolio VaR |
| **Execution** | Sequential per-bot (fine) | Parallel execution (already supported via `fetch_parallel`) | Order management system with priority queue |
| **Monitoring** | Dashboard + WhatsApp (fine) | Add Grafana/Prometheus | Automated anomaly detection |

The current system is well-sized for its scale. Single-machine, file-based state is the right choice for <20 strategies and <$500 daily risk budget. Do not over-engineer for scale that does not exist yet.

## Suggested Build Order (Dependencies Between Components)

The improvements integrate into the existing architecture in this dependency order:

### Phase 1: Close the Feedback Loop (blocks everything else)

**Must build first because:** Every other improvement assumes calibrated models. Without settlement reconciliation and Brier scores, you cannot validate whether any change actually improved performance.

Components to activate/fix:
1. Run `reconcile-trades.py` + `backfill-settlements.py` (existing code, never executed)
2. Run `backtest.py` to generate Brier scores (existing code, results currently null)
3. Run `calibrate-sigma.py` to populate `calibration.json` (existing code, output currently empty)
4. Wire into daily cron: `reconcile -> backtest -> calibrate -> drift-alert`

**Dependencies:** None. This is the foundation.
**Changes needed:** Minimal code changes. Primarily operational: run the scripts, verify output, set up cron.

### Phase 2: Enable Position Exits

**Must build second because:** Without working exits, capital is trapped and risk is unmanaged. This is the second-highest-leverage fix after calibration.

Components to activate/fix:
1. Start `position-monitor.py` as a supervisor-managed daemon
2. Verify it reads entry records from all bot trade logs correctly
3. Validate exit thresholds against actual settlement outcomes (from Phase 1 data)

**Dependencies:** Phase 1 (need settled trade data to calibrate exit thresholds).
**Changes needed:** Add to supervisor enabled list. May need to adjust TP/SL thresholds based on Brier analysis.

### Phase 3: Activate Dormant Bots

**Must build third because:** These are existing strategies with zero execution. Fixing them adds trade volume that feeds back into calibration (Phase 1 needs more settled trades).

Components to fix:
1. Entertainment bot (99.7% skip rate -> reduce to reasonable level)
2. Economics bot (zero trades -> fix Cleveland Fed scraper)
3. BeatRelease scanner (zero trades -> fix LLM pipeline)
4. Strategy trader (5 trades ever -> diagnose allocator bottleneck)

**Dependencies:** Phase 1 (need calibration data to set appropriate edge thresholds). Phase 2 (need exits working before increasing trade volume).
**Changes needed:** Per-bot debugging and configuration. No architectural changes.

### Phase 4: Speed Edge Optimization

**Must build fourth because:** Speed edge requires reliable data sources and calibrated models to know *which* data is worth racing on.

Components to build/modify:
1. Reduce NWS polling interval to 60s during peak hours for active cities
2. Add DSM detection (watch for NWS daily summary message releases)
3. Implement adaptive polling (fast when markets are near-threshold, slow otherwise)

**Dependencies:** Phase 1 (need to know which markets have historically settled in ways that reward speed). Phase 3 (source monitor must be working correctly).
**Changes needed:** Modify `source-monitor.py` to support variable polling intervals. Add city-hour-awareness to polling logic.

### Phase 5: New Strategies and Model Improvements

**Must build last because:** Adding new strategies on top of an uncalibrated, exit-less system multiplies the problem. Fix the foundation first.

Components to build:
1. Box office trading bot (framework exists in `probability.py`, bot does not)
2. Cross-platform arb execution (monitoring exists, execution disabled)
3. Crypto model recalibration (claimed edges vs actual signal quality)
4. Market maker activation (Avellaneda-Stoikov exists but disabled)

**Dependencies:** Phase 1-4 all complete. Need proven calibration loop, working exits, and validated edge estimates before expanding.
**Changes needed:** New bot code for box office. Enable flags for arb execution. Recalibrate crypto model parameters.

## Sources

### Architecture Patterns
- [Data Pipeline Design in Algorithmic Trading](https://medium.com/@edwinsalguero/data-pipeline-design-in-an-algorithmic-trading-system-ac0d8109c4b9) -- MEDIUM confidence
- [Quant Trading System Architecture & Infrastructure](https://mbrenndoerfer.com/writing/quant-trading-system-architecture-infrastructure) -- MEDIUM confidence
- [QuantTradingOS modular architecture](https://github.com/QuantTradingOS) -- MEDIUM confidence (open-source reference)
- [NautilusTrader event-driven framework](https://nautilustrader.io/) -- MEDIUM confidence (reference architecture)

### Calibration and Model Validation
- [Calibration and Skill of Kalshi Prediction Markets](https://www.cwdatasolutions.com/post/calibration-and-skill-of-the-kalshi-prediction-markets) -- HIGH confidence (Kalshi-specific)
- [Guide to Quantitative Trading Strategies and Backtesting](https://www.pyquantnews.com/free-python-resources/guide-to-quantitative-trading-strategies-and-backtesting) -- MEDIUM confidence
- [Approaching Human-Level Forecasting with Language Models](https://arxiv.org/html/2402.18563v1) -- MEDIUM confidence (calibration pipeline patterns)

### Speed Edge and Data Pipeline
- [Weather Market Trading Guide (wethr.net)](https://wethr.net/edu/trading-guide) -- HIGH confidence (domain-specific)
- [Navigating Market Bots & Strategies (wethr.net)](https://wethr.net/edu/market-bots) -- HIGH confidence (documents actual Kalshi bot competition)
- [VPS for Prediction Market Bots](https://newyorkcityservers.com/blog/vps-for-prediction-market-bots) -- MEDIUM confidence (latency benchmarks)
- [How AI is helping retail traders exploit prediction market glitches](https://www.coindesk.com/markets/2026/02/21/how-ai-is-helping-retail-traders-exploit-prediction-market-glitches-to-make-easy-money) -- MEDIUM confidence

### Risk Management and Kelly Criterion
- [Risk-Constrained Kelly Criterion](https://blog.quantinsti.com/risk-constrained-kelly-criterion/) -- HIGH confidence (academic + practical)
- [Kelly Criterion in Portfolio Optimization (arxiv)](https://arxiv.org/pdf/1710.00431) -- HIGH confidence (academic)
- [Practical Implementation of Kelly Criterion](https://www.frontiersin.org/journals/applied-mathematics-and-statistics/articles/10.3389/fams.2020.577050/full) -- HIGH confidence (peer-reviewed)

### Kalshi-Specific
- [Kalshi Weather Markets Help Center](https://help.kalshi.com/markets/popular-markets/weather-markets) -- HIGH confidence (official)
- [Kalshi API Documentation](https://docs.kalshi.com/welcome) -- HIGH confidence (official)
- [Building Automated Event Trading Bot with Kalshi](https://jinlow.medium.com/building-an-automated-event-trading-bot-with-kalshi-prediction-markets-a-practical-engineering-a1af3ee619e6) -- MEDIUM confidence
- [Kalshi Trading Bot analysis (alphascope)](https://www.alphascope.app/blog/kalshi-trading-bot-github) -- MEDIUM confidence

### Multi-Strategy Coordination
- [Kelly's Criterion in Portfolio Optimization: A Decoupled Problem](https://arxiv.org/pdf/1710.00431) -- HIGH confidence
- [Money Management via Kelly Criterion (QuantStart)](https://www.quantstart.com/articles/Money-Management-via-the-Kelly-Criterion/) -- MEDIUM confidence
- [Systematic Strategies & Quant Trading 2025 (Gresham)](https://www.greshamllc.com/media/kycp0t30/systematic-report_0525_v1b.pdf) -- MEDIUM confidence

---

*Architecture analysis: 2026-02-26*
