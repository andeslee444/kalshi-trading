# Operational Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 6 operational issues found in bot audit: 2 critical crashes, 1 PID conflict, weather API tier mismatch, log spam, missing decision metadata.

**Architecture:** Targeted fixes to each bot's crash site or config. No shared module changes except one log-level change in probability.py. Each fix is independent.

**Tech Stack:** Python, JSON config

---

### Task 1: Fix position-monitor `record_trade()` keyword mismatch

**Files:**
- Modify: `src/kalshi/position-monitor.py:1046,1110,1121`

**Step 1: Fix the 3 call sites**

At line 1046, replace:
```python
                allocator.record_trade("position-monitor", ticker, risk=0, edge=0)
```
with:
```python
                allocator.record_trade("position-monitor", ticker, risk_cents=0, edge=0)
```

At line 1110, replace:
```python
                                allocator.record_trade("position-monitor", pending_ticker, risk=0, edge=0)
```
with:
```python
                                allocator.record_trade("position-monitor", pending_ticker, risk_cents=0, edge=0)
```

At line 1121, replace:
```python
                                allocator.record_trade("position-monitor", pending_ticker, risk=0, edge=0)
```
with:
```python
                                allocator.record_trade("position-monitor", pending_ticker, risk_cents=0, edge=0)
```

**Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/kalshi/position-monitor.py', doraise=True); print('OK')"`
Expected: OK

**Step 3: Run position-monitor tests**

Run: `python3 -m pytest tests/test_position_monitor.py -q --tb=short 2>&1 | tail -5`
Expected: All pass

**Step 4: Commit**

```bash
git add src/kalshi/position-monitor.py
git commit -m "fix(positions): record_trade() keyword risk → risk_cents

Position monitor crashed every scan because allocator.record_trade()
parameter is 'risk_cents', not 'risk'. All 3 call sites fixed."
```

---

### Task 2: Fix economics-bot `load_trades()` method call

**Files:**
- Modify: `src/kalshi/economics-bot.py:1248`

**Step 1: Fix the call**

At line 1248, replace:
```python
        existing_trades = trade_manager.load_trades()
```
with:
```python
        existing_trades = load_trades(TRADES_PATH)
```

Note: `load_trades` is already imported at line 22 from `kalshi_auth`. `TRADES_PATH` is defined at the top of the file.

**Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/kalshi/economics-bot.py', doraise=True); print('OK')"`
Expected: OK

**Step 3: Run economics tests**

Run: `python3 -m pytest tests/test_economics.py -q --tb=short 2>&1 | tail -5`
Expected: All pass

**Step 4: Commit**

```bash
git add src/kalshi/economics-bot.py
git commit -m "fix(economics): use standalone load_trades() not TradeManager method

TradeManager has no load_trades() method. The standalone function
from kalshi_auth is already imported — just needed the right call."
```

---

### Task 3: Remove beatrelease self-managed PID logic

**Files:**
- Modify: `src/kalshi/beatrelease-scanner.py:12,28-29,56-74,772-774`

**Step 1: Remove `atexit` from imports**

At line 12, replace:
```python
import json, time, datetime, os, sys, re, signal, atexit, hashlib
```
with:
```python
import json, time, datetime, os, sys, re, signal, hashlib
```

**Step 2: Remove PID_FILE constant and mkdir**

Delete lines 28-29:
```python
PID_FILE = PROJECT_DIR / "data" / "pids" / "beatrelease-scanner.pid"
PID_FILE.parent.mkdir(parents=True, exist_ok=True)
```

**Step 3: Remove PID management functions**

Delete lines 56-74 (the entire `# === PID Management ===` section):
```python
# === PID Management ===
def write_pid():
    PID_FILE.write_text(str(os.getpid()))

def remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def check_existing():
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if running
            log.info(f"Another instance running (PID {pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # Stale PID file
```

**Step 4: Remove PID calls from `run_daemon()`**

At lines 772-774, replace:
```python
    check_existing()
    write_pid()
    atexit.register(remove_pid)
    setup_signal_handlers()
```
with:
```python
    setup_signal_handlers()
```

**Step 5: Clean up stale PID file**

Run: `rm -f data/pids/beatrelease-scanner.pid`

**Step 6: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/kalshi/beatrelease-scanner.py', doraise=True); print('OK')"`
Expected: OK

**Step 7: Commit**

```bash
git add src/kalshi/beatrelease-scanner.py
git commit -m "fix(beatrelease): remove self-managed PID, let supervisor handle lifecycle

Beatrelease maintained its own PID file (beatrelease-scanner.pid)
separate from the supervisor's (beatrelease.pid). On supervisor
restart, the new instance found the old PID alive and refused to
start, creating a 30s crash loop."
```

---

### Task 4: Weather bot — disable ensemble and HRRR on free tier

**Files:**
- Modify: `config/kalshi-config.json` (ensemble.enabled → false, hrrr.enabled → false)

**Step 1: Disable ensemble in config**

In `config/kalshi-config.json`, change the ensemble section:
```json
"ensemble": {
    "enabled": false,
```
(only change `true` to `false`, leave models and weights as-is for when premium is purchased)

**Step 2: Disable HRRR in config**

In `config/kalshi-config.json`, change the hrrr section:
```json
"hrrr": {
    "enabled": false,
```

**Step 3: Reset open-meteo error count in health-state.json**

Run:
```python
python3 -c "
import json
from pathlib import Path
p = Path('data/health-state.json')
d = json.loads(p.read_text())
for key in ['open-meteo', 'open-meteo-gfs', 'open-meteo-ecmwf', 'open-meteo-icon']:
    if key in d.get('sources', {}):
        d['sources'][key]['error_count'] = 0
        d['sources'][key]['last_error'] = None
p.write_text(json.dumps(d, indent=2))
print('Reset open-meteo error counts')
"
```

**Step 4: Commit**

```bash
git add config/kalshi-config.json
git commit -m "fix(weather): disable ensemble and HRRR on free Open-Meteo tier

Free plan excludes ensemble and HRRR endpoints. Every scan was
wasting 470s hammering 429/400 errors. Basic forecast endpoint
works fine on free tier. Re-enable when premium key is purchased."
```

---

### Task 5: Probability clamped log level — WARNING → DEBUG

**Files:**
- Modify: `src/kalshi/probability.py:1493,1557`

**Step 1: Change log levels**

At line 1493, replace:
```python
        _log.warning("Probability clamped: %.6f → [0.001, 0.999]", original_prob)
```
with:
```python
        _log.debug("Probability clamped: %.6f → [0.001, 0.999]", original_prob)
```

At line 1557, replace:
```python
        _log.warning("Probability clamped (sell): %.6f → [0.001, 0.999]", raw_p_true)
```
with:
```python
        _log.debug("Probability clamped (sell): %.6f → [0.001, 0.999]", raw_p_true)
```

**Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/kalshi/probability.py', doraise=True); print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add src/kalshi/probability.py
git commit -m "fix(probability): clamp log WARNING → DEBUG

Probabilities of 1.0 are expected for confirmed NWS outcomes.
Logging WARNING for expected boundary conditions spams the
source-monitor log (~1000 entries per restart)."
```

---

### Task 6: Strategy decisions — add edge to `profit_risk_ratio` skip

**Files:**
- Modify: `src/kalshi/strategy-trader.py:178-179`

**Step 1: Add edge and kelly info to the profit_risk_ratio decision**

At lines 178-179, replace:
```python
            trade_manager.log_decision(ticker, "no", "skipped", "profit_risk_ratio",
                                       no_price=no_price, sell_price=sell_price)
```
with:
```python
            trade_manager.log_decision(ticker, "no", "skipped", "profit_risk_ratio",
                                       no_price=no_price, sell_price=sell_price,
                                       edge=round(est_edge, 4))
```

**Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('src/kalshi/strategy-trader.py', doraise=True); print('OK')"`
Expected: OK

**Step 3: Commit**

```bash
git add src/kalshi/strategy-trader.py
git commit -m "fix(strategy): add edge value to profit_risk_ratio decisions

The 39 profit_risk_ratio skips had no_price and sell_price but not
edge — making it impossible to debug from decision logs alone."
```

---

### Task 7: Verify all bots, restart supervisor

**Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=line 2>&1 | tail -5`
Expected: Same pass count as before (2173), no new failures

**Step 2: Push**

Run: `git push origin plan2-consistency-testing-maintainability`

**Step 3: Restart supervisor**

```bash
pkill -f "supervisor.py run"
sleep 3
nohup python3 -u scripts/supervisor.py run > data/logs/supervisor.log 2>&1 &
sleep 15
python3 scripts/supervisor.py status
```

All 9 bots should show "running".

**Step 4: Verify individual bots**

Wait 2 minutes, then check each previously-broken bot:

```bash
# Position monitor should complete a scan (no TypeError)
tail -5 data/logs/positions.log

# Economics should complete a scan (no AttributeError)
tail -5 data/logs/economics.log

# Beatrelease should not show "Another instance running"
tail -5 data/logs/beatrelease.log

# Weather should complete in <60s with no ensemble/HRRR errors
grep "SCAN SUMMARY" data/logs/weather.log | tail -1

# Monitor should have no "Probability clamped" warnings
grep -c "Probability clamped" data/logs/monitor.log
# (Should be 0 for new entries after restart)
```
