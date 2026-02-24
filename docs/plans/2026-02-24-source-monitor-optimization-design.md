# Source Monitor Optimization Suite — Design Document

**Date**: 2026-02-24
**Scope**: 10 optimizations across data quality, probability models, trade logic, testing, and settlement feedback
**Environment**: Dual (demo dev + production Mac Mini) — all changes backward-compatible with safe defaults

---

## Context

The source monitor is the highest-priority bot (allocator priority 1.0) trading information arbitrage on 3 data sources: HDD album sales, box office revenue, and NWS actual temperatures. Deep analysis identified 10 optimization opportunities that collectively could yield +2-5% edge improvement while maintaining risk controls.

### Current Weaknesses

1. No data freshness validation for HDD/box office (NWS has 2-hour staleness check)
2. Static day-of-week sigma with no time decay — stale data gets same confidence as fresh
3. Zero retry logic on transient data source failures
4. No dedicated test coverage (zero test files for the most complex bot)
5. Hard-coded NWS edge thresholds (step function at hour 17)
6. Entertainment bot duplicates source-monitor's HDD/box office logic
7. No cross-market consistency checks
8. Conservative sizing even for near-certain info-arb trades
9. Fragile HTML regex scraping for box office data
10. No settlement feedback loop to auto-calibrate sigma parameters

---

## Section 1: Data Quality & Resilience

### 1a. Data Freshness Validation

**Problem**: HDD and box office data have no age check in source-monitor. Stale data gets the same sigma as fresh data.

**Design**:

In `source-monitor.py`, add freshness gating before trade evaluation:

- **HDD**: `get_album_sales()` returns `chart_date` field. Add check:
  - If `chart_date` parseable and data is >72 hours old for Mon/Tue (projections), increase sigma by 50%
  - If data is >24 hours old for Fri+ (actuals), increase sigma by 50%
  - If >168 hours (7 days), skip trade entirely (matches entertainment-bot's `MAX_DATA_AGE_HOURS`)
  - If `chart_date` missing or unparseable, fail-open (pass through, same as entertainment-bot pattern)

- **Box office**: The scraped data itself has no timestamp. Use day-of-week logic:
  - If current day is not in `activeDays` (already handled), skip
  - Add: if current day is Monday and gross data looks like last weekend's opening (heuristic: compare to previous snapshot if available), flag as stale
  - Simpler approach: pass `hours_since_publication` based on day-of-week to `boxoffice_data_sigma()`:
    - Friday/Saturday: 0 hours (fresh estimates)
    - Sunday: 24 hours (yesterday's estimates + today's actuals)
    - Monday: 48 hours (weekend wrap-up)

**Files**: `source-monitor.py` (add `_compute_data_age_hours()` helper, modify `check_hdd()` and `evaluate_album_trade()`)

### 1b. Retry Logic with Backoff

**Problem**: Single `try/except` per source in main loop. One transient failure skips source for full polling interval (10-30 minutes).

**Design**:

Add `_check_with_retry()` wrapper in `source-monitor.py`:

```python
def _check_with_retry(check_fn, source_name, prefetched, ss, max_retries=2):
    for attempt in range(max_retries + 1):
        try:
            check_fn(prefetched_markets=prefetched)
            health.record_source_success(source_name)
            if ss: ss.source_ok(source_name)
            return
        except Exception as e:
            if attempt < max_retries:
                log.warning(f"{source_name} attempt {attempt+1} failed: {e}, retrying in {2**attempt}s")
                time.sleep(2 ** attempt)
            else:
                log.error(f"{source_name} failed after {max_retries+1} attempts: {e}")
                health.record_source_error(source_name, str(e))
                if ss: ss.source_fail(source_name, str(e))
                traceback.print_exc()
```

Replace the 3 `try/except` blocks in main loop (lines 725-765) with calls to `_check_with_retry()`.

**Files**: `source-monitor.py` (main loop refactor only)

### 1c. Structured Box Office API (TMDb)

**Problem**: HTML regex scraping of The Numbers and Box Office Mojo breaks on layout changes.

**Design**:

Add TMDb API as primary box office data source:

- **Discovery**: `GET /movie/now_playing?api_key={key}&region=US` returns current movies with `id`, `title`, `revenue`
- **Detail**: `GET /movie/{id}?api_key={key}` returns full revenue data
- **API key**: Read from `TMDB_API_KEY` env var or `config/keys/tmdb.txt` (gitignored)
- **Fallback**: If TMDb unavailable or API key missing, fall back to existing HTML scraping
- **Integration point**: Replace `check_boxoffice()` internals with TMDb-first, HTML-fallback pattern

Config addition to `kalshi-monitor-config.json`:
```json
"boxoffice": {
  "enabled": true,
  "tmdbEnabled": true,
  "urls": ["https://www.boxofficemojo.com/", "https://www.the-numbers.com/"],
  ...
}
```

**Files**: `source-monitor.py` (new `_fetch_tmdb_boxoffice()` function), `config/kalshi-monitor-config.json`

---

## Section 2: Probability Model Improvements

### 2a. Time-Decay Sigma for Album/Box Office Data

**Problem**: Static sigma per day-of-week doesn't account for data aging. A 6-day-old Friday chart gets the same 5% sigma as a 1-hour-old chart.

**Design**:

Add optional `hours_since_publication` parameter to `album_data_sigma()` and `boxoffice_data_sigma()`:

```python
def album_data_sigma(day_of_week, hours_since_publication=0):
    """Day-dependent + time-decay uncertainty for album sales."""
    cal = _load_calibration()
    album_cal = cal.get("album_sales", {}).get("sigma_by_day", {})

    if day_of_week <= 1:
        base_sigma = album_cal.get("mon_tue", 0.15)
    elif day_of_week <= 3:
        base_sigma = album_cal.get("wed_thu", 0.10)
    else:
        base_sigma = album_cal.get("fri_sun", 0.05)

    # Time decay: data uncertainty grows 50% per 48 hours of staleness
    if hours_since_publication > 0:
        decay_factor = 1.0 + 0.5 * (hours_since_publication / 48.0)
        base_sigma *= min(decay_factor, 3.0)  # cap at 3x

    return base_sigma
```

Same pattern for `boxoffice_data_sigma(day_of_week, hours_since_publication=0)`.

**Backward compatibility**: Default `hours_since_publication=0` means existing callers (entertainment-bot, backtest) get identical behavior. Only source-monitor passes the new parameter.

**Files**: `probability.py` (modify `album_data_sigma`, `boxoffice_data_sigma`), `source-monitor.py` (pass age parameter)

### 2b. Dynamic NWS Edge Thresholds (CI-Based Gating)

**Problem**: Hard step function at hour 17 for edge thresholds. A 5 degree F margin at hour 10 is more informative than 0.5 degree F at hour 18, but current code doesn't capture this.

**Design**:

Extract sigma computation from `nws_probability()` into a standalone helper, then use it for margin-aware edge thresholds:

```python
def _nws_sigma_for_hour(hour_of_day):
    """Extract NWS sigma for a given hour (reusable outside probability calc)."""
    cal = _load_calibration()
    nws_section = cal.get("nws", {})
    nws_cal = nws_section.get("sigma_by_hour", {})

    if nws_cal:
        # Legacy step-function
        if hour_of_day < 6:
            return max(0.5, nws_cal.get("overnight", 5.0))
        elif hour_of_day >= 17:
            return max(0.5, nws_cal.get("17+", 0.5))
        elif hour_of_day >= 15:
            return max(0.5, nws_cal.get("15-16", 1.5))
        else:
            return max(0.5, nws_cal.get("before_15", 3.0))
    else:
        if hour_of_day < 6:
            return max(0.5, 5.0)
        else:
            return max(0.5, 4.0 * math.exp(-0.18 * (hour_of_day - 6)))
```

In `source-monitor.py`, replace the hour-17 step with CI-based gating:

```python
sigma = _nws_sigma_for_hour(now.hour)
margin = abs(running_high - threshold)
ci_99 = 2.576 * sigma  # 99% CI half-width

if margin > ci_99:
    min_edge = 0.05   # Very confident
elif margin > ci_99 * 0.5:
    min_edge = 0.10   # Moderate
elif is_bracket:
    min_edge = 0.20   # Bracket always needs higher threshold
else:
    min_edge = 0.15   # Uncertain threshold
```

Export `_nws_sigma_for_hour` from `probability.py` as `nws_sigma_for_hour()`.

**Files**: `probability.py` (extract + export sigma helper, refactor `nws_probability` to use it), `source-monitor.py` (replace edge threshold logic)

---

## Section 3: Trade Logic Improvements

### 3a. Entertainment Bot Consolidation

**Problem**: Entertainment bot duplicates source-monitor's HDD/box office logic with different parameters (3% vs 5% edge, no staleness check in source-monitor, lower allocator priority 0.8).

**Design**:

1. **Migrate** entertainment-bot's `_check_hdd_staleness()` into source-monitor (already covered by 1a above)
2. **Disable** entertainment-bot in supervisor config: set `"enabled": false` in `config/bots-config.json` under `entertainment`
3. **Keep** the entertainment-bot file intact for rollback
4. **Lower** source-monitor's album min_edge from 5% to 4% when sigma <= 5% (split the difference between entertainment's 3% and current 5%)
5. **Update** source-monitor's `maxDailyTrades` from 20 to 25 (absorb entertainment-bot's volume)

**Files**: `config/bots-config.json` (disable entertainment), `source-monitor.py` (adjust min_edge), `config/kalshi-monitor-config.json` (increase daily trades)

### 3b. Cross-Market Consistency Validation

**Problem**: Multiple markets on the same entity (same artist at different thresholds) evaluated independently. No check for contradictory signals.

**Design**:

Add `_validate_market_cluster()` in `source-monitor.py`:

```python
def _validate_market_cluster(entity_key, market_signals):
    """Check that markets for the same entity have monotonically decreasing
    probabilities as thresholds increase.

    entity_key: identifier (artist name, movie title, city+date)
    market_signals: list of (market, threshold, probability) tuples

    Returns filtered list of consistent (market, threshold, prob) tuples,
    or empty list if signals are contradictory.
    """
    if len(market_signals) < 2:
        return market_signals

    sorted_signals = sorted(market_signals, key=lambda s: s[1])  # sort by threshold

    for i in range(len(sorted_signals) - 1):
        lower_prob = sorted_signals[i][2]
        higher_prob = sorted_signals[i + 1][2]
        if lower_prob < higher_prob - 0.05:  # 5% tolerance for noise
            log.warning(
                f"Inconsistent {entity_key}: P(>{sorted_signals[i][1]})={lower_prob:.0%} "
                f"< P(>{sorted_signals[i+1][1]})={higher_prob:.0%} — skipping all"
            )
            return []

    return sorted_signals
```

Integration points:
- **HDD**: Group markets by artist name before evaluating. Run consistency check on the group.
- **Box office**: Group markets by movie title before evaluating. Run consistency check.
- **NWS**: Group by city+date. Threshold markets should have monotonically decreasing P(>T) as T increases.

**Files**: `source-monitor.py` (add validation, modify `check_hdd`, `match_boxoffice_to_markets`, `match_nws_to_markets`)

### 3c. High-Confidence Sizing Increase

**Problem**: Current allocator high-confidence gate (`confidence > 0.90 and edge > 0.15`) is conservative for info-arb trades where we have direct settlement data.

**Design**:

Add `source_type` parameter to `PortfolioAllocator.request_budget()`:

```python
def request_budget(self, bot_name, ticker, edge=0, confidence=0, source_type=None):
    ...
    # Adjusted high-confidence gate for info-arb
    if source_type == "info_arb":
        high_conf_threshold = 0.85
        high_edge_threshold = 0.10
    else:
        high_conf_threshold = 0.90
        high_edge_threshold = 0.15

    if confidence > high_conf_threshold and edge > high_edge_threshold:
        high_conf_max = int(available_balance * 0.25)
        ...
```

Source-monitor passes `source_type="info_arb"` for HDD and box office trades. NWS trades pass `source_type="nws"` (uses default thresholds since it's model-based, not direct observation). Other bots don't pass `source_type` — no change in behavior.

**Files**: `capital_allocator.py` (add `source_type` parameter), `source-monitor.py` (pass source_type in budget requests)

---

## Section 4: Testing & Settlement Feedback

### 4a. Source Monitor Test Suite

**Problem**: Zero dedicated test coverage for the most complex, highest-priority bot.

**Design**:

Create `tests/test_source_monitor.py` with the following test groups:

| Test Group | Functions Tested | Test Cases |
|---|---|---|
| `TestHDDMatching` | `check_hdd`, album regex | Artist name matching (exact, partial, false positive), threshold parse from title vs ticker, K/thousand/units suffix handling |
| `TestBoxOfficeMatching` | `match_boxoffice_to_markets` | Movie title word matching, $50M/$1B threshold parsing, multi-word title, short-word filtering |
| `TestNWSMatching` | `match_nws_to_markets` | City+date filter, timezone-aware today check, running_high propagation, stale observation rejection |
| `TestEdgeThresholds` | Edge gating logic | 5% vs 10% sigma-based threshold, CI-based NWS thresholds (margin > CI, margin ~ CI/2, margin < CI/2) |
| `TestDataFreshness` | `_compute_data_age_hours` | Fresh data passthrough, 72h+ sigma increase, 168h+ rejection, missing chart_date fail-open |
| `TestCrossMarketConsistency` | `_validate_market_cluster` | Monotonic pass, non-monotonic rejection, single-market passthrough, tolerance boundary |
| `TestRetryLogic` | `_check_with_retry` | Success on first try, success on retry, failure after max retries |
| `TestTimDecaySigma` | `album_data_sigma`, `boxoffice_data_sigma` | Default (0 hours), 48h decay, 96h decay, cap at 3x |

Test pattern: stub `kalshi_auth` and `capital_allocator` in `sys.modules`, use `importlib.util.spec_from_file_location` for hyphenated filename. Mock `KalshiClient`, `TradeManager`, `PortfolioAllocator`.

**Files**: `tests/test_source_monitor.py` (new)

### 4b. Settlement Feedback Loop (Auto-Calibration)

**Problem**: `calibration.json` has 0 matched trades for all source-monitor categories. No mechanism to learn from settlement outcomes.

**Design**:

Extend `scripts/calibrate-sigma.py` with info-arb calibration:

1. **Data collection**: After `reconcile-trades.py` annotates trades with `settlement_result`, the calibrator reads `data/kalshi-monitor-trades.json`

2. **Bucketing**: Group settled trades by:
   - `source_type` (album, boxoffice, nws)
   - Day-of-week bucket (mon_tue, wed_thu, fri_sun) for albums/box office
   - Hour bucket (overnight, before_15, 15-16, 17+) for NWS

3. **Sigma optimization**: Per bucket, if 10+ settled trades:
   - Compute Brier score with current sigma
   - Grid search sigma in [0.01, 0.50] (step 0.01) to minimize Brier
   - Apply shrinkage toward default: `weight = n_trades / (n_trades + 15)`
   - `calibrated_sigma = weight * grid_best + (1 - weight) * default_sigma`

4. **Write to calibration.json**:
   ```json
   {
     "album_sales": {
       "n": 25,
       "sigma_by_day": { "mon_tue": 0.13, "wed_thu": 0.08, "fri_sun": 0.04 },
       "brier": 0.12
     },
     "box_office": { ... },
     "nws": { ... }
   }
   ```

5. **Guard rails**:
   - Minimum 10 trades per bucket (below this, use defaults)
   - Bayesian shrinkage prevents overfitting on small samples
   - Log old vs new sigma + Brier improvement for audit trail
   - Never auto-apply without human review (calibrate is run manually via `npm run calibrate`)

**Files**: `scripts/calibrate-sigma.py` (extend with info-arb sections), `scripts/reconcile-trades.py` (ensure `source_type` is preserved in annotations)

---

## Implementation Order

Changes ordered by dependency (earlier changes are prerequisites for later ones):

1. **probability.py**: Time-decay sigma functions, extract `nws_sigma_for_hour()` helper
2. **capital_allocator.py**: Add `source_type` parameter to `request_budget()`
3. **source-monitor.py**: Data freshness, retry logic, CI-based NWS thresholds, consistency validation, entertainment consolidation, high-confidence sizing, TMDb integration
4. **config/kalshi-monitor-config.json**: Increase daily trades, add TMDb config
5. **config/bots-config.json**: Disable entertainment bot
6. **tests/test_source_monitor.py**: Full test suite
7. **scripts/calibrate-sigma.py**: Settlement feedback loop
8. **scripts/reconcile-trades.py**: Ensure source_type preservation

---

## Risk Assessment

| Change | Risk | Mitigation |
|---|---|---|
| Time-decay sigma | Low | Default `hours_since_publication=0` preserves existing behavior |
| NWS CI-based thresholds | Medium | May fire earlier than current hour-17 gate; monitor first week's trade volume |
| Entertainment bot disable | Low | File kept intact; re-enable in supervisor config if needed |
| TMDb integration | Low | Falls back to HTML scraping if API key missing or TMDb down |
| High-confidence sizing | Medium | Only affects trades with >85% confidence AND >10% edge; city limits still hard caps |
| Cross-market consistency | Low | Conservative (skips trades on inconsistency); fail-open if single market |
| Retry logic | None | Only adds resilience; no change to trade logic |
| Settlement feedback | None | Manual calibration run; never auto-applied |

---

## Success Criteria

1. **Data quality**: Zero trades on data older than 168 hours
2. **Resilience**: Transient failures recovered within 4 seconds (2 retries)
3. **Model accuracy**: Brier score improvement trackable after 50+ settlements
4. **Test coverage**: 30+ test cases covering all source-monitor code paths
5. **Trade volume**: Source-monitor absorbs entertainment-bot's market coverage with no missed opportunities
6. **Sizing**: High-confidence info-arb trades sized 2-3x larger than current (within risk limits)
