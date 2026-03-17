# Ledger Parity Regression

## When To Use

Use this runbook when ledger-backed readers disagree with the legacy JSON artifacts.

## Signals

- `python3 scripts/ledger-parity-report.py` reports `overall_ok: False`
- ledger-backed and legacy reports disagree on counts or hashes
- dashboard behavior changes when ledger mode is enabled

## Immediate Actions

1. Run the parity report:

```bash
python3 scripts/ledger-parity-report.py --json
```

2. Compare legacy and ledger read paths on the same report:

```bash
python3 scripts/analyze-performance.py --reconcile
python3 scripts/analyze-performance.py --reconcile --use-ledger
python3 scripts/daily-attribution.py --save
python3 scripts/daily-attribution.py --save --use-ledger
```

3. If parity is broken, keep consumers on legacy artifacts until the mismatch is understood.

## Escalation

- Treat hash mismatches as data-quality incidents, not display bugs.
- Capture the exact legacy path, ledger path, and report scope before any repair.

## Exit Criteria

- parity report returns `overall_ok: True`
- the affected reader produces the same business totals in legacy and ledger mode
- the root cause is documented in a PR or incident follow-up
