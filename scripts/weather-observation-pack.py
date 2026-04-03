#!/usr/bin/env python3
"""Build a read-only weather observation pack from local artifacts.

The pack is Phase-4 safe: it reads existing local files only, with an
optional --save for a derived summary artifact.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
CONFIG_DIR = PROJECT_DIR / "config"
DEFAULT_OUTPUT_PATH = DATA_DIR / "weather-observation-pack.json"
DEFAULT_FINANCIAL_SNAPSHOT_PATH = DATA_DIR / "financial-snapshot.json"
DEFAULT_BACKTEST_RESULTS_PATH = DATA_DIR / "backtest-results.json"
DEFAULT_CALIBRATION_PATH = CONFIG_DIR / "calibration.json"
DEFAULT_WEATHER_VERIFICATION_PATH = DATA_DIR / "weather-verification.json"
DEFAULT_KALSHI_TRADES_PATH = DATA_DIR / "kalshi-trades.json"
DEFAULT_MONITOR_TRADES_PATH = DATA_DIR / "kalshi-monitor-trades.json"
DEFAULT_WEATHER_BIAS_PATH = CONFIG_DIR / "weather-live-bias.json"

KNOWN_CITY_ALIASES = {
    "AUS": "AUS",
    "Austin": "AUS",
    "ATL": "ATL",
    "BOS": "BOS",
    "CHI": "CHI",
    "DAL": "DAL",
    "Denver": "DEN",
    "DEN": "DEN",
    "DC": "DC",
    "Houston": "HOU",
    "HOU": "HOU",
    "LAX": "LAX",
    "LV": "LV",
    "MIA": "MIA",
    "MIN": "MIN",
    "NOLA": "NOLA",
    "NY": "NY",
    "OKC": "OKC",
    "PHIL": "PHIL",
    "PHX": "PHX",
    "SATX": "SATX",
    "SEA": "SEA",
    "SFO": "SFO",
    "Washington DC": "DC",
    "Los Angeles": "LAX",
    "Miami": "MIA",
    "New York": "NY",
    "Philadelphia": "PHIL",
    "Chicago": "CHI",
    "Boston": "BOS",
    "Seattle": "SEA",
    "San Francisco": "SFO",
    "Phoenix": "PHX",
    "Minneapolis": "MIN",
    "New Orleans": "NOLA",
    "Oklahoma City": "OKC",
    "San Antonio": "SATX",
    "Las Vegas": "LV",
}


def _load_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _resolve_path(path, default_path):
    if path is None:
        return Path(default_path)
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = PROJECT_DIR / resolved
    return resolved


def _safe_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _is_buy_action(value):
    if value is None:
        return True
    normalized = str(value).strip().lower()
    return normalized in ("", "buy", "none", "null")


def _parse_iso_datetime(value):
    if not value or not isinstance(value, str):
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_summary(timestamp_value, now=None):
    ts = _parse_iso_datetime(timestamp_value)
    if ts is None:
        return {
            "timestamp": timestamp_value,
            "age_seconds": None,
            "age_minutes": None,
            "age_hours": None,
            "age_days": None,
        }
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    age_seconds = max(0.0, delta.total_seconds())
    return {
        "timestamp": ts.isoformat(),
        "age_seconds": round(age_seconds, 1),
        "age_minutes": round(age_seconds / 60.0, 2),
        "age_hours": round(age_seconds / 3600.0, 2),
        "age_days": round(age_seconds / 86400.0, 2),
    }


def _local_today(now=None):
    if now is None:
        return date.today()
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            return now.astimezone().date()
        return now.date()
    return date.today()


def _normalize_city(value):
    if not value:
        return None
    if value in KNOWN_CITY_ALIASES:
        return KNOWN_CITY_ALIASES[value]
    upper = str(value).upper()
    if upper in KNOWN_CITY_ALIASES:
        return KNOWN_CITY_ALIASES[upper]
    return str(value)


def _city_from_ticker(ticker):
    if not ticker or not isinstance(ticker, str):
        return None
    if not ticker.startswith("KXHIGH"):
        return None
    suffix = ticker[len("KXHIGH"):].split("-", 1)[0]
    if suffix in KNOWN_CITY_ALIASES:
        return KNOWN_CITY_ALIASES[suffix]
    if suffix.startswith("T"):
        candidate = suffix[1:]
        if candidate in KNOWN_CITY_ALIASES:
            return KNOWN_CITY_ALIASES[candidate]
    match = re.match(r"^T?([A-Z]+)$", suffix)
    if match:
        return _normalize_city(match.group(1))
    return _normalize_city(suffix)


def _trade_city(trade):
    return _normalize_city(trade.get("city")) or _city_from_ticker(trade.get("ticker"))


def _is_executed_status(status):
    normalized = str(status or "").lower()
    return normalized in ("", "executed", "filled")


def _trade_cost_cents(trade):
    cost = trade.get("cost_cents")
    if cost is not None:
        return _safe_int(cost)
    price = trade.get("price_cents")
    if price is None:
        price = trade.get("price")
    return _safe_int(price) * _safe_int(trade.get("count", 0))


def _is_forecast_weather_trade(trade):
    ticker = str(trade.get("ticker", ""))
    if not ticker.startswith("KXHIGH"):
        return False
    return trade.get("source_bot") in ("weather", None)


def _is_source_monitor_trade(trade):
    return trade.get("source_bot") == "source-monitor"


def _is_source_monitor_nws_trade(trade):
    ticker = str(trade.get("ticker", ""))
    if not (_is_source_monitor_trade(trade) and ticker.startswith("KXHIGH")):
        return False
    source_type = str(trade.get("source_type", "")).lower()
    strategy = str(trade.get("strategy", "")).lower()
    reasoning = str(trade.get("reasoning", "")).lower()
    return (
        source_type == "nws"
        or strategy.startswith("nws")
        or "nws" in strategy
        or reasoning.startswith("nws ")
    )


def _compute_city_pnl_from_trades(trades, *, include_trade):
    """Compute city-level settled P&L proxy from executed local trade logs."""
    by_city = defaultdict(lambda: {
        "pnl_cents": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "settled": 0,
        "fees_cents": 0,
        "settled_markets": set(),
    })
    overall = {
        "pnl_cents": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "settled": 0,
        "fees_cents": 0,
        "settled_markets": set(),
    }

    for trade in trades:
        if not include_trade(trade):
            continue
        if not _is_buy_action(trade.get("action")):
            continue
        if not _is_executed_status(trade.get("status")):
            continue
        result = trade.get("settlement_result")
        if result not in ("won", "lost"):
            continue
        city = _trade_city(trade)
        if not city:
            continue

        count = _safe_int(trade.get("count", 0), default=0)
        cost_cents = _trade_cost_cents(trade)
        fee_cents = _safe_int(trade.get("fee_cents", 0), default=0)
        if result == "won":
            pnl = 100 * count - cost_cents
        else:
            pnl = -cost_cents

        row = by_city[city]
        row["pnl_cents"] += pnl
        row["trades"] += 1
        row["settled"] += 1
        row["fees_cents"] += fee_cents
        if trade.get("ticker"):
            row["settled_markets"].add(str(trade["ticker"]))
        if result == "won":
            row["wins"] += 1
        else:
            row["losses"] += 1

        overall["pnl_cents"] += pnl
        overall["trades"] += 1
        overall["settled"] += 1
        overall["fees_cents"] += fee_cents
        if trade.get("ticker"):
            overall["settled_markets"].add(str(trade["ticker"]))
        if result == "won":
            overall["wins"] += 1
        else:
            overall["losses"] += 1

    rows = []
    for city, stats in by_city.items():
        rows.append({
            "city": city,
            **stats,
            "settled_markets": len(stats["settled_markets"]),
            "win_rate": round(stats["wins"] / stats["settled"], 3) if stats["settled"] else None,
            "net_after_fees_cents": stats["pnl_cents"] - stats["fees_cents"],
        })
    rows.sort(key=lambda row: (-row["pnl_cents"], row["city"]))
    overall["settled_markets"] = len(overall["settled_markets"])
    overall["win_rate"] = round(overall["wins"] / overall["settled"], 3) if overall["settled"] else None
    overall["net_after_fees_cents"] = overall["pnl_cents"] - overall["fees_cents"]
    return {"source": "local_executed_trade_log_settlements", "overall": overall, "by_city": rows}


def compute_weather_pnl_from_trades(trades):
    """Compute city-level realized P&L from local forecast-weather trade logs."""
    return _compute_city_pnl_from_trades(
        trades,
        include_trade=_is_forecast_weather_trade,
    )


def compute_source_monitor_nws_pnl_from_trades(trades):
    """Compute city-level realized P&L from source-monitor NWS trade logs."""
    return _compute_city_pnl_from_trades(
        trades,
        include_trade=_is_source_monitor_nws_trade,
    )


def compute_verification_source_mix(verified_rows, lookback_days, now=None):
    cutoff = _local_today(now=now) - date.resolution * lookback_days
    counts = Counter()
    total = 0
    for row in verified_rows:
        row_date = row.get("date")
        if not row_date:
            continue
        try:
            parsed = date.fromisoformat(row_date)
        except ValueError:
            continue
        if parsed < cutoff:
            continue
        counts[row.get("actual_source") or "missing"] += 1
        total += 1
    shares = {source: round(count / total, 3) for source, count in sorted(counts.items())} if total else {}
    return {
        "lookback_days": lookback_days,
        "total": total,
        "counts": dict(sorted(counts.items())),
        "shares": shares,
    }


def _compute_execution_quality(trades, *, include_trade):
    city_rows = defaultdict(lambda: {
        "trades": 0,
        "executed": 0,
        "resting": 0,
        "maker": 0,
        "maker_executed": 0,
        "taker_executed": 0,
        "total_edge": 0.0,
        "bias_records": 0,
        "total_abs_bias": 0.0,
        "bias_conflict": 0,
        "bias_capped": 0,
    })
    overall = {
        "trades": 0,
        "executed": 0,
        "resting": 0,
        "maker": 0,
        "maker_executed": 0,
        "taker_executed": 0,
        "total_edge": 0.0,
        "bias_records": 0,
        "total_abs_bias": 0.0,
        "bias_conflict": 0,
        "bias_capped": 0,
    }

    for trade in trades:
        if not include_trade(trade):
            continue
        city = _trade_city(trade) or trade.get("city") or "UNKNOWN"
        row = city_rows[city]
        status = trade.get("status")
        style = trade.get("execution_style") or "legacy"
        edge = float(trade.get("edge") or 0.0)
        bias_applied = trade.get("bias_applied_f")
        bias_conflict = bool(trade.get("bias_conflict"))
        bias_capped = bool(trade.get("bias_capped"))

        for target in (row, overall):
            target["trades"] += 1
            target["total_edge"] += edge
            if status == "executed":
                target["executed"] += 1
            if status == "resting":
                target["resting"] += 1
            if style == "maker":
                target["maker"] += 1
                if status == "executed":
                    target["maker_executed"] += 1
            elif status == "executed":
                target["taker_executed"] += 1
            if isinstance(bias_applied, (int, float)):
                target["bias_records"] += 1
                target["total_abs_bias"] += abs(float(bias_applied))
                if bias_conflict:
                    target["bias_conflict"] += 1
                if bias_capped:
                    target["bias_capped"] += 1

    def finalize(stats):
        trades_n = max(1, stats["trades"])
        maker_n = max(1, stats["maker"])
        executed_n = max(1, stats["executed"])
        bias_records = max(1, stats["bias_records"])
        return {
            **stats,
            "avg_edge": round(stats["total_edge"] / trades_n, 3),
            "maker_share": round(stats["maker"] / trades_n, 3),
            "fill_rate": round(stats["executed"] / trades_n, 3),
            "maker_fill_rate": round(stats["maker_executed"] / maker_n, 3),
            "maker_exec_share": round(stats["maker_executed"] / executed_n, 3) if stats["executed"] else 0.0,
            "avg_abs_bias_applied": round(stats["total_abs_bias"] / bias_records, 3) if stats["bias_records"] else None,
            "bias_conflict_share": round(stats["bias_conflict"] / bias_records, 3) if stats["bias_records"] else None,
            "bias_cap_share": round(stats["bias_capped"] / bias_records, 3) if stats["bias_records"] else None,
        }

    return {
        "overall": finalize(overall),
        "per_city": {city: finalize(stats) for city, stats in sorted(city_rows.items())},
    }


def compute_weather_execution_quality(trades):
    return _compute_execution_quality(
        trades,
        include_trade=_is_forecast_weather_trade,
    )


def compute_source_monitor_nws_execution_quality(trades):
    return _compute_execution_quality(
        trades,
        include_trade=_is_source_monitor_nws_trade,
    )


def _compute_settled_trade_counts(trades, *, include_trade, require_executed=False):
    total = 0
    for trade in trades:
        if not include_trade(trade):
            continue
        if require_executed and not _is_executed_status(trade.get("status")):
            continue
        if trade.get("settlement_result") in ("won", "lost"):
            total += 1
    return total


def _source_monitor_nws_snapshot_attribution(realized_source_monitor, monitor_trades):
    settled_source_monitor = _compute_settled_trade_counts(
        monitor_trades,
        include_trade=_is_source_monitor_trade,
        require_executed=True,
    )
    settled_nws = _compute_settled_trade_counts(
        monitor_trades,
        include_trade=_is_source_monitor_nws_trade,
        require_executed=True,
    )
    attributable = (
        isinstance(realized_source_monitor, dict)
        and settled_source_monitor > 0
        and settled_source_monitor == settled_nws
    )
    if attributable:
        reason = "all_executed_settled_source_monitor_trades_are_nws_weather"
    elif settled_source_monitor == 0:
        reason = "no_executed_settled_source_monitor_trades"
    else:
        reason = "executed_source_monitor_trade_log_contains_non_nws_or_non_weather_settlements"
    return {
        "fully_attributable_to_nws": attributable,
        "source_monitor_settled_total": settled_source_monitor,
        "source_monitor_nws_settled": settled_nws,
        "reason": reason,
        "requires_executed_status": True,
        "trade_log_only": True,
    }


def compute_historical_city_bias(historical_data):
    per_city = historical_data.get("per_city", {}) if isinstance(historical_data, dict) else {}
    out = {}
    for city, models in per_city.items():
        biases = []
        if isinstance(models, dict):
            for stats in models.values():
                if isinstance(stats, dict) and stats.get("bias") is not None:
                    biases.append(float(stats["bias"]))
        if biases:
            out[city] = {
                "bias_f": round(sum(biases) / len(biases), 2),
                "n_models": len(biases),
            }
    return out


def compute_live_city_bias(verified_rows, lookback_days, now=None, min_samples=2):
    cutoff = _local_today(now=now) - date.resolution * lookback_days
    city_errors = defaultdict(list)
    for row in verified_rows:
        if row.get("record_kind", "snapshot") != "snapshot":
            continue
        city = _normalize_city(row.get("city"))
        if not city:
            continue
        row_date = row.get("date")
        if not row_date:
            continue
        try:
            parsed = date.fromisoformat(row_date)
        except ValueError:
            continue
        if parsed < cutoff:
            continue
        errors = row.get("errors", {})
        if not isinstance(errors, dict) or not errors:
            continue
        vals = [v for v in errors.values() if isinstance(v, (int, float))]
        if not vals:
            continue
        city_errors[city].append(sum(vals) / len(vals))

    summary = {}
    for city, values in city_errors.items():
        if len(values) < min_samples:
            continue
        bias_f = sum(values) / len(values)
        summary[city] = {
            "bias_f": round(bias_f, 2),
            "n": len(values),
        }
    return summary


def build_city_bias_conflicts(historical_biases, live_biases, city_pnl, min_gap_f=4.0):
    rows = []
    for city in sorted(set(historical_biases) | set(live_biases)):
        hist = historical_biases.get(city, {})
        live = live_biases.get(city, {})
        hist_bias = hist.get("bias_f")
        live_bias = live.get("bias_f")
        if hist_bias is None or live_bias is None:
            continue
        gap = abs(hist_bias - live_bias)
        sign_flip = (hist_bias > 0 > live_bias) or (hist_bias < 0 < live_bias)
        conflict = sign_flip or gap >= min_gap_f
        pnl_row = city_pnl.get(city, {})
        rows.append({
            "city": city,
            "historical_bias_f": hist_bias,
            "live_bias_f": live_bias,
            "gap_f": round(gap, 2),
            "sign_flip": sign_flip,
            "bias_conflict": conflict,
            "historical_models": hist.get("n_models"),
            "live_samples": live.get("n"),
            "weather_pnl_cents": pnl_row.get("pnl_cents", 0),
            "weather_trades": pnl_row.get("trades", 0),
        })

    rows.sort(key=lambda row: (
        not row["bias_conflict"],
        not row["sign_flip"],
        -row["gap_f"],
        row["city"],
    ))
    return rows


def _freshness_from(path, payload, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return {
            "path": str(path),
            "present": False,
        }
    timestamp = payload.get("generated_at") or payload.get("timestamp") or payload.get("sigma_updated_at")
    age = _age_summary(timestamp, now=now)
    return {
        "path": str(path),
        "present": True,
        **age,
    }


def _combine_realized_tracks(track_map):
    rows = [(name, payload) for name, payload in track_map.items() if isinstance(payload, dict)]
    if not rows:
        return None
    combined = {
        "pnl_cents": 0,
        "wins": 0,
        "losses": 0,
        "fees_cents": 0,
        "track_count": len(rows),
    }
    leader = None
    leader_pnl = None
    for name, payload in rows:
        pnl = _safe_int(payload.get("pnl_cents"), default=0)
        combined["pnl_cents"] += pnl
        combined["wins"] += _safe_int(payload.get("wins"), default=0)
        combined["losses"] += _safe_int(payload.get("losses"), default=0)
        combined["fees_cents"] += _safe_int(payload.get("fees_cents"), default=0)
        if leader is None or pnl > leader_pnl:
            leader = name
            leader_pnl = pnl
    total = combined["wins"] + combined["losses"]
    combined["win_rate"] = round(combined["wins"] / total, 3) if total else None
    combined["leader_by_realized_pnl"] = leader
    combined["net_after_fees_cents"] = combined["pnl_cents"] - combined["fees_cents"]
    return combined


def _snapshot_local_reconciliation(realized_payload, local_summary):
    realized = realized_payload if isinstance(realized_payload, dict) else {}
    local = local_summary.get("overall", {}) if isinstance(local_summary, dict) else {}
    snapshot_settled = _safe_int(realized.get("wins"), default=0) + _safe_int(realized.get("losses"), default=0)
    local_settled_trade_rows = _safe_int(local.get("settled"), default=0)
    local_settled_markets = _safe_int(local.get("settled_markets"), default=0)
    snapshot_pnl = _safe_int(realized.get("pnl_cents"), default=0)
    local_pnl = _safe_int(local.get("pnl_cents"), default=0)
    return {
        "snapshot_settled_markets": snapshot_settled,
        "local_settled_trade_rows": local_settled_trade_rows,
        "local_settled_markets": local_settled_markets,
        "snapshot_pnl_cents": snapshot_pnl,
        "local_pnl_cents": local_pnl,
        "settled_mismatch": snapshot_settled != local_settled_markets,
        "pnl_mismatch": snapshot_pnl != local_pnl,
    }


def _reporting_recommendation(snapshot_reconciliation):
    reconciliation = snapshot_reconciliation if isinstance(snapshot_reconciliation, dict) else {}
    if not reconciliation:
        return {
            "status": "unavailable",
            "recommended_basis": None,
            "warning": None,
        }

    mismatch = bool(
        reconciliation.get("settled_mismatch")
        or reconciliation.get("pnl_mismatch")
    )
    if mismatch:
        return {
            "status": "mismatch_under_review",
            "recommended_basis": "financial_snapshot_by_bot_with_proxy_warning",
            "warning": (
                "financial snapshot by-bot totals and executed local weather trade proxies diverge; "
                "keep source-monitor bot P&L visible as bot-level context, but do not attribute it "
                "to source-monitor NWS or combined weather-family realized P&L until reconciliation "
                "is resolved"
            ),
        }

    return {
        "status": "aligned",
        "recommended_basis": "financial_snapshot_by_bot_and_local_proxy",
        "warning": None,
    }


def _source_monitor_reporting_recommendation(
    *,
    source_monitor_basis=None,
    source_monitor_local_reconciliation=None,
    snapshot_reconciliation=None,
):
    local_basis = "local_buy_orders_joined_to_api_fills_and_settlement_outcomes"
    if source_monitor_basis == local_basis and isinstance(source_monitor_local_reconciliation, dict):
        if source_monitor_local_reconciliation.get("eligible_local_join_basis"):
            return {
                "status": "aligned",
                "recommended_basis": "financial_snapshot_local_joined_fills_by_bot",
                "warning": None,
            }
    return _reporting_recommendation(snapshot_reconciliation)


def build_observation_pack(
    lookback_days=30,
    source_lookback_days=(7, 30),
    top_cities=8,
    min_conflict_gap_f=4.0,
    now=None,
    financial_snapshot_path=None,
    backtest_results_path=None,
    calibration_path=None,
    weather_verification_path=None,
    weather_trades_path=None,
    monitor_trades_path=None,
    weather_bias_path=None,
):
    now = now or datetime.now(timezone.utc)
    financial_snapshot_path = _resolve_path(financial_snapshot_path, DEFAULT_FINANCIAL_SNAPSHOT_PATH)
    backtest_results_path = _resolve_path(backtest_results_path, DEFAULT_BACKTEST_RESULTS_PATH)
    calibration_path = _resolve_path(calibration_path, DEFAULT_CALIBRATION_PATH)
    weather_verification_path = _resolve_path(weather_verification_path, DEFAULT_WEATHER_VERIFICATION_PATH)
    weather_trades_path = _resolve_path(weather_trades_path, DEFAULT_KALSHI_TRADES_PATH)
    monitor_trades_path = _resolve_path(monitor_trades_path, DEFAULT_MONITOR_TRADES_PATH)
    weather_bias_path = _resolve_path(weather_bias_path, DEFAULT_WEATHER_BIAS_PATH)

    financial_snapshot = _load_json(financial_snapshot_path, default={})
    backtest_results = _load_json(backtest_results_path, default={})
    calibration = _load_json(calibration_path, default={})
    verification = _load_json(weather_verification_path, default={})
    trades = _load_json(weather_trades_path, default=[])
    monitor_trades = _load_json(monitor_trades_path, default=[])
    prior_bias = _load_json(weather_bias_path, default={})

    realized_weather = None
    if isinstance(financial_snapshot, dict):
        realized_pnl = financial_snapshot.get("realized_pnl", {})
        by_bot = realized_pnl.get("by_bot", {}) if isinstance(realized_pnl, dict) else {}
        by_bot_api = realized_pnl.get("by_bot_api_settlements", {}) if isinstance(realized_pnl, dict) else {}
        by_bot_local = realized_pnl.get("by_bot_local_joined_fills", {}) if isinstance(realized_pnl, dict) else {}
        by_bot_basis_map = realized_pnl.get("by_bot_basis_map", {}) if isinstance(realized_pnl, dict) else {}
        by_bot_reporting_notes = realized_pnl.get("by_bot_reporting_notes", {}) if isinstance(realized_pnl, dict) else {}
        by_bot_local_reconciliation = (
            realized_pnl.get("by_bot_local_reconciliation", {}) if isinstance(realized_pnl, dict) else {}
        )
        realized_weather = by_bot.get("weather") or by_bot_api.get("weather")
        realized_source_monitor = by_bot.get("source-monitor") or by_bot_api.get("source-monitor")
        realized_demo_weather = (
            by_bot.get("demo-weather-history")
            or by_bot_api.get("demo-weather-history")
        )
        realized_unattributed_weather = (
            by_bot.get("unattributed-weather")
            or by_bot_api.get("unattributed-weather")
        )
        realized_weather_api = by_bot_api.get("weather")
        realized_source_monitor_api = by_bot_api.get("source-monitor")
        realized_source_monitor_local = by_bot_local.get("source-monitor")
        realized_source_monitor_basis = by_bot_basis_map.get("source-monitor")
        realized_source_monitor_local_reconciliation = by_bot_local_reconciliation.get("source-monitor")
        realized_demo_weather_basis = by_bot_basis_map.get("demo-weather-history")
        realized_unattributed_weather_basis = by_bot_basis_map.get("unattributed-weather")
        realized_demo_weather_note = by_bot_reporting_notes.get("demo-weather-history")
        realized_unattributed_weather_note = by_bot_reporting_notes.get("unattributed-weather")
    else:
        realized_source_monitor = None
        realized_demo_weather = None
        realized_unattributed_weather = None
        realized_weather_api = None
        realized_source_monitor_api = None
        realized_source_monitor_local = None
        realized_source_monitor_basis = None
        realized_source_monitor_local_reconciliation = None
        realized_demo_weather_basis = None
        realized_unattributed_weather_basis = None
        realized_demo_weather_note = None
        realized_unattributed_weather_note = None

    verification_rows = verification.get("verified", []) if isinstance(verification, dict) else []
    verification_source_mix = [
        compute_verification_source_mix(verification_rows, days, now=now)
        for days in source_lookback_days
    ]
    execution_quality = compute_weather_execution_quality(trades)
    source_monitor_nws_pnl = compute_source_monitor_nws_pnl_from_trades(monitor_trades)
    source_monitor_nws_execution = compute_source_monitor_nws_execution_quality(monitor_trades)
    source_monitor_nws_attribution = _source_monitor_nws_snapshot_attribution(realized_source_monitor, monitor_trades)
    source_monitor_nws_reconciliation = (
        _snapshot_local_reconciliation(realized_source_monitor, source_monitor_nws_pnl)
        if realized_source_monitor else None
    )
    source_monitor_nws_reporting = (
        _source_monitor_reporting_recommendation(
            source_monitor_basis=realized_source_monitor_basis,
            source_monitor_local_reconciliation=realized_source_monitor_local_reconciliation,
            snapshot_reconciliation=source_monitor_nws_reconciliation,
        )
        if source_monitor_nws_attribution["fully_attributable_to_nws"]
        else {
            "status": "unavailable",
            "recommended_basis": None,
            "warning": None,
        }
    )
    source_monitor_nws_realized = (
        (realized_source_monitor_local or realized_source_monitor)
        if source_monitor_nws_attribution["fully_attributable_to_nws"]
        and source_monitor_nws_reporting.get("status") == "aligned"
        else None
    )
    weather_family_realized = _combine_realized_tracks({
        "forecast_weather": realized_weather,
        "source_monitor_nws": source_monitor_nws_realized,
    })

    pack = {
        "artifact_type": "weather_observation_pack",
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "sources": {
            "financial_snapshot": str(financial_snapshot_path),
            "backtest_results": str(backtest_results_path),
            "calibration": str(calibration_path),
            "weather_verification": str(weather_verification_path),
            "weather_bias": str(weather_bias_path),
            "kalshi_trades": str(weather_trades_path),
            "weather_execution_trades": str(weather_trades_path),
            "monitor_trades": str(monitor_trades_path),
            "source_monitor_nws_execution_trades": str(monitor_trades_path),
        },
        "weather_pnl": {
            "realized": realized_weather,
            "realized_api_settlements": realized_weather_api,
            "financial_snapshot": _freshness_from(financial_snapshot_path, financial_snapshot, now=now),
        },
        "demo_weather_history": {
            "realized": realized_demo_weather,
            "basis": realized_demo_weather_basis,
            "financial_snapshot": _freshness_from(financial_snapshot_path, financial_snapshot, now=now),
            "reporting_note": realized_demo_weather_note or (
                "Known demo-trader weather activity matched from data/demo-trades-log.json; keep visible as "
                "historical context but exclude from attributable forecast-weather and combined weather-family realized P&L"
            ) if realized_demo_weather else None,
        },
        "unattributed_weather": {
            "realized": realized_unattributed_weather,
            "basis": realized_unattributed_weather_basis,
            "financial_snapshot": _freshness_from(financial_snapshot_path, financial_snapshot, now=now),
            "reporting_note": realized_unattributed_weather_note or (
                "API-only KXHIGH settlements/fills with no canonical local order match; keep visible as historical "
                "weather context but exclude from attributable forecast-weather and combined weather-family realized P&L"
            ) if realized_unattributed_weather else None,
        },
        "source_monitor_nws": {
            "realized": source_monitor_nws_realized,
            "source_monitor_bot_realized": realized_source_monitor,
            "source_monitor_bot_realized_api": realized_source_monitor_api,
            "source_monitor_bot_realized_local_joined": realized_source_monitor_local,
            "source_monitor_bot_realized_basis": realized_source_monitor_basis,
            "financial_snapshot": _freshness_from(financial_snapshot_path, financial_snapshot, now=now),
            "local_trade_log": source_monitor_nws_pnl,
            "execution_quality": source_monitor_nws_execution,
            "snapshot_attribution": source_monitor_nws_attribution,
            "snapshot_local_reconciliation": source_monitor_nws_reconciliation,
            "source_monitor_local_join_reconciliation": realized_source_monitor_local_reconciliation,
            "reporting_recommendation": source_monitor_nws_reporting,
        },
        "weather_family": {
            "realized": weather_family_realized,
            "reporting_recommendation": source_monitor_nws_reporting if realized_source_monitor else None,
        },
        "freshness": {
            "backtest_results": _freshness_from(backtest_results_path, backtest_results, now=now),
            "calibration": _freshness_from(calibration_path, calibration, now=now),
            "calibration_sigma_updated_at": _age_summary(calibration.get("sigma_updated_at"), now=now) if isinstance(calibration, dict) else None,
            "weather_bias": _freshness_from(weather_bias_path, prior_bias, now=now),
        },
        "verification_source_mix": {
            "state_path": str(weather_verification_path),
            "summaries": verification_source_mix,
        },
        "city_pnl": compute_weather_pnl_from_trades(trades),
        "execution_quality": execution_quality,
        "city_bias": {
            "historical": compute_historical_city_bias(prior_bias),
            "live": compute_live_city_bias(verification_rows, lookback_days, now=now),
        },
    }
    all_conflicts = build_city_bias_conflicts(
        pack["city_bias"]["historical"],
        pack["city_bias"]["live"],
        {row["city"]: row for row in pack["city_pnl"]["by_city"]},
        min_gap_f=min_conflict_gap_f,
    )
    pack["city_bias"]["conflicts"] = all_conflicts
    pack["city_bias"]["top_conflicts"] = all_conflicts[:top_cities]
    return pack


def _print_human(pack):
    realized = pack.get("weather_pnl", {}).get("realized") or {}
    if realized:
        print(
            f"Weather realized P&L: {realized.get('pnl_cents', 0)} cents "
            f"({realized.get('wins', 0)}W/{realized.get('losses', 0)}L, "
            f"win_rate={realized.get('win_rate')})"
        )
    source_monitor = pack.get("source_monitor_nws", {}).get("realized") or {}
    if source_monitor:
        print(
            f"Source-monitor NWS realized P&L: {source_monitor.get('pnl_cents', 0)} cents "
            f"({source_monitor.get('wins', 0)}W/{source_monitor.get('losses', 0)}L, "
            f"win_rate={source_monitor.get('win_rate')})"
        )
    demo_weather = pack.get("demo_weather_history", {}).get("realized") or {}
    if demo_weather and (
        demo_weather.get("pnl_cents", 0) != 0
        or demo_weather.get("wins", 0)
        or demo_weather.get("losses", 0)
    ):
        print(
            f"Demo weather history: {demo_weather.get('pnl_cents', 0)} cents "
            f"({demo_weather.get('wins', 0)}W/{demo_weather.get('losses', 0)}L)"
        )
    unattributed = pack.get("unattributed_weather", {}).get("realized") or {}
    if unattributed and (
        unattributed.get("pnl_cents", 0) != 0
        or unattributed.get("wins", 0)
        or unattributed.get("losses", 0)
    ):
        print(
            f"Unattributed weather history: {unattributed.get('pnl_cents', 0)} cents "
            f"({unattributed.get('wins', 0)}W/{unattributed.get('losses', 0)}L)"
        )
    family = pack.get("weather_family", {}).get("realized") or {}
    if family:
        print(
            f"Weather family realized P&L: {family.get('pnl_cents', 0)} cents "
            f"(leader={family.get('leader_by_realized_pnl')})"
        )
    freshness = pack.get("freshness", {})
    backtest = freshness.get("backtest_results", {})
    calibration = freshness.get("calibration", {})
    sigma_age = freshness.get("calibration_sigma_updated_at", {})
    print(
        f"Backtest freshness: {backtest.get('timestamp')} age_days={backtest.get('age_days')}"
    )
    print(
        f"Calibration freshness: {calibration.get('timestamp')} age_days={calibration.get('age_days')} "
        f"sigma_updated_at={sigma_age.get('timestamp')} sigma_age_days={sigma_age.get('age_days')}"
    )

    print("Verification source mix:")
    for summary in pack.get("verification_source_mix", {}).get("summaries", []):
        parts = ", ".join(
            f"{source}={count} ({summary['shares'].get(source, 0):.1%})"
            for source, count in summary.get("counts", {}).items()
        )
        print(f"  {summary.get('lookback_days')}d: total={summary.get('total', 0)} | {parts}")

    exec_quality = pack.get("execution_quality", {}).get("overall", {})
    if exec_quality:
        print(
            "Execution quality: "
            f"trades={exec_quality.get('trades', 0)} "
            f"executed={exec_quality.get('executed', 0)} "
            f"fill_rate={exec_quality.get('fill_rate')} "
            f"maker_share={exec_quality.get('maker_share')}"
        )

    print("City bias conflicts:")
    for row in pack.get("city_bias", {}).get("top_conflicts", pack.get("city_bias", {}).get("conflicts", [])[:8]):
        print(
            f"  {row['city']}: hist={row['historical_bias_f']:.2f} live={row['live_bias_f']:.2f} "
            f"gap={row['gap_f']:.2f} sign_flip={row['sign_flip']} "
            f"conflict={row['bias_conflict']} pnl={row['weather_pnl_cents']}"
        )

    city_pnl_rows = pack.get("city_pnl", {}).get("by_city", [])
    print("City P&L:")
    for row in city_pnl_rows[:8]:
        print(
            f"  {row['city']}: pnl={row['pnl_cents']} trades={row['trades']} "
            f"win_rate={row.get('win_rate')}"
        )


def main():
    parser = argparse.ArgumentParser(description="Build a read-only weather observation pack")
    parser.add_argument("--lookback-days", type=int, default=30, help="Live-bias lookback window")
    parser.add_argument(
        "--source-lookback-days",
        nargs="*",
        type=int,
        default=[7, 30],
        help="Verification source-mix windows",
    )
    parser.add_argument("--top-cities", type=int, default=8, help="Number of conflict rows to include")
    parser.add_argument(
        "--min-conflict-gap-f",
        type=float,
        default=4.0,
        help="Historical-vs-live bias gap needed to flag a conflict",
    )
    parser.add_argument(
        "--financial-snapshot-path",
        default=str(DEFAULT_FINANCIAL_SNAPSHOT_PATH),
        help="Path to financial-snapshot.json",
    )
    parser.add_argument(
        "--backtest-results-path",
        default=str(DEFAULT_BACKTEST_RESULTS_PATH),
        help="Path to backtest-results.json",
    )
    parser.add_argument(
        "--calibration-path",
        default=str(DEFAULT_CALIBRATION_PATH),
        help="Path to calibration.json",
    )
    parser.add_argument(
        "--weather-verification-path",
        default=str(DEFAULT_WEATHER_VERIFICATION_PATH),
        help="Path to weather-verification.json",
    )
    parser.add_argument(
        "--weather-trades-path",
        default=str(DEFAULT_KALSHI_TRADES_PATH),
        help="Path to the canonical weather trade log",
    )
    parser.add_argument(
        "--monitor-trades-path",
        default=str(DEFAULT_MONITOR_TRADES_PATH),
        help="Path to the canonical source-monitor trade log",
    )
    parser.add_argument(
        "--weather-bias-path",
        default=str(DEFAULT_WEATHER_BIAS_PATH),
        help="Path to weather-live-bias.json",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--save", action="store_true", help="Save a derived artifact to data/weather-observation-pack.json")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output path for a saved derived artifact",
    )
    args = parser.parse_args()

    pack = build_observation_pack(
        lookback_days=args.lookback_days,
        source_lookback_days=args.source_lookback_days,
        top_cities=args.top_cities,
        min_conflict_gap_f=args.min_conflict_gap_f,
        financial_snapshot_path=args.financial_snapshot_path,
        backtest_results_path=args.backtest_results_path,
        calibration_path=args.calibration_path,
        weather_verification_path=args.weather_verification_path,
        weather_trades_path=args.weather_trades_path,
        monitor_trades_path=args.monitor_trades_path,
        weather_bias_path=args.weather_bias_path,
    )

    if args.save:
        output_path = _resolve_path(args.output, DEFAULT_OUTPUT_PATH)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(pack, indent=2, sort_keys=True) + "\n")
        print(f"Saved derived observation pack to {output_path}", file=sys.stderr)

    if args.json:
        print(json.dumps(pack, indent=2, sort_keys=True))
        return

    _print_human(pack)


if __name__ == "__main__":
    main()
