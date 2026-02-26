# Feature Landscape

**Domain:** Automated prediction market (Kalshi) quant trading system
**Researched:** 2026-02-26

## Table Stakes

Features the system *must* have or it will lose money. Missing any of these means the system is running blind, sizing incorrectly, or hemorrhaging capital on broken trades.

### Feedback Loop & Model Validation

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Settlement reconciliation pipeline | Cannot validate any edge without knowing actual outcomes. Currently zero settled trades annotated. | Med | Scripts exist (`reconcile-trades.py`, `backfill-settlements.py`) but have never been run successfully. Must produce `settlement_result` field on every trade record. |
| Brier score computation | The single most important metric for a probability trading system. Null Brier scores = trading on faith. | Low | `backtest.py` exists but produces null scores because no settlement data exists upstream. Dependent on reconciliation. |
| Calibration curve / reliability diagram | Brier score alone is insufficient -- a low Brier score does not guarantee calibration. Need binned predicted-vs-actual plots to detect systematic over/under-confidence. | Med | Does not exist. Must bin model probabilities (0-10%, 10-20%, etc.) and compare to actual win rates. Critical for detecting if weather model is overconfident at 70-80% range vs underconfident at 20-30%. |
| Per-model performance tracking | Must know which bot/model is profitable and which is losing. Aggregate P&L hides broken models. | Med | Decision logs exist (`*-decisions.json`) but no automated per-bot P&L or win rate computation. |
| Automated recalibration pipeline | Models drift. Weather patterns change seasonally. Album sales patterns shift. Without auto-recalibration, edge decays to zero over weeks. | High | `calibrate-sigma.py` exists but `calibration.json` is empty -- has never been run. Need daily `reconcile -> backtest -> recalibrate` loop with drift alerting. |

### Position Sizing & Capital Management

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Correct bankroll basis for Kelly | Kelly criterion requires sizing against *available* capital, not total balance. Currently uses total portfolio balance including locked capital from open positions. | Low | CONCERNS.md flags this as WARN. Positions can be ~2x oversized when >50% capital is locked. Fix: use `available_balance_cents` everywhere. |
| Consistent fee treatment | Three different fee handling patterns across 6 bots. Some use deprecated `edge_after_fees()`, some pass `fee_cents` to Kelly, some ignore fees entirely. | Low | 5-15% variance in position sizing across bots. Standardize on `fee_cents` parameter to Kelly functions. |
| Daily P&L tracking with real numbers | Must know actual daily P&L (not theoretical). Current system has no realized P&L computation -- only trade logs with cost data. | Med | `analyze-performance.py` exists but requires `--reconcile` flag and API calls. Need automated daily P&L that accounts for settlements and fees. |
| Position concentration limits | Must prevent putting all capital into one market or correlated markets (e.g., all MIA weather markets on the same day). | Med | `PortfolioAllocator` exists with `maxConcurrentPositions: 20` but no per-market-type or per-correlation-cluster limits. |

### Risk Controls

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Working kill switch | Emergency halt mechanism. Exists (`data/HALT_TRADING`) and is checked by TradeManager. | Low | Already implemented and functional. TABLE STAKES MET. |
| Circuit breaker (system-level) | Halt trading after consecutive API failures. | Low | Already implemented in `kalshi_auth.py`. TABLE STAKES MET, but only system-wide -- no per-bot granularity. |
| Daily loss limits per bot | Cap losses before they compound. | Low | Already implemented in TradeManager config. TABLE STAKES MET. Max $10-50/day per bot. |
| Max trade amount caps | Prevent single catastrophic trade. | Low | Already implemented. $5-10 per trade. TABLE STAKES MET. |
| Deduplication / cooldown | Prevent re-entering the same market within a scan cycle. | Low | Already implemented via `RecentTradeTracker` with 12h cooldown. TABLE STAKES MET. |
| Balance check before order | Prevent orders that exceed available balance. | Low | Already implemented in TradeManager. TABLE STAKES MET. |

### Position Management / Exits

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Working take-profit exits | Lock in gains when bid reaches threshold. Holding every position to settlement leaves money on the table and exposes to reversal risk. | Med | Position monitor exists but has ZERO exits ever. Must be debugged and validated. Configured at 80% threshold. |
| Working stop-loss exits | Cut losses on positions that have moved against you. | Med | Position monitor exists with 30% threshold but zero executions. Same debugging needed. |
| Model-shift exits | Exit when updated model probability disagrees with original entry thesis by >20%. Most important exit type for an edge-based system. | Med | Implemented in position-monitor.py but untested. Requires re-running probability model with current data. |
| Trailing stop logic | Lock in gains dynamically as position value increases. Better than fixed take-profit for trending markets. | Med | Implemented with `trailingDropCents: 10` config but zero executions. Peak tracking state file exists. |
| Stale order cancellation | Cancel resting orders that won't fill before settlement. | Low | Implemented with `orderTtlMinutes: 120`. Needs validation. |

### Data Pipeline Reliability

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Data source health monitoring | Must detect when external data sources go down (NWS, Open-Meteo, HDD, Coinbase, Cleveland Fed). Trading on stale data is worse than not trading. | Med | `HealthCheckMonitor` exists and writes to `health-state.json`. Source freshness tracking exists but is not rigorously enforced -- some sources can fail silently. |
| Stale data rejection | Refuse to trade if data is older than a threshold. | Med | TradeManager has stale data check but implementation varies by bot. Weather bot checks forecast freshness; entertainment bot checks `hours_since_publication`. Needs standardization. |
| Data source fallback | When primary source fails, fall back to secondary. | High | NO fallback sources exist. If HDD goes down, entertainment stops entirely. If Open-Meteo fails, weather stops. Single points of failure throughout. |
| Atomic file writes | Prevent corrupted state files from crashed writes. | Low | Already implemented via `_atomic_write_json()`. TABLE STAKES MET. |

### Observability

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Decision logging | Log every market evaluated with trade/skip reason. Essential for debugging "why didn't it trade?" | Low | Already implemented via `ScanSummary`. TABLE STAKES MET. |
| Structured trade records | Every trade must record: ticker, side, price, contracts, edge, model_prob, market_prob, sigma, source, timestamp, fees. | Low | Already implemented. Trade logs contain rich metadata. TABLE STAKES MET. |
| WhatsApp/webhook alerting | Notify on trades, errors, and daily summaries. | Low | Already implemented. TABLE STAKES MET. |
| Dashboard with real-time status | Visual monitoring of bot status, P&L, positions. | Low | FastAPI dashboard exists at port 3456. TABLE STAKES MET. |

## Differentiators

Features that provide competitive advantage over other Kalshi participants. Not expected by default, but create edge.

### Speed Edge (Primary Alpha Thesis)

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| NWS actual temperature arbitrage | Get NWS running high temperature data before market prices it in. Weather markets settle on NWS data but prices often lag actual observations. Info window can be minutes to hours. | Med | Source monitor exists and implements this. NWS sigma model with continuous exponential decay. Has made trades but unvalidated edge. This is the system's most proven alpha source. |
| Multi-source weather ensemble | Combine GFS + ECMWF + ICON forecasts via Bayesian Model Averaging. Better probability estimates than any single model. | Med | Already implemented with horizon-dependent weights. Differentiator because most retail traders use single forecast source. Needs calibration validation. |
| Album sales info arbitrage | Get HITS Daily Double chart data before Kalshi album sales markets reflect it. HDD publishes estimates mid-week; settlement is end-of-week. | Med | Entertainment bot + HDD parser exist but 99.7% skip rate, 2 trades ever. Source-aware sigma model is sophisticated (different uncertainty for chart data vs article data vs midweek estimates). |
| Sub-minute data polling | Poll NWS/Open-Meteo more frequently than competitors. Most physics models run 4x/day; higher-frequency observation data provides edge between model runs. | Low | Weather bot polls every 30 min; source monitor every 10-30 min. Could be reduced to 5 min for NWS observations during peak hours (10am-4pm local). |
| LLM-powered news parsing | Use DeepSeek to extract trading signals from BeatRelease blog posts and music industry news. Faster than manual reading. | Med | Beatrelease scanner exists but zero executed trades. LLM parsing is regex-based (fragile). Needs strict JSON schema + validation. |

### Model Sophistication

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Student-t fat tails for weather | Using df=6 Student-t CDF instead of Gaussian captures real forecast error distribution. 3x heavier tails at 3-sigma vs Gaussian. | Low | Already implemented in `probability.py`. Most retail traders use simple point estimates or Gaussian assumptions. |
| Per-city sigma calibration | Different cities have different forecast error distributions. MIA (coastal, humidity) vs DEN (altitude, microclimate) require different sigma parameters. | Med | Framework exists in `calibration.json` and `weather_probability()`. But calibration.json is EMPTY -- never calibrated. Must actually run calibration to realize this advantage. |
| Ornstein-Uhlenbeck mean reversion for crypto | Crypto prices mean-revert on short timeframes. OU model dampens GBM volatility for sub-3-hour horizons, producing more realistic probabilities. | Low | Already implemented with smooth blend (full OU below 180min, linear taper to 300min). Needs validation against realized vol. |
| Category-adjusted longshot bias | Different market categories (sports, entertainment, politics) have different longshot bias magnitudes. Sports has strongest bias (65% amplitude), weather weakest (30%). | Low | Already implemented with `LONGSHOT_BIAS_PARAMS` and `classify_ticker_category()`. Strategy trader has only 5 trades ever -- needs scaling. |
| Time-decay sigma models | Data uncertainty grows with age. Album data staleness increases sigma by 50% per 48h, capped at 3x. NWS sigma decays exponentially through the day. | Low | Already implemented for both album and NWS. Sophisticated feature that most competitors lack. |
| Economics nowcast with sigma step-down | CPI nowcast uncertainty decreases as release date approaches (0.10% at 14d to 0.03% at release). Edge is largest 1-3 days before release. | Low | Implemented but economics bot has zero trades. Cleveland Fed scraper may be broken. |

### Execution Quality

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Edge-adaptive limit pricing | Adjust limit price aggressiveness based on edge strength: high edge = pay full ask; low edge = midpoint. Balances fill rate vs execution cost. | Low | Already implemented in `compute_limit_price()` with three tiers. |
| Liquidity-aware trade filtering | Skip markets with wide spreads or low volume. Prevents adverse selection in thin markets. | Low | Already implemented via `is_market_liquid()` with configurable thresholds. |
| Parallel API fetching | Fetch multiple data sources concurrently to reduce scan latency. | Low | Already implemented via `fetch_parallel()`. |

### Operational Edge

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Cross-platform arbitrage monitoring | Detect price discrepancies between Kalshi and Polymarket. 1.5-4.5% per-pair after fees on high-volume events. | Med | Monitoring exists but execution is disabled. Polymarket client implemented. Phase 1 = monitoring only. |
| Automated daily report with drift detection | Daily backtest comparison flags model degradation >10%. Catches model drift before it costs real money. | Low | `daily-backtest.py` exists with WhatsApp alerting. Never been run in production. |
| Process supervisor with crash recovery | Auto-restart bots after crashes. Crash rate limiting (5 in 10 min = disable). Heartbeat staleness detection. | Med | Already implemented. TABLE STAKES infrastructure that doubles as differentiator through reliability. |

## Anti-Features

Features to explicitly NOT build. Either because they're traps, they're premature, or they distract from core profitability.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| High-frequency trading infrastructure | Kalshi API caches market snapshots every 5 seconds. REST rate limits make true HFT impractical. Building sub-second infrastructure is wasted effort. | Focus on medium-frequency (5-30 min) event-driven strategies where information edge matters more than speed edge. |
| Custom frontend / mobile app | Dashboard already works. Building pretty UIs does not generate alpha. | Keep existing FastAPI dashboard. Invest time in model validation instead. |
| Full market making at scale | Avellaneda-Stoikov bot exists but disabled. Market making requires significant capital ($5K-25K minimum), deep understanding of inventory risk, and constant monitoring. Premature before core models are validated. | Keep disabled until core directional bots are profitable. Revisit only after Brier scores prove model calibration. |
| Websocket streaming | REST polling at 5-30 minute intervals is sufficient for event-driven markets. Websocket adds complexity without proportional edge for current strategies. Kalshi's FIX protocol exists but is overkill for $5-10 trades. | Use REST polling. Consider websocket only if latency becomes provably costly (measure first). |
| Machine learning / deep learning models | ML models require large training datasets. With zero settlement reconciliation, there is no labeled data to train on. ML before data = overfitting to noise. | Fix feedback loop first. Accumulate 6+ months of labeled settlement data. Only then consider ML for edge detection. |
| Multi-exchange execution beyond Kalshi + Polymarket | Adding more exchanges (ForecastEx, PredictIt successors) fragments attention. Focus on depth over breadth. | Master Kalshi profitability first. Polymarket read-only for arb monitoring is sufficient. |
| Automated parameter optimization without guardrails | Auto-tuning edge thresholds, Kelly fractions, or scan intervals without human review can optimize toward noise. One bad auto-tune can blow up the account. | Always require human sign-off on calibration changes. Auto-suggest, never auto-apply to production. |
| Tax optimization / reporting automation | Important eventually but zero priority when the system isn't generating consistent P&L. | Defer until system is profitable for 3+ months. Keep trade logs clean for future tax prep. |
| Social / copy trading features | Building social features or trade sharing is a product play, not a trading edge play. | Focus entirely on alpha generation. |

## Feature Dependencies

```
Settlement Reconciliation
  |
  v
Brier Score Computation
  |
  +---> Calibration Curve / Reliability Diagram
  |
  +---> Per-Model Performance Tracking
  |        |
  |        v
  |     Identify which bots to fix/disable
  |
  v
Automated Recalibration Pipeline
  |
  v
Per-City Sigma Calibration (weather)
  |
  v
Validated weather probability model
  |
  v
Confident position sizing (Kelly is only as good as edge estimate)

---

Correct Bankroll Basis (available balance)
  +---> All Kelly sizing functions
  +---> Capital allocator state

---

Working Take-Profit / Stop-Loss / Model-Shift Exits
  +---> Requires position monitor to actually execute
  +---> Requires entry records from all bot trade logs
  +---> Requires current model probability for model-shift

---

Data Source Health Monitoring
  +---> Stale Data Rejection
  +---> Data Source Fallback (if primary fails)

---

NWS Actual Temp Arbitrage  (independent, can run now)
Album Sales Info Arbitrage  (requires HDD source to be working)
Economics Nowcast           (requires Cleveland Fed scraper fix)
Crypto GBM                  (requires model validation first)
Longshot Bias Selling       (requires Becker model validation)
```

## MVP Recommendation

The system has extensive infrastructure but near-zero validated trading. Priority must be establishing the feedback loop before expanding strategies.

### Phase 1: Establish Feedback Loop (Blocks Everything Else)

Prioritize:
1. **Settlement reconciliation pipeline** -- Annotate all trade logs with actual outcomes. This is prerequisite zero.
2. **Brier score computation** -- Generate actual calibration metrics. Know which models work.
3. **Calibration curve generation** -- Binned predicted-vs-actual analysis. Detect systematic biases.
4. **Fix bankroll basis** -- Use available balance, not total balance, for Kelly sizing. Quick win that prevents oversizing.
5. **Standardize fee treatment** -- One pattern across all bots. 30-minute fix that removes 5-15% sizing variance.

### Phase 2: Get Bots Actually Trading

Prioritize:
1. **Debug and fix position monitor exits** -- Zero exits ever is a critical gap. Capital is locked until settlement.
2. **Run per-city sigma calibration** -- calibration.json is empty. Fill it. This unlocks the weather ensemble advantage.
3. **Fix entertainment bot skip rate** -- 99.7% skip rate means the liquidity/spread filters are too tight or data pipeline is broken.
4. **Fix economics bot scraper** -- Cleveland Fed data is a proven edge. Zero trades means scraper is broken.
5. **Scale longshot bias selling** -- 5 trades ever on a strategy with documented systematic edge. Debug allocator rejection rate.

### Phase 3: Validate and Expand

1. **Automated daily recalibration** -- reconcile -> backtest -> recalibrate loop with drift alerting.
2. **Per-model P&L tracking** -- Know which bot is making money and which is losing.
3. **Enable cross-platform arb execution** -- Start with $1-2 positions once monitoring validates.
4. **Crypto model validation** -- Claimed 8-80% edges vs 3-5% signal quality is a red flag.
5. **Build box office trading bot** -- Framework exists in probability.py, bot does not.

Defer:
- **Market making** -- Until core bots are validated profitable (3+ months)
- **ML/DL models** -- Until 6+ months of labeled settlement data exists
- **Multi-exchange execution** -- Until Kalshi profitability is proven
- **Websocket streaming** -- Until latency is measured and proven costly

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Table stakes (feedback loop) | HIGH | Universal quant trading requirement. Well-documented in literature. Codebase analysis confirms zero settlement reconciliation. |
| Table stakes (risk controls) | HIGH | Already implemented. Verified in codebase. Multiple safety layers present. |
| Table stakes (exits) | HIGH | Position monitor code exists but is non-functional. Zero exits confirmed from PROJECT.md. |
| Differentiators (speed edge) | MEDIUM | Speed edge is real but unquantified. NWS info window is documented but edge magnitude unknown without Brier scores. Kalshi API 5-second cache limits true speed advantage. |
| Differentiators (model sophistication) | MEDIUM | Models are theoretically sound (Student-t, ensemble, OU) but entirely unvalidated. Could be producing garbage probabilities. Confidence upgrades to HIGH only after calibration proves them. |
| Anti-features | HIGH | Well-established quant trading wisdom: fix measurement before optimization, avoid premature complexity, master one venue before expanding. |

## Sources

- [QuantPedia: Systematic Edges in Prediction Markets](https://quantpedia.com/systematic-edges-in-prediction-markets/) -- longshot bias, behavioral edges
- [CoinDesk: AI Exploiting Prediction Market Glitches (Feb 2026)](https://www.coindesk.com/markets/2026/02/21/how-ai-is-helping-retail-traders-exploit-prediction-market-glitches-to-make-easy-money) -- $150K bot performance, 8,894 trades
- [QuantVPS: Kalshi VPS Low Latency](https://www.quantvps.com/kalshi-vps) -- API latency data, 5-second cache
- [NYC Servers: VPS for Prediction Market Bots 2026](https://newyorkcityservers.com/blog/vps-for-prediction-market-bots) -- infrastructure requirements
- [Unchained: 6 Ways to Make Money in Prediction Markets 2026](https://unchainedcrypto.com/6-easy-ways-to-make-money-in-prediction-markets-in-2026/) -- strategy overview
- [wethr.net: Weather Market Trading Guide](https://wethr.net/edu/trading-guide) -- NWS settlement, data sources
- [Kalshi Help: Weather Markets](https://help.kalshi.com/markets/popular-markets/weather-markets) -- settlement rules, NWS data
- [QuantInsti: Risk-Constrained Kelly Criterion](https://blog.quantinsti.com/risk-constrained-kelly-criterion/) -- practical Kelly implementation
- [Alpha Theory: Kelly Criterion in Practice](https://www.alphatheory.com/blog/kelly-criterion-in-practice-1) -- half-Kelly rationale
- [QuantInsti: Autoregressive Drift Detection](https://blog.quantinsti.com/autoregressive-drift-detection-method/) -- model drift detection
- [Statsig: Model Drift Detection Methods](https://www.statsig.com/perspectives/model-drift-detection-methods-metrics) -- drift monitoring practices
- [Potent Pages: Data Pipeline SLAs for Alternative Data](https://potentpages.com/web-crawler-development/web-crawlers-and-hedge-funds/monitoring-alerting-for-data-pipelines-slas-for-alternative-data) -- data reliability monitoring
- [ICLR Blogposts 2025: Understanding Model Calibration](https://iclr-blogposts.github.io/2025/blog/calibration/) -- calibration curves, ECE
- [Arize: Calibration Curves](https://arize.com/blog-course/what-is-calibration-reliability-curve/) -- reliability diagrams
- [LuxAlgo: Risk Management for Algo Trading](https://www.luxalgo.com/blog/risk-management-strategies-for-algo-trading/) -- kill switch, circuit breaker best practices
- [PMC: Systemic Failures in Algorithmic Trading](https://pmc.ncbi.nlm.nih.gov/articles/PMC8978471/) -- operational reliability
- Codebase analysis: `probability.py`, `kalshi_auth.py`, `position-monitor.py`, `weather-bot.py`, `config/bots-config.json`
- Project files: `.planning/PROJECT.md`, `.planning/codebase/CONCERNS.md`
