# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Autonomous research pipeline that optimizes NBA prop betting parameters for the Oracle trading system (in sibling repo `../kalshi-trading/`). A Claude Code subprocess ("research agent") repeatedly modifies `experiment.py`, runs backtests, and keeps or reverts changes based on Brier score improvements. A director checks for convergence, metric gaming, and data sufficiency every 5 minutes. A FastAPI dashboard on `:3457` shows live progress.

## Commands

```bash
# Full pipeline (dashboard + agent + director)
npm run dev                    # python3 scripts/orchestrator.py

# Dashboard + director only (no agent subprocess)
npm run dev:no-agent

# Collect data first, then start pipeline
npm run dev:collect-first

# Director checks every 60s instead of 300s
npm run dev:fast

# Individual components
npm run dashboard              # FastAPI dashboard on :3457
npm run experiment             # Single backtest run
npm run experiment:verbose     # With per-stat breakdown
npm run baseline               # Run with default params
npm run director               # One-shot director check
npm run director:json          # Director check (JSON output)

# Data collection
npm run collect                # Async scrape (50 players, 20+ min games)
npm run collect:dry            # Dry run
npm run collect:full           # Full collection (100 players, 30 games)

# Export to kalshi-trading
npm run export                 # Write to ../kalshi-trading/config/oracle-calibration.json
npm run export:dry             # Preview export

# Tests
npm run test                   # python3 -m pytest tests/ -v
python3 -m pytest tests/test_models.py -v -k "test_signal"  # Single test
```

## Architecture

### Autonomous Research Loop

```
experiment.py ──→ run_experiment.py ──→ backtester.py ──→ metrics
    ↑ (agent modifies)     (harness, DO NOT MODIFY)        ↓
    │                                                  results.tsv
    │                                                  (append-only memory)
    └── agent reads results.tsv, decides next params ──┘
```

The orchestrator (`scripts/orchestrator.py`) coordinates three components:
1. **Dashboard** — FastAPI on `127.0.0.1:3457` (localhost only), background daemon thread with health monitoring
2. **Research agent** — Claude Code subprocess, modifies `experiment.py` in a loop
3. **Director** — runs `scripts/director_checks.py` every 5 min, checks convergence/gaming

### Agent Constraints

The research agent operates under strict boundaries:
- **CAN modify:** `experiment.py` only (parameter values)
- **CANNOT modify:** `run_experiment.py`, `analysis/`, `collect/`, `shared/`, `export/`, `data/`
- **CANNOT:** install packages, access the internet

The agent commits `experiment.py` before each run. If metrics regress, it does `git reset --hard HEAD~1`. Commit messages follow `exp: <description>` format.

### Key Modules

| Module | Purpose |
|--------|---------|
| `experiment.py` | Parameter values — the only file the agent touches |
| `run_experiment.py` | Harness: loads params, runs backtest, appends `results.tsv` |
| `analysis/backtester.py` | Hold-one-out cross-validation engine |
| `analysis/metrics.py` | Brier score, calibration error, profit calculation |
| `scripts/orchestrator.py` | Master coordinator (agent + dashboard + director) |
| `scripts/director_checks.py` | Convergence detection, CI overlap, metric gaming |
| `scripts/research-dashboard.py` | FastAPI dashboard UI |
| `collect/batch_collector.py` | Async NBA player data scraper (Real Sports API) |
| `export/export_calibration.py` | Writes optimized params to kalshi-trading config |

### State Files

- `results.tsv` — Append-only experiment history (long-term memory across sessions)
- `results.tsv.integrity` — SHA-256 hash chain sidecar (one hash per data row)
- `data/experiment-archive.jsonl` — Full `experiment.py` content per run (survives git reset)
- `data/director-state.json` — Current director assessment (overwritten each check)
- `data/director-log.json` — Audit trail of all director decisions (JSONL, appended)
- `data/orchestrator-state.json` — Orchestrator lifecycle tracking
- `data/director-hints.txt` — Optional hints from director to research agent

### Data Flow

```
Real Sports API → collect/batch_collector.py → data/processed/gamelogs/*.json
                                                       ↓
                                              analysis/backtester.py
                                                       ↓
                                              Brier score + metrics → results.tsv
                                                       ↓
                                              export/ → ../kalshi-trading/config/
```

### Eval-Scope Locking

Two defense layers prevent metric gaming:
1. **Harness injection**: `run_experiment.py` injects `TEST_LINE_OFFSETS=[-2,0,2]` and `USE_EXTENDED_STATS=True` into the params dict.
2. **Backtester reads from params**: `backtester.py` reads these keys from the params dict (with hardcoded defaults as fallback).

If `sample_size < 2000` or `sample_size == 0`, the harness aborts with exit code 2 (distinct from crash exit 1). The `results.tsv` row is logged with status `"integrity"`.

### Export Safety

`export_calibration.py` validates before writing to the trading bot config:
- **Missing parameter check**: fails loudly if any expected parameter is absent from `experiment.py` (prevents silent drops on rename/typo)
- **Range validation**: `PARAM_BOUNDS` table covers all Book B, Book C, edge threshold, and sizing parameters. Rejects out-of-range values (e.g., `BOOK_B_SIZE_PCT > 0.10`, `OT_BASE_MULTIPLIER > 2.0`)
- **Crash row filtering**: `load_best_result` skips rows with status `"crash"` or `"integrity"`

### API Client Error Handling

`RealSportsClient._get()` raises `RealSportsAuthError` on 401/403 (after auto-login retry). Callers must handle this exception — it does NOT silently return `{}`. `batch_collector.py` catches `RealSportsAuthError` and exits with code 1.

### Data Validation

- **Backtester**: validates gamelog stat values against `_STAT_BOUNDS` (e.g., `points` 0–100, `minutes` 0–65). Games with implausible values are skipped.
- **Collector**: skips in-progress games (`gameStatus != "final"`), uses precise minutes (`minutes + seconds/60`), and converts UTC `dateTime` to US Eastern for correct game dates.
- **`_compute_avg_minutes`**: delegates to `_find_box_scores()` from `collect/real_sports.py` (no duplicated key traversal).

### Experiment Archive

`run_experiment.py` appends the full `experiment.py` content to `data/experiment-archive.jsonl` before each run. This preserves parameter states that survive `git reset --hard` (which destroys the commit). One JSON line per experiment with commit hash, timestamp, and full source.

### Domain Model Enums

`oracle/models.py` uses typed enums instead of bare strings:
- `Side.YES` / `Side.NO` — used by `Signal` and `Position` (coerces from string)
- `GameStatus.SCHEDULED` / `.LIVE` / `.FINAL` — used by `Game`
- `MarketStatus.OPEN` / `.CLOSED` / `.SETTLED` — used by `Market`
- `PlayerStatus.ACTIVE` / `.OUT` / `.QUESTIONABLE` — used by `Player`

### Results Integrity Chain

`results.tsv.integrity` is a sidecar file with one SHA-256 hash per data row. Each hash = `SHA256(previous_hash + row_content)`, seeded from the TSV header. The chain detects insertion, modification, or deletion of any row.

- `run_experiment.py` writes the chain hash FIRST, then the TSV row (both flush+fsync). If killed between the two writes, the orphan hash is tolerated by the verifier (`n_chain == n_data + 1` trimmed automatically); the reverse (missing hash) would permanently break the chain.
- `verify_results_integrity()` walks both files and returns `(valid, n_verified, message)`
- **Director**: calls verifier before trusting results; chain break triggers ALERT
- **Export**: calls verifier before writing to trading bot config; chain break aborts export
- Pre-chain rows (before this feature) are tolerated — verification starts from where the chain begins

### Bootstrap CI Correction

`bootstrap_brier_ci()` applies a √n_offsets correction (default √3) to widen the CI for correlated multi-offset samples. The 3 line offsets per (player, stat, game) triple are not independent, so the raw bootstrap CI is overconfident by ~√3.

### Real Sports Auth (Reverse-Engineered)

```
Header: real-auth-info = {userId}!{deviceId}!{token}
Header: real-request-token = Hashids('realwebapp', 16).encode(Date.now())
```
Per-request token with 2–5 min TTL. Rate limits: ~30–40 concurrent before 429.

## Metrics

- **`brier_score`** — Primary metric, lower is better. Baseline ~0.25, good <0.23
- **`calibration_error`** — ECE across probability bins, lower is better
- **`expected_profit_pct`** — Simulated ROI with 7% Kalshi fee structure
- **`brier_95ci`** — Bootstrap 95% CI; overlapping CIs = not statistically significant

## Python

- Python 3.11+ required
- Async code uses `httpx` + `asyncio`
- Auth tokens use `hashids` library
- Statistics via `scipy`
- Tests: `pytest` with `asyncio_mode = "auto"`
- No type checker configured; models in `oracle/models.py` and `shared/models.py`

### Dashboard Security

The dashboard binds to `127.0.0.1` (localhost only). Both the orchestrator's `start_agent()` and the dashboard's `POST /api/research/start` use `O_CREAT|O_EXCL` on the PID file to prevent TOCTOU races where two requests both spawn an agent. State files (`director-state.json`, `orchestrator-state.json`) use atomic temp+rename writes to prevent torn reads.

### Signal Handling

The orchestrator's signal handler (`SIGINT`/`SIGTERM`) only sets `_shutdown` — it does NOT call `stop_agent()` or `sys.exit()` to avoid unsafe re-entrant unwind. Cleanup happens in `main()`'s `finally` block.

## Testing

55 tests across 7 test files:
- `test_models.py` — Signal/Position validation, GameState, enums
- `test_metrics.py` — Brier score, profit math, ECE, bootstrap CI
- `test_export_validation.py` — Param bounds, missing params, crash row filtering
- `test_integrity_chain.py` — Chain creation, tamper detection, orphan tolerance
- `test_backtester.py` — Weighted hit rate, gamelog validation, eval-scope injection
- `test_director_checks.py` — Convergence, gaming detection, v2 filtering
- `test_empirical_cdf.py` — Median, hit rate, empty input

## Branching

Research branches follow `autoresearch/YYYYMMDD` naming. The `main` branch is the PR target. Experiment commits use `exp: <description>` messages and may be bulk-reverted.
