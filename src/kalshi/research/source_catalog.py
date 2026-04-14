"""Canonical source catalog and scorecard helpers."""

from __future__ import annotations

import datetime
import json
import logging
from collections import defaultdict
from pathlib import Path

from artifact_contracts import normalize_health_state
from event_ledger import DEFAULT_LEDGER_PATH, EventLedger
from ops.health_monitor import BOT_SOURCE_MAP, HEALTH_STATE_PATH
from runtime_paths import resolve_data_dir


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE_CATALOG_PATH = resolve_data_dir(PROJECT_DIR) / "source-catalog.json"

log = logging.getLogger("source-catalog")


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def _load_health_state_safe(filepath):
    """Load normalized health-state.json. Returns an empty normalized payload on failure."""
    try:
        path = Path(filepath)
        if not path.exists():
            return normalize_health_state(None)
        text = path.read_text().strip()
        if not text:
            return normalize_health_state(None)
        return normalize_health_state(json.loads(text))
    except (json.JSONDecodeError, ValueError, OSError):
        return normalize_health_state(None)


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _source_family(source_name):
    source = str(source_name or "").lower()
    if source.startswith("open-meteo"):
        return "weather_forecast"
    if source.startswith("nws"):
        return "weather_observation"
    if source in {"coinbase", "deribit", "binance"}:
        return "crypto_market_data"
    if source in {"cleveland-fed", "gdpnow", "cme-fedwatch", "aaa-gas", "truflation", "tips-breakeven"}:
        return "economics_data"
    if source in {"hdd", "boxoffice"}:
        return "entertainment_data"
    if source.startswith("beatrelease"):
        return "blog_signal"
    return "other"


class SourceCatalog:
    """Build a canonical source-health and freshness catalog."""

    def __init__(
        self,
        *,
        health_state_path=HEALTH_STATE_PATH,
        ledger_path=DEFAULT_LEDGER_PATH,
        bot_source_map=None,
        utc_now_func=_utc_now,
    ):
        self._health_state_path = Path(health_state_path)
        self._ledger_path = Path(ledger_path)
        self._bot_source_map = BOT_SOURCE_MAP if bot_source_map is None else bot_source_map
        self._utc_now = utc_now_func
        self._health_state = normalize_health_state(None)
        self._source_observations = []

    def load(self, *, health_state=None, source_observations=None):
        self._health_state = normalize_health_state(
            health_state if health_state is not None else _load_health_state_safe(self._health_state_path)
        )
        if source_observations is not None:
            self._source_observations = list(source_observations)
        else:
            self._source_observations = EventLedger(self._ledger_path).get_source_observation_records()

    def _owners_by_source(self):
        owner_map = defaultdict(list)
        for bot_name, sources in (self._bot_source_map or {}).items():
            for source in sources or []:
                owner_map[source].append(bot_name)
        return {source: sorted(set(bots)) for source, bots in owner_map.items()}

    def build(self):
        now = self._utc_now()
        owners = self._owners_by_source()
        observations_by_source = defaultdict(list)
        for record in self._source_observations:
            source_name = record.get("source_name")
            if source_name:
                observations_by_source[source_name].append(record)

        all_sources = sorted(
            set(owners)
            | set(self._health_state.get("sources", {}))
            | set(observations_by_source)
        )

        entries = []
        for source_name in all_sources:
            health = dict(self._health_state.get("sources", {}).get(source_name, {}))
            observations = observations_by_source.get(source_name, [])
            observation_times = [
                _parse_timestamp(record.get("observed_at") or record.get("timestamp"))
                for record in observations
            ]
            observation_times = [ts for ts in observation_times if ts is not None]
            last_observation_dt = max(observation_times) if observation_times else None
            last_success_dt = _parse_timestamp(health.get("last_success"))
            last_error_dt = _parse_timestamp(health.get("last_error"))
            freshest_dt = max(
                [ts for ts in (last_observation_dt, last_success_dt) if ts is not None],
                default=None,
            )

            if health.get("opened_at") is not None and health.get("error_count", 0) > 0:
                status = "error"
            elif health.get("error_count", 0) > 0:
                status = "warning"
            elif freshest_dt is not None:
                status = "ok"
            else:
                status = "unknown"

            entries.append(
                {
                    "source_name": source_name,
                    "source_family": _source_family(source_name),
                    "owner_bots": owners.get(source_name, []),
                    "status": status,
                    "error_count": int(health.get("error_count", 0) or 0),
                    "last_success": health.get("last_success"),
                    "last_error": health.get("last_error"),
                    "last_error_message": health.get("last_error_message"),
                    "last_observation": last_observation_dt.isoformat() if last_observation_dt is not None else None,
                    "freshest_seen_at": freshest_dt.isoformat() if freshest_dt is not None else None,
                    "freshness_minutes": round((now - freshest_dt).total_seconds() / 60, 1) if freshest_dt is not None else None,
                    "observation_count_24h": sum(
                        1
                        for ts in observation_times
                        if (now - ts).total_seconds() <= 24 * 3600
                    ),
                    "observation_count_7d": sum(
                        1
                        for ts in observation_times
                        if (now - ts).total_seconds() <= 7 * 24 * 3600
                    ),
                }
            )

        status_rank = {"error": 0, "warning": 1, "unknown": 2, "ok": 3}
        entries.sort(key=lambda entry: (status_rank.get(entry["status"], 9), entry["source_name"]))

        return {
            "generated_at": now.isoformat(),
            "summary": {
                "total_sources": len(entries),
                "error_sources": sum(1 for entry in entries if entry["status"] == "error"),
                "warning_sources": sum(1 for entry in entries if entry["status"] == "warning"),
                "sources_with_recent_observations": sum(1 for entry in entries if entry["observation_count_24h"] > 0),
            },
            "sources": entries,
        }

    def summary_report(self):
        report = self.build()
        lines = [
            "=" * 60,
            "SOURCE SCORECARD",
            "=" * 60,
            "",
            f"Total sources: {report['summary']['total_sources']}",
            f"Error sources: {report['summary']['error_sources']}",
            f"Warning sources: {report['summary']['warning_sources']}",
            f"Sources with observations in last 24h: {report['summary']['sources_with_recent_observations']}",
            "",
            "-" * 60,
        ]
        for entry in report["sources"]:
            freshness = f"{entry['freshness_minutes']:.1f}m" if entry["freshness_minutes"] is not None else "n/a"
            owners = ",".join(entry["owner_bots"]) if entry["owner_bots"] else "unowned"
            lines.append(
                f"{entry['source_name']:20s}  {entry['status']:7s}  owners={owners:20s}  "
                f"freshness={freshness:>8s}  errors={entry['error_count']:>2d}  obs24h={entry['observation_count_24h']:>3d}"
            )
            if entry.get("last_error_message"):
                lines.append(f"  last_error: {entry['last_error_message']}")
        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)


__all__ = [
    "DEFAULT_SOURCE_CATALOG_PATH",
    "SourceCatalog",
    "_load_health_state_safe",
]
