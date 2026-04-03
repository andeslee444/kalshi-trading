#!/usr/bin/env python3
"""Pregame H2 collector for crowd-vs-Kalshi game-market divergence."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import time

from kalshi_auth import (
    is_shutdown_requested,
    setup_logging,
    setup_signal_handlers,
    setup_unbuffered,
)

from apps.oracle_latency_probe import (
    _build_oracle_kalshi_client,
    _extract_crowd_probabilities,
    _fetch_market_context,
    _load_oracle_config,
)
from domain.oracle.alpha_capture import DEFAULT_ORACLE_ALPHA_LEDGER_PATH, OracleAlphaCapture
from domain.oracle.execution.quote_check import quote_from_orderbook
from domain.oracle.latency_probe import OracleLatencyProbe
from domain.oracle.real_sports_client import RealSportsClient, RealSportsConfig


LIVE_GAME_STATUSES = frozenset({"inprogress", "live", "active"})
FINAL_GAME_STATUSES = frozenset({"final", "completed", "closed"})
PREGAME_COLLECTOR_MODE = "pregame"
PREGAME_SOURCE_ARTIFACT = "oracle_h2_pregame_collector"

log = logging.getLogger("oracle.h2_pregame_collector")


def _parse_datetime(value) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _is_pregame_game(game: dict, *, now: dt.datetime) -> bool:
    status = str(game.get("status") or "").strip().lower()
    if status in LIVE_GAME_STATUSES or status in FINAL_GAME_STATUSES:
        return False
    start_time = _parse_datetime(game.get("start_time"))
    if start_time is None:
        return True
    return start_time > now


def _pregame_games(context: dict, *, now: dt.datetime) -> list[dict]:
    return [
        game
        for game in context.get("discovery_games", [])
        if _is_pregame_game(game, now=now)
    ]


def _collector_cycle_id(now: dt.datetime) -> str:
    return f"oracle-h2-pregame-{int(now.timestamp() * 1000)}"


async def _capture_cycle(
    *,
    capture: OracleAlphaCapture,
    real_client: RealSportsClient,
    kalshi_client,
    hypothesis_id: str,
    lookahead_hours: float,
) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    context = await _fetch_market_context(real_client, kalshi_client)
    pregame_games = []
    for game in _pregame_games(context, now=now):
        start_time = _parse_datetime(game.get("start_time"))
        if start_time is not None:
            hours_to_tip = (start_time - now).total_seconds() / 3600.0
            if hours_to_tip < 0 or hours_to_tip > float(lookahead_hours):
                continue
        pregame_games.append(game)

    game_index = OracleLatencyProbe.build_game_market_index(
        pregame_games,
        context.get("kalshi_markets", []),
    )
    cycle_id = _collector_cycle_id(now)
    reverse_index = {
        ticker: game_id
        for game_id, tickers in game_index.items()
        for ticker in tickers
    }
    games_by_id = {
        str(game.get("game_id")): game
        for game in pregame_games
        if game.get("game_id") not in (None, "")
    }

    crowd_rows = []
    for row in _extract_crowd_probabilities(context.get("crowd_markets_raw", [])):
        game_id = str(row.get("game_id")) if row.get("game_id") not in (None, "") else ""
        game = games_by_id.get(game_id)
        if not game:
            continue
        start_time = _parse_datetime(game.get("start_time"))
        hours_to_tip = None
        if start_time is not None:
            hours_to_tip = round((start_time - now).total_seconds() / 3600.0, 3)
        crowd_rows.append(
            {
                **row,
                "collector_mode": PREGAME_COLLECTOR_MODE,
                "collector_cycle_id": cycle_id,
                "hypothesis_id": hypothesis_id,
                "game_status": game.get("status"),
                "start_time": game.get("start_time"),
                "scheduled_day": game.get("scheduled_day"),
                "hours_to_tip": hours_to_tip,
            }
        )

    recorded_crowd_rows = 0
    for row in crowd_rows:
        capture.record_probe_snapshot(
            snapshot_name="crowd_probability",
            payload=row,
            hypothesis_id=hypothesis_id,
            source_artifact=PREGAME_SOURCE_ARTIFACT,
        )
        recorded_crowd_rows += 1

    mapped_tickers = sorted({ticker for tickers in game_index.values() for ticker in tickers})
    snapshot_row = capture.record_probe_snapshot(
        snapshot_name="market_index_snapshot",
        payload={
            "collector_mode": PREGAME_COLLECTOR_MODE,
            "collector_cycle_id": cycle_id,
            "mapped_games": len(game_index),
            "mapped_tickers": mapped_tickers,
            "market_index": game_index,
            "lookahead_hours": float(lookahead_hours),
            "pregame_game_ids": sorted(games_by_id),
        },
        hypothesis_id=hypothesis_id,
        source_artifact=PREGAME_SOURCE_ARTIFACT,
    )

    quote_rows = 0
    for ticker in mapped_tickers:
        try:
            orderbook = await asyncio.to_thread(kalshi_client.get_orderbook, ticker)
            quote = quote_from_orderbook(ticker, orderbook)
        except Exception as exc:
            log.warning("Pregame collector orderbook fetch failed for %s: %s", ticker, exc)
            continue
        game_id = reverse_index.get(ticker)
        game = games_by_id.get(str(game_id or ""))
        start_time = _parse_datetime(game.get("start_time")) if isinstance(game, dict) else None
        hours_to_tip = None
        if start_time is not None:
            hours_to_tip = round((start_time - now).total_seconds() / 3600.0, 3)
        capture.record_quote_snapshot(
            ticker=ticker,
            yes_bid_cents=quote.yes_bid,
            yes_ask_cents=quote.yes_ask,
            yes_bid_depth=quote.bid_depth,
            yes_ask_depth=quote.ask_depth,
            quote_timestamp=quote.timestamp,
            source_event_id=snapshot_row["event_id"],
            source_event_type="market_index_snapshot",
            source_event_timestamp_utc=snapshot_row["observed_at"],
            game_id=game_id,
            hypothesis_id=hypothesis_id,
            extra={
                "capture_mode": "baseline",
                "collector_mode": PREGAME_COLLECTOR_MODE,
                "collector_cycle_id": cycle_id,
                "hours_to_tip": hours_to_tip,
                "game_status": game.get("status") if isinstance(game, dict) else None,
                "start_time": game.get("start_time") if isinstance(game, dict) else None,
            },
            source_artifact=PREGAME_SOURCE_ARTIFACT,
        )
        quote_rows += 1

    return {
        "collector_cycle_id": cycle_id,
        "pregame_games": len(pregame_games),
        "mapped_games": len(game_index),
        "mapped_tickers": len(mapped_tickers),
        "crowd_rows": recorded_crowd_rows,
        "quote_rows": quote_rows,
    }


async def async_main(
    *,
    once: bool = False,
    duration_seconds: int = 0,
    refresh_seconds: int = 120,
    lookahead_hours: float = 8.0,
) -> None:
    oracle_cfg = _load_oracle_config()
    real_cfg = RealSportsConfig.from_bots_config(oracle_cfg)
    missing = real_cfg.validate()
    if missing:
        raise SystemExit(
            "Missing Real Sports credentials for Oracle H2 pregame collector: " + ", ".join(missing)
        )

    hypothesis_id = oracle_cfg.get("research", {}).get("hypothesisId", "H2_crowd_divergence")
    capture = OracleAlphaCapture(
        path=oracle_cfg.get("research", {}).get("alphaLedgerPath", DEFAULT_ORACLE_ALPHA_LEDGER_PATH),
        logger=log,
    )
    real_client = RealSportsClient(real_cfg)
    if real_cfg.can_auto_login:
        ok = await real_client.login()
        if not ok:
            raise SystemExit("Real Sports auto-login failed for Oracle H2 pregame collector")
    kalshi_client = _build_oracle_kalshi_client()

    started = time.monotonic()
    try:
        while not is_shutdown_requested():
            summary = await _capture_cycle(
                capture=capture,
                real_client=real_client,
                kalshi_client=kalshi_client,
                hypothesis_id=hypothesis_id,
                lookahead_hours=lookahead_hours,
            )
            log.info(
                "Oracle H2 pregame cycle %s: pregame_games=%d mapped_games=%d mapped_tickers=%d crowd_rows=%d quote_rows=%d",
                summary["collector_cycle_id"],
                summary["pregame_games"],
                summary["mapped_games"],
                summary["mapped_tickers"],
                summary["crowd_rows"],
                summary["quote_rows"],
            )
            if once:
                break
            if duration_seconds and (time.monotonic() - started) >= duration_seconds:
                log.info("Oracle H2 pregame collector duration reached (%ds), exiting", duration_seconds)
                break
            await asyncio.sleep(refresh_seconds)
    finally:
        await real_client.close()


def main() -> None:
    setup_unbuffered()
    global log
    log = setup_logging("oracle-h2-pregame-collector")
    setup_signal_handlers()

    parser = argparse.ArgumentParser(description="Oracle H2 pregame crowd-divergence collector")
    parser.add_argument("--once", action="store_true", help="Run one collection cycle and exit")
    parser.add_argument("--duration-seconds", type=int, default=0, help="Optional max runtime")
    parser.add_argument("--refresh-seconds", type=int, default=120, help="Collection cadence")
    parser.add_argument(
        "--lookahead-hours",
        type=float,
        default=8.0,
        help="Ignore games further than this many hours from tip-off",
    )
    args = parser.parse_args()

    asyncio.run(
        async_main(
            once=args.once,
            duration_seconds=args.duration_seconds,
            refresh_seconds=args.refresh_seconds,
            lookahead_hours=args.lookahead_hours,
        )
    )


if __name__ == "__main__":
    main()
