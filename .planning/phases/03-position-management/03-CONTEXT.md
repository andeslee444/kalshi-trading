# Phase 3: Position Management - Context

**Gathered:** 2026-02-27
**Status:** Ready for planning

<domain>
## Phase Boundary

The position monitor actively manages open positions with exit strategies — take-profit, stop-loss, model-shift exits, trailing stops, and stale order cancellation. All exit logic lives in `position-monitor.py`. This phase does NOT add new entry strategies or modify how bots open positions.

</domain>

<decisions>
## Implementation Decisions

### Exit execution behavior
- Market orders for stop-loss and trailing stop exits (urgent — priority is getting out)
- Limit orders at current bid for take-profit and model-shift exits (patient — can wait a cycle)
- Partial exits allowed: sell a fraction at take-profit, let the rest ride to settlement or trailing stop
- Stop-loss and model-shift exits close the full position
- If a limit exit order doesn't fill, retry next scan cycle

### Threshold configuration
- Per-bot thresholds in `bots-config.json` — each bot has its own take-profit, stop-loss, model-shift divergence, and trailing stop values
- Default starting values from roadmap: take-profit 80c, stop-loss 30c, model-shift 20pp divergence, trailing 10c drop from peak
- Model-shift exit uses the live model with current calibration (not stored entry params)
- Position monitor routes to the correct probability model by reading bot name from the trade log entry

### Trailing stop mechanics
- Peak bid tracked in-memory, persisted to `data/trailing-state.json` on each scan cycle (survives restarts)
- Trailing stop arms only after position reaches 10c profit above entry price — prevents premature exit on normal noise
- Skip positions in illiquid markets (no bids / wide spread) — log warning, don't act on stale data
- Fixed 10c drop from peak triggers exit for all positions

### Logging and observability
- WhatsApp alert on every exit — format: "EXIT [type] TICKER: bought Xc, sold Yc, +/-$Z.ZZ"
- Exit decisions written to existing per-bot `*-decisions.json` files (dashboard already reads these)
- New "active exits" dashboard panel showing: ticker, entry price, current bid, nearest threshold, trailing peak value
- Dashboard panel reads from position monitor's exit state

### Claude's Discretion
- Take-profit partial exit fraction (default and configurability)
- Dashboard panel layout and API endpoint design
- Trailing state file structure and cleanup strategy
- How to handle multiple exit triggers firing simultaneously on the same position

</decisions>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches consistent with existing bot patterns.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 03-position-management*
*Context gathered: 2026-02-27*
