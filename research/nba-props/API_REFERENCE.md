# Real Sports App - API Reference

> **Base URL:** `https://web.realapp.com`
> **Frontend:** `https://www.realapp.com`
> **CDN (JS/CSS):** `https://realsports.io`
> **Media CDN:** `https://media.realapp.com`
>
> Captured: 2026-03-16

---

## Table of Contents

1. [Authentication](#authentication)
2. [Home / Sport Feed](#home--sport-feed)
3. [Live Feed](#live-feed)
4. [Games](#games)
5. [Prediction Markets (Core for Trading Bot)](#prediction-markets)
6. [User / Profile](#user--profile)
7. [Players (Detailed)](#players-detailed)
8. [Player Rankings](#player-rankings)
9. [Player Stat Leaders](#player-stat-leaders)
10. [Team Standings](#team-standings)
11. [Team Rankings](#team-rankings)
12. [Team Stat Leaders](#team-stat-leaders)
13. [Stat Trackers (Player Props)](#stat-trackers)
14. [Cards & Collecting](#cards--collecting)
15. [Card Marketplace](#card-marketplace)
16. [Groups / Chat](#groups--chat)
17. [Activity / Notifications](#activity--notifications)
18. [Messages](#messages)
19. [Leaderboards](#leaderboards)
20. [Squads](#squads)
21. [Players (Search)](#players-search)
22. [Menu / Navigation](#menu--navigation)
23. [Tracking / Analytics](#tracking--analytics)

---

## Authentication

### Login
```
POST /login
```
- Called on initial sign-in
- Returns session/auth token (likely cookie-based or bearer token)
- Subsequent requests are authenticated automatically

---

## Home / Sport Feed

### Get Home Feed (per sport)
```
GET /home/{sport}/next?cohort={cohort}
```
- **sport**: `nba`, `nhl`, `mlb`, `cbb`, `fc`, `ufc`, `golf`, `nfl`, `wnba`, `cfb`
- **cohort**: Integer (observed: `0`)
- Returns: Games list, scores, player rankings, breakout players, top plays, picks

### Get Boost Control
```
GET /home/{sport}/boostcontrol?cohort={cohort}&day={YYYY-MM-DD}
```
- **sport**: Same as above
- **day**: Date string (e.g., `2026-03-16`)
- Returns: Boost/promotion configuration for the day

---

## Live Feed

### Get Global Live Feed
```
GET /livefeed/all/feed
```
- Returns: Real-time feed of all live game events across all sports
- Includes: Play-by-play, scores, player stats, reaction counts

---

## Games

### Get Game Feed
```
GET /games/{gameId}/sport/{sport}/feed?version=2&view=recent&viewFrame=default
```
- **gameId**: Numeric game ID (e.g., `23454`)
- **sport**: `nba`, `nhl`, etc.
- **version**: API version (observed: `2`)
- **view**: `recent` (likely also `all`, `top`)
- **viewFrame**: `default`
- Returns: Game play-by-play feed, scores, player performances

### Get Player Rating Contest
```
GET /games/playerratingcontest/{contestId}?contestType=sport&source=home
```
- **contestId**: Numeric (e.g., `1548` for NBA, `1550` for NHL)
- **contestType**: `sport`
- **source**: `home`
- Returns: Daily draft/player rating contest data

---

## Prediction Markets

> **This is the core section for building a trading bot.**

### Get All Game Markets (per sport)
```
GET /predictions/gamemarkets/{sport}
```
- **sport**: `nba`, `nhl`, etc.
- Returns: All active prediction markets for current games in that sport
- Market types observed: **Game Winner**, **Spread**, **Total (Over/Under)**

### Get Markets for a Specific Game
```
GET /predictions/game/{sport}/{gameId}/markets
```
- **sport**: `nba`, `nhl`, etc.
- **gameId**: Numeric game ID
- Returns: All markets for a specific game with current odds/percentages

### Get Market Detail (Embed)
```
GET /predictions/marketembed/{marketId}
```
- **marketId**: Numeric market ID (e.g., `1943`)
- Returns: Full market detail including current prices, volume, chart data

### Get Market Positions
```
GET /predictions/market/{marketId}/positions
```
- **marketId**: Numeric market ID
- Returns: User's positions in this market, order history

### Get Open Positions (Portfolio)
```
GET /predictions/openpositions
```
- Returns: All of the user's currently open prediction positions across all markets

### Get Portfolio Performance
```
GET /predictions/portfolioperformance?timeframe={timeframe}
```
- **timeframe**: `1d`, `1w`, `1m`
- Returns: P&L data, performance metrics over the specified period

### Get Predictions Feed Config
```
GET /predictions/feed/config
```
- Returns: Configuration for the predictions feed/market listing

---

## User / Profile

### Get Current User
```
GET /user
```
- Returns: Current authenticated user data (username, ID, settings)

### Get User Skins
```
GET /user/{userId}/skins
```
- **userId**: String (e.g., `k3LkNN1v`)

### Get Checklist and Info
```
GET /getchecklistsandinfo
```
- Returns: Onboarding checklists, user info

### Get User Brawl Stats
```
GET /sportbrawluserstats/{userId}
```
- Returns: User's game/brawl statistics

### Get User Featured Card Rows
```
GET /userfeaturedcardrows/{userId}
```
- Returns: User's featured card display configuration

### Get User Card Albums
```
GET /cardalbums/{userId}
```
- Returns: User's card album collections

### Get User Albums
```
GET /user/{userId}/albums
```
- Returns: User's photo/content albums

### Get User Brawl Trophies
```
GET /userbrawltrophies/{userId}?limit={limit}
```
- **limit**: Integer (e.g., `10`)

---

## Cards & Collecting

### Get Collecting Info
```
GET /collecting/{userId}/info
```
- Returns: User's card collection summary

### Get Collecting Packs Shop Info
```
GET /collectingpacks/{sport}/season/{seasonYear}/shopinfo
```
- **sport**: `nba`, `nhl`, etc.
- **seasonYear**: `2025`
- Returns: Available card packs for purchase

---

## Card Marketplace

### Get Marketplace Configuration
```
GET /cardmarketplaceconfiguration
```
- Returns: Marketplace settings, filters, categories

### Get Marketplace Listings
```
GET /cardmarketplacelistings?cohort={cohort}&listingType={type}&prestige={prestige}&rarity={rarity}&season={year}&sport={sport}
```
- **cohort**: `all`
- **listingType**: `all`
- **prestige**: `all`
- **rarity**: `all` (likely also: `common`, `uncommon`, `epic`, `legendary`)
- **season**: `2025`
- **sport**: `nba`, `nhl`, etc.
- Returns: Card listings with bid prices, card details, rarity

---

## Groups / Chat

### Get Group Feed
```
GET /groups/{groupId}/feedadvanced
```
- **groupId**: Numeric (e.g., `777777777` for General)
- Returns: Chat messages, posts in the group

---

## Activity / Notifications

### Get Activity Feed
```
GET /activity
```
- Returns: User notifications, mentions, announcements

### Clear Activity Info
```
PUT /activity/clearinfo
```
- Marks activity/notifications as seen

---

## Messages

### Get Message Channels
```
GET /messages/channels?type={type}
```
- **type**: `default`
- Returns: List of direct message conversations

---

## Leaderboards

### Get Leaderboard Categories
```
GET /leaderboard/{sport}/categories
```
- **sport**: `nba`, `nhl`, etc.
- Returns: Available leaderboard categories/types

### Get Leaderboard Data
```
GET /leaderboard/{sport}/season/{seasonYear}/type/{type}/typevalue/{value}?visibility={visibility}
```
- **sport**: `nba`, `nhl`, etc.
- **seasonYear**: `2025`
- **type**: `weekly`, `monthly`, `season`
- **typevalue**: Week/month number (e.g., `24`)
- **visibility**: `all`
- Returns: Ranked user list with scores

---

## Squads

### Get Squads
```
GET /squads?sport={sport}
```
- **sport**: `nba`, `nhl`, etc.
- Returns: User's squad configurations

---

## Players (Search)

### Search Players
```
GET /players/sport/{sport}/search?includeNoOneOption={bool}&searchType={type}&season={year}
```
- **sport**: `nba`, `nhl`, etc.
- **includeNoOneOption**: `false`/`true`
- **searchType**: `cardsTabUpsell`
- **season**: `2025`
- Returns: Player list with names, status (Active/Out/Questionable)

---

## Players (Detailed)

> **Key for trading bot** — detailed player data for making informed bets.

### Get Player Profile
```
GET /players/{playerId}/sport/{sport}?season={year}
```
- **playerId**: Numeric player ID (e.g., `8481032`)
- **sport**: `nba`, `nhl`, etc.
- **season**: `2025`
- Returns: Full player profile including:
  - Season stats with league rankings (e.g., pts 459th, goals 303rd, hits 22nd)
  - Splits data (averages/totals for last 3/5/10/20 games, season, home/away, per-opponent)
  - 7-day, 30-day, and season rankings
  - Play tags (#leadtaking, #gamewinner, #skateoff, etc.)

### Get Player Season Feed
```
GET /players/{playerId}/sport/{sport}/seasonfeed?limit={limit}&season={year}&view=recent&viewFrame=default
```
- **playerId**: Numeric player ID
- **sport**: `nba`, `nhl`, etc.
- **limit**: Number of entries (e.g., `10`)
- **season**: `2025`
- **view**: `recent`
- Returns: Game-by-game performance feed with ratings, recent performances chart

### Get Player Box Score
```
GET /playerboxscores/{boxscoreId}?version=2
```
- **boxscoreId**: Numeric box score ID
- **version**: `2`
- Returns: Detailed player box score for a specific game performance

### Get Game Packs
```
GET /gamepacks/{gameId}/sport/{sport}
```
- **gameId**: Numeric game ID (e.g., `2025021064`)
- **sport**: `nba`, `nhl`, etc.
- Returns: Game pack data (related card/collectible packs for a game)

### Get Player Box Score Comments
```
GET /comments/type/playerboxscore/entity/{id}?limit=25&sort=top&version=2
```
- **id**: Entity ID for the box score
- **limit**: Number of comments (e.g., `25`)
- **sort**: `top`
- Returns: Community comments on a player's box score performance

### Get Unseen Info for Entity
```
GET /groupunseeninfo?entityId={id}&entityType=playerboxscore
```
- **entityId**: Entity ID
- **entityType**: `playerboxscore`
- Returns: Unseen/unread status for the entity

---

## Player Rankings

> **Key for trading bot** — identify top-performing and trending players.

### Get Player Rankings (7-day)
```
GET /rankings/sport/{sport}/entity/player/ranking/tertiary
```
- **sport**: `nba`, `nhl`, etc.
- Returns: Top 27 players ranked by 7-day performance rating

### Get Player Rankings (30-day)
```
GET /rankings/sport/{sport}/entity/player/ranking/secondary?season={year}
```
- **sport**: `nba`, `nhl`, etc.
- **season**: `2025` (optional)
- Returns: Top 27 players ranked by 30-day performance rating

### Get Player Rankings (Season)
```
GET /rankings/sport/{sport}/entity/player/ranking/primary?season={year}
```
- **sport**: `nba`, `nhl`, etc.
- **season**: `2025` (optional)
- Returns: Top 27 players ranked by full season performance rating

---

## Player Stat Leaders

> **Key for trading bot** — stat-specific leaderboards for player prop analysis.

### Get Available Seasons
```
GET /playerstatleaders/{sport}/seasons
```
- **sport**: `nba`, `nhl`, etc.
- Returns: Available seasons and stat categories for the sport

### Get Stat Leaders
```
GET /playerstatleaders/{sport}/seasons/{year}/seasontypes/{seasonType}/stats/{statId}
```
- **sport**: `nba`, `nhl`, etc.
- **year**: `2025`
- **seasonType**: `regularseason`
- **statId**: Numeric stat identifier (e.g., `1` for points in NHL)
- Returns: Ranked list of players by specific stat (points, goals, assists, hits, etc.)
- Filterable by: season, team, position

---

## Team Standings

### Get Standings Config
```
GET /teamstandings/sport/{sport}
```
- **sport**: `nba`, `nhl`, etc.
- Returns: Conference/division structure and available seasons

### Get Division Standings
```
GET /teamstandings/sport/{sport}/conference/{conference}/division/{division}?season={year}
```
- **sport**: `nba`, `nhl`, etc.
- **conference**: `Eastern`, `Western`
- **division**: `ATL`, `Metro`, etc. (NHL); division names vary by sport
- **season**: `2025`
- Returns: Ranked teams with: points, record (W-L-OT), last 10, streak, home/away records, goals for/against
- Also includes playoff clinch/elimination status

---

## Team Rankings

### Get Team Rankings (7-day)
```
GET /rankings/sport/{sport}/entity/team/ranking/tertiary
```
- **sport**: `nba`, `nhl`, etc.
- Returns: All teams ranked by 7-day performance rating

### Get Team Rankings (30-day)
```
GET /rankings/sport/{sport}/entity/team/ranking/secondary?season={year}
```
- **sport**: `nba`, `nhl`, etc.
- Returns: All teams ranked by 30-day performance rating

### Get Team Rankings (Season)
```
GET /rankings/sport/{sport}/entity/team/ranking/primary?season={year}
```
- **sport**: `nba`, `nhl`, etc.
- Returns: All teams ranked by full season performance rating

---

## Team Stat Leaders

### Get Available Seasons
```
GET /teamstatleaders/{sport}/seasons
```
- **sport**: `nba`, `nhl`, etc.
- Returns: Available seasons and stat categories for team stats

### Get Team Stat Leaders
```
GET /teamstatleaders/{sport}/seasons/{year}/seasontypes/{seasonType}/stats/{statId}
```
- **sport**: `nba`, `nhl`, etc.
- **year**: `2025`
- **seasonType**: `regularseason`
- **statId**: Numeric stat identifier (e.g., `78` for points in NHL)
- Returns: Ranked list of teams by specific stat

---

## Stat Trackers (Player Props)

> **Key for trading bot** — over/under player prop tracking with notifications.

### Get Game Schedule
```
GET /home/{sport}/days?type=condensed
```
- **sport**: `nba`, `nhl`, etc.
- **type**: `condensed`
- Returns: Calendar of games for the sport (date picker data)

### Get Stat Trackers
```
GET /stattrackers?day={YYYY-MM-DD}&sport={sport}
```
- **day**: Date string (e.g., `2026-03-16`)
- **sport**: `nba`, `nhl`, etc.
- Returns: Player over/under stat lines for the day (shots on goal, saves, assists, game totals, etc.)
- Can be used to track player props and set notifications

---

## Menu / Navigation

### Get Home Menu
```
GET /homemenu
```
- Returns: Navigation menu configuration

---

## Tracking / Analytics

### Send Tracking Event
```
POST /tracking/web
```
- Sends analytics/telemetry data
- Returns 401 when not authenticated, 200 when authenticated
