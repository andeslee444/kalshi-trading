# Technology Stack

**Project:** Kalshi Quant Trading System -- Stack Improvements
**Researched:** 2026-02-26
**Mode:** Ecosystem (brownfield -- what to add, not what to replace)

## Context

This is NOT a greenfield stack recommendation. The system runs Python 3, uses `requests` for HTTP, `json` for serialization, hand-rolled grid search for calibration, flat JSON files for state, and REST polling for market data. All of these work. The question is: **what additions provide measurable edge** in trade execution speed, model calibration quality, and operational reliability?

The "no scipy" constraint in PROJECT.md applies to the core `probability.py` hot path. Calibration scripts (offline, run manually) are not constrained by this -- they can use heavier libraries.

---

## Recommended Additions

### 1. Real-Time Data Pipeline (Speed Edge)

The primary alpha thesis is "get data before the market prices it in." The current system polls REST APIs on 5-30 minute intervals. Adding WebSocket support for Kalshi and async HTTP for data sources is the single highest-impact infrastructure change.

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `websockets` | 16.0 | Kalshi WebSocket client for orderbook, fills, ticker | Kalshi offers real-time WebSocket channels (orderbook_delta, fill, ticker, trade). Currently polling REST every 5-30 min means missing fast-moving price changes. WebSocket gives sub-second fill notifications and price updates. | HIGH |
| `httpx` | 0.28+ | Async HTTP client (replaces `requests` for data fetching) | Supports both sync and async in one library. HTTP/2 support. Connection pooling. Drop-in replacement for `requests` API. Switching from `requests` to `httpx.AsyncClient` enables concurrent data fetches (NWS + Open-Meteo + Coinbase simultaneously) instead of sequential. | HIGH |
| `asyncio` | stdlib | Event loop for concurrent operations | Already in Python stdlib. Needed for `websockets` and `httpx` async. No new dependency. | HIGH |

**Why not `aiohttp`?** aiohttp is 2x faster than httpx in raw async benchmarks, but httpx provides both sync and async APIs, HTTP/2 support, and a `requests`-compatible interface. For a brownfield migration where bots currently use `requests`, httpx is the pragmatic choice -- you can migrate incrementally (sync first, async later) without rewriting everything at once. aiohttp is async-only, requiring a full rewrite of every HTTP call.

**Why not `aiokalshi`?** The asyncio-native Kalshi client from the-odds-company. Its WebSocket support is still in development, and the project is early-stage. Better to use the `websockets` library directly with Kalshi's documented WebSocket protocol, which has official Python examples. Revisit aiokalshi when it reaches 1.0.

**Why not `predmarket` or `pmxt`?** Unified prediction market SDKs that wrap Kalshi + Polymarket. Both are under rapid development (pre-1.0). The existing `KalshiClient` in `kalshi_auth.py` is battle-tested with retry, circuit breaker, and caching. Adding an abstraction layer introduces risk for unclear gain. When cross-platform arb moves to Phase 2, evaluate `pmxt` then.

#### Implementation Priority

1. **Kalshi WebSocket for fill notifications** -- Know immediately when orders fill instead of polling. Critical for position monitor exits.
2. **WebSocket ticker channel** -- Real-time price updates for all watched markets. Enables reactive trading (price moves -> evaluate edge -> trade) instead of periodic scanning.
3. **Async data fetches** -- Fetch NWS, Open-Meteo, Coinbase, Deribit, and Cleveland Fed concurrently. Reduces scan cycle time from serial (30+ seconds) to parallel (5-10 seconds).

### 2. Calibration & Optimization

The current calibration uses brute-force grid search over 3 parameters (intercept, slope, df) with O(n^3) complexity. This works but is slow and doesn't scale to higher-dimensional parameter spaces (per-city, per-market-type, ensemble weights).

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `optuna` | 4.7+ | Bayesian hyperparameter optimization | Tree-structured Parzen Estimator (TPE) finds optimal sigma/slope/df in ~100 trials instead of ~50,000 grid points. Built-in pruning stops bad trials early. Dashboard for visualization. Multi-objective optimization (minimize Brier score + maximize Sharpe). Replaces the nested `for` loops in `calibrate-sigma.py`. | HIGH |
| `scipy` | 1.17+ | `scipy.optimize.minimize` for gradient-based calibration, `scipy.stats` for proper scoring rules | For calibration scripts ONLY (not probability.py hot path). Nelder-Mead/L-BFGS-B converge in seconds vs. minutes for grid search. `scipy.stats` provides `brier_score_loss` decomposition (reliability + resolution + uncertainty). Keep `math.erf` in probability.py for runtime. | MEDIUM |
| `scikit-learn` | 1.8+ | `sklearn.metrics.brier_score_loss`, `sklearn.calibration.calibration_curve` | Reliability diagrams and Brier score decomposition for model validation. Use in backtest/audit scripts only. | MEDIUM |

**Why Optuna over scipy.optimize?** They serve different purposes. `scipy.optimize.minimize` is for continuous optimization with a single objective. Optuna handles mixed continuous/discrete parameter spaces (sigma is continuous, df is discrete), multi-objective optimization, pruning, and provides a dashboard. Use both: Optuna for hyperparameter search, scipy for fine-tuning within a trial.

**Why not full scikit-learn for probability models?** The constraint is clear: no heavy dependencies in the trading hot path. scikit-learn's `CalibratedClassifierCV` and isotonic regression are overkill for CDF-based probability models. The models are mathematical (normal CDF), not ML classifiers. Use scikit-learn only for evaluation metrics in offline scripts.

### 3. Volatility Modeling (Crypto Edge)

The crypto bot uses a static log-normal/GBM model. Crypto volatility clusters heavily -- a GARCH model would produce time-varying volatility estimates that better reflect current market conditions.

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `arch` | 8.0 | GARCH(1,1) / EGARCH volatility forecasting for crypto | The current crypto model uses realized volatility (backward-looking) blended with Deribit IV. GARCH captures volatility clustering and mean-reversion, producing forward-looking vol estimates. The `arch` package supports GARCH, EGARCH, APARCH with Cython/Numba acceleration. Fits in seconds on hourly data. | MEDIUM |
| `ccxt` | 4.x | Unified crypto exchange API (Coinbase + Deribit) | Currently using raw `requests` calls to Coinbase and Deribit separately. `ccxt` provides unified interface to 100+ exchanges, handles auth, rate limiting, and data normalization. Specifically, Deribit volatility history endpoints are well-supported. Reduces maintenance burden for crypto data fetching. | MEDIUM |

**Why `arch` over hand-rolled GARCH?** GARCH estimation requires MLE fitting with numerical optimization. Implementing this correctly from scratch is error-prone (convergence issues, parameter constraints). The `arch` package is the standard Python implementation, battle-tested in academic finance, and handles edge cases (non-stationarity, parameter bounds). Uses Cython for speed.

**Why not LSTM/deep learning for volatility?** The academic literature shows LSTM-GARCH hybrids outperform pure GARCH. But the system trades small ($5-10) on hourly crypto markets. The marginal improvement from LSTM over GARCH doesn't justify the complexity, training data requirements, and inference cost. GARCH(1,1) captures 90% of the signal. Revisit if crypto becomes a larger share of P&L.

### 4. Serialization & Performance

The system reads/writes JSON files hundreds of times per hour. The stdlib `json` module is adequate but leaves performance on the table.

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `orjson` | 3.10+ | Fast JSON serialization/deserialization | 6-10x faster than stdlib `json`. Native support for `datetime`, `dataclass`, `numpy` types. Drop-in replacement for `json.dumps`/`json.loads` in hot paths (`_atomic_write_json`, `load_trades`, market cache reads). The system does hundreds of JSON read/writes per hour across all bots. | HIGH |

**Why not `ujson`?** `orjson` is 2-4x faster than `ujson` on large/nested payloads, and handles more Python types natively. `ujson` has no advantage over `orjson` in any benchmark.

**Why not msgpack/protobuf?** JSON files are human-readable and debuggable. The trade logs, decision logs, and state files are frequently inspected manually. Binary serialization saves ~30% space but eliminates debuggability. Not worth it for this use case.

### 5. Configuration Validation

The system uses raw `json.load()` for all config files with no validation. Typos in config keys cause silent failures (e.g., `edgeThreshhold` instead of `edgeThreshold`).

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `pydantic` | 2.10+ | Config file validation with type checking | Define schemas for `bots-config.json`, `kalshi-config.json`, `kalshi-monitor-config.json`. Catches typos, wrong types, missing fields at startup instead of at trade time. Also validates trade records, API responses, and calibration outputs. | HIGH |
| `pydantic-settings` | 2.13+ | Environment variable validation | Replace raw `os.environ.get()` with typed settings. Validates `KALSHI_MODE` is "demo" or "production", `KALSHI_API_KEY` is non-empty, etc. at import time. | MEDIUM |

**Why Pydantic over dataclasses/attrs?** Pydantic provides validation, serialization, JSON schema generation, and env var loading in one package. `dataclasses` only provide structure, not validation. `attrs` validates but doesn't handle JSON/env loading. For a system where config errors cause silent trading failures, validation is the primary need.

### 6. Structured Logging & Observability

The system uses Python's `logging` module with custom formatters. This works for console output but makes log analysis difficult (grep-based debugging, no structured queries).

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `structlog` | 25.x | Structured JSON logging with context binding | Attach `bot_name`, `market_ticker`, `edge`, `action` as structured fields to every log line. Query logs with `jq` instead of `grep`. Correlate trades with decisions with errors. Works with existing `logging` module (wraps it, doesn't replace it). | MEDIUM |

**Why not OpenTelemetry/Datadog?** Overkill for a single-machine system. The bots run on one Mac Mini. Full observability platforms add network dependency and cost. `structlog` -> JSON files -> `jq` queries is sufficient. Revisit if the system grows to multiple machines.

### 7. Task Scheduling

The current supervisor uses sleep loops (`time.sleep(interval)`) for scan scheduling. This is fragile -- drift accumulates, and there's no way to schedule "run at market close" or "run 30 min before CPI release."

| Technology | Version | Purpose | Why | Confidence |
|------------|---------|---------|-----|------------|
| `APScheduler` | 4.x | Cron-style and interval-based job scheduling | Replace sleep loops with proper scheduling. Support cron expressions ("run at 14:30 ET every weekday"), interval-based ("every 5 minutes"), and one-off ("30 min before next CPI release"). Built-in missed-job handling and jitter. | LOW |

**Why LOW confidence?** The current sleep-loop approach works. APScheduler adds complexity (job stores, executor management) for modest benefit. The main value is event-driven scheduling (pre-CPI, pre-settlement), which could also be done with simple datetime checks in the existing loop. Evaluate during implementation -- if sleep loops prove problematic, add APScheduler. Otherwise, keep it simple.

### 8. Data Storage (Considered but NOT recommended)

| Technology | Status | Why Not |
|------------|--------|---------|
| SQLite | **Do not add** | The flat JSON file approach is a deliberate design choice (see PROJECT.md: "File-based state over database"). JSON files are human-readable, atomic-writable, S3-syncable, and grep-able. SQLite would require a migration, break S3 sync, and add query complexity without clear benefit at current scale (<1000 trades/day). |
| TinyDB/TinyFlux | **Do not add** | Performance degrades after 10K-50K records. JSON files with `orjson` will be faster. |
| Redis | **Do not add** | In-memory cache for a single-process system is unnecessary. Python dicts serve the same purpose. |
| PostgreSQL/TimescaleDB | **Do not add** | Way overkill. Single machine, <1000 trades/day, <100 markets watched. |

---

## Alternatives Considered

| Category | Recommended | Alternative | Why Not Alternative |
|----------|-------------|-------------|---------------------|
| HTTP Client | `httpx` | `aiohttp` | aiohttp is faster but async-only; httpx enables incremental migration from `requests` |
| HTTP Client | `httpx` | `requests` (keep) | `requests` has no async support; can't do concurrent data fetches |
| Kalshi WS | `websockets` (direct) | `aiokalshi` | aiokalshi WS support is still in development; direct `websockets` is well-documented by Kalshi |
| Kalshi SDK | Keep custom `KalshiClient` | `kalshi-python` (official) | Official SDK is auto-generated, lacks retry/circuit-breaker/caching that custom client has |
| Calibration | `optuna` | Manual grid search (keep) | Grid search is O(n^3) and doesn't scale; Optuna's TPE finds optima in ~100 trials |
| Calibration | `optuna` | `scipy.optimize.minimize` alone | Scipy can't handle discrete params (df), doesn't prune, no dashboard |
| Volatility | `arch` | Hand-rolled GARCH | GARCH MLE is hard to implement correctly; `arch` handles edge cases and is Cython-accelerated |
| Crypto data | `ccxt` | Raw `requests` to each exchange | `ccxt` handles auth, rate limiting, normalization for 100+ exchanges |
| JSON | `orjson` | `ujson` | `orjson` is 2-4x faster than `ujson`, handles more types natively |
| JSON | `orjson` | `msgpack` | Binary serialization breaks human debuggability of trade/decision logs |
| Config | `pydantic` | `dataclasses` | `dataclasses` don't validate; config errors cause silent trading failures |
| Logging | `structlog` | `logging` (keep) | `structlog` wraps `logging`, adds structure; incremental improvement not replacement |
| Scheduling | Sleep loops (keep) | `APScheduler` | Current approach works; APScheduler adds complexity for modest benefit |
| Database | JSON files (keep) | SQLite / Postgres | Breaks S3 sync, loses human readability, overkill at current scale |
| Prediction market SDK | Custom client (keep) | `predmarket` / `pmxt` | Pre-1.0 libraries; custom client is battle-tested with production safety features |

---

## Installation

```bash
# Speed edge: real-time data pipeline
pip install websockets>=16.0 httpx>=0.28.0

# Calibration: optimization and evaluation
pip install optuna>=4.7 scipy>=1.17.0 scikit-learn>=1.8.0

# Volatility modeling (crypto)
pip install arch>=8.0

# Performance: fast JSON
pip install orjson>=3.10

# Configuration validation
pip install pydantic>=2.10 pydantic-settings>=2.13

# Observability: structured logging
pip install structlog>=25.0

# Crypto data (optional, for ccxt migration)
pip install ccxt>=4.0
```

### Updated requirements.txt (proposed)

```
# Core (existing)
requests>=2.28.0
cryptography>=41.0.0
beautifulsoup4>=4.12.0
pytest>=7.0
python-dotenv>=1.0.0
fastapi>=0.100.0
uvicorn>=0.23.0

# Speed edge
websockets>=16.0
httpx>=0.28.0

# Calibration & evaluation (offline scripts only)
optuna>=4.7
scipy>=1.17.0
scikit-learn>=1.8.0

# Volatility modeling
arch>=8.0

# Performance
orjson>=3.10

# Configuration
pydantic>=2.10
pydantic-settings>=2.13

# Observability
structlog>=25.0

# Crypto data (optional)
# ccxt>=4.0
```

---

## Phased Adoption Strategy

The brownfield nature of this system demands incremental adoption. Do NOT attempt to introduce all libraries simultaneously.

### Phase 1: Feedback Loop (P0 from roadmap)
- **Add:** `orjson` (drop-in JSON replacement, zero risk)
- **Add:** `pydantic` (validate config files at startup, catches existing bugs)
- **Add:** `scipy` + `scikit-learn` (calibration scripts only -- Brier decomposition, reliability diagrams)

### Phase 2: Speed Edge (P1 from roadmap)
- **Add:** `websockets` (Kalshi WebSocket for fills + ticker)
- **Add:** `httpx` (async data fetches, migrate one bot at a time starting with source-monitor)

### Phase 3: Model Improvements (P3 from roadmap)
- **Add:** `optuna` (replace grid search in calibrate-sigma.py)
- **Add:** `arch` (GARCH for crypto volatility)
- **Add:** `ccxt` (unified crypto exchange interface)

### Phase 4: Operational Hardening (P4 from roadmap)
- **Add:** `structlog` (structured logging across all bots)
- **Evaluate:** `APScheduler` (only if sleep loops prove problematic)

---

## Key Constraints

1. **No scipy in `probability.py`** -- Keep `math.erf` for the trading hot path. scipy is for offline calibration/evaluation scripts only.
2. **No database** -- File-based state is a deliberate design choice. JSON + S3 sync works.
3. **No heavy ML** -- GARCH yes, LSTM/transformers no. The edge comes from data speed, not model complexity.
4. **Incremental migration** -- Every library addition must work alongside existing code. No big-bang rewrites.
5. **Single machine** -- No distributed systems, no message queues, no cloud services beyond S3.

---

## Sources

### Official Documentation (HIGH confidence)
- [Kalshi WebSocket API Docs](https://docs.kalshi.com/websockets/websocket-connection) -- WebSocket channels, authentication
- [Kalshi Orderbook Updates](https://docs.kalshi.com/websockets/orderbook-updates) -- orderbook_delta channel details
- [Kalshi User Fills](https://docs.kalshi.com/websockets/user-fills) -- fill notification channel
- [websockets 16.0 documentation](https://websockets.readthedocs.io/en/stable/) -- Python WebSocket library
- [HTTPX documentation](https://www.python-httpx.org/) -- Async/sync HTTP client
- [arch 8.0 documentation](https://arch.readthedocs.io/en/stable/) -- GARCH volatility models
- [Optuna 4.7 documentation](https://optuna.readthedocs.io/) -- Hyperparameter optimization
- [Pydantic documentation](https://docs.pydantic.dev/latest/) -- Data validation
- [scikit-learn brier_score_loss](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.brier_score_loss.html) -- Proper scoring rules
- [orjson GitHub](https://github.com/ijl/orjson) -- Fast JSON library
- [structlog documentation](https://pypi.org/project/structlog/) -- Structured logging
- [CCXT documentation](https://docs.ccxt.com/) -- Crypto exchange library
- [scipy.optimize documentation](https://docs.scipy.org/doc/scipy/reference/optimize.html) -- Optimization algorithms

### Community / Ecosystem (MEDIUM confidence)
- [HTTPX vs Requests vs AIOHTTP comparison](https://oxylabs.io/blog/httpx-vs-requests-vs-aiohttp) -- Performance benchmarks
- [aiokalshi GitHub](https://github.com/the-odds-company/aiokalshi) -- Asyncio Kalshi client (WS in development)
- [Pydantic Settings 2025 guide](https://levelup.gitconnected.com/pydantic-settings-2025-a-clean-way-to-handle-configs-f1c432030085)
- [Mastering Pydantic for Traders](https://www.marketcalls.in/python/mastering-pydantic-for-traders-a-step-by-step-guide.html)
- [orjson benchmarks](https://dollardhingra.com/blog/python-json-benchmarking/) -- 6-10x faster than stdlib json
- [GARCH forecasting with arch package](https://blog.quantinsti.com/garch-gjr-garch-volatility-forecasting-python/)
- [Optuna vs Hyperopt comparison](https://neptune.ai/blog/optuna-vs-hyperopt)

### Prediction Market Ecosystem (LOW-MEDIUM confidence)
- [predmarket unified SDK](https://github.com/ashercn97/predmarket) -- Pre-1.0, under rapid development
- [pmxt - CCXT for prediction markets](https://github.com/pmxt-dev/pmxt) -- Early stage
- [kalshi-python-unofficial](https://github.com/humz2k/kalshi-python-unofficial) -- Community Kalshi wrapper
- [Awesome Prediction Market Tools](https://github.com/aarora4/Awesome-Prediction-Market-Tools) -- Curated list
