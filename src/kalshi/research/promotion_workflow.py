"""Audited promotion-stage workflow helpers for experiment runs."""

from __future__ import annotations

import datetime
from pathlib import Path

from research.registry import ExperimentRunRegistry


PROMOTION_STAGES = ("research", "shadow", "capped_live", "live")
_PROMOTION_STAGE_ORDER = {stage: idx for idx, stage in enumerate(PROMOTION_STAGES)}


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalize_stage(stage):
    if stage is None:
        return None
    normalized = str(stage).strip().lower().replace("-", "_")
    return normalized or None


def _coerce_metadata(metadata):
    return dict(metadata) if isinstance(metadata, dict) else {}


class PromotionWorkflowError(ValueError):
    """Raised when a requested experiment promotion transition is invalid."""


class PromotionWorkflow:
    """Manage explicit research -> shadow -> capped_live -> live transitions."""

    def __init__(
        self,
        *,
        experiment_registry=None,
        source_bot="promotion-workflow",
        source_path=None,
        utc_now_iso_func=_utc_now_iso,
    ):
        self._registry = experiment_registry or ExperimentRunRegistry()
        self._source_bot = source_bot
        self._source_path = Path(source_path) if source_path is not None else Path(__file__)
        self._utc_now_iso = utc_now_iso_func

    def get(self, experiment_id):
        return self._registry.get(experiment_id)

    def promote(
        self,
        experiment_id,
        *,
        target_stage,
        actor=None,
        note=None,
        status=None,
        metadata=None,
        artifact_path=None,
        event_at=None,
    ):
        entry = self._require_entry(experiment_id)
        current_stage = _normalize_stage(entry.get("promotion_stage"))
        target_stage = self._require_stage(target_stage)
        self._validate_forward_transition(current_stage, target_stage)

        event_at = event_at or self._utc_now_iso()
        event_metadata = self._event_metadata(
            current_stage,
            actor=actor,
            note=note,
            metadata=metadata,
        )

        return self._registry.register(
            experiment_id,
            entry.get("experiment_type"),
            strategy_id=entry.get("strategy_id"),
            source_bot=self._source_bot,
            source_path=self._source_path,
            status=status or target_stage,
            promotion_stage=target_stage,
            metadata=event_metadata,
            artifact_path=artifact_path,
            event_type=f"promoted_to_{target_stage}",
            event_at=event_at,
        )

    def rollback(
        self,
        experiment_id,
        *,
        target_stage="research",
        actor=None,
        reason=None,
        status="rolled_back",
        metadata=None,
        artifact_path=None,
        event_at=None,
    ):
        entry = self._require_entry(experiment_id)
        current_stage = _normalize_stage(entry.get("promotion_stage"))
        target_stage = self._require_stage(target_stage)
        self._validate_rollback_transition(current_stage, target_stage)

        event_at = event_at or self._utc_now_iso()
        event_metadata = self._event_metadata(
            current_stage,
            actor=actor,
            note=reason,
            metadata=metadata,
            note_key="reason",
        )

        return self._registry.register(
            experiment_id,
            entry.get("experiment_type"),
            strategy_id=entry.get("strategy_id"),
            source_bot=self._source_bot,
            source_path=self._source_path,
            status=status,
            promotion_stage=target_stage,
            metadata=event_metadata,
            artifact_path=artifact_path,
            event_type=f"rolled_back_to_{target_stage}",
            event_at=event_at,
        )

    def _require_entry(self, experiment_id):
        entry = self._registry.get(experiment_id)
        if entry is None:
            raise PromotionWorkflowError(f"Unknown experiment_id: {experiment_id}")
        return entry

    @staticmethod
    def _require_stage(stage):
        normalized = _normalize_stage(stage)
        if normalized not in _PROMOTION_STAGE_ORDER:
            raise PromotionWorkflowError(f"Unsupported promotion stage: {stage}")
        return normalized

    @staticmethod
    def _validate_forward_transition(current_stage, target_stage):
        if current_stage is None:
            raise PromotionWorkflowError("Experiment is missing a current promotion_stage")
        current_rank = _PROMOTION_STAGE_ORDER[current_stage]
        target_rank = _PROMOTION_STAGE_ORDER[target_stage]
        if target_rank <= current_rank:
            raise PromotionWorkflowError(
                f"Forward promotion must advance stage: {current_stage} -> {target_stage}"
            )
        if target_rank != current_rank + 1:
            raise PromotionWorkflowError(
                f"Skipped promotion stages are not allowed: {current_stage} -> {target_stage}"
            )

    @staticmethod
    def _validate_rollback_transition(current_stage, target_stage):
        if current_stage is None:
            raise PromotionWorkflowError("Experiment is missing a current promotion_stage")
        current_rank = _PROMOTION_STAGE_ORDER[current_stage]
        target_rank = _PROMOTION_STAGE_ORDER[target_stage]
        if target_rank >= current_rank:
            raise PromotionWorkflowError(
                f"Rollback must move to an earlier stage: {current_stage} -> {target_stage}"
            )

    @staticmethod
    def _event_metadata(current_stage, *, actor=None, note=None, metadata=None, note_key="note"):
        payload = _coerce_metadata(metadata)
        if current_stage is not None:
            payload["from_stage"] = current_stage
        if actor:
            payload["actor"] = actor
        if note:
            payload[note_key] = note
        return payload or None


__all__ = [
    "PROMOTION_STAGES",
    "PromotionWorkflow",
    "PromotionWorkflowError",
]
