# Oracle Activation Gates

Date: 2026-04-02
Status: Current operating plan

## Default posture

- Oracle stays fail-closed in `config/bots-config.json`.
- No live capital.
- No Book B.
- No broad Book C passive execution.
- No Book C prop strategy.

## Allowed bounded studies

### Study A: Clutch-only Book C shadow collection

Purpose: measure whether the recalibrated clutch comeback path produces a real post-fee edge in demo/shadow mode.

Required runtime shape:

- `scripts/run-oracle-shadow-bot.sh`
- demo mode only
- Book C only
- `propSignalsEnabled = false`
- `clutchComebackEnabled = true`
- `passiveExecution = false`
- game markets only

Primary artifact:

- `python3 src/kalshi/oracle-scorecard.py`

### Study B: H2 pregame crowd divergence collection

Purpose: capture paired Real crowd probabilities and Kalshi game quotes before tip-off and test whether divergence survives spread and fees.

Required runtime shape:

- `scripts/run-oracle-h2-pregame-collector.sh`
- scheduled pregame collection window
- crowd snapshots and quote snapshots must share a collector cycle id

Primary artifact:

- `python3 scripts/oracle-h2-crowd-divergence.py --source ledger`

### Study C: H8 maker demo validation

Purpose: measure actual demo passive fills, cancel outcomes, and queue behavior for clutch-only Book C orders without enabling live capital.

Required runtime shape:

- `scripts/run-oracle-h8-maker-demo.sh`
- demo mode only
- Book C only
- `propSignalsEnabled = false`
- `clutchComebackEnabled = true`
- `passiveExecution = true`
- game markets only

Primary artifact:

- `python3 src/kalshi/oracle-scorecard.py`

## Promotion gates

No Oracle strategy can move past research unless all relevant gates are met.

### Gate set for clutch Book C

1. `100+` clutch-only demo/shadow triggered signals
2. post-fee net EV confidence interval above `0`
3. positive CLV in at least `60%` of settled study days
4. `0` stale open orders older than the scorecard threshold
5. no config drift from fail-closed defaults outside the bounded study service

Operational note:
- Run `python3 src/kalshi/oracle-alpha-ledger-cleanup.py` before trusting stale-order gates if the scorecard shows legacy reconcile contamination.
- Use `scripts/switch-oracle-study.sh shadow` or `scripts/switch-oracle-study.sh maker` rather than trying to run both Oracle bot studies at once; the bot intentionally uses a singleton lock.

### Additional gate set for any maker/passive thesis

1. `50+` demo passive orders
2. measured cancel success and time-to-fill from real demo orders
3. no unresolved passive timeout leakage
4. realized post-fee EV confidence interval above `0`

### Gate set for H2 / Book A

1. paired pregame collector cycles across multiple slates
2. meaningful divergence after spread and fee hurdles
3. stable signal quality by time-to-tip bucket

## Kill deadline

If Oracle does not clear a credible path to promotion by **2026-04-30**, it should be deprioritized or killed and roadmap time should move to a cleaner alpha source.

## Daily review commands

```bash
python3 src/kalshi/oracle-alpha-ledger-cleanup.py
python3 src/kalshi/oracle-scorecard.py
python3 scripts/oracle-h2-crowd-divergence.py --source ledger
python3 src/kalshi/oracle-latency-report.py --daily-summary-only
```
