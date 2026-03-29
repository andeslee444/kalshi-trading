# Oracle Source-of-Truth Spec

Date: 2026-03-19
Observed live checks run on 2026-03-20 UTC using repo `.env` credentials.

## Purpose

Define one canonical primary source per Oracle data object before more collector work.

Rules:

- One primary source per data object.
- No silent fallback in production or alpha research.
- If the primary source is empty, degraded, or inconsistent, fail closed and record a source failure.
- Alternate sources are allowed only for diagnostics, shadow comparison, or backfill, and must be tagged separately.

## Decisions

| Data object | Canonical primary | Why | Fail-closed condition | Alternates allowed only for |
| --- | --- | --- | --- | --- |
| Scheduled NBA game universe | Real `GET /home/{sport}/days?type=condensed` | Best observed forward coverage. Live check returned 14 days with `2026-03-20` count `6` while `game_markets` returned `0`. | Missing day list or no games on an expected slate day. | Shadow comparison with `home/{sport}/next` |
| Same-day game metadata and contextual lines | Real `GET /home/{sport}/next` | Rich current-day payload with `games`, `status`, `homeMoneyline`, `awayMoneyline`, `pointSpread`, `overUnder`. Good for context, not crowd price. | Empty `latestDayContent.games` on a day where schedule shows games. | UI/context checks |
| Book A crowd game probability | Real `GET /predictions/gamemarkets/{sport}` | This is the only verified Real endpoint shaped like crowd-market pricing. It should own crowd probability when available. | Empty response when Book A is expected to trade. Book A must disable, not substitute. | Diagnostics against `game_detail.market` when that field is observed live |
| Tradeable Kalshi game universe | Kalshi `get_all_markets("KXNBAGAME", "open")` | Oracle should map against Kalshi's live daily game-winner series, not the season-futures `KXNBA-26-*` universe. | Empty open game-market set on a day with known games. | None |
| Executable price, spread, and displayed depth | Kalshi `GET /markets/{ticker}/orderbook` | This is the executable truth for entry logic and latency studies. | Missing orderbook or unusable empty book on a target market. | Historical candlesticks/trades for backtest only |
| Player universe and ID lookup | Real `GET /players/sport/{sport}/search` | Verified live. Returned `50` NBA players in sample query and stable player IDs. | Empty player search during active season. | Cached player map |
| Player static metadata | Real `GET /players/{id}/sport/{sport}` | Verified live. Returns team, position, season card, player metadata. | Empty profile for a mapped player. | Cached profile |
| Player recent game logs and minutes | Real `GET /players/{id}/sport/{sport}/seasonfeed` | Verified live. Returns `playerBoxScores`, `statsInfo`, and recent performance structures needed for Book B. | Empty season feed for an active mapped player. | Stored research gamelogs |
| Pregame prop tradeable universe | Kalshi open NBA prop markets | Tradeable universe must come from Kalshi, not Real. Real `stattrackers` was empty on both `2026-03-19` and `2026-03-20`. | Empty filtered prop market set when Kalshi clearly has NBA props open. | None |
| Pregame prop line reference | Kalshi market ticker and orderbook | Oracle is pricing and trading Kalshi markets, so the line reference must be Kalshi-native. | Ticker parser or market mapping failure. | Real `stattrackers` only as shadow comparison if it becomes non-empty |
| Live event trigger for Book C | Real WebSocket events | Fastest observed live-state path and the core hypothesis for H1 latency. | WebSocket stale, disconnected, or auth invalid. Book C disables. | REST `livefeed/all/feed` shadow monitor |
| Live game-state confirmation and backfill | Real `GET /games/{gameId}/sport/{sport}/feed` | Verified live. Returns `game`, `plays`, `hasMarket`, and feed structure needed for event confirmation and backfill. | Empty detail/feed for a mapped live game. | Stored feed snapshots |
| Historical Kalshi price truth | Kalshi historical candlesticks and historical trades | Canonical research truth for price-aware backtests. | Missing historical series for a market under study. | Own forward quote ledger |
| Own signal/quote/fill truth | Oracle SQLite alpha ledger | Canonical internal truth for signal-time snapshots, source failures, and fill attribution. | Ledger write failure. | None |

## Explicit non-primaries

These were considered and rejected as primaries for now.

### Real `GET /predictions/gamemarkets/{sport}` for general game discovery

Rejected as a discovery primary.

Observed on 2026-03-20 UTC:

- `game_markets` returned `0`
- Real schedule still showed `14` days and `2026-03-20` had `6` games

Conclusion:

- `game_markets` is a crowd-price source only.
- It is not reliable enough to own the base scheduled game universe.

### Real `GET /home/{sport}/next` for crowd pricing

Rejected as a crowd-price primary.

Observed fields included:

- `homeMoneyline`
- `awayMoneyline`
- `pointSpread`
- `overUnder`

But the payload did not show crowd-probability or crowd-market fields analogous to Book A pricing.

Conclusion:

- `home` is contextual line data, not the canonical crowd-price object for Oracle.

### Real `GET /stattrackers?day={date}&sport=nba`

Rejected as a primary source for Book B prop discovery or line reference.

Observed on:

- `2026-03-19`: `0` trackers
- `2026-03-20`: `0` trackers

Conclusion:

- Book B cannot depend on `stattrackers` as a primary.
- If it becomes populated later, it should first be used as a shadow comparison source only.

### Real `game_detail.market`

Rejected as a primary crowd-price source until directly observed live with populated market fields.

Observed on final game `23474`:

- `hasMarket = true`
- `market = null`

Conclusion:

- Not good enough yet to assign as primary.

## Book-level source assignments

### Book A

- Game discovery: Real schedule
- Current-day context: Real home feed
- Crowd probability: Real game markets
- Tradeable universe: Kalshi open game markets
- Executable price: Kalshi orderbook

Operational rule:

- If Real game markets are empty, Book A is unavailable. Do not substitute `homeMoneyline`, sportsbook odds, or any other derived proxy.

### Book B

- Game schedule and start times: Real schedule
- Player ID discovery: Real player search
- Player history / minutes / recent performances: Real season feed
- Player metadata: Real player profile
- Tradeable universe and prop line reference: Kalshi open prop markets + Kalshi orderbook

Operational rule:

- Do not use Real stat trackers as the primary source for Book B until they are revalidated live and consistently non-empty.

### Book C

- Live trigger: Real WebSocket
- Event confirmation and backfill: Real game detail feed
- Executable market view: Kalshi orderbook

Operational rule:

- If WebSocket health is degraded, Book C disables. Do not mask that degradation with hidden REST substitution in production.

## Immediate implementation implications

1. Oracle latency probe should no longer treat Real `game_markets` as the primary discovery source.
2. The probe should split:
   - scheduled game discovery from Real schedule
   - crowd-price availability from Real game markets
3. Book A health should explicitly track crowd-price availability, separate from schedule availability.
4. Book B mapping should be Kalshi-props-first plus Real-player-data, not Real-stattrackers-first.
5. Book C should stay Real-WebSocket-first and fail closed on live source degradation.

## Highest-priority next code change

Refactor the latency probe into two explicit source checks:

- `schedule availability probe`
- `crowd-price availability probe`

This keeps the source model aligned with the decisions above and avoids mixing different data objects under one source-health flag.
