"""Tests for Oracle Real Sports client (async with respx mocks)."""

import asyncio
import pytest
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from domain.oracle.real_sports_client import (
    RealSportsConfig,
    RealSportsClient,
    RealSportsWebSocket,
    parse_game_market_response,
    parse_home_game_response,
    parse_player_splits,
    LiveEvent,
    _generate_request_token,
)


# ── Config tests (sync) ──

def test_config_defaults():
    cfg = RealSportsConfig()
    assert cfg.base_url == "https://web.realapp.com"
    assert cfg.ws_url == "https://web.realsports.io"
    assert cfg.polling_interval_seconds == 5


def test_config_from_env():
    env = {
        "REAL_USER_ID": "k3LkNN1v",
        "REAL_TOKEN": "test-token",
        "REAL_DEVICE_ID": "dev-123",
        "REAL_DEVICE_UUID": "uuid-456",
    }
    with patch.dict("os.environ", env, clear=False):
        cfg = RealSportsConfig.from_env()
        assert cfg.user_id == "k3LkNN1v"  # string, not int
        assert cfg.token == "test-token"
        assert cfg.device_id == "dev-123"


def test_config_from_bots_config():
    oracle_cfg = {
        "realSports": {
            "baseUrl": "https://custom.api.com",
            "pollingIntervalSeconds": 10,
            "minVolume": 200000,
        },
    }
    with patch.dict("os.environ", {}, clear=False):
        cfg = RealSportsConfig.from_bots_config(oracle_cfg)
        assert cfg.base_url == "https://custom.api.com"
        assert cfg.polling_interval_seconds == 10
        assert cfg.min_volume == 200000


# ── Parse helpers (sync) ──

def test_parse_game_market_response():
    raw = {
        "gameId": 23454,
        "homeTeam": {"name": "Los Angeles Lakers"},
        "awayTeam": {"name": "Houston Rockets"},
        "homeTeamWinPct": 68,
        "awayTeamWinPct": 32,
        "volume": 5820000,
        "status": "live",
        "sport": "nba",
        "period": "Q3",
        "clock": "5:30",
        "homeScore": 78,
        "awayScore": 65,
    }
    parsed = parse_game_market_response(raw)
    assert parsed["game_id"] == 23454
    assert parsed["home_team"] == "Los Angeles Lakers"
    assert parsed["away_team"] == "Houston Rockets"
    assert parsed["home_pct"] == 68
    assert parsed["volume"] == 5820000
    assert parsed["status"] == "live"


def test_parse_game_market_response_missing_fields():
    raw = {"gameId": 100, "status": "scheduled"}
    parsed = parse_game_market_response(raw)
    assert parsed["game_id"] == 100
    assert parsed["home_team"] == ""
    assert parsed["volume"] == 0


def test_parse_home_game_response():
    raw = {
        "id": 23454,
        "homeTeam": {"name": "Los Angeles Lakers"},
        "awayTeam": {"name": "Houston Rockets"},
        "status": "live",
        "dateTime": "2026-03-18T23:30:00.000Z",
        "periodName": "Q3",
        "homeTeamScore": 78,
        "awayTeamScore": 65,
        "homeMoneyline": -140,
        "awayMoneyline": 120,
        "pointSpread": "3.5",
        "overUnder": "228.5",
    }
    parsed = parse_home_game_response(raw)
    assert parsed["game_id"] == 23454
    assert parsed["home_team"] == "Los Angeles Lakers"
    assert parsed["away_team"] == "Houston Rockets"
    assert parsed["start_time"] == "2026-03-18T23:30:00.000Z"
    assert parsed["home_moneyline"] == -140
    assert parsed["over_under"] == "228.5"


def test_parse_player_splits():
    raw = {
        "splits": {
            "points": {
                "last5": [28, 32, 22, 35, 30],
                "last10": [28, 32, 22, 35, 30, 24, 19, 31, 27, 26],
                "seasonAvg": 26.5,
                "homeAvg": 28.0,
                "awayAvg": 25.0,
                "perOpponent": {"HOU": 31.5},
            },
            "rebounds": {
                "last5": [8, 10, 7, 12, 9],
                "seasonAvg": 8.5,
                "homeAvg": 9.0,
                "awayAvg": 8.0,
            },
        },
    }
    stats = parse_player_splits(raw)
    assert stats["points"]["last_5"] == [28, 32, 22, 35, 30]
    assert stats["points"]["season_avg"] == 26.5
    assert stats["points"]["per_opponent"] == {"HOU": 31.5}
    assert stats["rebounds"]["last_5"] == [8, 10, 7, 12, 9]


def test_parse_player_splits_empty():
    stats = parse_player_splits({})
    assert stats["points"]["last_5"] == []
    assert stats["points"]["season_avg"] == 0


# ── LiveEvent tests ──

def test_live_event_creation():
    event = LiveEvent(
        event_type="GameUpdated",
        game_id=23454,
        data={"homeScore": 85, "awayScore": 72},
    )
    assert event.event_type == "GameUpdated"
    assert event.game_id == 23454
    assert event.data["homeScore"] == 85


# ── Hashids encoding ──

def test_generate_request_token():
    class FakeHashids:
        def encode(self, value):
            return f"tok-{value}"

    with patch("domain.oracle.real_sports_client._get_request_token_hashids", return_value=FakeHashids()):
        token = _generate_request_token()
        assert isinstance(token, str)
        assert token.startswith("tok-")
        # Tokens from different calls should differ (timestamp-based)
        import time
        time.sleep(0.01)  # ensure different millisecond
        token2 = _generate_request_token()
        assert token != token2


def test_config_validate_missing():
    cfg = RealSportsConfig()  # all defaults = missing credentials
    missing = cfg.validate()
    assert "REAL_USER_ID" in missing
    assert "REAL_TOKEN" in missing
    assert "REAL_DEVICE_ID" in missing


def test_config_validate_complete():
    cfg = RealSportsConfig(
        user_id="k3LkNN1v", token="tok", device_id="dev", device_uuid="uuid",
    )
    assert cfg.validate() == []


def test_auth_headers_format():
    """Verify real-auth-info header matches spec: {userId}!{deviceId}!{token}."""
    cfg = RealSportsConfig(
        user_id="k3LkNN1v",
        token="3293b82e-1fd8-4e87-b6c2-6cd7363dcc70",
        device_id="39bQYPRE",
        device_uuid="test-uuid",
    )
    client = RealSportsClient(cfg)
    headers = client._base_headers()
    assert headers["real-auth-info"] == "k3LkNN1v!39bQYPRE!3293b82e-1fd8-4e87-b6c2-6cd7363dcc70"
    assert headers["real-device-type"] == "desktop_web"
    assert headers["real-device-uuid"] == "test-uuid"
    assert headers["real-version"] == "28"
    assert "real-device-name" in headers
    assert headers["origin"] == "https://www.realapp.com"


def test_real_ws_url_matches_browser_query_shape():
    cfg = RealSportsConfig(
        ws_url="https://web.realsports.io",
        user_id="k3LkNN1v",
        token="3293b82e-1fd8-4e87-b6c2-6cd7363dcc70",
        device_id="39bQYPRE",
    )
    ws = RealSportsWebSocket(cfg)
    with patch("domain.oracle.real_sports_client._generate_request_token", return_value="tok-123"), patch(
        "domain.oracle.real_sports_client.uuid.uuid4",
        return_value="generated-uuid",
    ):
        url = ws._build_ws_url()

    parsed = urlsplit(url)
    qs = parse_qs(parsed.query)

    assert parsed.scheme == "wss"
    assert parsed.netloc == "web.realsports.io"
    assert parsed.path == "/socket.io/"
    assert qs["socketType"] == ["LiveFeed"]
    assert qs["sport"] == ["all"]
    assert qs["deviceType"] == ["desktop_web"]
    assert qs["deviceVersion"] == ["undefined"]
    assert qs["realVersion"] == ["28"]
    assert qs["realRequestToken"] == ["tok-123"]
    assert qs["auth"] == ["k3LkNN1v!39bQYPRE!3293b82e-1fd8-4e87-b6c2-6cd7363dcc70"]
    assert qs["deviceUuid"] == ["generated-uuid"]
    assert qs["EIO"] == ["3"]
    assert qs["transport"] == ["websocket"]


def test_real_ws_dispatch_unwraps_play_lists():
    cfg = RealSportsConfig(
        user_id="k3LkNN1v",
        token="tok",
        device_id="dev",
        device_uuid="uuid",
    )
    ws = RealSportsWebSocket(cfg)
    seen = []

    async def handler(event):
        seen.append(event)

    ws.on("LiveFeedSocketPlaysAdded", handler)
    asyncio.run(
        ws._dispatch_event(
            "LiveFeedSocketPlaysAdded",
            {
                "plays": [
                    {"gameId": 23547, "playerId": 20001835, "type": "FieldGoalMade"},
                    {"gameId": 23547, "playerId": 20002270, "type": "Assist"},
                ]
            },
        )
    )

    assert [event.game_id for event in seen] == [23547, 23547]
    assert [event.player_id for event in seen] == [20001835, 20002270]
    assert [event.data["type"] for event in seen] == ["FieldGoalMade", "Assist"]


# ── Async client tests (using respx if available, otherwise skip) ──

try:
    import respx
    import httpx

    @pytest.mark.asyncio
    async def test_login_matches_browser_request_shape():
        config = RealSportsConfig(
            base_url="https://mock.api.com",
            email="andes@example.com",
            password="secret",
        )
        client = RealSportsClient(config)

        with patch("domain.oracle.real_sports_client._generate_request_token", return_value="tok-123"), patch(
            "domain.oracle.real_sports_client.uuid.uuid4",
            return_value="generated-uuid",
        ), respx.mock:
            route = respx.post("https://mock.api.com/login").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "user": {"id": "k3LkNN1v"},
                        "token": "fresh-token",
                        "deviceId": "39bQYPRE",
                    },
                )
            )
            ok = await client.login()

        assert ok is True
        request = route.calls.last.request
        assert request.headers["real-request-token"] == "tok-123"
        assert request.headers["real-device-type"] == "desktop_web"
        assert request.headers["real-device-uuid"] == "generated-uuid"
        assert request.headers["referer"] == "https://www.realapp.com/"
        assert json.loads(request.read().decode()) == {
            "login": "andes@example.com",
            "password": "secret",
            "tfaAuthCode": "",
        }
        assert config.user_id == "k3LkNN1v"
        assert config.token == "fresh-token"
        assert config.device_id == "39bQYPRE"
        assert config.device_uuid == "generated-uuid"

    @pytest.mark.asyncio
    async def test_get_game_markets():
        config = RealSportsConfig(base_url="https://mock.api.com")
        client = RealSportsClient(config)

        mock_response = [
            {
                "gameId": 23454,
                "homeTeam": {"name": "Lakers"},
                "awayTeam": {"name": "Rockets"},
                "homeTeamWinPct": 68,
                "volume": 5000000,
            },
        ]

        with respx.mock:
            respx.get("https://mock.api.com/predictions/gamemarkets/nba").mock(
                return_value=httpx.Response(200, json=mock_response)
            )
            markets = await client.get_game_markets("nba")
            assert len(markets) == 1
            assert markets[0]["gameId"] == 23454

        await client.close()

    @pytest.mark.asyncio
    async def test_get_player_profile():
        config = RealSportsConfig(base_url="https://mock.api.com")
        client = RealSportsClient(config)

        mock_profile = {
            "playerId": 2544,
            "name": "LeBron James",
            "team": "LAL",
            "splits": {"points": {"last5": [28, 32, 22, 35, 30]}},
        }

        with respx.mock:
            respx.get("https://mock.api.com/players/2544/sport/nba").mock(
                return_value=httpx.Response(200, json=mock_profile)
            )
            profile = await client.get_player_profile(2544)
            assert profile["name"] == "LeBron James"

        await client.close()

    @pytest.mark.asyncio
    async def test_get_stat_trackers():
        config = RealSportsConfig(base_url="https://mock.api.com")
        client = RealSportsClient(config)

        mock_trackers = [
            {"playerId": 2544, "stat": "points", "line": 27.5},
        ]

        with respx.mock:
            respx.get("https://mock.api.com/stattrackers").mock(
                return_value=httpx.Response(200, json=mock_trackers)
            )
            trackers = await client.get_stat_trackers("2026-03-18")
            assert len(trackers) == 1

        await client.close()

    @pytest.mark.asyncio
    async def test_client_error_handling():
        config = RealSportsConfig(base_url="https://mock.api.com")
        client = RealSportsClient(config)

        with respx.mock:
            respx.get("https://mock.api.com/predictions/gamemarkets/nba").mock(
                return_value=httpx.Response(500, json={"error": "server error"})
            )
            # HTTP errors are now caught gracefully — returns empty list
            result = await client.get_game_markets("nba")
            assert result == []

        await client.close()

except ImportError:
    # respx not installed — skip async tests
    pass
