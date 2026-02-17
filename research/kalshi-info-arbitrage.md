# Kalshi Information Arbitrage Research

*Last updated: 2026-02-16*

## Core Thesis

Kalshi markets settle based on **specific official data sources** (NWS, BLS, HITS Daily Double, Spotify, etc.). These sources often publish data **before** Kalshi settles or before the market fully prices in the information. By monitoring sources and trading faster than other participants, you can capture near-risk-free profit.

**Andes already proved this works** with the Don Toliver album sales trade — HITS Daily Double published numbers before the Kalshi market adjusted.

---

## 1. Complete Settlement Source Table

| Market Category | Settlement Source | Source URL | Publication Schedule | Time Window (est.) | Scrapeable? |
|---|---|---|---|---|---|
| **Temperature (Daily High/Low)** | NWS Daily Climate Report (CLI) | `forecast.weather.gov/product.php?site=XXX&product=CLI&issuedby=XXX` | Twice daily: ~4:30 PM (preliminary) and ~1:30 AM (final) local time | **Hours** — real-time NWS station data available all day before CLI publishes | ✅ Yes, plain text |
| **Album Sales** | HITS Daily Double Top 50 Chart | `hitsdailydouble.com/charts/hits-top-50` | Weekly, chart dated Thursday (e.g., 2026-02-12). Mid-week estimates also published | **Hours to days** — HDD publishes estimates before Kalshi settles | ⚠️ JS-rendered, needs scraping |
| **CPI / Inflation** | Bureau of Labor Statistics (BLS) | `bls.gov/news.release/cpi.htm` | Monthly, **8:30 AM ET** sharp, embargoed until release | **Seconds** — markets react instantly, HFT territory | ✅ Predictable URL |
| **Jobs / Employment** | BLS Employment Situation | `bls.gov/news.release/empsit.htm` | Monthly, **8:30 AM ET**, first Friday of month | **Seconds** — same as CPI | ✅ Predictable URL |
| **Gas Prices** | EIA (Weekly Petroleum Status Report) / AAA Fuel Gauge | `eia.gov/petroleum/supply/weekly/` / `gasprices.aaa.com` | EIA: Wednesdays **10:30 AM ET** (Thursdays on holiday weeks). AAA: Daily updates | **Minutes to hours** — Kalshi gas markets use EIA weekly data | ✅ Yes |
| **Spotify Top Songs** | Spotify Charts (official) | `charts.spotify.com` | Daily, updates ~3 PM ET for previous day's data (1-day lag) | **Hours** — chart data available before Kalshi settles next morning | ⚠️ Requires auth/scraping |
| **Box Office** | The Numbers / Box Office Mojo (likely) | `the-numbers.com` / `boxofficemojo.com` | Daily estimates during opening weekends; final Monday/Tuesday | **Hours** — Sunday estimates widely available before Monday settlement | ✅ Scrapeable |
| **TV Ratings** | Nielsen Ratings | Published via press/trade sites | Next business day, ~late morning | **Hours** — overnight ratings leak via TV trade press before Kalshi settles | ⚠️ No direct API |
| **Sports (NFL, NBA, etc.)** | Official league stats (NFL.com, NBA.com, ESPN) | Various league APIs | **Real-time** during games | **Seconds** — live data feeds available | ✅ APIs exist |
| **Fed Rate Decisions** | Federal Reserve (FOMC Statement) | `federalreserve.gov` | Scheduled FOMC dates, **2:00 PM ET** | **Seconds** — instant market reaction | ✅ Predictable |
| **GDP** | Bureau of Economic Analysis (BEA) | `bea.gov` | Quarterly, **8:30 AM ET** | **Seconds** | ✅ |

---

## 2. HITS Daily Double (Album Sales) — Deep Dive

### Publication Pattern
- **URL pattern**: `hitsdailydouble.com/charts/hits-top-50` (latest) or `hitsdailydouble.com/charts/hits-top-50/YYYY-MM-DD` (historical)
- **Chart dates**: Charts are dated with a Thursday date (e.g., 2026-02-12, 2026-02-05)
- **Building chart**: `hitsdd.section101.com/building_album_chart` — mid-week estimates/building data
- **Update cadence**: Weekly final chart + mid-week "building" estimates

### Key Insight for Kalshi
- Kalshi album sales contracts settle based on the **HITS Daily Double Top 50 chart** for a specific dated week
- The "Albums" column on the Top 50 chart is what matters (not streaming equivalents unless specified)
- HDD publishes **mid-week estimates** (building chart) BEFORE the final chart drops
- The final chart typically appears on **Wednesday/Thursday** before Kalshi settles
- **This is exactly the edge Andes exploited with Don Toliver**

### Scraping Approach
- The main chart page is JS-rendered (React app) — need headless browser or find the underlying API
- Check for: `hitsdailydouble.com/api/...` or XHR requests in network tab
- Historical charts have predictable date-based URLs
- No known RSS feed

### Automation Priority: 🔴 HIGH
- Clear information edge (hours to days)
- Relatively low competition vs economic data
- Moderate volume markets

---

## 3. NWS Temperature Data — Deep Dive

### Settlement Mechanics
- Settles on **NWS Daily Climate Report (CLI)** — the FINAL report (issued ~1:30 AM next day)
- Preliminary CLI issued ~4:30 PM same day
- But real-time temperature data is available ALL DAY from NWS stations

### Weather Stations Used
| City | Station | Location |
|---|---|---|
| NYC | KNYC | Central Park |
| Miami | KMIA | Miami International Airport |
| Chicago | KMDW | Chicago-Midway Airport |
| Denver | KDEN | Denver International Airport |
| Austin | KAUS | Austin-Bergstrom Airport |
| Houston | KHOU | Houston Hobby Airport |
| Philadelphia | KPHL | Philadelphia International Airport |

### Critical Technical Details (from Reddit deep dive)
- **Two types of stations**: Hourly stations vs 5-minute stations
- **Hourly stations**: Record to nearest 0.1°F, convert to 0.1°C, convert back → small rounding error
- **5-minute stations**: Average 5 one-minute readings, round to nearest whole °F, convert to °C → **significant rounding error (±1°F+)**
- The daily high is typically **higher than** the highest reading you see in hourly data
- **Real-time NWS data**: Available via `weather.gov` time series — you can see temperature updates throughout the day
- **wethr.net**: Third-party site built specifically for Kalshi weather market traders

### Data Sources for Monitoring
1. **NWS Time Series** (real-time): `forecast.weather.gov/product.php?site=XXX&product=CLI&issuedby=XXX`
2. **METAR/ASOS data**: Raw airport weather observations, available via `aviationweather.gov`
3. **wethr.net**: Pre-built dashboard for Kalshi weather markets
4. **Weather Underground**: Historical + real-time station data

### Automation Priority: 🟡 MEDIUM-HIGH
- Real-time data available but requires understanding of rounding/conversion
- High volume, active community already trading this
- Multiple markets daily (multiple cities)
- Edge comes from understanding station quirks, not just speed

---

## 4. Economic Data (BLS/BEA) — Deep Dive

### CPI Release
- **Schedule**: `bls.gov/schedule/news_release/cpi.htm`
- **Time**: Exactly **8:30 AM ET** on scheduled dates
- **Embargo**: Media gets data under lockup, released simultaneously at 8:30 AM
- **URL**: `bls.gov/news.release/cpi.htm` (updates at release time)
- **PDF**: `bls.gov/news.release/pdf/cpi.pdf`

### Jobs Report
- **Time**: **8:30 AM ET**, first Friday of month
- **URL**: `bls.gov/news.release/empsit.htm`

### Edge Assessment: 🔴 LOW for information arbitrage
- Everyone gets the data at exactly the same time (8:30 AM ET)
- Kalshi markets typically close BEFORE the data release or react within seconds
- This is HFT territory — you'd need sub-second execution
- Better suited for cross-platform arbitrage (Kalshi vs Polymarket) than source-monitoring

---

## 5. Gas Prices (EIA) — Deep Dive

### EIA Weekly Petroleum Status Report
- **Schedule**: `eia.gov/petroleum/supply/weekly/schedule.php`
- **Standard release**: Wednesdays at **10:30 AM ET**
- **Holiday weeks**: Thursdays at 12:00 PM and 2:00 PM ET
- **Data endpoint**: `ir.eia.gov/` — dedicated release server

### AAA Gas Prices
- **URL**: `gasprices.aaa.com`
- **Updates**: Daily
- **Kalshi markets**: Use AAA or EIA as settlement source (check specific contract)
- **Kalshi market URL**: `kalshi.com/markets/aaagasm/aaa-gas-monthly`

### Automation Priority: 🟡 MEDIUM
- Predictable release schedule
- Moderate competition
- Monthly markets = fewer trading opportunities

---

## 6. Spotify Charts

### Settlement Source
- Kalshi uses **Spotify's official charts** (published web charts)
- **Key gotcha**: Spotify charts have a **1-day lag** — today's chart shows yesterday's data
- Markets settle based on a specific chart date, NOT real-time stream counts
- Charts update ~3 PM ET

### Edge
- If you track real-time stream counts (via third-party trackers), you can predict tomorrow's chart position
- But Kalshi seems aware of this — markets often close the day before settlement around 3 PM
- Limited edge unless you have better real-time stream tracking than other traders

### Automation Priority: 🟡 MEDIUM
- Some edge possible with real-time stream tracking
- Need to verify exact market close times vs chart publication times

---

## 7. Timing Analysis & Profit Windows

### By Market Type

| Market | Info Available | Kalshi Settles | Window | Est. Profit Margin |
|---|---|---|---|---|
| **Album Sales (HDD)** | Mid-week estimates (Wed) | After final chart (Thu) | **Hours to 1 day** | **10-40%** (proven by Don Toliver) |
| **Temperature** | Real-time all day | Next morning (~1:30 AM) | **Hours** | **5-20%** (depends on how settled the temp is) |
| **Box Office** | Sunday evening estimates | Monday/Tuesday | **Hours** | **10-30%** |
| **Gas Prices (EIA)** | Wed 10:30 AM ET | After EIA release | **Minutes** | **3-10%** |
| **CPI/Jobs** | 8:30 AM ET release | After BLS release | **Seconds** | **1-5%** (HFT competition) |
| **Sports** | Real-time game data | After game ends | **Seconds** | Low (well-arbitraged) |

### Key Finding
**Album sales and box office have the widest information windows** — hours to days of edge. Economic data has near-zero edge for manual traders. Temperature is in between — good edge if you understand the technical quirks.

---

## 8. Competition Analysis

### What Others Are Doing

**Cross-Platform Arbitrage (Kalshi ↔ Polymarket)** — WELL KNOWN:
- Multiple open-source bots exist (GitHub)
- `eventarb.com` — real-time arbitrage scanner
- `defirate.com/prediction-markets/calculators/` — fee calculators
- Spreads are compressing: 3-5% in 2024 → 1-2% in 2026
- Fees eat into profits: Kalshi ~0.7% taker, Polymarket 0.01-2%

**Information Preemption Arbitrage (what we're doing)** — LESS KNOWN:
- PANews article describes "Information Preemption Arbitrage" as a known strategy
- QuantPedia study: "opportunities typically exist only for a few seconds"
- BUT: This applies to economic data. **Entertainment/culture markets are much less competed**
- No open-source bots specifically targeting settlement source monitoring
- The album sales edge Andes found is **relatively untapped**

### Why Entertainment Markets Are Underserved
1. Financial traders focus on economic data (CPI, jobs) — they have existing infrastructure
2. Entertainment markets are newer on Kalshi
3. Lower volume = fewer sophisticated traders
4. Data sources (HDD, Nielsen) are less standardized than BLS/Fed
5. The information edge is harder to automate (scraping vs API)

---

## 9. Opportunity Ranking

### Tier 1: Best Opportunities (Target First)

| Rank | Market | Why |
|---|---|---|
| **1** | **Album Sales (HITS Daily Double)** | Proven edge (Don Toliver). Hours-long window. Low competition. Mid-week estimates telegraph the outcome. |
| **2** | **Box Office (Opening Weekends)** | Sunday estimates widely available before Monday settlement. Predictable release calendar. |
| **3** | **Temperature (NWS)** | Real-time data available. Multiple daily markets across cities. Requires technical knowledge of station quirks = barrier to entry. |

### Tier 2: Good Opportunities

| Rank | Market | Why |
|---|---|---|
| **4** | **Spotify Daily Charts** | Possible edge with real-time tracking, but Kalshi may close markets before chart updates |
| **5** | **Gas Prices (EIA/AAA)** | Predictable schedule, moderate edge |
| **6** | **TV Ratings (Nielsen)** | Overnight ratings leak early, but need to confirm Kalshi settlement timing |

### Tier 3: Low Priority

| Rank | Market | Why |
|---|---|---|
| **7** | **CPI / Economic Data** | Zero information edge — everyone gets data simultaneously. Only viable via cross-platform arb |
| **8** | **Sports** | Well-arbitraged, real-time data advantage is sub-second |
| **9** | **Fed Decisions** | Priced in well before announcement |

---

## 10. Technical Architecture: Settlement Source Monitor Bot

### System Design

```
┌─────────────────────────────────────────────────┐
│                SOURCE MONITORS                   │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ HDD      │  │ NWS CLI  │  │ Box Office   │  │
│  │ Scraper  │  │ Monitor  │  │ Scraper      │  │
│  │ (5 min)  │  │ (1 min)  │  │ (10 min)     │  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│       │              │               │           │
│  ┌────┴──────────────┴───────────────┴──────┐   │
│  │          DATA PARSER / NORMALIZER         │   │
│  └────────────────────┬──────────────────────┘   │
│                       │                          │
│  ┌────────────────────┴──────────────────────┐   │
│  │         KALSHI MARKET MATCHER             │   │
│  │   (match parsed data to active markets)   │   │
│  └────────────────────┬──────────────────────┘   │
│                       │                          │
│  ┌────────────────────┴──────────────────────┐   │
│  │         MISPRICING DETECTOR               │   │
│  │   (compare source data to market prices)  │   │
│  └────────────────────┬──────────────────────┘   │
│                       │                          │
│  ┌────────────────────┴──────────────────────┐   │
│  │         TRADE EXECUTOR                    │   │
│  │   (Kalshi API — auto-place orders)        │   │
│  └───────────────────────────────────────────┘   │
│                                                  │
│  ┌───────────────────────────────────────────┐   │
│  │         ALERTING (Discord/SMS/Push)       │   │
│  └───────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

### Tech Stack
- **Language**: Python (fast prototyping) or Node.js (for browser scraping)
- **Scraping**: Playwright/Puppeteer for JS-rendered sites (HDD), `requests` for plain text (NWS)
- **Kalshi API**: REST API with WebSocket for real-time orderbook — `docs.kalshi.com`
- **Scheduler**: Cron jobs or event-driven (check every N minutes)
- **Alerting**: Discord webhook, Pushover, or Telegram bot
- **Hosting**: Mac Mini (always-on) or VPS

### Implementation Priority

**Phase 1: Album Sales Bot (Week 1-2)**
1. Scrape HITS Daily Double building chart + final Top 50
2. Match album names to active Kalshi album sales markets
3. Compare HDD numbers to Kalshi contract strike prices
4. Alert when clear mispricing detected
5. Manual trade initially, then auto-trade via API

**Phase 2: Temperature Bot (Week 3-4)**
1. Monitor NWS real-time station data for target cities
2. Track running high temperature throughout the day
3. Compare to Kalshi temperature market prices
4. Factor in rounding/conversion quirks for 5-min stations
5. Auto-trade when temperature clearly exceeds/misses a strike

**Phase 3: Box Office Bot (Week 5-6)**
1. Scrape The Numbers / Box Office Mojo for weekend estimates
2. Sunday evening: compare estimates to Kalshi box office markets
3. Trade before Monday settlement

### Kalshi API Key Endpoints
- `GET /trade-api/v2/markets` — list all markets
- `GET /trade-api/v2/markets/{ticker}` — market details + rules
- `GET /trade-api/v2/markets/{ticker}/orderbook` — current orderbook
- `POST /trade-api/v2/portfolio/orders` — place order
- WebSocket: `wss://api.elections.kalshi.com/trade-api/ws/v2` — real-time updates

---

## 11. Risk Factors

1. **Kalshi can void/adjust markets** — Rule 7.2(a) gives them discretion
2. **Liquidity risk** — may not be able to fill large orders at desired price
3. **Source data changes** — HDD can revise numbers; NWS can correct readings
4. **Fees** — Kalshi charges ~0.7% taker fee, eating into thin margins
5. **Market close timing** — Kalshi may close markets before source publishes (as with Spotify)
6. **Regulatory risk** — CFTC oversight means Kalshi must maintain fair markets; if they detect systematic exploitation, they could change rules
7. **Competition** — as this strategy becomes known, windows will shrink

---

## 12. Immediate Action Items

1. **✅ Set up Kalshi API access** — Get API keys, test authentication
2. **🔨 Build HDD scraper** — Monitor `hitsdailydouble.com/charts/hits-top-50` for changes
3. **🔨 Build NWS monitor** — Track real-time temps for all 7 cities
4. **📋 Map active markets** — Use Kalshi API to list all active album sales + temperature markets
5. **📋 Document exact settlement timing** — For each market type, note when Kalshi actually settles vs when source publishes
6. **💰 Paper trade first** — Track would-be trades for 2 weeks to validate edge before risking capital

---

## 13. Key URLs & Resources

| Resource | URL |
|---|---|
| Kalshi API Docs | `docs.kalshi.com` |
| Kalshi Markets | `kalshi.com/markets` |
| HITS Daily Double Top 50 | `hitsdailydouble.com/charts/hits-top-50` |
| HDD Building Chart | `hitsdd.section101.com/building_album_chart` |
| NWS Climate Products | `forecast.weather.gov/product.php?site=XXX&product=CLI&issuedby=XXX` |
| wethr.net (weather dashboard) | `wethr.net` |
| BLS Release Schedule | `bls.gov/schedule/2025/home.htm` |
| EIA Weekly Petroleum | `eia.gov/petroleum/supply/weekly/` |
| EIA Release Schedule | `eia.gov/petroleum/supply/weekly/schedule.php` |
| Box Office Mojo | `boxofficemojo.com` |
| The Numbers | `the-numbers.com` |
| Spotify Charts | `charts.spotify.com` |
| EventArb (cross-platform) | `eventarb.com` |
| Kalshi Reddit | `reddit.com/r/Kalshi` |
| NWS Temp Guide (Reddit) | `reddit.com/r/Kalshi/comments/1hfvnmj/` |
