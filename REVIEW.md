# Code Review Guidelines

## File Ownership (Critical)

Each bot owns specific files per CLAUDE.md. Flag any PR that modifies files outside the bot's ownership scope. Shared modules (`probability.py`, `kalshi_auth.py`, `capital_allocator.py`, `ticker_utils.py`, `trade_files.py`) require dedicated review.

## Always Check

### Safety & Risk Controls
- Trade amounts, daily limits, and edge thresholds are not loosened without justification
- Kill switch integration (`check_kill_switch()`) is preserved in trade paths
- Circuit breaker logic is not bypassed
- No hardcoded API keys, credentials, or secrets (keys belong in `config/keys/`, gitignored)
- `KALSHI_CONFIRM_PRODUCTION=yes` guard is not removed

### Data Integrity
- Trade logs include `source_bot` field for P&L attribution
- Atomic writes (`atomic_write_json`) used for any JSON file that bots read/write concurrently
- No direct computation of P&L from trade logs — use `data/financial-snapshot.json`

### Probability & Sizing
- Sigma values are physically reasonable (0.5F-10F range for weather)
- Kelly fractions don't exceed half-Kelly without justification
- Edge thresholds are not below 4% without justification
- No division by zero in probability calculations

### Weather Bot Specific
- Bias correction applied before probability computation, not after
- HRRR/NAM (mesoscale models) are NOT bias-corrected
- Ensemble weights sum to ~1.0
- `calibration.json` fields are preserved when writing (use `setdefault`)

### Testing
- New functions have tests
- Tests don't make real API calls
- Tests that import bot modules use the `load_bot_module()` pattern with stubbed `kalshi_auth`
- `_reset_calibration()` called in setup/teardown for probability tests

## Skip

- Changes only in `data/` (gitignored runtime state)
- Changes only in `archive/` (historical reference)
- Formatting-only changes in config JSON files
- `research/` documentation updates
