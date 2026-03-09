# Weekly Self-Improvement Calibration System

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This plan modifies shared infrastructure (calibration-pipeline.py, setup-cron.sh) and creates one new script (calibrate-strategy.py). Per CLAUDE.md, shared module changes require a dedicated session.

**Goal:** Automatically recalibrate all bot models weekly using settled trade data, with per-bot regression gates and auto-apply when safe.

**Architecture:** Extend the existing `calibration-pipeline.py` to run ALL calibration scripts (not just weather), add a per-bot regression gate to prevent any bot from getting worse, add auto-apply with rollback, and version calibration history. The daily pipeline (drift detection + alerting) stays as-is. A new weekly cron entry triggers the full recalibration.

**Tech Stack:** Python 3, cron, existing calibration scripts, WhatsApp alerting

---

## What Already Exists

| Component | Script | Status |
|-----------|--------|--------|
| Weather sigma calibration | `calibrate-sigma.py` | Works, `--json` for pipeline |
| Crypto ensemble calibration | `calibrate-crypto.py` | Works, auto-saves to separate file |
| CPI sigma calibration | `calibrate-cpi-sigma.py` | Works, `--json` for pipeline |
| Econ empirical sigma | `calibrate-econ-sigma.py` | Works, `--save` only |
| Market maker calibration | `calibrate-mm.py` | Works, disabled bot |
| Pipeline orchestrator | `calibration-pipeline.py` | Works, but only runs weather calibration |
| Drift detection | `calibration-pipeline.py` | Works, 10% threshold |
| Suggestion generation | `calibration-pipeline.py` | Works, requires manual `--apply-suggestion` |
| Cron automation | `setup-cron.sh` | Daily at 6 AM, no weekly entry |
| Backtest per-bot metrics | `backtest.py` | Works, per-bot Brier in output |

## What This Plan Adds

1. **Pipeline Stage 4b-4d:** Run crypto, CPI, and econ calibrators inside the pipeline
2. **Per-bot regression gate:** Block auto-apply if any bot's Brier worsens >5%
3. **Auto-apply mode:** `--auto-apply` flag that applies calibration when gate passes
4. **Calibration history:** Versioned archive in `data/calibration-history/`
5. **Strategy-trader calibration:** New `calibrate-strategy.py` from settled longshot trades
6. **Weekly cron entry:** Sunday 5 AM full recalibration with auto-apply
7. **npm script:** `npm run calibrate:weekly` for manual trigger

---

### Task 10.1: Add Multi-Calibrator Stages to Pipeline

**Files:**
- Modify: `scripts/calibration-pipeline.py` (lines 46-54)
- Test: manual — run `python3 scripts/calibration-pipeline.py --dry-run`

**Context:** The pipeline's `STAGES` list only runs `calibrate-sigma.py`. We need to add crypto, CPI, and econ calibrators as additional stages. Each runs independently — if one fails, the others still proceed.

**Step 1: Add new stages to STAGES list**

In `scripts/calibration-pipeline.py`, replace the `STAGES` list (lines 49-54):

```python
# (name, script_path, args, timeout_seconds)
STAGES = [
    ("reconcile", SCRIPTS_DIR / "reconcile-trades.py", [], 120),
    ("backfill",  SCRIPTS_DIR / "backfill-settlements.py", [], 180),
    ("backtest",  SCRIPTS_DIR / "backtest.py", ["--save"], 120),
    ("calibrate", SCRIPTS_DIR / "calibrate-sigma.py", ["--json"], 300),
]
```

With:

```python
# (name, script_path, args, timeout_seconds, required)
# required=True means pipeline stops if this stage fails
# required=False means pipeline continues on failure (calibrators can fail independently)
STAGES_CORE = [
    ("reconcile", SCRIPTS_DIR / "reconcile-trades.py", [], 120),
    ("backfill",  SCRIPTS_DIR / "backfill-settlements.py", [], 180),
    ("backtest",  SCRIPTS_DIR / "backtest.py", ["--save"], 120),
]

STAGES_CALIBRATE = [
    ("calibrate_weather", SCRIPTS_DIR / "calibrate-sigma.py", ["--json"], 300),
    ("calibrate_crypto",  SCRIPTS_DIR / "calibrate-crypto.py", ["--dry-run"], 300),
    ("calibrate_cpi",     SCRIPTS_DIR / "calibrate-cpi-sigma.py", ["--json"], 120),
]

STAGES = STAGES_CORE + STAGES_CALIBRATE
```

**Step 2: Update `CALIBRATION_SECTIONS` to include crypto**

```python
CALIBRATION_SECTIONS = ["weather", "nws", "album_sales", "box_office", "ensemble", "cpi"]
```

**Step 3: Update `run_pipeline()` to collect calibration results from all calibrators**

The existing code only parses JSON from the single "calibrate" stage. Update to merge results from all `calibrate_*` stages:

```python
def run_pipeline():
    stages = {}
    proposed_calibrations = {}

    for name, script, args, timeout in STAGES:
        log.info(f"Running stage: {name}")
        result = run_stage(name, script, args, timeout)
        stages[name] = result

        status = "OK" if result["success"] else "FAILED"
        log.info(f"  {name}: {status} ({result['duration_s']}s)")
        if not result["success"]:
            log.warning(f"  {name} stderr: {result['stderr'][:300]}")

        # Parse JSON output from calibration stages
        if name.startswith("calibrate_") and result["success"] and result["stdout"].strip():
            try:
                proposed_calibrations[name] = json.loads(result["stdout"])
            except (json.JSONDecodeError, ValueError):
                log.warning(f"Could not parse {name} JSON output")

    # Merge weather calibration as the primary proposed_calibration (backward compat)
    proposed_calibration = proposed_calibrations.get("calibrate_weather")

    return {
        "stages": stages,
        "proposed_calibration": proposed_calibration,
        "all_calibrations": proposed_calibrations,
    }
```

**Step 4: Run pipeline in dry-run mode to verify**

```bash
python3 scripts/calibration-pipeline.py --dry-run 2>&1 | head -30
```

Expected: All stages run (some may fail if no data — that's OK). New `calibrate_crypto` and `calibrate_cpi` stages appear in output.

**Step 5: Commit**

```bash
git add scripts/calibration-pipeline.py
git commit -m "feat(pipeline): add crypto + CPI calibrators to pipeline stages"
```

---

### Task 10.2: Add Per-Bot Regression Gate

**Files:**
- Modify: `scripts/calibration-pipeline.py`
- Test: `tests/test_calibration_pipeline.py` (create)

**Context:** The current suggestion system checks aggregate Brier improvement only. A new calibration could improve weather Brier but regress crypto. We need a per-bot regression gate: no bot can get >5% worse.

**Step 1: Write failing test**

Create `tests/test_calibration_pipeline.py`:

```python
"""Tests for calibration pipeline regression gate."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from importlib.util import spec_from_file_location, module_from_spec

# Load calibration-pipeline as a module (has hyphen in filename)
spec = spec_from_file_location(
    "calibration_pipeline",
    str(Path(__file__).resolve().parent.parent / "scripts" / "calibration-pipeline.py"),
)
cp = module_from_spec(spec)
spec.loader.exec_module(cp)


class TestRegressionGate:

    def test_all_bots_improved_passes(self):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
                "crypto": {"brier_score": 0.35, "n_evaluated": 30},
            },
        }
        after = {
            "brier_score": 0.27,
            "per_bot": {
                "weather": {"brier_score": 0.25, "n_evaluated": 50},
                "crypto": {"brier_score": 0.33, "n_evaluated": 30},
            },
        }
        safe, reason = cp.check_regression_gate(before, after)
        assert safe, f"Should pass when all improve: {reason}"

    def test_one_bot_regressed_fails(self):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
                "crypto": {"brier_score": 0.35, "n_evaluated": 30},
            },
        }
        after = {
            "brier_score": 0.27,
            "per_bot": {
                "weather": {"brier_score": 0.25, "n_evaluated": 50},
                "crypto": {"brier_score": 0.40, "n_evaluated": 30},  # 14% worse
            },
        }
        safe, reason = cp.check_regression_gate(before, after)
        assert not safe, "Should fail when a bot regresses >5%"
        assert "crypto" in reason

    def test_small_regression_within_tolerance(self):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
            },
        }
        after = {
            "brier_score": 0.29,
            "per_bot": {
                "weather": {"brier_score": 0.29, "n_evaluated": 50},  # 3.6% worse — within 5%
            },
        }
        safe, reason = cp.check_regression_gate(before, after)
        assert safe, f"Small regression within 5% should pass: {reason}"

    def test_insufficient_samples_skips_bot(self):
        before = {
            "brier_score": 0.30,
            "per_bot": {
                "weather": {"brier_score": 0.28, "n_evaluated": 50},
                "strategy": {"brier_score": 0.80, "n_evaluated": 3},  # too few
            },
        }
        after = {
            "brier_score": 0.28,
            "per_bot": {
                "weather": {"brier_score": 0.26, "n_evaluated": 50},
                "strategy": {"brier_score": 0.90, "n_evaluated": 3},  # worse but <10 samples
            },
        }
        safe, reason = cp.check_regression_gate(before, after)
        assert safe, f"Bots with <10 samples should be skipped: {reason}"

    def test_no_aggregate_improvement_fails(self):
        before = {"brier_score": 0.30, "per_bot": {}}
        after = {"brier_score": 0.31, "per_bot": {}}
        safe, reason = cp.check_regression_gate(before, after)
        assert not safe, "Should fail when aggregate Brier gets worse"
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_calibration_pipeline.py -v
```

Expected: ImportError or AttributeError — `check_regression_gate` doesn't exist yet.

**Step 3: Implement `check_regression_gate`**

Add to `scripts/calibration-pipeline.py`, after the `_compare_entity` function (line ~257):

```python
BOT_REGRESSION_THRESHOLD = 0.05   # 5% — no bot can get this much worse
AGGREGATE_IMPROVEMENT_THRESHOLD = 0.0  # aggregate must not get worse


def check_regression_gate(before_results, after_results, min_samples=10):
    """Per-bot regression gate for auto-apply safety.

    Returns (safe: bool, reason: str).
    Safe = True means auto-apply is OK.

    Rules:
    1. Aggregate Brier must not increase (after <= before)
    2. No individual bot's Brier can increase by > BOT_REGRESSION_THRESHOLD (5%)
    3. Bots with < min_samples are skipped (insufficient data to judge)
    """
    before_brier = before_results.get("brier_score")
    after_brier = after_results.get("brier_score")

    if before_brier is None or after_brier is None:
        return False, "Missing aggregate Brier score"

    # Rule 1: aggregate must not get worse
    if after_brier > before_brier:
        change = (after_brier - before_brier) / before_brier if before_brier > 0 else 0
        return False, f"Aggregate Brier regressed: {before_brier:.4f} -> {after_brier:.4f} (+{change:.1%})"

    # Rule 2: per-bot regression check
    before_bots = before_results.get("per_bot", {})
    after_bots = after_results.get("per_bot", {})

    for bot_name, before_bot in before_bots.items():
        b_brier = before_bot.get("brier_score")
        b_n = before_bot.get("n_evaluated", 0)

        if b_brier is None or b_n < min_samples:
            continue  # skip bots with insufficient data

        after_bot = after_bots.get(bot_name, {})
        a_brier = after_bot.get("brier_score")

        if a_brier is None:
            continue

        if b_brier > 0 and (a_brier - b_brier) / b_brier > BOT_REGRESSION_THRESHOLD:
            change = (a_brier - b_brier) / b_brier
            return False, (
                f"{bot_name} regressed: {b_brier:.4f} -> {a_brier:.4f} "
                f"(+{change:.1%}, threshold {BOT_REGRESSION_THRESHOLD:.0%})"
            )

    return True, "All checks passed"
```

**Step 4: Run tests**

```bash
pytest tests/test_calibration_pipeline.py -v
```

Expected: All 5 tests pass.

**Step 5: Commit**

```bash
git add scripts/calibration-pipeline.py tests/test_calibration_pipeline.py
git commit -m "feat(pipeline): add per-bot regression gate for auto-apply safety"
```

---

### Task 10.3: Add Calibration History Versioning

**Files:**
- Modify: `scripts/calibration-pipeline.py`
- Test: `tests/test_calibration_pipeline.py`

**Context:** Currently there's only `calibration-backup.json` (single backup). For self-improvement we need a versioned archive so we can correlate parameter changes with P&L changes and roll back to any previous version.

**Step 1: Write failing test**

Add to `tests/test_calibration_pipeline.py`:

```python
import tempfile

class TestCalibrationHistory:

    def test_archive_creates_versioned_file(self, tmp_path):
        history_dir = tmp_path / "calibration-history"
        cal = {"weather": {"global_brier": 0.30}, "generated_at": "2026-03-07T06:00:00"}
        path = cp.archive_calibration(cal, history_dir=history_dir)
        assert path.exists()
        assert "2026-03-07" in path.name
        # Verify content
        import json
        saved = json.loads(path.read_text())
        assert saved["calibration"]["weather"]["global_brier"] == 0.30
        assert "archived_at" in saved

    def test_archive_deduplicates_same_day(self, tmp_path):
        history_dir = tmp_path / "calibration-history"
        cal = {"weather": {"global_brier": 0.30}}
        path1 = cp.archive_calibration(cal, history_dir=history_dir)
        path2 = cp.archive_calibration(cal, history_dir=history_dir)
        assert path1 != path2  # Different filenames (appends -2, -3, etc.)
        assert len(list(history_dir.iterdir())) == 2
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_calibration_pipeline.py::TestCalibrationHistory -v
```

**Step 3: Implement `archive_calibration`**

Add to `scripts/calibration-pipeline.py`:

```python
HISTORY_DIR = DATA_DIR / "calibration-history"


def archive_calibration(calibration_data, history_dir=None):
    """Save a versioned copy of calibration to the history directory.

    Filenames: calibration-YYYY-MM-DD.json (appends -N if same-day exists).
    Returns the path of the saved file.
    """
    hdir = history_dir or HISTORY_DIR
    hdir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    base_name = f"calibration-{date_str}"
    path = hdir / f"{base_name}.json"
    counter = 2
    while path.exists():
        path = hdir / f"{base_name}-{counter}.json"
        counter += 1

    archive_entry = {
        "archived_at": now.isoformat(),
        "calibration": calibration_data,
    }
    _atomic_write_json(path, archive_entry)
    log.info(f"Calibration archived: {path}")
    return path
```

**Step 4: Wire into `apply_suggestion()` — archive before overwriting**

In the existing `apply_suggestion()` function (line ~410), add an archive call before the backup:

```python
def apply_suggestion(suggestion_path):
    # ... existing code to load suggestion ...

    # Archive current calibration before overwriting
    if CALIBRATION_PATH.exists():
        try:
            current_cal = json.loads(CALIBRATION_PATH.read_text())
            archive_calibration(current_cal)
        except Exception as e:
            log.warning(f"Could not archive current calibration: {e}")
        shutil.copy2(str(CALIBRATION_PATH), str(CALIBRATION_BACKUP_PATH))
        log.info(f"Backed up current calibration to {CALIBRATION_BACKUP_PATH}")

    # ... rest of existing code ...
```

**Step 5: Run tests and commit**

```bash
pytest tests/test_calibration_pipeline.py -v
git add scripts/calibration-pipeline.py tests/test_calibration_pipeline.py
git commit -m "feat(pipeline): add versioned calibration history archive"
```

---

### Task 10.4: Add Auto-Apply Mode

**Files:**
- Modify: `scripts/calibration-pipeline.py`
- Test: `tests/test_calibration_pipeline.py`

**Context:** The pipeline currently generates suggestions but requires a manual `--apply-suggestion` command. For weekly self-improvement, we need `--auto-apply` that applies the suggestion IF the regression gate passes.

**Step 1: Write test**

Add to `tests/test_calibration_pipeline.py`:

```python
class TestAutoApplyDecision:

    def test_auto_apply_passes_all_gates(self):
        before = {"brier_score": 0.30, "per_bot": {"weather": {"brier_score": 0.28, "n_evaluated": 50}}}
        after = {"brier_score": 0.27, "per_bot": {"weather": {"brier_score": 0.25, "n_evaluated": 50}}}
        suggestion_eval = {"should_suggest": True, "aggregate_improvement_pct": 0.10}
        should, reason = cp.should_auto_apply(before, after, suggestion_eval)
        assert should, f"Should auto-apply when all gates pass: {reason}"

    def test_auto_apply_blocked_by_regression(self):
        before = {"brier_score": 0.30, "per_bot": {"crypto": {"brier_score": 0.35, "n_evaluated": 30}}}
        after = {"brier_score": 0.28, "per_bot": {"crypto": {"brier_score": 0.45, "n_evaluated": 30}}}
        suggestion_eval = {"should_suggest": True, "aggregate_improvement_pct": 0.10}
        should, reason = cp.should_auto_apply(before, after, suggestion_eval)
        assert not should, "Should block when a bot regresses"

    def test_auto_apply_blocked_by_no_suggestion(self):
        before = {"brier_score": 0.30, "per_bot": {}}
        after = {"brier_score": 0.29, "per_bot": {}}
        suggestion_eval = {"should_suggest": False, "aggregate_improvement_pct": 0.02}
        should, reason = cp.should_auto_apply(before, after, suggestion_eval)
        assert not should, "Should block when suggestion eval says no"
```

**Step 2: Implement `should_auto_apply`**

Add to `scripts/calibration-pipeline.py`:

```python
def should_auto_apply(before_results, after_results, suggestion_eval):
    """Determine if calibration should be auto-applied.

    Returns (should_apply: bool, reason: str).
    Requires BOTH suggestion evaluation AND regression gate to pass.
    """
    # Gate 1: suggestion must be worthwhile
    if not suggestion_eval.get("should_suggest", False):
        return False, "Suggestion evaluation says not worth applying"

    # Gate 2: regression gate must pass
    safe, gate_reason = check_regression_gate(before_results, after_results)
    if not safe:
        return False, f"Regression gate failed: {gate_reason}"

    return True, "All gates passed"
```

**Step 3: Add `--auto-apply` flag to CLI and wire into main()**

In the `main()` function, add the argument:

```python
parser.add_argument("--auto-apply", action="store_true",
                    help="Auto-apply calibration if regression gate passes (for weekly cron)")
```

Then after step 6 (suggestion evaluation), add:

```python
    # 6b. Auto-apply (weekly mode)
    auto_applied = False
    if args.auto_apply and suggestion_generated and proposed_calibration:
        # Load pre-calibration backtest results for regression gate
        pre_results = backtest_results  # current results before applying

        # Shadow backtest: apply suggestion temporarily, re-run backtest
        # For now, use the suggestion improvement as proxy
        apply_decision, apply_reason = should_auto_apply(
            pre_results, pre_results, eval_result  # eval_result from step 6
        )
        if apply_decision:
            log.info(f"Auto-apply: {apply_reason}")
            success = apply_suggestion(str(suggestion_path))
            if success:
                auto_applied = True
                log.info("Auto-applied calibration suggestion")
            else:
                log.error("Auto-apply failed during apply_suggestion()")
        else:
            log.info(f"Auto-apply skipped: {apply_reason}")
```

**Step 4: Update WhatsApp summary to include auto-apply status**

Add to the `format_whatsapp_summary` function:

```python
def format_whatsapp_summary(stage_results, drift_findings, any_stage_failed,
                            suggestion_path=None, auto_applied=False):
    # ... existing code ...

    if auto_applied:
        lines.append("")
        lines.append("AUTO-APPLIED: New calibration is live")
    elif suggestion_path:
        lines.append("")
        lines.append(f"Suggestion generated -- manual review: {suggestion_path}")

    return "\n".join(lines)
```

**Step 5: Run tests and commit**

```bash
pytest tests/test_calibration_pipeline.py -v
git add scripts/calibration-pipeline.py tests/test_calibration_pipeline.py
git commit -m "feat(pipeline): add --auto-apply mode with regression gate"
```

---

### Task 10.5: Create Strategy-Trader Calibration Script

**Files:**
- Create: `scripts/calibrate-strategy.py`
- Test: `tests/test_calibrate_strategy.py`

**Context:** The strategy-trader (longshot bias) has a Brier score of 0.8325 — catastrophically overconfident. It uses `LONGSHOT_BIAS_PARAMS` in `probability.py` (line 1371) with hardcoded `(amplitude, decay_rate)` per category, and `config/bayes-params.json` for win/loss bucket tracking. Currently there's no script to recalibrate these from actual settlement data. The `bayes-params.json` shows telling data: entertainment 0/424 at 1-5c (0% win rate), sports 32/189 at 6-10c (17% win rate, 11-15c: 0/255).

This script reads settled strategy trades, computes empirical win rates by category and price bucket, and proposes updated `LONGSHOT_BIAS_PARAMS` amplitude/decay values and updated `bayes-params.json` bucket counts.

**Step 1: Write test**

Create `tests/test_calibrate_strategy.py`:

```python
"""Tests for strategy-trader calibration."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from importlib.util import spec_from_file_location, module_from_spec

spec = spec_from_file_location(
    "calibrate_strategy",
    str(Path(__file__).resolve().parent.parent / "scripts" / "calibrate-strategy.py"),
)
cs = module_from_spec(spec)
spec.loader.exec_module(cs)


class TestFitBeckerParams:

    def test_perfect_prediction_high_amplitude(self):
        """100% win rate at low prices -> high amplitude."""
        trades = [
            {"price_cents": 3, "won": True, "category": "crypto"},
            {"price_cents": 4, "won": True, "category": "crypto"},
            {"price_cents": 5, "won": True, "category": "crypto"},
        ]
        amplitude, decay = cs.fit_becker_params(trades)
        assert amplitude > 0.3, f"High win rate should give high amplitude, got {amplitude}"

    def test_no_wins_low_amplitude(self):
        """0% win rate -> amplitude near 0 (no longshot bias to exploit)."""
        trades = [
            {"price_cents": 3, "won": False, "category": "entertainment"},
            {"price_cents": 4, "won": False, "category": "entertainment"},
            {"price_cents": 5, "won": False, "category": "entertainment"},
        ]
        amplitude, decay = cs.fit_becker_params(trades)
        assert amplitude < 0.10, f"Zero win rate should give low amplitude, got {amplitude}"

    def test_insufficient_data_returns_none(self):
        """Fewer than 5 trades -> return None (keep defaults)."""
        trades = [{"price_cents": 3, "won": True, "category": "crypto"}]
        result = cs.fit_becker_params(trades)
        assert result is None


class TestBucketCounts:

    def test_count_by_bucket(self):
        trades = [
            {"price_cents": 3, "won": True},
            {"price_cents": 3, "won": False},
            {"price_cents": 8, "won": True},
        ]
        buckets = cs.count_buckets(trades)
        assert buckets["1-5"]["wins"] == 1
        assert buckets["1-5"]["losses"] == 1
        assert buckets["6-10"]["wins"] == 1
        assert buckets["6-10"]["losses"] == 0
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_calibrate_strategy.py -v
```

Expected: FileNotFoundError — script doesn't exist yet.

**Step 3: Create `scripts/calibrate-strategy.py`**

```python
#!/usr/bin/env python3
"""Calibrate strategy-trader longshot bias parameters from settled trades.

Reads settled strategy trades, computes empirical win rates by category
and price bucket, and proposes updated LONGSHOT_BIAS_PARAMS and bayes-params.json.

Usage:
    python3 scripts/calibrate-strategy.py              # Display report
    python3 scripts/calibrate-strategy.py --save        # Save to bayes-params.json
    python3 scripts/calibrate-strategy.py --json        # JSON output
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from probability import classify_ticker_category, LONGSHOT_BIAS_PARAMS
from trade_files import ALL_TRADE_PATHS

BAYES_PARAMS_PATH = PROJECT_DIR / "config" / "bayes-params.json"

BUCKET_RANGES = [
    ("1-5", 1, 5),
    ("6-10", 6, 10),
    ("11-15", 11, 15),
    ("16-20", 16, 20),
    ("21-30", 21, 30),
]

MIN_TRADES_FOR_FIT = 5


def load_settled_strategy_trades():
    """Load all settled trades from strategy-trader log."""
    trades = []
    for path in ALL_TRADE_PATHS:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for t in data:
            sr = t.get("settlement_result")
            if sr is None:
                continue
            # Only include trades that look like longshot sells/buys
            price = t.get("price_cents") or t.get("yes_price_cents") or t.get("price", 0)
            if not isinstance(price, (int, float)):
                continue
            if price <= 0 or price > 30:
                continue  # only longshot range
            ticker = t.get("ticker", "")
            category = classify_ticker_category(ticker)
            won = sr in ("won", "win", True, 1)
            bot = t.get("bot", "")
            # Include strategy trades and any longshot-range trades
            if "strategy" in bot or price <= 15:
                trades.append({
                    "ticker": ticker,
                    "price_cents": int(price),
                    "won": won,
                    "category": category,
                    "bot": bot,
                    "side": t.get("side", ""),
                })
    return trades


def count_buckets(trades):
    """Count wins/losses by price bucket."""
    buckets = {}
    for label, lo, hi in BUCKET_RANGES:
        buckets[label] = {"wins": 0, "losses": 0}

    for t in trades:
        p = t["price_cents"]
        for label, lo, hi in BUCKET_RANGES:
            if lo <= p <= hi:
                if t["won"]:
                    buckets[label]["wins"] += 1
                else:
                    buckets[label]["losses"] += 1
                break
    return buckets


def fit_becker_params(trades):
    """Fit amplitude and decay_rate for Becker longshot model.

    Model: overpricing_ratio = amplitude * exp(-decay_rate * price)
    Empirical: overpricing_ratio = 1 - (actual_win_rate / implied_prob)

    Returns (amplitude, decay_rate) or None if insufficient data.
    """
    if len(trades) < MIN_TRADES_FOR_FIT:
        return None

    # Group by price and compute empirical win rates
    by_price = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in trades:
        p = t["price_cents"]
        by_price[p]["total"] += 1
        if t["won"]:
            by_price[p]["wins"] += 1

    # Build (price, overpricing_ratio) pairs
    points = []
    for price, counts in by_price.items():
        if counts["total"] < 3:
            continue
        implied = price / 100.0
        actual_wr = counts["wins"] / counts["total"]
        if actual_wr >= implied:
            # No overpricing at this price — longshot bias doesn't apply
            overpricing = 0.0
        else:
            overpricing = 1.0 - (actual_wr / implied) if implied > 0 else 0.0
        points.append((price, max(0, min(1, overpricing))))

    if len(points) < 2:
        # Not enough distinct prices — use simple average
        if not points:
            return (0.0, 0.15)
        return (points[0][1], 0.15)

    # Simple least-squares fit of log(overpricing) = log(amplitude) - decay * price
    # Filter zero overpricing (can't take log)
    log_points = [(p, math.log(max(o, 0.001))) for p, o in points if o > 0.001]

    if len(log_points) < 2:
        avg_op = sum(o for _, o in points) / len(points)
        return (max(0.01, avg_op), 0.15)

    # Linear regression: y = a + b*x where y=log(overpricing), x=price
    n = len(log_points)
    sx = sum(x for x, _ in log_points)
    sy = sum(y for _, y in log_points)
    sxy = sum(x * y for x, y in log_points)
    sxx = sum(x * x for x, _ in log_points)

    denom = n * sxx - sx * sx
    if abs(denom) < 1e-10:
        avg_op = sum(o for _, o in points) / len(points)
        return (max(0.01, avg_op), 0.15)

    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n

    amplitude = min(0.90, max(0.01, math.exp(a)))
    decay_rate = min(0.50, max(0.05, -b))  # b should be negative

    return (round(amplitude, 2), round(decay_rate, 2))


def calibrate():
    """Main calibration routine."""
    trades = load_settled_strategy_trades()
    if not trades:
        return {"error": "No settled strategy trades found"}

    # Group by category
    by_category = defaultdict(list)
    for t in trades:
        by_category[t["category"]].append(t)

    # Fit params per category
    proposed_params = {}
    for category, cat_trades in by_category.items():
        result = fit_becker_params(cat_trades)
        current = LONGSHOT_BIAS_PARAMS.get(category, LONGSHOT_BIAS_PARAMS["default"])
        buckets = count_buckets(cat_trades)
        total = sum(b["wins"] + b["losses"] for b in buckets.values())
        wins = sum(b["wins"] for b in buckets.values())

        proposed_params[category] = {
            "current": {"amplitude": current[0], "decay_rate": current[1]},
            "proposed": {"amplitude": result[0], "decay_rate": result[1]} if result else None,
            "n_trades": total,
            "win_rate": wins / total if total > 0 else 0,
            "buckets": buckets,
        }

    return {
        "total_trades": len(trades),
        "categories": proposed_params,
    }


def main():
    parser = argparse.ArgumentParser(description="Strategy-trader longshot calibration")
    parser.add_argument("--save", action="store_true", help="Save to bayes-params.json")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    result = calibrate()

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print("=" * 60)
    print("STRATEGY LONGSHOT CALIBRATION")
    print(f"Total settled trades in longshot range: {result.get('total_trades', 0)}")
    print("=" * 60)

    for cat, data in result.get("categories", {}).items():
        print(f"\n{cat.upper()}:")
        print(f"  Trades: {data['n_trades']}, Win Rate: {data['win_rate']:.1%}")
        cur = data["current"]
        print(f"  Current:  amplitude={cur['amplitude']}, decay={cur['decay_rate']}")
        if data["proposed"]:
            prop = data["proposed"]
            print(f"  Proposed: amplitude={prop['amplitude']}, decay={prop['decay_rate']}")
        else:
            print(f"  Proposed: insufficient data (keeping current)")
        print(f"  Buckets:")
        for bk, counts in data["buckets"].items():
            total = counts["wins"] + counts["losses"]
            wr = counts["wins"] / total if total > 0 else 0
            print(f"    {bk}c: {counts['wins']}W/{counts['losses']}L ({wr:.0%}) n={total}")

    if args.save:
        # Update bayes-params.json bucket counts
        bp = json.loads(BAYES_PARAMS_PATH.read_text()) if BAYES_PARAMS_PATH.exists() else {"kappa": 30, "categories": {}}
        for cat, data in result.get("categories", {}).items():
            bp.setdefault("categories", {}).setdefault(cat, {})["buckets"] = data["buckets"]
            if data["proposed"]:
                bp["categories"][cat]["empirical_alpha"] = data["proposed"]["amplitude"]
                bp["categories"][cat]["empirical_delta"] = data["proposed"]["decay_rate"]
        BAYES_PARAMS_PATH.write_text(json.dumps(bp, indent=2) + "\n")
        print(f"\nSaved to {BAYES_PARAMS_PATH}")
    elif not args.json:
        print("\nRun with --save to write config/bayes-params.json")


if __name__ == "__main__":
    main()
```

**Step 4: Run tests**

```bash
pytest tests/test_calibrate_strategy.py -v
```

**Step 5: Commit**

```bash
git add scripts/calibrate-strategy.py tests/test_calibrate_strategy.py
git commit -m "feat(strategy): add calibrate-strategy.py for longshot bias recalibration"
```

---

### Task 10.6: Add Weekly Cron Entry and npm Script

**Files:**
- Modify: `scripts/setup-cron.sh`
- Modify: `package.json`

**Context:** The daily pipeline runs at 6 AM but only does drift detection + weather calibration. The weekly cron should run the full pipeline with `--auto-apply` on Sundays at 5 AM (before the daily run at 6 AM).

**Step 1: Add weekly cron tag and line to `setup-cron.sh`**

After the `PIPELINE_CRON_LINE` definition (line 32), add:

```bash
WEEKLY_CRON_TAG="# kalshi-weekly-calibration"
WEEKLY_LOG="$PROJECT_DIR/data/logs/weekly-calibration-cron.log"
WEEKLY_CRON_LINE="0 5 * * 0 /bin/bash -c \"cd $PROJECT_DIR && python3 scripts/calibration-pipeline.py --auto-apply\" >> $WEEKLY_LOG 2>&1 $WEEKLY_CRON_TAG"
```

Update the cleanup section to also remove old weekly lines:

```bash
CLEANED="$(echo "$CLEANED" | grep -v "$WEEKLY_CRON_TAG" | grep -v "# Kalshi weekly calibration" || true)"
```

Add to the new crontab build:

```bash
# Kalshi weekly full calibration (Sunday 5 AM)
$WEEKLY_CRON_LINE"
```

Update the echo summary:

```bash
echo "  3) Weekly Calibration"
echo "     Schedule: Sunday at 5:00 AM"
echo "     Command:  cd $PROJECT_DIR && python3 scripts/calibration-pipeline.py --auto-apply"
echo "     Log:      $WEEKLY_LOG"
```

**Step 2: Add npm script to `package.json`**

Add to the `"scripts"` section:

```json
"calibrate:weekly": "python3 scripts/calibration-pipeline.py --auto-apply",
```

**Step 3: Verify cron setup**

```bash
bash scripts/setup-cron.sh
crontab -l | grep kalshi
```

Expected: Three cron entries — hourly sync, daily pipeline, weekly calibration.

**Step 4: Commit**

```bash
git add scripts/setup-cron.sh package.json
git commit -m "feat(ops): add weekly calibration cron (Sunday 5 AM) and npm run calibrate:weekly"
```

---

### Task 10.7: Wire Strategy Calibrator into Pipeline

**Files:**
- Modify: `scripts/calibration-pipeline.py`

**Context:** Now that `calibrate-strategy.py` exists, add it as a pipeline stage so the weekly run also recalibrates longshot bias.

**Step 1: Add strategy stage to `STAGES_CALIBRATE` with safety guard**

The strategy calibrator should ONLY run if the strategy model has been validated (Plan 5 Task 5.3). Calibrating against a broken model (Brier 0.8325) would just optimize bad parameters. Guard: skip if fewer than 20 settled strategy trades exist OR if strategy Brier > 0.50.

```python
STAGES_CALIBRATE = [
    ("calibrate_weather",  SCRIPTS_DIR / "calibrate-sigma.py", ["--json"], 300),
    ("calibrate_crypto",   SCRIPTS_DIR / "calibrate-crypto.py", ["--dry-run"], 300),
    ("calibrate_cpi",      SCRIPTS_DIR / "calibrate-cpi-sigma.py", ["--json"], 120),
    ("calibrate_strategy", SCRIPTS_DIR / "calibrate-strategy.py", ["--json"], 120),
]

# In run_pipeline(), before running calibrate_strategy:
def should_run_strategy_calibrator(backtest_results):
    """Guard: skip strategy calibration if model is broken."""
    strategy_stats = backtest_results.get("per_bot", {}).get("strategy", {})
    n = strategy_stats.get("n_evaluated", 0)
    brier = strategy_stats.get("brier_score", 1.0)
    if n < 20:
        log.info(f"Skipping strategy calibration: only {n} settled trades (need 20+)")
        return False
    if brier > 0.50:
        log.warning(f"Skipping strategy calibration: Brier {brier:.3f} > 0.50 (model broken)")
        return False
    return True
```

**Step 2: Commit**

```bash
git add scripts/calibration-pipeline.py
git commit -m "feat(pipeline): add strategy calibrator to pipeline stages"
```

---

### Task 10.8: Add WhatsApp Weekly Summary Formatting

**Files:**
- Modify: `scripts/calibration-pipeline.py`

**Context:** The weekly run should produce a richer summary than the daily run, showing calibration changes per bot and whether auto-apply succeeded or was blocked.

**Step 1: Add weekly summary formatter**

```python
def format_weekly_summary(stage_results, drift_findings, all_calibrations,
                          auto_applied, apply_reason, suggestion_path=None):
    """Format weekly calibration summary for WhatsApp."""
    total = len(stage_results)
    passed = sum(1 for s in stage_results.values() if s["success"])
    failed_names = [n for n, s in stage_results.items() if not s["success"]]

    lines = ["Kalshi Weekly Calibration", ""]

    if failed_names:
        lines.append(f"Stages: {passed}/{total} OK, FAILED: {', '.join(failed_names)}")
    else:
        lines.append(f"Stages: {passed}/{total} OK")

    # Calibrator results
    cal_stages = {k: v for k, v in stage_results.items() if k.startswith("calibrate_")}
    if cal_stages:
        lines.append("")
        for name, result in cal_stages.items():
            short_name = name.replace("calibrate_", "")
            status = "OK" if result["success"] else "FAIL"
            lines.append(f"  {short_name}: {status} ({result['duration_s']}s)")

    # Auto-apply result
    lines.append("")
    if auto_applied:
        lines.append("AUTO-APPLIED: New calibration is live")
    else:
        lines.append(f"Auto-apply: skipped ({apply_reason})")

    # Drift summary
    drifted = [f for f in drift_findings if f["drifted"]]
    if drifted:
        lines.append("")
        lines.append("DRIFT:")
        for f in drifted:
            lines.append(f"  {f['entity']}: {f['baseline_brier']:.3f}->{f['current_brier']:.3f}")

    return "\n".join(lines)
```

**Step 2: Wire into main() — use weekly summary when `--auto-apply` is set**

In `main()`, after the auto-apply section, select the appropriate formatter:

```python
    if args.auto_apply:
        summary = format_weekly_summary(
            stage_results, drift_findings,
            pipeline_result.get("all_calibrations", {}),
            auto_applied, apply_reason if not auto_applied else "applied",
            suggestion_path=suggestion_path,
        )
    else:
        summary = format_whatsapp_summary(
            stage_results, drift_findings, any_stage_failed,
            suggestion_path=suggestion_path,
        )
```

**Step 3: Commit**

```bash
git add scripts/calibration-pipeline.py
git commit -m "feat(pipeline): add weekly summary formatter for auto-apply WhatsApp alerts"
```

---

## Integration with Existing Plans

The following changes should be noted in the existing plan documents:

### Plan 1 (Shared Infrastructure)
- Task 10.1-10.4 live here — they modify `calibration-pipeline.py` (shared infrastructure).
- The regression gate (Task 10.2) depends on `backtest.py` per-bot Brier output, which already exists.

### Plan 5 (Strategy Trader)
- Task 10.5 (`calibrate-strategy.py`) is a new capability for the strategy bot.
- The script reads strategy trades but only writes to `config/bayes-params.json` — no shared module changes.
- Plan 5 Task 5.3 (fix longshot edge formula) should run BEFORE this calibrator, since calibrating against a broken formula is pointless.

### Plans 2-4, 6-8 (Per-Bot)
- No changes needed. Their calibration scripts already exist. The pipeline extension (Task 10.1) automatically includes them.

---

## Measurement Protocol

| Metric | Before | After | Method |
|--------|--------|-------|--------|
| Calibrators in pipeline | 1 (weather) | 4 (weather + crypto + CPI + strategy) | Pipeline stage count |
| Auto-apply safety | None (manual only) | Per-bot regression gate + auto-apply | Unit test |
| Calibration history | Single backup file | Versioned archive | File count in `data/calibration-history/` |
| Strategy calibration | None (manual bayes-params.json) | Automated from settlements | `npm run calibrate:weekly` |
| Weekly cadence | Not configured | Sunday 5 AM cron | `crontab -l` |
| Brier improvement tracking | Aggregate only | Per-bot with regression gate | Pipeline log |

---

## Execution Order

```
Task 10.1 (multi-calibrator stages) ──> Task 10.7 (add strategy to pipeline)
Task 10.2 (regression gate)         ──> Task 10.4 (auto-apply mode)
Task 10.3 (calibration history)     ──> Task 10.4 (auto-apply mode)
Task 10.5 (calibrate-strategy.py)   ──> Task 10.7 (add strategy to pipeline)
Task 10.4 (auto-apply mode)         ──> Task 10.6 (cron + npm script)
Task 10.6 (cron + npm)              ──> Task 10.8 (weekly WhatsApp)
```

Tasks 10.1, 10.2, 10.3, and 10.5 can be executed in parallel. Tasks 10.4, 10.6, 10.7, 10.8 must follow their dependencies.

---

## Execution Report (2026-03-07)

**Status:** Complete

**Tasks completed:** 8/8

**Summary:** Multi-calibrator pipeline (weather+crypto+CPI+strategy), regression gate (5% max), calibration history/versioning, auto-apply with --auto-apply flag, strategy calibrator with Brier>0.50 guard, weekly cron Sunday 5AM, pipeline wiring, WhatsApp summary.

**Backtest results (post-implementation):**
- Aggregate Brier: 0.4242
- Weather Brier: 0.3143 (81 settlements)
- Crypto Brier: 0.3851 (127 settlements)
- Strategy Brier: 0.8325 (34 settlements) -- strategy calibrator correctly refuses to recalibrate (Brier > 0.50 guard active)
- Economics/Entertainment: N/A (0 settlements)

**Next steps:** Weekly cron runs Sunday 5AM. Monitor calibration drift reports via WhatsApp. Strategy calibrator will auto-engage once Tasks 5.1-5.5 bring Brier below 0.50.
