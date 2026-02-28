# Phase 3: Position Management - Research

**Researched:** 2026-02-27
**Domain:** Position exit logic, trailing stops, order management, multi-model probability routing
**Confidence:** HIGH

## Summary

Phase 3 adds active position exit management to `position-monitor.py`, which already has a working scaffold including take-profit, stop-loss, trailing stop, model-shift, and stale order cancellation functions. The existing code covers about 60% of the requirements but needs specific changes to match the CONTEXT.md decisions: market orders for urgent exits, limit orders for patient exits, partial exits at take-profit, per-bot configurable thresholds, multi-model probability routing for model-shift, trailing state persistence, illiquid market skipping, WhatsApp notifications on every exit, and a new dashboard "active exits" panel.

The implementation is entirely within the existing Python/JSON codebase -- no new libraries needed. The main complexity lies in three areas: (1) routing model-shift evaluation to the correct probability model by reading `source_bot` from trade log entries, (2) implementing market orders via Kalshi's existing `"type": "market"` support in `sell_position`, and (3) properly handling partial exits at take-profit where a fraction of the position is sold while the rest rides.

**Primary recommendation:** Modify the existing `position-monitor.py` and `TradeManager.sell_position()` to support market vs limit order types, add per-bot threshold config routing, implement partial exit logic, persist trailing state to `data/trailing-state.json`, add WhatsApp notifications, and extend the dashboard with an active exits panel.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Market orders for stop-loss and trailing stop exits (urgent -- priority is getting out)
- Limit orders at current bid for take-profit and model-shift exits (patient -- can wait a cycle)
- Partial exits allowed: sell a fraction at take-profit, let the rest ride to settlement or trailing stop
- Stop-loss and model-shift exits close the full position
- If a limit exit order doesn't fill, retry next scan cycle
- Per-bot thresholds in `bots-config.json` -- each bot has its own take-profit, stop-loss, model-shift divergence, and trailing stop values
- Default starting values from roadmap: take-profit 80c, stop-loss 30c, model-shift 20pp divergence, trailing 10c drop from peak
- Model-shift exit uses the live model with current calibration (not stored entry params)
- Position monitor routes to the correct probability model by reading bot name from the trade log entry
- Peak bid tracked in-memory, persisted to `data/trailing-state.json` on each scan cycle (survives restarts)
- Trailing stop arms only after position reaches 10c profit above entry price -- prevents premature exit on normal noise
- Skip positions in illiquid markets (no bids / wide spread) -- log warning, don't act on stale data
- Fixed 10c drop from peak triggers exit for all positions
- WhatsApp alert on every exit -- format: "EXIT [type] TICKER: bought Xc, sold Yc, +/-$Z.ZZ"
- Exit decisions written to existing per-bot `*-decisions.json` files (dashboard already reads these)
- New "active exits" dashboard panel showing: ticker, entry price, current bid, nearest threshold, trailing peak value
- Dashboard panel reads from position monitor's exit state

### Claude's Discretion
- Take-profit partial exit fraction (default and configurability)
- Dashboard panel layout and API endpoint design
- Trailing state file structure and cleanup strategy
- How to handle multiple exit triggers firing simultaneously on the same position

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| EXIT-01 | Position monitor executes take-profit exits when bid reaches 80% threshold | Existing `evaluate_take_profit()` at line 139 uses `TAKE_PROFIT_THRESHOLD`. Needs: (a) per-bot threshold lookup via `source_bot` from trade records, (b) partial exit support (sell fraction, keep rest), (c) limit order at current bid. |
| EXIT-02 | Position monitor executes stop-loss exits at 30% threshold | Existing `evaluate_stop_loss()` at line 183 uses both absolute and entry-price-relative stops. Needs: (a) per-bot threshold, (b) market order execution via `"type": "market"` in sell_position. |
| EXIT-03 | Position monitor executes model-shift exits when updated probability disagrees with entry by >20% | Existing `evaluate_model_shift()` at line 320 only handles weather (NWS) markets. Needs: multi-model routing (weather -> `nws_probability`/`weather_probability`, entertainment -> `info_arb_probability`, crypto -> `crypto_price_probability`, economics -> `econ_nowcast_probability`). Route using `source_bot` field from trade log entries. 20pp divergence threshold is the CONTEXT decision. |
| EXIT-04 | Trailing stop tracks peak value and exits on 10-cent drop | Existing `evaluate_trailing_stop()` at line 232. Already implements peak tracking with arming at min profit. Needs: (a) rename persist file from `position-peaks.json` to `trailing-state.json`, (b) skip illiquid markets, (c) market order execution, (d) per-bot trailing drop config. |
| EXIT-05 | Stale resting orders cancelled after configured TTL (120 min default) | Existing `cancel_stale_orders()` at line 392 already implements TTL-based cancellation. Already reads `ORDER_TTL_MINUTES` from config (default 120). This is substantially complete. Minor cleanup: per-bot TTL if desired. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python 3 stdlib | 3.11+ | All logic (json, datetime, argparse, math, pathlib, zoneinfo) | Already used by all bots |
| requests | 2.31+ | HTTP API calls via `KalshiClient` | Already in requirements.txt |
| pytest | 9.0.2 | Testing | Already configured in project |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| Chart.js (CDN) | 4.x | Dashboard charts | Already loaded in dashboard.html |
| Tailwind CSS (CDN) | 3.x | Dashboard styling | Already loaded in dashboard.html |
| FastAPI | 0.100+ | Dashboard API server | Already used by dashboard.py |

### Alternatives Considered
None -- this phase uses entirely existing dependencies.

**Installation:**
No new packages required. All dependencies already in `requirements.txt`.

## Architecture Patterns

### Recommended Project Structure
```
src/kalshi/
├── position-monitor.py      # Modified: multi-model routing, partial exits, market orders
├── kalshi_auth.py            # Modified: sell_position gains order_type param
├── probability.py            # No changes needed (all models already exist)
├── ticker_utils.py           # No changes needed
config/
├── bots-config.json          # Modified: per-bot exit thresholds added to each bot section
data/
├── trailing-state.json       # NEW: trailing stop peak state (renamed from position-peaks.json)
├── kalshi-position-trades.json  # Existing: exit trade log
├── kalshi-position-trades-decisions.json  # Existing: exit decision log
scripts/
├── dashboard.py              # Modified: new /api/exit-state endpoint
├── dashboard.html            # Modified: active exits panel
```

### Pattern 1: Per-Bot Threshold Routing
**What:** Each bot can have different exit thresholds. The position monitor reads the `source_bot` field from the trade log entry that opened the position, then looks up that bot's exit config in `bots-config.json`.
**When to use:** Every time a position is evaluated for exit.
**Example:**
```python
# In bots-config.json, each bot section gets exit thresholds:
# "weather": { ..., "exit": { "takeProfitCents": 80, "stopLossCents": 30, "modelShiftPp": 20, "trailingDropCents": 10 } }
# "crypto":  { ..., "exit": { "takeProfitCents": 85, "stopLossCents": 25, "modelShiftPp": 15, "trailingDropCents": 15 } }

# Source bot name mapping (setup_logging name -> bots-config.json key):
BOT_CONFIG_MAP = {
    "weather": "weather",          # weather-bot.py -> setup_logging("weather")
    "source-monitor": "weather",   # NWS trades use weather config
    "entertainment": "entertainment",
    "crypto": "crypto",
    "economics": "economics",
    "strategy": "strategy",
    "beatrelease": "beatrelease",
}

def _get_exit_config(source_bot):
    """Look up per-bot exit thresholds from bots-config.json."""
    config_key = BOT_CONFIG_MAP.get(source_bot, "position_monitor")
    bot_cfg = bots_config.get(config_key, {})
    exit_cfg = bot_cfg.get("exit", {})
    return {
        "take_profit_cents": exit_cfg.get("takeProfitCents", 80),
        "stop_loss_cents": exit_cfg.get("stopLossCents", 30),
        "model_shift_pp": exit_cfg.get("modelShiftPp", 20),
        "trailing_drop_cents": exit_cfg.get("trailingDropCents", 10),
        "trailing_min_profit_cents": exit_cfg.get("trailingMinProfitCents", 10),
    }
```

### Pattern 2: Multi-Model Probability Routing for Model-Shift
**What:** The position monitor must recompute probability using the same model that opened the position, with current data. Routes by `source_bot` name.
**When to use:** Model-shift evaluation.
**Example:**
```python
def _compute_current_probability(ticker, source_bot, entry_side):
    """Recompute probability using the model that opened this position.

    Returns (probability_for_our_side, reasoning_str) or (None, None) on failure.
    """
    # Weather: parse ticker, fetch NWS running high, use nws_probability
    if source_bot in ("weather", "source-monitor"):
        parsed = parse_temp_ticker(ticker)
        if not parsed:
            return None, None
        running_high = _fetch_nws_running_high(parsed["city"])
        if running_high is None:
            return None, None
        prob = nws_probability(running_high, parsed["threshold"], parsed["direction"],
                               datetime.datetime.now().hour)
        return (prob if entry_side == "yes" else 1.0 - prob,
                f"NWS {parsed['city']} high {running_high}F")

    # Crypto: fetch current price, use crypto_price_probability
    if source_bot == "crypto":
        # Parse ticker for threshold, direction, settlement time
        # Fetch spot price from Coinbase
        ...

    # Entertainment/info-arb: no live data source to recompute -- skip model-shift
    if source_bot in ("entertainment", "beatrelease"):
        return None, None  # Hold to settlement

    return None, None  # Unknown bot, skip
```

### Pattern 3: Market vs Limit Order Type in sell_position
**What:** `sell_position()` currently hardcodes `"type": "limit"`. Add an `order_type` parameter that can be `"limit"` or `"market"`.
**When to use:** Stop-loss and trailing stop use market orders; take-profit and model-shift use limit orders.
**Example:**
```python
def sell_position(self, ticker, side, price_cents, count, reasoning,
                  order_type="limit", **extra_fields):
    order_body = {
        "ticker": ticker,
        "action": "sell",
        "side": side,
        "type": order_type,  # "limit" or "market"
        "count": count,
    }
    # Only include price for limit orders
    if order_type == "limit":
        if side == "yes":
            order_body["yes_price"] = price_cents
        else:
            order_body["no_price"] = price_cents
```

### Pattern 4: Partial Exit for Take-Profit
**What:** At take-profit, sell a configurable fraction (e.g., 50%) and let the rest ride to settlement or trailing stop.
**When to use:** Take-profit exits only. Stop-loss and model-shift always exit full position.
**Example:**
```python
TAKE_PROFIT_FRACTION = pm_config.get("takeProfitFraction", 0.50)

def evaluate_take_profit(position, market, exit_config):
    # ... existing logic ...
    if net_proceeds >= take_profit_cents:
        exit_count = max(1, int(total_count * TAKE_PROFIT_FRACTION))
        return {
            "action": "take_profit",
            "count": exit_count,  # partial, not total_count
            ...
        }
```

### Pattern 5: WhatsApp Notification on Exit
**What:** After every successful exit trade, send a WhatsApp alert with entry/exit prices and P&L.
**When to use:** After `sell_position()` returns successfully.
**Example:**
```python
from kalshi_auth import notify_whatsapp

if result:
    # Calculate P&L
    entry_cost = (entry_price or 0) * exit_signal["count"]
    exit_proceeds = exit_signal["price"] * exit_signal["count"]
    pnl_cents = exit_proceeds - entry_cost
    pnl_str = f"+${pnl_cents/100:.2f}" if pnl_cents >= 0 else f"-${abs(pnl_cents)/100:.2f}"

    msg = (f"EXIT [{exit_signal['action']}] {ticker}: "
           f"bought {entry_price or '?'}c, sold {exit_signal['price']}c, {pnl_str}")
    notify_whatsapp(msg, logger=log)
```

### Anti-Patterns to Avoid
- **Re-fetching market data per exit type:** Fetch market data once per position per scan, share across all evaluators. The current code already does this correctly.
- **Persisting trailing state only in-memory:** Must write to disk every scan cycle. Process restarts must not lose peak tracking. Already handled by `_save_peaks()`.
- **Acting on stale/illiquid data for exits:** If `yes_bid == 0` or spread is very wide, do NOT trigger stop-loss or trailing stop. Log a warning and skip. This prevents selling into thin air.
- **Multiple exit types firing simultaneously:** Priority order matters. The current code evaluates take-profit -> stop-loss -> trailing stop -> model-shift and takes the FIRST match. This is correct behavior -- only one exit per position per scan cycle.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Probability models | New probability functions | Existing `probability.py` functions | All models already exist and are calibrated |
| Trade execution | Raw API calls | `TradeManager.sell_position()` | Handles kill switch, circuit breaker, cooldown, logging |
| WhatsApp notifications | Custom notification system | `notify_whatsapp()` from kalshi_auth | Already implements openclaw CLI integration |
| Atomic JSON writes | Manual file writes | `_atomic_write_json()` from kalshi_auth | Handles crash-safety with temp file + rename |
| Trade log loading | Custom JSON parsing | `load_trades()` from kalshi_auth | Handles missing files, corrupt JSON, encoding |
| Decision logging | Custom logging | `TradeManager.log_decision()` | Standardized format, dashboard integration |

**Key insight:** The position monitor is almost entirely a composition of existing infrastructure. The new code is routing logic and configuration, not new algorithms.

## Common Pitfalls

### Pitfall 1: Source Bot Name Mismatch
**What goes wrong:** The `source_bot` field in trade records uses the `setup_logging()` name (e.g., "weather", "source-monitor", "crypto"), but `bots-config.json` uses different keys (e.g., "position_monitor" uses underscores). A mismatch means positions fall through to defaults.
**Why it happens:** Bot names in `setup_logging()` don't match config file keys 1:1.
**How to avoid:** Create an explicit `BOT_CONFIG_MAP` dict mapping `source_bot` names to `bots-config.json` keys. Test the mapping with all known bot names.
**Warning signs:** Log messages showing "using default exit thresholds" for bots that should have custom ones.

### Pitfall 2: Market Order Without Bid Check
**What goes wrong:** Sending a market sell order when there are no bids results in the API rejecting the order or filling at a terrible price (1 cent).
**Why it happens:** Market orders don't guarantee price, and some markets have zero liquidity.
**How to avoid:** Always check that `yes_bid > 0` (or `no_bid > 0`) before sending ANY order. For market orders, also check spread width -- if spread > 20c, log a warning and use a limit order at the bid instead.
**Warning signs:** Exit trades filling at 1-2 cents on positions worth 30+ cents.

### Pitfall 3: Partial Exit Losing Track of Remaining Position
**What goes wrong:** After a partial take-profit exit, the remaining position needs to be tracked for trailing stop. If the peak tracker still tracks the old full count, the trailing stop exit count will be wrong.
**Why it happens:** Peak state records entry price but not remaining quantity. The position count comes from the API on each scan, which naturally reflects the partial exit.
**How to avoid:** Always read position count from the API (the `get_open_positions()` response), never from cached state. The API is the source of truth for remaining quantity.
**Warning signs:** Exit orders trying to sell more contracts than owned.

### Pitfall 4: Model-Shift Data Source Unavailability
**What goes wrong:** The model-shift evaluator tries to fetch live data (NWS, Coinbase, etc.) but the source is down, returning None. If not handled, this triggers a false exit or crashes.
**Why it happens:** External data sources have variable availability.
**How to avoid:** Every model-shift evaluator must return `(None, None)` on data fetch failure. The position monitor treats None as "no model-shift signal" and skips. Already handled for NWS; must replicate for crypto/economics.
**Warning signs:** Sporadic "Failed to fetch" errors followed by unexpected exits.

### Pitfall 5: Trailing Stop Arming Race Condition on Restart
**What goes wrong:** On process restart, trailing state is loaded from disk, but the peak bid may be stale. If the market gapped down during downtime, the trailing stop fires immediately on restart based on stale peak vs current bid.
**Why it happens:** Peak was persisted at a high value, then process died while price dropped.
**How to avoid:** On load, add a "grace period" -- skip trailing stop evaluation for the first scan cycle after loading persisted state, allowing peaks to re-sync with current market. OR: re-validate that current bid is within reasonable range of peak before triggering.
**Warning signs:** Batch of trailing stop exits immediately after bot restart.

### Pitfall 6: Sell Cooldown Blocking Retry of Unfilled Limit Exit
**What goes wrong:** A take-profit limit order is placed but doesn't fill. Next scan, the sell cooldown (10 minutes default) blocks retrying.
**Why it happens:** `sell_position()` records a cooldown timestamp on every sell attempt, whether it fills or not.
**How to avoid:** For limit exit orders that remain resting (not filled), the position will still appear in `get_open_positions()` but the sell cooldown blocks a new order. Check if a resting sell order already exists for this ticker before attempting a new one. If a resting order exists, skip (it's already working). If no resting order (cancelled or expired), then cooldown should allow retry.
**Warning signs:** Repeated "Sell cooldown" log messages for positions that should have been exited.

## Code Examples

Verified patterns from the existing codebase:

### Reading Source Bot from Trade Record
```python
# From position-monitor.py line 61-73
def _load_entry_records():
    """Load most recent BUY trade record per ticker across all bot logs."""
    entries = {}
    for log_path in ALL_TRADE_LOGS:
        trades = load_trades(log_path)
        for t in trades:
            if t.get("action") != "sell":
                entries[t.get("ticker", "")] = t  # last write wins
    return entries

# Usage: entry_rec.get("source_bot") returns e.g. "weather", "crypto", "entertainment"
# Usage: entry_rec.get("model_prob") returns the probability at entry time
```

### Kalshi Market Order Body
```python
# From research/kalshi-deep-dive.md line 130-138
# Kalshi supports "type": "market" in the order body
order_body = {
    "ticker": "KXHIGH...",
    "action": "sell",
    "side": "yes",
    "type": "market",   # market order -- fills at best available
    "count": 5,
    # no price field needed for market orders
}
```

### WhatsApp Notification
```python
# From kalshi_auth.py line 1498
from kalshi_auth import notify_whatsapp
notify_whatsapp("EXIT [stop_loss] KXHIGHMIA-26FEB27-T86: bought 45c, sold 28c, -$0.85", logger=log)
```

### Atomic JSON Persistence
```python
# From kalshi_auth.py -- used by _save_peaks
from kalshi_auth import _atomic_write_json
_atomic_write_json(TRAILING_STATE_PATH, state_dict)
```

### Existing Dashboard Position Panel (dashboard.html line 460-476)
```javascript
// Current panel shows: ticker, qty, spread, exposure, P&L
// New "active exits" panel should show: ticker, entry price, current bid,
// nearest exit threshold, trailing peak (if any)
```

### Per-Bot Config Structure in bots-config.json
```json
{
  "weather": {
    "maxTradeAmount": 25,
    "exit": {
      "takeProfitCents": 80,
      "stopLossCents": 30,
      "modelShiftPp": 20,
      "trailingDropCents": 10,
      "trailingMinProfitCents": 10,
      "takeProfitFraction": 0.50
    }
  },
  "crypto": {
    "maxTradeAmount": 10,
    "exit": {
      "takeProfitCents": 85,
      "stopLossCents": 25,
      "modelShiftPp": 15,
      "trailingDropCents": 15,
      "trailingMinProfitCents": 15,
      "takeProfitFraction": 0.50
    }
  },
  "position_monitor": {
    "takeProfitThreshold": 0.80,
    "stopLossThreshold": 0.30,
    ...
  }
}
```

### Trailing State File Structure (data/trailing-state.json)
```json
{
  "KXHIGHMIA-26FEB27-T86": {
    "entry_price": 45,
    "peak_bid": 72,
    "side": "yes",
    "source_bot": "weather",
    "first_seen": "2026-02-27T10:30:00",
    "last_updated": "2026-02-27T14:15:00"
  }
}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Hardcoded thresholds in position-monitor | Per-bot config in bots-config.json | This phase | Each bot type gets optimal exit thresholds |
| All exits via limit orders | Market orders for urgent exits | This phase | Stop-loss and trailing stop get immediate execution |
| Full position exit only | Partial exit at take-profit | This phase | Can capture profits while maintaining upside exposure |
| Weather-only model-shift | Multi-model routing via source_bot | This phase | Crypto, economics positions also get model-shift protection |
| No exit notifications | WhatsApp alert on every exit | This phase | Real-time operational awareness |
| position-peaks.json for trailing state | trailing-state.json with richer structure | This phase | Cleaner naming, source_bot tracking, cleanup strategy |

## Open Questions

1. **Kalshi Market Order Behavior on Low Liquidity**
   - What we know: Kalshi docs confirm `"type": "market"` is supported. Market orders fill at best available.
   - What's unclear: Does Kalshi reject market sell orders when there are zero bids? Or does it accept and sit unfilled?
   - Recommendation: Defensively check `yes_bid > 0` before sending market orders. If no bids, fall back to limit order at 1 cent (worst case) or skip with warning. Test on demo API first.

2. **Sell Cooldown Interaction with Limit Exit Retries**
   - What we know: `sell_position()` has a 10-minute cooldown per ticker+side. Limit take-profit orders may not fill in one cycle.
   - What's unclear: Should cooldown be bypassed for exit retries? Or should we check for existing resting sell orders?
   - Recommendation: Before placing a new sell order, check if a resting sell order already exists for this ticker (via `/portfolio/orders?status=resting`). If yes, skip (already working). If no, allow retry regardless of cooldown. This may require a small change to `sell_position()` or a caller-level check.

3. **Entertainment/Beatrelease Model-Shift Feasibility**
   - What we know: Entertainment bot uses HDD/article data that may not be available on every scan cycle.
   - What's unclear: Is there a reliable live data source to recompute info_arb probability for model-shift?
   - Recommendation: For Phase 3, skip model-shift for entertainment/beatrelease positions (return None). These positions are info-arb plays that should be held to settlement anyway. Model-shift is most valuable for weather and crypto where external data is continuously available.

4. **Simultaneous Exit Trigger Priority**
   - What we know: Current code evaluates take-profit -> stop-loss -> trailing -> model-shift in order, takes first match.
   - What's unclear: What if a position triggers both stop-loss (urgent, market order) and model-shift (patient, limit order)?
   - Recommendation: Keep current priority order. Stop-loss should beat model-shift since it's more urgent. The user decision says stop-loss uses market orders (fastest exit), so giving it higher priority is correct. Only one exit fires per position per scan cycle.

## Sources

### Primary (HIGH confidence)
- `src/kalshi/position-monitor.py` -- current implementation, 691 lines, all five exit types scaffolded
- `src/kalshi/kalshi_auth.py` -- TradeManager.sell_position() at line 1204, notify_whatsapp at line 1498
- `src/kalshi/probability.py` -- all probability models (weather, NWS, info_arb, crypto, economics)
- `config/bots-config.json` -- current per-bot config structure, position_monitor section at line 56
- `scripts/dashboard.py` -- existing /api/positions endpoint at line 802
- `scripts/dashboard.html` -- existing positions panel at line 86-105, JS at line 445-477
- `research/kalshi-deep-dive.md` -- Kalshi API order format confirming "type": "market" support (line 134)
- `tests/test_model_shift.py` -- existing test patterns for model-shift exit logic

### Secondary (MEDIUM confidence)
- Kalshi API documentation (inferred from codebase patterns and research docs)
- `setup_logging()` names across all bots (verified via grep)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new dependencies, all existing Python stdlib + project modules
- Architecture: HIGH -- existing scaffold covers 60%+ of requirements, changes are well-scoped
- Pitfalls: HIGH -- identified from reading actual code, verified edge cases in existing implementation

**Research date:** 2026-02-27
**Valid until:** 2026-03-27 (stable domain, no external dependency changes expected)
