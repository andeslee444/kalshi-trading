"""Sports Research Experiment — the ONLY file the agent modifies.

Contains parameter configurations that the backtest harness evaluates.
The agent tries different values; the harness reports metrics.
All values below are the current Oracle production defaults.

NOTE: Eval-scope params (line offsets, stat types, min games) are locked
in backtester.py and cannot be changed by the agent.
"""

# ── Book B: Probability Adjustments ──────────────────────────────

MATCHUP_MULTIPLIER = 0.08
MATCHUP_CAP = 0.03
VENUE_MULTIPLIER = 0.08
VENUE_CAP = 0.03
B2B_PENALTY = 0.0
RECENCY_WEIGHT_LAST5 = 1.10
RECENCY_WEIGHT_LAST10 = 1.0
RECENCY_WEIGHT_SEASON = 0.95
FALLBACK_SIGMA = 0.15
USE_STAT_SPACE = False

# ── Book C: Live Event Parameters ────────────────────────────────

FOUL_TROUBLE_PROB = 0.25
FOUL_TROUBLE_MINUTES_REDUCTION = 0.70
BLOWOUT_PROB = 0.15
BLOWOUT_MINUTES_THRESHOLD = 10.0
BLOWOUT_MINUTES_REDUCTION = 0.50
OT_MINUTES = 5.0
OT_MARGIN_FACTOR_TIED = 1.0
OT_MARGIN_FACTOR_CLOSE = 0.55
OT_CLOCK_DECAY = 200.0
OT_BASE_MULTIPLIER = 0.80
OT_MAX_PROB = 0.85
OT_OVER_PROB_BASE = 0.50
OT_OVER_PROB_MULTIPLIER = 2.0
OT_OVER_PROB_MIN = 0.40
OT_OVER_PROB_MAX = 0.90
OT_NO_OT_PROB_BASE = 0.30
OT_NO_OT_PROB_MULTIPLIER = 2.0
OT_NO_OT_PROB_MIN = 0.10
OT_NO_OT_PROB_MAX = 0.80
OT_SAFETY_MARGIN = 1.05
OT_GAP_THRESHOLD = 0.30

# ── Edge Thresholds ──────────────────────────────────────────────

BOOK_A_MIN_EDGE = 0.15
BOOK_B_MIN_EDGE = 0.06
BOOK_C_MIN_EDGE = 0.12

# ── Position Sizing ──────────────────────────────────────────────

BOOK_A_SIZE_PCT = 0.02
BOOK_B_SIZE_PCT = 0.015
BOOK_C_SIZE_PCT = 0.01
