"""Kalshi client and market-cache helpers extracted from kalshi_auth."""

from __future__ import annotations

import base64
import fcntl
import json
import logging
import os
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from storage import atomic_write_json as storage_atomic_write_json


PROJECT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0
MARKET_CACHE_PATH = PROJECT_DIR / "data" / "market-cache.json"
MARKET_CACHE_TTL = 60

_log = logging.getLogger("kalshi-client")

_DEMO_MODES = {"demo", "paper", "sandbox"}
_PRODUCTION_MODES = {"production", "prod", "live"}


def _safe_close_response(response):
    if response is None:
        return
    try:
        response.close()
    except Exception:
        pass


def _buffer_and_close_response(response):
    if response is None:
        return None
    try:
        _ = response.content
    except Exception:
        pass
    finally:
        _safe_close_response(response)
    return response


def _round_half_up(value):
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _dollars_to_cents(value):
    return _round_half_up(float(value) * 100)


def _normalize_mode(value):
    mode = str(value or "demo").strip().lower()
    if mode in _DEMO_MODES:
        return "demo"
    if mode in _PRODUCTION_MODES:
        return "production"
    raise ValueError(
        f"Unsupported Kalshi mode {value!r}. Use one of: demo, production."
    )


def _normalize_base_url(value):
    base = str(value).rstrip("/")
    if base.endswith("/trade-api/v2"):
        return base
    return f"{base}/trade-api/v2"


_MARKET_FIELD_MAP = [
    ("yes_bid_dollars", "yes_bid", _dollars_to_cents),
    ("yes_ask_dollars", "yes_ask", _dollars_to_cents),
    ("no_bid_dollars", "no_bid", _dollars_to_cents),
    ("no_ask_dollars", "no_ask", _dollars_to_cents),
    ("last_price_dollars", "last_price", _dollars_to_cents),
    ("volume_fp", "volume", lambda value: int(float(value))),
    ("open_interest_fp", "open_interest", lambda value: int(float(value))),
]


def normalize_market(market):
    """Convert Kalshi API v2 dollar-string fields to integer-cent fields."""
    for new_field, old_field, convert in _MARKET_FIELD_MAP:
        if market.get(old_field) is not None:
            continue
        raw = market.get(new_field)
        if raw is None:
            continue
        try:
            market[old_field] = convert(raw)
        except (ValueError, TypeError):
            market[old_field] = 0
    return market


def normalize_markets(markets):
    """Normalize a list of market dicts in-place. Returns the same list."""
    for market in markets:
        normalize_market(market)
    return markets


def write_market_cache(
    markets_by_prefix,
    *,
    cache_path=MARKET_CACHE_PATH,
    atomic_write_json_func=storage_atomic_write_json,
    time_func=time.time,
):
    """Write market data to the shared cache file."""
    atomic_write_json_func(Path(cache_path), {
        "updated_at": time_func(),
        "markets": markets_by_prefix,
    })


def read_market_cache(
    prefix=None,
    max_age=MARKET_CACHE_TTL,
    *,
    cache_path=MARKET_CACHE_PATH,
    normalize_markets_func=normalize_markets,
    time_func=time.time,
):
    """Read markets from the shared cache if fresh enough."""
    cache_path = Path(cache_path)
    try:
        if not cache_path.exists():
            return None
        data = json.loads(cache_path.read_text())
        age = time_func() - data.get("updated_at", 0)
        if age > max_age:
            return None
        markets = data.get("markets", {})
        if prefix is not None:
            result = markets.get(prefix)
            if isinstance(result, list):
                normalize_markets_func(result)
            return result
        for _, market_list in markets.items():
            if isinstance(market_list, list):
                normalize_markets_func(market_list)
        return markets
    except (json.JSONDecodeError, OSError, KeyError):
        return None


class KalshiClient:
    """Kalshi API client with RSA-PSS authentication and retry logic."""

    def __init__(
        self,
        api_key=None,
        key_path=None,
        mode=None,
        confirm_production=None,
        *,
        project_dir=PROJECT_DIR,
        default_key_path=DEFAULT_KEY_PATH,
        demo_base_url=DEMO_BASE_URL,
        prod_base_url=PROD_BASE_URL,
        max_retries=MAX_RETRIES,
        retry_backoff_base=RETRY_BACKOFF_BASE,
        logger=None,
        requests_module=requests,
        read_market_cache_func=read_market_cache,
        write_market_cache_func=write_market_cache,
        normalize_market_func=normalize_market,
        normalize_markets_func=normalize_markets,
        market_cache_path=MARKET_CACHE_PATH,
        market_cache_ttl=MARKET_CACHE_TTL,
        time_module=time,
    ):
        self.api_key = api_key or os.environ.get("KALSHI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Kalshi API key required. Set KALSHI_API_KEY env var or pass api_key="
            )

        key_file = key_path or os.environ.get("KALSHI_KEY_FILE", str(default_key_path))
        key_path_obj = Path(key_file)
        if not key_path_obj.is_absolute():
            key_path_obj = Path(project_dir) / key_file

        try:
            with open(key_path_obj, "rb") as handle:
                self.private_key = serialization.load_pem_private_key(
                    handle.read(), password=None, backend=default_backend()
                )
        except FileNotFoundError:
            raise FileNotFoundError(
                f"RSA private key not found at {key_path_obj}. "
                f"Set KALSHI_KEY_FILE env var to the correct path."
            )
        except Exception as exc:
            raise ValueError(f"Failed to load RSA private key from {key_path_obj}: {exc}")

        requested_mode = mode or os.environ.get("KALSHI_MODE", "demo")
        self.mode = _normalize_mode(requested_mode)
        self._log = logger or _log
        self.log = self._log
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.requests_module = requests_module
        self.session = requests_module.Session()
        self._read_market_cache = read_market_cache_func
        self._write_market_cache = write_market_cache_func
        self._normalize_market = normalize_market_func
        self._normalize_markets = normalize_markets_func
        self._market_cache_path = Path(market_cache_path)
        self._market_cache_ttl = market_cache_ttl
        self._time_module = time_module
        self._market_cache = {}

        base_url_override = os.environ.get("KALSHI_BASE_URL")
        if base_url_override:
            self.base_url = _normalize_base_url(base_url_override)
        elif self.mode == "production":
            production_confirmed = confirm_production
            if production_confirmed is None:
                production_confirmed = os.environ.get("KALSHI_CONFIRM_PRODUCTION") == "yes"
            if not production_confirmed:
                raise ValueError(
                    "Production mode requires KALSHI_CONFIRM_PRODUCTION=yes env var. "
                    "Set this explicitly to confirm you intend to trade with real money."
                )
            self._log.warning("PRODUCTION MODE ACTIVE — trading with real money")
            self.base_url = prod_base_url
        else:
            self.base_url = demo_base_url

    def _sign(self, method: str, path: str) -> dict:
        """Generate authentication headers for a request."""
        ts = str(int(self._time_module.time() * 1000))
        path_clean = path.split("?")[0]
        msg = f"{ts}{method}{path_clean}"
        sig = self.private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body=None, timeout=15):
        """Make an authenticated API request with retry on transient errors."""
        requests_module = getattr(self, "requests_module", requests)
        log = getattr(self, "log", getattr(self, "_log", _log))
        time_module = getattr(self, "_time_module", time)
        max_retries = getattr(self, "max_retries", MAX_RETRIES)
        retry_backoff_base = getattr(self, "retry_backoff_base", RETRY_BACKOFF_BASE)

        url = self.base_url + path
        full_path = "/trade-api/v2" + path
        headers = self._sign(method, full_path)
        is_idempotent = method in ("GET", "HEAD", "OPTIONS")

        last_err = None
        for attempt in range(max_retries):
            response = None
            try:
                response = self.session.request(method, url, headers=headers, json=body, timeout=timeout)
                if response.status_code == 429:
                    _safe_close_response(response)
                    wait = retry_backoff_base * (2 ** attempt)
                    log.warning("Rate limited (429), retrying in %.1fs...", wait)
                    time_module.sleep(wait)
                    headers = self._sign(method, full_path)
                    continue

                try:
                    response.raise_for_status()
                except requests_module.exceptions.HTTPError:
                    _safe_close_response(response)
                    raise

                if response.status_code == 204:
                    _safe_close_response(response)
                    return {}
                response = _buffer_and_close_response(response)
                if not response.content:
                    return {}
                try:
                    return response.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    log.error(
                        "Non-JSON response from %s %s (status %d): %s",
                        method,
                        path,
                        response.status_code,
                        response.text[:200],
                    )
                    raise ValueError(
                        f"Non-JSON response from {method} {path} "
                        f"(status {response.status_code}): {response.text[:100]}"
                    ) from exc

            except requests_module.exceptions.ConnectionError as exc:
                _safe_close_response(response)
                if not is_idempotent:
                    log.error("Non-retryable %s %s failed (ConnectionError): %s", method, path, exc)
                    raise
                last_err = exc
                wait = retry_backoff_base * (2 ** attempt)
                log.warning("Connection error, retrying in %.1fs... (%s)", wait, exc)
                time_module.sleep(wait)
                headers = self._sign(method, full_path)
            except requests_module.exceptions.Timeout as exc:
                _safe_close_response(response)
                if not is_idempotent:
                    log.error("Non-retryable %s %s failed (Timeout): %s", method, path, exc)
                    raise
                last_err = exc
                wait = retry_backoff_base * (2 ** attempt)
                log.warning("Timeout, retrying in %.1fs...", wait)
                time_module.sleep(wait)
                headers = self._sign(method, full_path)
            except requests_module.exceptions.HTTPError:
                raise
            except ValueError:
                raise
            except Exception:
                _safe_close_response(response)
                raise

        raise last_err or Exception("Max retries exceeded")

    def get(self, path: str, **kwargs):
        return self._request("GET", path, **kwargs)

    def post(self, path: str, body=None, **kwargs):
        return self._request("POST", path, body=body, **kwargs)

    def delete(self, path: str, **kwargs):
        return self._request("DELETE", path, **kwargs)

    def get_all_markets(self, prefix=None, status="open", max_pages=50, cache_ttl=0, use_shared_cache=True):
        """Paginate through all open markets, optionally filtering by ticker prefix."""
        log = getattr(self, "log", getattr(self, "_log", _log))
        time_module = getattr(self, "_time_module", time)
        read_market_cache_func = getattr(self, "_read_market_cache", read_market_cache)
        write_market_cache_func = getattr(self, "_write_market_cache", write_market_cache)
        normalize_markets_func = getattr(self, "_normalize_markets", normalize_markets)
        market_cache_path = getattr(self, "_market_cache_path", MARKET_CACHE_PATH)
        market_cache_ttl = getattr(self, "_market_cache_ttl", MARKET_CACHE_TTL)

        cache_key = f"{prefix or ''}:{status}"
        if cache_ttl > 0 and cache_key in self._market_cache:
            cached_time, cached_data = self._market_cache[cache_key]
            if time_module.time() - cached_time < cache_ttl:
                log.debug("Market cache hit for %s (%d markets)", cache_key, len(cached_data))
                return cached_data

        if use_shared_cache and prefix and status == "open":
            shared = read_market_cache_func(prefix=prefix, cache_path=market_cache_path)
            if shared is not None:
                log.debug("Shared market cache hit for %s (%d markets)", prefix, len(shared))
                if cache_ttl > 0:
                    self._market_cache[cache_key] = (time_module.time(), shared)
                return shared

        all_markets = []
        cursor = None
        for _ in range(max_pages):
            path = f"/markets?status={status}&limit=1000"
            if cursor:
                path += f"&cursor={cursor}"
            try:
                data = self.get(path)
            except Exception as exc:
                log.error("Market page error: %s", exc)
                break
            batch = data.get("markets", [])
            if prefix:
                for market in batch:
                    if market.get("ticker", "").startswith(prefix):
                        all_markets.append(market)
            else:
                all_markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        if cursor and batch:
            log.warning(
                "get_all_markets pagination may be truncated after %d pages (%d markets). "
                "Increase max_pages if needed.",
                max_pages,
                len(all_markets),
            )

        normalize_markets_func(all_markets)

        if cache_ttl > 0:
            self._market_cache[cache_key] = (time_module.time(), all_markets)

        if use_shared_cache and prefix and status == "open":
            try:
                lock_path = Path(market_cache_path).with_suffix(".lock")
                with open(lock_path, "w") as lock_fd:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    try:
                        existing = read_market_cache_func(
                            max_age=market_cache_ttl * 10,
                            cache_path=market_cache_path,
                        ) or {}
                        if not isinstance(existing, dict):
                            existing = {}
                        existing[prefix] = all_markets
                        write_market_cache_func(existing, cache_path=market_cache_path)
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception as exc:
                log.debug("Failed to write shared market cache: %s", exc)

        return all_markets

    def get_market(self, ticker):
        """Fetch a single market by ticker with field normalization."""
        log = getattr(self, "log", getattr(self, "_log", _log))
        normalize_market_func = getattr(self, "_normalize_market", normalize_market)
        try:
            data = self.get(f"/markets/{ticker}")
            market = data.get("market", data)
            if market:
                normalize_market_func(market)
            return market
        except Exception as exc:
            log.warning("get_market(%s) failed: %s", ticker, exc)
            return None

    def get_orderbook(self, ticker):
        """Fetch a market orderbook by ticker."""
        return self.get(f"/markets/{ticker}/orderbook")

    def get_balance(self):
        """Get portfolio balance. Returns (balance_cents, available_cents)."""
        data = self.get("/portfolio/balance")
        self._market_exposure = data.get("market_exposure", 0)
        return data.get("balance", 0), data.get("available_balance", data.get("balance", 0))


__all__ = [
    "DEFAULT_KEY_PATH",
    "DEMO_BASE_URL",
    "KalshiClient",
    "MARKET_CACHE_PATH",
    "MARKET_CACHE_TTL",
    "MAX_RETRIES",
    "PROD_BASE_URL",
    "RETRY_BACKOFF_BASE",
    "_normalize_base_url",
    "_normalize_mode",
    "normalize_market",
    "normalize_markets",
    "read_market_cache",
    "write_market_cache",
]
