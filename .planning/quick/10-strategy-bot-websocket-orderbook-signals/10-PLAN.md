---
phase: quick-10
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
requirements: [STRAT-ORDERBOOK, STRAT-ARB]

must_haves:
  truths:
    - "OrderbookSignals fetches REST orderbook snapshot and computes imbalance ratio"
    - "Spread dynamics signal adjusts edge down for tight spreads, up for wide"
    - "Volume acceleration above 3.0 triggers SKIP, above 1.5 reduces edge"
    - "compute_edge_adjustment returns a single float multiplier combining all three signals"
    - "CrossPlatformArb reuses fuzzy matching from cross-platform-arb.py to find arb pairs"
    - "compute_locked_profit correctly subtracts both Kalshi and Polymarket fees"
    - "Arb opportunities with net profit below 2c + fees are rejected"
    - "Strategy trader scan loop applies microstructure signals before sizing"
    - "Arb scan runs after longshot strategies with 20% bankroll daily cap"
  artifacts:
    - path: "src/kalshi/strategy_engine.py"
      provides: "OrderbookSignals, ArbOpportunity, CrossPlatformArb classes"
      min_lines: 350
    - path: "tests/test_strategy_engine.py"
      provides: "Tests for orderbook signals and cross-platform arb"
      min_lines: 350
  key_links:
    - from: "src/kalshi/strategy-trader.py"
      to: "src/kalshi/strategy_engine.py"
      via: "import OrderbookSignals, CrossPlatformArb"
      pattern: "from strategy_engine import"
    - from: "src/kalshi/strategy_engine.py"
      to: "src/kalshi/cross-platform-arb.py"
      via: "reuses normalize_event_text, fuzzy_match_score, validate_match"
      pattern: "from cross_platform_arb import|normalize_event_text|fuzzy_match_score"
    - from: "src/kalshi/strategy_engine.py"
      to: "src/kalshi/polymarket_client.py"
      via: "PolymarketClient for price fetching"
      pattern: "from polymarket_client import"
---

<objective>
Add orderbook microstructure signals (REST snapshot-based) and Polymarket cross-platform arb detection to the strategy bot engine.

Purpose: Orderbook signals refine edge estimates using real-time market microstructure (imbalance, spread, volume). Cross-platform arb detects hedged profit opportunities between Kalshi and Polymarket, placing the Kalshi side and logging the Polymarket opportunity.

Output: Extended `strategy_engine.py` with OrderbookSignals and CrossPlatformArb classes, comprehensive TDD tests, integrated `strategy-trader.py` scan loop.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@src/kalshi/strategy_engine.py
@src/kalshi/strategy-trader.py
@src/kalshi/cross-platform-arb.py
@src/kalshi/polymarket_client.py
@src/kalshi/kalshi_auth.py
@tests/test_strategy_engine.py
@config/bots-config.json
@docs/plans/bot-improvements/04-strategy-bot.md (Sections 3 and 7)

<interfaces>
<!-- Existing interfaces the executor needs -->

From src/kalshi/kalshi_auth.py:
```python
class KalshiClient:
    def get(self, path: str, **kwargs): ...  # Authenticated GET
    # Orderbook endpoint: client.get(f"/markets/{ticker}/orderbook")
    # Returns: {"orderbook": {"yes": [[price, depth], ...], "no": [[price, depth], ...]}}
```

From src/kalshi/strategy_engine.py (existing, to extend):
```python
EdgeEstimate = namedtuple("EdgeEstimate", ["mu_edge", "sigma_edge", "confidence_ratio", "category", "price_bucket", "n_observations"])
class BayesianEdgeEstimator: ...
class CorrelationAwareSizer: ...
def bayesian_kelly_multiplier(confidence_ratio): ...
def longshot_edge_sell(yes_price_cents, ticker, hours_to_close, estimator): ...
def longshot_edge_buy(yes_price_cents, ticker, hours_to_close, estimator): ...
```

From src/kalshi/polymarket_client.py:
```python
class PolymarketClient:
    def get_markets(self, query=None, limit=100, offset=0): ...  # Returns list of market dicts
    def get_price(self, token_id): ...  # Returns decimal 0-1
    def get_best_bid(self, token_id): ...  # Returns decimal 0-1 or None
    def get_midpoint(self, token_id): ...  # Returns decimal 0-1 or None
```

From src/kalshi/cross-platform-arb.py (functions to REUSE, not rewrite):
```python
def normalize_event_text(text): ...
def fuzzy_match_score(text1, text2): ...
def validate_match(k_text, p_text, score): ...
def match_markets(kalshi_markets, polymarket_markets): ...
def _extract_direction(text): ...
def _extract_numbers(text): ...
# These are module-level functions. Import via importlib since the file has a hyphen.
```

From src/kalshi/probability.py:
```python
def kalshi_fee_cents(price_cents): ...
def classify_ticker_category(ticker): ...
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: TDD OrderbookSignals and CrossPlatformArb classes in strategy_engine.py</name>
  <files>tests/test_strategy_engine.py, src/kalshi/strategy_engine.py</files>
  <behavior>
    --- OrderbookSignals ---
    - Test: imbalance_ratio({bid_depth: 650, ask_depth: 350}) returns 0.65
    - Test: imbalance_ratio({bid_depth: 0, ask_depth: 0}) returns 0.5 (neutral fallback)
    - Test: edge_adjustment_for_imbalance(IR=0.70) returns positive adjustment ((0.70-0.5)*0.4 = 0.08)
    - Test: edge_adjustment_for_imbalance(IR=0.30) returns negative adjustment ((0.30-0.5)*0.4 = -0.08)
    - Test: edge_adjustment_for_imbalance(IR=0.50) returns 0.0 (neutral)
    - Test: spread_multiplier(spread_pct=0.05) returns 0.80 (tight spread)
    - Test: spread_multiplier(spread_pct=0.20) returns 1.0 (mid-range, no adjustment)
    - Test: spread_multiplier(spread_pct=0.40) returns 1.10 (wide spread)
    - Test: volume_signal(vol_ratio=4.0) returns ("skip", None) -- SKIP trade
    - Test: volume_signal(vol_ratio=2.0) returns ("reduce", 0.90) -- reduce edge
    - Test: volume_signal(vol_ratio=0.3) returns ("ok", 1.0) -- no adjustment
    - Test: compute_edge_adjustment(IR=0.65, spread_pct=0.05, vol_ratio=1.0) returns combined multiplier
    - Test: compute_edge_adjustment with vol_ratio > 3.0 returns 0.0 (SKIP signal)
    - Test: compute_edge_adjustment neutral case (IR=0.5, spread_pct=0.15, vol_ratio=1.0) returns ~1.0
    - Test: parse_orderbook_response parses Kalshi REST orderbook JSON into bid_depth, ask_depth, best_bid, best_ask, volume_estimate

    --- ArbOpportunity namedtuple ---
    - Test: ArbOpportunity has fields: kalshi_ticker, poly_token_id, kalshi_price, poly_price, spread, locked_profit, direction

    --- CrossPlatformArb ---
    - Test: compute_locked_profit(kalshi_price=40, poly_price=0.48, kalshi_fee=0.7, poly_fee=2.0) returns correct cents
    - Test: compute_locked_profit returns 0 when spread < fees
    - Test: find_arb_opportunities returns empty list when no Polymarket matches
    - Test: find_arb_opportunities returns ArbOpportunity when spread exceeds minimum viable
    - Test: arb daily cap rejects trades when 20% bankroll limit hit
    - Test: arb direction is "buy_kalshi" when kalshi_price < poly_price, "sell_kalshi" when opposite
  </behavior>
  <action>
    **Phase RED: Write failing tests first.**

    Append new test classes to `tests/test_strategy_engine.py`:

    1. `class TestOrderbookSignals:` with all orderbook signal tests listed in behavior.
       - Tests use pure data (dicts/numbers), no API calls.
       - For `parse_orderbook_response`, provide a sample Kalshi orderbook JSON:
         ```python
         sample_book = {"orderbook": {"yes": [[60, 100], [58, 200]], "no": [[42, 150], [44, 50]]}}
         ```
         The YES bids at 60c and 58c mean bid_depth = 300c-equivalent. The NO asks at 42c and 44c correspond to YES asks at 56c and 58c. Parse to extract: best_bid (YES), best_ask (YES), bid_depth (total YES bid size), ask_depth (total YES ask size).
         NOTE: Kalshi orderbook format is `[[price_cents, num_contracts], ...]`. YES side lists buy orders (bids for YES), NO side lists buy orders for NO (which are effectively sell orders for YES). best_yes_bid = max price on yes side, best_yes_ask = 100 - max price on no side.

    2. `class TestCrossPlatformArb:` with all arb tests listed in behavior.
       - Use mock data for Kalshi markets and Polymarket results.
       - For `find_arb_opportunities`, supply pre-matched pairs (the class delegates matching to the imported fuzzy match functions from cross-platform-arb.py).
       - The `CrossPlatformArb` constructor takes a `PolymarketClient` instance and optional bankroll_cents.
       - Test the 20% daily cap by creating a sizer that tracks arb exposure and rejects when exceeded.

    Run tests: `pytest tests/test_strategy_engine.py -x -v -k "TestOrderbookSignals or TestCrossPlatformArb"` -- ALL MUST FAIL (RED).

    **Phase GREEN: Implement classes in strategy_engine.py.**

    Add to `src/kalshi/strategy_engine.py` (append after existing CorrelationAwareSizer class):

    **Import the fuzzy matching functions from cross-platform-arb.py using importlib** (since filename has a hyphen):
    ```python
    import importlib.util
    _arb_spec = importlib.util.spec_from_file_location(
        "cross_platform_arb",
        Path(__file__).parent / "cross-platform-arb.py"
    )
    _arb_mod = importlib.util.module_from_spec(_arb_spec)
    _arb_spec.loader.exec_module(_arb_mod)
    normalize_event_text = _arb_mod.normalize_event_text
    fuzzy_match_score = _arb_mod.fuzzy_match_score
    validate_match = _arb_mod.validate_match
    match_markets = _arb_mod.match_markets
    ```
    IMPORTANT: This import will execute cross-platform-arb.py's module-level code which instantiates KalshiClient. To avoid this, instead COPY the four pure functions (normalize_event_text, fuzzy_match_score, _extract_direction, _extract_numbers, validate_match, match_markets) into a new section of strategy_engine.py. They are pure text-processing functions with no external dependencies except `re` and `difflib.SequenceMatcher`. This avoids the side-effect problem. Add a comment: `# Fuzzy matching functions adapted from cross-platform-arb.py`.

    **OrderbookSignals class:**
    ```python
    class OrderbookSignals:
        """Orderbook microstructure signals from REST snapshots."""

        @staticmethod
        def parse_orderbook_response(data):
            """Parse Kalshi GET /markets/{ticker}/orderbook response.
            Returns dict: {best_bid, best_ask, bid_depth, ask_depth, spread_pct, midpoint}
            """
            # data = {"orderbook": {"yes": [[price, qty], ...], "no": [[price, qty], ...]}}

        @staticmethod
        def imbalance_ratio(bid_depth, ask_depth):
            """IR = bid_depth / (bid_depth + ask_depth). Returns 0.5 if both zero."""

        @staticmethod
        def spread_multiplier(spread_pct):
            """Tight (<10%) -> 0.80, wide (>30%) -> 1.10, else 1.0."""

        @staticmethod
        def volume_signal(vol_ratio):
            """vol_ratio > 3.0 -> ("skip", None), > 1.5 -> ("reduce", 0.90), else ("ok", 1.0)."""

        @staticmethod
        def compute_edge_adjustment(imbalance_ratio_val, spread_pct, vol_ratio):
            """Combine all signals into a single float edge multiplier.
            If vol_ratio > 3.0: return 0.0 (SKIP).
            Otherwise: base = 1.0 + (IR - 0.5) * 0.4, then *= spread_multiplier, then *= vol_multiplier.
            Clamp result to [0.5, 1.5].
            """
    ```

    **ArbOpportunity namedtuple:**
    ```python
    ArbOpportunity = namedtuple("ArbOpportunity", [
        "kalshi_ticker", "poly_token_id", "kalshi_price", "poly_price",
        "spread", "locked_profit", "direction"
    ])
    ```

    **CrossPlatformArb class:**
    ```python
    POLYMARKET_FEE_PCT = 0.02  # 2% taker fee
    KALSHI_MIN_SPREAD_CENTS = 2  # Minimum 2c profit after fees

    class CrossPlatformArb:
        """Cross-platform arb detection: Kalshi vs Polymarket."""

        def __init__(self, pm_client, bankroll_cents=0, daily_cap_pct=0.20):
            self._pm = pm_client
            self._bankroll = bankroll_cents
            self._daily_cap_pct = daily_cap_pct
            self._daily_arb_exposure = 0

        def compute_locked_profit(self, kalshi_price_cents, poly_price_decimal, kalshi_fee_cents_val, poly_fee_pct=POLYMARKET_FEE_PCT):
            """Compute locked profit in cents for buying Kalshi YES + selling Poly YES.
            locked_profit = (poly_price * 100 - kalshi_price_cents) - kalshi_fee - (poly_price * 100 * poly_fee_pct)
            Returns max(0, locked_profit) as int cents.
            """

        def find_arb_opportunities(self, kalshi_markets, pm_markets):
            """Find arb opportunities between matched Kalshi and Polymarket markets.
            Uses match_markets() for fuzzy matching.
            For each match, fetches Polymarket price, computes locked profit.
            Filters: locked_profit >= KALSHI_MIN_SPREAD_CENTS, within daily cap.
            Returns list of ArbOpportunity.
            """

        def check_daily_cap(self, proposed_cost_cents):
            """Check if proposed arb trade is within 20% bankroll daily cap."""
            cap = self._bankroll * self._daily_cap_pct
            return (self._daily_arb_exposure + proposed_cost_cents) <= cap

        def record_arb_trade(self, cost_cents):
            """Record arb exposure for daily cap tracking."""
            self._daily_arb_exposure += cost_cents

        def reset_daily(self):
            """Reset daily arb exposure tracking."""
            self._daily_arb_exposure = 0
    ```

    Run tests: `pytest tests/test_strategy_engine.py -x -v -k "TestOrderbookSignals or TestCrossPlatformArb"` -- ALL MUST PASS (GREEN).

    **Phase REFACTOR:** Ensure all existing tests still pass: `pytest tests/test_strategy_engine.py -x -v` -- no regressions.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_strategy_engine.py -x -v 2>&1 | tail -30</automated>
  </verify>
  <done>All new tests pass (OrderbookSignals: ~15 tests, CrossPlatformArb: ~7 tests). All existing tests in test_strategy_engine.py still pass. No scipy/numpy imports.</done>
</task>

<task type="auto">
  <name>Task 2: Integrate OrderbookSignals and CrossPlatformArb into strategy-trader.py scan loop</name>
  <files>src/kalshi/strategy-trader.py, config/bots-config.json</files>
  <action>
    **Modify `src/kalshi/strategy-trader.py`:**

    1. **Add imports** at the top (extend existing strategy_engine import):
       ```python
       from strategy_engine import (
           BayesianEdgeEstimator, CorrelationAwareSizer,
           bayesian_kelly_multiplier, longshot_edge_sell, longshot_edge_buy, EdgeEstimate,
           OrderbookSignals, CrossPlatformArb, ArbOpportunity,
       )
       from polymarket_client import PolymarketClient
       ```

    2. **Initialize OrderbookSignals and CrossPlatformArb** after existing `correlation_sizer` initialization:
       ```python
       # Orderbook microstructure signals (REST snapshot-based)
       _orderbook_signals_enabled = _bots_cfg.get("orderbookSignals", True)
       orderbook_signals = OrderbookSignals()

       # Cross-platform arb (Polymarket)
       _arb_enabled = _bots_cfg.get("arbEnabled", False)  # Default disabled until user enables
       _arb_daily_cap_pct = _bots_cfg.get("arbDailyCapPct", 0.20)
       pm_client = PolymarketClient() if _arb_enabled else None
       cross_arb = None  # Initialized in run_scan when bankroll known
       ```

    3. **Add orderbook signal application to `find_longshot_sells`** -- after edge estimation but before Kelly sizing, for each candidate that passes the min_edge filter:
       ```python
       # Apply orderbook microstructure signals
       ob_multiplier = 1.0
       if _orderbook_signals_enabled:
           try:
               ob_data = client.get(f"/markets/{ticker}/orderbook")
               parsed = orderbook_signals.parse_orderbook_response(ob_data)
               if parsed:
                   # Volume ratio: use market volume as rough proxy (no 15-min history yet)
                   vol_ratio = 1.0  # Default: no volume acceleration signal without historical baseline
                   ob_multiplier = orderbook_signals.compute_edge_adjustment(
                       parsed["imbalance_ratio"], parsed["spread_pct"], vol_ratio
                   )
                   if ob_multiplier == 0.0:
                       trade_manager.log_decision(ticker, "no", "skipped", "orderbook_volume_spike",
                                                   imbalance=round(parsed["imbalance_ratio"], 3),
                                                   spread_pct=round(parsed["spread_pct"], 3))
                       continue
                   est_edge *= ob_multiplier
           except Exception as e:
               log.debug(f"Orderbook fetch failed for {ticker}: {e}")
               # Gracefully degrade: proceed without microstructure adjustment
       ```
       Add the same pattern to `find_longshot_buys`.
       Include `ob_multiplier` in the candidate dict and reasoning string.

    4. **Add arb scan to `run_scan()`** -- after the buy-side longshot block (after line ~583), before the final balance check:
       ```python
       # Strategy 3: Cross-platform arbitrage
       if _arb_enabled and pm_client:
           log.info("\n" + "=" * 70)
           log.info("STRATEGY 3: Cross-Platform Arbitrage (Kalshi vs Polymarket)")
           log.info("=" * 70)

           # Initialize arb sizer with current bankroll
           cross_arb = CrossPlatformArb(pm_client, bankroll_cents=avail, daily_cap_pct=_arb_daily_cap_pct)

           # Fetch Polymarket markets for matching
           pm_markets = []
           for query in ["CPI", "Bitcoin", "Ethereum", "President", "Federal Reserve", "GDP", "jobs"]:
               try:
                   results = pm_client.get_markets(query=query, limit=50)
                   if isinstance(results, list):
                       pm_markets.extend(results)
                   time.sleep(0.3)  # rate limit
               except Exception as e:
                   log.error(f"Polymarket fetch error for '{query}': {e}")

           if pm_markets:
               arb_opps = cross_arb.find_arb_opportunities(markets, pm_markets)
               log.info(f"  Found {len(arb_opps)} arb opportunities")

               for opp in arb_opps[:5]:  # Top 5 arb trades
                   log.info(f"\n  ARB: {opp.kalshi_ticker}")
                   log.info(f"    Kalshi@{opp.kalshi_price}c vs Poly@{opp.poly_price*100:.0f}c")
                   log.info(f"    Direction: {opp.direction} | Locked profit: {opp.locked_profit}c")

                   # Place Kalshi-side trade only (Polymarket execution is manual/deferred)
                   if opp.direction == "buy_kalshi":
                       side = "yes"
                       price = opp.kalshi_price
                   else:
                       side = "no"
                       price = 100 - opp.kalshi_price

                   budget = allocator.request_budget("strategy", opp.kalshi_ticker, edge=opp.spread)
                   if not budget.approved:
                       log.info(f"    Allocator denied: {budget.reason}")
                       continue

                   if not cross_arb.check_daily_cap(price):
                       log.info(f"    Daily arb cap reached")
                       break

                   fee = kalshi_fee_cents(price)
                   contracts, risk, kelly_details = quarter_kelly(
                       opp.spread, price, budget.max_cost_cents,
                       bankroll_cents=budget.bankroll_cents, fee_cents=fee,
                       return_details=True,
                   )
                   if contracts <= 0:
                       continue

                   reasoning = (
                       f"Cross-platform arb: Kalshi {opp.kalshi_ticker} {side}@{price}c vs "
                       f"Polymarket@{opp.poly_price*100:.0f}c, spread={opp.spread*100:.1f}%, "
                       f"locked_profit={opp.locked_profit}c. Poly side: MANUAL."
                   )
                   result = trade_manager.place_order(
                       opp.kalshi_ticker, side, price, contracts, reasoning,
                       strategy="cross_platform_arb", est_edge=f"{opp.spread*100:.2f}%",
                       risk_cents=risk, poly_token_id=opp.poly_token_id,
                       poly_price=round(opp.poly_price, 4),
                   )
                   if result:
                       cross_arb.record_arb_trade(price * contracts)
                       allocator.record_trade("strategy", opp.kalshi_ticker, risk, edge=opp.spread)
                       trades_executed.append({
                           "ticker": opp.kalshi_ticker,
                           "strategy": "cross_platform_arb",
                           "direction": f"{side.upper()} @ {price}c",
                           "contracts": contracts,
                           "risk_cents": risk,
                           "est_edge": f"{opp.spread*100:.1f}%",
                           "reasoning": reasoning,
                           "order_id": result.get("order_id", "?"),
                           "status": result.get("status", "?"),
                       })
           else:
               log.info("  No Polymarket markets fetched")
       ```

    5. **Log arb opportunities to file** for dashboard visibility:
       After the arb scan loop, write to `data/arb-opportunities.json`:
       ```python
       if _arb_enabled and arb_opps:
           arb_log = [{"timestamp": datetime.datetime.now().isoformat(),
                       "ticker": o.kalshi_ticker, "poly_token": o.poly_token_id,
                       "kalshi_price": o.kalshi_price, "poly_price": round(o.poly_price, 4),
                       "spread": round(o.spread, 4), "locked_profit": o.locked_profit,
                       "direction": o.direction} for o in arb_opps]
           arb_path = DATA_DIR / "arb-opportunities.json"
           _atomic_write_json(arb_path, arb_log)
       ```

    6. **Update `config/bots-config.json`** -- add new fields to strategy section:
       ```json
       "orderbookSignals": true,
       "arbEnabled": false,
       "arbDailyCapPct": 0.20
       ```
       Add these three keys to the "strategy" object. Keep `arbEnabled: false` by default (user enables when ready).

    7. **Update scan summary** to include arb and orderbook stats in the final log output.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && python3 -c "
import sys; sys.path.insert(0, 'src/kalshi')
from strategy_engine import OrderbookSignals, CrossPlatformArb, ArbOpportunity
# Verify OrderbookSignals works
ob = OrderbookSignals()
adj = ob.compute_edge_adjustment(0.65, 0.05, 1.0)
assert 0.5 <= adj <= 1.5, f'adjustment out of range: {adj}'
assert ob.compute_edge_adjustment(0.5, 0.15, 4.0) == 0.0, 'should return 0 for vol spike'
# Verify ArbOpportunity
opp = ArbOpportunity('KXBTC-TEST', 'poly123', 40, 0.48, 0.08, 5, 'buy_kalshi')
assert opp.locked_profit == 5
print('Integration smoke test PASSED')
" && pytest tests/test_strategy_engine.py -x -v 2>&1 | tail -20</automated>
  </verify>
  <done>
    - OrderbookSignals integrated into find_longshot_sells and find_longshot_buys with graceful degradation on API errors
    - CrossPlatformArb scan added as Strategy 3 in run_scan(), places Kalshi-side trades only
    - Arb opportunities logged to data/arb-opportunities.json
    - Config updated with orderbookSignals, arbEnabled, arbDailyCapPct
    - All tests pass, no regressions
    - Volume acceleration defaults to 1.0 (no historical baseline yet -- future enhancement)
  </done>
</task>

</tasks>

<verification>
1. `pytest tests/test_strategy_engine.py -x -v` -- all tests pass including new OrderbookSignals and CrossPlatformArb tests
2. `pytest tests/ -x --timeout=120` -- no regressions across full test suite
3. `python3 -c "from strategy_engine import OrderbookSignals, CrossPlatformArb, ArbOpportunity"` -- imports succeed
4. `python3 -c "import json; c=json.load(open('config/bots-config.json')); assert 'orderbookSignals' in c['strategy']"` -- config updated
</verification>

<success_criteria>
- OrderbookSignals class computes imbalance ratio, spread multiplier, volume signal, and combined edge adjustment from REST orderbook snapshots
- CrossPlatformArb class detects arb opportunities using fuzzy matching (adapted from cross-platform-arb.py) with locked profit computation and 20% daily cap
- Strategy trader scan loop applies microstructure signals to edge estimates before sizing
- Arb scan runs after longshot strategies, places Kalshi-side trades only, logs opportunities
- All new behavior covered by TDD tests (RED then GREEN)
- No scipy/numpy -- only math module
- Zero test regressions
</success_criteria>

<output>
After completion, create `.planning/quick/10-strategy-bot-websocket-orderbook-signals/10-SUMMARY.md`
</output>
