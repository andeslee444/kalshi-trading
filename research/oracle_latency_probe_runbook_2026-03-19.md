# Oracle Latency Probe Runbook

## Why a Live Run Can Fail

- The Real and Kalshi credentials live in repo `.env`, but they are not present in a fresh shell unless we explicitly source that file.
- Codex shell sessions start sandboxed. Outbound requests to Real and Kalshi can fail until the run is escalated for network access.
- Before `2026-03-19`, `--once` only built the Real-to-Kalshi game mapping and exited, so a successful run could still leave no durable ledger file. That has now been fixed.
- As of the latest probe behavior, the collector separates:
  - discovery health: Real schedule + Real home feed + Kalshi open game markets
  - crowd-price health: Real `game_markets`
- The collector fails closed only on discovery-path failures. If Real `game_markets` returns zero NBA markets, that is recorded as a `source_failure` for Book A crowd pricing, but the latency probe still runs if discovery data is healthy.
- Canonical source choices now live in `research/oracle_source_of_truth_spec_2026-03-19.md`. Do not change the probe to use alternates unless that spec changes.

## Canonical Live Command

```bash
set -a
source .env
set +a
python3 src/kalshi/oracle-latency-probe.py --once
```

## Expected Artifact

- SQLite ledger: `data/oracle-alpha-ledger.sqlite3`
- On a healthy run, it should contain:
  - `probe_snapshot` source rows for the market-index baseline
  - `quote_snapshot` rows for each mapped Kalshi game market captured during the run
- On a degraded-but-running run, it may also contain:
  - `source_failure` rows for crowd-price unavailability
- On a fail-closed run, it should contain:
  - `source_failure` rows showing which discovery-path primary source returned unusable data

## Longer Validation Run

```bash
set -a
source .env
set +a
python3 src/kalshi/oracle-latency-probe.py --duration-seconds 300 --refresh-seconds 120
```

## Next Check

- Verify source and quote rows exist in `data/oracle-alpha-ledger.sqlite3`
- Then inspect `source_to_quote_ms`, spread, depth, and which Real event types lead to the slowest Kalshi repricing
