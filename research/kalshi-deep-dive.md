# Kalshi Prediction Markets — Deep Research for Trading Bot

**Last Updated:** February 16, 2026  
**Purpose:** Comprehensive research for building an automated Kalshi trading bot with $100 starting bankroll

---

## Table of Contents
1. [Market Categories & How They Work](#1-market-categories--how-they-work)
2. [Kalshi API Deep Dive](#2-kalshi-api-deep-dive)
3. [Existing Kalshi Bots](#3-existing-kalshi-bots)
4. [Community Discussion & Insights](#4-community-discussion--insights)
5. [Strategy Ideas](#5-strategy-ideas)
6. [Risk Management](#6-risk-management)
7. [Practical Setup](#7-practical-setup)
8. [Recommended First Strategy](#recommended-first-strategy)

---

## 1. Market Categories & How They Work

### Categories Found (from live API data)

| Category | Examples | Typical Volume | Notes |
|----------|----------|---------------|-------|
| **Climate and Weather** | Daily temperature (NYC, Chicago, Miami, Austin, Denver, Houston, Philly), earthquakes, climate goals | **HIGH** — daily markets with frequent turnover | Settles via NWS Daily Climatological Report |
| **Sports** | NCAA basketball spreads/totals, NHL, table tennis, tennis (ATP/WTA), esports (Dota 2, LoL), Stanley Cup | **HIGHEST** — college basketball spreads dominate volume | Binary: team wins, spread covers, over/under totals |
| **Politics** | Next Pope, Speaker of House, G7 leaders, EU exit, Taiwan travel advisory | Medium | Long-dated, lower liquidity |
| **Elections** | UK elections, party wins | Medium | Seasonal spikes |
| **Economics** | Trillionaire race, unemployment, GDP, China overtakes USA | Low-Medium | Long-dated |
| **Financials** | IPO races (OpenAI vs Anthropic, Deel vs Rippling), monopoly cases | Low-Medium | Long-dated |
| **Entertainment** | Next James Bond, GTA VI, TV show releases, music | Low | Fun markets, low liquidity |
| **Science and Technology** | Nuclear fusion, Mars missions, FDA approvals, AI company takeovers | Low | Very long-dated |
| **Companies** | IPOs, CEO replacements, monopoly rulings | Low | |
| **Social** | Population changes, celebrity events | Low | |
| **World** | EU expansion, Elon Musk Mars visit | Low | |

### Market Structure

- **All markets are binary** (Yes/No contracts, $0.00-$1.00)
- **Notional value:** $1.00 per contract
- **Tick size:** $0.01 (1 cent)
- **Settlement:** Automated based on defined resolution sources
- **Weather markets:** 6 brackets (2°F wide for middle 4, edge brackets catch tails)
- **Sports spreads:** Multiple strike points per game (e.g., "Duke wins by over 7.5 points")
- **Mutually exclusive events:** Some categories use MECNET collateral (multi-outcome)

### Highest Volume Markets (from demo API snapshot)

Sports (especially NCAA basketball) dominates by far:
- **Villanova vs Creighton spread:** 472 contracts open interest on one strike alone, liquidity >$51K
- **Texas Tech vs Arizona:** 1,003+ open interest, >$25K liquidity
- **Weather markets:** High daily turnover but smaller per-market size
- **Long-dated politics/entertainment:** Near-zero volume on demo

### Typical Spreads

From live data:
- **High-volume sports:** 2-6 cent spread (e.g., bid 72/ask 86 = 14¢ spread on less liquid; bid 88/ask 94 = 6¢ spread on popular)
- **Weather markets:** Similar 5-15 cent spreads
- **Low-volume markets:** Often 100/100 (no bid/ask) — completely illiquid
- **Best spreads:** Popular NCAA basketball games, 2-4 cents

---

## 2. Kalshi API Deep Dive

### Authentication

RSA key-based signing. Format:
```
message = timestamp_ms + "\n" + method + "\n" + path
signature = base64(PKCS1v15_SHA256_sign(private_key, message))
```

Headers:
```
KALSHI-ACCESS-KEY: <api_key>
KALSHI-ACCESS-SIGNATURE: <base64_signature>
KALSHI-ACCESS-TIMESTAMP: <unix_ms>
```

```python
import time, base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

def sign_request(private_key_path, method, path):
    ts = str(int(time.time() * 1000))
    message = f"{ts}\n{method}\n{path}"
    
    with open(private_key_path, 'rb') as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    
    signature = private_key.sign(
        message.encode(),
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    return ts, base64.b64encode(signature).decode()
```

### Base URLs

| Environment | URL |
|------------|-----|
| Demo | `https://demo-api.kalshi.co/trade-api/v2` |
| Production | `https://trading-api.kalshi.com/trade-api/v2` |

### Key Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/events` | GET | No | List events with filters |
| `/events/{ticker}` | GET | No | Single event details |
| `/markets` | GET | No | List markets with filters |
| `/markets/{ticker}` | GET | No | Single market details |
| `/markets/{ticker}/orderbook` | GET | No | Current orderbook |
| `/portfolio/balance` | GET | Yes | Account balance |
| `/portfolio/positions` | GET | Yes | Current positions |
| `/portfolio/orders` | GET | Yes | List orders |
| `/portfolio/orders` | POST | Yes | **Create order** |
| `/portfolio/orders/{id}` | DELETE | Yes | Cancel order |
| `/portfolio/orders/batched` | POST | Yes | Batch create orders |

### Order Types

```python
# Create order payload
{
    "ticker": "KXNCAAMBSPREAD-26FEB14CLEMDUKE-DUKE13",
    "action": "buy",          # "buy" or "sell"
    "side": "yes",            # "yes" or "no"
    "type": "limit",          # "limit" or "market"
    "count": 10,              # number of contracts
    "yes_price": 43,          # price in cents (for limit orders)
    "expiration_ts": None     # optional GTC otherwise
}
```

- **Limit orders:** Specify price, rests on book until filled/cancelled
- **Market orders:** Fill at best available price immediately

### WebSocket

Kalshi provides WebSocket feeds for real-time data:
- **URL:** `wss://trading-api.kalshi.com/trade-api/ws/v2` (production)
- **Demo:** `wss://demo-api.kalshi.co/trade-api/ws/v2`
- Channels: orderbook updates, trades, market status changes
- Auth required for private channels (positions, fills)

### Rate Limits

| Tier | Read | Write | How to Qualify |
|------|------|-------|---------------|
| **Basic** | 20/sec | 10/sec | Complete signup |
| **Advanced** | 30/sec | 30/sec | Fill out typeform |
| **Premier** | 100/sec | 100/sec | 3.75% of exchange volume/month |
| **Prime** | 400/sec | 400/sec | 7.5% of exchange volume/month |

- Batch cancel: each cancel = 0.2 transactions
- **Recommendation:** Apply for Advanced tier immediately (free, just a form)

### FIX Protocol

Available for Premier/Prime tiers. Lower latency than REST.

---

## 3. Existing Kalshi Bots

### OctagonAI/kalshi-deep-trading-bot

**Architecture:** Simple 6-step pipeline
1. Fetch top 50 events by volume
2. Get top 10 markets per event
3. Deep research via Octagon AI (without seeing odds — prevents anchoring)
4. Fetch current bid/ask
5. OpenAI makes structured betting decisions
6. Execute trades

**Key Config:**
- `MAX_BET_AMOUNT=25.0`
- `HEDGE_RATIO=0.25` (hedge 25% of main bet on opposite side)
- `MIN_CONFIDENCE_FOR_HEDGING=0.6`
- `SKIP_EXISTING_POSITIONS=true`

**Strategy:** AI research-driven directional bets with hedging
**Strengths:** Simple, uses external research for edge
**Weakness:** Depends on expensive Octagon + OpenAI API calls; no published win rate

### ryanfrigo/kalshi-ai-trading-bot

**Architecture:** 5-model AI ensemble with weighted voting

| Model | Role | Weight |
|-------|------|--------|
| Grok-4 | Lead Forecaster | 30% |
| Claude Sonnet 4 | News Analyst | 20% |
| GPT-4o | Bull Researcher | 20% |
| Gemini 2.5 Flash | Bear Researcher | 15% |
| DeepSeek R1 | Risk Manager | 15% |

**Strategy Allocation:**
- 50% Directional trading (Kelly-sized)
- 40% Market making (automated limit orders capturing spreads)
- 10% Arbitrage detection

**Risk Management:**
- Fractional Kelly (0.75x)
- Max 5% of balance per position
- Max 15 concurrent positions
- Daily loss limit: 15%
- Max drawdown: 50%
- Trailing take-profit at 20% gain
- Stop-loss at 15% per position
- 10-day max hold

**Strengths:** Sophisticated multi-model approach, proper risk management
**Weakness:** Very expensive API costs (5 LLM calls per decision), complex setup

### Other Notable Bots

- **yllvar/Kalshi-Quant-TeleBot:** Enterprise-grade with Telegram integration
- **kalshitradingbot.net:** Copy trading service (copies successful traders)
- **Kalshi-Polymarket Arbitrage Bot** (Reddit): Scans for probability divergence between platforms

### Common Pitfalls Across All Bots

1. **API costs exceed profits** — LLM calls can cost $0.10-$2.00 each; with $100 bankroll, this eats margins fast
2. **Overfitting to recent events** — Markets adapt quickly
3. **Liquidity issues** — Many markets have wide spreads or no counterparty
4. **Settlement edge cases** — Especially weather (NWS reporting quirks)
5. **Not accounting for Kalshi's ~7% fee** on profits

---

## 4. Community Discussion & Insights

### Weather Market Edge (Most Discussed)

From Kalshi's official guide and Reddit community:

**The Core Edge:** Weather forecasts are publicly available, but:
- Most retail traders only check 1-2 forecasts
- Multiple weather models (GFS, ECMWF, NAM, HRRR) often diverge from the consensus
- **The market often prices the "consensus forecast" but doesn't account for model disagreement**
- If models diverge, buying cheap tail brackets can be very profitable

**NWS Settlement Quirks (Critical Knowledge):**
- Weather stations report via two systems: hourly and 5-minute
- 5-minute stations have **significant rounding/conversion errors** (F→C→F)
- The actual high = highest 1-minute average from either station, rounded to nearest °F
- **This means the NWS time series you see during the day may differ from the final settlement by 1-2°F**
- Understanding this edge is what separates profitable weather traders

**Resolution Sources:**
- NYC: KNYC (Central Park)
- Miami: KMIA (Miami International Airport)
- Chicago: KMDW (Midway Airport)
- Austin: KAUS (Austin-Bergstrom Airport)
- Denver: KDEN (Denver International Airport)
- Houston: KHOU (Hobby Airport)
- Philadelphia: KPHL (Philadelphia International Airport)

### Kalshi-Polymarket Arbitrage

From r/algotrading:
- **Strategy:** When implied probability diverges between Kalshi and Polymarket, buy YES on one and NO on the other
- **Returns:** Reported 2-5% consistent returns
- **Challenge:** Different settlement rules between platforms can create "false arbitrage"
- **Fees eat margins** — need >3-4% divergence to profit after fees

### General Sentiment

- Weather markets are **the most consistently tradeable** for small bankrolls
- Sports markets have highest volume but odds are efficient (hard to beat the line)
- Long-dated political markets are illiquid but occasionally mispriced
- **Most traders lose money** — the edge goes to those with better data or faster execution

---

## 5. Strategy Ideas

### Strategy 1: Weather Data Alpha (⭐ RECOMMENDED)

**Concept:** Compare multiple weather model forecasts against Kalshi market prices. Buy underpriced brackets.

**Edge:** Most traders use 1-2 forecasts. You use 5+ models via Open-Meteo API.

**Implementation:**
```python
import requests

def get_weather_models(lat, lon):
    """Fetch multiple model forecasts from Open-Meteo"""
    models = ['gfs_seamless', 'ecmwf_ifs025', 'jma_seamless', 'gem_seamless', 'icon_seamless']
    forecasts = {}
    for model in models:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m&temperature_unit=fahrenheit&models={model}"
        resp = requests.get(url)
        data = resp.json()
        forecasts[model] = max(data['hourly']['temperature_2m'][:24])  # Today's high
    return forecasts

# NYC Central Park
forecasts = get_weather_models(40.7829, -73.9654)
# Compare model spread to Kalshi bracket prices
```

**Risk:** Low — weather markets resolve daily, no overnight risk
**Expected edge:** 3-8% when models diverge significantly from market pricing
**Capital needed:** $5-20 per trade

### Strategy 2: Sports Spread Market Making

**Concept:** Place limit orders on both sides of NCAA basketball spread markets, capturing the bid-ask spread.

**Edge:** Many spread strikes have wide spreads (10-20 cents). Place orders inside the spread.

**Implementation:**
- Identify games with multiple spread strikes and >$100 liquidity
- Place limit buy at mid-price minus 3 cents, limit sell at mid-price plus 3 cents
- Cancel unfilled side when one side fills

**Risk:** Medium — you can get stuck with a position if market moves against you
**Expected edge:** 2-5% per round trip
**Capital needed:** $20-50 per market

### Strategy 3: Cross-Platform Arbitrage (Kalshi vs Polymarket)

**Concept:** Monitor identical events on both platforms for probability divergence.

**Edge:** Different user bases, different liquidity, different fee structures create price gaps.

**Challenge:** 
- Different settlement rules (false arbitrage risk)
- Kalshi is USD, Polymarket is USDC
- Fees on both sides (~7% Kalshi, ~2% Polymarket)
- Need >5% divergence after fees to profit

**Risk:** Low if settlement rules truly match
**Capital needed:** $50+ split across platforms

### Strategy 4: NWS Settlement Edge (Weather Sub-Strategy)

**Concept:** Exploit the F→C→F rounding error in NWS 5-minute stations.

**Edge:** When NWS time series shows a temperature right at a bracket boundary, the actual settlement could go either way due to rounding. If the market hasn't priced this in, buy the "wrong" bracket cheaply.

**Implementation:**
- Monitor NWS real-time data during the day
- When temp is at a bracket boundary (e.g., NWS shows 84°F but actual could be 83°F or 85°F)
- Buy cheap contracts in the adjacent bracket

**Risk:** Low per trade (contracts cost 3-10 cents)
**Expected edge:** 10-30% on specific setups (but infrequent)
**Capital needed:** $2-5 per trade

### Strategy 5: News-Driven Event Trading

**Concept:** AI reads breaking news, identifies relevant Kalshi markets, trades before market reacts.

**Challenge:** 
- Markets react within minutes to major news
- Need very fast pipeline (< 60 seconds from news to trade)
- LLM API latency is 2-10 seconds
- Kalshi's API latency adds more

**Risk:** High — news interpretation is noisy, and you're competing with fast traders
**Capital needed:** $10-25 per trade
**Not recommended for $100 bankroll** — too expensive and unreliable

---

## 6. Risk Management

### Position Sizing for $100 Bankroll

| Rule | Value | Rationale |
|------|-------|-----------|
| Max per trade | $5 (5%) | Kelly fraction for small bankroll |
| Max concurrent positions | 5 | $25 max exposure at any time |
| Max daily loss | $10 (10%) | Stop trading for the day |
| Max total drawdown | $30 (30%) | Reassess strategy entirely |
| Min edge required | 8%+ | Don't trade unless model confidence is high |

### Kelly Criterion for Prediction Markets

```
Kelly % = (p * b - q) / b

Where:
- p = your estimated probability of winning
- q = 1 - p
- b = payout odds (net payout / amount risked)

Example: Market price is $0.40 (Yes), you estimate true probability is 55%
- Buy Yes at $0.40: risk $0.40 to win $0.60
- b = 0.60 / 0.40 = 1.5
- Kelly = (0.55 * 1.5 - 0.45) / 1.5 = 0.25 / 1.5 = 16.7%
- With $100 bankroll: bet $16.70
- Use HALF Kelly (8.3%) for safety: bet $8.30
```

**Always use fractional Kelly (0.25x-0.5x).** Full Kelly is too aggressive for prediction markets where probability estimates are uncertain.

### When to Stop Trading

1. **Down 10% in a day** → Stop, review
2. **Down 30% total** → Full strategy review, paper trade for a week
3. **3 consecutive losses** → Reduce position size by 50%
4. **API costs > 5% of bankroll** → Switch to cheaper strategy
5. **Win rate below 45% over 20+ trades** → Strategy doesn't work, pivot

---

## 7. Practical Setup

### Free Weather APIs

| API | Features | Rate Limit | Best For |
|-----|----------|------------|---------|
| **Open-Meteo** | Multiple models (GFS, ECMWF, ICON, GEM, JMA), hourly, free | 10,000 req/day | ⭐ Primary source for model comparison |
| **NWS API** | Official NWS data, real-time observations | No hard limit (be respectful) | Settlement verification, intraday monitoring |
| **Weather.gov** | Same as NWS, different interface | Same | Backup |
| **Visual Crossing** | Free tier: 1000 req/day | 1000/day | Historical backtesting |

### NWS Real-Time Monitoring URLs

```
NYC (KNYC):     https://api.weather.gov/stations/KNYC/observations/latest
Miami (KMIA):   https://api.weather.gov/stations/KMIA/observations/latest
Chicago (KMDW): https://api.weather.gov/stations/KMDW/observations/latest
Austin (KAUS):  https://api.weather.gov/stations/KAUS/observations/latest
Denver (KDEN):  https://api.weather.gov/stations/KDEN/observations/latest
```

### How Fast Do Markets Move After Weather Data?

- **Morning forecast update (~6 AM):** Markets adjust within 15-30 minutes
- **NWS hourly observations:** Markets react within 5-10 minutes
- **Approaching close (2-4 PM local):** Very fast, seconds to minutes
- **Key insight:** The biggest edge is **before markets open** (10 AM previous day) and **during the day as models update**

### Demo API Quick Test

```bash
# Check balance (needs auth)
curl -s "https://demo-api.kalshi.co/trade-api/v2/portfolio/balance" \
  -H "KALSHI-ACCESS-KEY: 64b1b6ff-eac2-4977-919a-fd1b9865f0aa" \
  -H "KALSHI-ACCESS-TIMESTAMP: $(date +%s000)" \
  -H "KALSHI-ACCESS-SIGNATURE: <signed>"

# Get weather events (no auth needed)
curl -s "https://demo-api.kalshi.co/trade-api/v2/events?limit=20&status=open&series_ticker=KXHIGHNY"

# Get specific market orderbook
curl -s "https://demo-api.kalshi.co/trade-api/v2/markets/{ticker}/orderbook"
```

### Recommended Tech Stack

```
Python 3.12+
├── httpx or aiohttp (async HTTP for API calls)
├── cryptography (RSA signing)
├── websockets (real-time data)
├── open-meteo-py or requests (weather data)
├── sqlite3 or DuckDB (trade logging)
├── schedule or APScheduler (cron-like scheduling)
└── rich (console output)
```

---

## Recommended First Strategy

### 🌡️ Weather Temperature Alpha Bot

**Why this strategy:**
1. **Lowest risk** — Daily settlement, no overnight risk, small positions ($3-5)
2. **Clear edge** — Compare 5+ weather models vs market prices using free APIs
3. **No LLM costs** — Pure data-driven, no expensive AI calls
4. **High frequency** — 4-7 cities × daily = 4-7 trades per day
5. **Learnable** — Understand NWS quirks for extra edge
6. **$100 bankroll is sufficient** — $5 max per trade, 20 trades before reassessment

**Phase 1 (Week 1-2): Paper Trading**
1. Set up Open-Meteo API to fetch 5 weather models daily for all cities
2. Calculate "model consensus" and "model spread" (how much models disagree)
3. Compare to Kalshi market prices at 10 AM when markets open
4. Log what you would have traded; track hypothetical P&L
5. Learn NWS station quirks for each city

**Phase 2 (Week 3-4): Demo Trading**
1. Use demo API credentials to place real orders with fake money
2. Build the full pipeline: data → signal → order → track
3. Refine signal thresholds (only trade when model-market divergence > 8%)
4. Verify that your understanding of settlement matches reality

**Phase 3 (Week 5+): Live with $100**
1. Start with $3-5 per trade
2. Focus on 1-2 cities you understand best
3. Track every trade meticulously
4. Scale up position size only after 20+ profitable trades
5. Add intraday NWS monitoring for extra edge

**Target:** 5-10% monthly return ($5-10/month) with <15% max drawdown

**Key Risk:** Weather markets are getting more efficient as more traders discover them. The edge may shrink over time. Be prepared to pivot to sports or other categories.

---

## Appendix: Demo API Credentials

- **API Key:** `64b1b6ff-eac2-4977-919a-fd1b9865f0aa`
- **Private Key:** `/Users/andeslee/Documents/cursor-projects/class-sniper/config/keys/kalshi-demo.pem`
- **Demo Base URL:** `https://demo-api.kalshi.co/trade-api/v2`
- **Production Base URL:** `https://trading-api.kalshi.com/trade-api/v2`
