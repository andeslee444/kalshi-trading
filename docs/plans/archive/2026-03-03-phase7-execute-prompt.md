# Phase 7 Execution Prompt

Paste this into a new Claude Code session:

---

Execute the implementation plan at `docs/plans/2026-03-03-phase7-microstructure-intelligence.md` — Phase 7: Market Microstructure & Intelligence.

This is the final phase of the consolidated quant desk roadmap. All 6 preceding phases are complete and merged to main. The plan has 16 tasks with full inline code (tests first, then implementation). Each task is independently committable.

**Key instructions:**
- Use `/superpowers:executing-plans` skill to manage task-by-task execution
- Git author for ALL commits: `--author="Andes Lee <andes.lee444@gmail.com>"`
- Branch: `phase7/microstructure` (from `main`)
- TDD: commit failing tests first, then commit passing implementation
- Run `pytest tests/ -v --tb=short` after each implementation task to verify no regressions
- The plan file contains complete code for every task — paste it directly, don't improvise
- New modules (`pnl_attribution.py`, `edge_monitor.py`, `execution_quality.py`, `orderbook_sim.py`) have NO `kalshi_auth` dependency — they're pure analytics modules
- For Task 11 (market-maker.py modifications), read the existing file first and apply the changes surgically
- For Task 12-13 (dashboard), read existing `scripts/dashboard.py` and `scripts/dashboard.html` before modifying
- For Task 15 (allocator integration), read existing `src/kalshi/capital_allocator.py` before modifying

Start with Task 1 (create branch) and work through sequentially to Task 16.
