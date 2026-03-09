# Audit: P&L Snapshot Tool (pnl-snapshot.py)

You are auditing a P&L snapshot tool that was recently built for this Kalshi trading project. Your job is to verify correctness, find bugs, and flag anything that doesn't add up.

## What was built

A dual-source verified P&L snapshot tool (`scripts/pnl-snapshot.py`) that:
1. Fetches settlement, fill, position, and balance data from the Kalshi API
2. Loads local trade logs from `data/kalshi-*-trades.json` files
3. Cross-references both sources to find orphans and P&L disagreements
4. Computes realized P&L, unrealized P&L, and a balance-equation cross-check
5. Writes `data/financial-snapshot.json` as the single source of truth

Supporting changes:
- `tests/test_pnl_snapshot.py` — 42 unit tests
- `data/deposits.json` — Manual deposit ledger (2 entries: $500 + $4500)
- `scripts/dashboard.py` — Added `/api/snapshot` endpoint
- `scripts/s3-sync.sh` — Added `financial-snapshot.json` to sync filters
- `scripts/daily-automation.sh` — Added snapshot generation as step 3
- `package.json` — Added `npm run snapshot` script
- `CLAUDE.md` — Added P&L documentation section

## What to audit

### 1. Math correctness

The core P&L formula in `compute_realized_pnl()`:
```
profit = revenue - yes_total_cost - no_total_cost
```
- Verify this matches Kalshi API semantics. The claim is that Kalshi's `revenue` field is **gross payout** (cost recovery + profit), NOT net profit.
- Check: does a winning YES trade with cost=200, payout=400 yield `revenue=400, yes_total_cost=200, profit=200`? Or does `revenue` already mean net?
- Read the actual API settlement data in `data/financial-snapshot.json` to spot-check real numbers.

Fee handling:
```python
fee_cents = round(float(s.get("fee_cost", "0") or "0") * 100)
```
- Verify `fee_cost` is actually dollars-as-string in the API (not already cents).
- Check if `net_after_fees_cents = total_cents - total_fees_cents` is the right formula (are fees already deducted from revenue, or separate?).

### 2. Balance check logic

The balance check (`_build_balance_check()`) derives true P&L from:
```
true_total_pnl = NAV - net_funded (deposits - withdrawals)
implied_unrealized = true_total_pnl - realized_net_after_fees
```

Verify:
- Is the accounting identity correct? Does `NAV = deposits + realized + unrealized + fees`? Or is there a sign error?
- The current live output says: NAV=$4981.33, deposits=$5000, realized_net=+$50.12, implied_unrealized=-$68.79. Does $50.12 + (-$68.79) = -$18.67 = $4981.33 - $5000? Check.
- Should fees be added back or are they already subtracted? `realized_net_after_fees = total_cents - total_fees_cents`. Is `total_cents` before or after fees?

### 3. ROI calculation

ROI was changed from `realized_pnl / deposits` to `(NAV - deposits) / deposits`.
- Verify this is the standard ROI formula for a trading account.
- Check edge cases: what if deposits = 0? What if there are withdrawals?

### 4. Unrealized P&L (the known-bad calculation)

`compute_unrealized_pnl()` uses `market_exposure` from the Kalshi API as "current value." The claim is that `market_exposure` actually returns cost basis, making most unrealized = $0.

- Read the positions in `data/financial-snapshot.json` and check: do positions with non-zero unrealized make sense? Are the ones showing $0 unrealized actually stale?
- Is `compute_unrealized_pnl` still useful at all, or should it be removed/deprecated since `balance_check.implied_unrealized_cents` is authoritative?
- Does the code/docs clearly communicate that `unrealized_pnl` section is unreliable?

### 5. Bot attribution

`_infer_bot()` maps ticker prefixes to bot names. Check:
- Does it cover all bot types in the project? (weather, crypto, economics, entertainment, strategy, source-monitor, position-monitor, beatrelease, arb, mm)
- Many bots map to "other" — is that acceptable or are there missing prefixes?
- The local trade logs have a `source_bot` field that takes precedence. Verify the priority logic in `build_snapshot()`.

### 6. Verification logic

`verify_settlements()` cross-checks API vs local:
- Does it correctly exclude sell/exit trades from orphan detection?
- The P&L agreement check compares `api: revenue - cost` vs `local: settlement_revenue_cents - cost_cents`. But earlier analysis found these use different semantics. Is the comparison even valid?
- Are there any cases where a ticker appears multiple times (multiple fills for same market) that could cause double-counting?

### 7. Test coverage gaps

Read `tests/test_pnl_snapshot.py` and check:
- Are there tests for the balance check with withdrawals (not just deposits)?
- Are there tests for positions with sell-side fills (short positions)?
- Are there tests for duplicate tickers in settlements?
- Are edge cases covered: empty fee_cost, None values, zero-position filtering?
- Do the test fixtures match real Kalshi API field names and types?

### 8. Integration points

- Read `scripts/dashboard.py` around the `/api/snapshot` endpoint. Does it handle missing file gracefully?
- Read `scripts/daily-automation.sh`. Is the snapshot step in the right order (after reconcile, before report)?
- Read `scripts/s3-sync.sh` sync_filters. Is `financial-snapshot.json` properly included?
- Read `CLAUDE.md` P&L section. Is the documentation accurate given the actual code?

### 9. Security and robustness

- Does `_fetch_api_data()` handle API errors/timeouts? Or will it crash the daily automation?
- The `main()` function imports `KalshiClient` which reads env vars. Could this fail silently?
- Is `_atomic_write_json` used correctly to prevent corrupt snapshots?
- Any risk of the snapshot file growing unbounded over time?

## Files to read

```
scripts/pnl-snapshot.py          # Main implementation
tests/test_pnl_snapshot.py       # Test suite
data/financial-snapshot.json     # Live output (real numbers to spot-check)
data/deposits.json               # Deposit ledger
scripts/dashboard.py             # /api/snapshot endpoint
scripts/daily-automation.sh      # Automation integration
scripts/s3-sync.sh               # S3 sync filters
CLAUDE.md                        # Documentation (P&L section)
```

## Output format

For each section above, provide:
- **Status:** PASS, WARN, or FAIL
- **Finding:** What you found (be specific, cite line numbers)
- **Recommendation:** Fix needed, or "none"

End with an overall verdict and a prioritized list of any fixes needed.
