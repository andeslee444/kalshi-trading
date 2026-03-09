# Quant Roadmap — February 2026

> **DEPRECATED**: This document is superseded by [Consolidated Quant Desk Design](2026-03-02-consolidated-quant-desk-design.md). Kept for historical reference only.

Prioritized action items from production log audit and system-wide analysis. Ordered by impact and dependency chain.

---

## P0: Fix Feedback Loop (Blocks Everything)

### 1. Run settlement reconciliation
- [ ] `npm run reconcile` — annotate all trade logs with settlement outcomes
- [ ] `npm run backfill` — query individual market endpoints for unsettled trades
- [ ] Verify `data/*-trades.json` files now have `settlement_result` fields
- **Why:** Zero settled trades = zero model validation. Every edge estimate is unvalidated theory.

### 2. Generate actual Brier scores
- [ ] `npm run backtest` — evaluate model calibration against settled outcomes
- [ ] Check `data/backtest-results.json` for non-null Brier scores
- [ ] Compare weather YES vs NO win rates to validate `disableWeatherYes: false` experiment
- **Why:** Brier scores are currently null. Can't optimize what you can't measure.

### 3. Run per-city sigma calibration
- [ ] `npm run calibrate` — grid-search optimal sigma params per city
- [ ] Verify `config/calibration.json` has per-city entries (not empty)
- [ ] Compare calibrated sigmas vs hardcoded defaults (intercept=2.0, slope=0.5)
- **Why:** calibration.json is empty. All weather bots using uncalibrated defaults.

---

## P1: Enable Existing Alpha Sources (This Week)

### 4. Re-enable entertainment bot
- [ ] Set `enabled: true` in `config/bots-config.json` → entertainment section
- [ ] Run `python3 src/kalshi/entertainment-bot.py --once` (if supported) to validate
- [ ] Monitor `data/kalshi-entertainment-trades-decisions.json` for correct skip/execute ratio
- [ ] Verify relaxed liquidity thresholds (min_volume=5, max_spread=40) are working
- **Why:** 99.7% skip rate, 2 trades ever. Album sales have hours-to-days info windows.

### 5. Debug beatrelease scanner
- [ ] Run `python3 src/kalshi/beatrelease-scanner.py --once` and trace output
- [ ] Verify DeepSeek API key is valid: `cat config/keys/deepseek.txt`
- [ ] Check if blog URLs are returning content: `curl -s https://www.beatrelease.com/blog`
- [ ] Verify market matching (blog tickers → Kalshi tickers)
- [ ] Check if new calibration discount (15%) is rejecting everything
- **Why:** Zero executed trades ever. LLM-driven info arb should be producing regular edges.

### 6. Start position monitor
- [ ] Verify position-monitor.py is in supervisor config
- [ ] Run `python3 src/kalshi/position-monitor.py` in daemon mode
- [ ] Check `data/health-state.json` for heartbeat within 15 min
- [ ] Monitor for take-profit and stop-loss exits over 48h
- **Why:** Zero exits ever. Holding all positions to settlement leaves money on the table.

### 7. Verify economics bot scraper
- [ ] `curl -s https://www.clevelandfed.org/api/indicators/inflation-nowcasting` — check if JSON API exists
- [ ] If API works: add as primary source in economics-bot.py (HTML scraping as fallback)
- [ ] If API doesn't exist: fix HTML scraping resilience
- [ ] Run `python3 src/kalshi/economics-bot.py --once` (if supported) and check for nowcast data
- [ ] Next CPI release date: check BLS calendar and validate sigma step-down
- **Why:** CPI nowcast is a proven 3-8% edge strategy. Economics bot has zero executed trades.

---

## P2: Build Missing High-Value Strategies (Weeks 2-3)

### 8. Build box office trading bot
- [ ] Scrape The Numbers / Box Office Mojo for weekend estimates
- [ ] Match to Kalshi KXBOX/KXMOVIE markets
- [ ] Use `boxoffice_data_sigma()` (already in probability.py) for probability model
- [ ] Follow bot pattern: data fetch → edge calc → TradeManager.place_order()
- [ ] Backtest against historical weekend box office data
- **Why:** Research identifies $5-15K/year potential. Framework exists, bot doesn't.

### 9. Scale longshot bias selling
- [ ] Audit strategy-trader.py: why only 5 trades ever?
- [ ] Check allocator rejection rate for strategy trades
- [ ] Lower strategy edge threshold if allocator is the bottleneck
- [ ] Target sports/entertainment markets during major events
- [ ] Track Becker model predictions vs actual settlement for validation
- **Why:** Becker (2025) proves systematic overpricing at <10c. Only executing 1% of available volume.

### 10. Enable cross-platform arb execution
- [ ] Review `data/` for arb monitoring logs — are spreads being detected?
- [ ] Validate Polymarket price feeds are accurate (compare to Kalshi)
- [ ] Set `executionEnabled: true` in bots-config.json once monitoring validates
- [ ] Start with tiny position sizes ($1-2) to verify execution flow
- **Why:** 1.5-2% per-pair after fees. Infrastructure exists, execution disabled.

---

## P3: Model Improvements (Weeks 3-4)

### 11. Validate crypto model
- [ ] Compare claimed edges (8-80%) vs signal_quality (3-5%) — huge disconnect
- [ ] Backtest `crypto_price_probability()` against historical BTC 15-min candles
- [ ] Check if settlement time estimates are correct (T=2456min seems wrong for hourly markets)
- [ ] Validate realized vol computation matches actual vol (compare to Deribit DVOL)
- [ ] Consider tightening edge threshold if model is overconfident
- **Why:** 37 trades with wildly varying edge claims. Need to know if model is calibrated.

### 12. Build automated calibration pipeline
- [ ] `reconcile → backtest → (if Brier > threshold) → recalibrate` as a single script
- [ ] Run daily via cron or supervisor
- [ ] Alert (WhatsApp) if model quality degrades >10%
- [ ] Auto-update `config/calibration.json` if new calibration improves Brier score
- **Why:** Currently manual. Should be automatic — models drift, data changes.

### 13. Verify new weather cities
- [ ] Run ticker discovery: `python3 -c "import sys; sys.path.insert(0, 'src/kalshi'); from kalshi_auth import KalshiClient; c = KalshiClient(); markets = c.get_all_markets(prefix='KXHIGH'); cities = set(); import re; [cities.add(re.match(r'KXHIGH([A-Z]+)-', m.get('ticker','')).group(1)) for m in markets if re.match(r'KXHIGH([A-Z]+)-', m.get('ticker',''))]; print(sorted(cities))"`
- [ ] Compare against cities in `config/kalshi-config.json`
- [ ] Remove any cities Kalshi doesn't offer
- [ ] Add any cities Kalshi offers that we're missing
- **Why:** We added 10 cities but haven't verified Kalshi's actual ticker codes.

---

## P4: Operational Hardening (Ongoing)

### 14. Monitor production after today's deploy
- [ ] Watch `data/logs/` for crash loops over 24h
- [ ] Check `data/*-decisions.json` for correct skip reasons (near_threshold, pre_dawn, brackets_disabled)
- [ ] Verify weather YES trades are executing (disableWeatherYes now false)
- [ ] Compare trade volume before/after (should increase with 6% threshold + new cities)

### 15. Set up daily automation
- [ ] Verify `npm run report:notify` sends WhatsApp P&L summary
- [ ] Verify `npm run backtest:daily` runs and alerts on >10% Brier drift
- [ ] Add settlement reconciliation to daily cron (if not already)

### 16. Re-enable HDD source when endpoints work
- [ ] Periodically check HDD Sanity CMS endpoints
- [ ] When working again: set `hdd.enabled: true` in kalshi-monitor-config.json
- [ ] Verify hdd_parser.py handles current chart format

---

## Completed (2026-02-26)

- [x] Fix weather-bot crash: None/empty-dict guards in probability.py + weather-bot.py
- [x] Fix NWS midnight trade bug: pre-dawn gate (hour < 8) in source-monitor.py
- [x] Lower weather edge threshold: 8% → 6%
- [x] Fix entertainment illiquidity filter: is_market_liquid() now accepts overrides
- [x] Increase crypto daily loss limit: $25 → $50
- [x] Increase weather dedup cooldown: 6h → 12h
- [x] Fix source-monitor heartbeat: moved to after successful cycle
- [x] Disable crypto bracket markets: enableBrackets=false (87% non-fill rate)
- [x] Add min forecast distance filter: skip |forecast-threshold| < 2°F
- [x] Add 10 new weather cities + NWS stations
- [x] Disable dead HDD endpoints
- [x] Add beatrelease calibration discount: 15% LLM confidence reduction + 60% gate
- [x] Re-enable weather YES trades (experiment)
- [x] Adjust position monitor thresholds: TP 80%, SL 30%, model-shift 20%
