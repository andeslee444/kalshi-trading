#!/usr/bin/env python3
"""Canonical missed-trade report over the opportunity log artifact."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from research.missed_trade_analysis import DEFAULT_OPPORTUNITY_LOG_PATH, MissedTradeAnalyzer


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_REPORT_PATH = DATA_DIR / "missed-trade-report.json"


def main():
    parser = argparse.ArgumentParser(description="Analyze missed trades from the canonical opportunity log")
    parser.add_argument("--bot", type=str, default=None, help="Filter to a specific source_bot")
    parser.add_argument("--days", type=int, default=None, help="Only include opportunities from the last N days")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON instead of text")
    parser.add_argument("--save", action="store_true", help="Save the report to data/missed-trade-report.json")
    parser.add_argument("--limit", type=int, default=10, help="Number of top missed trades to include")
    parser.add_argument("--min-edge", type=float, default=0.0, help="Minimum edge required for top missed trades")
    parser.add_argument("--path", type=str, default=str(DEFAULT_OPPORTUNITY_LOG_PATH), help="Opportunity log path")
    args = parser.parse_args()

    analyzer = MissedTradeAnalyzer(args.path)
    analyzer.load_opportunities()

    report = analyzer.full_report(bot=args.bot, days=args.days, limit=args.limit, min_edge=args.min_edge)
    if args.json_output:
        output = json.dumps(report, indent=2)
    else:
        output = analyzer.summary_report(bot=args.bot, days=args.days, limit=args.limit, min_edge=args.min_edge)

    print(output)

    if args.save:
        DEFAULT_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(str(DEFAULT_REPORT_PATH) + ".tmp", "w") as handle:
            json.dump(report, handle, indent=2)
        os.replace(str(DEFAULT_REPORT_PATH) + ".tmp", str(DEFAULT_REPORT_PATH))


if __name__ == "__main__":
    main()
