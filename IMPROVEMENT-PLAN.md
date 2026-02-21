# Kalshi Trading System — Improvement Plan

Generated: 2026-02-21 | Status: Draft

---

## Table of Contents

1. [Critical Fixes (Do Immediately)](#phase-0-critical-fixes)
2. [Phase 1: Config-Only Wins](#phase-1-config-only-wins)
3. [Phase 2: Core Infrastructure](#phase-2-core-infrastructure)
4. [Phase 3: Probability Model Upgrades](#phase-3-probability-model-upgrades)
5. [Phase 4: Risk Management](#phase-4-risk-management)
6. [Phase 5: New Market Expansion](#phase-5-new-market-expansion)
7. [Phase 6: Operational Excellence](#phase-6-operational-excellence)
8. [Dependencies & Requirements](#dependencies--requirements)
9. [Implementation Timeline](#implementation-timeline)

---

## Phase 0: Critical Fixes

These are bugs that are actively costing money or causing crashes. Fix before anything else.

### 0.1 — Fix NWS Station ID Mismatches (ACTIVELY LOSING MONEY)

**Problem**: Three NWS station IDs in `config/kalshi-monitor-config.json` don't match Kalshi's settlement sources. Your bots are comparing against the wrong thermometer.

**Kalshi uses these stations** (from their settlement docs):
| City | Current (WRONG) | Correct | Difference |
|------|-----------------|---------|------------|
| NY   | KJFK (JFK Airport) | KNYC (Central Park) | 2-5°F (heat island) |
| CHI  | KORD (O'Hare)   | KMDW (Midway)       | 1-3°F |
| HOU  | KIAH (IAH)      | KHOU (Hobby)        | 1-2°F |

**Impact**: When your model uses KJFK reading of 82°F but Kalshi settles on KNYC reading of 85°F, your probability estimate is off by ~1 sigma. This systematically biases your edge calculations for 3 of 8 cities.

**Files to change**:
- `config/kalshi-monitor-config.json` — update `sources.nws.stations`
- `config/kalshi-config.json` — verify city lat/lon correspond to station locations (Central Park, not JFK)

**Effort**: 5 minutes. Config edit only.

### 0.2 — Fix NameError in source-monitor.py

**Problem**: `source-monitor.py:598` references undefined variable `today`.

```python
# Line 598 — BROKEN:
log.info(f"  No KXHIGH markets settling today ({today})")

# FIX:
log.info(f"  No KXHIGH markets settling today ({city_today})")
```

**Files**: `src/kalshi/source-monitor.py`
**Effort**: 1 line fix.

### 0.3 — Fix Naive vs Aware Datetime Comparison

**Problem**: `kalshi_auth.py:415,435` — `RecentTradeTracker` compares `datetime.datetime.now()` (naive) with `datetime.datetime.fromisoformat(ts_str)` which may produce aware datetimes. Python 3.12+ raises `TypeError`.

**Fix**: Use `datetime.datetime.now()` consistently (strip timezone from stored timestamps), OR use aware datetimes everywhere.

```python
# In RecentTradeTracker._load(), line 422:
ts = datetime.datetime.fromisoformat(ts_str)
# Add after:
if ts.tzinfo is not None:
    ts = ts.replace(tzinfo=None)
```

**Files**: `src/kalshi/kalshi_auth.py`
**Effort**: 3-line fix.

### 0.4 — Fix Box Office Gross Heuristic

**Problem**: `entertainment-bot.py:189` — `if val < 1000: val *= 1_000_000` turns a legit $800 daily gross into $800M.

**Fix**: Tighten the guard:
```python
# Replace: if val < 1000: val *= 1_000_000
# With:    if 0.5 < val < 500: val *= 1_000_000
```

Also fix the same pattern at line 224.

**Files**: `src/kalshi/entertainment-bot.py`
**Effort**: 2-line fix.

### 0.5 — Reduce Allocator Balance Cache to 10 Seconds

**Problem**: 60-second balance cache means bots can over-allocate immediately after another bot trades.

**Fix**: `capital_allocator.py:254` — change `60` to `10`.

```python
# Line 254:
if self._cached_balance is not None and (now - self._balance_fetched_at) < 10:
```

**Trade-off**: More frequent balance API calls (~6x more), but the API rate limit is generous enough for this.

**Files**: `src/kalshi/capital_allocator.py`
**Effort**: 1-line fix.

---

## Phase 1: Config-Only Wins

Zero code changes. Just toggle existing features.

### 1.1 — Enable Ensemble Forecasting

**What**: Turn on GFS+ECMWF+ICON Bayesian Model Averaging for weather markets.

**Why**: Ensemble reduces forecast MAE by ~15-20% vs single-model GFS. The code is already written, tested, and ready.

**Config change** in `config/kalshi-config.json`:
```json
"ensemble": {
  "enabled": true,
  "models": ["gfs", "ecmwf", "icon"],
  "weights": { "gfs": 0.40, "ecmwf": 0.40, "icon": 0.20 }
}
```

**Risk**: Slightly more API calls to Open-Meteo (3 models instead of 1). Open-Meteo rate limits are generous.

**Expected impact**: +15-20% weather model accuracy → better edge estimates → higher win rate.

### 1.2 — Activate Cross-Platform Arb Execution (Phase 2)

**What**: Enable soft arb — buy Kalshi side only when Polymarket shows significant price discrepancy.

**Config change** in `config/bots-config.json`:
```json
"cross_platform_arb": {
  "executionEnabled": true,
  "maxTradeAmount": 5,
  "minSpreadPct": 0.03
}
```

**Prerequisite**: Run the arb monitor in logging mode for 1 week first. Review `data/arb-spread-log.json` to verify match quality. Only enable execution after confirming matches are correct.

**Risk**: Fuzzy matching could match wrong markets. The `validate_match()` function requires both text similarity AND matching numerical thresholds, which mitigates this. Start with $5 max trade to limit downside.

**Expected impact**: New edge source with mechanical (non-model) basis.

### 1.3 — Reduce Crypto Scan Interval

**Config change** in `config/bots-config.json`:
```json
"crypto": {
  "scanIntervalMinutes": 1
}
```

**Why**: 5-minute intervals miss short-lived mispricings. 1-minute is still conservative (WebSocket in Phase 2 will be better).

**Risk**: More API calls. Kalshi rate limit is ~10 req/sec, so 1-min polling is fine.

---

## Phase 2: Core Infrastructure

### 2.1 — Fill Monitoring & Order Lifecycle Management

**Problem**: Orders are placed and assumed to fill. No mechanism to detect unfilled orders, reprice, or reclaim capital.

**Design**:
```
TradeManager.place_order()
  → POST /portfolio/orders
  → Returns order_id
  → NEW: Start background monitor for this order_id

OrderMonitor (new class in kalshi_auth.py):
  - Tracks all pending order_ids
  - Every 30 seconds: GET /portfolio/orders/{id}
  - If status == "resting" and age > 5 minutes:
    → If edge still exists: reprice (cancel + resubmit closer to market)
    → If edge gone: cancel
  - If status == "executed": update trade record with fill price
  - If status == "canceled" (by exchange): log and free capital
```

**Files**: `src/kalshi/kalshi_auth.py` (new OrderMonitor class), all bots (integrate monitor)
**Effort**: ~200 LOC
**Expected impact**: +20-30% capital efficiency

### 2.2 — WebSocket Integration

**What**: Add WebSocket client for real-time market data streaming.

**Endpoints**:
- Production: `wss://trading-api.kalshi.com/trade-api/ws/v2`
- Demo: `wss://demo-api.kalshi.co/trade-api/ws/v2`

**Authentication**: Same RSA-PSS signing as REST API, sent during WebSocket handshake.

**Heartbeat**: Server sends Ping every 10 seconds. Client must respond with Pong.

**Design**:
```python
# New file: src/kalshi/kalshi_ws.py

class KalshiWebSocket:
    """Real-time market data streaming via WebSocket."""

    def __init__(self, api_key, private_key, mode="demo"):
        ...

    async def connect(self):
        """Establish authenticated WebSocket connection."""
        ...

    async def subscribe(self, channels, tickers):
        """Subscribe to orderbook/trade/status channels for specific tickers."""
        ...

    def on_orderbook_update(self, callback):
        """Register callback for order book changes."""
        ...

    def on_trade(self, callback):
        """Register callback for trade executions."""
        ...
```

**Priority integration**: Crypto bot first (fastest-moving markets, most benefit from real-time data), then market maker, then source monitor.

**Dependency**: `websockets` Python package (add to requirements.txt).
**Effort**: ~250 LOC for the client, ~100 LOC per bot integration
**Expected impact**: 10-60x faster opportunity capture for crypto; enables market maker activation

### 2.3 — Shared Market Data Layer

**Problem**: 10 bots each call `get_all_markets()` independently.

**Design**: Single-writer market cache file updated by one process:

```
data/market-cache.json (updated every 60 seconds by a dedicated fetcher)
  {
    "updated_at": "2026-02-21T10:00:00",
    "markets": { "KXHIGH": [...], "KXBTC": [...], ... }
  }

All bots read from this file instead of calling the API directly.
```

**Alternative**: If WebSocket is implemented (2.2), the WS client can maintain an in-memory order book that all bots read from.

**Files**: New `src/kalshi/market_cache.py`, modify all bots to read from cache
**Effort**: ~150 LOC
**Expected impact**: 5-10x reduction in API calls

### 2.4 — Consolidate Shared Ticker Parsers

**Problem**: `parse_temp_ticker()` is copy-pasted in 3 files.

**Fix**: Move to `kalshi_auth.py` or a new `src/kalshi/ticker_utils.py`. Import everywhere.

**Files**: `src/kalshi/kalshi_auth.py`, `weather-bot.py`, `source-monitor.py`, `position-monitor.py`
**Effort**: ~30 LOC (mostly deleting duplicates)

### 2.5 — Consolidate HDD Parsing

**Problem**: `entertainment-bot.py` and `source-monitor.py` have duplicate HDD regex parsers. `hdd-scraper.py` has a better CMS-based approach.

**Fix**: Create `src/kalshi/hdd_parser.py` with the best parser from hdd-scraper.py. Import in entertainment-bot and source-monitor.

**Files**: New `src/kalshi/hdd_parser.py`, modify `entertainment-bot.py`, `source-monitor.py`
**Effort**: ~100 LOC (mostly moving + deleting)

---

## Phase 3: Probability Model Upgrades

### 3.1 — Student's t Distribution for Weather

**Problem**: Gaussian CDF underestimates tail probabilities. NWS forecast errors are leptokurtic (fat-tailed).

**Implementation**: Replace `_norm_cdf` with a Student's t CDF (~6 degrees of freedom) in `probability.py`. No scipy needed — use the regularized incomplete beta function:

```python
def _student_t_cdf(x, df=6):
    """Student's t CDF using the regularized incomplete beta function.
    More accurate than Gaussian for fat-tailed NWS forecast errors."""
    t2 = x * x
    p = 0.5 * _regularized_beta(df / (df + t2), df / 2, 0.5)
    return 1.0 - p if x > 0 else p

def _regularized_beta(x, a, b, n_terms=100):
    """Regularized incomplete beta function via continued fraction."""
    # ~20 lines, well-known numerical recipe
    ...
```

**Calibration**: Use `df` as a calibration parameter. Start with df=6 (matches NWS kurtosis ~4.5), tune via backtest.

**Files**: `src/kalshi/probability.py`
**Effort**: ~40 LOC
**Expected impact**: +5-10% Brier score improvement, better bracket market performance

### 3.2 — Sublinear Sigma Scaling

**Problem**: `sigma = intercept + slope * days_out` (linear). Reality is sublinear.

**Fix**:
```python
# Current:
sigma = intercept + slope * max(0, days_out)

# Better:
sigma = intercept + slope * math.sqrt(max(0, days_out))
```

**Caveat**: This changes probability estimates for all weather markets. Run backtest before/after to verify improvement.

**Files**: `src/kalshi/probability.py` (weather_probability function)
**Effort**: 1-line change + backtest validation

### 3.3 — Crypto Mean-Reversion Model

**Problem**: GBM overstates move probability at short horizons (< 4h). BTC/ETH show mean-reverting behavior intraday.

**Implementation**: Add Ornstein-Uhlenbeck option to `crypto_price_probability()`:

```python
def crypto_price_probability(..., use_ou=True, ou_theta=None):
    if use_ou and time_horizon_minutes < 240:
        # OU mean-reversion model for short horizons
        theta = ou_theta or 0.05  # mean-reversion speed (calibrate from data)
        # Effective vol is lower than GBM at short horizons
        effective_vol = sigma * math.sqrt((1 - math.exp(-2 * theta * T)) / (2 * theta * T))
        ...
```

**Calibration**: Compute autocorrelation of 5-minute BTC returns from Coinbase. Typical theta ~ 0.03-0.08 for BTC.

**Files**: `src/kalshi/probability.py`
**Effort**: ~30 LOC
**Expected impact**: Better intraday crypto probability → fewer false positives

### 3.4 — CPI Sigma Empirical Calibration

**Problem**: `cpi_nowcast_sigma()` is entirely heuristic.

**Implementation**:
1. Download historical CPI releases from BLS (public CSV)
2. Download historical Cleveland Fed nowcast values (scrape or manual)
3. Compute `nowcast_error = actual - nowcast` for each release
4. Fit sigma as a function of days_to_release
5. Store in `config/calibration.json` under `cpi.sigma_by_days`

**Effort**: ~100 LOC (one-time calibration script)
**Files**: New `scripts/calibrate-cpi-sigma.py`, update `config/calibration.json`

### 3.5 — Dynamic Ensemble Weight Updates

**Problem**: BMA weights are static. GFS/ECMWF/ICON relative accuracy varies by city and season.

**Implementation**: After each settlement, compute which model was closest. Update weights in calibration.json using exponential moving average:

```python
# In calibrate-sigma.py or a new auto-calibration module:
def update_ensemble_weights(city, model_errors):
    """Update BMA weights based on recent model performance."""
    for model in ["gfs", "ecmwf", "icon"]:
        # Lower MAE → higher weight
        inv_mae = 1.0 / (model_errors[model] + 0.1)
        weights[model] = 0.9 * weights[model] + 0.1 * inv_mae
    # Normalize
    total = sum(weights.values())
    return {k: v/total for k, v in weights.items()}
```

**Files**: `scripts/calibrate-sigma.py`, `config/calibration.json`
**Effort**: ~50 LOC

---

## Phase 4: Risk Management

### 4.1 — Entry-Price-Relative Stop Loss

**Problem**: Fixed 20c stop-loss is meaningless for high-price entries (NO at 97c).

**Implementation**: Position monitor should track entry price and compute stop as percentage of cost:

```python
# In position-monitor.py:
def evaluate_stop_loss(position, market, entry_price_cents):
    # Stop at 40% loss from entry
    stop_price = int(entry_price_cents * 0.60)
    current_bid = market.get("yes_bid", 0)  # or no_bid
    if current_bid <= stop_price:
        return exit_signal
```

**Requires**: Store entry price in trade records (already done via golden record `price_cents` field).

**Files**: `src/kalshi/position-monitor.py`
**Effort**: ~30 LOC

### 4.2 — Trailing Stop

**Implementation**: Track peak bid since entry for each position:

```python
# New state file: data/position-peaks.json
# { "KXHIGH...": { "peak_bid": 92, "entry_price": 65, "side": "yes" } }

def evaluate_trailing_stop(position, market, peak_info):
    current_bid = market.get("yes_bid", 0)
    # Update peak
    if current_bid > peak_info["peak_bid"]:
        peak_info["peak_bid"] = current_bid
    # Trail: exit if dropped 10c from peak (and peak > entry + 10c)
    if (peak_info["peak_bid"] - current_bid >= 10 and
        peak_info["peak_bid"] >= peak_info["entry_price"] + 10):
        return exit_signal
```

**Files**: `src/kalshi/position-monitor.py`
**Effort**: ~50 LOC

### 4.3 — Disable Take-Profit for Confirmed Info-Arb

**Problem**: Exiting at 85c on a confirmed outcome (HDD data showing definitive answer) leaves 15c on the table.

**Fix**: Check trade record's `source_bot`. If source is `source-monitor` with confidence > 95%, skip take-profit evaluation and hold to settlement.

**Files**: `src/kalshi/position-monitor.py`
**Effort**: ~10 LOC

### 4.4 — Shared Circuit Breaker

**Implementation**: Store circuit breaker state in `data/allocator-state.json`:

```json
{
  "circuit_breaker": {
    "failures": 3,
    "opened_at": "2026-02-21T10:00:00",
    "max_failures": 5
  }
}
```

All bots check this before placing orders. Any bot that hits a Kalshi API error increments the shared counter.

**Files**: `src/kalshi/capital_allocator.py`, `src/kalshi/kalshi_auth.py`
**Effort**: ~40 LOC

### 4.5 — Correlated City Exposure Limits

**Implementation**: Add region grouping to the allocator:

```python
CITY_REGIONS = {
    "SOUTH_TX": ["HOU", "AUS"],
    "NORTHEAST": ["NY", "PHIL"],
}
MAX_REGION_FRACTION = 0.15  # 15% of bankroll per region
```

**Files**: `src/kalshi/capital_allocator.py`
**Effort**: ~20 LOC

### 4.6 — Max Concurrent Positions Limit

**Implementation**: Before approving a budget, check current position count:

```python
# In PortfolioAllocator.request_budget():
positions = self.client.get("/portfolio/positions")
if len(positions.get("market_positions", [])) >= MAX_POSITIONS:
    return BudgetResponse(False, reason="max positions reached")
```

**Config**: Add `"maxConcurrentPositions": 20` to bots-config.json.

**Files**: `src/kalshi/capital_allocator.py`
**Effort**: ~15 LOC

---

## Phase 5: New Market Expansion

### 5.1 — Sports Markets (Longshot Sells)

**What**: Extend strategy trader to scan KXNBA, KXNFL, KXNHL, KXMLB tickers.

**Why**: Sports has the strongest longshot bias (amplitude=0.65). The Becker model and `classify_ticker_category()` already handle sports.

**Implementation**: Add sports prefixes to strategy trader's market scan:

```python
# In strategy-trader.py, add to market fetch:
for prefix in ["KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXUFC", "KXNCAA"]:
    markets.extend(client.get_all_markets(prefix=prefix))
```

**No model needed** — longshot bias is a statistical anomaly, not a forecasting problem.

**Effort**: ~10 LOC
**Expected impact**: 2-3x more longshot opportunities per scan

### 5.2 — Fed Rate Decision Markets (KXFED)

**What**: Trade KXFED markets using CME FedWatch implied probabilities.

**Data source**: CME Group publishes FedWatch probabilities. These can be scraped from `cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html` or derived from 30-day Fed Funds futures prices (available from FRED API).

**Implementation**:
```python
# New file: src/kalshi/fed-rate-bot.py
# 1. Fetch CME FedWatch implied probabilities
# 2. Fetch KXFED markets from Kalshi
# 3. Compare: if |CME_prob - Kalshi_price| > threshold, trade
# 4. Use half_kelly for sizing
```

**Edge quality**: Very high. CME FedWatch reflects billions in positioning; Kalshi reflects retail opinion. Divergence is reliable alpha.

**Effort**: ~200 LOC (new bot)
**Dependency**: CME FedWatch data access (see Dependencies section)

### 5.3 — Gas Price Markets (KXGAS)

**What**: You already fetch AAA gas prices in economics-bot. If Kalshi lists gas price markets, add a gas price trading function.

**Implementation**: Add KXGAS prefix to economics bot's market scan. Use same CDF model with gas price volatility (~2% weekly std dev).

**Effort**: ~50 LOC added to economics-bot.py

### 5.4 — Daemonize Strategy Trader

**What**: Convert from one-shot to daemon with 15-minute scan interval.

**Why**: New markets are listed throughout the day. Longshot mispricing is often highest at listing time.

**Implementation**:
```python
# Add to strategy-trader.py:
while True:
    main()
    time.sleep(900)  # 15 minutes
```

Also add health heartbeat and proper signal handling (already imported but not used in daemon mode).

**Effort**: ~15 LOC

---

## Phase 6: Operational Excellence

### 6.1 — Process Supervisor

**What**: Single orchestrator that manages all bot processes.

**Design**:
```python
# New file: scripts/supervisor.py
BOTS = [
    {"name": "weather", "cmd": "python3 src/kalshi/weather-bot.py", "enabled": True},
    {"name": "crypto", "cmd": "python3 src/kalshi/crypto-bot.py", "enabled": True},
    ...
]

class Supervisor:
    def start_all(self): ...
    def stop_all(self): ...
    def restart_crashed(self): ...
    def health_check(self): ...
    def status(self): ...  # prints table of running bots
```

**Effort**: ~200 LOC
**Benefit**: No more manually managing 10+ terminal windows/screen sessions

### 6.2 — Daily Automated Backtest

**What**: Cron job runs `npm run backtest -- --save` daily at midnight.

**Implementation**: Add to Mac Mini crontab:
```
0 0 * * * cd /path/to/kalshi-trading && npm run backtest -- --save >> data/logs/backtest-cron.log 2>&1
```

**Bonus**: Add a drift detector that alerts when Brier score degrades > 10% from baseline.

**Effort**: ~30 minutes setup

### 6.3 — Daily P&L Notification

**What**: Extend `scripts/daily-report.py` to send a WhatsApp summary.

**Implementation**: Already have `notify_whatsapp()` in kalshi_auth.py. Wire it to the daily report.

**Effort**: ~20 LOC

### 6.4 — Improve Cleveland Fed Scraping Robustness

**Problem**: HTML regex scraping is fragile, and no structured API exists.

**Improvements**:
1. Cache last successful nowcast values in `data/econ-nowcast-cache.json`
2. Add BeautifulSoup parsing as primary (regex as fallback)
3. Add multiple fallback data sources (other Fed bank estimates)
4. Alert when parsing fails 3+ times consecutively

**Files**: `src/kalshi/economics-bot.py`
**Effort**: ~60 LOC

---

## Dependencies & Requirements

### Things Needed From You

| Item | Phase | Urgency | Notes |
|------|-------|---------|-------|
| **Verify NWS station IDs** | 0.1 | IMMEDIATE | Confirm Kalshi uses KNYC/KMDW/KHOU by checking their settlement docs or a recent settlement |
| **Review arb spread log** | 1.2 | Before enabling | Run `npm run arb` for 1 week, review `data/arb-spread-log.json` match quality |
| **Set KALSHI_CONFIRM_PRODUCTION=yes** | All | When ready for live | Only needed when switching from demo to production |
| **CME FedWatch data access** | 5.2 | Phase 5 | Check if CME Group requires registration for FedWatch data. Free tier may suffice. |
| **Polygon wallet** (optional) | Future | Phase 3 arb | Only needed for full two-leg cross-platform arb (Phase 3 of arb bot) |

### New Python Dependencies

| Package | Phase | Purpose |
|---------|-------|---------|
| `websockets` | 2.2 | Kalshi WebSocket client |
| `beautifulsoup4` | 6.4 | Already in requirements.txt |

### No External Dependencies Needed

- Ensemble forecasting: Open-Meteo API (free, no auth)
- Cross-platform arb: Polymarket API (free, no auth)
- Sports markets: Already supported by existing Becker model
- Fill monitoring: Uses existing Kalshi REST API
- All probability model changes: Pure math, no external data

---

## Implementation Timeline

### Week 1: Critical Fixes + Config Wins
- [ ] **Day 1**: Phase 0 (all 5 critical fixes) — ~30 minutes
- [ ] **Day 1**: Phase 1.1 (enable ensemble) — config change
- [ ] **Day 1**: Phase 1.3 (reduce crypto interval) — config change
- [ ] **Day 2**: Phase 2.4 (consolidate ticker parsers) — cleanup
- [ ] **Day 2**: Phase 5.1 (add sports to strategy trader) — 10 LOC
- [ ] **Day 3**: Phase 5.4 (daemonize strategy trader) — 15 LOC
- [ ] **Day 3-7**: Monitor ensemble performance, run arb in logging mode

### Week 2: Fill Monitoring + Position Management
- [ ] Phase 2.1 (fill monitoring / order lifecycle) — ~200 LOC
- [ ] Phase 4.1 (entry-price-relative stop loss) — ~30 LOC
- [ ] Phase 4.2 (trailing stop) — ~50 LOC
- [ ] Phase 4.3 (disable take-profit for confirmed info-arb) — ~10 LOC

### Week 3: Probability Models
- [ ] Phase 3.1 (Student's t distribution) — ~40 LOC
- [ ] Phase 3.2 (sublinear sigma scaling) — 1 line + backtest
- [ ] Phase 3.4 (CPI sigma calibration) — ~100 LOC
- [ ] Run full backtest, compare Brier scores before/after

### Week 4: Infrastructure + Risk
- [ ] Phase 1.2 (activate cross-platform arb, if spread log looks good)
- [ ] Phase 2.2 (WebSocket integration) — ~350 LOC
- [ ] Phase 4.4 (shared circuit breaker) — ~40 LOC
- [ ] Phase 4.5 (correlated city limits) — ~20 LOC

### Week 5-6: New Markets + Operations
- [ ] Phase 5.2 (Fed rate bot) — ~200 LOC
- [ ] Phase 6.1 (process supervisor) — ~200 LOC
- [ ] Phase 6.2 (daily automated backtest) — cron setup
- [ ] Phase 6.3 (daily P&L notification) — ~20 LOC

### Ongoing
- [ ] Phase 3.3 (crypto mean-reversion) — after WebSocket is live
- [ ] Phase 3.5 (dynamic ensemble weights) — after accumulating more settlement data
- [ ] Phase 2.3 (shared market data layer) — after WebSocket is live
- [ ] Phase 2.5 (consolidate HDD parsers) — when convenient

---

## Expected Cumulative Impact

| Phase | Estimated ROI Improvement | Confidence |
|-------|--------------------------|------------|
| Phase 0 (critical fixes) | +10-15% (station IDs alone) | High |
| Phase 1 (config wins) | +15-20% (ensemble) | High |
| Phase 2 (infrastructure) | +20-30% (fill monitoring) | Medium-High |
| Phase 3 (models) | +5-10% (Student's t + calibration) | Medium |
| Phase 4 (risk) | +5-10% (better exits) | Medium |
| Phase 5 (new markets) | +20-40% (more opportunities) | Medium |

**Note**: These are rough estimates. The true impact depends on bankroll size, market conditions, and execution quality. The most reliable improvements are Phase 0 and Phase 1 — they fix known problems and activate already-built features.
