# System-Wide Issues & Cross-Bot Improvements

**Priority: Applies to all bots**
**Status: Multiple systemic issues affecting every bot**
**Last audited: 2026-03-06**

---

## Executive Summary

Beyond individual bot bugs, there are system-level issues that affect the entire trading operation. The most critical is the **timezone bug in daily resets** — all daily limit tracking uses local system time with no timezone awareness, causing potential misattribution of trades across day boundaries. Second is the **daily automation gap** — the full reconciliation/reporting pipeline is built but not scheduled. Third is the **stale bot epidemic** — multiple bots are crashed or stale due to missing dependencies and configuration issues.

**Note:** The sizing ceiling bug (previously SYS-2, `max()` vs `min()` in `_effective_max_trade_cents`) has been **fixed** — both methods now correctly use `min()` to treat static config as a ceiling.

---

## Issues

### SYS-1: Daily Automation Not Scheduled (CRITICAL)

**Problem:** The full settlement reconciliation and reporting pipeline exists and works, but nothing triggers it automatically. The pipeline scripts are:

- `scripts/reconcile-trades.py` — annotates trade records with settlement outcomes (idempotent)
- `scripts/backfill-settlements.py` — queries individual market endpoints for unsettled trades
- `scripts/daily-report.py` — P&L summary with optional WhatsApp notification
- `scripts/daily-backtest.py` — Brier score evaluation with >10% drift detection alerts
- `scripts/daily-automation.sh` — orchestrates all of the above in sequence with lock file

**What currently runs automatically:**
- Hourly S3 sync (cron active): `npm run sync:up`

**What does NOT run automatically:**
- Settlement reconciliation
- Daily reporting with WhatsApp alerts
- Backtest drift detection
- Calibration pipeline

**Impact:** Without automated reconciliation, settlement data accumulates without being matched to trade records. Cannot evaluate bot performance until someone manually runs `npm run reconcile`.

**Fix (Mac Mini action):**

Option A — LaunchAgent (recommended for Mac Mini):
```bash
# Template exists at scripts/com.kalshi.daily-report.plist
# Needs REPLACE_WITH_PROJECT_DIR substituted with actual path
cp scripts/com.kalshi.daily-report.plist ~/Library/LaunchAgents/
# Edit to set correct project directory
launchctl load ~/Library/LaunchAgents/com.kalshi.daily-report.plist
# Runs daily at 9:00 AM, outputs to data/logs/daily-automation-*.log
```

Option B — Cron:
```bash
0 9 * * * cd /path/to/kalshi-trading && bash scripts/daily-automation.sh >> data/logs/daily-automation.log 2>&1
```

---

### ~~SYS-2: RESOLVED~~ Sizing Ceiling Bug

**Status: FIXED.** Both `_effective_max_trade_cents()` and `_effective_max_daily_loss_cents()` in `kalshi_auth.py:1065-1091` now correctly use `min(static_cents, dynamic_cents)`. The static config acts as a hard ceiling; the percentage scales down with small bankroll.

Current behavior example (entertainment bot, $10K bankroll):
- Static: $15, Dynamic (3% x $10K): $300 → **Effective: $15** (ceiling holds)

---

### SYS-3: Stale Bot Epidemic (HIGH)

**Snapshot from 2026-03-04 22:28 (may have changed):**

| Bot | Last Heartbeat | Status | Issue |
|-----|---------------|--------|-------|
| source-monitor | 14:58 (today) | OK | Active |
| position-monitor | 14:59 (today) | Stale 10h | `daily_exit_limit` blocking all exits |
| weather | 14:46 (today) | OK | Active but finding nothing |
| entertainment | 14:59 (today) | OK | Active but 100% illiquid |
| crypto | 23:12 (Mar 1) | Stale 3d | Was running during analysis session |
| economics | 12:39 (Feb 28) | **Crashed 4d** | `feedparser` import error |
| cross-platform-arb | 11:07 (today) | Stale 11h | Unknown |
| beatrelease | 23:12 (Mar 1) | Stale 3d | Unknown |
| strategy | 23:12 (Mar 1) | Stale 3d | One-shot, runs once then exits |

**Root causes:**
1. **Economics:** Missing `feedparser` dependency (see SYS-9)
2. **Position-monitor:** Exit counter may not be resetting daily (see SYS-7 timezone bug)
3. **Strategy:** Runs as one-shot (not daemon), only executes when triggered — this is expected
4. **Beatrelease/Crypto:** Need investigation on Mac Mini — may be supervisor restart issue

**Fix (Mac Mini action):**
1. Install `feedparser`: `pip install feedparser`
2. Check supervisor status: `npm run supervisor:status`
3. Review supervisor logs: `cat data/logs/supervisor.log | tail -100`
4. Verify all daemon bots are running after fixes

---

### SYS-4: Backtest Brier Score 0.321 (HIGH)

**Problem:** The aggregate Brier score across all bots is 0.321, which is worse than predicting 50% on every trade (0.25). This means the collective model is DESTROYING value, not creating it.

**Breakdown by bot:**
- Weather: 0.310 (69 trades evaluated) — the only bot with enough settlements for a meaningful score
- Strategy: N/A (0 settlements evaluated)
- All others: N/A

**Root cause:** Primarily the weather model's inverted calibration (see weather bot plan BUG-1). Note: this score may be based on incomplete settlement data — running reconciliation (SYS-1) first could change the picture.

**Target:** Aggregate Brier < 0.15 before going to production. This requires:
1. Recalibrating weather sigma (weather bot BUG-1)
2. Widening economics CPI sigma (economics bot BUG-1)
3. Running settlement reconciliation to measure all bots
4. Iterating on any bot with Brier > 0.20

---

### SYS-5: Position Monitor Exit Limit (MEDIUM)

**Problem:** The position-monitor evaluated 4,765 decisions and ALL were blocked by `daily_exit_limit`. This means it's monitoring 37 positions but cannot exit any of them.

**Impact:** Take-profit, stop-loss, trailing stop, and model-shift exits are all disabled. Positions that should be closed remain open indefinitely.

**Config:** `maxDailyExits` is set to 20 in `config/bots-config.json`, which is a reasonable limit. The root cause may not be the limit itself but rather the **daily reset not firing** — see SYS-7 (timezone bug). If the exit counter never resets to zero, it would accumulate across days and permanently block exits.

**Investigation needed (Mac Mini):**
1. Check if 4,765 decisions span multiple calendar days (would confirm reset bug)
2. Verify `exits_today` counter resets at day boundary
3. Check position-monitor decision log timestamps: `cat data/position-decisions.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(set(t['timestamp'][:10] for t in d))"`

---

### SYS-6: Entertainment Bot 100% Illiquid (LOW)

**Problem:** The entertainment bot scans 914 markets but 854 (93%) are illiquid (`illiquid_no_ask`). Album sales and entertainment prediction markets on Kalshi simply don't have enough liquidity for automated trading.

**Options:**
1. **Accept it** — entertainment markets may become more liquid over time
2. **Disable** — save scan resources, reduce log noise
3. **Pivot** — focus entertainment bot on higher-liquidity subcategories (e.g., box office, awards)

---

### SYS-7: Timezone Bug in Daily Resets (HIGH)

**Problem:** All daily reset logic uses `datetime.date.today()` (local system time) with zero timezone awareness. If the Mac Mini production server and MacBook development machine are in different timezones, daily limits reset at different UTC times and trades can be misattributed to wrong days.

**Affected code:**

| Component | File | Line | Code |
|-----------|------|------|------|
| PortfolioAllocator reset | `capital_allocator.py` | 382 | `today = datetime.date.today().isoformat()` |
| PortfolioAllocator risk sum | `capital_allocator.py` | 521 | `today = datetime.date.today().isoformat()` |
| TradeManager reset | `kalshi_auth.py` | 1030 | `today = datetime.date.today().isoformat()` |
| TradeManager log rebuild | `kalshi_auth.py` | 1043 | `ts.startswith(today_str)` |
| Trade timestamp recording | `capital_allocator.py` | 471 | `datetime.datetime.now().isoformat()` |

**Note:** A `_local_today(city_code)` function exists in `kalshi_auth.py` that correctly uses `ZoneInfo`, but it is **never called in any daily reset path**. It's only used in source-monitor/position-monitor for weather market observation windows.

**Consequences:**
- Daily trade counts may not reset at the expected time
- Daily loss limits may accumulate across days (explains SYS-5?)
- Capital allocator and TradeManager could disagree on day boundaries
- `_risk_today_cents()` compares timestamps against local-time `today`, causing wrong risk sums

**Fix (code change — can be done from MacBook, needs Mac Mini testing):**

All daily reset paths should use UTC consistently:
```python
# Before:
today = datetime.date.today().isoformat()

# After:
today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
```

All timestamps should include timezone info:
```python
# Before:
"timestamp": datetime.datetime.now().isoformat()

# After:
"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
```

Files to change:
- `src/kalshi/capital_allocator.py` — lines 382, 471, 521
- `src/kalshi/kalshi_auth.py` — line 1030, 1043

---

### SYS-8: Missing Dependency Management (MEDIUM)

**Problem:** The economics bot crashed for 4+ days because `feedparser` wasn't installed on the production server. There is no import validation at startup and no mechanism to detect missing dependencies before a bot crashes.

**Impact:** A missing pip package silently crashes a bot. The supervisor auto-restarts it, it crashes again, and after 5 crashes in 10 minutes the supervisor disables it permanently. The bot stays dead until someone manually investigates.

**Fix:**

1. **Immediate (Mac Mini):** `pip install feedparser` to fix economics bot
2. **Pin dependencies:** Ensure `requirements.txt` includes all transitive dependencies
3. **Startup validation:** Add import check to supervisor before launching each bot:
   ```python
   # In supervisor.py, before starting a bot:
   result = subprocess.run([sys.executable, "-c", f"import importlib; importlib.import_module('{module}')"],
                           capture_output=True, timeout=10)
   if result.returncode != 0:
       log.error("Bot %s has missing dependencies: %s", name, result.stderr)
   ```
4. **Verification script:** Create `scripts/check-deps.py` that imports all bot modules and reports failures

---

### SYS-9: No Config Schema Validation (LOW)

**Problem:** Bots read from `config/bots-config.json` and `config/kalshi-config.json` at startup. Malformed JSON or missing keys cause runtime crashes with unhelpful error messages.

**Examples of silent failures:**
- Missing `maxTradeAmount` → `KeyError` crash in `TradeManager.__init__`
- Wrong type (string instead of number) → crash during first trade attempt
- Typo in key name → silently uses default, potentially unsafe behavior

**Fix:** Add config validation at bot startup or in supervisor:
- Validate required keys exist for each bot's section
- Validate types (numbers are numbers, booleans are booleans)
- Log warnings for unrecognized keys (catches typos)

---

## Cross-Bot Improvements

### CROSS-1: Schedule Existing Reconciliation Pipeline

The unified daily pipeline **already exists** at `scripts/daily-automation.sh`. It runs:
1. `backfill-settlements.py` — query market endpoints for settlement outcomes
2. `reconcile-trades.py` — annotate trade records across all 10 trade log files
3. `daily-report.py --notify --with-backtest` — P&L summary + WhatsApp
4. `daily-backtest.py` — Brier score evaluation + drift detection alerts

**What's needed:** Schedule it (see SYS-1 fix). No new code required.

### CROSS-2: Trade Record Field Standardization

**Current state by bot:**

| Bot | Edge Field | Format | Has `model_prob`? |
|-----|-----------|--------|-------------------|
| weather | `raw_edge` | float | Yes |
| crypto | `raw_edge` | float | Yes |
| economics | `raw_edge` + `edge` | float (both) | Yes |
| entertainment | `raw_edge` | float | Yes (via `confidence`) |
| source-monitor | `raw_edge` | float | Yes (via `confidence`) |
| beatrelease | `raw_edge` | float | Yes (via `confidence`) |
| cross-platform-arb | `raw_edge` | float | Yes |
| **strategy-trader** | **`est_edge`** | **string ("5%")** | **No** |
| **market-maker** | **(none)** | **—** | **No** |

**Critical gap:** Reconciliation scripts (`reconcile-trades.py`, `backfill-settlements.py`) use the `model_prob` field to compute `realized_edge`. Strategy-trader and market-maker don't populate `model_prob`, so their trades can **never** get `realized_edge` computed during reconciliation.

**Fix:**
1. **strategy-trader.py:** Replace `est_edge=f"{edge*100:.0f}%"` with `raw_edge=round(edge, 4)` and add `model_prob=round(0.5 + edge, 4)`
2. **market-maker.py:** Add `model_prob` (reservation price as probability) and `raw_edge` fields
3. Both are bot-specific changes scoped to their own files

### CROSS-3: Log Rotation

Production logs on S3 are growing (50MB total). Implement log rotation:
- Keep 7 days of detailed logs
- Archive older logs to compressed format
- Ensure supervisor.log doesn't grow unbounded

### CROSS-4: Dashboard Not Supervisor-Managed

**Problem:** The dashboard (`scripts/dashboard.py`, port 3456) must be started manually with `npm run dashboard`. It is not part of the supervisor's managed processes. If it crashes, no one is alerted and there is no auto-restart.

**Options:**
1. **Add to supervisor** as a managed daemon (like other bots) — gets heartbeat monitoring, crash recovery, and status in `supervisor:status`
2. **Run as separate LaunchAgent** — independent lifecycle from trading bots
3. **Accept manual start** — if dashboard is only used during active monitoring sessions

---

## Pre-Production Checklist

Before switching from DEMO to PRODUCTION (`KALSHI_MODE=production`):

- [ ] **Daily automation scheduled** — reconciliation + reporting running daily (SYS-1)
- [x] ~~**Sizing ceiling bug** fixed (max->min in `_effective_max_trade_cents`)~~ DONE
- [ ] **Timezone bug fixed** — all daily resets use UTC (SYS-7)
- [ ] **Settlement reconciliation** running daily with positive aggregate P&L
- [ ] **Brier score < 0.20** across all active bots
- [ ] **Economics sigma** widened and validated
- [ ] **Weather sigma** recalibrated
- [ ] **All bots running** — no crashes, no stale heartbeats for 7+ days
- [ ] **Dependencies pinned** — all imports validated at startup (SYS-8)
- [ ] **Position monitor** exits enabled and tested (verify daily reset works)
- [ ] **Trade record fields** standardized — strategy-trader and market-maker have `model_prob` (CROSS-2)
- [ ] **Kill switch** tested (`data/HALT_TRADING` stops all bots within 1 scan cycle)
- [ ] **Daily report** pipeline working (`npm run report:notify`)
- [ ] **Bankroll limit** set to acceptable production amount (start small: $500)
- [ ] **KALSHI_CONFIRM_PRODUCTION=yes** safety guard understood and documented

---

## Mac Mini Action Items

Consolidated list of actions that need to be performed on the production Mac Mini:

### Immediate (do first)

1. **Install feedparser:** `pip install feedparser` — fixes economics bot crash (SYS-3, SYS-8)
2. **Check bot status:** `npm run supervisor:status` — get current heartbeat snapshot (SYS-3)
3. **Check timezone:** `date` — confirm what timezone the Mac Mini uses (SYS-7)

### Schedule Daily Automation

4. **Install LaunchAgent** for daily reconciliation pipeline (SYS-1):
   ```bash
   cp scripts/com.kalshi.daily-report.plist ~/Library/LaunchAgents/
   # Edit: replace REPLACE_WITH_PROJECT_DIR with actual project path
   launchctl load ~/Library/LaunchAgents/com.kalshi.daily-report.plist
   ```

5. **Run reconciliation manually once** to establish baseline:
   ```bash
   npm run backfill && npm run reconcile && npm run report
   ```

### Investigate

6. **Position monitor exits:** Check if exit counter accumulated across days (SYS-5):
   ```bash
   # Check decision timestamps span multiple days
   python3 -c "
   import json
   d = json.load(open('data/position-decisions.json'))
   dates = set(t.get('timestamp','')[:10] for t in d if t.get('reason') == 'daily_exit_limit')
   print(f'Blocked decisions span {len(dates)} days: {sorted(dates)}')
   "
   ```

7. **Supervisor logs:** Check why crypto/beatrelease/arb went stale:
   ```bash
   tail -200 data/logs/supervisor.log | grep -E "(crypto|beatrelease|arb|crash|restart|stale)"
   ```

8. **Verify S3 sync is healthy:**
   ```bash
   npm run sync:down  # Pull latest from S3
   ls -la data/*-trades.json  # Check file dates
   ```

---

## File Index

| Plan | File | Priority |
|------|------|----------|
| Economics Bot | `docs/plans/bot-improvements/01-economics-bot.md` | #1 |
| Crypto Bot | `docs/plans/bot-improvements/02-crypto-bot.md` | #2 |
| Weather Bot | `docs/plans/bot-improvements/03-weather-bot.md` | #3 |
| Strategy Bot | `docs/plans/bot-improvements/04-strategy-bot.md` | #4 |
| System-Wide | `docs/plans/bot-improvements/05-system-wide.md` | All |
