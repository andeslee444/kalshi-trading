# Runbooks

These runbooks cover the highest-value operational failure modes first.

## Index

- [fresh-mac-rebuild-and-deploy.md](./fresh-mac-rebuild-and-deploy.md)
- [supervisor-and-stale-bots.md](./supervisor-and-stale-bots.md)
- [source-freshness-and-parser-failures.md](./source-freshness-and-parser-failures.md)
- [ledger-parity-regression.md](./ledger-parity-regression.md)
- [promotion-rollback.md](./promotion-rollback.md)

## Operator Defaults

- Prefer supervisor actions over ad hoc process management.
- Prefer canonical reports over manual JSON inspection when a report exists.
- Record rollbacks and promotions through `scripts/promotion-workflow.py`.
- Record incidents and follow-up links through `scripts/incident-workflow.py`.
- Link incidents back to a code PR or config change before closing them.
