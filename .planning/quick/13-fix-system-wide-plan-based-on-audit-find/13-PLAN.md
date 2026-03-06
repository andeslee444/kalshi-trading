---
phase: quick-13
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - docs/plans/bot-improvements/05-system-wide.md
autonomous: true
requirements: [Q13-01]

must_haves:
  truths:
    - "Plan accurately reflects current system state based on audit findings"
    - "No stale or already-fixed issues presented as open problems"
    - "New issues discovered in audit are documented with correct details"
  artifacts:
    - path: "docs/plans/bot-improvements/05-system-wide.md"
      provides: "Corrected system-wide improvement plan"
      contains: "SYS-7"
  key_links: []
---

<objective>
Rewrite docs/plans/bot-improvements/05-system-wide.md to reflect audit findings.

Purpose: The current plan has stale/inaccurate information — a fixed bug listed as open, incomplete descriptions of real issues, and missing items discovered during audit. Accuracy matters because this document drives production deployment decisions.

Output: Updated 05-system-wide.md with all 10 audit corrections applied.
</objective>

<execution_context>
@/Users/andeslee/.claude/get-shit-done/workflows/execute-plan.md
@/Users/andeslee/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@docs/plans/bot-improvements/05-system-wide.md
</context>

<tasks>

<task type="auto">
  <name>Task 1: Rewrite 05-system-wide.md with all 10 audit corrections</name>
  <files>docs/plans/bot-improvements/05-system-wide.md</files>
  <action>
Read the current file at docs/plans/bot-improvements/05-system-wide.md, then rewrite it applying ALL of the following changes. Keep the same overall structure (Executive Summary, Issues, Cross-Bot Improvements, Pre-Production Checklist, File Index) but update content as specified.

**1. Remove SYS-2 entirely.** The `_effective_max_trade_cents` bug is already fixed (uses `min()` not `max()`). Delete the entire SYS-2 section. Update Executive Summary to remove the "sizing ceiling/floor bug" mention.

**2. Rewrite SYS-1.** The reconciliation pipeline is fully built (`reconcile-trades.py`, `backfill-settlements.py`, `daily-automation.sh`). The real remaining issue is that daily automation is not scheduled — a LaunchAgent template exists but is not installed on the Mac Mini. Rewrite to say:
- Pipeline scripts exist and work
- Problem: nothing triggers them automatically
- Mark as "Mac Mini action needed"
- Keep the `npm run reconcile` / `npm run backfill` commands as reference

**3. Rewrite SYS-5 (Position Monitor Exit Limit).** The config already has `maxDailyExits: 20` which is reasonable. The real issue may be a timezone bug in the daily reset counter (see new SYS-7). Rewrite to:
- Note the config value is reasonable (20)
- State the real suspect is the daily reset timezone bug
- Cross-reference SYS-7
- Mark as "Needs Mac Mini investigation"

**4. Rewrite CROSS-2 (Edge Field Standardization).** The current claims are partially wrong. Correct version:
- Economics actually passes BOTH `edge` AND `raw_edge` (not "edge is null")
- 7 of 10 bots correctly use `raw_edge` (float)
- The real inconsistency: strategy-trader uses `est_edge` (string like "5%") and has NO `model_prob`; market-maker has no edge fields at all
- Reconciliation scripts use `model_prob` not `raw_edge`, so strategy-trader and market-maker trades can never get `realized_edge` computed
- Fix should focus on strategy-trader and market-maker, not all bots

**5. Promote CROSS-3 to SYS-7: Timezone Bug in Daily Resets.** This is a concrete bug, not just a "verify" item. Write as a proper SYS issue with:
- `capital_allocator.py:382` uses `datetime.date.today()` (local system time)
- `capital_allocator.py:521` uses `datetime.date.today()` (in `_risk_today_cents`)
- `kalshi_auth.py:1030` uses `datetime.date.today()` (in TradeManager)
- `_local_today()` exists with proper ZoneInfo but is never called in reset paths
- Fix: replace all `datetime.date.today()` in reset paths with `datetime.datetime.now(datetime.timezone.utc).date()` or call `_local_today()`
- Mark as "Code fix needed (can be done from MacBook but needs Mac Mini testing)"

**6. Add SYS-8: Daily Automation Not Scheduled.** New issue:
- The pipeline exists (reconcile, backfill, report, daily-backtest)
- LaunchAgent template exists but has placeholder path and is not installed
- This is a Mac Mini action item
- Include what needs to happen: edit path in plist, copy to ~/Library/LaunchAgents, load with launchctl

**7. Add SYS-9: Dependency Management.** New issue:
- `feedparser` crash in economics bot shows no import validation at startup
- Bots should validate critical imports on startup and fail with clear error messages
- No requirements.txt enforcement or virtual environment on production Mac Mini

**8. Add to Cross-Bot Improvements:**
- CROSS-5: Dashboard Not Supervisor-Managed — dashboard has no crash recovery; if it dies, no auto-restart. Should be added to supervisor bot list.
- CROSS-6: No Config Schema Validation — malformed JSON in config files crashes bots silently. Add JSON schema validation or at minimum a startup config check.

**9. Update Pre-Production Checklist:**
- Remove "Sizing ceiling bug" line (already fixed)
- Add: "Timezone standardization for daily resets (SYS-7)"
- Add: "Daily automation scheduling (LaunchAgent or cron) (SYS-8)"
- Add: "Dependency validation at startup (SYS-9)"
- Add: "Config schema validation (CROSS-6)"

**10. Add "Mac Mini Action Items" section** at the very end (before File Index). Consolidated list:
- Install LaunchAgent for daily automation (SYS-8)
- Install missing dependencies: feedparser (SYS-9)
- Investigate position monitor exit blocking (SYS-5)
- Verify timezone behavior of daily resets (SYS-7)
- Restart stale bots: crypto, beatrelease (SYS-3)

Renumber SYS issues sequentially after removing SYS-2 (keep original numbers but note SYS-2 as removed/fixed, do NOT renumber existing — just add new ones as SYS-7, SYS-8, SYS-9).
  </action>
  <verify>
    <automated>grep -c "SYS-7\|SYS-8\|SYS-9\|CROSS-5\|CROSS-6\|Mac Mini Action Items" docs/plans/bot-improvements/05-system-wide.md | xargs test 6 -le</automated>
  </verify>
  <done>
- SYS-2 section removed, Executive Summary updated
- SYS-1 rewritten to reflect pipeline exists but is unscheduled
- SYS-5 rewritten to reference timezone bug (SYS-7)
- CROSS-2 corrected with accurate edge field analysis
- SYS-7 (timezone bug), SYS-8 (daily automation), SYS-9 (dependency management) added
- CROSS-5 (dashboard not supervised), CROSS-6 (config validation) added
- Pre-Production Checklist updated (sizing bug removed, 4 new items)
- Mac Mini Action Items section present at end
  </done>
</task>

</tasks>

<verification>
grep for "SYS-7", "SYS-8", "SYS-9", "CROSS-5", "CROSS-6", "Mac Mini Action Items" in the output file — all must be present. grep for "max() Not min()" — must NOT be present (SYS-2 removed).
</verification>

<success_criteria>
All 10 audit corrections applied. Document is internally consistent. No stale information remains.
</success_criteria>

<output>
After completion, create `.planning/quick/13-fix-system-wide-plan-based-on-audit-find/13-SUMMARY.md`
</output>
