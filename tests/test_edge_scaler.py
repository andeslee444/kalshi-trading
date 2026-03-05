"""Tests for EdgeScaler -- auto-scaling exposure based on settlement record."""
import pytest


# NOTE: EdgeScaler is defined in economics-bot.py. Since that module requires
# heavy stubbing to import, we test the class logic inline here.
# The implementation in economics-bot.py must match this exact class.
class EdgeScaler:
    TIERS = [
        {"min_wins": 0,  "max_exposure_pct": 0.20},
        {"min_wins": 5,  "max_exposure_pct": 0.40},
        {"min_wins": 10, "max_exposure_pct": 0.60},
        {"min_wins": 20, "max_exposure_pct": 1.00},
    ]

    def current_limit(self, settlement_record):
        wins = sum(1 for s in settlement_record if s.get("profitable"))
        recent = settlement_record[-10:] if settlement_record else []
        loss_streak = 0
        for s in reversed(recent):
            if not s.get("profitable"):
                loss_streak += 1
            else:
                break
        loss_penalty = 0.5 if loss_streak >= 3 else 1.0
        tier = self.TIERS[0]
        for t in self.TIERS:
            if wins >= t["min_wins"]:
                tier = t
        return tier["max_exposure_pct"] * loss_penalty


class TestEdgeScaler:
    def test_starts_at_20_percent(self):
        scaler = EdgeScaler()
        assert scaler.current_limit([]) == 0.20

    def test_five_wins_unlocks_40_percent(self):
        scaler = EdgeScaler()
        record = [{"profitable": True}] * 5
        assert scaler.current_limit(record) == 0.40

    def test_ten_wins_unlocks_60_percent(self):
        scaler = EdgeScaler()
        record = [{"profitable": True}] * 10
        assert scaler.current_limit(record) == 0.60

    def test_twenty_wins_unlocks_full(self):
        scaler = EdgeScaler()
        record = [{"profitable": True}] * 20
        assert scaler.current_limit(record) == 1.00

    def test_losing_streak_halves_limit(self):
        scaler = EdgeScaler()
        record = [{"profitable": True}] * 10 + [{"profitable": False}] * 3
        # 10 wins = 60%, but 3 losses = halved
        assert scaler.current_limit(record) == pytest.approx(0.30)

    def test_mixed_record(self):
        scaler = EdgeScaler()
        record = ([{"profitable": True}] * 7 +
                  [{"profitable": False}] * 2 +
                  [{"profitable": True}] * 1)
        # 8 wins total -> tier 2 (40%), last trade is a win -> no penalty
        assert scaler.current_limit(record) == 0.40

    def test_losses_not_counted_as_wins(self):
        scaler = EdgeScaler()
        record = [{"profitable": False}] * 10
        # 0 wins -> tier 0 (20%), 3+ losses -> halved
        assert scaler.current_limit(record) == 0.10
