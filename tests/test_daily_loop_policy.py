"""Tests for the daily loop policy engine."""

import datetime
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from ops.daily_loop_policy import (
    ACTION_CLASS_AUTO,
    ACTION_CLASS_PROPOSE,
    DailyLoopPolicy,
    HealthVerdict,
    ImprovementOpportunity,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
TEST_CONFIG_PATH = PROJECT_DIR / "config" / "daily-loop-policy.json"

FIXED_NOW = datetime.datetime(2026, 3, 17, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_policy(now=None, config_path=None):
    return DailyLoopPolicy(
        config_path=config_path or TEST_CONFIG_PATH,
        now_func=lambda: now or FIXED_NOW,
    )


# ── Health Gate Tests ───────────────────────────────────────────────


class TestHealthGateParity:
    def test_parity_ok(self):
        policy = _make_policy()
        verdict = policy.evaluate_health(parity_report={"overall_ok": True})
        assert verdict.parity["status"] == SEVERITY_OK
        assert verdict.overall_status == SEVERITY_OK

    def test_parity_failure_is_critical(self):
        policy = _make_policy()
        verdict = policy.evaluate_health(parity_report={"overall_ok": False})
        assert verdict.parity["status"] == SEVERITY_CRITICAL
        assert verdict.overall_status == SEVERITY_CRITICAL
        assert len(verdict.incident_candidates) == 1
        assert verdict.incident_candidates[0]["type"] == "parity_regression"

    def test_missing_parity_report_defaults_to_critical(self):
        policy = _make_policy()
        verdict = policy.evaluate_health(parity_report=None)
        assert verdict.parity["status"] == SEVERITY_CRITICAL
        assert verdict.overall_status == SEVERITY_CRITICAL
        assert any(ic["type"] == "parity_data_missing" for ic in verdict.incident_candidates)

    def test_empty_parity_report_defaults_to_critical(self):
        policy = _make_policy()
        verdict = policy.evaluate_health(parity_report={})
        assert verdict.parity["status"] == SEVERITY_CRITICAL
        assert verdict.overall_status == SEVERITY_CRITICAL
        assert any(ic["type"] == "parity_data_missing" for ic in verdict.incident_candidates)


class TestHealthGateSources:
    def test_sources_all_ok(self):
        policy = _make_policy()
        sources = {"sources": {
            "open-meteo-batch": {"status": "ok"},
            "coinbase": {"status": "ok"},
        }}
        verdict = policy.evaluate_health(source_summary=sources)
        assert verdict.sources["status"] == SEVERITY_OK

    def test_source_error_is_warning(self):
        policy = _make_policy()
        sources = {"sources": {
            "open-meteo-batch": {"status": "error", "error_count": 10},
        }}
        verdict = policy.evaluate_health(
            parity_report={"overall_ok": True},
            source_summary=sources,
        )
        assert verdict.sources["status"] == SEVERITY_WARNING
        assert "open-meteo-batch" in verdict.sources["details"]["error_sources"]
        assert len(verdict.incident_candidates) == 1

    def test_source_warning_is_warning(self):
        policy = _make_policy()
        sources = {"sources": {
            "nws": {"status": "warning", "error_count": 2},
        }}
        verdict = policy.evaluate_health(source_summary=sources)
        assert verdict.sources["status"] == SEVERITY_WARNING
        assert "nws" in verdict.sources["details"]["warning_sources"]

    def test_source_error_populates_degraded_strategies(self):
        """H2: error sources should appear in degraded_strategies."""
        policy = _make_policy()
        sources = {"sources": {
            "open-meteo-batch": {"status": "error", "error_count": 10},
            "coinbase": {"status": "ok"},
        }}
        verdict = policy.evaluate_health(source_summary=sources)
        assert "open-meteo-batch" in verdict.degraded_strategies
        assert "coinbase" not in verdict.degraded_strategies

    def test_degraded_strategies_in_to_dict(self):
        """H2: degraded_strategies should appear in HealthVerdict.to_dict()."""
        policy = _make_policy()
        sources = {"sources": {
            "nws": {"status": "error"},
            "hdd": {"status": "error"},
        }}
        verdict = policy.evaluate_health(source_summary=sources)
        d = verdict.to_dict()
        assert "degraded_strategies" in d
        assert "nws" in d["degraded_strategies"]
        assert "hdd" in d["degraded_strategies"]

    def test_no_degraded_strategies_when_all_ok(self):
        """H2: no degraded_strategies when all sources are ok."""
        policy = _make_policy()
        sources = {"sources": {"coinbase": {"status": "ok"}}}
        verdict = policy.evaluate_health(source_summary=sources)
        assert verdict.degraded_strategies == []


class TestHealthGateProcesses:
    def test_processes_all_ok(self):
        policy = _make_policy()
        processes = {"bots": {
            "weather": {"status": "ok", "last_heartbeat": FIXED_NOW.isoformat()},
        }}
        verdict = policy.evaluate_health(process_summary=processes)
        assert verdict.processes["status"] == SEVERITY_OK

    def test_stale_bot_is_warning(self):
        policy = _make_policy()
        processes = {"bots": {
            "weather": {"status": "stale", "last_heartbeat": "2026-03-17T08:00:00+00:00"},
        }}
        verdict = policy.evaluate_health(process_summary=processes)
        assert verdict.processes["status"] == SEVERITY_WARNING
        assert "weather" in verdict.processes["details"]["stale_bots"]

    def test_stale_bot_triggers_auto_restart(self):
        policy = _make_policy()
        processes = {"bots": {
            "crypto": {"status": "stale", "restart_count_today": 0},
        }}
        verdict = policy.evaluate_health(process_summary=processes)
        auto_restarts = [a for a in verdict.auto_actions if a["type"] == "restart_bot"]
        assert len(auto_restarts) == 1
        assert auto_restarts[0]["target"] == "crypto"

    def test_crash_loop_is_critical(self):
        policy = _make_policy()
        processes = {"bots": {
            "economics": {"status": "crash-looping"},
        }}
        verdict = policy.evaluate_health(process_summary=processes)
        assert verdict.processes["status"] == SEVERITY_CRITICAL
        assert "economics" in verdict.processes["details"]["crash_loop_bots"]
        assert any(ic["type"] == "crash_loop" for ic in verdict.incident_candidates)

    def test_non_dict_bot_data_treated_as_stale(self):
        """M5: non-dict bot data should be classified as unknown/stale, not ok."""
        policy = _make_policy()
        processes = {"bots": {
            "broken": "not-a-dict",
            "also_broken": 42,
        }}
        verdict = policy.evaluate_health(process_summary=processes)
        assert verdict.processes["status"] == SEVERITY_WARNING
        assert "broken" in verdict.processes["details"]["stale_bots"]
        assert "also_broken" in verdict.processes["details"]["stale_bots"]
        # Should attempt auto-restart since restart_count defaults to 0
        auto_restarts = [a for a in verdict.auto_actions if a["type"] == "restart_bot"]
        restart_targets = {a["target"] for a in auto_restarts}
        assert "broken" in restart_targets
        assert "also_broken" in restart_targets

    def test_multiple_stale_bots_restart_limits_per_bot(self):
        """Regression: restart_count_today must be read from each bot's data,
        not from a leaked loop variable."""
        policy = _make_policy()
        processes = {"bots": {
            "weather": {"status": "stale", "restart_count_today": 1},  # at limit
            "crypto": {"status": "stale", "restart_count_today": 0},   # under limit
        }}
        verdict = policy.evaluate_health(process_summary=processes)
        # weather should be an incident (at restart limit), crypto should get restart
        auto_restarts = [a for a in verdict.auto_actions if a["type"] == "restart_bot"]
        incidents = [ic for ic in verdict.incident_candidates if ic["type"] == "stale_bot_repeated"]
        restart_targets = {a["target"] for a in auto_restarts}
        incident_bots = {ic["details"]["bot"] for ic in incidents}
        assert "crypto" in restart_targets
        assert "weather" in incident_bots
        assert "weather" not in restart_targets


class TestHealthGateSnapshot:
    def test_snapshot_ok(self):
        policy = _make_policy()
        verdict = policy.evaluate_health(snapshot_result={"success": True})
        assert verdict.snapshot["status"] == SEVERITY_OK

    def test_snapshot_failure_is_critical(self):
        policy = _make_policy()
        verdict = policy.evaluate_health(snapshot_result={"success": False, "error": "API timeout"})
        assert verdict.snapshot["status"] == SEVERITY_CRITICAL
        assert verdict.overall_status == SEVERITY_CRITICAL
        assert any(ic["type"] == "snapshot_failure" for ic in verdict.incident_candidates)


class TestOverallStatus:
    def test_worst_wins(self):
        policy = _make_policy()
        verdict = policy.evaluate_health(
            parity_report={"overall_ok": True},
            source_summary={"sources": {"nws": {"status": "error"}}},
            process_summary={"bots": {"weather": {"status": "crash-looping"}}},
            snapshot_result={"success": True},
        )
        assert verdict.overall_status == SEVERITY_CRITICAL


# ── Improvement Rules Tests ─────────────────────────────────────────


class TestImprovementRules:
    def test_no_opportunities_when_everything_ok(self):
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            attribution={"by_bot": {}},
            missed_trade_report={"top_missed_trades": [], "reason_distribution": {}},
            execution_quality={"by_bot": {}},
            constraint_analysis={
                "summary": {"total_foregone_ev_cents": 0},
                "denial_count_by_constraint": {},
            },
        )
        assert len(opps) == 0

    def test_high_missed_ev_from_allocator(self):
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            missed_trade_report={
                "top_missed_trades": [
                    {"estimated_pnl_per_contract_cents": 300},
                    {"estimated_pnl_per_contract_cents": 250},
                ],
                "reason_distribution": {
                    "weather": {"daily allocation exhausted": 5, "edge below threshold": 1},
                },
            },
            constraint_analysis={
                "summary": {"total_foregone_ev_cents": 600},
                "denial_count_by_constraint": {"bot_daily_limit": 5},
                "top_binding_constraint": "bot_daily_limit",
                "top_constraint_foregone_ev_cents": 600,
                "reallocation_candidates": [{"bot": "weather", "foregone_ev_cents": 600}],
            },
        )
        assert len(opps) >= 1
        cap_opps = [o for o in opps if o.category == "capital_reallocation"]
        assert len(cap_opps) >= 1

    def test_negative_pnl_triggers_model_review(self):
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            attribution={"by_bot": {
                "strategy": {"pnl_cents": -500, "trades": 10, "win_rate": 0.2},
            }},
        )
        model_opps = [o for o in opps if o.category == "model_review"]
        assert len(model_opps) == 1
        assert "strategy" in model_opps[0].affected_bots

    def test_insufficient_trades_no_model_review(self):
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            attribution={"by_bot": {
                "strategy": {"pnl_cents": -500, "trades": 2, "win_rate": 0.0},
            }},
        )
        model_opps = [o for o in opps if o.category == "model_review"]
        assert len(model_opps) == 0

    def test_high_slippage_triggers_execution_review(self):
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            execution_quality={"by_bot": {
                "crypto": {"trades": 15, "avg_slippage_cents": 3.5, "fill_rate": 0.9},
            }},
        )
        exec_opps = [o for o in opps if o.category == "execution_policy"]
        assert len(exec_opps) == 1
        assert "crypto" in exec_opps[0].affected_bots

    def test_poor_fill_rate_triggers_execution_review(self):
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            execution_quality={"by_bot": {
                "weather": {"trades": 20, "avg_slippage_cents": 0.5, "fill_rate": 0.3},
            }},
        )
        exec_opps = [o for o in opps if o.category == "execution_policy"]
        assert len(exec_opps) == 1

    def test_opportunities_sorted_by_impact(self):
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            attribution={"by_bot": {
                "strategy": {"pnl_cents": -100, "trades": 10, "win_rate": 0.3},
                "crypto": {"pnl_cents": -500, "trades": 20, "win_rate": 0.2},
            }},
        )
        if len(opps) >= 2:
            assert opps[0].estimated_impact_cents >= opps[1].estimated_impact_cents

    def test_weak_pnl_acceptable_edge_bad_execution_recommends_execution_not_model(self):
        """Spec line 213: if P&L weak + edge acceptable + execution degrading
        → execution recommendation INSTEAD OF model recommendation."""
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            attribution={"by_bot": {
                "crypto": {"pnl_cents": -300, "trades": 20, "win_rate": 0.55},
            }},
            execution_quality={"by_bot": {
                "crypto": {"trades": 20, "avg_slippage_cents": 4.0, "fill_rate": 0.4},
            }},
        )
        # Should get execution_policy, NOT model_review for crypto
        exec_opps = [o for o in opps if o.category == "execution_policy" and "crypto" in o.affected_bots]
        model_opps = [o for o in opps if o.category == "model_review" and "crypto" in o.affected_bots]
        assert len(exec_opps) >= 1
        assert len(model_opps) == 0
        assert exec_opps[0].evidence.get("diagnosis") == "execution_not_model"

    def test_weak_pnl_bad_edge_no_execution_recommends_model(self):
        """Low win rate = bad edge → should get model_review, not execution."""
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            attribution={"by_bot": {
                "strategy": {"pnl_cents": -500, "trades": 10, "win_rate": 0.15},
            }},
            execution_quality={"by_bot": {
                "strategy": {"trades": 10, "avg_slippage_cents": 0.5, "fill_rate": 0.9},
            }},
        )
        model_opps = [o for o in opps if o.category == "model_review" and "strategy" in o.affected_bots]
        assert len(model_opps) == 1

    def test_no_duplicate_execution_policy_for_same_bot(self):
        """A bot flagged by edge+execution check should NOT get a second
        execution_policy from the standalone execution quality check."""
        policy = _make_policy()
        opps = policy.evaluate_improvements(
            attribution={"by_bot": {
                "crypto": {"pnl_cents": -300, "trades": 20, "win_rate": 0.55},
            }},
            execution_quality={"by_bot": {
                "crypto": {"trades": 20, "avg_slippage_cents": 4.0, "fill_rate": 0.4},
            }},
        )
        exec_opps = [o for o in opps if o.category == "execution_policy" and "crypto" in o.affected_bots]
        # Should be exactly 1, not 2 (edge+exec check only, no duplicate from standalone)
        assert len(exec_opps) == 1


# ── Promotion Blocker Tests ─────────────────────────────────────────


class TestPromotionBlockers:
    def test_no_blockers_when_clean(self):
        # Set now to after parity window
        post_phase4 = datetime.datetime(2026, 4, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        policy = _make_policy(now=post_phase4)
        blockers = policy.check_promotion_blockers("exp-001", open_incidents=[])
        assert blockers == []

    def test_phase4_blocks_promotion(self):
        policy = _make_policy()
        blockers = policy.check_promotion_blockers("exp-001", open_incidents=[])
        assert any("Phase 4" in b for b in blockers)

    def test_open_parity_incident_blocks(self):
        post_phase4 = datetime.datetime(2026, 4, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        policy = _make_policy(now=post_phase4)
        blockers = policy.check_promotion_blockers(
            "exp-001",
            open_incidents=[{"id": "INC-001", "type": "parity_regression"}],
        )
        assert any("parity" in b.lower() for b in blockers)

    def test_open_source_incident_blocks(self):
        post_phase4 = datetime.datetime(2026, 4, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        policy = _make_policy(now=post_phase4)
        blockers = policy.check_promotion_blockers(
            "exp-001",
            open_incidents=[{"id": "INC-002", "type": "source_failure"}],
        )
        assert any("source" in b.lower() for b in blockers)

    def test_snapshot_failure_incident_blocks(self):
        post_phase4 = datetime.datetime(2026, 4, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        policy = _make_policy(now=post_phase4)
        blockers = policy.check_promotion_blockers(
            "exp-001",
            open_incidents=[{"id": "INC-003", "type": "snapshot_failure"}],
        )
        assert any("snapshot" in b.lower() for b in blockers)


# ── Action Classification Tests ─────────────────────────────────────


class TestActionClassification:
    def test_auto_restart_stays_auto(self):
        policy = _make_policy()
        verdict = HealthVerdict()
        verdict.auto_actions = [{"type": "restart_bot", "target": "weather"}]
        actions = policy.classify_actions(verdict, [])
        assert len(actions["auto_executed"]) == 1
        assert actions["auto_executed"][0]["type"] == "restart_bot"

    def test_phase4_downgrades_config_writes(self):
        policy = _make_policy()
        verdict = HealthVerdict()
        verdict.auto_actions = [{"type": "apply_calibration", "target": "weather"}]
        actions = policy.classify_actions(verdict, [])
        # Should be downgraded to operator_required
        assert len(actions["auto_executed"]) == 0
        assert len(actions["operator_required"]) == 1
        assert actions["operator_required"][0].get("downgraded_from") == "auto"

    def test_improvements_become_operator_required(self):
        policy = _make_policy()
        verdict = HealthVerdict()
        opps = [ImprovementOpportunity(
            category="model_review",
            severity=SEVERITY_WARNING,
            description="Test",
            action_class=ACTION_CLASS_PROPOSE,
        )]
        actions = policy.classify_actions(verdict, opps)
        assert len(actions["operator_required"]) == 1

    def test_incident_candidates_propagated(self):
        policy = _make_policy()
        verdict = HealthVerdict()
        verdict.incident_candidates = [{"type": "test", "summary": "test incident"}]
        actions = policy.classify_actions(verdict, [])
        assert len(actions["incident_candidates"]) == 1


# ── Follow-Through Tests ────────────────────────────────────────────


class TestFollowThrough:
    def test_no_stale_when_no_previous(self):
        policy = _make_policy()
        ft = policy.evaluate_follow_through(previous_loop=None)
        assert ft["stale_recommendations"] == []

    def test_stale_recommendation_detected(self):
        old_time = (FIXED_NOW - datetime.timedelta(days=10)).isoformat()
        policy = _make_policy()
        ft = policy.evaluate_follow_through(
            previous_loop={
                "generated_at": old_time,
                "actions": {
                    "operator_required": [
                        {"description": "Review model", "created_at": old_time},
                    ],
                },
            },
        )
        assert len(ft["stale_recommendations"]) == 1

    def test_actioned_recommendation_not_stale(self):
        old_time = (FIXED_NOW - datetime.timedelta(days=10)).isoformat()
        policy = _make_policy()
        ft = policy.evaluate_follow_through(
            previous_loop={
                "generated_at": old_time,
                "actions": {
                    "operator_required": [
                        {"description": "Review model", "created_at": old_time, "disposition": "accepted"},
                    ],
                },
            },
        )
        assert len(ft["stale_recommendations"]) == 0

    def test_stale_incident_detected(self):
        old_time = (FIXED_NOW - datetime.timedelta(days=20)).isoformat()
        policy = _make_policy()
        ft = policy.evaluate_follow_through(
            open_incidents=[{"id": "INC-001", "opened_at": old_time, "status": "open"}],
        )
        assert len(ft["stale_incidents"]) == 1


# ── Phase 4 Observation Tests ──────────────────────────────────────


class TestPhase4Observation:
    def test_active_during_window(self):
        policy = _make_policy()
        assert policy.phase4_observation_active is True

    def test_inactive_after_window(self):
        post_phase4 = datetime.datetime(2026, 4, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        policy = _make_policy(now=post_phase4)
        assert policy.phase4_observation_active is False
