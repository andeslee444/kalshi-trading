# Kalshi Trading System — Full Codebase Review Prompt

Use this prompt with Claude Code (Opus) to conduct a comprehensive review of the Kalshi trading system. The goal is to maximize expected returns and minimize risk of ruin.

---

## THE PROMPT

You are reviewing an automated prediction market trading system on Kalshi. The system runs 8+ Python bots that trade weather, crypto, economics, entertainment, and longshot markets using probability models + Kelly sizing. Your job is to find every issue that **loses money**, **increases risk of ruin**, or **leaves profitable edge on the table** — then produce a prioritized fix plan.

### PHASE 1: Edge Calculation Audit (probability models)

Read `src/kalshi/probability.py` completely and verify:

1. **Weather model (`weather_probability`)**: Is `sigma = intercept + slope * sqrt(days_out)` a good model for NWS forecast error growth? Empirical NWS error plateaus after day 3-5 — does the sqrt growth overfit to long-range markets? Check if the default `df=6` for Student's t is validated or arbitrary. Check if the sigma floor of 0.5F creates false certainty near settlement.

2. **NWS real-time model (`nws_probability`)**: The exponential sigma decay `4.0 * exp(-0.18 * (hour - 6))` has never been backtested against settled NWS trades. Is the decay rate reasonable? Does the model handle overnight observations (hour < 6) correctly? Does it account for seasonal differences (summer vs winter intraday temp patterns)?

3. **Info-arb models (`info_arb_probability`, `album_data_sigma`, `boxoffice_data_sigma`)**: The day-of-week sigma values (15% Mon, 10% Wed, 3% Fri+) are hardcoded guesses — `calibration.json` shows n=0 for album_sales and box_office. Are these values defensible? Is normal distribution appropriate for count data (album sales follow Poisson)?

4. **CPI nowcast (`econ_nowcast_probability`, `cpi_nowcast_sigma`)**: The sigma floor at 0.03% may create false certainty for day-of-release trades. Is the exponential decay from 0.10% to 0.03% reasonable? Does it account for preliminary vs final CPI releases?

5. **Crypto GBM (`crypto_price_probability`)**: Check the default volatility assumptions (BTC 50%, ETH 65%). In the current post-ETF era, BTC vol may be 35-45%. Does the Ornstein-Uhlenbeck mean reversion mode ever trigger given typical settlement horizons? Is drift=0 the right assumption for trending markets?

6. **Longshot bias (`longshot_edge`)**: Verify the Becker (2025) model implementation. Check: does `longshot_edge()` correctly convert the overpricing ratio to an additive edge (line ~590)? Is the time decay factor at line ~583 going the right direction (should edge INCREASE near settlement as retail panic buys, or DECREASE as market efficiency improves)?

7. **Cross-model consistency**: All bots should use the same edge definition (additive: our_prob - implied_prob). Verify that every bot passes `edge` to Kelly functions in the correct format. Check especially `strategy-trader.py` and `source-monitor.py`.

### PHASE 2: Position Sizing Audit (Kelly criterion)

Read `half_kelly()`, `half_kelly_sell()`, `quarter_kelly()`, and `high_conviction_kelly()` in `probability.py`:

1. **Kelly math**: Verify `f = (b*p - q) / b` where `b = win/loss ratio`. Is fee handling correct (reduces payout, not probability)? Does the bankroll scaling work properly?

2. **Risk per contract for NO trades**: In `kalshi_auth.py` `TradeManager.place_order()` around line 1051, when `side == "no"`, the code computes `risk_per_contract = 100 - price_cents`. But when buying NO at price P, your max loss is P (not 100-P). Verify this is a bug — it would overestimate NO-side risk by `(100-P)/P`, making NO trades appear 2-4x riskier than they are. Check if this causes under-sizing of NO trades.

3. **Daily counter reconstruction**: In `_rebuild_daily_counters_from_log()` (~line 918), the reconstruction uses `t.get("cost_cents", 0)` for daily spend. But the live check uses `(100 - price) * count` for NO side. These are inconsistent — verify whether reconstructed counters undercount or overcount NO-side risk after a bot restart.

4. **high_conviction_kelly**: Does the bankroll threshold (< $500 uses half-Kelly, >= $500 uses 60% Kelly) ever trigger in practice? If all accounts are $2000+, then 60% Kelly is always used. Is 60% Kelly appropriate given the expected number of trades? (At 200 trades, 60% Kelly has ~16% ruin probability vs ~10% for half-Kelly.)

### PHASE 3: Risk Controls Audit

Read `kalshi_auth.py` (TradeManager, CircuitBreaker) and `capital_allocator.py`:

1. **Allocator enforcement**: After `allocator.request_budget()` returns an allocation, the bot places a trade, then should call `allocator.record_trade()`. Verify that ALL bots actually call `record_trade()` after successful trades. If any skip this, the allocator is advisory-only and portfolio limits aren't enforced. Grep for `allocator.record_trade` across all bot files.

2. **Race condition on daily limits**: Between the daily limit check (line ~1044) and the increment (line ~1127), can two bots concurrently pass the check? The counters are in-memory per-process, not shared. Is this mitigated by the allocator's file-based state?

3. **Aggregate daily loss**: Sum up `maxDailyLoss` across all bots from `config/bots-config.json` and `config/kalshi-config.json`. What is the total worst-case daily loss if all bots hit their limits simultaneously? Compare against typical bankroll. Is this acceptable?

4. **Circuit breaker edge cases**: Can the circuit breaker get stuck open if `_opened_at` is corrupted to `None` in the state file? Check the `is_open()` logic.

5. **City/region concentration**: In `capital_allocator.py`, check the `_CITY_TO_REGION` mapping. Are all traded cities (MIA, LAX, CHI, DEN, NY, PHIL, HOU, AUS) assigned to regions? Missing cities bypass region concentration limits.

6. **Kill switch reliability**: Is the kill switch checked once at the start of `place_order()` only? Could a trade slip through if the kill switch is activated between the check and the API call?

### PHASE 4: Bot-Level Execution Audit

For each bot, identify the specific code path from data fetch → probability → edge calculation → sizing → order placement, and verify:

1. **weather-bot.py**:
   - Line ~214: The NO edge formula `edge_yes = -((1 - our_prob) - (no_ask / 100.0))`. Verify this correctly computes NO-side edge with the negative sign convention.
   - Line ~268: YES trades require 15% edge minimum (based on "0% historical win rate"). Is this still justified? Check if we have enough settled YES trades to validate.
   - Ensemble mode: When disabled, does `forecast_data` (a dict) degrade gracefully to single-model?

2. **source-monitor.py**:
   - Line ~580: `min_edge = max(min_edge, 0.15)` after computing a lower threshold for late-day NWS trades. This `max()` overrides the late-day reduction entirely. Is this intentional or a bug?
   - NWS observation staleness: We added a 2h staleness warning — is 2h the right threshold?

3. **crypto-bot.py**:
   - Volatility blending (60% IV + 40% RV): For 5-minute scan intervals, is IV (30-day forward) or RV (recent realized) more predictive? Should the blend be reversed?
   - Settlement buffer: Default 2 minutes. Kalshi closes 15 seconds before settlement. Is 2 minutes leaving money on the table or providing necessary safety margin?

4. **economics-bot.py**:
   - Stale nowcast: If the Cleveland Fed cache is >24h old, does the bot actually skip trading or just warn? Trace the `_stale` flag through the code.
   - Gas price markets: Is the 8% edge threshold too high for gas (low vol asset)? Should gas have its own threshold?

5. **strategy-trader.py**:
   - Longshot sell: When selling YES at 5c, verify the full pipeline: `longshot_edge(5)` → additive edge → `half_kelly_sell(edge, 5, ...)` → contracts. Is the sizing reasonable?

6. **position-monitor.py**:
   - Stop loss formula: `stop_price = entry_price * (1 - stop_loss_pct)`. Does this match the intended behavior? A 40% stop on a 50c entry exits at 30c (20c loss = 40% of entry). Is 40% the right default?
   - Trailing stop: Does the peak tracker update when positions are added to (averaging up)?

7. **entertainment-bot.py**:
   - Data freshness: Is there any staleness check on HDD album data? What happens if the scrape returns empty or stale data?

8. **cross-platform-arb.py**:
   - Phase 1 is monitoring only. If Phase 2 execution is enabled, verify the spread calculation accounts for Kalshi fees correctly on both YES and NO sides.

### PHASE 5: Calibration & Backtesting Audit

Read `scripts/calibrate-sigma.py`, `scripts/backtest.py`, and `config/calibration.json`:

1. **Calibration data**: Is `calibration.json` populated (n > 0 for any category)? If all n=0, ALL models are running on hardcoded defaults that have never been validated against settlement data. This is the single biggest risk.

2. **Backtesting methodology**: Does the backtest use current model parameters or the parameters from trade time? (Look-ahead bias check.) Does it skip trades without settlement data? (Survivorship bias check.) What's the sample size? (<30 settled trades = unreliable metrics.)

3. **Calibration metric**: The calibration script minimizes Brier score. For Kelly-based trading, should it minimize log loss instead? (Brier penalizes moderate predictions more than extreme ones.)

### PHASE 6: Operational Reliability

Read `scripts/supervisor.py`, `scripts/s3-sync.sh`, `scripts/reconcile-trades.py`:

1. **Supervisor**: Does it detect hung/zombie processes (running but not producing heartbeats)? Does it alert on one-shot bot crashes (strategy-trader, hdd-scraper)?

2. **S3 sync**: Is the sync atomic? What happens on partial upload (network failure mid-sync)? Can two machines sync simultaneously and overwrite each other?

3. **Reconciliation**: Does `/portfolio/settlements` pagination work for >1000 settlements? Does the realized edge calculation use fill price or order price?

4. **Alerting**: What mechanisms exist to notify the operator of critical failures (bot down, large loss, API outage)? Are there any?

### DELIVERABLE

After completing all 6 phases, produce:

1. **Critical Bugs** (code that actively loses money or enables risk of ruin): List each with file, line, explanation, and fix.

2. **Edge Leaks** (profitable opportunities being left on the table): List each with estimated weekly $ impact and fix.

3. **Calibration Gaps** (models running on unvalidated parameters): List each with risk level and what data is needed to calibrate.

4. **Operational Gaps** (silent failures, missing alerts): List each with failure scenario and fix.

5. **Prioritized Fix Plan**: Rank all issues by (impact × probability × ease of fix). Group into:
   - **Fix today** (critical bugs, easy fixes)
   - **Fix this week** (edge leaks, moderate effort)
   - **Fix this month** (calibration, operational improvements)
   - **Monitor only** (low-impact, needs more data)

For every proposed fix, include the specific code change (not just a description). If a fix requires new data collection before it can be designed, say so.
