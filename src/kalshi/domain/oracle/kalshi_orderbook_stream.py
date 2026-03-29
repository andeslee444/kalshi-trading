"""Kalshi WebSocket orderbook stream for Oracle H1 latency research."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from domain.oracle.execution.quote_check import quote_from_orderbook

_log = logging.getLogger("oracle.kalshi_orderbook_stream")


def _ws_url_from_base(base_url: str) -> str:
    base = str(base_url).rstrip("/")
    if base.endswith("/trade-api/v2"):
        base = base[:-len("/trade-api/v2")]
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/trade-api/ws/v2"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/trade-api/ws/v2"
    return base + "/trade-api/ws/v2"


def _dollars_to_cents(value: Any) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _shares_to_int(value: Any) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _timestamp_to_epoch(value: Any) -> float:
    if value in (None, ""):
        return dt.datetime.now(dt.timezone.utc).timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return dt.datetime.now(dt.timezone.utc).timestamp()


class KalshiOrderbookStream:
    """Maintains live top-of-book state from Kalshi orderbook WebSocket updates."""

    CHANNEL = "orderbook_delta"
    WS_PATH = "/trade-api/ws/v2"
    RECONNECT_BACKOFF_SECONDS = 1.0
    RECONNECT_MAX_SECONDS = 30.0

    def __init__(self, kalshi_client, *, logger=None):
        self._client = kalshi_client
        self.log = logger or _log
        self._ws_url = _ws_url_from_base(getattr(kalshi_client, "base_url", ""))
        self._session = None
        self._ws = None
        self._reader_task: asyncio.Task | None = None
        self._connected = False
        self._desired_tickers: set[str] = set()
        self._subscription_sid: int | None = None
        self._command_id = 0
        self._book_state: dict[str, dict[str, Any]] = {}
        self._quote_state: dict[str, Any] = {}
        self._sequence_by_sid: dict[int, int] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._reconnect_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def get_quote(self, ticker: str, *, max_age_seconds: float = 5.0):
        quote = self._quote_state.get(ticker)
        if quote is None:
            return None
        if max_age_seconds is not None and (dt.datetime.now(dt.timezone.utc).timestamp() - quote.timestamp) > max_age_seconds:
            return None
        return quote

    async def connect(self) -> None:
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError("aiohttp is required for Kalshi WebSocket streaming") from exc

        self._closed = False
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
            self._reconnect_task = None
        await self._connect_socket(aiohttp_module=aiohttp)

    async def _connect_socket(self, *, aiohttp_module=None) -> None:
        if aiohttp_module is None:
            try:
                import aiohttp as aiohttp_module
            except ImportError as exc:
                raise RuntimeError("aiohttp is required for Kalshi WebSocket streaming") from exc
        async with self._lock:
            if self._connected and self._ws is not None:
                return
            await self._close_transport_locked()
            self._subscription_sid = None
            self._sequence_by_sid.clear()
            headers = self._client._sign("GET", self.WS_PATH)
            self._session = aiohttp_module.ClientSession()
            self._ws = await self._session.ws_connect(
                self._ws_url,
                headers=headers,
                autoping=True,
                heartbeat=20.0,
            )
            self._connected = True
            self.log.info("Connected to Kalshi orderbook WebSocket: %s", self._ws_url)
            self._reader_task = asyncio.create_task(
                self._reader_loop(),
                name="kalshi-orderbook-stream",
            )
            if self._desired_tickers:
                await self._send_subscribe(sorted(self._desired_tickers))

    async def disconnect(self) -> None:
        self._closed = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
            self._reconnect_task = None
        async with self._lock:
            self._connected = False
            await self._close_transport_locked()

    async def _close_transport_locked(self) -> None:
        self._subscription_sid = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def set_market_tickers(self, tickers: list[str]) -> None:
        desired = {ticker for ticker in tickers if ticker}
        previous = set(self._desired_tickers)
        self._desired_tickers = desired
        if not self._connected or self._ws is None:
            return
        if self._subscription_sid is None:
            if desired:
                await self._send_subscribe(sorted(desired))
            return
        add = sorted(desired - previous)
        remove = sorted(previous - desired)
        if add:
            await self._send_update_subscription(add, action="add_markets")
        if remove:
            await self._send_update_subscription(remove, action="delete_markets")
            for ticker in remove:
                self._book_state.pop(ticker, None)
                self._quote_state.pop(ticker, None)

    async def _send_subscribe(self, market_tickers: list[str]) -> None:
        if not market_tickers or self._ws is None:
            return
        await self._send_json({
            "id": self._next_command_id(),
            "cmd": "subscribe",
            "params": {
                "channels": [self.CHANNEL],
                "market_tickers": market_tickers,
            },
        })

    async def _send_update_subscription(self, market_tickers: list[str], *, action: str) -> None:
        if not market_tickers or self._subscription_sid is None or self._ws is None:
            return
        await self._send_json({
            "id": self._next_command_id(),
            "cmd": "update_subscription",
            "params": {
                "sid": self._subscription_sid,
                "market_tickers": market_tickers,
                "action": action,
            },
        })

    async def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            return
        await self._ws.send_str(json.dumps(payload))

    def _next_command_id(self) -> int:
        self._command_id += 1
        return self._command_id

    async def _reader_loop(self) -> None:
        try:
            async for message in self._ws:
                if message.type.name == "TEXT":
                    self._handle_message(json.loads(message.data))
                elif message.type.name in {"CLOSED", "CLOSING", "ERROR"}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.warning("Kalshi orderbook stream reader failed: %s", exc)
        finally:
            self._connected = False
            if not self._closed:
                self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._closed:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(),
            name="kalshi-orderbook-reconnect",
        )

    async def _reconnect_loop(self) -> None:
        delay = self.RECONNECT_BACKOFF_SECONDS
        while not self._closed:
            try:
                await asyncio.sleep(delay)
                await self._connect_socket()
                self.log.info("Reconnected to Kalshi orderbook WebSocket")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning(
                    "Kalshi orderbook reconnect failed, retrying in %.1fs: %s",
                    delay,
                    exc,
                )
                delay = min(delay * 2, self.RECONNECT_MAX_SECONDS)

    def _handle_message(self, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type == "subscribed":
            msg = payload.get("msg", {})
            if msg.get("channel") == self.CHANNEL:
                self._subscription_sid = msg.get("sid")
                self.log.info(
                    "Kalshi orderbook subscription active: sid=%s markets=%d",
                    self._subscription_sid,
                    len(self._desired_tickers),
                )
            return
        if msg_type == "ok":
            return
        if msg_type == "error":
            self.log.warning("Kalshi orderbook stream error: %s", payload.get("msg"))
            return
        if msg_type == "orderbook_snapshot":
            self._apply_orderbook_snapshot(payload)
            return
        if msg_type == "orderbook_delta":
            self._apply_orderbook_delta(payload)

    def _apply_orderbook_snapshot(self, payload: dict) -> None:
        sid = payload.get("sid")
        seq = payload.get("seq")
        if sid is not None and seq is not None:
            self._sequence_by_sid[int(sid)] = int(seq)
        msg = payload.get("msg", {})
        ticker = msg.get("market_ticker")
        if not ticker:
            return
        yes_levels = self._parse_levels(msg.get("yes_dollars_fp", []))
        no_levels = self._parse_levels(msg.get("no_dollars_fp", []))
        timestamp = _timestamp_to_epoch(msg.get("ts") or msg.get("time"))
        self._book_state[ticker] = {
            "yes": yes_levels,
            "no": no_levels,
            "timestamp": timestamp,
        }
        self._refresh_quote(ticker)

    def _apply_orderbook_delta(self, payload: dict) -> None:
        sid = payload.get("sid")
        seq = payload.get("seq")
        if sid is not None and seq is not None:
            sid_int = int(sid)
            seq_int = int(seq)
            previous = self._sequence_by_sid.get(sid_int)
            if previous is not None and seq_int <= previous:
                return
            self._sequence_by_sid[sid_int] = seq_int
        msg = payload.get("msg", {})
        ticker = msg.get("market_ticker")
        side = msg.get("side")
        if not ticker or side not in {"yes", "no"}:
            return
        state = self._book_state.setdefault(
            ticker,
            {"yes": [], "no": [], "timestamp": dt.datetime.now(dt.timezone.utc).timestamp()},
        )
        levels = list(state.get(side, []))
        price_cents = _dollars_to_cents(msg.get("price_dollars", "0"))
        delta_size = _shares_to_int(msg.get("delta_fp", "0"))
        level_by_price = {price: size for price, size in levels}
        new_size = level_by_price.get(price_cents, 0) + delta_size
        if new_size <= 0:
            level_by_price.pop(price_cents, None)
        else:
            level_by_price[price_cents] = new_size
        state[side] = sorted(level_by_price.items(), key=lambda item: item[0], reverse=True)
        state["timestamp"] = _timestamp_to_epoch(msg.get("ts"))
        self._book_state[ticker] = state
        self._refresh_quote(ticker)

    @staticmethod
    def _parse_levels(raw_levels: list[list[Any]]) -> list[tuple[int, int]]:
        levels = []
        for raw in raw_levels or []:
            if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                continue
            levels.append((_dollars_to_cents(raw[0]), _shares_to_int(raw[1])))
        levels.sort(key=lambda item: item[0], reverse=True)
        return levels

    def _refresh_quote(self, ticker: str) -> None:
        state = self._book_state.get(ticker)
        if not state:
            return
        quote = quote_from_orderbook(
            ticker,
            {
                "orderbook": {
                    "yes": [[price, size] for price, size in state.get("yes", [])],
                    "no": [[price, size] for price, size in state.get("no", [])],
                }
            },
        )
        quote.timestamp = float(state.get("timestamp") or dt.datetime.now(dt.timezone.utc).timestamp())
        self._quote_state[ticker] = quote


__all__ = ["KalshiOrderbookStream", "_ws_url_from_base"]
