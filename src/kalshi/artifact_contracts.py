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

BUDGET_RESPONSE_FIELDS = (
    "approved",
    "max_cost_cents",
    "bankroll_cents",
    "reason",
    "binding_constraint",
)

HEALTH_SUMMARY_REQUIRED_FIELDS = (
    "sources",
    "bots",
    "overall",
)

HEALTH_SUMMARY_SOURCE_FIELDS = (
    "status",
    "error_count",
    "last_success",
    "last_error",
)

HEALTH_SUMMARY_BOT_FIELDS = (
    "status",
    "last_heartbeat",
)

VERIFICATION_PENDING_FIELDS = (
    "city",
    "date",
    "models",
    "record_kind",
    "recorded_at",
)

VERIFICATION_VERIFIED_FIELDS = (
    "city",
    "date",
    "models",
    "actual_high",
    "errors",
    "record_kind",
    "recorded_at",
    "verified_at",
)

NWS_CROSSCHECK_PENDING_FIELDS = (
    "city",
    "date",
    "open_meteo_temp",
    "nws_temp",
    "open_meteo_minus_nws",
    "mode",
    "recorded_at",
)

NWS_CROSSCHECK_VERIFIED_FIELDS = (
    "city",
    "date",
    "open_meteo_temp",
    "nws_temp",
    "open_meteo_minus_nws",
    "mode",
    "actual_high",
    "open_meteo_error",
    "nws_error",
    "winner",
    "verified_at",
)

FINANCIAL_SNAPSHOT_REQUIRED_FIELDS = (
    "generated_at",
    "sources_used",
    "account",
    "realized_pnl",
    "unrealized_pnl",
    "balance_check",
    "verification",
    "deposits",
)

SCHEMA_METADATA_FIELDS = ("artifact_type", "schema_version")

HEALTH_STATE_ARTIFACT = "health_state"
ALLOCATOR_STATE_ARTIFACT = "allocator_state"
WEATHER_VERIFICATION_ARTIFACT = "weather_verification_state"
WEATHER_NWS_CROSSCHECK_ARTIFACT = "weather_nws_cross_check_state"
SUPERVISOR_STATE_ARTIFACT = "supervisor_state"
FINANCIAL_SNAPSHOT_ARTIFACT = "financial_snapshot"
MODEL_REGISTRY_ARTIFACT = "model_registry"
STRATEGY_CONFIG_REGISTRY_ARTIFACT = "strategy_config_registry"
EXPERIMENT_RUNS_ARTIFACT = "experiment_runs"
TRADE_ATTRIBUTION_ARTIFACT = "trade_attribution"
INCIDENT_REVIEWS_ARTIFACT = "incident_reviews"

HEALTH_STATE_SCHEMA_VERSION = 1
ALLOCATOR_STATE_SCHEMA_VERSION = 1
WEATHER_VERIFICATION_SCHEMA_VERSION = 1
WEATHER_NWS_CROSSCHECK_SCHEMA_VERSION = 1
SUPERVISOR_STATE_SCHEMA_VERSION = 1
FINANCIAL_SNAPSHOT_SCHEMA_VERSION = 1
MODEL_REGISTRY_SCHEMA_VERSION = 1
STRATEGY_CONFIG_REGISTRY_SCHEMA_VERSION = 1
EXPERIMENT_RUNS_SCHEMA_VERSION = 1
TRADE_ATTRIBUTION_SCHEMA_VERSION = 1
INCIDENT_REVIEWS_SCHEMA_VERSION = 1


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


def normalize_health_summary(data):
    """Normalize HealthCheckMonitor.get_summary() output."""
    normalized = dict(data) if isinstance(data, dict) else {}
    normalized["sources"] = dict(normalized.get("sources")) if isinstance(normalized.get("sources"), dict) else {}
    normalized["bots"] = dict(normalized.get("bots")) if isinstance(normalized.get("bots"), dict) else {}
    overall = normalized.get("overall")
    normalized["overall"] = overall if isinstance(overall, str) and overall else "healthy"
    return normalized


def normalize_registry_state(data, artifact_type, schema_version):
    """Normalize registry artifacts keyed by stable ids."""
    normalized = with_schema_metadata(data, artifact_type, schema_version)
    entries = normalized.get("entries")
    normalized["entries"] = dict(entries) if isinstance(entries, dict) else {}
    return normalized


def normalize_model_registry(data):
    """Normalize model-registry.json while preserving unknown entry payloads."""
    return normalize_registry_state(
        data,
        MODEL_REGISTRY_ARTIFACT,
        MODEL_REGISTRY_SCHEMA_VERSION,
    )


def normalize_strategy_config_registry(data):
    """Normalize strategy-config-registry.json while preserving unknown entry payloads."""
    return normalize_registry_state(
        data,
        STRATEGY_CONFIG_REGISTRY_ARTIFACT,
        STRATEGY_CONFIG_REGISTRY_SCHEMA_VERSION,
    )


def normalize_experiment_runs(data):
    """Normalize experiment-runs.json while preserving unknown entry payloads."""
    return normalize_registry_state(
        data,
        EXPERIMENT_RUNS_ARTIFACT,
        EXPERIMENT_RUNS_SCHEMA_VERSION,
    )


def normalize_incident_reviews(data):
    """Normalize incident-reviews.json while preserving unknown entry payloads."""
    return normalize_registry_state(
        data,
        INCIDENT_REVIEWS_ARTIFACT,
        INCIDENT_REVIEWS_SCHEMA_VERSION,
    )


def normalize_trade_attribution(data):
    """Normalize the canonical post-trade attribution artifact."""
    normalized = with_schema_metadata(
        data,
        TRADE_ATTRIBUTION_ARTIFACT,
        TRADE_ATTRIBUTION_SCHEMA_VERSION,
    )
    generated_at = normalized.get("generated_at")
    normalized["generated_at"] = generated_at if isinstance(generated_at, str) else None

    report_name = normalized.get("report_name")
    normalized["report_name"] = report_name if isinstance(report_name, str) and report_name else "daily_attribution"

    for key in ("by_bot", "by_edge_bucket", "by_regime", "by_sizing", "by_market_type", "summary"):
        value = normalized.get(key)
        normalized[key] = dict(value) if isinstance(value, dict) else {}
    return normalized


def normalize_financial_snapshot(data):
    """Normalize financial-snapshot.json while preserving extra sections."""
    normalized = with_schema_metadata(
        data,
        FINANCIAL_SNAPSHOT_ARTIFACT,
        FINANCIAL_SNAPSHOT_SCHEMA_VERSION,
    )
    sources_used = normalized.get("sources_used")
    normalized["sources_used"] = list(sources_used) if isinstance(sources_used, list) else []
    for key in ("account", "realized_pnl", "unrealized_pnl", "balance_check", "verification", "deposits"):
        value = normalized.get(key)
        normalized[key] = dict(value) if isinstance(value, dict) else {}
    generated_at = normalized.get("generated_at")
    normalized["generated_at"] = generated_at if isinstance(generated_at, str) else None
    return normalized
