"""Canonical missed-trade analysis over the opportunity log artifact."""

from __future__ import annotations

import datetime
import json
import logging
from collections import defaultdict
from pathlib import Path

from research.opportunity_log import DEFAULT_OPPORTUNITY_LOG_PATH


log = logging.getLogger("missed-trade-analysis")

MISSED_ACTIONS = {"skipped", "rejected", "pruned"}


def _load_opportunities_safe(filepath):
    """Load opportunity records from a JSON file. Returns [] on missing/corrupt data."""
    try:
        path = Path(filepath)
        if not path.exists():
            return []
        text = path.read_text().strip()
        if not text:
            return []
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError, OSError):
        return []


def _parse_timestamp(timestamp):
    if not timestamp:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _coerce_positive_edge(record):
    edge = record.get("edge")
    if not isinstance(edge, (int, float)) or edge <= 0:
        return None
    return float(edge)


def _coerce_price_cents(record):
    price = record.get("price_cents")
    if not isinstance(price, (int, float)):
        return None
    if price < 0 or price > 100:
        return None
    return int(price)


class MissedTradeAnalyzer:
    """Summarize missed opportunities from the canonical opportunity log."""

    def __init__(self, opportunity_log_path=DEFAULT_OPPORTUNITY_LOG_PATH):
        self._opportunity_log_path = Path(opportunity_log_path)
        self._records = []

    def load_opportunities(self, records=None):
        if records is not None:
            self._records = list(records)
        else:
            self._records = _load_opportunities_safe(self._opportunity_log_path)

    def _filtered_records(self, *, bot=None, days=None):
        cutoff = None
        if days is not None:
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

        filtered = []
        for record in self._records:
            action = record.get("action", "")
            if action not in MISSED_ACTIONS:
                continue
            source_bot = record.get("source_bot", "unknown")
            if bot and source_bot != bot:
                continue
            if cutoff is not None:
                parsed_ts = _parse_timestamp(record.get("timestamp"))
                if parsed_ts is None or parsed_ts < cutoff:
                    continue
            filtered.append(record)
        return filtered

    def reason_distribution(self, *, bot=None, days=None):
        dist = defaultdict(lambda: defaultdict(int))
        for record in self._filtered_records(bot=bot, days=days):
            source_bot = record.get("source_bot", "unknown")
            reason = record.get("reason", "unknown")
            dist[source_bot][reason] += 1
        return {source_bot: dict(reasons) for source_bot, reasons in dist.items()}

    def stage_distribution(self, *, bot=None, days=None):
        dist = defaultdict(int)
        for record in self._filtered_records(bot=bot, days=days):
            dist[record.get("opportunity_stage", "unknown")] += 1
        return dict(dist)

    def top_missed_trades(self, *, bot=None, days=None, limit=10, min_edge=0.0):
        results = []
        for record in self._filtered_records(bot=bot, days=days):
            edge = _coerce_positive_edge(record)
            price_cents = _coerce_price_cents(record)
            if edge is None or edge <= min_edge or price_cents is None:
                continue
            estimated_pnl = round(edge * max(0, 100 - price_cents), 4)
            results.append({
                "ticker": record.get("ticker", ""),
                "source_bot": record.get("source_bot", "unknown"),
                "reason": record.get("reason", ""),
                "action": record.get("action", ""),
                "opportunity_stage": record.get("opportunity_stage", "unknown"),
                "side": record.get("side", ""),
                "edge": round(edge, 4),
                "price_cents": price_cents,
                "estimated_pnl_per_contract_cents": estimated_pnl,
                "timestamp": record.get("timestamp", ""),
                "model_name": record.get("model_name"),
                "feature_snapshot_id": record.get("feature_snapshot_id"),
            })
        results.sort(
            key=lambda row: (
                row["estimated_pnl_per_contract_cents"],
                row["edge"],
                row["timestamp"],
            ),
            reverse=True,
        )
        return results[:limit]

    def full_report(self, *, bot=None, days=None, limit=10, min_edge=0.0):
        filtered = self._filtered_records(bot=bot, days=days)
        positive_edge_records = sum(1 for record in filtered if _coerce_positive_edge(record) is not None)
        return {
            "summary": {
                "total_missed_opportunities": len(filtered),
                "positive_edge_opportunities": positive_edge_records,
                "bot_filter": bot,
                "days_filter": days,
                "min_edge": min_edge,
            },
            "stage_distribution": self.stage_distribution(bot=bot, days=days),
            "reason_distribution": self.reason_distribution(bot=bot, days=days),
            "top_missed_trades": self.top_missed_trades(bot=bot, days=days, limit=limit, min_edge=min_edge),
        }

    def summary_report(self, *, bot=None, days=None, limit=10, min_edge=0.0):
        report = self.full_report(bot=bot, days=days, limit=limit, min_edge=min_edge)
        summary = report["summary"]
        lines = [
            "=" * 60,
            "MISSED TRADE REPORT",
            "=" * 60,
            f"",
            f"Total missed opportunities: {summary['total_missed_opportunities']}",
            f"Positive-edge opportunities: {summary['positive_edge_opportunities']}",
        ]

        if report["stage_distribution"]:
            lines.append("")
            lines.append("-" * 60)
            lines.append("OPPORTUNITY STAGES")
            lines.append("-" * 60)
            for stage, count in sorted(report["stage_distribution"].items()):
                lines.append(f"  {stage}: {count}")

        if report["reason_distribution"]:
            lines.append("")
            lines.append("-" * 60)
            lines.append("MISSED DISTRIBUTION BY BOT")
            lines.append("-" * 60)
            for source_bot in sorted(report["reason_distribution"]):
                reasons = report["reason_distribution"][source_bot]
                lines.append(f"")
                lines.append(f"  {source_bot} ({sum(reasons.values())} missed):")
                for reason in sorted(reasons, key=reasons.get, reverse=True):
                    lines.append(f"    {reason}: {reasons[reason]}")

        if report["top_missed_trades"]:
            lines.append("")
            lines.append("-" * 60)
            lines.append("TOP MISSED TRADES")
            lines.append("-" * 60)
            for item in report["top_missed_trades"]:
                lines.append(
                    f"  {item['ticker']}  edge={item['edge']:.2%}  "
                    f"price={item['price_cents']}c  est_pnl={item['estimated_pnl_per_contract_cents']:.2f}c  "
                    f"reason={item['reason']}  bot={item['source_bot']}"
                )

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)


__all__ = [
    "DEFAULT_OPPORTUNITY_LOG_PATH",
    "MISSED_ACTIONS",
    "MissedTradeAnalyzer",
    "_load_opportunities_safe",
]
