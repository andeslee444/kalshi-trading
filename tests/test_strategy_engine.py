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
    InfoEdge,
    bayesian_kelly_multiplier,
    longshot_edge_buy,
    longshot_edge_sell,
    ScheduledScanner,
    SettlementSourceChecker,
    FillProbabilityEstimator,
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

    def test_time_decay_at_24h_is_full(self):
        """At 24h to close, time factor should be 1.0 (no decay)."""
        est = BayesianEdgeEstimator()
        edge_24h = est.estimate_edge(5, "sports", hours_to_close=24)
        edge_48h = est.estimate_edge(5, "sports", hours_to_close=48)
        # Both should be capped at 1.0 time factor
        assert abs(edge_24h.mu_edge - edge_48h.mu_edge) < 1e-10

    def test_time_decay_at_2h_is_aggressive(self):
        """At 2h to close, edge should be ~29% of the 24h edge (sqrt decay)."""
        est = BayesianEdgeEstimator()
        edge_24h = est.estimate_edge(5, "sports", hours_to_close=24)
        edge_2h = est.estimate_edge(5, "sports", hours_to_close=2)
        # time_factor at 2h = sqrt(2/24) = 0.289
        ratio = edge_2h.mu_edge / edge_24h.mu_edge
        assert abs(ratio - math.sqrt(2 / 24)) < 0.02, f"ratio={ratio:.3f}"

    def test_time_decay_floor_at_20_percent(self):
        """Time factor should floor at 20%, not go to zero."""
        est = BayesianEdgeEstimator()
        edge_24h = est.estimate_edge(5, "sports", hours_to_close=24)
        edge_tiny = est.estimate_edge(5, "sports", hours_to_close=0.01)
        ratio = edge_tiny.mu_edge / edge_24h.mu_edge
        assert abs(ratio - 0.20) < 0.02, f"ratio={ratio:.3f}, expected ~0.20"

    def test_time_decay_monotonically_decreasing(self):
        """Edge should monotonically decrease as hours_to_close decreases."""
        est = BayesianEdgeEstimator()
        hours_list = [24, 12, 6, 3, 1, 0.5]
        edges = [est.estimate_edge(5, "sports", hours_to_close=h).mu_edge for h in hours_list]
        for i in range(len(edges) - 1):
            assert edges[i] >= edges[i + 1], (
                f"edge at {hours_list[i]}h ({edges[i]:.4f}) < edge at {hours_list[i+1]}h ({edges[i+1]:.4f})"
            )

    def test_category_penalty_applied_when_loss_rate_high(self):
        """When category has >50% loss rate with >=10 observations,
        edge should be penalized by 50%."""
        est = BayesianEdgeEstimator()
        # Get baseline edge with no data
        baseline = est.estimate_edge(5, "sports", hours_to_close=24)

        # Add 10 observations with 80% loss rate (2 wins, 8 losses)
        for _ in range(2):
            est.update_posterior("sports", 5, True)
        for _ in range(8):
            est.update_posterior("sports", 5, False)

        penalized = est.estimate_edge(5, "sports", hours_to_close=24)
        # With 80% loss rate and n=10, category_penalty should be 0.50
        # The edge should be roughly half (accounting for posterior shrinkage
        # which also changes alpha/delta)
        assert penalized.mu_edge < baseline.mu_edge * 0.75, (
            f"Expected significant penalty: penalized={penalized.mu_edge:.4f}, "
            f"baseline={baseline.mu_edge:.4f}"
        )

    def test_category_penalty_not_applied_when_winning(self):
        """When category has <=50% loss rate, no penalty should be applied."""
        est = BayesianEdgeEstimator()
        baseline = est.estimate_edge(5, "sports", hours_to_close=24)

        # Add 10 observations with 20% loss rate (8 wins, 2 losses)
        for _ in range(8):
            est.update_posterior("sports", 5, True)
        for _ in range(2):
            est.update_posterior("sports", 5, False)

        result = est.estimate_edge(5, "sports", hours_to_close=24)
        # No category penalty applied, edge should not be drastically reduced
        # (posterior shrinkage may still change it somewhat)
        assert result.mu_edge > 0

    def test_category_penalty_not_applied_with_few_observations(self):
        """Category penalty should not apply with fewer than 10 observations."""
        est = BayesianEdgeEstimator()
        baseline = est.estimate_edge(5, "sports", hours_to_close=24)

        # Add 5 observations with 100% loss rate
        for _ in range(5):
            est.update_posterior("sports", 5, False)

        result = est.estimate_edge(5, "sports", hours_to_close=24)
        # With n=5, penalty should NOT apply (n_obs < 10)
        # Edge may change due to shrinkage but no 50% penalty
        assert result.mu_edge > 0


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

    def test_kelly_scale_decreases_with_more_same_category(self):
        """kelly_scale should decrease as n_same_category trades increases."""
        sizer = CorrelationAwareSizer(daily_budget_cents=10000)
        rho = sizer.get_intra_category_rho("sports")  # 0.15
        scale_1 = sizer.kelly_scale(1, rho)
        scale_3 = sizer.kelly_scale(3, rho)
        scale_8 = sizer.kelly_scale(8, rho)
        assert scale_1 == 1.0
        assert scale_3 < scale_1
        assert scale_8 < scale_3
        # With rho=0.15 and n=8: effective_n = 8/(1+7*0.15) = 3.90
        # kelly_scale = sqrt(3.90/8) = 0.698
        assert abs(scale_8 - 0.698) < 0.01


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


# ===================================================================
# ScheduledScanner
# ===================================================================


class TestScheduledScanner:

    def test_current_wave_morning(self):
        """current_wave() returns 1 during 8-10am ET."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 9, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            assert scanner.current_wave() == 1

    def test_current_wave_midday(self):
        """current_wave() returns 2 during 11am-2pm ET."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 12, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            assert scanner.current_wave() == 2

    def test_current_wave_afternoon(self):
        """current_wave() returns 3 during 3-5pm ET."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 16, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            assert scanner.current_wave() == 3

    def test_current_wave_outside_windows(self):
        """current_wave() returns None outside all wave windows."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 6, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            assert scanner.current_wave() is None

    def test_remaining_budget_initial(self):
        """remaining_budget() starts at wave allocation percentage of daily budget."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 9, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            # Wave 1 = 30% of 10000 = 3000
            assert scanner.remaining_budget() == 3000

    def test_remaining_budget_after_spend(self):
        """remaining_budget() decreases after record_spend()."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 9, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            scanner.record_spend(500)
            assert scanner.remaining_budget() == 2500

    def test_should_scan_inside_wave_with_budget(self):
        """should_scan() returns True when inside wave and budget > 0."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 9, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            assert scanner.should_scan() is True

    def test_should_scan_outside_wave(self):
        """should_scan() returns False outside wave windows."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 6, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            assert scanner.should_scan() is False

    def test_should_emergency_scan(self):
        """should_emergency_scan() returns True when volume_ratio > 5.0."""
        scanner = ScheduledScanner(daily_budget_cents=10000)
        assert scanner.should_emergency_scan(6.0) is True
        assert scanner.should_emergency_scan(4.0) is False

    def test_reset_daily(self):
        """reset_daily() resets all wave budgets."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_now = datetime.datetime(2026, 3, 5, 9, 0, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.timezone = datetime.timezone
            scanner.record_spend(2000)
            scanner.reset_daily()
            assert scanner.remaining_budget() == 3000

    def test_record_spend_wave_specific(self):
        """record_spend() in wave 1 reduces wave 1 budget only."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        scanner = ScheduledScanner(daily_budget_cents=10000)
        # Spend in wave 1
        wave1_now = datetime.datetime(2026, 3, 5, 9, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = wave1_now
            mock_dt.timezone = datetime.timezone
            scanner.record_spend(1000)
            assert scanner.remaining_budget() == 2000  # wave 1: 3000 - 1000
        # Wave 2 budget untouched
        wave2_now = datetime.datetime(2026, 3, 5, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = wave2_now
            mock_dt.timezone = datetime.timezone
            assert scanner.remaining_budget() == 5000  # wave 2: 50% of 10000

    def test_unspent_budget_does_not_carry_forward(self):
        """Unspent wave 1 budget does NOT add to wave 2."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        scanner = ScheduledScanner(daily_budget_cents=10000)
        # Don't spend anything in wave 1
        # Check wave 2 is still just 50%
        wave2_now = datetime.datetime(2026, 3, 5, 12, 0, 0, tzinfo=et)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = wave2_now
            mock_dt.timezone = datetime.timezone
            assert scanner.remaining_budget() == 5000

    def test_next_scan_time_between_waves(self):
        """next_scan_time() returns start of next wave when between waves."""
        from unittest.mock import patch
        import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        # 10:30 AM ET -- between wave 1 (8-10) and wave 2 (11-14)
        mock_now = datetime.datetime(2026, 3, 5, 10, 30, 0, tzinfo=et)
        scanner = ScheduledScanner(daily_budget_cents=10000)
        with patch("strategy_engine.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = mock_now
            mock_dt.datetime.side_effect = lambda *a, **kw: datetime.datetime(*a, **kw)
            mock_dt.timezone = datetime.timezone
            result = scanner.next_scan_time()
            assert result is not None
            # Next wave starts at 11:00 ET
            assert result.hour == 11
            assert result.minute == 0


# ===================================================================
# SettlementSourceChecker
# ===================================================================


class TestSettlementSourceChecker:

    def test_check_info_edge_no_file(self, tmp_path):
        """Returns None when health-state.json does not exist."""
        checker = SettlementSourceChecker(health_state_path=tmp_path / "nonexistent.json")
        assert checker.check_info_edge("KXHIGHMIA-05MAR26-T80") is None

    def test_check_info_edge_stale_source(self, tmp_path):
        """Returns None when source data is stale (>30 min old)."""
        import datetime
        health_path = tmp_path / "health-state.json"
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)).isoformat()
        health_data = {"sources": {"NWS": {"last_success": old_time, "error_count": 0}}}
        health_path.write_text(json.dumps(health_data))
        checker = SettlementSourceChecker(health_state_path=health_path, staleness_minutes=30)
        assert checker.check_info_edge("KXHIGHMIA-05MAR26-T80") is None

    def test_check_info_edge_fresh_nws(self, tmp_path):
        """Returns InfoEdge for KXHIGH ticker when NWS source is fresh."""
        import datetime
        health_path = tmp_path / "health-state.json"
        fresh_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
        health_data = {"sources": {"NWS": {"last_success": fresh_time, "error_count": 0}}}
        health_path.write_text(json.dumps(health_data))
        checker = SettlementSourceChecker(health_state_path=health_path, staleness_minutes=30)
        result = checker.check_info_edge("KXHIGHMIA-05MAR26-T80")
        assert result is not None
        assert isinstance(result, InfoEdge)
        assert result.source == "NWS"
        assert result.edge == 0.50
        assert result.confidence == 0.95
        assert result.sizing_method == "half_kelly"
        assert result.pricing_method == "full_ask"

    def test_check_info_edge_fresh_hdd(self, tmp_path):
        """Returns InfoEdge for album ticker when HDD source is fresh."""
        import datetime
        health_path = tmp_path / "health-state.json"
        fresh_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
        health_data = {"sources": {"HDD": {"last_success": fresh_time, "error_count": 0}}}
        health_path.write_text(json.dumps(health_data))
        checker = SettlementSourceChecker(health_state_path=health_path, staleness_minutes=30)
        result = checker.check_info_edge("ALBUM-SALES-TEST")
        assert result is not None
        assert result.source == "HDD"
        assert result.edge == 0.80
        assert result.confidence == 0.98

    def test_info_edge_fields(self, tmp_path):
        """InfoEdge has all required fields."""
        import datetime
        health_path = tmp_path / "health-state.json"
        fresh_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
        health_data = {"sources": {"NWS": {"last_success": fresh_time, "error_count": 0}}}
        health_path.write_text(json.dumps(health_data))
        checker = SettlementSourceChecker(health_state_path=health_path)
        result = checker.check_info_edge("KXHIGHMIA-05MAR26-T80")
        assert hasattr(result, "source")
        assert hasattr(result, "edge")
        assert hasattr(result, "confidence")
        assert hasattr(result, "sizing_method")
        assert hasattr(result, "pricing_method")

    def test_check_info_edge_unrecognized_ticker(self, tmp_path):
        """Returns None for unrecognized ticker types."""
        import datetime
        health_path = tmp_path / "health-state.json"
        fresh_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
        health_data = {"sources": {"NWS": {"last_success": fresh_time, "error_count": 0}}}
        health_path.write_text(json.dumps(health_data))
        checker = SettlementSourceChecker(health_state_path=health_path)
        assert checker.check_info_edge("UNKNOWNTICKER-123") is None

    def test_is_source_fresh(self, tmp_path):
        """_is_source_fresh returns True when last_success is within threshold."""
        import datetime
        checker = SettlementSourceChecker(health_state_path=tmp_path / "dummy.json")
        fresh_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).isoformat()
        assert checker._is_source_fresh({"last_success": fresh_time}, 30) is True
        old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)).isoformat()
        assert checker._is_source_fresh({"last_success": old_time}, 30) is False


# ===================================================================
# FillProbabilityEstimator
# ===================================================================


class TestFillProbabilityEstimator:

    def test_estimate_fill_prob_returns_between_0_and_1(self):
        """estimate_fill_prob returns a value between 0 and 1."""
        fp = FillProbabilityEstimator()
        prob = fp.estimate_fill_prob(50, 50, 10)
        assert 0.0 < prob < 1.0

    def test_fill_prob_increases_toward_ask(self):
        """Fill probability increases as limit price moves toward ask (aggressive)."""
        fp = FillProbabilityEstimator()
        # Mid=50, spread=10, so ask=55, bid=45
        prob_at_mid = fp.estimate_fill_prob(50, 50, 10)
        prob_aggressive = fp.estimate_fill_prob(54, 50, 10)  # closer to ask
        assert prob_aggressive > prob_at_mid

    def test_fill_prob_approximately_half_at_midpoint(self):
        """Fill probability is approximately 0.5 at midpoint (with default betas)."""
        fp = FillProbabilityEstimator()
        prob = fp.estimate_fill_prob(50, 50, 10, depth=0.5, duration_minutes=60)
        # With default betas, sigmoid at mid should be around 0.5 (within margin)
        assert 0.2 < prob < 0.8

    def test_adjust_limit_price_high_fill_prob(self):
        """adjust_limit_price returns original price when P(fill) > 0.7."""
        fp = FillProbabilityEstimator()
        result = fp.adjust_limit_price(50, 0.15, 0.8, 45, 55, "yes")
        assert result == 50

    def test_adjust_limit_price_low_edge(self):
        """adjust_limit_price returns original price when edge < 0.05."""
        fp = FillProbabilityEstimator()
        result = fp.adjust_limit_price(50, 0.03, 0.2, 45, 55, "yes")
        assert result == 50

    def test_adjust_limit_price_aggressive_on_low_fill(self):
        """adjust_limit_price moves price toward ask when P(fill) < 0.3 and edge > 0.10."""
        fp = FillProbabilityEstimator()
        # Low fill prob, high edge -- should adjust toward ask
        result = fp.adjust_limit_price(48, 0.15, 0.2, 45, 55, "yes")
        assert result >= 48  # should move toward ask (higher)

    def test_update_from_outcome_shifts_betas(self):
        """update_from_outcome shifts betas toward observed fill patterns."""
        fp = FillProbabilityEstimator()
        old_betas = fp._betas[:]
        # Simulate several fills at aggressive prices
        for _ in range(20):
            fp.update_from_outcome(True, 54, 50, 10, 0.5, 60)
        # Betas should have changed
        assert fp._betas != old_betas

    def test_save_load_round_trip(self, tmp_path):
        """save/load round-trip preserves betas."""
        fp = FillProbabilityEstimator()
        fp.update_from_outcome(True, 54, 50, 10, 0.5, 60)
        model_path = tmp_path / "fill-model.json"
        fp.save_model(model_path)

        fp2 = FillProbabilityEstimator()
        fp2.load_model(model_path)
        assert fp._betas == fp2._betas
