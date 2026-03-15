#!/usr/bin/env python3
"""Shared Kalshi API authentication and utilities.

All bots should use this module instead of duplicating auth logic.

Usage:
    from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered

    client = KalshiClient()  # reads from env vars
    data = client.get("/portfolio/balance")
    client.post("/portfolio/orders", body={...})
"""

import json, time, os, sys, logging, datetime, tempfile, fcntl
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from pathlib import Path
from dotenv import load_dotenv

from execution.order_monitor import OrderMonitor as ExecutionOrderMonitor
from execution.trade_manager import (
    RecentTradeTracker as ExecutionRecentTradeTracker,
    TradeManager as ExecutionTradeManager,
    trim_trade_log as execution_trim_trade_log,
    validate_trade_config as execution_validate_trade_config,
)
from infra.kalshi_client import (
    KalshiClient as InfraKalshiClient,
    normalize_market as infra_normalize_market,
    normalize_markets as infra_normalize_markets,
    read_market_cache as infra_read_market_cache,
    write_market_cache as infra_write_market_cache,
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
from ops.notifications import (
    _reset_imessage_rate_limiter as ops_reset_imessage_rate_limiter,
    _reset_webhook_rate_limiter as ops_reset_webhook_rate_limiter,
    notify_imessage as ops_notify_imessage,
    notify_webhook as ops_notify_webhook,
    notify_whatsapp as ops_notify_whatsapp,
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


def normalize_market(m):
    """Compatibility wrapper over the extracted infra.kalshi_client module."""
    return infra_normalize_market(m)


def normalize_markets(markets):
    """Compatibility wrapper over the extracted infra.kalshi_client module."""
    return infra_normalize_markets(markets)


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


class KalshiClient(InfraKalshiClient):
    """Compatibility wrapper over the extracted infra.kalshi_client module."""

    def __init__(self, api_key=None, key_path=None, mode=None):
        super().__init__(
            api_key=api_key,
            key_path=key_path,
            mode=mode,
            project_dir=PROJECT_DIR,
            default_key_path=DEFAULT_KEY_PATH,
            demo_base_url=DEMO_BASE_URL,
            prod_base_url=PROD_BASE_URL,
            max_retries=MAX_RETRIES,
            retry_backoff_base=RETRY_BACKOFF_BASE,
            logger=_log,
            requests_module=requests,
            read_market_cache_func=read_market_cache,
            write_market_cache_func=write_market_cache,
            normalize_market_func=normalize_market,
            normalize_markets_func=normalize_markets,
            market_cache_path=MARKET_CACHE_PATH,
            market_cache_ttl=MARKET_CACHE_TTL,
            time_module=time,
        )

    def _request(self, method: str, path: str, body=None, timeout=15):
        if not hasattr(self, "requests_module"):
            self.requests_module = requests
        if not hasattr(self, "max_retries"):
            self.max_retries = MAX_RETRIES
        if not hasattr(self, "retry_backoff_base"):
            self.retry_backoff_base = RETRY_BACKOFF_BASE
        if not hasattr(self, "_time_module"):
            self._time_module = time
        if not hasattr(self, "_log"):
            self._log = getattr(self, "log", _log)
        return InfraKalshiClient._request(self, method, path, body=body, timeout=timeout)

    def get_market(self, ticker):
        try:
            data = self.get(f"/markets/{ticker}")
            market = data.get("market", data)
            if market:
                normalize_market(market)
            return market
        except Exception as e:
            _log.warning("get_market(%s) failed: %s", ticker, e)
            return None


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

def write_market_cache(markets_by_prefix, *, cache_path=None, atomic_write_json_func=None, time_func=None):
    """Compatibility wrapper over the extracted infra.kalshi_client module."""
    return infra_write_market_cache(
        markets_by_prefix,
        cache_path=cache_path or MARKET_CACHE_PATH,
        atomic_write_json_func=atomic_write_json_func or _atomic_write_json,
        time_func=time_func or time.time,
    )


def read_market_cache(prefix=None, max_age=MARKET_CACHE_TTL, *, cache_path=None, normalize_markets_func=None, time_func=None):
    """Compatibility wrapper over the extracted infra.kalshi_client module."""
    return infra_read_market_cache(
        prefix=prefix,
        max_age=max_age,
        cache_path=cache_path or MARKET_CACHE_PATH,
        normalize_markets_func=normalize_markets_func or normalize_markets,
        time_func=time_func or time.time,
    )


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
    """Compatibility wrapper over the extracted ops.notifications module."""
    return ops_notify_whatsapp(
        message,
        phone=phone,
        logger=logger,
        project_dir=PROJECT_DIR,
    )


# === Webhook Alerting ===


def _reset_webhook_rate_limiter():
    """Compatibility wrapper over the extracted ops.notifications module."""
    return ops_reset_webhook_rate_limiter()


def notify_webhook(message, level="info", logger=None):
    """Compatibility wrapper over the extracted ops.notifications module."""
    return ops_notify_webhook(
        message,
        level=level,
        logger=logger or _log,
        requests_module=requests,
    )


# === iMessage Alerting (BlueBubbles) ===


def _reset_imessage_rate_limiter():
    """Compatibility wrapper over the extracted ops.notifications module."""
    return ops_reset_imessage_rate_limiter()


def notify_imessage(message, logger=None):
    """Compatibility wrapper over the extracted ops.notifications module."""
    return ops_notify_imessage(
        message,
        logger=logger,
        requests_module=requests,
    )
