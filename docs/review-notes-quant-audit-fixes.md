# Quant Audit Fix — Review Notes

## Context
Two prior audit commits (56699b3, 30feecf) fixed 22 issues but left 12 recommendations. This commit addresses all 12 across 5 phases.

## Changes by Recommendation

### R1 — Student-t for economics (probability.py)
- `econ_nowcast_probability()` now uses Student-t CDF (default df=5) instead of Gaussian
- **Why:** CPI/GDP surprise prints at 3+ sigma happen far more often than normal predicts. Student-t with df=5 gives ~3x heavier tails at 3-sigma.
- **Backward-compatible:** df=None loads from calibration.json, falls back to 5. Existing callers unchanged.

### R10 — Continuous CPI sigma decay (probability.py)
- Replaced step-function `cpi_nowcast_sigma()` with exponential: `0.03 + 0.07 * (1 - exp(-0.20 * d))`
- **Why:** Step function had discontinuous jumps (0.04→0.06 at day 3) causing sudden bet size changes.
- Calibration.json `cpi.sigma_by_days` still overrides (no change to that path).
- Added `gdp_nowcast_sigma()` with wider range (GDP is noisier than CPI).

### R12-prep — _probit() and _norm_pdf() (probability.py)
- Added Acklam rational approximation for inverse normal CDF (~70 lines)
- Accurate to ~1e-4 for p in (0.001, 0.999)
- **Why:** Needed by Phase 5 market-maker binary sigma formula.

### R3 — Sigma multiplier for ensemble (probability.py, weather-bot.py)
- `weather_probability()` gains `sigma_override` param
- `ensemble_weather_probability()` gains `sigma_multiplier` param — widens sigma when models disagree
- Weather-bot: computes `spread_mult` once (was duplicated at lines 210 and 265), passes to ensemble
- **Why:** When forecast models disagree by 8°F, probability should be wider. Previously only edge threshold was doubled.

### R11 — Brier-weighted BMA (probability.py)
- `ensemble_weather_probability()` now checks `data/backtest-results.json` for `weather.per_model_brier`
- If present, computes weights as 1/brier (inverse weighting, normalized)
- **Why:** Static weights ignore model accuracy. Feature activates once backtest.py outputs per-model Brier (future work).
- Graceful fallback: no data → use static/calibration weights.

### R9 — SIGUSR1 graceful shutdown (kalshi_auth.py, 9 daemon bots)
- Added `_shutdown_requested` flag, `is_shutdown_requested()` export, SIGUSR1 handler
- All 9 daemon bots check `is_shutdown_requested()` before `time.sleep()` in their main loop
- **Why:** Supervisor sends SIGUSR1 before SIGTERM, but default behavior was immediate termination.

### R8 — Webhook --require-secret (github-webhook.py)
- Refactored inline code to `main(args=None)` for testability
- Added `--require-secret` flag: exits with code 1 if GITHUB_WEBHOOK_SECRET is empty
- **Why:** Empty secret silently accepts all payloads in production.

### R7 — Dynamic crypto vol in position monitor (position-monitor.py)
- Added `_get_latest_crypto_vol(asset)` — reads crypto-decisions.json, rejects if >4h stale
- Replaced hardcoded `realized_vol_pct=0.50` with dynamic vol from crypto-bot's decision log
- **Why:** Crypto-bot uses dynamically fetched vol from Coinbase/Deribit; position monitor should too.

### R4 — Market maker kParam 25→8 (bots-config.json)
- **Why:** k=25 implies ~25 orders/hour — appropriate for equity markets, not Kalshi. Produces dangerously tight spreads on illiquid markets.

### R2 — Crypto JD params from config (bots-config.json, crypto-bot.py)
- Added `jumpDiffusion` section to crypto config with explicit params
- Crypto-bot loads JD params from config instead of hardcoded defaults
- **Why:** Jump-diffusion defaults were academic placeholders, should be tunable.

### R5 — Deduplicate 4-way JD/GBM branch (crypto-bot.py)
- Extracted `_compute_crypto_prob()` helper handling T-direction and bracket with JD comparison
- Replaced 4 duplicated code paths with single function call
- **Why:** Any formula fix previously needed 4 identical edits.

### R6 — Beatrelease confidence floor 0.55→0.51 (beatrelease-scanner.py)
- Changed `max(0.55, 0.50 + price_edge * 2.0)` → `max(0.51, 0.50 + price_edge * 1.5)`
- **Why:** Floor of 55% meant zero-edge blog posts got 5% upward bias. With the 0.15 LLM calibration discount, a no-data post now produces 0.36 calibrated confidence → below 0.60 gate → no trade.

### R12 — Binary sigma via CDF derivative (market-maker.py)
- `estimate_market_sigma()` now accepts `mid_price` param
- For KXHIGH with valid mid: `price_sigma = temp_sigma * phi(probit(mid/100)) * 100`
- **Why:** Linear `temp_sigma * 4` ignores that price sensitivity varies nonlinearly. At mid=50c max sensitivity; at 10c/90c much less.
- Fallback: no mid_price → keep linear approximation.

## Test Changes
- **test_probability.py:** +7 new test classes (probit, student-t econ, CPI smoothness, GDP sigma, ensemble sigma_multiplier, binary sigma)
- **test_infrastructure.py:** New file — SIGUSR1 flag, webhook --require-secret, crypto vol staleness
- **6 test stub files:** Added `is_shutdown_requested` and `crypto_price_probability` to fake_auth/fake_prob stubs
- Updated `test_day_14_near_010` bounds for exponential decay (was [0.09, 0.11], now [0.08, 0.11])

## Risks / Review Focus Areas
1. **Student-t in econ_nowcast_probability** changes trade behavior — verify economics-bot edge calculations still reasonable
2. **Exponential CPI sigma** changes the fallback curve — existing calibration.json overrides are unaffected
3. **sigma_multiplier in ensemble** is new behavior — verify weather-bot logs show correct spread_mult values
4. **_compute_crypto_prob helper** must produce identical output to the 4-way branch it replaced
5. **Test stub additions** — verify no test files were missed (grep for `fake_auth.setup_signal_handlers`)
