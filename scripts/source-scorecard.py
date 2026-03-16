#!/usr/bin/env python3
"""Canonical source scorecard report backed by source_catalog."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from event_ledger import DEFAULT_LEDGER_PATH
from ops.health_monitor import HEALTH_STATE_PATH
from research.source_catalog import DEFAULT_SOURCE_CATALOG_PATH, SourceCatalog


def main():
    parser = argparse.ArgumentParser(description="Build a source scorecard from health state and source observations")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON instead of text")
    parser.add_argument("--save", action="store_true", help="Save the report to data/source-catalog.json")
    parser.add_argument("--health-state", type=str, default=str(HEALTH_STATE_PATH), help="health-state.json path")
    parser.add_argument("--ledger", type=str, default=str(DEFAULT_LEDGER_PATH), help="event-ledger.sqlite3 path")
    args = parser.parse_args()

    catalog = SourceCatalog(health_state_path=args.health_state, ledger_path=args.ledger)
    catalog.load()
    report = catalog.build()

    if args.json_output:
        print(json.dumps(report, indent=2))
    else:
        print(catalog.summary_report())

    if args.save:
        DEFAULT_SOURCE_CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(str(DEFAULT_SOURCE_CATALOG_PATH) + ".tmp", "w") as handle:
            json.dump(report, handle, indent=2)
        os.replace(str(DEFAULT_SOURCE_CATALOG_PATH) + ".tmp", str(DEFAULT_SOURCE_CATALOG_PATH))


if __name__ == "__main__":
    main()
