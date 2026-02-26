# Coding Conventions

**Analysis Date:** 2026-02-26

## Naming Patterns

**Files:**
- Python source files use hyphens: `weather-bot.py`, `beatrelease-scanner.py`, `cross-platform-arb.py`
- Module files use underscores: `kalshi_auth.py`, `hdd_parser.py`, `ticker_utils.py`, `capital_allocator.py`
- Test files match source modules: `test_probability.py`, `test_kelly.py`, `test_trade_manager.py`
- Scripts in `scripts/` use hyphens: `daily-backtest.py`, `calibrate-sigma.py`, `s3-sync.sh`

**Functions:**
- All lowercase with underscores: `weather_probability()`, `load_trades()`, `setup_logging()`
- Private/internal functions prefixed with underscore: `_norm_cdf()`, `_load_calibration()`, `_reset_calibration()`, `_atomic_write_json()`
- Helper functions in shared modules: `setup_unbuffered()`, `setup_signal_handlers()`, `trim_trade_log()`

**Variables:**
- Lowercase with underscores: `forecast_temp`, `trade_manager`, `max_trades`, `scan_interval`
- Constants in UPPERCASE: `DEMO_BASE_URL`, `KILL_SWITCH_PATH`, `MAX_RETRIES`, `PROJECT_DIR`
- Protected/internal variables prefixed with underscore: `_failures`, `_opened_at`, `_log`

**Types:**
- Classes use PascalCase: `KalshiClient`, `TradeManager`, `CircuitBreaker`, `PortfolioAllocator`, `HealthCheckMonitor`, `OrderMonitor`
- Dataclass names also PascalCase: `ScanSummary`, `BudgetResponse`

**Module-level imports:**
- Import early and at module level: seen in all bots at top after shebang and docstring
- Use `from` imports for specific functions from shared modules to enable direct usage

## Code Style

**Formatting:**
- No enforced formatter (no `.prettierrc` or `pyproject.toml` for black/ruff detected)
- Follows PEP 8 style: 4-space indentation, max line length appears flexible (~100-120 chars typical)
- Docstrings use triple-double-quotes: `"""Module docstring."""` and `"""Function docstring."""`

**Linting:**
- No `.eslintrc` or pylint config detected
- Tests use pytest (no explicit linting config in test files)

## Import Organization

**Order (observed pattern):**
1. Standard library imports (json, time, datetime, os, sys, re, math, argparse, etc.)
2. Third-party imports (requests, pathlib, cryptography, beautifulsoup4, pytest, etc.)
3. Relative imports from shared modules (kalshi_auth, probability, ticker_utils, etc.)

**Path aliases:**
- No absolute aliases (no `jsconfig.json` or `tsconfig.json` path mappings for Python)
- All imports use relative module names from `src/kalshi/` (conftest.py adds this to sys.path)
- Bots import directly: `from probability import half_kelly` (not `from src.kalshi.probability...`)

**Module imports style:**
- Single-line comma-separated imports common: `import json, time, datetime, os, sys, re`
- Multi-line imports for many functions: seen in `weather-bot.py` importing multiple utilities
- All bots import `setup_unbuffered`, `setup_logging`, `setup_signal_handlers` from `kalshi_auth`

## Error Handling

**Patterns:**
- Broad `except Exception` blocks used when suppressing expected errors (e.g., parsing, API retries)
- Specific exception catching for recoverable errors: `except (json.JSONDecodeError, ValueError, OSError)`
- Connection/timeout errors caught separately: `except (requests.exceptions.ConnectionError, requests.exceptions.Timeout)`
- Custom validation errors raised explicitly: `raise ValueError(f"{prefix}maxTradeAmount...")`

**Logging errors:**
- Errors logged via logger with context: `log.error("Failed to fetch data: %s", exc)`
- Warnings for recoverable issues: `_log.warning("Invalid df=%s in calibration, using default", df)`
- Info-level logging for normal operations and decisions: `log.info("Placed order: %s", order_id)`

**Recovery approach:**
- Retry logic with exponential backoff in `KalshiClient.get()` and `retry_request()`
- Circuit breaker pattern in `CircuitBreaker` class for API failures
- Graceful degradation: fallback to single-model GFS if ensemble API fails
- Config validation at startup prevents invalid state propagation

## Logging

**Framework:** Python `logging` module (standard library)

**Setup pattern (all bots):**
- Call `setup_logging("bot-name")` at module level to configure both stdout and rotating file handlers
- Logs go to `data/logs/{bot-name}.log` (5MB rotation, 3 backups)
- Handlers use consistent format: `"%(asctime)s [%(name)s] %(levelname)s: %(message)s"`

**Common patterns:**
```python
log = setup_logging("weather")
log.info("Starting weather bot scan...")
log.warning("Forecast data stale, using default")
log.error("Failed to place order: %s", response.status_code)
```

**Decision logging:**
- Trade decisions logged to decision files: `data/*-decisions.json` (JSON format)
- Each decision includes: ticker, source data, computed probability, edge, sizing decision
- Health state logged to: `data/health-state.json` (bot heartbeats, error counts)

## Comments

**When to Comment:**
- Complex mathematical models documented with derivations (e.g., CDF implementations, Kelly sizing)
- Non-obvious algorithm choices explained (e.g., sqrt scaling for forecast uncertainty)
- Integration points between bots/modules noted
- Configuration options documented inline

**JSDoc/TSDoc:**
- Docstrings follow Python convention: triple-quoted string immediately after function/class declaration
- Include parameter descriptions, return types, and examples for public APIs
- Short one-liner for simple helpers; detailed explanation for complex functions

**Example pattern:**
```python
def weather_probability(forecast_temp, threshold, direction, days_out=0, city=None):
    """CDF-based probability for KXHIGH weather markets.

    sigma scales with forecast horizon: sigma = intercept + slope * sqrt(days_out)
    Default: sigma = 2.0 + 0.5 * sqrt(days_out)

    direction="T": P(actual > threshold) = 1 - Phi((threshold - forecast) / sigma)
    direction="B": P(threshold <= actual < threshold+1) = bracket probability
    """
```

## Function Design

**Size:**
- Functions kept to single responsibility (30-50 lines typical for complex algorithms)
- Helper functions extracted for repeated patterns (e.g., `_norm_cdf()`, `_student_t_cdf()`)
- Long functions (>100 lines) split into logical sections with comments

**Parameters:**
- Positional for required args: `weather_probability(forecast_temp, threshold, direction)`
- Optional args with sensible defaults: `days_out=0, city=None, bankroll_cents=None`
- Config dicts passed as single parameter: `TradeManager(client, path, config_dict)`

**Return Values:**
- Tuples for multiple related values: `half_kelly()` returns `(contracts, risk)`
- Dicts for structured data: probability functions return dicts with calculation details
- None for optional results: `weather_probability()` returns None if forecast_temp is None
- Booleans for predicates: `check_kill_switch()`, `is_open()`, `is_market_liquid()`

## Module Design

**Exports:**
- Public functions listed in module docstring or clearly separated from private `_` functions
- Example `probability.py`: exports `weather_probability()`, `half_kelly()`, etc.; keeps `_norm_cdf()`, `_load_calibration()` private
- Shared modules (`kalshi_auth.py`, `probability.py`) list imports in docstring

**Barrel Files:**
- No barrel files (no `__init__.py` that re-exports)
- Import directly from module files: `from kalshi_auth import KalshiClient`
- `src/kalshi/__init__.py` empty (exists for package discovery only)

**Module organization:**
- Constants/config at top after imports
- Helper functions (_-prefixed) below constants
- Public functions in logical order (dependencies first)
- Classes at end (KalshiClient, TradeManager, CircuitBreaker)

## Data Validation

**Pattern:**
- Configuration validated at startup via `validate_trade_config()`
- Explicit type checks: `if not isinstance(trades, int)`
- Value bounds checked: `if amt > 100: raise ValueError(...)`
- Environment variables loaded via `dotenv` and checked in `__init__()` methods

**Trade safety:**
- Market cache includes TTL to prevent stale data usage
- Kill switch file (`data/HALT_TRADING`) checked before every trade
- Trade deduplication via `RecentTradeTracker` with per-market cooldown
- Balance verified before placing order

## Strategy and Intent

**Shared patterns:**
- All bots follow same structure: load config → fetch data → calculate edge → place trades via TradeManager
- Edge calculation always uses probability models from `probability.py`
- Position sizing always uses half-Kelly or derived Kelly variants
- Risk limits (max amount, max daily trades, max daily loss) enforced in TradeManager, not individual bots

**Signal sources:**
- Weather: Open-Meteo API (ensemble: GFS/ECMWF/ICON)
- Entertainment: HITS Daily Double (album charts), Box Office Mojo
- Economics: Cleveland Fed nowcast, AAA gas prices
- Crypto: Coinbase spot prices, Deribit IV
- Strategy: Custom arbitrage rules

**Risk controls:**
- Capital allocator assigns per-bot budgets based on signal quality
- City exposure limits prevent concentration
- Daily loss limits with grace period (3 days before hard stop)
- Circuit breaker prevents cascading API failures

---

*Convention analysis: 2026-02-26*
