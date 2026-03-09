# P&L Snapshot — Dual-Source Verified Financial Summary

**Date:** 2026-03-06
**Status:** Approved

## Problem

P&L calculation is error-prone because:
1. Kalshi API v2 dropped deposit/withdrawal endpoints (v1 had them)
2. The `revenue` field is gross payout (cost recovery + profit), not net — but local `settlement_revenue_cents` is sometimes net, sometimes gross
3. Three separate P&L calculation methods exist across 5 files, producing different numbers for the same trades
4. No automated cross-check between API settlements and local trade logs
5. No deposit tracking means Claude can't distinguish deposits from trading gains

## Solution

A standalone `scripts/pnl-snapshot.py` that:
1. Fetches authoritative data from Kalshi API (balance, settlements, fills, positions)
2. Loads local trade logs (from disk)
3. Cross-references both sources, flags every discrepancy
4. Writes a verified `data/financial-snapshot.json` with unambiguous field names
5. S3-synced so both machines have it
6. Dashboard serves it at `/api/snapshot` (read-only, no computation)

## Architecture

```
pnl-snapshot.py  →  data/financial-snapshot.json  →  S3 sync
                                                  →  dashboard.py /api/snapshot (reads file)
```

## Output Schema

```json
{
  "generated_at": "ISO timestamp",
  "sources_used": ["kalshi_api", "local_trade_logs"],

  "account": {
    "balance_cents": 48500,
    "portfolio_value_cents": 1200,
    "nav_cents": 49700
  },

  "realized_pnl": {
    "total_cents": -350,
    "total_fees_cents": 120,
    "net_after_fees_cents": -470,
    "wins": 42,
    "losses": 28,
    "win_rate": 0.6,
    "by_bot": { "weather": { "pnl_cents": 200, "wins": 15, "losses": 8 } },
    "by_day": { "2026-03-01": 50, "2026-03-02": -100 },
    "source": "kalshi_api_settlements"
  },

  "unrealized_pnl": {
    "total_cents": 150,
    "positions": [
      { "ticker": "...", "side": "yes", "cost_cents": 300, "current_value_cents": 450, "unrealized_cents": 150 }
    ],
    "source": "kalshi_api_positions_and_fills"
  },

  "verification": {
    "status": "ok | warnings | errors",
    "checks": [
      { "check": "settlement_count_match", "api": 70, "local": 68, "status": "warning" },
      { "check": "pnl_agreement", "api_cents": -350, "local_cents": -340, "delta_cents": 10, "status": "warning" },
      { "check": "fill_price_accuracy", "mismatches": 0, "status": "ok" },
      { "check": "fee_conversion", "total_fees_cents": 120, "status": "ok" }
    ],
    "unmatched_api_settlements": [],
    "unmatched_local_trades": []
  },

  "deposits": {
    "tracked": true,
    "total_deposited_cents": 50000,
    "total_withdrawn_cents": 0,
    "roi_pct": -0.7,
    "source": "data/deposits.json"
  }
}
```

## Verification Checks

| Check | What it verifies |
|-------|-----------------|
| settlement_count_match | API settlement count == local settled trade count |
| pnl_agreement | API P&L (revenue - cost) == local P&L per ticker |
| fill_price_accuracy | API fill prices match local recorded prices |
| fee_conversion | fee_cost string→cents conversion is lossless |
| orphan_settlements | API settlements with no local trade record |
| orphan_trades | Local trades with no API settlement |
| balance_consistency | deposits - withdrawals + realized P&L ≈ current balance |

## Deposit Tracking

Manually-maintained `data/deposits.json`:
```json
[
  {"date": "2026-02-15", "type": "deposit", "amount_cents": 50000, "note": "initial funding"}
]
```

Optional — if file doesn't exist, ROI is skipped and deposits section shows `"tracked": false`.

## Integration

- `npm run snapshot` — runs the script
- S3 sync includes `financial-snapshot.json`
- Dashboard serves at `/api/snapshot` (reads JSON file)
- Claude reads `data/financial-snapshot.json` directly
- Can be added to daily automation / cron

## File Ownership

| File | Action |
|------|--------|
| `scripts/pnl-snapshot.py` | Create (new) |
| `data/deposits.json` | Create template (new, gitignored) |
| `scripts/dashboard.py` | Add `/api/snapshot` endpoint (4 lines) |
| `scripts/s3-sync.sh` | Add `financial-snapshot.json` to sync filters |
| `package.json` | Add `snapshot` script |
| `tests/test_pnl_snapshot.py` | Create (new) |
