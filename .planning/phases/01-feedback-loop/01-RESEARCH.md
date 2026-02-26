# Phase 1: Feedback Loop - Research

**Researched:** 2026-02-26
**Domain:** Settlement reconciliation, Brier score computation, calibration curves, P&L analytics, sigma calibration
**Confidence:** HIGH

## Summary

Phase 1 builds the measurement foundation for the entire trading system. The good news is that substantial infrastructure already exists: `reconcile-trades.py`, `backfill-settlements.py`, `backtest.py`, `calibrate-sigma.py`, and `analyze-performance.py` are all functional scripts with correct core logic. However, they have critical gaps that prevent the feedback loop from actually closing: `config/calibration.json` contains zero calibrated parameters (all `"n": 0`), the backtest produces no per-market-type Brier breakdowns, there is no calibration curve visualization, P&L tracking lacks fee-adjusted and time-windowed views, and the dashboard has no endpoints for Brier scores or calibration data.

The work is enhancement and gap-filling, not greenfield. Every script needs targeted improvements: reconciliation needs to handle ALL trade files consistently (some are missing from certain scripts' TRADE_FILES lists), backtest needs per-market-type Brier scores and calibration curve output, performance analysis needs daily/weekly/cumulative P&L with rolling Sharpe, and calibrate-sigma needs to actually produce non-empty results (which requires reconciled settlement data first -- creating a strict ordering dependency).

**Primary recommendation:** Execute in strict sequence: reconcile first (FEED-01/02), then backtest/calibration-curve/P&L (FEED-03/04/05) in parallel, then sigma calibration last (FEED-06) since it depends on reconciled data.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Reconcile ALL historical trades -- maximum data for calibration, not just recent
- Idempotent design -- safe to re-run daily without duplicating results
- Multi-objective optimization: Brier score as primary target, realized P&L as tiebreaker
- Storage: Both JSON files in data/ (persistent, S3-syncable) AND dashboard API endpoints (live view)
- Brier score breakdowns: ALL levels -- per bot, per market type, per city (weather), AND aggregate
- Report format: Both CLI table (quick terminal checks) AND dashboard page (visual deep-dives with charts)
- P&L method: Both realized (settlement-only) AND unrealized (mark-to-market) shown separately
- Fee handling: Show both gross P&L and net-of-fees P&L side by side
- Sharpe ratio: Both rolling 30-day AND all-time, shown side by side
- P&L granularity: Daily, weekly, AND all-time cumulative per bot

### Claude's Discretion
- Reconciliation idempotency implementation (overwrite vs upsert pattern)
- Missing settlement data handling strategy
- Backfill rate limiting (1 req/sec vs 1 req/3sec based on API docs)
- Calibration minimum sample size threshold
- Sparse data fallback method (pooled global vs nearest-climate)
- Grid search vs Optuna decision (based on actual runtime)
- Calibration curve bin count (based on sample size)
- Dashboard chart library and visualization approach

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| FEED-01 | Settlement reconciliation pipeline annotates all trade logs with `settlement_result` field from Kalshi API | Existing `reconcile-trades.py` handles this; needs TRADE_FILES list consistency fix and revenue computation improvement |
| FEED-02 | Backfill script queries individual market endpoints for trades missing settlement data | Existing `backfill-settlements.py` works; needs rate limiting tuning and integration with reconcile flow |
| FEED-03 | Brier score computation produces non-null scores per bot and per market type | Existing `backtest.py` has `brier_score()` and per-bot breakdown; needs per-market-type and per-city breakdowns added |
| FEED-04 | Calibration curve (reliability diagram) shows binned predicted-vs-actual for each probability model | Existing `calibration_table()` in backtest.py computes bins; needs per-model output, JSON persistence, and dashboard visualization |
| FEED-05 | Per-bot P&L tracking computes realized P&L, win rate, and Sharpe ratio from settled trades | Existing `analyze-performance.py` has reconciliation with Sharpe; needs daily/weekly/cumulative, rolling Sharpe, fee separation, and per-bot granularity |
| FEED-06 | Per-city sigma calibration populates `config/calibration.json` with optimized parameters | Existing `calibrate-sigma.py` has full grid search with Bayesian shrinkage; currently produces `"n": 0` because no trades are reconciled yet -- depends on FEED-01/02 completing first |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python 3 (stdlib) | 3.11+ | All scripts | Project standard, no scipy dependency |
| `math.erf` | stdlib | Normal CDF for probability models | Already used throughout `probability.py` |
| `json` | stdlib | Trade log I/O, calibration persistence | Project pattern: atomic JSON writes |
| `collections.defaultdict` | stdlib | Aggregation and bucketing | Used in all analytics scripts |
| `pathlib.Path` | stdlib | File path handling | Project convention |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| FastAPI | existing | Dashboard API endpoints | Adding /api/backtest, /api/calibration-curve endpoints |
| `_atomic_write_json` | kalshi_auth | Safe file writes | All JSON persistence (trade logs, metrics, calibration) |
| `requests` | existing | Kalshi API calls via KalshiClient | Settlement/fill fetching |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Grid search | Optuna/TPE | Optuna is v2 requirement (INFRA-03); grid search runs in seconds for current data volume -- keep grid search |
| matplotlib for charts | HTML/JS in dashboard | Dashboard already uses vanilla JS/HTML; add Chart.js via CDN for calibration curves -- no Python plotting dependency needed |
| scipy for statistics | math stdlib | Project explicitly avoids scipy; `math.erf` and hand-rolled Student-t CDF already sufficient |

## Architecture Patterns

### Existing Script Architecture (DO NOT CHANGE)
```
scripts/
├── reconcile-trades.py      # FEED-01: Annotate trade logs with settlement_result
├── backfill-settlements.py  # FEED-02: Query /markets/{ticker} for unsettled trades
├── backtest.py              # FEED-03/04: Brier scores, calibration tables
├── calibrate-sigma.py       # FEED-06: Grid search sigma -> calibration.json
├── analyze-performance.py   # FEED-05: P&L, win rate, Sharpe
├── dashboard.py             # Dashboard with API endpoints
└── dashboard.html           # Frontend UI
```

### Pattern 1: Reconciliation Pipeline (Sequential)
**What:** `npm run reconcile` then `npm run backfill` then `npm run backtest`
**When to use:** Daily automated pipeline (Phase 5 will automate this)
**Key insight:** reconcile uses `/portfolio/settlements` (bulk, fast), backfill uses `/markets/{ticker}` (per-ticker, slow). Run reconcile first to minimize backfill work.

### Pattern 2: Idempotent Annotation (Existing)
**What:** Skip records where `settlement_result is not None`
**When to use:** All reconciliation/backfill operations
**Implementation:** Already in place in both scripts -- the upsert pattern is correct. Do NOT change to overwrite.
```python
# Existing pattern in reconcile-trades.py line 119
if trade.get("settlement_result") is not None:
    return False  # Skip already-annotated records
```

### Pattern 3: Trade File Lists (CRITICAL CONSISTENCY ISSUE)
**What:** Each script defines its own `TRADE_FILES` list, but they are inconsistent.
**Current state:**
- `reconcile-trades.py`: 8 files (hardcoded list, missing arb and mm)
- `backfill-settlements.py`: Uses glob + hardcoded (inconsistent, may double-count)
- `backtest.py`: 4 files only (weather, strategy, crypto, beatrelease)
- `calibrate-sigma.py`: 7 files (missing position, arb, mm)
- `analyze-performance.py`: 10 files (most complete)
- `dashboard.py`: 10 files (matches analyze-performance)

**Fix:** Consolidate to a single canonical TRADE_FILES definition. Either create a shared module or use the dashboard.py/analyze-performance.py list as the standard.

### Pattern 4: Metrics JSON Persistence
**What:** Save computed metrics to `data/` as JSON for S3 sync
**Files to create/update:**
- `data/backtest-results.json` -- already exists, needs per-market-type Brier breakdown
- `data/calibration-curves.json` -- NEW: binned predicted-vs-actual per model
- `data/performance-metrics.json` -- NEW: daily/weekly/cumulative P&L, rolling Sharpe, per-bot
**S3 sync:** Add new files to `scripts/s3-sync.sh` whitelist

### Anti-Patterns to Avoid
- **Duplicating settlement fetch logic:** Four scripts independently implement `fetch_settlements()` with identical pagination. Refactor to shared utility.
- **Mixing gross and net P&L:** `backfill-settlements.py` computes `settlement_revenue_cents` as `(100 - price) * count` which is gross. Dashboard computes `profit = revenue - yes_cost - no_cost` which includes fees. Keep both but label clearly.
- **Overwriting calibration.json without backup:** The calibrate script writes directly. Add a backup or diff-report before overwriting.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Calibration curve visualization | Custom canvas drawing | Chart.js via CDN in dashboard.html | Standard charting, scatter/line chart with diagonal reference line |
| Sharpe ratio computation | New Sharpe function | Existing logic in `analyze-performance.py` lines 549-556 | Already correct: `(mean / std) * sqrt(252)` with sample variance |
| Settlement pagination | New pagination logic | Existing `_fetch_all_settlements()` pattern | Already handles cursor-based pagination correctly |
| Atomic JSON writes | Manual file operations | `_atomic_write_json()` from `kalshi_auth` | Handles write-then-rename atomicity, prevents corruption |
| Weather ticker parsing | New regex | Existing `parse_weather_ticker()` in backtest.py and `ticker_utils.py` | Already handles KXHIGH format correctly |

**Key insight:** Almost all the building blocks exist. The work is connecting them, filling gaps in breakdowns, and adding dashboard endpoints -- not building new algorithms.

## Common Pitfalls

### Pitfall 1: Empty Calibration Due to Missing Reconciliation
**What goes wrong:** `calibrate-sigma.py` produces `"n": 0` for all categories because no trades have `settlement_result` annotations.
**Why it happens:** Calibration reads trade logs and matches against the settlement API, but the matching logic requires trades to have been reconciled first (to know the outcome). Currently `config/calibration.json` shows `"n": 0` for everything.
**How to avoid:** Run `npm run reconcile` and `npm run backfill` BEFORE `npm run calibrate`. Make this ordering explicit in documentation and the future automation pipeline.
**Warning signs:** calibration.json with all `"n": 0` sections.

### Pitfall 2: TRADE_FILES Inconsistency
**What goes wrong:** A bot's trades get counted in performance analysis but not in Brier score computation, or vice versa.
**Why it happens:** Each script maintains its own TRADE_FILES list, and they have drifted out of sync.
**How to avoid:** Create a single canonical trade files definition shared across all scripts.
**Warning signs:** Different trade counts between `npm run backtest` and `npm run reconcile`.

### Pitfall 3: Settlement Revenue vs Profit Confusion
**What goes wrong:** P&L numbers don't match between scripts because some use gross revenue and others use net-of-fees profit.
**Why it happens:** Kalshi API returns `revenue` (gross payout) and separately `fee_cost`. Some scripts use revenue directly, others subtract costs.
**How to avoid:** Always compute and display both. Label as "Gross P&L" and "Net P&L" explicitly. The dashboard already does this correctly (line 677: `profit = revenue - yes_cost - no_cost`).
**Warning signs:** P&L totals differ between `npm run backtest` and dashboard `/api/settlements`.

### Pitfall 4: Brier Score With Insufficient Data
**What goes wrong:** Brier scores computed from <30 trades appear meaningful but are statistically unreliable.
**Why it happens:** Some bots or market types may have very few settled trades.
**How to avoid:** Already partially handled -- backtest.py warns at <30 trades (line 569). Extend this: report sample size alongside every Brier score, flag scores with <30 observations.
**Warning signs:** Brier score reported without sample size context.

### Pitfall 5: Calibrate-sigma Grid Search Using Log Loss Not Brier
**What goes wrong:** The variable is named `best_brier` but actually optimizes `log_loss` (line 214). This is intentional (log loss is better for Kelly-based trading) but confusing.
**Why it happens:** Variable naming inconsistency from an earlier refactor.
**How to avoid:** Rename variables for clarity. Also add Brier score as a secondary metric in the report (user wants multi-objective: Brier primary, P&L tiebreaker).
**Warning signs:** None operationally, but confusing for maintenance.

### Pitfall 6: Rate Limiting on Backfill
**What goes wrong:** Backfill queries individual `/markets/{ticker}` endpoints and can hit rate limits with many unsettled tickers.
**Why it happens:** Current rate limiting is weak: `time.sleep(0.5)` every 20 requests = ~40 req/sec peak.
**How to avoid:** Kalshi API rate limit is 10 req/sec. Use `time.sleep(0.1)` per request as a safe baseline. The existing code at line 103 sleeps 0.5s every 20 queries which averages ~2.5 req/sec -- this is actually fine. Keep it.
**Warning signs:** 429 errors in backfill output.

## Code Examples

### Existing Brier Score (from backtest.py)
```python
# Source: scripts/backtest.py lines 119-126
def brier_score(predictions):
    """Compute Brier score from list of (predicted_prob, actual_outcome) tuples.
    Returns None if empty. Lower is better (0=perfect, 0.25=random, 1=worst).
    """
    if not predictions:
        return None
    return sum((p - a) ** 2 for p, a in predictions) / len(predictions)
```

### Existing Calibration Table (from backtest.py)
```python
# Source: scripts/backtest.py lines 128-165
def calibration_table(predictions, n_bins=5):
    """Bin predictions and compute calibration statistics.
    Returns list of dicts: {bin_label, n, predicted_avg, actual_avg, gap}
    """
    # ... binning logic already correct, just needs n_bins parameter exposed
```

### Existing Sharpe Ratio (from analyze-performance.py)
```python
# Source: scripts/analyze-performance.py lines 549-556
if len(daily_pnl) >= 2:
    pnl_values = list(daily_pnl.values())
    mean_pnl = sum(pnl_values) / len(pnl_values)
    variance = sum((v - mean_pnl) ** 2 for v in pnl_values) / (len(pnl_values) - 1)
    std_pnl = math.sqrt(variance)
    if std_pnl > 0:
        sharpe = round((mean_pnl / std_pnl) * math.sqrt(252), 4)
```

### Pattern for Adding Dashboard Endpoint
```python
# Source: scripts/dashboard.py pattern from /api/settlements
@app.get("/api/backtest")
async def api_backtest():
    """Serve backtest results from data/backtest-results.json."""
    path = DATA_DIR / "backtest-results.json"
    if not path.exists():
        return {"error": "Run npm run backtest --save first"}
    data = json.loads(path.read_text())
    return data
```

### Pattern for Per-Market-Type Brier Breakdown (NEW)
```python
# Group evaluated trades by market type, compute per-type Brier
by_market_type = defaultdict(list)
for t in all_evaluated:
    ticker = t.get("ticker", "")
    if ticker.startswith("KXHIGH"):
        by_market_type["weather"].append((t["predicted"], t["actual"]))
    elif ticker.startswith(("KXBTC", "KXETH")):
        by_market_type["crypto"].append((t["predicted"], t["actual"]))
    # ... etc

per_type_brier = {
    mtype: {"brier": brier_score(preds), "n": len(preds)}
    for mtype, preds in by_market_type.items()
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Hardcoded sigma defaults | Grid search + Bayesian shrinkage | Already in calibrate-sigma.py | Calibration exists but has no data (n=0) |
| Gaussian CDF | Student-t CDF (df=6) | Already in probability.py | Fat tails for forecast errors already modeled |
| Single-model weather | Ensemble (GFS/ECMWF/ICON) BMA | Already in probability.py | Ensemble weights calibration already in calibrate-sigma.py |
| Manual P&L tracking | Automated reconciliation | Already in reconcile-trades.py | Scripts exist but haven't been run against production data |

**Key realization:** The codebase is architecturally sound. The problem is operational -- the pipeline hasn't been run end-to-end to populate the data that downstream scripts need. Phase 1 is about making the pipeline actually work and adding the missing analytics dimensions.

## Open Questions

1. **How many settled trades exist in the Kalshi API?**
   - What we know: `config/calibration.json` was generated on 2026-02-20 and found 44 settlements but 0 matched weather trades. This suggests trades exist but the matching logic may need debugging.
   - What's unclear: Whether the 44 settlements correspond to trades in the local trade logs, or if the trade logs are empty/on a different machine.
   - Recommendation: Run `npm run sync:down` first to pull production trade logs from S3, then run reconciliation. The planner should include a "sync data" step as prerequisite.

2. **Chart.js in dashboard -- CDN or bundled?**
   - What we know: Dashboard is a single `dashboard.html` file served by FastAPI. No build system.
   - What's unclear: Whether CDN access is reliable on the Mac Mini production server.
   - Recommendation: Use Chart.js from CDN (`https://cdn.jsdelivr.net/npm/chart.js`) with a `<script>` tag. If offline needed, download and serve from `scripts/static/`.

3. **Calibration curve bin count**
   - What we know: Current `calibration_table()` uses 5 bins. User wants 10 or 20 based on sample size.
   - Recommendation: Use 10 bins as default. If any bin has <5 observations, fall back to 5 bins. This is a simple conditional.

4. **Rolling 30-day Sharpe requires daily P&L history**
   - What we know: Current Sharpe uses all settlements grouped by date. Rolling requires maintaining a time series.
   - Recommendation: Persist daily P&L in `data/performance-metrics.json` as `{"daily_pnl": {"2026-02-20": 150, ...}}`. Compute rolling 30-day window from this.

## Discretion Recommendations

Based on research, here are my recommendations for Claude's Discretion items:

| Item | Recommendation | Rationale |
|------|---------------|-----------|
| Idempotency implementation | Keep existing upsert (skip if `settlement_result is not None`) | Already correct, battle-tested pattern |
| Missing settlement data | Mark as `"settlement_result": null` (current behavior) -- don't infer from price | Inferring from final price introduces false precision; explicit null is honest |
| Backfill rate limiting | Keep current 0.5s sleep per 20 requests (~2.5 req/sec) | Well within Kalshi's 10 req/sec limit, safe margin |
| Minimum sample size | 30 for global, 10 for per-city with Bayesian shrinkage | Standard statistical practice; calibrate-sigma.py already uses 5 for per-city which is too low |
| Sparse data fallback | Bayesian shrinkage toward global (already implemented) | `calibrate-sigma.py` already does this with SHRINKAGE_K=15; better than nearest-climate which requires geographic data |
| Grid search vs Optuna | Keep grid search | Runs in seconds with current data volume; Optuna is explicitly deferred to INFRA-03 |
| Calibration curve bins | 10 bins default, fall back to 5 if any bin has <5 observations | Balances resolution with statistical reliability |
| Dashboard chart library | Chart.js via CDN | Lightweight, no build step needed, well-documented, perfect for scatter/line calibration curves |

## Sources

### Primary (HIGH confidence)
- Existing codebase: `scripts/reconcile-trades.py`, `scripts/backfill-settlements.py`, `scripts/backtest.py`, `scripts/calibrate-sigma.py`, `scripts/analyze-performance.py`, `scripts/dashboard.py`
- Existing codebase: `src/kalshi/probability.py`, `src/kalshi/kalshi_auth.py`
- Existing codebase: `config/calibration.json` (current state: all n=0)
- Project documentation: `CLAUDE.md` (architecture, conventions, npm scripts)

### Secondary (MEDIUM confidence)
- Kalshi API: 10 req/sec rate limit (from codebase comments and retry logic in kalshi_auth.py)
- Brier score best practices: minimum 30 observations for reliable point estimate (standard statistical guidance)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all tools already in the codebase, no new dependencies needed
- Architecture: HIGH -- scripts exist and are well-structured, work is enhancement not creation
- Pitfalls: HIGH -- identified from direct code analysis of inconsistencies between scripts
- Calibration: HIGH -- `calibrate-sigma.py` is 725 lines of working grid search code, just needs data

**Research date:** 2026-02-26
**Valid until:** 2026-03-26 (stable -- internal codebase, no external API changes expected)
