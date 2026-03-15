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

from execution.order_monitor import OrderMonitor as ExecutionOrderMonitor
from execution.trade_manager import (
    RecentTradeTracker as ExecutionRecentTradeTracker,
    TradeManager as ExecutionTradeManager,
    trim_trade_log as execution_trim_trade_log,
    validate_trade_config as execution_validate_trade_config,
)
from ops.health_monitor import (
    BOT_SOURCE_MAP,
    HEALTH_STATE_PATH,
    HealthCheckMonitor as OpsHealthCheckMonitor,
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


class HealthCheckMonitor(OpsHealthCheckMonitor):
    """Compatibility wrapper over the extracted ops.health_monitor module."""

    def __init__(self, state_path=None, staleness_minutes=60, auto_halt=False, logger=None,
                 alert_cooldown_minutes=30, per_bot_halt_cooldown_seconds=600,
                 source_breaker_threshold=5, source_breaker_cooldown_seconds=600):
        super().__init__(
            state_path=state_path,
            staleness_minutes=staleness_minutes,
            auto_halt=auto_halt,
            logger=logger or _log,
            alert_cooldown_minutes=alert_cooldown_minutes,
            per_bot_halt_cooldown_seconds=per_bot_halt_cooldown_seconds,
            source_breaker_threshold=source_breaker_threshold,
            source_breaker_cooldown_seconds=source_breaker_cooldown_seconds,
            atomic_write_json_func=_atomic_write_json,
            utc_now_iso_func=_utc_now_iso,
            notify_webhook_func=lambda *args, **kwargs: notify_webhook(*args, **kwargs),
            notify_imessage_func=lambda *args, **kwargs: notify_imessage(*args, **kwargs),
            per_bot_halt_path_func=lambda name: per_bot_halt_path(name),
            bot_source_map=BOT_SOURCE_MAP,
        )


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
