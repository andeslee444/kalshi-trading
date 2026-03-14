# Decision Record Schema

## Required Fields

Every decision record written by `TradeManager.log_decision()` must include:

| Field | Type | Source |
|-------|------|--------|
| `timestamp` | ISO 8601 string | `TradeManager` |
| `ticker` | string | bot / `TradeManager` |
| `side` | string | bot / `TradeManager` |
| `action` | string | bot / `TradeManager` |
| `reason` | string | bot / `TradeManager` |
| `source_bot` | string | `TradeManager` logger name |

## Common Optional Fields

| Field | Type | Notes |
|-------|------|-------|
| `edge` | float | Rounded to 4 decimals when present |
| `price_cents` | int | Market price observed when decision was made |
| `model_prob` | float | Bot-specific probability estimate |
| `raw_edge` | float | Bot-specific edge estimate |
| `market_snapshot` | dict | Optional richer context for audit/replay |

Notes:

- Decision logs are append-only JSON arrays.
- Decision log filenames are derived from the trade log stem as `*-decisions.json`.
