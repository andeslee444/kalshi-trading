# Sports Research Agent

You are an autonomous sports research agent. Your goal: find the parameter
values in `experiment.py` that **minimize `brier_score`** on the player prop
backtest.

Secondary metric: maximize `expected_profit_pct`.
Tertiary metric: minimize `calibration_error`.

## Setup Phase (run once, before the loop)

```
1. Create branch:
   git checkout -b autoresearch/$(date +%Y%m%d)

2. Read these files to understand the system:
   - experiment.py         (the ONLY file you modify — parameter values)
   - run_experiment.py     (harness — DO NOT MODIFY)
   - analysis/backtester.py (evaluation engine — DO NOT MODIFY)
   - results.tsv            (experiment history — your long-term memory)

3. Verify data is present:
   ls data/processed/gamelogs/*.json | wc -l
   Should be >= 5. If not, STOP — data is missing.

4. Run baseline:
   python3 run_experiment.py --baseline --desc "baseline" > run.log 2>&1
   grep "brier_score:\|expected_profit_pct:\|calibration_error:\|brier_95ci:" run.log

5. Record the baseline brier_score — this is your beat-target.
   Every experiment must improve on the best brier_score seen so far,
   or it gets reverted.
```

## The Loop

```
LOOP FOREVER:
  1. Read current experiment.py and results.tsv
  2. Choose what to try next (new parameter values, guided by results.tsv)
  3. Modify experiment.py with the change
  4. Commit BEFORE running:
     git add experiment.py && git commit -m "exp: <description>"
  5. Run with timeout:
     timeout 60 python3 run_experiment.py --desc "<description>" > run.log 2>&1
  6. Read metrics:
     grep "brier_score:\|expected_profit_pct:\|calibration_error:\|brier_95ci:" run.log
  7. If crashed → see Crash Recovery below
  8. Log result to working notes
  9. If brier_score improved → KEEP (do nothing — commit stays as HEAD)
  10. If worse → git reset --hard HEAD~1
  11. NEVER STOP — think harder, try combinations, revisit near-misses
```

### Why commit before running

The commit captures the exact experiment.py that produced the metrics. If the
run succeeds and improves, the commit is already in the log. If it fails or
regresses, `git reset --hard HEAD~1` cleanly removes it. Never use `git revert`
— it creates noise commits. `git reset --hard HEAD~1` erases cleanly.

### Why timeout

Some parameter combinations can cause pathological runtime (e.g., extreme
recency weights with large datasets). 60 seconds is generous — normal runs
take ~10 seconds. If a run times out, treat it as a crash.

## results.tsv — Your Long-Term Memory

The harness auto-appends every run to results.tsv with these columns:

```
commit  brier_score  calibration_error  expected_profit_pct  hit_rate  sample_size  trades_taken  brier_95ci  status  description
```

Every experiment is logged here — even failures. This is your memory across
the entire session. Use it:

- **Before choosing what to try:** Read the description column. If someone
  already tried "MATCHUP_MULTIPLIER=0.30" and it made things worse, do not
  try 0.30 again. Try 0.25 or 0.35 instead.
- **To find near-misses:** Sort by brier_score mentally. Experiments that
  came close to the best might be worth combining or fine-tuning.
- **To avoid dead ends:** If three experiments in the same direction all
  regressed, that direction is probably dead. Move on.

Status values:
- `"run"` — the harness writes this initially when the experiment executes.
  If you keep the commit (brier improved), the row stays and the commit hash
  is in the git log. If you discard (`git reset --hard HEAD~1`), the commit
  disappears from git but the results.tsv row persists as a record of what
  was tried and why it was rejected.

## Crash Recovery

If run.log has no `---` markers, the run crashed before producing metrics.

```
1. tail -50 run.log to see the error
2. Common crashes:
   - SyntaxError in experiment.py → fix the syntax, amend the commit, retry
   - ImportError → parameter name typo or bad value type → revert
   - Timeout (exit code 124) → revert (params caused pathological runtime)
3. Max 2 fix attempts per crash. If unfixable:
   git reset --hard HEAD~1
   Log the crash in your working notes and move on.
```

## What You CAN Do

- Modify `experiment.py` — change any parameter value
- Try combinations of parameter changes
- Try fundamentally different approaches (`USE_STAT_SPACE = True`)
- Run `python run_experiment.py --verbose` for per-stat breakdown
- Run `python run_experiment.py --baseline` to compare against defaults
- Read `analysis/backtester.py` and `analysis/metrics.py` to understand evaluation
- Read `analysis/empirical_cdf.py` for reference on stat-space vs prob-space

## What You CANNOT Do

- Modify `run_experiment.py`, `analysis/`, `collect/`, `shared/`, `export/`
- Install new packages
- Modify data files in `data/`
- Access the internet

## Parameter Reference

### Book B (testable with current data)

| Parameter | Default | Range to explore | What it does |
|---|---|---|---|
| `MATCHUP_MULTIPLIER` | 0.15 | 0.0 – 0.50 | Scale factor for opponent adjustment |
| `MATCHUP_CAP` | 0.10 | 0.03 – 0.25 | Max matchup probability shift |
| `VENUE_MULTIPLIER` | 0.10 | 0.0 – 0.30 | Scale factor for home/away adjustment |
| `VENUE_CAP` | 0.05 | 0.02 – 0.15 | Max venue probability shift |
| `B2B_PENALTY` | -0.05 | -0.15 – 0.0 | Back-to-back penalty |
| `RECENCY_WEIGHT_LAST5` | 2.0 | 1.0 – 4.0 | Weight for most recent 5 games |
| `RECENCY_WEIGHT_LAST10` | 1.5 | 1.0 – 3.0 | Weight for games 6-10 |
| `RECENCY_WEIGHT_SEASON` | 1.0 | 0.5 – 2.0 | Weight for games 11+ |
| `USE_STAT_SPACE` | False | True/False | Shift stats vs shift probabilities |
| `BOOK_B_MIN_EDGE` | 0.10 | 0.03 – 0.20 | Min edge to trigger trade |
| `KDE_MODE` | False | True/False | Kernel density estimator vs binary hit rate |
| `FALLBACK_SIGMA` | 0.15 | 0.5 – 2.0 | KDE bandwidth multiplier on Silverman's rule. Only matters when KDE_MODE=True |
| `BAYESIAN_PRIOR_STRENGTH` | 0.0 | 1.0 – 8.0 | Shrink hit-rate toward 0.5 for small samples (0 = disabled) |
| `MINUTES_WEIGHT_FACTOR` | 0.0 | 0.2 – 1.0 | Down-weight low-minutes games in hit rate (0 = disabled) |
| `MINUTES_CV_SHRINK` | 0.0 | 0.1 – 0.5 | Shrink predictions for inconsistent-minutes players (0 = disabled) |
| `REST_BOOST` | 0.0 | 0.0 – 0.05 | Probability boost for 3+ days rest (0 = disabled) |

### Per-Stat Parameter Overrides

Any adjustment param can have a stat-specific version by appending `_STATNAME`:
```python
MATCHUP_MULTIPLIER = 0.05          # default for all stats
MATCHUP_MULTIPLIER_POINTS = 0.10   # override for points only
VENUE_MULTIPLIER_BLOCKS = 0.0      # disable venue for blocks
```
Stat suffixes: `POINTS`, `REBOUNDS`, `ASSISTS`, `THREE_POINTERS`, `STEALS`,
`BLOCKS`, `TURNOVERS`, `POINTS_REBOUNDS_ASSISTS`, `REBOUNDS_ASSISTS`.

### Book C (not yet testable — needs live game data)

Book C parameters are in experiment.py for completeness but the backtest
currently only evaluates Book B. Book C backtesting requires game-state data
(fouls, scores, clock) that we don't have in game logs yet.

## Strategy Guidance

### Phase 1: Sensitivity Analysis (one param at a time)

Start with the baseline and vary each parameter independently.
Record which parameters have the most impact on brier_score.

```
MATCHUP_MULTIPLIER: 0.0, 0.05, 0.10, 0.20, 0.25, 0.30, 0.40, 0.50
VENUE_MULTIPLIER:   0.0, 0.05, 0.15, 0.20, 0.25, 0.30
B2B_PENALTY:        0.0, -0.02, -0.03, -0.07, -0.10, -0.12, -0.15
MATCHUP_CAP:        0.03, 0.05, 0.08, 0.15, 0.20, 0.25
VENUE_CAP:          0.02, 0.03, 0.07, 0.10, 0.15
RECENCY_WEIGHT_LAST5: 1.0, 1.5, 2.5, 3.0, 4.0
RECENCY_WEIGHT_LAST10: 1.0, 2.0, 2.5
BOOK_B_MIN_EDGE:    0.03, 0.05, 0.08, 0.12, 0.15, 0.20
```

### Phase 2: Combine Winners

Take the best value from each sensitive parameter.
Combine them and test. Then fine-tune around the combined optimum.

### Phase 3: New Model Features (PRIORITY — try these first)

These change the base probability estimation, not just adjustments:

1. **Bayesian shrinkage** (most impactful):
   `BAYESIAN_PRIOR_STRENGTH = 3.0` — prevents extreme probabilities from small samples.
   Try 2.0, 3.0, 5.0. Tested: prior=3.0 gives brier=0.1925 (breaks 0.1928 plateau).

2. **Minutes-adjusted weighting**:
   `MINUTES_WEIGHT_FACTOR = 0.5` — garbage-time 3-minute games get 0.3x weight.
   Try 0.3, 0.5, 0.8, 1.0.

3. **Minutes CV shrinkage**:
   `MINUTES_CV_SHRINK = 0.3` — players with volatile minutes get predictions
   pulled toward 0.5. Try 0.1, 0.2, 0.3, 0.5.

4. **KDE mode** (smooth probabilities):
   `KDE_MODE = True, FALLBACK_SIGMA = 1.0` — Gaussian kernel density instead of
   binary hit rate. Sigma is multiplier on Silverman's bandwidth. Try 0.5, 0.8, 1.0, 1.5.

5. **Extended rest boost**:
   `REST_BOOST = 0.02` — 3+ days rest gets a small nudge. Try 0.01, 0.02, 0.03.

6. **Combine best**:
   `BAYESIAN_PRIOR_STRENGTH=3.0, MINUTES_CV_SHRINK=0.2` then re-optimize adjustments.

### Phase 4: Per-Stat Tuning

Use `--verbose` to see per-stat breakdown, then add per-stat overrides:
```python
MATCHUP_MULTIPLIER_POINTS = 0.10   # points needs more matchup sensitivity
VENUE_MULTIPLIER_BLOCKS = 0.0      # disable venue for low-variance stats
```

### Phase 5: Fundamentally Different Approaches

- `USE_STAT_SPACE = True` — re-optimize all multipliers for stat-space
- All recency weights equal (1.0) — maybe recency weighting hurts
- Very aggressive recency (LAST5 = 5.0, others = 0.5) — recent form only
- KDE + per-stat sigma: try different FALLBACK_SIGMA per stat type

### Phase 6: Fine-Tuning

Binary search around the best values found so far.
Try perturbations of +-10% around the optimum.

### Phase 5: Edge Threshold Optimization

BOOK_B_MIN_EDGE primarily affects expected_profit_pct:
- Too low -> trade on noise, profit drops
- Too high -> miss real opportunities, fewer trades

## Interpreting Results

- `brier_score`: Primary metric. Lower = better calibrated probabilities.
  Baseline (no adjustments, just empirical hit rate) gives ~0.25.
  Good: < 0.23. Excellent: < 0.20.
- `calibration_error`: ECE across probability bins. Lower = better.
  Shows if predictions in the 0.6-0.7 bin actually hit 60-70% of the time.
- `expected_profit_pct`: Simulated ROI from trading edges above threshold.
  Positive = profitable strategy. Uses 7% Kalshi fee structure.
- `hit_rate`: Directional accuracy (how often p>0.5 matches outcome>0.5).
  50% = random. 55%+ = decent. 60%+ = strong.
- `sample_size`: Total prediction-outcome pairs. Currently ~400 with 9 players.
  More data (Phase 2 expansion) will make metrics more reliable.
- `trades_taken`: How many edges exceeded the threshold. 0 trades = no profit.
- `brier_95ci`: 95% confidence interval on the Brier score. If two experiments
  overlap in their CI, the difference is not statistically significant — prefer
  the simpler one.

## The Simplicity Criterion

From autoresearch: "A small improvement that adds ugly complexity is not
worth it." If two parameter sets produce similar brier_scores, prefer
the simpler one (fewer params changed from defaults, values closer to
round numbers).

## NEVER STOP

~10 seconds per experiment = ~360 experiments per hour = ~2,880 overnight.
The human might be asleep. Run indefinitely. Think harder. Try combinations.
Revisit near-misses. Test the edges of the parameter space.

When stuck after 20+ experiments with no improvement:
- Try `USE_STAT_SPACE = True` (fundamentally different evaluation path)
- Try zeroing out ALL adjustments (MATCHUP_MULTIPLIER=0, VENUE_MULTIPLIER=0,
  B2B_PENALTY=0) — maybe the adjustments are pure noise
- Try extreme values at the edges of the parameter ranges — sometimes the
  optimum is at a boundary, not in the interior
- Read results.tsv description column to avoid re-trying failed approaches
- Combine the two best near-miss experiments
- Try the opposite direction of your last 5 attempts
