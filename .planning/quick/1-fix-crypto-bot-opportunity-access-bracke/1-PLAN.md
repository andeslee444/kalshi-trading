---
phase: quick-1
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/crypto-bot.py
  - src/kalshi/ticker_utils.py
  - tests/test_crypto.py
  - tests/test_ticker_utils.py
autonomous: true
requirements: [ISSUE-1, ISSUE-2, ISSUE-3, ISSUE-4, ISSUE-5]

must_haves:
  truths:
    - "Bracket markets with NO-side liquidity are evaluated, not skipped as bracket_illiquid"
    - "Markets with last_trade_price but no current ask/bid are evaluated using fallback pricing"
    - "15-minute bracket tickers (KXBTC15M), monthly max/min tickers are parsed successfully"
    - "Drift is zeroed for sub-24h markets where it is negligible, and raw drift is logged alongside capped value"
  artifacts:
    - path: "src/kalshi/crypto-bot.py"
      provides: "Relaxed bracket liquidity filter, price fallback logic, drift zeroing for short horizons"
    - path: "src/kalshi/ticker_utils.py"
      provides: "Regex patterns for KXBTC15M, KXBTCMAXMON, KXBTCMINMON formats"
    - path: "tests/test_crypto.py"
      provides: "Tests for bracket NO-side liquidity, price fallback, drift zeroing"
    - path: "tests/test_ticker_utils.py"
      provides: "Tests for new ticker formats"
  key_links:
    - from: "src/kalshi/ticker_utils.py"
      to: "src/kalshi/crypto-bot.py"
      via: "parse_crypto_ticker() return value"
      pattern: "parse_crypto_ticker"
    - from: "src/kalshi/crypto-bot.py"
      to: "probability.py"
      via: "crypto_price_probability with drift_pct=0 for short horizons"
      pattern: "drift_pct"
---

<objective>
Fix five opportunity-access issues in the crypto bot that cause 98.7% of markets to be filtered out before model evaluation. Currently only 30 of 2,321 markets reach the probability model.

Purpose: Unlock hundreds of additional market evaluations per scan cycle so the GBM model can find tradeable edges.
Output: Updated crypto-bot.py, ticker_utils.py, and corresponding test files.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@src/kalshi/crypto-bot.py
@src/kalshi/ticker_utils.py
@tests/test_crypto.py
@tests/test_ticker_utils.py
@docs/plans/bot-improvements/02-crypto-bot.md

<interfaces>
From src/kalshi/ticker_utils.py:
```python
def parse_crypto_ticker(ticker):
    """Returns dict with keys: asset, direction, threshold, and optionally date, settlement_hour.
    Returns None if unparseable."""
```

From src/kalshi/probability.py:
```python
def crypto_price_probability(current_price, threshold, direction, time_horizon_minutes,
                              realized_vol_pct, iv_pct=None, drift_pct=0.0,
                              use_ou=False, ou_half_life_minutes=120):
    """Returns float probability in [0, 1]."""

def is_market_liquid(market):
    """Returns bool — checks spread and volume."""
```

Market dict keys used: yes_ask, no_ask, yes_bid, volume, last_price, close_time, expected_expiration_time, ticker, title, subtitle.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add new ticker formats to parse_crypto_ticker and test</name>
  <files>src/kalshi/ticker_utils.py, tests/test_ticker_utils.py</files>
  <action>
Add three new ticker format patterns to `parse_crypto_ticker()` in `ticker_utils.py`:

1. **15-minute brackets**: `KXBTC15M-{YYMONDDHMM}-{threshold}` (e.g., `KXBTC15M-26MAR042230-30`).
   - The `15M` suffix means 15-minute settlement window.
   - Date portion: `26MAR04` = 2026-Mar-04, `2230` = 22:30 settlement time.
   - No T/B direction prefix on threshold — these are bracket markets (direction="B").
   - Return: `{"asset": "BTC", "date": "2026-03-04", "direction": "B", "threshold": 30.0, "settlement_hour": 22, "settlement_minute": 30, "market_type": "15m"}`.

2. **Monthly max**: `KXBTCMAXMON-BTC-{YYMONDD}-{threshold}` (e.g., `KXBTCMAXMON-BTC-26MAR31-8750000`).
   - The asset is repeated after the prefix. Extract from the second segment.
   - Threshold is in cents (8750000 = $87,500.00) — divide by 100.
   - Direction is always "T" (above) since it's a max price market.
   - Return: `{"asset": "BTC", "date": "2026-03-31", "direction": "T", "threshold": 87500.0, "market_type": "maxmon"}`.

3. **Monthly min**: `KXBTCMINMON-BTC-{YYMONDD}-{threshold}` (e.g., `KXBTCMINMON-BTC-26MAR31-6500000`).
   - Same structure as MAXMON but direction is "B" (below) for min price.
   - Return: `{"asset": "BTC", "date": "2026-03-31", "direction": "B", "threshold": 65000.0, "market_type": "minmon"}`.

Implementation approach: Add these as early-return regex checks BEFORE the existing main pattern in `parse_crypto_ticker()`. Each gets its own `re.match()` block.

For the 15M pattern:
```python
m15 = re.match(r"KX(BTC|ETH|SOL|DOGE|XRP)15M-(\d{2})([A-Z]{3})(\d{2})(\d{4})-(\d+\.?\d*)", ticker)
```

For MAXMON/MINMON:
```python
mmax = re.match(r"KX(BTC|ETH|SOL|DOGE|XRP)(MAX|MIN)MON-\w+-(\d{2})([A-Z]{3})(\d{2})-(\d+\.?\d*)", ticker)
```

Also update `format_ticker_human()` to handle these new formats (add a `market_type` check in the crypto section).

In `tests/test_ticker_utils.py`, update the three existing `test_btc15m_returns_none`, `test_btcmax100_returns_none`, and `test_btcminmon_returns_none` tests to assert they NOW return valid parsed results instead of None. Add additional test cases:
- `KXETH15M-26MAR042230-50` (ETH 15-min bracket)
- `KXBTCMAXMON-BTC-26MAR31-8750000` (monthly max)
- `KXBTCMINMON-BTC-26MAR31-6500000` (monthly min)
- `KXBTC15M-26MAR040100-97000` (early morning 15-min bracket)
- Verify `market_type` field is set correctly in returned dict
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_ticker_utils.py -v -x</automated>
  </verify>
  <done>parse_crypto_ticker handles KXBTC15M, KXBTCMAXMON, KXBTCMINMON formats. All existing tests still pass, new format tests pass. Unparseable count drops from 136 to near-zero.</done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Fix bracket liquidity, price fallback, and drift cap in crypto-bot</name>
  <files>src/kalshi/crypto-bot.py, tests/test_crypto.py</files>
  <behavior>
    - Test: Bracket market with no yes_bid but valid no_ask (spread on NO side < 15c) should pass bracket filter
    - Test: Bracket market with no yes_bid AND no no_ask but volume >= 10 and last_price set should pass bracket filter
    - Test: Market with yes_ask=0 but last_price=45 should use last_price as fallback (not skip as no_price)
    - Test: Market with yes_ask=0, no_ask=0, last_price=0 should still be skipped (truly no price)
    - Test: Drift is zeroed when minutes_to_settle < 1440 (sub-daily)
    - Test: Drift uses actual value when minutes_to_settle >= 1440 (daily/weekly)
    - Test: Raw drift value is returned alongside capped value (for logging)
  </behavior>
  <action>
**A. Relax bracket liquidity filter** (lines 470-481 of crypto-bot.py):

Replace the current bracket filter logic:
```python
# BEFORE:
b_spread = (m.get("yes_ask", 0) - m.get("yes_bid", 0)) if m.get("yes_bid") else 999
b_volume = m.get("volume", 0) or 0
if b_spread > 15 or b_volume < 10:
```

With a dual-side liquidity check:
```python
# AFTER:
yes_bid = m.get("yes_bid", 0) or 0
yes_ask = m.get("yes_ask", 0) or 0
no_bid = m.get("no_bid", 0) or 0
no_ask = m.get("no_ask", 0) or 0
b_volume = m.get("volume", 0) or 0

# Check YES-side spread
yes_spread = (yes_ask - yes_bid) if (yes_bid and yes_ask) else 999
# Check NO-side spread
no_spread = (no_ask - no_bid) if (no_bid and no_ask) else 999
# Market is liquid if EITHER side has a tight spread
b_spread = min(yes_spread, no_spread)

# Also accept if market has recent trades even with empty book
has_recent_trade = bool(m.get("last_price"))
if b_spread > 15 and not (has_recent_trade and b_volume >= 10):
    ss.skip("bracket_illiquid")
    trade_manager.log_decision(ticker, "yes", "skipped", "bracket_illiquid",
                               spread=b_spread, volume=b_volume, asset=asset)
    continue
if b_volume < 5:  # Lower volume floor (was 10) since we have spread confirmation
    ss.skip("bracket_illiquid")
    trade_manager.log_decision(ticker, "yes", "skipped", "bracket_illiquid",
                               spread=b_spread, volume=b_volume, asset=asset)
    continue
```

**B. Add price fallback** (lines 532-538 of crypto-bot.py):

Replace the current price gate:
```python
# BEFORE:
yes_ask = m.get("yes_ask", 0)
no_ask = m.get("no_ask", 0)
yes_bid = m.get("yes_bid", 0)

if not yes_ask or yes_ask >= 99:
    ss.skip("no_price")
    continue
```

With fallback logic:
```python
# AFTER:
yes_ask = m.get("yes_ask", 0) or 0
no_ask = m.get("no_ask", 0) or 0
yes_bid = m.get("yes_bid", 0) or 0
last_price = m.get("last_price", 0) or 0

# Primary: use yes_ask if available and reasonable
# Fallback 1: compute from no_ask (yes_ask ~ 100 - no_ask)
# Fallback 2: use last_price as stale reference
if yes_ask and 1 <= yes_ask <= 99:
    market_price = yes_ask
elif no_ask and 1 <= no_ask <= 99:
    market_price = 100 - no_ask  # implied yes price from NO side
elif last_price and 1 <= last_price <= 99:
    market_price = last_price
else:
    ss.skip("no_price")
    continue
```

Then use `market_price` instead of `yes_ask` in the edge computation block below (lines 547-585). Specifically:
- Line 549: `edge = prob - market_price / 100` (was `yes_ask / 100`)
- When computing NO edge, use `no_ask` directly if available, else `100 - market_price`

**C. Zero drift for sub-daily markets** (around line 516):

After computing `drift = drift_by_asset.get(asset, DRIFT_PCT)`, add:
```python
# Drift is negligible for sub-daily horizons and introduces noise
if minutes_to_settle < 1440:
    drift = 0.0
```

**D. Log raw vs capped drift** in `compute_trailing_drift()` (line 165):

Change from:
```python
return max(-2.0, min(2.0, annualized))
```
To:
```python
capped = max(-2.0, min(2.0, annualized))
if abs(annualized) > 2.0:
    log.debug(f"  Drift capped: raw={annualized*100:.0f}% -> {capped*100:.0f}%")
return capped
```

This requires adding `log` access at the module level (it already exists as a module-level variable).

Add all corresponding tests to `tests/test_crypto.py` in new test classes:
- `TestBracketLiquidityRelaxed` — tests for NO-side liquidity acceptance, last_price fallback
- `TestPriceFallback` — tests for yes_ask -> no_ask -> last_price cascade
- `TestDriftZeroShortHorizon` — tests for drift=0 when minutes_to_settle < 1440
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_crypto.py tests/test_ticker_utils.py -v -x</automated>
  </verify>
  <done>
    - Bracket markets with NO-side-only liquidity pass the filter (ISSUE-1)
    - Markets with no yes_ask but valid no_ask or last_price are evaluated (ISSUE-4)
    - Drift is zeroed for sub-daily markets, raw values are logged (ISSUE-5)
    - Settlement buffer remains at 3 minutes (already minimal per config, ISSUE-2 is non-issue)
    - All existing tests pass, new tests cover relaxed filter logic
  </done>
</task>

</tasks>

<verification>
```bash
# All crypto and ticker tests pass
pytest tests/test_crypto.py tests/test_ticker_utils.py -v

# Full test suite still passes (no regressions)
pytest tests/ -x --timeout=120

# Verify no syntax errors in modified files
python3 -c "import py_compile; py_compile.compile('src/kalshi/crypto-bot.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('src/kalshi/ticker_utils.py', doraise=True)"
```
</verification>

<success_criteria>
- parse_crypto_ticker handles KXBTC15M, KXBTCMAXMON, KXBTCMINMON formats (136 tickers unlocked)
- Bracket liquidity filter accepts NO-side liquidity (expected: 200-500 additional markets)
- Price fallback uses no_ask and last_price when yes_ask unavailable (expected: 100-200 additional markets)
- Drift zeroed for sub-daily horizons to prevent annualization noise
- All existing tests pass without modification (except the 3 ticker tests that expected None)
- Total: markets evaluated per scan should increase from ~30 to 200+
</success_criteria>

<output>
After completion, create `.planning/quick/1-fix-crypto-bot-opportunity-access-bracke/1-SUMMARY.md`
</output>
