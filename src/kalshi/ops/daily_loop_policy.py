"""Daily loop policy engine — classifies signals and recommends actions.

Pure-function policy module. No CLI logic, no subprocess calls, no side effects.
Takes artifact data as input, returns classifications and action recommendations.

Usage:
    from ops.daily_loop_policy import DailyLoopPolicy

    policy = DailyLoopPolicy()  # loads config/daily-loop-policy.json
    health = policy.evaluate_health(parity, sources, processes, snapshot)
    improvements = policy.evaluate_improvements(attribution, missed, execution, allocator)
    actions = policy.classify_actions(health, improvements)
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "daily-loop-policy.json"

_log = logging.getLogger("daily-loop-policy")

# Action classes from the improvement loop doc
ACTION_CLASS_AUTO = "auto"          # Class A: safe to auto-execute
ACTION_CLASS_PROPOSE = "propose"    # Class B: auto-propose, human-approve
ACTION_CLASS_MANUAL = "manual"      # Class C: explicitly manual

# Severity levels
SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def _load_config(path=None):
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        return json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        _log.warning("Failed to load policy config from %s, using defaults", config_path)
        return {}


class HealthVerdict:
    """Result of health gate evaluation."""

    __slots__ = ("overall_status", "parity", "sources", "processes", "snapshot",
                 "incident_candidates", "auto_actions", "degraded_strategies")

    def __init__(self):
        self.overall_status = SEVERITY_OK
        self.parity = {"status": SEVERITY_OK, "details": {}}
        self.sources = {"status": SEVERITY_OK, "details": {}}
        self.processes = {"status": SEVERITY_OK, "details": {}}
        self.snapshot = {"status": SEVERITY_OK, "details": {}}
        self.incident_candidates = []
        self.auto_actions = []
        self.degraded_strategies = []

    def to_dict(self):
        return {
            "overall_status": self.overall_status,
            "parity": self.parity,
            "sources": self.sources,
            "processes": self.processes,
            "snapshot": self.snapshot,
            "incident_candidates": self.incident_candidates,
            "auto_actions": self.auto_actions,
            "degraded_strategies": self.degraded_strategies,
        }


class ImprovementOpportunity:
    """A diagnosed improvement opportunity with evidence."""

    __slots__ = ("category", "severity", "description", "evidence", "recommended_action",
                 "action_class", "affected_bots", "estimated_impact_cents")

    def __init__(self, category, severity, description, evidence=None,
                 recommended_action="", action_class=ACTION_CLASS_PROPOSE,
                 affected_bots=None, estimated_impact_cents=0):
        self.category = category
        self.severity = severity
        self.description = description
        self.evidence = evidence or {}
        self.recommended_action = recommended_action
        self.action_class = action_class
        self.affected_bots = affected_bots or []
        self.estimated_impact_cents = estimated_impact_cents

    def to_dict(self):
        return {
            "category": self.category,
            "severity": self.severity,
            "description": self.description,
            "evidence": self.evidence,
            "recommended_action": self.recommended_action,
            "action_class": self.action_class,
            "affected_bots": self.affected_bots,
            "estimated_impact_cents": self.estimated_impact_cents,
        }


class DailyLoopPolicy:
    """Policy engine for daily operations loop.

    Evaluates health signals, diagnoses improvement opportunities,
    and classifies actions into auto/propose/manual.
    """

    def __init__(self, config_path=None, now_func=None):
        self._config = _load_config(config_path)
        self._now = now_func or _utc_now
        self._health_cfg = self._config.get("health_gate", {})
        self._improvement_cfg = self._config.get("improvement", {})
        self._promotion_cfg = self._config.get("promotion", {})
        self._constraint_cfg = self._config.get("constraint_analysis", {})
        self._phase4_cfg = self._config.get("phase4_observation", {})
        self._follow_cfg = self._config.get("follow_through", {})

    @property
    def phase4_observation_active(self):
        if not self._phase4_cfg.get("enabled", True):
            return False
        deadline = _parse_iso(self._phase4_cfg.get("parity_required_until"))
        if deadline is None:
            return True
        return self._now() <= deadline

    # ── Health Gate (Stage 1) ───────────────────────────────────────

    def evaluate_health(self, parity_report=None, source_summary=None,
                        process_summary=None, snapshot_result=None):
        """Evaluate health signals and produce a HealthVerdict.

        Args:
            parity_report: dict from ledger-parity-report.py --json
            source_summary: dict from source-scorecard.py --json or health_monitor.get_summary()
            process_summary: dict from supervisor status (bots section)
            snapshot_result: dict with 'success' bool and optional 'error'
        """
        verdict = HealthVerdict()

        self._evaluate_parity(verdict, parity_report or {})
        self._evaluate_sources(verdict, source_summary or {})
        self._evaluate_processes(verdict, process_summary or {})
        self._evaluate_snapshot(verdict, snapshot_result or {})

        # Overall status is the worst of all subsystems
        statuses = [
            verdict.parity["status"],
            verdict.sources["status"],
            verdict.processes["status"],
            verdict.snapshot["status"],
        ]
        if SEVERITY_CRITICAL in statuses:
            verdict.overall_status = SEVERITY_CRITICAL
        elif SEVERITY_WARNING in statuses:
            verdict.overall_status = SEVERITY_WARNING

        return verdict

    def _evaluate_parity(self, verdict, parity_report):
        overall_ok = parity_report.get("overall_ok")
        if overall_ok is None:
            verdict.parity = {
                "status": SEVERITY_CRITICAL,
                "details": {"error": "parity report missing or incomplete"},
            }
            verdict.incident_candidates.append({
                "type": "parity_data_missing",
                "summary": "Parity report unavailable — cannot verify ledger integrity",
                "severity": "critical",
            })
            return
        if not overall_ok:
            severity = self._health_cfg.get("parity_failure_severity", SEVERITY_CRITICAL)
            verdict.parity = {
                "status": severity,
                "details": {
                    "overall_ok": False,
                    "mismatches": parity_report.get("mismatches", []),
                },
            }
            verdict.incident_candidates.append({
                "type": "parity_regression",
                "severity": severity,
                "summary": "Ledger parity check failed",
                "details": parity_report,
            })
        else:
            verdict.parity = {
                "status": SEVERITY_OK,
                "details": {"overall_ok": True},
            }

    def _evaluate_sources(self, verdict, source_summary):
        sources = source_summary.get("sources", {})
        error_sources = []
        warning_sources = []

        for source_name, source_data in sources.items():
            status = source_data.get("status", "ok")
            if status == "error":
                error_sources.append(source_name)
            elif status == "warning":
                warning_sources.append(source_name)

        if error_sources:
            verdict.sources = {
                "status": self._health_cfg.get("source_error_severity", SEVERITY_WARNING),
                "details": {
                    "error_sources": error_sources,
                    "warning_sources": warning_sources,
                },
            }
            for source in error_sources:
                verdict.incident_candidates.append({
                    "type": "source_failure",
                    "severity": SEVERITY_WARNING,
                    "summary": f"Source {source} in error state",
                    "details": sources.get(source, {}),
                })
                verdict.degraded_strategies.append(source)
        elif warning_sources:
            verdict.sources = {
                "status": SEVERITY_WARNING,
                "details": {
                    "error_sources": [],
                    "warning_sources": warning_sources,
                },
            }
        else:
            verdict.sources = {
                "status": SEVERITY_OK,
                "details": {"error_sources": [], "warning_sources": []},
            }

    def _evaluate_processes(self, verdict, process_summary):
        bots = process_summary.get("bots", {})
        stale_minutes = self._health_cfg.get("bot_stale_minutes", 120)
        max_restarts = self._health_cfg.get("max_auto_restarts_per_bot_per_day", 1)
        now = self._now()

        stale_bots = []
        crash_loop_bots = []

        for bot_name, bot_data in bots.items():
            status = bot_data.get("status", "ok") if isinstance(bot_data, dict) else "unknown"
            if status == "unknown":
                stale_bots.append(bot_name)
            elif status == "stale":
                stale_bots.append(bot_name)
            elif status in ("crash-looping", "crash_looping"):
                crash_loop_bots.append(bot_name)
            else:
                # Check heartbeat staleness directly
                if isinstance(bot_data, dict):
                    last_hb = _parse_iso(bot_data.get("last_heartbeat"))
                    if last_hb is not None:
                        age_minutes = (now - last_hb).total_seconds() / 60
                        if age_minutes > stale_minutes:
                            stale_bots.append(bot_name)

        # Auto-restart stale bots (Class A, limited to max_restarts per day)
        for bot in stale_bots:
            # Look up this specific bot's data (not the leaked loop variable)
            this_bot_data = bots.get(bot, {})
            if not isinstance(this_bot_data, dict):
                this_bot_data = {}
            restart_count = this_bot_data.get("restart_count_today", 0)
            if restart_count < max_restarts:
                verdict.auto_actions.append({
                    "type": "restart_bot",
                    "action_class": ACTION_CLASS_AUTO,
                    "target": bot,
                    "reason": f"Bot {bot} heartbeat stale",
                })
            else:
                verdict.incident_candidates.append({
                    "type": "stale_bot_repeated",
                    "severity": SEVERITY_WARNING,
                    "summary": f"Bot {bot} stale after {max_restarts} restart(s) today",
                    "details": {"bot": bot},
                })

        for bot in crash_loop_bots:
            verdict.incident_candidates.append({
                "type": "crash_loop",
                "severity": SEVERITY_CRITICAL,
                "summary": f"Bot {bot} is crash-looping",
                "details": {"bot": bot},
            })

        if crash_loop_bots:
            verdict.processes["status"] = SEVERITY_CRITICAL
        elif stale_bots:
            verdict.processes["status"] = SEVERITY_WARNING
        else:
            verdict.processes["status"] = SEVERITY_OK

        verdict.processes["details"] = {
            "stale_bots": stale_bots,
            "crash_loop_bots": crash_loop_bots,
        }

    def _evaluate_snapshot(self, verdict, snapshot_result):
        success = snapshot_result.get("success", True)
        if not success:
            severity = self._health_cfg.get("snapshot_failure_severity", SEVERITY_CRITICAL)
            verdict.snapshot = {
                "status": severity,
                "details": {
                    "success": False,
                    "error": snapshot_result.get("error", "unknown"),
                },
            }
            verdict.incident_candidates.append({
                "type": "snapshot_failure",
                "severity": severity,
                "summary": "Financial snapshot could not be produced",
                "details": snapshot_result,
            })
        else:
            verdict.snapshot = {
                "status": SEVERITY_OK,
                "details": {"success": True},
            }

    # ── Improvement Rules (Stage 3) ─────────────────────────────────

    def evaluate_improvements(self, attribution=None, missed_trade_report=None,
                              execution_quality=None, constraint_analysis=None):
        """Diagnose improvement opportunities from daily signals.

        Implements the conditional rule from the spec: if a bot has weak P&L
        but acceptable edge AND deteriorating execution, recommend execution
        review INSTEAD OF model review.

        Returns a list of ImprovementOpportunity objects.
        """
        opportunities = []

        self._check_missed_trade_ev(opportunities, missed_trade_report or {}, constraint_analysis or {})
        exec_flagged = self._check_realized_edge_with_execution(
            opportunities,
            attribution or {},
            execution_quality or {},
        )
        self._check_execution_quality(opportunities, execution_quality or {}, skip_bots=exec_flagged)
        self._check_allocator_constraints(opportunities, constraint_analysis or {})

        # Sort by estimated impact descending
        opportunities.sort(key=lambda o: o.estimated_impact_cents, reverse=True)
        return opportunities

    def _check_missed_trade_ev(self, opportunities, missed_report, constraint_analysis):
        high_ev_threshold = self._improvement_cfg.get("missed_ev_high_cents", 500)
        moderate_ev_threshold = self._improvement_cfg.get("missed_ev_moderate_cents", 100)

        summary = constraint_analysis.get("summary", {})
        total_foregone = summary.get("total_foregone_ev_cents", 0)
        if total_foregone <= 0:
            # Fall back to missed trade report
            top_missed = missed_report.get("top_missed_trades", [])
            total_foregone = sum(
                t.get("estimated_pnl_per_contract_cents", 0) for t in top_missed
            )

        if total_foregone < moderate_ev_threshold:
            return

        # Determine dominant reason
        reason_dist = missed_report.get("reason_distribution", {})
        all_reasons = {}
        for bot_reasons in reason_dist.values():
            if isinstance(bot_reasons, dict):
                for reason, count in bot_reasons.items():
                    all_reasons[reason] = all_reasons.get(reason, 0) + count

        dominant_reason = max(all_reasons, key=all_reasons.get) if all_reasons else "unknown"

        # Check if allocator denial is dominant
        denial_reasons = constraint_analysis.get("denial_count_by_constraint", {})
        total_denials = sum(denial_reasons.values())

        severity = SEVERITY_CRITICAL if total_foregone >= high_ev_threshold else SEVERITY_WARNING

        if total_denials > 0 and ("allocator" in dominant_reason.lower() or any(
            k in dominant_reason.lower() for k in ("limit", "budget", "cap", "exhausted")
        )):
            opportunities.append(ImprovementOpportunity(
                category="capital_reallocation",
                severity=severity,
                description=f"High missed EV ({total_foregone}c) primarily due to allocator constraints",
                evidence={
                    "total_foregone_ev_cents": total_foregone,
                    "dominant_reason": dominant_reason,
                    "denial_count_by_constraint": denial_reasons,
                },
                recommended_action="Review capital allocation limits for constrained bots",
                action_class=ACTION_CLASS_PROPOSE,
                estimated_impact_cents=total_foregone,
            ))
        elif "source" in dominant_reason.lower() or "stale" in dominant_reason.lower():
            opportunities.append(ImprovementOpportunity(
                category="source_remediation",
                severity=severity,
                description=f"High missed EV ({total_foregone}c) primarily due to source failures",
                evidence={
                    "total_foregone_ev_cents": total_foregone,
                    "dominant_reason": dominant_reason,
                },
                recommended_action="Remediate or disable failing data sources",
                action_class=ACTION_CLASS_PROPOSE,
                estimated_impact_cents=total_foregone,
            ))
        else:
            opportunities.append(ImprovementOpportunity(
                category="missed_ev_general",
                severity=severity,
                description=f"High missed EV ({total_foregone}c) — dominant reason: {dominant_reason}",
                evidence={
                    "total_foregone_ev_cents": total_foregone,
                    "dominant_reason": dominant_reason,
                    "reason_distribution": all_reasons,
                },
                recommended_action="Review missed trade reasons and address dominant cause",
                action_class=ACTION_CLASS_PROPOSE,
                estimated_impact_cents=total_foregone,
            ))

    def _check_realized_edge_with_execution(self, opportunities, attribution, execution_quality):
        """Conditional improvement rule per spec line 213.

        If a bot has weak P&L but acceptable edge AND deteriorating execution,
        recommend execution-policy review INSTEAD OF model review.
        Returns set of bot names already flagged for execution_policy (for dedup).
        """
        min_trades = self._improvement_cfg.get("min_trades_for_edge_assessment", 5)
        exec_min_trades = self._improvement_cfg.get("min_trades_for_execution_assessment", 10)
        exec_flagged_bots = set()

        by_bot_attr = attribution.get("by_bot", {})
        by_bot_exec = execution_quality.get("by_bot", {})

        for bot_name, stats in by_bot_attr.items():
            trades = stats.get("trades", 0)
            if trades < min_trades:
                continue

            win_rate = stats.get("win_rate", 0.0)
            pnl_cents = stats.get("pnl_cents", 0)

            if pnl_cents >= 0:
                continue  # Not weak P&L, skip

            # Check if this bot has deteriorating execution quality
            exec_stats = by_bot_exec.get(bot_name, {})
            exec_trades = exec_stats.get("trades", 0)
            avg_slippage = exec_stats.get("avg_slippage_cents", 0)
            fill_rate = exec_stats.get("fill_rate")

            # Execution is deteriorating if: high slippage OR poor fill rate
            execution_degraded = (
                exec_trades >= exec_min_trades and (
                    avg_slippage > 2.0 or
                    (fill_rate is not None and fill_rate < 0.5)
                )
            )

            # Edge is "acceptable" if win rate is at least 40% (winning enough,
            # but losing money due to execution, not model quality)
            edge_acceptable = win_rate >= 0.40

            if edge_acceptable and execution_degraded:
                # Spec line 213: execution recommendation INSTEAD OF model
                exec_flagged_bots.add(bot_name)
                opportunities.append(ImprovementOpportunity(
                    category="execution_policy",
                    severity=SEVERITY_WARNING,
                    description=(
                        f"Bot {bot_name} has negative P&L ({pnl_cents}c) "
                        f"with acceptable edge ({win_rate:.0%} WR) but "
                        f"degraded execution (slippage={avg_slippage:.1f}c, "
                        f"fill_rate={fill_rate:.0%})" if fill_rate is not None
                        else f"Bot {bot_name} has negative P&L ({pnl_cents}c) "
                        f"with acceptable edge ({win_rate:.0%} WR) but "
                        f"degraded execution (slippage={avg_slippage:.1f}c)"
                    ),
                    evidence={
                        "bot": bot_name,
                        "pnl_cents": pnl_cents,
                        "trades": trades,
                        "win_rate": win_rate,
                        "avg_slippage_cents": avg_slippage,
                        "fill_rate": fill_rate,
                        "diagnosis": "execution_not_model",
                    },
                    recommended_action=f"Review execution policy for {bot_name} — edge is acceptable, execution is degrading",
                    action_class=ACTION_CLASS_PROPOSE,
                    affected_bots=[bot_name],
                    estimated_impact_cents=abs(pnl_cents),
                ))
            else:
                # Standard model review recommendation
                opportunities.append(ImprovementOpportunity(
                    category="model_review",
                    severity=SEVERITY_WARNING,
                    description=f"Bot {bot_name} has negative P&L ({pnl_cents}c) over {trades} trades",
                    evidence={
                        "bot": bot_name,
                        "pnl_cents": pnl_cents,
                        "trades": trades,
                        "win_rate": win_rate,
                    },
                    recommended_action=f"Review model or threshold for {bot_name}",
                    action_class=ACTION_CLASS_PROPOSE,
                    affected_bots=[bot_name],
                    estimated_impact_cents=abs(pnl_cents),
                ))

        return exec_flagged_bots

    def _check_execution_quality(self, opportunities, execution_quality, skip_bots=None):
        min_trades = self._improvement_cfg.get("min_trades_for_execution_assessment", 10)
        degradation_pct = self._improvement_cfg.get("execution_quality_degradation_pct", 20.0)

        skip_bots = skip_bots or set()
        by_bot = execution_quality.get("by_bot", {})
        for bot_name, stats in by_bot.items():
            if bot_name in skip_bots:
                continue  # Already flagged by edge+execution check
            trades = stats.get("trades", 0)
            if trades < min_trades:
                continue

            avg_slippage = stats.get("avg_slippage_cents", 0)
            fill_rate = stats.get("fill_rate")

            # Significant slippage
            if avg_slippage > 2.0:
                opportunities.append(ImprovementOpportunity(
                    category="execution_policy",
                    severity=SEVERITY_WARNING,
                    description=f"Bot {bot_name} has high avg slippage ({avg_slippage:.1f}c) over {trades} trades",
                    evidence={
                        "bot": bot_name,
                        "avg_slippage_cents": avg_slippage,
                        "trades": trades,
                        "fill_rate": fill_rate,
                    },
                    recommended_action=f"Review execution aggressiveness for {bot_name}",
                    action_class=ACTION_CLASS_PROPOSE,
                    affected_bots=[bot_name],
                    estimated_impact_cents=int(avg_slippage * trades),
                ))

            # Poor fill rate
            if fill_rate is not None and fill_rate < 0.5 and trades >= min_trades:
                opportunities.append(ImprovementOpportunity(
                    category="execution_policy",
                    severity=SEVERITY_WARNING,
                    description=f"Bot {bot_name} fill rate is {fill_rate:.0%} over {trades} trades",
                    evidence={
                        "bot": bot_name,
                        "fill_rate": fill_rate,
                        "trades": trades,
                    },
                    recommended_action=f"Review pricing or order type for {bot_name}",
                    action_class=ACTION_CLASS_PROPOSE,
                    affected_bots=[bot_name],
                ))

    def _check_allocator_constraints(self, opportunities, constraint_analysis):
        if not constraint_analysis:
            return

        top_constraint = constraint_analysis.get("top_binding_constraint")
        top_ev = constraint_analysis.get("top_constraint_foregone_ev_cents", 0)
        realloc_threshold = self._constraint_cfg.get("reallocation_threshold_cents", 200)

        if top_ev >= realloc_threshold and top_constraint:
            candidates = constraint_analysis.get("reallocation_candidates", [])
            opportunities.append(ImprovementOpportunity(
                category="capital_reallocation",
                severity=SEVERITY_WARNING,
                description=(
                    f"Constraint '{top_constraint}' blocked {top_ev}c of estimated EV. "
                    f"{len(candidates)} reallocation candidate(s) identified."
                ),
                evidence={
                    "top_binding_constraint": top_constraint,
                    "foregone_ev_cents": top_ev,
                    "reallocation_candidates": candidates,
                },
                recommended_action=f"Consider relaxing '{top_constraint}' constraint",
                action_class=ACTION_CLASS_PROPOSE,
                estimated_impact_cents=top_ev,
            ))

    # ── Promotion Rules ─────────────────────────────────────────────

    def check_promotion_blockers(self, experiment_id, open_incidents=None):
        """Check whether a promotion should be blocked.

        Returns list of blocker reason strings. Empty list = safe to promote.
        """
        blockers = []
        open_incidents = open_incidents or []

        if self.phase4_observation_active:
            blockers.append("Phase 4 parity observation window is still active")

        for incident in open_incidents:
            inc_type = incident.get("type", "")
            if self._promotion_cfg.get("block_on_open_parity_incident") and "parity" in inc_type:
                blockers.append(f"Open parity incident: {incident.get('id', 'unknown')}")
            if self._promotion_cfg.get("block_on_open_source_incident") and "source" in inc_type:
                blockers.append(f"Open source incident: {incident.get('id', 'unknown')}")
            if self._promotion_cfg.get("block_on_open_execution_incident") and "execution" in inc_type:
                blockers.append(f"Open execution incident: {incident.get('id', 'unknown')}")
            if any(kw in inc_type for kw in ("snapshot",)):
                blockers.append(f"Open snapshot incident: {incident.get('id', 'unknown')}")

        return blockers

    # ── Action Classification ───────────────────────────────────────

    def classify_actions(self, health_verdict, improvement_opportunities):
        """Classify all actions from health and improvement evaluation.

        Returns dict with auto_executed, operator_required, incident_candidates,
        promotion_candidates lists.
        """
        auto_executed = list(health_verdict.auto_actions)
        operator_required = []
        incident_candidates = list(health_verdict.incident_candidates)
        promotion_candidates = []  # Deferred to Slice 5 per spec line 326

        for opp in improvement_opportunities:
            action_entry = opp.to_dict()
            if opp.action_class == ACTION_CLASS_AUTO:
                auto_executed.append(action_entry)
            elif opp.action_class == ACTION_CLASS_PROPOSE:
                operator_required.append(action_entry)
            # ACTION_CLASS_MANUAL items are logged but not auto-proposed

        # During Phase 4, downgrade any auto actions that write config
        if self.phase4_observation_active:
            safe_auto = []
            for action in auto_executed:
                action_type = action.get("type", "")
                if action_type in ("restart_bot", "refresh_artifact", "open_incident", "update_incident"):
                    safe_auto.append(action)
                else:
                    action["downgraded_from"] = "auto"
                    action["downgrade_reason"] = "Phase 4 observation window active"
                    operator_required.append(action)
            auto_executed = safe_auto

        return {
            "auto_executed": auto_executed,
            "operator_required": operator_required,
            "incident_candidates": incident_candidates,
            "promotion_candidates": promotion_candidates,
        }

    # ── Follow-Through ──────────────────────────────────────────────

    def evaluate_follow_through(self, previous_loop=None, open_incidents=None,
                                open_experiments=None):
        """Check follow-through on prior recommendations.

        Returns dict with open_incidents, open_experiments, stale_recommendations.
        """
        stale_days = self._follow_cfg.get("stale_recommendation_days", 7)
        stale_incident_days = self._follow_cfg.get("stale_incident_days", 14)
        now = self._now()

        stale_recommendations = []
        if previous_loop and "actions" in previous_loop:
            prev_actions = previous_loop["actions"]
            for action in prev_actions.get("operator_required", []):
                if action.get("disposition") in ("accepted", "rejected", "deferred"):
                    continue
                created = _parse_iso(action.get("created_at") or previous_loop.get("generated_at"))
                if created and (now - created).days >= stale_days:
                    stale_recommendations.append(action)

        stale_incidents = []
        for incident in (open_incidents or []):
            opened = _parse_iso(incident.get("opened_at") or incident.get("start_time"))
            if opened and (now - opened).days >= stale_incident_days:
                stale_incidents.append(incident)

        return {
            "open_incidents": open_incidents or [],
            "open_experiments": open_experiments or [],
            "stale_recommendations": stale_recommendations,
            "stale_incidents": stale_incidents,
        }


__all__ = [
    "ACTION_CLASS_AUTO",
    "ACTION_CLASS_MANUAL",
    "ACTION_CLASS_PROPOSE",
    "DailyLoopPolicy",
    "HealthVerdict",
    "ImprovementOpportunity",
    "SEVERITY_CRITICAL",
    "SEVERITY_OK",
    "SEVERITY_WARNING",
]
