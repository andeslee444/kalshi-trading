"""Tests for Oracle latency probe mapping and capture."""

import asyncio
import importlib
import sys

import pytest

from conftest import make_fake_auth
from event_ledger import EVENT_TYPE_MARKET_SNAPSHOT, EVENT_TYPE_SOURCE_OBSERVATION
from domain.oracle.alpha_capture import OracleAlphaCapture
from domain.oracle.latency_probe import OracleLatencyProbe
from domain.oracle.models import QuoteSnapshot
from domain.oracle.real_sports_client import LiveEvent


def test_build_game_market_index_matches_real_games():
    real_markets = [
        {
            "game_id": 23454,
            "home_team": "Rockets",
            "away_team": "Hawks",
            "start_time": "2026-03-21T00:30:00Z",
            "scheduled_day": "2026-03-20",
        },
    ]
    kalshi_markets = [
        {"ticker": "KXNBAGAME-26MAR20ATLHOU-HOU"},
        {"ticker": "KXNBAGAME-26MAR20ATLHOU-ATL"},
        {"ticker": "KXNBAGAME-26MAR20NYKBKN-NYK"},
    ]

    index = OracleLatencyProbe.build_game_market_index(real_markets, kalshi_markets)

    assert index == {"23454": ["KXNBAGAME-26MAR20ATLHOU-ATL", "KXNBAGAME-26MAR20ATLHOU-HOU"]}


def test_latency_probe_handles_event_and_snapshots_quotes(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")
    probe = OracleLatencyProbe(
        kalshi_client=None,
        capture=capture,
        fetch_orderbook_func=lambda ticker: {
            "orderbook": {
                "yes": [[61, 9]],
                "no": [[37, 14]],
            }
        },
    )
    probe.refresh_game_market_index(
        [
            {
                "game_id": 23454,
                "home_team": "Atlanta Hawks",
                "away_team": "Golden State Warriors",
                "start_time": "2026-03-21T23:30:00Z",
            },
        ],
        [{"ticker": "KXNBAGAME-26MAR21GSWATL-ATL"}],
    )

    event = LiveEvent(
        event_type="GameUpdated",
        game_id=23454,
        data={"homeScore": 98, "awayScore": 97, "period": "Q4", "clock": "1:15"},
        timestamp=1_763_339_200.0,
    )

    snapshots = asyncio.run(probe.handle_event(event))

    source_rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)
    quote_rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_MARKET_SNAPSHOT)

    assert len(snapshots) == 1
    assert source_rows[-1]["game_id"] == 23454
    assert source_rows[-1]["derived_event_class"] == "ot_likely_entry"
    assert source_rows[-1]["game_state"] == "ot_likely"
    assert quote_rows[-1]["ticker"] == "KXNBAGAME-26MAR21GSWATL-ATL"
    assert quote_rows[-1]["derived_event_class"] == "ot_likely_entry"
    assert quote_rows[-1]["yes_bid_cents"] == 61
    assert quote_rows[-1]["yes_ask_cents"] == 63


def test_latency_probe_schedules_followup_snapshots_for_classified_events(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")
    orderbooks = [
        {
            "orderbook": {
                "yes": [[61, 9]],
                "no": [[37, 14]],
            }
        },
        {
            "orderbook": {
                "yes": [[70, 11]],
                "no": [[28, 9]],
            }
        },
    ]

    def fetch_orderbook(_ticker):
        return orderbooks.pop(0)

    probe = OracleLatencyProbe(
        kalshi_client=None,
        capture=capture,
        followup_delays_seconds=(0.01,),
        fetch_orderbook_func=fetch_orderbook,
    )
    probe.refresh_game_market_index(
        [
            {
                "game_id": 23454,
                "home_team": "Atlanta Hawks",
                "away_team": "Golden State Warriors",
                "start_time": "2026-03-21T23:30:00Z",
            },
        ],
        [{"ticker": "KXNBAGAME-26MAR21GSWATL-ATL"}],
    )

    event = LiveEvent(
        event_type="GameUpdated",
        game_id=23454,
        data={"homeScore": 98, "awayScore": 97, "period": "Q4", "clock": "1:15"},
        timestamp=1_763_339_200.0,
    )

    async def run_capture():
        await probe.handle_event(event)
        await probe.wait_for_pending_followups(timeout_seconds=0.2)

    asyncio.run(run_capture())

    quote_rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_MARKET_SNAPSHOT)

    assert len(quote_rows) == 2
    assert quote_rows[0]["capture_mode"] == "event_immediate"
    assert quote_rows[0]["horizon_seconds"] == 0.0
    assert quote_rows[1]["capture_mode"] == "event_followup"
    assert quote_rows[1]["horizon_seconds"] == 0.01
    assert quote_rows[1]["yes_bid_cents"] == 70
    assert quote_rows[1]["yes_ask_cents"] == 72


def test_latency_probe_uses_external_quote_source_when_available(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")
    probe = OracleLatencyProbe(
        kalshi_client=None,
        capture=capture,
        fetch_quote_func=lambda ticker: QuoteSnapshot(
            ticker=ticker,
            yes_bid=62,
            yes_ask=64,
            bid_depth=7,
            ask_depth=8,
            timestamp=1_763_339_201.0,
        ),
        fetch_orderbook_func=lambda _ticker: (_ for _ in ()).throw(AssertionError("REST fallback should not run")),
    )
    probe.refresh_game_market_index(
        [
            {
                "game_id": 23454,
                "home_team": "Atlanta Hawks",
                "away_team": "Golden State Warriors",
                "start_time": "2026-03-21T23:30:00Z",
            },
        ],
        [{"ticker": "KXNBAGAME-26MAR21GSWATL-ATL"}],
    )

    event = LiveEvent(
        event_type="GameUpdated",
        game_id=23454,
        data={"homeScore": 98, "awayScore": 97, "period": "Q4", "clock": "1:15"},
        timestamp=1_763_339_200.0,
    )

    asyncio.run(probe.handle_event(event))

    quote_rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_MARKET_SNAPSHOT)

    assert quote_rows[-1]["quote_source"] == "stream"
    assert quote_rows[-1]["yes_bid_cents"] == 62
    assert quote_rows[-1]["yes_ask_cents"] == 64


def test_latency_probe_tracks_mapped_source_event_stats(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")
    probe = OracleLatencyProbe(
        kalshi_client=None,
        capture=capture,
        fetch_orderbook_func=lambda ticker: {
            "orderbook": {
                "yes": [[61, 9]],
                "no": [[37, 14]],
            }
        },
    )
    probe.refresh_game_market_index(
        [
            {
                "game_id": 23454,
                "home_team": "Atlanta Hawks",
                "away_team": "Golden State Warriors",
                "start_time": "2026-03-21T23:30:00Z",
            },
        ],
        [{"ticker": "KXNBAGAME-26MAR21GSWATL-ATL"}],
    )

    event = LiveEvent(
        event_type="GameUpdated",
        game_id=23454,
        data={"homeScore": 98, "awayScore": 97, "period": "Q4", "clock": "1:15"},
        timestamp=1_763_339_200.0,
    )

    asyncio.run(probe.handle_event(event))

    assert probe.source_event_count == 1
    assert probe.mapped_source_event_count == 1
    assert probe.last_source_event_age_seconds(now=probe._last_source_event_observed_at + 10.0) == 10.0
    assert probe.last_mapped_source_event_age_seconds(now=probe._last_mapped_source_event_observed_at + 10.0) == 10.0


def test_latency_probe_captures_market_index_snapshot(tmp_path):
    capture = OracleAlphaCapture(path=tmp_path / "oracle-alpha.sqlite3")
    probe = OracleLatencyProbe(
        kalshi_client=None,
        capture=capture,
        fetch_orderbook_func=lambda ticker: {
            "orderbook": {
                "yes": [[58, 11]],
                "no": [[40, 13]],
            }
        },
    )
    probe.refresh_game_market_index(
        [
            {
                "game_id": 23454,
                "home_team": "Atlanta Hawks",
                "away_team": "Golden State Warriors",
                "start_time": "2026-03-21T23:30:00Z",
            },
        ],
        [{"ticker": "KXNBAGAME-26MAR21GSWATL-ATL"}],
    )

    snapshots = asyncio.run(
        probe.capture_market_index_snapshot(extra={"real_games_count": 1, "kalshi_markets_count": 1})
    )

    source_rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)
    quote_rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_MARKET_SNAPSHOT)

    assert len(snapshots) == 1
    assert source_rows[-1]["record_kind"] == "probe_snapshot"
    assert source_rows[-1]["mapped_games"] == 1
    assert quote_rows[-1]["source_event_type"] == "market_index_snapshot"
    assert quote_rows[-1]["yes_bid_cents"] == 58


def test_fatal_source_failures_identify_empty_schedule():
    fake_auth = make_fake_auth()
    sys.modules["kalshi_auth"] = fake_auth
    try:
        mod = importlib.import_module("apps.oracle_latency_probe")
        failures = mod._fatal_source_failures(
            {
                "schedule": {},
                "latest_day": "2026-03-19",
                "expected_game_count": None,
                "discovery_games": [],
                "crowd_markets": [],
                "kalshi_markets": [{"ticker": "KXNBA-TEST"}],
            }
        )
    finally:
        sys.modules.pop("kalshi_auth", None)
        sys.modules.pop("apps.oracle_latency_probe", None)

    assert len(failures) == 1
    assert failures[0]["source_name"] == "real_schedule"
    assert failures[0]["failure_code"] == "missing_schedule_days"


def test_crowd_source_failures_identify_empty_book_a_source():
    fake_auth = make_fake_auth()
    sys.modules["kalshi_auth"] = fake_auth
    try:
        mod = importlib.import_module("apps.oracle_latency_probe")
        failures = mod._crowd_source_failures(
            {
                "latest_day": "2026-03-19",
                "expected_game_count": 8,
                "discovery_games": [{"game_id": 23454}],
                "crowd_markets": [],
            }
        )
    finally:
        sys.modules.pop("kalshi_auth", None)
        sys.modules.pop("apps.oracle_latency_probe", None)

    assert len(failures) == 1
    assert failures[0]["source_name"] == "real_sports_game_markets"
    assert failures[0]["failure_code"] == "empty_crowd_price_response"


def test_fetch_kalshi_game_markets_uses_series_ticker_query():
    fake_auth = make_fake_auth()
    sys.modules["kalshi_auth"] = fake_auth
    try:
        mod = importlib.import_module("apps.oracle_latency_probe")

        class FakeKalshiClient:
            def __init__(self):
                self.seen = []

            def get(self, path):
                self.seen.append(path)
                return {"markets": [{"ticker": "KXNBAGAME-26MAR20ATLHOU-ATL"}]}

        client = FakeKalshiClient()
        markets = asyncio.run(mod._fetch_kalshi_game_markets(client))
    finally:
        sys.modules.pop("kalshi_auth", None)
        sys.modules.pop("apps.oracle_latency_probe", None)

    assert client.seen == ["/markets?series_ticker=KXNBAGAME&status=open&limit=1000"]
    assert markets == [{"ticker": "KXNBAGAME-26MAR20ATLHOU-ATL"}]


def test_async_main_fails_closed_on_empty_discovery_games_when_schedule_has_slate(tmp_path, monkeypatch):
    fake_auth = make_fake_auth(PROJECT_DIR=tmp_path)
    sys.modules["kalshi_auth"] = fake_auth
    try:
        mod = importlib.import_module("apps.oracle_latency_probe")
        ledger_path = tmp_path / "oracle-alpha.sqlite3"

        class FakeRealConfig:
            can_auto_login = False

            def validate(self):
                return []

        class FakeRealClient:
            def __init__(self, _cfg):
                self.closed = False

            async def close(self):
                self.closed = True

        monkeypatch.setattr(mod.RealSportsConfig, "from_bots_config", classmethod(lambda cls, cfg: FakeRealConfig()))
        monkeypatch.setattr(mod, "_load_oracle_config", lambda: {"research": {"alphaLedgerPath": str(ledger_path)}})
        monkeypatch.setattr(mod, "RealSportsClient", FakeRealClient)
        monkeypatch.setattr(mod, "KalshiClient", lambda: object())

        async def fake_fetch_market_context(_real_client, _kalshi_client):
            return {
                "schedule": {"days": [{"day": "2026-03-19", "count": "8"}]},
                "latest_day": "2026-03-19",
                "expected_game_count": 8,
                "discovery_games": [],
                "crowd_markets": [],
                "kalshi_markets": [{"ticker": "KXNBA-TEST"}],
            }

        monkeypatch.setattr(mod, "_fetch_market_context", fake_fetch_market_context)

        with pytest.raises(SystemExit, match="failing closed"):
            asyncio.run(mod.async_main(once=True))

        capture = OracleAlphaCapture(path=ledger_path)
        rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)
    finally:
        sys.modules.pop("kalshi_auth", None)
        sys.modules.pop("apps.oracle_latency_probe", None)

    assert rows[-1]["record_kind"] == "source_failure"
    assert rows[-1]["source_name"] == "real_home_next"


def test_async_main_allows_empty_crowd_markets_when_discovery_is_healthy(tmp_path, monkeypatch):
    fake_auth = make_fake_auth(PROJECT_DIR=tmp_path)
    sys.modules["kalshi_auth"] = fake_auth
    try:
        mod = importlib.import_module("apps.oracle_latency_probe")
        ledger_path = tmp_path / "oracle-alpha.sqlite3"

        class FakeRealConfig:
            can_auto_login = False

            def validate(self):
                return []

        class FakeRealClient:
            def __init__(self, _cfg):
                self.closed = False

            async def close(self):
                self.closed = True

        monkeypatch.setattr(mod.RealSportsConfig, "from_bots_config", classmethod(lambda cls, cfg: FakeRealConfig()))
        monkeypatch.setattr(mod, "_load_oracle_config", lambda: {"research": {"alphaLedgerPath": str(ledger_path)}})
        monkeypatch.setattr(mod, "RealSportsClient", FakeRealClient)
        monkeypatch.setattr(mod, "KalshiClient", lambda: object())

        async def fake_fetch_market_context(_real_client, _kalshi_client):
            return {
                "schedule": {"days": [{"day": "2026-03-19", "count": "1"}]},
                "latest_day": "2026-03-19",
                "expected_game_count": 1,
                "discovery_games": [
                    {
                        "game_id": 23454,
                        "home_team": "Atlanta Hawks",
                        "away_team": "Golden State Warriors",
                        "start_time": "2026-03-21T23:30:00Z",
                    }
                ],
                "crowd_markets": [],
                "kalshi_markets": [{"ticker": "KXNBAGAME-26MAR21GSWATL-ATL"}],
            }

        monkeypatch.setattr(mod, "_fetch_market_context", fake_fetch_market_context)

        asyncio.run(mod.async_main(once=True))

        capture = OracleAlphaCapture(path=ledger_path)
        rows = capture.ledger._fetch_event_payloads(EVENT_TYPE_SOURCE_OBSERVATION)
    finally:
        sys.modules.pop("kalshi_auth", None)
        sys.modules.pop("apps.oracle_latency_probe", None)

    assert any(row["record_kind"] == "source_failure" and row["failure_code"] == "empty_crowd_price_response" for row in rows)
    assert any(row["record_kind"] == "probe_snapshot" for row in rows)


def test_runtime_health_failures_detect_zero_mapping():
    fake_auth = make_fake_auth()
    sys.modules["kalshi_auth"] = fake_auth
    try:
        mod = importlib.import_module("apps.oracle_latency_probe")

        class FakeProbe:
            mapped_source_event_count = 0

            def last_mapped_source_event_age_seconds(self, *, now=None):
                return None

        class FakeRealWs:
            connected = True
            is_disabled = False
            is_stale = False
            data_age_seconds = 5.0

        failures = mod._runtime_health_failures(
            context={
                "latest_day": "2026-03-21",
                "discovery_games": [
                    {"game_id": 1, "status": "inprogress"},
                    {"game_id": 2, "status": "scheduled"},
                ],
                "kalshi_markets": [{"ticker": "KXNBAGAME-1"}],
            },
            index={},
            probe=FakeProbe(),
            real_ws=FakeRealWs(),
            kalshi_stream=None,
            event_drought_seconds=120.0,
            now=1_000.0,
        )
    finally:
        sys.modules.pop("kalshi_auth", None)
        sys.modules.pop("apps.oracle_latency_probe", None)

    assert len(failures) == 1
    assert failures[0]["source_name"] == "oracle_mapping"
    assert failures[0]["failure_code"] == "zero_mapped_tickers"


def test_runtime_health_failures_detect_live_stream_gaps():
    fake_auth = make_fake_auth()
    sys.modules["kalshi_auth"] = fake_auth
    try:
        mod = importlib.import_module("apps.oracle_latency_probe")

        class FakeProbe:
            mapped_source_event_count = 0

            def last_mapped_source_event_age_seconds(self, *, now=None):
                return 180.0

        class FakeRealWs:
            connected = True
            is_disabled = False
            is_stale = False
            data_age_seconds = 2.0

        class FakeKalshiStream:
            connected = False

        failures = mod._runtime_health_failures(
            context={
                "latest_day": "2026-03-21",
                "discovery_games": [
                    {"game_id": 1, "status": "inprogress"},
                    {"game_id": 2, "status": "final"},
                ],
                "kalshi_markets": [{"ticker": "KXNBAGAME-1"}],
            },
            index={"1": ["KXNBAGAME-1"]},
            probe=FakeProbe(),
            real_ws=FakeRealWs(),
            kalshi_stream=FakeKalshiStream(),
            event_drought_seconds=120.0,
            now=1_000.0,
        )
    finally:
        sys.modules.pop("kalshi_auth", None)
        sys.modules.pop("apps.oracle_latency_probe", None)

    assert len(failures) == 2
    assert {(failure["source_name"], failure["failure_code"]) for failure in failures} == {
        ("oracle_mapping", "mapped_event_drought"),
        ("kalshi_orderbook_ws", "disconnected"),
    }
