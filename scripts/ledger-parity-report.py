#!/usr/bin/env python3
"""Compare legacy JSON artifacts against the Phase 4 SQLite ledger views."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from bot_registry import DECISION_FILE_SPECS
from event_ledger import DEFAULT_LEDGER_PATH, get_event_ledger
from trade_files import TRADE_FILES as CANONICAL_TRADE_FILES

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"


def _trade_specs():
    return [
        {"bot": spec["bot"], "path": DATA_DIR / spec["filename"]}
        for spec in CANONICAL_TRADE_FILES
    ]


def _decision_specs():
    return [
        {"bot": spec["bot"], "path": DATA_DIR / spec["filename"]}
        for spec in DECISION_FILE_SPECS
    ]


def _verification_specs():
    return [
        {
            "path": DATA_DIR / "weather-verification.json",
            "category": "weather_verification",
        },
        {
            "path": DATA_DIR / "weather-nws-cross-check.json",
            "category": "weather_nws_crosscheck",
        },
    ]


def _overall_ok(report):
    rows = list(report.get("trade_logs", [])) + list(report.get("decision_logs", []))
    rows.extend(report.get("verification", []))
    for row in rows:
        if not row:
            continue
        if row.get("comparison_status") == "no_overlap":
            continue
        if "count_match" in row and not row["count_match"]:
            return False
        if "hash_match" in row and not row["hash_match"]:
            return False
        if "verified_hash_match" in row and not row["verified_hash_match"]:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Build a legacy-vs-ledger parity report")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout")
    parser.add_argument("--save", action="store_true", help="Save to data/ledger-parity-report.json")
    parser.add_argument(
        "--ledger-path",
        default=str(DEFAULT_LEDGER_PATH),
        help="Path to event-ledger.sqlite3",
    )
    args = parser.parse_args()

    ledger = get_event_ledger(path=args.ledger_path)
    report = ledger.build_parity_report(
        trade_specs=_trade_specs(),
        decision_specs=_decision_specs(),
        verification_specs=_verification_specs(),
    )
    report["overall_ok"] = _overall_ok(report)
    ledger.save_parity_report(report, scope="phase4")

    if args.save:
        out_path = DATA_DIR / "ledger-parity-report.json"
        out_path.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Saved parity report to {out_path}", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    print(f"overall_ok: {report['overall_ok']}")
    for section in ("trade_logs", "decision_logs", "verification"):
        print(f"[{section}]")
        for row in report.get(section, []):
            path = row.get("path")
            mode = row.get("comparison_mode")
            status = row.get("comparison_status")
            if section == "verification":
                print(
                    f"  {path}: mode={mode} status={status} "
                    f"legacy_verified={row.get('legacy_verified_count')}/"
                    f"{row.get('legacy_verified_total_count')} "
                    f"ledger_verified={row.get('ledger_verified_count')}/"
                    f"{row.get('ledger_verified_total_count')} "
                    f"hash_match={row.get('verified_hash_match')}"
                )
            else:
                extra = row.get("ledger_extra_count")
                extra_str = f" extra={extra}" if extra is not None else ""
                print(
                    f"  {path}: mode={mode} status={status} "
                    f"legacy={row.get('legacy_count')}/{row.get('legacy_total_count')} "
                    f"ledger={row.get('ledger_count')}/{row.get('ledger_total_count')} "
                    f"hash_match={row.get('hash_match')}{extra_str}"
                )


if __name__ == "__main__":
    main()
