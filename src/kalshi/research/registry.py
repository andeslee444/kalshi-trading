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
]
