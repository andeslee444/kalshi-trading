#!/usr/bin/env python3
"""Summarize Oracle latency-capture records from the alpha ledger."""

from __future__ import annotations

import argparse
import json

from domain.oracle.alpha_capture import (
    DEFAULT_HYPOTHESIS_ID,
    DEFAULT_ORACLE_ALPHA_LEDGER_PATH,
    OracleAlphaCapture,
)
from domain.oracle.latency_analysis import (
    summarize_alpha_execution_capture,
    summarize_h1_daily_activity,
    summarize_h1_decision,
    summarize_latency_capture,
)
from event_ledger import (
    EVENT_TYPE_MARKET_SNAPSHOT,
    EVENT_TYPE_SOURCE_OBSERVATION,
    EventLedger,
)


def _render_horizon(value) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _render_pct(value) -> str:
    if value is None:
        return "-"
    return f"{int(round(100 * value))}%"


def _render_ci(ci: dict | None) -> str:
    if not ci:
        return "-"
    ci_low = ci.get("ci_low")
    ci_high = ci.get("ci_high")
    if ci_low is None or ci_high is None:
        return "-"
    return f"[{ci_low:.1f},{ci_high:.1f}]"


def _render_seconds(value) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return f"{int(value)}s"
    return f"{value:.3f}s"


def _render_ratio(value) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"


def _render_cents(value) -> str:
    if value is None:
        return "-"
    return f"{int(value)}c"


def _render_proof_flags(proof_checks: dict | None) -> str:
    if not proof_checks:
        return "---"
    return "".join(
        [
            "S" if proof_checks.get("stage1_research_signal_target_met") else "-",
            "F" if proof_checks.get("stage2_shadow_trade_target_met_proxy") else "-",
            "E" if proof_checks.get("bootstrap_mean_best_markout_ci_above_zero") else "-",
        ]
    )


def _shadow_table_rows(summary: dict, limit: int) -> list[str]:
    rows = [
        "signal_type          book  sig  ord  fill  ord%  sig%  med_ttf  p75_ttf  exp_fill",
        "-------------------  ----  ---  ---  ----  ----  ----  -------  -------  --------",
    ]
    ranked = sorted(
        summary.get("by_signal_type", {}).values(),
        key=lambda row: (
            row.get("order_fill_rate") is not None,
            row.get("order_fill_rate") or 0,
            row.get("filled_order_rows") or 0,
            row.get("signal_rows") or 0,
            row.get("mean_time_to_fill_seconds") or 0,
        ),
        reverse=True,
    )[:limit]
    for row in ranked:
        rows.append(
            f"{row['signal_type']:<19}  "
            f"{row['book']:<4}  "
            f"{row['signal_rows']:>3}  "
            f"{row['order_rows']:>3}  "
            f"{row['filled_order_rows']:>4}  "
            f"{_render_ratio(row.get('order_fill_rate')):>4}  "
            f"{_render_ratio(row.get('signal_fill_rate')):>4}  "
            f"{_render_seconds(row.get('median_time_to_fill_seconds')):>7}  "
            f"{_render_seconds(row.get('p75_time_to_fill_seconds')):>7}  "
            f"{_render_ratio(row.get('expected_fill_probability_mean')):>8}"
        )
    return rows


def _render_execution_summary(summary: dict) -> str:
    return (
        "signal_rows="
        f"{summary.get('signal_rows', 0)} "
        f"settlement_rows={summary.get('settlement_rows', 0)} "
        f"settled_contracts={summary.get('settled_contracts', 0)} "
        f"gross_pnl={_render_cents(summary.get('realized_gross_pnl_cents'))} "
        f"net_pnl={_render_cents(summary.get('realized_net_pnl_cents'))} "
        f"clv={_render_cents(summary.get('realized_clv_cents'))} "
        f"pass_fail={summary.get('pass_fail_status', '-')} "
        f"no_settlement_yet={summary.get('no_settlement_yet', True)} "
        f"no_close_yet={summary.get('no_close_yet', True)}"
    )


def _render_h1_decision(summary: dict) -> str:
    return (
        "h1_status="
        f"{summary.get('status', '-')} "
        f"best_event={summary.get('best_latency_event_class') or '-'} "
        f"best_med_ms={summary.get('best_latency_median_ms') or '-'} "
        f"best_p75_ms={summary.get('best_latency_p75_ms') or '-'} "
        f"fill_rate={_render_ratio(summary.get('order_fill_rate'))} "
        f"net_ev_ci={_render_ci(summary.get('bootstrap_net_ev_per_signal_cents'))} "
        f"clv_ci={_render_ci(summary.get('bootstrap_clv_per_signal_cents'))}"
    )


def _render_market_type_capture(summary: dict, market_type: str) -> str:
    row = summary.get("by_market_type", {}).get(market_type, {})
    return (
        f"{market_type}_capture: "
        f"source_events={row.get('source_events', 0)} "
        f"quoted_source_events={row.get('quoted_source_events', 0)} "
        f"capture_rate={_render_ratio(row.get('capture_rate'))} "
        f"quote_snapshots={row.get('quote_snapshots', 0)} "
        f"event_immediate={row.get('event_immediate_quote_snapshots', 0)} "
        f"event_followup={row.get('event_followup_quote_snapshots', 0)} "
        f"paired_fillable_opportunities={row.get('paired_fillable_opportunities', 0)}"
    )


def _daily_table_rows(summary: dict, limit: int) -> list[str]:
    rows = [
        "date        src  quote  sig  ord  fill  setl  net_pnl  clv  cap%  fill%",
        "----------  ---  -----  ---  ---  ----  ----  -------  ---  ----  -----",
    ]
    for row in list(summary.get("rows", []))[-limit:]:
        rows.append(
            f"{row['date']:<10}  "
            f"{row['source_events']:>3}  "
            f"{row['quote_snapshots']:>5}  "
            f"{row['signals']:>3}  "
            f"{row['orders']:>3}  "
            f"{row['fills']:>4}  "
            f"{row['settlements']:>4}  "
            f"{_render_cents(row.get('realized_net_pnl_cents')):>7}  "
            f"{_render_cents(row.get('realized_clv_cents')):>3}  "
            f"{_render_pct(row.get('capture_rate')):>4}  "
            f"{_render_pct(row.get('order_fill_rate_proxy')):>5}"
        )
    return rows


def _table_rows(summary: dict, limit: int) -> list[str]:
    def render(value) -> str:
        return "-" if value is None else str(value)

    ranked = summary.get("ranked_horizon_rows", [])[:limit]
    rows = [
        "event_class           hor_s  src  q0  qh  fill  cap%  med_ms  mv%  mean_c  ci_c        gate",
        "--------------------  -----  ---  --  --  ----  ----  ------  ---  ------  ----------  ----",
    ]
    for row in ranked:
        rows.append(
            f"{row['event_class']:<20}  "
            f"{_render_horizon(row.get('horizon_seconds')):>5}  "
            f"{row['source_events']:>3}  "
            f"{row['quoted_source_events']:>2}  "
            f"{row['quote_snapshots']:>2}  "
            f"{row['paired_fillable_opportunities']:>4}  "
            f"{_render_pct(row.get('capture_rate')):>4}  "
            f"{render(row.get('median_latency_ms')):>6}  "
            f"{_render_pct(row.get('moved_quote_rate')):>3}  "
            f"{render(row.get('mean_best_markout_cents')):>6}  "
            f"{_render_ci(row.get('bootstrap_mean_best_markout_cents')):>10}  "
            f"{_render_proof_flags(row.get('proof_checks')):>4}"
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle latency capture report")
    parser.add_argument(
        "--ledger-path",
        default=str(DEFAULT_ORACLE_ALPHA_LEDGER_PATH),
        help="Path to the Oracle alpha ledger SQLite file",
    )
    parser.add_argument(
        "--hypothesis-id",
        default=DEFAULT_HYPOTHESIS_ID,
        help="Hypothesis id to summarize",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of event classes to print in table mode",
    )
    parser.add_argument(
        "--max-spread-cents",
        type=int,
        default=8,
        help="Maximum spread for displayed aggressive fillability proxy",
    )
    parser.add_argument(
        "--min-depth-contracts",
        type=int,
        default=5,
        help="Minimum displayed depth for aggressive fillability proxy",
    )
    parser.add_argument(
        "--daily-summary-only",
        action="store_true",
        help="Output only the compact daily H1 summary",
    )
    args = parser.parse_args()

    ledger = EventLedger(args.ledger_path)
    alpha_capture = OracleAlphaCapture(path=args.ledger_path)
    source_rows = ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)
    quote_rows = ledger._fetch_event_payloads(EVENT_TYPE_MARKET_SNAPSHOT)
    signal_rows = alpha_capture.load_signal_rows(hypothesis_id=args.hypothesis_id)
    order_rows = alpha_capture.load_order_rows(hypothesis_id=args.hypothesis_id)
    fill_rows = alpha_capture.load_fill_rows(hypothesis_id=args.hypothesis_id)
    settlement_rows = alpha_capture.load_settlement_rows(hypothesis_id=args.hypothesis_id)
    summary = summarize_latency_capture(
        source_rows,
        quote_rows,
        hypothesis_id=args.hypothesis_id,
        max_spread_cents=args.max_spread_cents,
        min_depth_contracts=args.min_depth_contracts,
    )
    shadow_summary = summarize_alpha_execution_capture(
        signal_rows,
        order_rows,
        fill_rows,
        hypothesis_id=args.hypothesis_id,
        settlement_rows=settlement_rows,
        require_signal_link=True,
    )
    daily_summary = summarize_h1_daily_activity(
        source_rows,
        quote_rows,
        signal_rows,
        order_rows,
        fill_rows,
        settlement_rows,
        hypothesis_id=args.hypothesis_id,
        require_signal_link=True,
    )
    h1_decision = summarize_h1_decision(summary, shadow_summary, daily_summary)

    if args.format == "json":
        if args.daily_summary_only:
            print(json.dumps(daily_summary, indent=2, sort_keys=True))
            return 0
        report = dict(summary)
        report["shadow_execution_summary"] = shadow_summary
        report["execution_summary"] = shadow_summary["execution_summary"]
        report["daily_h1_summary"] = daily_summary
        report["h1_decision"] = h1_decision
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.daily_summary_only:
        print(f"days={daily_summary['days']} settled_days={daily_summary['settled_days']}")
        print(
            "positive_net_pnl_day_share="
            f"{_render_ratio(daily_summary.get('positive_net_pnl_day_share'))} "
            f"positive_clv_day_share={_render_ratio(daily_summary.get('positive_clv_day_share'))}"
        )
        for line in _daily_table_rows(daily_summary, args.limit):
            print(line)
        return 0

    print(f"ledger_path={args.ledger_path}")
    print(f"hypothesis_id={summary['hypothesis_id']}")
    print(
        "source_events="
        f"{summary['source_events']} "
        f"quoted_source_events={summary['quoted_source_events']} "
        f"capture_rate={summary.get('capture_rate')} "
        f"quote_snapshots={summary['quote_snapshots']}"
    )
    print(
        "paired_fillable_opportunities="
        f"{summary.get('paired_fillable_opportunities', 0)} "
        f"bootstrap_clusters={summary.get('bootstrap_mean_best_markout_cents', {}).get('cluster_count', 0)} "
        f"bootstrap_mean_best_markout_ci={_render_ci(summary.get('bootstrap_mean_best_markout_cents'))} "
        f"proof_flags={_render_proof_flags(summary.get('proof_checks'))}"
    )
    print("proof_flags: S=source_events>=200 F=fillable_pairs>=100 E=bootstrap_mean_best_markout_ci_above_zero")
    print(_render_market_type_capture(summary, "game"))
    print(_render_market_type_capture(summary, "prop"))
    print(
        "shadow_signal_rows="
        f"{shadow_summary['signal_rows']} "
        f"shadow_order_rows={shadow_summary['order_rows']} "
        f"shadow_fill_rows={shadow_summary['fill_rows']} "
        f"order_fill_rate={_render_ratio(shadow_summary.get('order_fill_rate'))} "
        f"signal_fill_rate={_render_ratio(shadow_summary.get('signal_fill_rate'))} "
        f"median_time_to_fill={_render_seconds(shadow_summary.get('median_time_to_fill_seconds'))} "
        f"no_fills_yet={shadow_summary['no_fills_yet']}"
    )
    print(_render_execution_summary(shadow_summary))
    print(_render_h1_decision(h1_decision))
    print("shadow: fill probability is observed order fill rate; time-to-fill is first fill minus order submission")
    print(
        "daily_h1_summary: positive_net_pnl_day_share="
        f"{_render_ratio(daily_summary.get('positive_net_pnl_day_share'))} "
        f"positive_clv_day_share={_render_ratio(daily_summary.get('positive_clv_day_share'))}"
    )
    for line in _shadow_table_rows(shadow_summary, args.limit):
        print(line)
    for line in _table_rows(summary, args.limit):
        print(line)
    for line in _daily_table_rows(daily_summary, args.limit):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
