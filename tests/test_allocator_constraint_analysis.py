"""Tests for the allocator constraint analysis module."""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from research.allocator_constraint_analysis import (
    ConstraintAnalyzer,
    _estimate_ev_cents,
    _normalize_constraint,
)

FIXED_NOW = datetime.datetime(2026, 3, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _now():
    return FIXED_NOW


def _make_decision(*, approved=True, bot_name="weather", ticker="KXHIGHMIA-26MAR17-T80",
                   edge=0.10, confidence=0.85, bot_max_cost_cents=500,
                   reason="", binding_constraint="", timestamp=None):
    return {
        "timestamp": timestamp or FIXED_NOW.isoformat(),
        "bot_name": bot_name,
        "ticker": ticker,
        "edge": edge,
        "confidence": confidence,
        "bot_max_cost_cents": bot_max_cost_cents,
        "source_type": "weather",
        "approved": approved,
        "max_cost_cents": bot_max_cost_cents if approved else 0,
        "bankroll_cents": 500000,
        "reason": reason,
        "binding_constraint": binding_constraint,
    }


# ── EV Estimation Tests ────────────────────────────────────────────


class TestEstimateEv:
    def test_positive_edge_with_cost(self):
        ev = _estimate_ev_cents({"edge": 0.10, "bot_max_cost_cents": 500})
        assert ev == 50.0  # 0.10 * 500

    def test_zero_edge(self):
        ev = _estimate_ev_cents({"edge": 0.0, "bot_max_cost_cents": 500})
        assert ev == 0

    def test_negative_edge(self):
        ev = _estimate_ev_cents({"edge": -0.05, "bot_max_cost_cents": 500})
        assert ev == 0

    def test_missing_edge(self):
        ev = _estimate_ev_cents({"bot_max_cost_cents": 500})
        assert ev == 0

    def test_no_cost_fallback(self):
        ev = _estimate_ev_cents({"edge": 0.10})
        assert ev == 10.0  # 0.10 * 100 (fallback)


# ── Constraint Normalization Tests ─────────────────────────────────


class TestNormalizeConstraint:
    def test_known_capacity_constraint(self):
        constraint, is_dedup = _normalize_constraint({"binding_constraint": "bot_daily_limit", "reason": ""})
        assert constraint == "bot_daily_limit"
        assert is_dedup is False

    def test_dedup_by_binding_constraint(self):
        constraint, is_dedup = _normalize_constraint({"binding_constraint": "dedup", "reason": ""})
        assert is_dedup is True

    def test_dedup_by_reason_already_traded(self):
        constraint, is_dedup = _normalize_constraint({
            "binding_constraint": "",
            "reason": "already traded by economics",
        })
        assert is_dedup is True

    def test_dedup_by_reason_cooldown(self):
        constraint, is_dedup = _normalize_constraint({
            "binding_constraint": "",
            "reason": "cooldown period active",
        })
        assert is_dedup is True

    def test_reason_maps_to_capacity_bucket(self):
        constraint, is_dedup = _normalize_constraint({
            "binding_constraint": "",
            "reason": "daily allocation exhausted for weather",
        })
        assert constraint == "bot_daily_limit"
        assert is_dedup is False

    def test_reason_maps_city_concentration(self):
        constraint, is_dedup = _normalize_constraint({
            "binding_constraint": "",
            "reason": "city exposure limit reached for MIA",
        })
        assert constraint == "city_concentration"
        assert is_dedup is False

    def test_unknown_reason_passes_through(self):
        constraint, is_dedup = _normalize_constraint({
            "binding_constraint": "",
            "reason": "some novel reason",
        })
        assert constraint == "some novel reason"
        assert is_dedup is False


# ── Denial Count Tests ──────────────────────────────────────────────


class TestDenialCounts:
    def test_empty_decisions(self):
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=[], now_func=_now)
        assert analyzer.denial_count_by_constraint() == {}

    def test_all_approved_no_denials(self):
        decisions = [_make_decision(approved=True) for _ in range(5)]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)
        assert analyzer.denial_count_by_constraint() == {}
        assert len(analyzer._denied) == 0

    def test_denials_grouped_by_constraint(self):
        decisions = [
            _make_decision(approved=False, binding_constraint="bot_daily_limit", reason="weather daily allocation exhausted"),
            _make_decision(approved=False, binding_constraint="bot_daily_limit", reason="weather daily allocation exhausted"),
            _make_decision(approved=False, binding_constraint="city_concentration", reason="city exposure limit reached for MIA"),
            _make_decision(approved=True),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        counts = analyzer.denial_count_by_constraint()
        assert counts["bot_daily_limit"] == 2
        assert counts["city_concentration"] == 1

    def test_denials_by_bot(self):
        decisions = [
            _make_decision(approved=False, bot_name="weather", binding_constraint="bot_daily_limit", reason="limit"),
            _make_decision(approved=False, bot_name="weather", binding_constraint="bot_daily_limit", reason="limit"),
            _make_decision(approved=False, bot_name="crypto", binding_constraint="bot_daily_limit", reason="limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        counts = analyzer.denial_count_by_bot()
        assert counts["weather"] == 2
        assert counts["crypto"] == 1

    def test_dedup_excluded_from_capacity_counts(self):
        decisions = [
            _make_decision(approved=False, reason="already traded by economics"),
            _make_decision(approved=False, reason="already traded by economics"),
            _make_decision(approved=False, binding_constraint="bot_daily_limit", reason="limit hit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        # Capacity denials should only count the bot_daily_limit one
        assert len(analyzer._denied) == 1
        assert len(analyzer._dedup_denied) == 2
        counts = analyzer.denial_count_by_constraint()
        assert "bot_daily_limit" in counts
        assert counts["bot_daily_limit"] == 1


# ── Foregone EV Tests ───────────────────────────────────────────────


class TestForegoneEv:
    def test_ev_by_constraint(self):
        decisions = [
            _make_decision(approved=False, edge=0.10, bot_max_cost_cents=500,
                          binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, edge=0.08, bot_max_cost_cents=300,
                          binding_constraint="city_concentration"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        ev = analyzer.foregone_ev_by_constraint()
        assert ev["bot_daily_limit"] == 50.0   # 0.10 * 500
        assert ev["city_concentration"] == 24.0  # 0.08 * 300

    def test_ev_by_bot(self):
        decisions = [
            _make_decision(approved=False, bot_name="weather", edge=0.10,
                          bot_max_cost_cents=500, binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, bot_name="crypto", edge=0.15,
                          bot_max_cost_cents=1000, binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        ev = analyzer.foregone_ev_by_bot()
        assert ev["weather"] == 50.0
        assert ev["crypto"] == 150.0

    def test_min_edge_filter(self):
        decisions = [
            _make_decision(approved=False, edge=0.01, bot_max_cost_cents=500,
                          binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, edge=0.10, bot_max_cost_cents=500,
                          binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        ev = analyzer.foregone_ev_by_constraint(min_edge=0.05)
        total = sum(ev.values())
        assert total == 50.0  # Only the 0.10 edge decision

    def test_dedup_excluded_from_foregone_ev(self):
        decisions = [
            _make_decision(approved=False, edge=0.10, bot_max_cost_cents=500,
                          reason="already traded by economics"),  # dedup
            _make_decision(approved=False, edge=0.10, bot_max_cost_cents=500,
                          binding_constraint="bot_daily_limit"),  # capacity
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        ev = analyzer.foregone_ev_by_constraint()
        # Only the capacity denial should appear
        assert sum(ev.values()) == 50.0


# ── Top Denied Opportunities Tests ──────────────────────────────────


class TestTopDenied:
    def test_top_denied_ranked_by_ev(self):
        decisions = [
            _make_decision(approved=False, edge=0.05, bot_max_cost_cents=200,
                          ticker="A", binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, edge=0.20, bot_max_cost_cents=500,
                          ticker="B", binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, edge=0.10, bot_max_cost_cents=300,
                          ticker="C", binding_constraint="city_concentration"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        top = analyzer.top_denied_opportunities(limit=3)
        assert len(top) == 3
        assert top[0]["ticker"] == "B"  # 0.20 * 500 = 100
        assert top[1]["ticker"] == "C"  # 0.10 * 300 = 30
        assert top[2]["ticker"] == "A"  # 0.05 * 200 = 10

    def test_limit_applied(self):
        decisions = [_make_decision(approved=False, edge=0.10,
                                    binding_constraint="bot_daily_limit") for _ in range(20)]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        top = analyzer.top_denied_opportunities(limit=5)
        assert len(top) == 5


# ── Approval Rate Tests ─────────────────────────────────────────────


class TestApprovalRate:
    def test_empty(self):
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=[], now_func=_now)
        rate = analyzer.approval_rate()
        assert rate["overall"] is None

    def test_mixed(self):
        decisions = [
            _make_decision(approved=True, bot_name="weather"),
            _make_decision(approved=True, bot_name="weather"),
            _make_decision(approved=False, bot_name="weather", binding_constraint="bot_daily_limit"),
            _make_decision(approved=True, bot_name="crypto"),
            _make_decision(approved=False, bot_name="crypto", binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        rate = analyzer.approval_rate()
        assert rate["overall"] == 0.6  # 3/5
        assert rate["by_bot"]["weather"] == pytest.approx(0.6667, abs=0.001)
        assert rate["by_bot"]["crypto"] == 0.5

    def test_approval_rate_missing_approved_field(self):
        """Decisions without an 'approved' key should default to True (approved)."""
        decisions = [
            # Two decisions with no 'approved' key at all — treated as approved
            {"timestamp": FIXED_NOW.isoformat(), "bot_name": "weather", "ticker": "T1",
             "edge": 0.10, "bot_max_cost_cents": 500, "reason": "", "binding_constraint": ""},
            {"timestamp": FIXED_NOW.isoformat(), "bot_name": "weather", "ticker": "T2",
             "edge": 0.08, "bot_max_cost_cents": 300, "reason": "", "binding_constraint": ""},
            # One explicit denial
            _make_decision(approved=False, bot_name="weather", binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        rate = analyzer.approval_rate()
        # 2 approved (missing field defaults True) + 1 denied = 2/3 approved
        assert rate["overall"] == pytest.approx(0.6667, abs=0.001)
        # The missing-field decisions should NOT appear in denied lists
        assert len(analyzer._denied) == 1
        assert len(analyzer._all_denied) == 1


# ── Reallocation Candidates Tests ───────────────────────────────────


class TestReallocationCandidates:
    def test_capacity_constraint_creates_candidate(self):
        decisions = [
            _make_decision(approved=False, bot_name="weather", edge=0.10,
                          bot_max_cost_cents=500, binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, bot_name="weather", edge=0.12,
                          bot_max_cost_cents=400, binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, bot_name="weather", edge=0.08,
                          bot_max_cost_cents=300, binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        candidates = analyzer.reallocation_candidates(min_foregone_ev_cents=50)
        assert len(candidates) == 1
        assert candidates[0]["bot"] == "weather"
        assert candidates[0]["dominant_constraint"] == "bot_daily_limit"

    def test_low_ev_no_candidate(self):
        decisions = [
            _make_decision(approved=False, edge=0.01, bot_max_cost_cents=100,
                          binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        candidates = analyzer.reallocation_candidates(min_foregone_ev_cents=200)
        assert len(candidates) == 0

    def test_dedup_denials_dont_create_candidates(self):
        """Dedup denials are excluded, so even with high EV they don't surface."""
        decisions = [
            _make_decision(approved=False, bot_name="weather", edge=0.20,
                          bot_max_cost_cents=2000, reason="already traded by economics"),
            _make_decision(approved=False, bot_name="weather", edge=0.20,
                          bot_max_cost_cents=2000, reason="already traded by economics"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        candidates = analyzer.reallocation_candidates(min_foregone_ev_cents=50)
        assert len(candidates) == 0


# ── Full Report Tests ───────────────────────────────────────────────


class TestFullReport:
    def test_report_structure(self):
        decisions = [
            _make_decision(approved=True),
            _make_decision(approved=False, edge=0.10, binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        report = analyzer.full_report()

        assert "summary" in report
        assert "denial_count_by_constraint" in report
        assert "foregone_ev_by_constraint" in report
        assert "denial_count_by_bot" in report
        assert "foregone_ev_by_bot" in report
        assert "top_denied_opportunities" in report
        assert "approval_rate" in report
        assert "top_binding_constraint" in report
        assert "reallocation_candidates" in report
        assert "dedup" in report
        assert "opportunity_log" in report

        assert report["summary"]["total_decisions"] == 2
        assert report["summary"]["total_denials_capacity"] == 1
        assert report["summary"]["total_denials_dedup"] == 0

    def test_empty_report(self):
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=[], now_func=_now)

        report = analyzer.full_report()
        assert report["summary"]["total_decisions"] == 0
        assert report["summary"]["total_denials_capacity"] == 0
        assert report["top_binding_constraint"] is None

    def test_mixed_dedup_and_capacity(self):
        decisions = [
            _make_decision(approved=False, reason="already traded by economics"),
            _make_decision(approved=False, reason="already traded by economics"),
            _make_decision(approved=False, reason="already traded by economics"),
            _make_decision(approved=False, binding_constraint="bot_daily_limit", edge=0.10),
            _make_decision(approved=True),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        report = analyzer.full_report()
        assert report["summary"]["total_denials_all"] == 4
        assert report["summary"]["total_denials_capacity"] == 1
        assert report["summary"]["total_denials_dedup"] == 3
        assert report["dedup"]["count"] == 3
        assert report["top_binding_constraint"] == "bot_daily_limit"


# ── Lookback Window Tests ───────────────────────────────────────────


class TestLookbackWindow:
    def test_old_decisions_filtered_out(self):
        old_time = (FIXED_NOW - datetime.timedelta(days=3)).isoformat()
        recent_time = (FIXED_NOW - datetime.timedelta(hours=6)).isoformat()

        decisions = [
            _make_decision(approved=False, timestamp=old_time, binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, timestamp=recent_time, binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, lookback_days=1, now_func=_now)

        assert len(analyzer._denied) == 1

    def test_wider_lookback_includes_more(self):
        old_time = (FIXED_NOW - datetime.timedelta(days=3)).isoformat()
        recent_time = (FIXED_NOW - datetime.timedelta(hours=6)).isoformat()

        decisions = [
            _make_decision(approved=False, timestamp=old_time, binding_constraint="bot_daily_limit"),
            _make_decision(approved=False, timestamp=recent_time, binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, lookback_days=7, now_func=_now)

        assert len(analyzer._denied) == 2

    def test_null_timestamp_excluded(self):
        """Decisions without a parseable timestamp should be dropped, not included."""
        recent_time = (FIXED_NOW - datetime.timedelta(hours=6)).isoformat()

        decisions = [
            _make_decision(approved=False, timestamp=recent_time, binding_constraint="bot_daily_limit"),
            # No timestamp at all
            {"bot_name": "weather", "ticker": "T1", "edge": 0.10, "approved": False,
             "bot_max_cost_cents": 500, "reason": "limit", "binding_constraint": "bot_daily_limit"},
            # Invalid timestamp
            {"timestamp": "not-a-date", "bot_name": "crypto", "ticker": "T2", "edge": 0.10,
             "approved": False, "bot_max_cost_cents": 500, "reason": "limit",
             "binding_constraint": "bot_daily_limit"},
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, lookback_days=1, now_func=_now)

        # Only the decision with a valid recent timestamp should survive
        assert len(analyzer._decisions) == 1
        assert len(analyzer._denied) == 1

    def test_null_timestamp_opportunity_records_excluded(self):
        """Opportunity records without parseable timestamps should also be dropped."""
        recent_time = (FIXED_NOW - datetime.timedelta(hours=6)).isoformat()

        opp_records = [
            {"timestamp": recent_time, "action": "skipped", "reason": "bracket_illiquid",
             "edge": 0.10, "price_cents": 30},
            # No timestamp
            {"action": "skipped", "reason": "bracket_illiquid",
             "edge": 0.05, "price_cents": 20},
            # Invalid timestamp
            {"timestamp": "garbage", "action": "skipped", "reason": "bracket_illiquid",
             "edge": 0.05, "price_cents": 20},
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=[], opportunity_records=opp_records,
                                        lookback_days=1, now_func=_now)

        # Only the record with a valid recent timestamp should survive
        assert len(analyzer._opportunity_records) == 1


# ── Opportunity Log Integration Tests ──────────────────────────────


class TestOpportunityLogIntegration:
    def test_opportunity_log_summary_empty(self):
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=[], opportunity_records=[], now_func=_now)
        summary = analyzer.opportunity_log_summary()
        assert summary["total_records"] == 0
        assert summary["reason_counts"] == {}

    def test_opportunity_log_summary_counts(self):
        opp_records = [
            {"timestamp": FIXED_NOW.isoformat(), "action": "skipped", "reason": "bracket_illiquid",
             "edge": 0.10, "price_cents": 30},
            {"timestamp": FIXED_NOW.isoformat(), "action": "skipped", "reason": "bracket_illiquid",
             "edge": 0.05, "price_cents": 20},
            {"timestamp": FIXED_NOW.isoformat(), "action": "rejected", "reason": "edge_below_threshold",
             "edge": 0.01, "price_cents": 50},
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=[], opportunity_records=opp_records, now_func=_now)
        summary = analyzer.opportunity_log_summary()
        assert summary["reason_counts"]["bracket_illiquid"] == 2
        assert summary["reason_counts"]["edge_below_threshold"] == 1

    def test_full_report_includes_opportunity_log(self):
        opp_records = [
            {"timestamp": FIXED_NOW.isoformat(), "action": "skipped", "reason": "bracket_illiquid",
             "edge": 0.10, "price_cents": 30},
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=[], opportunity_records=opp_records, now_func=_now)
        report = analyzer.full_report()
        assert "opportunity_log" in report
        assert report["opportunity_log"]["reason_counts"]["bracket_illiquid"] == 1


# ── Summary Text Tests ──────────────────────────────────────────────


class TestSummaryText:
    def test_summary_text_runs(self):
        decisions = [
            _make_decision(approved=True),
            _make_decision(approved=False, edge=0.10, binding_constraint="bot_daily_limit"),
        ]
        analyzer = ConstraintAnalyzer()
        analyzer.load_budget_decisions(decisions=decisions, now_func=_now)

        text = analyzer.summary_text()
        assert "ALLOCATOR CONSTRAINT ANALYSIS" in text
        assert "bot_daily_limit" in text
        assert "Dedup denials" in text
