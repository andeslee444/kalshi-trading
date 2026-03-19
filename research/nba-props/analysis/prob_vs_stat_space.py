"""Compare stat-space vs probability-space adjustment approaches (placeholder).

Key research question: Is it better to adjust the stat line (e.g., +2 points for
home advantage) or adjust the probability (e.g., +3% for home advantage)?

Usage:
    python -m analysis.prob_vs_stat_space
"""

from __future__ import annotations


def main():
    print("Stat-space vs probability-space comparison")
    print("TODO: Implement once empirical CDFs are available")
    print()
    print("Approaches to compare:")
    print("  1. Stat-space: Adjust player line, then compute CDF probability")
    print("  2. Prob-space: Compute base CDF probability, then apply multipliers")
    print("  3. Hybrid: Small adjustments in stat-space, regime shifts in prob-space")


if __name__ == "__main__":
    main()
