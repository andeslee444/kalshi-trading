# World-Class Economics Bot Design

**Date:** 2026-03-04
**Status:** Approved
**Builds on:** `01-economics-bot.md` (bug fixes)

---

## Executive Summary

Redesign the economics bot from a single-distribution nowcast trader into a **scenario-weighted Bayesian system** that honestly models the uncertainty of Trump-era macroeconomics. The core insight: CPI outcomes are not normally distributed when tariff policy can shift +/-0.5pp overnight. A mixture of scenario-conditional distributions, weighted by market-implied probabilities, captures bimodal/multimodal risk that a single bell curve cannot.

**Key upgrades:**
1. Bayesian belief filter fusing Cleveland Fed + Truflation + TIPS (replaces heuristic macro bias)
2. 6-scenario mixture model with market-implied weights (replaces single Gaussian CDF)
3. Uncertainty-aware Kelly sizing that scales with model confidence (replaces fake-certainty quarter-Kelly)
4. Auto-scaling exposure ratchet based on settlement track record
5. All 5 bugs from `01-economics-bot.md` fixed as foundation

---

## Architecture

```
economics-bot.py
  |
  +-- cpi_belief_filter.py (NEW - Bayesian fusion)
  |     +-- macro_engine.py (Truflation, FRED data)
  |     +-- probability.py (base sigma functions)
  |
  +-- scenario_engine.py (NEW - 6-scenario model)
  |     +-- polymarket_client.py (market-implied weights)
  |     +-- macro_engine.py (FRED proxy signals)
  |
  +-- probability.py (uncertainty_kelly, econ_nowcast_probability)
  +-- kalshi_auth.py (TradeManager with fixed sizing)
```

---

## Phase 1: Bug Fixes (Foundation)

All 5 bugs from `01-economics-bot.md`, with adjustments from code review:

### BUG-1: CPI Sigma Too Narrow
**File:** `probability.py`, `cpi_nowcast_sigma()`
**Fix:** Widen fallback asymptote from 0.10% to 0.40%:
```python
return 0.05 + 0.35 * (1 - math.exp(-0.05 * d))
# d=0: 0.05%, d=7: 0.17%, d=30: 0.27%, d=107: 0.40%
```
Note: This gets superseded by the Bayesian filter in Phase 2, but is needed as an immediate safety fix for the production bot.

### BUG-2: Position Sizing Ceiling vs Floor
**File:** `kalshi_auth.py`, `_effective_max_trade_cents()` and `_effective_max_daily_loss_cents()`
**Fix:** Change `max()` to `min()` so static config is a hard ceiling:
```python
return min(static_cents, dynamic_cents) if dynamic_cents > 0 else static_cents
```
Update docstrings from "floor" to "ceiling" to match.

### BUG-3: No Ticker-Level Concentration Limit
**File:** `economics-bot.py`, `scan_and_trade()`
**Fix:** Before each trade, sum existing position cost across ticker family. Skip if:
- Per-ticker-family > 15% of bankroll
- Per-release-date > 25% of bankroll
- Total econ exposure > 40% of bankroll

### BUG-4: feedparser Crash
**File:** `economics-bot.py`, line 29
**Fix:** Make `MacroEngine` import optional:
```python
try:
    from macro_engine import MacroEngine
except ImportError:
    MacroEngine = None
```
Note: `feedparser` is already in `requirements.txt`. The real fix is graceful degradation.

### BUG-5: Edge Not Stored
**File:** `economics-bot.py`, `place_order()` call
**Fix:** Add `edge=round(edge, 4)` as a primary parameter alongside `raw_edge`.

---

## Phase 2: Bayesian Belief Filter

### New Module: `src/kalshi/cpi_belief_filter.py`

Conjugate normal Bayesian filter that fuses multiple CPI nowcast sources into a single posterior estimate with proper uncertainty.

```python
class CPIBeliefFilter:
    """Bayesian fusion of multiple CPI nowcast sources.

    Replaces the heuristic macro_signal.cpi_bias adjustment with
    proper precision-weighted Bayesian inference.
    """

    def __init__(self, prior_mean, prior_sigma):
        self.mean = prior_mean      # Cleveland Fed nowcast
        self.sigma = prior_sigma    # cpi_nowcast_sigma(days_to_release)

    def update(self, observation, obs_sigma):
        """Conjugate normal update: fuse a new observation.

        After N updates, posterior is tighter than any single source.
        """
        prior_precision = 1 / self.sigma**2
        obs_precision = 1 / obs_sigma**2
        posterior_precision = prior_precision + obs_precision

        self.mean = (self.mean * prior_precision + observation * obs_precision) / posterior_precision
        self.sigma = 1 / math.sqrt(posterior_precision)

    @property
    def posterior(self):
        return (self.mean, self.sigma)
```

### Data Source Integration

| Source | Observation | Likelihood sigma | Rationale |
|--------|-------------|------------------|-----------|
| Cleveland Fed | CPI nowcast (scraped) | `cpi_nowcast_sigma(d)` | Their published CI width |
| Truflation | Real-time CPI proxy | 0.15pp | Experimental, wider uncertainty |
| TIPS breakeven | 10Y breakeven (FRED T10YIE) | 0.25pp | Confounded by real rate expectations |

### Fusion Logic in economics-bot.py

```python
# Replace current macro bias adjustment with:
base_sigma = cpi_nowcast_sigma(days_to_release)
belief = CPIBeliefFilter(nowcast_value, base_sigma)

if truflation_cpi is not None:
    belief.update(truflation_cpi, obs_sigma=0.15)

if tips_breakeven is not None:
    belief.update(tips_breakeven, obs_sigma=0.25)

fused_nowcast, posterior_sigma = belief.posterior
```

### Graceful Degradation

If Truflation is down: filter uses Cleveland Fed + TIPS only (wider posterior).
If TIPS unavailable: filter uses Cleveland Fed + Truflation only.
If only Cleveland Fed available: filter = prior (no update). Same as current behavior but with honest sigma.

---

## Phase 3: Scenario Engine

### New Module: `src/kalshi/scenario_engine.py`

6 macro scenarios with dynamic weights. Final probability is a weighted mixture.

### Scenarios

| # | Scenario | Default Weight | CPI Shift | Sigma Mult | Signal Sources |
|---|----------|---------------|-----------|------------|----------------|
| 1 | Status Quo | 0.45 | 0 | 1.0x | Residual (1 - others) |
| 2 | Tariff Escalation | 0.15 | +0.3 to +0.5pp | 1.5x | Polymarket tariff markets |
| 3 | Tariff Reversal / Deal | 0.10 | -0.2pp | 1.2x | Polymarket deal markets |
| 4 | Supply Shock | 0.08 | +0.5 to +1.0pp | 2.5x | Crude oil vs 90d MA (FRED DCOILWTICO) |
| 5 | Recession / Demand Collapse | 0.12 | -0.3 to -0.5pp | 2.0x | Yield curve (FRED T10Y2Y) |
| 6 | Stagflation | 0.10 | +0.2pp | 3.0x | GDPNow declining + TIPS rising |

### Weight Computation

```python
def compute_scenario_weights(polymarket_data, fred_data):
    weights = DEFAULT_WEIGHTS.copy()  # base rate priors

    # Tier 1: Market-implied (if available)
    tariff_prob = polymarket_data.get("tariff_escalation_prob")
    if tariff_prob is not None:
        weights["tariff_escalation"] = tariff_prob * 0.5
        weights["tariff_reversal"] = (1 - tariff_prob) * 0.3

    # Tier 2: FRED proxy signals (always available)
    oil_dev = fred_data["crude_oil"] / fred_data["crude_oil_90d_ma"] - 1
    if oil_dev > 0.20:
        weights["supply_shock"] += 0.10

    yield_spread = fred_data["T10Y2Y"]
    if yield_spread < -0.50:
        weights["recession"] += 0.10

    gdpnow = fred_data.get("gdpnow")
    tips_trend = fred_data.get("tips_5y_minus_10y")
    if gdpnow is not None and gdpnow < 0.5 and tips_trend and tips_trend > 0:
        weights["stagflation"] += 0.08

    # Normalize to sum = 1.0
    total = sum(weights.values())
    return {k: v/total for k, v in weights.items()}
```

### Probability Computation

```python
def scenario_probability(fused_nowcast, posterior_sigma, threshold, direction,
                          scenario_weights, scenario_configs):
    """Mixture probability across scenarios."""
    total_prob = 0.0
    per_scenario = {}

    for name, weight in scenario_weights.items():
        config = scenario_configs[name]
        shifted_mean = fused_nowcast + config["cpi_shift"]
        scaled_sigma = posterior_sigma * config["sigma_mult"]

        # Per-scenario probability
        z = (threshold - shifted_mean) / scaled_sigma
        if direction == "above":
            p = 1 - normal_cdf(z)
        else:
            p = normal_cdf(z)

        per_scenario[name] = p
        total_prob += weight * p

    # Scenario agreement: 1.0 = all agree, 0.0 = total split
    probs = list(per_scenario.values())
    agreement = 1.0 - (max(probs) - min(probs))

    return total_prob, agreement, per_scenario
```

### Polymarket Fallback

When Polymarket has no relevant tariff/trade markets:
1. FRED proxy signals (crude oil, yield curve, TIPS) provide weaker but always-available adjustments
2. Base rate priors remain (calibrated from 2020-2026 frequency of each scenario type)
3. Scenario agreement drops → Kelly confidence drops → smaller positions (self-regulating)

---

## Phase 4: Uncertainty-Aware Sizing

### New Function: `uncertainty_kelly()` in `probability.py`

Replaces `quarter_kelly()` for economics bot. Scales position size by model confidence.

```python
def uncertainty_kelly(edge, price_cents, max_cost_cents, bankroll_cents,
                      scenario_agreement, posterior_sigma, fee_cents=0):
    """Kelly sizing scaled by model confidence.

    Two confidence multipliers:
    1. scenario_agreement: do all scenarios agree on the direction?
    2. posterior_sigma: how tight is the Bayesian estimate?

    Combined via geometric mean to avoid double-counting.
    """
    base_count, risk, details = quarter_kelly(
        edge, price_cents, max_cost_cents, bankroll_cents, fee_cents,
        return_details=True
    )

    # Scenario agreement: 1.0 → full size, 0.5 → ~35% size
    agreement_mult = 0.1 + 0.9 * scenario_agreement**2

    # Sigma confidence: narrow posterior → full size
    sigma_mult = min(1.0, 0.10 / max(posterior_sigma, 0.05))

    confidence = math.sqrt(agreement_mult * sigma_mult)
    adjusted_count = max(1, int(base_count * confidence))

    return adjusted_count, risk * confidence, {
        **details,
        "confidence": confidence,
        "agreement_mult": agreement_mult,
        "sigma_mult": sigma_mult,
    }
```

### Concentration Limits (in `scan_and_trade()`)

```
Level 1: Per-trade           → min(static_max, bankroll * pct)
Level 2: Per-ticker-family   → 15% of bankroll (e.g., KXECONSTATCPIYOY-26MAY-*)
Level 3: Per-release-date    → 25% of bankroll (all May CPI contracts)
Level 4: Total econ exposure → 40% of bankroll (all CPI + GDP + Jobs + Gas)
```

---

## Phase 5: Scale-With-Edge Ratchet

### New: `EdgeScaler` class (in `economics-bot.py` or `capital_allocator.py`)

```python
class EdgeScaler:
    TIERS = [
        {"min_wins": 0,  "max_exposure_pct": 0.20},  # Start: 20% of bankroll
        {"min_wins": 5,  "max_exposure_pct": 0.40},  # After 5 wins: 40%
        {"min_wins": 10, "max_exposure_pct": 0.60},  # After 10 wins: 60%
        {"min_wins": 20, "max_exposure_pct": 1.00},  # After 20 wins: full
    ]

    def current_limit(self, settlement_record):
        wins = sum(1 for s in settlement_record if s["profitable"])
        loss_streak = count_consecutive_losses(settlement_record[-10:])
        loss_penalty = 0.5 if loss_streak >= 3 else 1.0

        tier = self.TIERS[0]
        for t in self.TIERS:
            if wins >= t["min_wins"]:
                tier = t

        return tier["max_exposure_pct"] * loss_penalty
```

State tracked in `config/calibration.json` under `economics.edge_scaler`.

---

## Phase 6: Integration & Testing

### Unit Tests
- `test_cpi_belief_filter.py`: Filter convergence, multi-source fusion, graceful degradation
- `test_scenario_engine.py`: Weight normalization, probability mixture, Polymarket fallback
- `test_uncertainty_kelly.py`: Confidence scaling, edge cases (zero agreement, wide sigma)

### Integration Tests
- Full `scan_and_trade()` with mocked Cleveland Fed + Truflation + FRED data
- Concentration limit enforcement across multiple scan cycles
- Edge scaler tier progression

### Backtest
- Compare old model (single Gaussian) vs new model (scenario mixture) against 2020-2026 CPI releases
- Measure: Brier score, calibration curve, expected P&L at various Kelly fractions
- Validate: scenario engine would have widened sigma before tariff-surprise CPI prints

### Dashboard Additions
- Scenario weights visualization (bar chart)
- Bayesian filter posterior (mean + CI)
- Confidence multiplier history
- Edge scaler tier status

---

## Implementation Order

| Phase | Description | Dependencies | Estimated Complexity |
|-------|-------------|--------------|---------------------|
| 1 | Bug fixes (5 bugs) | None | Low - direct code fixes |
| 2 | Bayesian belief filter | Phase 1 (sigma fix) | Medium - new module, ~150 lines |
| 3 | Scenario engine | Phase 2 (filter output feeds scenarios) | High - new module, ~300 lines, FRED/Polymarket integration |
| 4 | Uncertainty-aware sizing | Phase 3 (scenario agreement metric) | Medium - new function + scan_and_trade rewire |
| 5 | Scale-with-edge ratchet | Phase 1 (concentration limits) | Low - ~80 lines, config changes |
| 6 | Testing & integration | Phases 1-5 | Medium - comprehensive test suite |

---

## Success Criteria

- [ ] Bot running without crashes for 7 consecutive days
- [ ] `model_prob` < 0.99 for all contracts >30 days from release
- [ ] No single trade exceeds static max ($50)
- [ ] No single ticker family exceeds 15% of bankroll
- [ ] Scenario weights sum to 1.0 and update every scan
- [ ] Bayesian filter posterior is tighter than any single source when multiple sources available
- [ ] Confidence multiplier < 0.5 when scenario agreement < 0.6
- [ ] Edge scaler starts at 20% and only scales after proven settlements
- [ ] Brier score < 0.20 on CPI markets
- [ ] All 3 data sources (Cleveland Fed, Truflation, TIPS) fetched and logged every scan
- [ ] Graceful degradation verified: bot trades correctly with 0, 1, 2, or 3 data sources available

---

## What Makes This World-Class

1. **Honest uncertainty** - Bimodal policy risk modeled as bimodal, not forced into a bell curve
2. **Information fusion** - Three independent sources combined via Bayes, not heuristic averaging
3. **Adaptive sizing** - Bets proportional to confidence, not just edge
4. **Self-scaling** - Capital deployment proves itself through settlements
5. **Graceful degradation** - Every data source optional; system always trades with appropriate uncertainty
6. **Regime-aware** - Scenario engine captures Trump-era tariff/policy discontinuities
7. **Auditable** - Every trade records scenario weights, filter posterior, confidence multiplier
