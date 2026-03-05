---
phase: quick-9
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
requirements: [SCHED-5, SETTLE-6, FILL-9]

must_haves:
  truths:
    - "Strategy bot scans in three ET waves (8-10am, 11am-2pm, 3-5pm) with budget allocation 30/50/20%"
    - "ScheduledScanner tracks daily budget spend across waves and prevents overspend"
    - "Settlement source checker reads health-state.json and returns InfoEdge when external data confirms outcome"
    - "High-conviction info-arb trades use half_kelly sizing and full ask pricing"
    - "FillProbabilityEstimator computes sigmoid fill probability and adjusts limit prices"
    - "Fill model learns online from trade outcomes"
  artifacts:
    - path: "src/kalshi/strategy_engine.py"
      provides: "ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator classes"
      contains: "class ScheduledScanner"
    - path: "src/kalshi/strategy-trader.py"
      provides: "Wave-based main() loop and info-arb integration in run_scan()"
      contains: "ScheduledScanner"
    - path: "tests/test_strategy_engine.py"
      provides: "Tests for all three new classes"
      contains: "TestScheduledScanner"
  key_links:
    - from: "src/kalshi/strategy-trader.py"
      to: "src/kalshi/strategy_engine.py"
      via: "import ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator"
      pattern: "from strategy_engine import.*ScheduledScanner"
    - from: "src/kalshi/strategy_engine.py"
      to: "data/health-state.json"
      via: "SettlementSourceChecker reads health-state.json for source freshness"
      pattern: "health-state.json"
    - from: "src/kalshi/strategy_engine.py"
      to: "data/strategy-fill-model.json"
      via: "FillProbabilityEstimator persists learned betas"
      pattern: "strategy-fill-model.json"
---

<objective>
Add intraday wave scheduling, settlement source integration, and fill probability model to the strategy bot.

Purpose: These three features from the world-class strategy bot design (Sections 5, 6, 9) make the bot time-aware, info-arb-capable, and fill-rate-optimized. Wave scheduling captures time-of-day edge (retail activity peaks), settlement sources exploit confirmed-outcome mispricing for massive edge, and the fill model prevents leaving edge on the table with overly patient limit prices.

Output: Three new classes in strategy_engine.py, integration in strategy-trader.py, comprehensive tests.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docs/plans/bot-improvements/04-strategy-bot.md (Sections 5, 6, 9)
@src/kalshi/strategy_engine.py (existing classes to extend)
@src/kalshi/strategy-trader.py (main() and run_scan() to modify)
@src/kalshi/kalshi_auth.py (HealthCheckMonitor, HEALTH_STATE_PATH)
@tests/test_strategy_engine.py (existing tests to extend)
@config/bots-config.json (strategy config)

<interfaces>
<!-- Key types and contracts the executor needs -->

From src/kalshi/strategy_engine.py:
```python
EdgeEstimate = namedtuple("EdgeEstimate",
    ["mu_edge", "sigma_edge", "confidence_ratio", "category", "price_bucket", "n_observations"])

class BayesianEdgeEstimator:
    def estimate_edge(self, price_cents, category, hours_to_close=999) -> EdgeEstimate
    def update_posterior(self, category, price_cents, won)
    def save_params(self, path)
    def load_params(self, path)

class CorrelationAwareSizer:
    def kelly_scale(self, n_trades, rho) -> float
    def check_category_cap(self, category, proposed_risk_cents) -> bool
    def record_trade(self, category, risk_cents)
    def reset_daily(self)
```

From src/kalshi/kalshi_auth.py:
```python
HEALTH_STATE_PATH = PROJECT_DIR / "data" / "health-state.json"
# health-state.json structure:
# {
#   "sources": { "NWS": {"last_success": "ISO-ts", "last_error": "ISO-ts", "error_count": 0}, ... },
#   "bots": { "source-monitor": {"last_heartbeat": "ISO-ts"}, ... }
# }
```

From src/kalshi/probability.py:
```python
def half_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None, fee_cents=0, return_details=False)
def compute_limit_price(yes_bid, yes_ask, side, edge=None)
def classify_ticker_category(ticker) -> str
def kalshi_fee_cents(price_cents) -> float
```

From src/kalshi/strategy-trader.py:
```python
# main() currently: --once mode runs run_scan() once; daemon mode runs in while True with SCAN_INTERVAL sleep
# run_scan() currently: checks settlements, fetches all markets, runs find_longshot_sells/buys, places trades
# Key variables: SCAN_INTERVAL (from config, default 15), trade_manager, client, allocator, edge_estimator
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator to strategy_engine.py with tests</name>
  <files>src/kalshi/strategy_engine.py, tests/test_strategy_engine.py</files>
  <behavior>
    ## ScheduledScanner
    - Test: current_wave() returns 1 during 8-10am ET, 2 during 11am-2pm ET, 3 during 3-5pm ET, None outside all windows
    - Test: remaining_budget() starts at wave allocation (30%/50%/20% of daily_budget) and decreases after record_spend()
    - Test: should_scan() returns True when inside a wave window and budget > 0
    - Test: should_scan() returns False outside wave windows (unless emergency)
    - Test: should_emergency_scan() returns True when volume_ratio > 5.0
    - Test: reset_daily() resets all wave budgets
    - Test: record_spend() in wave 1 reduces wave 1 budget, not wave 2/3
    - Test: spent budget carries forward (unspent wave 1 budget does NOT add to wave 2)
    - Test: next_scan_time() returns start of next wave when between waves

    ## SettlementSourceChecker
    - Test: check_info_edge() returns None when health-state.json does not exist
    - Test: check_info_edge() returns None when source data is stale (>30 min old)
    - Test: check_info_edge() returns InfoEdge for KXHIGH ticker when NWS source is fresh
    - Test: check_info_edge() returns InfoEdge for album ticker when HDD source is fresh
    - Test: InfoEdge has fields: source, edge, confidence, sizing_method, pricing_method
    - Test: check_info_edge() returns None for unrecognized ticker types
    - Test: _is_source_fresh() returns True when last_success is within threshold_minutes

    ## FillProbabilityEstimator
    - Test: estimate_fill_prob() returns sigmoid value between 0 and 1
    - Test: estimate_fill_prob() increases as limit price moves toward ask (aggressive)
    - Test: estimate_fill_prob() returns ~0.5 at midpoint (with default betas)
    - Test: adjust_limit_price() moves price toward ask when P(fill) < 0.3 and edge > 0.10
    - Test: adjust_limit_price() returns original price when P(fill) > 0.7
    - Test: adjust_limit_price() returns original price when edge < 0.05 (not worth urgency)
    - Test: update_from_outcome() shifts betas toward observed fill patterns
    - Test: save/load round-trip preserves betas
  </behavior>
  <action>
    Write tests FIRST in tests/test_strategy_engine.py (append to existing file), then implement in src/kalshi/strategy_engine.py.

    **ScheduledScanner class** (append to strategy_engine.py):
    - Constructor takes `daily_budget_cents` (int), `tz_name="America/New_York"` (string for zoneinfo)
    - Uses `from zoneinfo import ZoneInfo` and `import datetime` (already available via stdlib)
    - Wave definitions as class constants:
      ```
      WAVES = [
          {"id": 1, "start_hour": 8, "end_hour": 10, "budget_pct": 0.30, "label": "morning"},
          {"id": 2, "start_hour": 11, "end_hour": 14, "budget_pct": 0.50, "label": "midday"},
          {"id": 3, "start_hour": 15, "end_hour": 17, "budget_pct": 0.20, "label": "afternoon"},
      ]
      ```
    - `current_wave()` -> int or None: checks current ET hour against wave windows
    - `remaining_budget()` -> int: returns remaining cents for current wave (0 if outside wave)
    - `should_scan()` -> bool: True if inside a wave AND budget > 0
    - `should_emergency_scan(volume_ratio)` -> bool: True if volume_ratio > 5.0 (regardless of wave)
    - `record_spend(cents)`: deducts from current wave's remaining budget
    - `reset_daily()`: resets all wave budgets to their initial allocation
    - `next_scan_time()` -> datetime or None: returns start of next wave window (for sleep calculation)
    - Internal state: `_wave_budgets = {1: int, 2: int, 3: int}`, `_last_reset_date`

    **SettlementSourceChecker class** (append to strategy_engine.py):
    - Constructor takes `health_state_path` (Path, default `PROJECT_DIR / "data" / "health-state.json"`), `staleness_minutes=30`
    - `InfoEdge = namedtuple("InfoEdge", ["source", "edge", "confidence", "sizing_method", "pricing_method"])` -- define at module level
    - `check_info_edge(ticker)` -> InfoEdge | None:
      1. Read and parse health-state.json (catch FileNotFoundError, JSONDecodeError -> return None)
      2. Determine ticker type: KXHIGH -> check "NWS" source; ALBUM/BILLBOARD/HDD tickers -> check "HDD" source; BOX/MOVIE -> check "BoxOfficeMojo" source
      3. If source not in health data or source stale (last_success older than staleness_minutes) -> return None
      4. If source is fresh -> return InfoEdge with:
         - edge=0.50 for NWS (temperature data), edge=0.80 for HDD (album chart), edge=0.60 for BoxOffice
         - confidence=0.95 for NWS, 0.98 for HDD, 0.85 for BoxOffice
         - sizing_method="half_kelly" (not quarter -- double conviction per design doc)
         - pricing_method="full_ask" (urgency -- fill before market reprices)
    - `_is_source_fresh(source_data, threshold_minutes)` -> bool: helper that checks last_success timestamp
    - Use `from kalshi_auth import PROJECT_DIR` (already imported pattern in this file)
    - **IMPORTANT**: Ticker type detection uses prefix matching: `ticker.startswith("KXHIGH")` for weather, check for album/entertainment/box-office prefixes

    **FillProbabilityEstimator class** (append to strategy_engine.py):
    - Sigmoid model: `P(fill) = 1 / (1 + exp(-(b0 + b1*price_position + b2*depth_factor + b3*time_factor)))`
    - Constructor takes `betas_path` (Path or None) for persistence
    - Initial beta coefficients (reasonable priors, no historical data):
      ```
      b0 = -0.5  (slight bias toward not filling -- conservative)
      b1 = 2.0   (price aggressiveness is strongest predictor)
      b2 = -0.3  (more depth on same side = harder to fill)
      b3 = 0.5   (longer duration = more likely to fill)
      ```
    - `estimate_fill_prob(limit_price, mid, spread, depth=0.5, duration_minutes=60)` -> float:
      - `price_position = (limit_price - mid) / max(spread, 1)` -- positive = more aggressive toward ask
      - `depth_factor = depth` (0-1 normalized same-side depth)
      - `time_factor = min(duration_minutes / 60.0, 3.0)` (cap at 3 hours)
      - Apply sigmoid: `1.0 / (1.0 + math.exp(-(b0 + b1*price_position + b2*depth_factor + b3*time_factor)))`
      - Clamp to [0.01, 0.99]
    - `adjust_limit_price(original_limit, edge, fill_prob, yes_bid, yes_ask, side)` -> int:
      - If `fill_prob >= 0.7`: return original_limit (patient placement is fine)
      - If `edge < 0.05`: return original_limit (not worth urgency on thin edge)
      - If `fill_prob < 0.3 and edge > 0.10`: move price toward ask by `int((1.0 - fill_prob) * spread * 0.5)` cents
      - Otherwise: minor adjustment `int((0.7 - fill_prob) * spread * 0.3)` cents
      - Clamp result to [1, 99] and don't exceed the ask
    - `update_from_outcome(filled, limit_price, mid, spread, depth, duration_minutes)`:
      - Compute features same as estimate_fill_prob
      - Online SGD update: for each beta_i, `beta_i += learning_rate * (filled - predicted) * feature_i`
      - `learning_rate = 0.01` (slow learning, stable)
    - `save_model(path)` / `load_model(path)`: JSON round-trip of betas list
    - All math uses only `math` module

    **Tests** (append three new test classes to tests/test_strategy_engine.py):
    - `TestScheduledScanner`: Use `unittest.mock.patch` to mock `datetime.datetime.now()` to control time
    - `TestSettlementSourceChecker`: Use `tmp_path` fixture to create mock health-state.json files
    - `TestFillProbabilityEstimator`: Pure math tests, no mocking needed

    Import the new namedtuple `InfoEdge` in the test file's import block.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python -m pytest tests/test_strategy_engine.py -x -v 2>&1 | tail -40</automated>
  </verify>
  <done>All existing + new tests pass. ScheduledScanner returns correct waves for mocked ET times, tracks budget per wave. SettlementSourceChecker returns InfoEdge for fresh sources and None for stale/missing. FillProbabilityEstimator computes sigmoid fill prob, adjusts limit prices toward ask on low fill prob + high edge, and updates betas online.</done>
</task>

<task type="auto">
  <name>Task 2: Integrate wave scheduling, settlement sources, and fill model into strategy-trader.py</name>
  <files>src/kalshi/strategy-trader.py, config/bots-config.json</files>
  <action>
    **1. Update imports in strategy-trader.py** (line ~12-15):
    Add `ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator, InfoEdge` to the existing `from strategy_engine import (...)` block.

    **2. Add config keys to bots-config.json** under "strategy":
    ```json
    "waveScheduling": true,
    "settlementSources": true,
    "fillModel": true,
    "dailyBudgetCents": 10000,
    "fillModelPath": "data/strategy-fill-model.json"
    ```

    **3. Initialize new components** (after line ~47, after correlation_sizer initialization):
    ```python
    # Intraday wave scheduler
    _wave_scheduling_enabled = _bots_cfg.get("waveScheduling", True)
    _daily_budget = _bots_cfg.get("dailyBudgetCents", _bots_cfg.get("maxDailyLoss", 100) * 100)
    scheduler = ScheduledScanner(daily_budget_cents=_daily_budget) if _wave_scheduling_enabled else None

    # Settlement source checker
    _settlement_sources_enabled = _bots_cfg.get("settlementSources", True)
    settlement_checker = SettlementSourceChecker() if _settlement_sources_enabled else None

    # Fill probability model
    _fill_model_enabled = _bots_cfg.get("fillModel", True)
    FILL_MODEL_PATH = DATA_DIR / _bots_cfg.get("fillModelPath", "strategy-fill-model.json")
    fill_estimator = FillProbabilityEstimator(betas_path=FILL_MODEL_PATH) if _fill_model_enabled else None
    ```

    **4. Modify main() for wave-based scheduling** (replace the daemon while-True loop at line ~686-699):
    Replace the fixed `time.sleep(SCAN_INTERVAL * 60)` with wave-aware scheduling:
    ```python
    # Daemon loop
    if scheduler:
        scheduler.reset_daily()
    while True:
        try:
            health.record_bot_heartbeat("strategy")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()

            if scheduler and _wave_scheduling_enabled:
                wave = scheduler.current_wave()
                if wave and scheduler.should_scan():
                    log.info(f"Wave {wave} scan (budget remaining: ${scheduler.remaining_budget()/100:.2f})")
                    run_scan()
                elif not wave:
                    next_t = scheduler.next_scan_time()
                    if next_t:
                        wait_secs = max(60, (next_t - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
                        wait_secs = min(wait_secs, SCAN_INTERVAL * 60)
                        log.info(f"Between waves. Next wave at {next_t.strftime('%H:%M ET')}. Sleeping {wait_secs/60:.0f}m")
                        time.sleep(wait_secs)
                        continue
                    else:
                        log.info("No more waves today. Sleeping until tomorrow.")
                        time.sleep(SCAN_INTERVAL * 60)
                        continue
                else:
                    log.info(f"Wave budget exhausted. Sleeping {SCAN_INTERVAL}m...")
                    time.sleep(SCAN_INTERVAL * 60)
                    continue
            else:
                run_scan()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)
    ```
    Keep `--once` mode unchanged (runs run_scan() directly, no wave logic).

    **5. Add settlement source checking in run_scan()** (insert after market fetch, before longshot scanning):
    After `log.info(f"  Found {len(markets)} open markets")` (around line ~437), add:
    ```python
    # Strategy 0: Settlement source info-arb (highest priority)
    info_arb_trades = []
    if settlement_checker and _settlement_sources_enabled:
        log.info("\n" + "=" * 70)
        log.info("STRATEGY 0: Settlement Source Info-Arb (High Conviction)")
        log.info("=" * 70)
        for m in markets:
            ticker = m.get("ticker", "")
            info_edge = settlement_checker.check_info_edge(ticker)
            if info_edge is None:
                continue
            yes_bid = m.get("yes_bid", 0)
            yes_ask = m.get("yes_ask", 0)
            if yes_ask <= 0 or yes_bid <= 0:
                continue

            log.info(f"  INFO-ARB: {ticker} | Source: {info_edge.source} | Edge: {info_edge.edge*100:.0f}% | Confidence: {info_edge.confidence*100:.0f}%")

            # High-conviction protocol: half_kelly sizing, full ask pricing
            from probability import half_kelly
            budget = allocator.request_budget("strategy", ticker, edge=info_edge.edge)
            if not budget.approved:
                log.info(f"    Allocator denied: {budget.reason}")
                continue

            fee = kalshi_fee_cents(yes_ask)
            contracts, risk, kelly_details = half_kelly(
                info_edge.edge, yes_ask, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee,
                return_details=True,
            )
            if contracts <= 0:
                continue

            # Full ask pricing (urgency)
            buy_price = yes_ask

            reasoning = f"Info-arb: {info_edge.source} confirms outcome. Edge {info_edge.edge*100:.0f}%, conf {info_edge.confidence*100:.0f}%. Half-Kelly @ full ask."
            result = trade_manager.place_order(
                ticker, "yes", buy_price, contracts, reasoning,
                strategy="info_arb", est_edge=f"{info_edge.edge*100:.0f}%",
                risk_cents=risk, title=m.get("title", "")[:80],
                market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                sizing_method="half_kelly",
            )
            if result:
                allocator.record_trade("strategy", ticker, risk, edge=info_edge.edge)
                if scheduler:
                    scheduler.record_spend(risk)
                info_arb_trades.append({"ticker": ticker, "source": info_edge.source, "edge": info_edge.edge})
                trades_executed.append({
                    "ticker": ticker, "title": m.get("title", "")[:80],
                    "strategy": "info_arb", "direction": f"BUY YES @ {buy_price}c",
                    "contracts": contracts, "risk_cents": risk,
                    "est_edge": f"{info_edge.edge*100:.0f}%",
                    "reasoning": reasoning,
                    "order_id": result.get("order_id", "?"), "status": result.get("status", "?"),
                })
        log.info(f"  Info-arb trades: {len(info_arb_trades)}")
    ```
    Note: Move `trades_executed = []` to BEFORE Strategy 0 (currently it's at line ~464, move to ~438).

    **6. Integrate fill probability into limit pricing** in find_longshot_sells():
    After `sell_price = compute_limit_price(...)` (line ~119), add fill probability adjustment:
    ```python
    # Fill probability adjustment
    if fill_estimator and _fill_model_enabled and yes_bid and yes_ask and yes_ask > yes_bid:
        mid = (yes_bid + yes_ask) / 2.0
        spread = yes_ask - yes_bid
        fill_prob = fill_estimator.estimate_fill_prob(sell_price, mid, spread)
        adjusted = fill_estimator.adjust_limit_price(sell_price, est_edge_prelim, fill_prob, yes_bid, yes_ask, "no")
        if adjusted != sell_price:
            log.debug(f"  Fill prob {fill_prob:.2f} -> adjusted limit {sell_price} -> {adjusted}")
            sell_price = adjusted
    ```
    Same pattern in find_longshot_buys() after `buy_price = compute_limit_price(...)` (line ~278):
    ```python
    if fill_estimator and _fill_model_enabled and yes_bid and yes_ask and yes_ask > yes_bid:
        mid = (yes_bid + yes_ask) / 2.0
        spread = yes_ask - yes_bid
        fill_prob = fill_estimator.estimate_fill_prob(buy_price, mid, spread)
        adjusted = fill_estimator.adjust_limit_price(buy_price, est_edge, fill_prob, yes_bid, yes_ask, "yes")
        if adjusted != buy_price:
            buy_price = adjusted
    ```

    **7. Track budget spend in run_scan():**
    After each successful trade placement (in both longshot sell and buy sections), add:
    ```python
    if scheduler:
        scheduler.record_spend(c["risk_cents"])
    ```

    **8. Daily reset in run_scan():**
    At the top of run_scan(), after `correlation_sizer.reset_daily()`, add:
    ```python
    if scheduler:
        scheduler.reset_daily()
    ```

    **CRITICAL NOTES:**
    - Do NOT remove any existing functionality. All changes are additive.
    - All three features are gated by config flags (waveScheduling, settlementSources, fillModel). When disabled, behavior is identical to current.
    - The `--once` mode must continue working unchanged.
    - `fill_estimator` and `settlement_checker` are module-level globals (same pattern as `edge_estimator`).
    - The `half_kelly` import for info-arb can be at top of file (add to the existing `from probability import` line).
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python -c "
import sys, types
# Stub kalshi_auth to avoid API calls
stub = types.ModuleType('kalshi_auth')
stub.KalshiClient = type('KC', (), {'__init__': lambda s: None, 'get_balance': lambda s: (50000, 50000), 'get': lambda s,p: {}, 'get_all_markets': lambda s,**kw: []})
stub.setup_unbuffered = lambda: None
stub.setup_signal_handlers = lambda: None
stub.setup_logging = lambda n: __import__('logging').getLogger(n)
stub.PROJECT_DIR = __import__('pathlib').Path('.')
stub.TradeManager = type('TM', (), {'__init__': lambda s,*a,**k: None, 'log_decision': lambda s,*a,**k: None})
stub.trim_trade_log = lambda p: None
stub._atomic_write_json = lambda p,d: None
stub.build_market_snapshot = lambda **k: {}
stub.HealthCheckMonitor = type('HCM', (), {'__init__': lambda s,**k: None, 'record_bot_heartbeat': lambda s,b: None, 'check_health': lambda s: []})
stub.OrderMonitor = type('OM', (), {'__init__': lambda s,*a,**k: None, 'check_orders': lambda s: None})
stub.ScanSummary = type('SS', (), {'__init__': lambda s,*a,**k: None, 'markets_fetched': 0, 'trades_placed': 0, 'finalize': lambda s: None})
sys.modules['kalshi_auth'] = stub

# Stub capital_allocator
ca_stub = types.ModuleType('capital_allocator')
ca_stub.PortfolioAllocator = type('PA', (), {'__init__': lambda s,*a,**k: None, 'request_budget': lambda s,*a,**k: type('B',(),{'approved':False,'reason':'stub'})()})
sys.modules['capital_allocator'] = ca_stub

# Now test the imports work
from strategy_engine import ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator, InfoEdge
print('All new classes import OK')
s = ScheduledScanner(daily_budget_cents=10000)
print(f'ScheduledScanner created, wave={s.current_wave()}')
sc = SettlementSourceChecker()
print(f'SettlementSourceChecker created')
fp = FillProbabilityEstimator()
print(f'FillProbabilityEstimator created, fill_prob at mid={fp.estimate_fill_prob(50, 50, 10):.3f}')
print('Integration smoke test PASSED')
" 2>&1</automated>
  </verify>
  <done>Strategy bot imports and initializes all three new components. Wave scheduling gates the daemon loop. Settlement source checking runs as Strategy 0 before longshot scanning. Fill probability adjusts limit prices. All features gated by config flags. Existing tests still pass. --once mode unchanged.</done>
</task>

</tasks>

<verification>
1. `pytest tests/test_strategy_engine.py -x -v` -- all tests pass (existing + new)
2. `pytest tests/ -x --timeout=120` -- full test suite passes (no regressions)
3. Smoke import test passes (strategy-trader.py loads without API calls)
4. Config flags `waveScheduling: false`, `settlementSources: false`, `fillModel: false` disable all new features cleanly
</verification>

<success_criteria>
- ScheduledScanner correctly identifies wave windows in ET timezone
- SettlementSourceChecker reads health-state.json and returns InfoEdge for confirmed sources
- FillProbabilityEstimator computes sigmoid fill probability and adjusts limit prices
- strategy-trader.py daemon loop uses wave scheduling instead of fixed interval
- Info-arb trades use half_kelly sizing and full ask pricing
- All features are config-gated and backward-compatible
- All tests pass (existing + ~25 new tests)
</success_criteria>

<output>
After completion, create `.planning/quick/9-strategy-bot-intraday-wave-scheduling-se/9-SUMMARY.md`
</output>
