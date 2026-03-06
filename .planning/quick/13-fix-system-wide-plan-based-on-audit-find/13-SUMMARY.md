# Quick Task 13: Fix System-Wide Plan Based on Audit Findings

## What Changed

Rewrote `docs/plans/bot-improvements/05-system-wide.md` based on comprehensive audit of the actual codebase state.

## Changes Made

1. **Removed SYS-2** — `_effective_max_trade_cents` already uses `min()`, marked as RESOLVED
2. **Rewrote SYS-1** — Pipeline exists, real issue is scheduling. Now accurately describes what's built vs what's missing
3. **Rewrote SYS-5** — Connected exit limit blocking to timezone bug (SYS-7) as likely root cause
4. **Rewrote CROSS-2** — Fixed incorrect claims about edge fields. Real gap is `model_prob` missing from strategy-trader and market-maker, not `raw_edge` vs `edge`
5. **Promoted CROSS-3 to SYS-7** — Timezone bug is a concrete code issue with specific file/line references, not just "verify"
6. **Added SYS-8** — Missing dependency management (feedparser crash pattern)
7. **Added SYS-9** — No config schema validation
8. **Added CROSS-4** — Dashboard not supervisor-managed
9. **Renamed old CROSS-1** to accurately say "schedule existing pipeline" not "create pipeline"
10. **Updated Pre-Production Checklist** — Removed fixed item, added timezone fix, automation scheduling, dependency pinning, trade record standardization
11. **Added Mac Mini Action Items section** — Consolidated list of production server actions with exact commands

## Files Modified

- `docs/plans/bot-improvements/05-system-wide.md` — Complete rewrite
