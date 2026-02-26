# Architecture

**Analysis Date:** 2026-02-26

## Pattern Overview

**Overall:** Microservice-style daemon architecture with centralized shared authentication, probability models, and capital allocation.

**Key Characteristics:**
- All bots are independent Python daemons that share common infrastructure modules
- Synchronous REST API calls to Kalshi trading API via authenticated HTTP client
- Event-driven trade execution (scan interval triggers evaluation → decision → execution)
- File-based state management with atomic writes and fcntl locking for cross-process coordination
- Portfolio-wide risk controls via shared allocator state file
- Separation of concerns: API logic (`kalshi_auth.py`), probability models (`probability.py`), capital allocation (`capital_allocator.py`)

## Layers

**API & Authentication (`kalshi_auth.py`):**
- Purpose: Handle Kalshi API communication, RSA-PSS signing, retry logic, session management
- Location: `src/kalshi/kalshi_auth.py`
- Contains: `KalshiClient` (API requests, market caching), `TradeManager` (order placement with guardrails), `CircuitBreaker` (API failure tracking), `RecentTradeTracker` (dedup), trade file I/O, logging setup
- Depends on: requests library, cryptography, Python stdlib
- Used by: All bots and scripts; core dependency layer

**Probability Models (`probability.py`):**
- Purpose: Calculate win probabilities and Kelly sizing for market outcomes
- Location: `src/kalshi/probability.py`
- Contains: CDF-based weather model, info-arb model, NWS arbitrage, economics nowcast, crypto GBM, Kelly sizing functions, calibration lazy-loading
- Depends on: math stdlib, config/calibration.json
- Used by: All trading bots and strategy modules

**Capital Allocator (`capital_allocator.py`):**
- Purpose: Enforce portfolio-level risk limits and coordinate capital across bots
- Location: `src/kalshi/capital_allocator.py`
- Contains: `PortfolioAllocator` (budget approval, concentration limits, daily loss cap), circuit breaker integration, per-bot priority tiers
- Depends on: kalshi_auth (for client, shared state file)
- Used by: All bots (must request budget before trading)

**Utilities:**
- `ticker_utils.py`: Parses market tickers (extract city, threshold, direction)
- `hdd_parser.py`: HITS Daily Double chart/article parsing for album sales data
- `polymarket_client.py`: Polymarket CLOB API client for cross-platform arbitrage

**Trading Bots (src/kalshi/*.py):**
- Purpose: Autonomous market evaluation and execution
- Location: `src/kalshi/{weather,entertainment,crypto,economics,strategy,beatrelease,position,market-maker,cross-platform-arb}-{bot,trader,monitor,scanner}.py`
- Pattern: Each bot independently (1) fetches data, (2) calculates probability via shared models, (3) requests capital budget, (4) places order via trade manager
- Depends on: kalshi_auth, probability, capital_allocator, data sources (APIs)
- Lifecycle: One-shot (strategy, hdd-scraper) or daemon (weather: 30min, entertainment: 15min, crypto: 5min)

**Observability & Operations (`scripts/*.py`):**
- Purpose: Monitor, analyze, audit, and manage running bots
- Location: `scripts/`
- Modules:
  - `supervisor.py` — Process manager with heartbeat monitoring, crash detection, restart logic
  - `dashboard.py` — FastAPI web UI (port 3456) with real-time bot status, P&L, trade history
  - `audit.py` — Comprehensive math audit (edge validation, sizing, slippage, win rate)
  - `backtest.py` — Walk forward evaluation, Brier score calculation, parameter sensitivity
  - `reconcile-trades.py` — Match trades to settlement outcomes, fill prices
  - `analyze-performance.py` — P&L attribution, Sharpe ratio, trade quality metrics
  - `calibrate-sigma.py` — Grid-search optimal probability model parameters
  - `s3-sync.sh` — Cross-machine trade log synchronization

## Data Flow

**Real-time Trading Loop:**

1. **Scan** — Bot runs on a schedule (e.g., 30 min for weather)
2. **Fetch** — Bot calls KalshiClient to retrieve markets, external data (weather API, HDD Sanity CMS, NWS)
3. **Model** — Bot calls probability functions (e.g., `weather_probability()`) to estimate win rates
4. **Allocate** — Bot calls `allocator.request_budget()` to get position size within portfolio limits
5. **Size** — Bot calls `half_kelly()` with requested budget to compute contract count and limit price
6. **Execute** — Bot calls `trade_manager.place_order()` which:
   - Checks kill switch (`data/HALT_TRADING`)
   - Checks circuit breaker status (from `data/allocator-state.json`)
   - Enforces daily trade/loss limits
   - Checks dedup (6-hour cooldown by default)
   - Validates balance vs. cost
   - Signs and submits order via KalshiClient.post("/portfolio/orders")
   - Writes atomic golden record to trade log (JSON)
7. **Record** — Trade logged to bot-specific file (e.g., `data/kalshi-trades.json`) with full context (edge, sizing, market snapshot, reasoning)

**Decision Logging:**

- Each bot maintains a `*-decisions.json` file documenting every market evaluated (trade or skip)
- Structured: `{timestamp, ticker, evaluated_probability, edge, decision, reasoning, model_inputs}`
- Consumed by dashboard for decision audit trail

**State Management:**

- **Trade logs** — JSON files in `data/` (one per bot), atomically written, file-locked for concurrent access
- **Allocator state** — `data/allocator-state.json` shared by all bots, manages circuit breaker + daily loss tracking
- **Health state** — `data/health-state.json` heartbeats from all bots, source freshness timestamps
- **Market cache** — `data/market-cache.json` cross-process cache of Kalshi market data (60s TTL)
- **Scan summaries** — `data/scan-summaries.json` append-only log of bot scan outcomes (for audit)

**Observability Files (in data/):**

| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `health-state.json` | All bots (HealthCheckMonitor) | Supervisor, dashboard | Bot heartbeats, error counts, source freshness |
| `allocator-state.json` | PortfolioAllocator | All bots (TradeManager) | Circuit breaker status, daily loss tracking |
| `*-trades.json` | TradeManager.place_order() | Dashboard, audit, backtest | Golden record of all executed trades |
| `*-decisions.json` | Each bot (ScanSummary) | Dashboard | Every market evaluated (trade or skip) |
| `scan-summaries.json` | ScanSummary.finalize() | Dashboard, audit | Scan completion markers and summary stats |
| `market-cache.json` | KalshiClient.get_all_markets() | All bots (KalshiClient) | Market metadata cache (60s TTL) |

## Key Abstractions

**KalshiClient:**
- Purpose: Authenticated API communication with retry logic
- Examples: `src/kalshi/kalshi_auth.py:122-346`
- Pattern: Stateful session object; RSA-PSS signs every request with fresh timestamp; retries idempotent ops (GET), safe-retry 429 all methods, never-retry POST/DELETE on failures

**TradeManager:**
- Purpose: One-stop-shop for placing orders with all safety guardrails
- Examples: `src/kalshi/kalshi_auth.py:911-1200+`
- Pattern: Call `place_order(ticker, side, price_cents, count, reasoning, **extra_fields)` once; method enforces all checks (kill switch, circuit breaker, daily limits, dedup, cost cap, staleness, balance) and logs atomically

**PortfolioAllocator:**
- Purpose: Coordinate capital across bots; prevent double-trading same ticker
- Examples: `src/kalshi/capital_allocator.py:100-300+`
- Pattern: Bot calls `request_budget(bot_name, ticker, edge, confidence)` → allocator returns `BudgetRequest(approved, max_cost_cents, bankroll_cents)` based on daily loss cap, per-bot fraction, per-ticker concentration, per-city exposure

**ScanSummary:**
- Purpose: Track outcomes of a bot scan (markets evaluated, trades made, errors)
- Pattern: `summary = ScanSummary(bot_name)` → bot evaluates markets → `summary.trade(ticker, decision)` or `summary.skip(ticker, reason)` → `summary.finalize()` writes to `data/*-decisions.json` and `data/scan-summaries.json`

**CircuitBreaker:**
- Purpose: Auto-halt trading on repeated API failures
- Pattern: Each API call recorded via `breaker.record_success()` or `record_failure()`. After N consecutive failures, `breaker.is_open()` returns True and bots skip trading. Auto-resets after timeout.

**RecentTradeTracker:**
- Purpose: Prevent duplicate trades on same ticker within a window (default 6h)
- Pattern: Load from trade log; check `is_recent(ticker)` before trading; call `record(ticker)` after execution

## Entry Points

**Weather Bot:**
- Location: `src/kalshi/weather-bot.py`
- Triggers: npm script `npm run weather` or cron via supervisor
- Responsibilities: Fetch Open-Meteo GFS/ECMWF/ICON forecasts, evaluate KXHIGH temperature markets, place trades
- Interval: ~30 minutes

**Entertainment Bot:**
- Location: `src/kalshi/entertainment-bot.py`
- Triggers: npm script `npm run entertainment` or supervisor
- Responsibilities: Scrape HITS Daily Double for album sales, evaluate album sales markets, place trades
- Interval: ~15 minutes

**Source Monitor:**
- Location: `src/kalshi/source-monitor.py`
- Triggers: npm script `npm run monitor` or supervisor
- Responsibilities: Information arbitrage — monitor NWS actual temps, box office, HDD charts; trade when data conflicts with market prices
- Interval: 10-30 min depending on source

**Crypto Bot:**
- Location: `src/kalshi/crypto-bot.py`
- Triggers: npm script `npm run crypto`
- Responsibilities: BTC/ETH price trading via log-normal GBM model
- Interval: ~5 minutes

**Economics Bot:**
- Location: `src/kalshi/economics-bot.py`
- Triggers: npm script `npm run economics`
- Responsibilities: CPI/GDP/Jobs nowcast trading via Cleveland Fed nowcast API
- Interval: ~6 hours

**Beatrelease Scanner:**
- Location: `src/kalshi/beatrelease-scanner.py`
- Triggers: npm script via `python3 src/kalshi/beatrelease-scanner.py` (daemon) or `--once` flag
- Responsibilities: Scan BeatRelease blog for music release gossip, LLM classification, predictive trading
- Interval: ~4 hours

**Strategy Trader:**
- Location: `src/kalshi/strategy-trader.py`
- Triggers: npm script `npm run strategy` or cron (one-shot)
- Responsibilities: Longshot bias arbitrage + near-settlement arb; evaluates all open markets
- Interval: On-demand or scheduled

**Position Monitor:**
- Location: `src/kalshi/position-monitor.py`
- Triggers: npm script `npm run positions` or supervisor
- Responsibilities: Exit management — place profit-taking and stop-loss orders on open positions
- Interval: ~15 minutes

**Market Maker:**
- Location: `src/kalshi/market-maker.py`
- Triggers: npm script `npm run mm` or supervisor
- Responsibilities: Avellaneda-Stoikov market making on KXHIGH (disabled by default)
- Interval: ~5 minutes

**Supervisor:**
- Location: `scripts/supervisor.py`
- Triggers: npm script `npm run supervisor`
- Responsibilities: Start/monitor/restart all enabled bots; detect hangs, crashes; enforce kill switch
- Runs: Foreground with real-time status table

**Dashboard:**
- Location: `scripts/dashboard.py`
- Triggers: npm script `npm run dashboard`
- Responsibilities: FastAPI web UI serving bot status, P&L, trades, decisions, health checks
- Runs: HTTP server on port 3456

**Audit:**
- Location: `scripts/audit.py`
- Triggers: npm script `npm run audit`
- Responsibilities: Comprehensive system audit (P&L accuracy, sizing validation, slippage, edge models, Sharpe)
- Runs: One-shot analysis

**Backtest:**
- Location: `scripts/backtest.py`
- Triggers: npm script `npm run backtest` or `npm run backtest:daily`
- Responsibilities: Walk-forward evaluation of bots on historical trade logs
- Runs: One-shot or daily

## Error Handling

**Strategy:** Defensive — fail loudly but gracefully. All bots log errors to `data/logs/{bot_name}.log` with RotatingFileHandler (5MB, 3 backups).

**Patterns:**

- **API failures** — Logged and counted in circuit breaker; after N failures, TradeManager blocks trading
- **Network errors** — Retried with exponential backoff (3 attempts, base 1s) for idempotent GET; never retried for POST
- **Config errors** — Raise ValueError on startup (missing keys, invalid values); supervisor detects and restarts
- **Kill switch** — Check before every trade; skip trade if `data/HALT_TRADING` exists
- **Stale data** — TradeManager rejects trades older than configurable TTL (default none, but checks passed explicitly)
- **Balance/cost** — TradeManager validates available balance >= cost + fee before submitting order
- **Daily limits** — TradeManager tracks daily trade count and spend; returns None if exhausted

## Cross-Cutting Concerns

**Logging:** All bots use `setup_logging(bot_name)` from kalshi_auth; configures stdout + file logging with consistent format `%(asctime)s [%(name)s] %(levelname)s: %(message)s`.

**Validation:** `validate_trade_config()` checks maxTradeAmount (1-100 USD), maxDailyTrades (1-100), maxDailyLoss (1-500 USD); called by TradeManager.__init__.

**Authentication:** RSA-PSS signing in KalshiClient._sign(); timestamp included in every request (milliseconds); signature base64-encoded in headers.

**Concurrency:** File locking via fcntl on trade logs and shared state files; atomic writes using tempfile + os.replace(); no threads except for parallel HTTP fetches (`fetch_parallel`).

**Notifications:** Webhook support via `notify_webhook()` for critical alerts (circuit breaker open); WhatsApp notifications via `notify_whatsapp()` for daily reports.

**Monitoring:** HealthCheckMonitor tracks bot heartbeats, error counts, source freshness; OrderMonitor validates order fills + rejections; both write to `data/health-state.json`.

---

*Architecture analysis: 2026-02-26*
