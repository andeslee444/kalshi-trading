#!/usr/bin/env python3
"""Kalshi Weather Trading Bot - Demo Paper Trading
Scans KXHIGH temperature markets, compares to Open-Meteo forecasts, and places trades on edge.
"""

import json, time, datetime, os, sys, re, threading, hashlib
import requests
from pathlib import Path
from zoneinfo import ZoneInfo
from app_bootstrap import AppContext, install_app_context
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot, HealthCheckMonitor, OrderMonitor, ScanSummary, is_shutdown_requested, CITY_TIMEZONES, _local_today, normalize_markets
from probability import weather_probability, weather_sigma, weather_sigma_hourly, ensemble_weather_probability, ensemble_spread_sigma_multiplier, ensemble_weather_probability_v2, empirical_ensemble_probability, half_kelly, quarter_kelly, high_conviction_kelly, compute_limit_price, kalshi_fee_cents, is_market_liquid, _load_calibration
from ticker_utils import parse_weather_ticker as parse_ticker
from capital_allocator import PortfolioAllocator
from forecast_verifier import ForecastVerifier, DEFAULT_STATION_MAP, NWSCrossCheckVerifier
from weather_data import EnsembleCollector, HRRRFetcher, NAMFetcher, PreviousRunsFetcher, OrderBookDepth, next_model_run, latest_available_model_run, canonical_model_name, open_meteo_model_name, STATION_MAP, NWSForecastFetcher, BiasCorrector
from singleton_lock import acquire_process_singleton, release_process_singleton

# === Config ===
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
WEATHER_SINGLETON_LOCK_PATH = PROJECT_DIR / "data" / "pids" / "weather-bot.lock"

_APP_CONTEXT = None
log = None
config = {}
CITIES = {}
client = None
allocator = None
health = None
order_monitor = None
trade_manager = None


def _release_singleton_lock():
    """Release the weather singleton lock if held by this process."""
    release_process_singleton("weather")


def _acquire_singleton_lock(lock_path=None):
    """Ensure only one weather bot instance can run at a time."""
    return acquire_process_singleton(
        "weather",
        PROJECT_DIR,
        log,
        lock_path=lock_path or WEATHER_SINGLETON_LOCK_PATH,
        display_name="weather bot",
    )


# === Days-out-aware dedup cooldown ===
# Local dedup layer on top of TradeManager's RecentTradeTracker.
# Day-0 markets should re-evaluate frequently (NWS updates every 1-2h),
# while day-3+ markets don't need to be re-traded for 12 hours.

_local_trade_times = {}  # ticker -> datetime of last trade
_weather_market_cache = {"fetched_at": 0.0, "markets": []}


def get_dedup_cooldown(days_out):
    """Return dedup cooldown in seconds based on days until settlement.

    Shorter cooldown for nearer-term markets where forecast accuracy
    improves rapidly with new data (NWS, HRRR updates).
    """
    if days_out == 0:
        return 1800    # 30 min -- NWS updates frequently
    elif days_out == 1:
        return 7200    # 2 hours
    elif days_out == 2:
        return 14400   # 4 hours
    else:
        return 43200   # 12 hours (original default)


def is_locally_deduped(ticker, days_out):
    """Check if this ticker was traded too recently given its days_out.

    Returns True if the ticker should be skipped (still in cooldown).
    """
    last_trade = _local_trade_times.get(ticker)
    if not last_trade:
        return False
    cooldown = get_dedup_cooldown(days_out)
    elapsed = (datetime.datetime.now() - last_trade).total_seconds()
    return elapsed < cooldown


def record_local_trade(ticker):
    """Record that we traded this ticker (for local dedup tracking)."""
    _local_trade_times[ticker] = datetime.datetime.now()
    # Prune old entries to prevent unbounded growth
    if len(_local_trade_times) > 500:
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=24)
        expired = [k for k, v in _local_trade_times.items() if v < cutoff]
        for k in expired:
            del _local_trade_times[k]


def _city_now(city_code):
    """Return current datetime in the market city's local timezone."""
    tz = ZoneInfo(CITY_TIMEZONES.get(city_code, "America/New_York"))
    return datetime.datetime.now(tz)


def _city_today(city_code):
    """Return today's local settlement date for a city."""
    try:
        return datetime.date.fromisoformat(_local_today(city_code))
    except Exception:
        return _city_now(city_code).date()


def _effective_weather_edge_threshold():
    """Use the stricter of config and calibrated recommendations."""
    recommended = _load_calibration().get("weather", {}).get("recommended_edge_threshold")
    base = config.get("edgeThreshold", 0.06)
    if isinstance(recommended, (int, float)) and recommended > 0:
        return max(base, recommended)
    return base


def _resolve_optional_project_path(path_str, project_dir=None):
    if not path_str:
        return None
    path = Path(path_str)
    if path.is_absolute():
        return path
    return Path(project_dir or PROJECT_DIR) / path


def get_weather_markets(cache_ttl=600):
    """Fetch weather markets directly by city series.

    Prefix-scanning all open markets can miss KXHIGH contracts when they sit
    beyond the first pagination window. Weather series are stable and bounded,
    so querying each configured city series is both faster and more reliable.
    """
    now = time.time()
    cached_markets = _weather_market_cache.get("markets", [])
    if cached_markets and (now - _weather_market_cache.get("fetched_at", 0.0)) < cache_ttl:
        return list(cached_markets)

    markets = []
    seen = set()
    series_failures = 0

    for city_code in CITIES:
        series_ticker = f"KXHIGH{city_code}"
        cursor = None
        while True:
            path = f"/markets?series_ticker={series_ticker}&status=open&limit=1000"
            if cursor:
                path += f"&cursor={cursor}"
            try:
                data = client.get(path)
            except Exception as e:
                series_failures += 1
                log.warning("Weather market fetch failed for %s: %s", series_ticker, e)
                break

            batch = normalize_markets(data.get("markets", []))
            for market in batch:
                ticker = market.get("ticker")
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    markets.append(market)

            cursor = data.get("cursor")
            if not cursor or not batch:
                break

    if not markets and series_failures:
        log.warning("Series-based weather market discovery returned no markets; falling back to prefix scan")
        return client.get_all_markets(prefix="KXHIGH", cache_ttl=cache_ttl)

    _weather_market_cache["fetched_at"] = now
    _weather_market_cache["markets"] = list(markets)
    return markets

# === Weather Forecast ===

# Ensemble model endpoints for Open-Meteo
ENSEMBLE_MODELS = {
    "gfs": "gfs_seamless",
    "ecmwf": "ecmwf_ifs025",       # 0.25-degree (9km), was incorrectly ifs04
    "icon": "icon_seamless",
    "nbm": "nbm_conus",            # NOAA bias-corrected consensus, 2.5km
    "aifs": "ecmwf_aifs025",       # ECMWF AI model
    "graphcast": "gfs_graphcast025",  # DeepMind AI model
}
ENSEMBLE_ENABLED = False

# === Open-Meteo API Configuration ===
# Premium API: set OPEN_METEO_API_KEY in .env for higher rate limits and priority.
# NOAA-family models (GFS/HRRR/NBM/NAM/GraphCast) are served from the GFS API.
_OPEN_METEO_API_KEY = os.environ.get("OPEN_METEO_API_KEY", "")
_OPEN_METEO_FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
_OPEN_METEO_PREMIUM_FORECAST_BASE = "https://customer-api.open-meteo.com/v1/forecast"
_OPEN_METEO_GFS_BASE = "https://api.open-meteo.com/v1/gfs"
_OPEN_METEO_PREMIUM_GFS_BASE = "https://customer-api.open-meteo.com/v1/gfs"
_GFS_API_MODELS = {
    "gfs_seamless",
    "gfs_graphcast025",
    "hrrr_conus",
    "ncep_hrrr_conus",
    "nbm_conus",
    "ncep_nbm_conus",
    "nam_conus",
    "ncep_nam_conus",
}
_endpoint_route_warned = set()
if _OPEN_METEO_API_KEY:
    OPEN_METEO_BASE = _OPEN_METEO_PREMIUM_FORECAST_BASE
else:
    OPEN_METEO_BASE = _OPEN_METEO_FORECAST_BASE


# === Rate Limiter ===

class RateLimiter:
    """Token-bucket rate limiter for API requests (max N req/s with backoff)."""

    def __init__(self, max_per_second=5, burst=5):
        self._lock = threading.Lock()
        self._max_per_second = max_per_second
        self._tokens = burst
        self._last_refill = time.monotonic()
        self._interval = 1.0 / max_per_second

    def acquire(self):
        """Block until a token is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._max_per_second, self._tokens + elapsed * self._max_per_second)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            time.sleep(self._interval)


# Premium API allows higher rate limits
_open_meteo_limiter = None


def _open_meteo_request_target(model_name=None):
    resolved_model = open_meteo_model_name(model_name, api_key=_OPEN_METEO_API_KEY)
    if resolved_model in _GFS_API_MODELS:
        route_key = (model_name, resolved_model)
        if route_key not in _endpoint_route_warned:
            target = "premium gfs" if _OPEN_METEO_API_KEY else "public gfs"
            if model_name and model_name != resolved_model:
                log.info("Open-Meteo routing: %s -> %s uses %s endpoint", model_name, resolved_model, target)
            else:
                log.info("Open-Meteo routing: %s uses %s endpoint", resolved_model, target)
            _endpoint_route_warned.add(route_key)
        if _OPEN_METEO_API_KEY:
            return _OPEN_METEO_PREMIUM_GFS_BASE, _OPEN_METEO_API_KEY
        return _OPEN_METEO_GFS_BASE, ""
    if _OPEN_METEO_API_KEY:
        return OPEN_METEO_BASE, _OPEN_METEO_API_KEY
    return _OPEN_METEO_FORECAST_BASE, ""


def _open_meteo_url(params, model_name=None):
    """Build Open-Meteo URL with API key if configured."""
    base, api_key = _open_meteo_request_target(model_name=model_name)
    url = f"{base}?{params}"
    if api_key:
        url += f"&apikey={api_key}"
    return url


def _rate_limited_request(url, timeout=15, max_retries=2):
    """Open-Meteo request with rate limiting."""
    _open_meteo_limiter.acquire()
    return retry_request("GET", url, timeout=timeout, max_retries=max_retries)


# Forecast response cache — reduces redundant Open-Meteo calls within scan cycles
_forecast_cache = {}  # key -> (timestamp, response_data)
_FORECAST_CACHE_TTL = 300  # 5 minutes — covers a full scan cycle


def _cached_request(url, timeout=15, max_retries=2):
    """Rate-limited Open-Meteo request with TTL caching."""
    now = time.monotonic()
    # Evict stale entries periodically
    if len(_forecast_cache) > 100:
        stale = [k for k, (ts, _) in _forecast_cache.items() if now - ts > _FORECAST_CACHE_TTL]
        for k in stale:
            del _forecast_cache[k]

    if url in _forecast_cache:
        ts, data = _forecast_cache[url]
        if now - ts < _FORECAST_CACHE_TTL:
            return data

    resp = _rate_limited_request(url, timeout=timeout, max_retries=max_retries)
    if resp is not None and resp.status_code == 200:
        _forecast_cache[url] = (now, resp)
    return resp


def _record_source_failure(source, message, status_code=None, immediate_on_bad_request=False):
    """Record source failures and trip breakers immediately on deterministic 400s."""
    if immediate_on_bad_request and status_code == 400 and hasattr(health, "trip_source_breaker"):
        health.trip_source_breaker(source, message)
        log.warning("%s returned HTTP 400, opening circuit breaker immediately", source)
        return
    health.record_source_error(source, message)


# === Forecast Verification ===
VERIFICATION_ENABLED = True
VERIFICATION_CONFIG = {}
bias_cfg = {}
nws_cfg = {}
verifier = None
nws_audit_enabled = False
nws_audit_path = None
nws_crosscheck_verifier = None
ensemble_collector = None
hrrr_fetcher = None
nam_fetcher = None
bias_corrector = None
nws_fetcher = None
orderbook = None


def get_forecast(lat, lon):
    """Single-model GFS forecast (fallback)."""
    url = _open_meteo_url(f"latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=auto&forecast_days=14")
    r = _cached_request(url, timeout=10)
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))


def get_batch_forecasts(cities_dict):
    """Batch forecast for all cities in a single Open-Meteo API call.

    Open-Meteo supports comma-separated lat/lon for multi-location requests.
    Returns {city_code: {date: temp}} for all cities, or empty dict on failure.
    """
    codes = list(cities_dict.keys())
    lats = ",".join(str(cities_dict[c]["lat"]) for c in codes)
    lons = ",".join(str(cities_dict[c]["lon"]) for c in codes)

    url = _open_meteo_url(
        f"latitude={lats}&longitude={lons}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&timezone=auto&forecast_days=14"
    )

    try:
        r = _cached_request(url, timeout=30)
        data = r.json()

        # Multi-location returns a list of results
        if isinstance(data, list):
            results = {}
            for i, city_data in enumerate(data):
                if i >= len(codes):
                    break
                d = city_data.get("daily", {})
                if d and "time" in d and "temperature_2m_max" in d:
                    results[codes[i]] = dict(zip(d["time"], d["temperature_2m_max"]))
            return results
        else:
            # Single location response (only 1 city)
            d = data.get("daily", {})
            if d and "time" in d and "temperature_2m_max" in d:
                return {codes[0]: dict(zip(d["time"], d["temperature_2m_max"]))}
    except Exception as e:
        log.error(f"Batch forecast failed: {e}")

    return {}


def get_batch_ensemble_forecasts(cities_dict):
    """Batch ensemble forecast for all cities in a single API call per model.

    Fetches all models in ENSEMBLE_MODELS (GFS, ECMWF, ICON, NBM, AIFS, GraphCast).
    Returns {city_code: {date: {model: temp}}} using 1 API call per model.
    """
    codes = list(cities_dict.keys())
    lats = ",".join(str(cities_dict[c]["lat"]) for c in codes)
    lons = ",".join(str(cities_dict[c]["lon"]) for c in codes)

    def _fetch_single_model_forecasts(model_name):
        """Fallback for model endpoints that reject multi-location requests."""
        city_forecasts = {}
        request_model_name = open_meteo_model_name(model_name, api_key=_OPEN_METEO_API_KEY)
        for code in codes:
            info = cities_dict[code]
            url = _open_meteo_url(
                f"latitude={info['lat']}&longitude={info['lon']}"
                f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
                f"&timezone=auto&forecast_days=14"
                f"&models={request_model_name}",
                model_name=request_model_name,
            )
            r = _cached_request(url, timeout=30)
            data = r.json()
            d = data.get("daily", {})
            if d and "time" in d and "temperature_2m_max" in d:
                city_forecasts[code] = dict(zip(d["time"], d["temperature_2m_max"]))
        return city_forecasts

    # Fetch each model in a single batch request
    model_results = {}  # model_key -> {city_code: {date: temp}}
    for model_key, model_name in ENSEMBLE_MODELS.items():
        if health.is_source_open(f"open-meteo-{model_key}"):
            log.warning(f"Circuit breaker open for {model_key}, skipping")
            continue
        request_model_name = open_meteo_model_name(model_name, api_key=_OPEN_METEO_API_KEY)

        url = _open_meteo_url(
            f"latitude={lats}&longitude={lons}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=auto&forecast_days=14"
            f"&models={request_model_name}",
            model_name=request_model_name,
        )

        try:
            r = _cached_request(url, timeout=30)
            data = r.json()

            city_forecasts = {}
            if isinstance(data, list):
                for i, city_data in enumerate(data):
                    if i >= len(codes):
                        break
                    d = city_data.get("daily", {})
                    if d and "time" in d and "temperature_2m_max" in d:
                        city_forecasts[codes[i]] = dict(zip(d["time"], d["temperature_2m_max"]))
            else:
                d = data.get("daily", {})
                if d and "time" in d and "temperature_2m_max" in d:
                    city_forecasts[codes[0]] = dict(zip(d["time"], d["temperature_2m_max"]))

            model_results[model_key] = city_forecasts
            health.record_source_success(f"open-meteo-{model_key}")
        except Exception as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 400 and len(codes) > 1:
                log.warning(
                    "Batch ensemble %s failed with HTTP 400; retrying per-city",
                    model_key,
                )
                try:
                    city_forecasts = _fetch_single_model_forecasts(model_name)
                except Exception as fallback_error:
                    log.warning(f"Per-city ensemble fallback {model_key} failed: {fallback_error}")
                    _record_source_failure(
                        f"open-meteo-{model_key}",
                        str(fallback_error),
                        status_code=getattr(getattr(fallback_error, "response", None), "status_code", None),
                        immediate_on_bad_request=True,
                    )
                    continue
                if city_forecasts:
                    model_results[model_key] = city_forecasts
                    health.record_source_success(f"open-meteo-{model_key}")
                    continue
                log.warning("Per-city ensemble fallback %s returned no data", model_key)
                _record_source_failure(
                    f"open-meteo-{model_key}",
                    "per-city fallback returned no data",
                    status_code=status_code,
                    immediate_on_bad_request=False,
                )
                continue
            log.warning(f"Batch ensemble {model_key} failed: {e}")
            _record_source_failure(
                f"open-meteo-{model_key}",
                str(e),
                status_code=status_code,
                immediate_on_bad_request=True,
            )

    if not model_results:
        return {}

    # Combine: {city_code: {date: {model: temp}}}
    combined = {}
    for code in codes:
        city_data = {}
        all_dates = set()
        for model_key, city_forecasts in model_results.items():
            if code in city_forecasts:
                all_dates.update(city_forecasts[code].keys())

        for date_str in sorted(all_dates):
            day = {}
            for model_key, city_forecasts in model_results.items():
                if code in city_forecasts and date_str in city_forecasts[code]:
                    day[model_key] = city_forecasts[code][date_str]
            if day:
                city_data[date_str] = day

        if city_data:
            combined[code] = city_data

    return combined


def get_ensemble_forecast(lat, lon):
    """Fetch GFS, ECMWF, and ICON forecasts with per-model error recovery.

    Returns dict: {date_str: {"gfs": temp, "ecmwf": temp, "icon": temp}}
    Graceful degradation: if one or two models fail, returns the remainder.
    Only falls back to single-model GFS if ALL models fail.
    Per-model circuit breaker tracking so one model's failure doesn't block others.
    """
    from kalshi_auth import fetch_parallel

    # Skip models whose circuit breaker is open
    urls = {}
    skipped_models = []
    for model_key, model_name in ENSEMBLE_MODELS.items():
        if health.is_source_open(f"open-meteo-{model_key}"):
            skipped_models.append(model_key)
            continue
        url = _open_meteo_url(
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=auto&forecast_days=14"
            f"&models={model_name}",
            model_name=model_name,
        )
        urls[url] = model_key

    if skipped_models:
        log.info("Skipping models with open circuit breaker: %s", ", ".join(skipped_models))

    if not urls:
        log.warning("All ensemble model circuit breakers open, falling back to single GFS")
        try:
            single = get_forecast(lat, lon)
            return {date: {"gfs": temp} for date, temp in single.items()}
        except Exception:
            return {}

    responses = fetch_parallel(list(urls.keys()), timeout=15)

    # Parse each model's response with per-model health tracking
    model_forecasts = {}  # model_key -> {date: temp}
    failed_models = []
    for url, response in responses.items():
        model_key = urls[url]
        if response is None or response.status_code != 200:
            log.warning(f"Ensemble model {model_key} failed (status={getattr(response, 'status_code', 'None')})")
            _record_source_failure(
                f"open-meteo-{model_key}",
                f"status={getattr(response, 'status_code', 'None')}",
                status_code=getattr(response, "status_code", None),
                immediate_on_bad_request=True,
            )
            failed_models.append(model_key)
            continue
        try:
            d = response.json()["daily"]
            model_forecasts[model_key] = dict(zip(d["time"], d["temperature_2m_max"]))
            health.record_source_success(f"open-meteo-{model_key}")
        except (KeyError, ValueError) as e:
            log.warning(f"Ensemble model {model_key} parse error: {e}")
            health.record_source_error(f"open-meteo-{model_key}", str(e))
            failed_models.append(model_key)

    if not model_forecasts:
        log.warning("All ensemble models failed, falling back to single GFS")
        try:
            single = get_forecast(lat, lon)
            return {date: {"gfs": temp} for date, temp in single.items()}
        except Exception:
            return {}

    if failed_models:
        log.info("Ensemble degraded: %d/%d models available (%s failed)",
                 len(model_forecasts), len(ENSEMBLE_MODELS), ", ".join(failed_models))

    # Combine into {date: {model: temp}} structure
    all_dates = set()
    for forecasts in model_forecasts.values():
        all_dates.update(forecasts.keys())

    combined = {}
    for date in sorted(all_dates):
        day_data = {}
        for model_key, forecasts in model_forecasts.items():
            if date in forecasts:
                day_data[model_key] = forecasts[date]
        if day_data:  # Only include dates with at least one model's data
            combined[date] = day_data

    return combined

# === Trading ===

def choose_order_type(yes_bid, yes_ask, side, edge, our_prob, depth_data=None):
    """Choose between limit and market order based on spread width and depth.

    Returns:
        Tuple of (order_type, price_cents) where order_type is "limit" or "market".

    Logic:
    - Wide spread (>5c) or thin depth (<100 contracts): use limit at model fair value
    - Tight spread and good depth: use market (take the ask)
    - Very high edge (>20%): always market (urgency, fill certainty matters)
    """
    spread = (yes_ask - yes_bid) if (yes_ask and yes_bid) else 0

    # Very high edge -> market order for fill certainty
    if edge > 0.20:
        if side == "yes":
            return "market", yes_ask
        else:
            return "market", 100 - yes_bid if yes_bid else yes_ask

    # Check depth if available
    thin_book = False
    if depth_data:
        side_depth = depth_data.get("total_ask_depth", 0) if side == "yes" else depth_data.get("total_bid_depth", 0)
        if side_depth < 100:
            thin_book = True

    # Wide spread or thin book -> limit order at model fair value
    if spread > 5 or thin_book:
        model_price = int(our_prob * 100) if side == "yes" else int((1 - our_prob) * 100)
        # Clamp to be competitive: between bid+1 and ask-1
        if side == "yes":
            limit_price = max(yes_bid + 1 if yes_bid else 1, min(model_price, yes_ask - 1 if yes_ask > 1 else yes_ask))
        else:
            no_bid = 100 - yes_ask if yes_ask else 0
            no_ask = 100 - yes_bid if yes_bid else 100
            limit_price = max(no_bid + 1 if no_bid else 1, min(model_price, no_ask - 1 if no_ask > 1 else no_ask))
        return "limit", max(1, limit_price)

    # Default: use compute_limit_price for edge-tiered placement
    price = compute_limit_price(yes_bid, yes_ask, side, edge=edge)
    if not price or price <= 0:
        price = yes_ask if side == "yes" else (100 - yes_bid if yes_bid else 0)
    return "market", price


def _maker_execution_config():
    raw = config.get("makerExecution", {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "maxDaysOut": int(raw.get("maxDaysOut", 1)),
        "minEdgeNearTerm": float(raw.get("minEdgeNearTerm", 0.10)),
        "minEdgeFar": float(raw.get("minEdgeFar", 0.14)),
        "minImprovementCents": int(raw.get("minImprovementCents", 1)),
        "maxJoinUpliftCents": int(raw.get("maxJoinUpliftCents", 18)),
        "pricePriorityEdgeBuffer": float(raw.get("pricePriorityEdgeBuffer", 0.02)),
        "maxPriceCents": int(raw.get("maxPriceCents", 95)),
    }


def _side_quotes(market, side):
    yes_bid = int(market.get("yes_bid", 0) or 0)
    yes_ask = int(market.get("yes_ask", 0) or 0)
    no_bid = int(market.get("no_bid", 0) or (100 - yes_ask if yes_ask else 0))
    no_ask = int(market.get("no_ask", 0) or (100 - yes_bid if yes_bid else 0))
    if side == "yes":
        return {"bid": yes_bid, "ask": yes_ask}
    return {"bid": no_bid, "ask": no_ask}


def _maker_min_edge(days_out, maker_cfg=None):
    cfg = maker_cfg or _maker_execution_config()
    if days_out is not None and days_out <= cfg["maxDaysOut"]:
        return cfg["minEdgeNearTerm"]
    return cfg["minEdgeFar"]


def _build_displayed_entry_plan(market, side, side_prob, liquid):
    quotes = _side_quotes(market, side)
    ask = quotes["ask"]
    if not liquid or not ask or ask >= 99:
        return None
    edge = side_prob - (ask / 100.0)
    return {
        "execution_mode": "taker",
        "price": ask,
        "edge": edge,
        "bid": quotes["bid"],
        "ask": ask,
    }


def _build_maker_entry_plan(market, side, side_prob, days_out, maker_cfg=None):
    cfg = maker_cfg or _maker_execution_config()
    if not cfg.get("enabled", True):
        return None
    if days_out is not None and days_out > cfg["maxDaysOut"]:
        return None

    quotes = _side_quotes(market, side)
    bid = quotes["bid"]
    ask = quotes["ask"]
    volume = int(market.get("volume", 0) or 0)
    if not bid and not ask and volume <= 0:
        return None

    side_prob = max(0.0, min(1.0, float(side_prob)))
    fair_value_cents = side_prob * 100.0
    min_edge = _maker_min_edge(days_out, cfg)
    target_price = int(fair_value_cents - min_edge * 100.0)
    target_price = max(1, min(cfg["maxPriceCents"], target_price))

    if bid and ask:
        if ask - bid > 1:
            floor = bid + cfg["minImprovementCents"]
            ceiling = ask - 1
        else:
            floor = max(1, bid)
            ceiling = min(cfg["maxPriceCents"], bid + cfg["maxJoinUpliftCents"] // 2)
    elif bid:
        confidence_bonus = max(0, int(round(max(0.0, side_prob - 0.70) * 30)))
        floor = bid + cfg["minImprovementCents"]
        ceiling = min(cfg["maxPriceCents"], bid + cfg["maxJoinUpliftCents"] + confidence_bonus)
    elif ask:
        floor = 1
        ceiling = max(1, min(cfg["maxPriceCents"], ask - 1 if ask > 1 else 1))
    else:
        return None

    if ceiling < floor:
        return None

    price = max(floor, min(target_price, ceiling))
    edge = side_prob - (price / 100.0)
    if edge + 1e-9 < min_edge:
        return None

    return {
        "execution_mode": "maker",
        "price": price,
        "edge": round(edge, 4),
        "bid": bid,
        "ask": ask,
        "fair_value_cents": round(fair_value_cents, 1),
        "min_edge": min_edge,
    }


def _select_weather_execution_plan(displayed_plan, maker_plan, market_liquid, maker_cfg=None):
    cfg = maker_cfg or _maker_execution_config()
    if maker_plan and (not market_liquid or not displayed_plan):
        return maker_plan
    if maker_plan and displayed_plan:
        if maker_plan["edge"] >= displayed_plan["edge"] + cfg["pricePriorityEdgeBuffer"]:
            return maker_plan
    return displayed_plan


def _weather_bias_trade_fields(bias_applied=None, hist_bias=None, live_bias=None,
                               live_n=0, live_confidence=0.0, alpha=None, bias_meta=None):
    meta = bias_meta or {}

    def _rounded(value, digits=3):
        if isinstance(value, (int, float)):
            return round(float(value), digits)
        return None

    return {
        "bias_applied_f": _rounded(bias_applied),
        "bias_hist_f": _rounded(hist_bias),
        "bias_live_f": _rounded(live_bias),
        "bias_live_n": int(live_n or 0),
        "bias_live_confidence": _rounded(live_confidence, 4),
        "bias_alpha": _rounded(alpha, 4),
        "bias_capped": bool(meta.get("capped", False)),
        "bias_conflict": bool(meta.get("conflict", False)),
    }


def _weather_model_name(probability_method):
    method = re.sub(r"[^a-z0-9]+", "_", str(probability_method or "single_model").lower()).strip("_")
    return f"weather_{method or 'single_model'}"


def _weather_research_fields(parsed, *, forecast_temp=None, sigma_used=None, probability_method=None,
                             market_type="threshold", execution_mode=None, days_out=None, city=None,
                             per_model_probs=None, weights_used=None, model_run_tags=None,
                             verification_confidence=None, bias_fields=None):
    resolved_city = city or parsed.get("city")
    resolved_method = probability_method or "single_model"
    descriptor = {
        "model_family": "weather",
        "model_type": "probability",
        "probability_method": resolved_method,
        "market_type": market_type,
        "city": resolved_city,
        "days_out": days_out,
        "direction": parsed.get("direction"),
        "threshold": parsed.get("threshold"),
    }
    if execution_mode:
        descriptor["execution_mode"] = execution_mode
    if model_run_tags:
        descriptor["model_run_tags"] = dict(model_run_tags)
    if isinstance(verification_confidence, (int, float)):
        descriptor["verification_confidence"] = round(float(verification_confidence), 4)

    model_inputs = {
        "city": resolved_city,
        "date": parsed.get("date"),
        "days_out": days_out,
        "forecast_temp": round(float(forecast_temp), 3) if isinstance(forecast_temp, (int, float)) else forecast_temp,
        "sigma_used": round(float(sigma_used), 4) if isinstance(sigma_used, (int, float)) else sigma_used,
        "direction": parsed.get("direction"),
        "threshold": parsed.get("threshold"),
        "probability_method": resolved_method,
    }
    if per_model_probs:
        model_inputs["per_model_probs"] = {
            key: round(float(value), 4) if isinstance(value, (int, float)) else value
            for key, value in per_model_probs.items()
        }
    if weights_used:
        model_inputs["weights_used"] = {
            key: round(float(value), 4) if isinstance(value, (int, float)) else value
            for key, value in weights_used.items()
        }
    if model_run_tags:
        model_inputs["model_run_tags"] = dict(model_run_tags)
    if isinstance(verification_confidence, (int, float)):
        model_inputs["verification_confidence"] = round(float(verification_confidence), 4)
    if isinstance(bias_fields, dict):
        model_inputs["bias"] = {
            key: value for key, value in bias_fields.items() if value is not None
        }

    snapshot_payload = json.dumps(model_inputs, sort_keys=True, separators=(",", ":"))
    feature_snapshot_id = f"weather:{hashlib.sha256(snapshot_payload.encode('utf-8')).hexdigest()[:16]}"
    return {
        "model_name": _weather_model_name(resolved_method),
        "model_family": "weather",
        "model_type": "probability",
        "model_descriptor": descriptor,
        "feature_snapshot_id": feature_snapshot_id,
        "inline_model_inputs": model_inputs,
    }


def _apply_live_model_bias(city, forecasts, city_model_bias=None):
    """Apply sample-shrunk live per-model bias corrections when available."""
    if not isinstance(forecasts, dict):
        return forecasts, []
    live_map = (city_model_bias or {}).get(city, {})
    if not live_map:
        return dict(forecasts), []

    adjusted = dict(forecasts)
    applied = []
    for model_name, temp in adjusted.items():
        if temp is None:
            continue
        stats = live_map.get(canonical_model_name(model_name))
        if not stats:
            continue
        confidence = float(stats.get("confidence") or 0.0)
        bias_f = float(stats.get("bias_f") or 0.0)
        correction = bias_f * confidence
        if abs(correction) < 0.05:
            continue
        adjusted[model_name] = temp - correction
        applied.append({
            "model": canonical_model_name(model_name),
            "bias_f": round(bias_f, 3),
            "confidence": round(confidence, 3),
            "correction_f": round(correction, 3),
        })

    return adjusted, applied


def _weather_selection_config():
    raw = config.get("opportunitySelection", {})
    return {
        "enabled": bool(raw.get("enabled", True)),
        "oversampleFactor": max(1, int(raw.get("oversampleFactor", 3))),
        "minCandidates": max(1, int(raw.get("minCandidates", 12))),
        "maxPerCity": max(1, int(raw.get("maxPerCity", 3))),
        "maxPerCityDate": max(1, int(raw.get("maxPerCityDate", 2))),
        "maxBracketPerCityDate": max(0, int(raw.get("maxBracketPerCityDate", 1))),
    }


def _weather_opportunity_priority(opp):
    parsed = opp.get("parsed", {})
    price = max(1, int(opp.get("entry_price") or 1))
    price_dollars = price / 100.0
    edge = float(opp.get("edge") or 0.0)
    roi = edge / max(price_dollars, 0.01)
    is_threshold = 1 if parsed.get("direction") != "B" else 0
    is_taker = 1 if opp.get("execution_mode") != "maker" else 0
    verification_confidence = float(opp.get("verification_confidence") or 0.0)
    days_out = opp.get("days_out")
    if not isinstance(days_out, int):
        days_out = 99
    return (
        is_threshold,
        roi,
        edge,
        is_taker,
        verification_confidence,
        -days_out,
    )


def _select_weather_opportunities(opportunities, remaining_slots, selection_cfg=None):
    cfg = selection_cfg or _weather_selection_config()
    if not opportunities:
        return [], []
    if not cfg.get("enabled", True):
        ranked = sorted(opportunities, key=_weather_opportunity_priority, reverse=True)
        return ranked, []
    if remaining_slots <= 0:
        return [], list(opportunities)

    ranked = sorted(opportunities, key=_weather_opportunity_priority, reverse=True)
    max_candidates = max(cfg["minCandidates"], remaining_slots * cfg["oversampleFactor"])
    selected = []
    pruned = []
    city_counts = {}
    city_date_counts = {}
    bracket_counts = {}

    for opp in ranked:
        city = opp.get("city")
        date_str = opp.get("parsed", {}).get("date")
        key = (city, date_str)
        is_bracket = opp.get("parsed", {}).get("direction") == "B"

        if city_counts.get(city, 0) >= cfg["maxPerCity"]:
            pruned.append(opp)
            continue
        if city_date_counts.get(key, 0) >= cfg["maxPerCityDate"]:
            pruned.append(opp)
            continue
        if is_bracket and bracket_counts.get(key, 0) >= cfg["maxBracketPerCityDate"]:
            pruned.append(opp)
            continue

        selected.append(opp)
        city_counts[city] = city_counts.get(city, 0) + 1
        city_date_counts[key] = city_date_counts.get(key, 0) + 1
        if is_bracket:
            bracket_counts[key] = bracket_counts.get(key, 0) + 1

        if len(selected) >= max_candidates:
            break

    selected_ids = {id(opp) for opp in selected}
    pruned_ids = {id(opp) for opp in pruned}
    remaining = [opp for opp in ranked if id(opp) not in selected_ids and id(opp) not in pruned_ids]
    pruned.extend(remaining)
    return selected, pruned


def _remaining_weather_trade_slots():
    if hasattr(trade_manager, "remaining_daily_trade_slots"):
        return max(0, int(trade_manager.remaining_daily_trade_slots()))
    if hasattr(trade_manager, "_reset_daily_if_needed"):
        trade_manager._reset_daily_if_needed()
    return max(0, config.get("maxDailyTrades", 10) - getattr(trade_manager, "_daily_trades", 0))


def compute_probability(forecast_temp, threshold, direction, days_out=0, city=None):
    """Estimate probability that YES resolves true.

    Delegates to weather_probability() with calibrated sigma from probability module.
    If city is provided and calibration data exists, uses calibrated sigma.
    """
    return weather_probability(forecast_temp, threshold, direction, days_out, city=city)

def compute_adaptive_interval(markets, config):
    """Compute scan interval based on nearest market settlement date.

    Returns shorter intervals when day-0 markets exist (5 min) vs day-1 (15 min)
    vs day-2+ (30 min), as short-lived opportunities concentrate near settlement.

    Args:
        markets: list of market dicts from Kalshi API
        config: bot config dict

    Returns:
        Interval in minutes (float).
    """
    adaptive_cfg = config.get("adaptiveScan", {})
    if not adaptive_cfg.get("enabled", False):
        return config.get("scanIntervalMinutes", 30)

    day0_min = adaptive_cfg.get("day0Minutes", 5)
    day1_min = adaptive_cfg.get("day1Minutes", 15)
    day2_min = adaptive_cfg.get("day2PlusMinutes", 30)

    if not markets:
        return day2_min

    min_days_out = float("inf")

    for m in markets:
        ticker = m.get("ticker", "")
        if not ticker.startswith("KXHIGH") or ticker.startswith("KXHIGHINFLATION"):
            continue
        parsed = parse_ticker(ticker)
        if not parsed:
            continue
        try:
            market_date = datetime.date.fromisoformat(parsed["date"])
            if "_local_today" in globals():
                local_today = datetime.date.fromisoformat(_local_today(parsed.get("city")))
            else:
                local_today = datetime.date.today()
            days_out = max(0, (market_date - local_today).days)
            min_days_out = min(min_days_out, days_out)
        except (ValueError, TypeError):
            continue

    if min_days_out == 0:
        return day0_min
    elif min_days_out == 1:
        return day1_min
    else:
        return day2_min


def scan_and_trade():
    now = datetime.datetime.now(datetime.timezone.utc)
    ss = ScanSummary("weather", log)
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Market scan starting...")

    # Balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")
        ss.finalize()
        return

    # Get weather markets (10-min cache — market structure doesn't change fast,
    # and direct city-series queries avoid missing contracts beyond global pagination)
    try:
        t0 = time.time()
        markets = get_weather_markets(cache_ttl=600)
        ss.markets_fetched = len(markets)
        log.info(f"Found {len(markets)} KXHIGH markets ({time.time()-t0:.1f}s)")
    except Exception as e:
        log.error(f"Market fetch error: {e}")
        ss.finalize()
        return

    if not markets:
        log.info("No weather markets found.")
        ss.finalize()
        return

    active_city_codes = set()
    near_term_city_codes = set()
    for market in markets:
        ticker = market.get("ticker", "")
        if not ticker.startswith("KXHIGH") or ticker.startswith("KXHIGHINFLATION"):
            continue
        parsed = parse_ticker(ticker)
        if not parsed:
            continue
        city_code = parsed.get("city")
        if city_code not in CITIES:
            continue
        active_city_codes.add(city_code)
        try:
            market_date = datetime.date.fromisoformat(parsed["date"])
            days_out = max(0, (market_date - _city_today(city_code)).days)
        except (ValueError, TypeError):
            days_out = 0
        if days_out <= 1:
            near_term_city_codes.add(city_code)

    active_cities = {code: CITIES[code] for code in sorted(active_city_codes)}
    near_term_cities = {code: CITIES[code] for code in sorted(near_term_city_codes)}
    if active_cities:
        log.info("Active weather cities this scan: %d/%d (%s)",
                 len(active_cities), len(CITIES), ", ".join(active_cities.keys()))
    if near_term_cities:
        log.info("Near-term weather cities for HRRR/NAM: %d (%s)",
                 len(near_term_cities), ", ".join(near_term_cities.keys()))

    # Get forecasts (batch API: 1-3 requests instead of 20-60+)
    # Uses separate circuit breaker keys so batch failures don't block per-model fallback
    t_forecast = time.time()
    forecasts = {}
    batch_failed = False
    if health.is_source_open("open-meteo-batch"):
        log.info("Batch endpoint circuit breaker open, skipping batch fetch")
        batch_failed = True
    else:
        try:
            if ENSEMBLE_ENABLED:
                forecasts = get_batch_ensemble_forecasts(CITIES)
            else:
                forecasts = get_batch_forecasts(CITIES)
            if forecasts:
                health.record_source_success("open-meteo-batch")
                log.info(f"Batch forecasts received for {len(forecasts)} cities")
            else:
                log.warning("Batch forecast returned empty, falling back to per-city")
                health.record_source_error("open-meteo-batch", "empty response")
                batch_failed = True
        except Exception as e:
            log.error(f"Batch forecast error: {e}")
            health.record_source_error("open-meteo-batch", str(e))
            batch_failed = True

    # Fallback: per-city fetch for any missing cities
    # Uses per-model circuit breaker keys (open-meteo-gfs, etc.) so one model's
    # failure doesn't block the others. Falls back gracefully: 3-model ensemble
    # -> 2-model -> 1-model -> single GFS.
    missing_cities = [code for code in CITIES if code not in forecasts]
    if missing_cities and batch_failed:
        log.info("Attempting per-city fallback for %d missing cities", len(missing_cities))
    for code in missing_cities:
        info = CITIES[code]
        # Per-model fetches have their own circuit breaker keys (open-meteo-gfs, etc.)
        # so we don't check a single "open-meteo" breaker here
        try:
            if ENSEMBLE_ENABLED:
                forecasts[code] = get_ensemble_forecast(info["lat"], info["lon"])
            else:
                forecasts[code] = get_forecast(info["lat"], info["lon"])
            health.record_source_success("open-meteo-single")
        except Exception as e:
            log.error(f"Forecast error for {info['name']}: {e}")
            health.record_source_error("open-meteo-single", str(e))

    # Log overall forecast recovery status
    if forecasts:
        models_available = set()
        for city_data in forecasts.values():
            if isinstance(city_data, dict):
                # Check for date-keyed dicts containing model dicts
                for v in city_data.values():
                    if isinstance(v, dict):
                        models_available.update(v.keys())
                        break
        if models_available:
            log.info("Ensemble models available: %s (%d/%d cities)",
                     ", ".join(sorted(models_available)), len(forecasts), len(CITIES))

    # NWS fallback: fetch NWS forecasts for cities still missing after Open-Meteo attempts
    # Also cross-validates when both sources are available (flags >3F disagreements)
    nws_missing = [code for code in CITIES if code not in forecasts]
    nws_forecasts = {}  # {city: {date: temp}} for cross-validation
    if nws_missing:
        log.info("NWS fallback: fetching for %d cities missing Open-Meteo data", len(nws_missing))
    for code in nws_missing:
        if health.is_source_open("nws-forecast"):
            break
        try:
            nws_data = nws_fetcher.fetch_forecast(code)
            if nws_data:
                # Use NWS as single-model forecast (keyed as "nws" in ensemble dict)
                if ENSEMBLE_ENABLED:
                    forecasts[code] = {date: {"nws": temp} for date, temp in nws_data.items()}
                else:
                    forecasts[code] = nws_data
                nws_forecasts[code] = nws_data
                health.record_source_success("nws-forecast")
                log.info("  NWS fallback for %s: %d days of forecast data", code, len(nws_data))
        except Exception as e:
            log.warning("NWS fallback failed for %s: %s", code, e)
            health.record_source_error("nws-forecast", str(e))

    # Cross-validate Open-Meteo vs NWS where both are available
    if not nws_missing:  # Only cross-validate if we didn't need NWS as primary
        eligible_codes = [
            code for code in (list(active_cities.keys()) or list(CITIES.keys()))
            if nws_fetcher.should_cross_validate(code)
        ]
        sample_codes = eligible_codes[:5]
        for code in sample_codes:  # Sample up to 5 relevant cities to limit API calls
            if code in forecasts and not health.is_source_open("nws-forecast"):
                try:
                    nws_data = nws_fetcher.fetch_forecast(code)
                    if nws_data:
                        nws_forecasts[code] = nws_data
                        health.record_source_success("nws-forecast")
                except Exception:
                    pass  # Cross-validation is non-blocking

    # Flag divergences between sources
    for code, nws_data in nws_forecasts.items():
        if code not in forecasts:
            continue
        city_forecast = forecasts[code]
        for date_str, nws_temp in nws_data.items():
            if date_str not in city_forecast:
                continue
            om_data = city_forecast[date_str]
            if isinstance(om_data, dict):
                om_temps = [t for t in om_data.values() if t is not None]
                if om_temps:
                    om_mean = sum(om_temps) / len(om_temps)
                    days_out = nws_fetcher._days_out(code, date_str)
                    threshold_f = nws_fetcher.cross_validate_threshold(code, days_out=days_out)
                    if nws_crosscheck_verifier:
                        nws_crosscheck_verifier.record_comparison(
                            code,
                            date_str,
                            om_mean,
                            nws_temp,
                            days_out=days_out,
                            threshold_f=threshold_f,
                            mode=nws_fetcher.cross_validate_mode(code),
                            open_meteo_models=om_data,
                        )
                    nws_fetcher.cross_validate(code, om_mean, nws_temp, date_str=date_str)
            elif om_data is not None:
                days_out = nws_fetcher._days_out(code, date_str)
                threshold_f = nws_fetcher.cross_validate_threshold(code, days_out=days_out)
                if nws_crosscheck_verifier:
                    nws_crosscheck_verifier.record_comparison(
                        code,
                        date_str,
                        om_data,
                        nws_temp,
                        days_out=days_out,
                        threshold_f=threshold_f,
                        mode=nws_fetcher.cross_validate_mode(code),
                    )
                nws_fetcher.cross_validate(code, om_data, nws_temp, date_str=date_str)

    model_run_tags = {}
    convergence_data = {}  # {city_code: {date_str: {current, previous, delta}}}
    prev_runs_cfg = config.get("previousRuns", {})

    # Track which model run each forecast came from so convergence only
    # fires when the upstream model cycle actually changes.
    for city_forecast in forecasts.values():
        for model_data in city_forecast.values():
            if not isinstance(model_data, dict):
                continue
            for model_name in model_data.keys():
                if model_name in model_run_tags:
                    continue
                run_dt = latest_available_model_run(model_name)
                if run_dt is not None:
                    model_run_tags[model_name] = run_dt.isoformat()

    # Previous-runs convergence (independent of verification system)
    if prev_runs_cfg.get("enabled", False) and verifier:
        model_name = canonical_model_name(prev_runs_cfg.get("model", "gfs"))
        current_run_tag = model_run_tags.get(model_name)
        if current_run_tag is not None:
            try:
                convergence_data = verifier.get_run_to_run_deltas(
                    forecasts,
                    model_name,
                    current_run_tag,
                )
                if convergence_data:
                    log.info("Convergence data available for %d cities", len(convergence_data))
            except Exception as e:
                log.warning("Convergence signal failed (non-blocking): %s", e)

    # Forecast verification: verify past forecasts and record new ones
    if verifier:
        try:
            verifier.verify_past_forecasts(station_map=DEFAULT_STATION_MAP)
        except Exception as e:
            log.warning("Verification check failed (non-blocking): %s", e)

        # Record current forecasts for later verification
        for code, info in CITIES.items():
            if code in forecasts:
                city_forecast = forecasts[code]
                for date_str, model_data in city_forecast.items():
                    if isinstance(model_data, dict):
                        verifier.record_forecast(
                            code,
                            date_str,
                            model_data,
                            model_run_tags=model_run_tags,
                        )

    if nws_crosscheck_verifier:
        try:
            nws_crosscheck_verifier.verify_past_comparisons(station_map=DEFAULT_STATION_MAP)
        except Exception as e:
            log.warning("NWS cross-check verification failed (non-blocking): %s", e)

    # Get adaptive ensemble data from verification (if available)
    verification_summary = None
    city_bias = {}
    city_model_bias = {}
    if verifier:
        try:
            lookback = VERIFICATION_CONFIG.get("lookback_days", 30)
            verification_summary = verifier.get_verification_summary(lookback)
            city_bias = verifier.get_city_bias(
                lookback,
                min_samples=int(bias_cfg.get("liveMinSamples", 2)),
                full_weight_n=int(bias_cfg.get("skewFullWeightSamples", 10)),
            )
            city_model_bias = verifier.get_city_model_bias(
                lookback,
                min_samples=int(bias_cfg.get("liveMinSamples", 2)),
                full_weight_n=int(bias_cfg.get("skewFullWeightSamples", 10)),
            )
            if verification_summary:
                log.info("Adaptive weights available from %d models",
                        len(verification_summary))
            if city_bias:
                log.info("City bias available for %d cities", len(city_bias))
            if city_model_bias:
                log.info("City-model bias available for %d cities", len(city_model_bias))
        except Exception as e:
            log.warning("Verification summary failed (non-blocking): %s", e)

    # Fetch raw ensemble member data for empirical CDF model
    ensemble_members = {}  # {city_code: {date_str: [member_temps]}}
    if ENSEMBLE_ENABLED and not health.is_source_open("open-meteo-ensemble"):
        target_cities = active_cities or CITIES
        for code, info in target_cities.items():
            if health.is_source_open("open-meteo-ensemble"):
                break
            try:
                _open_meteo_limiter.acquire()  # rate limit ensemble API too
                members = ensemble_collector.fetch_ensemble(info["lat"], info["lon"])
                if members:
                    ensemble_members[code] = members
                    health.record_source_success("open-meteo-ensemble")
            except Exception as e:
                log.warning("Ensemble member fetch failed for %s: %s", code, e)
                health.record_source_error("open-meteo-ensemble", str(e))
    if ensemble_members:
        log.info("Ensemble member data available for %d cities", len(ensemble_members))

    # Fetch HRRR deterministic forecast (Phase 3: 3km resolution, hourly updates)
    hrrr_data = {}  # {city_code: {date_str: max_temp_f}}
    hrrr_cfg = config.get("hrrr", {})
    hrrr_source = "open-meteo-hrrr"
    if hrrr_cfg.get("enabled", False) and not health.is_source_open(hrrr_source):
        target_cities = near_term_cities or active_cities
        for code, info in target_cities.items():
            if health.is_source_open(hrrr_source):
                log.info("HRRR circuit breaker open, skipping remaining cities")
                break
            try:
                data = hrrr_fetcher.fetch_hrrr(info["lat"], info["lon"])
                if data:
                    hrrr_data[code] = data
                    health.record_source_success(hrrr_source)
                else:
                    _record_source_failure(
                        hrrr_source,
                        getattr(hrrr_fetcher, "last_error", "empty response"),
                        status_code=getattr(hrrr_fetcher, "last_status_code", None),
                        immediate_on_bad_request=True,
                    )
            except Exception as e:
                log.warning("HRRR fetch failed for %s: %s", code, e)
                _record_source_failure(
                    hrrr_source,
                    str(e),
                    status_code=getattr(getattr(e, "response", None), "status_code", None),
                    immediate_on_bad_request=True,
                )
        if hrrr_data:
            log.info("HRRR data available for %d cities", len(hrrr_data))

    # Fetch NAM deterministic forecast (3km resolution, 60h horizon)
    nam_data = {}  # {city_code: {date_str: max_temp_f}}
    nam_cfg = config.get("nam", {})
    nam_source = "open-meteo-nam"
    if nam_cfg.get("enabled", False) and not health.is_source_open(nam_source):
        target_cities = near_term_cities or active_cities
        for code, info in target_cities.items():
            if health.is_source_open(nam_source):
                log.info("NAM circuit breaker open, skipping remaining cities")
                break
            try:
                data = nam_fetcher.fetch_nam(info["lat"], info["lon"])
                if data:
                    nam_data[code] = data
                    health.record_source_success(nam_source)
                else:
                    _record_source_failure(
                        nam_source,
                        getattr(nam_fetcher, "last_error", "empty response"),
                        status_code=getattr(nam_fetcher, "last_status_code", None),
                        immediate_on_bad_request=True,
                    )
            except Exception as e:
                log.warning("NAM fetch failed for %s: %s", code, e)
                _record_source_failure(
                    nam_source,
                    str(e),
                    status_code=getattr(getattr(e, "response", None), "status_code", None),
                    immediate_on_bad_request=True,
                )
        if nam_data:
            log.info("NAM data available for %d cities", len(nam_data))

    log.info(f"Forecast data collected ({time.time()-t_forecast:.1f}s)")

    # Analyze markets
    t_analysis = time.time()
    opportunities = []
    base_edge_threshold = _effective_weather_edge_threshold()
    maker_cfg = _maker_execution_config()
    for m in markets:
        ticker = m.get("ticker", "")
        # Filter non-weather KXHIGH tickers (e.g., KXHIGHINFLATION)
        if not ticker.startswith("KXHIGH") or ticker.startswith("KXHIGHINFLATION"):
            continue
        parsed = parse_ticker(ticker)
        if not parsed:
            log.warning("Unparseable KXHIGH ticker: %s", ticker)
            ss.skip("no_parse")
            continue

        city = parsed["city"]
        if city not in CITIES or city not in forecasts:
            ss.skip("no_city")
            continue

        date_str = parsed["date"]
        if date_str not in forecasts[city]:
            ss.skip("no_date")
            continue

        forecast_data = forecasts[city][date_str]
        city_now = _city_now(city)
        try:
            market_date = datetime.date.fromisoformat(date_str)
            days_out = max(0, (market_date - _city_today(city)).days)
        except (ValueError, TypeError):
            days_out = 0
        hour_for_sigma = city_now.hour if days_out == 0 else None

        # Compute probability — empirical ensemble CDF > parametric ensemble > single-model
        spread_mult = 1.0
        disagreement_score = 0.0
        used_empirical = False
        our_prob = None
        forecast_temp = None
        sigma_used = None
        per_model_probs = {}
        probability_method = "single_model"
        weights_used = {}
        verification_confidence = 0.0
        bias = None
        hist_bias = None
        live_bias = None
        live_n = 0
        live_confidence = 0.0
        alpha = None
        bias_meta = {}
        market_record_models = dict(forecast_data) if isinstance(forecast_data, dict) else None

        if ENSEMBLE_ENABLED and isinstance(forecast_data, dict):
            if not forecast_data:
                ss.skip("empty_forecast")
                continue
            # Bias-correct each model's forecast before computing mean
            corrected_data = bias_corrector.correct_forecast_dict(city, forecast_data)
            corrected_data, live_model_bias_applied = _apply_live_model_bias(
                city,
                corrected_data,
                city_model_bias,
            )
            if live_model_bias_applied:
                detail = ", ".join(
                    f"{item['model']}={item['correction_f']:+.1f}F@{item['confidence']:.2f}"
                    for item in live_model_bias_applied
                )
                log.info("  %s: live model bias applied (%s)", ticker, detail)
            valid_temps = [t for t in corrected_data.values() if t is not None]
            if not valid_temps:
                ss.skip("null_forecast")
                continue
            city_skew = city_bias.get(city, {}).get("skew_alpha", 0.0) if city_bias else 0.0

            # Primary model: empirical ensemble CDF (if member data available)
            if city in ensemble_members and date_str in ensemble_members.get(city, {}):
                members = list(ensemble_members[city][date_str])  # copy to avoid mutation
                if members and len(members) >= 10:
                    extra_points = []
                    if city in hrrr_data and date_str in hrrr_data.get(city, {}):
                        hrrr_temp = hrrr_data[city][date_str]
                        if days_out == 0:
                            hrrr_weight = hrrr_cfg.get("weight_day0", 0.60)
                        elif days_out == 1:
                            hrrr_weight = hrrr_cfg.get("weight_day1", 0.30)
                        else:
                            hrrr_weight = 0.0

                        if hrrr_weight > 0:
                            extra_points.append({"temp": hrrr_temp, "weight": hrrr_weight, "label": "hrrr"})
                            market_record_models["hrrr"] = hrrr_temp
                            log.info("  %s: HRRR temp %.1fF blended into empirical ensemble (relative weight=%.0f%%)",
                                     ticker, hrrr_temp, hrrr_weight * 100)

                    if city in nam_data and date_str in nam_data.get(city, {}):
                        nam_temp = nam_data[city][date_str]
                        if days_out == 0:
                            nam_weight = nam_cfg.get("weight_day0", 0.40)
                        elif days_out == 1:
                            nam_weight = nam_cfg.get("weight_day1", 0.20)
                        else:
                            nam_weight = 0.0

                        if nam_weight > 0:
                            extra_points.append({"temp": nam_temp, "weight": nam_weight, "label": "nam"})
                            market_record_models["nam"] = nam_temp
                            log.info("  %s: NAM temp %.1fF blended into empirical ensemble (relative weight=%.0f%%)",
                                     ticker, nam_temp, nam_weight * 100)

                    # Blend historical calibration bias with live ForecastVerifier bias
                    live_bias = city_bias.get(city, {}).get("bias_f", 0.0) if city_bias else 0.0
                    live_n = city_bias.get(city, {}).get("n", 0) if city_bias else 0
                    live_confidence = city_bias.get(city, {}).get("confidence", 0.0) if city_bias else 0.0
                    bias, hist_bias, alpha, bias_meta = bias_corrector.blend_live_bias(
                        city,
                        live_bias=live_bias,
                        live_n=live_n,
                        ramp_n=int(bias_cfg.get("liveRampSamples", 8)),
                        min_live_samples=int(bias_cfg.get("liveMinSamples", 2)),
                        max_abs_bias_f=float(bias_cfg.get("historicalMaxAbsF", 6.0)),
                        conflict_gap_f=float(bias_cfg.get("conflictGapF", 4.0)),
                        conflict_alpha_floor=float(bias_cfg.get("conflictAlphaFloor", 0.35)),
                        hist_model_weights=BiasCorrector.EMPIRICAL_MEMBER_MODEL_WEIGHTS,
                    )
                    if abs(bias) > 0.1:
                        extras = []
                        if bias_meta.get("conflict"):
                            extras.append("conflict_guard")
                        if bias_meta.get("capped"):
                            extras.append(f"cap={bias_meta.get('cap_f'):.1f}F")
                        suffix = f" [{' '.join(extras)}]" if extras else ""
                        log.info(
                            "  %s: bias correction %.1fF (hist=%.1fF, live=%.1fF, alpha=%.2f)%s",
                            ticker,
                            bias,
                            hist_bias,
                            live_bias,
                            alpha,
                            suffix,
                        )

                    empirical_result = empirical_ensemble_probability(
                        members, parsed["threshold"], parsed["direction"],
                        bias_offset=bias,
                        extra_points=extra_points,
                        return_details=True,
                    )
                    if empirical_result and empirical_result[0] is not None:
                        our_prob, empirical_details = empirical_result
                        forecast_temp = empirical_details.get("center_temp")
                        sigma_used = empirical_details.get("sigma_used")
                        probability_method = empirical_details.get("method", "empirical_ensemble")
                        log.info("  %s: empirical CDF from %d members (bias=%.1fF, n_eff=%.1f) -> P=%.3f",
                                 ticker, len(members), bias,
                                 empirical_details.get("effective_sample_size", 0.0), our_prob)
                        used_empirical = True
                        # Empirical CDF already captures model disagreement
                        disagreement_score = 0.0
                        empirical_model_inputs = dict(corrected_data)
                        if city in hrrr_data and date_str in hrrr_data.get(city, {}) and days_out <= 1:
                            empirical_model_inputs["hrrr"] = hrrr_data[city][date_str]
                        if city in nam_data and date_str in nam_data.get(city, {}) and days_out <= 1:
                            empirical_model_inputs["nam"] = nam_data[city][date_str]
                        per_model_probs = {
                            model_name: weather_probability(
                                temp,
                                parsed["threshold"],
                                parsed["direction"],
                                days_out,
                                city=city,
                                hour_of_day=hour_for_sigma,
                                skew=city_skew,
                            )
                            for model_name, temp in empirical_model_inputs.items()
                            if temp is not None
                        }
                    else:
                        log.warning("  %s: empirical CDF returned None, falling back to parametric", ticker)

            # Fallback: parametric ensemble (v2 with adaptive weights)
            if not used_empirical:
                # Phase 3: Blend HRRR into parametric forecast data
                parametric_data = dict(corrected_data)
                if city in hrrr_data and date_str in hrrr_data.get(city, {}) and days_out <= 1:
                    parametric_data["hrrr"] = hrrr_data[city][date_str]
                    log.info("  %s: HRRR temp %.1fF added to parametric ensemble",
                             ticker, hrrr_data[city][date_str])
                if city in nam_data and date_str in nam_data.get(city, {}) and days_out <= 1:
                    parametric_data["nam"] = nam_data[city][date_str]
                    log.info("  %s: NAM temp %.1fF added to parametric ensemble",
                             ticker, nam_data[city][date_str])

                # Compute spread multiplier once — widens sigma AND gates edge
                pvalid = [t for t in parametric_data.values() if t is not None]
                if len(pvalid) >= 2:
                    spread = max(pvalid) - min(pvalid)
                    spread_mult = ensemble_spread_sigma_multiplier(spread)
                    if spread_mult > 1.0:
                        log.info(f"  {ticker}: ensemble spread {spread:.1f}F (sigma_mult={spread_mult:.2f})")

                # Use v2 ensemble with adaptive weights, hour-aware sigma, disagreement scoring
                result = ensemble_weather_probability_v2(
                    parametric_data, parsed["threshold"], parsed["direction"],
                    days_out, city=city, sigma_multiplier=spread_mult,
                    hour_of_day=hour_for_sigma,
                    verification_data=verification_summary if verification_summary else None,
                    return_details=True,
                    static_weights=config.get("ensemble", {}).get("weights"),
                    skew=city_skew,
                )

                if result[0] is not None:
                    our_prob, ensemble_details = result
                    forecast_temp = ensemble_details.get("center_temp")
                    sigma_used = ensemble_details.get("sigma_used")
                    probability_method = ensemble_details.get("method", "parametric_ensemble_v2")
                    disagreement_score = ensemble_details.get("disagreement_score", 0.0)
                    per_model_probs = ensemble_details.get("per_model_probs", {})
                    weights_used = ensemble_details.get("weights_used", {})
                    verification_confidence = ensemble_details.get("verification_confidence", 0.0)
                    if disagreement_score > 0.3:
                        log.info(f"  {ticker}: high ensemble disagreement ({disagreement_score:.2f}), doubling edge threshold")
                    elif verification_confidence and verification_confidence < 0.6:
                        log.info(
                            "  %s: adaptive sample confidence %.2f still thin, shrinking toward static weights",
                            ticker,
                            verification_confidence,
                        )
                else:
                    # Ensemble failed (zero weight) — fall back to single-model
                    log.warning("Ensemble returned None for %s, falling back to single-model", ticker)
                    forecast_temp = sum(valid_temps) / len(valid_temps)
                    our_prob = weather_probability(
                        forecast_temp,
                        parsed["threshold"],
                        parsed["direction"],
                        days_out,
                        city=city,
                        hour_of_day=hour_for_sigma,
                        skew=city_skew,
                    )
                    sigma_used = weather_sigma_hourly(0, city, hour_for_sigma) if hour_for_sigma is not None else weather_sigma(days_out, city)
        else:
            if isinstance(forecast_data, dict):
                if not forecast_data:
                    ss.skip("empty_forecast")
                    continue
                # Get first model key and correct its temp
                first_model = list(forecast_data.keys())[0]
                single_corrected, live_model_bias_applied = _apply_live_model_bias(
                    city,
                    {
                        first_model: bias_corrector.correct(
                            city,
                            first_model,
                            forecast_data[first_model],
                        )
                    },
                    city_model_bias,
                )
                forecast_temp = single_corrected.get(first_model)
                if live_model_bias_applied:
                    item = live_model_bias_applied[0]
                    log.info(
                        "  %s: live model bias applied (%s=%+.1fF@%.2f)",
                        ticker,
                        item["model"],
                        item["correction_f"],
                        item["confidence"],
                    )
            else:
                forecast_temp = forecast_data
            if forecast_temp is None:
                ss.skip("null_forecast")
                continue
            our_prob = weather_probability(
                forecast_temp,
                parsed["threshold"],
                parsed["direction"],
                days_out,
                city=city,
                hour_of_day=hour_for_sigma,
            )
            sigma_used = weather_sigma_hourly(0, city, hour_for_sigma) if hour_for_sigma is not None else weather_sigma(days_out, city)

        if our_prob is None or forecast_temp is None:
            ss.skip("null_probability")
            continue

        if verifier and market_record_models:
            try:
                verifier.record_forecast(
                    city,
                    date_str,
                    market_record_models,
                    threshold=parsed["threshold"],
                    direction=parsed["direction"],
                    model_prob=our_prob,
                    hour_of_day=hour_for_sigma,
                    per_model_probs=per_model_probs or None,
                    model_run_tags=model_run_tags,
                    record_kind="market",
                )
            except Exception as e:
                log.debug("Market verification record failed for %s: %s", ticker, e)

        bias_fields = _weather_bias_trade_fields(
            bias_applied=bias,
            hist_bias=hist_bias,
            live_bias=live_bias,
            live_n=live_n,
            live_confidence=live_confidence,
            alpha=alpha,
            bias_meta=bias_meta,
        )
        research_fields = _weather_research_fields(
            parsed,
            forecast_temp=forecast_temp,
            sigma_used=sigma_used,
            probability_method=probability_method,
            market_type="bracket" if parsed["direction"] == "B" else "threshold",
            days_out=days_out,
            city=city,
            per_model_probs=per_model_probs,
            weights_used=weights_used,
            model_run_tags=model_run_tags,
            verification_confidence=verification_confidence,
            bias_fields=bias_fields,
        )

        # Skip near-threshold coinflips — dynamic based on calibrated sigma
        # With sigma=4.7F (global), min_distance=2.35F. For NY day-0 (sigma=3.1F), 1.55F.
        # Floor of 1.0F prevents degenerate cases.
        sigma = sigma_used if isinstance(sigma_used, (int, float)) and sigma_used > 0 else (
            weather_sigma_hourly(0, city, hour_for_sigma) if hour_for_sigma is not None else weather_sigma(days_out, city)
        )
        min_forecast_distance = max(1.0, 0.5 * sigma)
        distance = abs(forecast_temp - parsed["threshold"])
        if distance < min_forecast_distance:
            ss.skip("near_threshold")
            trade_manager.log_decision(ticker, "skip", "skipped", "near_threshold",
                                       forecast=forecast_temp, threshold=parsed["threshold"],
                                       distance=round(distance, 1),
                                       min_distance=round(min_forecast_distance, 1),
                                       sigma=round(sigma, 2),
                                       **research_fields)
            continue

        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        no_bid = m.get("no_bid", 0) or (100 - yes_ask if yes_ask else 0)
        no_ask = m.get("no_ask", 0) or (100 - yes_bid if yes_bid else 0)

        # Relaxed liquidity for near-settlement markets (0-1 days out)
        # where forecast accuracy is best and trading interest highest
        if days_out is not None and days_out <= 1:
            liquid = is_market_liquid(m, min_volume=5)
        else:
            liquid = is_market_liquid(m)

        # Compute edge against the price we'd actually pay (ask for taker flow,
        # passive limit price for maker flow).
        city_name = CITIES[city]["name"]
        side = "yes" if our_prob > 0.5 else "no"
        side_prob = our_prob if side == "yes" else (1 - our_prob)
        displayed_plan = _build_displayed_entry_plan(m, side, side_prob, liquid)
        maker_plan = _build_maker_entry_plan(m, side, side_prob, days_out, maker_cfg=maker_cfg)
        entry_plan = _select_weather_execution_plan(displayed_plan, maker_plan, liquid, maker_cfg=maker_cfg)

        if entry_plan is None:
            displayed_edge = displayed_plan["edge"] if displayed_plan else None
            if isinstance(displayed_edge, (int, float)) and displayed_edge < 0:
                ss.skip("negative_edge")
                trade_manager.log_decision(
                    ticker,
                    side,
                    "skipped",
                    "negative_edge",
                    edge=displayed_edge,
                    price_cents=displayed_plan["price"] if displayed_plan else None,
                    **research_fields,
                )
            elif displayed_plan is None and maker_plan is None:
                ss.skip("illiquid" if not liquid else "no_price")
            else:
                ss.skip("illiquid")
            continue

        ss.markets_evaluated += 1
        edge_yes = entry_plan["edge"]

        # Guard: never trade on negative edge (model says we'd lose money)
        if edge_yes < 0:
            ss.skip("negative_edge")
            trade_manager.log_decision(ticker, side,
                                       "skipped", "negative_edge",
                                       edge=edge_yes,
                                       price_cents=entry_plan["price"],
                                       **research_fields)
            continue

        # Adjust edge threshold for high ensemble spread or disagreement (defense in depth)
        effective_edge_threshold = base_edge_threshold
        disagree_mult = VERIFICATION_CONFIG.get("disagreement_edge_multiplier", 2.0)
        if disagreement_score > 0.3:
            effective_edge_threshold = base_edge_threshold * disagree_mult
        elif spread_mult > 1.5:
            effective_edge_threshold = base_edge_threshold * 2
        elif verification_confidence and verification_confidence < 0.6:
            effective_edge_threshold *= 1.0 + ((0.6 - verification_confidence) * 0.5)

        if edge_yes >= effective_edge_threshold:
            if parsed["direction"] == "B" and edge_yes < base_edge_threshold * 2:
                ss.skip("bracket_low_edge")
                trade_manager.log_decision(
                    ticker,
                    side,
                    "skipped",
                    f"bracket edge {edge_yes*100:.1f}% < 2x threshold",
                    edge=edge_yes,
                    price_cents=entry_plan["price"],
                    **research_fields,
                )
                continue
            opportunities.append({
                "ticker": ticker, "market": m, "parsed": parsed,
                "forecast": forecast_temp, "our_prob": our_prob,
                "market_price": (entry_plan["price"] / 100.0),
                "edge": edge_yes,
                "side": side,
                "city_name": city_name,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "no_bid": no_bid,
                "no_ask": no_ask,
                "days_out": days_out, "city": city,
                "per_model_probs": per_model_probs,
                "probability_method": probability_method,
                "sigma_used": sigma,
                "weights_used": weights_used,
                "verification_confidence": verification_confidence,
                "effective_edge_threshold": effective_edge_threshold,
                "execution_mode": entry_plan["execution_mode"],
                "entry_price": entry_plan["price"],
                "maker_plan": maker_plan,
                "research_fields": dict(
                    research_fields,
                    model_descriptor=dict(
                        research_fields["model_descriptor"],
                        execution_mode=entry_plan["execution_mode"],
                    ),
                ),
                **bias_fields,
            })
        else:
            ss.skip("low_edge")
            trade_manager.log_decision(
                ticker, side, "skipped",
                "edge below threshold", edge=edge_yes,
                price_cents=entry_plan["price"],
                **research_fields,
            )

    # Sort by edge magnitude
    remaining_trade_slots = _remaining_weather_trade_slots()
    selection_cfg = _weather_selection_config()
    opportunities, pruned_opportunities = _select_weather_opportunities(
        opportunities,
        remaining_trade_slots,
        selection_cfg=selection_cfg,
    )
    if pruned_opportunities:
        log.info(
            "Opportunity selection kept %d/%d candidates (remaining daily slots=%d)",
            len(opportunities),
            len(opportunities) + len(pruned_opportunities),
            remaining_trade_slots,
        )
        for _ in pruned_opportunities:
            ss.skip("selection_pruned")
    log.info(f"Found {len(opportunities)} opportunities with edge >= {base_edge_threshold*100:.0f}%")

    for opp in opportunities:
        ticker = opp["ticker"]
        edge = opp["edge"]
        forecast = opp["forecast"]
        threshold = opp["parsed"]["threshold"]
        direction = opp["parsed"]["direction"]  # T=threshold, B=bracket
        city_name = opp["city_name"]
        yes_ask = opp["yes_ask"]
        no_ask = opp["no_ask"]
        yes_bid = opp["yes_bid"]
        no_bid = opp["no_bid"]
        is_bracket = (direction == "B")

        # Days-out-aware dedup: shorter cooldown for near-settlement markets
        days_out_val = opp.get("days_out", 0)
        if is_locally_deduped(ticker, days_out_val):
            cooldown_secs = get_dedup_cooldown(days_out_val)
            log.info(f"  Skipping {ticker}: local dedup cooldown ({cooldown_secs}s for day-{days_out_val})")
            ss.skip("dedup_cooldown")
            trade_manager.log_decision(ticker, "yes" if opp["our_prob"] > 0.5 else "no",
                                       "skipped", f"dedup_cooldown ({cooldown_secs}s for day-{days_out_val})",
                                       edge=edge, price_cents=yes_ask if opp["our_prob"] > 0.5 else no_ask,
                                       **opp.get("research_fields", {}))
            continue

        # Rec 1: Brackets require 2x edge threshold (higher model uncertainty)
        if is_bracket and edge < base_edge_threshold * 2:
            log.info(f"  Skipping bracket {ticker}: edge {edge*100:.1f}% < {base_edge_threshold*200:.0f}% (2x threshold)")
            ss.skip("bracket_low_edge")
            trade_manager.log_decision(ticker, opp["side"], "skipped",
                                       f"bracket edge {edge*100:.1f}% < 2x threshold",
                                       edge=edge, price_cents=yes_ask if opp["side"] == "yes" else no_ask,
                                       **opp.get("research_fields", {}))
            continue

        # Edge is always positive (computed against the price we'd actually quote/pay)
        side = opp["side"]
        execution_mode = opp.get("execution_mode", "taker")
        price = opp.get("entry_price")

        # Phase 3: Order book depth gating — skip thin books, improve limit pricing
        ob_cfg = config.get("orderbookDepth", {})
        depth_data = None
        if ob_cfg.get("enabled", False):
            depth_data = orderbook.fetch_depth(client, ticker)
            if depth_data:
                min_depth = ob_cfg.get("minDepthContracts", 5)
                side_depth = depth_data["total_ask_depth"] if side == "yes" else depth_data["total_bid_depth"]
                if side_depth < min_depth:
                    maker_plan = opp.get("maker_plan") or _build_maker_entry_plan(
                        opp["market"],
                        side,
                        opp["our_prob"] if side == "yes" else (1 - opp["our_prob"]),
                        opp["days_out"],
                        maker_cfg=maker_cfg,
                    )
                    if maker_plan is None:
                        log.info(f"  Skipping {ticker}: insufficient depth ({side_depth} < {min_depth})")
                        ss.skip("low_depth")
                        trade_manager.log_decision(
                            ticker,
                            side,
                            "skipped",
                            f"insufficient depth ({side_depth} < {min_depth})",
                            edge=edge,
                            price_cents=yes_ask if side == "yes" else no_ask,
                            **opp.get("research_fields", {}),
                        )
                        continue
                    execution_mode = "maker"
                    price = maker_plan["price"]
                    edge = maker_plan["edge"]
                    log.info(
                        "  %s: thin book (%d < %d), switching to passive %s quote @ %dc",
                        ticker,
                        side_depth,
                        min_depth,
                        side.upper(),
                        price,
                    )
                    if edge < opp.get("effective_edge_threshold", base_edge_threshold):
                        ss.skip("low_depth")
                        trade_manager.log_decision(
                            ticker,
                            side,
                            "skipped",
                            "maker edge below threshold after thin-book repricing",
                            edge=edge,
                            price_cents=price,
                            **opp.get("research_fields", {}),
                        )
                        continue

        if side == "yes" and ((yes_ask and yes_ask < 99) or execution_mode == "maker"):
            # Config-level YES disable — if set, skip all weather YES trades
            if config.get("disableWeatherYes", False):
                trade_manager.log_decision(ticker, "yes", "skipped", "weather YES disabled by config",
                                            edge=edge, price_cents=price or yes_ask,
                                            **opp.get("research_fields", {}))
                continue
            # Rec 2: NO-only weather constraint — skip YES unless edge >= 15%
            # YES side has 0% historical win rate; only trade with very high conviction
            if edge < 0.15:
                trade_manager.log_decision(ticker, "yes", "skipped", f"YES edge {edge*100:.1f}% < 15% minimum",
                                            edge=edge, price_cents=price or yes_ask,
                                            **opp.get("research_fields", {}))
                continue
            if execution_mode == "maker":
                order_type = "maker-limit"
                if not price or price <= 0:
                    price = _build_maker_entry_plan(
                        opp["market"],
                        "yes",
                        opp["our_prob"],
                        opp["days_out"],
                        maker_cfg=maker_cfg,
                    )["price"]
                reasoning = (
                    f"{city_name} forecast: {forecast}F, {ticker} YES passive quote at {price}c "
                    f"(maker) -> our prob {opp['our_prob']*100:.0f}%, edge +{edge*100:.1f}%"
                )
            else:
                order_type, price = choose_order_type(yes_bid, yes_ask, "yes", edge, opp["our_prob"], depth_data)
                if not price or price <= 0:
                    price = yes_ask
                reasoning = f"{city_name} forecast: {forecast}F, {ticker} YES at {price}c ({order_type}) -> our prob {opp['our_prob']*100:.0f}%, edge +{edge*100:.1f}%, buying YES"
        elif side == "no" and (no_ask and no_ask < 99 or execution_mode == "maker"):
            if execution_mode == "maker":
                order_type = "maker-limit"
                if not price or price <= 0:
                    price = _build_maker_entry_plan(
                        opp["market"],
                        "no",
                        1 - opp["our_prob"],
                        opp["days_out"],
                        maker_cfg=maker_cfg,
                    )["price"]
                reasoning = (
                    f"{city_name} forecast: {forecast}F, {ticker} NO passive quote at {price}c "
                    f"(maker) -> our prob {(1-opp['our_prob'])*100:.0f}%, edge +{edge*100:.1f}%"
                )
            else:
                order_type, price = choose_order_type(yes_bid, yes_ask, "no", edge, opp["our_prob"], depth_data)
                if not price or price <= 0:
                    price = no_ask
                reasoning = f"{city_name} forecast: {forecast}F, {ticker} NO at {price}c ({order_type}) -> our prob {(1-opp['our_prob'])*100:.0f}%, edge +{edge*100:.1f}%, buying NO"
        else:
            continue

        # Request budget from portfolio allocator (includes Rec 6 dedup via global ticker check)
        budget = allocator.request_budget("weather", ticker, edge=edge)
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            ss.skip("allocator_denied")
            trade_manager.log_decision(ticker, side, "skipped", f"allocator denied: {budget.reason}",
                                       edge=edge, price_cents=price,
                                       **opp.get("research_fields", {}))
            continue

        # Position sizing based on market type, conviction, and calibration status
        fee = kalshi_fee_cents(price)
        cal = _load_calibration()
        city_code = opp["parsed"]["city"]
        is_calibrated = bool(cal.get("weather", {}).get("per_city", {}).get(city_code))

        sigma_val = opp.get("sigma_used") or weather_sigma(opp["days_out"], opp["city"])
        log.debug(f"  {ticker}: sigma={sigma_val:.2f} ({'calibrated' if is_calibrated else 'global'}) for {city_code}")

        if is_bracket:
            # Rec 3+10: Quarter-Kelly for brackets (always, regardless of calibration)
            count, risk, kelly_details = quarter_kelly(
                edge, price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            sizing_label = "quarter-Kelly"
        elif is_calibrated and side == "no" and (1 - opp["our_prob"]) > 0.80:
            # Calibration-validated: high-conviction threshold-NO -> 60% Kelly
            count, risk, kelly_details = high_conviction_kelly(
                edge, price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            sizing_label = "60%-Kelly (high-conviction, calibrated)"
        elif is_calibrated:
            # Calibration-validated: standard threshold -> half-Kelly
            count, risk, kelly_details = half_kelly(
                edge, price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            sizing_label = "half-Kelly (calibrated)"
        else:
            # Not calibrated: default to quarter-Kelly (SIZE-03)
            count, risk, kelly_details = quarter_kelly(
                edge, price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            sizing_label = "quarter-Kelly (uncalibrated)"

        # Apply forecast convergence multiplier to Kelly count
        if prev_runs_cfg.get("enabled", False) and city_code in convergence_data:
            city_conv = convergence_data[city_code]
            date_str_opp = opp["parsed"]["date"]
            if date_str_opp in city_conv and city_conv[date_str_opp].get("delta") is not None:
                conv_mult = PreviousRunsFetcher.convergence_multiplier(city_conv[date_str_opp]["delta"])
                if conv_mult != 1.0:
                    old_count = count
                    count = max(1, int(count * conv_mult))
                    log.info("  %s: convergence mult %.2f (delta=%.1fF), count %d->%d",
                             ticker, conv_mult, city_conv[date_str_opp]["delta"], old_count, count)

        # Phase 3: Improve limit price using orderbook depth (after Kelly sizing for accurate qty)
        if depth_data and count > 0 and execution_mode != "maker":
            old_price = price
            if side == "yes":
                fill_price = orderbook.estimate_fill_price(depth_data, "yes", count)
                if fill_price is not None and fill_price < price:
                    log.info(f"  {ticker}: depth suggests better YES fill at {fill_price:.0f}c (vs {price}c)")
                    price = int(fill_price)
            elif side == "no":
                fill_price_yes = orderbook.estimate_fill_price(depth_data, "no", count)
                if fill_price_yes is not None:
                    fill_price_no = 100 - round(fill_price_yes)
                    if fill_price_no < price:
                        log.info(f"  {ticker}: depth suggests better NO fill at {fill_price_no}c (vs {price}c)")
                        price = fill_price_no
            if price != old_price:
                fee = kalshi_fee_cents(price)

        if count <= 0:
            log.info(f"  Kelly says 0 contracts for {ticker} (edge too small for price), skipping")
            ss.skip("kelly_zero")
            trade_manager.log_decision(ticker, side, "skipped", "kelly_zero: edge too small for price",
                                       edge=edge, price_cents=price,
                                       **opp.get("research_fields", {}))
            continue

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c ({sizing_label}, bankroll=${budget.bankroll_cents/100:.2f})")

        # Include per-model forecasts for ensemble weight calibration
        city_code = opp["parsed"]["city"]
        date_str = opp["parsed"]["date"]
        raw_forecast = forecasts.get(city_code, {}).get(date_str) if ENSEMBLE_ENABLED else None
        ensemble_data = raw_forecast if isinstance(raw_forecast, dict) else None

        result = trade_manager.place_order(
            ticker, side, price, count, reasoning,
            forecast_temp=forecast, threshold=threshold, edge=round(edge, 4),
            market_snapshot=build_market_snapshot(
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                volume=opp["market"].get("volume"),
                open_interest=opp["market"].get("open_interest"),
            ),
            model_prob=round(opp["our_prob"], 4),
            raw_edge=round(edge, 4),
            fee_cents=round(kalshi_fee_cents(price), 2),
            sizing_method=sizing_label,
            market_close_time=opp["market"].get("close_time"),
            kelly_fraction=kelly_details.get("kelly_fraction"),
            bankroll_used=kelly_details.get("bankroll_used"),
            ensemble_forecasts=ensemble_data,
            ensemble_models=list(ensemble_data.keys()) if ensemble_data else None,
            sigma_used=round(opp.get("sigma_used") or weather_sigma(opp["days_out"], opp["city"]), 2),
            is_calibrated=is_calibrated,
            days_out=opp["days_out"],
            city=opp["city"],
            market_type="bracket" if is_bracket else "threshold",
            probability_method=opp.get("probability_method"),
            per_model_probs=opp.get("per_model_probs"),
            weights_used=opp.get("weights_used"),
            verification_confidence=round(opp.get("verification_confidence", 0.0), 4),
            execution_style=execution_mode,
            bias_applied_f=opp.get("bias_applied_f"),
            bias_hist_f=opp.get("bias_hist_f"),
            bias_live_f=opp.get("bias_live_f"),
            bias_live_n=opp.get("bias_live_n"),
            bias_live_confidence=opp.get("bias_live_confidence"),
            bias_alpha=opp.get("bias_alpha"),
            bias_capped=opp.get("bias_capped"),
            bias_conflict=opp.get("bias_conflict"),
            **opp.get("research_fields", {}),
        )
        if result:
            ss.trades_placed += 1
            allocator.record_trade("weather", ticker, result.get("cost_cents", risk), edge=edge)
            record_local_trade(ticker)

    log.info(f"Market analysis + trading ({time.time()-t_analysis:.1f}s)")

    # Save verification state
    if verifier:
        try:
            verifier.cleanup()
            verifier.save()
        except Exception as e:
            log.warning("Verification save failed (non-blocking): %s", e)
    if nws_crosscheck_verifier:
        try:
            nws_crosscheck_verifier.cleanup()
            nws_crosscheck_verifier.save()
        except Exception as e:
            log.warning("NWS cross-check save failed (non-blocking): %s", e)

    ss.finalize()
    return markets


def load_config(project_dir=None):
    project_dir = Path(project_dir or PROJECT_DIR)
    return json.loads((project_dir / "config" / "kalshi-config.json").read_text())


def build_app(project_dir=None):
    project_dir = Path(project_dir or PROJECT_DIR)
    setup_unbuffered()
    logger = setup_logging("weather")
    setup_signal_handlers()

    config_path = project_dir / "config" / "kalshi-config.json"
    trades_path = project_dir / "data" / "kalshi-trades.json"
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    singleton_lock_path = project_dir / "data" / "pids" / "weather-bot.lock"

    loaded_config = load_config(project_dir)
    cities = loaded_config["cities"]
    client_obj = KalshiClient()
    allocator_obj = PortfolioAllocator(client_obj, logger=logger)
    health_monitor = HealthCheckMonitor(logger=logger)
    order_monitor_obj = OrderMonitor(
        client_obj,
        log=logger,
        max_age_seconds=loaded_config.get("makerExecution", {}).get("maxRestingSeconds", 300),
    )
    trade_manager_obj = TradeManager(client_obj, trades_path, {
        "maxTradeAmount": loaded_config["maxTradeAmount"],
        "maxTradeAmountPct": loaded_config.get("maxTradeAmountPct"),
        "maxDailyTrades": loaded_config.get("maxDailyTrades", 10),
        "maxDailyLoss": loaded_config.get("maxDailyLoss", 10),
        "maxDailyLossPct": loaded_config.get("maxDailyLossPct"),
    }, logger=logger, order_monitor=order_monitor_obj, cooldown_hours=0.5, bot_name="weather")
    trim_trade_log(trades_path)

    verification_enabled = loaded_config.get("verification", {}).get("enabled", True)
    verification_config = loaded_config.get("verification", {})
    local_bias_cfg = loaded_config.get("biasCorrection", {})
    local_nws_cfg = loaded_config.get("nwsCrossValidation", {})
    open_meteo_base = (
        _OPEN_METEO_PREMIUM_FORECAST_BASE if _OPEN_METEO_API_KEY else _OPEN_METEO_FORECAST_BASE
    )
    open_meteo_limiter = RateLimiter(
        max_per_second=10 if _OPEN_METEO_API_KEY else 4,
        burst=10 if _OPEN_METEO_API_KEY else 4,
    )
    verification_path = project_dir / "data" / "weather-verification.json"
    verifier_obj = ForecastVerifier(verification_path, logger=logger) if verification_enabled else None
    if verifier_obj:
        verifier_obj.load()
    local_nws_audit_enabled = bool(local_nws_cfg.get("auditEnabled", True))
    local_nws_audit_path = _resolve_optional_project_path(
        local_nws_cfg.get("auditPath", "data/weather-nws-cross-check.json"),
        project_dir=project_dir,
    )
    crosscheck_verifier = (
        NWSCrossCheckVerifier(local_nws_audit_path, logger=logger)
        if local_nws_audit_enabled and local_nws_audit_path is not None
        else None
    )
    if crosscheck_verifier:
        crosscheck_verifier.load()

    bias_calibration_override = _resolve_optional_project_path(
        os.environ.get("WEATHER_BIAS_CALIBRATION_PATH") or local_bias_cfg.get("calibrationPath"),
        project_dir=project_dir,
    )
    if bias_calibration_override:
        logger.info("Using weather bias calibration override: %s", bias_calibration_override)

    context = AppContext({
        "PROJECT_DIR": project_dir,
        "CONFIG_PATH": config_path,
        "TRADES_PATH": trades_path,
        "WEATHER_SINGLETON_LOCK_PATH": singleton_lock_path,
        "log": logger,
        "config": loaded_config,
        "CITIES": cities,
        "client": client_obj,
        "allocator": allocator_obj,
        "health": health_monitor,
        "order_monitor": order_monitor_obj,
        "trade_manager": trade_manager_obj,
        "ENSEMBLE_ENABLED": loaded_config.get("ensemble", {}).get("enabled", False),
        "OPEN_METEO_BASE": open_meteo_base,
        "_endpoint_route_warned": set(),
        "_open_meteo_limiter": open_meteo_limiter,
        "_forecast_cache": {},
        "_local_trade_times": {},
        "_weather_market_cache": {"fetched_at": 0.0, "markets": []},
        "VERIFICATION_ENABLED": verification_enabled,
        "VERIFICATION_CONFIG": verification_config,
        "bias_cfg": local_bias_cfg,
        "nws_cfg": local_nws_cfg,
        "verifier": verifier_obj,
        "nws_audit_enabled": local_nws_audit_enabled,
        "nws_audit_path": local_nws_audit_path,
        "nws_crosscheck_verifier": crosscheck_verifier,
        "ensemble_collector": EnsembleCollector(logger=logger),
        "hrrr_fetcher": HRRRFetcher(logger=logger, rate_limiter=open_meteo_limiter.acquire),
        "nam_fetcher": NAMFetcher(logger=logger, rate_limiter=open_meteo_limiter.acquire),
        "bias_corrector": BiasCorrector(
            calibration_path=str(bias_calibration_override) if bias_calibration_override else None,
            logger=logger,
            require_lead_time_matched=True,
        ),
        "nws_fetcher": NWSForecastFetcher(
            logger=logger,
            grid_map=local_nws_cfg.get("gridOverrides"),
            city_coords=cities,
            threshold_map=local_nws_cfg.get("thresholds"),
            threshold_scale=local_nws_cfg.get("citySigmaScale"),
            max_threshold_f=local_nws_cfg.get("maxThresholdF"),
            mode_map=local_nws_cfg.get("cityModes") or local_nws_cfg.get("modes"),
            default_mode=local_nws_cfg.get("defaultMode", "gridpoint"),
        ),
        "orderbook": OrderBookDepth(logger=logger),
    })
    return install_app_context(globals(), context)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weather temperature trading bot")
    parser.add_argument("--once", action="store_true", help="Run single scan then exit")
    args = parser.parse_args()
    build_app()

    if not _acquire_singleton_lock():
        log.warning("Duplicate weather bot launch blocked; exiting.")
        return

    log.info("=" * 60)
    log.info("Kalshi Weather Trading Bot (DEMO)")
    log.info(f"Mode: {os.environ.get('KALSHI_MODE', 'demo')} | Max: ${config['maxTradeAmount']}/trade | Edge: {_effective_weather_edge_threshold()*100:.0f}%")
    log.info("=" * 60)

    # Verify auth
    log.info("\nVerifying authentication...")
    try:
        balance, _ = client.get_balance()
        log.info(f"Auth OK! Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Auth failed: {e}")
        sys.exit(1)

    if args.once:
        scan_and_trade()
        return

    # Main loop
    last_markets = []
    while True:
        try:
            health.record_bot_heartbeat("weather")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()
            result = scan_and_trade()
            if result:
                last_markets = result
        except Exception as e:
            log.error("Scan error: %s", e, exc_info=True)

        # Phase 3: Adaptive scan cadence based on nearest market settlement
        base_interval = compute_adaptive_interval(last_markets, config)
        interval = base_interval

        # Phase 3: Model-run timing — shorten interval when fresh data imminent
        adaptive_cfg = config.get("adaptiveScan", {})
        if adaptive_cfg.get("enabled", False):
            try:
                model, minutes_until = next_model_run()
                trigger_min = adaptive_cfg.get("modelRunTriggerMinutes", 2)
                if minutes_until <= trigger_min:
                    interval = min(interval, max(1, minutes_until))
                    log.info(f"New {model} run imminent in {minutes_until:.0f}min, scanning in {interval:.0f}min")
            except Exception as e:
                log.warning("Model-run timing check failed (non-blocking): %s", e)

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break
        log.info(f"\nNext scan in {interval} minutes...")
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
