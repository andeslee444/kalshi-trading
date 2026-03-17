#!/usr/bin/env python3
"""Audited incident review and follow-up workflow CLI."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from research.incident_registry import DEFAULT_INCIDENT_REVIEWS_PATH, IncidentRegistry, IncidentRegistryError


def _load_metadata(raw):
    if not raw:
        return None
    return json.loads(raw)


def _print_entry(entry):
    print(json.dumps(entry, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description="Incident review and follow-up workflow")
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_INCIDENT_REVIEWS_PATH,
        help="Path to incident-reviews.json",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="Print one incident entry")
    show_parser.add_argument("incident_id")

    open_parser = subparsers.add_parser("open", help="Open or update an incident review entry")
    open_parser.add_argument("incident_id")
    open_parser.add_argument("--summary", required=True)
    open_parser.add_argument("--severity", required=True)
    open_parser.add_argument("--owner")
    open_parser.add_argument("--status", default="open")
    open_parser.add_argument("--service", action="append", dest="services")
    open_parser.add_argument("--source", action="append", dest="sources")
    open_parser.add_argument("--experiment-id", action="append", dest="experiment_ids")
    open_parser.add_argument("--config-version", action="append", dest="config_versions")
    open_parser.add_argument("--model-version", action="append", dest="model_versions")
    open_parser.add_argument("--pr-number", action="append", type=int, dest="pr_numbers")
    open_parser.add_argument("--change-ref", action="append", dest="change_refs")
    open_parser.add_argument("--note")
    open_parser.add_argument("--metadata-json")

    link_parser = subparsers.add_parser("link", help="Link follow-up code/config changes to an incident")
    link_parser.add_argument("incident_id")
    link_parser.add_argument("--actor")
    link_parser.add_argument("--status")
    link_parser.add_argument("--experiment-id", action="append", dest="experiment_ids")
    link_parser.add_argument("--config-version", action="append", dest="config_versions")
    link_parser.add_argument("--model-version", action="append", dest="model_versions")
    link_parser.add_argument("--pr-number", action="append", type=int, dest="pr_numbers")
    link_parser.add_argument("--change-ref", action="append", dest="change_refs")
    link_parser.add_argument("--note")
    link_parser.add_argument("--metadata-json")

    close_parser = subparsers.add_parser("close", help="Close an incident and record the final follow-up linkage")
    close_parser.add_argument("incident_id")
    close_parser.add_argument("--actor")
    close_parser.add_argument("--status", default="closed")
    close_parser.add_argument("--resolution")
    close_parser.add_argument("--experiment-id", action="append", dest="experiment_ids")
    close_parser.add_argument("--config-version", action="append", dest="config_versions")
    close_parser.add_argument("--model-version", action="append", dest="model_versions")
    close_parser.add_argument("--pr-number", action="append", type=int, dest="pr_numbers")
    close_parser.add_argument("--change-ref", action="append", dest="change_refs")
    close_parser.add_argument("--metadata-json")

    args = parser.parse_args()
    registry = IncidentRegistry(args.path)

    try:
        if args.command == "show":
            entry = registry.get(args.incident_id)
            if entry is None:
                raise IncidentRegistryError(f"Unknown incident_id: {args.incident_id}")
        elif args.command == "open":
            entry = registry.open(
                args.incident_id,
                summary=args.summary,
                severity=args.severity,
                owner=args.owner,
                status=args.status,
                affected_services=args.services,
                affected_sources=args.sources,
                experiment_ids=args.experiment_ids,
                config_versions=args.config_versions,
                model_versions=args.model_versions,
                pr_numbers=args.pr_numbers,
                change_refs=args.change_refs,
                note=args.note,
                metadata=_load_metadata(args.metadata_json),
                artifact_path=Path(__file__).resolve(),
            )
        elif args.command == "link":
            entry = registry.link(
                args.incident_id,
                actor=args.actor,
                status=args.status,
                experiment_ids=args.experiment_ids,
                config_versions=args.config_versions,
                model_versions=args.model_versions,
                pr_numbers=args.pr_numbers,
                change_refs=args.change_refs,
                note=args.note,
                metadata=_load_metadata(args.metadata_json),
                artifact_path=Path(__file__).resolve(),
            )
        else:
            entry = registry.close(
                args.incident_id,
                actor=args.actor,
                status=args.status,
                resolution=args.resolution,
                experiment_ids=args.experiment_ids,
                config_versions=args.config_versions,
                model_versions=args.model_versions,
                pr_numbers=args.pr_numbers,
                change_refs=args.change_refs,
                metadata=_load_metadata(args.metadata_json),
                artifact_path=Path(__file__).resolve(),
            )
    except (IncidentRegistryError, json.JSONDecodeError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)

    _print_entry(entry)


if __name__ == "__main__":
    main()
