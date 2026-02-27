# Phase 2: Position Sizing - Research

**Researched:** 2026-02-27
**Domain:** Kelly criterion position sizing, fee treatment, bankroll computation
**Confidence:** HIGH

## Summary

Phase 2 addresses three specific, well-bounded requirements: fixing the Kelly bankroll basis (SIZE-01), standardizing fee treatment (SIZE-02), and defaulting to quarter-Kelly sizing (SIZE-03). The codebase is already well-structured for these changes -- the `probability.py` module centralizes all sizing functions, the `PortfolioAllocator` already uses `available_balance` for the bankroll, and all bots already pass `fee_cents` to Kelly functions. The work is primarily about verifying the existing bankroll flow is correct, auditing for any remaining `edge_after_fees()` calls, and changing the default sizing method from `half_kelly` to `quarter_kelly` for bots without calibration-validated models.

**Primary recommendation:** Implement all three SIZE requirements in a single plan since they are tightly coupled and the changes are localized to `probability.py`, `capital_allocator.py`, and the sizing call sites in each bot.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| SIZE-01 | All Kelly sizing functions use available balance (not total balance) as bankroll basis | Bankroll flow analysis (Section: Architecture Patterns > Bankroll Flow), allocator already uses `available_balance` but `get_status()` reports `total` as `bankroll_cents` -- needs audit |
| SIZE-02 | Fee treatment is standardized across all bots (single pattern via `fee_cents` parameter) | Fee audit (Section: Architecture Patterns > Fee Flow), `edge_after_fees()` is deprecated but may still be called; all active bots already use the correct `fee_cents` pattern |
| SIZE-03 | Quarter-Kelly is the default sizing until models are calibration-validated | Sizing method audit (Section: Current State > Bot Sizing Methods), requires changing 6 bots from `half_kelly` to `quarter_kelly` |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `probability.py` | N/A (internal) | All Kelly sizing functions: `half_kelly`, `half_kelly_sell`, `quarter_kelly`, `high_conviction_kelly` | Single source of truth for position sizing math |
| `capital_allocator.py` | N/A (internal) | Bankroll computation, budget allocation, concentration limits | Provides `BudgetResponse.bankroll_cents` to all bots |
| `kalshi_auth.py` | N/A (internal) | `KalshiClient.get_balance()` returns `(balance, available_balance)` | Kalshi API wrapper returning both balance types |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `config/calibration.json` | N/A | Per-bot/per-city calibration parameters | Consulted to determine if a bot's model is "calibration-validated" |
| `config/bots-config.json` | N/A | Per-bot maxTradeAmount, maxDailyTrades, maxDailyLoss | Config caps that bound Kelly output |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Quarter-Kelly as blanket default | Per-bot Kelly fraction from config | Config-driven is more flexible but adds complexity; quarter-Kelly is safer and simpler for the current validation stage |
| Hard-coding `quarter_kelly` calls | Adding a `default_kelly_fraction` param to allocator | Would centralize the decision but couples allocator to sizing logic; current approach is explicit and auditable per-bot |

## Architecture Patterns

### Current Bankroll Flow (SIZE-01)
```
KalshiClient.get_balance()
  -> returns (balance_cents, available_balance_cents)
     where available_balance = balance - locked_in_open_orders

PortfolioAllocator._get_balance()
  -> calls client.get_balance()
  -> caches for 5 seconds
  -> returns (total, available)

PortfolioAllocator._request_budget_inner()
  -> bankroll = available_balance   [line 472]
  -> BudgetResponse(bankroll_cents=bankroll)  [line 558]

Each bot:
  -> budget = allocator.request_budget(...)
  -> half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee)
```

**Current state:** The allocator ALREADY uses `available_balance` (not `total_balance`) for the bankroll. This was implemented in Phase 1 and tested in `test_allocator.py::TestBankrollUsesAvailable`. The key line is `capital_allocator.py:472`: `bankroll = available_balance`.

**Issue found:** `PortfolioAllocator.get_status()` at line 567 reports `bankroll_cents: total` (total balance, not available). This is a logging/monitoring inconsistency -- it doesn't affect trade sizing but is misleading for the dashboard. Should be `available`.

**Verification needed:** Confirm that the Kalshi API's `available_balance` field correctly subtracts open position exposure (not just pending orders). If it only subtracts pending order capital, we may need to manually compute `balance - sum(position_exposure)`. This requires a live API test.

### Current Fee Flow (SIZE-02)
```
All bots follow this pattern:
  fee = kalshi_fee_cents(price)    # probability.py:530
  half_kelly(edge, price, max_cost, bankroll_cents=..., fee_cents=fee)

Inside half_kelly:
  win_amount = (100 - fee_cents) - price_cents   # fee reduces payout
  b = win_amount / loss_amount
  kelly_f = (b * our_prob - (1 - our_prob)) / b
```

**Current state:** All 8 active bots already pass `fee_cents` to their Kelly sizing function. The deprecated `edge_after_fees()` function exists in `probability.py:536` but has a docstring warning against use. No active bot calls it.

**Standardization work:** Minimal. The pattern is already standard. The only action is:
1. Verify no code path calls `edge_after_fees()` (confirmed: no active bot does)
2. Consider removing or hiding `edge_after_fees()` to prevent future misuse
3. Ensure `beatrelease-scanner.py` (which uses `sizing_method="llm_recommended"` with no Kelly at all) at least records `fee_cents` in trade records

### Current Sizing Method Audit (SIZE-03)
```
Bot                    | Current Sizing     | Should Be (SIZE-03)
-----------------------|--------------------|-----------------------
weather-bot            | half_kelly + quarter_kelly (brackets) + high_conviction_kelly | quarter_kelly default until validated
entertainment-bot      | half_kelly          | quarter_kelly until validated
source-monitor         | half_kelly          | quarter_kelly until validated
economics-bot          | half_kelly          | quarter_kelly until validated
crypto-bot             | quarter_kelly       | ALREADY CORRECT
strategy-trader        | half_kelly_sell     | quarter_kelly_sell? (sell-side quarter-Kelly not implemented)
cross-platform-arb     | half_kelly          | quarter_kelly until validated
beatrelease-scanner    | llm_recommended     | Add Kelly-based sizing (separate concern, Phase 4)
market-maker           | fixed_mm            | Not applicable (Avellaneda-Stoikov, disabled)
position-monitor       | N/A (exit-only)     | Not applicable
```

**Key issue:** There is no `quarter_kelly_sell` function. The strategy trader uses `half_kelly_sell` for longshot bias selling. To meet SIZE-03, we either:
(a) Create a `quarter_kelly_sell` wrapper (like `quarter_kelly` wraps `half_kelly`), or
(b) Document that sell-side sizing is exempt from SIZE-03 because longshot sells have a different risk profile (the loss is bounded by 100-sell_price, which is high-probability profit)

**Recommendation:** Option (a) -- create `quarter_kelly_sell` for consistency. The math is identical: call `half_kelly_sell` and halve the result.

### Anti-Patterns to Avoid
- **Subtracting fee from edge instead of payout:** The old `edge_after_fees()` approach reduces the probability estimate, which is mathematically wrong. Fee should reduce the payout in the Kelly b-ratio. All bots have been fixed but the deprecated function remains.
- **Using total balance as bankroll:** When $80 of a $100 balance is locked in open positions, sizing based on $100 would yield 5x more contracts than the $20 actually available. The allocator already handles this correctly.
- **Mixing Kelly fractions within the same edge quality tier:** Weather-bot currently uses three different Kelly fractions (quarter, half, 60%) based on market type and conviction. SIZE-03 says default to quarter-Kelly, but `high_conviction_kelly` should be preserved for validated high-edge signals after model calibration proves them out.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Fee calculation | Per-bot fee logic | `kalshi_fee_cents(price)` from `probability.py` | Single formula `0.07 * P * (1-P) * 100`; consistent across all bots |
| Bankroll computation | Per-bot balance fetching | `allocator.request_budget()` -> `budget.bankroll_cents` | Caches balance, handles available vs total, enforces concentration limits |
| Kelly fraction | Per-bot Kelly math | `half_kelly()`, `quarter_kelly()`, `half_kelly_sell()` from `probability.py` | Fee-aware, bankroll-aware, handles edge cases (0 edge, boundary prices, etc.) |

**Key insight:** The codebase already centralizes all three concerns. The phase work is about auditing that all bots use the central functions correctly, not about building new infrastructure.

## Common Pitfalls

### Pitfall 1: Available Balance May Not Account for Open Position Exposure
**What goes wrong:** Kalshi's `available_balance` API field may only reflect funds locked by *pending orders* (unfilled limit orders), not by *open positions* (filled contracts still held). If a bot holds 50 contracts at 50c each ($25 at risk), that $25 may not be subtracted from `available_balance`.
**Why it happens:** Prediction market APIs often treat "available balance" as cash available for new orders, not as risk-adjusted capital.
**How to avoid:** Test the API response with open positions. If `available_balance` equals `balance` when holding positions but no pending orders, we need to subtract position exposure manually: `effective_bankroll = available_balance - sum(position * cost_basis)`.
**Warning signs:** `bankroll_used` in trade records is suspiciously close to total balance when the account has many open positions.

### Pitfall 2: Quarter-Kelly May Zero Out Low-Edge Trades
**What goes wrong:** `quarter_kelly` halves the already-halved Kelly fraction. For small edges (8-10%), this can produce 0 contracts because `int(...)` truncation rounds sub-1 values to zero.
**Why it happens:** Quarter-Kelly with a small bankroll and moderate edge yields fractional contracts. `int((f * bankroll) / price)` truncates.
**How to avoid:** The minimum order is 1 contract. After computing quarter-Kelly, if the result is 0 but edge exceeds the threshold, consider placing exactly 1 contract (minimum viable trade). This is already a risk for bracket markets; switching all bots to quarter-Kelly will make it more common.
**Warning signs:** Spike in `kelly_zero` skip decisions in decision logs after deploying the change.

### Pitfall 3: Sell-Side Kelly Has Different Risk Semantics
**What goes wrong:** Buy-side risk = `price_cents * count`. Sell-side risk = `(100 - sell_price) * count`. If you apply the same quarter-Kelly wrapper to sell-side, the exposure cap math needs to use the correct risk-per-contract.
**Why it happens:** `quarter_kelly` uses `half_kelly` internally, which uses `price_cents` as the risk per contract (correct for buy-side). `half_kelly_sell` uses `100 - sell_price` (correct for sell-side). The new `quarter_kelly_sell` must delegate to `half_kelly_sell`, not `half_kelly`.
**How to avoid:** Model `quarter_kelly_sell` exactly like `quarter_kelly` but delegating to `half_kelly_sell`.

### Pitfall 4: Weather Bot's Tiered Sizing Breaks With Blanket Quarter-Kelly
**What goes wrong:** Weather bot currently uses three tiers: quarter-Kelly for brackets, half-Kelly for standard threshold markets, and 60%-Kelly for high-conviction threshold-NO. Changing everything to quarter-Kelly would reduce capital deployment on the highest-ROI trades.
**Why it happens:** SIZE-03 says "default to quarter-Kelly until calibration-validated." If the weather model IS calibrated (calibration.json has per-city parameters), it should be exempt from the blanket downgrade.
**How to avoid:** Check `config/calibration.json` at sizing time. If the bot's model type has calibrated parameters, allow half-Kelly or higher. If not, enforce quarter-Kelly. This makes the sizing upgrade path clear: validate your model -> get half-Kelly -> validate more -> get 60%-Kelly.
**Warning signs:** Post-deployment, weather bot profit drops despite the same edge quality because it's sizing at 50% of previous levels.

## Code Examples

### Pattern 1: Creating quarter_kelly_sell (new function)
```python
# In probability.py, after quarter_kelly():

def quarter_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents=None,
                       max_exposure_cents=None, fee_cents=0, return_details=False):
    """Quarter-Kelly for sell-side trades (higher model uncertainty).

    Mirrors quarter_kelly but for selling YES (buying NO).
    Uses half_kelly_sell internally, then halves the result.

    max_exposure_cents: hard cap on total position risk.
                        Default scales with bankroll: max($5, 5% of bankroll).
    """
    if max_exposure_cents is None:
        max_exposure_cents = max(500, int((bankroll_cents or 10000) * 0.05))
    result = half_kelly_sell(edge, sell_price_cents, max_cost_cents, bankroll_cents,
                             fee_cents=fee_cents, return_details=True)
    contracts, _risk, details = result
    # Halve the half-Kelly position (= quarter-Kelly)
    contracts = contracts // 2
    details["kelly_fraction"] = details["kelly_fraction"] / 2
    # Hard-cap exposure
    risk_per = 100 - sell_price_cents
    if contracts * risk_per > max_exposure_cents:
        contracts = max_exposure_cents // risk_per
    risk = contracts * risk_per
    if return_details:
        return (contracts, risk, details)
    return (contracts, risk)
```

### Pattern 2: Calibration-Gated Sizing Upgrade
```python
# In a bot's sizing section, check if model is validated before upgrading from quarter-Kelly:

from probability import quarter_kelly, half_kelly, _load_calibration

def get_sizing_function(bot_type, market_type=None):
    """Return the appropriate Kelly sizing function based on calibration status.

    Default: quarter_kelly (SIZE-03 compliant).
    Upgrade to half_kelly only when calibration.json validates the model.
    """
    cal = _load_calibration()

    if bot_type == "weather" and cal.get("weather", {}).get("per_city"):
        # Weather model has per-city calibration -> validated, allow half-Kelly
        return half_kelly
    if bot_type == "crypto" and cal.get("crypto", {}).get("validated"):
        return half_kelly
    # ... other bot types

    return quarter_kelly  # default: conservative until proven
```

### Pattern 3: Fixing get_status() Bankroll Reporting
```python
# In capital_allocator.py, get_status():
def get_status(self):
    """Return current allocation status for logging/monitoring."""
    self._reset_daily_if_needed()
    total, available = self._get_balance()
    return {
        "bankroll_cents": available,        # FIX: was 'total', should be 'available'
        "total_balance_cents": total,       # Add total for reference
        "available_cents": available,
        "total_risk_today_cents": self._total_risk_cents,
        "portfolio_risk_limit_cents": int(available * PORTFOLIO_DAILY_LOSS_FRACTION) if available else 0,
        "tickers_traded_today": len(self._traded_tickers),
        "bot_spend": dict(self._bot_spend),
    }
```

### Pattern 4: Current Correct Fee Usage (reference)
```python
# This is the correct pattern already used by all active bots:
fee = kalshi_fee_cents(price)
count, risk, kelly_details = quarter_kelly(
    edge, price, budget.max_cost_cents,
    bankroll_cents=budget.bankroll_cents,
    fee_cents=fee,
    return_details=True,
)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `edge_after_fees()` subtracted fee from edge probability | `fee_cents` parameter reduces payout in Kelly formula | Phase 1 | Mathematically correct fee treatment; all bots migrated |
| `total_balance` for bankroll | `available_balance` for bankroll via allocator | Phase 1 | Prevents over-sizing when capital is locked in positions |
| No Kelly sizing (fixed contract counts) | Kelly-based sizing via probability.py | Pre-Phase 1 | Risk-proportional position sizing |

**Deprecated/outdated:**
- `edge_after_fees()`: Deprecated, kept for backward compat. Should be removed or hidden behind `_` prefix.

## Open Questions

1. **Does Kalshi `available_balance` subtract open position exposure?**
   - What we know: `available_balance` is returned by `/portfolio/balance`. The allocator uses it as bankroll. Tests mock it.
   - What's unclear: Whether `available_balance` = `balance - pending_orders_capital` or `balance - pending_orders_capital - open_positions_exposure`. This is critical for SIZE-01.
   - Recommendation: Test against live API with open positions. If it does NOT subtract position exposure, add manual computation: fetch positions, sum exposure, subtract from available_balance.

2. **Should beatrelease-scanner use Kelly sizing?**
   - What we know: It uses `sizing_method="llm_recommended"` with quantities from LLM. No Kelly math.
   - What's unclear: Whether SIZE-03 applies to LLM-recommended trades.
   - Recommendation: Out of scope for Phase 2. Beatrelease is a Phase 4 concern (EXEC-02). Document that it's excluded from SIZE-03 compliance.

3. **What constitutes "calibration-validated"?**
   - What we know: `config/calibration.json` stores per-city sigma parameters. Phase 1 built the calibration pipeline.
   - What's unclear: The exact threshold for when a model is "validated enough" to upgrade from quarter-Kelly to half-Kelly.
   - Recommendation: Define a simple rule: if `calibration.json` has parameters for the bot's market type AND Brier score < 0.25 (better than random), the model is validated. This can be a Phase 5 refinement.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest |
| Config file | `pytest.ini` (not present, uses defaults) |
| Quick run command | `pytest tests/test_kelly.py tests/test_allocator.py -x` |
| Full suite command | `pytest tests/ -q` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SIZE-01 | bankroll_cents uses available_balance, not total | unit | `pytest tests/test_allocator.py::TestBankrollUsesAvailable -x` | Exists |
| SIZE-01 | get_status() reports available as bankroll | unit | `pytest tests/test_allocator.py::TestBankrollStatusReporting -x` | Wave 0 |
| SIZE-02 | All bots pass fee_cents to Kelly (no edge_after_fees calls) | unit | `pytest tests/test_kelly.py::TestHalfKelly::test_fee_cents_reduces_contracts -x` | Exists |
| SIZE-02 | edge_after_fees deprecated or removed | unit | `pytest tests/test_probability.py -k fee` | Partial |
| SIZE-03 | Bots default to quarter_kelly | unit | `pytest tests/test_kelly.py::TestDefaultQuarterKelly -x` | Wave 0 |
| SIZE-03 | quarter_kelly_sell exists and works correctly | unit | `pytest tests/test_kelly.py::TestQuarterKellySell -x` | Wave 0 |
| SIZE-03 | Calibration-validated models can upgrade sizing | unit | `pytest tests/test_kelly.py::TestCalibrationGatedSizing -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_kelly.py tests/test_allocator.py tests/test_probability.py -x`
- **Per wave merge:** `pytest tests/ -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_kelly.py::TestQuarterKellySell` -- tests for new `quarter_kelly_sell` function
- [ ] `tests/test_kelly.py::TestDefaultQuarterKelly` -- tests verifying quarter-Kelly is default for uncalibrated bots
- [ ] `tests/test_allocator.py::TestBankrollStatusReporting` -- test that `get_status()` reports `available` not `total`

## Sources

### Primary (HIGH confidence)
- `src/kalshi/probability.py` -- All Kelly sizing functions, fee helpers (direct code inspection)
- `src/kalshi/capital_allocator.py` -- Bankroll computation, available_balance usage (direct code inspection)
- `src/kalshi/kalshi_auth.py:342-345` -- `get_balance()` returns `(balance, available_balance)` (direct code inspection)
- `tests/test_kelly.py` -- 24 existing tests for Kelly sizing (direct code inspection)
- `tests/test_allocator.py` -- 25 existing tests including `TestBankrollUsesAvailable` (direct code inspection)

### Secondary (MEDIUM confidence)
- `research/kalshi-deep-dive.md` -- Kalshi API endpoint documentation
- Bot source files: `weather-bot.py`, `entertainment-bot.py`, `source-monitor.py`, `economics-bot.py`, `crypto-bot.py`, `strategy-trader.py`, `cross-platform-arb.py`, `beatrelease-scanner.py` -- All sizing call sites audited

### Tertiary (LOW confidence)
- Kalshi API `available_balance` semantics -- Not verified against live API; assumed to subtract at least pending order capital

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - All code is internal, directly inspected
- Architecture: HIGH - Bankroll flow and fee flow fully traced through codebase
- Pitfalls: HIGH - Based on direct code analysis and existing test coverage

**Research date:** 2026-02-27
**Valid until:** 2026-03-27 (stable; internal codebase, no external dependency changes expected)
