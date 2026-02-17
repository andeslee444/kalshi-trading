# Kalshi Markets Research — Arbitrage & Edge Strategies

**Date:** 2026-02-16  
**Status:** Deep research compilation from Reddit, Twitter/X, academic papers, blogs, and open-source projects

---

## Table of Contents
1. [Executive Summary](#executive-summary)
2. [Key Academic Findings](#key-academic-findings)
3. [Strategy Rankings](#strategy-rankings)
4. [Market Category Deep Dives](#market-category-deep-dives)
5. [Mathematical Methods](#mathematical-methods)
6. [Cross-Platform Arbitrage](#cross-platform-arbitrage)
7. [Market Making](#market-making)
8. [Implementation Roadmap](#implementation-roadmap)

---

## Executive Summary

The prediction market ecosystem (Kalshi, Polymarket, Robinhood, IBKR) is **still meaningfully inefficient** as of early 2026. Key findings:

1. **Favourite-longshot bias is real and persistent on Kalshi** — contracts priced <10¢ win far less than implied; contracts >50¢ yield small positive returns (Whelan 2025, Becker 2025)
2. **Makers systematically profit at takers' expense** — avg excess return +1.12% for makers, -1.12% for takers across 72M trades/$18B volume
3. **Cross-platform arbitrage windows exist** but are brief (seconds to minutes) and shrinking as bots proliferate
4. **Economics markets (CPI, jobs) are the highest-edge category** for informed traders with access to nowcasting models
5. **Sports dominates volume (72%)** but is most efficient; niche categories have better edges

---

## Key Academic Findings

### Whelan (2025) — "Makers and Takers: The Economics of the Kalshi Prediction Market"
- Clear favourite-longshot bias: **low-price contracts (<10¢) generate very large average losses for buyers**
- High-price contracts (>50¢) earn small positive returns
- **Actionable: Systematically sell longshots (sell YES on low-probability events) = positive expected value**
- Bias is diminishing over time but still exploitable

### Becker (2025) — "The Microstructure of Wealth Transfer in Prediction Markets"  
- 72.1M trades, $18.26B volume analyzed
- Takers show **-57% mispricing at 1¢ contracts** (win 0.43% vs implied 1%)
- Makers show **+57% mispricing** on the same contracts
- **Takers lose at 80 of 99 price levels**
- Sports & Entertainment: strongest bias. Finance: approaches efficiency
- YES/NO asymmetry: takers disproportionately buy YES at longshot prices

### Ng, Peng, Tao, Zhou (2025) — Prediction Market Efficiency Study
- National presidential markets showed **higher trading volume but GREATER inefficiency** than lower-liquidity state-level markets
- Attention-driven mispricing overwhelms information aggregation
- **Actionable: High-attention events = more mispricing to exploit**

### Kelly Criterion in Prediction Markets (arxiv 2412.14144)
- Prediction market prices ≠ probabilities (risk aversion creates a wedge)
- Payout asymmetry matters: a bet away from 50% gives one side more leverage
- Misestimation of bias or investment fraction significantly impacts growth rate

---

## Strategy Rankings

**Score = (Expected Annual Profit) × (Probability of Working) / (Effort to Build)**

| Rank | Strategy | Est. Edge | Annual Profit Potential | Prob. Works | Effort | Score |
|------|----------|-----------|------------------------|-------------|--------|-------|
| 1 | **Longshot Bias Exploitation (Maker)** | 5-15% per trade | $20-50K | 90% | Medium | ★★★★★ |
| 2 | **CPI/Economics Nowcast Trading** | 3-8% per event | $15-40K | 80% | Medium | ★★★★★ |
| 3 | **Cross-Platform Arb (Kalshi↔Polymarket)** | 2-5% per arb | $10-30K | 85% | Medium | ★★★★ |
| 4 | **Weather Ensemble Model Upgrade** | 2-5% improvement | $10-25K | 85% | Low | ★★★★ |
| 5 | **Sports: Kalshi↔Sportsbook Arb** | 2-4% per arb | $15-40K | 80% | Medium | ★★★★ |
| 6 | **BTC/Crypto: Kalshi↔Options Chain Arb** | 3-8% | $10-30K | 70% | High | ★★★ |
| 7 | **Market Making (Avellaneda-Stoikov)** | 1-3% per market | $20-60K | 70% | High | ★★★ |
| 8 | **Politics: Polling Aggregator Model** | 2-5% | $5-15K (seasonal) | 65% | High | ★★ |
| 9 | **Entertainment: Billboard/Spotify Scraping** | 3-8% | $5-15K | 70% | Medium | ★★★ |
| 10 | **Company Events: SEC Filing NLP** | 2-5% | $5-10K | 50% | Very High | ★ |

---

## Market Category Deep Dives

### 1. Weather (Already Have Bot)

**Improvements to existing system:**

- **Ensemble model blending**: Use GEFS (21 members), ECMWF EPS (51 members), ICON-EPS, and Canadian CMCE. Weight by recent accuracy (Bayesian Model Averaging). The "Grand Ensemble" approach consistently outperforms any single model.
- **AI weather models**: Google DeepMind's GenCast, Huawei's Pangu-Weather, NVIDIA FourCastNet — these are free to run and sometimes beat NWS, especially for 3-7 day temperature forecasts
- **Micro-climate corrections**: Station-level bias correction using historical NWS forecast vs actual. Many airports have systematic warm/cold biases. A simple linear regression on (forecast - actual) by station, season, and wind direction can add 1-2°F accuracy.
- **Monte Carlo from ensemble spread**: Rather than point estimates, derive full probability distributions from ensemble spread for each Kalshi bucket
- **Key edge**: NWS forecasts update every 6 hours. Commercial/AI models update more frequently. Being faster to incorporate the latest model run = edge.

**Effort**: Low (incremental to existing bot)  
**Expected improvement**: 2-5% better calibration = significant over hundreds of trades

### 2. Economics (CPI, Jobs, GDP) ⭐ HIGH PRIORITY

**Settlement sources & timing:**
- CPI: BLS releases at 8:30am ET, usually 2nd or 3rd week of month
- Jobs (NFP): BLS, first Friday of month, 8:30am ET  
- GDP: BEA, advance estimate ~4 weeks after quarter end
- Fed decisions: FOMC statement at 2:00pm ET on meeting days

**Lead indicators for CPI (our biggest opportunity):**
- **Cleveland Fed Inflation Nowcast**: Updated EVERY BUSINESS DAY. Uses daily oil prices, weekly gasoline retail prices, monthly consumer prices. Free at clevelandfed.org/indicators-and-data/inflation-nowcasting
- **Gasoline prices** (AAA daily avg): ~8% weight in CPI, updates daily
- **Used car prices** (Manheim index): Released ~2 weeks before CPI
- **Rent indices** (Zillow, Apartment List): OER is ~25% of CPI. Monthly Zillow data leads CPI rent by 6-12 months
- **PriceStats** / MIT Billion Prices Project: Daily online price scraping
- **ISM Manufacturing/Services PMI**: Prices paid component

**Strategy**: Build a CPI nowcast model combining Cleveland Fed + gasoline + used cars + rent. Compare to Kalshi market prices. When divergence > 3%, trade. NBER study found Kalshi's median/mode already beats Bloomberg consensus for CPI, but **individual traders with daily-updating models can still find edges when new data (gas prices, used cars) shifts the distribution between market updates**.

**Effort**: Medium (data scraping + simple regression model)  
**Expected edge**: 3-8% per CPI release, 12 releases/year

### 3. Entertainment (Already Have HDD Scraper)

**Additional data sources:**
- **Billboard chart data**: billboard.com, weekly. Predict #1 album/song markets
- **Spotify daily streaming counts**: Available via Spotify Charts (spotifycharts.com). Stream velocity predicts chart position
- **Apple Music daily top 100**: Complementary to Spotify
- **Letterboxd / IMDB ratings**: For Oscar/awards prediction markets
- **Box office tracking**: Already have HDD. Also check The-Numbers.com for per-theater averages (better predictor than raw gross)
- **Social media sentiment**: Twitter/X mention volume for artists/movies correlates with first-week sales
- **Pre-order/pre-save data**: Spotify pre-save counts (sometimes leaked) signal first-week numbers

**Strategy**: Combine HDD with Spotify streaming velocity for album/chart markets. Box office + per-theater average for movie markets.

**Effort**: Medium  
**Expected edge**: 3-8% on entertainment markets

### 4. Sports

**Public data edges:**
- Kalshi sports odds often **lag sportsbooks by minutes to hours** (Reddit: "most bookies had the underdog at +160, but Polymarket had him at +300")
- **Strategy**: Real-time odds scraping from Pinnacle, DraftKings, FanDuel → compare to Kalshi → buy when Kalshi is 5%+ off consensus
- **Injury news**: Follow beat reporters on Twitter. Kalshi adjusts slower than sharp sportsbooks
- **Weather for outdoor sports**: Wind, temperature, altitude affect scoring totals
- **Line movement tracking**: When sharp books move, Kalshi follows with delay

**Key insight from Becker (2025)**: Sports has the STRONGEST longshot bias on Kalshi. Casual bettors flood YES on underdogs. **Selling longshots in sports = highest edge category.**

**Effort**: Medium (odds API scraping + comparison engine)  
**Expected edge**: 2-4% per trade, high volume

### 5. Crypto (BTC/ETH Price Markets)

**Cross-exchange arbitrage:**
- Kalshi BTC hourly price markets vs Polymarket BTC hourly markets
- Open-source bot exists: `CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot` on GitHub
- Buy YES on Kalshi + buy DOWN on Polymarket (or vice versa) when combined cost < $1.00
- **Also**: Compare Kalshi BTC price contracts to Deribit/CME BTC options chain. A Kalshi "BTC above $X" contract = a digital call option. If Deribit binary options or interpolated from vanilla options give different implied prob, arbitrage exists.

**Key insight from Moontower Meta**: "The moment you see a bet on Kalshi, your mind should jump to the options chain." Kalshi priced 9% chance of BTC $250K while options markets implied different odds.

**Risks**: Different settlement times, different reference prices, counterparty risk on offshore platforms

**Effort**: High (need options data feed, cross-platform execution)  
**Expected edge**: 3-8% when opportunities appear, but infrequent

### 6. Politics

**Polling aggregator edges:**
- 538, RealClearPolitics, Silver Bulletin, The Economist models
- **Key finding**: National-level prediction market prices show MORE inefficiency than state-level (attention-driven mispricing)
- Build Bayesian model combining polls + fundamentals (economy, incumbency, historical patterns)
- **State-level arbitrage**: Sum of all candidate probabilities should = 100%. When it doesn't on Kalshi, arb within the platform.

**Timing**: Seasonal — only valuable during election cycles. 2026 midterms approaching.

**Effort**: High  
**Expected edge**: 2-5% during election season

### 7. Company Events (Earnings, Layoffs, Product Launches)

**Alternative data signals:**
- **SEC EDGAR filings**: 8-K (material events), 10-Q tone changes, insider trading Form 4
- **Job postings**: Indeed/LinkedIn scraping. Mass layoff signals from declining postings; hiring surges from increasing
- **Glassdoor reviews**: Sentiment decline precedes negative events
- **Patent filings**: Signal product launches
- **Satellite imagery / web traffic**: For retail earnings (but very expensive data)

**Assessment**: This is mostly theoretical. Kalshi company event markets have LOW liquidity and few contracts. The effort to build NLP pipelines for SEC filings is very high relative to the small number of tradeable markets.

**Effort**: Very High  
**Expected edge**: 2-5% but tiny market size → not worth it yet

---

## Mathematical Methods

### Kelly Criterion & Position Sizing

**Current approach: Half-Kelly. Is it optimal?**

Half-Kelly captures **~71% of optimal returns with only ~38% of the volatility** (Tastylive analysis). This is generally considered the sweet spot for most traders.

**Recommendation: Stick with half-Kelly BUT with modifications:**
- **Quarter-Kelly for low-confidence bets** (estimated edge <3%)
- **Half-Kelly for medium-confidence** (edge 3-8%)
- **Three-quarter Kelly for high-confidence** (edge >8%, strong model support)
- **Never full Kelly** — probability estimates in prediction markets are inherently uncertain; full Kelly assumes perfect knowledge of true probabilities

**Multi-market Kelly**: When placing multiple simultaneous bets, use the simultaneous Kelly criterion (accounts for correlation). For independent events, individual Kelly fractions can be used but total capital at risk should be capped at 25-30% of bankroll.

### Bayesian Updating with Multiple Data Sources

For CPI/economics trading:
```
Prior: Cleveland Fed nowcast distribution
Update 1: New gasoline price data → shift distribution  
Update 2: Used car index release → shift distribution
Update 3: Rent data → shift distribution
Posterior: Our CPI estimate distribution
Compare to Kalshi market distribution → trade divergences
```

Use conjugate priors where possible (Beta distribution for binary outcomes, Normal for continuous like CPI). For multi-outcome markets (CPI in range X-Y), use Dirichlet distribution.

### Market Microstructure — Bid/Ask Spread Exploitation

From Becker's research:
- **Be a Maker, not a Taker**: +1.12% avg excess return as maker vs -1.12% as taker
- Place limit orders and let them fill. Avoid market orders.
- On Kalshi specifically: **always use limit orders on both sides**
- In log-odds space, spreads are more consistent (Hacker News insight: "calculate skews/spreads in log-odds space")

### Mean Reversion in Prediction Markets

- After news-driven price spikes, prediction market prices tend to **overshoot** then revert
- Strategy: When a contract moves >10% in <1 hour on no material news, fade the move
- **Caution**: Distinguish between noise moves (revert) and information moves (don't revert)
- Works best in low-liquidity markets where single large orders move prices

### Monte Carlo Simulation for Weather

Already in use. Improvements:
- Use ensemble member spread as basis for simulation rather than assumed distributions
- Calibrate historical ensemble spread vs actual outcomes by forecast horizon
- Account for spatial correlation (nearby cities' weather is correlated — don't treat as independent bets)

---

## Cross-Platform Arbitrage

### Kalshi ↔ Polymarket

**Mechanics**: Buy YES on Platform A + NO on Platform B. If combined cost < $1.00 (minus fees), guaranteed profit.

**Fee structure (critical)**:
- Kalshi: ~0.7% transaction fee (was higher pre-2025)
- Polymarket US: 0.01% on trades (essentially free)
- Polymarket International: 2% on net winnings

**Minimum viable edge after fees**: ~1.5-2% spread needed

**Key challenges**:
- **Settlement risk**: Different platforms may resolve the same event differently (different settlement sources, different interpretation of edge cases)
- **Speed**: Arb windows last seconds to minutes. Need co-located or fast API access.
- **Capital lockup**: Capital is locked until settlement on both platforms
- **Polymarket leads Kalshi** in price discovery due to higher liquidity (QuantPedia finding)

**Tools**:
- `realfishsam/prediction-market-arbitrage-bot` — open source, Kalshi↔Polymarket
- `CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot` — BTC-specific
- pmxt.dev — prediction market API toolkit
- defirate.com/prediction-markets/calculators — fee calculators

### Kalshi ↔ Sportsbooks

**Higher edge but more manual**:
- Kalshi sports odds can diverge 5-15% from sharp sportsbooks (Pinnacle)
- Use odds comparison APIs (The Odds API, OddsJam)
- Place hedge on sportsbook + opposite on Kalshi
- **Reddit reports**: "5-10 arbs per week at $20-50 per opportunity with $1,000 bankroll"

### Kalshi ↔ IBKR (Interactive Brokers Event Contracts)

- IBKR now offers event contracts competing with Kalshi
- Same events, different prices
- Both are regulated US platforms = lower settlement risk
- Reddit: "bid up Kalshi to 15c and IBKR down to 15c" for guaranteed profit

### Kalshi ↔ Options Markets

- **BTC/ETH price contracts** on Kalshi = digital options. Compare to Deribit/CME binary options or derive from vanilla option skew
- **S&P 500 daily range** on Kalshi = compare to SPX 0DTE options implied distributions
- **Most sophisticated arb** but potentially highest edge for quant traders

---

## Market Making

### Avellaneda-Stoikov Framework for Kalshi

The 2008 Avellaneda-Stoikov model provides the mathematical foundation:

**Reservation price** = mid-price - (inventory × γ × σ²)  
**Optimal spread** = γσ² + (2/γ) × ln(1 + γ/k)

Where:
- γ = risk aversion parameter
- σ² = price variance  
- k = order arrival rate parameter

**Kalshi-specific adaptations** (from Hacker News discussion):
1. Work in **log-odds space** for more stable spread calculations
2. Account for **binary payout structure** (price bounded 0-100)
3. As market approaches expiry, reduce inventory aggressively (reservation price → mid-price)
4. Use **RL-enhanced γ** parameter that adapts to market conditions (per PMC/PLOS paper)

**Kalshi Market Maker Program**: Selective, requires significant capital and track record. But you can market-make informally on any market.

**Practical approach**:
1. Pick low-attention markets where you have informational edge
2. Post limit orders on both sides, centered on your model's fair value
3. Wider spreads in volatile/uncertain markets, tighter near settlement
4. Maximum inventory limits to prevent blowup

**Expected returns**: NPR profiled Evan Semet making **six figures/month** on Kalshi using statistical models + market making. He uses AWS-hosted models.

---

## Implementation Roadmap

### Phase 1: Quick Wins (1-2 weeks)
1. **Longshot bias exploitation**: Add rule to existing bot — systematically sell YES on contracts priced <10¢ in sports/entertainment. Backtest against Becker's dataset (available on GitHub).
2. **Limit orders only**: Modify all existing trading to use limit orders (maker) instead of market orders (taker). Immediate +2% edge.
3. **Weather ensemble upgrade**: Add ECMWF EPS and GEFS ensemble spread to Monte Carlo simulation.

### Phase 2: CPI/Economics Bot (2-4 weeks)
1. Scrape Cleveland Fed nowcast daily
2. Scrape AAA gasoline prices daily
3. Scrape Manheim used car index monthly
4. Build simple regression model: predicted CPI = f(Cleveland nowcast, gas prices, used cars)
5. Compare to Kalshi CPI market prices
6. Trade when divergence > 2 standard deviations

### Phase 3: Cross-Platform Arbitrage (3-6 weeks)
1. Set up Polymarket API access (CLOB)
2. Build market matching engine (same events across platforms)
3. Real-time spread monitoring
4. Semi-automated execution (alert + one-click trade)
5. Start with BTC hourly markets (most liquid, easiest to match)

### Phase 4: Sports Odds Comparison (4-8 weeks)
1. Subscribe to odds API (The Odds API: free tier available)
2. Real-time comparison: Kalshi sports vs Pinnacle/consensus
3. Alert when Kalshi diverges >5%
4. Manual execution initially, automate later

### Phase 5: Market Making (8-12 weeks)
1. Implement Avellaneda-Stoikov in log-odds space
2. Start on weather markets (already have forecasting edge)
3. Expand to economics after CPI model is validated
4. Track inventory, P&L, and spread capture metrics

---

## Key Resources

- **Becker dataset**: github.com/jon-becker/prediction-market-analysis (72M trades)
- **Arb bots**: github.com/realfishsam/prediction-market-arbitrage-bot
- **BTC arb bot**: github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot
- **Kalshi MM paper**: jdsemrau.substack.com/p/automated-market-making-on-kalshi
- **Whelan paper**: karlwhelan.com/Papers/Kalshi.pdf
- **Cleveland Fed CPI Nowcast**: clevelandfed.org/indicators-and-data/inflation-nowcasting
- **Kelly for prediction markets**: arxiv.org/html/2412.14144v1
- **QuantPedia edges overview**: quantpedia.com/systematic-edges-in-prediction-markets/
- **Prediction market tools directory**: predictionmarket.tools
- **Fee calculators**: defirate.com/prediction-markets/calculators/

---

## Bottom Line

The three highest-ROI strategies to implement **right now**:

1. **Be a Maker, exploit longshot bias** — Simple rule changes to existing trading. Sell longshots, use limit orders. Immediate edge.
2. **CPI nowcast model** — Cleveland Fed + gas prices + used cars. 12 high-value trading opportunities per year. Medium effort, high payoff.
3. **Cross-platform arb monitoring** — Even if manual initially, knowing when Kalshi and Polymarket diverge on the same event is pure alpha.

The prediction market space is where crypto was in 2017 — growing fast, still inefficient, and the quant infrastructure is primitive. The window for easy edges is closing but not closed.

---

## Research Update — 2026-02-16 (Cycle 2)

### Market Landscape Changes

**Kalshi hit $1B Super Bowl volume (Feb 9, 2026)** — 2,700% YoY growth. Bad Bunny halftime first-song market alone exceeded $100M volume. This confirms massive retail influx into prop/entertainment markets = more longshot bias to exploit.

**Kalshi launched 15-minute crypto markets** — BTC and ETH contracts settling every 15 min. These are essentially ultra-short-dated binary options. Reddit r/algotrading reports bots struggling to find edge here — markets are surprisingly efficient due to tight connection to spot prices. However, the fee structure (lower at extremes) creates an opening for selling deep OTM 15-min contracts.

**FOMC March 2026 disconnect** — Kalshi traders pricing 64% chance of 25bps cut at March 17-18 meeting. Bond market is more conservative. Fortune reports Kalshi has a "perfect forecast record" on Fed decisions (NBER working paper). This is a high-conviction trade setup: compare CME FedWatch vs Kalshi pricing. When they diverge, the CME tool (driven by Fed Funds futures with real institutional money) is likely more accurate.

**Polymarket 5-minute BTC markets** — Even shorter duration than Kalshi's 15-min. Cross-platform arb between Kalshi 15-min and Polymarket 15-min/5-min crypto contracts is a new opportunity vector, though execution speed is critical.

### New Strategy: Fed Rate Decision Arb (CME FedWatch vs Kalshi)

- **Concept**: CME FedWatch tool derives probabilities from Fed Funds futures (institutional money). Kalshi Fed markets are retail-driven. When they diverge by >5%, trade Kalshi toward CME consensus.
- **Edge estimate**: 3-5% per FOMC meeting (8 meetings/year)
- **Effort**: Low — just check both before each meeting, no model needed
- **Data source**: CME FedWatch (free), Kalshi API
- **Current opportunity**: March meeting — Kalshi at 64% cut, check CME for comparison

### New Strategy: March Madness Tournament Markets (Upcoming)

- **Concept**: Selection Sunday is mid-March. Kalshi and Polymarket both offer March Madness winner markets. Current favorites: Michigan (17%), Arizona (14%), Duke (12%).
- **Edge sources**:
  - KenPom efficiency ratings (free, updated daily) — the gold standard for CBB analytics
  - Bart Torvik T-Rank (free) — alternative efficiency model
  - Compare KenPom tournament simulation probabilities to Kalshi market prices
  - Historical longshot bias is EXTREME in March Madness: casual bettors overweight brand names (Duke, Kentucky) and underweight mid-majors
- **Edge estimate**: 5-10% on mispriced teams, especially in round-by-round markets
- **Effort**: Medium — need to run KenPom sim and compare to market prices
- **Timing**: Act in late Feb / early March before bracket is set; opportunities increase after Selection Sunday

### New Strategy: Prop Market Exploitation During Live Events

- **Concept**: Super Bowl showed $1B in prop trading. Next major events: March Madness, NBA Playoffs, MLB Opening Day. During live events, Kalshi prop markets (first scorer, halftime score, etc.) are flooded with retail money and exhibit extreme longshot bias.
- **Edge sources**:
  - Pre-game statistical models for props (e.g., player scoring distributions from NBA stats API)
  - Live line comparison vs sportsbooks (DraftKings, FanDuel adjust faster)
  - Sell YES on extreme longshot props (e.g., "Will [bench player] score first?" at 3¢)
- **Edge estimate**: 5-15% per prop on longshots, but need volume
- **Effort**: Medium — need prop odds comparison system
- **Data source**: The Odds API, NBA/NFL stats APIs

### New Strategy: 15-Min Crypto — Sell Deep OTM

- **Concept**: Kalshi's 15-min BTC/ETH markets. In any 15-minute window, BTC rarely moves >1%. Contracts asking "Will BTC be above [price +3%] in 15 minutes?" should settle NO ~99% of the time. Sell YES (buy NO) on extreme strikes.
- **Edge estimate**: 1-3% per contract, but extremely high frequency (96 windows/day)
- **Risk**: Black swan 15-min moves (flash crashes) can wipe out dozens of wins. Need strict position limits.
- **Effort**: Medium-High — need real-time BTC volatility model + automated execution
- **Data source**: Binance/Coinbase websocket for real-time BTC price + historical intraday vol
- **Key insight from Crypticorn**: "91% of Polymarket traders lose not because they can't predict — but because they don't size." Kelly criterion is essential here.

### Updated Fee Intelligence

Kalshi fee formula: **0.07 × P × (1-P)** where P = contract price.
- Max fee: 1.75¢ at 50¢ contracts
- Min fee: ~0.07¢ at 1¢/99¢ contracts
- **Maker fees are lower than taker fees** (confirmed by Deadspin/Kalshi help center)
- 2% processing fee on debit card deposits/withdrawals (use ACH to avoid)
- **Implication**: Trading at extremes (selling longshots at 1-5¢ or buying near-certainties at 95-99¢) has minimal fee drag. This reinforces the longshot-selling strategy.

### Cross-Platform Arb Update

New open-source arb bot from r/algotrading (Jan 2026): `realfishsam/prediction-market-arbitrage-bot`
- Monitors Fed rate cut, crypto, and political markets across Kalshi + Polymarket
- Strategy: Synthetic arbitrage — buy YES on one platform, NO on the other when combined < $1.00
- Key insight from the dev: "Many arbitrageurs make the mistake of holding until maturity" — better to exit when spread narrows for faster capital recycling
- Typical spread: 3¢ ($0.97 combined cost for $1.00 payout)
- After fees (~1.5¢ combined), net profit ~1.5¢ per pair = ~1.5% return per trade
- Capital recycling: If you exit in hours instead of waiting for settlement, annualized returns can be 50%+

---

## 🎯 Three Specific Trade Ideas for Next Cycle

### Trade 1: March FOMC Rate Decision — Fade Kalshi if CME Diverges
- **Market**: Kalshi "Fed cuts 25bps at March meeting" — currently 64¢
- **Action**: Check CME FedWatch. If CME shows <55% probability, buy NO on Kalshi at ~36¢
- **Size**: Half-Kelly based on divergence magnitude
- **Timeline**: Enter now, settles March 18
- **Expected edge**: 3-5% if CME divergence confirmed
- **Max risk**: 36¢ per contract

### Trade 2: March Madness Winner — Sell Overpriced Blue Bloods
- **Market**: Kalshi/Polymarket "Who wins 2026 NCAA Championship?"
- **Action**: Run KenPom tournament simulation. If Duke's sim probability is <8% but market prices at 12¢, sell Duke YES (buy NO at 88¢). Similarly look for underpriced mid-majors.
- **Size**: Quarter-Kelly (tournament outcomes are high variance)
- **Timeline**: Enter late Feb, settles early April
- **Expected edge**: 5-10% on mispriced teams
- **Data needed**: KenPom ratings (kenpom.com subscription, $25)

### Trade 3: Sell BTC 15-Min Extreme Strikes
- **Market**: Kalshi "BTC above $X in next 15 minutes" where X is >2% above current price
- **Action**: If current BTC = $67K, sell YES on "BTC above $68.5K" contracts. Historical 15-min returns show <1% probability of 2%+ moves.
- **Size**: Tiny per trade (1/4 Kelly), but repeat 20-50x/day
- **Timeline**: Continuous, settles every 15 min
- **Expected edge**: 1-3% per contract
- **Risk management**: Stop if realized vol exceeds 2x expected. Never have >5% of bankroll at risk simultaneously across 15-min contracts.
