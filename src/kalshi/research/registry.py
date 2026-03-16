"""Phase 7 registry helpers for model and strategy provenance."""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from pathlib import Path

from artifact_contracts import (
    normalize_model_registry,
    normalize_strategy_config_registry,
)
from storage import StateStore


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_MODEL_REGISTRY_PATH = PROJECT_DIR / "data" / "model-registry.json"
DEFAULT_STRATEGY_CONFIG_REGISTRY_PATH = PROJECT_DIR / "data" / "strategy-config-registry.json"

_log = logging.getLogger("research-registry")


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _stable_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def _coerce_path(path):
    if path is None:
        return None
    return str(Path(path))


def _resolve_model_version(record, *, strategy_id=None, model_registry=None, source_bot=None, source_path=None, logger=None):
    model_version = record.get("model_version")
    if model_version:
        return model_version

    model_name = record.get("model_name") or record.get("sizing_method")
    if not model_name or model_registry is None:
        return None

    descriptor = {
        "strategy_id": strategy_id,
        "model_name": model_name,
    }
    for key in ("model_family", "model_type", "sizing_method"):
        value = record.get(key)
        if value is not None:
            descriptor[key] = value
    custom_descriptor = record.get("model_descriptor")
    if isinstance(custom_descriptor, dict):
        descriptor.update(custom_descriptor)

    try:
        entry = model_registry.register(
            model_name,
            descriptor,
            strategy_id=strategy_id,
            source_bot=source_bot or record.get("source_bot"),
            source_path=source_path,
        )
        return entry.get("model_version")
    except Exception as e:
        (logger or _log).warning("Failed to register model metadata for %s: %s", model_name, e)
        return None


def annotate_research_record(record, *, strategy_id=None, config_version=None, model_registry=None, source_bot=None, source_path=None, logger=None):
    """Apply shared provenance metadata to a persisted artifact record."""
    normalized = dict(record)
    if normalized.get("strategy_id") is None and strategy_id is not None:
        normalized["strategy_id"] = strategy_id
    if normalized.get("config_version") is None and config_version is not None:
        normalized["config_version"] = config_version
    if normalized.get("source_bot") is None and source_bot is not None:
        normalized["source_bot"] = source_bot
    if normalized.get("model_name") is None and normalized.get("sizing_method") is not None:
        normalized["model_name"] = normalized.get("sizing_method")
    if normalized.get("feature_snapshot_id") is None and normalized.get("model_inputs") is None:
        inline_model_inputs = normalized.get("inline_model_inputs")
        if inline_model_inputs is not None:
            normalized["model_inputs"] = inline_model_inputs

    model_version = _resolve_model_version(
        normalized,
        strategy_id=normalized.get("strategy_id"),
        model_registry=model_registry,
        source_bot=normalized.get("source_bot"),
        source_path=source_path,
        logger=logger,
    )
    if model_version is not None:
        normalized["model_version"] = model_version
    return normalized


class StrategyConfigRegistry:
    """Canonical registry for strategy/bot config snapshots."""

    def __init__(self, path=DEFAULT_STRATEGY_CONFIG_REGISTRY_PATH, logger=None, state_store_cls=StateStore, utc_now_iso_func=_utc_now_iso):
        self.path = Path(path)
        self.log = logger or _log
        self._utc_now_iso = utc_now_iso_func
        self._store = state_store_cls(
            self.path,
            logger=self.log,
            normalizer=normalize_strategy_config_registry,
        )

    def register(self, strategy_id, config, *, source_bot=None, source_path=None, metadata=None):
        strategy_id = strategy_id or "unknown"
        config_payload = dict(config) if isinstance(config, dict) else {}
        config_version = _stable_hash({"strategy_id": strategy_id, "config": config_payload})
        entry_key = f"{strategy_id}:{config_version}"
        metadata_payload = dict(metadata) if isinstance(metadata, dict) else None
        source_path_str = _coerce_path(source_path)

        def updater(state):
            entries = state.setdefault("entries", {})
            current = dict(entries.get(entry_key, {}))
            entry = {
                "strategy_id": strategy_id,
                "config_version": config_version,
                "source_bot": source_bot,
                "source_path": source_path_str,
                "registered_at": current.get("registered_at") or self._utc_now_iso(),
                "config": config_payload,
            }
            if metadata_payload:
                entry["metadata"] = metadata_payload
            elif "metadata" in current:
                entry["metadata"] = current["metadata"]
            entries[entry_key] = entry
            return state

        state = self._store.update(updater, default={})
        return dict(state["entries"][entry_key])


class ModelRegistry:
    """Canonical registry for model descriptors referenced by trades and decisions."""

    def __init__(self, path=DEFAULT_MODEL_REGISTRY_PATH, logger=None, state_store_cls=StateStore, utc_now_iso_func=_utc_now_iso):
        self.path = Path(path)
        self.log = logger or _log
        self._utc_now_iso = utc_now_iso_func
        self._store = state_store_cls(
            self.path,
            logger=self.log,
            normalizer=normalize_model_registry,
        )

    def register(self, model_name, descriptor=None, *, strategy_id=None, source_bot=None, source_path=None, metadata=None):
        descriptor_payload = dict(descriptor) if isinstance(descriptor, dict) else {}
        source_path_str = _coerce_path(source_path)
        metadata_payload = dict(metadata) if isinstance(metadata, dict) else None
        version_payload = {
            "model_name": model_name,
            "strategy_id": strategy_id,
            "descriptor": descriptor_payload,
        }
        model_version = _stable_hash(version_payload)
        entry_key = f"{model_name}:{model_version}"

        def updater(state):
            entries = state.setdefault("entries", {})
            current = dict(entries.get(entry_key, {}))
            entry = {
                "model_name": model_name,
                "model_version": model_version,
                "strategy_id": strategy_id,
                "source_bot": source_bot,
                "source_path": source_path_str,
                "registered_at": current.get("registered_at") or self._utc_now_iso(),
                "descriptor": descriptor_payload,
            }
            if metadata_payload:
                entry["metadata"] = metadata_payload
            elif "metadata" in current:
                entry["metadata"] = current["metadata"]
            entries[entry_key] = entry
            return state

        state = self._store.update(updater, default={})
        return dict(state["entries"][entry_key])


__all__ = [
    "DEFAULT_MODEL_REGISTRY_PATH",
    "DEFAULT_STRATEGY_CONFIG_REGISTRY_PATH",
    "ModelRegistry",
    "StrategyConfigRegistry",
    "annotate_research_record",
]
