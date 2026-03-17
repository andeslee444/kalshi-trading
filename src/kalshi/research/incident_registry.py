"""Canonical incident review and follow-up registry helpers."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from artifact_contracts import normalize_incident_reviews
from storage import StateStore


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_INCIDENT_REVIEWS_PATH = PROJECT_DIR / "data" / "incident-reviews.json"

_log = logging.getLogger("incident-registry")


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _coerce_path(path):
    if path is None:
        return None
    return str(Path(path))


def _coerce_metadata(metadata):
    return dict(metadata) if isinstance(metadata, dict) else None


def _merge_unique_strings(existing, new_values):
    merged = []
    seen = set()
    for value in list(existing or []) + list(new_values or []):
        if value is None:
            continue
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def _merge_unique_ints(existing, new_values):
    merged = []
    seen = set()
    for value in list(existing or []) + list(new_values or []):
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            continue
        if normalized < 0 or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


class IncidentRegistryError(ValueError):
    """Raised when an incident registry operation is invalid."""


class IncidentRegistry:
    """Canonical registry for incidents and linked follow-up changes."""

    def __init__(self, path=DEFAULT_INCIDENT_REVIEWS_PATH, logger=None, state_store_cls=StateStore, utc_now_iso_func=_utc_now_iso):
        self.path = Path(path)
        self.log = logger or _log
        self._utc_now_iso = utc_now_iso_func
        self._store = state_store_cls(
            self.path,
            logger=self.log,
            normalizer=normalize_incident_reviews,
        )

    def load(self):
        return self._store.load(default={})

    def get(self, incident_id):
        state = self.load()
        entry = state.get("entries", {}).get(incident_id)
        return dict(entry) if isinstance(entry, dict) else None

    def open(
        self,
        incident_id,
        *,
        summary,
        severity,
        owner=None,
        status="open",
        affected_services=None,
        affected_sources=None,
        experiment_ids=None,
        config_versions=None,
        model_versions=None,
        pr_numbers=None,
        change_refs=None,
        note=None,
        metadata=None,
        artifact_path=None,
        event_at=None,
    ):
        if not summary:
            raise IncidentRegistryError("summary is required when opening an incident")
        if not severity:
            raise IncidentRegistryError("severity is required when opening an incident")
        return self._record(
            incident_id,
            event_type="opened",
            summary=summary,
            severity=severity,
            owner=owner,
            status=status,
            affected_services=affected_services,
            affected_sources=affected_sources,
            experiment_ids=experiment_ids,
            config_versions=config_versions,
            model_versions=model_versions,
            pr_numbers=pr_numbers,
            change_refs=change_refs,
            note=note,
            metadata=metadata,
            artifact_path=artifact_path,
            event_at=event_at,
        )

    def link(
        self,
        incident_id,
        *,
        actor=None,
        status=None,
        experiment_ids=None,
        config_versions=None,
        model_versions=None,
        pr_numbers=None,
        change_refs=None,
        note=None,
        metadata=None,
        artifact_path=None,
        event_at=None,
    ):
        return self._record(
            incident_id,
            event_type="linked_follow_up",
            actor=actor,
            status=status,
            experiment_ids=experiment_ids,
            config_versions=config_versions,
            model_versions=model_versions,
            pr_numbers=pr_numbers,
            change_refs=change_refs,
            note=note,
            metadata=metadata,
            artifact_path=artifact_path,
            event_at=event_at,
            require_existing=True,
        )

    def close(
        self,
        incident_id,
        *,
        actor=None,
        status="closed",
        resolution=None,
        experiment_ids=None,
        config_versions=None,
        model_versions=None,
        pr_numbers=None,
        change_refs=None,
        metadata=None,
        artifact_path=None,
        event_at=None,
    ):
        return self._record(
            incident_id,
            event_type="closed",
            actor=actor,
            status=status,
            resolution=resolution,
            experiment_ids=experiment_ids,
            config_versions=config_versions,
            model_versions=model_versions,
            pr_numbers=pr_numbers,
            change_refs=change_refs,
            metadata=metadata,
            artifact_path=artifact_path,
            event_at=event_at,
            require_existing=True,
        )

    def _record(
        self,
        incident_id,
        *,
        event_type,
        summary=None,
        severity=None,
        owner=None,
        actor=None,
        status=None,
        resolution=None,
        affected_services=None,
        affected_sources=None,
        experiment_ids=None,
        config_versions=None,
        model_versions=None,
        pr_numbers=None,
        change_refs=None,
        note=None,
        metadata=None,
        artifact_path=None,
        event_at=None,
        require_existing=False,
    ):
        incident_id = str(incident_id or "").strip()
        if not incident_id:
            raise IncidentRegistryError("incident_id is required")

        event_at = event_at or self._utc_now_iso()
        metadata_payload = _coerce_metadata(metadata)
        artifact_path_str = _coerce_path(artifact_path)

        def updater(state):
            entries = state.setdefault("entries", {})
            current = dict(entries.get(incident_id, {}))
            if require_existing and not current:
                raise IncidentRegistryError(f"Unknown incident_id: {incident_id}")

            history = list(current.get("history", [])) if isinstance(current.get("history"), list) else []
            artifact_paths = _merge_unique_strings(current.get("artifact_paths", []), [artifact_path_str])
            affected_services_merged = _merge_unique_strings(current.get("affected_services", []), affected_services)
            affected_sources_merged = _merge_unique_strings(current.get("affected_sources", []), affected_sources)
            experiment_ids_merged = _merge_unique_strings(current.get("experiment_ids", []), experiment_ids)
            config_versions_merged = _merge_unique_strings(current.get("config_versions", []), config_versions)
            model_versions_merged = _merge_unique_strings(current.get("model_versions", []), model_versions)
            pr_numbers_merged = _merge_unique_ints(current.get("pr_numbers", []), pr_numbers)
            change_refs_merged = _merge_unique_strings(current.get("change_refs", []), change_refs)

            entry = {
                "incident_id": incident_id,
                "summary": summary if summary is not None else current.get("summary"),
                "severity": severity if severity is not None else current.get("severity"),
                "owner": owner if owner is not None else current.get("owner"),
                "status": status if status is not None else current.get("status") or "open",
                "opened_at": current.get("opened_at") or event_at,
                "updated_at": event_at,
                "affected_services": affected_services_merged,
                "affected_sources": affected_sources_merged,
                "experiment_ids": experiment_ids_merged,
                "config_versions": config_versions_merged,
                "model_versions": model_versions_merged,
                "pr_numbers": pr_numbers_merged,
                "change_refs": change_refs_merged,
                "artifact_paths": artifact_paths,
            }
            if resolution is not None:
                entry["resolution"] = resolution
            elif "resolution" in current:
                entry["resolution"] = current["resolution"]

            if entry["status"] == "closed" or event_type == "closed":
                entry["closed_at"] = event_at
            elif "closed_at" in current:
                entry["closed_at"] = current["closed_at"]

            merged_metadata = dict(current.get("metadata", {})) if isinstance(current.get("metadata"), dict) else {}
            if metadata_payload:
                merged_metadata.update(metadata_payload)
            if merged_metadata:
                entry["metadata"] = merged_metadata

            history_event = {
                "event_type": event_type,
                "event_at": event_at,
            }
            if actor:
                history_event["actor"] = actor
            if note:
                history_event["note"] = note
            if resolution is not None:
                history_event["resolution"] = resolution
            if status is not None:
                history_event["status"] = status
            if summary is not None:
                history_event["summary"] = summary
            if severity is not None:
                history_event["severity"] = severity
            if owner is not None:
                history_event["owner"] = owner
            if affected_services:
                history_event["affected_services"] = _merge_unique_strings([], affected_services)
            if affected_sources:
                history_event["affected_sources"] = _merge_unique_strings([], affected_sources)
            if experiment_ids:
                history_event["experiment_ids"] = _merge_unique_strings([], experiment_ids)
            if config_versions:
                history_event["config_versions"] = _merge_unique_strings([], config_versions)
            if model_versions:
                history_event["model_versions"] = _merge_unique_strings([], model_versions)
            if pr_numbers:
                history_event["pr_numbers"] = _merge_unique_ints([], pr_numbers)
            if change_refs:
                history_event["change_refs"] = _merge_unique_strings([], change_refs)
            if artifact_path_str is not None:
                history_event["artifact_path"] = artifact_path_str
            if metadata_payload:
                history_event["metadata"] = metadata_payload
            history.append(history_event)
            entry["history"] = history

            entries[incident_id] = entry
            return state

        state = self._store.update(updater, default={})
        return dict(state["entries"][incident_id])


__all__ = [
    "DEFAULT_INCIDENT_REVIEWS_PATH",
    "IncidentRegistry",
    "IncidentRegistryError",
]
