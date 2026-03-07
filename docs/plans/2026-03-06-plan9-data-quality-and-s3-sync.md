# Plan 9: Data Quality & S3 Sync Hardening

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This plan modifies `scripts/s3-sync.sh`, `scripts/sync-up-cron.sh`, `trade_files.py`, and related scripts. Shared module rules apply — run broad tests after changes.

**Goal:** Ensure trade logs uploaded to S3 are complete, consistent, and complementary to the improvements in Plans 1-8. Close data gaps that cause orphan trades, missing fields, and divergent local/remote state. Make S3 the reliable single source of truth for cross-machine development and disaster recovery.

**Architecture:** Three layers: (1) Pre-upload validation that blocks sync if data is incomplete, (2) Sync scope expansion to cover new metrics/state files from Plans 1-8, (3) Post-download integrity checks that flag stale or missing data on the receiving end.

**Tech Stack:** Bash (s3-sync.sh), Python 3 (validation scripts), AWS CLI

**Current State:**
- S3 sync uses `--exclude=* --include=<whitelist>` strategy — safe but incomplete
- 10 canonical trade logs (via `trade_files.py`) — ALL included in sync filters
- Decision logs (`*-decisions.json`), state files (`health-state.json`, `allocator-state.json`, `scan-summaries.json`) — synced
- Bot log files (`data/logs/*.log`) — synced
- `config/calibration.json` — synced via separate `aws s3 cp`
- `deposits.json` — NOT synced (needed for snapshot P&L calculation)
- New metrics files from Plans 2-8 (`data/{bot}-metrics.json`) — NOT synced yet
- New state files (`correlation-state.json`, `regime-state.json`, `pf-state-crypto.json`) — NOT synced yet
- No pre-upload validation — corrupt or empty trade logs get synced silently
- No post-download freshness check — stale data goes undetected
- `--size-only` download strategy assumes larger file = newer, but truncation/corruption breaks this
- Upload verify step hardcodes 12 filenames instead of using `trade_files.py`
- 34 orphan API settlements (from zombie processes) prove trade log completeness is not guaranteed

---

### Task 9.1: Add Pre-Upload Trade Log Validation

**Files:**
- Create: `scripts/validate-sync-data.py`
- Modify: `scripts/s3-sync.sh` (cmd_upload function)

**Context:** Currently `s3-sync.sh` uploads whatever is in `data/` without checking quality. An empty, corrupt, or truncated trade log can overwrite good data on S3. After the 34-orphan incident, we need a gate that ensures trade logs are healthy before uploading.

**Step 1: Write validation script**

```python
#!/usr/bin/env python3
"""Pre-upload validation for trade logs and state files.

Exit code 0 = all checks pass, safe to sync.
Exit code 1 = validation failures found, block upload.

Checks:
1. Every canonical trade file is valid JSON (or empty list [])
2. No trade log has shrunk since last sync (possible truncation)
3. financial-snapshot.json exists and is <24h old (forces fresh snapshot before sync)
4. No trade records with null source_bot (unattributed trades)
5. Reconciliation coverage: warn if >10% of settled trades lack settlement_result
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

# Import canonical trade file list
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))
from trade_files import TRADE_FILES, ALL_TRADE_PATHS

SIZES_CACHE = DATA_DIR / ".sync-sizes.json"

def load_previous_sizes():
    if SIZES_CACHE.exists():
        try:
            return json.loads(SIZES_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}

def save_current_sizes(sizes):
    SIZES_CACHE.write_text(json.dumps(sizes, indent=2))

def validate():
    errors = []
    warnings = []
    prev_sizes = load_previous_sizes()
    curr_sizes = {}

    # Check 1 & 2: Trade logs valid JSON and not shrunk
    for tf in TRADE_FILES:
        path = DATA_DIR / tf["filename"]
        if not path.exists():
            warnings.append(f"Trade log missing (may be new bot): {tf['filename']}")
            continue

        size = path.stat().st_size
        curr_sizes[tf["filename"]] = size

        # Validate JSON
        try:
            trades = json.loads(path.read_text())
            if not isinstance(trades, list):
                errors.append(f"{tf['filename']}: root is {type(trades).__name__}, expected list")
                continue
        except json.JSONDecodeError as e:
            errors.append(f"{tf['filename']}: corrupt JSON — {e}")
            continue

        # Check for shrinkage (possible truncation)
        prev = prev_sizes.get(tf["filename"], 0)
        if prev > 0 and size < prev * 0.8:  # >20% shrinkage is suspicious
            errors.append(
                f"{tf['filename']}: shrunk from {prev} to {size} bytes "
                f"({(1 - size/prev)*100:.0f}% smaller) — possible truncation"
            )

        # Check 4: source_bot attribution
        missing_bot = sum(1 for t in trades if not t.get("source_bot"))
        if missing_bot > 0:
            warnings.append(
                f"{tf['filename']}: {missing_bot}/{len(trades)} trades missing source_bot"
            )

        # Check 5: Reconciliation coverage
        settled = [t for t in trades if t.get("settlement_result") is not None]
        unreconciled = [t for t in trades
                        if t.get("settlement_result") is None
                        and t.get("status") == "executed"]
        if len(trades) > 0 and len(unreconciled) > len(trades) * 0.5:
            warnings.append(
                f"{tf['filename']}: {len(unreconciled)}/{len(trades)} trades unreconciled "
                f"— consider running npm run reconcile before sync"
            )

    # Check 3: Snapshot freshness
    snapshot_path = DATA_DIR / "financial-snapshot.json"
    if not snapshot_path.exists():
        warnings.append("financial-snapshot.json missing — run npm run snapshot before sync")
    else:
        try:
            snap = json.loads(snapshot_path.read_text())
            gen = snap.get("generated_at", "")
            if gen:
                gen_dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600
                if age_hours > 24:
                    warnings.append(
                        f"financial-snapshot.json is {age_hours:.0f}h old "
                        f"— run npm run snapshot for fresh data"
                    )
        except (json.JSONDecodeError, ValueError):
            warnings.append("financial-snapshot.json: could not parse generated_at")

    # Print results
    if warnings:
        for w in warnings:
            print(f"WARNING: {w}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        print(f"\n{len(errors)} error(s) found — upload blocked. Fix issues and retry.")
        return 1

    # Save sizes for next comparison
    save_current_sizes(curr_sizes)
    print(f"Validation passed: {len(curr_sizes)} trade logs checked, "
          f"{len(warnings)} warning(s)")
    return 0

if __name__ == "__main__":
    sys.exit(validate())
```

**Step 2: Integrate into s3-sync.sh upload**

In `cmd_upload()`, add validation gate before the `aws s3 sync` call:

```bash
cmd_upload() {
  acquire_lock
  echo "Uploading trade data to s3://${BUCKET}..."

  # Pre-upload validation
  echo "Running pre-upload validation..."
  if ! python3 "$PROJECT_DIR/scripts/validate-sync-data.py"; then
    echo "Upload blocked by validation. Use --force to override."
    if [ "${2:-}" != "--force" ]; then
      exit 1
    fi
    echo "WARNING: --force flag set, uploading despite validation failures"
  fi

  # ... existing sync commands ...
}
```

**Step 3: Write tests**

```python
def test_validate_catches_corrupt_json(tmp_path):
    trade_file = tmp_path / "kalshi-trades.json"
    trade_file.write_text("{invalid json")
    # Validation should fail
    assert validate_file(trade_file) is False

def test_validate_catches_shrinkage(tmp_path):
    sizes = {"kalshi-trades.json": 10000}
    trade_file = tmp_path / "kalshi-trades.json"
    trade_file.write_text("[]")  # 2 bytes vs 10000 previous
    assert detect_shrinkage("kalshi-trades.json", 2, sizes) is True
```

**Step 4: Commit**

```bash
git add scripts/validate-sync-data.py scripts/s3-sync.sh tests/test_sync_validation.py
git commit -m "feat(sync): add pre-upload trade log validation gate"
```

---

### Task 9.2: Expand Sync Scope for Plan 1-8 Outputs

**Files:**
- Modify: `scripts/s3-sync.sh` (sync_filters function)

**Context:** Plans 1-8 introduce new data files that are NOT currently synced. These need to be included so the development MacBook has access to production metrics, and vice versa. Without syncing these, the development machine has stale or missing observability data.

**New files to sync (from plan improvements):**

| File | Written By | Plan | Purpose |
|------|-----------|------|---------|
| `data/weather-metrics.json` | weather-bot | Plan 2 | Per-scan metrics (Brier, edges, forecasts) |
| `data/crypto-metrics.json` | crypto-bot | Plan 3 | Per-scan metrics (vol, regime, Kelly) |
| `data/economics-metrics.json` | economics-bot | Plan 4 | Nowcast accuracy, concentration |
| `data/strategy-metrics.json` | strategy-trader | Plan 5 | Longshot edge, CPU, scan timing |
| `data/source-monitor-metrics.json` | source-monitor | Plan 6 | NWS accuracy, data freshness |
| `data/position-monitor-metrics.json` | position-monitor | Plan 8 | Exit types (TP/SL/shift/stale) |
| `data/correlation-state.json` | correlation_engine | Plan 1 | Cross-bot correlation estimates |
| `data/regime-state.json` | regime_detector | Plan 3 | Regime classification state |
| `data/pf-state-crypto.json` | particle_filter | Plan 3 | Particle filter beliefs |
| `data/deposits.json` | Manual | — | Account deposits for P&L calc |
| `data/crypto-price-history.json` | crypto-bot | Plan 3 | Historical price snapshots |
| `data/weather-verification.json` | weather-bot | Plan 2 | Forecast accuracy tracking |
| `data/nowcast-history.json` | economics-bot | Plan 4 | Nowcast vs actual tracking |
| `config/bayes-params.json` | calibrate | Plan 4 | Bayesian prior parameters |

**Step 1: Update sync_filters()**

```bash
sync_filters() {
  echo "--exclude=*"

  # ── Trade logs (golden records) ──
  echo "--include=kalshi-*-trades.json"
  echo "--include=kalshi-trades.json"
  echo "--include=beatrelease-trades.json"
  echo "--include=beatrelease-state.json"

  # ── Analytics & snapshots ──
  echo "--include=backtest-results.json"
  echo "--include=performance-metrics.json"
  echo "--include=financial-snapshot.json"
  echo "--include=deposits.json"

  # ── Per-bot metrics (Plans 2-8) ──
  echo "--include=*-metrics.json"

  # ── Observability state ──
  echo "--include=health-state.json"
  echo "--include=allocator-state.json"
  echo "--include=scan-summaries.json"
  echo "--include=circuit-breaker-state.json"
  echo "--include=correlation-state.json"
  echo "--include=regime-state.json"
  echo "--include=pf-state-crypto.json"

  # ── Decision logs ──
  echo "--include=*-decisions.json"

  # ── Model tracking ──
  echo "--include=weather-verification.json"
  echo "--include=nowcast-history.json"
  echo "--include=crypto-price-history.json"

  # ── Caches (useful for debugging, not critical) ──
  echo "--include=econ-nowcast-cache.json"
  echo "--include=macro-cache.json"
}
```

**Step 2: Add config/bayes-params.json to config sync**

```bash
# In cmd_upload(), after calibration.json sync:
if [ -f "$PROJECT_DIR/config/bayes-params.json" ]; then
  aws s3 cp "$PROJECT_DIR/config/bayes-params.json" "s3://${BUCKET}/config/bayes-params.json"
fi

# In cmd_download(), after calibration.json sync:
aws s3 cp "s3://${BUCKET}/config/bayes-params.json" "$PROJECT_DIR/config/bayes-params.json" 2>/dev/null || true
```

**Step 3: Commit**

```bash
git add scripts/s3-sync.sh
git commit -m "feat(sync): expand sync scope for Plan 1-8 metrics, state, and tracking files"
```

---

### Task 9.3: Replace Hardcoded Upload Verify List with trade_files.py

**Files:**
- Modify: `scripts/s3-sync.sh` (cmd_upload verify section, lines 95-104)

**Context:** The upload verification loop hardcodes 12 filenames to check after upload. This duplicates and diverges from the canonical `trade_files.py` list (10 trade logs). When new bots are added or files are renamed, the verify list goes stale silently. The current list includes `health-state.json` and `scan-summaries.json` (not trade logs), but is missing nothing critical. However, the approach is fragile.

**Step 1: Generate verify list from trade_files.py**

Replace the hardcoded list with a dynamic one:

```bash
# Verify key files by comparing local vs remote sizes
echo "Verifying upload..."
verify_files=$(python3 -c "
import sys; sys.path.insert(0, '$PROJECT_DIR/src/kalshi')
from trade_files import TRADE_FILES
for tf in TRADE_FILES:
    print(tf['filename'])
# Also verify non-trade critical files
for f in ['health-state.json', 'scan-summaries.json', 'financial-snapshot.json']:
    print(f)
")

for f in $verify_files; do
  if [ -f "$PROJECT_DIR/data/$f" ]; then
    local_size=$(stat -f%z "$PROJECT_DIR/data/$f" 2>/dev/null || stat -c%s "$PROJECT_DIR/data/$f" 2>/dev/null || echo 0)
    remote_info=$(aws s3 ls "s3://${BUCKET}/data/$f" 2>/dev/null || true)
    remote_size=$(echo "$remote_info" | awk '{print $3}')
    if [ -n "$remote_size" ] && [ "$local_size" != "$remote_size" ]; then
      echo "WARNING: Size mismatch for $f (local=$local_size, remote=$remote_size)"
    fi
  fi
done
```

**Step 2: Commit**

```bash
git add scripts/s3-sync.sh
git commit -m "fix(sync): generate upload verify list from trade_files.py instead of hardcoded list"
```

---

### Task 9.4: Add Post-Download Integrity Report

**Files:**
- Create: `scripts/check-sync-health.py`
- Modify: `scripts/s3-sync.sh` (cmd_download function)

**Context:** After downloading from S3, there's no check that the data is complete or fresh. A developer pulling on the MacBook has no way to know if trade logs are hours or days stale, if reconciliation was run, or if the snapshot reflects current API state.

**Step 1: Write integrity check script**

```python
#!/usr/bin/env python3
"""Post-download data integrity report.

Prints a summary of data freshness, completeness, and reconciliation status.
Non-blocking — always exits 0. Purely informational.
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))
from trade_files import TRADE_FILES

def check():
    print("=" * 60)
    print("POST-DOWNLOAD DATA INTEGRITY REPORT")
    print("=" * 60)

    # Trade log summary
    total_trades = 0
    total_reconciled = 0
    total_unreconciled = 0
    missing_source_bot = 0

    for tf in TRADE_FILES:
        path = DATA_DIR / tf["filename"]
        if not path.exists():
            print(f"  {tf['label']:25s}  MISSING")
            continue
        try:
            trades = json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"  {tf['label']:25s}  CORRUPT JSON")
            continue

        n = len(trades)
        settled = sum(1 for t in trades if t.get("settlement_result"))
        unsettled = sum(1 for t in trades
                        if not t.get("settlement_result")
                        and t.get("status") == "executed")
        no_bot = sum(1 for t in trades if not t.get("source_bot"))
        has_edge = sum(1 for t in trades if t.get("edge_at_entry") is not None)
        has_model_fv = sum(1 for t in trades if t.get("model_fair_value_cents") is not None)

        total_trades += n
        total_reconciled += settled
        total_unreconciled += unsettled
        missing_source_bot += no_bot

        # Freshness: most recent trade timestamp
        if trades:
            latest = max(t.get("timestamp", "") for t in trades)
            print(f"  {tf['label']:25s}  {n:4d} trades  "
                  f"{settled:3d} settled  {unsettled:3d} pending  "
                  f"latest={latest[:16]}")
        else:
            print(f"  {tf['label']:25s}  empty")

    print(f"\n  TOTAL: {total_trades} trades, "
          f"{total_reconciled} reconciled, "
          f"{total_unreconciled} pending reconciliation")

    if missing_source_bot > 0:
        print(f"  WARNING: {missing_source_bot} trades missing source_bot attribution")

    # Snapshot freshness
    print()
    snap_path = DATA_DIR / "financial-snapshot.json"
    if snap_path.exists():
        try:
            snap = json.loads(snap_path.read_text())
            gen = snap.get("generated_at", "unknown")
            nav = snap.get("account", {}).get("nav_cents", 0)
            pnl = snap.get("realized_pnl", {}).get("net_after_fees_cents", 0)
            orphans = len(snap.get("verification", {}).get("orphan_settlements", []))
            print(f"  Snapshot: generated={gen}")
            print(f"  NAV: ${nav/100:.2f}  |  Net P&L: ${pnl/100:.2f}  |  Orphans: {orphans}")
        except (json.JSONDecodeError, ValueError):
            print("  Snapshot: CORRUPT")
    else:
        print("  Snapshot: MISSING — run npm run snapshot")

    # Calibration freshness
    cal_path = PROJECT_DIR / "config" / "calibration.json"
    if cal_path.exists():
        try:
            cal = json.loads(cal_path.read_text())
            gen = cal.get("generated", "unknown")
            setts = cal.get("total_settlements", "?")
            print(f"  Calibration: generated={gen}, settlements={setts}")
        except json.JSONDecodeError:
            print("  Calibration: CORRUPT")
    else:
        print("  Calibration: MISSING — run npm run calibrate")

    # Per-bot metrics files
    print()
    metrics_files = list(DATA_DIR.glob("*-metrics.json"))
    if metrics_files:
        print(f"  Bot metrics files: {len(metrics_files)}")
        for mf in sorted(metrics_files):
            try:
                data = json.loads(mf.read_text())
                if isinstance(data, list) and data:
                    latest = data[-1].get("timestamp", "?")
                    print(f"    {mf.name}: {len(data)} entries, latest={latest[:16]}")
                elif isinstance(data, dict):
                    print(f"    {mf.name}: dict with {len(data)} keys")
            except json.JSONDecodeError:
                print(f"    {mf.name}: CORRUPT")
    else:
        print("  Bot metrics files: none (Plans 2-8 not yet implemented)")

    # Health state
    health_path = DATA_DIR / "health-state.json"
    if health_path.exists():
        try:
            h = json.loads(health_path.read_text())
            bots = h.get("bots", {})
            if bots:
                print()
                print(f"  Bot heartbeats ({len(bots)}):")
                for name, info in sorted(bots.items()):
                    hb = info.get("last_heartbeat", "?")
                    print(f"    {name:20s}  {hb[:19]}")
        except json.JSONDecodeError:
            pass

    # Recommendations
    print()
    recs = []
    if total_unreconciled > 10:
        recs.append("Run `npm run reconcile` to annotate settled trades")
    if not snap_path.exists():
        recs.append("Run `npm run snapshot` for verified P&L")
    if missing_source_bot > 0:
        recs.append(f"Fix {missing_source_bot} trades missing source_bot")

    if recs:
        print("  RECOMMENDATIONS:")
        for r in recs:
            print(f"    - {r}")
    else:
        print("  All checks OK.")

    print("=" * 60)

if __name__ == "__main__":
    check()
```

**Step 2: Integrate into s3-sync.sh download**

At the end of `cmd_download()`:

```bash
cmd_download() {
  # ... existing download commands ...

  echo "Download complete."

  # Post-download integrity report
  echo ""
  python3 "$PROJECT_DIR/scripts/check-sync-health.py" || true
}
```

**Step 3: Add npm script**

In `package.json`:

```json
"sync:check": "python3 scripts/check-sync-health.py"
```

**Step 4: Commit**

```bash
git add scripts/check-sync-health.py scripts/s3-sync.sh package.json
git commit -m "feat(sync): add post-download data integrity report"
```

---

### Task 9.5: Add Reconcile-Before-Sync to Cron Workflow

**Files:**
- Modify: `scripts/sync-up-cron.sh`

**Context:** The cron job (`sync-up-cron.sh`) currently just runs `npm run sync:up`. This uploads whatever is on disk, even if trades haven't been reconciled with the API. Since reconciliation adds `settlement_result`, `fill_price_cents`, and `realized_edge` to trade records, running it before sync ensures S3 always has the most complete version of each trade record.

The cron workflow should be: snapshot → reconcile → validate → sync.

**Step 1: Expand cron script**

```bash
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export SHELL="/bin/bash"
cd /Users/andeslee/Documents/cursor-projects/kalshi-trading

# Load env vars for API access
set -a
source .env 2>/dev/null || true
set +a

LOG="data/logs/sync-cron.log"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) — sync-up-cron starting" >> "$LOG"

# Step 1: Refresh P&L snapshot (captures current API state)
echo "$(date -u +%T) Running snapshot..." >> "$LOG"
/opt/homebrew/bin/npm run snapshot >> "$LOG" 2>&1 || true

# Step 2: Reconcile trades (annotate with settlement/fill data)
echo "$(date -u +%T) Running reconcile..." >> "$LOG"
/opt/homebrew/bin/npm run reconcile >> "$LOG" 2>&1 || true

# Step 3: Upload to S3 (includes pre-upload validation)
echo "$(date -u +%T) Running sync:up..." >> "$LOG"
/opt/homebrew/bin/npm run sync:up >> "$LOG" 2>&1

echo "$(date -u +%T) sync-up-cron complete" >> "$LOG"
```

**Step 2: Commit**

```bash
git add scripts/sync-up-cron.sh
git commit -m "feat(sync): add snapshot + reconcile before S3 upload in cron"
```

---

### Task 9.6: Add Trade Record Field Completeness Standards

**Files:**
- Create: `docs/trade-record-schema.md`
- Modify: `src/kalshi/kalshi_auth.py` (add field presence warnings)

**Context:** Plans 1-8 add new fields to trade records: `edge_at_entry`, `model_fair_value_cents`, `model_inputs`, `slippage_cents`, `order_price_cents`. Legacy trades won't have these. The system needs to know which fields are expected vs optional, and the sync validation (Task 9.1) needs to check field completeness for NEW trades (not legacy).

**Step 1: Document trade record schema**

```markdown
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

## Settlement Fields (added by reconcile-trades.py)

| Field | Type | Source |
|-------|------|--------|
| settlement_result | "won" or "lost" | reconcile |
| settlement_revenue_cents | int | reconcile |
| fill_price_cents | int (if not set at trade time) | reconcile |
| realized_edge | float | reconcile |
```

**Step 2: Add field completeness log to TradeManager**

In `_build_golden_record()`, after building the record, log a warning if expected fields are missing:

```python
# After building record, check field completeness
_expected = {"model_prob", "raw_edge", "source_bot", "model_inputs"}
missing = _expected - set(k for k, v in record.items() if v is not None)
if missing:
    self.log.debug(f"Trade record missing expected fields: {missing}")
```

This is debug-level, not a blocker — it just creates an audit trail for improving field coverage over time.

**Step 3: Commit**

```bash
git add docs/trade-record-schema.md src/kalshi/kalshi_auth.py
git commit -m "docs(schema): document trade record field standards; add completeness logging"
```

---

### Task 9.7: Fix Download --size-only Weakness

**Files:**
- Modify: `scripts/s3-sync.sh` (cmd_download function)

**Context:** The download uses `--size-only` to prevent overwriting newer local files. The assumption is "larger file = more trades = newer." But this breaks in two cases:
1. **Truncation:** A crash or disk issue creates a smaller (corrupt) file that gets uploaded, and then `--size-only` won't overwrite it on the other machine because it's a different size (it downloads the corrupt version).
2. **Reconciliation:** Running `npm run reconcile` adds fields to existing records, making the file larger without adding new trades. The next sync could push this reconciled version to S3, and a `--size-only` download on the other machine would see the size difference and overwrite the local copy — which is the correct behavior here, but the inverse (where the un-reconciled machine's larger file overwrites the reconciled one) could lose reconciliation data.

The safer approach: use file modification timestamps instead of size-only.

**Step 1: Replace --size-only with timestamp-based sync**

```bash
cmd_download() {
  acquire_lock
  echo "Downloading trade data from s3://${BUCKET}..."

  # For trade logs: use --exact-timestamps to only download if remote is newer
  # This is safer than --size-only for files that grow via reconciliation
  aws s3 sync "s3://${BUCKET}/data/" "$PROJECT_DIR/data/" \
    $(sync_filters) --exact-timestamps

  # ... rest of download ...
}
```

Note: `--exact-timestamps` compares modification times. Since upload always happens from the production Mac Mini (via cron), and download always happens on the development MacBook, the production version is authoritative. If you need to push from dev → prod, use `sync:up` explicitly.

**Step 2: Add conflict detection**

Before downloading, check if any local trade log has been modified more recently than the remote version:

```bash
# Before download, check for local modifications that would be overwritten
echo "Checking for local changes..."
for f in $(python3 -c "
import sys; sys.path.insert(0, '$PROJECT_DIR/src/kalshi')
from trade_files import TRADE_FILES
for tf in TRADE_FILES: print(tf['filename'])
"); do
  local_file="$PROJECT_DIR/data/$f"
  if [ -f "$local_file" ]; then
    local_mod=$(stat -f%m "$local_file" 2>/dev/null || stat -c%Y "$local_file" 2>/dev/null || echo 0)
    remote_info=$(aws s3api head-object --bucket "$BUCKET" --key "data/$f" 2>/dev/null || true)
    if [ -n "$remote_info" ]; then
      remote_mod=$(echo "$remote_info" | python3 -c "
import sys,json
from datetime import datetime
info = json.load(sys.stdin)
dt = datetime.fromisoformat(info['LastModified'].replace('+00:00',''))
print(int(dt.timestamp()))
" 2>/dev/null || echo 0)
      if [ "$local_mod" -gt "$remote_mod" ] 2>/dev/null; then
        echo "WARNING: Local $f is newer than S3 — local changes will be preserved"
      fi
    fi
  fi
done
```

**Step 3: Commit**

```bash
git add scripts/s3-sync.sh
git commit -m "fix(sync): replace --size-only with --exact-timestamps for safer downloads"
```

---

### Task 9.8: Add Sync Manifest for Audit Trail

**Files:**
- Modify: `scripts/s3-sync.sh` (cmd_upload function)

**Context:** There's no record of WHEN a sync happened, WHAT was uploaded, or whether validation passed. Adding a sync manifest creates an audit trail that helps debug data inconsistencies (like the 34-orphan incident).

**Step 1: Generate and upload sync manifest**

At the end of `cmd_upload()`, after all syncs complete:

```bash
# Generate sync manifest
echo "Generating sync manifest..."
python3 -c "
import json, sys, os
from pathlib import Path
from datetime import datetime, timezone

proj = Path('$PROJECT_DIR')
data = proj / 'data'
sys.path.insert(0, str(proj / 'src' / 'kalshi'))
from trade_files import TRADE_FILES

manifest = {
    'sync_time': datetime.now(timezone.utc).isoformat(),
    'hostname': '$(hostname)',
    'direction': 'upload',
    'trade_logs': {},
    'state_files': {},
}

for tf in TRADE_FILES:
    p = data / tf['filename']
    if p.exists():
        try:
            trades = json.loads(p.read_text())
            manifest['trade_logs'][tf['filename']] = {
                'size_bytes': p.stat().st_size,
                'trade_count': len(trades),
                'latest_timestamp': max((t.get('timestamp','') for t in trades), default=''),
                'reconciled_count': sum(1 for t in trades if t.get('settlement_result')),
            }
        except json.JSONDecodeError:
            manifest['trade_logs'][tf['filename']] = {'error': 'corrupt JSON'}

for sf in ['health-state.json', 'financial-snapshot.json', 'allocator-state.json',
           'scan-summaries.json', 'circuit-breaker-state.json']:
    p = data / sf
    if p.exists():
        manifest['state_files'][sf] = {
            'size_bytes': p.stat().st_size,
            'modified': datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
        }

print(json.dumps(manifest, indent=2))
" > "$PROJECT_DIR/data/.sync-manifest.json"

aws s3 cp "$PROJECT_DIR/data/.sync-manifest.json" "s3://${BUCKET}/data/.sync-manifest.json"

# Append to sync history (last 100 entries)
aws s3 cp "s3://${BUCKET}/data/.sync-history.jsonl" "$PROJECT_DIR/data/.sync-history.jsonl" 2>/dev/null || true
cat "$PROJECT_DIR/data/.sync-manifest.json" >> "$PROJECT_DIR/data/.sync-history.jsonl"
tail -100 "$PROJECT_DIR/data/.sync-history.jsonl" > "$PROJECT_DIR/data/.sync-history-trimmed.jsonl"
mv "$PROJECT_DIR/data/.sync-history-trimmed.jsonl" "$PROJECT_DIR/data/.sync-history.jsonl"
aws s3 cp "$PROJECT_DIR/data/.sync-history.jsonl" "s3://${BUCKET}/data/.sync-history.jsonl"
```

**Step 2: Commit**

```bash
git add scripts/s3-sync.sh
git commit -m "feat(sync): add sync manifest and history for audit trail"
```

---

### Task 9.9: Add npm run sync:full Workflow Command

**Files:**
- Modify: `package.json`
- Create: `scripts/sync-full.sh`

**Context:** The full recommended sync workflow is: snapshot → reconcile → validate → upload. Currently users must run 3-4 separate commands. A single `npm run sync:full` command ensures the correct order and nothing is forgotten.

**Step 1: Create full sync script**

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== Full Sync Workflow ==="
echo ""

# Step 1: Fresh P&L snapshot
echo "[1/4] Running P&L snapshot..."
python3 scripts/pnl-snapshot.py
echo ""

# Step 2: Reconcile trades with API settlements/fills
echo "[2/4] Running trade reconciliation..."
python3 scripts/reconcile-trades.py
echo ""

# Step 3: Pre-upload validation
echo "[3/4] Validating data quality..."
python3 scripts/validate-sync-data.py
echo ""

# Step 4: Upload to S3
echo "[4/4] Uploading to S3..."
bash scripts/s3-sync.sh upload
echo ""

echo "=== Full sync complete ==="
```

**Step 2: Add npm scripts**

```json
"sync:full": "bash scripts/sync-full.sh",
"sync:check": "python3 scripts/check-sync-health.py"
```

**Step 3: Commit**

```bash
git add scripts/sync-full.sh package.json
git commit -m "feat(sync): add npm run sync:full workflow (snapshot → reconcile → validate → upload)"
```

---

### Task 9.10: Update CLAUDE.md Data Sync Section

**Files:**
- Modify: `CLAUDE.md` (Data Sync section)

**Context:** CLAUDE.md documents the sync workflow but doesn't mention validation, reconciliation, the full workflow, or the new metrics files. Update to reflect the improved pipeline.

**Step 1: Update Data Sync section**

Replace the existing Data Sync section with:

```markdown
## Data Sync

Trade logs in `data/` are gitignored but essential for backtesting and auditing. S3 syncs them between machines (Mac Mini production → MacBook development).

```bash
npm run sync:setup   # Create S3 bucket (one-time)
npm run sync:full    # RECOMMENDED: snapshot → reconcile → validate → upload
npm run sync:up      # Upload only (with pre-upload validation)
npm run sync:down    # Pull from S3 (with post-download integrity report)
npm run sync:check   # Data integrity report without syncing
```

Requires AWS CLI configured with credentials. Set `S3_BUCKET` in `.env` (defaults to `kalshi-trading-logs`).

**Synced (trade logs):** All files in `trade_files.py` — `kalshi-*-trades.json`, `beatrelease-trades.json`.
**Synced (analytics):** `financial-snapshot.json`, `backtest-results.json`, `performance-metrics.json`, `deposits.json`.
**Synced (observability):** `health-state.json`, `allocator-state.json`, `scan-summaries.json`, `circuit-breaker-state.json`, `*-decisions.json`, `*-metrics.json`.
**Synced (model state):** `correlation-state.json`, `regime-state.json`, `pf-state-crypto.json`, `weather-verification.json`, `nowcast-history.json`, `crypto-price-history.json`.
**Synced (config):** `config/calibration.json`, `config/bayes-params.json`.
**Synced (logs):** `data/logs/*.log`.
**Excluded:** `data/pids/`, `data/HALT_TRADING`, market caches, demo trades, source snapshots, temp files.

**Pre-upload validation** blocks sync if trade logs are corrupt, truncated, or missing `source_bot`. **Post-download report** shows trade counts, reconciliation status, snapshot freshness, and bot heartbeats. **Sync manifest** logs every upload with file sizes, trade counts, and timestamps for audit trail.

**Cron:** `scripts/sync-up-cron.sh` runs the full workflow (snapshot → reconcile → upload) on schedule.
```

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): update Data Sync section for new validation and sync:full workflow"
```

---

## Dependency Map

```
Task 9.1 (validation script) ──┐
Task 9.2 (expand sync scope)   ├── can be parallel
Task 9.3 (verify from trade_files) ┘
                                │
Task 9.4 (post-download check) ── independent
                                │
Task 9.5 (cron workflow) ─── depends on 9.1 (validation exists)
                                │
Task 9.6 (field schema) ─── depends on Plan 1 (new fields defined)
                                │
Task 9.7 (fix --size-only) ── independent
                                │
Task 9.8 (sync manifest) ─── depends on 9.3 (uses trade_files)
                                │
Task 9.9 (sync:full cmd) ─── depends on 9.1, 9.4, 9.5
                                │
Task 9.10 (CLAUDE.md) ─── depends on all above (documents final state)
```

---

## Measurement Protocol

| Metric | Before | Target | Method |
|--------|--------|--------|--------|
| Pre-upload validation | None | Blocks on corrupt/truncated logs | `validate-sync-data.py` exit code |
| Sync scope | 15 file patterns | 25+ patterns (all metrics, state, tracking) | `sync_filters()` count |
| Upload verify source | Hardcoded 12 files | Dynamic from `trade_files.py` | Code inspection |
| Post-download reporting | None | Full integrity report | `check-sync-health.py` output |
| Cron workflow | Upload only | Snapshot → reconcile → validate → upload | `sync-up-cron.sh` |
| Trade record completeness | 11 required fields | 11 required + 6 expected (logged) | Validation warnings |
| Download safety | `--size-only` (fragile) | `--exact-timestamps` | Sync behavior |
| Sync audit trail | None | Per-upload manifest + history | `.sync-manifest.json` |
| Full sync command | 3-4 manual steps | `npm run sync:full` | One command |
| CLAUDE.md accuracy | Missing validation/metrics | Complete sync documentation | Docs review |

---

## Execution Report (2026-03-07)

**Status:** Complete

**Tasks completed:** 10/10

**Summary:** Pre-upload validation, expanded sync scope, dynamic verify, integrity report, cron pipeline, schema docs, timestamp fix, sync manifest, sync:full command, CLAUDE.md updated.

**Backtest results (post-implementation):**
- Data pipeline now validates all trade records before S3 upload
- Sync manifest provides audit trail for every upload
- `npm run sync:full` consolidates 3-4 manual steps into one command

**Next steps:** None. All data quality and S3 sync tasks complete.
