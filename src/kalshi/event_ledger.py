"""Phase 4 canonical event ledger backed by SQLite."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_LEDGER_PATH = PROJECT_DIR / "data" / "event-ledger.sqlite3"
LEDGER_SCHEMA_VERSION = 1

EVENT_TYPE_SOURCE_OBSERVATION = "source_observation"
EVENT_TYPE_MARKET_SNAPSHOT = "market_snapshot"
EVENT_TYPE_FORECAST_SNAPSHOT = "forecast_snapshot"
EVENT_TYPE_TRADE_DECISION = "trade_decision"
EVENT_TYPE_BUDGET_DECISION = "budget_decision"
EVENT_TYPE_ORDER_SUBMITTED = "order_submitted"
EVENT_TYPE_ORDER_UPDATE = "order_update"
EVENT_TYPE_FILL = "fill"
EVENT_TYPE_POSITION_SNAPSHOT = "position_snapshot"
EVENT_TYPE_SETTLEMENT = "settlement"
EVENT_TYPE_VERIFICATION_RESULT = "verification_result"
EVENT_TYPE_POST_TRADE_ATTRIBUTION = "post_trade_attribution"

_log = logging.getLogger("event-ledger")


def _utc_now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _json_dumps(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def _payload_hash(data):
    return hashlib.sha256(_json_dumps(data).encode("utf-8")).hexdigest()


def _record_hash(records):
    return hashlib.sha256(_json_dumps(records).encode("utf-8")).hexdigest()


def _safe_path(path):
    return str(Path(path)) if path else None


def _event_date(ts):
    if not ts or not isinstance(ts, str):
        return None
    return ts[:10]


def _trade_event_key(trade, source_path=None):
    order_id = trade.get("order_id")
    if order_id:
        return f"order:{order_id}"
    parts = [
        _safe_path(source_path),
        trade.get("timestamp", ""),
        trade.get("ticker", ""),
        trade.get("action", ""),
        trade.get("side", ""),
        str(trade.get("price_cents", "")),
        str(trade.get("count", "")),
    ]
    return "trade:" + "|".join(str(p) for p in parts)


def _decision_event_key(decision, source_path=None):
    parts = [
        _safe_path(source_path),
        decision.get("timestamp", ""),
        decision.get("ticker", ""),
        decision.get("side", ""),
        decision.get("action", ""),
        decision.get("reason", ""),
    ]
    return "decision:" + "|".join(str(p) for p in parts)


def _budget_event_key(record):
    parts = [
        record.get("timestamp", ""),
        record.get("bot_name", ""),
        record.get("ticker", ""),
        str(record.get("approved", "")),
        record.get("reason", ""),
    ]
    return "budget:" + "|".join(str(p) for p in parts)


def _forecast_event_key(record, category):
    parts = [
        category,
        record.get("city", ""),
        record.get("date", ""),
        record.get("record_kind", record.get("mode", "")),
        record.get("recorded_at", ""),
        str(record.get("threshold", record.get("threshold_f", ""))),
        record.get("direction", ""),
        str(record.get("days_out", "")),
    ]
    return "forecast:" + "|".join(str(p) for p in parts)


def _verification_event_key(record, category):
    parts = [
        category,
        record.get("city", ""),
        record.get("date", ""),
        record.get("record_kind", record.get("mode", "")),
        record.get("recorded_at", ""),
        record.get("verified_at", ""),
        str(record.get("threshold", record.get("threshold_f", ""))),
        record.get("direction", ""),
        str(record.get("days_out", "")),
    ]
    return "verify:" + "|".join(str(p) for p in parts)


class EventLedger:
    """SQLite-backed canonical ledger with dual-write helpers and legacy views."""

    def __init__(self, path=None, logger=None):
        self.path = Path(path or os.environ.get("KALSHI_LEDGER_PATH") or DEFAULT_LEDGER_PATH)
        self.log = logger or _log

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_schema(self):
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_date TEXT,
                    bot_name TEXT,
                    ticker TEXT,
                    order_id TEXT,
                    source_artifact TEXT,
                    source_path TEXT,
                    legacy_key TEXT,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_events_type_time
                    ON events(event_type, event_time);
                CREATE INDEX IF NOT EXISTS idx_events_source
                    ON events(source_path, event_type, event_time);
                CREATE INDEX IF NOT EXISTS idx_events_ticker
                    ON events(ticker, event_type, event_time);
                CREATE INDEX IF NOT EXISTS idx_events_order
                    ON events(order_id, event_type, event_time);

                CREATE TABLE IF NOT EXISTS parity_reports (
                    run_at TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    PRIMARY KEY (run_at, scope)
                );
                """
            )
            conn.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(LEDGER_SCHEMA_VERSION),),
            )
            conn.commit()

    def record_event(
        self,
        *,
        event_type,
        event_id,
        payload,
        event_time=None,
        bot_name=None,
        ticker=None,
        order_id=None,
        source_artifact=None,
        source_path=None,
        legacy_key=None,
    ):
        self.ensure_schema()
        event_time = event_time or _utc_now_iso()
        payload_json = _json_dumps(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        event_date = _event_date(event_time)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    event_id, event_type, event_time, event_date, bot_name, ticker,
                    order_id, source_artifact, source_path, legacy_key,
                    payload_json, payload_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    event_type=excluded.event_type,
                    event_time=excluded.event_time,
                    event_date=excluded.event_date,
                    bot_name=excluded.bot_name,
                    ticker=excluded.ticker,
                    order_id=excluded.order_id,
                    source_artifact=excluded.source_artifact,
                    source_path=excluded.source_path,
                    legacy_key=excluded.legacy_key,
                    payload_json=excluded.payload_json,
                    payload_hash=excluded.payload_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    event_id,
                    event_type,
                    event_time,
                    event_date,
                    bot_name,
                    ticker,
                    order_id,
                    source_artifact,
                    _safe_path(source_path),
                    legacy_key,
                    payload_json,
                    payload_hash,
                    _utc_now_iso(),
                ),
            )
            conn.commit()

    def record_trade_decision(self, decision, source_path=None):
        event_id = _decision_event_key(decision, source_path)
        self.record_event(
            event_type=EVENT_TYPE_TRADE_DECISION,
            event_id=event_id,
            payload=dict(decision),
            event_time=decision.get("timestamp"),
            bot_name=decision.get("source_bot"),
            ticker=decision.get("ticker"),
            source_artifact="decision_log",
            source_path=source_path,
            legacy_key=event_id,
        )

    def record_order_submitted(self, trade, source_path=None):
        payload = dict(trade)
        event_id = _trade_event_key(payload, source_path)
        self.record_event(
            event_type=EVENT_TYPE_ORDER_SUBMITTED,
            event_id=event_id,
            payload=payload,
            event_time=payload.get("timestamp"),
            bot_name=payload.get("source_bot"),
            ticker=payload.get("ticker"),
            order_id=payload.get("order_id"),
            source_artifact="trade_log",
            source_path=source_path,
            legacy_key=event_id,
        )
        if payload.get("status"):
            self.record_event(
                event_type=EVENT_TYPE_ORDER_UPDATE,
                event_id=f"{event_id}:status",
                payload={
                    "timestamp": payload.get("timestamp"),
                    "ticker": payload.get("ticker"),
                    "order_id": payload.get("order_id"),
                    "status": payload.get("status"),
                    "action": payload.get("action"),
                    "side": payload.get("side"),
                },
                event_time=payload.get("timestamp"),
                bot_name=payload.get("source_bot"),
                ticker=payload.get("ticker"),
                order_id=payload.get("order_id"),
                source_artifact="trade_log",
                source_path=source_path,
                legacy_key=event_id,
            )
        if payload.get("best_bid") is not None or payload.get("best_ask") is not None:
            self.record_event(
                event_type=EVENT_TYPE_MARKET_SNAPSHOT,
                event_id=f"{event_id}:market",
                payload={
                    "timestamp": payload.get("timestamp"),
                    "ticker": payload.get("ticker"),
                    "order_id": payload.get("order_id"),
                    "best_bid": payload.get("best_bid"),
                    "best_ask": payload.get("best_ask"),
                    "spread": payload.get("spread"),
                    "volume": payload.get("volume"),
                },
                event_time=payload.get("timestamp"),
                bot_name=payload.get("source_bot"),
                ticker=payload.get("ticker"),
                order_id=payload.get("order_id"),
                source_artifact="trade_log",
                source_path=source_path,
                legacy_key=event_id,
            )

    def record_fill(self, fill_record, source_path=None):
        order_id = fill_record.get("order_id")
        if not order_id:
            return
        event_id = f"fill:{order_id}"
        self.record_event(
            event_type=EVENT_TYPE_FILL,
            event_id=event_id,
            payload=dict(fill_record),
            event_time=fill_record.get("timestamp") or fill_record.get("created_time") or _utc_now_iso(),
            bot_name=fill_record.get("source_bot"),
            ticker=fill_record.get("ticker"),
            order_id=order_id,
            source_artifact="trade_log",
            source_path=source_path,
            legacy_key=order_id,
        )

    def record_settlement(self, settlement_record, source_path=None):
        payload = dict(settlement_record)
        event_id = _trade_event_key(payload, source_path) + ":settlement"
        self.record_event(
            event_type=EVENT_TYPE_SETTLEMENT,
            event_id=event_id,
            payload=payload,
            event_time=payload.get("timestamp") or payload.get("verified_at") or _utc_now_iso(),
            bot_name=payload.get("source_bot"),
            ticker=payload.get("ticker"),
            order_id=payload.get("order_id"),
            source_artifact="trade_log",
            source_path=source_path,
            legacy_key=_trade_event_key(payload, source_path),
        )

    def record_budget_decision(self, decision):
        event_id = _budget_event_key(decision)
        self.record_event(
            event_type=EVENT_TYPE_BUDGET_DECISION,
            event_id=event_id,
            payload=dict(decision),
            event_time=decision.get("timestamp"),
            bot_name=decision.get("bot_name"),
            ticker=decision.get("ticker"),
            source_artifact="allocator_state",
            legacy_key=event_id,
        )

    def record_forecast_snapshot(self, record, source_path=None, category="weather_forecast"):
        event_id = _forecast_event_key(record, category)
        payload = dict(record)
        payload["category"] = category
        self.record_event(
            event_type=EVENT_TYPE_FORECAST_SNAPSHOT,
            event_id=event_id,
            payload=payload,
            event_time=payload.get("recorded_at"),
            ticker=payload.get("city"),
            source_artifact="verification_state",
            source_path=source_path,
            legacy_key=event_id,
        )

    def record_verification_result(self, record, source_path=None, category="weather_verification"):
        event_id = _verification_event_key(record, category)
        payload = dict(record)
        payload["category"] = category
        self.record_event(
            event_type=EVENT_TYPE_VERIFICATION_RESULT,
            event_id=event_id,
            payload=payload,
            event_time=payload.get("verified_at") or payload.get("recorded_at"),
            ticker=payload.get("city"),
            source_artifact="verification_state",
            source_path=source_path,
            legacy_key=event_id,
        )

    def record_post_trade_attribution(self, report, report_name="daily_attribution"):
        timestamp = report.get("generated_at") or _utc_now_iso()
        event_id = f"attribution:{report_name}:{timestamp}"
        payload = dict(report)
        payload.setdefault("report_name", report_name)
        self.record_event(
            event_type=EVENT_TYPE_POST_TRADE_ATTRIBUTION,
            event_id=event_id,
            payload=payload,
            event_time=timestamp,
            source_artifact="attribution_report",
            legacy_key=event_id,
        )

    def record_source_observation(self, source_name, snapshot_name, content_hash, observed_at=None, extra=None):
        observed_at = observed_at or _utc_now_iso()
        payload = {
            "source_name": source_name,
            "snapshot_name": snapshot_name,
            "content_hash": content_hash,
            "observed_at": observed_at,
        }
        if extra:
            payload.update(extra)
        event_id = f"source:{source_name}:{snapshot_name}"
        self.record_event(
            event_type=EVENT_TYPE_SOURCE_OBSERVATION,
            event_id=event_id,
            payload=payload,
            event_time=observed_at,
            ticker=source_name,
            source_artifact="source_snapshot",
            legacy_key=event_id,
        )

    def record_position_snapshot(self, positions, observed_at=None, source="position_monitor"):
        observed_at = observed_at or _utc_now_iso()
        event_id = f"positions:{source}:{observed_at}"
        self.record_event(
            event_type=EVENT_TYPE_POSITION_SNAPSHOT,
            event_id=event_id,
            payload={"positions": list(positions), "observed_at": observed_at, "source": source},
            event_time=observed_at,
            source_artifact="positions",
            legacy_key=event_id,
        )

    def _fetch_event_payloads(self, event_type, *, source_path=None):
        self.ensure_schema()
        query = """
            SELECT payload_json, event_time, source_path
            FROM events
            WHERE event_type = ?
        """
        params = [event_type]
        if source_path is not None:
            query += " AND source_path = ?"
            params.append(_safe_path(source_path))
        query += " ORDER BY event_time ASC, event_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get_trade_records(self, source_path=None):
        orders = self._fetch_event_payloads(EVENT_TYPE_ORDER_SUBMITTED, source_path=source_path)
        fills = self._fetch_event_payloads(EVENT_TYPE_FILL, source_path=source_path)
        settlements = self._fetch_event_payloads(EVENT_TYPE_SETTLEMENT, source_path=source_path)
        fill_by_order = {
            record.get("order_id"): record
            for record in fills
            if record.get("order_id")
        }
        settlement_by_key = {}
        for record in settlements:
            order_id = record.get("order_id")
            if order_id:
                settlement_by_key[order_id] = record
            else:
                settlement_by_key[_trade_event_key(record, source_path)] = record

        result = []
        for order in orders:
            merged = dict(order)
            order_id = order.get("order_id")
            settle = settlement_by_key.get(order_id) or settlement_by_key.get(_trade_event_key(order, source_path))
            fill = fill_by_order.get(order_id)
            if fill:
                if fill.get("fill_price_cents") is not None:
                    merged["fill_price_cents"] = fill.get("fill_price_cents")
                if fill.get("fill_count") is not None:
                    merged["fill_count"] = fill.get("fill_count")
            if settle:
                for key in (
                    "settlement_result",
                    "settlement_revenue_cents",
                    "fill_price_cents",
                    "realized_edge",
                ):
                    if key in settle:
                        merged[key] = settle.get(key)
            result.append(merged)
        return result

    def get_decision_records(self, source_path=None):
        return self._fetch_event_payloads(EVENT_TYPE_TRADE_DECISION, source_path=source_path)

    def get_source_observation_records(self, source_name=None):
        records = self._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)
        if source_name is None:
            return records
        return [record for record in records if record.get("source_name") == source_name]

    def get_verification_records(self, category=None):
        records = self._fetch_event_payloads(EVENT_TYPE_VERIFICATION_RESULT)
        if category is None:
            return records
        return [record for record in records if record.get("category") == category]

    def get_forecast_snapshot_records(self, category=None):
        records = self._fetch_event_payloads(EVENT_TYPE_FORECAST_SNAPSHOT)
        if category is None:
            return records
        return [record for record in records if record.get("category") == category]

    def get_trades_by_bot(self, trade_specs):
        grouped = defaultdict(list)
        source_to_bot = {str(Path(spec["path"])): spec.get("bot", "unknown") for spec in trade_specs}
        for spec in trade_specs:
            grouped[spec.get("bot", "unknown")].extend(self.get_trade_records(spec["path"]))
        result = []
        for spec in trade_specs:
            bot = spec.get("bot", "unknown")
            result.append({"label": bot, "trades": list(grouped.get(bot, []))})
        return result

    def get_actual_source_summary(self, lookback_days=30, category="weather_verification"):
        cutoff = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
        records = [
            record for record in self.get_verification_records(category=category)
            if isinstance(record.get("date"), str) and record["date"] >= cutoff
        ]
        counts = {}
        for record in records:
            source = record.get("actual_source") or "missing"
            counts[source] = counts.get(source, 0) + 1
        total = len(records)
        shares = {
            key: round(value / total, 3)
            for key, value in sorted(counts.items())
        } if total else {}
        return {
            "lookback_days": lookback_days,
            "total": total,
            "counts": dict(sorted(counts.items())),
            "shares": shares,
        }

    def get_nws_crosscheck_summary(self, lookback_days=30, tolerance_f=0.11):
        cutoff = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
        records = [
            record for record in self.get_verification_records(category="weather_nws_crosscheck")
            if isinstance(record.get("date"), str) and record["date"] >= cutoff
        ]
        return {
            "lookback_days": lookback_days,
            "overall": self._summarize_crosscheck_records(records, tolerance_f=tolerance_f),
            "per_city": {
                city: self._summarize_crosscheck_records(rows, tolerance_f=tolerance_f)
                for city, rows in sorted(self._group_by(records, "city").items())
            },
            "per_mode": {
                mode: self._summarize_crosscheck_records(rows, tolerance_f=tolerance_f)
                for mode, rows in sorted(self._group_by(records, "mode").items())
            },
            "per_days_out": {
                str(days_out): self._summarize_crosscheck_records(rows, tolerance_f=tolerance_f)
                for days_out, rows in sorted(self._group_by(records, "days_out").items())
            },
        }

    @staticmethod
    def _group_by(records, key):
        grouped = {}
        for record in records:
            grouped.setdefault(record.get(key), []).append(record)
        return grouped

    @staticmethod
    def _temps_match(lhs, rhs, tolerance_f=0.11):
        try:
            return lhs is not None and rhs is not None and abs(float(lhs) - float(rhs)) <= tolerance_f
        except (TypeError, ValueError):
            return False

    @classmethod
    def _summarize_crosscheck_records(cls, records, tolerance_f=0.11):
        if not records:
            return {
                "n": 0,
                "open_meteo_mae": None,
                "nws_mae": None,
                "open_meteo_bias": None,
                "nws_bias": None,
                "mean_open_meteo_minus_nws": None,
                "open_meteo_better": 0,
                "nws_better": 0,
                "ties": 0,
                "open_meteo_better_share": None,
                "nws_better_share": None,
                "tie_share": None,
            }
        om_errors = [float(r["open_meteo_error"]) for r in records if r.get("open_meteo_error") is not None]
        nws_errors = [float(r["nws_error"]) for r in records if r.get("nws_error") is not None]
        diffs = [float(r["open_meteo_minus_nws"]) for r in records if r.get("open_meteo_minus_nws") is not None]
        open_meteo_better = 0
        nws_better = 0
        ties = 0
        for record in records:
            winner = record.get("winner")
            if winner == "open_meteo":
                open_meteo_better += 1
            elif winner == "nws":
                nws_better += 1
            elif winner == "tie":
                ties += 1
            else:
                om_abs = record.get("open_meteo_abs_error")
                nws_abs = record.get("nws_abs_error")
                if cls._temps_match(om_abs, nws_abs, tolerance_f):
                    ties += 1
                elif om_abs is not None and nws_abs is not None and om_abs < nws_abs:
                    open_meteo_better += 1
                else:
                    nws_better += 1
        total = len(records)
        return {
            "n": total,
            "open_meteo_mae": round(sum(abs(e) for e in om_errors) / len(om_errors), 2) if om_errors else None,
            "nws_mae": round(sum(abs(e) for e in nws_errors) / len(nws_errors), 2) if nws_errors else None,
            "open_meteo_bias": round(sum(om_errors) / len(om_errors), 2) if om_errors else None,
            "nws_bias": round(sum(nws_errors) / len(nws_errors), 2) if nws_errors else None,
            "mean_open_meteo_minus_nws": round(sum(diffs) / len(diffs), 2) if diffs else None,
            "open_meteo_better": open_meteo_better,
            "nws_better": nws_better,
            "ties": ties,
            "open_meteo_better_share": round(open_meteo_better / total, 3) if total else None,
            "nws_better_share": round(nws_better / total, 3) if total else None,
            "tie_share": round(ties / total, 3) if total else None,
        }

    def build_parity_report(
        self,
        *,
        trade_specs=None,
        decision_specs=None,
        verification_specs=None,
    ):
        report = {"generated_at": _utc_now_iso(), "trade_logs": [], "decision_logs": [], "verification": []}
        trade_specs = trade_specs or []
        decision_specs = decision_specs or []
        verification_specs = verification_specs or []

        for spec in trade_specs:
            path = Path(spec["path"])
            legacy = self._load_json_list(path)
            ledger = self.get_trade_records(path)
            report["trade_logs"].append(self._compare_records(path, legacy, ledger))

        for spec in decision_specs:
            path = Path(spec["path"])
            legacy = self._load_json_list(path)
            ledger = self.get_decision_records(path)
            report["decision_logs"].append(self._compare_records(path, legacy, ledger))

        for spec in verification_specs:
            path = Path(spec["path"])
            category = spec.get("category")
            legacy_state = self._load_json_dict(path)
            legacy_verified = [
                self._normalize_verification_row(record)
                for record in legacy_state.get("verified", [])
            ]
            ledger_verified = [
                self._normalize_verification_row(record)
                for record in self.get_verification_records(category=category)
            ]
            report["verification"].append({
                "path": str(path),
                "legacy_verified_count": len(legacy_verified),
                "ledger_verified_count": len(ledger_verified),
                "verified_hash_match": _record_hash(legacy_verified) == _record_hash(ledger_verified),
            })

        return report

    def save_parity_report(self, report, scope="default"):
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO parity_reports(run_at, scope, report_json)
                VALUES (?, ?, ?)
                """,
                (report.get("generated_at", _utc_now_iso()), scope, _json_dumps(report)),
            )
            conn.commit()

    @staticmethod
    def _load_json_list(path):
        try:
            if not Path(path).exists():
                return []
            data = json.loads(Path(path).read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def _load_json_dict(path):
        try:
            if not Path(path).exists():
                return {}
            data = json.loads(Path(path).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _compare_records(path, legacy, ledger):
        return {
            "path": str(path),
            "legacy_count": len(legacy),
            "ledger_count": len(ledger),
            "count_match": len(legacy) == len(ledger),
            "hash_match": _record_hash(legacy) == _record_hash(ledger),
        }

    @staticmethod
    def _normalize_verification_row(record):
        normalized = dict(record)
        normalized.pop("category", None)
        return normalized


def get_event_ledger(path=None, logger=None):
    return EventLedger(path=path, logger=logger)


__all__ = [
    "DEFAULT_LEDGER_PATH",
    "EVENT_TYPE_BUDGET_DECISION",
    "EVENT_TYPE_FILL",
    "EVENT_TYPE_FORECAST_SNAPSHOT",
    "EVENT_TYPE_MARKET_SNAPSHOT",
    "EVENT_TYPE_ORDER_SUBMITTED",
    "EVENT_TYPE_ORDER_UPDATE",
    "EVENT_TYPE_POSITION_SNAPSHOT",
    "EVENT_TYPE_POST_TRADE_ATTRIBUTION",
    "EVENT_TYPE_SETTLEMENT",
    "EVENT_TYPE_SOURCE_OBSERVATION",
    "EVENT_TYPE_TRADE_DECISION",
    "EVENT_TYPE_VERIFICATION_RESULT",
    "EventLedger",
    "get_event_ledger",
]
