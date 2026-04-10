# Kalshi Trading

Automated prediction market trading system for [Kalshi](https://kalshi.com). Python bots execute weather forecasting, entertainment/album sales information arbitrage, longshot bias exploitation, and copy-trading strategies against the Kalshi REST API.

## Architecture

All bots share a common authentication module (`kalshi_auth.py`) that handles RSA-PSS request signing, automatic retry with exponential backoff, and demo/production endpoint switching. Each bot follows the same pattern: import shared auth, load config, fetch external data, calculate edge, and place limit orders.

```
src/kalshi/
  kalshi_auth.py          Shared auth client, logging, trade I/O
  weather-bot.py          Temperature forecast trading (daemon)
  entertainment-bot.py    Album sales / box office arbitrage (daemon)
  source-monitor.py       Multi-source settlement monitor (daemon)
  strategy-trader.py      Longshot bias exploitation (one-shot)
  beatrelease-scanner.py  Blog copy-trading via LLM (daemon)
  hdd-scraper.py          HITS Daily Double data collection (one-shot / monitor)
  demo-trader.py          End-to-end trading flow test (one-shot)
  check-settlements.py    Portfolio diagnostics (one-shot)

scripts/
  trade-cycle-2.py        Multi-strategy scan + trade cycle (one-shot)
  analyze-performance.py  Cross-bot analytics + API reconciliation

config/
  kalshi-config.json          Weather bot settings (cities, risk limits)
  kalshi-monitor-config.json  Source monitor settings (intervals, stations)
  bots-config.json            Entertainment, BeatRelease, strategy settings
  keys/                       RSA private keys (gitignored)

data/                         Trade logs, snapshots, state files (gitignored)
research/                     Strategy documentation and market research
archive/                      Superseded cycle iterations (historical reference)
tests/                        Unit tests for pure functions
```

## Bots

### Weather Bot (`weather-bot.py`)

Daemon that scans KXHIGH temperature markets every 30 minutes. Fetches 7-day forecasts from the Open-Meteo API, computes YES/NO probability using a step-function model based on NWS forecast error distribution (+-3F = 68% CI, +-6F = 95% CI), and places limit orders when edge exceeds 8%.

- **Markets**: KXHIGH (e.g. `KXHIGHMIA-26FEB16-T86`)
- **Data source**: Open-Meteo forecast API
- **Risk**: $5/trade max, 10 trades/day, 8% edge threshold
- **Trade log**: `data/kalshi-trades.json`

### Entertainment Bot (`entertainment-bot.py`)

Daemon that monitors HITS Daily Double (album sales) and Box Office Mojo / The Numbers (movie grosses) every 15 minutes. When external data reveals a market outcome before Kalshi settles, places limit orders on the confirmed side.

- **Markets**: Auto-discovered via keyword matching (ALBUM, BOX, MOVIE, MUSIC, BILLBOARD, OSCAR, GRAMMY, etc.)
- **Data sources**: HITS Daily Double (web scraping), Box Office Mojo, The Numbers
- **Risk**: $5/trade max, 10 trades/day, 85% confidence threshold
- **Trade log**: `data/kalshi-entertainment-trades.json`

### Source Monitor (`source-monitor.py`)

Persistent daemon that independently polls three settlement data sources on separate timers. Trades when external data confirms an outcome before Kalshi's settlement window.

| Source | Interval | Markets | Method |
|--------|----------|---------|--------|
| HITS Daily Double | 15 min | Album sales (KXALBUMSALES) | HTML scraping + regex |
| Box Office Mojo / The Numbers | 30 min (Fri-Mon) | Movie grosses (KXBOXOFFICE) | HTML scraping |
| NWS weather.gov | 10 min | Temperature (KXHIGH) | JSON API, trades after 3 PM |

- **Risk**: $10/trade max, 20 trades/day, 10% edge threshold
- **Trade log**: `data/kalshi-monitor-trades.json`
- **Snapshots**: `data/kalshi-source-snapshots/` (timestamped source data for audit trail)

### Strategy Trader (`strategy-trader.py`)

One-shot script that exploits the favourite-longshot bias documented in Becker (2025). Scans all open markets for YES contracts priced at 1-10 cents, estimates true probability using the Becker model (`edge = 0.57 * exp(-0.15 * price)`), and sells longshots (buys NO) with half-Kelly position sizing.

- **Markets**: All open markets (longshots at <=10c)
- **Data source**: Kalshi API only (no external data)
- **Risk**: $5/trade max, half-Kelly sizing, top 5 trades per run
- **Trade log**: `data/kalshi-strategy-trades.json`

### BeatRelease Scanner (`beatrelease-scanner.py`)

Daemon that monitors BeatRelease.com blog for Kalshi prediction posts every 4 hours. Extracts trade recommendations via DeepSeek LLM, validates ticker/price/quantity, and places demo trades. Supports `--once` flag for single scan.

- **Markets**: Extracted from blog posts via LLM
- **Data sources**: BeatRelease blog (HTML scraping + BeautifulSoup), DeepSeek chat API
- **Risk**: $5/trade max, 10 contracts max per trade
- **Trade log**: `data/beatrelease-trades.json`
- **State**: `data/beatrelease-state.json` (tracks seen URLs)

### HDD Scraper (`hdd-scraper.py`)

Data collection tool that queries HITS Daily Double's Sanity CMS API directly (not web scraping) for album chart data and articles. Parses tab-separated chart format, extracts sales numbers from article text, and matches to Kalshi markets. Data-only — does not place trades automatically.

```bash
python3 src/kalshi/hdd-scraper.py scan              # One-shot scan
python3 src/kalshi/hdd-scraper.py charts             # Fetch charts only
python3 src/kalshi/hdd-scraper.py articles           # Fetch articles only
python3 src/kalshi/hdd-scraper.py monitor --interval 15  # Loop every 15 min
```

### Trade Cycle (`scripts/trade-cycle-2.py`)

One-shot script that runs a complete trading session: checks balance and positions, scans up to 80,000 markets, identifies longshot and near-settlement opportunities, scores them by category and price, places up to 5 trades (3 longshot sells + 2 near-settlement), and logs results with a markdown performance report.

### Utility Scripts

- **Demo Trader** (`demo-trader.py`) — End-to-end test of the trading flow: authenticate, fetch markets, categorize, place 3 test trades, verify positions.
- **Check Settlements** (`check-settlements.py`) — Diagnostic dump of portfolio settlements, balance, positions, fills, and order status.

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env: set KALSHI_API_KEY and KALSHI_KEY_FILE

# Place your RSA private key
# Demo: config/keys/kalshi-demo.pem
# Live: config/keys/kalshi-live.pem

# (Optional) For BeatRelease scanner, add DeepSeek API key
# config/keys/deepseek.txt
```

## Deploy Layout

Operational deploy roots are separate Git worktrees, not separate repositories:

- dev: `Documents/cursor-projects/kalshi-trading`
- demo: `~/deploy/kalshi-demo`
- prod: `~/deploy/kalshi-prod`
- oracle demo: `~/deploy/kalshi-oracle-demo`

See [docs/desk-ops/runbooks/fresh-mac-rebuild-and-deploy.md](docs/desk-ops/runbooks/fresh-mac-rebuild-and-deploy.md) for the clean-Mac restore flow and the deploy bootstrap/install commands.

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `KALSHI_API_KEY` | Yes | — | Kalshi API key |
| `KALSHI_KEY_FILE` | No | `config/keys/kalshi-demo.pem` | Path to RSA private key |
| `KALSHI_MODE` | No | `demo` | `demo` or `production` |

## Running

All bots can be run via npm scripts or directly with Python:

| Command | Script | Mode |
|---------|--------|------|
| `npm run weather` | `src/kalshi/weather-bot.py` | Daemon (30 min loop) |
| `npm run entertainment` | `src/kalshi/entertainment-bot.py` | Daemon (15 min loop) |
| `npm run monitor` | `src/kalshi/source-monitor.py` | Daemon (10-30 min loops) |
| `npm run strategy` | `src/kalshi/strategy-trader.py` | One-shot |
| `npm run hdd` | `src/kalshi/hdd-scraper.py` | One-shot |
| `npm run cycle` | `scripts/trade-cycle-2.py` | One-shot |
| `npm run demo` | `src/kalshi/demo-trader.py` | One-shot |

The BeatRelease scanner runs separately:

```bash
python3 src/kalshi/beatrelease-scanner.py          # Daemon (4h loop)
python3 src/kalshi/beatrelease-scanner.py --once    # Single scan
```

## Testing

Tests cover pure functions (no API calls required):

```bash
pytest tests/ -v                    # Run all tests (79 tests)
pytest tests/test_kelly.py          # Half-Kelly position sizing
pytest tests/test_weather.py        # Ticker parsing, probability model
pytest tests/test_hdd_parser.py     # Chart parsing, sales extraction
pytest tests/test_trades.py         # Trade file I/O
pytest tests/test_performance.py    # Reconciliation logic, Sharpe ratio
```

## Performance Analysis

```bash
# Offline analysis (reads local trade logs, no API needed)
python3 scripts/analyze-performance.py
python3 scripts/analyze-performance.py --json

# With API reconciliation (computes win rate, P&L, Sharpe ratio)
python3 scripts/analyze-performance.py --reconcile
python3 scripts/analyze-performance.py --reconcile --json
```

Reads trade logs from all 4 bots (`data/kalshi-trades.json`, `data/kalshi-strategy-trades.json`, `data/kalshi-entertainment-trades.json`, `data/beatrelease-trades.json`) and reports per-bot and aggregate statistics. The `--reconcile` flag queries Kalshi's settlement and fill endpoints to compute actual win rates, P&L, and annualized Sharpe ratio.

## Risk Controls

All bots enforce configurable limits:

| Control | Weather | Entertainment | Monitor | Strategy | BeatRelease |
|---------|---------|---------------|---------|----------|-------------|
| Max per trade | $5 | $5 | $10 | $5 | $5 |
| Max daily trades | 10 | 10 | 20 | 5 | ~3-5 |
| Edge threshold | 8% | 5% | 10% | ~3% (Becker) | — |
| Position sizing | Fixed | Fixed | Fixed | Half-Kelly | Fixed |
| Scan interval | 30 min | 15 min | 10-30 min | One-shot | 4h |

Configuration lives in `config/kalshi-config.json` (weather), `config/kalshi-monitor-config.json` (source monitor), and `config/bots-config.json` (entertainment, beatrelease, strategy).

## Strategy Notes

**Longshot bias** — Becker (2025) found that 1-cent contracts on Kalshi are overpriced by ~57% (true win rate 0.43% vs 1% implied). Edge decays exponentially with price: `0.57 * exp(-0.15 * price)`. The strategy trader and trade cycle exploit this by selling longshots (buying NO).

**Weather probability** — Step-function model based on NWS forecast error distribution. A forecast 3F above the threshold maps to ~85% YES probability; 6F above maps to ~95%. Trades when our probability estimate diverges from the market price by more than the edge threshold.

**Information arbitrage** — HITS Daily Double publishes album sales data hours before Kalshi settles entertainment markets. NWS publishes actual temperatures before temperature markets close. The source monitor and entertainment bot detect these windows and trade on confirmed outcomes.

## API Endpoints

Controlled by `KALSHI_MODE` environment variable:

- **Demo**: `https://demo-api.kalshi.co/trade-api/v2`
- **Production**: `https://trading-api.kalshi.com/trade-api/v2`

## Dependencies

- `requests` — HTTP client for Kalshi API and external data sources
- `cryptography` — RSA-PSS request signing
- `beautifulsoup4` — HTML parsing for web scraping
- `pytest` — Test framework

## Research

The `research/` directory contains strategy documentation:

- `kalshi-deep-dive.md` — Comprehensive Kalshi API analysis, community insights, strategy ranking
- `kalshi-info-arbitrage.md` — Settlement source monitoring: which sources publish before Kalshi settles, timing windows, scraping methods
- `kalshi-markets-research.md` — Academic findings (Becker 2025, Whelan 2025), strategy rankings, mathematical methods, cross-platform arbitrage analysis

