---
phase: quick-4
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - src/kalshi/probability.py
  - src/kalshi/kalshi_auth.py
  - src/kalshi/economics-bot.py
  - src/kalshi/macro_engine.py
  - src/kalshi/cpi_belief_filter.py
  - src/kalshi/scenario_engine.py
  - tests/test_probability.py
  - tests/test_sizing_ceiling.py
  - tests/test_econ_concentration.py
  - tests/test_cpi_belief_filter.py
  - tests/test_scenario_engine.py
  - tests/test_uncertainty_kelly.py
  - tests/test_edge_scaler.py
  - tests/test_econ_integration.py
autonomous: true
requirements: [ECON-REDESIGN]
must_haves:
  truths:
    - "CPI sigma at 107 days is ~0.40% (not 0.10%), preventing fake certainty"
    - "Static maxTradeAmount/maxDailyLoss is a ceiling (min), not floor (max)"
    - "Economics bot refuses trades exceeding 15% bankroll per ticker family or 40% total econ"
    - "MacroEngine import failure does not crash the bot"
    - "Edge field is populated in all economics trade records"
    - "CPIBeliefFilter fuses 3 sources into tighter posterior than any single source"
    - "Scenario engine produces 6-scenario mixture probability that never returns fake 1.0"
    - "uncertainty_kelly scales position size down when agreement is low or sigma is wide"
    - "EdgeScaler starts at 20% exposure and ratchets up with profitable settlements"
    - "Full pipeline (filter -> scenarios -> sizing) produces reasonable probabilities and positions"
  artifacts:
    - path: "src/kalshi/cpi_belief_filter.py"
      provides: "Bayesian conjugate normal filter for CPI nowcast fusion"
      exports: ["CPIBeliefFilter"]
    - path: "src/kalshi/scenario_engine.py"
      provides: "6-scenario macro model with dynamic weights"
      exports: ["SCENARIOS", "DEFAULT_WEIGHTS", "compute_scenario_weights", "scenario_probability", "ScenarioResult"]
    - path: "tests/test_cpi_belief_filter.py"
      provides: "Belief filter unit tests"
    - path: "tests/test_scenario_engine.py"
      provides: "Scenario engine unit tests"
    - path: "tests/test_uncertainty_kelly.py"
      provides: "Confidence-scaled sizing tests"
    - path: "tests/test_econ_integration.py"
      provides: "Full pipeline integration tests"
  key_links:
    - from: "src/kalshi/economics-bot.py"
      to: "src/kalshi/cpi_belief_filter.py"
      via: "from cpi_belief_filter import CPIBeliefFilter"
      pattern: "CPIBeliefFilter"
    - from: "src/kalshi/economics-bot.py"
      to: "src/kalshi/scenario_engine.py"
      via: "from scenario_engine import compute_scenario_weights, scenario_probability"
      pattern: "scenario_probability"
    - from: "src/kalshi/economics-bot.py"
      to: "src/kalshi/probability.py"
      via: "from probability import uncertainty_kelly"
      pattern: "uncertainty_kelly"
---

<objective>
Implement the full economics bot world-class redesign from the detailed plan at
`docs/plans/2026-03-04-economics-bot-world-class.md`.

This transforms the economics bot from a single-distribution nowcast trader into a
scenario-weighted Bayesian system with honest uncertainty modeling, proper data fusion,
and adaptive position sizing.

Purpose: Fix 5 critical bugs (fake sigma, sizing floor/ceiling inversion, no concentration
limits, fragile macro import, missing edge field) and add 3 new capabilities (Bayesian
belief filter, 6-scenario mixture model, uncertainty-aware Kelly sizing with edge scaling).

Output: 2 new modules, 9 new test files, 4 modified source files, all tests passing.
</objective>

<execution_context>
@docs/plans/2026-03-04-economics-bot-world-class.md
</execution_context>

<context>
@src/kalshi/economics-bot.py
@src/kalshi/probability.py
@src/kalshi/kalshi_auth.py
@src/kalshi/macro_engine.py
@tests/test_probability.py

The implementation plan at `docs/plans/2026-03-04-economics-bot-world-class.md` contains
EXACT code for every change. Follow it task-by-task. The plan has 16 tasks across 6 phases.
This executor plan groups them into 3 tasks for context efficiency.
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Phase 1 Bug Fixes (Plan Tasks 1-5)</name>
  <files>
    src/kalshi/probability.py
    src/kalshi/kalshi_auth.py
    src/kalshi/economics-bot.py
    tests/test_probability.py
    tests/test_sizing_ceiling.py
    tests/test_econ_concentration.py
  </files>
  <behavior>
    - CPI sigma: cpi_nowcast_sigma(0) ~= 0.05, cpi_nowcast_sigma(107) ~= 0.40, monotonically increasing
    - Sizing ceiling: _effective_max_trade_cents uses min() not max(), so $50 static cap is ceiling for $4K balance at 5%
    - Concentration: _ticker_family extracts family, _check_concentration blocks at 15%/25%/40% limits
    - MacroEngine import: try/except wraps import, macro=None when feedparser missing
    - Edge field: edge=round(edge,4) added to place_order call
  </behavior>
  <action>
Follow Tasks 1-5 from `docs/plans/2026-03-04-economics-bot-world-class.md` EXACTLY. The plan contains
complete code for each change. Execute in order:

**Task 1 (BUG-1): Widen CPI sigma asymptote**
- Update tests in `tests/test_probability.py` class `TestCpiNowcastSigma` with new assertions per plan
- Run tests (expect FAIL)
- Update `cpi_nowcast_sigma()` formula in `probability.py`: `0.05 + 0.35 * (1 - exp(-0.05 * d))`
- Update docstring
- Run tests (expect PASS)
- Commit: "fix: widen CPI sigma asymptote from 0.10% to 0.40%"

**Task 2 (BUG-2): Position sizing ceiling**
- Create `tests/test_sizing_ceiling.py` with tests per plan
- Run tests (expect FAIL on ceiling tests)
- Change `max()` to `min()` in `_effective_max_trade_cents()` and `_effective_max_daily_loss_cents()` in `kalshi_auth.py`
- Run tests (expect PASS)
- Commit: "fix: static config is ceiling not floor for trade/loss limits"

**Task 3 (BUG-3): Concentration limits**
- Create `tests/test_econ_concentration.py` with tests per plan
- Add `_ticker_family()`, `_compute_exposure()`, `_check_concentration()` to economics-bot.py after `_classify_econ_market()`
- Wire concentration check into `scan_and_trade()` before price computation
- Run tests, commit: "fix: add per-ticker-family and total econ concentration limits"

**Task 4 (BUG-4): Optional MacroEngine import**
- Wrap `from macro_engine import MacroEngine` in try/except, set `MacroEngine = None`
- Guard `MacroEngine(...)` instantiation with `if MacroEngine`
- Guard `macro.compute_signal()` and `macro.compute_sigma_multiplier()` with `if macro is not None`
- Commit: "fix: make MacroEngine import optional for feedparser resilience"

**Task 5 (BUG-5): Edge field in trade records**
- Add `edge=round(edge, 4)` to the `place_order()` call in economics-bot.py
- Commit: "fix: populate edge field in economics trade records"

After all 5, run: `pytest tests/ -x -q` to verify no regressions.

IMPORTANT: Use `git commit --author="Andes Lee <andes.lee444@gmail.com>"` for all commits.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_probability.py::TestCpiNowcastSigma tests/test_sizing_ceiling.py tests/test_econ_concentration.py -v</automated>
  </verify>
  <done>
    - cpi_nowcast_sigma(107) returns ~0.40 (not 0.10)
    - _effective_max_trade_cents uses min() for ceiling behavior
    - Concentration limit helpers exist and are wired into scan_and_trade
    - MacroEngine import wrapped in try/except
    - edge= kwarg present in place_order call
    - All 5 commits created, all tests pass
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: New Modules - Belief Filter + Scenario Engine (Plan Tasks 6-10)</name>
  <files>
    src/kalshi/cpi_belief_filter.py
    src/kalshi/scenario_engine.py
    src/kalshi/economics-bot.py
    src/kalshi/macro_engine.py
    tests/test_cpi_belief_filter.py
    tests/test_scenario_engine.py
  </files>
  <behavior>
    - CPIBeliefFilter: conjugate normal, posterior precision = sum of precisions, 3 sources tighter than any single
    - ScenarioEngine: 6 scenarios (status_quo/tariff_escalation/tariff_reversal/supply_shock/recession/stagflation), weights sum to 1.0, mixture probability never reaches 1.0
    - FRED series: crude_oil (DCOILWTICO) and yield_curve (T10Y2Y) added to macro_engine.py
    - Wiring: economics bot uses belief filter for nowcast fusion, scenario engine for probability
  </behavior>
  <action>
Follow Tasks 6-10 from `docs/plans/2026-03-04-economics-bot-world-class.md` EXACTLY.

**Task 6: Create CPIBeliefFilter module**
- Create `tests/test_cpi_belief_filter.py` with all test classes per plan (TestFilterConstruction, TestSingleUpdate, TestMultipleUpdates, TestGracefulDegradation, TestEdgeCases)
- Run tests (expect FAIL: ModuleNotFoundError)
- Create `src/kalshi/cpi_belief_filter.py` with CPIBeliefFilter class (conjugate normal Bayesian filter)
- Run tests (expect PASS)
- Commit: "feat: add CPIBeliefFilter -- Bayesian fusion for CPI nowcast sources"

**Task 7: Wire belief filter into economics bot**
- Add `from cpi_belief_filter import CPIBeliefFilter` import
- Add Truflation and TIPS data fetching in scan_and_trade() (guarded by `if macro is not None`)
- Replace macro bias adjustment with belief filter: construct CPIBeliefFilter with nowcast as prior, update with truflation and TIPS
- Use fused_nowcast and posterior_sigma in probability computation
- Add filter state to opportunity dicts and trade records
- Run tests, commit: "feat: wire Bayesian belief filter into economics bot"

**Task 8: Create scenario engine module**
- Create `tests/test_scenario_engine.py` with all test classes per plan (TestDefaultWeights, TestComputeWeights, TestScenarioProbability)
- Run tests (expect FAIL: ModuleNotFoundError)
- Create `src/kalshi/scenario_engine.py` with SCENARIOS, DEFAULT_WEIGHTS, compute_scenario_weights(), scenario_probability(), ScenarioResult
- Run tests (expect PASS)
- Commit: "feat: add scenario engine -- 6 macro scenarios with dynamic weights"

**Task 9: Add FRED proxy series**
- Add "crude_oil": "DCOILWTICO" and "yield_curve": "T10Y2Y" to FREDClient.SERIES in macro_engine.py
- Run tests, commit: "feat: add crude oil and yield curve FRED series for scenario weights"

**Task 10: Wire scenario engine into economics bot**
- Add `from scenario_engine import compute_scenario_weights, scenario_probability` import
- Fetch FRED scenario data and compute scenario weights in scan_and_trade()
- Replace econ_nowcast_probability() call with scenario_probability() mixture
- Add scenario_agreement and per_scenario to opportunity dicts and trade records
- Run tests, commit: "feat: wire scenario engine into economics bot"

IMPORTANT: Use `git commit --author="Andes Lee <andes.lee444@gmail.com>"` for all commits.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_cpi_belief_filter.py tests/test_scenario_engine.py -v</automated>
  </verify>
  <done>
    - cpi_belief_filter.py exists with CPIBeliefFilter class
    - scenario_engine.py exists with 6 scenarios, dynamic weights, mixture probability
    - economics-bot.py imports and uses both modules
    - macro_engine.py has crude_oil and yield_curve FRED series
    - All 5 commits created, all tests pass
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 3: Uncertainty Sizing + Integration Tests (Plan Tasks 11-16)</name>
  <files>
    src/kalshi/probability.py
    src/kalshi/economics-bot.py
    tests/test_uncertainty_kelly.py
    tests/test_edge_scaler.py
    tests/test_econ_integration.py
  </files>
  <behavior>
    - uncertainty_kelly: wraps quarter_kelly with agreement_mult and sigma_mult, geometric mean confidence, min 1 contract if base > 0
    - EdgeScaler: 4 tiers (0/5/10/20 wins -> 20/40/60/100%), 3-loss streak halves limit
    - Integration: full pipeline (filter -> scenarios -> sizing) produces prob < 0.99, count > 0, confidence < 1.0
  </behavior>
  <action>
Follow Tasks 11-16 from `docs/plans/2026-03-04-economics-bot-world-class.md` EXACTLY.

**Task 11: Add uncertainty_kelly() function**
- Create `tests/test_uncertainty_kelly.py` with all test classes per plan
- Run tests (expect FAIL: ImportError)
- Add `uncertainty_kelly()` to `probability.py` after `quarter_kelly()` -- wraps quarter_kelly with agreement_mult and sigma_mult confidence scaling
- Run tests (expect PASS)
- Commit: "feat: add uncertainty_kelly -- confidence-scaled position sizing"

**Task 12: Wire uncertainty_kelly into economics bot**
- Add `uncertainty_kelly` to probability import in economics-bot.py
- Replace quarter_kelly call with uncertainty_kelly for CPI/GDP markets (keep quarter_kelly for gas/fed)
- Update sizing_method in trade record
- Run tests, commit: "feat: use uncertainty_kelly for CPI/GDP markets"

**Task 13: Create EdgeScaler**
- Create `tests/test_edge_scaler.py` with inline EdgeScaler class and tests per plan
- Add EdgeScaler class to economics-bot.py after concentration helpers
- Wire into scan_and_trade: compute max_exposure_pct from settlement record
- Run tests, commit: "feat: add EdgeScaler -- auto-scaling exposure based on settlements"

**Task 14: Comprehensive integration tests**
- Create `tests/test_econ_integration.py` with TestFullPipeline per plan
- Tests cover: typical CPI trade, no-extra-sources degradation, tariff shock, near-threshold
- Run tests (expect PASS)
- Commit: "test: add integration tests for full economics bot pipeline"

**Task 15: Run full test suite and fix regressions**
- Run `pytest tests/ -v --tb=short`
- Fix any regressions (likely old sigma assertions, import changes)
- Commit if fixes needed: "fix: resolve test regressions from economics bot redesign"

**Task 16: Final verification**
- Run `pytest tests/ -v` -- all pass
- Verify imports: `python3 -c "from cpi_belief_filter import CPIBeliefFilter; print('OK')"` from src/kalshi/
- Verify imports: `python3 -c "from scenario_engine import scenario_probability; print('OK')"` from src/kalshi/
- Run `git log --oneline -15` to verify clean commit history
- Update CLAUDE.md if needed to document new modules (cpi_belief_filter.py, scenario_engine.py)

IMPORTANT: Use `git commit --author="Andes Lee <andes.lee444@gmail.com>"` for all commits.
  </action>
  <verify>
    <automated>cd /Users/andeslee/Documents/Cursor-Projects/kalshi-trading && pytest tests/test_uncertainty_kelly.py tests/test_edge_scaler.py tests/test_econ_integration.py tests/ -x -q</automated>
  </verify>
  <done>
    - uncertainty_kelly exists in probability.py and is used by economics bot for CPI/GDP markets
    - EdgeScaler class exists in economics-bot.py with 4-tier ratchet
    - Integration tests pass: full pipeline produces reasonable probabilities and positions
    - ALL tests pass (pytest tests/ -x -q returns 0 failures)
    - Clean commit history with 3-5 commits for this task
  </done>
</task>

</tasks>

<verification>
1. `pytest tests/ -v` -- all tests pass (existing + 9 new test files)
2. `python3 -c "import sys; sys.path.insert(0, 'src/kalshi'); from cpi_belief_filter import CPIBeliefFilter; print('OK')"` -- imports clean
3. `python3 -c "import sys; sys.path.insert(0, 'src/kalshi'); from scenario_engine import scenario_probability; print('OK')"` -- imports clean
4. `python3 -c "import sys; sys.path.insert(0, 'src/kalshi'); from probability import uncertainty_kelly; print('OK')"` -- imports clean
5. `git log --oneline -20` -- shows ~13 clean commits for the full redesign
</verification>

<success_criteria>
- 5 bug fixes landed: sigma asymptote, sizing ceiling, concentration limits, optional macro import, edge field
- 2 new modules created: cpi_belief_filter.py, scenario_engine.py
- 1 new function added: uncertainty_kelly in probability.py
- 1 new class added: EdgeScaler in economics-bot.py
- 9 new test files created, all passing
- Full test suite passes with no regressions
- Economics bot uses Bayesian fusion -> scenario mixture -> uncertainty-aware Kelly for CPI/GDP markets
</success_criteria>

<output>
After completion, create `.planning/quick/4-implement-economics-bot-world-class-rede/4-SUMMARY.md`
</output>
