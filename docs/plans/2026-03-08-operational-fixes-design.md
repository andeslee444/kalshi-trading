# Operational Fixes — Design Doc

**Date**: 2026-03-08
**Status**: Approved
**Context**: Comprehensive bot audit found 6 issues: 2 critical crashes, 1 PID conflict, weather API tier mismatch, log spam, missing decision metadata.

## Problem Summary

Position-monitor and economics-bot crash every scan cycle (never complete work). Beatrelease is stuck in a PID conflict restart loop. Weather bot wastes 470s/scan hammering premium-only endpoints on the free tier. Source-monitor spams warnings for expected behavior. Strategy decisions lack edge values for debugging.

## Bug Analysis

### Bug 1 (Critical): Position-monitor `record_trade()` keyword mismatch

`position-monitor.py:1046` calls `allocator.record_trade("position-monitor", ticker, risk=0, edge=0)`.
`capital_allocator.py:568` signature is `record_trade(self, bot_name, ticker, risk_cents, edge=0.0)`.

The keyword `risk` doesn't match the parameter name `risk_cents`. Python raises `TypeError: got an unexpected keyword argument 'risk'` on every scan. Positions are never monitored — take-profit, stop-loss, and model-shift exits are all disabled.

3 call sites: lines 1046, 1110, 1121.

### Bug 2 (Critical): Economics-bot `load_trades()` method missing

`economics-bot.py:1248` calls `trade_manager.load_trades()` but `TradeManager` has no such method. The standalone function `load_trades(path)` is already imported at line 22. The bot finds edge (e.g., KXECONSTATCPIYOY-26MAY-T2.0 at 97.2% edge) but crashes before placing trades.

### Bug 3 (High): Beatrelease PID conflict loop

Beatrelease manages its own PID file (`beatrelease-scanner.pid`) separately from the supervisor's (`beatrelease.pid`). On supervisor restart, the new beatrelease instance finds the old PID still alive and exits. Supervisor detects it stopped and restarts it, creating a 30s restart loop.

The supervisor already handles process lifecycle fully. The bot's self-managed PID is redundant and harmful.

### Bug 4 (High): Weather bot on free Open-Meteo tier

`kalshi-config.json` has `ensemble.enabled: true` but the free plan excludes ensemble and HRRR endpoints. Every scan hammers ~30 ensemble URLs and ~15 HRRR URLs, all returning 429/400. This wastes 470s per scan (should be <60s) and has accumulated 1,870 consecutive errors in health-state.

Free tier limits: 10,000 calls/day, 600/min. Basic forecast endpoint works fine on free tier.

### Bug 5 (Low): Probability clamped log spam

`probability.py:1493,1557` logs WARNING on every probability clamp to [0.001, 0.999]. Source-monitor evaluating confirmed NWS outcomes naturally produces probabilities of 1.0 — this is expected, not anomalous.

### Bug 6 (Medium): Strategy decisions missing edge values

Strategy bot's `log_decision()` calls only pass skip reason. Missing: edge value, Kelly contracts, yes_price. Makes it impossible to debug why trades were placed or skipped from decision logs alone.

## Fixes

### Fix 1: Position-monitor keyword fix
Change `risk=0` to `risk_cents=0` at 3 call sites in `position-monitor.py`.

### Fix 2: Economics-bot load_trades fix
Change `trade_manager.load_trades()` to `load_trades(TRADES_PATH)` at line 1248.

### Fix 3: Remove beatrelease self-managed PID
Delete `check_existing()`, `write_pid()`, `remove_pid()`, `PID_FILE` constant, and `atexit.register(remove_pid)`. The supervisor handles lifecycle.

### Fix 4: Weather free tier compatibility
- Set `ensemble.enabled: false` in `kalshi-config.json`
- Guard HRRR calls behind `_OPEN_METEO_API_KEY` check in `weather-bot.py`
- Reset `open-meteo` error count in `health-state.json`

### Fix 5: Probability clamp log level
Change `_log.warning()` to `_log.debug()` at lines 1493 and 1557 in `probability.py`.

### Fix 6: Strategy decision metadata
Add `edge=`, `kelly_contracts=`, `yes_price=` to `log_decision()` calls for evaluated candidates in `strategy-trader.py`.

## Files Modified

| File | Changes |
|------|---------|
| `src/kalshi/position-monitor.py` | `risk=` → `risk_cents=` (3 sites) |
| `src/kalshi/economics-bot.py` | `trade_manager.load_trades()` → `load_trades(TRADES_PATH)` |
| `src/kalshi/beatrelease-scanner.py` | Remove self-managed PID logic |
| `config/kalshi-config.json` | `ensemble.enabled: false` |
| `src/kalshi/weather-bot.py` | Guard HRRR behind premium key |
| `src/kalshi/probability.py` | Clamp log WARNING → DEBUG |
| `src/kalshi/strategy-trader.py` | Add edge values to log_decision calls |

## Success Criteria

- Position-monitor completes full scan without crash
- Economics-bot completes scan and can place trades
- Beatrelease starts cleanly after supervisor restart
- Weather bot scans in <60s with zero API errors
- No "Probability clamped" warnings in logs
- Strategy decisions include edge values
