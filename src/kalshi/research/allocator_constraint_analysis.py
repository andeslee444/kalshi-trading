"""Allocator constraint analysis — quantifies missed EV by binding constraint.

Combines budget-decision events from the event ledger with opportunity-log
records to answer:
  1. Which constraint blocked the most opportunities today?
  2. Which constraint blocked the highest estimated EV today?
  3. Which bot had the highest foregone EV due to caps?
  4. Which markets were skipped due to concentration, not lack of edge?
  5. If capital were reallocated across bots, what was the top candidate move?

Pure analytics module — no side effects.

Usage:
    from research.allocator_constraint_analysis import ConstraintAnalyzer

    analyzer = ConstraintAnalyzer(ledger_path="data/event-ledger.sqlite3")
    analyzer.load_budget_decisions(lookback_days=1)
    report = analyzer.full_report()
"""

from __future__ import annotations

import datetime
import json
import logging
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[3]

_log = logging.getLogger("allocator-constraint-analysis")

# Dedup/identity constraints — the allocator correctly blocking double-ups.
# These are NOT capacity constraints and should be separated in reports.
DEDUP_CONSTRAINTS = frozenset({
    "dedup", "already_traded", "duplicate", "cooldown",
})

# Normalized capacity constraint buckets — these map raw binding_constraint
# values and reason substrings to a canonical bucket name for reporting.
CAPACITY_CONSTRAINTS = frozenset({
    "bot_daily_limit", "portfolio_daily_limit", "bot_config_cap",
    "ticker_concentration", "city_concentration", "region_exposure",
    "cluster_concentration", "max_concurrent_positions",
    "absolute_daily_risk_cap", "strategy_daily_allocation",
})


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


def _estimate_ev_cents(decision):
    """Estimate expected value of a denied budget decision.

    EV ≈ edge × (100 - price_implied) for a single contract.
    We use edge × max_cost as a rough proxy when price is unknown.
    """
    edge = decision.get("edge")
    if not isinstance(edge, (int, float)) or edge <= 0:
        return 0

    # If we have bot_max_cost_cents, use edge * cost as EV proxy
    max_cost = decision.get("bot_max_cost_cents", 0)
    if isinstance(max_cost, (int, float)) and max_cost > 0:
        return round(edge * max_cost, 2)

    # Fallback: edge * 100 (one contract at 100c max)
    return round(edge * 100, 2)


def _normalize_constraint(decision):
    """Normalize a decision's constraint to a canonical bucket.

    Returns (canonical_constraint, is_dedup) where is_dedup=True means the
    denial is a dedup/identity constraint (not a capacity issue).
    """
    bc = (decision.get("binding_constraint") or "").strip()
    reason = (decision.get("reason") or "").strip().lower()

    # 1. If binding_constraint is set and recognized, use it directly
    if bc:
        bc_lower = bc.lower()
        # Check dedup
        if bc_lower in DEDUP_CONSTRAINTS or "already traded" in reason or "dedup" in reason:
            return bc, True
        # Check known capacity constraints
        if bc_lower in CAPACITY_CONSTRAINTS:
            return bc, False
        # Attempt fuzzy match on reason for capacity keywords
        for kw in ("limit", "cap", "exhausted", "allocation", "budget",
                    "concentration", "exposure", "concurrent", "risk_cap"):
            if kw in bc_lower or kw in reason:
                return bc, False
        # Unknown constraint — still not dedup
        return bc, False

    # 2. No binding_constraint — classify from reason string
    if "already traded" in reason or "dedup" in reason or "duplicate" in reason or "cooldown" in reason:
        return "dedup", True
    # Map common reason phrases to constraint buckets
    for phrase, bucket in [
        ("daily allocation exhausted", "bot_daily_limit"),
        ("daily limit", "bot_daily_limit"),
        ("daily loss", "portfolio_daily_limit"),
        ("concentration", "ticker_concentration"),
        ("city exposure", "city_concentration"),
        ("region exposure", "region_exposure"),
        ("concurrent", "max_concurrent_positions"),
        ("risk cap", "absolute_daily_risk_cap"),
        ("budget", "bot_config_cap"),
    ]:
        if phrase in reason:
            return bucket, False

    # Fallback — use the raw reason as the constraint name
    return reason or "unknown", False


class ConstraintAnalyzer:
    """Analyzes allocator budget decisions to quantify constraint impact.

    Combines budget-decision events from the ledger with opportunity-log
    records (the missed-trade analysis) to produce a unified constraint report
    that separates dedup denials from genuine capacity constraints.
    """

    def __init__(self, ledger_path=None, opportunity_log_path=None, logger=None):
        self._ledger_path = Path(ledger_path) if ledger_path else PROJECT_DIR / "data" / "event-ledger.sqlite3"
        self._opportunity_log_path = (
            Path(opportunity_log_path) if opportunity_log_path
            else PROJECT_DIR / "data" / "opportunity-log.json"
        )
        self._log = logger or _log
        self._decisions = []     # All budget decisions in window
        self._denied = []        # Only denied decisions (excluding dedup)
        self._dedup_denied = []  # Dedup denials (separated for clarity)
        self._all_denied = []    # All denied (both dedup + capacity)
        self._opportunity_records = []  # From opportunity-log.json

    def load_budget_decisions(self, lookback_days=1, decisions=None,
                              opportunity_records=None, now_func=None):
        """Load budget decisions from ledger or from pre-loaded list.

        Also loads opportunity-log records to enrich the constraint picture
        with missed trades that were rejected before reaching the allocator.

        Args:
            lookback_days: How many days back to look.
            decisions: Optional pre-loaded list (for testing).
            opportunity_records: Optional pre-loaded opportunity log (for testing).
            now_func: Optional callable returning current UTC datetime.
        """
        if decisions is not None:
            self._decisions = list(decisions)
        else:
            self._decisions = self._load_from_ledger()

        # Load opportunity log records
        if opportunity_records is not None:
            self._opportunity_records = list(opportunity_records)
        else:
            self._opportunity_records = self._load_opportunity_log()

        now = (now_func or _utc_now)()
        cutoff = now - datetime.timedelta(days=lookback_days)

        # Filter decisions to lookback window
        filtered = []
        for d in self._decisions:
            ts = _parse_iso(d.get("timestamp"))
            if ts is not None and ts >= cutoff:
                filtered.append(d)
            elif ts is None:
                self._log.debug("Dropping decision without parseable timestamp: %s",
                                d.get("ticker", "unknown"))
        self._decisions = filtered

        # Filter opportunity records to lookback window
        opp_filtered = []
        for r in self._opportunity_records:
            ts = _parse_iso(r.get("timestamp"))
            if ts is not None and ts >= cutoff:
                opp_filtered.append(r)
            elif ts is None:
                self._log.debug("Dropping opportunity record without parseable timestamp: %s",
                                r.get("ticker", "unknown"))
        self._opportunity_records = opp_filtered

        # Separate denied decisions into dedup vs capacity using normalization
        self._denied = []
        self._dedup_denied = []
        self._all_denied = []
        for d in self._decisions:
            if d.get("approved", True):
                continue
            # Normalize the constraint and classify
            normalized, is_dedup = _normalize_constraint(d)
            d["_normalized_constraint"] = normalized
            d["_is_dedup"] = is_dedup
            self._all_denied.append(d)
            if is_dedup:
                self._dedup_denied.append(d)
            else:
                self._denied.append(d)

    def _load_from_ledger(self):
        """Load budget_decision events from the SQLite ledger."""
        if not self._ledger_path.exists():
            self._log.warning("Ledger not found at %s", self._ledger_path)
            return []

        try:
            from event_ledger import get_event_ledger, EVENT_TYPE_BUDGET_DECISION
            ledger = get_event_ledger(path=self._ledger_path, logger=self._log)
            return ledger._fetch_event_payloads(EVENT_TYPE_BUDGET_DECISION)
        except Exception as e:
            self._log.warning("Failed to load budget decisions from ledger: %s", e)
            return []

    def _load_opportunity_log(self):
        """Load opportunity-log records from JSON file."""
        try:
            if not self._opportunity_log_path.exists():
                return []
            text = self._opportunity_log_path.read_text().strip()
            if not text:
                return []
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError, ValueError):
            self._log.warning("Failed to load opportunity log from %s", self._opportunity_log_path)
            return []

    def denial_count_by_constraint(self):
        """Count denied decisions grouped by normalized constraint (excludes dedup)."""
        counts = defaultdict(int)
        for d in self._denied:
            counts[d["_normalized_constraint"]] += 1
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))

    def foregone_ev_by_constraint(self, min_edge=0.0):
        """Estimate foregone EV grouped by normalized constraint (excludes dedup)."""
        ev_by_constraint = defaultdict(float)
        for d in self._denied:
            edge = d.get("edge")
            if not isinstance(edge, (int, float)) or edge < min_edge:
                continue
            ev_by_constraint[d["_normalized_constraint"]] += _estimate_ev_cents(d)
        return dict(sorted(ev_by_constraint.items(), key=lambda x: x[1], reverse=True))

    def denial_count_by_bot(self):
        """Count denied decisions grouped by bot_name (excludes dedup)."""
        counts = defaultdict(int)
        for d in self._denied:
            bot = d.get("bot_name", "unknown")
            counts[bot] += 1
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))

    def foregone_ev_by_bot(self, min_edge=0.0):
        """Estimate foregone EV grouped by bot_name (excludes dedup)."""
        ev_by_bot = defaultdict(float)
        for d in self._denied:
            edge = d.get("edge")
            if not isinstance(edge, (int, float)) or edge < min_edge:
                continue
            bot = d.get("bot_name", "unknown")
            ev_by_bot[bot] += _estimate_ev_cents(d)
        return dict(sorted(ev_by_bot.items(), key=lambda x: x[1], reverse=True))

    def top_denied_opportunities(self, limit=10, min_edge=0.0):
        """Return the top denied opportunities ranked by estimated EV (excludes dedup)."""
        scored = []
        for d in self._denied:
            edge = d.get("edge")
            if not isinstance(edge, (int, float)) or edge < min_edge:
                continue
            ev = _estimate_ev_cents(d)
            scored.append({
                "ticker": d.get("ticker", ""),
                "bot_name": d.get("bot_name", "unknown"),
                "edge": round(float(edge), 4) if edge else 0,
                "confidence": d.get("confidence"),
                "bot_max_cost_cents": d.get("bot_max_cost_cents", 0),
                "reason": d.get("reason", ""),
                "binding_constraint": d.get("_normalized_constraint", d.get("binding_constraint", "")),
                "estimated_ev_cents": ev,
                "timestamp": d.get("timestamp", ""),
            })
        scored.sort(key=lambda x: x["estimated_ev_cents"], reverse=True)
        return scored[:limit]

    def approval_rate(self):
        """Compute overall and per-bot approval rates."""
        total = len(self._decisions)
        if total == 0:
            return {"overall": None, "by_bot": {}}

        approved_count = sum(1 for d in self._decisions if d.get("approved", True))
        overall_rate = round(approved_count / total, 4)

        by_bot_total = defaultdict(int)
        by_bot_approved = defaultdict(int)
        for d in self._decisions:
            bot = d.get("bot_name", "unknown")
            by_bot_total[bot] += 1
            if d.get("approved", True):
                by_bot_approved[bot] += 1

        by_bot = {}
        for bot in sorted(by_bot_total):
            by_bot[bot] = round(by_bot_approved[bot] / by_bot_total[bot], 4) if by_bot_total[bot] > 0 else None

        return {"overall": overall_rate, "by_bot": by_bot}

    def reallocation_candidates(self, min_foregone_ev_cents=200):
        """Identify bots that could benefit from higher allocation limits.

        A reallocation candidate is a bot where:
          - significant EV was denied due to capacity constraints
          - dedup denials are excluded (they are not actionable via reallocation)
        """
        ev_by_bot = self.foregone_ev_by_bot()
        count_by_bot = self.denial_count_by_bot()

        # For each bot, find dominant normalized constraint (already excludes dedup)
        bot_constraints = defaultdict(lambda: defaultdict(float))
        for d in self._denied:
            bot = d.get("bot_name", "unknown")
            ev = _estimate_ev_cents(d)
            bot_constraints[bot][d["_normalized_constraint"]] += ev

        candidates = []
        for bot, total_ev in ev_by_bot.items():
            if total_ev < min_foregone_ev_cents:
                continue

            constraints = dict(bot_constraints[bot])
            dominant = max(constraints, key=constraints.get) if constraints else "unknown"

            # Since dedup is already excluded, all remaining constraints are
            # capacity constraints. But verify the dominant is actionable.
            if dominant in CAPACITY_CONSTRAINTS or any(
                kw in dominant.lower() for kw in ("limit", "cap", "exhausted", "allocation", "budget", "concentration")
            ):
                candidates.append({
                    "bot": bot,
                    "foregone_ev_cents": round(total_ev, 2),
                    "denial_count": count_by_bot.get(bot, 0),
                    "dominant_constraint": dominant,
                    "constraint_breakdown": {k: round(v, 2) for k, v in constraints.items()},
                    "recommendation": f"Consider increasing {dominant} for {bot}",
                })

        candidates.sort(key=lambda c: c["foregone_ev_cents"], reverse=True)
        return candidates

    def opportunity_log_summary(self):
        """Summarize opportunity-log missed trades by reason (pre-allocator rejections).

        These are trades that were skipped/rejected/pruned before reaching the
        allocator, so they don't appear in budget_decision events.
        """
        reason_counts = defaultdict(int)
        ev_by_reason = defaultdict(float)
        for r in self._opportunity_records:
            action = r.get("action", "")
            if action not in ("skipped", "rejected", "pruned"):
                continue
            reason = r.get("reason", "unknown")
            reason_counts[reason] += 1
            edge = r.get("edge")
            price = r.get("price_cents")
            if isinstance(edge, (int, float)) and edge > 0 and isinstance(price, (int, float)):
                ev_by_reason[reason] += round(edge * max(0, 100 - price), 2)
        return {
            "reason_counts": dict(sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)),
            "ev_by_reason": dict(sorted(ev_by_reason.items(), key=lambda x: x[1], reverse=True)),
            "total_records": len(self._opportunity_records),
        }

    def full_report(self, limit=10, min_edge=0.02, min_realloc_ev=200):
        """Generate complete constraint analysis report.

        Separates dedup denials from capacity denials, and includes
        opportunity-log missed trades for a unified constraint picture.
        """
        ev_by_constraint = self.foregone_ev_by_constraint(min_edge=min_edge)
        denial_by_constraint = self.denial_count_by_constraint()

        top_constraint = max(ev_by_constraint, key=ev_by_constraint.get) if ev_by_constraint else None
        top_constraint_ev = ev_by_constraint.get(top_constraint, 0) if top_constraint else 0

        # Dedup statistics (separate from capacity constraints)
        dedup_count = len(self._dedup_denied)
        dedup_by_bot = defaultdict(int)
        for d in self._dedup_denied:
            dedup_by_bot[d.get("bot_name", "unknown")] += 1

        return {
            "summary": {
                "total_decisions": len(self._decisions),
                "total_denials_all": len(self._all_denied),
                "total_denials_capacity": len(self._denied),
                "total_denials_dedup": dedup_count,
                "total_foregone_ev_cents": round(sum(ev_by_constraint.values()), 2),
            },
            "denial_count_by_constraint": denial_by_constraint,
            "foregone_ev_by_constraint": {k: round(v, 2) for k, v in ev_by_constraint.items()},
            "denial_count_by_bot": self.denial_count_by_bot(),
            "foregone_ev_by_bot": {k: round(v, 2) for k, v in self.foregone_ev_by_bot(min_edge=min_edge).items()},
            "top_denied_opportunities": self.top_denied_opportunities(limit=limit, min_edge=min_edge),
            "approval_rate": self.approval_rate(),
            "top_binding_constraint": top_constraint,
            "top_constraint_foregone_ev_cents": round(top_constraint_ev, 2),
            "reallocation_candidates": self.reallocation_candidates(min_foregone_ev_cents=min_realloc_ev),
            "dedup": {
                "count": dedup_count,
                "by_bot": dict(sorted(dedup_by_bot.items(), key=lambda x: x[1], reverse=True)),
                "note": "Dedup denials are the allocator correctly preventing double-ups, not capacity constraints.",
            },
            "opportunity_log": self.opportunity_log_summary(),
        }

    def summary_text(self, report=None):
        """Generate human-readable summary text."""
        report = report or self.full_report()
        s = report["summary"]
        lines = [
            "=" * 60,
            "ALLOCATOR CONSTRAINT ANALYSIS",
            "=" * 60,
            f"Total budget decisions: {s['total_decisions']}",
            f"Capacity denials: {s.get('total_denials_capacity', s.get('total_denials', 0))}",
            f"Dedup denials: {s.get('total_denials_dedup', 0)} (not capacity constraints)",
            f"Total foregone EV (capacity only): {s['total_foregone_ev_cents']:.0f}c (${s['total_foregone_ev_cents']/100:.2f})",
        ]

        approval = report.get("approval_rate", {})
        if approval.get("overall") is not None:
            lines.append(f"Approval rate: {approval['overall']:.1%}")

        if report["denial_count_by_constraint"]:
            lines.append("")
            lines.append("-" * 60)
            lines.append("DENIALS BY CONSTRAINT")
            lines.append("-" * 60)
            ev_map = report.get("foregone_ev_by_constraint", {})
            for constraint, count in report["denial_count_by_constraint"].items():
                ev = ev_map.get(constraint, 0)
                lines.append(f"  {constraint}: {count} denials, {ev:.0f}c foregone EV")

        if report["denial_count_by_bot"]:
            lines.append("")
            lines.append("-" * 60)
            lines.append("DENIALS BY BOT")
            lines.append("-" * 60)
            ev_map = report.get("foregone_ev_by_bot", {})
            for bot, count in report["denial_count_by_bot"].items():
                ev = ev_map.get(bot, 0)
                lines.append(f"  {bot}: {count} denials, {ev:.0f}c foregone EV")

        if report["reallocation_candidates"]:
            lines.append("")
            lines.append("-" * 60)
            lines.append("REALLOCATION CANDIDATES")
            lines.append("-" * 60)
            for c in report["reallocation_candidates"]:
                lines.append(
                    f"  {c['bot']}: {c['foregone_ev_cents']:.0f}c foregone "
                    f"({c['denial_count']} denials, constraint={c['dominant_constraint']})"
                )

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)


__all__ = [
    "CAPACITY_CONSTRAINTS",
    "ConstraintAnalyzer",
    "DEDUP_CONSTRAINTS",
    "_estimate_ev_cents",
    "_normalize_constraint",
]
