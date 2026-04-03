#!/usr/bin/env python3
"""Build an operational Oracle scorecard from the alpha ledger."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from domain.oracle.alpha_capture import (
    DEFAULT_HYPOTHESIS_ID,
    DEFAULT_ORACLE_ALPHA_LEDGER_PATH,
    OracleAlphaCapture,
)
from domain.oracle.latency_analysis import (
    summarize_alpha_execution_capture,
    summarize_h1_daily_activity,
    summarize_latency_capture,
)
from domain.oracle.risk.fees import expected_value_cents
from event_ledger import (
    EVENT_TYPE_MARKET_SNAPSHOT,
    EVENT_TYPE_ORDER_UPDATE,
    EVENT_TYPE_SOURCE_OBSERVATION,
    EventLedger,
)


PROJECT_DIR = Path(__file__).resolve().parents[3]
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
DEFAULT_SCORECARD_PATH = PROJECT_DIR / "data" / "oracle-scorecard.json"
DEFAULT_KILL_DEADLINE = dt.date(2026, 4, 30)
DEFAULT_STALE_ORDER_MINUTES = 15
EASTERN_TZ = ZoneInfo("America/New_York")
ORDER_FILLED_STATUSES = frozenset({"filled", "complete", "executed"})
ORDER_CANCELED_STATUSES = frozenset({"canceled", "cancelled", "expired", "rejected", "voided", "closed"})
ORDER_TERMINAL_STATUSES = ORDER_FILLED_STATUSES | ORDER_CANCELED_STATUSES


def _round(value: float | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _coerce_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value):
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load_oracle_config(config_path: Path = BOTS_CONFIG_PATH) -> dict:
    try:
        return json.loads(config_path.read_text()).get("oracle", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _signal_edge_pct(row: dict) -> float | None:
    model_prob = _coerce_float(row.get("model_prob"))
    market_prob = _coerce_float(row.get("market_prob"))
    if model_prob is None or market_prob is None:
        return None
    return (model_prob - market_prob) * 100.0


def _entry_price_cents(row: dict) -> int | None:
    entry_price = _coerce_float(row.get("entry_price"))
    if entry_price is not None:
        if entry_price > 1:
            return int(round(entry_price))
        return int(round(entry_price * 100))
    order_price = _coerce_int(row.get("order_price_cents"))
    if order_price is not None:
        return order_price
    return None


def _signal_post_fee_ev_cents(row: dict) -> float | None:
    model_prob = _coerce_float(row.get("model_prob"))
    price_cents = _entry_price_cents(row)
    if model_prob is None or price_cents in (None, 0):
        return None
    return expected_value_cents(model_prob, price_cents, contracts=1)


def _signal_depth_contracts(row: dict) -> int | None:
    bid_depth = _coerce_int(row.get("yes_bid_depth"))
    ask_depth = _coerce_int(row.get("yes_ask_depth"))
    depths = [depth for depth in (bid_depth, ask_depth) if depth is not None]
    if not depths:
        return None
    return min(depths)


def _normalize_reason(reason: str | None) -> str:
    text = str(reason or "").strip()
    if not text:
        return "unknown"
    if ":" in text:
        return text.split(":", 1)[0].strip()
    if "(" in text:
        return text.split("(", 1)[0].strip()
    return text


def _mean(values: list[float | int]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _mode_counts(rows: list[dict]) -> dict[str, int]:
    counts = Counter()
    for row in rows:
        counts[str(row.get("mode") or "unknown")] += 1
    return dict(sorted(counts.items()))


def _skip_reason_counts(signal_rows: list[dict]) -> dict[str, int]:
    counts = Counter()
    for row in signal_rows:
        if row.get("triggered"):
            continue
        counts[_normalize_reason(row.get("reason"))] += 1
    return dict(counts.most_common())


def _is_study_order_row(row: dict) -> bool:
    signal_id = str(row.get("signal_id") or "").strip()
    if signal_id:
        return True
    entry_type = str(row.get("entry_type") or "").strip().lower()
    if entry_type in {"passive", "aggressive"}:
        return True
    mode = str(row.get("mode") or "").strip().lower()
    if mode in {"demo", "shadow", "live"}:
        return True
    signal_type = str(row.get("signal_type") or "").strip()
    if signal_type:
        return True
    book = str(row.get("book") or "").strip().upper()
    if book in {"A", "B", "C"}:
        return True
    return False


def _order_event_timestamp(row: dict) -> dt.datetime | None:
    return _parse_iso_datetime(
        row.get("update_timestamp_utc")
        or row.get("order_timestamp_utc")
        or row.get("timestamp")
        or row.get("signal_timestamp_utc")
    )


def _latest_order_states(order_rows: list[dict], order_update_rows: list[dict]) -> dict[str, dict]:
    states: dict[str, dict] = {}

    def _ensure_state(order_id: str, row: dict, *, timestamp: dt.datetime | None) -> dict:
        return states.setdefault(
            order_id,
            {
                "order_id": order_id,
                "ticker": row.get("ticker") or row.get("market_ticker"),
                "signal_type": row.get("signal_type"),
                "entry_type": row.get("entry_type"),
                "mode": row.get("mode"),
                "book": row.get("book"),
                "side": row.get("side"),
                "signal_id": row.get("signal_id"),
                "hypothesis_id": row.get("hypothesis_id"),
                "count": _coerce_int(row.get("count")),
                "study_eligible": _is_study_order_row(row),
                "submitted_at": timestamp,
                "submitted_at_iso": timestamp.isoformat() if timestamp is not None else None,
                "latest_status": str(row.get("status") or "").strip().lower(),
                "latest_reason": row.get("reason"),
                "latest_timestamp": timestamp,
                "latest_timestamp_iso": timestamp.isoformat() if timestamp is not None else None,
                "latest_fill_count": _coerce_int(row.get("fill_count")) or 0,
                "latest_fill_price_cents": _coerce_int(row.get("fill_price_cents")),
            },
        )

    for row in order_rows:
        order_id = row.get("order_id")
        if order_id in (None, ""):
            continue
        order_id = str(order_id)
        timestamp = _order_event_timestamp(row)
        state = _ensure_state(order_id, row, timestamp=timestamp)
        if timestamp is not None:
            submitted_at = state.get("submitted_at")
            if submitted_at is None or timestamp < submitted_at:
                state["submitted_at"] = timestamp
                state["submitted_at_iso"] = timestamp.isoformat()
        for key in ("ticker", "signal_type", "entry_type", "mode", "book", "side", "signal_id", "hypothesis_id"):
            if row.get(key) not in (None, ""):
                state[key] = row.get(key)
        state["study_eligible"] = bool(state.get("study_eligible")) or _is_study_order_row(row)
        if _coerce_int(row.get("count")) is not None:
            state["count"] = _coerce_int(row.get("count"))
        latest_timestamp = state.get("latest_timestamp")
        if latest_timestamp is None or (timestamp is not None and timestamp >= latest_timestamp):
            state["latest_status"] = str(row.get("status") or "").strip().lower()
            state["latest_reason"] = row.get("reason")
            state["latest_timestamp"] = timestamp
            state["latest_timestamp_iso"] = timestamp.isoformat() if timestamp is not None else None
            state["latest_fill_count"] = _coerce_int(row.get("fill_count")) or 0
            state["latest_fill_price_cents"] = _coerce_int(row.get("fill_price_cents"))

    for row in order_update_rows:
        order_id = row.get("order_id")
        if order_id in (None, ""):
            continue
        order_id = str(order_id)
        timestamp = _order_event_timestamp(row)
        state = _ensure_state(order_id, row, timestamp=timestamp)
        for key in ("ticker", "signal_type", "entry_type", "mode", "book", "side", "signal_id", "hypothesis_id"):
            if row.get(key) not in (None, ""):
                state[key] = row.get(key)
        state["study_eligible"] = bool(state.get("study_eligible")) or _is_study_order_row(row)
        if _coerce_int(row.get("count")) is not None:
            state["count"] = _coerce_int(row.get("count"))
        latest_timestamp = state.get("latest_timestamp")
        if latest_timestamp is None or (timestamp is not None and timestamp >= latest_timestamp):
            state["latest_status"] = str(row.get("status") or "").strip().lower()
            state["latest_reason"] = row.get("reason")
            state["latest_timestamp"] = timestamp
            state["latest_timestamp_iso"] = timestamp.isoformat() if timestamp is not None else None
            state["latest_fill_count"] = _coerce_int(row.get("fill_count")) or 0
            state["latest_fill_price_cents"] = _coerce_int(row.get("fill_price_cents"))

    return states


def _stale_open_orders(
    order_rows: list[dict],
    order_update_rows: list[dict],
    fill_rows: list[dict],
    settlement_rows: list[dict],
    *,
    now: dt.datetime,
    stale_minutes: int,
) -> dict:
    order_states = _latest_order_states(order_rows, order_update_rows)
    filled_order_ids = {
        str(row.get("order_id"))
        for row in fill_rows
        if row.get("order_id") not in (None, "")
    }
    settled_order_ids = {
        str(row.get("order_id"))
        for row in settlement_rows
        if row.get("order_id") not in (None, "")
    }
    stale_rows = []
    ignored_rows = []
    threshold = dt.timedelta(minutes=int(stale_minutes))
    for order_id, row in order_states.items():
        if order_id in filled_order_ids or order_id in settled_order_ids:
            continue
        if str(row.get("latest_status") or "").strip().lower() in ORDER_TERMINAL_STATUSES:
            continue
        submitted_at = row.get("submitted_at")
        if submitted_at is None:
            continue
        age = now - submitted_at
        if age < threshold:
            continue
        stale_row = {
            "order_id": order_id,
            "ticker": row.get("ticker") or row.get("market_ticker"),
            "signal_type": row.get("signal_type"),
            "entry_type": row.get("entry_type"),
            "mode": row.get("mode"),
            "status": row.get("latest_status"),
            "age_minutes": round(age.total_seconds() / 60.0, 1),
        }
        if row.get("study_eligible"):
            stale_rows.append(stale_row)
        else:
            ignored_rows.append(stale_row)
    stale_rows.sort(key=lambda row: row["age_minutes"], reverse=True)
    ignored_rows.sort(key=lambda row: row["age_minutes"], reverse=True)
    return {
        "count": len(stale_rows),
        "oldest_age_minutes": stale_rows[0]["age_minutes"] if stale_rows else None,
        "rows": stale_rows[:10],
        "ignored_legacy_count": len(ignored_rows),
        "ignored_legacy_rows": ignored_rows[:10],
    }


def _passive_demo_summary(
    signal_rows: list[dict],
    order_rows: list[dict],
    order_update_rows: list[dict],
    fill_rows: list[dict],
    settlement_rows: list[dict],
    *,
    hypothesis_id: str,
) -> dict:
    order_states = _latest_order_states(order_rows, order_update_rows)
    passive_states = [
        state
        for state in order_states.values()
        if str(state.get("entry_type") or "") == "passive"
        and str(state.get("mode") or "") == "demo"
    ]
    passive_order_ids = {str(state["order_id"]) for state in passive_states}
    passive_signal_ids = {
        str(state.get("signal_id"))
        for state in passive_states
        if state.get("signal_id") not in (None, "")
    }
    passive_updates = [
        row
        for row in order_update_rows
        if str(row.get("order_id") or "") in passive_order_ids
    ]
    passive_fills = [
        row
        for row in fill_rows
        if str(row.get("order_id") or "") in passive_order_ids
    ]
    passive_settlements = [
        row
        for row in settlement_rows
        if str(row.get("order_id") or "") in passive_order_ids
    ]
    passive_signals = [
        row
        for row in signal_rows
        if str(row.get("signal_id") or "") in passive_signal_ids
    ]

    fill_counts_by_order: dict[str, int] = {}
    first_fill_by_order: dict[str, dt.datetime] = {}
    for row in passive_fills:
        order_id = str(row.get("order_id") or "")
        if not order_id:
            continue
        fill_counts_by_order[order_id] = fill_counts_by_order.get(order_id, 0) + (_coerce_int(row.get("fill_count")) or 0)
        fill_ts = _parse_iso_datetime(row.get("fill_timestamp_utc") or row.get("timestamp"))
        if fill_ts is not None and (
            order_id not in first_fill_by_order or fill_ts < first_fill_by_order[order_id]
        ):
            first_fill_by_order[order_id] = fill_ts

    timeout_cancel_attempt_ids = {
        str(row.get("order_id") or "")
        for row in passive_updates
        if str(row.get("reason") or "") in {"passive_timeout_cancel", "passive_timeout_cancel_failed"}
    }
    timeout_canceled_ids = {
        str(row.get("order_id") or "")
        for row in passive_updates
        if str(row.get("reason") or "") == "passive_timeout_cancel"
        and str(row.get("status") or "").strip().lower() in ORDER_CANCELED_STATUSES
    }

    filled_orders = 0
    partial_fill_orders = 0
    canceled_orders = 0
    time_to_first_fill_seconds = []
    time_to_terminal_seconds = []
    for state in passive_states:
        order_id = str(state["order_id"])
        fill_count = fill_counts_by_order.get(order_id, 0)
        submitted_at = state.get("submitted_at")
        latest_status = str(state.get("latest_status") or "").strip().lower()
        count = _coerce_int(state.get("count")) or 0
        if fill_count > 0:
            filled_orders += 1
            if order_id in first_fill_by_order and submitted_at is not None:
                time_to_first_fill_seconds.append(
                    max(0.0, (first_fill_by_order[order_id] - submitted_at).total_seconds())
                )
            if 0 < fill_count < count:
                partial_fill_orders += 1
        elif latest_status in {"partial_fill", "partially_filled", "partial"}:
            partial_fill_orders += 1
        if latest_status in ORDER_CANCELED_STATUSES:
            canceled_orders += 1
        if latest_status in ORDER_TERMINAL_STATUSES and state.get("latest_timestamp") is not None and submitted_at is not None:
            time_to_terminal_seconds.append(
                max(0.0, (state["latest_timestamp"] - submitted_at).total_seconds())
            )

    execution_rollup = summarize_alpha_execution_capture(
        passive_signals,
        [row for row in order_rows if str(row.get("order_id") or "") in passive_order_ids],
        passive_fills,
        settlement_rows=passive_settlements,
        hypothesis_id=hypothesis_id,
        require_signal_link=True,
    ).get("execution_summary", {})

    cancel_attempts = len(timeout_cancel_attempt_ids)
    return {
        "order_rows": len(passive_states),
        "filled_orders": filled_orders,
        "fill_rate": _round(filled_orders / len(passive_states), 3) if passive_states else None,
        "partial_fill_orders": partial_fill_orders,
        "canceled_orders": canceled_orders,
        "timeout_cancel_attempts": cancel_attempts,
        "timeout_canceled_orders": len(timeout_canceled_ids),
        "timeout_cancel_success_rate": (
            _round(len(timeout_canceled_ids) / cancel_attempts, 3) if cancel_attempts else None
        ),
        "avg_time_to_first_fill_seconds": _round(_mean(time_to_first_fill_seconds), 3),
        "avg_time_to_terminal_seconds": _round(_mean(time_to_terminal_seconds), 3),
        "settled_orders": execution_rollup.get("settlement_rows", 0),
        "realized_net_pnl_cents": execution_rollup.get("realized_net_pnl_cents"),
        "realized_clv_cents": execution_rollup.get("realized_clv_cents"),
        "pass_fail_status": execution_rollup.get("pass_fail_status"),
    }


def _pregame_capture_summary(source_rows: list[dict], quote_rows: list[dict]) -> dict:
    crowd_rows = [
        row
        for row in source_rows
        if row.get("snapshot_name") == "crowd_probability" and row.get("collector_mode") == "pregame"
    ]
    index_rows = [
        row
        for row in source_rows
        if row.get("snapshot_name") == "market_index_snapshot" and row.get("collector_mode") == "pregame"
    ]
    pregame_quotes = [
        row
        for row in quote_rows
        if row.get("collector_mode") == "pregame"
    ]
    cycle_ids = {
        str(row.get("collector_cycle_id"))
        for row in crowd_rows + index_rows + pregame_quotes
        if row.get("collector_cycle_id") not in (None, "")
    }
    observed_times = [
        parsed
        for parsed in (
            _parse_iso_datetime(row.get("observed_at"))
            or _parse_iso_datetime(row.get("quote_timestamp_utc"))
            for row in crowd_rows + index_rows + pregame_quotes
        )
        if parsed is not None
    ]
    games = {
        str(row.get("game_id"))
        for row in crowd_rows + pregame_quotes
        if row.get("game_id") not in (None, "")
    }
    pregame_window_rows = 0
    for row in crowd_rows:
        observed_at = _parse_iso_datetime(row.get("observed_at"))
        if observed_at is None:
            continue
        eastern = observed_at.astimezone(EASTERN_TZ)
        if 14 <= eastern.hour < 19:
            pregame_window_rows += 1
    return {
        "collector_cycles": len(cycle_ids),
        "crowd_probability_rows": len(crowd_rows),
        "market_index_rows": len(index_rows),
        "quote_rows": len(pregame_quotes),
        "games": len(games),
        "pregame_window_rows_2pm_to_7pm_et": pregame_window_rows,
        "last_collected_at": max(observed_times).isoformat() if observed_times else None,
    }


def _study_metrics(signal_rows: list[dict]) -> dict:
    triggered_rows = [row for row in signal_rows if row.get("triggered")]
    edges = [value for value in (_signal_edge_pct(row) for row in signal_rows) if value is not None]
    post_fee_evs = [
        value for value in (_signal_post_fee_ev_cents(row) for row in signal_rows)
        if value is not None
    ]
    spreads = [
        value for value in (_coerce_int(row.get("spread_cents")) for row in signal_rows)
        if value is not None
    ]
    depths = [
        value for value in (_signal_depth_contracts(row) for row in signal_rows)
        if value is not None
    ]
    return {
        "signal_rows": len(signal_rows),
        "triggered_signal_rows": len(triggered_rows),
        "trigger_rate": _round(len(triggered_rows) / len(signal_rows), 3) if signal_rows else None,
        "avg_edge_pct": _round(_mean(edges), 3),
        "avg_post_fee_ev_cents": _round(_mean(post_fee_evs), 3),
        "avg_spread_cents": _round(_mean(spreads), 3),
        "avg_depth_contracts": _round(_mean(depths), 3),
        "mode_counts": _mode_counts(signal_rows),
        "skip_reason_counts": _skip_reason_counts(signal_rows),
    }


def _gate_status(*, actual, target=None, passed: bool, detail: str) -> dict:
    return {
        "passed": bool(passed),
        "actual": actual,
        "target": target,
        "detail": detail,
    }


def build_scorecard(
    *,
    ledger_path: str | Path = DEFAULT_ORACLE_ALPHA_LEDGER_PATH,
    hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
    stale_order_minutes: int = DEFAULT_STALE_ORDER_MINUTES,
    now: dt.datetime | None = None,
) -> dict:
    ledger_path = Path(ledger_path)
    now_utc = now or _utc_now()
    config = _load_oracle_config()

    capture = OracleAlphaCapture(path=ledger_path)
    ledger = EventLedger(ledger_path)
    source_rows = ledger._fetch_event_payloads(
        EVENT_TYPE_SOURCE_OBSERVATION,
        source_path=str(ledger_path),
    )
    order_update_rows = ledger._fetch_event_payloads(
        EVENT_TYPE_ORDER_UPDATE,
        source_path=str(ledger_path),
    )
    quote_rows = ledger._fetch_event_payloads(
        EVENT_TYPE_MARKET_SNAPSHOT,
        source_path=str(ledger_path),
    )
    signal_rows = capture.load_signal_rows(hypothesis_id=hypothesis_id)
    order_rows = capture.load_order_rows(hypothesis_id=hypothesis_id)
    fill_rows = capture.load_fill_rows(hypothesis_id=hypothesis_id)
    settlement_rows = capture.load_settlement_rows(hypothesis_id=hypothesis_id)

    latency_summary = summarize_latency_capture(source_rows, quote_rows, hypothesis_id=hypothesis_id)
    execution_summary = summarize_alpha_execution_capture(
        signal_rows,
        order_rows,
        fill_rows,
        settlement_rows=settlement_rows,
        hypothesis_id=hypothesis_id,
        require_signal_link=True,
    )
    daily_summary = summarize_h1_daily_activity(
        source_rows,
        quote_rows,
        signal_rows,
        order_rows,
        fill_rows,
        settlement_rows,
        hypothesis_id=hypothesis_id,
        require_signal_link=True,
    )

    study_metrics = _study_metrics(signal_rows)
    stale_orders = _stale_open_orders(
        order_rows,
        order_update_rows,
        fill_rows,
        settlement_rows,
        now=now_utc,
        stale_minutes=stale_order_minutes,
    )
    pregame_capture = _pregame_capture_summary(source_rows, quote_rows)
    passive_demo = _passive_demo_summary(
        signal_rows,
        order_rows,
        order_update_rows,
        fill_rows,
        settlement_rows,
        hypothesis_id=hypothesis_id,
    )

    passive_demo_orders = [
        row for row in order_rows
        if str(row.get("entry_type") or "") == "passive"
        and str(row.get("mode") or "") == "demo"
    ]
    clutch_shadow_rows = [
        row
        for row in signal_rows
        if row.get("signal_type") == "clutch_comeback"
        and str(row.get("mode") or "") in {"demo", "shadow"}
        and row.get("triggered")
    ]

    execution_rollup = execution_summary.get("execution_summary", {})
    net_ev_ci = execution_rollup.get("bootstrap_net_ev_per_signal_cents", {})
    positive_clv_day_share = daily_summary.get("positive_clv_day_share")
    kill_deadline_passed = now_utc.date() > DEFAULT_KILL_DEADLINE

    gates = {
        "oracle_fail_closed_default": _gate_status(
            actual={
                "oracle_enabled": bool(config.get("enabled")),
                "book_c_enabled": bool(config.get("books", {}).get("C", {}).get("enabled")),
                "passive_execution": bool(config.get("books", {}).get("C", {}).get("passiveExecution")),
            },
            passed=(
                not config.get("enabled")
                and not config.get("books", {}).get("C", {}).get("enabled")
                and not config.get("books", {}).get("C", {}).get("passiveExecution")
            ),
            detail="Default config must remain fail-closed outside bounded study services.",
        ),
        "clutch_shadow_signals_gte_100": _gate_status(
            actual=len(clutch_shadow_rows),
            target=100,
            passed=len(clutch_shadow_rows) >= 100,
            detail="Clutch-only demo/shadow triggers required before any live discussion.",
        ),
        "maker_demo_orders_gte_50": _gate_status(
            actual=len(passive_demo_orders),
            target=50,
            passed=len(passive_demo_orders) >= 50,
            detail="Passive execution stays research-only until 50+ demo passive orders exist.",
        ),
        "stale_open_orders_eq_0": _gate_status(
            actual=stale_orders["count"],
            target=0,
            passed=stale_orders["count"] == 0,
            detail="No unresolved order older than the stale-order threshold may remain in the ledger view.",
        ),
        "net_ev_ci_above_zero": _gate_status(
            actual=net_ev_ci,
            target={"ci_low_gt": 0},
            passed=net_ev_ci.get("ci_low") is not None and net_ev_ci.get("ci_low") > 0,
            detail="Post-fee realized net EV confidence interval must clear zero.",
        ),
        "positive_clv_day_share_gte_0_60": _gate_status(
            actual=positive_clv_day_share,
            target=0.60,
            passed=positive_clv_day_share is not None and positive_clv_day_share >= 0.60,
            detail="Shadow/demonstration quality should show positive CLV on most settled days.",
        ),
        "kill_deadline_2026_04_30": _gate_status(
            actual=now_utc.date().isoformat(),
            target=DEFAULT_KILL_DEADLINE.isoformat(),
            passed=not kill_deadline_passed,
            detail="If Oracle does not validate by April 30, 2026, it should be deprioritized or killed.",
        ),
    }

    recommendation = "keep_oracle_fail_closed"
    if stale_orders["count"] > 0:
        recommendation = "fix_order_leakage_before_any_study_expansion"
    elif stale_orders.get("ignored_legacy_count", 0) > 0:
        recommendation = "run_oracle_alpha_ledger_cleanup"
    elif not gates["clutch_shadow_signals_gte_100"]["passed"]:
        recommendation = "continue_clutch_only_shadow_collection"
    elif not gates["net_ev_ci_above_zero"]["passed"]:
        recommendation = "shadow_data_still_not_profitable"
    elif kill_deadline_passed:
        recommendation = "kill_oracle_or_reallocate_roadmap_time"

    return {
        "generated_at": now_utc.isoformat(),
        "ledger_path": str(ledger_path),
        "hypothesis_id": hypothesis_id,
        "kill_deadline": DEFAULT_KILL_DEADLINE.isoformat(),
        "recommendation": recommendation,
        "config_posture": {
            "oracle_enabled": bool(config.get("enabled")),
            "book_a_enabled": bool(config.get("books", {}).get("A", {}).get("enabled")),
            "book_b_enabled": bool(config.get("books", {}).get("B", {}).get("enabled")),
            "book_c_enabled": bool(config.get("books", {}).get("C", {}).get("enabled")),
            "book_c_prop_signals_enabled": bool(config.get("books", {}).get("C", {}).get("propSignalsEnabled")),
            "book_c_clutch_comeback_enabled": bool(config.get("books", {}).get("C", {}).get("clutchComebackEnabled")),
            "book_c_passive_execution": bool(config.get("books", {}).get("C", {}).get("passiveExecution")),
        },
        "study_metrics": study_metrics,
        "execution_summary": execution_rollup,
        "passive_demo_study": passive_demo,
        "by_signal_type": execution_summary.get("by_signal_type", {}),
        "daily_summary": daily_summary,
        "latency_summary": {
            "source_events": latency_summary.get("source_events"),
            "quoted_source_events": latency_summary.get("quoted_source_events"),
            "capture_rate": latency_summary.get("capture_rate"),
            "paired_fillable_opportunities": latency_summary.get("paired_fillable_opportunities"),
            "proof_checks": latency_summary.get("proof_checks"),
        },
        "stale_open_orders": stale_orders,
        "h2_pregame_capture": pregame_capture,
        "gates": gates,
    }


def _render_pct(value) -> str:
    if value is None:
        return "-"
    return f"{100.0 * float(value):.1f}%"


def _render_pct_points(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2f}pp"


def _render_cents(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2f}c"


def render_text(scorecard: dict) -> str:
    study = scorecard["study_metrics"]
    execution = scorecard["execution_summary"]
    passive_demo = scorecard["passive_demo_study"]
    pregame = scorecard["h2_pregame_capture"]
    stale = scorecard["stale_open_orders"]

    lines = [
        "Oracle Scorecard",
        f"generated_at={scorecard['generated_at']}",
        f"recommendation={scorecard['recommendation']}",
        (
            "config: "
            f"oracle_enabled={scorecard['config_posture']['oracle_enabled']} "
            f"book_c_enabled={scorecard['config_posture']['book_c_enabled']} "
            f"clutch={scorecard['config_posture']['book_c_clutch_comeback_enabled']} "
            f"props={scorecard['config_posture']['book_c_prop_signals_enabled']} "
            f"passive={scorecard['config_posture']['book_c_passive_execution']}"
        ),
        (
            "shadow: "
            f"signals={study['signal_rows']} "
            f"triggered={study['triggered_signal_rows']} "
            f"trigger_rate={_render_pct(study['trigger_rate'])} "
            f"avg_edge={_render_pct_points(study['avg_edge_pct'])} "
            f"avg_post_fee_ev={_render_cents(study['avg_post_fee_ev_cents'])} "
            f"avg_spread={study['avg_spread_cents'] if study['avg_spread_cents'] is not None else '-'} "
            f"avg_depth={study['avg_depth_contracts'] if study['avg_depth_contracts'] is not None else '-'}"
        ),
        (
            "execution: "
            f"orders={execution.get('order_rows', 0)} "
            f"fills={execution.get('fill_rows', 0)} "
            f"settlements={execution.get('settlement_rows', 0)} "
            f"net_pnl={execution.get('realized_net_pnl_cents')} "
            f"clv={execution.get('realized_clv_cents')} "
            f"pass_fail={execution.get('pass_fail_status')}"
        ),
        (
            "maker_demo: "
            f"orders={passive_demo['order_rows']} "
            f"filled={passive_demo['filled_orders']} "
            f"fill_rate={_render_pct(passive_demo['fill_rate'])} "
            f"partials={passive_demo['partial_fill_orders']} "
            f"timeout_cancel_success={_render_pct(passive_demo['timeout_cancel_success_rate'])} "
            f"avg_fill_s={passive_demo['avg_time_to_first_fill_seconds'] if passive_demo['avg_time_to_first_fill_seconds'] is not None else '-'} "
            f"clv={passive_demo['realized_clv_cents']}"
        ),
        (
            "h2_pregame: "
            f"cycles={pregame['collector_cycles']} "
            f"crowd_rows={pregame['crowd_probability_rows']} "
            f"quote_rows={pregame['quote_rows']} "
            f"games={pregame['games']} "
            f"last_collected_at={pregame['last_collected_at'] or '-'}"
        ),
        (
            "stale_orders: "
            f"count={stale['count']} "
            f"oldest_age_minutes={stale['oldest_age_minutes'] if stale['oldest_age_minutes'] is not None else '-'} "
            f"ignored_legacy={stale.get('ignored_legacy_count', 0)}"
        ),
        "gates:",
    ]
    for gate_name, gate in scorecard["gates"].items():
        status = "PASS" if gate["passed"] else "FAIL"
        lines.append(f"  {gate_name}: {status} actual={gate['actual']} target={gate['target']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an Oracle operational scorecard")
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
        "--save",
        action="store_true",
        help="Write the JSON artifact to data/oracle-scorecard.json",
    )
    parser.add_argument(
        "--stale-order-minutes",
        type=int,
        default=DEFAULT_STALE_ORDER_MINUTES,
        help="Age threshold for unresolved orders to be considered stale",
    )
    args = parser.parse_args()

    scorecard = build_scorecard(
        ledger_path=args.ledger_path,
        hypothesis_id=args.hypothesis_id,
        stale_order_minutes=args.stale_order_minutes,
    )

    if args.format == "json":
        print(json.dumps(scorecard, indent=2, sort_keys=True))
    else:
        print(render_text(scorecard))

    if args.save:
        DEFAULT_SCORECARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(str(DEFAULT_SCORECARD_PATH) + ".tmp")
        tmp_path.write_text(json.dumps(scorecard, indent=2, sort_keys=True))
        tmp_path.replace(DEFAULT_SCORECARD_PATH)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
