# World-Class Crypto Bot Design

**Date:** 2026-03-05
**Status:** Approved
**Scope:** Full Tier 1+2+3 upgrade — model stack, vol forecasting, portfolio sizing, calibration

---

## Problem Statement

The crypto bot has solid engineering (GBM, JD, particle filter, regime detector) but uses hand-tuned parameters, constant volatility assumptions, and independent position sizing. Against competing quant bots on Kalshi, these are exploitable weaknesses:

- GBM underestimates far-OTM probabilities (missing stochastic vol)
- Fixed jump intensity (lambda=1.0) regardless of market regime
- 60/40 IV/RV weighting regardless of time horizon
- Each position sized independently (ignoring BTC/ETH correlation ~0.75)
- Zero calibration feedback loop (weather bot has one, crypto doesn't)

## Architecture

```
Coinbase Spot --> VolForecaster --> crypto_models --> crypto-bot (scanner)
Deribit IV ----/   GARCH(1,1)       GBM            \
Price History -/   DCC corr         JD (regime-aware) --> Portfolio Kelly
                   Seasonality      Heston                (correlation-adj)
                                    BMA Ensemble
                                    (regime-weighted)
                        ^
                        |
                   RegimeDetector
                   (HMM 4-state)
```

## New Files

| File | Purpose | Est. Lines |
|------|---------|-----------|
| `src/kalshi/crypto_models.py` | Model stack: GBM, JD, Heston, AR1 Vol, BMA ensemble | ~600 |
| `src/kalshi/vol_forecaster.py` | GARCH(1,1) + DCC correlation + intraday seasonality | ~400 |
| `scripts/calibrate-crypto.py` | Calibration pipeline: data fetch, param estimation, validation | ~500 |
| `tests/test_crypto_models.py` | Unit tests for all models | ~400 |
| `tests/test_vol_forecaster.py` | Unit tests for GARCH/DCC/seasonality | ~250 |
| `config/crypto-calibration.json` | Output: calibrated parameters (generated) | N/A |

## Modified Files

| File | Changes |
|------|---------|
| `src/kalshi/crypto-bot.py` | Use EnsembleModel, VolForecaster, portfolio Kelly, smooth edge threshold, horizon-scaled Kelly |
| `src/kalshi/probability.py` | Add `crypto_price_probability_heston()` |
| `src/kalshi/regime_detector.py` | Export regime state for ensemble weight selection |
| `requirements.txt` | Add scipy, numpy |
| `package.json` | Add `calibrate:crypto` npm script |

## Design Details

### 1. Model Stack (crypto_models.py)

Four probability models behind a unified interface:

**GBM (Geometric Brownian Motion):** Existing implementation. Baseline pricing with `P = Phi(d2)`.

**Merton Jump-Diffusion:** Existing implementation, upgraded with regime-dependent lambda:
- low_vol regime: lambda = 0.1 jumps/year
- normal: lambda = 1.0
- high_vol: lambda = 3.0
- crisis: lambda = 10.0

**Heston Stochastic Volatility:** New. Semi-closed-form via characteristic function + numerical integration (scipy).
```
dS = mu*S*dt + sqrt(V)*S*dW1
dV = kappa*(theta-V)*dt + xi*sqrt(V)*dW2
corr(dW1, dW2) = rho

P(S_T > K) via Fourier inversion of characteristic function
```
Parameters (kappa, theta, xi, rho) calibrated from Deribit options data and Coinbase price history.

**AR(1) Vol Forecast:** Simple autoregressive vol model.
```
sigma_{t+1} = alpha + beta * sigma_t + epsilon
```
Provides forward-looking vol estimate for GBM/JD inputs instead of stale backward-looking RV.

**BMA Ensemble:** Bayesian Model Averaging with regime-dependent weights:
```
P_ensemble = w_gbm * P_gbm + w_jd * P_jd + w_heston * P_heston

Regime weights:
  low_vol:  [0.50, 0.30, 0.20]
  normal:   [0.35, 0.35, 0.30]
  high_vol: [0.15, 0.40, 0.45]
  crisis:   [0.05, 0.35, 0.60]
```
Weights optimized via calibration pipeline (not hand-tuned).

### 2. Vol Forecaster (vol_forecaster.py)

**GARCH(1,1) per asset:**
```
sigma^2_{t+1} = omega + alpha * epsilon^2_t + beta * sigma^2_t
```
Fit via maximum likelihood on 90 days of 5-min returns. Updates online with each new price observation.

**DCC (Dynamic Conditional Correlation):**
```
Q_{t+1} = (1-a-b)*Q_bar + a*(eps_t * eps_t') + b*Q_t
R_t = diag(Q_t)^{-1/2} * Q_t * diag(Q_t)^{-1/2}
```
Provides real-time BTC/ETH/SOL/DOGE/XRP correlation matrix for portfolio Kelly.

**Intraday Seasonality:**
Hour-of-day vol multiplier (0.7-1.3) estimated from 90 days of hourly vol patterns. Captures US market open/close spikes, Asia session, weekend effects.

**Horizon-dependent vol weighting:**
```
w_iv = 30 / (30 + T_minutes)
w_rv = T_minutes / (30 + T_minutes)
```
Short horizons trust RV more (captures recent dynamics). Long horizons trust IV more (forward-looking).

### 3. Portfolio-Level Kelly

**Correlation-adjusted sizing:**
```
# Current (wrong): total_risk = sum(f_i)
# New (correct):   total_risk = sqrt(sum_i sum_j f_i * f_j * rho_ij)
```
Uses DCC correlation matrix. Prevents over-betting on correlated BTC+ETH positions.

**Horizon-scaled Kelly fractions:**
```
T < 15 min:   1/8 Kelly (very noisy)
T < 60 min:   1/5 Kelly
T < 6 hours:  1/4 Kelly (current)
T < 24 hours: 1/3 Kelly
T >= 24h:     1/2 Kelly
```

**Bankroll-scaled caps:**
```
max_exposure = max(500, bankroll * 0.02)  # 2% of bankroll, min $5
```

### 4. Smooth Edge Threshold

Replace hard cutoff at 0.25/0.75 with continuous function:
```
edge_required = 0.06 + 0.09 * (0.5 - abs(prob - 0.5))^2
```
At prob=0.50 (max uncertainty): requires 8.25% edge.
At prob=0.05 or 0.95 (extreme): requires 6.0% edge.
Smooth transition, no discontinuities.

### 5. Microstructure Layer

**Bid-ask bounce correction:** Subtract estimated bounce variance from RV.
```
rv_corrected = sqrt(max(0, rv^2 - 2 * spread^2 / (4 * n_observations)))
```

**Spread forecasting near settlement:** Model spread compression as function of time-to-settle.

**Temporary impact:** For limit pricing, account for 30-50% reversion of large trades within 5 minutes.

### 6. Calibration Pipeline (calibrate-crypto.py)

**Data sources:**
- Coinbase OHLCV: 90 days of 5-min candles per asset (free API)
- Deribit DVOL history: 90 days hourly (free API)
- Kalshi trade log: 89+ historical trades with settlement outcomes

**Synthetic backtest (key innovation):**
Generate ~26,000 synthetic Kalshi markets from price history:
1. For each 5-min candle, create "Will BTC be above $X in T minutes?"
2. Threshold X sampled at current_price * [0.95, 0.97, 0.99, 1.01, 1.03, 1.05]
3. Horizons T in [5, 15, 60, 360, 1440] minutes
4. Actual outcome known from future candles
5. Score each model: Brier = mean((prob - outcome)^2)

**Parameter estimation:**
- GARCH(1,1): MLE on 5-min returns (scipy.optimize.minimize)
- Heston (kappa, theta, xi, rho): Fit to Deribit IV term structure
- Jump detection: Count large moves (>3 sigma) in returns, estimate lambda and jump distribution
- DCC: MLE on standardized residuals
- Intraday seasonality: Hourly vol means over 90 days
- Ensemble weights: Grid search minimizing Brier score per regime

**Output:** `config/crypto-calibration.json` with all parameters, Brier scores, data window, timestamp.

**Schedule:** `npm run calibrate:crypto` — run weekly or when Brier drift >10%.

## Dependencies

- `scipy` — Heston characteristic function (quad), GARCH MLE (optimize.minimize)
- `numpy` — Matrix operations for DCC, array math for GARCH

## Validation

Backtest on 89 historical trades + 26,000 synthetic markets. Compare:
- Model-by-model Brier scores
- Ensemble vs best single model
- Per-asset, per-horizon breakdown
- Old parameters vs new calibrated parameters

## Success Criteria

- [ ] Ensemble Brier score < 0.15 on synthetic validation set
- [ ] Ensemble outperforms every individual model on average
- [ ] Calibrated parameters differ meaningfully from hand-tuned defaults
- [ ] Correlation-adjusted Kelly reduces total portfolio risk by >15%
- [ ] GARCH vol forecast has lower RMSE than simple RV for 15-min horizon
- [ ] All existing tests pass (no regressions)
- [ ] New test coverage >90% for crypto_models.py and vol_forecaster.py

## Risk Assessment

| Risk | Probability | Mitigation |
|------|------------|------------|
| scipy adds deployment complexity | Low | Already used by numpy ecosystem |
| Heston calibration unstable | Medium | Fallback to JD+GBM if Heston fails |
| GARCH overfits on 90 days | Medium | Use rolling window, validate on holdout |
| Over-engineering for small trade volume | Medium | Calibration validates real edge exists |
| Coinbase API rate limits during calibration | Low | Batch requests, cache locally |
