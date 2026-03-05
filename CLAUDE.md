# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated prediction market trading system for Kalshi. Python bots execute weather forecasting, entertainment/album sales info arbitrage, and copy-trading strategies against the Kalshi REST API.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # Fill in KALSHI_API_KEY, KALSHI_KEY_FILE, KALSHI_MODE
```

RSA private keys go in `config/keys/` (gitignored): `kalshi-demo.pem`, `kalshi-live.pem`. For beatrelease scanner, add DeepSeek API key to `config/keys/deepseek.txt` or set `DEEPSEEK_API_KEY` env var. Production mode requires `KALSHI_CONFIRM_PRODUCTION=yes` as a safety guard.

## Data Sync

Trade logs in `data/` are gitignored but essential for backtesting and auditing. S3 syncs them between machines (Mac Mini production ↔ MacBook development).

```bash
npm run sync:setup   # Create S3 bucket (one-time)
npm run sync:up      # Push trade logs + calibration to S3
npm run sync:down    # Pull trade logs + calibration from S3
```

Requires AWS CLI configured with credentials. Set `S3_BUCKET` in `.env` (defaults to `kalshi-trading-logs`).

**Synced:** `data/kalshi-*-trades.json`, `data/beatrelease-trades.json`, `data/beatrelease-state.json`, `data/backtest-results.json`, `config/calibration.json`.
**Excluded:** `data/logs/`, `data/pids/`, `data/HALT_TRADING`, market caches, demo trades, source snapshots.

## Running Bots

All bots are run via npm scripts that call Python 3:

```bash
npm run weather        # Weather temperature trading (KXHIGH markets)
npm run entertainment  # Album sales / entertainment markets
npm run strategy       # Unified strategy trader
npm run hdd            # HITS Daily Double album data scraper
npm run monitor        # External data source monitor
npm run positions      # Position monitor (take-profit, stop-loss, model-shift exits)
npm run economics      # CPI/GDP/Jobs nowcast trading
npm run crypto         # BTC/ETH price trading
npm run arb            # Cross-platform arbitrage (Kalshi vs Polymarket)
npm run mm             # Market maker (Avellaneda-Stoikov, disabled by default)
npm run cycle          # Full trade cycle (scripts/trade-cycle-2.py)
npm run demo           # Demo trading interface
npm run calibrate      # Grid-search optimal sigma params → config/calibration.json
npm run backtest       # Brier score evaluation, sizing comparisons, edge threshold sweeps
```

Or run Python directly: `python3 src/kalshi/weather-bot.py`

The beatrelease scanner runs as a daemon: `python3 src/kalshi/beatrelease-scanner.py` (loops every 4h) or with `--once` for single scan.

The HDD scraper has subcommands:
```bash
python3 src/kalshi/hdd-scraper.py scan                   # One-shot scan
python3 src/kalshi/hdd-scraper.py charts                  # Fetch charts only
python3 src/kalshi/hdd-scraper.py articles                # Fetch articles only
python3 src/kalshi/hdd-scraper.py monitor --interval 15   # Loop every 15 min
```

## Operations & Monitoring

### Supervisor

Process manager that starts/stops/monitors all daemon bots with auto-restart on crash.

```bash
npm run supervisor          # Start all enabled bots in foreground
npm run supervisor:status   # Show live status table (PID, uptime, restarts, heartbeat)
```

Features: auto-restart with crash rate limiting (5 crashes in 10 min = disable), heartbeat staleness detection (>3x scan interval = stale), hung/zombie process detection, kill-switch integration (`data/HALT_TRADING` stops all bots), calibration staleness warnings.

### Dashboard

FastAPI web UI for real-time monitoring (default port 3456).

```bash
npm run dashboard    # Start web server at http://localhost:3456
```

Key API endpoints: `/api/bots` (status), `/api/account` (balance/P&L), `/api/trades` (recent fills), `/api/decisions` (bot decision logs), `/api/risk` (per-bot daily risk), `/api/settlements` (settled P&L, win rate), `/api/positions`, `/api/health`, `/api/logs`. Frontend served from `scripts/dashboard.html`.

### Daily Automation

```bash
npm run report              # Daily P&L summary report (stdout)
npm run report:notify       # Daily P&L + backtest results via WhatsApp
npm run backtest:daily      # Daily backtest with >10% drift detection alert
npm run audit               # Comprehensive math & strategy audit
npm run reconcile           # Annotate trade records with settlement outcomes
npm run backfill            # Backfill settlement data from market endpoints
```

- **`audit.py`** — Programmatic audit covering P&L truth, data integrity, backtesting realism, execution slippage, sizing math, and per-bot edge validation. Uses severity levels (PASS/WARN/FAIL/INFO). Flags: `--json`, `--section N`, `--reconcile`.
- **`reconcile-trades.py`** — Walks all trade log files, matches to API settlement/fill data, writes back `settlement_result`, `realized_edge`. Idempotent; `--dry-run` available.
- **`backfill-settlements.py`** — Queries individual `/markets/{ticker}` endpoints for unsettled trades. Complements reconcile. Flags: `--dry-run`, `--report`.
- **`daily-backtest.py`** — Runs `backtest.py --save`, compares Brier scores to previous results, sends WhatsApp alert if model quality degrades >10%.
- **Alerting** — WhatsApp notifications via `notify_whatsapp()` (uses `openclaw` command) and webhook support via `notify_webhook()`.

### Observability Files (in `data/`)

| File | Written by | Read by | Purpose |
|------|-----------|---------|---------|
| `health-state.json` | All bots | Supervisor, dashboard | Bot heartbeats, source freshness, error counts |
| `allocator-state.json` | Capital allocator | Dashboard, bots | Circuit breaker status, allocation state |
| `*-decisions.json` | Each bot | Dashboard | Every market evaluated with trade/skip reason |
| `*-last-run.json` | One-shot bots | Supervisor | Completion markers (strategy, hdd, beatrelease) |

## Testing

```bash
pytest tests/                      # Run all tests (41 test files, ~1259 tests)
pytest tests/ -v                   # Verbose output
pytest tests/test_kelly.py         # Run specific test file
pytest tests/test_kelly.py -k "test_returns_zero"  # Run matching tests
```

Tests cover pure functions — no API calls or credentials required.

**Test import patterns**:
- `conftest.py` adds `src/kalshi/` to `sys.path`, so shared modules import directly: `from probability import half_kelly`
- Source files use hyphens (`strategy-trader.py`), so Python can't import them directly. Tests use `importlib.util.spec_from_file_location` to load bot modules.
- Bots instantiate `KalshiClient` and read config at module-level import time, so tests must stub `kalshi_auth` in `sys.modules` before importing bot modules (see `test_kelly.py:_load_strategy_trader()` for the pattern).
- Probability tests must call `_reset_calibration()` in setup/teardown to clear cached calibration state between tests.

## Performance Analysis

```bash
python3 scripts/analyze-performance.py              # Summary report (local trade logs only)
python3 scripts/analyze-performance.py --json        # JSON output
python3 scripts/analyze-performance.py --reconcile   # With API reconciliation (win rate, P&L, Sharpe)
```

## Architecture

### Shared Modules

**`src/kalshi/kalshi_auth.py`** — Authentication, safety infrastructure, and trade management (~680 lines). All bots import from here:

```python
from kalshi_auth import KalshiClient, setup_logging, setup_unbuffered, setup_signal_handlers, PROJECT_DIR
from kalshi_auth import TradeManager, trim_trade_log

log = setup_logging("my-bot")
client = KalshiClient()             # reads KALSHI_API_KEY, KALSHI_KEY_FILE, KALSHI_MODE from env
data = client.get("/portfolio/balance")
client.post("/portfolio/orders", body={...})
markets = client.get_all_markets(prefix="KXHIGH")
```

Key components:
- `KalshiClient` — RSA-PSS signing, automatic retry with exponential backoff (429, connection errors, timeouts), demo/production endpoint switching, market caching with TTL
- `TradeManager` — Consolidated trade placement with guardrails: kill switch check, circuit breaker, daily trade/loss limits, dedup, cost cap, balance check, stale data check, atomic trade logging
- `CircuitBreaker` — Tracks consecutive API failures, auto-resets after timeout
- `RecentTradeTracker` — Deduplication across scan cycles (configurable cooldown)
- `check_kill_switch()` — Graceful halt via `data/HALT_TRADING` file
- Helper functions: `load_trades()`, `save_trade()`, `_atomic_write_json()`, `retry_request()`, `fetch_parallel()`, `validate_trade_config()`

**`src/kalshi/probability.py`** — Centralized probability models and position sizing (~450 lines). Uses `math.erf` for normal CDF (no scipy dependency):

```python
from probability import weather_probability, ensemble_weather_probability, nws_probability, info_arb_probability
from probability import econ_nowcast_probability, cpi_nowcast_sigma, crypto_price_probability
from probability import half_kelly, half_kelly_sell, quarter_kelly
from probability import album_data_sigma, boxoffice_data_sigma
```

Key functions:
- `weather_probability(forecast_temp, threshold, direction, days_out, city)` — CDF-based model for KXHIGH markets with configurable sigma scaling
- `ensemble_weather_probability(forecasts, threshold, direction, days_out, city)` — Bayesian Model Averaging of GFS/ECMWF/ICON forecasts
- `nws_probability(running_high, threshold, direction, hour_of_day)` — Real-time arbitrage (continuous sigma model, no time gate)
- `info_arb_probability(observed, threshold, data_sigma_pct)` — Album sales and box office data
- `econ_nowcast_probability(nowcast_value, nowcast_sigma, threshold, direction)` — CPI/GDP/Jobs markets
- `cpi_nowcast_sigma(days_to_release)` — Step function: 14d=0.10%, 7d=0.06%, 1d=0.03%, 0d=0.01%
- `crypto_price_probability(current_price, threshold, direction, time_horizon_minutes, realized_vol, iv)` — Log-normal/GBM model for BTC/ETH
- `half_kelly(edge, price_cents, max_cost_cents, bankroll_cents)` — Buy-side Kelly sizing
- `half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents)` — Sell-side Kelly (for longshot bias)
- `quarter_kelly(edge, price_cents, max_cost_cents, bankroll_cents)` — Quarter-Kelly for brackets/crypto
- Lazy-loads `config/calibration.json` for per-city/market-type parameter tuning (generated by `npm run calibrate`)

**`src/kalshi/capital_allocator.py`** — Capital allocation across bots with circuit breaker integration. Manages per-bot risk budgets and writes state to `data/allocator-state.json`.

**`src/kalshi/ticker_utils.py`** — Ticker parsing utilities (extract city, date, threshold, direction from tickers like `KXHIGHMIA-26FEB16-T86`).

**`src/kalshi/hdd_parser.py`** — HITS Daily Double chart and article parsing.

**`src/kalshi/polymarket_client.py`** — Polymarket CLOB API client for cross-platform arbitrage.

**`src/kalshi/macro_engine.py`** — Macro/geopolitics sentiment engine (~300 lines). Fetches from FRED API (TIPS breakevens, UMich expectations, GDPNow), Truflation real-time CPI, and curated blog/RSS feeds. DeepSeek LLM extracts sentiment. Produces `MacroSignal` with CPI bias and confidence for economics bot nowcast adjustment.

**`src/kalshi/particle_filter.py`** — Sequential Monte Carlo (particle filter) for Bayesian belief tracking (~400 lines). Maintains weighted particle distributions per market, carries memory across scans. `FilterManager` manages per-ticker filters with JSON state persistence. `ci_kelly_multiplier()` reduces Kelly fraction when filter CI is wide. Integrates with crypto-bot (5-min scans), extensible to weather/economics.

### Bot Structure

Each bot in `src/kalshi/` follows the same pattern:
1. Import shared modules: `from kalshi_auth import KalshiClient, TradeManager, setup_logging, ...`
2. Module-level initialization: `setup_unbuffered()`, `log = setup_logging("bot-name")`, `setup_signal_handlers()`
3. Create client and trade manager: `client = KalshiClient()`, `trade_manager = TradeManager(client, TRADES_PATH, config)`
4. Load config from `config/bots-config.json` or `config/kalshi-config.json`
5. Data fetching (weather APIs, web scraping, LLM calls)
6. Edge calculation using `probability.py` models
7. Trade execution via `trade_manager.place_order(...)` (handles all safety checks)

### Key Bots

| Bot | Markets | Data Source | Mode |
|-----|---------|-------------|------|
| `weather-bot.py` | KXHIGH temperature | Open-Meteo forecast API (ensemble: GFS/ECMWF/ICON) | Daemon (30 min) |
| `entertainment-bot.py` | Album sales | HITS Daily Double (Sanity CMS) | Daemon (15 min) |
| `source-monitor.py` | Weather + entertainment | NWS, HDD, Box Office Mojo | Daemon (10-30 min) |
| `strategy-trader.py` | Multiple | Longshot bias, near-settlement arbitrage | One-shot |
| `beatrelease-scanner.py` | Music/entertainment | BeatRelease blog + DeepSeek LLM | Daemon (4h) |
| `hdd-scraper.py` | (data only) | HITS Daily Double Sanity CMS | One-shot / monitor |
| `position-monitor.py` | All (exits) | Kalshi portfolio API | Daemon (15 min) |
| `economics-bot.py` | KXCPI/KXGDP/KXJOBS | Cleveland Fed nowcast, AAA gas | Daemon (6h) |
| `crypto-bot.py` | KXBTC/KXETH | Coinbase spot, Deribit IV | Daemon (5 min) |
| `cross-platform-arb.py` | Multiple | Polymarket CLOB prices | Daemon (30 min) |
| `market-maker.py` | KXHIGH (liquid) | Internal model + Avellaneda-Stoikov | Daemon (5 min) |

### Archived Code

`archive/` contains superseded files: predecessor beatrelease scripts, numbered cycle iteration scripts (`cycle3.py` through `cycle21.py`). Historical iterations kept for reference.

## Configuration

- **`.env`** — `KALSHI_API_KEY`, `KALSHI_KEY_FILE`, `KALSHI_MODE`, `KALSHI_CONFIRM_PRODUCTION` (see `.env.example`)
- **`config/kalshi-config.json`** — Weather bot settings: cities, risk limits (maxTradeAmount, edgeThreshold, scanInterval)
- **`config/kalshi-monitor-config.json`** — Source monitor: HDD/BoxOffice/NWS polling intervals and endpoints
- **`config/bots-config.json`** — Entertainment, beatrelease, strategy, crypto, economics, position monitor, cross-platform arb, and market maker settings
- **`config/calibration.json`** — Dynamic sigma parameters per city/market type (generated by `npm run calibrate`)
- **`config/keys/`** — RSA private keys (gitignored)

## API Endpoints

Controlled by `KALSHI_MODE` env var:
- `demo` (default): `https://demo-api.kalshi.co/trade-api/v2`
- `production`: `https://trading-api.kalshi.com/trade-api/v2`

## Data Storage

Trade logs, state files, PID files, bot logs, and source snapshots are saved under `data/` (gitignored, `.gitkeep` preserves directory). Each bot writes to its own trade log file (e.g., `data/kalshi-trades.json`, `data/kalshi-strategy-trades.json`, `data/kalshi-entertainment-trades.json`, `data/beatrelease-trades.json`). Trading can be halted by creating `data/HALT_TRADING` (checked by `TradeManager`).

## Risk Controls

Bots enforce: max trade amount ($5-10), max daily trades (10-20), max daily loss ($10-50), edge threshold (8%+), and half-Kelly position sizing. These are configured in the JSON config files. `TradeManager` enforces all limits plus circuit breaker, deduplication, and stale data rejection.

## Strategy Notes

- **Longshot bias** (strategy-trader): Becker (2025) model. 1c contracts are overpriced by ~57% (true win rate 0.43% vs 1% implied). Edge decays exponentially: `0.57 * exp(-0.15 * price)`.
- **Weather probability** (weather-bot): CDF-based model using NWS forecast error distribution with per-city sigma calibration. Optional ensemble (GFS+ECMWF+ICON) via Bayesian Model Averaging.
- **Info arbitrage** (source-monitor, entertainment-bot): Trade when external data (HDD charts, NWS actuals, box office) confirms outcome before Kalshi settles. NWS trades fire at all hours (sigma model handles morning uncertainty).
- **Economics nowcast** (economics-bot): Cleveland Fed CPI nowcast + CDF model. Sigma steps down as release date approaches.
- **Crypto GBM** (crypto-bot): Log-normal model using spot price, IV/realized vol, and time to settlement. Quarter-Kelly sizing.
- **Cross-platform arb** (cross-platform-arb): Monitors Kalshi vs Polymarket price discrepancies. Phase 1 = monitoring only.
- **Market making** (market-maker): Avellaneda-Stoikov reservation price with gamma increasing near settlement. Disabled by default.

## Bot Development Scope Rules

When working on a specific bot, ONLY modify files owned by that bot. Do NOT touch shared modules or other bots' files.

### File Ownership

| Bot Task | Can Modify | CANNOT Modify |
|----------|-----------|---------------|
| weather-bot | `weather-bot.py`, `weather_data.py`, `forecast_verifier.py`, `test_weather*.py`, `test_advanced_weather.py`, `test_empirical_ensemble.py`, `kalshi-config.json` | probability.py, kalshi_auth.py, other bots |
| crypto-bot | `crypto-bot.py`, `crypto_models.py`, `vol_forecaster.py`, `regime_detector.py`, `particle_filter.py`, `test_crypto*.py`, `test_heston.py`, `test_vol_forecaster.py` | probability.py, kalshi_auth.py, other bots |
| economics-bot | `economics-bot.py`, `scenario_engine.py`, `cpi_belief_filter.py`, `macro_engine.py`, `test_economics.py`, `test_econ*.py`, `test_scenario*.py` | probability.py, kalshi_auth.py, other bots |
| strategy-trader | `strategy-trader.py`, `strategy_engine.py`, `test_strategy*.py` | probability.py, kalshi_auth.py, other bots |
| entertainment-bot | `entertainment-bot.py`, `test_entertainment*.py` | probability.py, kalshi_auth.py, other bots |
| source-monitor | `source-monitor.py`, `hdd_parser.py`, `test_source*.py` | probability.py, kalshi_auth.py, other bots |

### Shared Modules (require dedicated session)

These files are shared across bots. Changes require a dedicated "shared infrastructure" session, NOT a bot-specific session:

- `probability.py` — All probability models and Kelly sizing
- `kalshi_auth.py` — Auth, TradeManager, safety infrastructure
- `capital_allocator.py` — Cross-bot capital allocation
- `ticker_utils.py` — Ticker parsing (used by multiple bots + dashboard)
- `config/bots-config.json` — Only modify YOUR bot's section

### If you need a shared module change

1. Do NOT modify the shared file
2. Document the needed change in your plan/summary
3. Flag it: "SHARED MODULE CHANGE NEEDED: [file] — [what and why]"
4. It will be handled in a separate session that can run all affected tests

### Test discipline

- Run ONLY your bot's tests, not the full suite
- If your bot's tests import from shared modules and fail, that's a signal to stop — don't "fix" the shared module
- Never update tests for other bots to match changes you made

## Research

`research/` contains strategy documentation: `kalshi-deep-dive.md`, `kalshi-info-arbitrage.md`, `kalshi-markets-research.md`. Reference these for Kalshi API details, market structure, and strategy rationale.

`prompts/full-codebase-review.md` contains a comprehensive audit prompt for systematic code review of edge models, sizing, risk controls, and operational reliability.
