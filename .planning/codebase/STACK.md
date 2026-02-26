# Technology Stack

**Analysis Date:** 2026-02-26

## Languages

**Primary:**
- Python 3 - Core trading bots, data processing, backtesting, and monitoring scripts

**Secondary:**
- Bash - S3 sync orchestration, process management
- HTML/JavaScript - Dashboard frontend (single-file `scripts/dashboard.html`)
- JSON - Configuration and state files

## Runtime

**Environment:**
- Python 3 (no version lock specified)
- Requires `.venv` or system Python installation

**Package Manager:**
- pip (Python packages)
- npm (JavaScript/Node.js wrapper scripts, no JavaScript dependencies)
- Lockfile: `requirements.txt` (pip pinned versions), no `package-lock.json`

## Frameworks

**Core:**
- FastAPI 0.100.0+ - REST API for dashboard (`scripts/dashboard.py`), serves JSON endpoints (`/api/bots`, `/api/trades`, `/api/account`, etc.)
- uvicorn 0.23.0+ - ASGI server for FastAPI (default port 3456)

**HTTP & Network:**
- requests 2.28.0+ - All external API calls (Kalshi, weather, crypto, markets APIs)

**Data Processing:**
- beautifulsoup4 4.12.0+ - HTML parsing for Box Office Mojo, BeatRelease blog scraping
- cryptography 41.0.0+ - RSA-PSS signing for Kalshi API authentication

**Testing:**
- pytest 7.0+ - Test runner for unit tests (`tests/` directory, 22 test files)

**Build/Dev:**
- python-dotenv 1.0.0+ - Environment variable loading from `.env` files

## Key Dependencies

**Critical:**
- requests - HTTP client for all external integrations (Kalshi API, Open-Meteo, Coinbase, DeepSeek, etc.)
- cryptography - Asymmetric signing required by Kalshi API (`RSA-PSS` with SHA-256)
- beautifulsoup4 - Data extraction from web sources (entertainment markets, economic data)

**Infrastructure:**
- FastAPI/uvicorn - Real-time monitoring dashboard (reads state files, serves live data)
- pytest - Testing framework ensuring edge models, Kelly sizing, and risk calculations are correct

## Configuration

**Environment:**
- `.env` file (gitignored, must be created from `.env.example`)
  - Required: `KALSHI_API_KEY` (API key for trading)
  - Required: `KALSHI_KEY_FILE` (path to RSA private key)
  - Required: `KALSHI_MODE` (`demo` or `production`)
  - Optional: `KALSHI_CONFIRM_PRODUCTION` (safety guard, must be `yes` for production)
  - Optional: `DEEPSEEK_API_KEY` (for beatrelease scanner LLM extraction)
  - Optional: `NOTIFICATION_PHONE` (WhatsApp alerts in E.164 format)
  - Optional: `ALERT_WEBHOOK_URL` (Slack/Discord webhook for critical alerts)
  - Optional: `S3_BUCKET` (AWS S3 bucket name, defaults to `kalshi-trading-logs`)

**Build:**
- `package.json` - npm script wrappers around Python entry points (see "Running Bots" in CLAUDE.md)
- `.env.example` - Template for environment configuration (committed, shows all required variables)

## Platform Requirements

**Development:**
- macOS or Linux with Python 3.8+
- AWS CLI configured (for S3 sync between machines)
- `openssl` or compatible RSA key generation tool
- Git for version control

**Production:**
- macOS Mini (specified in CLAUDE.md) or Linux server
- Same Python + dependencies as development
- AWS credentials (IAM user `peakr-deploy` with S3 bucket access)
- Process supervisor (built-in via `npm run supervisor`, uses `scripts/supervisor.py`)
- Optional: WhatsApp CLI tool `openclaw` for notifications

## API Endpoints & Services

**Kalshi REST API:**
- Base URLs:
  - Demo: `https://demo-api.kalshi.co/trade-api/v2`
  - Production: `https://trading-api.kalshi.com/trade-api/v2`
- Authentication: RSA-PSS (asymmetric signing via `config/keys/kalshi-{demo,live}.pem`)
- Key endpoints: `/portfolio/orders` (place trades), `/portfolio/balance` (account), `/markets/{ticker}` (settlement data)

**External Public APIs (no auth required):**
- Open-Meteo - Weather forecasts (GFS, ECMWF, ICON ensemble models)
- Coinbase - Spot prices for BTC/ETH
- Deribit - Implied volatility for crypto options
- Cleveland Fed - CPI nowcast data
- Sanity CMS (HITS Daily Double) - Album sales charts via GROQ queries
- DeepSeek - LLM API for parsing blog posts
- Polymarket CLOB - Cross-platform price arbitrage (read-only)

**Local Services:**
- Dashboard API: `http://localhost:3456` (FastAPI/uvicorn)

---

*Stack analysis: 2026-02-26*
