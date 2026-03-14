"""Canonical persisted artifact contracts for Phase 0.

This module freezes the minimum metadata and record fields that multiple
processes, dashboards, and audit jobs rely on during the refactor program.
"""

TRADE_REQUIRED_FIELDS = (
    "timestamp",
    "ticker",
    "action",
    "side",
    "price_cents",
    "count",
    "cost_cents",
    "reasoning",
    "order_id",
    "status",
    "source_bot",
)

DECISION_REQUIRED_FIELDS = (
    "timestamp",
    "ticker",
    "side",
    "action",
    "reason",
    "source_bot",
)

SCHEMA_METADATA_FIELDS = ("artifact_type", "schema_version")

HEALTH_STATE_ARTIFACT = "health_state"
ALLOCATOR_STATE_ARTIFACT = "allocator_state"
WEATHER_VERIFICATION_ARTIFACT = "weather_verification_state"
WEATHER_NWS_CROSSCHECK_ARTIFACT = "weather_nws_cross_check_state"
SUPERVISOR_STATE_ARTIFACT = "supervisor_state"
FINANCIAL_SNAPSHOT_ARTIFACT = "financial_snapshot"

HEALTH_STATE_SCHEMA_VERSION = 1
ALLOCATOR_STATE_SCHEMA_VERSION = 1
WEATHER_VERIFICATION_SCHEMA_VERSION = 1
WEATHER_NWS_CROSSCHECK_SCHEMA_VERSION = 1
SUPERVISOR_STATE_SCHEMA_VERSION = 1
FINANCIAL_SNAPSHOT_SCHEMA_VERSION = 1


def with_schema_metadata(data, artifact_type, schema_version):
    """Return a shallow-copied mapping with canonical schema metadata."""
    normalized = dict(data) if isinstance(data, dict) else {}
    normalized["artifact_type"] = artifact_type
    normalized["schema_version"] = schema_version
    return normalized


def normalize_health_state(data):
    """Normalize health-state.json while preserving unknown top-level keys."""
    normalized = with_schema_metadata(
        data,
        HEALTH_STATE_ARTIFACT,
        HEALTH_STATE_SCHEMA_VERSION,
    )
    bots = normalized.get("bots")
    sources = normalized.get("sources")
    normalized["bots"] = dict(bots) if isinstance(bots, dict) else {}
    normalized["sources"] = dict(sources) if isinstance(sources, dict) else {}
    return normalized


def normalize_allocator_state(data):
    """Normalize allocator-state.json while preserving unknown top-level keys."""
    normalized = with_schema_metadata(
        data,
        ALLOCATOR_STATE_ARTIFACT,
        ALLOCATOR_STATE_SCHEMA_VERSION,
    )
    for key in ("traded_tickers", "bot_spend", "city_risk", "region_risk"):
        value = normalized.get(key)
        normalized[key] = dict(value) if isinstance(value, dict) else {}
    daily_date = normalized.get("daily_date")
    normalized["daily_date"] = daily_date if isinstance(daily_date, str) or daily_date is None else None
    return normalized


def normalize_verification_state(data, artifact_type, schema_version):
    """Normalize verifier state files that share the pending/verified/stats shape."""
    normalized = with_schema_metadata(data, artifact_type, schema_version)

    pending = normalized.get("pending")
    verified = normalized.get("verified")
    normalized["pending"] = list(pending) if isinstance(pending, list) else []
    normalized["verified"] = list(verified) if isinstance(verified, list) else []

    stats = normalized.get("stats")
    stats = dict(stats) if isinstance(stats, dict) else {}
    total_verified = stats.get("total_verified")
    if not isinstance(total_verified, int) or total_verified < 0:
        total_verified = len(normalized["verified"])
    stats["last_verification"] = stats.get("last_verification")
    stats["total_verified"] = total_verified
    normalized["stats"] = stats
    return normalized


def normalize_supervisor_state(data):
    """Normalize supervisor-state.json without changing its top-level bot layout."""
    normalized = with_schema_metadata(
        {},
        SUPERVISOR_STATE_ARTIFACT,
        SUPERVISOR_STATE_SCHEMA_VERSION,
    )
    if not isinstance(data, dict):
        return normalized

    for key, value in data.items():
        if key in SCHEMA_METADATA_FIELDS or not isinstance(value, dict):
            continue
        entry = dict(value)
        started_at = entry.get("started_at")
        if not isinstance(started_at, (int, float)) and started_at is not None:
            started_at = None
        restart_count = entry.get("restart_count", 0)
        try:
            restart_count = max(0, int(restart_count))
        except (TypeError, ValueError):
            restart_count = 0
        normalized[key] = {
            "started_at": started_at,
            "restart_count": restart_count,
        }
    return normalized
