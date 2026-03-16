"""Canonical opportunity log artifact helpers."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from research.registry import annotate_research_record
from storage import MetricsStore


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_OPPORTUNITY_LOG_PATH = PROJECT_DIR / "data" / "opportunity-log.json"

_log = logging.getLogger("opportunity-log")


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class OpportunityLog:
    """Append-only canonical log for skipped and pruned opportunities."""

    def __init__(
        self,
        path=DEFAULT_OPPORTUNITY_LOG_PATH,
        logger=None,
        metrics_store_cls=MetricsStore,
        utc_now_iso_func=_utc_now_iso,
        strategy_id=None,
        config_version=None,
        model_registry=None,
        source_bot=None,
        source_path=None,
    ):
        self.path = Path(path)
        self.log = logger or _log
        self._utc_now_iso = utc_now_iso_func
        self._strategy_id = strategy_id
        self._config_version = config_version
        self._model_registry = model_registry
        self._source_bot = source_bot
        self._source_path = Path(source_path) if source_path is not None else self.path
        self._store = metrics_store_cls(self.path, logger=self.log, max_records=20000, trim_to=15000)

    def record(self, record):
        normalized = dict(record)
        normalized.setdefault("timestamp", self._utc_now_iso())
        normalized.setdefault("artifact", "opportunity_log")
        annotated = annotate_research_record(
            normalized,
            strategy_id=self._strategy_id,
            config_version=self._config_version,
            model_registry=self._model_registry,
            source_bot=self._source_bot,
            source_path=self._source_path,
            logger=self.log,
        )
        self._store.append(annotated)
        return annotated

    def load(self):
        return self._store.load()


__all__ = [
    "DEFAULT_OPPORTUNITY_LOG_PATH",
    "OpportunityLog",
]
