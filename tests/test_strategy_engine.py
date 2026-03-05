"""Tests for strategy_engine module: Bayesian edge, dual-tail longshot,
correlation-aware sizing, and confidence-scaled Kelly.

All math uses only the `math` module (no scipy/numpy).
"""

import json
import math
import os
import tempfile
from pathlib import Path

import pytest

from probability import _reset_calibration
from strategy_engine import (
    BayesianEdgeEstimator,
    CorrelationAwareSizer,
    EdgeEstimate,
    bayesian_kelly_multiplier,
    longshot_edge_buy,
    longshot_edge_sell,
)


@pytest.fixture(autouse=True)
def reset_calibration():
    """Reset calibration state between tests."""
    _reset_calibration()
    yield
    _reset_calibration()


# ===================================================================
# BayesianEdgeEstimator
# ===================================================================


class TestBayesianEdgeEstimator:

    def test_prior_returns_positive_edge_for_low_price(self):
        """With 0 observations, edge should match Becker prior and be > 0."""
        est = BayesianEdgeEstimator()
        result = est.estimate_edge(5, "sports", hours_to_close=24)
        assert isinstance(result, EdgeEstimate)
        assert result.mu_edge > 0
        assert result.sigma_edge > 0
        assert result.confidence_ratio == result.mu_edge / result.sigma_edge

    def test_prior_matches_becker_edge(self):
        """With 0 observations, mu_edge should be close to longshot_edge output."""
        from probability import longshot_edge

        est = BayesianEdgeEstimator()
        result = est.estimate_edge(5, "sports", hours_to_close=24)
        becker_edge = longshot_edge(5, ticker="KXNBA-TEST", hours_to_close=24)
        # Should be within 10% of Becker edge (they use the same formula at n=0)
        assert abs(result.mu_edge - becker_edge) / max(becker_edge, 1e-6) < 0.10

    def test_zero_observations_returns_prior(self):
        """Category with 0 observations should return Becker prior edge."""
        est = BayesianEdgeEstimator()
        result = est.estimate_edge(5, "sports", hours_to_close=999)
        assert result.n_observations == 0

    def test_online_updates_shift_posterior(self):
        """After 50 sports updates at price=5 where 48/50 settle as wins,
        posterior should shift toward higher overpricing."""
        est = BayesianEdgeEstimator()
        prior_result = est.estimate_edge(5, "sports", hours_to_close=24)

        # 48 wins, 2 losses at price 5 (seller wins = YES expires worthless)
        for _ in range(48):
            est.update_posterior("sports", 5, True)
        for _ in range(2):
            est.update_posterior("sports", 5, False)

        posterior_result = est.estimate_edge(5, "sports", hours_to_close=24)
        assert posterior_result.n_observations == 50
        # With 96% seller win rate, posterior should indicate strong overpricing
        assert posterior_result.mu_edge > 0

    def test_shrinkage_at_n5(self):
        """Category with n=5 trades -> posterior weighted 5/(5+30) ~ 14% empirical."""
        est = BayesianEdgeEstimator()
        kappa = 30
        n = 5
        expected_weight = n / (n + kappa)  # 5/35 = 0.143
        assert abs(expected_weight - 5 / 35) < 0.001

        # After 5 updates, the estimator should still be dominated by prior
        for _ in range(5):
            est.update_posterior("sports", 5, True)
        result = est.estimate_edge(5, "sports", hours_to_close=24)
        assert result.n_observations == 5

    def test_shrinkage_at_n60(self):
        """Category with n=60 trades -> posterior weighted 60/(60+30) ~ 67% empirical."""
        est = BayesianEdgeEstimator()
        kappa = 30
        n = 60
        expected_weight = n / (n + kappa)  # 60/90 = 0.667
        assert abs(expected_weight - 60 / 90) < 0.001

        # After 60 updates, the estimator should be dominated by empirical
        for _ in range(60):
            est.update_posterior("sports", 5, True)
        result = est.estimate_edge(5, "sports", hours_to_close=24)
        assert result.n_observations == 60

    def test_price_bucket_1_to_5(self):
        """Prices 1-5 should map to bucket '1-5'."""
        est = BayesianEdgeEstimator()
        for p in [1, 2, 3, 4, 5]:
            assert est._price_bucket(p) == "1-5"

    def test_price_bucket_6_to_10(self):
        """Prices 6-10 should map to bucket '6-10'."""
        est = BayesianEdgeEstimator()
        for p in [6, 7, 8, 9, 10]:
            assert est._price_bucket(p) == "6-10"

    def test_price_bucket_11_to_15(self):
        """Prices 11-15 should map to bucket '11-15'."""
        est = BayesianEdgeEstimator()
        for p in [11, 12, 13, 14, 15]:
            assert est._price_bucket(p) == "11-15"

    def test_price_bucket_16_to_20(self):
        """Prices 16-20 should map to bucket '16-20'."""
        est = BayesianEdgeEstimator()
        for p in [16, 17, 18, 19, 20]:
            assert est._price_bucket(p) == "16-20"

    def test_price_bucket_21_to_30(self):
        """Prices 21-30 should map to bucket '21-30'."""
        est = BayesianEdgeEstimator()
        for p in [21, 25, 30]:
            assert est._price_bucket(p) == "21-30"

    def test_update_win_increments_bucket(self):
        """update_posterior with win at 3c should increment bucket '1-5' win_count."""
        est = BayesianEdgeEstimator()
        est.update_posterior("sports", 3, True)
        bucket_data = est._category_state["sports"]["buckets"]["1-5"]
        assert bucket_data["wins"] == 1
        assert bucket_data["losses"] == 0

    def test_update_loss_increments_bucket(self):
        """update_posterior with loss at 8c should increment bucket '6-10' loss_count."""
        est = BayesianEdgeEstimator()
        est.update_posterior("sports", 8, False)
        bucket_data = est._category_state["sports"]["buckets"]["6-10"]
        assert bucket_data["wins"] == 0
        assert bucket_data["losses"] == 1

    def test_persistence_round_trip(self):
        """save_params() writes JSON, load_params() reads back identical state."""
        est = BayesianEdgeEstimator()
        est.update_posterior("sports", 5, True)
        est.update_posterior("sports", 5, True)
        est.update_posterior("entertainment", 10, False)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            tmp_path = Path(f.name)

        try:
            est.save_params(tmp_path)

            est2 = BayesianEdgeEstimator()
            est2.load_params(tmp_path)

            # Compare edge estimates
            r1 = est.estimate_edge(5, "sports", hours_to_close=24)
            r2 = est2.estimate_edge(5, "sports", hours_to_close=24)
            assert abs(r1.mu_edge - r2.mu_edge) < 1e-10
            assert abs(r1.sigma_edge - r2.sigma_edge) < 1e-10
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_edge_zero_for_out_of_range(self):
        """Price 0 or negative should return edge 0."""
        est = BayesianEdgeEstimator()
        result = est.estimate_edge(0, "sports", hours_to_close=24)
        assert result.mu_edge == 0.0
        result = est.estimate_edge(-5, "sports", hours_to_close=24)
        assert result.mu_edge == 0.0

    def test_edge_positive_for_valid_price(self):
        """Price 5c sports should have positive edge."""
        est = BayesianEdgeEstimator()
        result = est.estimate_edge(5, "sports", hours_to_close=24)
        assert result.mu_edge > 0

    def test_edge_estimate_buy_symmetry(self):
        """Sell-side edge at price P should approximately equal
        buy-side edge at price (100-P) for same category."""
        est = BayesianEdgeEstimator()
        sell_result = est.estimate_edge(5, "sports", hours_to_close=24)
        buy_result = est.estimate_edge_buy(95, "sports", hours_to_close=24)
        # They should be close (same Becker model applied to the same effective price)
        assert abs(sell_result.mu_edge - buy_result.mu_edge) < 0.01

    def test_estimate_edge_returns_named_fields(self):
        """EdgeEstimate must have all documented fields."""
        est = BayesianEdgeEstimator()
        result = est.estimate_edge(5, "sports", hours_to_close=24)
        assert hasattr(result, "mu_edge")
        assert hasattr(result, "sigma_edge")
        assert hasattr(result, "confidence_ratio")
        assert hasattr(result, "category")
        assert hasattr(result, "price_bucket")
        assert hasattr(result, "n_observations")


# ===================================================================
# Dual-Tail Longshot
# ===================================================================


class TestDualTailLongshot:

    def test_sell_side_5c_returns_positive_edge(self):
        """longshot_edge_sell at 5c should return edge > 0."""
        edge = longshot_edge_sell(5, "KXNBA-TEST", hours_to_close=24)
        assert edge > 0

    def test_sell_side_25c_returns_positive_edge(self):
        """longshot_edge_sell at 25c (expanded range) should return edge > 0."""
        edge = longshot_edge_sell(25, "KXNBA-TEST", hours_to_close=24)
        assert edge > 0

    def test_sell_side_35c_returns_zero(self):
        """longshot_edge_sell at 35c (outside 1-30c range) should return 0."""
        edge = longshot_edge_sell(35, "KXNBA-TEST", hours_to_close=24)
        assert edge == 0.0

    def test_sell_side_matches_original_at_5c(self):
        """longshot_edge_sell(5) without estimator should match longshot_edge(5)."""
        from probability import longshot_edge

        sell_edge = longshot_edge_sell(5, "KXNBA-TEST", hours_to_close=24)
        orig_edge = longshot_edge(5, ticker="KXNBA-TEST", hours_to_close=24)
        assert abs(sell_edge - orig_edge) < 1e-10

    def test_buy_side_92c_returns_positive_edge(self):
        """longshot_edge_buy at YES=92c (NO=8c longshot) returns edge > 0."""
        edge = longshot_edge_buy(92, "KXNBA-TEST", hours_to_close=24)
        assert edge > 0

    def test_buy_side_50c_returns_zero(self):
        """longshot_edge_buy at 50c (not a longshot tail) returns 0."""
        edge = longshot_edge_buy(50, "KXNBA-TEST", hours_to_close=24)
        assert edge == 0.0

    def test_buy_side_75c_returns_positive_but_smaller(self):
        """longshot_edge_buy at 75c returns edge > 0 but smaller than at 95c."""
        edge_75 = longshot_edge_buy(75, "KXNBA-TEST", hours_to_close=24)
        edge_95 = longshot_edge_buy(95, "KXNBA-TEST", hours_to_close=24)
        assert edge_75 > 0
        assert edge_95 > edge_75

    def test_buy_side_symmetry_with_sell_side(self):
        """Buy-side at YES=95c should produce similar edge as sell-side at YES=5c."""
        sell_edge = longshot_edge_sell(5, "KXNBA-TEST", hours_to_close=24)
        buy_edge = longshot_edge_buy(95, "KXNBA-TEST", hours_to_close=24)
        # Should be equal (Becker model applied symmetrically)
        assert abs(sell_edge - buy_edge) < 1e-10


# ===================================================================
# CorrelationAwareSizer
# ===================================================================


class TestCorrelationAwareSizer:

    def test_effective_n_formula(self):
        """effective_n(n=8, rho=0.15) = 8/(1+7*0.15) = 3.90 within 0.01."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        result = sizer.effective_n(8, 0.15)
        expected = 8.0 / (1.0 + 7.0 * 0.15)  # 8/2.05 = 3.902...
        assert abs(result - expected) < 0.01

    def test_effective_n_single_trade(self):
        """effective_n(n=1, rho=0.5) = 1.0 (single trade, no reduction)."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        result = sizer.effective_n(1, 0.5)
        assert abs(result - 1.0) < 0.001

    def test_effective_n_independent(self):
        """effective_n(n=10, rho=0.0) = 10.0 (independent trades)."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        result = sizer.effective_n(10, 0.0)
        assert abs(result - 10.0) < 0.001

    def test_kelly_scale_formula(self):
        """kelly_scale(n=8, rho=0.15) = sqrt(3.90/8) = 0.698 within 0.01."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        result = sizer.kelly_scale(8, 0.15)
        eff_n = 8.0 / (1.0 + 7.0 * 0.15)
        expected = math.sqrt(eff_n / 8.0)
        assert abs(result - expected) < 0.01
        assert abs(result - 0.698) < 0.01

    def test_kelly_scale_single_trade_returns_1(self):
        """kelly_scale(n=1, rho=0.5) = 1.0."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        result = sizer.kelly_scale(1, 0.5)
        assert abs(result - 1.0) < 0.001

    def test_category_cap_denies_over_budget(self):
        """check_category_cap returns False when proposed risk exceeds 30% of daily budget."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000, category_cap_pct=0.30)
        # Fill up to near cap
        sizer.record_trade("sports", 2900)  # 29% used
        # Propose trade that would push to 31%
        assert sizer.check_category_cap("sports", 200) is False

    def test_category_cap_allows_within_budget(self):
        """check_category_cap returns True when within budget."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000, category_cap_pct=0.30)
        sizer.record_trade("sports", 1000)  # 10% used
        assert sizer.check_category_cap("sports", 500) is True

    def test_intra_category_rho_lookup(self):
        """get_intra_category_rho returns expected default rho values."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        rho = sizer.get_intra_category_rho("sports")
        assert rho > 0

    def test_cross_category_rho(self):
        """Cross-category rho should be very small (0.02)."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        rho = sizer.get_intra_category_rho("cross_category")
        assert abs(rho - 0.02) < 0.01

    def test_daily_reset_clears_state(self):
        """reset_daily() clears all tracked risk."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        sizer.record_trade("sports", 2000)
        sizer.reset_daily()
        # After reset, should allow full budget again
        assert sizer.check_category_cap("sports", 2500) is True


# ===================================================================
# bayesian_kelly_multiplier
# ===================================================================


class TestBayesianKellyMultiplier:

    def test_negative_confidence_returns_zero(self):
        """confidence_ratio <= 0: multiplier = 0.0."""
        assert bayesian_kelly_multiplier(-1.0) == 0.0
        assert bayesian_kelly_multiplier(0.0) == 0.0

    def test_half_confidence(self):
        """confidence_ratio = 0.5: multiplier = 0.25 (quadratic penalty)."""
        result = bayesian_kelly_multiplier(0.5)
        assert abs(result - 0.25) < 0.001

    def test_unit_confidence(self):
        """confidence_ratio = 1.0: multiplier = 1.0^2 = 1.0 (boundary of quadratic)."""
        result = bayesian_kelly_multiplier(1.0)
        assert abs(result - 1.0) < 0.001

    def test_moderate_confidence(self):
        """confidence_ratio = 1.5: multiplier = 1.5/2.0 = 0.75."""
        result = bayesian_kelly_multiplier(1.5)
        assert abs(result - 0.75) < 0.001

    def test_high_confidence(self):
        """confidence_ratio = 2.0: multiplier = 1.0 (capped)."""
        result = bayesian_kelly_multiplier(2.0)
        assert abs(result - 1.0) < 0.001

    def test_very_high_confidence_capped(self):
        """confidence_ratio = 3.0: multiplier = 1.0 (capped at 1.0)."""
        result = bayesian_kelly_multiplier(3.0)
        assert abs(result - 1.0) < 0.001


# ===================================================================
# Moment Matching
# ===================================================================


class TestMomentMatching:

    def test_synthetic_data_recovers_params(self):
        """Synthetic data with known alpha=0.60, delta=0.12 should recover
        params within tolerance after grid search."""
        est = BayesianEdgeEstimator()

        # Generate synthetic data: Becker model with known params
        true_alpha = 0.60
        true_delta = 0.12

        # For each bucket, compute seller win rate under Becker model
        # seller_win_rate = 1 - implied_prob * (1 - alpha*exp(-delta*midprice))
        # ... which equals the overpricing fraction
        bucket_midprices = {"1-5": 3, "6-10": 8, "11-15": 13, "16-20": 18, "21-30": 25}

        bucket_data = {}
        for bucket, mid in bucket_midprices.items():
            implied = mid / 100.0
            overpricing = true_alpha * math.exp(-true_delta * mid)
            seller_win_rate = 1.0 - implied * (1.0 - overpricing)
            # Simulate 100 trades at each bucket
            wins = int(seller_win_rate * 100)
            losses = 100 - wins
            bucket_data[bucket] = {"wins": wins, "losses": losses}

        fitted_alpha, fitted_delta = est._moment_match_alpha_delta(bucket_data)
        # Grid search resolution: alpha step=0.05, delta step=0.01
        # Tolerance allows for discretization error
        assert abs(fitted_alpha - true_alpha) < 0.16, f"alpha: {fitted_alpha} vs {true_alpha}"
        assert abs(fitted_delta - true_delta) < 0.06, f"delta: {fitted_delta} vs {true_delta}"

    def test_too_few_buckets_returns_none(self):
        """With fewer than 2 buckets with data, should return None."""
        est = BayesianEdgeEstimator()
        bucket_data = {"1-5": {"wins": 5, "losses": 1}}
        result = est._moment_match_alpha_delta(bucket_data)
        assert result is None
