# Trade Record Schema

## Required Fields (every trade record MUST have)

| Field | Type | Source | Added |
|-------|------|--------|-------|
| timestamp | ISO 8601 string | TradeManager | v1 |
| ticker | string | TradeManager | v1 |
| action | "buy" or "sell" | TradeManager | v1 |
| side | "yes" or "no" | TradeManager | v1 |
| price_cents | int (1-99) | TradeManager | v1 |
| count | int (>0) | TradeManager | v1 |
| cost_cents | int | TradeManager | v1 |
| reasoning | string | Bot | v1 |
| order_id | string (UUID) | Kalshi API | v1 |
| status | string | Kalshi API | v1 |
| source_bot | string | TradeManager (from logger name) | v1 |

## Expected Fields (new trades SHOULD have, legacy may lack)

| Field | Type | Source | Plan |
|-------|------|--------|------|
| model_prob | float (0-1) | Bot | v1 |
| raw_edge | float | Bot | v1 |
| fee_cents | float | Bot | v1 |
| sizing_method | string | Bot | v1 |
| kelly_fraction | float | Bot | v1 |
| market_snapshot | dict | Bot | v1 |
| best_bid | int | Flattened from snapshot | v1 |
| best_ask | int | Flattened from snapshot | v1 |
| spread | int | Computed | v1 |
| limit_price_rule | string | TradeManager | v1 |
| model_inputs | dict | Bot | v1 |
| edge_at_entry | float | Bot/TradeManager | Plan 1 |
| model_fair_value_cents | int | Bot/TradeManager | Plan 1 |
| order_price_cents | int | TradeManager | Plan 1 |
| fill_price_cents | int | Kalshi API (immediate) | Plan 1 |
| slippage_cents | int | Computed | Plan 1 |

## Settlement Fields (added by settlement reconciliation/backfill)

| Field | Type | Source |
|-------|------|--------|
| settlement_result | "won" or "lost" | reconcile/backfill |
| settlement_revenue_cents | int gross payout (100c per winning contract, 0 for losses) | reconcile/backfill |
| fill_price_cents | int (if not set at trade time) | reconcile |
| realized_edge | float | reconcile/backfill |
