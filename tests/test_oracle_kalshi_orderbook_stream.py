"""Tests for Kalshi WebSocket orderbook stream state handling."""

import asyncio
import json

from domain.oracle.kalshi_orderbook_stream import KalshiOrderbookStream, _ws_url_from_base


class _FakeKalshiClient:
    base_url = "https://api.elections.kalshi.com/trade-api/v2"

    def _sign(self, method, path):
        return {"method": method, "path": path}


def test_ws_url_from_base_matches_trade_api_ws_v2():
    assert _ws_url_from_base("https://api.elections.kalshi.com/trade-api/v2") == (
        "wss://api.elections.kalshi.com/trade-api/ws/v2"
    )
    assert _ws_url_from_base("https://demo-api.kalshi.co/trade-api/v2") == (
        "wss://demo-api.kalshi.co/trade-api/ws/v2"
    )


def test_stream_applies_snapshot_and_delta_to_quote_state():
    stream = KalshiOrderbookStream(_FakeKalshiClient())

    stream._handle_message({
        "type": "orderbook_snapshot",
        "sid": 2,
        "seq": 1,
        "msg": {
            "market_ticker": "KXNBAGAME-TEST",
            "yes_dollars_fp": [["0.5800", "9.00"]],
            "no_dollars_fp": [["0.4000", "14.00"]],
            "time": "2026-03-20T20:00:00Z",
        },
    })

    initial = stream.get_quote("KXNBAGAME-TEST", max_age_seconds=None)
    assert initial is not None
    assert initial.yes_bid == 58
    assert initial.yes_ask == 60
    assert initial.bid_depth == 9
    assert initial.ask_depth == 14

    stream._handle_message({
        "type": "orderbook_delta",
        "sid": 2,
        "seq": 2,
        "msg": {
            "market_ticker": "KXNBAGAME-TEST",
            "price_dollars": "0.5900",
            "delta_fp": "7.00",
            "side": "yes",
            "ts": "2026-03-20T20:00:01Z",
        },
    })
    stream._handle_message({
        "type": "orderbook_delta",
        "sid": 2,
        "seq": 3,
        "msg": {
            "market_ticker": "KXNBAGAME-TEST",
            "price_dollars": "0.4100",
            "delta_fp": "12.00",
            "side": "no",
            "ts": "2026-03-20T20:00:02Z",
        },
    })

    updated = stream.get_quote("KXNBAGAME-TEST", max_age_seconds=None)
    assert updated is not None
    assert updated.yes_bid == 59
    assert updated.yes_ask == 59
    assert updated.bid_depth == 7
    assert updated.ask_depth == 12


def test_stream_sets_market_tickers_via_subscribe_and_update():
    class FakeWs:
        def __init__(self):
            self.messages = []

        async def send_str(self, data):
            self.messages.append(json.loads(data))

    stream = KalshiOrderbookStream(_FakeKalshiClient())
    stream._ws = FakeWs()
    stream._connected = True

    asyncio.run(stream.set_market_tickers(["T1", "T2"]))
    assert stream._ws.messages[0]["cmd"] == "subscribe"
    assert stream._ws.messages[0]["params"]["market_tickers"] == ["T1", "T2"]

    stream._subscription_sid = 7
    asyncio.run(stream.set_market_tickers(["T2", "T3"]))
    assert stream._ws.messages[1]["cmd"] == "update_subscription"
    assert stream._ws.messages[1]["params"]["action"] == "add_markets"
    assert stream._ws.messages[1]["params"]["market_tickers"] == ["T3"]
    assert stream._ws.messages[2]["cmd"] == "update_subscription"
    assert stream._ws.messages[2]["params"]["action"] == "delete_markets"
    assert stream._ws.messages[2]["params"]["market_tickers"] == ["T1"]


def test_stream_reconnect_loop_retries_until_success():
    stream = KalshiOrderbookStream(_FakeKalshiClient())
    stream.RECONNECT_BACKOFF_SECONDS = 0.0
    stream.RECONNECT_MAX_SECONDS = 0.0
    attempts = []

    async def fake_connect_socket(*, aiohttp_module=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")
        stream._connected = True

    stream._connect_socket = fake_connect_socket

    async def run_reconnect():
        stream._schedule_reconnect()
        await stream._reconnect_task

    asyncio.run(run_reconnect())

    assert len(attempts) == 2
    assert stream.connected is True
