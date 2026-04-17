#!/usr/bin/env python3
"""Daily P&L Attribution Report — Generates and saves attribution analysis.

Usage:
    python3 scripts/daily-attribution.py              # Print report
    python3 scripts/daily-attribution.py --save       # Save to data/attribution-report.json
    python3 scripts/daily-attribution.py --notify     # Send edge decay alerts via WhatsApp
"""

import argparse
import collections
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from pnl_attribution import PnLAttributor, _load_trades_safe
from edge_monitor import EdgeMonitor
from event_ledger import DEFAULT_LEDGER_PATH, get_event_ledger
from research.trade_attribution import TradeAttributionArtifact
from settlement_utils import compute_trade_pnl_cents
from trade_files import TRADE_FILES as _CANONICAL_TRADE_FILES

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

TRADE_FILES = [
    {"path": str(DATA_DIR / tf["filename"]), "bot": tf["bot"]}
    for tf in _CANONICAL_TRADE_FILES
]


def _parse_datetime(value):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _stats_bucket():
    return {"pnl_cents": 0, "trades": 0, "wins": 0, "losses": 0}


def _add_stats(group, key, pnl_cents):
    stats = group[key]
    stats["pnl_cents"] += pnl_cents
    stats["trades"] += 1
    if pnl_cents > 0:
        stats["wins"] += 1
    elif pnl_cents < 0:
        stats["losses"] += 1


def _finalize_stats(group):
    result = dict(group)
    for stats in result.values():
        total = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(stats["wins"] / total, 4) if total > 0 else 0.0
    return result


def _hour_bucket(hour_of_day):
    try:
        hour = int(hour_of_day)
    except (TypeError, ValueError):
        return "unknown"
    if hour < 12:
        return "08-11"
    if hour < 15:
        return "12-14"
    if hour < 18:
        return "15-17"
    return "18+"


def _price_bucket(price_cents):
    try:
        price = int(price_cents)
    except (TypeError, ValueError):
        return "unknown"
    if price <= 5:
        return "<=5c"
    if price <= 25:
        return "6-25c"
    if price <= 50:
        return "26-50c"
    if price <= 75:
        return "51-75c"
    return ">75c"


def build_nws_source_monitor_report_from_trades(trades, *, now=None, lookback_days=30):
    now_utc = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(days=max(1, int(lookback_days)))
    by_city = collections.defaultdict(_stats_bucket)
    by_hour_bucket = collections.defaultdict(_stats_bucket)
    by_price_bucket = collections.defaultdict(_stats_bucket)
    by_direction = collections.defaultdict(_stats_bucket)
    total_pnl_cents = 0
    total_trades = 0

    for trade in trades or []:
        if trade.get("source_bot") != "source-monitor" or trade.get("source_type") != "nws":
            continue
        pnl_cents, is_settled = compute_trade_pnl_cents(trade)
        if not is_settled:
            continue
        trade_dt = _parse_datetime(trade.get("timestamp"))
        if trade_dt is None or trade_dt < cutoff:
            continue

        total_trades += 1
        total_pnl_cents += pnl_cents
        _add_stats(by_city, trade.get("city") or "unknown", pnl_cents)
        _add_stats(by_hour_bucket, _hour_bucket(trade.get("hour_of_day")), pnl_cents)
        price = trade.get("fill_price_cents")
        if price is None:
            price = trade.get("price_cents")
        _add_stats(by_price_bucket, _price_bucket(price), pnl_cents)
        direction = "threshold" if trade.get("direction") == "T" else "bracket" if trade.get("direction") == "B" else "unknown"
        _add_stats(by_direction, direction, pnl_cents)

    return {
        "lookback_days": int(max(1, int(lookback_days))),
        "summary": {
            "total_trades": total_trades,
            "total_pnl_cents": total_pnl_cents,
        },
        "by_direction": _finalize_stats(by_direction),
        "by_city": _finalize_stats(by_city),
        "by_hour_bucket": _finalize_stats(by_hour_bucket),
        "by_price_bucket": _finalize_stats(by_price_bucket),
    }


def build_nws_source_monitor_report(trade_files=None, *, now=None, lookback_days=30):
    loaded_trades = []
    for trade_file in trade_files or []:
        loaded_trades.extend(_load_trades_safe(trade_file["path"]))
    return build_nws_source_monitor_report_from_trades(
        loaded_trades,
        now=now,
        lookback_days=lookback_days,
    )


def print_report(report):
    """Print human-readable attribution report."""
    print("=" * 60)
    print("P&L ATTRIBUTION REPORT")
    print("=" * 60)

    s = report["summary"]
    print(f"\nSettled trades: {s['total_trades_settled']} of {s['total_trades_all']} total")
    print(f"Total P&L: ${s['total_pnl_cents'] / 100:.2f}")

    print("\n--- By Bot ---")
    for bot, stats in sorted(report["by_bot"].items(), key=lambda x: -x[1]["pnl_cents"]):
        print(f"  {bot:20s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")

    print("\n--- By Edge Bucket ---")
    for bucket, stats in report["by_edge_bucket"].items():
        print(f"  {bucket:10s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR  "
              f"avg edge {stats.get('avg_edge', 0)*100:.1f}%")

    print("\n--- By Market Type ---")
    for mt, stats in sorted(report["by_market_type"].items(), key=lambda x: -x[1]["pnl_cents"]):
        print(f"  {mt:15s}  ${stats['pnl_cents']/100:>8.2f}  "
              f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")

    nws_report = report.get("nws_source_monitor", {})
    nws_summary = nws_report.get("summary", {})
    if nws_summary.get("total_trades", 0) > 0:
        print(f"\n--- Source Monitor NWS ({nws_report.get('lookback_days', 30)}d) ---")
        print(f"  Settled trades: {nws_summary['total_trades']}  "
              f"Total P&L: ${nws_summary['total_pnl_cents']/100:.2f}")

        print("\n  By Direction")
        for direction, stats in sorted(nws_report.get("by_direction", {}).items(), key=lambda x: -x[1]["pnl_cents"]):
            print(f"    {direction:10s}  ${stats['pnl_cents']/100:>8.2f}  "
                  f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")

        print("\n  By City")
        for city, stats in sorted(nws_report.get("by_city", {}).items(), key=lambda x: -x[1]["pnl_cents"]):
            print(f"    {city:10s}  ${stats['pnl_cents']/100:>8.2f}  "
                  f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")

        print("\n  By Hour")
        for bucket, stats in sorted(nws_report.get("by_hour_bucket", {}).items(), key=lambda x: x[0]):
            print(f"    {bucket:10s}  ${stats['pnl_cents']/100:>8.2f}  "
                  f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")

        print("\n  By Price")
        for bucket, stats in sorted(nws_report.get("by_price_bucket", {}).items(), key=lambda x: x[0]):
            print(f"    {bucket:10s}  ${stats['pnl_cents']/100:>8.2f}  "
                  f"{stats['trades']:>3d} trades  {stats['win_rate']*100:.0f}% WR")


def check_edge_decay(notify=False):
    """Check edge monitor for decay warnings."""
    em = EdgeMonitor(state_path=str(DATA_DIR / "edge-monitor-state.json"))
    em.load_state()
    report = em.json_report()

    alerts = []
    for mt, info in report.get("market_types", {}).items():
        if info.get("competitor_detected"):
            alerts.append(f"Competitor detected in {mt} markets")
        hl = info.get("edge_half_life_days", float("inf"))
        if hl != float("inf") and hl < 30:
            alerts.append(f"{mt} edge half-life is {hl:.0f} days (< 30 day threshold)")

    if alerts:
        msg = "EDGE DECAY ALERT\n" + "\n".join(alerts)
        print(f"\n{msg}")
        if notify:
            try:
                from kalshi_auth import notify_whatsapp
                notify_whatsapp(msg)
            except Exception as e:
                print(f"WhatsApp notification failed: {e}")

    return alerts


def main():
    parser = argparse.ArgumentParser(description="Daily P&L Attribution")
    parser.add_argument("--save", action="store_true", help="Save report to JSON")
    parser.add_argument("--notify", action="store_true", help="Send WhatsApp alerts")
    parser.add_argument("--use-ledger", action="store_true", help="Read trades from the SQLite event ledger")
    parser.add_argument("--nws-lookback-days", type=int, default=30, help="Lookback window for source-monitor NWS attribution section")
    args = parser.parse_args()

    attr = PnLAttributor(
        trade_file_paths=TRADE_FILES,
        regime_state_path=str(DATA_DIR / "regime-state.json"),
        ledger_path=DEFAULT_LEDGER_PATH if args.use_ledger else None,
    )
    attr.load_trades()
    report = attr.full_report()
    generated_at = report.get("generated_at") or datetime.datetime.now(datetime.timezone.utc).isoformat()
    report["generated_at"] = generated_at
    report["nws_source_monitor"] = build_nws_source_monitor_report(
        TRADE_FILES,
        now=_parse_datetime(generated_at),
        lookback_days=args.nws_lookback_days,
    )

    print_report(report)

    if args.save:
        artifact = TradeAttributionArtifact(path=DATA_DIR / "attribution-report.json")
        saved_report = artifact.save(report)
        try:
            get_event_ledger(path=DEFAULT_LEDGER_PATH).record_post_trade_attribution(
                saved_report,
                report_name=saved_report.get("report_name", "daily_attribution"),
                source_path=artifact.path,
            )
        except Exception:
            pass
        print(f"\nSaved to {artifact.path}")

    check_edge_decay(notify=args.notify)


if __name__ == "__main__":
    main()
