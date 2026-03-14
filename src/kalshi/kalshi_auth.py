#!/usr/bin/env python3
"""Shared Kalshi API authentication and utilities.

All bots should use this module instead of duplicating auth logic.

Usage:
    from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered

    client = KalshiClient()  # reads from env vars
    data = client.get("/portfolio/balance")
    client.post("/portfolio/orders", body={...})
"""

import json, time, base64, os, sys, signal, logging, datetime, tempfile, fcntl, threading
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from dotenv import load_dotenv

from artifact_contracts import normalize_health_state, normalize_health_summary

# === Constants ===
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_DIR / ".env")
DEFAULT_KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds

KILL_SWITCH_PATH = PROJECT_DIR / "data" / "HALT_TRADING"
PER_BOT_HALT_PREFIX = "HALT_bot_"
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
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    os.environ['PYTHONUNBUFFERED'] = '1'


def setup_logging(name, log_file=None):
    """Configure a logger with consistent format for a bot.

    Returns a logging.Logger with stdout handler and file handler.
    Auto-derives log file path from bot name if not explicitly provided:
    ``data/logs/{name}.log`` with 5MB rotation and 3 backups.

    Call once at bot startup: ``log = setup_logging("weather")``
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Only add StreamHandler if stdout is a real TTY (not redirected by supervisor)
    if hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    # Auto file logging — derive path from bot name if not explicitly provided
    if log_file is None:
        log_file = str(PROJECT_DIR / "data" / "logs" / f"{name}.log")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


_shutdown_requested = False


def is_shutdown_requested():
    """Check if a graceful shutdown has been requested via SIGUSR1.

    Bots should check this after each scan cycle and break their main loop
    to allow clean exit before SIGTERM arrives.
    """
    return _shutdown_requested


def setup_signal_handlers():
    """Install graceful shutdown handlers for SIGTERM, SIGINT, and SIGUSR1."""
    def _handler(signum, frame):
        _log.info("Received signal %s, shutting down gracefully...", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    def _sigusr1_handler(signum, frame):
        global _shutdown_requested
        _log.info("Received SIGUSR1, requesting graceful shutdown...")
        _shutdown_requested = True
    signal.signal(signal.SIGUSR1, _sigusr1_handler)


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
    if trades_path.exists():
        try:
            return json.loads(trades_path.read_text())
        except (json.JSONDecodeError, ValueError) as e:
            _log.warning("Corrupt trades file %s: %s", trades_path, e)
            return []
    return []


def atomic_write_json(path: Path, data):
    """Write JSON data to a file atomically using a temp file + os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

# Backward-compatible alias
_atomic_write_json = atomic_write_json


def save_trade(trades_path: Path, trade: dict):
    """Append a trade to a JSON trades file (atomic write with file lock)."""
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = trades_path.with_suffix(".lock")
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            trades = load_trades(trades_path)
            trades.append(trade)
            _atomic_write_json(trades_path, trades)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


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

class RecentTradeTracker:
    """Track recently traded tickers to prevent duplicate trades across scan cycles.

    Args:
        trades_path: Path to the bot's JSON trade log file.
        cooldown_hours: Ignore tickers traded within this window (default 6h).
    """

    def __init__(self, trades_path: Path, cooldown_hours=6):
        self.trades_path = trades_path
        self.cooldown_hours = cooldown_hours
        self._recent = {}  # ticker -> last trade datetime
        self._load()

    def _load(self):
        """Load recent tickers from the trade file."""
        trades = load_trades(self.trades_path)
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=self.cooldown_hours)
        for t in trades:
            ts_str = t.get("timestamp", "")
            ticker = t.get("ticker", "")
            if not ts_str or not ticker:
                continue
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
                if ts > cutoff:
                    existing = self._recent.get(ticker)
                    if not existing or ts > existing:
                        self._recent[ticker] = ts
            except (ValueError, TypeError):
                pass

    def is_recent(self, ticker):
        """Return True if this ticker was traded within the cooldown window.

        Lazily prunes expired entries to prevent unbounded memory growth
        in long-running daemon sessions.
        """
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=self.cooldown_hours)
        # Prune expired entries every 100 calls (amortized O(1))
        if not hasattr(self, "_prune_counter"):
            self._prune_counter = 0
        self._prune_counter += 1
        if self._prune_counter >= 100:
            self._prune_counter = 0
            self._recent = {k: v for k, v in self._recent.items() if v > cutoff}

        ts = self._recent.get(ticker)
        if not ts:
            return False
        return ts > cutoff

    def record(self, ticker):
        """Record a trade on this ticker."""
        self._recent[ticker] = datetime.datetime.now(datetime.timezone.utc)


# === Kill switch ===

def check_kill_switch(path=None):
    """Return True if the kill switch file exists (trading should halt)."""
    p = Path(path) if path else KILL_SWITCH_PATH
    return p.exists()


def per_bot_halt_path(bot_name):
    """Return Path for a per-bot halt file: data/HALT_bot_{name}."""
    return PROJECT_DIR / "data" / f"{PER_BOT_HALT_PREFIX}{bot_name}"


# === Circuit breaker ===

SHARED_BREAKER_PATH = PROJECT_DIR / "data" / "circuit-breaker-state.json"


class CircuitBreaker:
    """Tracks consecutive API failures and opens after a threshold.

    When open, callers should skip trading until the breaker auto-resets.
    Optionally persists state to a shared file so all bots see the same
    breaker status.

    Args:
        max_failures: Consecutive failures before opening (default 5).
        reset_seconds: Seconds to wait before auto-resetting (default 300).
        state_path: Path to shared state file. If None, breaker is in-memory only.
    """

    def __init__(self, max_failures=5, reset_seconds=300, state_path=None):
        self.max_failures = max_failures
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at = None
        self.state_path = Path(state_path) if state_path else None

    def _with_shared_lock(self, fn):
        """Execute fn under file lock on shared state."""
        if not self.state_path:
            return fn()
        lock_path = self.state_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _load_shared(self):
        """Load shared breaker state from disk."""
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            cb = data.get("circuit_breaker", {})
            self._failures = cb.get("failures", 0)
            self._opened_at = cb.get("opened_at")
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    def _save_shared(self):
        """Save breaker state to shared file (merge into existing data)."""
        if not self.state_path:
            return
        try:
            existing = {}
            if self.state_path.exists():
                try:
                    existing = json.loads(self.state_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            existing["circuit_breaker"] = {
                "failures": self._failures,
                "opened_at": self._opened_at,
                "max_failures": self.max_failures,
            }
            _atomic_write_json(self.state_path, existing)
        except Exception as e:
            _log.warning("Failed to save circuit breaker state: %s", e)

    def record_success(self):
        """Record a successful operation — resets the failure counter."""
        def _do():
            if self.state_path:
                self._load_shared()
            self._failures = 0
            self._opened_at = None
            if self.state_path:
                self._save_shared()
        self._with_shared_lock(_do)

    def record_failure(self):
        """Record a failed operation — may open the breaker."""
        def _do():
            if self.state_path:
                self._load_shared()
            self._failures += 1
            if self._failures >= self.max_failures and self._opened_at is None:
                self._opened_at = time.time()
                notify_webhook(
                    f"Circuit breaker OPEN after {self._failures} consecutive failures",
                    level="critical",
                )
            if self.state_path:
                self._save_shared()
        self._with_shared_lock(_do)

    def is_open(self):
        """Return True if the breaker is open (callers should back off)."""
        def _do():
            if self.state_path:
                self._load_shared()
            if self._failures < self.max_failures:
                return False
            # Breaker is tripped — check if we can auto-reset
            if self._opened_at is None or not isinstance(self._opened_at, (int, float)):
                # opened_at was lost or corrupted — set it now so timer starts
                self._opened_at = time.time()
                if self.state_path:
                    self._save_shared()
                return True
            if (time.time() - self._opened_at) >= self.reset_seconds:
                # Auto-reset after timeout
                self._failures = 0
                self._opened_at = None
                if self.state_path:
                    self._save_shared()
                return False
            return True
        return self._with_shared_lock(_do)


# === Config validation ===

def validate_trade_config(config, bot_name=""):
    """Validate trade-related config values. Raises ValueError on bad config.

    Static dollar values (maxTradeAmount, maxDailyLoss) serve as floors.
    Optional percentage keys (maxTradeAmountPct, maxDailyLossPct) enable
    bankroll-proportional scaling — effective limit = max(static, bankroll * pct).
    """
    prefix = f"[{bot_name}] " if bot_name else ""

    amt = config.get("maxTradeAmount")
    if amt is None or amt <= 0:
        raise ValueError(f"{prefix}maxTradeAmount must be positive, got {amt}")
    if amt > 100:
        raise ValueError(f"{prefix}maxTradeAmount={amt} exceeds $100 safety cap")

    trades = config.get("maxDailyTrades")
    if trades is None or not isinstance(trades, int) or trades <= 0:
        raise ValueError(f"{prefix}maxDailyTrades must be a positive integer, got {trades}")
    if trades > 100:
        raise ValueError(f"{prefix}maxDailyTrades={trades} exceeds 100 safety cap")

    loss = config.get("maxDailyLoss")
    if loss is None or loss <= 0:
        raise ValueError(f"{prefix}maxDailyLoss must be positive, got {loss}")
    if loss > 500:
        raise ValueError(f"{prefix}maxDailyLoss={loss} exceeds $500 safety cap")

    # Validate optional percentage-based scaling keys
    for pct_key, label in [("maxTradeAmountPct", "maxTradeAmountPct"),
                           ("maxDailyLossPct", "maxDailyLossPct")]:
        pct = config.get(pct_key)
        if pct is not None:
            if not isinstance(pct, (int, float)) or pct < 0:
                raise ValueError(f"{prefix}{label} must be non-negative, got {pct}")
            if pct > 0.25:
                raise ValueError(f"{prefix}{label}={pct} exceeds 25% safety cap")


# === Trade log trimming ===

def trim_trade_log(trades_path, max_age_days=90, max_entries=5000):
    """Remove old entries from a trade log file.

    Keeps only entries newer than max_age_days and limits total to max_entries.
    Uses file locking to prevent races with save_trade().
    """
    trades_path = Path(trades_path)
    lock_path = trades_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            trades = load_trades(trades_path)
            if not trades:
                return

            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)
            filtered = []
            for t in trades:
                ts_str = t.get("timestamp", "")
                if ts_str:
                    try:
                        ts = datetime.datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=datetime.timezone.utc)
                        if ts < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass
                filtered.append(t)

            # Also limit by count (keep most recent)
            if len(filtered) > max_entries:
                filtered = filtered[-max_entries:]

            if len(filtered) != len(trades):
                _log.info("Trimmed trade log %s: %d -> %d entries", trades_path.name, len(trades), len(filtered))
                _atomic_write_json(trades_path, filtered)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


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
        SCAN_SUMMARIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_path = SCAN_SUMMARIES_PATH.with_suffix(".lock")
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                existing = []
                if SCAN_SUMMARIES_PATH.exists():
                    existing = json.loads(SCAN_SUMMARIES_PATH.read_text())
                existing.append(summary)
                _maybe_alert_on_scan_summary(existing, summary)
                if len(existing) > 2000:
                    existing = existing[-1500:]
                _atomic_write_json(SCAN_SUMMARIES_PATH, existing)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
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

class OrderMonitor:
    """Tracks pending orders and manages their lifecycle.

    Detects unfilled limit orders, cancels stale ones, and reclaims
    capital from resting orders that have exceeded their max age.

    Args:
        client: KalshiClient instance.
        log: Logger instance.
        max_age_seconds: Cancel resting orders after this many seconds (default 300 = 5 min).
        check_interval: Minimum seconds between check_orders() API calls (default 30).
    """

    def __init__(self, client, log=None, max_age_seconds=300, check_interval=30):
        self.client = client
        self.log = log or _log
        self.max_age_seconds = max_age_seconds
        self.check_interval = check_interval
        self._pending = {}  # {order_id: {"placed_at": float, "ticker": str, "side": str, "price": int, "count": int}}
        self._last_check = 0

    def track(self, order_id, ticker, side, price_cents, count):
        """Register a newly placed order for monitoring."""
        self._pending[order_id] = {
            "placed_at": time.time(),
            "ticker": ticker,
            "side": side,
            "price": price_cents,
            "count": count,
        }

    def check_orders(self):
        """Poll order statuses and handle stale orders.

        Called from daemon bot main loops (not a background thread).
        Returns dict of {order_id: {"status": str, "action": str}} for any state changes.

        Respects check_interval to avoid excessive API calls.
        """
        now = time.time()
        if now - self._last_check < self.check_interval:
            return {}
        self._last_check = now

        if not self._pending:
            return {}

        changes = {}

        try:
            data = self.client.get("/portfolio/orders?status=resting")
            resting_ids = {o.get("order_id") for o in data.get("orders", [])}
        except Exception as e:
            self.log.warning("OrderMonitor: failed to fetch resting orders: %s", e)
            return {}

        stale_ids = []
        for order_id, info in list(self._pending.items()):
            age = now - info["placed_at"]

            if order_id not in resting_ids:
                # Order is no longer resting — filled, canceled, or expired
                self.log.info("OrderMonitor: %s on %s no longer resting (filled/canceled after %.0fs)",
                              order_id[:12], info["ticker"], age)
                changes[order_id] = {"status": "filled_or_canceled", "action": "removed"}
                del self._pending[order_id]
            elif age > self.max_age_seconds:
                # Still resting but too old — cancel it
                stale_ids.append(order_id)

        # Cancel stale orders
        for order_id in stale_ids:
            info = self._pending[order_id]
            success = self.cancel_order(order_id)
            if success:
                self.log.info("OrderMonitor: canceled stale order %s on %s (age %.0fs > %ds)",
                              order_id[:12], info["ticker"],
                              now - info["placed_at"], self.max_age_seconds)
                changes[order_id] = {"status": "canceled_stale", "action": "canceled"}
                del self._pending[order_id]

        return changes

    def cancel_order(self, order_id):
        """Cancel a specific order. Returns True on success."""
        try:
            self.client.delete(f"/portfolio/orders/{order_id}")
            return True
        except Exception as e:
            self.log.warning("OrderMonitor: failed to cancel %s: %s", order_id[:12], e)
            return False

    def get_pending_count(self):
        """Return number of orders still being tracked."""
        return len(self._pending)

    def get_pending_capital(self):
        """Return total cents locked in pending orders."""
        return sum(info["price"] * info["count"] for info in self._pending.values())


# === TradeManager ===

class TradeManager:
    """Consolidated trade placement with all safety guardrails.

    Replaces per-bot place_trade() functions with a single implementation
    that enforces: kill switch, circuit breaker, daily trade limit,
    daily loss limit, dedup, cost cap, balance check, and stale data check.

    Args:
        client: KalshiClient instance.
        trades_path: Path to the bot's JSON trade log file.
        config: Dict with keys: maxTradeAmount (dollars), maxDailyTrades (int),
                maxDailyLoss (dollars).
        logger: Optional logger; defaults to module logger.
        kill_switch_path: Path to kill switch file (default: data/HALT_TRADING).
        cooldown_hours: Hours to suppress duplicate trades on same ticker (default 6).
    """

    def __init__(self, client, trades_path, config, logger=None,
                 kill_switch_path=None, cooldown_hours=6, order_monitor=None,
                 breaker_state_path=SHARED_BREAKER_PATH, bot_name=None):
        validate_trade_config(config)
        self.client = client
        self.trades_path = Path(trades_path)
        self.config = config
        self.log = logger or _log
        self.kill_switch_path = kill_switch_path or KILL_SWITCH_PATH
        self.bot_name = bot_name
        self._per_bot_halt_path = per_bot_halt_path(bot_name) if bot_name else None
        self.tracker = RecentTradeTracker(self.trades_path, cooldown_hours=cooldown_hours)
        self.breaker = CircuitBreaker(state_path=breaker_state_path)
        self.order_monitor = order_monitor

        # Sell cooldown: prevent unlimited sell orders from malfunctioning monitors
        self._sell_cooldown = {}  # (ticker, side) -> timestamp
        self._sell_cooldown_seconds = 600  # 10 minutes between sells on same position

        # Daily counters
        self._daily_trades = 0
        self._daily_spend_cents = 0
        self._daily_date = None
        self._daily_loss_alerted = False
        self._daily_loss_block_count = 0
        self._daily_loss_first_blocked_at = None
        self._daily_loss_escalated = False
        self.last_error_code = None
        self.last_error_message = ""
        self._local_alerts = {}

        # Balance cache for bankroll-proportional limits (30s TTL)
        self._cached_balance_cents = None
        self._balance_fetched_at = 0

        # Write-ahead log for crash recovery
        self._wal_path = self.trades_path.with_suffix(".wal.json")
        self._recover_wal()

        # Log if percentage-based scaling is active
        if config.get("maxTradeAmountPct") or config.get("maxDailyLossPct"):
            self.log.info("Bankroll-proportional limits active: trade=%.1f%%, daily=%.1f%%",
                          config.get("maxTradeAmountPct", 0) * 100,
                          config.get("maxDailyLossPct", 0) * 100)

    def _reset_daily_if_needed(self):
        today = datetime.date.today().isoformat()
        if self._daily_date != today:
            self._daily_trades = 0
            self._daily_spend_cents = 0
            self._daily_date = today
            self._daily_loss_alerted = False
            self._daily_loss_block_count = 0
            self._daily_loss_first_blocked_at = None
            self._daily_loss_escalated = False
            self._rebuild_daily_counters_from_log(today)

    def remaining_daily_trade_slots(self):
        """Return how many trade slots remain today after syncing from the log."""
        self._reset_daily_if_needed()
        return max(0, int(self.config["maxDailyTrades"]) - self._daily_trades)

    def _clear_last_error(self):
        self.last_error_code = None
        self.last_error_message = ""

    def _set_last_error(self, code, message):
        self.last_error_code = code
        self.last_error_message = message

    def _should_send_local_alert(self, key, cooldown_seconds=1800):
        now = time.time()
        last_sent = self._local_alerts.get(key, 0)
        if (now - last_sent) < cooldown_seconds:
            return False
        self._local_alerts[key] = now
        return True

    def _rebuild_daily_counters_from_log(self, today_str):
        """Reconstruct daily counters from trade log after restart."""
        trades = load_trades(self.trades_path)
        for t in trades:
            ts = t.get("timestamp", "")
            if ts.startswith(today_str) and t.get("action", "buy") != "sell":
                self._daily_trades += 1
                self._daily_spend_cents += t.get("cost_cents", 0)
        if self._daily_trades > 0:
            self.log.info("Daily counters rebuilt from log: %d trades, $%.2f risk",
                          self._daily_trades, self._daily_spend_cents / 100)

    def _get_available_balance(self):
        """Get available balance in cents, cached for 30 seconds."""
        now = time.time()
        if self._cached_balance_cents is not None and (now - self._balance_fetched_at) < 30:
            return self._cached_balance_cents
        if self.client:
            try:
                _, available = self.client.get_balance()
                self._cached_balance_cents = available
                self._balance_fetched_at = now
                return available
            except Exception as e:
                self.log.warning("Failed to fetch balance, using cached: %s", e)
        return self._cached_balance_cents or 0

    def _effective_max_trade_cents(self):
        """Resolve max trade amount: min(static config, bankroll * pct).

        Static config is the ceiling — percentage scales down with small bankroll.
        """
        static_cents = int(self.config["maxTradeAmount"] * 100)
        pct = self.config.get("maxTradeAmountPct")
        if pct and pct > 0:
            balance = self._get_available_balance()
            if balance > 0:
                dynamic_cents = int(balance * pct)
                return min(static_cents, dynamic_cents)
        return static_cents

    def _effective_max_daily_loss_cents(self):
        """Resolve max daily loss: min(static config, bankroll * pct).

        Static config is the ceiling — percentage scales down with small bankroll.
        """
        static_cents = int(self.config["maxDailyLoss"] * 100)
        pct = self.config.get("maxDailyLossPct")
        if pct and pct > 0:
            balance = self._get_available_balance()
            if balance > 0:
                dynamic_cents = int(balance * pct)
                return min(static_cents, dynamic_cents)
        return static_cents

    # ─── Write-Ahead Log ───

    def _write_wal(self, entry):
        """Write a pending trade entry to the WAL file."""
        entries = self._read_wal()
        entries.append(entry)
        _atomic_write_json(self._wal_path, entries)

    def _read_wal(self):
        """Read all WAL entries. Returns [] if missing/corrupt."""
        if not self._wal_path.exists():
            return []
        try:
            data = json.loads(self._wal_path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _clear_wal(self, order_id):
        """Remove a confirmed entry from the WAL by order_id."""
        entries = self._read_wal()
        entries = [e for e in entries if e.get("order_id") != order_id]
        if entries:
            _atomic_write_json(self._wal_path, entries)
        elif self._wal_path.exists():
            self._wal_path.unlink()

    def _recover_wal(self):
        """On startup, check WAL for trades that were sent but not logged."""
        entries = self._read_wal()
        if not entries:
            return
        self.log.warning("WAL recovery: found %d pending entries", len(entries))
        failed_entries = []
        for entry in entries:
            ticker = entry.get("ticker", "?")
            order_id = entry.get("order_id")
            if not order_id:
                self.log.warning("WAL recovery: entry for %s has no order_id, clearing", ticker)
                continue
            try:
                result = self.client.get(f"/portfolio/orders/{order_id}")
                order = result.get("order", {})
                status = (order.get("status") or "").lower()
                if status in ("filled", "complete", "resting"):
                    self.log.warning("WAL recovery: order %s for %s was %s, writing to trade log",
                                     order_id, ticker, status)
                    record = entry.get("record", {})
                    record["status"] = status
                    record["wal_recovered"] = True
                    save_trade(self.trades_path, record)
                else:
                    self.log.warning("WAL recovery: order %s for %s status=%s, discarding",
                                     order_id, ticker, status)
            except Exception as e:
                self.log.warning("WAL recovery: failed to check order %s: %s", order_id, e)
                failed_entries.append(entry)
        # Only retain entries that failed to verify (transient API errors)
        if failed_entries:
            self.log.warning("WAL recovery: %d entries unverified, retaining for next startup",
                             len(failed_entries))
            _atomic_write_json(self._wal_path, failed_entries)
        elif self._wal_path.exists():
            self._wal_path.unlink()

    @staticmethod
    def _classify_limit_tier(edge):
        """Classify edge into a limit price urgency tier.

        Returns a string: "urgent", "balanced", or "patient".
        """
        if edge is None or edge >= 0.15:
            return "urgent"
        elif edge >= 0.08:
            return "balanced"
        return "patient"

    def _build_golden_record(self, ticker, side, price_cents, count, cost_cents,
                              reasoning, order_info, **extra_fields):
        """Build the canonical trade record with full decision-time context.

        Flattens market snapshot fields and adds settlement placeholders
        for later reconciliation.

        Standard extra_fields that bots SHOULD pass for debuggability:
            model_prob (float): Model's estimated probability
            raw_edge (float): Our prob - market implied prob
            fee_cents (int): Estimated fee in cents
            sizing_method (str): e.g. "half_kelly", "quarter_kelly"
            model_inputs (dict): Upstream data that produced model_prob, e.g.:
                - weather: {"forecast_temp": 85.2, "sigma": 3.1, "days_out": 2, "city": "MIA"}
                - crypto: {"spot_price": 67500, "iv": 0.55, "realized_vol": 0.48, "horizon_min": 720}
                - economics: {"nowcast": 3.1, "sigma": 0.06, "days_to_release": 3}
                - entertainment: {"observed": 150000, "threshold": 100000, "data_sigma": 0.10}
        """
        record = {
            "timestamp": _utc_now_iso(),
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "price_cents": price_cents,
            "count": count,
            "cost_cents": cost_cents,
            "reasoning": reasoning,
            "order_id": order_info.get("order_id"),
            "status": order_info.get("status"),
            "source_bot": self.log.name,
        }
        # Merge extra fields, excluding canonical keys that must not be overridden
        _protected = {"timestamp", "ticker", "action", "side", "source_bot"}
        record.update({k: v for k, v in extra_fields.items() if k not in _protected})

        # Flatten market_snapshot into top-level fields for easy querying
        snapshot = extra_fields.get("market_snapshot", {})
        if snapshot:
            record["best_bid"] = snapshot.get("yes_bid")
            record["best_ask"] = snapshot.get("yes_ask")
            bid = snapshot.get("yes_bid", 0) or 0
            ask = snapshot.get("yes_ask", 0) or 0
            record["spread"] = ask - bid if bid and ask else None
            if "volume" in snapshot:
                record["volume"] = snapshot["volume"]

        # Compute time_to_settle_minutes from market_close_time
        close_time = extra_fields.get("market_close_time")
        if close_time:
            try:
                close_dt = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                delta = close_dt - now
                record["time_to_settle_minutes"] = max(0, int(delta.total_seconds() / 60))
            except (ValueError, TypeError):
                pass

        # Classify limit price tier from edge
        edge = extra_fields.get("raw_edge")
        record["limit_price_rule"] = self._classify_limit_tier(edge)

        # Caps applied tracking
        record["caps_applied"] = extra_fields.get("caps_applied", [])

        # Edge decay tracking (canonical fields derived from bot-provided extras)
        record["edge_at_entry"] = extra_fields.get("raw_edge")
        model_prob = extra_fields.get("model_prob")
        record["model_fair_value_cents"] = round(model_prob * 100, 1) if model_prob is not None else None
        record["model_name"] = extra_fields.get("model_name") or extra_fields.get("sizing_method")

        # Settlement placeholders (filled by reconcile script)
        record.setdefault("settlement_result", None)
        record.setdefault("settlement_revenue_cents", None)
        record.setdefault("fill_price_cents", None)

        return record

    def place_order(self, ticker, side, price_cents, count, reasoning,
                    available_balance_cents=None, market_data_age_seconds=None,
                    **extra_fields):
        """Place a limit order with full safety checks.

        Args:
            ticker: Market ticker string.
            side: "yes" or "no".
            price_cents: Limit price in cents (1-99).
            count: Number of contracts.
            reasoning: Human-readable trade rationale.
            available_balance_cents: Optional balance for pre-trade check.
            market_data_age_seconds: Optional age of market data for staleness check.
            **extra_fields: Additional fields to store in the trade record.

        Returns:
            dict: Order info from API on success (contains 'order_id', 'status').
            None: On any failure. Check log for reason. Failure causes include:
                - invalid_side: Side not 'yes' or 'no'
                - kill_switch: Trading halted via data/HALT_TRADING
                - circuit_breaker: Too many consecutive API failures
                - daily_trade_limit: Max trades per day reached
                - daily_loss_limit: Max daily loss reached
                - dedup: Same ticker traded recently (cooldown)
                - cost_cap: Order cost exceeds max trade amount
                - balance: Insufficient available balance
                - stale_data: Market data too old
                - api_error: Kalshi API returned an error
                - allocator_denied: Capital allocator rejected the request
        """
        self._reset_daily_if_needed()
        self._clear_last_error()
        caps_applied = []

        if side not in ("yes", "no"):
            self.log.error("Invalid side '%s' — must be 'yes' or 'no'", side)
            self._set_last_error("invalid_side", side)
            return None

        # Validate price range
        if price_cents < 1 or price_cents > 99:
            self.log.error("Invalid price_cents=%d — must be 1-99. Skipping %s", price_cents, ticker)
            self._set_last_error("invalid_price", str(price_cents))
            return None
        if count < 1:
            self.log.error("Invalid count=%d — must be >= 1. Skipping %s", count, ticker)
            self._set_last_error("invalid_count", str(count))
            return None

        # 1. Kill switch
        if check_kill_switch(self.kill_switch_path):
            self.log.warning("KILL SWITCH ACTIVE — refusing trade on %s", ticker)
            notify_webhook("Kill switch ACTIVE — trades blocked", level="critical")
            self._set_last_error("kill_switch", "kill switch active")
            return None

        # 1b. Per-bot halt
        if self._per_bot_halt_path and self._per_bot_halt_path.exists():
            self.log.warning("PER-BOT HALT active for %s — refusing trade on %s", self.bot_name, ticker)
            self._set_last_error("per_bot_halt", f"{self.bot_name} halted")
            return None

        # 2. Circuit breaker
        if self.breaker.is_open():
            self.log.warning("Circuit breaker OPEN — skipping trade on %s", ticker)
            self._set_last_error("circuit_breaker", "circuit breaker open")
            return None

        # 3. Daily trade limit
        max_daily = self.config["maxDailyTrades"]
        if self._daily_trades >= max_daily:
            self.log.warning("Daily trade limit (%d) reached — skipping %s", max_daily, ticker)
            self._set_last_error("daily_trade_limit", f"maxDailyTrades={max_daily}")
            return None

        # 5. Dedup
        if self.tracker.is_recent(ticker):
            self.log.info("Skipping %s — traded recently (dedup)", ticker)
            self._set_last_error("dedup", "traded recently")
            return None

        # 6. Cost cap (adjust count down if needed, using risk-adjusted cost)
        max_cost_cents = self._effective_max_trade_cents()
        cost_per_contract = price_cents
        if cost_per_contract * count > max_cost_cents:
            original_count = count
            count = max(1, max_cost_cents // cost_per_contract)
            caps_applied.append("cost_cap")
            self.log.info("Cost cap: %dx → %dx on %s (max $%.2f)",
                          original_count, count, ticker, max_cost_cents / 100)

        # 6a. Daily loss/spend limit, using the post-cap size.
        max_loss_cents = self._effective_max_daily_loss_cents()
        risk_cents = price_cents * count
        if self._daily_spend_cents + risk_cents > max_loss_cents:
            self.log.warning(
                "Daily loss limit ($%.2f) would be exceeded — risked $%.2f + $%.2f > $%.2f. Skipping %s",
                max_loss_cents / 100,
                self._daily_spend_cents / 100, risk_cents / 100,
                max_loss_cents / 100, ticker
            )
            self._set_last_error("daily_loss_limit", f"daily loss limit ${max_loss_cents/100:.0f} reached")
            if self._daily_loss_first_blocked_at is None:
                self._daily_loss_first_blocked_at = time.time()
            self._daily_loss_block_count += 1
            if not self._daily_loss_alerted:
                notify_webhook(
                    f"Daily loss limit (${max_loss_cents/100:.0f}) reached — trades blocked",
                    level="warning",
                )
                self._daily_loss_alerted = True
            blocked_for = time.time() - self._daily_loss_first_blocked_at
            if (
                not self._daily_loss_escalated
                and self._daily_loss_block_count >= 3
                and blocked_for >= 900
            ):
                label = self.bot_name or self.log.name
                notify_webhook(
                    f"{label}: daily loss limit still blocking trades after {int(blocked_for // 60)}m "
                    f"({self._daily_loss_block_count} blocked attempts)",
                    level="warning",
                )
                self._daily_loss_escalated = True
            return None

        # 7. Balance check (optional)
        cost_cents = cost_per_contract * count
        if available_balance_cents is not None and cost_cents > available_balance_cents:
            self.log.warning(
                "Insufficient balance: need %dc but only %dc available. Skipping %s",
                cost_cents, available_balance_cents, ticker
            )
            self._set_last_error("balance", f"need={cost_cents} available={available_balance_cents}")
            return None

        # 8. Stale data check (optional)
        if market_data_age_seconds is not None and market_data_age_seconds > 600:
            self.log.warning(
                "Market data is %.0fs old (>600s stale threshold). Skipping %s",
                market_data_age_seconds, ticker
            )
            self._set_last_error("stale_data", f"age={market_data_age_seconds}")
            return None

        # 8b. Final kill switch re-check
        if check_kill_switch(self.kill_switch_path):
            self.log.warning("KILL SWITCH ACTIVE (late check) — refusing trade on %s", ticker)
            self._set_last_error("kill_switch", "kill switch active")
            return None

        # 9. Build and place order
        order_body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
        }
        if side == "yes":
            order_body["yes_price"] = price_cents
        else:
            order_body["no_price"] = price_cents

        try:
            result = self.client.post("/portfolio/orders", body=order_body)
            order_info = result.get("order", {})
            self.breaker.record_success()
        except requests.exceptions.HTTPError as e:
            self.breaker.record_failure()
            error_code = "api_error"
            error_message = e.response.text[:300] if e.response is not None else str(e)
            if e.response is not None:
                try:
                    payload = e.response.json()
                    err = payload.get("error", {})
                    error_code = err.get("code", error_code)
                    error_message = err.get("message", error_message)
                except ValueError:
                    pass
            self._set_last_error(error_code, error_message)
            self.log.error("Order failed for %s: %s %s", ticker,
                           getattr(e.response, "status_code", "?"), error_message)
            if error_code == "market_not_found":
                alert_key = f"market_not_found:{self.bot_name or self.log.name}:{ticker}"
                if self._should_send_local_alert(alert_key):
                    notify_webhook(
                        f"{self.bot_name or self.log.name}: market_not_found on {ticker}",
                        level="warning",
                        logger=self.log,
                    )
            return None
        except Exception as e:
            self.breaker.record_failure()
            self._set_last_error("api_error", str(e))
            self.log.error("Order failed for %s: %s", ticker, e)
            return None

        # 9b. WAL: write pending entry with order_id for crash recovery
        order_id = order_info.get("order_id")
        extra_fields["caps_applied"] = caps_applied
        trade_record = self._build_golden_record(
            ticker, side, price_cents, count, cost_cents,
            reasoning, order_info, **extra_fields
        )
        order_info["count"] = count
        order_info["cost_cents"] = cost_cents
        order_info["caps_applied"] = list(caps_applied)
        if order_id:
            try:
                self._write_wal({"order_id": order_id, "ticker": ticker, "record": trade_record})
            except Exception:
                pass  # WAL write failure should not block the trade

        # 10. Update counters and save trade (track risk, not raw cost)
        self._daily_trades += 1
        if side == "no":
            self._daily_spend_cents += price_cents * count
        else:
            self._daily_spend_cents += cost_cents
        self._daily_loss_block_count = 0
        self._daily_loss_first_blocked_at = None
        self._daily_loss_escalated = False

        # 11. Register with order monitor for fill tracking
        if self.order_monitor and order_id:
            self.order_monitor.track(order_id, ticker, side, price_cents, count)

        save_trade(self.trades_path, trade_record)
        self.tracker.record(ticker)

        # 12. Clear WAL entry (trade is now safely in the trade log)
        if order_id:
            try:
                self._clear_wal(order_id)
            except Exception:
                pass  # WAL clear failure is benign — recovery will handle it

        self.log.info("Order placed: %dx %s @ %dc on %s (ID: %s, Status: %s)",
                       count, side, price_cents, ticker,
                       order_info.get("order_id"), order_info.get("status"))
        return order_info

    def sell_position(self, ticker, side, price_cents, count, reasoning,
                      order_type="limit", **extra_fields):
        """Sell/exit an existing position with lighter safety checks.

        Exits free capital rather than consuming it, so daily trade limits
        and dedup are skipped. Kill switch, circuit breaker, and sell
        cooldown (10 min per ticker+side) are enforced.

        Args:
            ticker: Market ticker string.
            side: "yes" or "no" — the side we're selling.
            price_cents: Limit price in cents (1-99).
            count: Number of contracts to sell.
            reasoning: Human-readable exit rationale.
            order_type: "limit" (default) or "market". Kalshi API always
                requires a price field; for "market" exits we pass the
                current bid/ask as the price.
            **extra_fields: Additional fields for the trade record.

        Returns:
            Order info dict from API on success, or None if blocked/failed.
        """
        if side not in ("yes", "no"):
            self.log.error("Invalid side '%s' — must be 'yes' or 'no'", side)
            return None

        # 1. Kill switch
        if check_kill_switch(self.kill_switch_path):
            self.log.warning("KILL SWITCH ACTIVE — refusing exit on %s", ticker)
            return None

        # 2. Circuit breaker
        if self.breaker.is_open():
            self.log.warning("Circuit breaker OPEN — skipping exit on %s", ticker)
            return None

        # 3. Sell cooldown — prevent unlimited sells on same position
        cooldown_key = (ticker, side)
        last_sell = self._sell_cooldown.get(cooldown_key)
        if last_sell and (time.time() - last_sell) < self._sell_cooldown_seconds:
            remaining = self._sell_cooldown_seconds - (time.time() - last_sell)
            self.log.warning("Sell cooldown: %s %s — %.0fs remaining", ticker, side, remaining)
            return None

        # Build sell order
        order_body = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "type": order_type,
            "count": count,
        }
        # Kalshi API always requires a price field (no true market orders).
        # For "market" type exits, we pass the current bid as the price.
        if side == "yes":
            order_body["yes_price"] = price_cents
        else:
            order_body["no_price"] = price_cents

        try:
            result = self.client.post("/portfolio/orders", body=order_body)
            order_info = result.get("order", {})
            self.breaker.record_success()
        except requests.exceptions.HTTPError as e:
            self.breaker.record_failure()
            self.log.error("Sell order failed for %s: %s %s", ticker,
                           e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:
            self.breaker.record_failure()
            self.log.error("Sell order failed for %s: %s", ticker, e)
            return None

        # Save exit record via golden record builder
        cost_cents = count * price_cents
        trade_record = self._build_golden_record(
            ticker, side, price_cents, count, cost_cents,
            reasoning, {"order_id": order_info.get("order_id"),
                        "status": order_info.get("status")},
            **extra_fields,
        )
        trade_record["action"] = "sell"
        save_trade(self.trades_path, trade_record)

        # Record sell cooldown timestamp
        self._sell_cooldown[cooldown_key] = time.time()

        self.log.info("EXIT placed: sell %dx %s @ %dc on %s (ID: %s, Status: %s)",
                       count, side, price_cents, ticker,
                       order_info.get("order_id"), order_info.get("status"))
        return order_info

    def log_decision(self, ticker, side, action, reason, edge=None, price_cents=None, **extra):
        """Log a scan decision (trade placed, skipped, or rejected).

        Args:
            ticker: Market ticker.
            side: "yes" or "no".
            action: "placed", "skipped", or "rejected".
            reason: Why this action was taken (e.g., "edge below threshold", "daily limit").
            edge: Optional edge value.
            price_cents: Optional market price.
            **extra: Additional context fields.
        """
        decisions_path = self.trades_path.parent / f"{self.trades_path.stem}-decisions.json"
        record = {
            "timestamp": _utc_now_iso(),
            "ticker": ticker,
            "side": side,
            "action": action,
            "reason": reason,
            "source_bot": self.log.name,
        }
        if edge is not None:
            record["edge"] = round(edge, 4)
        if price_cents is not None:
            record["price_cents"] = price_cents
        record.update(extra)
        save_decision(decisions_path, record)


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
    decisions_path = Path(decisions_path)
    decisions_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = decisions_path.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                decisions = load_trades(decisions_path)
                if len(decisions) >= 5000:
                    decisions = decisions[-4000:]
                decisions.append(decision)
                _atomic_write_json(decisions_path, decisions)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception as e:
        _log.warning("Failed to save decision: %s", e)


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
