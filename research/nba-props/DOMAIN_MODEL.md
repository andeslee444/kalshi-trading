# Real Sports App - Domain Model & Data Types

## Core Entities

### Game
```typescript
interface Game {
  gameId: number;              // e.g., 23454
  sport: Sport;                // "nba", "nhl", etc.
  homeTeam: Team;
  awayTeam: Team;
  status: GameStatus;          // "live", "final", "scheduled", "half"
  period: string;              // "Q3", "P2", "OT", etc.
  clock: string;               // "2:18", "0.0", etc.
  homeScore: number;
  awayScore: number;
  viewCount: number;           // e.g., 219700 (displayed as "219.7k")
  excitementRating: number;    // e.g., 6.0
  momentum?: MomentumInfo;
}

type Sport = "nba" | "nhl" | "mlb" | "cbb" | "fc" | "ufc" | "golf" | "nfl" | "wnba" | "cfb";
type GameStatus = "live" | "final" | "scheduled" | "half";
```

### Market
```typescript
interface Market {
  marketId: number;            // e.g., 1943
  gameId: number;
  sport: Sport;
  type: MarketType;
  outcomes: Outcome[];
  volume: number;              // Total volume traded (e.g., 5820000)
  status: "open" | "settled";
}

type MarketType = "game_winner" | "spread" | "total";

interface Outcome {
  name: string;                // "LAL", "HOU", "Under 226.5", "Over 226.5"
  probability: number;         // 0-100 percentage (e.g., 68)
  line?: number;               // For spread: +2.5, -2.5. For total: 226.5
}
```

### Position
```typescript
interface Position {
  marketId: number;
  outcome: string;             // Which side (e.g., "LAL")
  amount: number;              // Size of position
  entryPrice: number;          // Price when entered (percentage)
  currentPrice: number;        // Current market price
  pnl: number;                // Profit/loss
  status: "open" | "settled";
}
```

### Portfolio
```typescript
interface Portfolio {
  openPositions: Position[];
  performance: {
    timeframe: "1d" | "1w" | "1m";
    pnl: number;
    chartData: DataPoint[];
  };
}
```

### Player
```typescript
interface Player {
  name: string;                // "N. Alexander-Walker"
  fps: number;                 // Fantasy points score (e.g., 59.9)
  rating: number;              // Performance rating (e.g., 6.8)
  sport: Sport;
  status: "Active" | "Out" | "Questionable";
  stats: Record<string, number>; // { pts: 41, reb: 7, "3pm": 9 }
}
```

### Player Detail (Full Profile)
```typescript
interface PlayerDetail {
  playerId: number;              // e.g., 8481032
  name: string;                  // "P. Cotter"
  sport: Sport;
  team: string;
  position: string;              // "C", "LW", "RW", "D", "G" (NHL); "PG", "SG", "SF", "PF", "C" (NBA)
  season: string;                // "2025"
  seasonStats: Record<string, number>;  // { pts: 12, goal: 5, hits: 42, ... }
  leagueRankings: Record<string, number>; // { pts: 459, goal: 303, hits: 22, ... }
  splits: PlayerSplits;
  rankings: {
    sevenDay: number;            // 7-day ranking score
    thirtyDay: number;           // 30-day ranking score
    season: number;              // Season ranking score
  };
  tags: string[];                // ["#leadtaking", "#gamewinner", "#skateoff"]
}

interface PlayerSplits {
  averages: SplitRow[];          // Per-game averages
  totals: SplitRow[];            // Cumulative totals
}

interface SplitRow {
  period: string;                // "Last 3", "Last 5", "Last 10", "Last 20", "Season", "Home", "Away", team abbreviation
  stats: Record<string, number>;
}
```

### Player Ranking Entry
```typescript
interface PlayerRankingEntry {
  rank: number;                  // 1-27 (top 27 shown)
  name: string;                  // "Nathan MacKinnon"
  score: number;                 // Rating score (e.g., 288.9 for season, 18.7 for 7-day)
  period: "tertiary" | "secondary" | "primary";  // 7-day, 30-day, season
}
```

### Team Ranking Entry
```typescript
interface TeamRankingEntry {
  rank: number;
  teamName: string;              // "Rangers", "Devils", etc.
  score: number;                 // Rating score (e.g., 100.0)
  period: "tertiary" | "secondary" | "primary";
}
```

### Stat Leader Entry
```typescript
interface StatLeaderEntry {
  rank: number;
  name: string;                  // Player or team name
  statValue: number;             // e.g., 114 (Connor McDavid points)
  statId: number;                // Numeric stat identifier
  sport: Sport;
  season: string;
  seasonType: string;            // "regularseason"
}
```

### Stat Tracker
```typescript
interface StatTracker {
  sport: Sport;
  day: string;                   // "2026-03-16"
  playerName: string;
  statType: string;              // "shots on goal", "saves", "assists", "game total"
  line: number;                  // Over/under line value
  overOdds?: number;
  underOdds?: number;
}
```

### Play / Feed Event
```typescript
interface PlayEvent {
  gameId: number;
  score: string;               // "80-74"
  period: string;              // "Q3"
  clock: string;               // "2:18"
  timeAgo: string;             // "1m", "3m"
  rating: number;              // Excitement rating (e.g., 5.2)
  description: string;         // "25' three"
  player: {
    name: string;              // "L. Doncic"
    statLine: string;          // "31 pt, 3 threes"
  };
  assist?: {
    name: string;
    statLine: string;
  };
  tags: string[];              // ["3-straight made FG", "6-game 30-point streak", "LAL 23-7 run"]
  reactions: {
    views: number;
    upvotes: number;
  };
}
```

### Momentum Info
```typescript
interface MomentumInfo {
  runScore: string;            // "14 - 2", "23 - 7"
  runDuration: string;         // "in last 2:37", "in last 3:31"
}
```

### Standings
```typescript
interface StandingsEntry {
  team: string;
  conference: "Eastern" | "Western";
  division: string;            // "ATL", "Metro", etc.
  rank: number;
  points: number;              // NHL points, or wins for other sports
  record: string;              // "41-20-6"
  last10: string;              // "9-1"
  streak: string;              // "W1", "L2", "OT1"
  homeRecord: string;          // "22-12"
  awayRecord: string;          // "19-14"
  goalsFor?: number;           // NHL
  goalsAgainst?: number;       // NHL
}
```

### Leaderboard Entry
```typescript
interface LeaderboardEntry {
  rank: number;
  username: string;
  score: number;               // Points/karma
  avatarUrl?: string;
}
```

### Card
```typescript
interface Card {
  id: string;
  player: string;
  team: string;
  sport: Sport;
  season: string;              // "2025-26"
  rarity: "Common" | "Uncommon" | "Epic" | "Legendary";
  type: "Performance" | "Goal" | "Assist" | "Shot";
  rating: number;              // e.g., 6.4
  description: string;         // "6' tip-in goal"
  stats: Record<string, any>;
  listingPrice?: number;
  bidCount?: number;
}
```

### User
```typescript
interface User {
  userId: string;              // "k3LkNN1v"
  username: string;            // "mtandes"
  handle: string;              // "@mtandes"
  karma: number;               // 120
  rax: number;                 // 100 (in-app currency)
  stats: {
    rankedDays: number;
    rankedGames: number;
    games: number;
    upvotes: number;
    pollsWon: number;
    cards: number;
    following: number;
    followers: number;
    friends: number;
  };
  joinedDate: string;
}
```

---

## URL Patterns

### Game Page
```
https://www.realapp.com/{shortId}
```
- Example: `https://www.realapp.com/z2DTjHpFGVv`

### Market Page
```
https://www.realapp.com/{shortId}
```
- Example: `https://www.realapp.com/nJxsBF5FGVy`

### User Profile
```
https://www.realapp.com/ (navigate via sidebar)
```

---

## Currency / Units

- **Rax**: In-app currency (user had 100)
- **Karma**: Reputation/activity score (user had 120)
- **Volume**: Displayed in shorthand (e.g., "5.82m", "384.3k")
- **Prices**: Expressed as percentage probability (0-100%)
- **FPS**: Fantasy Points Score for player performance
- **Rating**: Excitement/performance rating scale (appears to be 0-10)
