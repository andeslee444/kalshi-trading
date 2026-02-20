"""Tests for the audit engine's pure analysis functions.

Tests cover: fee bug quantification, Kelly recalculation, NWS station URL
construction, trade record schema validation, sigma schedules, probability
model spot-checks, and the Finding dataclass.

conftest.py adds src/kalshi/ to sys.path so direct imports work.
"""

import math
import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import audit module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from probability import (
    half_kelly, half_kelly_sell, quarter_kelly, high_conviction_kelly,
    kalshi_fee_cents, edge_after_fees,
    weather_probability, nws_probability, info_arb_probability,
    crypto_price_probability, econ_nowcast_probability, cpi_nowcast_sigma,
    album_data_sigma, boxoffice_data_sigma, longshot_edge,
    _norm_cdf, _reset_calibration,
)
from audit import Finding, AuditEngine, load_json_safe


@pytest.fixture(autouse=True)
def reset_calibration():
    """Reset calibration state before/after each test."""
    _reset_calibration()
    yield
    _reset_calibration()


# ─── Finding dataclass ───

class TestFinding:
    def test_finding_creation(self):
        f = Finding("0", "0.1", Finding.PASS, "Test title", "Test detail")
        assert f.section == "0"
        assert f.severity == "pass"

    def test_finding_to_dict(self):
        f = Finding("1", "1.1", Finding.WARN, "Warning", "Detail", {"key": "val"})
        d = f.to_dict()
        assert d["severity"] == "warn"
        assert d["evidence"]["key"] == "val"

    def test_finding_repr(self):
        f = Finding("0", "0.1", Finding.PASS, "OK", "")
        assert "✓" in repr(f)

    def test_finding_fail_repr(self):
        f = Finding("0", "0.1", Finding.FAIL, "Bad", "")
        assert "✗" in repr(f)


# ─── Fee bug quantification ───

class TestFeeBugQuantification:
    def test_fee_at_50c(self):
        """Fee at 50c should be 0.07 * 0.5 * 0.5 * 100 = 1.75c."""
        fee = kalshi_fee_cents(50)
        assert abs(fee - 1.75) < 0.01

    def test_fee_at_10c(self):
        """Fee at 10c should be 0.07 * 0.1 * 0.9 * 100 = 0.63c."""
        fee = kalshi_fee_cents(10)
        assert abs(fee - 0.63) < 0.01

    def test_fee_symmetric(self):
        """Fee at 30c should equal fee at 70c (symmetric in P*(1-P))."""
        assert abs(kalshi_fee_cents(30) - kalshi_fee_cents(70)) < 0.001

    def test_edge_after_fees_reduces_edge(self):
        """edge_after_fees should always return less than raw edge."""
        raw = 0.15
        adjusted = edge_after_fees(raw, 50)
        assert adjusted < raw

    def test_fee_cents_param_reduces_kelly(self):
        """half_kelly with fee_cents should produce <= contracts vs without."""
        c_no_fee, _ = half_kelly(0.15, 40, 1000, bankroll_cents=50000, fee_cents=0)
        c_with_fee, _ = half_kelly(0.15, 40, 1000, bankroll_cents=50000,
                                    fee_cents=kalshi_fee_cents(40))
        assert c_with_fee <= c_no_fee

    def test_fee_cents_does_not_cause_zero_for_large_edge(self):
        """A large edge should still produce trades even with fees."""
        fee = kalshi_fee_cents(30)
        contracts, _ = half_kelly(0.20, 30, 1000, bankroll_cents=50000, fee_cents=fee)
        assert contracts > 0

    def test_fee_cents_causes_zero_when_payout_negative(self):
        """If fee exceeds win amount (100-price), should return 0."""
        # At price 99c, win_amount = 1c. Fee at 99c ≈ 0.07 * 0.99 * 0.01 * 100 ≈ 0.07c
        # But if we artificially set fee_cents=2 (> 1c win), should get 0
        contracts, _ = half_kelly(0.05, 99, 1000, bankroll_cents=50000, fee_cents=2)
        assert contracts == 0


# ─── Kelly formula verification ───

class TestKellyFormulas:
    def test_half_kelly_sell_positive_edge(self):
        """half_kelly_sell with positive edge returns positive."""
        contracts, risk = half_kelly_sell(0.10, 10, 500, bankroll_cents=50000)
        assert contracts > 0
        assert risk > 0

    def test_half_kelly_sell_fee_reduces_size(self):
        """Fee parameter on sell should reduce contracts."""
        c_no_fee, _ = half_kelly_sell(0.10, 10, 500, bankroll_cents=50000, fee_cents=0)
        c_with_fee, _ = half_kelly_sell(0.10, 10, 500, bankroll_cents=50000, fee_cents=1)
        assert c_with_fee <= c_no_fee

    def test_quarter_kelly_leq_half_kelly(self):
        """quarter_kelly should always return <= half_kelly contracts."""
        hk, _ = half_kelly(0.15, 30, 1000, bankroll_cents=50000)
        qk, _ = quarter_kelly(0.15, 30, 1000, bankroll_cents=50000)
        assert qk <= hk

    def test_quarter_kelly_fee_passthrough(self):
        """quarter_kelly should accept fee_cents."""
        c, _ = quarter_kelly(0.15, 30, 1000, bankroll_cents=50000, fee_cents=1)
        assert c >= 0

    def test_high_conviction_kelly_fee(self):
        """high_conviction_kelly should accept fee_cents."""
        c, _ = high_conviction_kelly(0.30, 20, 1000, bankroll_cents=100000, fee_cents=1)
        assert c >= 0

    def test_high_conviction_scales_with_bankroll(self):
        """Small bankrolls use 50% Kelly, large use 60%."""
        # Small bankroll: $100 = 10000 cents
        c_small, _ = high_conviction_kelly(0.30, 20, 1000, bankroll_cents=10000)
        # Large bankroll: $1000 = 100000 cents
        c_large, _ = high_conviction_kelly(0.30, 20, 1000, bankroll_cents=100000)
        # Large bankroll should get more contracts (60% vs 50%)
        assert c_large >= c_small

    def test_risk_equals_contracts_times_price(self):
        """Risk should equal contracts * price_cents."""
        contracts, risk = half_kelly(0.15, 40, 1000, bankroll_cents=50000)
        if contracts > 0:
            assert risk == contracts * 40


# ─── Weather probability model ───

class TestWeatherModel:
    def test_forecast_above_threshold(self):
        """When forecast > threshold, P(actual > threshold) should be > 0.5."""
        p = weather_probability(85, 80, "T", days_out=0)
        assert p > 0.5

    def test_forecast_below_threshold(self):
        """When forecast < threshold, P(actual > threshold) should be < 0.5."""
        p = weather_probability(75, 80, "T", days_out=0)
        assert p < 0.5

    def test_bracket_positive(self):
        """Bracket probability should be positive."""
        p = weather_probability(80, 80, "B", days_out=0)
        assert 0 < p < 1

    def test_longer_horizon_wider(self):
        """Longer forecast horizon should reduce confidence."""
        p0 = weather_probability(82, 80, "T", days_out=0)
        p7 = weather_probability(82, 80, "T", days_out=7)
        # p0 should be further from 0.5 than p7
        assert abs(p0 - 0.5) > abs(p7 - 0.5)


# ─── NWS sigma model ───

class TestNWSModel:
    def test_late_afternoon_high_confidence(self):
        """At hour 17 with running_high > threshold, P should be very high."""
        p = nws_probability(85, 80, "T", 17)
        assert p > 0.90

    def test_morning_lower_confidence(self):
        """At hour 8 with running_high > threshold, P should be lower than afternoon."""
        p_morning = nws_probability(82, 80, "T", 8)
        p_afternoon = nws_probability(82, 80, "T", 17)
        assert p_afternoon > p_morning

    def test_sigma_floor(self):
        """Sigma should never go below floor (0.5)."""
        # At hour 23, sigma = max(0.5, 4.0 * exp(-0.18 * 17)) ≈ max(0.5, 0.19)
        # so sigma should be 0.5
        p = nws_probability(80, 80, "T", 23)
        # At exactly threshold with sigma=0.5, P(>threshold) should be ~0.5
        assert 0.3 < p < 0.7


# ─── Info arb model ───

class TestInfoArbModel:
    def test_observed_above_threshold(self):
        """When observed >> threshold, P should be near 1."""
        p = info_arb_probability(200000, 100000, 0.10)
        assert p > 0.99

    def test_observed_below_threshold(self):
        """When observed << threshold, P should be near 0."""
        p = info_arb_probability(50000, 100000, 0.10)
        assert p < 0.01

    def test_observed_at_threshold(self):
        """When observed == threshold, P should be ~0.5."""
        p = info_arb_probability(100000, 100000, 0.10)
        assert abs(p - 0.5) < 0.01


# ─── Sigma schedules ───

class TestSigmaSchedules:
    def test_album_sigma_monday(self):
        assert album_data_sigma(0) == 0.15

    def test_album_sigma_wednesday(self):
        assert album_data_sigma(2) == 0.10

    def test_album_sigma_friday(self):
        assert album_data_sigma(4) == 0.03

    def test_boxoffice_sigma_friday(self):
        assert boxoffice_data_sigma(4) == 0.12

    def test_boxoffice_sigma_sunday(self):
        assert boxoffice_data_sigma(6) == 0.05

    def test_boxoffice_sigma_monday(self):
        assert boxoffice_data_sigma(0) == 0.02


# ─── CPI nowcast sigma ───

class TestCPINowcastSigma:
    def test_at_release(self):
        """At release day (0), sigma should be 0.01."""
        assert cpi_nowcast_sigma(0) == 0.01

    def test_monotonic_increase_with_days(self):
        """Sigma should monotonically increase as days_to_release increases (more uncertainty)."""
        sigmas = [cpi_nowcast_sigma(d) for d in range(15)]
        for i in range(len(sigmas) - 1):
            assert sigmas[i] <= sigmas[i + 1], f"Non-monotonic at day {i}: {sigmas[i]} > {sigmas[i+1]}"


# ─── Crypto GBM model ───

class TestCryptoGBM:
    def test_at_money(self):
        """At-money should be slightly below 0.5 (negative drift)."""
        p = crypto_price_probability(100000, 100000, "above", 1440, 0.60)
        assert 0.40 < p < 0.50

    def test_deep_itm(self):
        """Deep in-the-money should give high probability."""
        p = crypto_price_probability(100000, 50000, "above", 1440, 0.60)
        assert p > 0.95

    def test_direction_below(self):
        """P(below) = 1 - P(above)."""
        p_above = crypto_price_probability(100000, 90000, "above", 1440, 0.60)
        p_below = crypto_price_probability(100000, 90000, "below", 1440, 0.60)
        assert abs(p_above + p_below - 1.0) < 0.001


# ─── Longshot edge ───

class TestLongshotEdge:
    def test_positive_at_1c(self):
        """Edge should be positive at 1c."""
        e = longshot_edge(1)
        assert e > 0

    def test_overpricing_ratio_decreases_with_price(self):
        """Overpricing RATIO should decrease with price even if absolute edge increases.

        At 1c: edge = 0.01 * 0.49 ≈ 0.005 (small implied_prob * large overpricing)
        At 10c: edge = 0.10 * 0.13 ≈ 0.013 (larger implied_prob * smaller overpricing)
        The ratio (edge/implied_prob) should decrease.
        """
        e1 = longshot_edge(1)
        e10 = longshot_edge(10)
        ratio_1 = e1 / (1 / 100)
        ratio_10 = e10 / (10 / 100)
        assert ratio_1 > ratio_10

    def test_time_decay(self):
        """Edge at 1h should be less than at 24h."""
        e24 = longshot_edge(5, hours_to_close=24)
        e1 = longshot_edge(5, hours_to_close=1)
        assert e1 < e24

    def test_zero_for_invalid_price(self):
        """Edge should be 0 for invalid prices."""
        assert longshot_edge(0) == 0
        assert longshot_edge(100) == 0


# ─── Audit engine integration ───

class TestAuditEngineIntegration:
    def test_engine_runs_without_crash(self):
        """AuditEngine should run all sections without crashing."""
        engine = AuditEngine(reconcile=False)
        findings = engine.run()
        assert len(findings) > 0

    def test_engine_section_filter(self):
        """Section filter should limit which sections run."""
        engine = AuditEngine(reconcile=False)
        findings = engine.run(sections=["4"])
        # All findings should be from section 4
        for f in findings:
            assert f.section == "4", f"Expected section 4, got {f.section}"

    def test_engine_produces_known_findings(self):
        """Engine should produce the known fee treatment warning."""
        engine = AuditEngine(reconcile=False)
        findings = engine.run(sections=["4"])
        qids = [f.qid for f in findings]
        assert "4.1" in qids  # fee treatment audit
        assert "4.2" in qids  # Kelly formula verification

    def test_load_json_safe_missing(self, tmp_path):
        """load_json_safe should return None for missing files."""
        assert load_json_safe(tmp_path / "nonexistent.json") is None

    def test_load_json_safe_corrupt(self, tmp_path):
        """load_json_safe should return None for corrupt files."""
        p = tmp_path / "bad.json"
        p.write_text("{invalid json")
        assert load_json_safe(p) is None

    def test_load_json_safe_valid(self, tmp_path):
        """load_json_safe should load valid JSON."""
        p = tmp_path / "good.json"
        p.write_text('[{"ticker": "TEST"}]')
        result = load_json_safe(p)
        assert isinstance(result, list)
        assert result[0]["ticker"] == "TEST"
