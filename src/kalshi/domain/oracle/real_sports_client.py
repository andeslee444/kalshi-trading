"""Async Real Sports API client (REST + WebSocket).

Connects to Real Sports App to fetch game prices, player data, and live events.
Uses httpx for async REST and python-socketio for WebSocket event streaming.

Auth (reverse-engineered from Real Sports JS bundle):
  - real-auth-info: {userId}!{deviceId}!{token}  (combined auth string)
  - real-request-token: Hashids('realwebapp', 16).encode(Date.now())  (per-request, 2-5min TTL)
  - real-device-type: desktop_web
  - real-device-uuid: {uuid}
  - real-version: 28

Base URL: https://web.realapp.com
WebSocket: https://web.realsports.io (Socket.io with query params)

IMPORTANT: This API is reverse-engineered. Endpoints may change without notice.
All Real Sports interaction is isolated in this file for easy adaptation.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import asyncio

import httpx

_log = logging.getLogger("oracle.real_sports")

# Retry and rate-limit defaults for reverse-engineered API
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_CONCURRENT_REQUESTS = 3

# Hashids configuration (reverse-engineered from Real Sports JS bundle)
# Only ONE Hashids instance needed: for per-request tokens.
# The userId is already a string from localStorage (e.g., "k3LkNN1v") — NOT encoded.
_REQUEST_TOKEN_SALT = "realwebapp"
_REQUEST_TOKEN_MIN_LENGTH = 16

_REAL_VERSION = "28"
_DEVICE_NAME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Cache Hashids instance (created on first use)
_request_token_hashids = None


def _get_request_token_hashids():
    """Lazy-init the request token Hashids instance."""
    global _request_token_hashids
    if _request_token_hashids is None:
        try:
            from hashids import Hashids
        except ImportError:
            raise ImportError(
                "hashids package is required for Real Sports auth. "
                "Install with: pip install hashids>=1.3"
            )
        _request_token_hashids = Hashids(
            salt=_REQUEST_TOKEN_SALT,
            min_length=_REQUEST_TOKEN_MIN_LENGTH,
        )
    return _request_token_hashids


def _generate_request_token() -> str:
    """Generate a per-request token: Hashids('realwebapp', 16).encode(Date.now()).

    Server validates the encoded timestamp is recent (2-5 min TTL).
    Must be generated fresh for each request.
    """
    h = _get_request_token_hashids()
    return h.encode(int(time.time() * 1000))


def _ensure_real_device_uuid(config: "RealSportsConfig") -> str:
    """Ensure a stable device UUID exists for websocket and login flows."""
    if not config.device_uuid:
        config.device_uuid = str(uuid.uuid4())
    return config.device_uuid


@dataclass
class RealSportsConfig:
    """Configuration for Real Sports API client."""
    base_url: str = "https://web.realapp.com"
    ws_url: str = "https://web.realsports.io"
    user_id: str = ""   # Already-encoded string from localStorage (e.g., "k3LkNN1v")
    token: str = ""
    device_id: str = ""
    device_uuid: str = ""
    # Auto-login credentials (sustainable — bot re-authenticates on token expiry)
    email: str = ""
    password: str = ""
    polling_interval_seconds: int = 5
    min_volume: int = 100000
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> RealSportsConfig:
        """Load config from environment variables.

        Two auth modes:
        1. Auto-login (recommended): Set REAL_EMAIL + REAL_PASSWORD.
           Bot calls POST /login on startup and on 401 to get fresh tokens.
        2. Manual: Set REAL_USER_ID + REAL_TOKEN + REAL_DEVICE_ID + REAL_DEVICE_UUID
           from browser localStorage. Token will expire.
        """
        return cls(
            base_url=os.environ.get("REAL_BASE_URL", "https://web.realapp.com"),
            ws_url=os.environ.get("REAL_WS_URL", "https://web.realsports.io"),
            user_id=os.environ.get("REAL_USER_ID", ""),
            token=os.environ.get("REAL_TOKEN", ""),
            device_id=os.environ.get("REAL_DEVICE_ID", ""),
            device_uuid=os.environ.get("REAL_DEVICE_UUID", ""),
            email=os.environ.get("REAL_EMAIL", ""),
            password=os.environ.get("REAL_PASSWORD", ""),
        )

    @property
    def can_auto_login(self) -> bool:
        """True if email+password are set for automatic re-authentication."""
        return bool(self.email and self.password)

    def validate(self) -> list[str]:
        """Check for missing required credentials.

        If auto-login is configured (email+password), token/userId/deviceId
        can be empty — they'll be populated on first login.
        """
        if self.can_auto_login:
            return []  # auto-login will populate everything
        missing = []
        if not self.user_id:
            missing.append("REAL_USER_ID")
        if not self.token:
            missing.append("REAL_TOKEN")
        if not self.device_id:
            missing.append("REAL_DEVICE_ID")
        return missing

    @classmethod
    def from_bots_config(cls, oracle_cfg: dict) -> RealSportsConfig:
        """Load from the oracle section of bots-config.json."""
        rs = oracle_cfg.get("realSports", {})
        cfg = cls.from_env()
        cfg.base_url = rs.get("baseUrl", cfg.base_url)
        cfg.ws_url = rs.get("wsUrl", cfg.ws_url)
        cfg.polling_interval_seconds = rs.get("pollingIntervalSeconds", 5)
        cfg.min_volume = rs.get("minVolume", 100000)
        return cfg


class RealSportsClient:
    """Async HTTP client for Real Sports REST API.

    Auth lifecycle:
    1. If email+password configured → calls POST /login on first request or 401
    2. Login response populates userId, token, deviceId
    3. All subsequent requests use these credentials
    4. On 401 → auto-re-login once, then retry the failed request
    """

    def __init__(self, config: RealSportsConfig):
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
        self._login_attempted = False  # prevent infinite login loops

    async def login(self) -> bool:
        """Authenticate via POST /login. Returns True on success.

        Populates config.user_id, config.token, config.device_id from response.
        Called automatically on startup (if email set) and on 401.
        """
        cfg = self._config
        if not cfg.can_auto_login:
            _log.warning("Cannot auto-login: REAL_EMAIL or REAL_PASSWORD not set")
            return False

        _log.info("Logging in to Real Sports as %s", cfg.email)
        try:
            # Use a fresh client for login (no auth headers needed)
            async with httpx.AsyncClient(
                base_url=cfg.base_url, timeout=cfg.timeout_seconds,
            ) as login_client:
                resp = await login_client.post(
                    "/login",
                    json={"login": cfg.email, "password": cfg.password, "tfaAuthCode": ""},
                    headers={
                        "content-type": "application/json",
                        "real-device-type": "desktop_web",
                        "real-device-name": _DEVICE_NAME,
                        "real-device-uuid": _ensure_real_device_uuid(cfg),
                        "real-version": _REAL_VERSION,
                        "real-request-token": _generate_request_token(),
                        "referer": "https://www.realapp.com/",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            # Real has returned at least two shapes in practice:
            # 1. {"authInfo": {"userId", "token", "deviceId"}}
            # 2. {"user": {"id"}, "token", "deviceId", ...}
            auth_info = data.get("authInfo", data)
            nested_user = data.get("user", {}) if isinstance(data.get("user"), dict) else {}
            new_user_id = auth_info.get("userId") or nested_user.get("id") or data.get("userId", "")
            new_token = auth_info.get("token") or data.get("token", "")
            new_device_id = auth_info.get("deviceId") or data.get("deviceId", "")

            if not new_user_id or not new_token:
                _log.error("Login response missing userId/token: %s", list(data.keys()))
                return False

            # Update config with fresh credentials
            cfg.user_id = new_user_id
            cfg.token = new_token
            if new_device_id:
                cfg.device_id = new_device_id

            # Force httpx client recreation with new headers
            if self._client and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

            _log.info("Login successful: userId=%s", cfg.user_id)
            return True

        except httpx.HTTPStatusError as exc:
            _log.error("Login failed: HTTP %d — check REAL_EMAIL/REAL_PASSWORD",
                       exc.response.status_code)
            return False
        except Exception as exc:
            _log.error("Login failed: %s", exc)
            return False

    def _base_headers(self) -> dict[str, str]:
        """Static headers set once on the httpx client (no per-request token).

        Auth format (from TRADING_BOT_ARCHITECTURE.md):
          real-auth-info: {userId}!{deviceId}!{token}
        where userId is the string from localStorage (already encoded).
        """
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "real-device-name": _DEVICE_NAME,
            "real-device-type": "desktop_web",
            "real-version": _REAL_VERSION,
            "origin": "https://www.realapp.com",
            "referer": "https://www.realapp.com/",
        }
        # real-auth-info: {userId}!{deviceId}!{token}
        cfg = self._config
        if cfg.user_id and cfg.device_id and cfg.token:
            headers["real-auth-info"] = f"{cfg.user_id}!{cfg.device_id}!{cfg.token}"
        if cfg.device_uuid:
            headers["real-device-uuid"] = cfg.device_uuid
        return headers

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                headers=self._base_headers(),
                timeout=self._config.timeout_seconds,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """Make an authenticated GET request with retry and rate limiting.

        Generates a fresh real-request-token for each attempt (2-5 min TTL).
        """
        client = await self._ensure_client()
        last_exc = None

        for attempt in range(_MAX_RETRIES):
            async with self._semaphore:
                try:
                    # Fresh per-request token (server validates timestamp recency)
                    per_request_headers = {
                        "real-request-token": _generate_request_token(),
                    }
                    resp = await client.get(path, params=params, headers=per_request_headers)
                    resp.raise_for_status()
                    self._login_attempted = False  # reset for future 401s
                    try:
                        return resp.json()
                    except (ValueError, UnicodeDecodeError):
                        _log.warning("Real Sports non-JSON response: %s", path)
                        return {}
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in (401, 403):
                        # Auto-login on 401 if credentials available (one attempt)
                        if not self._login_attempted and self._config.can_auto_login:
                            self._login_attempted = True
                            _log.info("Got HTTP %d, attempting auto-login...", status)
                            if await self.login():
                                client = await self._ensure_client()
                                continue  # retry the request with fresh token
                        _log.error(
                            "Real Sports auth failure (HTTP %d): %s — check credentials",
                            status, path,
                        )
                        return {}
                    if status in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_BACKOFF_BASE * (2 ** attempt)
                        _log.warning(
                            "Real Sports HTTP %d on %s, retrying in %.1fs (%d/%d)",
                            status, path, delay, attempt + 1, _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        last_exc = exc
                        continue
                    _log.warning("Real Sports HTTP %d: %s", status, path)
                    return {}
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_BACKOFF_BASE * (2 ** attempt)
                        _log.warning(
                            "Real Sports %s on %s, retrying in %.1fs (%d/%d)",
                            type(exc).__name__, path, delay, attempt + 1, _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        last_exc = exc
                        continue
                    _log.warning("Real Sports %s: %s (exhausted retries)", type(exc).__name__, path)
                    return {}

        _log.warning("Real Sports request failed after %d retries: %s — %s", _MAX_RETRIES, path, last_exc)
        return {}

    # ── Game & Market Endpoints ──

    async def get_game_markets(self, sport: str = "nba") -> list[dict]:
        """Get all active prediction markets for a sport.

        Returns list of market dicts with game info, prices, volume.
        The API returns {"gameMarkets": [...]}, each with outcomes, probabilities,
        probabilityHistory, volumeDisplay, isLocked, isSettled.
        """
        data = await self._get(f"/predictions/gamemarkets/{sport}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("gameMarkets", data.get("markets", []))
        return []

    async def get_game_detail(self, game_id: int, sport: str = "nba") -> dict:
        """Get detailed game data including play-by-play."""
        return await self._get(
            f"/games/{game_id}/sport/{sport}/feed",
            params={"version": "2", "view": "recent", "viewFrame": "default"},
        )

    async def get_market_detail(self, market_id: int) -> dict:
        """Get detailed market data including price history."""
        return await self._get(f"/predictions/marketembed/{market_id}")

    async def get_home_feed(self, sport: str = "nba") -> dict:
        """Get the home feed with today's games, scores, picks."""
        return await self._get(f"/home/{sport}/next", params={"cohort": "0"})

    async def get_live_feed(self) -> dict:
        """Get global live feed across all sports."""
        return await self._get("/livefeed/all/feed")

    # ── Player Data Endpoints ──

    async def get_player_profile(
        self, player_id: int, sport: str = "nba", season: str = "2025",
    ) -> dict:
        """Get full player profile with stats, splits, rankings."""
        return await self._get(
            f"/players/{player_id}/sport/{sport}",
            params={"season": season},
        )

    async def get_player_season_feed(
        self, player_id: int, sport: str = "nba",
        season: str = "2025", limit: int = 20,
    ) -> dict:
        """Get game-by-game performance feed for a player."""
        return await self._get(
            f"/players/{player_id}/sport/{sport}/seasonfeed",
            params={
                "limit": str(limit),
                "season": season,
                "view": "recent",
                "viewFrame": "default",
            },
        )

    async def get_stat_trackers(
        self, day: str, sport: str = "nba",
    ) -> list[dict]:
        """Get player prop stat trackers for a specific day.

        day: YYYY-MM-DD format.
        Returns player over/under lines.
        """
        data = await self._get(
            "/stattrackers",
            params={"day": day, "sport": sport},
        )
        if isinstance(data, list):
            return data
        return data.get("trackers", []) if isinstance(data, dict) else []

    async def search_players(
        self, sport: str = "nba", season: str = "2025",
    ) -> list[dict]:
        """Search all players for a sport/season."""
        data = await self._get(
            f"/players/sport/{sport}/search",
            params={
                "includeNoOneOption": "false",
                "searchType": "cardsTabUpsell",
                "season": season,
            },
        )
        if isinstance(data, list):
            return data
        return data.get("players", []) if isinstance(data, dict) else []

    # ── Box Score & Standings Endpoints (spec Section 11) ──

    async def get_player_box_score(self, boxscore_id: int) -> dict:
        """Get detailed player box score for a specific game."""
        return await self._get(
            f"/playerboxscores/{boxscore_id}",
            params={"version": "2"},
        )

    async def get_team_standings(
        self, sport: str = "nba", conference: str = "Western",
        division: str = "", season: str = "2025",
    ) -> dict:
        """Get division/conference standings."""
        path = f"/teamstandings/sport/{sport}/conference/{conference}"
        if division:
            path += f"/division/{division}"
        return await self._get(path, params={"season": season})

    async def get_team_rankings(
        self, sport: str = "nba", period: str = "tertiary",
    ) -> dict:
        """Get team rankings (tertiary=7-day, secondary=30-day, primary=season)."""
        return await self._get(
            f"/rankings/sport/{sport}/entity/team/ranking/{period}",
        )

    async def get_team_stat_leaders(
        self, sport: str = "nba", season: str = "2025",
        stat_id: int = 1,
    ) -> dict:
        """Get team stat leaders by category."""
        return await self._get(
            f"/teamstatleaders/{sport}/seasons/{season}/seasontypes/regularseason/stats/{stat_id}",
        )

    async def get_stat_leader_seasons(self, sport: str = "nba") -> dict:
        """Get available seasons and stat categories (for statId discovery)."""
        return await self._get(f"/teamstatleaders/{sport}/seasons")

    # ── Game Schedule ──

    async def get_game_schedule(self, sport: str = "nba") -> dict:
        """Get game schedule/calendar."""
        return await self._get(
            f"/home/{sport}/days",
            params={"type": "condensed"},
        )


@dataclass
class LiveEvent:
    """Parsed live event from WebSocket."""
    event_type: str
    game_id: Optional[int] = None
    player_id: Optional[int] = None
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class RealSportsWebSocket:
    """Real Sports live-feed websocket client.

    Real's browser client uses an Engine.IO v3 websocket on `/socket.io/`
    with auth and device fields carried in the query string. The generic
    python-socketio client was connecting at the transport layer but not
    receiving room data, so this client mirrors the browser protocol directly.
    """

    # Heartbeat watchdog: if no data received for this many seconds,
    # mark the connection as STALE (spec Section 12)
    STALE_THRESHOLD_SECONDS = 30.0
    MAX_RECONNECT_FAILURES = 3
    SOCKET_TYPE = "LiveFeed"
    SOCKET_SPORT = "all"
    DEVICE_VERSION = "undefined"

    def __init__(self, config: RealSportsConfig):
        self._config = config
        self._session = None
        self._ws = None
        self._runner_task: Optional[asyncio.Task] = None
        self._startup_future: Optional[asyncio.Future] = None
        self._stop_requested = False
        self._connected = False
        self._reconnecting = False
        self._reconnect_count = 0
        self._reconnect_failures = 0
        self._last_data_time: float = 0.0
        self._stale = False
        self._ping_interval_seconds = 25.0
        self._ping_timeout_seconds = 5.0
        self._handlers: dict[str, list[Callable]] = {}

    def on(self, event: str, handler: Callable) -> None:
        """Register a handler for a live event."""
        self._handlers.setdefault(event, []).append(handler)

    async def connect(self) -> None:
        """Connect to the Real Sports live websocket."""
        try:
            import aiohttp
        except ImportError:
            _log.error("aiohttp not installed")
            return

        if self._runner_task and not self._runner_task.done():
            return

        self._stop_requested = False
        self._startup_future = asyncio.get_running_loop().create_future()
        self._runner_task = asyncio.create_task(self._run_forever(aiohttp), name="real-live-ws")
        await self._startup_future

    def _ensure_device_uuid(self) -> str:
        generated = not bool(self._config.device_uuid)
        device_uuid = _ensure_real_device_uuid(self._config)
        if generated:
            _log.info("Generated Real Sports device UUID for websocket session")
        return device_uuid

    def _auth_query_value(self) -> str:
        cfg = self._config
        return f"{cfg.user_id}!{cfg.device_id}!{cfg.token}"

    def _build_ws_url(self) -> str:
        base_url = self._config.ws_url.rstrip("/")
        if base_url.startswith("https://"):
            base_url = "wss://" + base_url[len("https://"):]
        elif base_url.startswith("http://"):
            base_url = "ws://" + base_url[len("http://"):]
        params = {
            "socketType": self.SOCKET_TYPE,
            "realRequestToken": _generate_request_token(),
            "realVersion": _REAL_VERSION,
            "sport": self.SOCKET_SPORT,
            "deviceUuid": self._ensure_device_uuid(),
            "deviceVersion": self.DEVICE_VERSION,
            "deviceType": "desktop_web",
            "auth": self._auth_query_value(),
            "EIO": "3",
            "transport": "websocket",
        }
        return f"{base_url}/socket.io/?{urlencode(params)}"

    def _build_ws_headers(self) -> dict[str, str]:
        return {
            "origin": "https://www.realapp.com",
            "referer": "https://www.realapp.com/",
            "user-agent": _DEVICE_NAME,
        }

    def _extract_event_ids(self, payload: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
        game_id = payload.get("gameId") or payload.get("game_id")
        player_id = (
            payload.get("playerId")
            or payload.get("player_id")
            or payload.get("primaryPlayerId")
            or payload.get("primary_player_id")
        )
        return game_id, player_id

    def _iter_payloads(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            for key in ("plays", "players", "games", "markets"):
                items = payload.get(key)
                if isinstance(items, list) and items:
                    return [item for item in items if isinstance(item, dict)]
            return [payload]
        return [{"raw": payload}]

    async def _dispatch_event(self, event_name: str, payload: Any) -> None:
        handlers = self._handlers.get(event_name, [])
        if not handlers:
            return
        self._last_data_time = time.time()
        self._stale = False
        for item in self._iter_payloads(payload):
            game_id, player_id = self._extract_event_ids(item)
            event = LiveEvent(
                event_type=event_name,
                game_id=game_id,
                player_id=player_id,
                data=item,
            )
            for handler in handlers:
                try:
                    result = handler(event)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    _log.exception("Error in handler for %s", event_name)

    def _mark_connected(self, *, reconnected: bool) -> None:
        self._connected = True
        self._reconnecting = False
        self._stale = False
        self._reconnect_failures = 0
        self._last_data_time = time.time()
        if reconnected:
            self._reconnect_count += 1
            _log.info(
                "Reconnected to Real Sports WebSocket (total reconnects: %d)",
                self._reconnect_count,
            )
        else:
            _log.info("Connected to Real Sports WebSocket")

    async def _handle_engine_packet(self, packet: str) -> None:
        if not packet:
            return
        if packet == "2":
            if self._ws is not None:
                await self._ws.send_str("3")
            return
        if packet.startswith("0"):
            try:
                meta = json.loads(packet[1:])
            except json.JSONDecodeError:
                return
            try:
                self._ping_interval_seconds = float(meta.get("pingInterval", 25000)) / 1000.0
            except (TypeError, ValueError):
                self._ping_interval_seconds = 25.0
            try:
                self._ping_timeout_seconds = float(meta.get("pingTimeout", 5000)) / 1000.0
            except (TypeError, ValueError):
                self._ping_timeout_seconds = 5.0
            return
        if packet.startswith("40"):
            if not self._connected:
                self._mark_connected(reconnected=self._reconnecting)
                if self._startup_future is not None and not self._startup_future.done():
                    self._startup_future.set_result(None)
            return
        if packet.startswith("42"):
            try:
                message = json.loads(packet[2:])
            except json.JSONDecodeError:
                return
            if not isinstance(message, list) or not message:
                return
            event_name = message[0]
            payload = message[1] if len(message) > 1 else {}
            if isinstance(event_name, str):
                await self._dispatch_event(event_name, payload)

    async def _close_transport(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None

    async def _run_forever(self, aiohttp_module) -> None:
        initial_attempt = True
        while not self._stop_requested:
            try:
                timeout = aiohttp_module.ClientTimeout(total=None, sock_connect=20, sock_read=None)
                self._session = aiohttp_module.ClientSession(timeout=timeout)
                self._ws = await self._session.ws_connect(
                    self._build_ws_url(),
                    headers=self._build_ws_headers(),
                    autoping=False,
                    heartbeat=None,
                    max_msg_size=0,
                )
                async for message in self._ws:
                    if message.type == aiohttp_module.WSMsgType.TEXT:
                        await self._handle_engine_packet(message.data)
                    elif message.type == aiohttp_module.WSMsgType.CLOSE:
                        break
                    elif message.type == aiohttp_module.WSMsgType.ERROR:
                        raise RuntimeError(f"Real websocket error: {self._ws.exception()}")
                if initial_attempt and self._startup_future is not None and not self._startup_future.done():
                    raise RuntimeError("Real websocket closed before connect ack")
            except Exception as exc:
                if initial_attempt and self._startup_future is not None and not self._startup_future.done():
                    self._startup_future.set_exception(exc)
                    await self._close_transport()
                    self._runner_task = None
                    return
                self._reconnect_failures += 1
                self._reconnecting = True
                if self._reconnect_failures >= self.MAX_RECONNECT_FAILURES:
                    _log.error(
                        "Real Sports WebSocket reconnection failed %d times — Book C disabled, REST-only mode",
                        self._reconnect_failures,
                    )
                else:
                    _log.warning(
                        "Real Sports WebSocket reconnection error (%d/%d): %s",
                        self._reconnect_failures,
                        self.MAX_RECONNECT_FAILURES,
                        exc,
                    )
            finally:
                was_connected = self._connected
                self._connected = False
                if was_connected and not self._stop_requested:
                    self._reconnecting = True
                    _log.info("Disconnected from Real Sports WebSocket")
                await self._close_transport()

            initial_attempt = False
            if self._stop_requested:
                break
            await asyncio.sleep(min(30, max(1, self._reconnect_failures or 1)))

        if self._startup_future is not None and not self._startup_future.done():
            self._startup_future.set_result(None)
        self._runner_task = None

    async def disconnect(self) -> None:
        self._stop_requested = True
        await self._close_transport()
        if self._runner_task is not None and not self._runner_task.done():
            await asyncio.gather(self._runner_task, return_exceptions=True)
        self._connected = False
        self._reconnecting = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def is_stale(self) -> bool:
        """True if no data received for STALE_THRESHOLD_SECONDS.

        Spec Section 12: "No data 30+ seconds → mark STALE.
        Fall back to REST 5s. No new Book C trades."
        """
        if not self._connected or self._last_data_time == 0:
            return True
        age = time.time() - self._last_data_time
        if age > self.STALE_THRESHOLD_SECONDS:
            if not self._stale:
                self._stale = True
                _log.warning(
                    "WebSocket STALE: no data for %.0fs (threshold: %.0fs)",
                    age, self.STALE_THRESHOLD_SECONDS,
                )
            return True
        return False

    @property
    def is_disabled(self) -> bool:
        """True if reconnection has failed too many times.

        Spec Section 12: "Reconnect fails 3x → REST-only mode. Book C disabled."
        """
        return self._reconnect_failures >= self.MAX_RECONNECT_FAILURES

    @property
    def data_age_seconds(self) -> float:
        """Seconds since last data event (0 if never received)."""
        if self._last_data_time == 0:
            return 0.0
        return time.time() - self._last_data_time

    async def wait(self) -> None:
        """Block until disconnected (for daemon mode)."""
        if self._runner_task is not None:
            await asyncio.gather(self._runner_task, return_exceptions=True)


def parse_game_market_response(raw: dict) -> dict:
    """Parse a Real Sports game market response into normalized form.

    Extracts: game_id, home_team, away_team, home_pct, away_pct, volume, status.
    """
    return {
        "game_id": raw.get("gameId") or raw.get("id"),
        "home_team": raw.get("homeTeam", {}).get("name", ""),
        "away_team": raw.get("awayTeam", {}).get("name", ""),
        "home_pct": raw.get("homeTeamWinPct", 0),
        "away_pct": raw.get("awayTeamWinPct", 0),
        "volume": raw.get("volume", 0),
        "status": raw.get("status", "unknown"),
        "sport": raw.get("sport", "nba"),
        "start_time": raw.get("startTime", ""),
        "period": raw.get("period", ""),
        "clock": raw.get("clock", ""),
        "home_score": raw.get("homeScore", 0),
        "away_score": raw.get("awayScore", 0),
    }


def parse_home_game_response(raw: dict) -> dict:
    """Parse a Real home-feed game into normalized form.

    Extracts the same canonical game shape used for market mapping, but from
    `/home/{sport}/next` instead of `/predictions/gamemarkets/{sport}`.
    """
    return {
        "game_id": raw.get("id") or raw.get("gameId"),
        "home_team": raw.get("homeTeam", {}).get("name", ""),
        "away_team": raw.get("awayTeam", {}).get("name", ""),
        "home_pct": 0,
        "away_pct": 0,
        "volume": 0,
        "status": raw.get("status", "unknown"),
        "sport": raw.get("sport", "nba"),
        "start_time": raw.get("dateTime", ""),
        "period": raw.get("periodName") or raw.get("period", ""),
        "clock": "",
        "home_score": raw.get("homeTeamScore", 0),
        "away_score": raw.get("awayTeamScore", 0),
        "home_moneyline": raw.get("homeMoneyline"),
        "away_moneyline": raw.get("awayMoneyline"),
        "point_spread": raw.get("pointSpread"),
        "over_under": raw.get("overUnder"),
    }


def parse_player_splits(raw: dict) -> dict:
    """Parse Real Sports player profile response into splits data.

    Returns dict with last_5, last_10, season_avg, home_avg, away_avg per stat.
    """
    splits = raw.get("splits", {})
    stats = {}

    for stat_key in ["points", "rebounds", "assists", "threePointers", "steals", "blocks"]:
        stat_splits = splits.get(stat_key, {})
        stats[stat_key] = {
            "last_5": stat_splits.get("last5", []),
            "last_10": stat_splits.get("last10", []),
            "season_avg": stat_splits.get("seasonAvg", 0),
            "home_avg": stat_splits.get("homeAvg", 0),
            "away_avg": stat_splits.get("awayAvg", 0),
            "per_opponent": stat_splits.get("perOpponent", {}),
        }
    return stats
