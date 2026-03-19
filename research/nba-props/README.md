# Sportsmarket — Sports Research Base

Research lab for discovering, validating, and exporting signal models for the Oracle NBA trading system in [kalshi-trading](../kalshi-trading/).

## Architecture

```
collect/  → Pull raw data from Real Sports API, Kalshi, NBA stats
data/     → Raw API responses + processed game logs (gitignored)
analysis/ → Research scripts (empirical CDFs, calibration, backtests)
export/   → Export calibrated parameters to kalshi-trading
shared/   → Real Sports client, models
docs/     → API reference, specs
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env  # Fill in Real Sports credentials
```

## Data Collection

```bash
python -m collect.real_sports --players 50 --games 20  # Pull player game logs
```

## Key Research Questions

1. Stat-space vs probability-space adjustment approaches
2. Optimal adjustment magnitudes (matchup, venue, B2B, pace)
3. Edge threshold optimization per book
4. Book C signal probability calibration
5. Recency weighting optimization
