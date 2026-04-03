# Weather-Family Implementation Audit Note

Generated: 2026-03-29
Status: Implemented and validated. Live weather-family calibration is refreshed, the live forecast-weather prior is refreshed through the canonical training pipeline, operator artifacts are updated, source-monitor NWS execution changes are audit-tagged, and the shadow-prior path is hardened against silent empty outputs.

## Goal

Provide an audit-ready record of the March 29, 2026 weather-family calibration changes:

- fix source-monitor NWS weather calibration ingestion
- ensure running processes pick up refreshed calibration without a restart
- preserve existing weather calibration metadata on canonical save
- unify forecast-weather and source-monitor NWS into shared observation and promotion artifacts
- tighten source-monitor NWS execution around the strongest threshold NO setups
- refresh the live forecast-weather prior through the canonical training path
- align the active weather-family plan/docs with the actual two-track structure

This note is the manager-facing implementation record for the change set. It is not the long-run weather strategy doc.

## Scope

This audit covers the shared weather-family calibration path:

- forecast-weather:
  - `src/kalshi/apps/weather_bot.py`
  - `config/calibration.json` `weather` block
- observed-weather / source-monitor NWS:
  - `src/kalshi/apps/source_monitor.py`
  - `config/calibration.json` `nws` block

It does not cover:

- changing live city thresholds or sizing rules
- restarting bots

## Problem Statement

Before this change:

- source-monitor was the strongest snapshot-measured weather alpha, but its settled weather trades were not feeding `nws` calibration correctly
- `scripts/calibrate-sigma.py` left `config/calibration.json` with `nws.n = 0`
- long-running processes cached `config/calibration.json` and would not naturally pick up a refreshed file
- canonical calibration saves overwrote unrelated calibration metadata such as `weather.bias_correction` and `sigma_updated_at`
- the active docs made it too easy to treat forecast weather and source-monitor NWS as unrelated promotion tracks
- the top-level observation and promotion artifacts only reflected forecast-weather, even though source-monitor NWS was the strongest snapshot-measured weather alpha
- source-monitor still short-circuited on the shared liquidity gate before it could act on the strongest threshold NO NWS setups
- the NWS calibration buckets were still too easy to whipsaw on small-sample refreshes
- weather-family local trade-log settlement annotations were being mistaken for realized P&L even though resting/unfilled orders can still carry `settlement_result`

## Implemented Changes

### 1. Source-monitor NWS calibration ingestion fix

Changed:

- `scripts/calibrate-sigma.py`

What changed:

- `calibrate_nws()` now matches source-monitor weather trades through real NWS fields instead of the older brittle fallback
- accepted signals now include:
  - `source_type == "nws"`
  - `strategy` containing `nws`
  - `model_name` beginning with `weather_nws_observation_`
  - source-monitor records whose reasoning explicitly contains `nws`

Risk guard added:

- a negative regression test now proves a non-NWS `source-monitor` `KXHIGH` row does not contaminate the `nws` calibration bucket

### 2. Runtime calibration reload

Changed:

- `src/kalshi/probability.py`

What changed:

- calibration loading now fingerprints file contents rather than only caching once per process
- this allows running weather components to pick up a rewritten `config/calibration.json` without a manual restart

### 3. Canonical save preservation

Changed:

- `scripts/calibrate-sigma.py`

What changed:

- canonical saves now preserve non-sigma metadata from the existing or backup calibration file
- specifically preserved fields include:
  - `weather.bias_correction`
  - `sigma_updated_at`
- the save path also recovers those fields from `config/calibration-backup.json` if the current live file is already missing them

### 4. Weather-family observation and promotion artifacts

Changed:

- `scripts/weather-observation-pack.py`
- `scripts/weather-promotion-candidates.py`

What changed:

- the observation pack now records:
  - forecast-weather realized snapshot
  - source-monitor bot-level realized snapshot context
  - attributable source-monitor NWS realized snapshot only when attribution and reconciliation are both clean
  - combined weather-family realized snapshot from attributable tracks only
  - source-monitor local-log reconciliation against `financial-snapshot.json`
- the promotion artifact now records weather-family track ordering and exposes source-monitor reconciliation flags so operators do not treat forecast-weather as the only weather lane
- refreshed top-level derived artifacts were saved:
  - `data/weather-observation-pack.json`
  - `data/weather-promotion-candidates.json`

### 5. Source-monitor NWS execution improvement

Changed:

- `src/kalshi/apps/source_monitor.py`

What changed:

- conservative quarter-Kelly sizing remains in place after review
- source-monitor now has a narrow threshold-NO liquidity override for NWS weather:
  - only threshold contracts
  - only NO-side entries
  - only when the displayed NO price still clears the existing `_nws_min_edge()` rule
  - empty-book markets still skip
  - brackets and YES-side entries still use the normal liquidity gate
- the NWS execution path now writes explicit `execution_style` markers so override trades can be audited later:
  - `standard`
  - `nws_threshold_no_override`

### 6. Shadow prior diagnosis

Evidence gathered:

- older March 22 shadow-prior artifacts were empty because the backfill stage matched zero rows
- current March 29 reruns succeeded:
  - all-city, 14-day, 5-model backfill produced 2,780 training pairs
  - full `weather-shadow-refresh.py --refresh-shadow-prior` reruns produced a populated 20-city shadow bias artifact
- preserved investigation outputs now live under:
  - `data/shadow/investigate-multicity-refresh/weather-training.db`
  - `data/shadow/investigate-weather-shadow-refresh/weather-live-bias.json`

Meaning:

- there is no reproducible deterministic multi-city code bug left in the current shadow-prior path
- the remaining need is hardening and diagnostics, not a logic rewrite
- the scripts are now safer:
  - `backfill-weather-data.py` raises with city/model/url context when every previous-runs fetch fails for a city
  - `calibrate-weather-bias.py` now refuses to write a zero-row bias artifact unless explicitly allowed

### 7. Live forecast-weather prior promotion

Changed:

- `data/weather-training.db`
- `config/weather-live-bias.json`

What changed:

- the live training DB was refreshed through the canonical backfill path:
  - `python3 scripts/backfill-weather-data.py --days 14 --models gfs,ecmwf,icon,gem,graphcast --db-path data/weather-training.db`
- the live bias artifact was rebuilt from that DB:
  - `python3 scripts/calibrate-weather-bias.py --db-path data/weather-training.db --output config/weather-live-bias.json --json`
- the refreshed live DB now spans:
  - `period.start = 2026-02-27`
  - `period.end = 2026-03-28`
  - `period.days = 28`
  - `n_forecasts = 5580`
- pre-promotion backups were preserved:
  - `config/weather-live-bias.pre-20260329T2145.json`
  - `data/weather-training.pre-20260329T2145.db`

Meaning:

- the old live prior that stopped at March 12 is no longer the active runtime file
- the forecast-weather bot now reads a materially fresher lead-time-matched prior built from the same canonical scripts used in shadow validation

### 8. Plan/doc cleanup

Changed:

- `docs/plans/2026-03-06-master-plan.md`
- `docs/plans/2026-03-06-plan2-weather-bot.md`
- `docs/plans/2026-03-06-plan6-source-monitor.md`
- `docs/plans/2026-03-22-weather-observation-window-worklist.md`
- `docs/plans/2026-03-22-weather-april1-promotion-list.md`

What changed:

- the active docs now describe one weather family with two tracks:
  - forecast weather
  - source-monitor NWS weather
- the observation-window doc remains the execution doc during collection, but now records the explicit March 29 live calibration override
- the promotion list is now the post-gate review doc for the remaining prior, threshold, and storage changes

### 9. NWS calibration stability gate

Changed:

- `scripts/calibrate-sigma.py`

What changed:

- NWS calibration now aligns to the live runtime window by excluding pre-8am local-hour rows
- weak NWS hour buckets now carry forward the prior sigma instead of re-fitting on tiny samples
- the current gates are:
  - `17+`: minimum 10 trades
  - `15-16`: minimum 20 trades
  - `before_15`: minimum 40 trades
- the grid search now extends to `10.0` so `before_15` is not artificially capped at `7.9`
- the saved calibration now records:
  - `n_by_hour`
  - `basis_by_hour`
  - `runtime_min_hour`

Meaning:

- the current live `17+` bucket still fits from data
- the weaker `15-16` and `before_15` buckets stay conservative through carry-forward rather than loosening the live source-monitor posture

### 10. Narrowed weather-family reporting basis

Changed:

- `scripts/pnl-snapshot.py`
- `scripts/weather-observation-pack.py`
- `scripts/weather-promotion-candidates.py`

What changed:

- `financial-snapshot.json` now explicitly records the basis used for per-bot reporting:
  - `realized_pnl.by_bot_basis`
  - `realized_pnl.by_bot_api_settlements`
  - `realized_pnl.by_bot_local_joined_fills`
  - `realized_pnl.by_bot_reconciliation`
- the code now attempts a stricter local filled-order join, but it falls back to the API settlement rollup when there are no reliable local fill matches
- local weather-family city P&L in the observation pack now excludes resting rows and is explicitly labeled as an executed-trade proxy:
  - `city_pnl.source = local_executed_trade_log_settlements`
- source-monitor NWS reporting now keeps the source-monitor bot snapshot visible as context, but withholds NWS/weather-family realized attribution when the executed local trade proxy diverges

Meaning:

- manager-facing per-bot weather reporting no longer silently pretends the raw local trade logs are realized P&L
- city-level weather promotion artifacts are now more conservative because they no longer count resting orders as settled wins or losses
- source-monitor bot-level P&L is no longer folded into attributable source-monitor NWS or combined weather-family realized totals while attribution remains under review
- the deeper local-order-to-fill reconciliation is still incomplete and remains future work

## Validation

Commands run:

```bash
pytest tests/test_calibration.py tests/test_source_monitor.py tests/test_probability.py tests/test_calibrate_sigma_shadow_output.py -q
pytest tests/test_pnl_snapshot.py tests/test_weather_observation_pack.py tests/test_weather_promotion_candidates.py -q
pytest tests/test_calibration.py tests/test_source_monitor.py tests/test_probability.py tests/test_weather_observation_pack.py tests/test_weather_promotion_candidates.py -q
pytest tests/test_weather_backfill_scripts.py tests/test_weather_bias_calibration_script.py tests/test_weather_shadow_refresh.py -q
python3 scripts/pnl-snapshot.py
python3 scripts/calibrate-sigma.py --no-api --json
python3 scripts/calibrate-sigma.py --no-api --save --allow-canonical-save
python3 scripts/weather-observation-pack.py --save
python3 scripts/weather-promotion-candidates.py --save
python3 scripts/weather-shadow-refresh.py --refresh-shadow-prior --output-dir data/shadow/weather-refresh --json
python3 scripts/backfill-weather-data.py --days 14 --models gfs,ecmwf,icon,gem,graphcast --db-path data/shadow/tmp-all/weather-training.db
python3 scripts/backfill-weather-data.py --days 14 --models gfs,ecmwf,icon,gem,graphcast --db-path data/weather-training.db
python3 scripts/calibrate-weather-bias.py --db-path data/weather-training.db --output config/weather-live-bias.json --json
```

Result:

- focused observation / promotion suite: `20 passed`
- broader calibration / snapshot / source-monitor / probability / shadow-refresh suite: `270 passed`

Additional review:

- subagent code review found two real issues during implementation:
  - overly broad NWS trade matching
  - metadata loss on canonical save
- both findings were resolved before the final canonical calibration save
- a follow-on explorer review identified the threshold-NO execution path as the next best source-monitor NWS improvement; that narrow override is now implemented and audit-tagged
- a later code review found the proposed late-day half-Kelly NWS size-up was not supported by settled trade history; that size-up was reverted before final close-out
- a later docs/artifact review found unsafe source-monitor attribution and forecast-weather-only promotion assumptions; both were corrected before the refreshed observation/promotion artifacts were saved

## Outcome Snapshot

Final live artifact:

- `config/calibration.json`
- `config/weather-live-bias.json`

Final key values after save:

- `generated_at = 2026-03-29T22:02:45`
- `sigma_updated_at = 2026-03-29T22:02:45`
- `weather.n = 268`
- `nws.n = 80`
- `nws.sigma_by_hour = {"17+": 0.6, "15-16": 2.1, "before_15": 7.9}`
- `nws.n_by_hour = {"17+": 40, "15-16": 12, "before_15": 28}`
- `nws.basis_by_hour = {"17+": "fit", "15-16": "carried_forward", "before_15": "carried_forward"}`
- `weather.bias_correction` preserved
- `weather-live-bias.generated_at = 2026-03-29T21:18:25.986309+00:00`
- `weather-live-bias.period = 2026-02-27 -> 2026-03-28 (28 days)`
- `weather-live-bias.n_forecasts = 5580`
- `weather-live-bias.n_cities = 20`

Current derived weather-family artifacts:

- `data/weather-observation-pack.json`
  - `generated_at = 2026-04-02T18:00:43.197805+00:00`
  - `forecast-weather realized pnl = +$592.00`
  - `source-monitor NWS realized pnl = withheld pending reconciliation`
  - `source-monitor bot-level realized context = +$1,675.00`
  - `weather-family realized pnl = +$592.00`
  - `city_pnl.source = local_executed_trade_log_settlements`
- `data/weather-promotion-candidates.json`
  - `generated_at = 2026-04-02T18:00:43.271474+00:00`
  - `forecast-weather counts_by_action = {"hold": 4, "tighten": 3, "shadow_only": 13}`
  - `forecast-weather tighten = [CHI, HOU, NY]`
  - `source-monitor NWS counts_by_action = {"hold": 2, "tighten": 2, "shadow_only": 6}`
  - `review_order = [forecast_weather]`
  - `context_review_order = [source_monitor_bot_context, forecast_weather]`
  - `source_monitor_nws_realized_pnl_cents = 0`
  - `source_monitor_bot_realized_pnl_cents = 167500`
  - includes separate `forecast_weather` and `source_monitor_nws` candidate sections
- `data/financial-snapshot.json`
  - `realized_pnl.by_bot_basis = kalshi_api_settlements_fallback_incomplete_local_fill_coverage`
  - the attempted local filled-order join matched `0` orders on the current production data
  - the snapshot now records that fallback explicitly instead of silently implying a local realized basis

Rollback artifact:

- `config/calibration-backup.json`

Rollback state:

- backup file is now also a corrected post-fix artifact
- it preserves `weather.bias_correction`
- it preserves populated `nws` calibration

## Follow-On Evidence

### 1. NWS settlement confirmation and calibration trust

- a direct March 29 Kalshi settlement fetch returned `514` settlement records
- all `82` settled source-monitor NWS weather rows matched that API settlement set
- the earlier `36` figure was not an API-confirmation count:
  - it is the current `financial-snapshot.json` settled count attributed to `source-monitor`
  - it should not be interpreted as “only 36 NWS trades are API-confirmed”
- an API-only replay that strips local `settlement_result` fields comes out very close, but not identical:
  - local per-trade settlements: `{"17+": 0.6, "15-16": 2.1, "before_15": 7.9}`
  - API-only replay: `{"17+": 0.8, "15-16": 2.1, "before_15": 7.9}`
- the reason is that the settlement API is ticker-level, while the local trade log is per-trade:
  - there are `8` duplicate-ticker NWS rows in the current local history
  - at least one ticker (`KXHIGHDEN-26MAR20-T81`) has mixed YES/NO local rows
  - that makes local per-trade `settlement_result` the more faithful calibration source for the current implementation

Trust caveat:

- the identification quality is uneven by hour bucket
- `17+` looks moderately stable
- `15-16` is weakly identified
- `before_15` is low-trust and hits the top of the current grid, so the exact `7.9` value should be treated as a conservative fit, not a precisely pinned parameter

### 2. Old pre-fix NWS runtime behavior

Before the March 29 calibration-ingestion fix, source-monitor NWS used the hardcoded fallback from `nws_sigma_for_hour()` rather than a populated `nws` calibration block.

Practical fallback values were:

- hour `8`: `2.79`
- hour `10`: `1.95`
- hour `12`: `1.36`
- hour `14`: `0.95`
- hour `15`: `0.79`
- hour `16`: `0.66`
- hour `17`: `0.55`
- hour `18+`: `0.50`

Compared with the current live calibrated values:

- `before_15`: `7.9`
- `15-16`: `2.1`
- `17+`: `0.6`

Meaning:

- the March 29 fix made midday observed-weather trading materially more conservative
- late-day observed-weather behavior changed only slightly

### 3. Threshold-NO override volume estimate

Recent decision-log analysis points to a meaningful but bounded lift from the narrow NWS threshold-NO liquidity override.

March 29 decision-log evidence:

- `1,550` illiquid NWS threshold skips
- `1,086` inferred threshold-NO setups
- `895` raw skip rows matching the override proxy:
  - threshold market
  - inferred NO side
  - positive `yes_bid`
  - fresh observation (`<= 90` minutes)

After deduping repeated rescans:

- about `34` unique same-day tickers across `19` cities
- about `31` unique tickers after removing cases that later hit a different blocker such as:
  - `kelly_zero`
  - allocator dedup
  - `edge_below_min`

Expected lift if recent conditions persist:

- roughly `31-34` additional trades per day
- roughly `217-238` additional source-monitor NWS trades per week

This estimate is a proxy from recent decision logs and should be used as planning evidence, not as a guaranteed realized fill count.

## Operational Notes

- live weather collection files were not modified:
  - `data/weather-verification.json`
  - `data/weather-nws-cross-check.json`
- no bot restart was required for calibration uptake
- `financial-snapshot.json` now makes the weather-family per-bot reporting limitation explicit:
  - the stricter local filled-order join currently matches `0` local orders
  - manager-facing per-bot reporting therefore stays on the API settlement rollup for now
- the `financial-snapshot.json` settled count of `36` for `source-monitor` is not the same thing as Kalshi API settlement confirmation
- a direct March 29 settlement fetch confirmed all `82` settled source-monitor NWS weather rows are present in the API settlement set
- source-monitor NWS top-level snapshot and local trade-log settlements still disagree materially as reporting aggregates:
  - source-monitor bot-level snapshot context: `45W / 0L / +$1,675.00`
  - executed local source-monitor NWS proxy: `57 settled markets / 65 settled trade rows / +$267.36`
- `data/weather-observation-pack.json` now carries this as an explicit reporting-recommendation warning, and it withholds attributable source-monitor NWS / combined weather-family realized P&L until the reconciliation is resolved
- the full shadow-prior path now succeeds on the current tree and no longer silently writes empty artifacts when upstream data is missing

## Remaining High-Value Work

Still worth doing after this change:

1. Re-run the shadow weather-family promotion pack after the next collection interval and compare forecast-weather vs source-monitor NWS together with the refreshed live prior in place.
2. Review whether the new source-monitor NWS threshold-NO override is improving realized execution without widening drawdown.
3. Build a deeper local-trade-to-fill reconciliation path if manager reporting must ever move off the current API settlement fallback for by-bot weather P&L.
4. Keep the low-trust `before_15` / `15-16` NWS sigma buckets on the current carry-forward gate until materially larger settled samples accumulate.
5. Keep city threshold/sizing promotions gated on refreshed shadow evidence, not on the calibration refresh alone.

## Audit Summary

This change set is complete for its intended March 29 scope.

The two main defects were real and profit-relevant:

- source-monitor weather trades were not entering `nws` calibration
- the shared weather-family operator artifacts did not reflect the strongest snapshot-measured weather track

Those defects are now fixed, covered by regression tests, reflected in the canonical calibration artifact and top-level weather-family derived artifacts, and documented in the active weather-family plan stack.
