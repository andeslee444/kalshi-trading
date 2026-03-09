# World-Class Strategy Bot Design

**Date:** 2026-03-05
**Status:** Approved
**Scope:** Transform longshot bias bot into multi-signal quant pipeline

## Architecture

```
Layer 1: DATA INGESTION
  +-- REST API (existing) -- market prices, positions
  +-- WebSocket (new) -- real-time orderbook stream
  +-- Settlement DB (new) -- historical outcomes for online Bayes
  +-- External Sources (new) -- HDD, NWS, BoxOffice info edge
  +-- Polymarket CLOB (new) -- cross-platform price comparison

Layer 2: EDGE ESTIMATION (replaces longshot_edge)
  +-- Hierarchical Bayesian Model
  |   +-- Global prior: Becker(0.57, 0.15)
  |   +-- Category posterior: online updates after each settlement
  |   +-- Ticker-level shrinkage: thin data -> category mean
  +-- Credibility Interval: edge ~ N(mu, sigma^2) not point estimate
  +-- Dual-tail: both YES<30c and YES>70c longshots

Layer 3: MICROSTRUCTURE SIGNALS
  +-- Orderbook imbalance ratio (buy_vol / sell_vol)
  +-- Spread dynamics (compression = informed flow)
  +-- Volume acceleration (spike detection)
  +-- Fill probability estimator (maker advantage)

Layer 4: RISK-ADJUSTED SIZING
  +-- Bayesian Kelly: scale by confidence interval width
  +-- Copula correlation: effective independence count
  +-- Category budget caps (hard guardrails)
  +-- Intraday scheduling (morning/midday/afternoon waves)

Layer 5: EXECUTION
  +-- Smart limit pricing (existing, enhanced with fill prob)
  +-- Order monitoring + cancel/replace logic
  +-- Cross-platform arb execution (Kalshi vs Polymarket)
  +-- Post-trade attribution logging
```

## 1. Hierarchical Bayesian Edge Model (Online Learning)

Replaces point-estimate `longshot_edge()` with a posterior distribution that updates after every settlement.

### Mathematical Formulation

```
Global level:
  alpha_0 ~ N(0.57, 0.10^2)     # amplitude prior from Becker
  delta_0 ~ N(0.15, 0.05^2)     # decay rate prior from Becker

Category level (k = sports, entertainment, ...):
  alpha_k ~ N(alpha_0, tau_alpha^2)   # shrinkage toward global
  delta_k ~ N(delta_0, tau_delta^2)   # shrinkage toward global

Observation model (trade i in category k):
  overpricing_i = alpha_k * exp(-delta_k * price_i) * time_factor_i
  y_i ~ Bernoulli(implied_prob_i * (1 - overpricing_i))

Online posterior update (after each settlement):
  Use conjugate Beta-Binomial for win/loss tracking per price bucket
  Moment-match to update (alpha_k, delta_k) estimates
  Shrinkage: weight = n_k / (n_k + kappa), where kappa ~ 30
    posterior_k = weight * empirical_k + (1 - weight) * prior_k
```

### Key Output

Instead of `edge = 0.015` (point), we get `edge ~ N(0.015, 0.004^2)`:
- `mu_edge = 0.015` (best estimate)
- `sigma_edge = 0.004` (uncertainty)
- Confidence ratio: `mu / sigma = 3.75` (strong signal)

### Online Update Protocol

After each settlement event:
1. Load trade record (price, category, outcome)
2. Update category win/loss counts per price bucket (1-5c, 6-10c, 11-15c, 16-30c)
3. Refit amplitude/decay via moment matching
4. Apply shrinkage toward global prior
5. Persist updated params to `config/bayes-params.json`
6. Log parameter shift for drift detection

### Fallback

With <10 trades per category: use Becker priors directly. With 10-30: heavy shrinkage (weight < 0.5). With 30+: category params dominate.

## 2. Dual-Tail Longshot Exploitation

Expand from sell-side only (YES 1-15c) to both tails.

### Sell-Side (Expanded)

```
YES price 1-30c -> sell YES (buy NO)
overpricing = alpha * exp(-delta * yes_price)
edge = implied_prob * overpricing_ratio
```

Extended from 15c to 30c. At 20-30c, edge is smaller but still positive in retail-heavy categories (sports, entertainment). Quarter-Kelly naturally sizes down.

### Buy-Side (New)

```
YES price 70-99c -> buy YES (NO is the overpriced longshot)
no_price = 100 - yes_price (ranges 1-30c)
overpricing = alpha * exp(-delta * no_price)
no_implied_prob = no_price / 100
edge = no_implied_prob * overpricing_ratio

Trade: buy YES at market price
Risk: yes_price cents per contract (70-99c)
Profit: (100 - yes_price) cents if event occurs
```

Becker model applies symmetrically -- longshot bias exists at both tails.

### Implementation

New function `longshot_edge_buy(yes_price_cents, ticker, hours_to_close)`:
- Computes NO-side overpricing using `no_price = 100 - yes_price`
- Returns additive edge for buying YES
- Uses same category params as sell-side

New scanner `find_longshot_buys(markets, bankroll)`:
- Filters YES price 70-99c (equivalent to NO at 1-30c)
- Computes buy-side edge
- Sizes with `quarter_kelly()` (buy-side, risk = yes_price per contract)

## 3. Orderbook Microstructure Signals

### WebSocket Integration

Subscribe to Kalshi orderbook stream for all monitored tickers. Maintain in-memory orderbook state.

### Three Signal Types

```
1. Imbalance Ratio (IR):
   IR = bid_depth_cents / (bid_depth_cents + ask_depth_cents)
   IR > 0.65 -> buyers dominating -> retail flood -> INCREASE edge by (IR - 0.5) * 0.4
   IR < 0.35 -> sellers dominating -> smart money -> DECREASE edge by (0.5 - IR) * 0.4
   Neutral band: 0.35 < IR < 0.65 -> no adjustment

2. Spread Dynamics:
   spread_pct = (ask - bid) / midpoint
   spread_pct < 0.10 -> tight -> informed market -> edge *= 0.80
   spread_pct > 0.30 -> wide -> illiquid/retail -> edge *= 1.10
   Between: no adjustment

3. Volume Acceleration:
   vol_ratio = volume_last_15min / avg_15min_volume
   vol_ratio > 3.0 -> news event -> SKIP (too much uncertainty)
   vol_ratio > 1.5 -> elevated flow -> edge *= 0.90 (more informed)
   vol_ratio < 0.5 -> dead market -> no adjustment
```

### Fill Probability Model

```
P(fill | limit_price, orderbook) = sigmoid(
    beta_0 +
    beta_1 * (limit_price - midpoint) / spread +
    beta_2 * same_side_depth +
    beta_3 * time_in_force
)

Use fill probability to adjust limit pricing:
  If P(fill) < 0.3 and edge > 0.10: move price toward ask (urgency)
  If P(fill) > 0.7: keep patient placement (spread capture)
```

Train betas from historical fill/cancel data in trade logs.

## 4. Correlation-Aware Sizing

### Copula Model

```
For N trades in same category placed same day:
  rho_est = empirical_pairwise_correlation(category)
  # From settlement history: correlation between trades in same category

  effective_N = N / (1 + (N - 1) * rho_est)
  kelly_scale = sqrt(effective_N / N)

Each trade's Kelly fraction *= kelly_scale

Example:
  8 NBA trades, rho_est = 0.15
  effective_N = 8 / (1 + 7 * 0.15) = 8 / 2.05 = 3.9
  kelly_scale = sqrt(3.9 / 8) = 0.70
  -> Each trade sized at 70% of individual Kelly
```

### Category Correlation Priors

| Category Pair | Expected rho | Rationale |
|---------------|-------------|-----------|
| Same-sport same-night | 0.15-0.25 | Correlated sentiment, injury news |
| Same-sport different-night | 0.05-0.10 | Weak serial correlation |
| Cross-sport | 0.02-0.05 | Nearly independent |
| Entertainment same-week | 0.10-0.20 | Chart/award season effects |
| Weather same-region | 0.30-0.50 | Highly correlated (same weather system) |
| Cross-category | 0.01-0.03 | Near-independent |

### Hard Caps (Guardrails)

```
max_category_risk_pct = 0.30   # 30% of daily budget per category
max_single_trade_pct = 0.05    # 5% of daily budget per trade
max_total_daily_risk_pct = 0.60 # 60% of bankroll total daily risk
```

These fire even if copula model says trades are independent -- defense against model error.

## 5. Intraday Scheduling

### Three-Wave Structure

```
Wave 1 (8-10am ET):  30% of daily budget
  - Morning retail entering markets
  - Fresh overnight forecasts for weather
  - Sports lines from previous night settling

Wave 2 (11am-2pm ET): 50% of daily budget
  - Peak retail activity
  - Midday data releases (HDD, economic data)
  - Highest volume, best liquidity

Wave 3 (3-5pm ET):    20% of daily budget
  - Close-of-day consolidation
  - Final chance for near-settlement trades
  - Lower volume, wider spreads
```

### Implementation

Replace single `run_scan()` with `ScheduledScanner`:
- Maintains daily budget state across waves
- Each wave: full market scan with remaining budget allocation
- Between waves: WebSocket monitoring for exceptional opportunities
- Emergency scan triggered by volume acceleration > 5x

## 6. Settlement Source Integration

### Signal Sources

Check `data/health-state.json` and external source outputs for confirmed outcomes:

```
NWS Temperature:
  If current_temp confirms KXHIGH outcome with >95% confidence
  -> edge = max(0.50, 1.0 - kalshi_price/100)
  Source: source-monitor bot, data/nws-*.json

HDD Album Charts:
  If final chart published and confirms album ranking
  -> edge = 0.80+ (near certainty)
  Source: hdd-scraper, data/hdd-*.json

Box Office:
  If Sunday estimates published and confirm weekend total
  -> edge = 0.60-0.80 (estimates have ~5% error)
  Source: entertainment-bot
```

### High-Conviction Protocol

When external source confirms outcome:
- Sizing: half_kelly (not quarter) -- double normal conviction
- Limit pricing: full ask (urgency -- fill before market reprices)
- Skip orderbook signals (information edge dominates)
- Log as "info_arb" strategy type for attribution

## 7. Polymarket Cross-Platform Arbitrage

### Price Comparison

```
For each Kalshi market with a Polymarket equivalent:
  kalshi_yes = kalshi market YES price
  poly_yes = polymarket equivalent YES price

  spread = abs(kalshi_yes - poly_yes)

  If spread > fee_kalshi + fee_poly + 2c (minimum profit):
    If kalshi_yes < poly_yes:
      Buy Kalshi YES + Sell Polymarket YES
    Else:
      Sell Kalshi YES + Buy Polymarket YES

    Locked profit = spread - total_fees
    No directional risk (hedged)
```

### Market Matching

Use ticker parsing + title fuzzy matching to pair Kalshi/Polymarket markets:
- BTC/ETH price brackets (high overlap)
- Political events (moderate overlap)
- Sports props (some overlap)

Source: existing `polymarket_client.py` module.

### Sizing

Arb trades are near-riskless -- size more aggressively:
- Half-Kelly on the locked spread
- No correlation penalty (hedged position)
- Daily cap: 20% of bankroll in arb positions

## 8. Confidence-Scaled Kelly (Bayesian Kelly)

The key innovation connecting the Bayesian model to sizing:

```
Standard Kelly: f = (b*p - q) / b

Bayesian Kelly:
  mu_edge = posterior mean of edge
  sigma_edge = posterior std of edge
  confidence_ratio = mu_edge / sigma_edge

  If confidence_ratio < 1.0:
    # Uncertain edge -- aggressive shrinkage
    kelly_multiplier = confidence_ratio^2  (quadratic penalty)
  Elif confidence_ratio < 2.0:
    # Moderate confidence
    kelly_multiplier = confidence_ratio / 2.0
  Else:
    # High confidence
    kelly_multiplier = 1.0

  final_kelly = quarter_kelly * kelly_multiplier * copula_scale
```

This prevents over-betting when edge estimate is noisy (early in a category's history or unusual market conditions).

## 9. New Module: `strategy_engine.py`

All new logic lives in a new module to keep `strategy-trader.py` as the orchestrator:

```python
# strategy_engine.py -- World-class strategy pipeline

class BayesianEdgeEstimator:
    """Hierarchical Bayesian edge model with online learning."""
    def estimate_edge(self, price, category, hours) -> EdgeEstimate:
        # Returns EdgeEstimate(mu, sigma, confidence_ratio)
    def update_posterior(self, trade_outcome):
        # Online update after settlement
    def get_category_params(self, category) -> CategoryParams:
        # Current posterior (alpha, delta, n_trades)

class OrderbookSignals:
    """Real-time microstructure signals from WebSocket."""
    def imbalance_ratio(self, ticker) -> float
    def spread_dynamics(self, ticker) -> SpreadSignal
    def volume_acceleration(self, ticker) -> float
    def fill_probability(self, ticker, limit_price) -> float

class CorrelationAwareSizer:
    """Copula-based Kelly with category caps."""
    def size_trade(self, edge_estimate, category, n_concurrent) -> int
    def check_category_cap(self, category, proposed_risk) -> bool
    def effective_independence(self, category, n_trades) -> float

class ScheduledScanner:
    """Intraday wave-based scanning."""
    def current_wave(self) -> Wave  # 1, 2, or 3
    def remaining_budget(self) -> int  # cents
    def should_scan(self) -> bool

class CrossPlatformArb:
    """Polymarket price comparison and arb detection."""
    def find_arb_opportunities(self, kalshi_markets) -> list
    def compute_locked_profit(self, kalshi_price, poly_price) -> int

class SettlementSourceChecker:
    """Check external data for confirmed outcomes."""
    def check_info_edge(self, ticker) -> InfoEdge | None
```

## 10. Data Persistence

### New Files

| File | Purpose | Written by | Read by |
|------|---------|-----------|---------|
| `config/bayes-params.json` | Posterior params per category | BayesianEdgeEstimator | strategy-trader |
| `data/orderbook-state.json` | Current orderbook snapshots | WebSocket listener | OrderbookSignals |
| `data/correlation-history.json` | Pairwise settlement correlations | CorrelationAwareSizer | Sizer |
| `data/strategy-waves.json` | Intraday budget tracking | ScheduledScanner | Scanner |
| `data/arb-opportunities.json` | Cross-platform arb log | CrossPlatformArb | Dashboard |

### Settlement DB Schema

Extend existing trade logs with fields for Bayesian updates:
```json
{
  "ticker": "KXNBA-...",
  "category": "sports",
  "price_bucket": "6-10",
  "entry_price_cents": 8,
  "settlement_result": "win",
  "implied_prob": 0.08,
  "model_edge_at_entry": 0.0164,
  "posterior_alpha_before": 0.62,
  "posterior_alpha_after": 0.63
}
```

## Implementation Priority

1. **Bayesian edge model + online learning** -- highest impact on edge quality
2. **Dual-tail expansion (1-30c + 70-99c)** -- doubles opportunity set
3. **Correlation-aware sizing + category caps** -- prevents ruin
4. **Confidence-scaled Kelly** -- connects Bayes to sizing
5. **Intraday scheduling** -- captures time-of-day effects
6. **Settlement source integration** -- massive edge on triggered events
7. **Orderbook WebSocket** -- real-time microstructure
8. **Polymarket cross-platform arb** -- hedged profit extraction
9. **Fill probability model** -- requires historical fill data accumulation

## Testing Strategy

- Unit tests for BayesianEdgeEstimator: prior convergence, online updates, shrinkage behavior
- Unit tests for CorrelationAwareSizer: copula math, cap enforcement
- Unit tests for dual-tail edge: symmetry, price range validation
- Integration tests: full scan cycle with mocked orderbook/settlement data
- Backtest: compare Bayesian vs current model on historical settlements
- Monte Carlo: simulate 1000-day runs with correlation to verify ruin probability < 1%
