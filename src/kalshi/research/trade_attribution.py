"""Canonical post-trade attribution artifact helpers."""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

from artifact_contracts import normalize_trade_attribution
from storage import StateStore


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_TRADE_ATTRIBUTION_PATH = PROJECT_DIR / "data" / "attribution-report.json"

_log = logging.getLogger("trade-attribution")


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_trade_attribution_safe(filepath):
    """Load a normalized trade-attribution artifact or an empty payload."""
    try:
        path = Path(filepath)
        if not path.exists():
            return normalize_trade_attribution(None)
        text = path.read_text().strip()
        if not text:
            return normalize_trade_attribution(None)
        return normalize_trade_attribution(json.loads(text))
    except (json.JSONDecodeError, ValueError, OSError):
        return normalize_trade_attribution(None)


class TradeAttributionArtifact:
    """Persist the canonical post-trade attribution snapshot."""

    def __init__(
        self,
        path=DEFAULT_TRADE_ATTRIBUTION_PATH,
        logger=None,
        state_store_cls=StateStore,
        utc_now_iso_func=_utc_now_iso,
    ):
        self.path = Path(path)
        self.log = logger or _log
        self._utc_now_iso = utc_now_iso_func
        self._store = state_store_cls(
            self.path,
            logger=self.log,
            normalizer=normalize_trade_attribution,
        )

    def save(self, report, *, report_name="daily_attribution"):
        payload = dict(report) if isinstance(report, dict) else {}
        payload.setdefault("generated_at", self._utc_now_iso())
        payload["report_name"] = payload.get("report_name") or report_name
        return self._store.save(payload)

    def load(self):
        return self._store.load(default={})


__all__ = [
    "DEFAULT_TRADE_ATTRIBUTION_PATH",
    "TradeAttributionArtifact",
    "_load_trade_attribution_safe",
]
