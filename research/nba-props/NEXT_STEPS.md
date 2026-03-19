# Next Steps - Trading Bot Development

## Phase 1: Complete API Discovery

### Captured (56+ endpoints across all categories):
- **Home/Sport feeds** — games, scores, boosts, schedules
- **Live feed** — real-time play-by-play across all sports
- **Game detail** — per-game feeds, player rating contests
- **Prediction markets** — market discovery, positions, portfolio, performance
- **Player profiles** — full stats, league rankings, splits (last N games, home/away, per-opponent)
- **Player rankings** — 7-day, 30-day, season rankings
- **Player stat leaders** — leaderboards by stat category (points, goals, assists, etc.)
- **Player box scores** — detailed per-game player stats
- **Stat trackers** — player over/under prop lines by day
- **Team standings** — division/conference standings with full records
- **Team rankings** — 7-day, 30-day, season team performance ratings
- **Team stat leaders** — team stats by category
- **User/profile** — auth, user data, skins, albums, trophies
- **Cards/marketplace** — collecting, packs, marketplace listings
- **Social** — groups, activity, messages, leaderboards

### Completed:
- **Auth mechanism** - Fully documented (localStorage-based, custom `real-auth-info` header)
- **`real-request-token`** - Reverse-engineered: `Hashids('realwebapp', 16).encode(Date.now())`
- **Rate limits** - Tested: no sequential limit, ~30-40 concurrent limit before HTTP 429
- **WebSocket** - Endpoint `wss://web.realsports.io`, Socket.io with 16 socket types, all events documented

### Still must capture before building:
1. **Trade execution endpoint** (only needed if trading on Real, NOT needed for Kalshi/Polymarket):
   - The exact POST endpoint URL
   - Request body format (amount, outcome, price)
   - Response format (confirmation, position ID)

## Phase 2: Build Core Bot

### Recommended tech stack:
- **Language**: TypeScript/Node.js or Python
- **HTTP Client**: axios/fetch (Node) or httpx/aiohttp (Python)
- **WebSocket**: socket.io-client (matches their backend)
- **Scheduler**: node-cron or APScheduler

### Core modules to build:
1. **AuthClient** - Handle login, token management, session refresh
2. **MarketClient** - Fetch markets, prices, volumes
3. **LiveFeedClient** - Poll or subscribe to live game data
4. **PlayerDataService** - Fetch player profiles, rankings, stats, splits, and prop lines
5. **TradeExecutor** - Place buy/sell orders with position sizing
6. **PositionTracker** - Monitor open positions, P&L
7. **StrategyEngine** - Implement trading logic

## Phase 3: Strategy Ideas

### Momentum-based trading
- Detect scoring runs (e.g., "LAL 23-7 run") from live feed
- Buy the team on a run if market hasn't fully priced it in
- Use the `momentum` field and `tags` from play events

### Player performance-driven trading
- Use player rankings (7-day trending) to identify hot/cold players
- Cross-reference player splits (home/away, per-opponent) with current matchup
- Monitor stat trackers (over/under props) for edge identification
- Use player box scores to detect breakout performances mid-game

### Live odds arbitrage
- Compare Real's market prices to external odds providers (DraftKings, FanDuel)
- Trade when Real's prices diverge significantly from external consensus

### Volume-weighted mean reversion
- Track price movements relative to volume
- Low-volume price swings may revert; high-volume moves may continue

### End-of-game patterns
- Markets may misprice in final minutes when outcomes become near-certain
- Capture the remaining value as games conclude

### Standings-informed trading
- Use team standings data (streak, last 10, home/away records) to calibrate win probability models
- Teams on hot streaks or with strong home records may be underpriced

## Phase 4: Risk Management

- Set maximum position size per market
- Set maximum total exposure across all positions
- Implement stop-loss by monitoring `portfolioperformance`
- Track win rate and expected value per strategy
- Log all trades for post-analysis
