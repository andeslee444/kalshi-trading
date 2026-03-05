---
phase: quick-7
plan: 1
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/strategy_engine.py
  - src/kalshi/strategy-trader.py
  - tests/test_strategy_engine.py
  - config/bots-config.json
autonomous: true
requirements: [STRAT-BAYES, STRAT-DUAL, STRAT-CORR, STRAT-BKELLY]

must_haves:
  truths:
    - "Bayesian edge model produces posterior distribution (mu, sigma) not point estimate"
    - "Category parameters converge toward empirical data with more settlements"
    - "Shrinkage pulls thin-data categories toward Becker priors"
    - "Buy-side longshot exploitation finds YES 70-99c opportunities"
    - "Sell-side expanded to 30c (from 15c)"
    - "Copula-based sizing reduces Kelly when trades are correlated"
    - "Category caps enforce hard risk limits regardless of copula model"
    - "Confidence-scaled Kelly penalizes noisy edge estimates"
  artifacts:
    - path: "src/kalshi/strategy_engine.py"
      provides: "BayesianEdgeEstimator, CorrelationAwareSizer, bayesian_kelly_multiplier"
      min_lines: 250
    - path: "tests/test_strategy_engine.py"
      provides: "Comprehensive tests for all new engine classes"
      min_lines: 200
  key_links:
    - from: "src/kalshi/strategy-trader.py"
      to: "src/kalshi/strategy_engine.py"
      via: "import BayesianEdgeEstimator, CorrelationAwareSizer"
      pattern: "from strategy_engine import"
    - from: "src/kalshi/strategy_engine.py"
      to: "src/kalshi/probability.py"
      via: "imports classify_ticker_category, quarter_kelly_sell, quarter_kelly, longshot_edge"
      pattern: "from probability import"
    - from: "src/kalshi/strategy_engine.py"
      to: "config/bayes-params.json"
      via: "persists and loads category posteriors"
      pattern: "bayes-params\\.json"
---

<objective>
Build the core quant engine for the world-class strategy bot: Bayesian edge model with online learning, dual-tail longshot exploitation (sell-side 1-30c + buy-side 70-99c), copula-based correlation-aware sizing with category caps, and confidence-scaled Kelly sizing.

Purpose: Replace the point-estimate longshot_edge() with a full posterior distribution that learns from settlements, doubles the opportunity set with buy-side longshots, and prevents portfolio ruin through correlation-aware position sizing.

Output: New `strategy_engine.py` module, comprehensive tests, integrated strategy-trader.py.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@src/kalshi/probability.py (longshot_edge, classify_ticker_category, quarter_kelly_sell, quarter_kelly, kalshi_fee_cents, compute_limit_price, half_kelly)
@src/kalshi/strategy-trader.py (current bot to upgrade)
@config/bots-config.json (strategy config section)
@docs/plans/bot-improvements/04-strategy-bot.md (approved design doc)
@tests/test_strategy_bugs.py (existing test patterns for strategy)

<interfaces>
<!-- From src/kalshi/probability.py -->
```python
LONGSHOT_BIAS_PARAMS = {
    "sports": (0.65, 0.12), "entertainment": (0.60, 0.14),
    "politics": (0.45, 0.18), "weather": (0.30, 0.20),
    "economics": (0.35, 0.18), "crypto": (0.50, 0.15),
    "default": (0.57, 0.15),
}
def classify_ticker_category(ticker) -> str  # returns "sports", "entertainment", etc.
def longshot_edge(yes_price_cents, ticker="", hours_to_close=999) -> float  # additive edge
def quarter_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents=None, ...) -> (contracts, risk, details)
def quarter_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None, ...) -> (contracts, risk, details)
def kalshi_fee_cents(price_cents) -> float
def compute_limit_price(yes_bid, yes_ask, side, edge=None) -> int
```

<!-- From src/kalshi/kalshi_auth.py -->
```python
from kalshi_auth import KalshiClient, TradeManager, setup_logging, PROJECT_DIR, _atomic_write_json, build_market_snapshot
```

<!-- From src/kalshi/capital_allocator.py -->
```python
from capital_allocator import PortfolioAllocator
# allocator.request_budget("strategy", ticker, edge=est_edge) -> Budget
# allocator.record_trade("strategy", ticker, risk_cents, edge=edge)
```

<!-- From config/bots-config.json strategy section -->
```json
{
  "strategy": {
    "maxBetCents": 1000,
    "maxBetPct": 0.02,
    "maxDailyTrades": 20,
    "maxDailyLoss": 100,
    "maxDailyLossPct": 0.10,
    "scanIntervalMinutes": 15
  }
}
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Create strategy_engine.py with Bayesian edge, dual-tail, correlation-aware sizing, and confidence-scaled Kelly</name>
  <files>src/kalshi/strategy_engine.py, tests/test_strategy_engine.py</files>
  <behavior>
    ## BayesianEdgeEstimator
    - Prior init: Each category starts with LONGSHOT_BIAS_PARAMS amplitude/decay as prior mean, prior_n=0
    - estimate_edge(price=5, category="sports", hours=24) returns EdgeEstimate with mu > 0, sigma > 0, confidence_ratio = mu/sigma
    - estimate_edge(price=5, category="sports") with 0 observations returns Becker prior edge (identical to current longshot_edge)
    - After 50 sports updates at price=5 where 48/50 settle as wins for the seller (YES expires worthless), posterior alpha should shift toward higher overpricing
    - Shrinkage test: category with n=5 trades -> posterior weighted 5/(5+30) = 14% empirical, 86% prior
    - Shrinkage test: category with n=60 trades -> posterior weighted 60/(60+30) = 67% empirical, 33% prior
    - Price buckets: 1-5c, 6-10c, 11-15c, 16-20c, 21-30c correctly assigned
    - update_posterior with win at 3c: bucket "1-5" win_count increments
    - update_posterior with loss at 8c: bucket "6-10" loss_count increments
    - Persistence: save_params() writes JSON, load_params() reads it back with identical state
    - Edge estimate symmetry: sell-side edge at price P == buy-side edge at price (100-P) for same category

    ## Dual-tail longshot
    - longshot_edge_buy(yes_price=92, ticker="KXNBA-TEST", hours=24): returns edge > 0 (NO at 8c is overpriced longshot)
    - longshot_edge_buy(yes_price=50): returns 0 (not a longshot tail)
    - longshot_edge_buy(yes_price=75): returns edge > 0 but smaller than at 95c
    - longshot_edge_sell(yes_price=5): identical to current longshot_edge(5) with same params
    - longshot_edge_sell(yes_price=25): returns edge > 0 (expanded range)
    - longshot_edge_sell(yes_price=35): returns 0 (outside 1-30c range)

    ## CorrelationAwareSizer
    - effective_n(n=8, rho=0.15) = 8/(1+7*0.15) = 3.90 (within 0.01)
    - effective_n(n=1, rho=0.5) = 1.0 (single trade, no reduction)
    - effective_n(n=10, rho=0.0) = 10.0 (independent trades)
    - kelly_scale(n=8, rho=0.15) = sqrt(3.90/8) = 0.698 (within 0.01)
    - check_category_cap: returns False when proposed risk exceeds 30% of daily budget
    - check_category_cap: returns True when within budget
    - Category correlation priors: same_category -> 0.15, cross_category -> 0.02

    ## bayesian_kelly_multiplier (confidence-scaled Kelly)
    - confidence_ratio < 1.0: multiplier = confidence_ratio^2 (quadratic penalty)
    - confidence_ratio = 0.5: multiplier = 0.25
    - confidence_ratio = 1.5: multiplier = 0.75 (= 1.5/2.0)
    - confidence_ratio = 3.0: multiplier = 1.0 (capped)
    - confidence_ratio <= 0: multiplier = 0.0
  </behavior>
  <action>
    Create `src/kalshi/strategy_engine.py` with the following classes and functions. ALL math uses only `math` module (no scipy/numpy).

    **1. EdgeEstimate dataclass/namedtuple:**
    ```
    EdgeEstimate(mu_edge, sigma_edge, confidence_ratio, category, price_bucket, n_observations)
    ```

    **2. BayesianEdgeEstimator class:**
    - `__init__(params_path)`: Load from `config/bayes-params.json` or init from LONGSHOT_BIAS_PARAMS priors
    - Internal state: per-category, per-price-bucket win/loss counts. Price buckets: [1-5, 6-10, 11-15, 16-20, 21-30]
    - `_price_bucket(price_cents)`: Map 1-30c to bucket name
    - `estimate_edge(price_cents, category, hours_to_close)` -> EdgeEstimate:
      - Compute amplitude/decay posterior via shrinkage: `weight = n_k / (n_k + kappa)` where kappa=30
      - `posterior_alpha = weight * empirical_alpha + (1 - weight) * prior_alpha` (same for delta)
      - Empirical alpha/delta: fit from win/loss counts per price bucket using moment matching
      - `overpricing = posterior_alpha * exp(-posterior_delta * price) * time_factor`
      - `mu_edge = implied_prob * overpricing`
      - `sigma_edge` derived from posterior uncertainty: `prior_sigma / sqrt(1 + n_k/kappa)`
      - Return EdgeEstimate with confidence_ratio = mu/sigma
    - `estimate_edge_buy(yes_price_cents, category, hours_to_close)` -> EdgeEstimate:
      - Compute NO-side overpricing: `no_price = 100 - yes_price`, apply Becker model to no_price
      - Returns edge for buying YES (profiting from NO-side longshot bias)
    - `update_posterior(category, price_cents, won_bool)`:
      - Increment bucket win/loss counts
      - Refit empirical amplitude/decay via moment matching on win rates per bucket
      - The moment matching: for each bucket with data, compute empirical_win_rate = wins/(wins+losses).
        The Becker model predicts win_rate_seller = 1 - implied_prob*(1 - alpha*exp(-delta*midprice)).
        Use weighted least squares across buckets to fit alpha, delta.
        If fewer than 2 buckets have data, use prior directly.
    - `save_params(path)` / `load_params(path)`: JSON persistence of all category state
    - `_moment_match_alpha_delta(bucket_data)`: Given dict of {bucket: (wins, losses)}, fit alpha/delta
      using grid search over alpha in [0.1, 0.9] step 0.05 and delta in [0.05, 0.30] step 0.01.
      Minimize sum of squared errors between predicted and observed seller win rates, weighted by n per bucket.

    **3. Dual-tail functions (module-level, use BayesianEdgeEstimator internally):**
    - `longshot_edge_sell(yes_price_cents, ticker, hours_to_close, estimator=None)`:
      - Range: 1-30c (expanded from 15c)
      - If estimator provided, use Bayesian posterior; else fall back to current longshot_edge()
    - `longshot_edge_buy(yes_price_cents, ticker, hours_to_close, estimator=None)`:
      - Range: 70-99c (NO-side is 1-30c longshot)
      - Compute NO price, apply Becker model symmetrically
      - Return edge for buying YES

    **4. CorrelationAwareSizer class:**
    - `__init__(daily_budget_cents, category_cap_pct=0.30, single_trade_cap_pct=0.05)`:
      - Track risk per category for the current day
    - Category correlation priors dict (from design doc Section 4):
      ```
      CATEGORY_CORRELATIONS = {
          "same_sport_same_night": 0.20,
          "same_sport_diff_night": 0.07,
          "cross_sport": 0.03,
          "entertainment_same_week": 0.15,
          "weather_same_region": 0.40,
          "cross_category": 0.02,
      }
      ```
    - `get_intra_category_rho(category)`: Return default rho for that category type
    - `effective_n(n_trades, rho)`: `n / (1 + (n-1) * rho)`, handle n<=1
    - `kelly_scale(n_trades, rho)`: `sqrt(effective_n / n)`, handle n<=1 returns 1.0
    - `size_trade(edge_estimate, n_concurrent_same_category, bankroll_cents, max_cost_cents)`:
      - Compute kelly_scale from copula
      - Compute bayesian_kelly_multiplier from edge_estimate.confidence_ratio
      - Call quarter_kelly or quarter_kelly_sell based on trade side
      - Multiply contracts by kelly_scale * bayesian_multiplier
      - Return (contracts, risk_cents, details)
    - `check_category_cap(category, proposed_risk_cents)`: True if within cap
    - `record_trade(category, risk_cents)`: Track running risk per category
    - `reset_daily()`: Clear daily tracking

    **5. bayesian_kelly_multiplier(confidence_ratio) -> float:**
    Module-level function implementing the confidence-scaled Kelly from design doc Section 8:
    - confidence_ratio <= 0: return 0.0
    - confidence_ratio < 1.0: return confidence_ratio ** 2 (quadratic penalty)
    - confidence_ratio < 2.0: return confidence_ratio / 2.0
    - confidence_ratio >= 2.0: return 1.0

    **Tests in tests/test_strategy_engine.py:**
    Write tests FIRST (red), then implement (green). Structure with pytest classes:
    - `TestBayesianEdgeEstimator`: prior convergence, online updates (50 settlements), shrinkage at n=5 vs n=60, price bucket assignment, persistence round-trip, edge > 0 for valid prices, edge = 0 for out-of-range
    - `TestDualTailLongshot`: sell-side 1-30c returns edge > 0, sell-side 35c returns 0, buy-side 92c returns edge > 0, buy-side 50c returns 0, buy-side symmetry with sell-side
    - `TestCorrelationAwareSizer`: effective_n math, kelly_scale math, category cap enforcement (approve/deny), single trade cap, intra-category rho lookup, daily reset clears state
    - `TestBayesianKellyMultiplier`: boundary values (0, 0.5, 1.0, 1.5, 2.0, 3.0), negative confidence
    - `TestMomentMatching`: synthetic data with known alpha/delta recovers params within tolerance

    Import pattern: `from strategy_engine import BayesianEdgeEstimator, CorrelationAwareSizer, bayesian_kelly_multiplier, longshot_edge_sell, longshot_edge_buy, EdgeEstimate`
    Use `from probability import _reset_calibration` in fixture (autouse) to clear cached state.
    Tests must work with `pytest tests/test_strategy_engine.py` -- no API calls, no credentials.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_strategy_engine.py -v</automated>
  </verify>
  <done>
    - strategy_engine.py exists with BayesianEdgeEstimator, CorrelationAwareSizer, bayesian_kelly_multiplier, EdgeEstimate, longshot_edge_sell, longshot_edge_buy
    - All Bayesian math uses only `math` module (no scipy/numpy)
    - Tests pass: prior convergence, online updates shift posterior, shrinkage correct at n=5/60, dual-tail symmetry, copula effective_n math, category caps, Kelly multiplier boundaries
    - EdgeEstimate returns (mu, sigma, confidence_ratio) not just a float
    - Persistence round-trip works (save/load JSON)
  </done>
</task>

<task type="auto">
  <name>Task 2: Integrate strategy_engine into strategy-trader.py with dual-tail scanning</name>
  <files>src/kalshi/strategy-trader.py, config/bots-config.json</files>
  <action>
    Modify `strategy-trader.py` to use the new strategy_engine module while keeping the existing bot structure pattern intact.

    **1. New imports at top of file:**
    ```python
    from strategy_engine import (
        BayesianEdgeEstimator, CorrelationAwareSizer,
        bayesian_kelly_multiplier, longshot_edge_sell, longshot_edge_buy, EdgeEstimate,
    )
    ```

    **2. Initialize engine objects after client/allocator setup (module level):**
    ```python
    BAYES_PARAMS_PATH = PROJECT_DIR / "config" / "bayes-params.json"
    edge_estimator = BayesianEdgeEstimator(params_path=BAYES_PARAMS_PATH)

    # Daily budget for correlation-aware sizing
    _daily_budget = _bots_cfg.get("maxDailyLoss", 100) * 100  # convert to cents
    correlation_sizer = CorrelationAwareSizer(daily_budget_cents=_daily_budget)
    ```

    **3. Add strategy config to bots-config.json:**
    Add new fields to the "strategy" section:
    ```json
    {
      "strategy": {
        ... existing fields ...,
        "enableBuyLongshots": true,
        "sellMaxPrice": 30,
        "buyMinPrice": 70,
        "categoryCap": 0.30,
        "singleTradeCap": 0.05,
        "bayesianEdge": true,
        "bayesKappa": 30
      }
    }
    ```

    **4. Modify `find_longshot_sells()` to use Bayesian edge:**
    - Expand price filter from `yes_ask > 15` to `yes_ask > sell_max_price` (30c from config)
    - Replace `longshot_edge(yes_ask, ...)` call with `longshot_edge_sell(yes_ask, ..., estimator=edge_estimator)` when bayesian enabled, else keep original
    - Replace `longshot_edge(sell_price, ...)` recompute with same
    - Add copula sizing: count concurrent same-category candidates, compute kelly_scale
    - Apply bayesian_kelly_multiplier to final contracts
    - Check correlation_sizer.check_category_cap() before adding to candidates
    - Add edge_estimate fields to candidate dict: mu_edge, sigma_edge, confidence_ratio, kelly_multiplier, copula_scale
    - Update reasoning string to include Bayesian confidence

    **5. Add new `find_longshot_buys()` function:**
    ```python
    def find_longshot_buys(markets, bankroll):
        """Find YES 70-99c contracts to BUY (exploit NO-side longshot bias)."""
    ```
    - Filter: `yes_bid >= buy_min_price` (70c from config) and `yes_bid <= 99`
    - Compute edge via `longshot_edge_buy(yes_bid, ticker, hours, estimator=edge_estimator)`
    - Size with `quarter_kelly()` (buy-side: risk = yes_price per contract, win = 100-yes_price)
    - Apply bayesian_kelly_multiplier and copula_scale
    - Check category cap
    - Place limit within spread using `compute_limit_price(yes_bid, yes_ask, "yes", edge=est_edge)`
    - Build candidate dict with strategy="longshot_buy", side="yes", action="buy"
    - Reasoning string: "Buy-side longshot: YES@{price}c, NO equiv {100-price}c longshot. Becker model edge ~{edge}%."

    **6. Update `run_scan()` to include buy-side:**
    After the sell-side section, add:
    ```python
    # Strategy 2: Buy-side longshot exploitation
    log.info("\n" + "=" * 70)
    log.info("STRATEGY 2: Buy-Side Longshot Exploitation (Buy YES on high-prob events)")
    log.info("=" * 70)
    buy_longshots = find_longshot_buys(markets, avail)
    log.info(f"  Found {len(buy_longshots)} buy-side longshot candidates")
    ```
    Place buy-side trades after sell-side trades (top 5 buy-side, in addition to top 10 sell-side).
    Record trade via trade_manager.place_order with strategy="longshot_buy".

    **7. Add settlement update hook in `check_settled_trades()`:**
    After finding settled trades, call:
    ```python
    for s in settled:
        ticker = s.get("ticker", "")
        category = classify_ticker_category(ticker)
        # Determine price_cents from trade log
        won = s.get("settlement_result") == "won" or s.get("revenue", 0) > 0
        # Update Bayesian model
        edge_estimator.update_posterior(category, price_cents, won)
    edge_estimator.save_params(BAYES_PARAMS_PATH)
    ```

    **8. Reset daily correlation sizer at start of run_scan():**
    ```python
    # Reset daily caps if new day
    correlation_sizer.reset_daily()
    ```

    **Preserve all existing behavior:** The changes are additive. Existing sell-side trades still work identically when `bayesianEdge: false`. The buy-side is a new parallel strategy. All TradeManager safety guards (kill switch, circuit breaker, daily limits, dedup) still apply.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -c "
import sys, types
from pathlib import Path
from unittest.mock import MagicMock

# Stub kalshi_auth
fake_auth = types.ModuleType('kalshi_auth')
fake_auth.KalshiClient = lambda *a, **kw: MagicMock()
fake_auth.setup_unbuffered = lambda: None
fake_auth.setup_signal_handlers = lambda: None
fake_auth.setup_logging = lambda *a, **kw: __import__('logging').getLogger('test')
fake_auth.PROJECT_DIR = Path('/tmp/fake_strategy_test')
fake_auth.TradeManager = type('TradeManager', (), {'__init__': lambda self, *a, **kw: None, 'place_order': lambda self, *a, **kw: None, 'log_decision': lambda self, *a, **kw: None})
fake_auth.trim_trade_log = lambda *a, **kw: None
fake_auth._atomic_write_json = lambda *a, **kw: None
fake_auth.build_market_snapshot = lambda **kw: {}
fake_auth.HealthCheckMonitor = type('HealthCheckMonitor', (), {'__init__': lambda self, *a, **kw: None})
fake_auth.OrderMonitor = type('OrderMonitor', (), {'__init__': lambda self, *a, **kw: None})
fake_auth.ScanSummary = type('ScanSummary', (), {'__init__': lambda self, *a, **kw: None, 'markets_fetched': 0, 'trades_placed': 0, 'finalize': lambda self: None})
sys.modules['kalshi_auth'] = fake_auth

fake_alloc = types.ModuleType('capital_allocator')
fake_alloc.PortfolioAllocator = type('PortfolioAllocator', (), {'__init__': lambda self, *a, **kw: None})
sys.modules['capital_allocator'] = fake_alloc

import json
config_dir = Path('/tmp/fake_strategy_test/config')
config_dir.mkdir(parents=True, exist_ok=True)
data_dir = Path('/tmp/fake_strategy_test/data')
data_dir.mkdir(parents=True, exist_ok=True)
(config_dir / 'bots-config.json').write_text(json.dumps({'strategy': {'maxBetCents': 500, 'scanIntervalMinutes': 15, 'maxDailyTrades': 20, 'maxDailyLoss': 50, 'enableBuyLongshots': True, 'sellMaxPrice': 30, 'buyMinPrice': 70, 'categoryCap': 0.30, 'singleTradeCap': 0.05, 'bayesianEdge': True, 'bayesKappa': 30}}))

import importlib.util
spec = importlib.util.spec_from_file_location('strategy_trader', 'src/kalshi/strategy-trader.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert hasattr(mod, 'find_longshot_sells'), 'find_longshot_sells missing'
assert hasattr(mod, 'find_longshot_buys'), 'find_longshot_buys missing'
assert hasattr(mod, 'edge_estimator'), 'edge_estimator missing'
assert hasattr(mod, 'correlation_sizer'), 'correlation_sizer missing'
print('Strategy-trader integration OK: all new functions and objects present')
" && pytest tests/test_strategy_engine.py tests/test_strategy_bugs.py -v --tb=short</automated>
  </verify>
  <done>
    - strategy-trader.py imports and uses BayesianEdgeEstimator and CorrelationAwareSizer
    - find_longshot_sells() expanded to 30c with Bayesian edge and copula sizing
    - find_longshot_buys() function exists and scans YES 70-99c markets
    - run_scan() executes both sell-side and buy-side strategies
    - Settlement updates flow to Bayesian model via check_settled_trades()
    - bots-config.json has new strategy fields (enableBuyLongshots, sellMaxPrice, buyMinPrice, etc.)
    - All existing tests still pass (test_strategy_bugs.py)
    - Module loads without errors with stubbed dependencies
  </done>
</task>

</tasks>

<verification>
1. `pytest tests/test_strategy_engine.py -v` -- all new tests pass
2. `pytest tests/test_strategy_bugs.py -v` -- existing tests still pass
3. `pytest tests/ -x --timeout=120` -- full test suite passes (no regressions)
4. `python3 -c "from strategy_engine import BayesianEdgeEstimator, CorrelationAwareSizer, bayesian_kelly_multiplier, longshot_edge_sell, longshot_edge_buy"` -- imports work
5. No scipy/numpy imports in strategy_engine.py: `grep -c "import numpy\|import scipy" src/kalshi/strategy_engine.py` returns 0
</verification>

<success_criteria>
- BayesianEdgeEstimator produces EdgeEstimate(mu, sigma, confidence_ratio) and updates online
- Dual-tail scanning: sell-side 1-30c AND buy-side 70-99c both find candidates
- CorrelationAwareSizer applies copula scaling and enforces category caps
- Confidence-scaled Kelly reduces sizing when edge uncertainty is high
- All math uses only `math` module
- 30+ tests pass covering all new functionality
- Existing strategy-trader behavior preserved (sell-side still works, all safety guards intact)
</success_criteria>

<output>
After completion, create `.planning/quick/7-world-class-strategy-bot-bayesian-edge-m/7-SUMMARY.md`
</output>
