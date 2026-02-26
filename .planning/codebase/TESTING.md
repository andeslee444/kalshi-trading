# Testing Patterns

**Analysis Date:** 2026-02-26

## Test Framework

**Runner:**
- pytest 7.0+ (from `requirements.txt`)
- Config: no `pytest.ini` or `pyproject.toml` config detected; uses pytest defaults
- Invoked via: `pytest tests/` or `pytest tests/ -v` (from package.json)

**Assertion Library:**
- pytest built-in assertions (`assert x == y`, `assert x > y`)
- No external assertion library (no hypothesis, assertpy, etc.)

**Run Commands:**
```bash
npm test                              # Run all tests with verbose output
pytest tests/                         # Standard pytest run
pytest tests/ -v                      # Verbose mode with test names
pytest tests/test_probability.py      # Run specific test file
pytest tests/test_probability.py -k "test_zero"  # Run matching tests
```

## Test File Organization

**Location:**
- Tests co-located in `tests/` directory, separate from source (`src/kalshi/`)
- Mirror source module names: `src/kalshi/probability.py` → `tests/test_probability.py`
- Bot modules have test files: `test_weather.py`, `test_entertainment.py`, `test_economics.py`

**Naming:**
- Files: `test_*.py` (pytest discovery pattern)
- Classes: `Test<Module>` or `Test<Functionality>` (PascalCase)
- Methods: `test_<scenario>` (lowercase with underscores describing the test case)

**File Structure:**
```
tests/
├── conftest.py                    # Shared path setup, no fixtures
├── test_probability.py            # 500+ lines, 8 test classes
├── test_kelly.py                  # 60 lines, 2 test classes
├── test_trade_manager.py          # 700+ lines, 12 test classes
├── test_allocator.py              # Tests for capital_allocator.py
├── test_entertainment.py          # Entertainment bot logic
└── ... (22 test files total)
```

## Test Structure

**Suite Organization (pytest classes):**

All tests are organized as methods within `Test<Name>` classes. Example from `test_probability.py`:

```python
class TestNormCdf:
    """Test _norm_cdf() function."""

    def test_zero_gives_half(self):
        assert abs(_norm_cdf(0) - 0.5) < 1e-10

    def test_large_positive(self):
        assert _norm_cdf(6.0) > 0.999999

    def test_symmetry(self):
        """CDF(x) + CDF(-x) = 1."""
        for x in [0.5, 1.0, 2.0, 3.0]:
            assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < 1e-10
```

**Patterns:**

- **Setup method**: `def setup_method(self):` called before each test in class
- **Teardown method**: `def teardown_method(self):` called after each test
- **Test discovery**: All classes named `Test*` and methods named `test_*`

**Setup/teardown example (from test_probability.py):**
```python
class TestCalibration:
    def setup_method(self):
        """Reset calibration cache before each test."""
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    def test_default_sigma_without_calibration_file(self):
        prob = weather_probability(86, 86, "T", 0)
        assert 0.45 <= prob <= 0.55
```

**Conftest setup:**
- `tests/conftest.py` adds `src/kalshi/` to `sys.path` for direct imports
- No pytest fixtures defined (not used in this codebase)
- Enables pattern: `from probability import weather_probability` (not `from src.kalshi.probability`)

## Mocking

**Framework:** unittest.mock (standard library)

**Patterns (from test_allocator.py):**

```python
from unittest.mock import MagicMock, patch

def test_supersede_logic(self):
    mock_client = MagicMock()
    mock_client.get_balance.return_value = (50000, 50000)
    allocator = PortfolioAllocator(client=mock_client)
```

**What to Mock:**
- External API clients: `KalshiClient`, HTTP requests
- File system operations: `Path.read_text()`, file writes
- Time-dependent functions: `time.time()`, `datetime.datetime.now()`

**What NOT to Mock:**
- Pure mathematical functions: `_norm_cdf()`, `weather_probability()`
- Core business logic: `TradeManager.place_order()`, `CircuitBreaker.is_open()`
- Configuration loading (use actual JSON test files in temp directories)

**Example (trade log testing):**
```python
def test_appends_to_existing_file(self, tmp_path):
    trades_file = tmp_path / "trades.json"
    existing = [{"ticker": "OLD", "price": 10}]
    trades_file.write_text(json.dumps(existing))

    save_trade(trades_file, {"ticker": "NEW", "price": 20})

    result = json.loads(trades_file.read_text())
    assert len(result) == 2
```

## Fixtures and Factories

**Test Data:**

No custom fixtures, but files use `tmp_path` (pytest built-in) for temporary files:

```python
def test_creates_new_file_if_missing(self, tmp_path):
    trades_file = tmp_path / "subdir" / "trades.json"
    save_trade(trades_file, {"ticker": "FIRST"})
    assert trades_file.exists()
```

**Location:**
- Test data embedded in test methods using `tmp_path`
- Constants defined at class or module level for repeated test values
- No separate `fixtures/` directory or factory files

**Bot module loading pattern (test_kelly.py):**

Tests must stub `kalshi_auth` before importing bot modules (which call `KalshiClient()` at import time):

```python
def _load_strategy_trader():
    """Load strategy-trader.py, mocking out kalshi_auth first."""
    import importlib.util
    import sys
    from unittest.mock import MagicMock

    # Stub kalshi_auth before importing bot
    sys.modules['kalshi_auth'] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "strategy_trader",
        "/path/to/strategy-trader.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

## Coverage

**Requirements:** No coverage target enforced (no `.coveragerc` or pytest config)

**View Coverage:**
- Not configured for automated reporting
- Can run with `pytest --cov` if installed (not in requirements)

## Test Types

**Unit Tests (the focus of this codebase):**
- Scope: Single pure functions (probability models, Kelly sizing, data parsing)
- Approach: No external API calls, no credentials needed
- Example: `test_probability.py` tests CDF functions, Kelly sizing, weather probability
- Pattern: Assert output against known values or mathematical properties

**Integration Tests:**
- Scope: Minimal (only trade manager safety checks)
- Approach: Use `MagicMock` for API client, test TradeManager logic with mock client
- Example: `test_trade_manager.py` tests kill switch, circuit breaker, config validation
- Pattern: Verify component interactions (e.g., CircuitBreaker + TradeManager)

**E2E Tests:**
- Status: Not used (trading requires real API keys and market state)
- Alternatives: `demo-trader.py` for manual testing with demo API

## Common Patterns

**Async Testing:**
- Not used (codebase is synchronous)
- Async operations handled via `concurrent.futures.ThreadPoolExecutor` in non-test code

**Error Testing:**

```python
def test_negative_daily_trades_fails(self):
    with pytest.raises(ValueError, match="maxDailyTrades"):
        validate_trade_config({
            "maxTradeAmount": 5,
            "maxDailyTrades": -1,
            "maxDailyLoss": 25
        })
```

**Boundary Testing:**

```python
def test_price_at_boundary(self):
    """Price at 100 should return zero."""
    contracts, risk = half_kelly(0.20, 100, 500)
    assert contracts == 0

def test_at_threshold(self):
    """Forecast equals threshold -> ~0.50."""
    prob = weather_probability(86, 86, "T", 0)
    assert 0.45 <= prob <= 0.55
```

**Property Testing (mathematical):**

```python
def test_monotonically_increasing(self):
    """t_cdf should increase monotonically."""
    vals = [_student_t_cdf(x, 6) for x in [-3, -2, -1, 0, 1, 2, 3]]
    for i in range(len(vals) - 1):
        assert vals[i] < vals[i + 1]

def test_symmetry(self):
    """CDF(x) + CDF(-x) = 1."""
    for x in [0.5, 1.0, 2.0, 3.0]:
        assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < 1e-10
```

**Floating-point assertions:**

Always use tolerance for float comparisons:
```python
assert abs(_norm_cdf(0) - 0.5) < 1e-10          # strict tolerance for CDF
assert abs(_norm_cdf(1.0) - 0.8413) < 0.001     # 0.1% tolerance
assert 0.45 <= prob <= 0.55                      # range check for probabilities
```

## Key Test Files

**`test_probability.py` (550+ lines):**
- 8 test classes covering all probability models
- Tests mathematical properties (symmetry, monotonicity, convergence)
- Calibration cache reset in setup/teardown
- Tests for weather, NWS, info-arb, Kelly sizing

**`test_kelly.py` (180+ lines):**
- Tests half-Kelly buy-side and sell-side sizing
- Verifies bankroll constraints, edge scaling, fee impacts
- Symmetry test at 50c: buy and sell sizing should match

**`test_trade_manager.py` (750+ lines):**
- 12 test classes covering safety infrastructure
- Kill switch, circuit breaker, config validation
- Atomic writes, trade log trimming, deduplication
- OrderMonitor and RecentTradeTracker logic

**`test_allocator.py` (100+ lines):**
- Signal quality scoring
- Budget approval logic and supersede mechanism
- Per-bot and per-city exposure limits

## Test Execution Notes

**Import pattern for bot modules:**
- Bots use `from kalshi_auth import KalshiClient, ...` at module level
- Tests cannot import bots directly (import-time side effects)
- Solution: Use `importlib.util.spec_from_file_location()` + stub `sys.modules['kalshi_auth']`

**Calibration state management:**
- `probability.py` caches calibration file in module-level variable
- Tests call `_reset_calibration()` in setup/teardown to clear cache
- Prevents test interference when running suite

**Temporary files:**
- All tests using file I/O use `tmp_path` fixture (automatic cleanup)
- No manual cleanup needed (pytest manages lifecycle)

---

*Testing analysis: 2026-02-26*
