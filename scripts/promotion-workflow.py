#!/usr/bin/env python3
"""Audited promotion workflow CLI for experiment_runs."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from research.promotion_workflow import PromotionWorkflow, PromotionWorkflowError
from research.registry import DEFAULT_EXPERIMENT_RUNS_PATH, ExperimentRunRegistry


def _load_metadata(raw):
    if not raw:
        return None
    return json.loads(raw)


def _print_entry(entry):
    print(json.dumps(entry, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="Experiment promotion workflow")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_EXPERIMENT_RUNS_PATH,
        help="Path to experiment-runs.json",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="Print one experiment entry")
    show_parser.add_argument("experiment_id")

    promote_parser = subparsers.add_parser("promote", help="Advance one promotion stage")
    promote_parser.add_argument("experiment_id")
    promote_parser.add_argument("--to", required=True, choices=["shadow", "capped_live", "live"])
    promote_parser.add_argument("--actor")
    promote_parser.add_argument("--note")
    promote_parser.add_argument("--status")
    promote_parser.add_argument("--incident-id")
    promote_parser.add_argument("--config-version")
    promote_parser.add_argument("--model-version")
    promote_parser.add_argument("--pr-number", type=int)
    promote_parser.add_argument("--change-ref")
    promote_parser.add_argument("--metadata-json")

    rollback_parser = subparsers.add_parser("rollback", help="Move an experiment back to an earlier stage")
    rollback_parser.add_argument("experiment_id")
    rollback_parser.add_argument("--to", default="research", choices=["research", "shadow", "capped_live"])
    rollback_parser.add_argument("--actor")
    rollback_parser.add_argument("--reason")
    rollback_parser.add_argument("--status", default="rolled_back")
    rollback_parser.add_argument("--incident-id")
    rollback_parser.add_argument("--config-version")
    rollback_parser.add_argument("--model-version")
    rollback_parser.add_argument("--pr-number", type=int)
    rollback_parser.add_argument("--change-ref")
    rollback_parser.add_argument("--metadata-json")

    args = parser.parse_args()

    registry = ExperimentRunRegistry(args.path)
    workflow = PromotionWorkflow(
        experiment_registry=registry,
        source_path=Path(__file__).resolve(),
    )

    try:
        if args.command == "show":
            entry = workflow.get(args.experiment_id)
            if entry is None:
                raise PromotionWorkflowError(f"Unknown experiment_id: {args.experiment_id}")
        elif args.command == "promote":
            entry = workflow.promote(
                args.experiment_id,
                target_stage=args.to,
                actor=args.actor,
                note=args.note,
                status=args.status,
                incident_id=args.incident_id,
                config_version=args.config_version,
                model_version=args.model_version,
                pr_number=args.pr_number,
                change_ref=args.change_ref,
                metadata=_load_metadata(args.metadata_json),
            )
        else:
            entry = workflow.rollback(
                args.experiment_id,
                target_stage=args.to,
                actor=args.actor,
                reason=args.reason,
                status=args.status,
                incident_id=args.incident_id,
                config_version=args.config_version,
                model_version=args.model_version,
                pr_number=args.pr_number,
                change_ref=args.change_ref,
                metadata=_load_metadata(args.metadata_json),
            )
    except (PromotionWorkflowError, json.JSONDecodeError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)

    _print_entry(entry)


if __name__ == "__main__":
    main()
