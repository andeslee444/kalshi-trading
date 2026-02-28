# Phase 5: Automated Calibration - Context

**Gathered:** 2026-02-28
**Status:** Ready for planning

<domain>
## Phase Boundary

A daily pipeline that automatically runs settlement reconciliation, backtesting, Brier score comparison, and drift detection. Surfaces calibration parameter suggestions for human review. Does NOT auto-apply calibration changes or add new trading strategies.

</domain>

<decisions>
## Implementation Decisions

### Pipeline orchestration
- Run daily at 6 AM (before markets open, catches overnight settlements)
- Each run writes a timestamped log to `data/logs/calibration/` with stage results, timing, and any drift findings
- Full details in every log (not just on issues)

### Drift alerting
- WhatsApp summary sent every day — both "all models healthy" and "drift detected" messages
- Summary-only format: which bot/model drifted, by how much (e.g., "Weather Brier: 0.18->0.22, +22%"), one-liner recommendation
- No cooldown — alert fires every pipeline run as long as drift persists
- Daily healthy summary confirms the pipeline ran successfully

### Calibration suggestions
- Write suggestion to a JSON file (e.g., `data/calibration-suggestions/calibration-suggestion-2026-02-28.json`)
- Keep timestamped history of all suggestions (never overwrite)
- Suggestion includes proposed params, improvement metrics, and diff from current calibration

### Baseline management
- Per-bot baselines for all bots (entertainment, economics, strategy, etc.)
- Per-bot + per-city baselines for weather (Miami, NYC, etc.) — catches city-specific model degradation
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

</decisions>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 05-automated-calibration*
*Context gathered: 2026-02-28*
