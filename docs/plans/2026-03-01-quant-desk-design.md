# Quant Desk Simulation Infrastructure — Design Document

> **DEPRECATED**: This document is superseded by [Consolidated Quant Desk Design](2026-03-02-consolidated-quant-desk-design.md). Kept for historical reference only.

**Date**: 2026-03-01
**Goal**: Build institutional-grade simulation, correlation modeling, and competitive intelligence infrastructure to maximize profits while minimizing portfolio risk — designed to compete with quant desks as institutional trading arrives on Kalshi.
**Bankroll**: $5,000+ live
**Approach**: Balanced, profit-first sequencing across 5 phases (8-10 weeks)

## Context

The article "How to Simulate Like a Quant Desk" (gemchange_ltd, Feb 2026) outlines the full simulation stack used by institutional prediction market traders: Monte Carlo, importance sampling, particle filters, copula dependency modeling, agent-based simulation, and production monitoring.

Current system state:
- 8 bots running via supervisor on Mac Mini, all enabled
- Validated models: crypto Brier 0.037, weather ensemble with per-city calibration, economics nowcast
- Infrastructure: capital allocator, circuit breaker, health monitoring, dashboard, automated backtesting
- Pain point: too many market skips — bots evaluate hundreds of markets but trade very few
- $11.61 deployed against $5K+ bankroll (operating at ~10-20% capacity)

### What We Already Have (vs. Article)

| Article Topic | Our Status |
|---|---|
| GBM for binary contracts | Have it — closed-form CDF (more efficient than MC sampling) |
| Brier score calibration | Have it — operational with automated drift alerts |
| Kelly sizing | Have it — 4 variants (half, quarter, high-conviction, sell-side), fee-aware |
| Fat-tail distributions | Have it — Student-t CDF with df=6 via regularized beta |
| Ensemble averaging | Have it — BMA across GFS/ECMWF/ICON with horizon weights |

### Gaps to Fill

| Gap | Priority | Phase |
|---|---|---|
| Too many skips / conservative limits | Highest (immediate P&L) | 1 |
| Particle filter (dynamic belief updating) | High (smoother edge, fewer false skips) | 2 |
| Correlation / tail dependence modeling | High (portfolio risk at $5K+ scale) | 3 |
| Importance sampling for tail-risk contracts | Medium (proper tail pricing) | 4 |
| Regime detection (vol regime switching) | Medium (adaptive model behavior) | 4 |
| Jump-diffusion model for crypto | Medium (structural edge on tail contracts) | 4 |
| Agent-based order book simulation | Medium (market maker activation) | 5 |
| P&L attribution | Medium (know what generates alpha) | 5 |
| Competitive edge monitoring | High long-term (detect when edge decays) | 5 |

---

## Phase 1: Unblock the Flow (Week 1-2)

### 1.1 Skip Audit Tool

New script: `scripts/skip-audit.py`

Reads all `*-decisions.json` files and produces a comprehensive report:
- Skip reason distribution per bot (illiquid, low_edge, low_confidence, kelly_zero, allocator_denied, no_match, threshold_parse_fail)
- For each skip reason: what would the trade have been? What did the market settle at? Was the skip correct (avoided a loss) or incorrect (missed a profit)?
- Output: ranked list of "money left on the table" by skip category

Becomes permanent infrastructure — run weekly to detect when filters are too tight or too loose.

Testing: integration test with fixture decision logs covering all skip reasons.

### 1.2 Bankroll-Proportional Risk Limits

Scale limits for $5K+ bankroll. Not a simple 5x multiplier — scale intelligently based on model quality and edge type.

| Parameter | Current | Proposed ($5K) | Rationale |
|---|---|---|---|
| Weather maxTradeAmount | $15 | $30 | 2x — model-dependent edge |
| Crypto maxTradeAmount | $10 | $25 | 2.5x — validated Brier 0.037 |
| Economics maxTradeAmount | $25 | $50 | 2x — nowcast edge is high quality |
| Entertainment maxTradeAmount | $5 | $15 | 3x — info-arb edge is data-driven |
| Strategy maxTradeAmount | $5 | $10 | 2x — statistical edge, keep conservative |
| Portfolio daily loss cap | $150 | $500 | 10% of bankroll (was 15% of $1K) |
| Per-bot daily loss | $25-50 | $50-150 | Scale proportionally |
| MAX_TICKER_FRACTION | 5% | 3% | Tighter at scale — $5K * 5% = $250 is too much per position |
| MAX_CITY_FRACTION | 10% | 7% | Tighter correlation risk at scale |
| MAX_BOT_FRACTION | 40% | 30% | Better cross-strategy diversification |

Config changes only in `config/bots-config.json` — no code changes for limits.

### 1.3 Edge Threshold Recalibration

Validate edge thresholds against Brier scores:
- Brier < 0.10 (excellent): edge threshold can drop from 8% to 6%
- Brier 0.10-0.20 (good): keep current thresholds
- Brier > 0.20: raise thresholds

Extend `scripts/calibrate.py` grid-search to sweep edge thresholds alongside sigma parameters.

### 1.4 Crypto Bracket Recovery

Currently disabled (87% non-fill rate). Fix:
- Use midpoint pricing for illiquid brackets (patient fill)
- Accept 30-50% fill rates (still profitable if edge is real)
- Add fill-rate tracking to decision logs
- Gate: only enable brackets where spread < 15c AND volume > 10

### 1.5 Cross-Platform Arb Execution

Enable Kalshi-side execution (Phase 2 of arb roadmap):
- Buy Kalshi YES when Polymarket price > Kalshi price + fees
- Buy Kalshi NO when Polymarket price < Kalshi price - fees
- No Polymarket-side execution (requires Polygon wallet)
- Minimum spread after fees: 3% (conservative)
- Quarter-Kelly sizing (high model uncertainty — trusting Polymarket as price oracle)

### 1.6 Testing Strategy

- Skip audit tool: integration test with fixture decision logs
- Risk limits: config-only change, validate via dry-run scan cycles
- Edge thresholds: extend `tests/test_calibration.py` with threshold sweep tests
- Crypto brackets: add bracket limit pricing tests to `tests/test_crypto.py`
- Cross-platform arb: add spread calc, fee deduction, edge-gating tests to `tests/test_arb.py`

### 1.7 Rollout

- Deploy config changes to Mac Mini
- Run skip audit before and after to measure impact
- Monitor first 48 hours: trade volume increase? Win rate hold?
- Kill switch (`data/HALT_TRADING`) remains safety net

---

## Phase 2: Particle Filter Engine (Week 3-4)

### 2.1 Core Concept

Replace stateless per-scan probability snapshots with a Bayesian belief state that carries memory across scans. Each scan becomes an observation that updates the belief, not a fresh computation.

Benefits:
- Smoother edge estimates → fewer false skips from noisy oscillation above/below threshold
- Credible intervals → uncertainty-aware sizing (narrow CI = normal Kelly, wide CI = reduced Kelly)
- Trend detection → enter positions earlier when probability is trending in our direction
- Temporal smoothing → less reactive to transient market noise

### 2.2 Architecture

New shared module: `src/kalshi/particle_filter.py`

```
PredictionMarketParticleFilter
    __init__(N_particles, prior_prob, process_vol, obs_noise)
    update(observed_prob, observation_quality)  -> filtered_prob
    estimate()                                  -> weighted mean probability
    credible_interval(alpha)                    -> (lower, upper)
    trend()                                     -> slope over last N updates
    confidence_width()                          -> CI width (uncertainty)
    serialize() / deserialize()                 -> JSON persistence
    reset()                                     -> reinitialize particles
```

**State space model:**
- Hidden state x_t: true probability of the event (unobserved)
- Observation y_t: our model's probability estimate from the current scan
- State evolution: logit random walk — logit(x_t) = logit(x_{t-1}) + N(0, process_vol)
- Observation model: y_t = x_t + N(0, obs_noise / quality)
- Resampling: systematic resampling when ESS < N/2

**Persistence:** State written to `data/pf-state-{bot}.json` between scan cycles. Staleness check on load — reinitialize if age > 2x scan interval.

### 2.3 Per-Bot Configuration

**Crypto bot (highest value — 5-min scans):**
- One filter per active ticker
- process_vol = 0.02 (crypto prices move fast)
- obs_noise = 0.03 (GBM ~3% standard error at short horizons)
- observation_quality scales with IV confidence (both IV + RV available = high, default vol = low)
- Edge calculation: `edge = filtered_prob - market_price` (replaces raw model_prob)
- New skip reason: `"edge_unstable"` when CI width > 0.15
- New trade signal: `"trend_confirmed"` when trend() shows 3+ consecutive same-direction updates AND filtered edge > threshold

**Weather bot (high value — 30-min scans):**
- One filter per active ticker
- process_vol = 0.01 (weather forecasts evolve slowly)
- obs_noise scales with weather_sigma(days_out)
- observation_quality scales with ensemble agreement (GFS/ECMWF/ICON convergence = high quality)
- Ensemble integration: filter observes ensemble probability, not individual models

**Economics bot (medium value — 6-hour scans):**
- One filter per ticker
- process_vol = 0.005 (nowcasts update slowly)
- obs_noise = cpi_nowcast_sigma(days_to_release)
- Smooths step-function sigma transitions into continuous uncertainty decay

**Entertainment bot (lower value — 15-min scans):**
- One filter per matched artist/movie
- process_vol = 0.005 (album data updates weekly)
- Properly weights new HDD data against prior belief instead of jumping to new estimate

**Not integrated (initially):** source-monitor (fires on confirmed data), strategy (one-shot), beatrelease (LLM-based)

### 2.4 Competitive Advantages

1. **Temporal smoothing**: We see probability trending up for 30 minutes. Competitors using same GBM see a snapshot. We enter earlier with higher confidence.
2. **Uncertainty-aware sizing**: CI width feeds into Kelly sizing. Narrow CI → normal sizing. Wide CI → reduced sizing. Strictly better than binary trade/skip at fixed threshold.
3. **Regime detection feed-forward**: When process_vol drifts upward (particles spread), signals regime change — feeds Phase 4 regime detector.

### 2.5 Testing Strategy

- Unit: convergence to true probability, proper resampling, serialization round-trip, stale state detection
- Property-based: filter estimate converges to observation mean over many updates; CI narrows with more observations
- Integration: mock crypto scan cycle with 10 sequential price observations, verify filtered probability is smoother than raw
- Regression: replay historical crypto-bot decisions with/without filter, compare Brier scores
- Edge cases: particle degeneracy, single-value collapse, NaN/inf observations

### 2.6 Failure Modes

- **Particle degeneracy**: all particles collapse to same value. Mitigation: systematic resampling + jitter after resample
- **Stale state**: bot restart loads old state. Mitigation: staleness check, reinitialize if age > 2x scan interval
- **Model mismatch**: process_vol miscalibrated (too low = sluggish, too high = reactive). Mitigation: adaptive process_vol from prediction residuals (Phase 4)
- **Memory leak**: filters accumulate for expired tickers. Mitigation: prune filters not observed in 24 hours

---

## Phase 3: Correlation & Dependency Layer (Week 5-6)

### 3.1 Problem Statement

At $5K+ with all bots running, the portfolio accumulates correlated positions:
- BTC YES + ETH YES + SOL YES during crypto rally (60-80% correlated)
- Houston T85 + Dallas T86 + Austin T84 during Texas heat wave (70%+ correlated)
- CPI above 3% + Jobs below 200K (macro regime correlation)

Current `capital_allocator.py` uses crude position limits (5% per ticker, 10% per city) but doesn't model how positions interact. $250 BTC + $250 ETH is not $500 independent risk — it's ~$400 normal risk but ~$490 in a crash (tail dependence).

### 3.2 Architecture

New shared module: `src/kalshi/correlation_engine.py`

```
CorrelationEngine
    __init__(lookback_days, min_observations)
    update_returns(asset, timestamp, return_value)
    pairwise_correlation(asset_a, asset_b)       -> float
    correlation_matrix(assets)                    -> np.ndarray
    tail_dependence(asset_a, asset_b, quantile)   -> float
    portfolio_var(positions, confidence)           -> float (Value at Risk)
    portfolio_expected_shortfall(positions, conf)  -> float
    concentration_risk(positions)                  -> dict per cluster
    asset_clusters()                              -> list of correlated groups
    serialize() / deserialize()                    -> JSON persistence
```

### 3.3 Three Layers of Dependency Modeling

**Layer 1: Empirical pairwise correlation (Week 5)**
- Track daily returns for each asset class: crypto prices, weather outcomes, economic indicators
- Rolling 30-day correlation matrix
- Stored in `data/correlation-state.json`
- Feeds into capital allocator: replace fixed MAX_CITY_FRACTION with dynamic correlation-adjusted limits

**Layer 2: Student-t copula for tail dependence (Week 5-6)**
- Fit t-copula parameters (df, correlation matrix) from empirical data
- Key output: tail dependence coefficient lambda — probability of extreme co-movement
- Stress testing: "if BTC drops 15%, what's P(ETH also drops 15%)?"
- Feeds into portfolio VaR calculation

**Layer 3: Cluster-based concentration limits (Week 6)**
- Hierarchical clustering of assets by correlation
- Clusters: {BTC, ETH, SOL, DOGE, XRP}, {weather cities by region}, {CPI, GDP, Jobs}
- Per-cluster capital limit replaces per-asset limit
- Dynamic: cluster membership and limits update daily based on rolling correlation

### 3.4 Integration with Capital Allocator

`PortfolioAllocator.request_budget()` currently checks:
1. Global dedup
2. Per-bot daily spend
3. Per-ticker concentration (5%)
4. Per-city concentration (10%)
5. Portfolio daily loss cap

With correlation engine, adds:
6. **Cluster concentration**: total exposure to correlated cluster < cluster_limit
7. **Marginal VaR**: adding this position increases portfolio VaR by how much? If marginal VaR exceeds threshold, deny or reduce allocation
8. **Tail risk check**: if t-copula tail dependence for cluster > 0.15, reduce Kelly fraction by 25%

### 3.5 Data Sources

- **Crypto**: Coinbase spot prices (already collected every 5 min in crypto-price-history.json). Compute daily log returns.
- **Weather**: Historical NWS actuals per city. Open-Meteo historical API (free, no auth).
- **Economics**: BLS historical releases. Hardcoded correlation priors from FRED (CPI-Jobs well-studied).
- **Cross-asset**: Crypto-weather ~0 (independent). Crypto-economics weak. Start with zero, let data update.

### 3.6 Scipy Dependency

t-copula fitting needs scipy.stats.t and scipy.optimize. This is the first scipy dependency.
Options: add to requirements.txt OR implement t-copula CDF using existing _student_t_cdf in probability.py (feasible — regularized beta machinery already exists).
Decision: add scipy to requirements.txt — it's the industry standard, fighting it creates tech debt.

### 3.7 Testing Strategy

- Unit: correlation matrix is positive semi-definite, tail dependence bounded [0,1], VaR is monotone in confidence
- Synthetic data: generate correlated returns from known distribution, verify engine recovers true correlation within CI
- Integration: mock portfolio with 5 correlated crypto positions, verify cluster limit triggers before individual limits
- Stress: replay 2022 crypto crash (BTC -65%, ETH -68%, SOL -95%) through engine, verify VaR breach
- Regression: run allocator with/without correlation engine on historical allocator-state.json

### 3.8 Failure Modes

- **Insufficient data**: 30-day window needs 30 days. Until then, use hardcoded priors (crypto: 0.7, same-region weather: 0.5, cross-asset: 0.0)
- **Singular matrix**: two assets with identical returns. Mitigation: Ledoit-Wolf shrinkage toward identity
- **Stale correlation**: regime shifts (ETH decorrelates from BTC). Mitigation: exponentially weighted correlation with 7-day half-life
- **False precision**: small sample sizes produce noisy correlation estimates. Mitigation: min_observations threshold (default 20) below which use priors

---

## Phase 4: Advanced Simulation Engine (Week 7-8)

### 4.1 Component 1: Importance Sampling for Tail-Risk Contracts

New addition to `src/kalshi/simulation.py`:

```
ImportanceSampler
    __init__(model_type, N_paths)
    estimate_tail_probability(params, threshold, direction) -> (prob, std_error, ci)
    optimal_tilt(params, threshold)                        -> tilt_parameter
    variance_reduction_factor()                            -> float
    diagnostics()                                          -> dict (ESS, tilt_param)
```

**When to use IS vs closed-form:**
- |d2| < 3 (threshold within 3 sigma): use closed-form CDF (fast, accurate)
- |d2| >= 3 (tail event): switch to importance sampling (CDF numerically unreliable in deep tails)

**Exponential tilting method:**
1. Shift GBM drift so crash/spike threshold is typical under tilted measure: mu_tilt = log(K/S0) / T
2. Simulate 100K paths under tilted measure — most reach the threshold
3. Correct with likelihood ratio (original density / tilted density)
4. Corrected mean = unbiased tail probability estimate with proper standard error

**Integration with crypto bot:**
- Wrapper: `crypto_probability_robust()` checks |d2|, dispatches to CDF or IS
- IS results cached per (asset, threshold, direction, time_horizon) with 60s TTL
- Decision logs gain: simulation_method, is_std_error, is_variance_reduction

### 4.2 Component 2: Variance Reduction Stack

Three techniques that stack multiplicatively in `simulation.py`:

**Antithetic Variates:**
For every path Z, also simulate -Z. Average of f(Z) and f(-Z) has lower variance (negative correlation). Zero extra cost beyond doubling evaluations. Typical reduction: 50-75%.

**Control Variates:**
Use closed-form Black-Scholes digital price as control:
`p_adjusted = p_sim - beta * (p_BS_sim - p_BS_exact)`
We have the closed form (crypto_price_probability). Typical reduction: 80-95%.

**Stratified Sampling:**
Partition random number space into J strata (quantiles of terminal distribution), sample within each. Neyman allocation: oversample high-variance strata. Typical reduction: 60-90%.

**Stacking**: Antithetic inside each stratum + control variate correction. Combined: 100-500x reduction. 1,000 IS paths with full stack = precision of 500,000 crude paths.

**When invoked**: Always-on when simulation is active. No configuration needed.

### 4.3 Component 3: Regime Detection

New module: `src/kalshi/regime_detector.py`

```
RegimeDetector
    __init__(assets, lookback_days, n_regimes)
    update(asset, timestamp, price, vol)
    current_regime(asset)              -> str ("low_vol", "normal", "high_vol", "crisis")
    regime_probability(asset)          -> dict {regime: probability}
    regime_adjusted_vol(asset, base_vol) -> float
    regime_transition_matrix()          -> np.ndarray
    days_in_current_regime(asset)       -> int
    serialize() / deserialize()
```

**Hidden Markov Model with 4 states (crypto):**

| Regime | Vol Range | Drift | Mean-Reversion | Correlations |
|---|---|---|---|---|
| Low vol | 25-40% | Positive | Strong (short OU half-life) | Normal |
| Normal | 40-70% | Neutral | Moderate | Normal |
| High vol | 70-120% | Negative | Weak | Elevated |
| Crisis | 120%+ | Strongly negative | None | Spike to 0.9+ |

**Detection method (initial — threshold-based):**
- Rolling 24h realized vol (already computed by crypto bot)
- Rolling 7-day realized vol for slower regime shifts
- Vol-of-vol: std(rolling_vol) over 30 days — high vol-of-vol signals instability
- Thresholds with hysteresis (enter crisis at vol > 120%, exit only when < 90%)

**Future: proper HMM with Baum-Welch** once 3+ months of vol history available.

**Integration with probability models:**
- crypto_price_probability() gains optional regime parameter
- Crisis: Student-t with df=4, wider sigma, zero drift
- Low vol: OU with shorter half-life
- Particle filter process_vol auto-adapts by regime

**Integration with capital allocator:**
- Crisis: reduce Kelly by 50%, tighten daily loss caps
- High vol: reduce Kelly by 25%, widen edge thresholds by 2pp
- Low vol: normal sizing (bread-and-butter profitable periods)
- Regime state in `data/regime-state.json`, read by allocator and dashboard

**Weather regime detection (simpler):**
- Track ensemble spread (max - min across GFS/ECMWF/ICON) over time
- High spread = uncertain → widen sigma, reduce sizing
- Low spread = confident → tighten sigma, increase sizing

### 4.4 Component 4: Jump-Diffusion Model for Crypto

Merton (1976) jump-diffusion adds Poisson jump process to GBM:
`dS/S = (mu - lambda*k) dt + sigma dW + J dN`

Where:
- N: Poisson process, intensity lambda (avg jumps/year)
- J: jump size, log(1+J) ~ N(mu_J, sigma_J)
- k = E[J] = exp(mu_J + sigma_J^2/2) - 1

**Calibration from historical data:**
- Count price moves > 3 sigma in last 90 days -> lambda
- Average size of those jumps -> mu_J
- Std dev of jump sizes -> sigma_J
- Typical crypto: lambda ~= 12/year (one jump/month), mu_J ~= -0.02, sigma_J ~= 0.05

**Integration:**
- New function: `crypto_price_probability_jd()` in probability.py
- Time horizon > 60 min: use JD (jump risk is material)
- Time horizon < 60 min: standard GBM (jumps rare within an hour)
- Simulation engine handles JD naturally — add Poisson-drawn jumps to each simulated path

**Competitive edge:** Institutional traders using standard GBM systematically underprice tail-risk contracts. JD assigns higher tail probability. Buying deep OTM contracts priced by GBM traders = structural edge.

### 4.5 Testing Strategy

- **IS**: verify matches closed-form CDF for near-money; verify non-zero estimates for deep OTM where crude MC gives 0
- **Variance reduction**: measure actual factors; verify each independently reduces variance; verify stacking is multiplicative
- **Regime**: backtest on 2024-2026 BTC data; verify aligns with known events (ETF approval, crashes); verify regime-adjusted vol improves Brier
- **Jump-diffusion**: calibrate on historical, compare JD vs GBM vs actual outcomes; verify JD assigns higher tail probability than GBM
- **Integration**: full crypto scan cycle with IS + regime + particle filter, verify composition
- **Performance**: IS with 100K paths + variance reduction must complete in < 2 seconds

### 4.6 Failure Modes

- **IS tilt too aggressive**: extreme likelihood ratios. Mitigation: monitor ESS; if ESS < N/10, reduce tilt
- **Regime misclassification**: false "crisis" cuts sizing unnecessarily. Mitigation: require 2+ hour persistence; hysteresis thresholds
- **Jump calibration overfitting**: 90 days may overfit to recent events. Mitigation: Bayesian priors (lambda ~= 12/year from long-term crypto history)
- **Performance regression**: IS adds cost. Mitigation: 60s cache TTL; only run simulation when |d2| >= 3

---

## Phase 5: Market Microstructure & Intelligence (Week 9-10)

### 5.1 Component 1: Agent-Based Order Book Simulation

New module: `src/kalshi/orderbook_sim.py`

```
OrderBookSimulator
    __init__(true_prob, n_informed, n_noise, n_mm, market_params)
    run(n_steps)                         -> price_history
    estimate_price_impact(order_size)    -> expected_slippage_cents
    estimate_fill_probability(price, side, duration_min) -> float
    optimal_execution_schedule(total_size, urgency)      -> [(time, size, price)]
    calibrate_from_history(market_ticker) -> fitted_params
    kyle_lambda(market_ticker)           -> float (price impact coefficient)
```

**Agent types (calibrated to Kalshi market structure):**

| Agent | % of Activity | Behavior |
|---|---|---|
| Informed | 5-15% | Trade toward true probability, noisy signal (~2-5%) |
| Noise | 60-80% | Random direction, exponential size (retail traders) |
| Market makers | 10-20% | Quote bid/ask, widen on inventory buildup |
| Bots | 5-15% | Deterministic strategies (longshot sellers, arb) |

**Calibration from Kalshi data:**
- Fetch recent trade history per target market via API
- Estimate agent ratios from trade size distribution
- Estimate Kyle's lambda from price impact regression: delta_P = lambda * sign(order) * sqrt(size)
- Estimate order arrival rate k from inter-trade intervals

**Market maker activation process:**
1. Simulate 10,000 sessions with our MM placing quotes
2. Measure: spread captured, inventory risk, adverse selection cost, PnL distribution
3. Find optimal gamma/k per market's microstructure
4. Activate only on markets where simulated Sharpe > 1.0
5. Monitor real vs simulated PnL — auto-disable if divergence > 2 sigma

**Integration with market-maker.py:**
- Replace hardcoded gamma=0.3, k=1.5 with per-market calibrated params
- Pre-trade price impact check: if impact > 1 cent, split order or price further from market
- Fill probability estimation: if P(fill in 5 min) < 20%, price more aggressively

### 5.2 Component 2: Real-Time P&L Attribution

New module: `src/kalshi/pnl_attribution.py`

```
PnLAttributor
    __init__(trade_log_paths)
    attribute_by_source()      -> dict {model_component: pnl_cents}
    attribute_by_bot()         -> dict {bot_name: pnl_cents}
    attribute_by_regime()      -> dict {regime: pnl_cents}
    attribute_by_edge_bucket() -> dict {edge_range: pnl_cents, win_rate, count}
    marginal_value(component)  -> float (P&L with vs without)
    decay_analysis()           -> dict {strategy: half_life_days}
    report() / json_report()
```

**Attribution dimensions:**

1. **By model component**: P&L from base model, particle filter, regime adjustment, correlation sizing. Marginal contribution = P&L with component minus P&L without.

2. **By edge bucket**: Group by edge at entry (4-8%, 8-15%, 15%+). Reveals if profitable across all levels or only at high edge.

3. **By regime**: P&L during low-vol vs normal vs high-vol vs crisis. Validates regime detector.

4. **Decay analysis**: Per-strategy, measure P&L per trade over time. Detect declining edge before it shows in aggregate P&L.

**Integration with dashboard:**
- New endpoint: `/api/attribution`
- Dashboard "Attribution" tab: P&L by component, bot, regime, edge bucket
- Daily update via `scripts/daily-attribution.py`

### 5.3 Component 3: Competitive Edge Monitoring

New module: `src/kalshi/edge_monitor.py`

```
EdgeMonitor
    __init__(lookback_days)
    update_market_efficiency(ticker, our_prob, market_price, settled_outcome)
    efficiency_trend(market_type)     -> float (slope of efficiency over time)
    edge_half_life(strategy)          -> float (days until edge decays 50%)
    detect_new_competitor(market_type) -> bool
    optimal_strategy_allocation()     -> dict {strategy: weight}
    report() / json_report()
```

**Measurements:**

1. **Market efficiency trend**: Track gap between model probability and market price. Shrinking gap = competitors entering.

2. **Edge half-life**: Exponential decay model per strategy. 90-day half-life = durable. 14-day = competitors adapting fast.

3. **New competitor detection**: Sudden efficiency jump (spread tightening, faster price adjustment) = new sophisticated participant. WhatsApp alert.

4. **Optimal allocation**: If weather edge is stable but crypto edge decaying, shift capital. Dynamic priority weights feed back to capital_allocator.py.

**Competitive response playbook:**
- Immediate: raise edge threshold for affected strategy
- Short-term: recalibrate model (sigma, priors)
- Medium-term: find new data sources to restore edge
- Long-term: reduce allocation, redeploy to durable-edge strategies

### 5.4 Component 4: Execution Quality Analytics

Enhancement to TradeManager and decision logs:

**Fill rate tracking:**
- Every limit order: log price, time, conditions
- Every fill/cancel: fill price, time-to-fill, slippage
- Metrics: fill rate by bot, by edge bucket, average slippage, time-to-fill distribution

**Execution cost analysis:**
- Per filled trade: P&L with market order vs our limit order
- Implementation shortfall: decision price vs actual fill price
- Fee drag: fees as % of gross P&L

**Optimal execution timing:**
- Crypto: time-of-day fill rate patterns (Asia vs US session)
- Weather: earlier (uncertainty, wide spreads) vs later (certainty, tight spreads)
- Entertainment: immediate on HDD publication vs wait for digest

Feeds back into `compute_limit_price()` — refine edge-tiered pricing from actual fill data.

### 5.5 Testing Strategy

- **ABM**: price convergence, informed profit at noise expense, Kyle's lambda positive, calibration on synthetic data
- **Attribution**: sums to total P&L (no leakage), marginal value is additive, synthetic trade log coverage
- **Edge monitor**: decay detection on known half-life data, competitor detection on step-change, allocation shift toward long-half-life strategies
- **Execution**: partial fills, cancellations, expired orders handled; slippage correct for buy and sell

### 5.6 Failure Modes

- **ABM overfitting**: limited Kalshi history. Mitigation: regularize toward Gode-Sunder defaults; validate out-of-sample
- **Attribution circularity**: PF uses correlation uses regime. Mitigation: Shapley value (correct but expensive); approximation: drop-one-out for daily reports
- **Edge monitor false positives**: random variance. Mitigation: require 7+ day sustained improvement; t-test with p < 0.05
- **Execution survivorship bias**: only analyzing filled orders misses opportunity cost. Mitigation: track hypothetical market-order P&L for unfilled limits

---

## Cross-Phase Integration

### Data Flow

```
Market Data (APIs)
        |
        v
Phase 1: Bot Scans (weather, crypto, economics, entertainment)
        | raw observations
        v
Phase 2: Particle Filter (smooth, carry state, produce CI)
        | filtered_prob, ci_width, trend
        v
Phase 4: Simulation Engine (IS for tails, JD for jumps, regime adjustment)
        | robust_prob, std_error, regime
        v
Phase 3: Correlation Engine (portfolio VaR, cluster limits, tail dependence)
        | correlation-adjusted budget
        v
Capital Allocator + Phase 5 Edge Monitor (budget, dynamic strategy weights)
        | approved trade
        v
Phase 5: Execution (price impact, fill probability, optimal schedule) + TradeManager
        | filled trade
        v
Phase 5: P&L Attribution + Edge Monitor (decay detection, competitor alerts)
```

### New Files

| File | Phase | Purpose |
|---|---|---|
| `scripts/skip-audit.py` | 1 | Skip reason analysis, money-left-on-table report |
| `src/kalshi/particle_filter.py` | 2 | Sequential Monte Carlo belief state |
| `src/kalshi/correlation_engine.py` | 3 | Pairwise correlation, t-copula, portfolio VaR |
| `src/kalshi/simulation.py` | 4 | IS, variance reduction, MC engine |
| `src/kalshi/regime_detector.py` | 4 | HMM-based vol regime classification |
| `src/kalshi/orderbook_sim.py` | 5 | Agent-based market simulation |
| `src/kalshi/pnl_attribution.py` | 5 | P&L decomposition by component |
| `src/kalshi/edge_monitor.py` | 5 | Edge decay and competitor detection |
| `scripts/daily-attribution.py` | 5 | Daily P&L attribution report |
| `tests/test_particle_filter.py` | 2 | PF unit + integration tests |
| `tests/test_correlation.py` | 3 | Correlation engine tests |
| `tests/test_simulation.py` | 4 | IS + variance reduction tests |
| `tests/test_regime.py` | 4 | Regime detection tests |
| `tests/test_orderbook_sim.py` | 5 | ABM tests |
| `tests/test_attribution.py` | 5 | P&L attribution tests |
| `tests/test_edge_monitor.py` | 5 | Edge monitoring tests |

### Dependency Graph

```
Phase 1 (standalone)
    |
    v
Phase 2 (particle_filter.py — no new deps)
    |
    +---> Phase 3 (correlation_engine.py — introduces scipy)
    |         |
    |         v
    +---> Phase 4 (simulation.py + regime_detector.py — uses scipy from Phase 3)
              |
              v
         Phase 5 (orderbook_sim + attribution + edge_monitor — uses all above)
```

Phase 2 starts immediately after Phase 1. Phases 3 and 4 run in parallel after Phase 2. Phase 5 requires all prior phases.

### Success Criteria

| Metric | Current | Target (Phase 1) | Target (Phase 5) |
|---|---|---|---|
| Markets evaluated per scan (crypto) | ~30 | ~50 | ~50 |
| Trade rate (trades / evaluated) | ~5% | ~15% | ~20% |
| Brier score (crypto) | 0.037 | 0.035 | 0.025 |
| Brier score (weather) | ~0.15 | 0.12 | 0.08 |
| Portfolio VaR accuracy | Not measured | Not measured | Within 10% of realized |
| Edge decay detection | Not measured | Not measured | Detect within 7 days |
| Fill rate (limit orders) | ~50% est | ~60% | ~75% |
| Daily P&L (target) | ~$0 (demo) | $5-15/day | $20-50/day |

### Dependencies to Add

- `scipy` (Phase 3+): t-copula fitting, optimization, statistical tests
- `numpy` already in use (no change)
- No other new dependencies — all other components use pure Python + existing math infrastructure
