# External Integrations

**Analysis Date:** 2026-02-26

## APIs & External Services

**Kalshi Prediction Markets:**
- Service: Kalshi REST API (`trading-api.kalshi.com` production, `demo-api.kalshi.co` demo)
- What it's used for: Core trading API — place orders, fetch portfolio balance, retrieve settlement data, query market details
- SDK/Client: Custom `KalshiClient` in `src/kalshi/kalshi_auth.py` (uses `requests`)
- Auth: RSA-PSS asymmetric signing (SHA-256, PSS padding) with private keys in `config/keys/kalshi-{demo,live}.pem`
- Rate limits: Handled by automatic retry with exponential backoff (up to 3 retries, 429/timeout/connection errors)
- Connection caching: Market cache in `data/market-cache.json` with 60-second TTL

**Weather Data:**
- Service: Open-Meteo (api.open-meteo.com/v1/forecast)
- What it's used for: 14-day temperature forecasts for KXHIGH markets
- Models: Single GFS model (fallback) or ensemble (GFS, ECMWF, ICON) via Bayesian Model Averaging
- Auth: None (public API)
- Usage: `src/kalshi/weather-bot.py` calls `get_forecast()` and `get_ensemble_forecast()` every 30 minutes

**Cryptocurrency Data:**
- Service: Coinbase (public REST API)
- What it's used for: Spot prices for BTC-USD and ETH-USD
- Auth: None (public endpoint)
- Usage: `src/kalshi/crypto-bot.py` fetches current prices every 5 minutes

**Cryptocurrency Volatility:**
- Service: Deribit (public REST API)
- What it's used for: Implied volatility from options market for BTC/ETH
- Auth: None (public endpoint)
- Usage: `src/kalshi/crypto-bot.py` uses IV to calibrate log-normal pricing model

**Entertainment Markets Data:**
- Service: Sanity CMS (HITS Daily Double project 8aky18h3)
- Endpoint: `https://8aky18h3.api.sanity.io/v2021-10-21/data/query/production`
- What it's used for: Album sales charts (Hits Top 50) settle KXALBUMSALES markets
- Auth: None (public Sanity.io queries, no API key required)
- Implementation: `src/kalshi/hdd_parser.py` executes GROQ queries to fetch chart data
- Usage: `src/kalshi/entertainment-bot.py` and `src/kalshi/source-monitor.py` poll every 15 minutes for fresh data
- Parsing: Extract sales numbers from `chart_data` field, timestamp from `date` field

**Box Office Data:**
- Service: Box Office Mojo and The Numbers (web scraping)
- What it's used for: Weekend domestic gross for KXMOVIE markets (information arbitrage)
- Auth: None (public websites, user-agent header set)
- Implementation: `src/kalshi/entertainment-bot.py` scrapes `https://www.boxofficemojo.com/` and `/weekend/`
- Fallback: Optional TMDb API integration stub in `src/kalshi/source-monitor.py` (not currently used)
- Usage: Scraping triggered every 30 minutes or on-demand

**Economic Data (Nowcasts & Indicators):**
- Service: Cleveland Fed CPI Nowcast
- What it's used for: Real-time CPI estimates for KXCPI markets before official BLS release
- Auth: None (public data)
- Implementation: `src/kalshi/economics-bot.py` fetches nowcast point estimate and distribution

**Economic Data (Gas Prices):**
- Service: AAA Gasoline Prices (web scraping)
- What it's used for: ~8% component of CPI for gas price markets
- Auth: None (public website)
- Usage: `src/kalshi/economics-bot.py` extracts national average daily

**News & Blog Scraping:**
- Service: BeatRelease.com (blog scraping)
- What it's used for: Extract Kalshi trade recommendations from blog posts via LLM parsing
- Auth: None (public website)
- Implementation: `src/kalshi/beatrelease-scanner.py` scrapes blog URLs listed in config
- LLM: DeepSeek API for extracting structured trades from text

## Data Storage

**Databases:**
- None — all state is file-based JSON (local or S3)

**File Storage:**
- Local filesystem: `data/` directory (gitignored)
  - Trade logs: `data/kalshi-*-trades.json` (golden records)
  - Decision logs: `data/*-decisions.json` (every market evaluated with reason)
  - State files: `health-state.json`, `allocator-state.json`, `beatrelease-state.json`
  - Snapshots: `data/kalshi-source-snapshots/` (web responses, 7-day rotation)
  - Logs: `data/logs/*.log` (per-bot rotating files, 5MB each, 3 backups)

**Cloud Storage (S3):**
- AWS S3 bucket: `kalshi-trading-logs` (default, configurable via `S3_BUCKET` env var)
- Region: `us-west-2`
- Client: AWS CLI (`aws s3` commands in `scripts/s3-sync.sh`)
- Sync strategy: Whitelist approach (exclude all, include only essential files)
- Synced files:
  - Trade logs: `data/kalshi-*-trades.json`, `data/beatrelease-trades.json`
  - State: `data/beatrelease-state.json`, `data/health-state.json`, `data/allocator-state.json`, `data/scan-summaries.json`
  - Decision logs: `data/*-decisions.json`
  - Config: `config/calibration.json`
  - Results: `data/backtest-results.json`
- Excluded from sync: `data/logs/`, `data/pids/`, `data/HALT_TRADING`, market caches, demo trades, source snapshots
- Locking: S3-based lock file (`.sync-lock`) prevents concurrent uploads/downloads
- Commands: `npm run sync:up` (push), `npm run sync:down` (pull), `npm run sync:setup` (create bucket)

**Caching:**
- Market cache: `data/market-cache.json` (60-second TTL, cross-process)
- Nowcast cache: `data/econ-nowcast-cache.json` (24-hour TTL for economics data)
- Price history: `data/crypto-price-history.json` (in-memory dict loaded at startup)

## Authentication & Identity

**Auth Provider:**
- Custom RSA-PSS (Kalshi-specific requirement)

**Implementation:**
- Private keys: `config/keys/kalshi-demo.pem` and `config/keys/kalshi-live.pem` (gitignored)
- Signing logic: `src/kalshi/kalshi_auth.py` — `KalshiClient.sign_request()` creates RSA-PSS signature with SHA-256
- Key loading: `cryptography.hazmat.primitives` for asymmetric signing
- Per-request: Request body is hashed, signed with private key, signature included in headers
- Safety: `KALSHI_CONFIRM_PRODUCTION=yes` env var required for production mode to prevent accidental live trading

## Monitoring & Observability

**Error Tracking:**
- None (no third-party service like Sentry)
- Custom logging via Python `logging` module with rotating file handlers

**Logs:**
- Approach: Per-bot log files in `data/logs/{bot-name}.log` (5MB rotating, 3 backups)
- Setup: `setup_logging()` in `src/kalshi/kalshi_auth.py`
- Console output: Unbuffered stdout (real-time in terminal)
- Log format: `%(asctime)s [%(name)s] %(levelname)s: %(message)s`

**Health Monitoring:**
- Framework: Custom `HealthCheckMonitor` class in `src/kalshi/kalshi_auth.py`
- State file: `data/health-state.json` (contains heartbeats, source freshness, error counts per bot)
- Dashboard: `scripts/dashboard.py` reads health state and displays bot status table
- Supervisor integration: `scripts/supervisor.py` detects hung processes (stale heartbeat >3x scan interval)

**Trade Decision Logging:**
- Every market evaluated is logged to `data/{bot-name}-decisions.json`
- Structure: `[{"ticker": "...", "decision": "TRADE|SKIP", "reason": "...", "timestamp": "..."}]`
- Dashboard endpoint: `/api/decisions` shows decision logs for all bots

## CI/CD & Deployment

**Hosting:**
- None specified — runs on macOS Mini locally (production) or MacBook (development)
- No cloud deployment platform (manually managed)

**CI Pipeline:**
- None (no GitHub Actions or similar)
- Manual testing: `npm run test` runs pytest against 22 test files
- Daily automation: `npm run report:notify` sends WhatsApp summary; `npm run backtest:daily` alerts on model drift

## Environment Configuration

**Required env vars:**
- `KALSHI_API_KEY` - API key from Kalshi account
- `KALSHI_KEY_FILE` - Path to RSA private key (defaults to `config/keys/kalshi-demo.pem`)
- `KALSHI_MODE` - `demo` or `production`
- `KALSHI_CONFIRM_PRODUCTION` - Must be `yes` when `KALSHI_MODE=production`

**Optional env vars:**
- `DEEPSEEK_API_KEY` - DeepSeek LLM API key (for beatrelease scanner)
- `NOTIFICATION_PHONE` - WhatsApp phone number (E.164 format)
- `ALERT_WEBHOOK_URL` - Slack/Discord webhook for critical alerts
- `S3_BUCKET` - S3 bucket name (defaults to `kalshi-trading-logs`)
- `AWS_DEFAULT_REGION` - AWS region (defaults to `us-west-2`)
- `STARTING_BALANCE_CENTS` - Dashboard initial balance assumption (defaults to 50000 = $500)

**Secrets location:**
- `.env` file (gitignored, never committed)
- RSA private keys: `config/keys/` (gitignored)
- DeepSeek key: `config/keys/deepseek.txt` (gitignored) or `DEEPSEEK_API_KEY` env var
- Webhook URLs: Environment variables only
- API keys: All in `.env` file

## Webhooks & Callbacks

**Incoming:**
- None (bots only make requests, no inbound webhooks)

**Outgoing:**
- Slack/Discord webhook: `ALERT_WEBHOOK_URL` (for critical alerts like kill switch, circuit breaker, daily loss limit, health issues)
  - Format: Auto-detects Slack vs Discord by URL pattern
  - Rate limiting: 30-minute cooldown per unique message prefix to prevent spam
  - Implementation: `notify_webhook()` in `src/kalshi/kalshi_auth.py`

**WhatsApp Notifications:**
- Phone: Configured via `NOTIFICATION_PHONE` env var or `notificationPhone` in `config/bots-config.json`
- Implementation: `notify_whatsapp()` in `src/kalshi/kalshi_auth.py`
- Method: Calls CLI tool `openclaw` (must be installed separately)
- Usage: Daily P&L reports via `npm run report:notify`, beatrelease alerts

## Polling & Daemon Integrations

**Supervisor Process Management:**
- Script: `scripts/supervisor.py`
- Manages: All daemon bots (weather, entertainment, crypto, economics, positions, monitor, etc.)
- Features: Auto-restart on crash, crash rate limiting, heartbeat staleness detection, kill-switch integration
- Health tracking: Writes to `data/health-state.json` for dashboard monitoring

**Data Freshness Monitoring:**
- Source monitor: `src/kalshi/source-monitor.py` polls all sources every 10-30 minutes
- Markets refresh: Every scan fetches latest Kalshi market data via `/markets/{ticker}` endpoint
- Stale data checks: Reject trades if source data is older than configurable thresholds (168 hours for entertainment data)

---

*Integration audit: 2026-02-26*
