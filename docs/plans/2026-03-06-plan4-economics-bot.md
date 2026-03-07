# Plan 4: Economics Bot — Full Quant Desk Review

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Per CLAUDE.md, you may ONLY modify: `economics-bot.py`, `scenario_engine.py`, `cpi_belief_filter.py`, `macro_engine.py`, `test_economics.py`, `test_econ*.py`, `test_scenario*.py`. Do NOT touch probability.py, kalshi_auth.py, or other bots.

**Goal:** Economics bot has $0 settled P&L but 115,914 contracts open (~$2,322 in correlated CPI exposure). Fix concentration risk prevention, GDP sigma (via shared infra), belief filter tuning, and duplicate data fetching. Target: +$20-30/day with <$500 max single-market-type exposure.

**Architecture:** CPI/GDP/Jobs nowcast trading using Cleveland Fed + macro engine. Bayesian belief fusion via CPIBeliefFilter. Scenario engine for multi-outcome analysis.

**Tech Stack:** Python 3, FRED API, Cleveland Fed API, Truflation, DeepSeek LLM

---

## 1. Data Acquisition

### Task 4.1: Fix Duplicate FRED API Calls

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Context:** The bot makes duplicate FRED API calls — fetching the same series multiple times per scan. This wastes rate limit budget and slows scans.

**Step 1: Identify duplicate calls**

Read economics-bot.py and grep for `fred` or `FRED` calls. Map which series are fetched and where.

**Step 2: Implement a per-scan FRED cache**

```python
class FREDCache:
    def __init__(self):
        self._cache = {}

    def get(self, series_id):
        if series_id not in self._cache:
            self._cache[series_id] = fetch_fred_series(series_id)
        return self._cache[series_id]

    def clear(self):
        self._cache = {}
```

Use at scan start, clear at scan end.

**Step 3: Write tests and commit**

### Task 4.2: Add Data Source Redundancy (Recommendation D)

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Context:** Cleveland Fed CPI nowcast is the primary signal. Add Truflation real-time CPI and TIPS breakevens as cross-validation sources.

**Step 1: Implement multi-source nowcast**

When multiple sources agree, increase confidence (and Kelly). When they disagree, reduce position size.

```python
def get_cpi_nowcast():
    sources = {}
    try:
        sources["cleveland_fed"] = fetch_cleveland_fed()
    except: pass
    try:
        sources["truflation"] = fetch_truflation()
    except: pass
    try:
        sources["tips_breakeven"] = fetch_tips_breakeven()
    except: pass

    if len(sources) >= 2:
        agreement = check_agreement(sources)
        return combine_nowcasts(sources), agreement
    elif len(sources) == 1:
        return list(sources.values())[0], "single_source"
    else:
        raise RuntimeError("No CPI nowcast sources available")
```

**Step 2: Write tests and commit**

---

## 2. Signal & Model Quality

### Task 4.3: Fix TIPS Breakeven Mixing in Belief Filter

**Files:**
- Modify: `src/kalshi/cpi_belief_filter.py`
- Test: `tests/test_econ*.py`

**Context:** TIPS breakeven data is being mixed into the CPIBeliefFilter directly. TIPS reflect MARKET expectations (which include risk premium), not ACTUAL CPI forecasts. This contaminates the belief filter with market noise.

**Step 1: Audit belief filter inputs**

Read cpi_belief_filter.py and trace what data enters the filter. Identify where TIPS data enters.

**Step 2: Separate TIPS into a distinct signal channel**

TIPS should inform a market sentiment signal (for position sizing), not the CPI point estimate. Use it as a cross-check: if our nowcast diverges significantly from TIPS-implied, either we're wrong or the market is mispricing (edge opportunity).

**Step 3: Write tests and commit**

### Task 4.4: Fix Bayesian Fusion Source Independence Assumption

**Files:**
- Modify: `src/kalshi/cpi_belief_filter.py`
- Test: `tests/test_econ*.py`

**Context:** The belief filter multiplies likelihoods from different sources, assuming independence. But Cleveland Fed and Truflation are correlated (both measure CPI). This overcounts evidence and makes the filter overconfident.

**Step 1: Write test showing overconfidence**

```python
def test_correlated_sources_not_overconfident():
    """Two correlated sources should not halve the posterior uncertainty."""
    filter = CPIBeliefFilter()
    filter.update(source="cleveland_fed", value=0.3, sigma=0.05)
    sigma_after_one = filter.posterior_sigma

    filter.update(source="truflation", value=0.31, sigma=0.05)
    sigma_after_two = filter.posterior_sigma

    # With independent sources, sigma would halve. With correlated, it should only shrink ~30%
    ratio = sigma_after_two / sigma_after_one
    assert ratio > 0.5, f"Two correlated sources halved sigma to {ratio:.2f} — treating as independent"
```

**Step 2: Add correlation discount**

```python
SOURCE_CORRELATIONS = {
    ("cleveland_fed", "truflation"): 0.7,
    ("cleveland_fed", "tips_breakeven"): 0.4,
    ("truflation", "tips_breakeven"): 0.3,
}

def correlated_update(self, source, value, sigma):
    """Discount new evidence by correlation with existing sources."""
    max_corr = max(
        SOURCE_CORRELATIONS.get((s, source), SOURCE_CORRELATIONS.get((source, s), 0))
        for s in self.sources_used
    ) if self.sources_used else 0
    effective_sigma = sigma / (1 - max_corr + 0.01)  # Widen sigma for correlated sources
    self._raw_update(value, effective_sigma)
    self.sources_used.append(source)
```

**Step 3: Run tests and commit**

---

## 3. Edge & Sizing

### Task 4.5: Implement Concentration Prevention

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Context:** CRITICAL — 115,914 CPI contracts ($2,322 exposure). Need to prevent FUTURE concentration, NOT cut existing positions.

**Step 1: Add per-market-type exposure tracking**

```python
def get_current_exposure(client, market_type="CPI"):
    """Sum current exposure for a market type."""
    positions = client.get("/portfolio/positions")
    cpi_exposure = sum(
        abs(p["market_exposure"]) for p in positions
        if market_type.upper() in p.get("ticker", "").upper()
    )
    return cpi_exposure
```

**Step 2: Add pre-trade concentration check**

```python
MAX_TYPE_EXPOSURE_CENTS = 50000  # $500 max per market type

def should_trade(client, market_type, proposed_cost):
    current = get_current_exposure(client, market_type)
    if current + proposed_cost > MAX_TYPE_EXPOSURE_CENTS:
        log.warning(f"Concentration limit: {market_type} at ${current/100:.0f}, proposed +${proposed_cost/100:.0f} would exceed ${MAX_TYPE_EXPOSURE_CENTS/100}")
        return False
    return True
```

**Step 3: Write tests and commit**

### Task 4.6: Improve Kelly Sizing for Economics

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Context:** Economics markets settle infrequently (monthly CPI, quarterly GDP). Position sizing needs to account for capital being locked up longer.

**Step 1: Add time-to-settlement adjustment**

```python
def time_adjusted_kelly(base_kelly, days_to_settlement):
    """Reduce Kelly for longer lockup periods (opportunity cost)."""
    if days_to_settlement > 14:
        return base_kelly * 0.5  # Half Kelly for >2 week lockup
    elif days_to_settlement > 7:
        return base_kelly * 0.75
    return base_kelly
```

**Step 2: Write tests and commit**

---

## 4. Execution

### Task 4.7: Add Execution Logging

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Step 1: Log decision details per market evaluated**

Every CPI/GDP/Jobs market scanned should produce a decision record:

```python
decision = {
    "ticker": ticker,
    "market_type": "CPI",  # or GDP, JOBS
    "nowcast_value": nowcast,
    "nowcast_sources": sources_used,
    "model_probability": prob,
    "market_price_cents": price,
    "edge": edge,
    "action": "trade" or "skip",
    "skip_reason": reason if skipped,
    "current_type_exposure": current_cpi_exposure,
    "belief_filter_sigma": filter.posterior_sigma,
}
```

**Step 2: Write tests and commit**

---

## 5. Exit Management

### Task 4.8: Add Data-Driven Exit Triggers

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Context:** Economics positions can be held for weeks. If new data significantly changes the nowcast, the bot should flag positions for review.

**Step 1: Track entry nowcast vs current nowcast**

```python
def check_for_exit_signals(positions, current_nowcast):
    for pos in positions:
        entry_nowcast = pos.get("entry_nowcast")
        if entry_nowcast and abs(current_nowcast - entry_nowcast) > 0.1:
            log.warning(f"Nowcast shift: {pos['ticker']} entry={entry_nowcast:.2f} now={current_nowcast:.2f}")
            # Flag for position-monitor to handle exit
```

**Step 2: Write tests and commit**

---

## 6. Risk Controls

### Task 4.9: Add Correlation-Aware Sizing

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Context:** Multiple CPI bracket markets are highly correlated. If we have 10 CPI positions all betting "above 0.3%", they're effectively one big bet.

**Step 1: Detect correlated positions**

```python
def group_correlated_positions(positions):
    """Group positions by market type and direction."""
    groups = defaultdict(list)
    for pos in positions:
        key = (pos["market_type"], pos["direction"])
        groups[key].append(pos)
    return groups
```

**Step 2: Scale Kelly by sqrt(n) for correlated positions**

```python
def correlation_adjusted_kelly(base_kelly, n_correlated):
    """Reduce per-position Kelly when holding n correlated positions."""
    if n_correlated <= 1:
        return base_kelly
    return base_kelly / math.sqrt(n_correlated)
```

**Step 3: Write tests and commit**

---

## 7. Measurement Framework

### Task 4.10: Fix Stale Heartbeat

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Test: `tests/test_economics.py`

**Context (from Plan 0 Task 0.4):** Economics bot heartbeat shows Feb 28 despite the process restarting on Mar 7. The bot's `HealthCheckMonitor` call may be missing or only executing at the start of the first scan. The 6-hour scan interval means heartbeats can appear stale, but Feb 28 → Mar 7 (7 days) indicates the heartbeat update is broken or only fires once.

**Step 1: Verify heartbeat is called on every scan cycle, not just startup**

Read economics-bot.py main loop and ensure `health.beat("economics")` (or equivalent) runs at the start of each scan.

**Step 2: If missing, add heartbeat call at start of scan loop**

**Step 3: Write test and commit**

---

### Task 4.11: Create Economics Bot Metrics

**Files:**
- Modify: `src/kalshi/economics-bot.py`
- Output: `data/economics-metrics.json`

**Step 1: Log per-scan metrics**

```python
scan_metrics = {
    "timestamp": datetime.utcnow().isoformat(),
    "cpi_nowcast": cpi_value,
    "gdp_nowcast": gdp_value,
    "sources_used": sources,
    "belief_filter_sigma": sigma,
    "markets_scanned": len(markets),
    "trades_placed": len(trades),
    "total_cpi_exposure": cpi_exposure,
    "total_gdp_exposure": gdp_exposure,
    "concentration_blocks": concentration_block_count
}
```

**Step 2: Write tests and commit**

---

## Measurement Protocol

| Metric | Before | Target | Method |
|--------|--------|--------|--------|
| CPI exposure | $2,322 (115K contracts) | <$500 new | Position check |
| GDP position sizes | Oversized (tight sigma) | Correct (Plan 1) | Backtest |
| FRED API calls/scan | Duplicated | Cached (1 per series) | Log count |
| Belief filter overconfidence | Correlated treated as independent | Correlation discount | Unit test |
| Source agreement tracking | None | Per-scan logged | Metrics file |
| Daily P&L | $0 (no settlements yet) | +$20-30/day | Settlement analysis |
| Heartbeat freshness | Stale (Feb 28 despite running Mar 7) | Fresh (<6h scan interval) | health-state.json |
| Cleveland Fed freshness | Last success Feb 28 | Fresh per scan | health-state.json |

---

## Execution Report (2026-03-07)

**Status:** Complete

**Tasks completed:** 5/5

**Summary:** GDP sigma verified, belief filter correlation-aware, scenario engine weighted std, FRED cache halves API calls, concentration corrected to 15%/40%.

**Backtest results (post-implementation):**
- Economics Brier: N/A (0 settlements yet)
- No settled economics markets to evaluate model quality
- Concentration limit corrected from uncapped to 15%/40% prevents repeat of 115K CPI contract overexposure

**Next steps:** Monitor first CPI/GDP/Jobs settlements to establish baseline Brier score.
