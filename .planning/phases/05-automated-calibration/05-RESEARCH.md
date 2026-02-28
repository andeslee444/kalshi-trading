# Phase 5: Automated Calibration - Research

**Researched:** 2026-02-28
**Domain:** Pipeline orchestration, drift detection, calibration suggestion workflow
**Confidence:** HIGH

## Summary

Phase 5 builds a daily automated pipeline that chains three existing scripts (reconcile, backtest, calibrate) into a single orchestrated workflow, adds per-bot/per-city Brier score baseline tracking, drift detection with WhatsApp alerting, and calibration suggestion generation. The existing codebase already has all the heavy computational components -- `reconcile-trades.py`, `backtest.py`, `calibrate-sigma.py`, and `daily-backtest.py` -- so this phase is primarily about orchestration, baseline management, and surfacing actionable suggestions.

The key insight is that `daily-backtest.py` already implements a simple version of the drift detection loop (run backtest, compare to previous, alert on >10% degradation), but it operates only at the aggregate level and overwrites previous results. The new pipeline needs per-bot AND per-city baselines, timestamped history, calibration suggestion generation, and a daily summary message regardless of drift status.

**Primary recommendation:** Build a single `scripts/calibration-pipeline.py` that orchestrates the full daily cycle, manages baselines in `data/calibration-baselines.json`, writes timestamped logs to `data/logs/calibration/`, generates suggestions to `data/calibration-suggestions/`, and sends WhatsApp summaries. Trigger via cron (extending the existing `setup-cron.sh` pattern).

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Run daily at 6 AM (before markets open, catches overnight settlements)
- Each run writes a timestamped log to `data/logs/calibration/` with stage results, timing, and any drift findings
- Full details in every log (not just on issues)
- WhatsApp summary sent every day -- both "all models healthy" and "drift detected" messages
- Summary-only format: which bot/model drifted, by how much (e.g., "Weather Brier: 0.18->0.22, +22%"), one-liner recommendation
- No cooldown -- alert fires every pipeline run as long as drift persists
- Daily healthy summary confirms the pipeline ran successfully
- Write suggestion to a JSON file (e.g., `data/calibration-suggestions/calibration-suggestion-2026-02-28.json`)
- Keep timestamped history of all suggestions (never overwrite)
- Suggestion includes proposed params, improvement metrics, and diff from current calibration
- Per-bot baselines for all bots (entertainment, economics, strategy, etc.)
- Per-bot + per-city baselines for weather (Miami, NYC, etc.) -- catches city-specific model degradation
- Other bots track at bot level only

### Claude's Discretion
- Pipeline trigger mechanism (cron vs supervisor-managed vs standalone script)
- Failure handling between pipeline stages (retry/skip/abort strategy)
- Severity tiers for drift (single 10% threshold vs multi-tier)
- Suggestion criteria (Brier-only vs Brier + P&L impact)
- Apply mechanism for accepted suggestions (manual copy vs apply script)
- Baseline initialization approach (first run vs manual snapshot)
- Baseline update timing (after calibration vs rolling window)
- Minimum sample size before drift detection activates

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| CAL-01 | Daily pipeline runs reconcile -> backtest -> compare Brier scores -> alert on >10% degradation | Pipeline orchestration pattern, existing scripts as subprocess stages, baseline management, drift detection logic |
| CAL-02 | Calibration auto-suggests new sigma parameters when improvement detected (human approves) | Calibrate-sigma.py already computes optimal params; pipeline compares current vs proposed Brier, writes timestamped suggestion JSON |
| CAL-03 | Drift detection alerts via WhatsApp when any model's Brier score degrades >10% from baseline | Per-bot/per-city baseline tracking, `notify_whatsapp()` from kalshi_auth, daily summary format |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Python 3 stdlib | 3.x | subprocess, json, datetime, pathlib, argparse, logging | Already used by all scripts; no new dependencies needed |
| `kalshi_auth` (internal) | N/A | `notify_whatsapp()`, `setup_logging()`, `_atomic_write_json()`, `PROJECT_DIR` | Existing shared module used by every bot and script |
| `trade_files` (internal) | N/A | `TRADE_FILES`, `ALL_TRADE_PATHS` | Canonical trade file list already used by reconcile/backtest/calibrate |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `subprocess` (stdlib) | N/A | Run reconcile/backtest/calibrate as child processes | Pipeline stage execution |
| `shutil` (stdlib) | N/A | File backup operations | Baseline backup before update |
| `os` / `tempfile` (stdlib) | N/A | Atomic file writes | Already patterned in `_atomic_write_json()` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| subprocess orchestration | Direct Python function imports | Subprocess is safer (isolated failures, timeout control, matches existing `daily-backtest.py` pattern); function imports would couple the pipeline tightly to script internals |
| cron trigger | Supervisor-managed scheduled task | Cron is simpler, already used for S3 sync; supervisor is for long-running daemons, not daily one-shot tasks |
| JSON baselines file | SQLite | JSON is consistent with all other state files in `data/`; SQLite is v2 per REQUIREMENTS.md |

**Installation:**
```bash
# No new dependencies required -- pure Python stdlib + existing internal modules
```

## Architecture Patterns

### Recommended Project Structure
```
scripts/
    calibration-pipeline.py      # NEW: main pipeline orchestrator
    calibrate-sigma.py           # EXISTING: grid-search calibration
    backtest.py                  # EXISTING: Brier score computation
    reconcile-trades.py          # EXISTING: settlement annotation
    backfill-settlements.py      # EXISTING: market endpoint backfill
    daily-backtest.py            # EXISTING: simple drift detection (superseded by pipeline)
    setup-cron.sh                # EXISTING: cron installer (extend for pipeline)
data/
    calibration-baselines.json   # NEW: per-bot + per-city Brier score baselines
    calibration-suggestions/     # NEW: timestamped suggestion files
        calibration-suggestion-2026-02-28.json
    logs/calibration/            # NEW: timestamped pipeline run logs
        pipeline-2026-02-28.json
    backtest-results.json        # EXISTING: latest backtest output
config/
    calibration.json             # EXISTING: active calibration params
    calibration-backup.json      # EXISTING: backup before overwrite
```

### Pattern 1: Subprocess Pipeline with Stage Isolation
**What:** Each pipeline stage runs as a subprocess with timeout, capturing stdout/stderr. Failures in one stage are logged but don't necessarily abort the entire pipeline.
**When to use:** When chaining existing scripts that were designed to run independently.
**Example:**
```python
# Source: existing daily-backtest.py pattern (lines 98-109)
def run_stage(script_path, args=None, timeout=120):
    """Run a pipeline stage as subprocess. Returns (success, stdout, stderr, duration)."""
    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)
    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        duration = time.time() - start
        return result.returncode == 0, result.stdout, result.stderr, duration
    except subprocess.TimeoutExpired:
        return False, "", f"Timeout after {timeout}s", timeout

# Pipeline stages in order
stages = [
    ("reconcile", SCRIPTS / "reconcile-trades.py", [], 120),
    ("backfill", SCRIPTS / "backfill-settlements.py", [], 180),
    ("backtest", SCRIPTS / "backtest.py", ["--save"], 120),
    ("calibrate", SCRIPTS / "calibrate-sigma.py", ["--json"], 300),
]
```

### Pattern 2: Baseline Management with First-Run Initialization
**What:** Baselines stored in a single JSON file. On first run (or missing baselines), the pipeline snapshot from the current backtest results becomes the baseline. Subsequent runs compare against the stored baseline.
**When to use:** When you need to track historical reference points for drift detection.
**Example:**
```python
# Baseline structure
{
    "initialized_at": "2026-02-28T06:00:00Z",
    "last_updated": "2026-02-28T06:00:00Z",
    "aggregate": {"brier": 0.309, "n": 115},
    "per_bot": {
        "weather": {"brier": 0.330, "n": 69},
        "crypto": {"brier": 0.278, "n": 46}
    },
    "per_city": {
        "MIA": {"brier": 0.640, "n": 8},
        "NY": {"brier": 0.308, "n": 14}
    }
}
```

### Pattern 3: Drift Detection with Minimum Sample Gating
**What:** Compare current Brier scores against baselines, but only flag drift when both the baseline AND current run have sufficient sample size. Prevents false alarms from sparse data.
**When to use:** When statistical metrics can be misleading with small samples.
**Example:**
```python
MIN_SAMPLES_FOR_DRIFT = 10  # Don't flag drift with fewer than 10 evaluated trades

def check_drift(baseline_brier, current_brier, baseline_n, current_n, threshold=0.10):
    """Returns (drifted: bool, change_pct: float, reason: str)."""
    if baseline_brier is None or current_brier is None:
        return False, 0.0, "insufficient data"
    if baseline_n < MIN_SAMPLES_FOR_DRIFT or current_n < MIN_SAMPLES_FOR_DRIFT:
        return False, 0.0, f"low sample (baseline={baseline_n}, current={current_n})"
    if baseline_brier == 0:
        return False, 0.0, "baseline is zero"
    change = (current_brier - baseline_brier) / baseline_brier
    return change > threshold, change, ""
```

### Pattern 4: Timestamped Log and Suggestion Files
**What:** Every pipeline run produces a timestamped JSON log; calibration suggestions get their own timestamped files. Neither is overwritten.
**When to use:** When you need audit trail and historical analysis of pipeline behavior.
**Example:**
```python
# Log file: data/logs/calibration/pipeline-2026-02-28.json
{
    "run_at": "2026-02-28T06:00:15Z",
    "stages": {
        "reconcile": {"success": true, "duration_s": 12.3, "annotated": 5},
        "backfill": {"success": true, "duration_s": 45.1, "annotated": 2},
        "backtest": {"success": true, "duration_s": 8.7},
        "calibrate": {"success": true, "duration_s": 120.4}
    },
    "drift_analysis": {
        "aggregate": {"baseline": 0.309, "current": 0.315, "change_pct": 1.9, "drifted": false},
        "per_bot": { ... },
        "per_city": { ... }
    },
    "suggestion_generated": false,
    "whatsapp_sent": true,
    "total_duration_s": 186.5
}

# Suggestion file: data/calibration-suggestions/calibration-suggestion-2026-02-28.json
{
    "generated_at": "2026-02-28T06:02:30Z",
    "trigger": "weather global Brier improved 12% (0.330 -> 0.290)",
    "current_calibration": { ... },  # snapshot of config/calibration.json
    "proposed_calibration": { ... }, # output of calibrate-sigma.py
    "improvements": {
        "weather_global_brier": {"before": 0.330, "after": 0.290, "improvement_pct": 12.1},
        "per_city": { ... }
    },
    "recommendation": "Apply: weather sigma intercept 5.5->4.8, slope 0.2->0.3"
}
```

### Anti-Patterns to Avoid
- **Auto-applying calibration:** Explicitly out of scope per REQUIREMENTS.md. One bad auto-tune can blow up the account. Always surface suggestions for human review.
- **Overwriting baseline on every run:** Baselines should only update when calibration is explicitly accepted or at controlled intervals. A drifting model that keeps running will ratchet the baseline down if you update it on every run.
- **Running calibrate-sigma.py --save in the pipeline:** The pipeline should run calibrate in `--json` mode to get the proposed params, not `--save` which would auto-apply them.
- **Ignoring subprocess failures silently:** Each stage failure should be logged and included in the WhatsApp summary. A failed reconcile is important information.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Brier score computation | Custom metric | `backtest.py` via subprocess | Already tested, handles per-bot/per-city/per-market-type breakdown |
| Sigma grid search | Custom optimizer | `calibrate-sigma.py` via subprocess | Complex multi-objective optimization with Bayesian shrinkage already implemented |
| Settlement reconciliation | Custom API calls | `reconcile-trades.py` + `backfill-settlements.py` via subprocess | Pagination, idempotency, atomic writes all handled |
| WhatsApp notifications | Custom messaging | `notify_whatsapp()` from `kalshi_auth` | Already handles phone lookup, openclaw CLI, error handling |
| Atomic JSON writes | Custom file I/O | `_atomic_write_json()` from `kalshi_auth` | Temp file + `os.replace()` pattern prevents corruption |
| Cron scheduling | Custom scheduler | System cron via `setup-cron.sh` extension | Already proven for S3 sync; simple, reliable, OS-level |

**Key insight:** The entire computational pipeline already exists. This phase is glue code, baseline tracking, and notification formatting. Resist the urge to re-implement backtest or calibration logic inside the pipeline script.

## Common Pitfalls

### Pitfall 1: Stale Baseline Ratcheting
**What goes wrong:** If baselines auto-update after every run, a slowly degrading model never triggers an alert because each day's degradation is <10% from the previous day.
**Why it happens:** Confusing "latest result" with "known good baseline."
**How to avoid:** Baselines update ONLY when: (a) first run initializes them, or (b) a human explicitly accepts new calibration and the pipeline re-snapshots. Never update baselines automatically on detection runs.
**Warning signs:** Drift alerts never fire even though the model has degraded 30% over a month.

### Pitfall 2: False Drift Alerts from Small Samples
**What goes wrong:** A bot with 3 evaluated trades shows 50% Brier change, triggering alert spam.
**Why it happens:** Small-sample Brier scores are extremely noisy.
**How to avoid:** Gate drift detection behind minimum sample size (recommend: 10 per entity, matching calibration's MIN_TRADES_PER_CITY). Report "insufficient data" instead of drift for small samples.
**Warning signs:** Daily alerts about bots that have very few settled trades.

### Pitfall 3: Pipeline Stage Timeout Causing Partial State
**What goes wrong:** Backfill script times out after querying 80 of 100 tickers, leaving trade logs in a partially annotated state.
**Why it happens:** Backfill queries individual market endpoints with rate limiting (0.5s per 20), which can be slow.
**How to avoid:** Use generous timeouts (180s for backfill, 300s for calibrate). Existing scripts are idempotent, so partial runs are safe -- next run picks up remaining.
**Warning signs:** Backfill stage consistently times out; annotated count doesn't increase over multiple runs.

### Pitfall 4: Cron Environment Missing Dependencies
**What goes wrong:** Pipeline runs fine manually but fails under cron because PATH doesn't include Python or project dependencies.
**Why it happens:** Cron jobs run with minimal environment.
**How to avoid:** Follow existing `setup-cron.sh` pattern which explicitly sets PATH. Use absolute paths for Python and scripts. Source `.env` at start of pipeline.
**Warning signs:** Cron job shows "python3: not found" or "ModuleNotFoundError" in logs.

### Pitfall 5: Calibrate Suggestion Without Improvement Check
**What goes wrong:** Pipeline generates a calibration suggestion every day even when the proposed params are identical to current ones, or show negligible improvement.
**Why it happens:** Running calibrate always produces output; without comparison logic, every run looks like a "suggestion."
**How to avoid:** Compare proposed calibration Brier score against current calibration Brier score. Only generate suggestion when improvement exceeds a minimum threshold (recommend: 5% Brier improvement, matching the 0.001 tolerance used in calibrate-sigma.py).
**Warning signs:** data/calibration-suggestions/ fills up with identical suggestions daily.

### Pitfall 6: WhatsApp Message Too Long
**What goes wrong:** Message with all bots + all cities + all details exceeds WhatsApp message length limits or becomes unreadable.
**Why it happens:** Per-city breakdown for weather alone could be 7+ cities.
**How to avoid:** Summary-only format per user decision. Only list entities that drifted, not all entities. Aggregate healthy status into one line ("5/7 models healthy"). Keep message under 500 characters.
**Warning signs:** WhatsApp notification truncated or fails to send.

## Code Examples

Verified patterns from the existing codebase:

### Running a Script as Subprocess (from daily-backtest.py)
```python
# Source: scripts/daily-backtest.py lines 98-109
result = subprocess.run(
    [sys.executable, str(BACKTEST_SCRIPT), "--save"],
    capture_output=True, text=True, timeout=120,
)
if result.returncode != 0:
    log.error(f"Backtest failed (exit {result.returncode}): {result.stderr[:500]}")
```

### Loading Backtest Results (from daily-backtest.py)
```python
# Source: scripts/daily-backtest.py lines 30-37
def load_results():
    if not RESULTS_PATH.exists():
        return None
    try:
        return json.loads(RESULTS_PATH.read_text())
    except Exception:
        return None
```

### Drift Check with Per-Bot Breakdown (from daily-backtest.py)
```python
# Source: scripts/daily-backtest.py lines 40-81
# Current check_drift() compares aggregate + per_bot Brier scores
# Pipeline extends this to also include per_city for weather
old_bots = previous.get("per_bot", {})
new_bots = current.get("per_bot", {})
for bot in sorted(set(old_bots) | set(new_bots)):
    ob = old_bots.get(bot, {}).get("brier_score")
    nb = new_bots.get(bot, {}).get("brier_score")
    if ob is not None and nb is not None and ob > 0:
        bot_change = (nb - ob) / ob
```

### WhatsApp Notification (from kalshi_auth.py)
```python
# Source: src/kalshi/kalshi_auth.py lines 1502-1532
notify_whatsapp(alert_message, logger=log)
# Reads phone from bots-config.json if not provided
# Uses openclaw CLI under the hood
```

### Atomic JSON Write (from kalshi_auth.py)
```python
# Source: src/kalshi/kalshi_auth.py lines 361-372
from kalshi_auth import _atomic_write_json
_atomic_write_json(baselines_path, baselines_data)
# Uses tempfile + os.replace() for crash-safe writes
```

### Calibrate Output as JSON (from calibrate-sigma.py)
```python
# Source: scripts/calibrate-sigma.py main() lines 889-891
# Run with --json to get structured output without saving
# Pipeline captures this stdout and parses it
result = subprocess.run(
    [sys.executable, str(CALIBRATE_SCRIPT), "--json"],
    capture_output=True, text=True, timeout=300,
)
proposed = json.loads(result.stdout)
```

### Backtest Results Structure (from data/backtest-results.json)
```python
# Key fields used for drift detection:
# results["brier_score"]  -- aggregate
# results["per_bot"]["weather"]["brier_score"]  -- per-bot
# results["per_city_brier"]["MIA"]["brier"]  -- per-city (weather only)
# results["per_bot"]["weather"]["n_evaluated"]  -- sample size for gating
```

### Cron Job Pattern (from setup-cron.sh)
```bash
# Source: scripts/setup-cron.sh lines 29-30
# Existing pattern: idempotent cron installer with PATH setup
CRON_LINE="0 * * * * /bin/bash -c \"cd $PROJECT_DIR && $NPM_PATH run sync:up\" >> $LOG 2>&1 $CRON_TAG"
# Pipeline cron would be:
# 0 6 * * * /bin/bash -c "cd $PROJECT_DIR && python3 scripts/calibration-pipeline.py" >> $LOG 2>&1
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Manual `npm run reconcile && npm run backtest:daily` | Automated daily pipeline | This phase | Eliminates human-in-the-loop for routine quality checks |
| `daily-backtest.py` aggregate-only drift | Per-bot + per-city granular drift detection | This phase | Catches city-specific degradation that aggregate masks |
| No calibration suggestions | Timestamped suggestion files with improvement metrics | This phase | Enables informed human decision on when to recalibrate |
| Overwrite previous results | Baseline tracking + timestamped history | This phase | Enables trend analysis and prevents ratcheting |

**Deprecated/outdated:**
- `daily-backtest.py` will be functionally superseded by the new pipeline but should NOT be deleted (it's still useful as a lightweight standalone tool)

## Open Questions

1. **Baseline update policy after accepted calibration**
   - What we know: User wants baselines to track known-good state. Baselines should not auto-update.
   - What's unclear: After the user manually applies a calibration suggestion, should the pipeline auto-detect the change and re-snapshot baselines? Or should there be an explicit `--update-baseline` flag?
   - Recommendation: Add an `--update-baseline` flag to the pipeline script. Also auto-detect if `calibration.json` timestamp is newer than baseline timestamp and suggest baseline update in the WhatsApp message.

2. **Interaction with existing `npm run backtest:daily` cron**
   - What we know: OPS-02 in Phase 8 calls for daily backtest with drift alert. This phase implements that functionality.
   - What's unclear: Should the pipeline replace the `backtest:daily` npm script, or should both coexist?
   - Recommendation: Pipeline subsumes `backtest:daily` functionality. Update `npm run backtest:daily` to call the pipeline. Keep `daily-backtest.py` as a standalone tool for ad-hoc use.

3. **Per-city drift vs per-bot drift precedence**
   - What we know: Weather gets per-city baselines. Other bots get per-bot baselines.
   - What's unclear: If aggregate weather Brier is fine but one city drifts badly, should this appear in the WhatsApp summary?
   - Recommendation: Yes -- per-city drift should be reported independently. A city-specific model issue is actionable even if the aggregate looks healthy.

## Discretion Recommendations

Based on research of the existing codebase:

### Pipeline Trigger: Cron (recommended)
**Rationale:** The project already uses cron for S3 sync (setup-cron.sh). The supervisor manages long-running daemons, not daily one-shot tasks. Cron is simpler, more reliable, and already proven in this project. Add the pipeline entry to `setup-cron.sh`.

### Failure Handling: Continue with Logging (recommended)
**Rationale:** Pipeline stages are independent enough that a reconcile failure shouldn't prevent backtest from running on already-reconciled data. Strategy: run all stages, log per-stage success/failure, include failures in WhatsApp summary. Only abort if a stage produces data that would corrupt downstream (none of the current stages can do this since they're idempotent).

### Severity Tiers: Single 10% Threshold (recommended)
**Rationale:** User specified 10% in requirements (CAL-01, CAL-03). Multi-tier adds complexity without clear benefit at current trade volumes. The WhatsApp message already includes the exact percentage change, so the human reviewer gets full context. Can add tiers later if alert fatigue becomes an issue.

### Suggestion Criteria: Brier Primary + P&L Context (recommended)
**Rationale:** Match the existing calibrate-sigma.py pattern: Brier as primary objective with P&L as tiebreaker within tolerance. The suggestion should report both metrics so the human reviewer has full context, but the trigger should be Brier-only (>5% improvement) since that's the calibration metric.

### Apply Mechanism: Apply Script (recommended)
**Rationale:** Manual copy is error-prone (wrong file, partial copy). A simple `python3 scripts/calibration-pipeline.py --apply-suggestion data/calibration-suggestions/calibration-suggestion-2026-02-28.json` that copies proposed params to calibration.json (with backup) and updates baselines is safer and auditable. This is a ~20-line function, not a complex feature.

### Baseline Initialization: First Run Auto-Snapshot (recommended)
**Rationale:** On first pipeline run, if no baselines file exists, snapshot current backtest results as the baseline. This is zero-friction -- no manual step required. The alternative (require manual snapshot) adds unnecessary ceremony and delays pipeline activation.

### Baseline Update Timing: On Calibration Accept Only (recommended)
**Rationale:** Baselines should represent "known good" state. Auto-updating on every run causes ratcheting. Update baselines only when: (1) `--update-baseline` flag is passed explicitly, or (2) `--apply-suggestion` is used (accepting new calibration implicitly updates the baseline to match).

### Minimum Sample Size: 10 per Entity (recommended)
**Rationale:** Matches `MIN_TRADES_PER_CITY = 10` in calibrate-sigma.py. Entities with fewer than 10 evaluated trades are reported as "insufficient data" rather than triggering drift alerts.

## Sources

### Primary (HIGH confidence)
- `scripts/daily-backtest.py` -- existing drift detection pattern (subprocess, compare, alert)
- `scripts/backtest.py` -- Brier score computation, per-bot/per-city breakdown, `--save` output structure
- `scripts/calibrate-sigma.py` -- grid search, Bayesian shrinkage, `--json` output, backup logic
- `scripts/reconcile-trades.py` -- idempotent settlement annotation
- `scripts/backfill-settlements.py` -- individual market endpoint queries
- `scripts/setup-cron.sh` -- existing cron installation pattern
- `src/kalshi/kalshi_auth.py` -- `notify_whatsapp()`, `_atomic_write_json()`, `setup_logging()`
- `data/backtest-results.json` -- actual output structure with per_bot, per_city_brier fields
- `config/calibration.json` -- actual calibration param structure

### Secondary (MEDIUM confidence)
- `scripts/supervisor.py` -- daemon management pattern (confirmed pipeline should NOT use supervisor)
- `config/bots-config.json` -- notificationPhone for WhatsApp

### Tertiary (LOW confidence)
- None -- all research based on codebase inspection of existing, working components

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- pure Python stdlib + existing internal modules, no new dependencies
- Architecture: HIGH -- pipeline pattern matches existing daily-backtest.py, just extends it
- Pitfalls: HIGH -- identified from actual codebase patterns and data structures

**Research date:** 2026-02-28
**Valid until:** 2026-03-28 (stable -- internal codebase, no external API changes expected)
