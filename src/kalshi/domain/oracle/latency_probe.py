"""Latency probe for Real Sports events versus Kalshi market quotes."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Iterable

from domain.oracle.alpha_capture import DEFAULT_HYPOTHESIS_ID, OracleAlphaCapture
from domain.oracle.latency_analysis import classify_live_event
from domain.oracle.execution.quote_check import quote_from_orderbook
from domain.oracle.market_mapper import match_game_markets

_log = logging.getLogger("oracle.latency_probe")


def _parse_game_date(start_time: str) -> dt.date | None:
    if not start_time:
        return None
    try:
        return dt.datetime.fromisoformat(start_time.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class OracleLatencyProbe:
    """Captures Real source events and immediate Kalshi quote snapshots."""

    DEFAULT_EVENT_TYPES = frozenset({
        "LiveFeedSocketPlaysAdded",
        "LiveFeedSocketPlayersUpdated",
        "PlayerBoxScoreUpdated",
        "GameUpdated",
    })
    DEFAULT_FOLLOWUP_DELAYS_SECONDS = (1.0, 3.0, 5.0)

    def __init__(
        self,
        kalshi_client,
        capture: OracleAlphaCapture,
        *,
        hypothesis_id: str = DEFAULT_HYPOTHESIS_ID,
        capture_event_types: Iterable[str] | None = None,
        followup_delays_seconds: Iterable[float] | None = None,
        fetch_quote_func=None,
        fetch_orderbook_func=None,
        logger=None,
    ):
        self._client = kalshi_client
        self._capture = capture
        self._hypothesis_id = hypothesis_id
        self._capture_event_types = set(capture_event_types or self.DEFAULT_EVENT_TYPES)
        delays = (
            self.DEFAULT_FOLLOWUP_DELAYS_SECONDS
            if followup_delays_seconds is None
            else tuple(followup_delays_seconds)
        )
        self._followup_delays_seconds = tuple(
            sorted({
                float(delay)
                for delay in delays
                if delay is not None and float(delay) >= 0.0
            })
        )
        self._fetch_orderbook = fetch_orderbook_func or self._default_fetch_orderbook
        self._fetch_quote = fetch_quote_func or self._default_fetch_quote
        self._has_external_quote_source = fetch_quote_func is not None
        self._game_market_index: dict[str, list[str]] = {}
        self._game_state_index: dict[str, dict] = {}
        self._player_foul_index: dict[str, int] = {}
        self._quote_index: dict[str, dict] = {}
        self._pending_followup_tasks: set[asyncio.Task] = set()
        self._source_event_count = 0
        self._mapped_source_event_count = 0
        self._last_source_event_observed_at: float | None = None
        self._last_mapped_source_event_observed_at: float | None = None
        self.log = logger or _log

    @staticmethod
    def build_game_market_index(real_markets: list[dict], kalshi_markets: list[dict]) -> dict[str, list[str]]:
        """Map Real game ids to matching Kalshi game-market tickers."""
        index: dict[str, list[str]] = {}
        for market in real_markets:
            game_id = market.get("game_id")
            if game_id in (None, ""):
                continue
            game_date = _parse_game_date(market.get("scheduled_day") or market.get("start_time", ""))
            if game_date is None:
                continue
            matched = match_game_markets(
                market.get("home_team", ""),
                market.get("away_team", ""),
                game_date,
                kalshi_markets,
            )
            tickers = [m.get("ticker", "") for m in matched if m.get("ticker")]
            if tickers:
                index[str(game_id)] = sorted(set(tickers))
        return index

    def refresh_game_market_index(self, real_markets: list[dict], kalshi_markets: list[dict]) -> dict[str, list[str]]:
        self._game_market_index = self.build_game_market_index(real_markets, kalshi_markets)
        return dict(self._game_market_index)

    @property
    def game_market_index(self) -> dict[str, list[str]]:
        return dict(self._game_market_index)

    @property
    def source_event_count(self) -> int:
        return self._source_event_count

    @property
    def mapped_source_event_count(self) -> int:
        return self._mapped_source_event_count

    def last_source_event_age_seconds(self, *, now: float | None = None) -> float | None:
        if self._last_source_event_observed_at is None:
            return None
        return max(0.0, (time.time() if now is None else float(now)) - self._last_source_event_observed_at)

    def last_mapped_source_event_age_seconds(self, *, now: float | None = None) -> float | None:
        if self._last_mapped_source_event_observed_at is None:
            return None
        return max(0.0, (time.time() if now is None else float(now)) - self._last_mapped_source_event_observed_at)

    def _default_fetch_orderbook(self, ticker: str) -> dict:
        return self._client.get_orderbook(ticker)

    def _default_fetch_quote(self, ticker: str):
        return quote_from_orderbook(ticker, self._fetch_orderbook(ticker))

    @staticmethod
    def _player_index_key(game_id, player_id) -> str | None:
        if game_id in (None, "") or player_id in (None, ""):
            return None
        return f"{game_id}:{player_id}"

    def _classification_extra(self, classification: dict, *, include_scores: bool = False) -> dict:
        extra = {
            "derived_event_class": classification.get("derived_event_class"),
            "game_state": classification.get("game_state"),
            "state_transition": classification.get("state_transition"),
            "period": classification.get("period"),
            "clock_seconds": classification.get("clock_seconds"),
            "score_margin": classification.get("score_margin"),
            "player_fouls": classification.get("player_fouls"),
            "scoring_run_for": classification.get("scoring_run_for"),
            "scoring_run_points": classification.get("scoring_run_points"),
        }
        if include_scores:
            extra["home_score"] = classification.get("home_score")
            extra["away_score"] = classification.get("away_score")
        return extra

    def _update_state_indices(self, event, classification: dict) -> None:
        if event.game_id not in (None, ""):
            self._game_state_index[str(event.game_id)] = {
                "game_state": classification.get("game_state"),
                "home_score": classification.get("home_score"),
                "away_score": classification.get("away_score"),
                "period": classification.get("period"),
                "clock_seconds": classification.get("clock_seconds"),
                "player_fouls": classification.get("player_fouls"),
            }
        player_key = self._player_index_key(event.game_id, event.player_id)
        if player_key and classification.get("player_fouls") is not None:
            self._player_foul_index[player_key] = classification["player_fouls"]

    def _quote_delta_extra(self, ticker: str, quote) -> dict:
        previous = self._quote_index.get(ticker)
        midpoint = round((quote.yes_bid + quote.yes_ask) / 2)
        extra = {
            "prev_midpoint_cents": None,
            "midpoint_change_cents": None,
        }
        if previous is not None and previous.get("midpoint_cents") is not None:
            extra["prev_midpoint_cents"] = previous["midpoint_cents"]
            extra["midpoint_change_cents"] = midpoint - previous["midpoint_cents"]
        self._quote_index[ticker] = {
            "midpoint_cents": midpoint,
            "quote_timestamp": quote.timestamp,
        }
        return extra

    def _track_followup_task(self, task: asyncio.Task) -> None:
        self._pending_followup_tasks.add(task)
        task.add_done_callback(self._pending_followup_tasks.discard)

    async def wait_for_pending_followups(self, timeout_seconds: float | None = None) -> None:
        pending = tuple(self._pending_followup_tasks)
        if not pending:
            return
        if timeout_seconds is None:
            await asyncio.gather(*pending, return_exceptions=True)
            return
        done, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
        if still_pending:
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)

    async def _capture_event_quotes(
        self,
        *,
        tickers: list[str],
        source_record: dict,
        source_event_type: str,
        game_id,
        player_id,
        classification: dict,
        capture_mode: str,
        horizon_seconds: float,
    ) -> list[dict]:
        snapshots = []
        for ticker in tickers:
            quote_source = "stream" if self._has_external_quote_source else "rest"
            try:
                quote = await asyncio.to_thread(self._fetch_quote, ticker)
                if quote is None:
                    quote_source = "rest"
                    quote = await asyncio.to_thread(self._default_fetch_quote, ticker)
            except Exception as exc:
                self.log.warning("Latency probe orderbook fetch failed for %s: %s", ticker, exc)
                continue
            snapshot_extra = self._classification_extra(classification)
            snapshot_extra.update({
                "capture_mode": capture_mode,
                "horizon_seconds": horizon_seconds,
                "quote_source": quote_source,
            })
            snapshot_extra.update(self._quote_delta_extra(ticker, quote))
            snapshots.append(self._capture.record_quote_snapshot(
                ticker=ticker,
                yes_bid_cents=quote.yes_bid,
                yes_ask_cents=quote.yes_ask,
                yes_bid_depth=quote.bid_depth,
                yes_ask_depth=quote.ask_depth,
                quote_timestamp=quote.timestamp,
                source_event_id=source_record["event_id"],
                source_event_type=source_event_type,
                source_event_timestamp_utc=source_record["event_timestamp_utc"],
                game_id=game_id,
                player_id=player_id,
                hypothesis_id=self._hypothesis_id,
                extra=snapshot_extra,
            ))
        return snapshots

    async def _run_followup_snapshots(
        self,
        *,
        tickers: list[str],
        source_record: dict,
        source_event_type: str,
        game_id,
        player_id,
        classification: dict,
    ) -> list[dict]:
        snapshots = []
        last_delay = 0.0
        for delay in self._followup_delays_seconds:
            sleep_seconds = max(0.0, float(delay) - last_delay)
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            snapshots.extend(await self._capture_event_quotes(
                tickers=tickers,
                source_record=source_record,
                source_event_type=source_event_type,
                game_id=game_id,
                player_id=player_id,
                classification=classification,
                capture_mode="event_followup",
                horizon_seconds=float(delay),
            ))
            last_delay = float(delay)
        return snapshots

    async def capture_market_index_snapshot(self, *, extra: dict | None = None) -> list[dict]:
        """Persist a baseline mapping snapshot and current quotes for mapped markets."""
        tickers = sorted({ticker for values in self._game_market_index.values() for ticker in values})
        source_record = self._capture.record_probe_snapshot(
            snapshot_name="market_index_snapshot",
            hypothesis_id=self._hypothesis_id,
            payload={
                "mapped_games": len(self._game_market_index),
                "mapped_tickers": tickers,
                "game_market_index": self.game_market_index,
                **(extra or {}),
            },
        )

        snapshots = []
        for ticker in tickers:
            try:
                orderbook = await asyncio.to_thread(self._fetch_orderbook, ticker)
                quote = quote_from_orderbook(ticker, orderbook)
            except Exception as exc:
                self.log.warning("Latency probe baseline orderbook fetch failed for %s: %s", ticker, exc)
                continue
            snapshot_extra = {"capture_mode": "baseline"}
            snapshot_extra.update(self._quote_delta_extra(ticker, quote))
            snapshots.append(self._capture.record_quote_snapshot(
                ticker=ticker,
                yes_bid_cents=quote.yes_bid,
                yes_ask_cents=quote.yes_ask,
                yes_bid_depth=quote.bid_depth,
                yes_ask_depth=quote.ask_depth,
                quote_timestamp=quote.timestamp,
                source_event_id=source_record["event_id"],
                source_event_type=source_record["snapshot_name"],
                source_event_timestamp_utc=source_record["observed_at"],
                hypothesis_id=self._hypothesis_id,
                extra=snapshot_extra,
            ))
        return snapshots

    async def handle_event(self, event):
        """Persist the source event and snapshot mapped Kalshi quotes."""
        if event.event_type not in self._capture_event_types:
            return []

        observed_at = time.time()
        game_key = str(event.game_id) if event.game_id not in (None, "") else None
        player_key = self._player_index_key(event.game_id, event.player_id)
        classification = classify_live_event(
            event,
            previous_game_state=self._game_state_index.get(game_key) if game_key else None,
            previous_player_fouls=self._player_foul_index.get(player_key) if player_key else None,
        )
        self._update_state_indices(event, classification)
        tickers = self._game_market_index.get(str(event.game_id), [])
        source_record = self._capture.record_source_event(
            event,
            hypothesis_id=self._hypothesis_id,
            observed_at=observed_at,
            extra={
                "mapped_tickers": tickers,
                **self._classification_extra(classification, include_scores=True),
            },
        )
        self._source_event_count += 1
        self._last_source_event_observed_at = observed_at

        if not tickers:
            return []
        self._mapped_source_event_count += 1
        self._last_mapped_source_event_observed_at = observed_at

        snapshots = await self._capture_event_quotes(
            tickers=tickers,
            source_record=source_record,
            source_event_type=event.event_type,
            game_id=event.game_id,
            player_id=event.player_id,
            classification=classification,
            capture_mode="event_immediate",
            horizon_seconds=0.0,
        )
        if classification.get("derived_event_class") and self._followup_delays_seconds:
            task = asyncio.create_task(
                self._run_followup_snapshots(
                    tickers=list(tickers),
                    source_record=source_record,
                    source_event_type=event.event_type,
                    game_id=event.game_id,
                    player_id=event.player_id,
                    classification=classification,
                ),
                name=f"oracle-latency-followup:{source_record['event_id']}",
            )
            self._track_followup_task(task)
        return snapshots


__all__ = ["OracleLatencyProbe"]
