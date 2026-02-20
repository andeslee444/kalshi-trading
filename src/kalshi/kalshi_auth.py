#!/usr/bin/env python3
"""Shared Kalshi API authentication and utilities.

All bots should use this module instead of duplicating auth logic.

Usage:
    from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered

    client = KalshiClient()  # reads from env vars
    data = client.get("/portfolio/balance")
    client.post("/portfolio/orders", body={...})
"""

import json, time, base64, os, sys, signal, logging, datetime, tempfile
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

# === Constants ===
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_DIR / ".env")
DEFAULT_KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"

DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
PROD_BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds

KILL_SWITCH_PATH = PROJECT_DIR / "data" / "HALT_TRADING"

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


def round_half_up(value):
    """Round a float using arithmetic rounding (0.5 rounds up).

    Python's built-in round() uses banker's rounding. For C-to-F conversion
    and running high comparisons, arithmetic rounding matches NWS behavior.
    """
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


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


def setup_signal_handlers():
    """Install graceful shutdown handlers for SIGTERM and SIGINT."""
    def _handler(signum, frame):
        _log.info("Received signal %s, shutting down gracefully...", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


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
        """Make an authenticated API request with retry on transient errors."""
        url = self.base_url + path
        full_path = "/trade-api/v2" + path
        headers = self._sign(method, full_path)

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
                return r.json()

            except requests.exceptions.ConnectionError as e:
                last_err = e
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                _log.warning("Connection error, retrying in %.1fs... (%s)", wait, e)
                time.sleep(wait)
                headers = self._sign(method, full_path)
            except requests.exceptions.Timeout as e:
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

    def get_all_markets(self, prefix=None, status="open", max_pages=50, cache_ttl=0):
        """Paginate through all open markets, optionally filtering by ticker prefix.

        Args:
            prefix: Only return markets whose ticker starts with this string.
            status: Market status filter (default "open").
            max_pages: Maximum pagination pages to fetch.
            cache_ttl: When >0, return cached results if they are younger than
                       this many seconds. Daemon bots can pass e.g. 300 (5 min)
                       to avoid refetching identical market data every scan.
        """
        cache_key = f"{prefix or ''}:{status}"
        if cache_ttl > 0 and cache_key in self._market_cache:
            cached_time, cached_data = self._market_cache[cache_key]
            if time.time() - cached_time < cache_ttl:
                _log.debug("Market cache hit for %s (%d markets)", cache_key, len(cached_data))
                return cached_data

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

        if cache_ttl > 0:
            self._market_cache[cache_key] = (time.time(), all_markets)

        return all_markets

    def get_balance(self):
        """Get portfolio balance. Returns (balance_cents, available_cents)."""
        data = self.get("/portfolio/balance")
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


def _atomic_write_json(path: Path, data):
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


def save_trade(trades_path: Path, trade: dict):
    """Append a trade to a JSON trades file (atomic write)."""
    trades = load_trades(trades_path)
    trades.append(trade)
    _atomic_write_json(trades_path, trades)


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
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=self.cooldown_hours)
        for t in trades:
            ts_str = t.get("timestamp", "")
            ticker = t.get("ticker", "")
            if not ts_str or not ticker:
                continue
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
                if ts > cutoff:
                    existing = self._recent.get(ticker)
                    if not existing or ts > existing:
                        self._recent[ticker] = ts
            except (ValueError, TypeError):
                pass

    def is_recent(self, ticker):
        """Return True if this ticker was traded within the cooldown window."""
        ts = self._recent.get(ticker)
        if not ts:
            return False
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=self.cooldown_hours)
        return ts > cutoff

    def record(self, ticker):
        """Record a trade on this ticker."""
        self._recent[ticker] = datetime.datetime.now()


# === Kill switch ===

def check_kill_switch(path=None):
    """Return True if the kill switch file exists (trading should halt)."""
    p = Path(path) if path else KILL_SWITCH_PATH
    return p.exists()


# === Circuit breaker ===

class CircuitBreaker:
    """Tracks consecutive API failures and opens after a threshold.

    When open, callers should skip trading until the breaker auto-resets.

    Args:
        max_failures: Consecutive failures before opening (default 5).
        reset_seconds: Seconds to wait before auto-resetting (default 300).
    """

    def __init__(self, max_failures=5, reset_seconds=300):
        self.max_failures = max_failures
        self.reset_seconds = reset_seconds
        self._failures = 0
        self._opened_at = None

    def record_success(self):
        """Record a successful operation — resets the failure counter."""
        self._failures = 0
        self._opened_at = None

    def record_failure(self):
        """Record a failed operation — may open the breaker."""
        self._failures += 1
        if self._failures >= self.max_failures and self._opened_at is None:
            self._opened_at = time.time()

    def is_open(self):
        """Return True if the breaker is open (callers should back off)."""
        if self._failures < self.max_failures:
            return False
        if self._opened_at and (time.time() - self._opened_at) >= self.reset_seconds:
            # Auto-reset after timeout
            self._failures = 0
            self._opened_at = None
            return False
        return True


# === Config validation ===

def validate_trade_config(config, bot_name=""):
    """Validate trade-related config values. Raises ValueError on bad config."""
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


# === Trade log trimming ===

def trim_trade_log(trades_path, max_age_days=90, max_entries=5000):
    """Remove old entries from a trade log file.

    Keeps only entries newer than max_age_days and limits total to max_entries.
    """
    trades = load_trades(trades_path)
    if not trades:
        return

    cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age_days)
    filtered = []
    for t in trades:
        ts_str = t.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
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
                 kill_switch_path=None, cooldown_hours=6):
        validate_trade_config(config)
        self.client = client
        self.trades_path = Path(trades_path)
        self.config = config
        self.log = logger or _log
        self.kill_switch_path = kill_switch_path or KILL_SWITCH_PATH
        self.tracker = RecentTradeTracker(self.trades_path, cooldown_hours=cooldown_hours)
        self.breaker = CircuitBreaker()

        # Daily counters
        self._daily_trades = 0
        self._daily_spend_cents = 0
        self._daily_date = None

    def _reset_daily_if_needed(self):
        today = datetime.date.today().isoformat()
        if self._daily_date != today:
            self._daily_trades = 0
            self._daily_spend_cents = 0
            self._daily_date = today

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
        """
        record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": ticker,
            "side": side,
            "price_cents": price_cents,
            "count": count,
            "cost_cents": cost_cents,
            "reasoning": reasoning,
            "order_id": order_info.get("order_id"),
            "status": order_info.get("status"),
            "source_bot": self.log.name,
        }
        # Merge all extra fields (model_prob, raw_edge, fee_cents, sizing_method, etc.)
        record.update(extra_fields)

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
            Order info dict from API on success, or None if blocked/failed.
        """
        self._reset_daily_if_needed()
        caps_applied = []

        if side not in ("yes", "no"):
            self.log.error("Invalid side '%s' — must be 'yes' or 'no'", side)
            return None

        # 1. Kill switch
        if check_kill_switch(self.kill_switch_path):
            self.log.warning("KILL SWITCH ACTIVE — refusing trade on %s", ticker)
            return None

        # 2. Circuit breaker
        if self.breaker.is_open():
            self.log.warning("Circuit breaker OPEN — skipping trade on %s", ticker)
            return None

        # 3. Daily trade limit
        max_daily = self.config["maxDailyTrades"]
        if self._daily_trades >= max_daily:
            self.log.warning("Daily trade limit (%d) reached — skipping %s", max_daily, ticker)
            return None

        # 4. Daily loss/spend limit (risk-adjusted: YES risk = cost, NO risk = 100-price per contract)
        max_loss_cents = int(self.config["maxDailyLoss"] * 100)
        if side == "no":
            # Buying NO: max loss per contract is (100 - no_price) cents
            risk_per_contract = 100 - price_cents
        else:
            # Buying YES: max loss per contract is price_cents
            risk_per_contract = price_cents
        risk_cents = risk_per_contract * count
        if self._daily_spend_cents + risk_cents > max_loss_cents:
            self.log.warning(
                "Daily loss limit ($%.2f) would be exceeded — risked $%.2f + $%.2f > $%.2f. Skipping %s",
                self.config["maxDailyLoss"],
                self._daily_spend_cents / 100, risk_cents / 100,
                max_loss_cents / 100, ticker
            )
            return None

        # 5. Dedup
        if self.tracker.is_recent(ticker):
            self.log.info("Skipping %s — traded recently (dedup)", ticker)
            return None

        # 6. Cost cap (adjust count down if needed, using risk-adjusted cost)
        max_cost_cents = int(self.config["maxTradeAmount"] * 100)
        cost_per_contract = price_cents
        if cost_per_contract * count > max_cost_cents:
            count = max(1, max_cost_cents // cost_per_contract)
            caps_applied.append("cost_cap")

        # 7. Balance check (optional)
        cost_cents = cost_per_contract * count
        if available_balance_cents is not None and cost_cents > available_balance_cents:
            self.log.warning(
                "Insufficient balance: need %dc but only %dc available. Skipping %s",
                cost_cents, available_balance_cents, ticker
            )
            return None

        # 8. Stale data check (optional)
        if market_data_age_seconds is not None and market_data_age_seconds > 600:
            self.log.warning(
                "Market data is %.0fs old (>600s stale threshold). Skipping %s",
                market_data_age_seconds, ticker
            )
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
            self.log.error("Order failed for %s: %s %s", ticker,
                           e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:
            self.breaker.record_failure()
            self.log.error("Order failed for %s: %s", ticker, e)
            return None

        # 10. Update counters and save trade (track risk, not raw cost)
        self._daily_trades += 1
        if side == "no":
            self._daily_spend_cents += (100 - price_cents) * count
        else:
            self._daily_spend_cents += cost_cents

        extra_fields["caps_applied"] = caps_applied
        trade_record = self._build_golden_record(
            ticker, side, price_cents, count, cost_cents,
            reasoning, order_info, **extra_fields
        )
        save_trade(self.trades_path, trade_record)
        self.tracker.record(ticker)

        self.log.info("Order placed: %dx %s @ %dc on %s (ID: %s, Status: %s)",
                       count, side, price_cents, ticker,
                       order_info.get("order_id"), order_info.get("status"))
        return order_info

    def sell_position(self, ticker, side, price_cents, count, reasoning,
                      **extra_fields):
        """Sell/exit an existing position with lighter safety checks.

        Exits free capital rather than consuming it, so daily trade limits
        and dedup are skipped. Only kill switch + circuit breaker enforced.

        Args:
            ticker: Market ticker string.
            side: "yes" or "no" — the side we're selling.
            price_cents: Limit price in cents (1-99).
            count: Number of contracts to sell.
            reasoning: Human-readable exit rationale.
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

        # Build sell order
        order_body = {
            "ticker": ticker,
            "action": "sell",
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
            "timestamp": datetime.datetime.now().isoformat(),
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
    """Append a scan decision to the decisions log (atomic write)."""
    decisions = load_trades(decisions_path)  # reuse same JSON array format
    # Keep log bounded — retain last 500 decisions
    if len(decisions) >= 500:
        decisions = decisions[-400:]
    decisions.append(decision)
    _atomic_write_json(decisions_path, decisions)


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

    def __init__(self, state_path=None, staleness_minutes=60, auto_halt=False, logger=None):
        self.state_path = Path(state_path) if state_path else HEALTH_STATE_PATH
        self.staleness_minutes = staleness_minutes
        self.auto_halt = auto_halt
        self.log = logger or _log
        self._state = {
            "sources": {},      # source -> {"last_success": ts, "last_error": ts, "error_count": int}
            "bots": {},         # bot -> {"last_heartbeat": ts}
        }
        self._load()

    def _load(self):
        if self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_write_json(self.state_path, self._state)
        except Exception as e:
            self.log.warning("Failed to save health state: %s", e)

    def record_source_success(self, source):
        """Record a successful data source fetch."""
        if source not in self._state["sources"]:
            self._state["sources"][source] = {"last_success": None, "last_error": None, "error_count": 0}
        self._state["sources"][source]["last_success"] = datetime.datetime.now().isoformat()
        self._state["sources"][source]["error_count"] = 0
        self._save()

    def record_source_error(self, source, msg=""):
        """Record a data source error."""
        if source not in self._state["sources"]:
            self._state["sources"][source] = {"last_success": None, "last_error": None, "error_count": 0}
        self._state["sources"][source]["last_error"] = datetime.datetime.now().isoformat()
        self._state["sources"][source]["error_count"] = self._state["sources"][source].get("error_count", 0) + 1
        self._save()

    def record_bot_heartbeat(self, bot):
        """Record a bot heartbeat (proves the bot loop is running)."""
        self._state["bots"][bot] = {"last_heartbeat": datetime.datetime.now().isoformat()}
        self._save()

    def check_health(self, staleness_minutes=None):
        """Check for health issues. Returns list of issue strings.

        Issues:
          - Bot stale: no heartbeat for > staleness_minutes
          - Source errors: consecutive error count > 5
        """
        stale_min = staleness_minutes or self.staleness_minutes
        now = datetime.datetime.now()
        issues = []

        # Check bot staleness
        for bot, info in self._state.get("bots", {}).items():
            hb = info.get("last_heartbeat")
            if hb:
                try:
                    hb_dt = datetime.datetime.fromisoformat(hb)
                    age_min = (now - hb_dt).total_seconds() / 60
                    if age_min > stale_min:
                        issues.append(f"bot/{bot} stale: last heartbeat {age_min:.0f}min ago")
                except (ValueError, TypeError):
                    pass

        # Check source errors
        for source, info in self._state.get("sources", {}).items():
            error_count = info.get("error_count", 0)
            if error_count >= 5:
                issues.append(f"source/{source} failing: {error_count} consecutive errors")

        # Auto-halt on critical failure
        if self.auto_halt and issues:
            critical = [i for i in issues if "stale" in i or "failing" in i]
            if len(critical) >= 2:
                halt_path = KILL_SWITCH_PATH
                if not halt_path.exists():
                    halt_path.parent.mkdir(parents=True, exist_ok=True)
                    halt_path.write_text(f"Auto-halted: {'; '.join(critical)}")
                    self.log.warning("AUTO-HALT triggered: %s", "; ".join(critical))
                    issues.append("AUTO-HALT: HALT_TRADING file created")

        return issues


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
