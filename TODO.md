# Kalshi Trading — TODO

## Upcoming

- [ ] **Run Oracle latency probe live** — Target date: **2026-03-19**
  - Load repo `.env` into the shell before running
  - Run with escalated network access; sandboxed sessions can fail against Real/Kalshi
  - Canonical smoke test: `set -a; source .env; set +a; python3 src/kalshi/oracle-latency-probe.py --once`
  - Expected artifact: `data/oracle-alpha-ledger.sqlite3`
  - Runbook: `research/oracle_latency_probe_runbook_2026-03-19.md`
  - Source hierarchy: `research/oracle_source_of_truth_spec_2026-03-19.md`
- [ ] **Run `calibrate-sigma.py --save`** — Target date: **2026-03-30** (Sunday)
  - By then: ~210-230 settled weather trades (up from 170 currently)
  - Better per-city sample sizes for LAX/MIA/HOU (currently weak)
  - Data collection verified active (2026-03-19): bot heartbeat OK, ~10 trades/day, verification growing ~50-140/day
  - `model_prob` field IS recorded in trade logs (field name `model_prob`, not `model_probability`)
  - Run: `python3 scripts/calibrate-sigma.py --save`
  - Then run backtest: `npm run backtest` to verify Brier improved
  - Compare to baseline: `data/backtest-baseline-2026-03-07.json`
  - Check if df shifts toward 5 (quick analysis showed excess kurtosis=5.29, implying df~5 vs current df=6)
