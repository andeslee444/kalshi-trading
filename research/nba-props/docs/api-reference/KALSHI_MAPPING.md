# Kalshi ↔ Real Sports App — Market Mapping & Strategy

## Kalshi API Overview

- **Production**: `https://api.elections.kalshi.com/trade-api/v2`
- **Demo/Sandbox**: `https://demo-api.kalshi.co/trade-api/v2` (verified working)
- **Auth**: RSA-PSS signed requests (Key ID + Private Key)
- **Rate limits**: Basic tier = 20 read/s, 10 write/s

### Authentication (Verified Working)
```
KALSHI-ACCESS-KEY: 3ea63aee-35d2-45ad-8bfe-6aafa093700f
KALSHI-ACCESS-TIMESTAMP: {ms since epoch}
KALSHI-ACCESS-SIGNATURE: {base64(RSA-PSS-SHA256(timestamp + method + path))}
```

**Tested**: Auth, balance query ($3,447.10), order placement, and order cancellation all confirmed working on demo API.

> **Note**: Key is currently registered on the **demo** environment. To trade with real money, generate a new API key on https://kalshi.com (production). The private key PEM file is stored locally.

---

## Market Type Mapping: Real → Kalshi

### Direct 1:1 Mappings

| Real Market Type | Kalshi Series Ticker | Kalshi Format | Example |
|-----------------|---------------------|---------------|---------|
| **NBA Game Winner** | `KXNBAGAME` | `KXNBAGAME-{date}{away}{home}-{team}` | `KXNBAGAME-26MAR18LALHOU-LAL` |
| **NBA Spread** | `KXNBASPREAD` | `KXNBASPREAD-{date}{away}{home}-{team}{spread}` | `KXNBASPREAD-26MAR17OKCORL-OKC9` |
| **NBA Total (O/U)** | `KXNBATOTAL` | `KXNBATOTAL-{date}{away}{home}-{number}` | `KXNBATOTAL-26MAR17OKCORL-235` |
| **NHL Game Winner** | `KXNHLGAME` | same pattern | `KXNHLGAME-...` |
| **NHL Spread** | `KXNHLSPREAD` | same pattern | `KXNHLSPREAD-...` |
| **NHL Total** | `KXNHLTOTAL` | same pattern | `KXNHLTOTAL-...` |
| **CBB Game Winner** | `KXNCAAMBGAME` | same pattern | `KXNCAAMBGAME-26MAR20FURCONN-CONN` |
| **CBB Spread** | `KXNCAAMBSPREAD` | same pattern | `KXNCAAMBSPREAD-26MAR19HAWARK-ARK3` |
| **CBB Total** | `KXNCAAMBTOTAL` | same pattern | `KXNCAAMBTOTAL-...` |

### Kalshi-Only Market Types (no Real equivalent)

| Kalshi Series | Description | Count |
|--------------|-------------|-------|
| `KXNBAPTS` | NBA Player Points Props | ~158 |
| `KXNBAREB` | NBA Player Rebounds | ~149 |
| `KXNBAAST` | NBA Player Assists | ~130 |
| `KXNBA3PT` | NBA Player 3-Pointers | ~123 |
| `KXNBATEAMTOTAL` | NBA Team Total Points | ~90 |
| `KXNBA2D` | NBA Double-Doubles | ~26 |
| `KXNBA3D` | NBA Triple-Doubles | ~10 |
| `KXNBABLK` | NBA Blocks | ~7 |
| `KXNBASTL` | NBA Steals | ~11 |
| `KXNBA1HWINNER` | NBA 1st Half Winner | ~6 |
| `KXNBA2HWINNER` | NBA 2nd Half Winner | ~6 |
| `KXNCAAMB1HWINNER` | CBB 1st Half Winner | ~48 |
| `KXNCAAMB1HSPREAD` | CBB 1st Half Spread | ~42 |
| `KXNCAAMB1HTOTAL` | CBB 1st Half Total | ~36 |
| `KXNCAAMBFIRST10` | CBB First to 10 Points | ~2 |
| `KXNCAAWBGAME` | Women's CBB Game Winner | ~32 |
| `KXMLBSTGAME` | MLB Spring Training | ~28 |
| `KXATPSETWINNER` | Tennis Set Winner | ~12 |
| `KXATPMATCH` | Tennis Match Winner | ~6 |
| `KXWTAMATCH` | WTA Tennis Match | ~6 |
| `KXWBCTOTAL` | World Baseball Classic Total | ~12 |
| `KXWBCSPREAD` | World Baseball Classic Spread | ~4 |
| `KXAHLGAME` | AHL Hockey Game | ~12 |
| `KXELHGAME` | Euro League Hockey | ~4 |
| `KXCBAGAME` | Canadian Basketball | ~2 |
| `KXMVESPORTSMULTIGAMEEXTENDED` | Multi-Game Parlays | ~637 |
| `KXMVECBCHAMPIONSHIP` | CBB Championship Futures | ~77 |
| `KXMVECROSSCATEGORY` | Cross-Category Parlays | ~286 |

---

## Ticker Format Decoder

Kalshi tickers encode the game info directly:

```
KXNBAGAME-26MAR18LALHOU-LAL
│         │       │  │    └── Team (outcome)
│         │       │  └── Home team abbrev
│         │       └── Away team abbrev
│         └── Date: 2026-MAR-18
└── Series: NBA Game Winner
```

### Team Abbreviation Mapping (Real → Kalshi)

| Real | Kalshi | Team |
|------|--------|------|
| LAL | LAL | Los Angeles Lakers |
| HOU | HOU | Houston Rockets |
| OKC | OKC | Oklahoma City Thunder |
| DEN | DEN | Denver Nuggets |
| MIN | MIN | Minnesota Timberwolves |
| CLE | CLE | Cleveland Cavaliers |
| NYK/NY | ??? | New York Knicks |
| BKN | BKN | Brooklyn Nets |
| CHI | CHI | Chicago Bulls |
| MEM | MEM | Memphis Grizzlies |
| ATL | ATL | Atlanta Hawks |
| DAL | DAL | Dallas Mavericks |
| POR | POR | Portland Trail Blazers |
| IND | IND | Indiana Pacers |
| UTA | UTA | Utah Jazz |
| TOR | TOR | Toronto Raptors |

> Note: Abbreviation mapping needs refinement per-sport. Build a lookup table from actual market data.

---

## Price Comparison: Real vs Kalshi

| Metric | Real | Kalshi |
|--------|------|--------|
| **Price format** | Percentage (0-100) | Dollars (0.00-1.00) |
| **Conversion** | Real 68% = Kalshi $0.68 | Direct divide by 100 |
| **Bid/Ask** | Not visible (single price) | Full orderbook (bid/ask/last) |
| **Volume** | Displayed in app (e.g., "5.82m") | Exact contract count |
| **Settlement** | Binary (win/lose) | Binary ($1.00 or $0.00) |

### Price Translation
```javascript
// Real price (percentage) → Kalshi price (dollars)
const kalshiPrice = realPrice / 100;

// Example: Real says LAL 68% → Kalshi equivalent = $0.68
// If Kalshi LAL is trading at $0.49, that's a $0.19 gap
```

---

## Kalshi Order Placement

### Create Order
```
POST /trade-api/v2/portfolio/orders
```

```json
{
  "ticker": "KXNBAGAME-26MAR18LALHOU-LAL",
  "action": "buy",
  "side": "yes",
  "type": "limit",
  "count": 10,
  "yes_price": 49
}
```

### Key Fields
| Field | Type | Description |
|-------|------|-------------|
| `ticker` | string | Market ticker |
| `action` | enum | `buy` or `sell` |
| `side` | enum | `yes` or `no` |
| `type` | enum | `limit` or `market` |
| `count` | int | Number of contracts |
| `yes_price` | int | Price in cents (1-99) |
| `time_in_force` | enum | `good_till_canceled`, `fill_or_kill`, `immediate_or_cancel` |
| `post_only` | bool | Resting order only (maker) |

### Batch Orders
```
POST /trade-api/v2/portfolio/orders/batched
```
Up to multiple orders in one request.

---

## Authentication Setup (Python)

```python
import time
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Load your private key
with open('kalshi_private_key.pem', 'rb') as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)

KEY_ID = '3ea63aee-35d2-45ad-8bfe-6aafa093700f'

def kalshi_headers(method: str, path: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    path_no_query = path.split('?')[0]
    message = f"{timestamp}{method}{path_no_query}".encode('utf-8')

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )

    return {
        'KALSHI-ACCESS-KEY': KEY_ID,
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode('utf-8'),
        'Content-Type': 'application/json'
    }
```

---

## Strategy Logic

### Strategy 1: Real → Kalshi Price Divergence (Primary)

**Concept**: Real's prediction market prices reflect the crowd's view of game probabilities. Kalshi's prices reflect a different crowd. When they diverge significantly, trade the cheaper side.

**How it works**:
1. Poll Real's `/predictions/gamemarkets/nba` every 5-10 seconds (or subscribe via WebSocket)
2. Poll Kalshi's `GET /markets?series_ticker=KXNBAGAME&status=open` every 5-10 seconds
3. Match games by team names + date
4. Compare implied probabilities:
   - Real: LAL 68% (= $0.68 implied)
   - Kalshi: LAL last=$0.49, ask=$0.49
   - **Gap**: $0.19 — Real thinks LAL is more likely than Kalshi does
5. If gap > threshold (e.g., $0.10), buy on the cheaper platform

**Edge detection**:
```python
real_price = 0.68   # Real's implied probability for LAL
kalshi_ask = 0.49   # Kalshi's ask price for LAL YES

edge = real_price - kalshi_ask  # 0.19
if edge > 0.10:  # 10-cent threshold
    # BUY LAL YES on Kalshi at $0.49
    # Expected value: $0.68 - $0.49 = $0.19 per contract
    place_order(ticker='KXNBAGAME-...LAL', side='yes', action='buy', price=49)
```

**Risk**: Real's price may be wrong. Mitigate by:
- Only trading when Real has high volume (>500k) — more reliable signal
- Requiring larger gaps for less liquid Real markets
- Setting a maximum position size per game

### Strategy 2: Momentum-Driven Live Trading

**Concept**: Use Real's live feed (scoring runs, momentum shifts) to trade on Kalshi before market prices fully adjust.

**How it works**:
1. Subscribe to Real's WebSocket `LiveFeed` for real-time play-by-play
2. Detect momentum events: scoring runs ("LAL 23-7 run"), hot streaks, key player performances
3. Check Kalshi's current price for the game
4. If Kalshi hasn't moved yet (latency advantage), buy the team on a run

**Signal triggers**:
- Scoring run > 10-0 in basketball
- Team takes lead after being down by 10+
- Star player hits a hot streak (4+ consecutive baskets)
- Momentum field shows strong run in short time window

```python
# Detect scoring run from Real's live feed
if run_score_diff >= 10 and run_duration_minutes <= 3:
    team_on_run = identify_team(run_data)
    kalshi_price = get_kalshi_price(game, team_on_run)

    # If Kalshi hasn't priced in the run yet
    if kalshi_price < estimated_fair_value:
        place_order(side='yes', action='buy')
```

### Strategy 3: Player Props Cross-Platform Edge

**Concept**: Use Real's detailed player data (splits, rankings, stat trackers) to find mispriced player props on Kalshi.

**How it works**:
1. Fetch player splits from Real: `/players/{id}/sport/{sport}?season=2025`
   - Last 3/5/10/20 game averages
   - Home vs away splits
   - Per-opponent splits
2. Fetch player prop lines from Kalshi: `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`
3. Compare player's recent performance to prop lines
4. Trade when splits strongly favor over/under

**Example**:
```python
# Real data: LaMelo Ball averages 28.5 pts in last 5 games
# Kalshi: KXNBAPTS-...-CHALAMBALL20-25 (LaMelo 25+ pts) at $0.55

# If his last-5 average strongly exceeds the line:
if player_avg_last5 > kalshi_line + 3:  # 28.5 > 25 + 3 = 28
    # Buy YES on the over
    pass
```

### Strategy 4: End-of-Game Value Capture

**Concept**: As games near completion, outcomes become near-certain but markets may lag. Capture the remaining value.

**How it works**:
1. Monitor Real's live feed for games in the 4th quarter / final period
2. When a team is leading by a large margin with little time left
3. The game winner market should be near $0.95-0.99 but may be at $0.85
4. Buy the near-certain winner at a discount

**Conditions**:
- < 3 minutes remaining
- Lead > 10 points (NBA) / > 3 goals (NHL)
- Kalshi price < 0.90

### Strategy 5: Standings-Informed Pre-Game Trading

**Concept**: Use Real's team standings, rankings, and home/away records to build a probability model and compare to Kalshi's pre-game prices.

**Data inputs from Real**:
- Team standings: W-L record, last 10, streak, home/away splits
- Team rankings: 7-day, 30-day, season performance ratings
- Player rankings: identify teams with top-performing players
- Head-to-head: player per-opponent splits

---

## Bot Architecture (Updated)

```
┌──────────────────────────────────────────────────────────────┐
│                    TRADING BOT                                │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌─────────────────┐  ┌─────────────────┐                   │
│  │   REAL CLIENT    │  │  KALSHI CLIENT   │                  │
│  │  (Data Source)   │  │  (Execution)     │                  │
│  │                  │  │                  │                   │
│  │ • WebSocket feed │  │ • Market polling │                  │
│  │ • REST polling   │  │ • Order placement│                  │
│  │ • Player data    │  │ • Position mgmt  │                  │
│  │ • Hashids auth   │  │ • RSA-PSS auth   │                  │
│  └────────┬─────────┘  └────────┬─────────┘                 │
│           │                      │                            │
│           ▼                      ▼                            │
│  ┌──────────────────────────────────────────┐                │
│  │            MARKET MAPPER                  │               │
│  │  • Match Real games → Kalshi tickers     │               │
│  │  • Normalize team names                   │               │
│  │  • Convert prices (% → $)                │               │
│  └──────────────────┬───────────────────────┘               │
│                     │                                        │
│                     ▼                                        │
│  ┌──────────────────────────────────────────┐               │
│  │          STRATEGY ENGINE                  │               │
│  │  • Price divergence detection             │               │
│  │  • Momentum signals                       │               │
│  │  • Player prop analysis                   │               │
│  │  • End-of-game value capture              │               │
│  └──────────────────┬───────────────────────┘               │
│                     │                                        │
│                     ▼                                        │
│  ┌──────────────────────────────────────────┐               │
│  │          RISK MANAGER                     │               │
│  │  • Max position per market ($50)          │               │
│  │  • Max total exposure ($500)              │               │
│  │  • Min edge threshold ($0.10)             │               │
│  │  • Daily loss limit ($100)                │               │
│  └──────────────────┬───────────────────────┘               │
│                     │                                        │
│                     ▼                                        │
│  ┌──────────────────────────────────────────┐               │
│  │       KALSHI ORDER EXECUTOR               │               │
│  │  • Place limit/market orders              │               │
│  │  • Monitor fills                          │               │
│  │  • Track P&L                              │               │
│  └──────────────────────────────────────────┘               │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## Risk Management Parameters

| Parameter | Suggested Value | Reason |
|-----------|----------------|--------|
| Min edge threshold | $0.10 (10 cents) | Covers fees + noise |
| Max position per market | $50 (50 contracts) | Limit single-game exposure |
| Max total exposure | $500 | Portfolio-level cap |
| Daily loss limit | $100 | Stop trading after bad day |
| Min Real volume | 100k | Only trust high-volume signals |
| Max simultaneous positions | 10 | Manage complexity |
| Kalshi fee | ~$0.01-0.07/contract | Factor into edge calculation |

---

## Complete Sports League Cross-Reference

> **Audit date**: 2026-03-17 — 33,198 non-MVE single-game markets across 2,133 unique series tickers on Kalshi

### Real ↔ Kalshi Overlap (All 10 Real Sports Have Kalshi Equivalents)

| Real Sport | Real Code | Kalshi Series Prefix | Kalshi Markets | Market Types Available |
|-----------|-----------|---------------------|---------------|----------------------|
| **NBA** | `nba` | `KXNBA*` | ~2,455 | Game Winner, Spread, Total, Team Total, 1H/2H Winner, Player Props (PTS/REB/AST/3PT/BLK/STL/2D/3D), Season Wins, Awards, Futures |
| **NHL** | `nhl` | `KXNHL*` | ~727 | Game Winner, Spread, Total, Season Wins, Awards, Futures |
| **MLB** | `mlb` | `KXMLB*` | ~1,673 | Game Winner, Spread, Total, Spring Training, Season Wins, Stat Leaders, Awards |
| **College Basketball (M)** | `cbb` | `KXNCAAMB*` | ~1,975 | Game Winner, Spread, Total, 1H Winner/Spread/Total, First to 10, March Madness Futures |
| **College Basketball (W)** | — | `KXNCAAWB*` | ~1,304 | Game Winner, Spread, Total, March Madness Futures |
| **Soccer (FC)** | `fc` | `KX{league}*` | ~1,500+ | Match Winner (EPL, La Liga, Bundesliga, Serie A, Ligue 1, UCL, UEL, MLS, World Cup) |
| **UFC** | `ufc` | `KXUFC*`, `KXBOX*` | ~364 | Fight Winner, Method of Victory |
| **Golf** | `golf` | `KXPGA*`, `KXLPGA*` | ~1,298 | Tournament Winner, Matchups, Top 5/10/20, Cut Line |
| **NFL** | `nfl` | `KXNFL*` | ~1,131 | Game Winner, Spread, Total, Draft Picks, Awards, Futures, Season Wins |
| **WNBA** | `wnba` | `KXWNBA*` | — | (Off-season; markets appear when season starts) |
| **College Football** | `cfb` | `KXNCAAFB*` | — | (Off-season; markets appear when season starts) |

### Kalshi-Only Sports (No Real Equivalent — Data Advantage Not Available)

| Sport/League | Kalshi Series Prefix | Markets | Notes |
|-------------|---------------------|---------|-------|
| **Tennis (ATP)** | `KXATP*` | ~303 | Match & Set Winners |
| **Tennis (WTA)** | `KXWTA*` | ~88 | Match Winners |
| **F1** | `KXF1*` | ~36 | Race Winner, Constructor |
| **NASCAR** | `KXNASCAR*` | ~262 | Race Winner, Matchups |
| **IndyCar** | `KXINDY*` | ~25 | Race Winner |
| **Esports (CS2)** | `KXCS2*` | ~150+ | Match Winner, Map Winner |
| **Esports (Valorant)** | `KXVAL*` | ~120+ | Match Winner |
| **Esports (LoL)** | `KXLOL*` | ~100+ | Match Winner |
| **AHL Hockey** | `KXAHL*` | ~12 | Game Winner |
| **KHL Hockey** | `KXKHL*` | varies | Game Winner |
| **World Baseball Classic** | `KXWBC*` | ~16 | Game Winner, Spread, Total |
| **J-League (Japan Soccer)** | `KXJLEAGUE*` | varies | Match Winner |
| **K-League (Korea Soccer)** | `KXKLEAGUE*` | varies | Match Winner |
| **Brazilian Serie A** | `KXBRAZIL*` | varies | Match Winner |
| **Canadian Basketball** | `KXCBA*` | ~2 | Game Winner |
| **Euro League Hockey** | `KXELH*` | ~4 | Game Winner |

### Soccer League Breakdown (Real's `fc` → Multiple Kalshi Series)

Real Sports groups all soccer under `fc`. Kalshi splits by league:

| League | Kalshi Prefix | Markets | World Cup Year Boost |
|--------|--------------|---------|---------------------|
| **EPL (Premier League)** | `KXEPL*` | ~275 | — |
| **Champions League (UCL)** | `KXUCL*` | ~276 | — |
| **La Liga** | `KXLALIGA*` | ~210 | — |
| **Serie A** | `KXSERIEA*` | ~371 | — |
| **Bundesliga** | `KXBUNDES*` | ~189 | — |
| **Ligue 1** | `KXLIGUE1*` | ~189 | — |
| **MLS** | `KXMLS*` | ~240 | — |
| **World Cup** | `KXWC*` | ~656 | Active 2026 |

### Summary: Trading Coverage

```
REAL SPORTS (10 sports)  ──────  KALSHI (25+ sports)
━━━━━━━━━━━━━━━━━━━━━━          ━━━━━━━━━━━━━━━━━━━━━
 NBA ──────────────────────────── NBA (full props)
 NHL ──────────────────────────── NHL
 MLB ──────────────────────────── MLB
 CBB ──────────────────────────── NCAAM + NCAAW
 FC  ──────────────────────────── EPL/UCL/La Liga/Serie A/
                                  Bundesliga/Ligue 1/MLS/WC
 UFC ──────────────────────────── UFC + Boxing
 Golf ─────────────────────────── PGA + LPGA
 NFL ──────────────────────────── NFL
 WNBA ─────────────────────────── WNBA (seasonal)
 CFB ──────────────────────────── NCAAFB (seasonal)
                                  ┌─────────────────────┐
                                  │ KALSHI-ONLY:        │
                                  │ Tennis (ATP/WTA)    │
                                  │ F1, NASCAR, IndyCar │
                                  │ Esports (CS2/Val/LoL)│
                                  │ AHL, KHL, WBC       │
                                  │ J-League, K-League  │
                                  │ Brazilian soccer    │
                                  └─────────────────────┘
```

**Key insight**: All 10 of Real's sports have direct Kalshi equivalents. This means **every Real signal can be traded on Kalshi**. Additionally, Kalshi has 15+ sports/leagues that Real doesn't cover — these can't leverage Real's data advantage but could be traded with other signal sources in the future.

---

## Implementation Priority

1. **Market Mapper** — Match Real games to Kalshi tickers (team name normalization, date matching)
2. **Real Client** — Hashids token gen + WebSocket subscription for live prices
3. **Kalshi Client** — RSA auth + market data polling + order placement
4. **Strategy 1** (Price Divergence) — Simplest, most reliable edge
5. **Strategy 2** (Momentum) — Requires live feed, higher alpha potential
6. **Strategy 3** (Player Props) — Deepest data advantage
7. **Risk Manager** — Position sizing, loss limits
8. **Logging & Analytics** — Track all signals and trades for backtesting
