"""Tests for empirical_ensemble_probability() in probability.py."""

import sys
import pytest

sys.path.insert(0, "src/kalshi")

from probability import empirical_ensemble_probability, _reset_calibration


class TestEmpiricalEnsembleProbability:

    def setup_method(self):
        _reset_calibration()

    def teardown_method(self):
        _reset_calibration()

    # --- Direction "T" (above threshold) ---

    def test_all_members_above_threshold(self):
        """All members well above threshold -> probability near 1.0 (clamped to 0.99)."""
        members = [90.0 + i * 0.1 for i in range(30)]  # 90.0 to 92.9
        prob = empirical_ensemble_probability(members, 80.0, "T")
        assert prob == pytest.approx(0.99, abs=0.01)

    def test_all_members_below_threshold(self):
        """All members well below threshold -> probability near 0.0 (clamped to 0.01)."""
        members = [70.0 + i * 0.1 for i in range(30)]  # 70.0 to 72.9
        prob = empirical_ensemble_probability(members, 85.0, "T")
        assert prob == pytest.approx(0.01, abs=0.01)

    def test_members_split_50_50(self):
        """Members evenly split around threshold -> probability near 0.5."""
        # 15 below and 15 above, symmetric
        members = [80.0 + i for i in range(-15, 15)]
        prob = empirical_ensemble_probability(members, 80.0, "T")
        assert 0.40 <= prob <= 0.60

    def test_realistic_gfs_above_threshold(self):
        """31 GFS members around 85F, threshold 82F -> should be well above 0.5."""
        import random
        random.seed(42)
        members = [85.0 + random.gauss(0, 2.0) for _ in range(31)]
        prob = empirical_ensemble_probability(members, 82.0, "T")
        assert prob > 0.7

    def test_realistic_full_ensemble(self):
        """82 members (31 GFS + 51 ECMWF) around 75F, threshold 80F -> low probability."""
        import random
        random.seed(42)
        gfs = [75.0 + random.gauss(0, 2.0) for _ in range(31)]
        ecmwf = [74.5 + random.gauss(0, 1.8) for _ in range(51)]
        members = gfs + ecmwf
        prob = empirical_ensemble_probability(members, 80.0, "T")
        assert prob < 0.15

    # --- Direction "B" (bracket) ---

    def test_bracket_members_at_threshold(self):
        """Members clustered at threshold+0.5 -> reasonable bracket probability."""
        members = [85.0 + i * 0.1 for i in range(20)]  # 85.0 to 86.9
        prob = empirical_ensemble_probability(members, 85.0, "B")
        # Bracket is [85, 86), many members fall in this range
        assert 0.05 < prob < 0.60

    def test_bracket_members_far_above(self):
        """Members all far above bracket range -> low bracket probability."""
        members = [95.0 + i * 0.1 for i in range(20)]
        prob = empirical_ensemble_probability(members, 85.0, "B")
        assert prob < 0.10

    def test_bracket_members_far_below(self):
        """Members all far below bracket range -> low bracket probability."""
        members = [70.0 + i * 0.1 for i in range(20)]
        prob = empirical_ensemble_probability(members, 85.0, "B")
        assert prob < 0.10

    # --- Bias offset ---

    def test_bias_offset_shifts_probability(self):
        """Positive bias_offset reduces probability (corrects warm bias)."""
        members = [85.0] * 30
        prob_no_bias = empirical_ensemble_probability(members, 84.0, "T")
        prob_with_bias = empirical_ensemble_probability(members, 84.0, "T", bias_offset=2.0)
        # With warm bias correction, effective members are at 83F, so P(>84) is lower
        assert prob_with_bias < prob_no_bias

    def test_bias_offset_equivalence(self):
        """Members at 85F with bias=2.0 should give similar result as members at 83F."""
        members_high = [85.0] * 30
        members_low = [83.0] * 30
        prob_biased = empirical_ensemble_probability(members_high, 84.0, "T", bias_offset=2.0)
        prob_shifted = empirical_ensemble_probability(members_low, 84.0, "T", bias_offset=0.0)
        assert prob_biased == pytest.approx(prob_shifted, abs=0.02)

    # --- Edge cases ---

    def test_empty_list_returns_none(self):
        assert empirical_ensemble_probability([], 85.0, "T") is None

    def test_fewer_than_5_members_returns_none(self):
        assert empirical_ensemble_probability([85.0, 86.0, 84.0], 85.0, "T") is None

    def test_single_member_returns_none(self):
        assert empirical_ensemble_probability([85.0], 85.0, "T") is None

    def test_none_input_returns_none(self):
        assert empirical_ensemble_probability(None, 85.0, "T") is None

    def test_exactly_5_members_works(self):
        """5 members (minimum) should return a valid probability."""
        members = [80.0, 82.0, 84.0, 86.0, 88.0]
        prob = empirical_ensemble_probability(members, 84.0, "T")
        assert prob is not None
        assert 0.01 <= prob <= 0.99

    # --- Clamping ---

    def test_clamped_high(self):
        """All members far above threshold -> clamped to 0.99 max."""
        members = [100.0 + i for i in range(30)]
        prob = empirical_ensemble_probability(members, 50.0, "T")
        assert prob == pytest.approx(0.99, abs=0.001)

    def test_clamped_low(self):
        """All members far below threshold -> clamped to 0.01 min."""
        members = [30.0 + i * 0.1 for i in range(30)]
        prob = empirical_ensemble_probability(members, 90.0, "T")
        assert prob == pytest.approx(0.01, abs=0.001)

    # --- Return type ---

    def test_returns_float(self):
        members = [85.0 + i * 0.5 for i in range(20)]
        prob = empirical_ensemble_probability(members, 85.0, "T")
        assert isinstance(prob, float)

    def test_invalid_direction_returns_none(self):
        members = [85.0] * 20
        prob = empirical_ensemble_probability(members, 85.0, "X")
        assert prob is None

    # --- KDE bandwidth ---

    def test_tight_ensemble_narrow_bandwidth(self):
        """Very tight ensemble (all same value) uses minimum bandwidth of 0.5."""
        members = [85.0] * 30
        # Should still produce valid probability (not crash from zero std)
        prob = empirical_ensemble_probability(members, 85.0, "T")
        assert prob is not None
        # With all at 85 and threshold at 85, roughly 50%
        assert 0.40 <= prob <= 0.60
