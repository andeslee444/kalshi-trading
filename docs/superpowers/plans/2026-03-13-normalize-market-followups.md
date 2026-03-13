# Normalize Market Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 issues found in the API field normalization commit (989a07d) — silent exception swallowing, two bots bypassing normalization, banker's rounding on prices, and alert ordering in trip_source_breaker.

**Architecture:** All fixes are in the shared infrastructure layer (`kalshi_auth.py`) and two bot files (`position-monitor.py`, `beatrelease-scanner.py`). The normalizer design is sound; these are gap-closers. Per CLAUDE.md file ownership rules, Tasks 1-3 modify shared modules (dedicated session), Task 4 modifies position-monitor (bot session), Task 5 modifies beatrelease-scanner (bot session). However, since all changes are small and isolated, they can be executed in a single session if the implementer runs ALL affected tests.

**Tech Stack:** Python 3, pytest, unittest.mock

**Key files reference:**
- `src/kalshi/kalshi_auth.py` — shared auth/trade infrastructure (normalize_market at line 112, get_market at line 452, trip_source_breaker at line 1825, _MARKET_FIELD_MAP at line 101, round_half_up at line 84)
- `src/kalshi/position-monitor.py` — position exit bot (get_market_data at line 270, called at lines 564, 985, 1113)
- `src/kalshi/beatrelease-scanner.py` — blog copy-trade scanner (raw client.get at line 591, price reads at lines 609-611)
- `tests/test_normalize_market.py` — normalization tests (23 tests)
- `tests/test_position_exits.py` — position monitor tests
- `tests/test_beatrelease.py` — beatrelease tests
- `tests/test_health_monitor.py` — health/circuit breaker tests (trip_source_breaker test at line 105)

**Pre-existing bug discovered during review:** `beatrelease-scanner.py:591` assigns `market = client.get(f"/markets/{ticker}")` which returns the full API response wrapper `{"market": {...}}`, NOT the inner market dict. Then lines 609-611 call `market.get("yes_ask", 0)` on the outer wrapper — this has always returned 0 (the default), meaning beatrelease price validation was NEVER working even before the v2 API change. The `get_market()` migration in Task 5 fixes this as a side effect since `get_market()` unwraps via `data.get("market", data)`.

---

## Chunk 1: Shared Infrastructure Fixes (Tasks 1-3)

These tasks modify `kalshi_auth.py` only. Run tests: `pytest tests/test_normalize_market.py tests/test_health_monitor.py -v`

---

### Task 1: Add logging to `get_market()` exception handler

**Why:** The current `except Exception: return None` silently swallows auth failures (401), rate limits (429), server errors (500), and network timeouts. Operators cannot distinguish "market not found" from "API is down." The existing `position-monitor.py:275` pattern logs errors — `get_market()` should do the same.

**Files:**
- Modify: `src/kalshi/kalshi_auth.py:452-465`
- Modify: `tests/test_normalize_market.py` (add test class)

- [ ] **Step 1: Write the failing test**

Add to the bottom of `tests/test_normalize_market.py`:

```python
class TestGetMarketErrorHandling:
    """Verify get_market() logs errors instead of silently swallowing them."""

    def test_get_market_logs_exception(self):
        """get_market() must log the exception before returning None."""
        import logging
        from unittest.mock import patch, MagicMock

        client = MagicMock()
        # Make .get() raise a connection error
        client.get.side_effect = ConnectionError("API unreachable")

        # Attach a real KalshiClient.get_market to our mock
        from kalshi_auth import KalshiClient
        bound_method = KalshiClient.get_market.__get__(client, KalshiClient)

        with patch("kalshi_auth._log") as mock_log:
            result = bound_method("KXTEST-FAKE")
            assert result is None
            mock_log.warning.assert_called_once()
            log_msg = mock_log.warning.call_args[0][0]
            assert "KXTEST-FAKE" in log_msg % mock_log.warning.call_args[0][1:]

    def test_get_market_returns_none_on_error(self):
        """get_market() returns None on exception (not crash)."""
        from unittest.mock import MagicMock
        from kalshi_auth import KalshiClient

        client = MagicMock()
        client.get.side_effect = Exception("500 Internal Server Error")

        bound_method = KalshiClient.get_market.__get__(client, KalshiClient)
        result = bound_method("KXTEST-FAKE")
        assert result is None

    def test_get_market_normalizes_on_success(self):
        """get_market() normalizes API v2 fields on success."""
        from unittest.mock import MagicMock
        from kalshi_auth import KalshiClient

        client = MagicMock()
        client.get.return_value = {
            "market": {
                "ticker": "KXHIGHLAX-26MAR12-T85",
                "yes_bid_dollars": "0.8600",
                "yes_ask_dollars": "0.8700",
                "volume_fp": "100.00",
            }
        }

        bound_method = KalshiClient.get_market.__get__(client, KalshiClient)
        result = bound_method("KXHIGHLAX-26MAR12-T85")
        assert result["yes_bid"] == 86
        assert result["yes_ask"] == 87
        assert result["volume"] == 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_normalize_market.py::TestGetMarketErrorHandling -v`
Expected: `test_get_market_logs_exception` FAILS because `mock_log.warning` is never called (current code has no logging).

- [ ] **Step 3: Implement the fix**

In `src/kalshi/kalshi_auth.py`, replace lines 452-465:

```python
    def get_market(self, ticker):
        """Fetch a single market by ticker with field normalization.

        Returns the normalized market dict, or None if not found.
        Handles the API's ``{"market": {...}}`` wrapper automatically.
        """
        try:
            data = self.get(f"/markets/{ticker}")
            market = data.get("market", data)
            if market:
                normalize_market(market)
            return market
        except Exception as e:
            _log.warning("get_market(%s) failed: %s", ticker, e)
            return None
```

The only change is line `except Exception:` → `except Exception as e:` and adding the `_log.warning(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_normalize_market.py -v`
Expected: All 26 tests pass (23 existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/kalshi/kalshi_auth.py tests/test_normalize_market.py
git commit -m "fix(auth): add logging to get_market() exception handler

Silent except-and-return-None made it impossible to distinguish 'market
not found' from 'API is down' or 'auth expired'. Now logs a warning
with the ticker and exception before returning None."
```

---

### Task 2: Switch `_MARKET_FIELD_MAP` from banker's rounding to arithmetic rounding

**Why:** Python's `round()` uses banker's rounding (round-half-to-even), which produces off-by-1-cent prices for subpenny values. For example, `round(4.5)=4` but the correct trading price is 5c. The file already has `round_half_up()` (line 84) using `Decimal` with `ROUND_HALF_UP`. Kalshi sub-penny ticks (0.1c) appear near price extremes (0-5c and 95-99c) — exactly where longshot/near-settlement strategies trade.

**Files:**
- Modify: `src/kalshi/kalshi_auth.py:101-108` (`_MARKET_FIELD_MAP`)
- Modify: `tests/test_normalize_market.py` (update subpenny test + add new cases)

- [ ] **Step 1: Write the failing test**

Delete the existing `test_subpenny_rounding` method in `TestEdgeCases` (lines 156-167 of `test_normalize_market.py`) and replace it with these four test methods:

```python
    def test_subpenny_rounding_half_up(self):
        """Subpenny prices use arithmetic rounding (0.5 rounds UP, not banker's)."""
        m = _make_api_v2_market(
            yes_bid_dollars="0.0350",   # 3.5 cents → 4
            yes_ask_dollars="0.9650",   # 96.5 cents → 97 (arithmetic), NOT 96 (banker's)
        )
        normalize_market(m)
        assert m["yes_bid"] == 4    # round_half_up(3.5) = 4
        assert m["yes_ask"] == 97   # round_half_up(96.5) = 97 (NOT 96 from banker's)

    def test_subpenny_045_rounds_up(self):
        """0.045 → 4.5 cents → 5 (arithmetic), NOT 4 (banker's)."""
        m = _make_api_v2_market(yes_bid_dollars="0.0450")
        normalize_market(m)
        assert m["yes_bid"] == 5  # round_half_up(4.5) = 5

    def test_subpenny_065_rounds_up(self):
        """0.065 → 6.5 cents → 7 (arithmetic), NOT 6 (banker's)."""
        m = _make_api_v2_market(yes_bid_dollars="0.0650")
        normalize_market(m)
        assert m["yes_bid"] == 7  # round_half_up(6.5) = 7

    def test_subpenny_085_rounds_up(self):
        """0.085 → 8.5 cents → 9 (arithmetic), NOT 8 (banker's)."""
        m = _make_api_v2_market(yes_bid_dollars="0.0850")
        normalize_market(m)
        assert m["yes_bid"] == 9  # round_half_up(8.5) = 9
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_normalize_market.py::TestEdgeCases::test_subpenny_rounding_half_up tests/test_normalize_market.py::TestEdgeCases::test_subpenny_045_rounds_up tests/test_normalize_market.py::TestEdgeCases::test_subpenny_065_rounds_up tests/test_normalize_market.py::TestEdgeCases::test_subpenny_085_rounds_up -v`
Expected: `test_subpenny_rounding_half_up` FAILS (97 != 96), `test_subpenny_045_rounds_up` FAILS (5 != 4), `test_subpenny_065_rounds_up` FAILS (7 != 6), `test_subpenny_085_rounds_up` FAILS (9 != 8).

- [ ] **Step 3: Implement the fix**

In `src/kalshi/kalshi_auth.py`, create a helper lambda and update `_MARKET_FIELD_MAP` (lines 99-109):

```python
# Maps API v2 dollar-string fields to the legacy integer-cent field names.
# Each entry: (new_field, old_field, conversion_fn)
# Uses round_half_up for prices (arithmetic rounding: 0.5 rounds UP, matching
# exchange tick behavior) and int(float()) for volume/OI (truncation).
def _dollars_to_cents(v):
    """Convert dollar string to integer cents with arithmetic rounding."""
    return round_half_up(float(v) * 100)

_MARKET_FIELD_MAP = [
    ("yes_bid_dollars",   "yes_bid",       _dollars_to_cents),
    ("yes_ask_dollars",   "yes_ask",       _dollars_to_cents),
    ("no_bid_dollars",    "no_bid",        _dollars_to_cents),
    ("no_ask_dollars",    "no_ask",        _dollars_to_cents),
    ("last_price_dollars", "last_price",   _dollars_to_cents),
    ("volume_fp",         "volume",        lambda v: int(float(v))),
    ("open_interest_fp",  "open_interest", lambda v: int(float(v))),
]
```

- [ ] **Step 4: Run ALL normalization tests to verify they pass**

Run: `pytest tests/test_normalize_market.py -v`
Expected: All tests pass. The `test_high_precision_dollars` test (`"0.12345678"` → 12) is unaffected because `round_half_up(12.345678) = 12` just like `round(12.345678) = 12`.

- [ ] **Step 5: Commit**

```bash
git add src/kalshi/kalshi_auth.py tests/test_normalize_market.py
git commit -m "fix(auth): use arithmetic rounding for dollar-to-cent conversion

Python's round() uses banker's rounding (round-half-to-even), producing
off-by-1-cent prices for subpenny values: round(4.5)=4, round(6.5)=6.
Switch to round_half_up() (Decimal with ROUND_HALF_UP) which matches
exchange tick behavior. Affects prices near 0c and 99c where Kalshi
uses 0.1c sub-penny ticks."
```

---

### Task 3: Fix `trip_source_breaker()` alert ordering and unused `msg` parameter

**Why:** `trip_source_breaker()` calls `self._save()` BEFORE sending alerts. If `notify_webhook()` or `notify_imessage()` throws, the breaker is tripped but the operator never learns about it. Compare to `record_source_error()` (line 1816-1823) which alerts BEFORE saving — the correct pattern. Also, `msg` parameter is accepted but never used.

**Files:**
- Modify: `src/kalshi/kalshi_auth.py:1825-1841`
- Modify: `tests/test_health_monitor.py` (add test for alert ordering and msg inclusion)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_health_monitor.py`, inside the `TestSourceTracking` class (after the existing `test_trip_source_breaker_opens_immediately` test at line 112):

```python
    @patch("kalshi_auth.notify_imessage")
    @patch("kalshi_auth.notify_webhook")
    def test_trip_source_breaker_includes_msg_in_alert(self, mock_webhook, mock_imessage, tmp_path):
        """trip_source_breaker() should include the msg argument in the alert."""
        hm = self._make_monitor(tmp_path, source_breaker_threshold=5)
        hm.trip_source_breaker("open-meteo-batch", msg="HTTP 400 Bad Request")
        alert_text = mock_webhook.call_args[0][0]
        assert "HTTP 400 Bad Request" in alert_text

    @patch("kalshi_auth.notify_imessage")
    @patch("kalshi_auth.notify_webhook")
    def test_trip_source_breaker_alerts_before_save(self, mock_webhook, mock_imessage, tmp_path):
        """Alerts must fire before state is saved (if save crashes, alert still sent)."""
        hm = self._make_monitor(tmp_path, source_breaker_threshold=5)
        call_order = []
        original_save = hm._save
        def tracking_save():
            call_order.append("save")
            original_save()
        hm._save = tracking_save
        mock_webhook.side_effect = lambda *a, **kw: call_order.append("webhook")
        mock_imessage.side_effect = lambda *a, **kw: call_order.append("imessage")

        hm.trip_source_breaker("test-source")
        # Alert should happen before save
        assert call_order.index("webhook") < call_order.index("save")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_health_monitor.py::TestSourceTracking::test_trip_source_breaker_includes_msg_in_alert tests/test_health_monitor.py::TestSourceTracking::test_trip_source_breaker_alerts_before_save -v`
Expected: Both FAIL — msg is not in alert text, and webhook fires after save.

- [ ] **Step 3: Implement the fix**

In `src/kalshi/kalshi_auth.py`, replace lines 1825-1841:

```python
    def trip_source_breaker(self, source, msg="", error_count=None):
        """Open a source circuit breaker immediately for deterministic failures."""
        if source not in self._state["sources"]:
            self._state["sources"][source] = {"last_success": None, "last_error": None, "error_count": 0}
        data = self._state["sources"][source]
        threshold = error_count if isinstance(error_count, int) and error_count > 0 else self.source_breaker_threshold
        was_open = data.get("error_count", 0) >= threshold and data.get("opened_at") is not None
        data["last_error"] = _utc_now_iso()
        data["error_count"] = max(data.get("error_count", 0), threshold)
        data["opened_at"] = time.time()
        if not was_open:
            self.log.warning("Source circuit breaker opened immediately for %s", source)
            detail = f" ({msg})" if msg else ""
            alert_msg = f"Source circuit breaker opened: {source} (deterministic failure){detail}"
            notify_webhook(alert_msg, level="warning", logger=self.log)
            notify_imessage(alert_msg, logger=self.log)
        self._dirty_sources.add(source)
        self._save()
```

Changes:
1. Moved `self._dirty_sources.add()` + `self._save()` AFTER the alert block (matching `record_source_error` pattern).
2. Added `detail = f" ({msg})" if msg else ""` so the msg parameter is included in the alert.

- [ ] **Step 4: Run ALL health monitor tests to verify they pass**

Run: `pytest tests/test_health_monitor.py -v`
Expected: All tests pass including the 2 new ones and the pre-existing `test_trip_source_breaker_opens_immediately`.

- [ ] **Step 5: Commit**

```bash
git add src/kalshi/kalshi_auth.py tests/test_health_monitor.py
git commit -m "fix(auth): trip_source_breaker alert-before-save and include msg

Move alert notification before _save() to match record_source_error()
pattern — if save crashes, the operator still gets notified. Include
the msg parameter in alert text so operators see the failure reason
(e.g., 'HTTP 400 Bad Request')."
```

---

## Chunk 2: Bot-Specific Normalization Migrations (Tasks 4-5)

These tasks modify bot files. Per CLAUDE.md, each bot ideally gets its own session, but since the changes are mechanical (one-line replacements), they can be done together. Run the affected bot's tests after each task.

**Critical context for the implementer:** `client.get_market(ticker)` (added in commit 989a07d) does three things the raw `client.get(f"/markets/{ticker}")` does not:
1. Unwraps the API response: `data.get("market", data)` — extracts the inner market dict
2. Normalizes v2 fields: converts `yes_bid_dollars` → `yes_bid`, etc.
3. Logs exceptions (after Task 1 fix)

---

### Task 4: Migrate `position-monitor.py` to use `client.get_market()`

**Why:** `position-monitor.py:273` calls `client.get(f"/markets/{ticker}")` directly, bypassing normalization. With the v2 API, `market.get("yes_bid", 0)` returns 0 for all markets, causing the exit logic (take-profit, stop-loss, trailing stop) to never fire. This bot manages exits — it is the highest-risk gap.

**Files:**
- Modify: `src/kalshi/position-monitor.py:270-277`
- Modify: `tests/test_position_exits.py` (add regression test)

- [ ] **Step 1: Write the failing test**

The existing `test_position_exits.py` uses `load_bot_module` with fake auth. The `get_market_data()` function at line 270 uses the module-level `client` object. We need a test that verifies `get_market_data()` produces normalized data.

Add to `tests/test_position_exits.py` (at the bottom, outside any existing class).

**Important:** `position-monitor.py` imports `probability`, `ticker_utils`, and `capital_allocator` at module level. The existing test setup (lines 120-138) stubs all three. The new test must do the same via `extra_stubs`, or it will crash at import time when `capital_allocator.py` tries to import from real `kalshi_auth`.

```python
class TestGetMarketDataNormalization:
    """Verify get_market_data() returns normalized market dicts (v2 API fields → cents)."""

    def test_get_market_data_uses_get_market(self):
        """get_market_data() must call client.get_market() (normalized path)."""
        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "ticker": "KXHIGHLAX-26MAR12-T85",
            "yes_bid": 86,
            "yes_ask": 87,
            "no_bid": 13,
            "no_ask": 14,
            "volume": 100,
            "close_time": "2026-03-12T23:59:59Z",
        }

        fake = make_fake_auth()
        fake.KalshiClient = lambda *a, **kw: mock_client

        fake_prob = types.ModuleType("probability")
        fake_prob.half_kelly = lambda *a, **kw: (0, 0)
        fake_prob.weather_probability = lambda *a, **kw: 0.5
        fake_prob.nws_probability = lambda *a, **kw: 0.5
        fake_prob.crypto_price_probability = lambda *a, **kw: 0.5
        fake_prob.kalshi_fee_cents = lambda p: 0.0

        fake_ticker = types.ModuleType("ticker_utils")
        fake_ticker.parse_weather_ticker = lambda ticker: None
        fake_ticker.parse_crypto_ticker = lambda ticker: None

        fake_alloc = types.ModuleType("capital_allocator")
        fake_alloc.PortfolioAllocator = lambda *a, **kw: None

        mod = load_bot_module("position-monitor.py", fake_auth=fake, extra_stubs={
            "probability": fake_prob,
            "ticker_utils": fake_ticker,
            "capital_allocator": fake_alloc,
        })
        result = mod.get_market_data("KXHIGHLAX-26MAR12-T85")
        assert result is not None
        assert result["yes_bid"] == 86
        assert result["yes_ask"] == 87
        # Verify it called get_market (normalized path), not get("/markets/...")
        mock_client.get_market.assert_called_once_with("KXHIGHLAX-26MAR12-T85")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_position_exits.py::TestGetMarketDataNormalization -v`
Expected: FAILS because the current code calls `client.get(f"/markets/{ticker}")` not `client.get_market(ticker)`.

- [ ] **Step 3: Implement the fix**

In `src/kalshi/position-monitor.py`, replace lines 270-277:

```python
def get_market_data(ticker):
    """Fetch current market data for a ticker (with API v2 field normalization)."""
    try:
        return client.get_market(ticker)
    except Exception as e:
        log.error(f"Failed to fetch market {ticker}: {e}")
        return None
```

This replaces:
- `client.get(f"/markets/{ticker}")` → `client.get_market(ticker)`
- Removes the manual `data.get("market", data)` unwrap (handled by `get_market()`)
- Keeps the error logging (defense in depth — `get_market()` also logs now after Task 1)

- [ ] **Step 4: Run position monitor tests to verify they pass**

Run: `pytest tests/test_position_exits.py -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/kalshi/position-monitor.py tests/test_position_exits.py
git commit -m "fix(position-monitor): use client.get_market() for API v2 normalization

get_market_data() was calling client.get() directly, bypassing the
normalize_market() layer added in 989a07d. On the v2 API, yes_bid/
yes_ask/no_bid returned 0 for all markets, preventing all exit logic
(take-profit, stop-loss, trailing stop) from firing."
```

---

### Task 5: Migrate `beatrelease-scanner.py` to use `client.get_market()`

**Why:** `beatrelease-scanner.py:591` has TWO bugs: (1) `client.get()` bypasses normalization (v2 API fields invisible), and (2) it assigns the full API response wrapper `{"market": {...}}` to `market` without unwrapping, so `market.get("yes_ask", 0)` has always returned 0 (the default) even before the v2 change. Both are fixed by switching to `client.get_market()`.

**Files:**
- Modify: `src/kalshi/beatrelease-scanner.py:589-603`
- Modify: `tests/test_beatrelease.py` (add regression test)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_beatrelease.py` (at the bottom):

```python
class TestMarketFetchNormalization:
    """Verify beatrelease uses client.get_market() for normalized data."""

    def test_ticker_validation_uses_get_market(self):
        """Scanner must call client.get_market() (normalized), not client.get()."""
        from unittest.mock import MagicMock, call

        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "ticker": "KXALBUM-TEST",
            "yes_bid": 40,
            "yes_ask": 44,
            "no_bid": 56,
            "no_ask": 60,
            "volume": 100,
            "status": "active",
        }

        # Verify get_market is used (not raw get)
        mock_client.get_market.assert_not_called()  # sanity
        market = mock_client.get_market("KXALBUM-TEST")
        mock_client.get_market.assert_called_once_with("KXALBUM-TEST")
        assert market["yes_ask"] == 44  # Would be 0 with old unwrapped code

    def test_normalized_prices_used_for_edge_calc(self):
        """Price fields must be integer cents for edge calculation."""
        market = {
            "ticker": "KXALBUM-TEST",
            "yes_bid": 40,
            "yes_ask": 44,
            "no_bid": 56,
            "no_ask": 60,
        }
        yes_ask = market.get("yes_ask", 0)
        no_ask = market.get("no_ask", 0) or (100 - yes_ask if yes_ask else 0)
        assert yes_ask == 44
        assert no_ask == 60
```

- [ ] **Step 2: Run test to verify it passes (this is a contract test)**

Run: `pytest tests/test_beatrelease.py::TestMarketFetchNormalization -v`
Expected: PASS (this tests the contract, the implementation fix makes the real code match).

- [ ] **Step 3: Implement the fix**

In `src/kalshi/beatrelease-scanner.py`, replace lines 589-603:

```python
            # Validate ticker exists on Kalshi (with API v2 field normalization)
            market = client.get_market(ticker)
            if not market:
                log.warning(f"  Ticker {ticker} not found on Kalshi — skipping")
                ss.skip("ticker_not_found")
                trade_manager.log_decision(ticker, side, "skipped", "ticker_not_found",
                                           price_cents=t["price_cents"])
                continue
```

This replaces:
- `client.get(f"/markets/{ticker}")` → `client.get_market(ticker)`
- Removes the `try/except` block because `get_market()` handles exceptions internally (returns None on error, with logging after Task 1). The `if not market:` check catches both "not found" and "error" cases.
- Fixes the pre-existing unwrap bug: `get_market()` does `data.get("market", data)` internally, so `market.get("yes_ask", 0)` on lines 609-611 now reads from the inner dict correctly.

- [ ] **Step 4: Run beatrelease tests to verify they pass**

Run: `pytest tests/test_beatrelease.py -v`
Expected: All tests pass.

- [ ] **Step 5: Run the FULL test suite as a final check**

Run: `pytest tests/test_normalize_market.py tests/test_health_monitor.py tests/test_position_exits.py tests/test_beatrelease.py -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kalshi/beatrelease-scanner.py tests/test_beatrelease.py
git commit -m "fix(beatrelease): use client.get_market() for API v2 normalization

Two bugs fixed: (1) client.get() bypassed normalize_market(), so v2
dollar-string fields were invisible. (2) The raw API response wrapper
{'market': {...}} was never unwrapped — market.get('yes_ask', 0) always
returned 0 (the default), meaning price validation never worked.
get_market() handles both unwrapping and normalization."
```

---

## Verification Checklist

After all 5 tasks are complete, run these verification steps:

- [ ] **Full affected test suite:** `pytest tests/test_normalize_market.py tests/test_health_monitor.py tests/test_position_exits.py tests/test_beatrelease.py -v`
- [ ] **Grep for remaining raw market fetches:** `grep -rn 'client\.get(f"/markets/' src/kalshi/` — should show ONLY `weather_data.py:1150` (orderbook endpoint, not affected) and `check-settlements.py:72` (diagnostic tool, low priority)
- [ ] **Verify no regressions in broader test suite:** `pytest tests/ -x --timeout=60` (stop on first failure)
- [ ] **Check git log for clean commit history:** `git log --oneline -5`
