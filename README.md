# 📊 Kalshi Trading

Automated prediction market trading system for [Kalshi](https://kalshi.com). Runs weather forecasting, entertainment/album sales, and information arbitrage strategies.

## ✨ Features

- **Weather Bot** — Trades weather markets using forecast data and HDD analysis
- **Entertainment Bot** — Trades album sales and entertainment outcome markets
- **HDD Scraper** — Scrapes Heating Degree Day data for weather market edge
- **Source Monitor** — Tracks external data sources for information advantages
- **BeatRelease Copy-Trading** — Mirrors signals from BeatRelease for music markets
- **Strategy Trader** — Unified execution engine for all trading strategies
- **Trade Cycling** — Automated multi-round trading cycles

## 🛠 Tech Stack

- **Language:** Python
- **API:** Kalshi REST API
- **Data:** Web scraping, external forecast APIs
- **Config:** YAML/JSON strategy configs

## 🚀 Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env  # Add Kalshi API credentials & strategy params

# Run a specific bot
npm run weather
npm run entertainment
npm run strategy
```

## 📋 Commands

| Command | Description |
|---------|-------------|
| `npm run weather` | Run the weather trading bot |
| `npm run entertainment` | Run the entertainment trading bot |
| `npm run strategy` | Run the unified strategy trader |
| `npm run hdd` | Scrape HDD data |
| `npm run monitor` | Start the source monitor |
| `npm run cycle` | Run a full trade cycle |

## ⚙️ How It Works

1. Bots scrape external data sources for real-time signals
2. Signals are compared against current Kalshi market prices
3. When edge is detected, the strategy trader places orders via the Kalshi API
4. Positions are managed and cycled automatically
