---
phase: quick-8
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/economics-bot.py
  - src/kalshi/probability.py
  - tests/test_economics.py
  - tests/test_probability.py
autonomous: true
requirements: [IMP-1, IMP-2]
must_haves:
  truths:
    - "cpi_nowcast_sigma can accept an optional fed_ci_width parameter to use dynamic sigma from Cleveland Fed cross-measure dispersion"
    - "Every economics bot trade record includes nowcast_age_hours and data_source_timestamp fields"
    - "Sigma derived from cross-measure dispersion is used when multiple Fed measures are available"
    - "Stale cache age is visible in trade records for post-hoc audit"
  artifacts:
    - path: "src/kalshi/probability.py"
      provides: "cpi_nowcast_sigma with optional fed_ci_width override"
      contains: "fed_ci_width"
    - path: "src/kalshi/economics-bot.py"
      provides: "Cross-measure dispersion calculation and nowcast age tracking in trade records"
      contains: "nowcast_age_hours"
    - path: "tests/test_economics.py"
      provides: "Tests for CI extraction and age tracking"
    - path: "tests/test_probability.py"
      provides: "Tests for fed_ci_width sigma override"
  key_links:
    - from: "src/kalshi/economics-bot.py"
      to: "src/kalshi/probability.py"
      via: "cpi_nowcast_sigma(days, fed_ci_width=computed_width)"
      pattern: "cpi_nowcast_sigma.*fed_ci_width"
    - from: "src/kalshi/economics-bot.py"
      to: "trade_manager.place_order"
      via: "nowcast_age_hours kwarg"
      pattern: "nowcast_age_hours"
---

<objective>
Implement two unfinished economics bot improvements: (1) dynamic sigma from Cleveland Fed data dispersion, and (2) nowcast age tracking in trade records.

Purpose: IMP-1 eliminates hardcoded sigma by deriving uncertainty from the spread across CPI/Core CPI/PCE/Core PCE measures the Fed publishes. IMP-2 enables post-hoc auditing of data freshness per trade.

IMPORTANT CONTEXT: The Cleveland Fed page does NOT publish confidence intervals or quartile estimates (their FAQ says it is on their "to-do list"). Instead of scraping CIs, IMP-1 will compute cross-measure dispersion from the 4 measures already scraped (CPI, Core CPI, PCE, Core PCE) as a proxy for model uncertainty. When 3+ measures are available, their standard deviation provides a dynamic sigma floor that auto-calibrates.

Output: Updated probability.py with dynamic sigma, updated economics-bot.py with dispersion calculation and age tracking, tests for both.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@src/kalshi/economics-bot.py
@src/kalshi/probability.py
@tests/test_economics.py
@tests/test_probability.py

<interfaces>
From src/kalshi/probability.py (lines 891-912):
```python
def cpi_nowcast_sigma(days_to_release):
    """Exponential decay for CPI nowcast uncertainty based on time to release.
    Returns sigma in percentage points (e.g. 0.10 = 0.10%).
    """
    cal = _load_calibration()
    cpi_cal = cal.get("cpi", {}).get("sigma_by_days", {})
    if cpi_cal:
        key = str(min(14, max(0, days_to_release)))
        if key in cpi_cal:
            return cpi_cal[key]
    d = max(0, days_to_release)
    return 0.05 + 0.35 * (1 - math.exp(-0.05 * d))
```

From src/kalshi/economics-bot.py (nowcast cache):
```python
def _save_nowcast_cache(data):
    _atomic_write_json(NOWCAST_CACHE_PATH, {"cached_at": time.time(), "data": data})

def _nowcast_cache_age_hours():
    cache = json.loads(NOWCAST_CACHE_PATH.read_text())
    return (time.time() - cache.get("cached_at", 0)) / 3600
```

From src/kalshi/economics-bot.py (trade placement, lines 1069-1089):
```python
result = trade_manager.place_order(ticker, side, price, count, reasoning,
    edge=round(edge, 4),
    sigma_used=round(opp.get("sigma", 0), 4),
    nowcast_value=opp.get("nowcast_value"),
    days_to_release=opp.get("days_to_release"),
    market_type=_classify_econ_market(ticker),
    ...)
```

From src/kalshi/economics-bot.py (opportunity dicts, lines 865-895):
```python
opportunities.append({
    "ticker": ticker, "market": m, "side": "yes",
    "prob": prob, "edge": edge, "threshold": threshold,
    "nowcast_value": fused_nowcast, "sigma": posterior_sigma,
    "days_to_release": days_to_release, "raw_nowcast": nowcast_value,
    ...
})
```
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Add fed_ci_width parameter to cpi_nowcast_sigma and cross-measure dispersion in economics bot</name>
  <files>
    src/kalshi/probability.py
    src/kalshi/economics-bot.py
    tests/test_probability.py
    tests/test_economics.py
  </files>
  <behavior>
    - cpi_nowcast_sigma(7) without fed_ci_width returns same value as before (backward compatible)
    - cpi_nowcast_sigma(30, fed_ci_width=0.50) uses fed_ci_width/3.29 as sigma (90% CI -> sigma) instead of hardcoded formula, but still applies min with days-based formula to prevent sigma being TOO narrow
    - cpi_nowcast_sigma(0, fed_ci_width=0.30) at release day, dynamic sigma = 0.30/3.29 ~ 0.091, but days-based floor of 0.05 applies, so returns max(0.05, 0.091) = 0.091
    - When fed_ci_width is None or 0, falls back to existing exponential decay formula
    - Cross-measure dispersion: std([2.83, 3.14, 2.51]) for CPI/CoreCPI/PCE returns ~0.26
    - Dispersion stored in nowcast dict under key "cross_measure_dispersion"
    - Dispersion converted to fed_ci_width by multiplying by 1.645 (to approximate 90% CI from 1-sigma dispersion)
  </behavior>
  <action>
    1. In `probability.py`, add optional `fed_ci_width=None` parameter to `cpi_nowcast_sigma()`:
       - If `fed_ci_width` is provided and > 0, compute `dynamic_sigma = fed_ci_width / 3.29` (90% CI = 3.29 sigma for normal)
       - Return `max(dynamic_sigma, 0.03)` as a floor to prevent unreasonably tight sigma
       - Still check calibration.json override first (existing behavior unchanged)
       - If `fed_ci_width` is None/0, use existing exponential decay formula

    2. In `economics-bot.py`, add a `_compute_cross_measure_dispersion(nowcast)` function:
       - Takes the nowcast dict (has keys like cpi_yoy, core_cpi_yoy, pce_yoy, core_pce_yoy)
       - Collects all available YoY values into a list
       - If 3+ values available, compute standard deviation as dispersion
       - If 2 values, use abs(diff)/2 as rough dispersion
       - If <2 values, return None
       - Convert dispersion to CI width: `dispersion * 1.645` (1-sigma to 90% CI approximation)
       - Store result in nowcast dict under "cross_measure_dispersion" key

    3. In the scan loop (around line 819 where `sigma = cpi_nowcast_sigma(days_to_release)` is called), pass the computed dispersion as `fed_ci_width`:
       ```python
       dispersion_ci = nowcast.get("cross_measure_dispersion")
       sigma = cpi_nowcast_sigma(days_to_release, fed_ci_width=dispersion_ci)
       ```

    4. Add tests in `test_probability.py`:
       - `test_fed_ci_width_overrides_hardcoded`: cpi_nowcast_sigma(30, fed_ci_width=0.50) should return 0.50/3.29 ~ 0.152
       - `test_fed_ci_width_none_uses_default`: cpi_nowcast_sigma(30) same as before
       - `test_fed_ci_width_zero_uses_default`: cpi_nowcast_sigma(30, fed_ci_width=0) same as before
       - `test_fed_ci_width_floor`: cpi_nowcast_sigma(0, fed_ci_width=0.05) should return max(0.05/3.29, 0.03) = 0.03

    5. Add tests in `test_economics.py`:
       - `test_cross_measure_dispersion_three_measures`: with cpi=2.83, core_cpi=3.14, pce=2.51 -> dispersion ~0.26, CI ~0.42
       - `test_cross_measure_dispersion_two_measures`: with cpi=2.8, core_cpi=3.1 -> dispersion = 0.15, CI ~0.25
       - `test_cross_measure_dispersion_one_measure`: returns None
       - `test_cross_measure_dispersion_stored_in_nowcast`: after calling the function, nowcast dict has "cross_measure_dispersion" key
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_probability.py -k "fed_ci_width" -v && pytest tests/test_economics.py -k "dispersion" -v</automated>
  </verify>
  <done>
    - cpi_nowcast_sigma accepts optional fed_ci_width and uses it to derive dynamic sigma
    - Cross-measure dispersion computed from available Fed measures
    - All existing cpi_nowcast_sigma tests still pass (backward compatible)
    - New tests for both features pass
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Add nowcast age tracking to every trade record</name>
  <files>
    src/kalshi/economics-bot.py
    tests/test_economics.py
  </files>
  <behavior>
    - Every opportunity dict includes "nowcast_age_hours" (float, rounded to 1 decimal)
    - Every opportunity dict includes "data_source_timestamp" (ISO 8601 string from cache's cached_at)
    - place_order call passes nowcast_age_hours and data_source_timestamp as extra kwargs
    - Gas market opportunities also include these fields (gas data age approximated as 0.0 since fetched live each scan)
    - When nowcast comes from cache, age reflects actual staleness
    - When nowcast is freshly fetched, age is ~0.0
  </behavior>
  <action>
    1. Add a helper function `_nowcast_source_info()` that returns a dict with:
       - `nowcast_age_hours`: float from `_nowcast_cache_age_hours()`, rounded to 1 decimal
       - `data_source_timestamp`: ISO 8601 string converted from the cache's `cached_at` Unix timestamp
       - Returns `{"nowcast_age_hours": 0.0, "data_source_timestamp": datetime.datetime.utcnow().isoformat() + "Z"}` if cache does not exist

    2. In `fetch_cleveland_fed_nowcast()`, after `_save_nowcast_cache(nowcast)` succeeds (line 410), the cache timestamp is fresh. No changes needed here since `_nowcast_source_info()` reads from the cache file.

    3. In the scan loop, call `source_info = _nowcast_source_info()` once before the market iteration loop, then inject into every opportunity dict:
       ```python
       opportunities.append({
           ...existing fields...,
           "nowcast_age_hours": source_info["nowcast_age_hours"],
           "data_source_timestamp": source_info["data_source_timestamp"],
       })
       ```

    4. In the place_order call (lines 1069-1089), add the two new kwargs:
       ```python
       nowcast_age_hours=opp.get("nowcast_age_hours"),
       data_source_timestamp=opp.get("data_source_timestamp"),
       ```

    5. For gas market opportunities (around lines 928-942), inject age fields too:
       ```python
       "nowcast_age_hours": 0.0,  # gas prices fetched live each scan
       "data_source_timestamp": datetime.datetime.utcnow().isoformat() + "Z",
       ```
       And pass them in the gas market place_order calls (around lines 950-960).

    6. For FedWatch/CME opportunities (around lines 970-982), inject age fields similarly with `0.0` age since CME data is fetched live.

    7. Add tests in `test_economics.py`:
       - `test_nowcast_source_info_returns_age`: mock cache file with known timestamp, verify age calculation
       - `test_nowcast_source_info_returns_iso_timestamp`: verify ISO 8601 format
       - `test_nowcast_source_info_missing_cache`: returns age 0.0 and current timestamp
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_economics.py -k "nowcast_source" -v && pytest tests/test_economics.py -v</automated>
  </verify>
  <done>
    - Every economics bot trade record includes nowcast_age_hours (float) and data_source_timestamp (ISO string)
    - Gas and FedWatch opportunities also have these fields
    - Post-hoc audit can determine whether a trade used fresh or stale data
    - All existing economics bot tests still pass
  </done>
</task>

</tasks>

<verification>
```bash
# All economics and probability tests pass
cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_economics.py tests/test_probability.py -v

# Full test suite still passes (no regressions)
pytest tests/ --timeout=60 -x -q
```
</verification>

<success_criteria>
- cpi_nowcast_sigma accepts fed_ci_width parameter; dynamic sigma derived from cross-measure dispersion when 3+ Fed measures available
- All trade records from economics bot include nowcast_age_hours and data_source_timestamp
- Existing behavior unchanged when no cross-measure data available (backward compatible)
- All existing tests pass, new tests cover both features
</success_criteria>

<output>
After completion, create `.planning/quick/8-implement-economics-bot-unfinished-impro/8-SUMMARY.md`
</output>
