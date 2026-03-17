"""Health monitoring helpers extracted from kalshi_auth."""

from __future__ import annotations

import datetime
import fcntl
import json
import logging
import time
from pathlib import Path

from artifact_contracts import normalize_health_state, normalize_health_summary
from bot_registry import ALWAYS_DISABLED_BOT_IDS, BOT_CONFIG_KEY_MAP, BOT_HEALTH_KEY_MAP
from risk.kill_switch import per_bot_halt_path
from storage import atomic_write_json

PROJECT_DIR = Path(__file__).resolve().parents[3]

BOT_SOURCE_MAP = {
    "weather": [
        "open-meteo-batch",
        "open-meteo-single",
        "open-meteo-ensemble",
        "nws-forecast",
    ],
    "crypto": ["coinbase", "deribit"],
    "economics": ["cleveland-fed", "gdpnow", "cme-fedwatch"],
    "entertainment": ["hdd", "boxoffice"],
    "source-monitor": ["hdd", "boxoffice", "nws"],
    "beatrelease": ["beatrelease"],
}

HEALTH_STATE_PATH = PROJECT_DIR / "data" / "health-state.json"

_log = logging.getLogger("health-monitor")


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_ignored_bot_names(project_dir=PROJECT_DIR):
    ignored = {
        health_key
        for bot_id in ALWAYS_DISABLED_BOT_IDS
        if (health_key := BOT_HEALTH_KEY_MAP.get(bot_id))
    }

    config_path = Path(project_dir) / "config" / "bots-config.json"
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return ignored

    for bot_id, config_key in BOT_CONFIG_KEY_MAP.items():
        bot_cfg = config.get(config_key, {})
        if isinstance(bot_cfg, dict) and bot_cfg.get("enabled") is False:
            health_key = BOT_HEALTH_KEY_MAP.get(bot_id)
            if health_key:
                ignored.add(health_key)
    return ignored


class HealthCheckMonitor:
    """Tracks data source health and bot liveness."""

    def __init__(
        self,
        state_path=None,
        staleness_minutes=60,
        auto_halt=False,
        logger=None,
        alert_cooldown_minutes=30,
        per_bot_halt_cooldown_seconds=600,
        source_breaker_threshold=5,
        source_breaker_cooldown_seconds=600,
        ignored_bot_names=None,
        *,
        atomic_write_json_func=atomic_write_json,
        utc_now_iso_func=_utc_now_iso,
        notify_webhook_func=None,
        notify_imessage_func=None,
        per_bot_halt_path_func=per_bot_halt_path,
        bot_source_map=None,
    ):
        self.state_path = Path(state_path) if state_path else HEALTH_STATE_PATH
        self.staleness_minutes = staleness_minutes
        self.auto_halt = auto_halt
        self.log = logger or _log
        self._alert_cooldown_minutes = alert_cooldown_minutes
        self._alerts_sent = {}
        self._per_bot_halt_cooldown = per_bot_halt_cooldown_seconds
        self._halt_transitions = {}
        self.source_breaker_threshold = source_breaker_threshold
        self.source_breaker_cooldown_seconds = source_breaker_cooldown_seconds
        self._atomic_write_json = atomic_write_json_func
        self._utc_now_iso = utc_now_iso_func
        self._notify_webhook = notify_webhook_func or (lambda *args, **kwargs: False)
        self._notify_imessage = notify_imessage_func or (lambda *args, **kwargs: False)
        self._per_bot_halt_path = per_bot_halt_path_func
        self._bot_source_map = BOT_SOURCE_MAP if bot_source_map is None else bot_source_map
        self._ignored_bot_names = (
            set(_load_ignored_bot_names())
            if ignored_bot_names is None
            else set(ignored_bot_names)
        )
        self._state = normalize_health_state(None)
        self._dirty_bots = set()
        self._dirty_sources = set()
        self._load()

    def _load(self):
        if self.state_path.exists():
            try:
                self._state = normalize_health_state(json.loads(self.state_path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        """Save state without clobbering unrelated bot/source entries."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_fd:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    on_disk = normalize_health_state(None)
                    if self.state_path.exists():
                        try:
                            on_disk = normalize_health_state(json.loads(self.state_path.read_text()))
                        except (json.JSONDecodeError, OSError):
                            pass
                    on_disk_bots = on_disk.setdefault("bots", {})
                    for bot in self._dirty_bots:
                        if bot in self._state.get("bots", {}):
                            on_disk_bots[bot] = self._state["bots"][bot]
                    on_disk_sources = on_disk.setdefault("sources", {})
                    for source in self._dirty_sources:
                        if source in self._state.get("sources", {}):
                            on_disk_sources[source] = self._state["sources"][source]
                    on_disk = normalize_health_state(on_disk)
                    self._atomic_write_json(self.state_path, on_disk)
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception as exc:
            self.log.warning("Failed to save health state: %s", exc)

    @staticmethod
    def _parse_state_time(value):
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed
        except (ValueError, TypeError, AttributeError):
            return None

    def _source_issue_active(self, data, threshold=1, now=None):
        error_count = data.get("error_count", 0)
        if error_count < threshold:
            return False
        opened_at = data.get("opened_at")
        if opened_at is not None:
            try:
                if (time.time() - float(opened_at)) < (self.source_breaker_cooldown_seconds * 2):
                    return True
            except (TypeError, ValueError):
                pass
        now = now or datetime.datetime.now(datetime.timezone.utc)
        last_error = self._parse_state_time(data.get("last_error"))
        if last_error is None:
            return False
        active_window = max(self.source_breaker_cooldown_seconds * 2, 6 * 3600)
        return (now - last_error).total_seconds() <= active_window

    def record_source_success(self, source):
        if source not in self._state["sources"]:
            self._state["sources"][source] = {
                "last_success": None,
                "last_error": None,
                "last_error_message": None,
                "error_count": 0,
            }
        self._state["sources"][source]["last_success"] = self._utc_now_iso()
        self._state["sources"][source]["error_count"] = 0
        self._state["sources"][source]["opened_at"] = None
        self._state["sources"][source]["last_error_message"] = None
        self._dirty_sources.add(source)
        self._save()

    def record_source_error(self, source, msg=""):
        if source not in self._state["sources"]:
            self._state["sources"][source] = {
                "last_success": None,
                "last_error": None,
                "last_error_message": None,
                "error_count": 0,
            }
        data = self._state["sources"][source]
        data["last_error"] = self._utc_now_iso()
        data["last_error_message"] = str(msg) if msg else None
        data["error_count"] = data.get("error_count", 0) + 1
        if data["error_count"] >= self.source_breaker_threshold and data.get("opened_at") is None:
            data["opened_at"] = time.time()
            self.log.warning("Source circuit breaker opened for %s after %d errors", source, data["error_count"])
            alert_msg = f"Source circuit breaker opened: {source} ({data['error_count']} consecutive errors)"
            self._notify_webhook(alert_msg, level="warning", logger=self.log)
            self._notify_imessage(alert_msg, logger=self.log)
        self._dirty_sources.add(source)
        self._save()

    def trip_source_breaker(self, source, msg="", error_count=None):
        if source not in self._state["sources"]:
            self._state["sources"][source] = {
                "last_success": None,
                "last_error": None,
                "last_error_message": None,
                "error_count": 0,
            }
        data = self._state["sources"][source]
        threshold = error_count if isinstance(error_count, int) and error_count > 0 else self.source_breaker_threshold
        was_open = data.get("error_count", 0) >= threshold and data.get("opened_at") is not None
        data["last_error"] = self._utc_now_iso()
        data["last_error_message"] = str(msg) if msg else None
        data["error_count"] = max(data.get("error_count", 0), threshold)
        data["opened_at"] = time.time()
        if not was_open:
            self.log.warning("Source circuit breaker opened immediately for %s", source)
            detail = f" ({msg})" if msg else ""
            alert_msg = f"Source circuit breaker opened: {source} (deterministic failure){detail}"
            self._notify_webhook(alert_msg, level="warning", logger=self.log)
            self._notify_imessage(alert_msg, logger=self.log)
        self._dirty_sources.add(source)
        self._save()

    def is_source_open(self, source):
        data = self._state.get("sources", {}).get(source, {})
        if data.get("error_count", 0) < self.source_breaker_threshold:
            return False
        opened_at = data.get("opened_at")
        if opened_at is None:
            return True
        elapsed = time.time() - opened_at
        if elapsed >= self.source_breaker_cooldown_seconds:
            data["error_count"] = 0
            data["opened_at"] = None
            self._dirty_sources.add(source)
            self._save()
            return False
        return True

    def record_bot_heartbeat(self, bot):
        self._state["bots"][bot] = {"last_heartbeat": self._utc_now_iso()}
        self._dirty_bots.add(bot)
        self._save()

    def should_send_alert(self, alert_key):
        if alert_key not in self._alerts_sent:
            return True
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - self._alerts_sent[alert_key]).total_seconds() / 60
        return elapsed >= self._alert_cooldown_minutes

    def record_alert_sent(self, alert_key):
        self._alerts_sent[alert_key] = datetime.datetime.now(datetime.timezone.utc)

    def get_summary(self):
        summary = {"sources": {}, "bots": {}, "overall": "healthy"}

        issues = 0
        now = datetime.datetime.now(datetime.timezone.utc)
        for source, data in self._state.get("sources", {}).items():
            error_count = data.get("error_count", 0)
            active_error = self._source_issue_active(data, threshold=self.source_breaker_threshold, now=now)
            active_warning = self._source_issue_active(data, threshold=1, now=now)
            status = "error" if active_error else ("warning" if active_warning else "ok")
            if status == "error":
                issues += 1
            summary["sources"][source] = {
                "status": status,
                "error_count": error_count,
                "last_success": data.get("last_success"),
                "last_error": data.get("last_error"),
                "last_error_message": data.get("last_error_message"),
            }

        for bot, data in self._state.get("bots", {}).items():
            if bot in self._ignored_bot_names:
                continue
            last_hb = data.get("last_heartbeat")
            stale = False
            if last_hb:
                try:
                    hb_dt = datetime.datetime.fromisoformat(last_hb)
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=datetime.timezone.utc)
                    age_min = (datetime.datetime.now(datetime.timezone.utc) - hb_dt).total_seconds() / 60
                    stale = age_min > self.staleness_minutes
                except (ValueError, TypeError):
                    pass
            status = "stale" if stale else "ok"
            if stale:
                issues += 1
            summary["bots"][bot] = {
                "status": status,
                "last_heartbeat": last_hb,
            }

        if issues >= 2:
            summary["overall"] = "critical"
        elif issues >= 1:
            summary["overall"] = "degraded"

        return normalize_health_summary(summary)

    def check_health(self, staleness_minutes=None):
        stale_min = staleness_minutes or self.staleness_minutes
        now = datetime.datetime.now(datetime.timezone.utc)
        issues = []

        for bot, info in self._state.get("bots", {}).items():
            if bot in self._ignored_bot_names:
                continue
            hb = info.get("last_heartbeat")
            if hb:
                try:
                    hb_dt = datetime.datetime.fromisoformat(hb)
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=datetime.timezone.utc)
                    age_min = (now - hb_dt).total_seconds() / 60
                    if age_min > stale_min:
                        issues.append(f"bot/{bot} stale: last heartbeat {age_min:.0f}min ago")
                except (ValueError, TypeError):
                    pass

        for source, info in self._state.get("sources", {}).items():
            error_count = info.get("error_count", 0)
            if self._source_issue_active(info, threshold=self.source_breaker_threshold, now=now):
                issues.append(f"source/{source} failing: {error_count} consecutive errors")

        if issues:
            critical = [issue for issue in issues if "stale" in issue or "failing" in issue]
            if critical:
                self._notify_webhook(
                    f"Health check: {'; '.join(critical[:3])}",
                    level="warning",
                )

        if self.auto_halt and issues:
            halt_status = self.check_per_bot_halts()
            for bot_name, action in halt_status.items():
                issues.append(f"PER-BOT-HALT: {bot_name} {action}")

        return issues

    def check_per_bot_halts(self):
        now = time.time()
        status = {}
        sources = self._state.get("sources", {})

        for bot_name, required_sources in self._bot_source_map.items():
            halt_path = self._per_bot_halt_path(bot_name)
            currently_halted = halt_path.exists()
            all_failing = bool(required_sources) and all(
                sources.get(source, {}).get("error_count", 0) >= 5
                for source in required_sources
            )
            all_recovered = all(
                sources.get(source, {}).get("error_count", 0) == 0
                for source in required_sources
            )
            last_transition = self._halt_transitions.get(bot_name, 0)
            cooldown_ok = (now - last_transition) >= self._per_bot_halt_cooldown

            if all_failing and not currently_halted and cooldown_ok:
                halt_path.parent.mkdir(parents=True, exist_ok=True)
                failing_sources = [source for source in required_sources if sources.get(source, {}).get("error_count", 0) >= 5]
                halt_path.write_text(f"Auto-halted: sources failing: {', '.join(failing_sources)}")
                self._halt_transitions[bot_name] = now
                self.log.warning("PER-BOT HALT created for %s (sources: %s)", bot_name, ", ".join(failing_sources))
                status[bot_name] = "halted"
            elif all_recovered and currently_halted and cooldown_ok:
                halt_path.unlink(missing_ok=True)
                self._halt_transitions[bot_name] = now
                self.log.info("PER-BOT HALT removed for %s (sources recovered)", bot_name)
                status[bot_name] = "recovered"
            else:
                status[bot_name] = "unchanged"

        return status


__all__ = [
    "BOT_SOURCE_MAP",
    "HEALTH_STATE_PATH",
    "HealthCheckMonitor",
]
