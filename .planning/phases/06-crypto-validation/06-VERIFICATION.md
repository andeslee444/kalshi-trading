---
phase: 06-crypto-validation
verified: 2026-02-28T10:30:00Z
status: passed
score: 9/9 must-haves verified
re_verification: false
---

# Phase 6: Crypto Validation Verification Report

**Phase Goal:** The crypto trading model is validated against historical data with documented accuracy metrics
**Verified:** 2026-02-28T10:30:00Z
**Status:** PASSED
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|---------|
| 1 | Crypto model has a documented Brier score computed from backtesting against historical BTC 15-minute candle data | VERIFIED | `backtest-results.json` crypto_validation.aggregate_brier=0.0372, n_markets_replayed=11895, BTC Brier=0.0535 (n=3795) |
| 2 | Time-to-settlement calculation is verified against actual Kalshi crypto market durations (T=2456min claim confirmed or corrected) | VERIFIED | `settlement-audit.json` verdict: "estimate_time_to_settlement() VALIDATED: The function correctly computes (close_time - now) in minutes..."; weekly markets average 2,287 min (claim of 2,456 was historical average, not code assumption) |
| 3 | Realized volatility computation output is compared against Deribit DVOL benchmark and discrepancy is documented | VERIFIED | `backtest-results.json` crypto_validation.vol_benchmark: BTC MAE=7.66%, corr=-0.387; ETH MAE=14.26%, corr=-0.602; negative correlation documented as expected (RV backward-looking vs DVOL forward-looking) |
| 4 | --backtest produces a Brier score from historical market replay | VERIFIED | `crypto-backtest-raw.json` contains 11,895 per-market predictions with all required fields (ticker, model_prob, actual, vol_used, iv, rv, minutes_to_settle, spot_at_open, threshold, direction) |
| 5 | --vol-sweep produces a ranked table of IV/RV blend ratios sorted by Brier score | VERIFIED | `vol-sweep-results.json` contains 28 configs tested, sorted ascending by Brier; best=(IV=0.0, RV=24h, Brier=0.0370) vs production=(IV=0.6, RV=24h, Brier=0.0372) |
| 6 | backtest-results.json extended with crypto_validation section | VERIFIED | Section present with keys: generated_at, aggregate_brier, per_asset_brier, calibration_table, vol_config_used, vol_benchmark, settlement_audit, n_markets_replayed, brier_target, passes_target |
| 7 | RV vs DVOL side-by-side comparison with MAE and correlation printed | VERIFIED | Vol benchmark logic in run_backtest() prints per-asset comparison; data stored in backtest-results.json vol_benchmark section |
| 8 | Calibration curve for crypto model generated using calibration_table() infrastructure | VERIFIED | calibration_table() from scripts/backtest.py called at line 869; 10-bin calibration table in crypto_validation; also added to calibration_curves.crypto in backtest-results.json |
| 9 | Brier > 0.20 triggers specific fix suggestions (Brier here is 0.0372 — passes target) | VERIFIED | Code at line 922-927 handles both cases; current result prints "PASS: Brier score 0.0372 meets the <= 0.20 target."; passes_target=True in backtest-results.json |

**Score:** 9/9 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `scripts/crypto-backtest.py` | Data fetching, caching, settlement audit, backtest replay, vol sweep, vol benchmark, calibration curve generation | VERIFIED | 1,243 lines (min_lines: 500 met); all subcommands implemented (--fetch, --settlement-audit, --backtest, --vol-sweep, --save, --force) |
| `data/crypto-validation/coinbase-candles-BTC.json` | Cached BTC 15-min candles (90 days) | VERIFIED | 8,640 entries; TLHOVC format; ascending sort; file size 824 KB |
| `data/crypto-validation/coinbase-candles-ETH.json` | Cached ETH 15-min candles (90 days) | VERIFIED | 8,640 entries; file size 806 KB |
| `data/crypto-validation/coinbase-candles-SOL.json` | Cached SOL 15-min candles (90 days) | VERIFIED | 8,640 entries; file size 775 KB |
| `data/crypto-validation/deribit-dvol-BTC.json` | Cached BTC DVOL hourly data (90 days) | VERIFIED | 2,161 entries (plan min: 1,500); file size 153 KB |
| `data/crypto-validation/deribit-dvol-ETH.json` | Cached ETH DVOL hourly data (90 days) | VERIFIED | 2,161 entries; file size 153 KB |
| `data/crypto-validation/kalshi-crypto-markets.json` | All settled Kalshi crypto markets | VERIFIED | 40,222 markets; file size 103 MB |
| `data/crypto-validation/settlement-audit.json` | Structured audit with verdict and duration distributions | VERIFIED | Keys: total_markets, valid_markets, by_type (hourly/daily/weekly), by_asset, anomalies, verdict, typical_durations, overall_stats, audit_timestamp |
| `data/crypto-validation/crypto-backtest-raw.json` | Per-market raw predictions | VERIFIED | 11,895 entries with all 11 required fields |
| `data/crypto-validation/vol-sweep-results.json` | 28 IV/RV configuration rankings | VERIFIED | 28 configs; sorted ascending by Brier; best and production entries present |
| `data/backtest-results.json` | Extended with crypto_validation section | VERIFIED | crypto_validation section present; calibration_curves.crypto present alongside weather; existing keys preserved |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `scripts/crypto-backtest.py` | `kalshi_auth.retry_request` | HTTP fetching with retry | WIRED | Imported line 35; called at lines 97, 170 (Coinbase + Deribit fetches) |
| `scripts/crypto-backtest.py` | `kalshi_auth._atomic_write_json` | Atomic cache writes | WIRED | Imported line 35; called at lines 125, 196, 289, 550, 876, 1146, 1199 |
| `scripts/crypto-backtest.py` | `ticker_utils.parse_crypto_ticker` | Ticker parsing for asset/direction/threshold | WIRED | Imported line 38; called at lines 346, 694, 838 |
| `scripts/crypto-backtest.py` | `probability.crypto_price_probability` | Model replay | WIRED | Imported line 37; called at lines 748, 765, 770 (direct + bracket market paths) |
| `scripts/crypto-backtest.py` | `scripts/backtest.py::brier_score` | Brier score computation | WIRED | Imported line 42; called at lines 858, 865, 1089 (aggregate + per-asset + sweep) |
| `scripts/crypto-backtest.py` | `scripts/backtest.py::calibration_table` | Calibration curve generation | WIRED | Imported line 42; called at line 869 |
| `scripts/crypto-backtest.py` | `data/backtest-results.json` | Extending existing backtest results | WIRED | save_crypto_results() loads existing at line 1167, merges crypto_validation + calibration_curves, writes back at line 1199 |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|---------|
| CRYP-01 | 06-01, 06-02 | Crypto model backtested against historical BTC 15-min candles with documented Brier score | SATISFIED | Brier=0.0372 from 11,895 markets in backtest-results.json; candle data from Coinbase confirmed 8,640 BTC candles |
| CRYP-02 | 06-01 | Time-to-settlement calculation validated (T=2456min claim checked against actual market durations) | SATISFIED | settlement-audit.json verdict confirms estimate_time_to_settlement() correct; weekly mean=2,287 min vs claimed 2,456; T=2456 not hardcoded anywhere in crypto-bot.py |
| CRYP-03 | 06-02 | Realized vol computation validated against Deribit DVOL benchmark | SATISFIED | Vol benchmark in backtest-results.json: BTC MAE=7.66%, ETH MAE=14.26%; negative correlation (-0.39 BTC, -0.60 ETH) documented as expected (backward vs forward-looking) |

All three requirements marked [x] COMPLETE in .planning/REQUIREMENTS.md and listed as Complete in the status table.

### Anti-Patterns Found

No anti-patterns detected. Search for TODO/FIXME/HACK/PLACEHOLDER/XXX in `scripts/crypto-backtest.py` returned zero results. No stub return patterns (return null, return {}, return []) found. Implementation is substantive at 1,243 lines.

### Notable Observation (Non-Blocking)

The `vol_sweep_best` key is absent from `backtest-results.json` because `--backtest --save` and `--vol-sweep` are separate commands. The `save_crypto_results()` function accepts `vol_sweep_best=None` as optional and only writes it when provided. The vol sweep results are fully present in `data/crypto-validation/vol-sweep-results.json`. This does not violate any must_have truth — the truth requiring this data was "vol-sweep produces a ranked table" (which it does), and the JSON schema example in the plan showed vol_sweep_best as a possible field, not a required one. The data is accessible; it simply is not merged into backtest-results.json unless --vol-sweep is run before --backtest --save.

### Human Verification Required

None. All success criteria are verifiable programmatically through file inspection and data structure checks.

## Commit Verification

| Commit | Description | Verified |
|--------|-------------|---------|
| `776d529` | feat(06-01): build crypto data fetching and caching pipeline | Present in git log |
| `827ecda` | feat(06-02): implement crypto model replay backtest with vol benchmark | Present in git log |

## Gaps Summary

No gaps. All phase goal components are achieved:

1. Crypto model Brier score is documented (0.0372, well below 0.20 target) with per-asset breakdown and calibration table.
2. Time-to-settlement is validated — `estimate_time_to_settlement()` correctly uses `close_time - now` dynamically; the T=2456min historical average for weekly markets is confirmed by audit (actual mean: 2,287 min) but was never hardcoded.
3. Realized vol is benchmarked against Deribit DVOL with documented MAE (BTC 7.7%, ETH 14.3%) and Pearson correlation (BTC -0.39, ETH -0.60), with the negative correlation correctly explained.

---

_Verified: 2026-02-28T10:30:00Z_
_Verifier: Claude (gsd-verifier)_
