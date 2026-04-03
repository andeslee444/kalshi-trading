"""Oracle research capture helpers for latency and alpha studies.

Uses the existing SQLite event ledger so Real websocket capture does not
degenerate into expensive append-and-rewrite JSON files.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from event_ledger import (
    EVENT_TYPE_FILL,
    EVENT_TYPE_MARKET_SNAPSHOT,
    EVENT_TYPE_ORDER_SUBMITTED,
    EVENT_TYPE_ORDER_UPDATE,
    EVENT_TYPE_SETTLEMENT,
    EVENT_TYPE_SOURCE_OBSERVATION,
    EVENT_TYPE_TRADE_DECISION,
    EventLedger,
)
from domain.shared.sizing import kalshi_fee_cents


PROJECT_DIR = Path(__file__).resolve().parents[4]
DEFAULT_ORACLE_ALPHA_LEDGER_PATH = PROJECT_DIR / "data" / "oracle-alpha-ledger.sqlite3"
DEFAULT_HYPOTHESIS_ID = "H1_real_to_kalshi_latency"
SCHEMA_VERSION = 1

_log = logging.getLogger("oracle.alpha_capture")


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _iso_from_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).isoformat()
    return None


def _stable_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]


def _coerce_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _reconciliation_key(*parts: Any) -> str:
    payload = {
        "parts": [part for part in parts if part not in (None, "")],
    }
    return _stable_hash(payload)


def _normalize_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"yes", "no"} else ""


def _normalize_settlement_result(value: Any) -> str:
    return str(value or "").strip().lower()


def _infer_settlement_revenue_cents(settlement_result: str, fill_count: int | None) -> int | None:
    if fill_count in (None, 0):
        return None
    if settlement_result in {"win", "won"}:
        return 100 * int(fill_count)
    if settlement_result in {"loss", "lost"}:
        return 0
    return None


def _infer_settlement_yes_price_cents(settlement_result: str, side: str) -> int | None:
    if settlement_result in {"win", "won"}:
        return 100 if side == "yes" else 0
    if settlement_result in {"loss", "lost"}:
        return 0 if side == "yes" else 100
    return None


def _infer_fee_cents(fill_price_cents: int | None, fill_count: int | None) -> int | None:
    if fill_price_cents is None or fill_count in (None, 0):
        return None
    return int(round(kalshi_fee_cents(fill_price_cents) * int(fill_count)))


class OracleAlphaCapture:
    """Domain-specific event capture over the canonical event ledger."""

    def __init__(self, path=None, *, ledger=None, logger=None):
        self.path = Path(path or DEFAULT_ORACLE_ALPHA_LEDGER_PATH)
        self.log = logger or _log
        self.ledger = ledger or EventLedger(self.path, logger=self.log)

    def record_source_event(
        self,
        event,
        *,
        source_name: str = "real_sports",
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        observed_at: Any = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_latency_probe",
    ) -> dict:
        observed_iso = (
            _iso_from_value(observed_at)
            or _iso_from_value(getattr(event, "timestamp", None))
            or _utc_now_iso()
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "source_event",
            "hypothesis_id": hypothesis_id,
            "source_name": source_name,
            "event_type": getattr(event, "event_type", ""),
            "game_id": getattr(event, "game_id", None),
            "player_id": getattr(event, "player_id", None),
            "observed_at": observed_iso,
            "event_timestamp_utc": _iso_from_value(getattr(event, "timestamp", None)) or observed_iso,
            "data": getattr(event, "data", {}),
        }
        if extra:
            payload.update(extra)
        event_id = (
            f"oracle-source:{payload['event_type']}:{payload.get('game_id') or 'na'}:"
            f"{payload.get('player_id') or 'na'}:{_stable_hash(payload)}"
        )
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_SOURCE_OBSERVATION,
            event_id=event_id,
            payload=payload,
            event_time=observed_iso,
            bot_name="oracle",
            ticker=str(payload["game_id"]) if payload.get("game_id") is not None else None,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=event_id,
        )
        return payload

    def record_probe_snapshot(
        self,
        *,
        snapshot_name: str,
        payload: dict,
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        observed_at: Any = None,
        source_artifact: str = "oracle_latency_probe",
    ) -> dict:
        observed_iso = _iso_from_value(observed_at) or _utc_now_iso()
        row = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "probe_snapshot",
            "hypothesis_id": hypothesis_id,
            "snapshot_name": snapshot_name,
            "observed_at": observed_iso,
        }
        row.update(payload)
        event_id = f"oracle-probe:{snapshot_name}:{_stable_hash(row)}"
        row["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_SOURCE_OBSERVATION,
            event_id=event_id,
            payload=row,
            event_time=observed_iso,
            bot_name="oracle",
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=event_id,
        )
        return row

    def record_source_failure(
        self,
        *,
        source_name: str,
        message: str,
        failure_code: str,
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        observed_at: Any = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_latency_probe",
    ) -> dict:
        observed_iso = _iso_from_value(observed_at) or _utc_now_iso()
        row = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "source_failure",
            "hypothesis_id": hypothesis_id,
            "source_name": source_name,
            "status": "failed",
            "failure_code": failure_code,
            "message": message,
            "observed_at": observed_iso,
        }
        if extra:
            row.update(extra)
        event_id = f"oracle-source-failure:{source_name}:{failure_code}:{_stable_hash(row)}"
        row["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_SOURCE_OBSERVATION,
            event_id=event_id,
            payload=row,
            event_time=observed_iso,
            bot_name="oracle",
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=event_id,
        )
        return row

    def record_quote_snapshot(
        self,
        *,
        ticker: str,
        yes_bid_cents: int,
        yes_ask_cents: int,
        yes_bid_depth: int,
        yes_ask_depth: int,
        quote_timestamp: Any = None,
        source_event_id: str | None = None,
        source_event_type: str | None = None,
        source_event_timestamp_utc: Any = None,
        game_id: int | str | None = None,
        player_id: int | None = None,
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        extra: dict | None = None,
        source_artifact: str = "oracle_latency_probe",
    ) -> dict:
        quote_iso = _iso_from_value(quote_timestamp) or _utc_now_iso()
        source_iso = _iso_from_value(source_event_timestamp_utc)
        quote_dt = dt.datetime.fromisoformat(quote_iso.replace("Z", "+00:00"))
        source_to_quote_ms = None
        if source_iso:
            source_dt = dt.datetime.fromisoformat(source_iso.replace("Z", "+00:00"))
            source_to_quote_ms = int(round((quote_dt - source_dt).total_seconds() * 1000))
        midpoint_cents = round((yes_bid_cents + yes_ask_cents) / 2)
        spread_cents = yes_ask_cents - yes_bid_cents
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "quote_snapshot",
            "hypothesis_id": hypothesis_id,
            "ticker": ticker,
            "game_id": game_id,
            "player_id": player_id,
            "source_event_id": source_event_id,
            "source_event_type": source_event_type,
            "source_event_timestamp_utc": source_iso,
            "quote_timestamp_utc": quote_iso,
            "source_to_quote_ms": source_to_quote_ms,
            "yes_bid_cents": yes_bid_cents,
            "yes_ask_cents": yes_ask_cents,
            "yes_bid_depth": yes_bid_depth,
            "yes_ask_depth": yes_ask_depth,
            "midpoint_cents": midpoint_cents,
            "spread_cents": spread_cents,
        }
        if extra:
            payload.update(extra)
        event_id = f"oracle-quote:{ticker}:{source_event_id or 'na'}:{_stable_hash(payload)}"
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_MARKET_SNAPSHOT,
            event_id=event_id,
            payload=payload,
            event_time=quote_iso,
            bot_name="oracle",
            ticker=ticker,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=source_event_id or event_id,
        )
        return payload

    def record_signal(
        self,
        *,
        hypothesis_id: str,
        signal_timestamp_utc: Any,
        market_ticker: str,
        side: str,
        model_prob: float | None,
        market_prob: float | None,
        entry_price: float | None,
        book: str = "",
        game_id: str | None = None,
        player_id: int | None = None,
        stat: str | None = None,
        team: str | None = None,
        source_event_id: str | None = None,
        source_event_type: str | None = None,
        source_event_timestamp_utc: Any = None,
        signal_id: str | None = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_signal_capture",
    ) -> dict:
        signal_iso = _iso_from_value(signal_timestamp_utc) or _utc_now_iso()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "signal",
            "hypothesis_id": hypothesis_id,
            "signal_timestamp_utc": signal_iso,
            "market_ticker": market_ticker,
            "book": book,
            "game_id": game_id,
            "player_id": player_id,
            "team": team,
            "stat": stat,
            "side": side,
            "source_event_id": source_event_id,
            "source_event_type": source_event_type,
            "source_event_timestamp_utc": _iso_from_value(source_event_timestamp_utc),
            "model_prob": model_prob,
            "market_prob": market_prob,
            "entry_price": entry_price,
        }
        if extra:
            payload.update(extra)
        signal_id = signal_id or f"oracle-signal:{market_ticker}:{_stable_hash(payload)}"
        payload["signal_id"] = signal_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_TRADE_DECISION,
            event_id=signal_id,
            payload=payload,
            event_time=signal_iso,
            bot_name="oracle",
            ticker=market_ticker,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=signal_id,
        )
        return payload

    def record_fill(
        self,
        *,
        order_id: str,
        market_ticker: str,
        fill_timestamp_utc: Any,
        fill_price_cents: int,
        fill_count: int,
        side: str = "",
        hypothesis_id: str = "",
        signal_id: str | None = None,
        source_event_id: str | None = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_signal_capture",
    ) -> dict:
        fill_iso = _iso_from_value(fill_timestamp_utc) or _utc_now_iso()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "fill",
            "hypothesis_id": hypothesis_id,
            "signal_id": signal_id,
            "source_event_id": source_event_id,
            "ticker": market_ticker,
            "side": side,
            "order_id": order_id,
            "fill_timestamp_utc": fill_iso,
            "fill_price_cents": fill_price_cents,
            "fill_count": fill_count,
        }
        if extra:
            payload.update(extra)
        event_id = f"oracle-fill:{order_id}:{_stable_hash(payload)}"
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_FILL,
            event_id=event_id,
            payload=payload,
            event_time=fill_iso,
            bot_name="oracle",
            ticker=market_ticker,
            order_id=order_id,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=signal_id or order_id,
        )
        return payload

    def _existing_event_id(
        self,
        event_type: str,
        *,
        order_id: str | None = None,
        ticker: str | None = None,
        signal_id: str | None = None,
    ) -> str | None:
        filters: list[str] = ["event_type = ?", "source_path = ?"]
        params: list[Any] = [event_type, str(self.path)]
        if order_id not in (None, ""):
            filters.append("order_id = ?")
            params.append(order_id)
        elif signal_id not in (None, ""):
            filters.append("legacy_key = ?")
            params.append(signal_id)
        elif ticker not in (None, ""):
            filters.append("ticker = ?")
            params.append(ticker)
        else:
            return None

        query = (
            "SELECT event_id "
            "FROM events "
            f"WHERE {' AND '.join(filters)} "
            "ORDER BY event_time DESC, updated_at DESC "
            "LIMIT 1"
        )
        self.ledger.ensure_schema()
        with self.ledger._connection() as conn:
            row = conn.execute(query, params).fetchone()
        return row["event_id"] if row else None

    def record_reconciled_order_submission(
        self,
        *,
        order_id: str | None,
        market_ticker: str,
        order_timestamp_utc: Any = None,
        signal_id: str | None = None,
        signal_timestamp_utc: Any = None,
        side: str = "",
        price_cents: int = 0,
        count: int = 0,
        book: str = "",
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        game_id: str | int | None = None,
        player_id: int | None = None,
        status: str = "",
        entry_type: str = "",
        expected_fill_probability: float | None = None,
        source_input_path: str | None = None,
        source_record_kind: str | None = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_alpha_reconcile",
    ) -> dict:
        submitted_iso = _iso_from_value(order_timestamp_utc) or _iso_from_value(signal_timestamp_utc) or _utc_now_iso()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "order_submission",
            "reconciled": True,
            "hypothesis_id": hypothesis_id,
            "signal_id": signal_id,
            "ticker": market_ticker,
            "market_ticker": market_ticker,
            "book": book,
            "game_id": game_id,
            "player_id": player_id,
            "side": side,
            "order_id": order_id,
            "timestamp": submitted_iso,
            "order_timestamp_utc": submitted_iso,
            "signal_timestamp_utc": _iso_from_value(signal_timestamp_utc),
            "price_cents": price_cents,
            "count": count,
            "status": status,
            "entry_type": entry_type,
            "expected_fill_probability": expected_fill_probability,
            "source_input_path": str(source_input_path) if source_input_path else None,
            "source_record_kind": source_record_kind,
        }
        if extra:
            payload.update(extra)

        event_id = self._existing_event_id(
            EVENT_TYPE_ORDER_SUBMITTED,
            order_id=order_id,
            ticker=market_ticker,
            signal_id=signal_id,
        )
        if event_id is None:
            event_key = order_id or signal_id or f"{market_ticker}:{submitted_iso}:{side}:{price_cents}:{count}"
            event_id = f"oracle-reconcile-order:{event_key}:{_reconciliation_key(payload)}"
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_ORDER_SUBMITTED,
            event_id=event_id,
            payload=payload,
            event_time=submitted_iso,
            bot_name="oracle",
            ticker=market_ticker,
            order_id=order_id,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=order_id or signal_id or event_id,
        )
        return payload

    def record_reconciled_fill(
        self,
        *,
        order_id: str,
        market_ticker: str,
        fill_timestamp_utc: Any = None,
        fill_price_cents: int,
        fill_count: int,
        side: str = "",
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        signal_id: str | None = None,
        source_input_path: str | None = None,
        source_record_kind: str | None = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_alpha_reconcile",
    ) -> dict:
        fill_iso = _iso_from_value(fill_timestamp_utc) or _utc_now_iso()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "fill",
            "reconciled": True,
            "hypothesis_id": hypothesis_id,
            "signal_id": signal_id,
            "source_event_id": signal_id,
            "source_input_path": str(source_input_path) if source_input_path else None,
            "source_record_kind": source_record_kind,
            "ticker": market_ticker,
            "side": side,
            "order_id": order_id,
            "fill_timestamp_utc": fill_iso,
            "fill_price_cents": fill_price_cents,
            "fill_count": fill_count,
        }
        if extra:
            payload.update(extra)

        event_id = self._existing_event_id(
            EVENT_TYPE_FILL,
            order_id=order_id,
            ticker=market_ticker,
            signal_id=signal_id,
        )
        if event_id is None:
            event_id = f"oracle-reconcile-fill:{order_id}:{_reconciliation_key(payload)}"
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_FILL,
            event_id=event_id,
            payload=payload,
            event_time=fill_iso,
            bot_name="oracle",
            ticker=market_ticker,
            order_id=order_id,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=order_id or signal_id or event_id,
        )
        return payload

    def record_reconciled_settlement(
        self,
        *,
        order_id: str | None,
        market_ticker: str,
        settlement_timestamp_utc: Any = None,
        signal_id: str | None = None,
        side: str = "",
        fill_price_cents: int | None = None,
        fill_count: int | None = None,
        book: str = "",
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        game_id: str | int | None = None,
        player_id: int | None = None,
        settlement_result: str | None = None,
        settlement_revenue_cents: int | None = None,
        fee_cents: int | None = None,
        close_price_cents: int | None = None,
        source_input_path: str | None = None,
        source_record_kind: str | None = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_alpha_reconcile",
    ) -> dict:
        settled_iso = _iso_from_value(settlement_timestamp_utc) or _utc_now_iso()
        normalized_side = _normalize_side(side)
        normalized_result = _normalize_settlement_result(settlement_result)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "settlement",
            "reconciled": True,
            "hypothesis_id": hypothesis_id,
            "signal_id": signal_id,
            "ticker": market_ticker,
            "market_ticker": market_ticker,
            "book": book,
            "game_id": game_id,
            "player_id": player_id,
            "side": normalized_side,
            "order_id": order_id,
            "timestamp": settled_iso,
            "settlement_timestamp_utc": settled_iso,
            "fill_price_cents": fill_price_cents,
            "fill_count": fill_count,
            "settlement_result": normalized_result or None,
            "settlement_revenue_cents": settlement_revenue_cents,
            "fee_cents": fee_cents,
            "close_price_cents": close_price_cents,
            "settlement_price_cents": close_price_cents,
            "source_input_path": str(source_input_path) if source_input_path else None,
            "source_record_kind": source_record_kind,
        }
        if extra:
            payload.update(extra)

        event_id = self._existing_event_id(
            EVENT_TYPE_SETTLEMENT,
            order_id=order_id,
            ticker=market_ticker,
            signal_id=signal_id,
        )
        if event_id is None:
            event_key = order_id or signal_id or f"{market_ticker}:{settled_iso}:{normalized_result or 'settled'}"
            event_id = f"oracle-reconcile-settlement:{event_key}:{_reconciliation_key(payload)}"
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_SETTLEMENT,
            event_id=event_id,
            payload=payload,
            event_time=settled_iso,
            bot_name="oracle",
            ticker=market_ticker,
            order_id=order_id,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=order_id or signal_id or event_id,
        )
        return payload

    def load_signal_index_by_order_id(
        self,
        *,
        hypothesis_id: str | None = DEFAULT_HYPOTHESIS_ID,
    ) -> dict[str, dict]:
        index: dict[str, dict] = {}
        for row in self.load_signal_rows(hypothesis_id=hypothesis_id):
            order_id = row.get("order_id")
            if order_id in (None, ""):
                continue
            index[str(order_id)] = row
        return index

    def reconcile_trade_record(
        self,
        trade_record: dict,
        *,
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        signal_id: str | None = None,
        source_input_path: str | None = None,
        source_record_kind: str | None = None,
    ) -> dict[str, dict | None]:
        if not isinstance(trade_record, dict):
            return {"order": None, "fill": None, "settlement": None}

        order_id = trade_record.get("order_id")
        ticker = trade_record.get("ticker") or trade_record.get("market_ticker")
        if ticker in (None, ""):
            return {"order": None, "fill": None, "settlement": None}

        side = _normalize_side(trade_record.get("side") or "yes")
        price_cents = _coerce_int(trade_record.get("price_cents"))
        if price_cents is None:
            price_cents = _coerce_int(trade_record.get("fill_price_cents")) or 0
        count = _coerce_int(trade_record.get("count"))
        if count is None:
            count = _coerce_int(trade_record.get("fill_count")) or 0
        timestamp = trade_record.get("timestamp") or trade_record.get("created_time")
        status = str(trade_record.get("status") or "").lower()
        fill_price_cents = _coerce_int(trade_record.get("fill_price_cents"))
        fill_count = _coerce_int(trade_record.get("fill_count"))
        settlement_result = _normalize_settlement_result(trade_record.get("settlement_result"))
        settlement_revenue_cents = _coerce_int(
            trade_record.get("settlement_revenue_cents")
            if trade_record.get("settlement_revenue_cents") is not None
            else trade_record.get("revenue_cents")
        )
        if settlement_revenue_cents is None:
            settlement_revenue_cents = _infer_settlement_revenue_cents(settlement_result, fill_count or count)
        fee_cents = _coerce_int(
            trade_record.get("fee_cents")
            if trade_record.get("fee_cents") is not None
            else trade_record.get("fees_cents")
        )
        if fee_cents is None:
            fee_cents = _infer_fee_cents(fill_price_cents or price_cents, fill_count or count)
        close_price_cents = _coerce_int(
            trade_record.get("close_price_cents")
            if trade_record.get("close_price_cents") is not None
            else trade_record.get("settlement_price_cents")
        )
        if close_price_cents is None:
            close_price_cents = _coerce_int(
                trade_record.get("close_price")
                if trade_record.get("close_price") is not None
                else trade_record.get("settlement_price")
            )
        if close_price_cents is None:
            close_price_cents = _infer_settlement_yes_price_cents(settlement_result, side)
        if not status:
            if fill_price_cents is not None and fill_count and fill_count > 0:
                status = "filled"
            elif order_id:
                status = "resting"
            else:
                status = "unknown"
        linked_signal_id = signal_id or trade_record.get("signal_id")
        order = self.record_reconciled_order_submission(
            order_id=order_id,
            market_ticker=str(ticker),
            order_timestamp_utc=timestamp,
            signal_id=linked_signal_id,
            side=side,
            price_cents=price_cents,
            count=count,
            book=str(trade_record.get("book") or ""),
            hypothesis_id=hypothesis_id,
            game_id=trade_record.get("game_id"),
            player_id=_coerce_int(trade_record.get("player_id")),
            status=status,
            entry_type=str(trade_record.get("entry_type") or trade_record.get("mode") or ""),
            expected_fill_probability=_coerce_float(
                trade_record.get("expected_fill_probability")
                if trade_record.get("expected_fill_probability") is not None
                else trade_record.get("predicted_fill_probability")
            ),
            source_input_path=source_input_path,
            source_record_kind=source_record_kind or trade_record.get("record_kind"),
            extra={
                "source_trade_status": trade_record.get("status"),
                "source_trade_action": trade_record.get("action"),
                "source_trade_fill_price_cents": trade_record.get("fill_price_cents"),
                "source_trade_fill_count": trade_record.get("fill_count"),
                "source_trade_settlement_result": trade_record.get("settlement_result"),
                "source_trade_settlement_revenue_cents": trade_record.get("settlement_revenue_cents"),
                "source_trade_realized_edge": trade_record.get("realized_edge"),
            },
        )

        fill = None
        if order_id not in (None, "") and fill_price_cents is not None and fill_count is not None and fill_count > 0:
            fill = self.record_reconciled_fill(
                order_id=str(order_id),
                market_ticker=str(ticker),
                fill_timestamp_utc=trade_record.get("fill_timestamp_utc") or trade_record.get("fill_time") or timestamp,
                fill_price_cents=fill_price_cents,
                fill_count=fill_count,
                side=side,
                hypothesis_id=hypothesis_id,
                signal_id=linked_signal_id,
                source_input_path=source_input_path,
                source_record_kind=source_record_kind or trade_record.get("record_kind"),
                extra={
                    "source_trade_status": trade_record.get("status"),
                    "source_trade_action": trade_record.get("action"),
                },
            )

        settlement = None
        has_settlement = any(
            value not in (None, "", 0)
            for value in (
                settlement_result,
                settlement_revenue_cents,
                close_price_cents,
                trade_record.get("settled_at"),
                trade_record.get("verified_at"),
            )
        ) or status == "settled"
        if has_settlement:
            settlement = self.record_reconciled_settlement(
                order_id=str(order_id) if order_id not in (None, "") else None,
                market_ticker=str(ticker),
                settlement_timestamp_utc=(
                    trade_record.get("settled_at")
                    or trade_record.get("verified_at")
                    or trade_record.get("timestamp")
                    or trade_record.get("created_time")
                ),
                signal_id=linked_signal_id,
                side=side,
                fill_price_cents=fill_price_cents or price_cents,
                fill_count=fill_count or count,
                book=str(trade_record.get("book") or ""),
                hypothesis_id=hypothesis_id,
                game_id=trade_record.get("game_id"),
                player_id=_coerce_int(trade_record.get("player_id")),
                settlement_result=settlement_result,
                settlement_revenue_cents=settlement_revenue_cents,
                fee_cents=fee_cents,
                close_price_cents=close_price_cents,
                source_input_path=source_input_path,
                source_record_kind=source_record_kind or trade_record.get("record_kind"),
                extra={
                    "source_trade_status": trade_record.get("status"),
                    "source_trade_action": trade_record.get("action"),
                    "source_trade_realized_edge": trade_record.get("realized_edge"),
                },
            )

        return {"order": order, "fill": fill, "settlement": settlement}

    def reconcile_trade_records(
        self,
        trade_records: list[dict],
        *,
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        source_input_path: str | None = None,
        signal_index_by_order_id: dict[str, dict] | None = None,
        source_record_kind: str | None = None,
    ) -> dict[str, int]:
        signal_index_by_order_id = signal_index_by_order_id or {}
        summary = {
            "trade_rows": 0,
            "order_rows": 0,
            "fill_rows": 0,
            "settlement_rows": 0,
            "linked_signal_rows": 0,
            "unlinked_order_rows": 0,
        }
        for trade_record in trade_records:
            if not isinstance(trade_record, dict):
                continue
            order_id = trade_record.get("order_id")
            linked_signal = None
            if order_id not in (None, ""):
                linked_signal = signal_index_by_order_id.get(str(order_id))
            result = self.reconcile_trade_record(
                trade_record,
                hypothesis_id=hypothesis_id,
                signal_id=(linked_signal or {}).get("signal_id"),
                source_input_path=trade_record.get("source_input_path") or source_input_path,
                source_record_kind=source_record_kind or trade_record.get("record_kind"),
            )
            summary["trade_rows"] += 1
            if result.get("order") is not None:
                summary["order_rows"] += 1
                if result["order"].get("signal_id") not in (None, ""):
                    summary["linked_signal_rows"] += 1
                else:
                    summary["unlinked_order_rows"] += 1
            if result.get("fill") is not None:
                summary["fill_rows"] += 1
            if result.get("settlement") is not None:
                summary["settlement_rows"] += 1
        return summary

    def record_order_submission(
        self,
        *,
        order_id: str | None,
        signal_id: str | None = None,
        signal_timestamp_utc: Any = None,
        order_timestamp_utc: Any = None,
        market_ticker: str,
        side: str,
        price_cents: int,
        count: int,
        book: str = "",
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        game_id: str | int | None = None,
        player_id: int | None = None,
        status: str = "",
        entry_type: str = "",
        expected_fill_probability: float | None = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_signal_capture",
    ) -> dict:
        submitted_iso = _iso_from_value(order_timestamp_utc) or _iso_from_value(signal_timestamp_utc) or _utc_now_iso()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "order_submission",
            "hypothesis_id": hypothesis_id,
            "signal_id": signal_id,
            "ticker": market_ticker,
            "market_ticker": market_ticker,
            "book": book,
            "game_id": game_id,
            "player_id": player_id,
            "side": side,
            "order_id": order_id,
            "timestamp": submitted_iso,
            "order_timestamp_utc": submitted_iso,
            "signal_timestamp_utc": _iso_from_value(signal_timestamp_utc),
            "price_cents": price_cents,
            "count": count,
            "status": status,
            "entry_type": entry_type,
            "expected_fill_probability": expected_fill_probability,
        }
        if extra:
            payload.update(extra)
        event_id = f"oracle-order:{order_id or signal_id or market_ticker}:{_stable_hash(payload)}"
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_ORDER_SUBMITTED,
            event_id=event_id,
            payload=payload,
            event_time=submitted_iso,
            bot_name="oracle",
            ticker=market_ticker,
            order_id=order_id,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=order_id or signal_id or event_id,
        )
        return payload

    def record_order_update(
        self,
        *,
        order_id: str,
        market_ticker: str,
        update_timestamp_utc: Any = None,
        signal_id: str | None = None,
        signal_timestamp_utc: Any = None,
        side: str = "",
        price_cents: int | None = None,
        count: int | None = None,
        fill_price_cents: int | None = None,
        fill_count: int | None = None,
        book: str = "",
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        game_id: str | int | None = None,
        player_id: int | None = None,
        status: str = "",
        entry_type: str = "",
        reason: str | None = None,
        expected_fill_probability: float | None = None,
        extra: dict | None = None,
        source_artifact: str = "oracle_signal_capture",
    ) -> dict:
        updated_iso = _iso_from_value(update_timestamp_utc) or _iso_from_value(signal_timestamp_utc) or _utc_now_iso()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "record_kind": "order_update",
            "hypothesis_id": hypothesis_id,
            "signal_id": signal_id,
            "ticker": market_ticker,
            "market_ticker": market_ticker,
            "book": book,
            "game_id": game_id,
            "player_id": player_id,
            "side": side,
            "order_id": order_id,
            "timestamp": updated_iso,
            "update_timestamp_utc": updated_iso,
            "signal_timestamp_utc": _iso_from_value(signal_timestamp_utc),
            "price_cents": price_cents,
            "count": count,
            "fill_price_cents": fill_price_cents,
            "fill_count": fill_count,
            "status": status,
            "entry_type": entry_type,
            "reason": reason,
            "expected_fill_probability": expected_fill_probability,
        }
        if extra:
            payload.update(extra)
        event_id = f"oracle-order-update:{order_id}:{_stable_hash(payload)}"
        payload["event_id"] = event_id
        self.ledger.record_event(
            event_type=EVENT_TYPE_ORDER_UPDATE,
            event_id=event_id,
            payload=payload,
            event_time=updated_iso,
            bot_name="oracle",
            ticker=market_ticker,
            order_id=order_id,
            source_artifact=source_artifact,
            source_path=self.path,
            legacy_key=order_id or signal_id or event_id,
        )
        return payload

    def _fetch_rows(
        self,
        event_type: str,
        *,
        hypothesis_id: str | None = None,
        start: Any = None,
        end: Any = None,
    ) -> list[dict]:
        rows = self.ledger._fetch_event_payloads(
            event_type,
            source_path=self.path,
            start=start,
            end=end,
        )
        if hypothesis_id is None:
            return rows
        return [row for row in rows if row.get("hypothesis_id") == hypothesis_id]

    def load_signal_rows(
        self,
        *,
        hypothesis_id: str | None = DEFAULT_HYPOTHESIS_ID,
        start: Any = None,
        end: Any = None,
    ) -> list[dict]:
        return self._fetch_rows(
            EVENT_TYPE_TRADE_DECISION,
            hypothesis_id=hypothesis_id,
            start=start,
            end=end,
        )

    def load_order_rows(
        self,
        *,
        hypothesis_id: str | None = DEFAULT_HYPOTHESIS_ID,
        start: Any = None,
        end: Any = None,
    ) -> list[dict]:
        return self._fetch_rows(
            EVENT_TYPE_ORDER_SUBMITTED,
            hypothesis_id=hypothesis_id,
            start=start,
            end=end,
        )

    def load_order_update_rows(
        self,
        *,
        hypothesis_id: str | None = DEFAULT_HYPOTHESIS_ID,
        start: Any = None,
        end: Any = None,
    ) -> list[dict]:
        return self._fetch_rows(
            EVENT_TYPE_ORDER_UPDATE,
            hypothesis_id=hypothesis_id,
            start=start,
            end=end,
        )

    def load_fill_rows(
        self,
        *,
        hypothesis_id: str | None = DEFAULT_HYPOTHESIS_ID,
        start: Any = None,
        end: Any = None,
    ) -> list[dict]:
        return self._fetch_rows(
            EVENT_TYPE_FILL,
            hypothesis_id=hypothesis_id,
            start=start,
            end=end,
        )

    def load_settlement_rows(
        self,
        *,
        hypothesis_id: str | None = DEFAULT_HYPOTHESIS_ID,
        start: Any = None,
        end: Any = None,
    ) -> list[dict]:
        return self._fetch_rows(
            EVENT_TYPE_SETTLEMENT,
            hypothesis_id=hypothesis_id,
            start=start,
            end=end,
        )

    def load_shadow_trade_rows(
        self,
        *,
        hypothesis_id: str | None = DEFAULT_HYPOTHESIS_ID,
        start: Any = None,
        end: Any = None,
    ) -> dict[str, list[dict]]:
        return {
            "signals": self.load_signal_rows(hypothesis_id=hypothesis_id, start=start, end=end),
            "orders": self.load_order_rows(hypothesis_id=hypothesis_id, start=start, end=end),
            "fills": self.load_fill_rows(hypothesis_id=hypothesis_id, start=start, end=end),
            "settlements": self.load_settlement_rows(hypothesis_id=hypothesis_id, start=start, end=end),
        }


__all__ = [
    "DEFAULT_HYPOTHESIS_ID",
    "DEFAULT_ORACLE_ALPHA_LEDGER_PATH",
    "OracleAlphaCapture",
]
