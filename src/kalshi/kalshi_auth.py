#!/usr/bin/env python3
"""Shared Kalshi API authentication and utilities.

All bots should use this module instead of duplicating auth logic.

Usage:
    from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered

    client = KalshiClient()  # reads from env vars
    data = client.get("/portfolio/balance")
    client.post("/portfolio/orders", body={...})
"""

import json, time, base64, os, sys, logging, datetime, tempfile, fcntl, threading
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from dotenv import load_dotenv

from artifact_contracts import normalize_health_state, normalize_health_summary
from execution.order_monitor import OrderMonitor as ExecutionOrderMonitor
from execution.trade_manager import (
    RecentTradeTracker as ExecutionRecentTradeTracker,
    TradeManager as ExecutionTradeManager,
    trim_trade_log as execution_trim_trade_log,
    validate_trade_config as execution_validate_trade_config,
)
from ops.logging import (
    is_shutdown_requested as ops_is_shutdown_requested,
    setup_logging as ops_setup_logging,
    setup_signal_handlers as ops_setup_signal_handlers,
    setup_unbuffered as ops_setup_unbuffered,
)
from risk.circuit_breaker import (
    CircuitBreaker as RiskCircuitBreaker,
    SHARED_BREAKER_PATH,
)
from risk.kill_switch import (
    KILL_SWITCH_PATH,
    PER_BOT_HALT_PREFIX,
    check_kill_switch,
    per_bot_halt_path,
)
from storage import (
    MetricsStore,
    TradeStore,
    atomic_write_json as storage_atomic_write_json,
    load_trades as storage_load_trades,
    save_decision as storage_save_decision,
    save_trade as storage_save_trade,
)

# === Constants ===
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_DIR / ".env")
DEFAULT_KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds

BOT_SOURCE_MAP = {
    "weather": [
        "open-meteo-batch",
        "open-meteo-single",
        "open-meteo-ensemble",
        "nws-forecast",
        # NOTE: open-meteo-hrrr and open-meteo-nam are excluded — they are optional
        # feeds that may be disabled in config. Including them here would prevent
        # the halt from triggering (absent sources default to error_count=0).
    ],
    "crypto": ["coinbase", "deribit"],
    "economics": ["cleveland-fed", "gdpnow", "cme-fedwatch"],
    "entertainment": ["hdd", "boxoffice"],
    "source-monitor": ["hdd", "boxoffice", "nws"],
    "beatrelease": ["beatrelease"],
}
SCAN_SUMMARIES_PATH = PROJECT_DIR / "data" / "scan-summaries.json"
ZERO_TRADE_ALERT_STREAK = 6

# Shared market cache — cross-process file cache for market data
MARKET_CACHE_PATH = PROJECT_DIR / "data" / "market-cache.json"
MARKET_CACHE_TTL = 60  # seconds

# City timezone mapping — shared by source-monitor, position-monitor, etc.
CITY_TIMEZONES = {
    "MIA": "America/New_York",
    "LAX": "America/Los_Angeles",
    "PHIL": "America/New_York",
    "NY": "America/New_York",
    "CHI": "America/Chicago",
    "AUS": "America/Chicago",
    "DEN": "America/Denver",
    "HOU": "America/Chicago",
}


def _local_today(city_code):
    """Return today's date (ISO string) in the local timezone for a city."""
    tz = ZoneInfo(CITY_TIMEZONES.get(city_code, "America/New_York"))
    return datetime.datetime.now(tz).date().isoformat()


def _utc_now_iso():
    """Return current UTC time as ISO 8601 string with timezone offset."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def round_half_up(value):
    """Round a float using arithmetic rounding (0.5 rounds up).

    Python's built-in round() uses banker's rounding. For C-to-F conversion
    and running high comparisons, arithmetic rounding matches NWS behavior.
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# === Kalshi API v2 field normalization ===
#
# The Kalshi API v2 returns prices as dollar-amount strings (e.g., "0.86")
# in fields suffixed with _dollars/_fp, but all bot code reads integer-cent
# fields (e.g., yes_bid=86). This normalizer bridges the gap.

# Maps API v2 dollar-string fields to the legacy integer-cent field names.
# Each entry: (new_field, old_field, conversion_fn)
# Uses round_half_up for prices (arithmetic rounding: 0.5 rounds UP, matching
# exchange tick behavior) and int(float()) for volume/OI (truncation).
def _dollars_to_cents(v):
    """Convert dollar string to integer cents with arithmetic rounding."""
    return round_half_up(float(v) * 100)

_MARKET_FIELD_MAP = [
    ("yes_bid_dollars",   "yes_bid",       _dollars_to_cents),
    ("yes_ask_dollars",   "yes_ask",       _dollars_to_cents),
    ("no_bid_dollars",    "no_bid",        _dollars_to_cents),
    ("no_ask_dollars",    "no_ask",        _dollars_to_cents),
    ("last_price_dollars", "last_price",   _dollars_to_cents),
    ("volume_fp",         "volume",        lambda v: int(float(v))),
    ("open_interest_fp",  "open_interest", lambda v: int(float(v))),
]


def normalize_market(m):
    """Convert Kalshi API v2 dollar-string fields to integer-cent fields.

    Mutates the dict in-place for performance (avoids copying thousands of
    market dicts per scan). Also returns the dict for convenience.

    Idempotent: if a legacy field already has a non-None value, it is not
    overwritten. Safe to call on already-normalized data, cached data, or
    test fixtures that use the old field names directly.
    """
    for new_field, old_field, convert in _MARKET_FIELD_MAP:
        # Skip if legacy field already populated (idempotent guard)
        existing = m.get(old_field)
        if existing is not None:
            continue
        raw = m.get(new_field)
        if raw is None:
            continue
        try:
            m[old_field] = convert(raw)
        except (ValueError, TypeError):
            m[old_field] = 0
    return m


def normalize_markets(markets):
    """Normalize a list of market dicts in-place. Returns the same list."""
    for m in markets:
        normalize_market(m)
    return markets


_log = logging.getLogger("kalshi_auth")


def setup_unbuffered():
    """Enable unbuffered stdout for real-time logging."""
    ops_setup_unbuffered()


def setup_logging(name, log_file=None):
    """Configure a logger with consistent format for a bot.

    Returns a logging.Logger with stdout handler and file handler.
    Auto-derives log file path from bot name if not explicitly provided:
    ``data/logs/{name}.log`` with 5MB rotation and 3 backups.

    Call once at bot startup: ``log = setup_logging("weather")``
    """
    return ops_setup_logging(name, log_file=log_file, project_dir=PROJECT_DIR)


_shutdown_requested = False


def is_shutdown_requested():
    """Check if a graceful shutdown has been requested via SIGUSR1.

    Bots should check this after each scan cycle and break their main loop
    to allow clean exit before SIGTERM arrives.
    """
    return ops_is_shutdown_requested(_shutdown_requested)


def setup_signal_handlers():
    """Install graceful shutdown handlers for SIGTERM, SIGINT, and SIGUSR1."""
    def _mark_shutdown_requested(requested=True):
        global _shutdown_requested
        _shutdown_requested = ops_is_shutdown_requested(requested)

    ops_setup_signal_handlers(
        _mark_shutdown_requested,
        logger=_log,
    )


class KalshiClient:
    """Kalshi API client with RSA-PSS authentication and retry logic."""

    def __init__(self, api_key=None, key_path=None, mode=None):
        """Initialize client.

        Args:
            api_key: Kalshi API key. Falls back to KALSHI_API_KEY env var.
            key_path: Path to RSA private key PEM file. Falls back to
                      KALSHI_KEY_FILE env var, then default demo key path.
            mode: "demo" or "production". Falls back to KALSHI_MODE env var,
                  then defaults to "demo".
        """
        self.api_key = api_key or os.environ.get("KALSHI_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Kalshi API key required. Set KALSHI_API_KEY env var or pass api_key="
            )

        key_file = key_path or os.environ.get("KALSHI_KEY_FILE", str(DEFAULT_KEY_PATH))
        key_path_obj = Path(key_file)
        if not key_path_obj.is_absolute():
            key_path_obj = PROJECT_DIR / key_file

        try:
            with open(key_path_obj, "rb") as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        except FileNotFoundError:
            raise FileNotFoundError(
                f"RSA private key not found at {key_path_obj}. "
                f"Set KALSHI_KEY_FILE env var to the correct path."
            )
        except Exception as e:
            raise ValueError(f"Failed to load RSA private key from {key_path_obj}: {e}")

        self.mode = mode or os.environ.get("KALSHI_MODE", "demo")
        if self.mode == "production":
            if os.environ.get("KALSHI_CONFIRM_PRODUCTION") != "yes":
                raise ValueError(
                    "Production mode requires KALSHI_CONFIRM_PRODUCTION=yes env var. "
                    "Set this explicitly to confirm you intend to trade with real money."
                )
            _log.warning("PRODUCTION MODE ACTIVE — trading with real money")
            self.base_url = PROD_BASE_URL
        else:
            self.base_url = DEMO_BASE_URL

        # Persistent HTTP session for connection reuse (saves ~200-400ms per call)
        self.session = requests.Session()

        # Market cache: {cache_key: (timestamp, data)}
        self._market_cache = {}

    def _sign(self, method: str, path: str) -> dict:
        """Generate authentication headers for a request."""
        ts = str(int(time.time() * 1000))
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
        """Make an authenticated API request with retry on transient errors.

        Only idempotent methods (GET, HEAD, OPTIONS) are retried on
        ConnectionError/Timeout.  POST/DELETE are NOT retried because the
        server may have already processed the request — retrying could
        create duplicate orders.  Rate-limit 429 responses are safe to
        retry for all methods.
        """
        url = self.base_url + path
        full_path = "/trade-api/v2" + path
        headers = self._sign(method, full_path)
        is_idempotent = method in ("GET", "HEAD", "OPTIONS")

        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.request(method, url, headers=headers, json=body, timeout=timeout)

                # Don't retry client errors (4xx) except 429
                if r.status_code == 429:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    _log.warning("Rate limited (429), retrying in %.1fs...", wait)
                    time.sleep(wait)
                    headers = self._sign(method, full_path)  # re-sign with fresh timestamp
                    continue

                r.raise_for_status()
                if r.status_code == 204 or not r.content:
                    return {}
                try:
                    return r.json()
                except (ValueError, json.JSONDecodeError) as e:
                    _log.error("Non-JSON response from %s %s (status %d): %s",
                               method, path, r.status_code, r.text[:200])
                    raise ValueError(
                        f"Non-JSON response from {method} {path} "
                        f"(status {r.status_code}): {r.text[:100]}"
                    ) from e

            except requests.exceptions.ConnectionError as e:
                if not is_idempotent:
                    _log.error("Non-retryable %s %s failed (ConnectionError): %s", method, path, e)
                    raise
                last_err = e
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                _log.warning("Connection error, retrying in %.1fs... (%s)", wait, e)
                time.sleep(wait)
                headers = self._sign(method, full_path)
            except requests.exceptions.Timeout as e:
                if not is_idempotent:
                    _log.error("Non-retryable %s %s failed (Timeout): %s", method, path, e)
                    raise
                last_err = e
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                _log.warning("Timeout, retrying in %.1fs...", wait)
                time.sleep(wait)
                headers = self._sign(method, full_path)
            except requests.exceptions.HTTPError:
                raise  # Don't retry other HTTP errors
            except Exception:
                raise

        raise last_err or Exception("Max retries exceeded")

    def get(self, path: str, **kwargs):
        """Make an authenticated GET request."""
        return self._request("GET", path, **kwargs)

    def post(self, path: str, body=None, **kwargs):
        """Make an authenticated POST request."""
        return self._request("POST", path, body=body, **kwargs)

    def delete(self, path: str, **kwargs):
        """Make an authenticated DELETE request."""
        return self._request("DELETE", path, **kwargs)

    def get_all_markets(self, prefix=None, status="open", max_pages=50, cache_ttl=0, use_shared_cache=True):
        """Paginate through all open markets, optionally filtering by ticker prefix.

        Args:
            prefix: Only return markets whose ticker starts with this string.
            status: Market status filter (default "open").
            max_pages: Maximum pagination pages to fetch.
            cache_ttl: When >0, return cached results if they are younger than
                       this many seconds. Daemon bots can pass e.g. 300 (5 min)
                       to avoid refetching identical market data every scan.
            use_shared_cache: When True, check/update the cross-process file cache
                       at data/market-cache.json. This avoids redundant API calls
                       when multiple bots fetch the same prefix within 60s.
        """
        cache_key = f"{prefix or ''}:{status}"

        # 1. Check in-memory cache (existing behavior)
        if cache_ttl > 0 and cache_key in self._market_cache:
            cached_time, cached_data = self._market_cache[cache_key]
            if time.time() - cached_time < cache_ttl:
                _log.debug("Market cache hit for %s (%d markets)", cache_key, len(cached_data))
                return cached_data

        # 2. Check shared file cache (cross-process)
        if use_shared_cache and prefix and status == "open":
            shared = read_market_cache(prefix=prefix)
            if shared is not None:
                _log.debug("Shared market cache hit for %s (%d markets)", prefix, len(shared))
                if cache_ttl > 0:
                    self._market_cache[cache_key] = (time.time(), shared)
                return shared

        # 3. Fetch from API
        all_markets = []
        cursor = None
        for _ in range(max_pages):
            path = f"/markets?status={status}&limit=1000"
            if cursor:
                path += f"&cursor={cursor}"
            try:
                data = self.get(path)
            except Exception as e:
                _log.error("Market page error: %s", e)
                break
            batch = data.get("markets", [])
            if prefix:
                for m in batch:
                    if m.get("ticker", "").startswith(prefix):
                        all_markets.append(m)
            else:
                all_markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        if cursor and batch:
            _log.warning(
                "get_all_markets pagination may be truncated after %d pages (%d markets). "
                "Increase max_pages if needed.", max_pages, len(all_markets)
            )

        # 3b. Normalize API v2 dollar-string fields to integer cents
        normalize_markets(all_markets)

        # 4. Update caches
        if cache_ttl > 0:
            self._market_cache[cache_key] = (time.time(), all_markets)

        if use_shared_cache and prefix and status == "open":
            try:
                lock_path = MARKET_CACHE_PATH.with_suffix(".lock")
                with open(lock_path, "w") as lock_fd:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    try:
                        existing = read_market_cache(max_age=MARKET_CACHE_TTL * 10) or {}
                        if not isinstance(existing, dict):
                            existing = {}
                        existing[prefix] = all_markets
                        write_market_cache(existing)
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception as e:
                _log.debug("Failed to write shared market cache: %s", e)

        return all_markets

    def get_market(self, ticker):
        """Fetch a single market by ticker with field normalization.

        Returns the normalized market dict, or None if not found.
        Handles the API's ``{"market": {...}}`` wrapper automatically.
        """
        try:
            data = self.get(f"/markets/{ticker}")
            market = data.get("market", data)
            if market:
                normalize_market(market)
            return market
        except Exception as e:
            _log.warning("get_market(%s) failed: %s", ticker, e)
            return None

    def get_balance(self):
        """Get portfolio balance. Returns (balance_cents, available_cents).

        Also stores market_exposure (cost basis of open positions) on the client
        for NAV approximation: NAV ~ balance + market_exposure.
        """
        data = self.get("/portfolio/balance")
        self._market_exposure = data.get("market_exposure", 0)
        return data.get("balance", 0), data.get("available_balance", data.get("balance", 0))


# === Trade file utilities ===

def load_trades(trades_path: Path) -> list:
    """Load trades from a JSON file. Returns [] on missing/corrupt file."""
    return storage_load_trades(trades_path, logger=_log)


def atomic_write_json(path: Path, data):
    """Write JSON data to a file atomically using a temp file + os.replace()."""
    storage_atomic_write_json(path, data)

# Backward-compatible alias
_atomic_write_json = atomic_write_json


def save_trade(trades_path: Path, trade: dict):
    """Append a trade to a JSON trades file (atomic write with file lock)."""
    storage_save_trade(trades_path, trade, logger=_log)


# === Shared market data cache ===

def write_market_cache(markets_by_prefix):
    """Write market data to shared cache file (atomic write).

    Args:
        markets_by_prefix: Dict mapping prefix strings to market lists.
    """
    _atomic_write_json(MARKET_CACHE_PATH, {
        "updated_at": time.time(),
        "markets": markets_by_prefix,
    })


def read_market_cache(prefix=None, max_age=MARKET_CACHE_TTL):
    """Read markets from shared cache if fresh enough.

    Args:
        prefix: Ticker prefix to look up. If None, returns all cached data.
        max_age: Maximum age in seconds before cache is considered stale.

    Returns:
        List of market dicts if cache is fresh, or None if missing/stale.
    """
    try:
        if not MARKET_CACHE_PATH.exists():
            return None
        data = json.loads(MARKET_CACHE_PATH.read_text())
        age = time.time() - data.get("updated_at", 0)
        if age > max_age:
            return None
        markets = data.get("markets", {})
        if prefix is not None:
            result = markets.get(prefix)
            if isinstance(result, list):
                normalize_markets(result)
            return result
        # Normalize all prefixes when returning full cache
        for pfx, mkt_list in markets.items():
            if isinstance(mkt_list, list):
                normalize_markets(mkt_list)
        return markets
    except (json.JSONDecodeError, OSError, KeyError):
        return None


# === Concurrent fetch utility ===

def fetch_parallel(urls, headers=None, timeout=20, max_workers=5):
    """Fetch multiple URLs concurrently using a thread pool.

    Returns a dict mapping each URL to its Response object, or None on failure.
    """
    results = {}

    def _fetch_one(url):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            return url, r
        except Exception as e:
            _log.warning("Parallel fetch failed for %s: %s", url, e)
            return url, None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, u): u for u in urls}
        for future in as_completed(futures):
            url, response = future.result()
            results[url] = response

    return results


# === Retry utility for external (non-Kalshi) API calls ===

def retry_request(method, url, max_retries=3, backoff_base=1.0, **kwargs):
    """Make an HTTP request with retries on transient errors.

    Retries on ConnectionError, Timeout, and 429. Does NOT retry other 4xx.
    kwargs are forwarded to requests.request().
    """
    kwargs.setdefault("timeout", 20)
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.request(method, url, **kwargs)
            if r.status_code == 429:
                wait = backoff_base * (2 ** attempt)
                _log.warning("Rate limited (429) on %s, retrying in %.1fs...", url, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            wait = backoff_base * (2 ** attempt)
            _log.warning("Transient error on %s, retrying in %.1fs... (%s)", url, wait, e)
            time.sleep(wait)
        except requests.exceptions.HTTPError:
            raise
        except Exception:
            raise
    raise last_err or Exception(f"Max retries exceeded for {url}")


# === Trade deduplication tracker ===

class RecentTradeTracker(ExecutionRecentTradeTracker):
    """Compatibility wrapper over the extracted execution.trade_manager module."""

    def __init__(self, trades_path: Path, cooldown_hours=6):
        super().__init__(
            trades_path,
            cooldown_hours=cooldown_hours,
            load_trades_func=load_trades,
        )


# === Kill switch / circuit breaker ===


class CircuitBreaker(RiskCircuitBreaker):
    """Compatibility wrapper over the extracted risk.circuit_breaker module."""

    def __init__(self, max_failures=5, reset_seconds=300, state_path=None):
        super().__init__(
            max_failures=max_failures,
            reset_seconds=reset_seconds,
            state_path=state_path,
            state_writer=_atomic_write_json,
            notifier=notify_webhook,
            logger=_log,
        )


# === Config validation ===

def validate_trade_config(config, bot_name=""):
    """Validate trade-related config values. Raises ValueError on bad config."""
    return execution_validate_trade_config(config, bot_name=bot_name)


# === Trade log trimming ===

def trim_trade_log(trades_path, max_age_days=90, max_entries=5000):
    """Remove old entries from a trade log file."""
    return execution_trim_trade_log(
        trades_path,
        max_age_days=max_age_days,
        max_entries=max_entries,
        load_trades_func=load_trades,
        atomic_write_json_func=_atomic_write_json,
        logger=_log,
    )


# === Scan Summary ===

class ScanSummary:
    """Tracks scan-level metrics for observability."""

    def __init__(self, bot_name, logger=None):
        self.bot = bot_name
        self.log = logger
        self._start = time.time()
        self.markets_fetched = 0
        self.markets_evaluated = 0
        self.trades_placed = 0
        self.skips = {}           # reason -> count
        self.data_sources = {}    # source -> "ok" | error msg

    def skip(self, reason):
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def source_ok(self, name):
        self.data_sources[name] = "ok"

    def source_fail(self, name, msg="error"):
        self.data_sources[name] = msg

    def finalize(self):
        duration = round(time.time() - self._start, 1)
        total_skipped = sum(self.skips.values())
        summary = {
            "timestamp": _utc_now_iso(),
            "bot": self.bot,
            "duration_seconds": duration,
            "markets_fetched": self.markets_fetched,
            "markets_evaluated": self.markets_evaluated,
            "trades_placed": self.trades_placed,
            "total_skipped": total_skipped,
            "skips": dict(self.skips),
            "data_sources": dict(self.data_sources),
        }
        if self.log:
            skip_str = ", ".join(f"{k}={v}" for k, v in sorted(self.skips.items())) or "none"
            self.log.info(
                f"SCAN SUMMARY: {duration}s | fetched={self.markets_fetched} "
                f"evaluated={self.markets_evaluated} placed={self.trades_placed} "
                f"skipped={total_skipped} ({skip_str})"
            )
        _append_scan_summary(summary)
        return summary


def _append_scan_summary(summary):
    """Append scan summary to rotating JSON log (max 2000 entries).

    Uses fcntl.LOCK_EX to prevent concurrent writes from multiple bots
    clobbering each other's data.
    """
    try:
        store = MetricsStore(SCAN_SUMMARIES_PATH, logger=_log)

        def _append(existing):
            summaries = list(existing)
            summaries.append(summary)
            _maybe_alert_on_scan_summary(summaries, summary)
            return store._trim_records(
                summaries,
                max_records=store.max_records,
                trim_to=store.trim_to,
            )

        store.update(_append)
    except Exception as e:
        _log.warning("Failed to append scan summary: %s", e)


def _maybe_alert_on_scan_summary(summaries, latest):
    bot = latest.get("bot")
    if not bot:
        return
    bot_summaries = [s for s in summaries if s.get("bot") == bot]
    streak = 0
    for entry in reversed(bot_summaries):
        if entry.get("trades_placed", 0) == 0 and entry.get("markets_evaluated", 0) > 0:
            streak += 1
            continue
        break
    if streak != ZERO_TRADE_ALERT_STREAK:
        return
    skip_totals = {}
    for entry in bot_summaries[-ZERO_TRADE_ALERT_STREAK:]:
        for reason, count in entry.get("skips", {}).items():
            skip_totals[reason] = skip_totals.get(reason, 0) + count
    top_skip = "none"
    if skip_totals:
        top_skip = max(skip_totals.items(), key=lambda item: item[1])[0]
    notify_webhook(
        f"{bot}: 0 trades across {ZERO_TRADE_ALERT_STREAK} consecutive scans "
        f"(top skip={top_skip})",
        level="warning",
        logger=_log,
    )


# === Order Monitor ===


class OrderMonitor(ExecutionOrderMonitor):
    """Compatibility wrapper over the extracted execution.order_monitor module."""

    def __init__(self, client, log=None, max_age_seconds=300, check_interval=30):
        super().__init__(
            client,
            log=log or _log,
            max_age_seconds=max_age_seconds,
            check_interval=check_interval,
        )


# === TradeManager ===

class TradeManager(ExecutionTradeManager):
    """Compatibility wrapper over the extracted execution.trade_manager module."""

    def __init__(self, client, trades_path, config, logger=None,
                 kill_switch_path=None, cooldown_hours=6, order_monitor=None,
                 breaker_state_path=SHARED_BREAKER_PATH, bot_name=None):
        super().__init__(
            client,
            trades_path,
            config,
            logger=logger or _log,
            kill_switch_path=kill_switch_path,
            cooldown_hours=cooldown_hours,
            order_monitor=order_monitor,
            breaker_state_path=breaker_state_path,
            bot_name=bot_name,
            breaker_factory=lambda state_path: CircuitBreaker(state_path=state_path),
            load_trades_func=load_trades,
            save_trade_func=save_trade,
            save_decision_func=save_decision,
            notify_func=notify_webhook,
            atomic_write_json_func=_atomic_write_json,
            utc_now_iso_func=_utc_now_iso,
            trade_store_cls=TradeStore,
            requests_module=requests,
            tracker_cls=RecentTradeTracker,
            check_kill_switch_func=check_kill_switch,
            per_bot_halt_path_func=per_bot_halt_path,
        )


# === Market snapshot helper ===

def build_market_snapshot(yes_bid=None, yes_ask=None, volume=None, open_interest=None):
    """Build a market snapshot dict for inclusion in trade records."""
    snap = {}
    if yes_bid is not None:
        snap["yes_bid"] = yes_bid
    if yes_ask is not None:
        snap["yes_ask"] = yes_ask
    if volume is not None:
        snap["volume"] = volume
    if open_interest is not None:
        snap["open_interest"] = open_interest
    return snap


# === Scan decision log ===

def save_decision(decisions_path: Path, decision: dict):
    """Append a scan decision to the decisions log (atomic write).

    Uses fcntl.LOCK_EX to prevent concurrent writes from multiple bots.
    """
    storage_save_decision(decisions_path, decision, logger=_log)


# === Notification ===

# === Health Check Monitor ===

HEALTH_STATE_PATH = PROJECT_DIR / "data" / "health-state.json"


class HealthCheckMonitor:
    """Tracks data source health and bot liveness.

    Records source successes/errors and bot heartbeats. Detects staleness
    (no heartbeat for N minutes) and high error rates.

    Args:
        state_path: Path to persist health state. Default: data/health-state.json.
        staleness_minutes: Minutes without heartbeat before flagging stale (default 60).
        auto_halt: If True, creates HALT_TRADING file on critical failure (default False).
        logger: Optional logger.
    """

    def __init__(self, state_path=None, staleness_minutes=60, auto_halt=False, logger=None,
                 alert_cooldown_minutes=30, per_bot_halt_cooldown_seconds=600,
                 source_breaker_threshold=5, source_breaker_cooldown_seconds=600):
        self.state_path = Path(state_path) if state_path else HEALTH_STATE_PATH
        self.staleness_minutes = staleness_minutes
        self.auto_halt = auto_halt
        self.log = logger or _log
        self._alert_cooldown_minutes = alert_cooldown_minutes
        self._alerts_sent = {}  # key -> datetime of last alert
        self._per_bot_halt_cooldown = per_bot_halt_cooldown_seconds
        self._halt_transitions = {}  # bot_name -> timestamp of last halt/unhalt
        self.source_breaker_threshold = source_breaker_threshold
        self.source_breaker_cooldown_seconds = source_breaker_cooldown_seconds
        self._state = normalize_health_state(None)
        self._dirty_bots = set()      # bot names modified by this process
        self._dirty_sources = set()   # source names modified by this process
        self._load()

    def _load(self):
        if self.state_path.exists():
            try:
                self._state = normalize_health_state(json.loads(self.state_path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        """Save health state, merging this bot's data with other bots' on-disk state.

        Uses fcntl.LOCK_EX to prevent concurrent writes from erasing
        other bots' heartbeats.
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(".lock")
        try:
            with open(lock_path, "w") as lock_fd:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    on_disk = normalize_health_state(None)
                    if self.state_path.exists():
                        try:
                            on_disk = normalize_health_state(json.loads(self.state_path.read_text()))
                        except (json.JSONDecodeError, OSError):
                            pass
                    # Only write back entries this process has modified, to avoid
                    # overwriting other bots' fresh heartbeats with stale startup copies
                    on_disk_bots = on_disk.setdefault("bots", {})
                    for bot in self._dirty_bots:
                        if bot in self._state.get("bots", {}):
                            on_disk_bots[bot] = self._state["bots"][bot]
                    on_disk_sources = on_disk.setdefault("sources", {})
                    for source in self._dirty_sources:
                        if source in self._state.get("sources", {}):
                            on_disk_sources[source] = self._state["sources"][source]
                    on_disk = normalize_health_state(on_disk)
                    _atomic_write_json(self.state_path, on_disk)
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception as e:
            self.log.warning("Failed to save health state: %s", e)

    @staticmethod
    def _parse_state_time(value):
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed
        except (ValueError, TypeError, AttributeError):
            return None

    def _source_issue_active(self, data, threshold=1, now=None):
        error_count = data.get("error_count", 0)
        if error_count < threshold:
            return False
        opened_at = data.get("opened_at")
        if opened_at is not None:
            try:
                if (time.time() - float(opened_at)) < (self.source_breaker_cooldown_seconds * 2):
                    return True
            except (TypeError, ValueError):
                pass
        now = now or datetime.datetime.now(datetime.timezone.utc)
        last_error = self._parse_state_time(data.get("last_error"))
        if last_error is None:
            return False
        active_window = max(self.source_breaker_cooldown_seconds * 2, 6 * 3600)
        return (now - last_error).total_seconds() <= active_window

    def record_source_success(self, source):
        """Record a successful data source fetch. Clears circuit breaker if open."""
        if source not in self._state["sources"]:
            self._state["sources"][source] = {"last_success": None, "last_error": None, "error_count": 0}
        self._state["sources"][source]["last_success"] = _utc_now_iso()
        self._state["sources"][source]["error_count"] = 0
        self._state["sources"][source]["opened_at"] = None
        self._dirty_sources.add(source)
        self._save()

    def record_source_error(self, source, msg=""):
        """Record a data source error. Opens circuit breaker after threshold consecutive errors."""
        if source not in self._state["sources"]:
            self._state["sources"][source] = {"last_success": None, "last_error": None, "error_count": 0}
        data = self._state["sources"][source]
        data["last_error"] = _utc_now_iso()
        data["error_count"] = data.get("error_count", 0) + 1
        if data["error_count"] >= self.source_breaker_threshold and data.get("opened_at") is None:
            data["opened_at"] = time.time()
            self.log.warning("Source circuit breaker opened for %s after %d errors", source, data["error_count"])
            alert_msg = f"Source circuit breaker opened: {source} ({data['error_count']} consecutive errors)"
            notify_webhook(alert_msg, level="warning", logger=self.log)
            notify_imessage(alert_msg, logger=self.log)
        self._dirty_sources.add(source)
        self._save()

    def trip_source_breaker(self, source, msg="", error_count=None):
        """Open a source circuit breaker immediately for deterministic failures."""
        if source not in self._state["sources"]:
            self._state["sources"][source] = {"last_success": None, "last_error": None, "error_count": 0}
        data = self._state["sources"][source]
        threshold = error_count if isinstance(error_count, int) and error_count > 0 else self.source_breaker_threshold
        was_open = data.get("error_count", 0) >= threshold and data.get("opened_at") is not None
        data["last_error"] = _utc_now_iso()
        data["error_count"] = max(data.get("error_count", 0), threshold)
        data["opened_at"] = time.time()
        if not was_open:
            self.log.warning("Source circuit breaker opened immediately for %s", source)
            detail = f" ({msg})" if msg else ""
            alert_msg = f"Source circuit breaker opened: {source} (deterministic failure){detail}"
            notify_webhook(alert_msg, level="warning", logger=self.log)
            notify_imessage(alert_msg, logger=self.log)
        self._dirty_sources.add(source)
        self._save()

    def is_source_open(self, source):
        """Return True if source has tripped the circuit breaker (callers should skip).

        Opens after source_breaker_threshold consecutive errors.
        Auto-resets (half-open) after source_breaker_cooldown_seconds.
        """
        data = self._state.get("sources", {}).get(source, {})
        if data.get("error_count", 0) < self.source_breaker_threshold:
            return False
        opened_at = data.get("opened_at")
        if opened_at is None:
            return True
        elapsed = time.time() - opened_at
        if elapsed >= self.source_breaker_cooldown_seconds:
            # Half-open: reset error_count so one retry is allowed
            data["error_count"] = 0
            data["opened_at"] = None
            self._dirty_sources.add(source)
            self._save()
            return False
        return True

    def record_bot_heartbeat(self, bot):
        """Record a bot heartbeat (proves the bot loop is running)."""
        self._state["bots"][bot] = {"last_heartbeat": _utc_now_iso()}
        self._dirty_bots.add(bot)
        self._save()

    def should_send_alert(self, alert_key):
        """Check if an alert should be sent (respects cooldown window)."""
        if alert_key not in self._alerts_sent:
            return True
        elapsed = (datetime.datetime.now(datetime.timezone.utc) - self._alerts_sent[alert_key]).total_seconds() / 60
        return elapsed >= self._alert_cooldown_minutes

    def record_alert_sent(self, alert_key):
        """Record that an alert was sent (for deduplication)."""
        self._alerts_sent[alert_key] = datetime.datetime.now(datetime.timezone.utc)

    def get_summary(self):
        """Get a structured health summary for dashboard display.

        Returns dict with sources, bots, and overall status.
        """
        summary = {"sources": {}, "bots": {}, "overall": "healthy"}

        issues = 0
        now = datetime.datetime.now(datetime.timezone.utc)
        for source, data in self._state.get("sources", {}).items():
            error_count = data.get("error_count", 0)
            active_error = self._source_issue_active(data, threshold=self.source_breaker_threshold, now=now)
            active_warning = self._source_issue_active(data, threshold=1, now=now)
            status = "error" if active_error else ("warning" if active_warning else "ok")
            if status == "error":
                issues += 1
            summary["sources"][source] = {
                "status": status,
                "error_count": error_count,
                "last_success": data.get("last_success"),
                "last_error": data.get("last_error"),
            }

        for bot, data in self._state.get("bots", {}).items():
            last_hb = data.get("last_heartbeat")
            stale = False
            if last_hb:
                try:
                    hb_dt = datetime.datetime.fromisoformat(last_hb)
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=datetime.timezone.utc)
                    age_min = (datetime.datetime.now(datetime.timezone.utc) - hb_dt).total_seconds() / 60
                    stale = age_min > self.staleness_minutes
                except (ValueError, TypeError):
                    pass
            status = "stale" if stale else "ok"
            if stale:
                issues += 1
            summary["bots"][bot] = {
                "status": status,
                "last_heartbeat": last_hb,
            }

        if issues >= 2:
            summary["overall"] = "critical"
        elif issues >= 1:
            summary["overall"] = "degraded"

        return normalize_health_summary(summary)

    def check_health(self, staleness_minutes=None):
        """Check for health issues. Returns list of issue strings.

        Issues:
          - Bot stale: no heartbeat for > staleness_minutes
          - Source errors: consecutive error count > 5
        """
        stale_min = staleness_minutes or self.staleness_minutes
        now = datetime.datetime.now(datetime.timezone.utc)
        issues = []

        # Check bot staleness
        for bot, info in self._state.get("bots", {}).items():
            hb = info.get("last_heartbeat")
            if hb:
                try:
                    hb_dt = datetime.datetime.fromisoformat(hb)
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=datetime.timezone.utc)
                    age_min = (now - hb_dt).total_seconds() / 60
                    if age_min > stale_min:
                        issues.append(f"bot/{bot} stale: last heartbeat {age_min:.0f}min ago")
                except (ValueError, TypeError):
                    pass

        # Check source errors
        for source, info in self._state.get("sources", {}).items():
            error_count = info.get("error_count", 0)
            if self._source_issue_active(info, threshold=self.source_breaker_threshold, now=now):
                issues.append(f"source/{source} failing: {error_count} consecutive errors")

        # Webhook alert for critical health issues
        if issues:
            critical = [i for i in issues if "stale" in i or "failing" in i]
            if critical:
                notify_webhook(
                    f"Health check: {'; '.join(critical[:3])}",
                    level="warning",
                )

        # Per-bot halts (replaces global auto-halt)
        if self.auto_halt and issues:
            halt_status = self.check_per_bot_halts()
            for bot_name, action in halt_status.items():
                issues.append(f"PER-BOT-HALT: {bot_name} {action}")

        return issues

    def check_per_bot_halts(self):
        """Create/remove per-bot halt files based on source health.

        For each bot in BOT_SOURCE_MAP:
        - If ALL its sources have error_count >= 5 → create halt file
        - If ALL its sources have error_count == 0 → remove halt file
        - Respects cooldown between transitions (anti-flap)

        Returns:
            Dict of {bot_name: action} where action is "halted", "recovered", or "unchanged".
        """
        now = time.time()
        status = {}
        sources = self._state.get("sources", {})

        for bot_name, required_sources in BOT_SOURCE_MAP.items():
            halt_path = per_bot_halt_path(bot_name)
            currently_halted = halt_path.exists()

            # Check if all sources are failing (error_count >= 5)
            all_failing = bool(required_sources) and all(
                sources.get(s, {}).get("error_count", 0) >= 5
                for s in required_sources
            )

            # Check if all sources have recovered (error_count == 0)
            all_recovered = all(
                sources.get(s, {}).get("error_count", 0) == 0
                for s in required_sources
            )

            # Check cooldown
            last_transition = self._halt_transitions.get(bot_name, 0)
            cooldown_ok = (now - last_transition) >= self._per_bot_halt_cooldown

            if all_failing and not currently_halted and cooldown_ok:
                halt_path.parent.mkdir(parents=True, exist_ok=True)
                failing_sources = [s for s in required_sources if sources.get(s, {}).get("error_count", 0) >= 5]
                halt_path.write_text(f"Auto-halted: sources failing: {', '.join(failing_sources)}")
                self._halt_transitions[bot_name] = now
                self.log.warning("PER-BOT HALT created for %s (sources: %s)", bot_name, ", ".join(failing_sources))
                status[bot_name] = "halted"
            elif all_recovered and currently_halted and cooldown_ok:
                halt_path.unlink(missing_ok=True)
                self._halt_transitions[bot_name] = now
                self.log.info("PER-BOT HALT removed for %s (sources recovered)", bot_name)
                status[bot_name] = "recovered"
            else:
                status[bot_name] = "unchanged"

        return status


def notify_whatsapp(message, phone=None, logger=None):
    """Send a WhatsApp notification via openclaw CLI.

    Args:
        message: Text message to send.
        phone: Phone number (E.164 format). If None, reads from bots-config.json.
        logger: Optional logger instance.
    """
    import subprocess
    _log = logger or logging.getLogger("notify")
    if not phone:
        phone = os.environ.get("NOTIFICATION_PHONE", "")
    if not phone:
        try:
            cfg_path = PROJECT_DIR / "config" / "bots-config.json"
            with open(cfg_path) as f:
                cfg = json.load(f)
            phone = cfg.get("notificationPhone", "")
        except Exception:
            pass
    if not phone:
        _log.warning("No notificationPhone configured — notification logged only")
        return False
    try:
        result = subprocess.run(
            ["openclaw", "message", "send", "--to", phone,
             "--message", message, "--channel", "whatsapp"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            _log.info("WhatsApp notification sent")
            return True
        else:
            _log.warning("WhatsApp send failed: %s", result.stderr[:200])
            return False
    except FileNotFoundError:
        _log.warning("openclaw CLI not found — notification logged only")
        return False
    except Exception as e:
        _log.warning("WhatsApp error: %s", e)
        return False


# === Webhook Alerting ===

_webhook_rate_limiter = {}  # message_prefix -> last_sent_timestamp
_WEBHOOK_COOLDOWN_SECONDS = 1800  # 30 minutes


def _reset_webhook_rate_limiter():
    """Reset the webhook rate limiter (for testing)."""
    _webhook_rate_limiter.clear()


def notify_webhook(message, level="info", logger=None):
    """Send an alert to a Slack or Discord webhook.

    Auto-detects Slack vs Discord by URL pattern. Rate-limits duplicate
    messages (same first 80 chars) to at most once per 30 minutes.

    Args:
        message: Alert message text.
        level: "info", "warning", or "critical" — controls emoji prefix.
        logger: Optional logger instance.

    Returns:
        True if sent, False if skipped (no URL, rate-limited, or error).
    """
    log = logger or _log
    url = os.environ.get("ALERT_WEBHOOK_URL", "")
    if not url:
        return False

    # Rate limiting: 30-minute cooldown per unique message prefix
    prefix = message[:80]
    now = time.time()
    last_sent = _webhook_rate_limiter.get(prefix, 0)
    if now - last_sent < _WEBHOOK_COOLDOWN_SECONDS:
        return False

    # Level-based emoji prefix
    emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
    full_message = f"{emoji} [{level.upper()}] {message}"

    # Auto-detect Slack vs Discord by URL pattern
    if "discord" in url.lower():
        payload = {"content": full_message}
    else:
        payload = {"text": full_message}

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        _webhook_rate_limiter[prefix] = now
        log.info("Webhook alert sent: %s", message[:100])
        return True
    except requests.exceptions.ConnectionError:
        log.warning("Webhook connection error — alert not delivered")
        return False
    except requests.exceptions.HTTPError as e:
        log.warning("Webhook HTTP error %s — alert not delivered", e.response.status_code if e.response else "?")
        return False
    except Exception as e:
        log.warning("Webhook error: %s", e)
        return False


# === iMessage Alerting (BlueBubbles) ===

_imessage_rate_limiter = {}
_IMESSAGE_COOLDOWN_SECONDS = 1800  # 30 min


def _reset_imessage_rate_limiter():
    """Reset the iMessage rate limiter (for testing)."""
    _imessage_rate_limiter.clear()


def _send_imessage_blocking(message, prefix, logger):
    """Blocking iMessage send (runs in background thread)."""
    log = logger or _log
    bb_url = os.environ.get("BLUEBUBBLES_URL", "")
    bb_password = os.environ.get("BLUEBUBBLES_PASSWORD", "")
    bb_chat = os.environ.get("BLUEBUBBLES_CHAT_GUID", "")
    try:
        r = requests.post(
            f"{bb_url}/api/v1/message/text",
            params={"password": bb_password},
            json={"chatGuid": bb_chat, "message": message},
            timeout=10,
        )
        r.raise_for_status()
        _imessage_rate_limiter[prefix] = time.time()
        log.info("iMessage sent: %s", prefix)
    except Exception as e:
        log.warning("iMessage send failed: %s", e)


def notify_imessage(message, logger=None):
    """Send an iMessage via BlueBubbles API (fire-and-forget).

    Requires BLUEBUBBLES_URL, BLUEBUBBLES_PASSWORD, BLUEBUBBLES_CHAT_GUID env vars.
    Rate-limits duplicate messages (same first 80 chars) to once per 30 minutes.
    Sends in a daemon thread so callers are never blocked by network latency.

    Returns:
        True if dispatched, False if skipped (missing config or rate-limited).
    """
    bb_url = os.environ.get("BLUEBUBBLES_URL", "")
    bb_password = os.environ.get("BLUEBUBBLES_PASSWORD", "")
    bb_chat = os.environ.get("BLUEBUBBLES_CHAT_GUID", "")
    if not bb_url or not bb_password or not bb_chat:
        return False

    prefix = message[:80]
    now = time.time()
    if now - _imessage_rate_limiter.get(prefix, 0) < _IMESSAGE_COOLDOWN_SECONDS:
        return False

    # Optimistically mark as sent to prevent duplicate dispatches
    _imessage_rate_limiter[prefix] = now
    t = threading.Thread(target=_send_imessage_blocking, args=(message, prefix, logger), daemon=True)
    t.start()
    return True
