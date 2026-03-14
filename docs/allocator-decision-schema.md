# Allocator Decision Schema

The canonical allocator decision contract is `BudgetResponse`.

`PortfolioAllocator.request_budget()` returns a `BudgetResponse` object, and
Phase 0 freezes the serialized field set exposed by `BudgetResponse.as_record()`.

## Required Fields

| Field | Type | Meaning |
|-------|------|---------|
| `approved` | bool | Whether the allocator approved the trade |
| `max_cost_cents` | int | Maximum trade spend/risk approved for this request |
| `bankroll_cents` | int | Kelly bankroll to use for sizing |
| `reason` | string | Denial reason, or empty string on approval |
| `binding_constraint` | string | Tightest active limit on approval, or empty string on denial |

## Contract Notes

- `approved=false` responses should still populate the same field set.
- `binding_constraint` is the bridge to future ledger attribution, because it
  identifies which limit actually bound sizing.
- Refactors should preserve `BudgetResponse.as_record()` even if the allocator
  later writes dual-format records to a ledger.
