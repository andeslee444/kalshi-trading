# Trading Bot Architecture for Real Sports Prediction Markets

## Overview

Real Sports App operates a prediction market system where users can trade on sports game outcomes. Markets include **Game Winner**, **Spread**, and **Total (Over/Under)** for games across NBA, NHL, MLB, CBB, FC, UFC, Golf, NFL, WNBA, and CFB.

Prices are expressed as **percentages** (e.g., LAL 68%, HOU 32%) representing implied probability. Volume is tracked per market (observed up to 6.21m on a single NBA game).

---

## Core Trading Endpoints

### 1. Market Discovery

| Purpose | Endpoint | Method |
|---------|----------|--------|
| List all active markets for a sport | `GET /predictions/gamemarkets/{sport}` | GET |
| Get markets for a specific game | `GET /predictions/game/{sport}/{gameId}/markets` | GET |
| Get detailed market data | `GET /predictions/marketembed/{marketId}` | GET |
| Get market feed config | `GET /predictions/feed/config` | GET |

### 2. Position Management

| Purpose | Endpoint | Method |
|---------|----------|--------|
| View open positions | `GET /predictions/openpositions` | GET |
| View positions in a market | `GET /predictions/market/{marketId}/positions` | GET |
| Portfolio performance | `GET /predictions/portfolioperformance?timeframe={1d,1w,1m}` | GET |

### 3. Trade Execution (Inferred - needs confirmation)

These endpoints were **not directly observed** (no trades were placed during capture) but are likely based on the UI patterns:

```
POST /predictions/market/{marketId}/buy
POST /predictions/market/{marketId}/sell
POST /predictions/market/{marketId}/order
```

Possible request body:
```json
{
  "outcome": "LAL" | "HOU",
  "amount": 100,
  "price": 68
}
```

> **Important:** You'll need to place a test trade to capture the exact trade execution endpoint and payload format. Watch the network tab when clicking the buy/sell buttons on a market.

### 4. Live Data (for real-time decisions)

| Purpose | Endpoint | Method |
|---------|----------|--------|
| All live game events | `GET /livefeed/all/feed` | GET |
| Sport-specific game data | `GET /home/{sport}/next?cohort=0` | GET |
| Game play-by-play | `GET /games/{gameId}/sport/{sport}/feed?version=2&view=recent&viewFrame=default` | GET |

---

## Market Types Observed

### Game Winner
- Binary outcome: Team A vs Team B
- Example: LAL 68% / HOU 32%
- Volume: Up to ~6m per game

### Spread
- Binary outcome: Team A +X.5 vs Team B -X.5
- Example: LAL +2.5 (75%) / HOU -2.5 (25%)
- Volume: ~400k per game

### Total (Over/Under)
- Binary outcome: Under X.5 vs Over X.5
- Example: Under 226.5 (74%) / Over 226.5 (26%)
- Volume: ~360k per game

---

## Data Available for Strategy

### Real-time Game Data
- Live scores (updated every few seconds)
- Play-by-play events with ratings (e.g., "7.6" excitement score)
- Run/momentum tracking (e.g., "LAL 23-7 run")
- Quarter/period and time remaining
- Player performance stats (pts, reb, ast, etc.)

### Market Data
- Current implied probability (percentage)
- Trading volume per market
- Line/spread values
- Win probability percentages
- Historical chart data (via marketembed)

### Player Data (Detailed)
- **Player profile**: Full stats with league rankings (`/players/{id}/sport/{sport}?season=2025`)
- **Player season feed**: Game-by-game performance with ratings (`/players/{id}/sport/{sport}/seasonfeed`)
- **Player box scores**: Detailed per-game stats (`/playerboxscores/{id}?version=2`)
- **Player rankings**: 7-day, 30-day, and season rankings (`/rankings/sport/{sport}/entity/player/ranking/{period}`)
- **Player stat leaders**: Leaderboards by stat category (`/playerstatleaders/{sport}/seasons/{year}/seasontypes/regularseason/stats/{statId}`)
- **Splits data**: Averages/totals for last 3/5/10/20 games, home/away, per-opponent (included in player profile)
- **Play tags**: #leadtaking, #gamewinner, #skateoff, etc. (included in player profile)
- **Stat trackers**: Player over/under prop lines (`/stattrackers?day={date}&sport={sport}`)

### Contextual Data
- **Standings**: Division/conference standings with full records (`/teamstandings/sport/{sport}/conference/{conf}/division/{div}`)
- **Team rankings**: 7-day, 30-day, and season team performance (`/rankings/sport/{sport}/entity/team/ranking/{period}`)
- **Team stat leaders**: Team stats by category (`/teamstatleaders/{sport}/seasons/{year}/seasontypes/regularseason/stats/{statId}`)
- **Game schedules**: Calendar data by sport (`/home/{sport}/days?type=condensed`)
- Leaderboard data

---

## Suggested Bot Architecture

```
┌─────────────────────────────────────────────┐
│                 TRADING BOT                  │
├─────────────────────────────────────────────┤
│                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
│  │  Market   │  │   Live   │  │ Position │  │
│  │ Scanner  │  │   Feed   │  │ Manager  │  │
│  │          │  │  Monitor │  │          │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
│       │              │              │        │
│       ▼              ▼              ▼        │
│  ┌──────────────────────────────────────┐   │
│  │         Strategy Engine              │   │
│  │  - Momentum detection                │   │
│  │  - Live odds vs model odds           │   │
│  │  - Volume analysis                   │   │
│  │  - Run/streak detection              │   │
│  └──────────────┬───────────────────────┘   │
│                 │                            │
│                 ▼                            │
│  ┌──────────────────────────────────────┐   │
│  │         Order Executor               │   │
│  │  - Place buy/sell orders             │   │
│  │  - Position sizing                   │   │
│  │  - Risk management                   │   │
│  └──────────────────────────────────────┘   │
│                                             │
└─────────────────────────────────────────────┘
```

### Components

1. **Market Scanner** - Polls `GET /predictions/gamemarkets/{sport}` to find active markets and current prices
2. **Live Feed Monitor** - Polls `GET /livefeed/all/feed` for real-time game events and momentum shifts
3. **Position Manager** - Tracks open positions via `GET /predictions/openpositions`
4. **Player Data Service** - Fetches player profiles, rankings, stats, splits, and prop lines for informed betting
5. **Strategy Engine** - Compares live game state + player data to market prices to find mispriced markets
6. **Order Executor** - Places trades when edge is detected

### Polling Strategy

| Data | Endpoint | Suggested Interval |
|------|----------|--------------------|
| Live feed | `/livefeed/all/feed` | 3-5 seconds |
| Market prices | `/predictions/gamemarkets/{sport}` | 5-10 seconds |
| Game detail | `/games/{gameId}/sport/{sport}/feed` | 10-15 seconds |
| Stat trackers | `/stattrackers?day={date}&sport={sport}` | 15-30 seconds |
| Open positions | `/predictions/openpositions` | 30 seconds |
| Player rankings | `/rankings/sport/{sport}/entity/player/ranking/tertiary` | 5 minutes |
| Portfolio P&L | `/predictions/portfolioperformance` | 60 seconds |
| Player profiles | `/players/{id}/sport/{sport}?season={year}` | On-demand (cache 5 min) |
| Team standings | `/teamstandings/sport/{sport}` | 15 minutes |

---

## Authentication (Fully Captured)

Auth is **NOT cookie-based**. It uses custom headers with credentials stored in `localStorage`.

### Auth Storage
Credentials stored in `localStorage` under key `e-accounts`:
```json
{
  "authInfo": {
    "userId": "k3LkNN1v",
    "token": "3293b82e-1fd8-4e87-b6c2-6cd7363dcc70",
    "deviceId": "39bQYPRE"
  }
}
```
Device UUID stored separately: `localStorage['realdeviceuuid']`

### Required Request Headers
Every API request must include these headers:
```
real-auth-info: {userId}!{deviceId}!{token}
real-device-name: {user-agent string}
real-device-type: desktop_web
real-device-uuid: {uuid from localStorage}
real-request-token: {16-char generated token}
real-version: 28
accept: application/json
content-type: application/json
origin: https://www.realapp.com
referer: https://www.realapp.com/
```

### `real-request-token` (Reverse-Engineered)
- **Algorithm**: [Hashids](https://hashids.org/) encoding of the current timestamp
- **Library**: `hashids` npm package (bundled in the app's JS)
- **Parameters**: `new Hashids('realwebapp', 16)`
  - Salt: `"realwebapp"`
  - Minimum length: `16`
  - Alphabet: `abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890` (default)
  - Separators: `cfhistuCFHISTU` (default)
- **Encoded value**: `Date.now()` (Unix timestamp in milliseconds)
- **Generation**: `hashids.encode(Date.now())`
- **TTL**: ~2-5 minutes (server validates the timestamp is recent)
- **Result**: 16-character alphanumeric string

#### Generate in Node.js:
```javascript
const Hashids = require('hashids');
const hashids = new Hashids('realwebapp', 16);
const token = hashids.encode(Date.now());
// e.g., "1dPX7kAJ6j8YZXOW"
```

#### Generate in Python:
```python
from hashids import Hashids
import time
hashids = Hashids(salt='realwebapp', min_length=16)
token = hashids.encode(int(time.time() * 1000))
```

### Login Flow
1. `POST /login` returns the auth info (userId, token, deviceId)
2. Store in localStorage
3. Include `real-auth-info` header on all subsequent requests

---

## Rate Limits (Tested)

| Pattern | Result | Details |
|---------|--------|---------|
| Sequential requests (any endpoint) | **No rate limit** | 50 rapid sequential requests all returned 200 |
| Concurrent requests (~30) | **Partial 429s** | 50 concurrent: 33 OK, 17 rate-limited |
| Concurrent requests (~100) | **More 429s** | 100 concurrent: 64 OK, 36 rate-limited |
| Sequential throughput | **~4-6 req/s** | Limited by network latency (~150-250ms per request) |

### Key Findings
- **No per-minute or per-hour rate limit observed** for sequential polling
- **Concurrent connection limit**: ~30-40 simultaneous requests before 429s start
- **429 response**: Standard HTTP 429, no `Retry-After` header observed
- **Server**: nginx/1.28.0 behind Varnish CDN cache
- **Safe polling rate**: 1 request every 200ms (5 req/s) sequential is safe
- **For a bot**: Use sequential requests with 200-500ms intervals. No need for concurrent bursts.

---

## WebSocket (Real-Time Data)

### Connection Details
- **Endpoint**: `https://web.realsports.io` (upgrades to `wss://web.realsports.io`)
- **Library**: Socket.io (client must use `socket.io-client`)
- **Transport**: WebSocket only (no polling fallback)

### Connection Parameters
```javascript
const socket = io('https://web.realsports.io', {
  transports: ['websocket'],
  reconnectionDelay: 250,
  reconnectionDelayMax: 1000,
  forceNew: true,
  query: {
    socketType: 'LiveFeed',      // see socket types below
    realRequestToken: hashids.encode(Date.now()),
    realVersion: 28,
    // additional query params depend on socketType (e.g., gameId, sport)
  }
});
```

### Socket Types (rooms you can subscribe to)
| Socket Type | Purpose | Key Events |
|-------------|---------|------------|
| `LiveFeed` | Global live play-by-play | `PlaysAdded`, `PlaysUpdated`, `EmojiCountsUpdated`, `MetadataUpdated`, `PlayersUpdated` |
| `Game` | Single game updates | `Updated`, `MarketUpdated`, `CommentAdded`, `InteractionCountsUpdated` |
| `GameMarkets` | All markets for a game | `MarketUpdated` |
| `SportMarkets` | All markets for a sport | `MarketUpdated` |
| `Market` | Single market updates | `MarketUpdated` |
| `Home` | Home feed game updates | `GamesUpdated` |
| `Scores` | Score updates | `GamesUpdated` |
| `User` | User-specific updates | `ActivityUpdated`, `UserUpdated`, `LiveGameInfoUpdated`, `BatchUpdate` |
| `PredictionMarketOrder` | Prediction market trades | `MarketUpdated`, `OrderUpdated`, `GetExpectedPayout` |
| `PlayerBoxScore` | Player box score live | `PlayerBoxScoreUpdated`, `PulseUpdated` |
| `GroupChat` | Chat messages | `CommentAdded`, `UserTyping`, `CommentDeleted` |
| `GroupFeed` | Group feed | `ItemAdded`, `CommentAdded`, `ReplyAdded` |
| `CardMarketplace` | Card marketplace | `ListingEnded`, `BidUpdate` |

### Key Events for Trading Bot
```javascript
// Market price changes (most important)
socket.on('GameMarketUpdated', (data) => { /* market odds changed */ });
socket.on('SportMarketsMarketUpdated', (data) => { /* sport-wide market update */ });
socket.on('MarketMarketUpdated', (data) => { /* single market update */ });
socket.on('PredictionMarketUpdated', (data) => { /* prediction market update */ });
socket.on('PredictionMarketOrderUpdated', (data) => { /* order/trade update */ });

// Live game events
socket.on('LiveFeedSocketPlaysAdded', (data) => { /* new plays */ });
socket.on('LiveFeedSocketPlaysUpdated', (data) => { /* play updates */ });
socket.on('LiveFeedSocketPlayersUpdated', (data) => { /* player stat updates */ });

// Score changes
socket.on('GameUpdated', (data) => { /* game state change */ });
socket.on('HomeGamesUpdated', (data) => { /* home feed game updates */ });
```

### Socket Lifecycle Events
```javascript
// Socket.io internal events
socket.on('SocketInitialData', (data) => { /* initial state on connect */ });
socket.on('SocketRefreshData', (data) => { /* full refresh */ });
socket.on('SocketShallowRefreshData', (data) => { /* partial refresh */ });
```

### All Endpoints Summary
| Domain | Purpose |
|--------|---------|
| `https://www.realapp.com` | Frontend SPA |
| `https://web.realapp.com` | REST API |
| `https://web.realsports.io` | WebSocket (Socket.io) |
| `https://mobileweb.realapp.com` | Mobile web API |
| `https://media.realapp.com` | Media CDN |
| `https://mediaservice.real.vg` | Media service |
| `https://realsports.io` | CDN (JS/CSS bundles) |
| `https://staging.real.vg` | Staging environment |

---

## Important: Still Missing

1. **Trade execution** - NOT needed if trading on Kalshi/Polymarket instead of Real

---

## Supported Sports

| Sport Code | Full Name |
|-----------|-----------|
| `nba` | NBA Basketball |
| `nhl` | NHL Hockey |
| `mlb` | MLB Baseball |
| `cbb` | College Basketball |
| `fc` | Football Club (Soccer) |
| `ufc` | UFC Fighting |
| `golf` | Golf |
| `nfl` | NFL Football |
| `wnba` | WNBA Basketball |
| `cfb` | College Football |
