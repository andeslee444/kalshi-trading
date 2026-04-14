# Source Monitor (source_monitor.py)

Information arbitrage bot trading on real-time data before Kalshi settles markets. Daemon bot with adaptive polling (5-10 min).

## What it does

Three data sources (only NWS enabled on prod):
- **NWS weather** — Fetches actual temperature observations from NWS stations, trades KXHIGH same-day markets when running high diverges from market pricing
- **HDD (album sales)** — Scrapes HITS Daily Double for first-week sales data (disabled on prod)
- **Box office** — Scrapes Box Office Mojo for opening weekend data (disabled on prod)

## Key functions

- `get_nws_weather_markets()` — Fetches KXHIGH markets by `series_ticker` per configured NWS city. Uses `_weather_series_ticker()` for T-prefix handling. Falls back to prefix scan if all series queries fail.
- `check_nws()` — Fetches latest NWS observations for all configured stations
- `check_nws_daily_highs()` — Fetches all observations since local midnight, computes running high
- `match_nws_to_markets()` — Matches running highs to today's KXHIGH markets, evaluates edge, places trades
- `_nws_min_edge()` — Adaptive min edge threshold based on CI confidence (5%/10%/15%/20%)
- `_weather_series_ticker(city_code)` — Returns `KXHIGHT{city}` for T-prefix cities, `KXHIGH{city}` for originals

## Critical: Market fetch must use series queries

`get_markets_by_prefix("KXHIGH")` paginates through ALL 55k+ open markets on prod and frequently returns 0 results. The `get_nws_weather_markets()` function was added to fix this — it queries by `series_ticker` per city. **Never revert NWS market fetching to `get_markets_by_prefix`.**

## Kalshi T-prefix cities

Same as weather bot — `_T_PREFIX_CITIES` set handles the split. See CLAUDE-weather-bot.md for details.

## NWS trading gates

All must pass for a trade:
1. **Market settles today** — uses per-city local date (`_local_today(city)`)
2. **Pre-dawn gate: after 8 AM local** — running high meaningless before temps climb
3. **Observation freshness: < 90 min** — rejects stale NWS data
4. **`disableYes: true`** — YES-side disabled. Only NO trades on prod.

## Polling frequency

- **10 AM – 4 PM ET**: every 5 minutes (peak — running highs still developing)
- **Off-peak**: every 10 minutes (config `intervalMinutes`)

Note: Peak window is ET-only. Austin (CT) and Denver (MT) peak 1-2 hours later locally. This is a known limitation.

## Config

- `config/kalshi-monitor-config.json` — risk limits, source settings, NWS stations
- `config/bots-config.json` → `monitor` section — enabled flag

### Key config flags

- `sources.nws.disableYes: true` — **Must stay true on prod.** YES-side has 39% WR (-$124). NO-side is 65% WR (+$5,646).
- `sources.nws.stations` — Dict of city code → NWS station ID. Adding a city here automatically enables trading for it.
- `sources.nws.cityMinEdgeAdders` — Per-city edge threshold bumps (e.g., CHI +5%, LAX +5%)

## Prod deployment

- Config: 7 NWS cities (AUS, CHI, DAL, DEN, MIA, PHX, PHIL)
- Trade log: `data/kalshi-monitor-trades.json`
- Uses quarter-Kelly sizing (more conservative than weather bot's half-Kelly)

## Production findings (Apr 2026)

- NO-side is the only profitable side. YES-side is disabled.
- Best cities (NO-only demo data): AUS (+$2,252), MIA (+$2,544), DEN (+$408), PHIL (+$327)
- Biggest per-trade profits come from overnight/late-night hours when markets are stale
- The source monitor was completely broken on prod for 3 days (zero trades) due to `get_all_markets` pagination failure — fixed with series-level queries

## File ownership (from root CLAUDE.md)

Can modify: `source-monitor.py`, `hdd_parser.py`, `test_source*.py`

Cannot modify: `probability.py`, `kalshi_auth.py`, other bots, shared modules
