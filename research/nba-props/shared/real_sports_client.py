"""Standalone Real Sports API client for research data collection.

Simplified from kalshi-trading's oracle client. REST-only (no WebSocket),
focused on player data and box score collection.

Auth (reverse-engineered from Real Sports JS bundle):
  - real-auth-info: {userId}!{deviceId}!{token}
  - real-request-token: Hashids('realwebapp', 16).encode(Date.now())
  - real-device-type: desktop_web
  - real-device-name: <browser UA>
  - real-version: 28

Base URL: https://web.realapp.com
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx

_log = logging.getLogger("shared.real_sports")

# Retry and rate-limit defaults
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0  # seconds
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_CONCURRENT_REQUESTS = 15

# Hashids configuration (from Real Sports JS bundle)
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


@dataclass
class RealSportsConfig:
    """Configuration for Real Sports API client."""

    base_url: str = "https://web.realapp.com"
    user_id: str = ""
    token: str = ""
    device_id: str = ""
    device_uuid: str = ""
    email: str = ""
    password: str = ""
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> RealSportsConfig:
        """Load config from environment variables.

        Two auth modes:
        1. Auto-login (recommended): Set REAL_EMAIL + REAL_PASSWORD.
        2. Manual: Set REAL_USER_ID + REAL_TOKEN + REAL_DEVICE_ID.
        """
        return cls(
            base_url=os.environ.get("REAL_BASE_URL", "https://web.realapp.com"),
            user_id=os.environ.get("REAL_USER_ID", ""),
            token=os.environ.get("REAL_TOKEN", ""),
            device_id=os.environ.get("REAL_DEVICE_ID", ""),
            device_uuid=os.environ.get("REAL_DEVICE_UUID", ""),
            email=os.environ.get("REAL_EMAIL", ""),
            password=os.environ.get("REAL_PASSWORD", ""),
        )

    @property
    def can_auto_login(self) -> bool:
        return bool(self.email and self.password)

    def validate(self) -> list[str]:
        """Check for missing required credentials."""
        if self.can_auto_login:
            return []
        missing = []
        if not self.user_id:
            missing.append("REAL_USER_ID")
        if not self.token:
            missing.append("REAL_TOKEN")
        if not self.device_id:
            missing.append("REAL_DEVICE_ID")
        return missing


class RealSportsClient:
    """Async HTTP client for Real Sports REST API.

    Handles auth headers, per-request token generation, retry with backoff,
    and concurrency limiting. Supports auto-login on 401.
    """

    def __init__(self, config: RealSportsConfig):
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)
        self._login_attempted = False

    async def login(self) -> bool:
        """Authenticate via POST /login. Returns True on success."""
        cfg = self._config
        if not cfg.can_auto_login:
            _log.warning("Cannot auto-login: REAL_EMAIL or REAL_PASSWORD not set")
            return False

        _log.info("Logging in to Real Sports as %s", cfg.email)
        try:
            async with httpx.AsyncClient(
                base_url=cfg.base_url, timeout=cfg.timeout_seconds,
            ) as login_client:
                resp = await login_client.post(
                    "/login",
                    json={"email": cfg.email, "password": cfg.password},
                    headers={
                        "content-type": "application/json",
                        "real-device-type": "desktop_web",
                        "real-device-name": _DEVICE_NAME,
                        "real-version": _REAL_VERSION,
                        "real-request-token": _generate_request_token(),
                        "origin": "https://www.realapp.com",
                        "referer": "https://www.realapp.com/",
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            auth_info = data.get("authInfo", data)
            new_user_id = auth_info.get("userId", "")
            new_token = auth_info.get("token", "")
            new_device_id = auth_info.get("deviceId", "")

            if not new_user_id or not new_token:
                _log.error("Login response missing userId/token: %s", list(auth_info.keys()))
                return False

            cfg.user_id = new_user_id
            cfg.token = new_token
            if new_device_id:
                cfg.device_id = new_device_id

            # Force client recreation with new headers
            if self._client and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

            _log.info("Login successful: userId=%s", cfg.user_id)
            return True

        except httpx.HTTPStatusError as exc:
            _log.error("Login failed: HTTP %d", exc.response.status_code)
            return False
        except Exception as exc:
            _log.error("Login failed: %s", exc)
            return False

    def _base_headers(self) -> dict[str, str]:
        """Static headers for the httpx client."""
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "real-device-name": _DEVICE_NAME,
            "real-device-type": "desktop_web",
            "real-version": _REAL_VERSION,
            "origin": "https://www.realapp.com",
            "referer": "https://www.realapp.com/",
        }
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

    async def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        """Authenticated GET with retry, rate limiting, and per-request token."""
        client = await self._ensure_client()
        last_exc = None

        for attempt in range(_MAX_RETRIES):
            async with self._semaphore:
                try:
                    per_request_headers = {
                        "real-request-token": _generate_request_token(),
                    }
                    resp = await client.get(path, params=params, headers=per_request_headers)
                    resp.raise_for_status()
                    self._login_attempted = False
                    try:
                        return resp.json()
                    except (ValueError, UnicodeDecodeError):
                        _log.warning("Non-JSON response: %s", path)
                        return {}
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in (401, 403):
                        if not self._login_attempted and self._config.can_auto_login:
                            self._login_attempted = True
                            _log.info("Got HTTP %d, attempting auto-login...", status)
                            if await self.login():
                                client = await self._ensure_client()
                                continue
                        _log.error("Auth failure (HTTP %d): %s", status, path)
                        return {}
                    if status in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_BACKOFF_BASE * (2 ** attempt)
                        _log.warning(
                            "HTTP %d on %s, retrying in %.1fs (%d/%d)",
                            status, path, delay, attempt + 1, _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        last_exc = exc
                        continue
                    _log.warning("HTTP %d: %s", status, path)
                    return {}
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    if attempt < _MAX_RETRIES - 1:
                        delay = _RETRY_BACKOFF_BASE * (2 ** attempt)
                        _log.warning(
                            "%s on %s, retrying in %.1fs (%d/%d)",
                            type(exc).__name__, path, delay, attempt + 1, _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        last_exc = exc
                        continue
                    _log.warning("%s: %s (exhausted retries)", type(exc).__name__, path)
                    return {}

        _log.warning("Request failed after %d retries: %s -- %s", _MAX_RETRIES, path, last_exc)
        return {}

    # -- Player Data Endpoints --

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

    async def get_player_box_score(self, boxscore_id: int) -> dict:
        """Get detailed player box score for a specific game."""
        return await self._get(
            f"/playerboxscores/{boxscore_id}",
            params={"version": "2"},
        )

    async def search_players(
        self, sport: str = "nba", season: str = "2025",
    ) -> list:
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

    async def get_player_profile(
        self, player_id: int, sport: str = "nba", season: str = "2025",
    ) -> dict:
        """Get full player profile with stats, splits, rankings."""
        return await self._get(
            f"/players/{player_id}/sport/{sport}",
            params={"season": season},
        )
