# Weather Outage Recovery Audit Note

Generated: 2026-04-02
Status: Implemented, validated, and cut over live.

## Goal

Record the April 2, 2026 weather-family outage recovery and hardening work:

- eliminate the stale drawdown halt that kept blocking both weather runtimes after the outage
- make the drawdown/kill-switch path recover automatically when API balance data recovers
- repair local weather-family reconciliation so source-monitor and forecast-weather joins no longer collapse to zero on modern payloads
- cut over the live weather runtimes from legacy detached launches to supervisor-managed canonical processes
- finish weather-family attribution promotion so source-monitor NWS realized P&L can be counted directly when local fill coverage is complete
- extend the forecast-weather Open-Meteo stack only where live testing shows the premium endpoints produce usable incremental signal

This note is the manager-facing audit trail for the outage-recovery change set.

## Problem Statement

Before this change set:

- `weather-bot.py` and source-monitor weather were alive, but both were functionally constrained by stale risk state after multi-hour API/network failures
- allocator drawdown logic used cash plus `market_exposure`, which could stay stale/wrong after balance fetch failures
- automated `data/HALT_TRADING` could remain in place even after the true account NAV recovered
- local per-bot reconciliation had collapsed:
  - legacy/null local `action` fields were being dropped
  - Kalshi v2 fills (`count_fp`, `yes_price_dollars`, `no_price_dollars`) were not being joined correctly
- weather runtimes were still running as legacy bare-script processes (`weather-bot.py`, `source-monitor.py`), so supervisor status/restarts did not fully control them

## Implemented Changes

### 1. Automatic drawdown recovery

Changed:

- `src/kalshi/infra/kalshi_client.py`
- `src/kalshi/capital_allocator.py`

What changed:

- `KalshiClient.get_balance()` now stores `portfolio_value` from `/portfolio/balance` alongside `market_exposure`
- allocator drawdown checks now prefer live `portfolio_value` when available
- allocator only auto-clears `data/HALT_TRADING` when the file is an allocator-created automated drawdown halt
- manual/operator halt files are preserved and are not auto-cleared

Effect:

- after API recovery, a stale automated drawdown halt can clear automatically without manual file deletion
- the drawdown check is based on current account NAV semantics instead of stale exposure-only math

### 2. Local reconciliation repair

Changed:

- `scripts/pnl-snapshot.py`
- `scripts/reconcile-trades.py`
- `scripts/backfill-settlements.py`
- `scripts/weather-observation-pack.py`

What changed:

- legacy/null local `action` values are now treated as buy-side records where appropriate
- local reconciliation now understands Kalshi v2 fill fields:
  - `count_fp`
  - `yes_price_dollars`
  - `no_price_dollars`
- the shared integer coercion path now accepts decimal-string payloads like `"2.00"`

Effect:

- `financial-snapshot.json` local/API join no longer collapses to zero
- reconciliation improved from:
  - `matched_orders = 0`
  - to `matched_orders = 423`
- `unmatched_executed_buy_orders_without_fills` improved from:
  - `565`
  - to `0`

### 3. Live runtime cutover

Changed live state:

- terminated legacy detached weather-family processes:
  - weather PID `1187`
  - source-monitor PID `1188`
- restarted canonical supervisor-managed entries:
  - `src/kalshi/weather-bot.py`
  - `src/kalshi/source-monitor.py`

Effect:

- supervisor now owns the live weather-family runtimes again
- current live PIDs are canonical:
  - weather PID `85627`
  - monitor PID `85631`

### 4. Per-bot hybrid attribution promotion

Changed:

- `scripts/pnl-snapshot.py`
- `scripts/weather-observation-pack.py`

What changed:

- `financial-snapshot.json` no longer uses an all-or-nothing by-bot attribution basis
- the snapshot now selects canonical realized P&L per bot:
  - local joined fills when per-bot reconciliation is complete enough
  - API settlements otherwise
- `weather-observation-pack.py` now consumes that per-bot basis directly
- source-monitor NWS reporting is promoted when:
  - `source-monitor` is on the local joined-fill basis in `financial-snapshot.json`
  - per-bot reconciliation marks it `eligible_local_join_basis = true`

Effect:

- source-monitor NWS realized P&L is now attributable inside the weather-family pack
- forecast-weather can also use local joined fills once every executed-and-settled canonical weather order is covered
- API-only orphan fills still remain visible in reconciliation stats, but they no longer veto the canonical weather bot basis
- weather-family realized P&L is now a mixed but explicit hybrid:
  - forecast-weather from local joined fills
  - source-monitor NWS from local joined fills

### 5. Shadow weather refresh

Changed:

- `scripts/weather-shadow-refresh.py`
- refreshed outputs under `data/shadow/weather-refresh/`

What changed:

- reran the full shadow weather bundle after the live attribution repair
- refreshed shadow backtest, calibration, observation pack, and promotion candidates

Effect:

- the shadow weather analysis is no longer stale relative to the repaired post-outage live state
- April promotion review can now use current shadow artifacts instead of the older March 23 pack

### 6. Weather-family execution coordination follow-on

Changed:

- `src/kalshi/capital_allocator.py`
- `src/kalshi/apps/weather_bot.py`
- `src/kalshi/apps/source_monitor.py`
- `src/kalshi/execution/trade_manager.py`
- `config/kalshi-config.json`
- `config/kalshi-monitor-config.json`

What changed:

- allocator signal quality now distinguishes forecast-weather vs observed NWS weather:
  - `forecast_weather`
  - `nws`
- `weather-bot.py` now yields same-day weather markets to the observed NWS track by default
- `weather-bot.py` now supports narrow near-close taker escalation for same-day high-confidence setups when the passive edge advantage is small
- source-monitor NWS can now use a narrow late-day high-confidence daily-loss override instead of relying on the generic source-monitor daily-loss cap
- both live weather-family runtimes now record their source type back into allocator trade state:
  - forecast weather -> `forecast_weather`
  - source-monitor weather -> `nws`

Effect:

- forecast weather and observed NWS weather no longer rely only on coarse bot-level dedup; they now coordinate with track-aware policy
- same-day `KXHIGH` conflicts are reduced by policy before they reach order placement
- if a same-day conflict still exists, allocator supersede now favors the stronger observed NWS signal more explicitly
- forecast-weather execution has a new narrow path to convert a strong same-day late passive idea into taker flow when waiting is likely to miss the trade
- source-monitor late-day observed-weather trading can continue on the best threshold setups without raising the entire bot's daily loss limit

### 7. Open-Meteo premium-capability expansion

Changed:

- `scripts/backfill-weather-data.py`
- `scripts/weather-shadow-refresh.py`
- `src/kalshi/weather_data.py`
- `src/kalshi/apps/weather_bot.py`
- `scripts/weather-intraday-feature-audit.py`

What changed:

- extended the lead-time-matched historical prior pipeline to include `nbm`
- kept `aifs` as an explicit experimental model option, but did not promote it into the default live/shadow prior refresh set
- added research-only HRRR `minutely_15` capture and audit summaries for same-day weather risk:
  - temperature
  - CAPE
  - precipitation
  - cloud cover
  - dewpoint
- hardened `weather-bot.py` so a model that returns HTTP success but all-null daily highs is treated as unavailable instead of as valid model coverage

Evidence before promotion:

- local weather trade history showed `nbm` present on live forecast-weather trades but absent from `config/weather-live-bias.json`
- the promoted live bias file now contains `ecmwf`, `gem`, `gfs`, `graphcast`, `icon`, and `nbm`
- same-day weather trades are material enough to justify intraday research:
  - same-day trade rows: `135 / 316`
  - same-day settled rows: `130 / 302`
- `aifs` was tested end to end against Open-Meteo customer endpoints and still produced zero usable historical training pairs for prior generation, so it was explicitly held back from default live usage

Effect:

- forecast-weather historical priors now cover `nbm`, which was already a live forecast input but previously had zero prior-bias coverage
- the live weather runtime now loads the refreshed prior and no longer reports `aifs` as available when the endpoint returns all-null values
- the new intraday audit provides a research surface for same-day weather regimes without changing live pricing logic

## Validation

Tests:

- `pytest tests/test_allocator.py tests/test_pnl_snapshot.py tests/test_reconciliation.py tests/test_backfill_settlements_script.py tests/test_weather_observation_pack.py tests/test_trade_manager.py tests/test_risk_controls.py -q`
- result: `252 passed`
- `pytest tests/test_pnl_snapshot.py tests/test_weather_observation_pack.py tests/test_weather_promotion_candidates.py tests/test_backfill_settlements_script.py tests/test_reconciliation.py tests/test_trade_manager.py tests/test_risk_controls.py -q`
- result: `209 passed`

Static validation:

- `python3 -m py_compile scripts/pnl-snapshot.py scripts/reconcile-trades.py scripts/backfill-settlements.py scripts/weather-observation-pack.py src/kalshi/capital_allocator.py src/kalshi/infra/kalshi_client.py`
- `python3 -m py_compile scripts/pnl-snapshot.py scripts/weather-observation-pack.py scripts/daily-imessage-report.py scripts/daily-ops-loop.py src/kalshi/apps/weather_bot.py src/kalshi/apps/source_monitor.py`

Final follow-on validation:

- `pytest tests/test_pnl_snapshot.py tests/test_weather_observation_pack.py tests/test_source_monitor.py tests/test_weather.py tests/test_daily_imessage_report.py tests/test_daily_ops_loop.py -q`
- result: `217 passed`
- `pytest tests/test_weather_promotion_candidates.py tests/test_weather_shadow_refresh.py tests/test_backfill_settlements_script.py tests/test_reconciliation.py tests/test_trade_manager.py tests/test_risk_controls.py -q`
- result: `138 passed`
- `python3 scripts/pnl-snapshot.py`
- `python3 scripts/weather-observation-pack.py --save`
- `python3 scripts/weather-promotion-candidates.py --save`
- `python3 scripts/weather-shadow-refresh.py`
- `python3 scripts/daily-imessage-report.py --dry-run`
- `python3 scripts/daily-ops-loop.py --skip-refresh --dry-run --json`

Execution coordination validation:

- `pytest tests/test_allocator.py tests/test_weather.py tests/test_source_monitor.py tests/test_trade_manager.py -q`
- result: `295 passed`
- `pytest tests/test_allocator.py tests/test_weather.py tests/test_source_monitor.py tests/test_trade_manager.py tests/test_risk_controls.py tests/test_weather_observation_pack.py tests/test_weather_promotion_candidates.py tests/test_weather_shadow_refresh.py -q`
- result: `331 passed`
- `python3 -m py_compile src/kalshi/capital_allocator.py src/kalshi/apps/weather_bot.py src/kalshi/apps/source_monitor.py src/kalshi/execution/trade_manager.py`
- `python3 scripts/pnl-snapshot.py`
- `python3 scripts/weather-observation-pack.py --save`
- `python3 scripts/weather-promotion-candidates.py --save`
- `python3 scripts/weather-shadow-refresh.py`
- `python3 scripts/supervisor.py restart weather monitor`

Open-Meteo expansion validation:

- `pytest tests/test_weather_backfill_scripts.py tests/test_weather_shadow_refresh.py tests/test_weather.py tests/test_weather_data.py tests/test_weather_intraday_feature_audit.py -q`
- result: `203 passed`
- `python3 -m py_compile scripts/backfill-weather-data.py scripts/weather-shadow-refresh.py scripts/weather-intraday-feature-audit.py src/kalshi/weather_data.py src/kalshi/apps/weather_bot.py`
- `python3 scripts/weather-shadow-refresh.py --refresh-shadow-prior --json`
- `python3 scripts/backfill-weather-data.py --days 7 --city MIA --models aifs --db-path data/shadow/weather-aifs-probe.db`
  - result: zero training pairs; `aifs` not promoted into defaults
- `python3 scripts/backfill-weather-data.py --days 30 --models gfs,ecmwf,icon,gem,graphcast,nbm --db-path data/weather-training.db`
- `python3 scripts/calibrate-weather-bias.py --db-path data/weather-training.db --output config/weather-live-bias.json --json`
- `python3 scripts/weather-intraday-feature-audit.py --save --json`
- `python3 scripts/supervisor.py restart weather`

Artifacts refreshed:

- `data/financial-snapshot.json`
- `data/weather-observation-pack.json`
- `data/weather-promotion-candidates.json`
- `data/weather-intraday-feature-audit.json`
- `data/shadow/weather-refresh/weather-observation-pack.json`
- `data/shadow/weather-refresh/weather-calibration.json`
- `data/shadow/weather-refresh/weather-backtest-results.json`
- `data/shadow/weather-refresh/weather-intraday-feature-audit.json`
- `data/weather-training.db`
- `config/weather-live-bias.json`

## Live Recovery Evidence

As of April 2, 2026 after cutover:

- `data/HALT_TRADING` is absent
- supervisor status shows:
  - `weather` running, PID `47790`
  - `monitor` running, PID `47803`
- `weather.log` shows the restarted bot picked up the new config:
  - `Mode: demo | Max: $30/trade | Edge: 10% | City edge overrides: 3`
- `weather.log` shows a fresh post-cutover scan and verification cycle after restart
- `source-monitor.log` shows the restarted source-monitor completed a fresh NWS scan and resumed signal evaluation
- `daily-ops-loop.json` now records:
  - `by_bot_source = financial_snapshot.by_bot`
  - `by_bot_basis_map` present in `explain.trading_outcome`

Open-Meteo live-state addendum after the April 3 follow-on refresh:

- `config/weather-live-bias.json` generated at `2026-04-03T04:27:49.431994+00:00`
- live prior now covers:
  - `ecmwf`
  - `gem`
  - `gfs`
  - `graphcast`
  - `icon`
  - `nbm`
- `config/weather-live-bias.json` now contains `8200` forecast pairs
- after `python3 scripts/supervisor.py restart weather`, `weather.log` shows:
  - `BiasCorrector loaded from weather-live-bias.json: 8200 forecast pairs`
  - `Ensemble models available: ecmwf, gfs, graphcast, icon, nbm (20/20 cities)`
- `aifs` is intentionally absent from the live availability line above because the runtime now drops all-null model payloads instead of treating them as valid model data
- `data/weather-intraday-feature-audit.json` generated successfully at `2026-04-03T04:33:40.201010+00:00`

## Current Financial/Reconciliation State

From refreshed `data/financial-snapshot.json`:

- NAV: `477477` cents
- deposits: `500000` cents
- true total P&L: `-22523` cents
- by-bot basis remains:
  - `hybrid_per_bot_local_or_api`

Current per-bot attribution basis:

- `weather`:
  - `local_buy_orders_joined_to_api_fills_and_settlement_outcomes`
- `source-monitor`:
  - `local_buy_orders_joined_to_api_fills_and_settlement_outcomes`
- `strategy`:
  - `local_buy_orders_joined_to_api_fills_and_settlement_outcomes`
- `demo-weather-history`:
  - `kalshi_api_settlements`
- `unattributed-weather`:
  - `kalshi_api_settlements`
- most other bots:
  - `kalshi_api_settlements`

Current local/API reconciliation state:

- `matched_orders = 423`
- `local_buy_orders = 1373`
- `unmatched_local_buy_orders_without_fills = 637`
- `unmatched_executed_buy_orders_without_fills = 0`
- `unmatched_fills_without_local_trade = 93`
- `unmatched_filled_orders_without_settlement = 313`
- `unattributed-weather.api_fills_without_local_order = 23`

Weather-family attribution state:

- forecast-weather realized:
  - `16876` cents
- source-monitor NWS realized:
  - `73508` cents
- combined weather-family realized:
  - `90384` cents
- weather-family realized leader:
  - `source_monitor_nws`
- known demo-weather historical context:
  - currently `0` cents on the refreshed live settlement set
- unattributed historical weather context:
  - `2000` cents
  - intentionally excluded from canonical forecast-weather and combined weather-family realized totals

Source-monitor per-bot reconciliation now supports direct attribution:

- `executed_settled_orders = 65`
- `executed_settled_orders_with_fill_and_settlement = 65`
- `executed_settled_fill_coverage = 1.0`
- `eligible_local_join_basis = true`

Forecast-weather canonical attribution is now promoted with orphan API-only weather activity split out explicitly:

- `api_fills_without_local_order = 0`
- `filled_orders_without_settlement = 4`
- `eligible_local_join_basis = true`
- unmatched historical `KXHIGH` API-only activity now rolls into:
  - `unattributed-weather`

This means both source-monitor NWS and forecast-weather now use local joined fills for canonical weather-family reporting.

Manager-facing reporting state:

- `weather-observation-pack.json` now includes:
  - `demo_weather_history`
  - `unattributed_weather`
  - `weather_family.realized` excluding that bucket
- `daily-imessage-report.py` now prints bot basis labels and isolates `unattributed-weather history`
- `daily-imessage-report.py` also supports `demo-weather history` when that bucket is non-empty
- `daily-ops-loop.py` now prefers `financial_snapshot.by_bot` over attribution-only bot totals

Live city controls applied:

- forecast-weather config:
  - `config/kalshi-config.json -> cityEdgeThresholds = {CHI: 0.12, HOU: 0.12, NY: 0.12}`
- source-monitor NWS config:
  - `config/kalshi-monitor-config.json -> sources.nws.cityMinEdgeAdders = {CHI: 0.05, LAX: 0.05}`
- runtime code now reads those config hooks directly in:
  - `src/kalshi/apps/weather_bot.py`
  - `src/kalshi/apps/source_monitor.py`

Shadow weather refresh state:

- `data/shadow/weather-refresh/weather-backtest-results.json`
  - `generated_at = 2026-04-03T03:27:51`
- `data/shadow/weather-refresh/weather-calibration.json`
  - `generated_at = 2026-04-03T03:28:44`
- `data/shadow/weather-refresh/weather-observation-pack.json`
  - `generated_at = 2026-04-03T03:28:44.144030+00:00`

Execution coordination evidence after restart:

- supervisor restarted the live weather-family runtimes onto new canonical PIDs:
  - `weather -> 88049`
  - `monitor -> 88066`
- `data/opportunity-log.json` and `data/kalshi-trades-decisions.json` now show repeated `yield_same_day_to_source_monitor` reasons for same-day forecast-weather opportunities
- `data/logs/source-monitor.log` shows live same-day supersede behavior after restart:
  - `Signal supersede: source-monitor (quality 0.286) replaces weather (quality 0.095) on KXHIGHNY-26APR02-T61`
- `data/logs/weather.log` startup confirms the refreshed weather runtime booted with the current config:
  - `Mode: demo | Max: $30/trade | Edge: 10% | City edge overrides: 3`

## Remaining Work

- investigate the still-unexplained `unattributed-weather` residue if you want to fully eliminate orphan historical weather noise from manager reporting
- monitor the live effect of the new city tighten controls:
  - forecast-weather: `CHI`, `HOU`, `NY`
  - source-monitor NWS: `CHI`, `LAX`
- monitor whether same-day yield plus source-aware supersede removes enough weather-family ticker collisions without starving valid forecast-weather opportunities
- monitor whether the narrow source-monitor NWS daily-loss override actually triggers on live high-confidence late-day setups; if it never triggers, lower or remove it instead of letting dead config linger
- monitor whether forecast-weather near-close taker escalation actually improves fill capture; if the structured trade log shows no taker-escalated fills after sufficient sample, remove or retune it
- refresh the shadow/backtest pack again after a meaningful post-cutover sample accumulates and then revisit additional city promotions/expansions

## Bottom Line

The outage-recovery objective is met:

- weather-family live collection survived the outage
- stale automated drawdown state no longer requires manual cleanup by design
- local reconciliation is materially repaired
- source-monitor weather trading resumed live under supervisor-managed runtime control
- source-monitor NWS realized P&L is now attributable inside the weather-family reporting stack
- forecast-weather realized P&L is now also on the local joined canonical basis
- old API-only `KXHIGH` history is no longer silently counted as forecast-weather; it is surfaced as `unattributed-weather`
- known demo-weather history now has its own separate reporting bucket when present; on the current live settlement set it resolves to zero, which means the remaining orphan weather residue is still unexplained rather than known demo traffic
- the main manager-facing report paths now honor the repaired hybrid per-bot basis
- the current live weather tighten set is implemented in config and active after supervised restart
- the shadow weather bundle is refreshed against the repaired live state
- same-day weather-family coordination is now implemented in live code:
  - same-day forecast-weather yields to observed NWS by policy
  - source-aware allocator supersede favors NWS when the tracks still collide
  - forecast-weather can escalate to taker late in the day on a narrow high-confidence path
  - source-monitor NWS now has a narrow late-day high-confidence loss-cap override instead of a broader bot-wide increase

The remaining gap is no longer outage recovery, canonical weather-family attribution, or plan ambiguity. It is iterative post-cutover monitoring and any future promotion decisions after more live sample accumulates.
