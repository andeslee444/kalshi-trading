# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated prediction market trading system for Kalshi. Python bots execute weather forecasting, entertainment/album sales info arbitrage, and copy-trading strategies against the Kalshi REST API.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # Fill in KALSHI_API_KEY
```

## Running Bots

All bots are run via npm scripts that call Python 3:

```bash
npm run weather        # Weather temperature trading (KXHIGH markets)
npm run entertainment  # Album sales / entertainment markets
npm run strategy       # Unified strategy trader
npm run hdd            # HITS Daily Double album data scraper
npm run monitor        # External data source monitor
npm run cycle          # Full trade cycle (scripts/trade-cycle-2.py)
npm run demo           # Demo trading interface
```

Or run Python directly: `python3 src/kalshi/weather-bot.py`

The beatrelease scanner runs as a daemon: `python3 src/kalshi/beatrelease-scanner.py` (loops every 4h) or with `--once` for single scan.

## Testing

```bash
pytest tests/           # Run all tests
pytest tests/ -v        # Verbose output
pytest tests/test_kelly.py  # Run specific test file
```

Tests cover pure functions: Kelly sizing, weather probability calculations, ticker parsing, HDD chart parsing, and trade file I/O. No API calls required.

## Performance Analysis

```bash
python3 scripts/analyze-performance.py          # Summary report
python3 scripts/analyze-performance.py --json   # JSON output
```

## Architecture

### Shared Auth Module (`src/kalshi/kalshi_auth.py`)

All bots use the `KalshiClient` class from `kalshi_auth.py` for authentication and API calls:

```python
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR

log = setup_logging("my-bot")       # Standardized logging (stdout + optional file)
client = KalshiClient()             # reads KALSHI_API_KEY, KALSHI_KEY_FILE, KALSHI_MODE from env
data = client.get("/portfolio/balance")
client.post("/portfolio/orders", body={...})
markets = client.get_all_markets(prefix="KXHIGH")
```

The client handles RSA-PSS signing, automatic retry with exponential backoff on transient errors (429, connection errors, timeouts), and demo/production endpoint switching via `KALSHI_MODE` env var.

Helper functions: `load_trades(path)`, `save_trade(path, trade)`, `setup_unbuffered()`, `setup_signal_handlers()`, `setup_logging(name, log_file=None)`.

### Bot Structure

Each bot in `src/kalshi/` follows the same pattern:
1. Import shared auth: `from kalshi_auth import KalshiClient, setup_logging, ...`
2. Setup logging: `log = setup_logging("bot-name")`
3. Create client: `client = KalshiClient()`
4. Load config from `config/bots-config.json` (bot-specific settings) or `config/kalshi-config.json` (weather/general)
5. Data fetching (weather APIs, web scraping, LLM calls)
6. Edge calculation and trade execution via `client.post("/portfolio/orders", body=...)`
7. Trade logging to `data/` as JSON

### Key Bots

| Bot | Markets | Data Source |
|-----|---------|-------------|
| `weather-bot.py` | KXHIGH temperature | Open-Meteo forecast API |
| `entertainment-bot.py` | Album sales | HITS Daily Double (Sanity CMS) |
| `strategy-trader.py` | Multiple | Longshot bias, near-settlement arbitrage |
| `source-monitor.py` | Weather + entertainment | NWS, HDD, Box Office Mojo |
| `beatrelease-scanner.py` | Music/entertainment | BeatRelease blog + DeepSeek LLM |
| `hdd-scraper.py` | (data only) | HITS Daily Double Sanity CMS |

### Archived Code

`archive/` contains superseded files: predecessor beatrelease scripts, numbered cycle iteration scripts (`cycle3.py` through `cycle21.py`), and `cycle20.py`. These are historical iterations kept for reference.

## Configuration

- **`.env`** — `KALSHI_API_KEY`, `KALSHI_KEY_FILE`, `KALSHI_MODE` (see `.env.example`)
- **`config/kalshi-config.json`** — Weather bot settings: cities, risk limits (maxTradeAmount, edgeThreshold, scanInterval)
- **`config/kalshi-monitor-config.json`** — Source monitor: HDD/BoxOffice/NWS polling intervals and endpoints
- **`config/bots-config.json`** — Entertainment, beatrelease, and strategy bot settings (trade limits, intervals, tickers)
- **`config/keys/`** — RSA private keys (gitignored): `kalshi-demo.pem`, `kalshi-live.pem`, `deepseek.txt`

## API Endpoints

Controlled by `KALSHI_MODE` env var:
- `demo` (default): `https://demo-api.kalshi.co/trade-api/v2`
- `production`: `https://trading-api.kalshi.com/trade-api/v2`

## Data Storage

Trade logs, state files, PID files, bot logs, and source snapshots are saved under `data/` (gitignored, `.gitkeep` preserves directory).

## Python Dependencies

Managed via `requirements.txt`: `requests`, `cryptography`, `beautifulsoup4`, `pytest`.

## Risk Controls

Bots enforce: max trade amount ($5-10), max daily trades (10-20), max daily loss ($10-50), edge threshold (8%+), and half-Kelly position sizing. These are configured in the JSON config files.

## Strategy Notes

- **Longshot bias** (strategy-trader): Becker (2025) model. 1c contracts are overpriced by ~57% (true win rate 0.43% vs 1% implied). Edge decays exponentially: `0.57 * exp(-0.15 * price)`.
- **Weather probability** (weather-bot): Step-function based on NWS forecast error distribution (±3°F 68% CI, ±6°F 95% CI).
- **Info arbitrage** (source-monitor, entertainment-bot): Trade when external data (HDD charts, NWS actuals, box office) confirms outcome before Kalshi settles.

## Research

`research/` contains strategy documentation: `kalshi-deep-dive.md`, `kalshi-info-arbitrage.md`, `kalshi-markets-research.md`. Reference these for Kalshi API details, market structure, and strategy rationale.
