# Phase 6: Crypto Validation - Research

**Researched:** 2026-02-28
**Domain:** Crypto model backtesting, volatility benchmarking, time-to-settlement validation
**Confidence:** HIGH

## Summary

This phase validates the existing crypto trading model (GBM/log-normal in `probability.py::crypto_price_probability`) against historical data. Three workstreams: (1) backtest the model against real Kalshi crypto markets replayed with historical BTC/ETH/SOL price data, producing Brier scores; (2) verify the `estimate_time_to_settlement()` logic against actual Kalshi market durations; (3) benchmark realized volatility computation against Deribit DVOL.

The codebase already has strong infrastructure for this: `scripts/backtest.py` computes Brier scores and calibration tables, `probability.py` has the `crypto_price_probability()` function, and the dashboard renders calibration curves via Chart.js. The main new work is building data fetching pipelines for historical Kalshi markets, Coinbase candles, and Deribit DVOL -- then a backtest harness that replays predictions against known outcomes.

**Primary recommendation:** Build a standalone `scripts/crypto-backtest.py` script that fetches and caches all three data sources, replays each historical Kalshi crypto market using the existing `crypto_price_probability()` model, and outputs Brier scores + calibration curves in the same format as `data/backtest-results.json`. Extend the existing backtest results file with a `crypto_validation` section rather than creating a separate output.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Source BTC/ETH/SOL 15-minute candle data from Coinbase API (same source the live bot uses for spot prices -- keeps backtest consistent with production)
- Backtest period: 90 days of recent data (~8,600 candles per asset)
- All three assets (BTC, ETH, SOL) must be backtested
- Replay actual Kalshi markets using the Kalshi Historical Data API (`GET /historical/markets/{ticker}/candlesticks`, `GET /historical/markets`) -- NOT synthetic markets
  - Reference: https://docs.kalshi.com/getting_started/historical_data
  - Note: Kalshi is migrating historical data off live API by March 6, 2026 -- use historical endpoints
- Include all crypto market durations (hourly, daily, weekly brackets)
- Store both raw per-market predictions (ticker, model prob, actual outcome, vol used, time-to-settlement) AND aggregate Brier scores
- Side-by-side time series comparison for vol: compute our realized vol at each point in time, fetch Deribit DVOL for same timestamps, plot together with rolling correlation and mean absolute error
- Pre-download and cache Deribit DVOL history to a local file (faster backtest runs, reproducible, no rate limits)
- Validate the 60% IV / 40% RV blend ratio -- test alternatives (50/50, 70/30, pure IV) and measure which gives the best Brier score
- Test alternative realized vol lookback windows (6h, 12h, 24h, 48h) and document the optimal window
- Exhaustive audit: pull ALL settled crypto markets from Kalshi historical API, compute actual duration (open_time to close_time) for every market
- Break down settlement time distributions by market type (hourly, daily, weekly)
- Flag anomalies: markets that closed early, were voided, or had unusual settlement patterns
- If the T=2456min assumption or `estimate_time_to_settlement()` logic is wrong, fix it in this phase (not just document)
- Brier score target: 0.20 or better (crypto is noisier than weather's ~0.15 target)
- Extend existing `data/backtest-results.json` with crypto validation results (alongside weather/other bots), plus human-readable stdout summary
- Generate calibration curve (reliability diagram) for the crypto model -- reuse Phase 1 charting infrastructure
- On validation failure (Brier > 0.20): document gaps and suggest specific parameter fixes, do NOT auto-disable the bot -- user decides

### Claude's Discretion
- Exact data fetching pagination strategy for Kalshi historical API
- Coinbase API candle request batching (300 per request limit)
- Chart styling and formatting details for calibration curves
- How to structure the optimization sweep (grid search vs sequential) for vol parameters

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| CRYP-01 | Crypto model backtested against historical BTC 15-min candles with documented Brier score | Coinbase Exchange API provides public 15-min candles (granularity=900), 300 per request. Kalshi Historical API provides settled crypto markets. Existing `backtest.py` Brier/calibration infrastructure reusable. |
| CRYP-02 | Time-to-settlement calculation validated (T=2456min claim checked against actual market durations) | Kalshi Historical API `GET /historical/markets` with status=settled and series_ticker filtering returns `close_time`/`open_time` fields for duration computation. |
| CRYP-03 | Realized vol computation validated against Deribit DVOL benchmark | Deribit public API `get_volatility_index_data` returns hourly DVOL candles. CryptoDataDownload provides daily CSV backup. Existing `compute_realized_vol()` output compared point-by-point. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| requests | (existing) | HTTP calls to Coinbase, Deribit, Kalshi APIs | Already used throughout project |
| math | (stdlib) | Log-normal CDF, vol computations | Already used in `probability.py` |
| json | (stdlib) | Data caching and results serialization | Project pattern |
| pathlib | (stdlib) | File path management | Project pattern |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| statistics | (stdlib) | Mean, stdev for vol sweep results | Summarizing parameter sweep |
| csv | (stdlib) | Reading Deribit DVOL CSV cache | Parsing downloaded DVOL data |
| datetime | (stdlib) | Timestamp handling across APIs | Market duration computation |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Raw requests | pandas + yfinance | Adds heavy dependency for simple candle fetching; project avoids pandas |
| Manual pagination | ccxt library | Heavyweight; project prefers lightweight HTTP with `retry_request()` |
| Custom CSV parser | pandas read_csv | Unnecessary dependency for simple DVOL cache file |

**Installation:**
No new dependencies required. Everything uses stdlib + existing `requests` library.

## Architecture Patterns

### Recommended Project Structure
```
scripts/
├── crypto-backtest.py           # Main backtest harness (new)
├── backtest.py                  # Existing — will be extended to include crypto results
data/
├── crypto-validation/           # Cached data directory (new, gitignored)
│   ├── coinbase-candles-BTC.json
│   ├── coinbase-candles-ETH.json
│   ├── coinbase-candles-SOL.json
│   ├── deribit-dvol-BTC.json
│   ├── deribit-dvol-ETH.json
│   ├── kalshi-crypto-markets.json
│   └── crypto-backtest-raw.json    # Per-market raw predictions
├── backtest-results.json        # Existing — extended with crypto_validation section
```

### Pattern 1: Data Fetch-and-Cache
**What:** Download data once, cache locally, replay from cache
**When to use:** All data fetching — Coinbase candles, Deribit DVOL, Kalshi historical markets
**Why:** User locked decision: "reproducible, cached data, no rate limits"
**Example:**
```python
CACHE_DIR = PROJECT_DIR / "data" / "crypto-validation"

def fetch_coinbase_candles(asset, granularity=900, days=90, force=False):
    """Fetch and cache 15-min candles from Coinbase Exchange API."""
    cache_path = CACHE_DIR / f"coinbase-candles-{asset}.json"
    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text())

    # Coinbase returns max 300 candles per request
    # 90 days * 96 candles/day = 8,640 candles → ~29 requests
    all_candles = []
    end = int(time.time())
    start = end - days * 86400
    batch_seconds = 300 * granularity  # 300 candles worth
    current = start
    while current < end:
        batch_end = min(current + batch_seconds, end)
        url = (f"https://api.exchange.coinbase.com/products/{asset}-USD/candles"
               f"?start={datetime.utcfromtimestamp(current).isoformat()}"
               f"&end={datetime.utcfromtimestamp(batch_end).isoformat()}"
               f"&granularity={granularity}")
        r = retry_request("GET", url, timeout=15)
        candles = r.json()
        all_candles.extend(candles)
        current = batch_end
        time.sleep(0.5)  # rate limit courtesy

    _atomic_write_json(cache_path, all_candles)
    return all_candles
```

### Pattern 2: Market Replay Backtest
**What:** For each settled Kalshi market, look up the spot price at the market's open time from cached candles, compute model probability, compare to actual outcome
**When to use:** Main backtesting loop
**Example:**
```python
def replay_market(market, candles_by_asset, dvol_cache, vol_config):
    """Replay a single historical market through the model."""
    ticker = market["ticker"]
    parsed = parse_crypto_ticker(ticker)
    asset = parsed["asset"]

    # Find spot price at market open time
    open_ts = parse_iso_ts(market["open_time"])
    spot = find_nearest_candle_close(candles_by_asset[asset], open_ts)

    # Compute actual time to settlement
    close_ts = parse_iso_ts(market["close_time"])
    actual_minutes = (close_ts - open_ts) / 60

    # Get vol at that point in time
    iv = find_dvol_at_time(dvol_cache.get(asset), open_ts)
    rv = compute_rv_at_time(candles_by_asset[asset], open_ts, lookback_hours=vol_config["rv_lookback_hours"])
    blended_vol = vol_config["iv_weight"] * iv + (1 - vol_config["iv_weight"]) * rv

    # Run model
    prob = crypto_price_probability(
        spot, parsed["threshold"], "above" if parsed["direction"] == "T" else "below",
        time_horizon_minutes=actual_minutes,
        realized_vol_pct=blended_vol,
    )

    # Determine actual outcome
    result = market.get("result", "")
    actual = 1 if result in ("yes", "all_yes") else 0

    return {
        "ticker": ticker, "asset": asset,
        "model_prob": prob, "actual": actual,
        "vol_used": blended_vol, "iv": iv, "rv": rv,
        "minutes_to_settle": actual_minutes,
        "spot_at_open": spot, "threshold": parsed["threshold"],
    }
```

### Pattern 3: Parameter Sweep with Brier Score Ranking
**What:** Grid search over vol blend ratios and RV lookback windows, rank by Brier score
**When to use:** Validating the 60/40 IV/RV blend and optimal lookback
**Example:**
```python
def vol_parameter_sweep(markets, candles, dvol_cache):
    """Sweep IV/RV blend ratio and RV lookback window."""
    configs = []
    for iv_weight in [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0]:
        for rv_hours in [6, 12, 24, 48]:
            vol_config = {"iv_weight": iv_weight, "rv_lookback_hours": rv_hours}
            predictions = [replay_market(m, candles, dvol_cache, vol_config)
                          for m in markets]
            bs = brier_score([(p["model_prob"], p["actual"]) for p in predictions])
            configs.append({"iv_weight": iv_weight, "rv_hours": rv_hours, "brier": bs, "n": len(predictions)})
    return sorted(configs, key=lambda x: x["brier"])
```

### Anti-Patterns to Avoid
- **Fetching data on every run:** Cache everything. The user explicitly requires cached, reproducible runs.
- **Using live Kalshi API for historical markets:** Kalshi is deprecating historical data on the live API by March 6. Use `/historical/markets` endpoints.
- **Synthetic markets:** User explicitly prohibited synthetic market generation. Only replay real settled Kalshi markets.
- **Auto-disabling bot on validation failure:** User explicitly said to document gaps and suggest fixes, not auto-disable.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Brier score computation | Custom scorer | Existing `backtest.brier_score()` | Already validated, tested in `test_backtest.py` |
| Calibration table/curve | Custom binning | Existing `backtest.calibration_table()` | Consistent with other bots' output |
| Crypto ticker parsing | Custom regex | Existing `ticker_utils.parse_crypto_ticker()` | Already handles KXBTC/KXETH/KXSOL/KXCRYPTO |
| HTTP retry logic | Custom retry loop | Existing `kalshi_auth.retry_request()` | Handles 429, timeouts, backoff |
| JSON file writes | Manual open/write | Existing `kalshi_auth._atomic_write_json()` | Crash-safe atomic writes |
| Calibration chart rendering | New chart library | Existing Chart.js infrastructure in dashboard | Phase 1 already built calibration curve rendering |

**Key insight:** The project already has 80% of the infrastructure needed. The new work is data fetching/caching and the backtest replay loop.

## Common Pitfalls

### Pitfall 1: Coinbase Candle API Pagination
**What goes wrong:** Request more than 300 candles and the API silently returns empty or rejects.
**Why it happens:** Coinbase enforces a strict 300-candle limit per request.
**How to avoid:** Paginate by time window. For 15-min candles, each request covers 300 * 15min = 4500min = 75 hours. 90 days = ~29 batched requests.
**Warning signs:** Empty responses, missing candle gaps in the middle of the dataset.

### Pitfall 2: Coinbase Candle Timestamp Format
**What goes wrong:** Candle data uses Unix timestamps (seconds), not ISO 8601. Start/end parameters may expect ISO 8601 depending on the API version.
**Why it happens:** Coinbase Exchange API and Advanced Trade API have different conventions.
**How to avoid:** Use the Exchange API (`api.exchange.coinbase.com`) which accepts ISO 8601 start/end params and returns Unix timestamp arrays. The response format is `[timestamp, low, high, open, close, volume]` (note: NOT OHLCV, it's TLHOVC).
**Warning signs:** Candles in wrong order, timestamps don't match expected range.

### Pitfall 3: Kalshi Historical API Cutoff Boundary
**What goes wrong:** Some markets are in the live API, some only in the historical API; query results are incomplete.
**Why it happens:** Kalshi partitions data at a moving cutoff timestamp. Markets settled before the cutoff are only in `/historical/markets`; newer ones are only in the live API.
**How to avoid:** Call `GET /historical/cutoff` first, then query both live and historical endpoints, merge results, deduplicate by ticker.
**Warning signs:** Missing markets, unexpectedly low market count, markets from recent weeks absent.

### Pitfall 4: Deribit DVOL Only Covers BTC and ETH
**What goes wrong:** Attempting to fetch DVOL for SOL returns no data.
**Why it happens:** Deribit only publishes DVOL for BTC and ETH. SOL options market is too thin for a meaningful vol index.
**How to avoid:** For SOL backtesting, use realized vol only (no IV benchmark). Document this limitation clearly.
**Warning signs:** Empty API responses for SOL DVOL requests.

### Pitfall 5: Realized Vol Computation Bias
**What goes wrong:** Computing realized vol from 5-min bot observations (as `compute_realized_vol()` does) gives different results than computing from 15-min candles.
**Why it happens:** Different sampling frequencies produce different vol estimates (volatility signature plot effect). Bot observations are irregularly spaced.
**How to avoid:** For backtest consistency, compute RV from the same 15-min candles used as price source. Use `normalized log returns -> annualize` (same method as existing `compute_realized_vol()` but with regular intervals).
**Warning signs:** Large discrepancy between backtest RV and live bot RV for the same time period.

### Pitfall 6: Market Result Field Interpretation
**What goes wrong:** Incorrectly determining whether a market settled YES or NO.
**Why it happens:** Kalshi historical markets have different result field values: `"yes"`, `"no"`, `"all_yes"`, `"all_no"`, or empty string for voided/cancelled.
**How to avoid:** Check `market["result"]` -- treat `"yes"` and `"all_yes"` as YES outcome, `"no"` and `"all_no"` as NO outcome, skip empty/voided.
**Warning signs:** Win rate significantly different from expected (should be roughly 40-60% for a calibrated model).

### Pitfall 7: Time-to-Settlement Includes Non-Trading Hours
**What goes wrong:** The `estimate_time_to_settlement()` function uses `close_time - now` which includes overnight/weekend minutes.
**Why it happens:** Kalshi crypto markets trade 24/7, but the GBM model's `T` parameter means "minutes of price movement." For crypto, trading hours = calendar hours, so this is actually correct.
**How to avoid:** No fix needed for crypto (unlike stocks). But document that the validation confirms crypto markets trade continuously.
**Warning signs:** This would only be a problem if Kalshi had non-trading windows for crypto -- which they don't.

## Code Examples

### Fetching Kalshi Historical Crypto Markets
```python
# Source: Kalshi API docs (https://docs.kalshi.com/getting_started/historical_data)
def fetch_kalshi_historical_crypto_markets(client, force=False):
    """Fetch all settled crypto markets from Kalshi Historical API."""
    cache_path = CACHE_DIR / "kalshi-crypto-markets.json"
    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text())

    all_markets = []
    # Fetch from historical endpoint
    cursor = None
    for _ in range(200):  # generous page limit
        path = "/historical/markets?status=settled&limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        batch = data.get("markets", [])
        for m in batch:
            ticker = m.get("ticker", "")
            if any(ticker.startswith(p) for p in ["KXBTC", "KXETH", "KXSOL", "KXCRYPTO"]):
                all_markets.append(m)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    # Also check live API for recently settled markets
    for prefix in ["KXBTC", "KXETH", "KXSOL", "KXCRYPTO"]:
        try:
            live = client.get_all_markets(prefix=prefix, status="settled")
            for m in live:
                if m["ticker"] not in {x["ticker"] for x in all_markets}:
                    all_markets.append(m)
        except Exception:
            pass

    _atomic_write_json(cache_path, all_markets)
    return all_markets
```

### Fetching Deribit DVOL History
```python
# Source: Deribit API (https://docs.deribit.com)
# Resolution values: 1, 60, 3600, 43200, "1D"
def fetch_deribit_dvol_history(asset, days=90, resolution=3600, force=False):
    """Fetch and cache Deribit DVOL hourly data."""
    cache_path = CACHE_DIR / f"deribit-dvol-{asset}.json"
    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text())

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000

    # Deribit does not document a strict per-request limit,
    # but chunk into 7-day windows for safety
    all_points = []
    chunk_ms = 7 * 86400 * 1000
    current = start_ms
    while current < end_ms:
        chunk_end = min(current + chunk_ms, end_ms)
        url = (
            f"https://deribit.com/api/v2/public/get_volatility_index_data"
            f"?currency={asset}&start_timestamp={current}"
            f"&end_timestamp={chunk_end}&resolution={resolution}"
        )
        r = retry_request("GET", url, timeout=15)
        data = r.json()
        points = data.get("result", {}).get("data", [])
        all_points.extend(points)
        current = chunk_end
        time.sleep(0.3)

    _atomic_write_json(cache_path, all_points)
    return all_points  # Each point: [timestamp_ms, open, high, low, close]
```

### Computing RV from Cached Candles at a Specific Time
```python
def compute_rv_at_time(candles, target_ts, lookback_hours=24):
    """Compute realized vol from candles ending at target_ts."""
    lookback_seconds = lookback_hours * 3600
    cutoff = target_ts - lookback_seconds
    # Filter candles in lookback window
    window = [(c[0], c[4]) for c in candles if cutoff <= c[0] <= target_ts]  # (ts, close)
    if len(window) < 5:
        return None
    # Log returns
    log_returns = [math.log(window[i][1] / window[i-1][1])
                   for i in range(1, len(window))
                   if window[i-1][1] > 0]
    if len(log_returns) < 3:
        return None
    # Annualize: 15-min intervals → 365.25 * 24 * 4 per year
    intervals_per_year = 365.25 * 24 * 4
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean)**2 for r in log_returns) / (len(log_returns) - 1)
    vol = math.sqrt(variance * intervals_per_year)
    return max(0.10, min(3.0, vol))
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Live API for all market data | Historical API for settled markets | March 2026 | Must use `/historical/` endpoints for 90-day lookback |
| Coinbase Pro API | Coinbase Exchange API | 2023 | URL is `api.exchange.coinbase.com`, same format |
| Single vol source | Blended IV/RV | Current codebase | 60% IV + 40% RV blend in `crypto-bot.py` |

**Deprecated/outdated:**
- Kalshi live API for historical data: Being removed March 6, 2026. Use `/historical/` endpoints.
- Coinbase Pro API domain (`api.pro.coinbase.com`): Redirects to `api.exchange.coinbase.com`.

## Open Questions

1. **Kalshi Historical API ticker prefix filtering**
   - What we know: The live `/markets` endpoint supports `tickers` parameter (comma-separated list) but NOT `ticker_prefix`. The existing `get_all_markets()` method fetches ALL markets and filters client-side.
   - What's unclear: Whether `/historical/markets` supports `series_ticker` or similar filtering that could reduce response size for crypto-only queries.
   - Recommendation: Use `series_ticker` filter if available (likely maps to the event series like "KXBTC"), otherwise fetch all and filter client-side. Test both approaches and use whichever is available.
   - Confidence: MEDIUM

2. **Deribit DVOL data availability for full 90-day window**
   - What we know: Deribit DVOL API provides historical data. The crypto-bot already fetches from this endpoint successfully (hourly resolution, last 2 hours).
   - What's unclear: Whether 90 days of hourly data (2,160 points) is available in a reasonable number of requests. The API docs don't clearly state maximum response sizes.
   - Recommendation: Chunk into 7-day windows (168 points each, ~13 requests). If that fails, fall back to daily resolution (90 points, 1 request).
   - Confidence: MEDIUM

3. **SOL market availability on Kalshi**
   - What we know: The crypto bot has `KXSOL` in its prefix list, and `parse_crypto_ticker()` handles SOL.
   - What's unclear: Whether Kalshi has actually listed and settled any SOL markets in the 90-day backtest window.
   - Recommendation: Attempt to fetch SOL markets. If none exist, document "no SOL markets found in backtest period" and proceed with BTC/ETH only.
   - Confidence: LOW

4. **Kalshi market `result` field values for crypto**
   - What we know: For weather, the result field is typically "yes" or "no". For crypto bracket/threshold, the values may differ.
   - What's unclear: Exact result field semantics for crypto threshold vs bracket markets.
   - Recommendation: Inspect the first batch of fetched historical crypto markets to determine result field patterns. Handle all observed values.
   - Confidence: MEDIUM

## Sources

### Primary (HIGH confidence)
- Kalshi Historical Data docs: https://docs.kalshi.com/getting_started/historical_data -- Historical API endpoints, cutoff system, migration timeline
- Coinbase Exchange API: https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles -- Candle endpoint, 300 limit, granularity values
- Existing codebase: `src/kalshi/crypto-bot.py`, `src/kalshi/probability.py`, `scripts/backtest.py` -- Current model implementation and backtest infrastructure

### Secondary (MEDIUM confidence)
- Deribit API `get_volatility_index_data`: https://docs.deribit.com/api-reference/upcoming/market-data/public-get_volatility_index_data -- Resolution values (1, 60, 3600, 43200, 1D), response format
- CryptoDataDownload Deribit DVOL: https://www.cryptodatadownload.com/data/deribit/ -- Daily DVOL CSV download as backup data source
- Kalshi market endpoint parameters: https://docs.kalshi.com/api-reference/market/get-markets -- Query params including status=settled, min_close_ts, cursor pagination

### Tertiary (LOW confidence)
- Coinbase public access claim: Multiple blog posts suggest Exchange API candles are publicly accessible without auth -- verify empirically during implementation

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Uses only existing project dependencies (requests, stdlib)
- Architecture: HIGH - Extends existing backtest patterns (`backtest.py`, `calibration_table()`, `brier_score()`)
- Data fetching: MEDIUM - API pagination and rate limits need empirical validation
- Pitfalls: HIGH - Well-understood from existing bot operation and API documentation

**Research date:** 2026-02-28
**Valid until:** 2026-03-15 (Kalshi historical API migration deadline is March 6 -- validate endpoint availability)
