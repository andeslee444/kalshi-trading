#!/usr/bin/env python3
"""Kalshi Weather Trading Bot - Demo Paper Trading
Scans KXHIGH temperature markets, compares to Open-Meteo forecasts, and places trades on edge.
"""

import json, time, datetime, os, sys, re, threading
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot, HealthCheckMonitor, OrderMonitor, ScanSummary, is_shutdown_requested
from probability import weather_probability, weather_sigma, ensemble_weather_probability, ensemble_spread_sigma_multiplier, ensemble_weather_probability_v2, empirical_ensemble_probability, half_kelly, quarter_kelly, high_conviction_kelly, compute_limit_price, kalshi_fee_cents, is_market_liquid, _load_calibration
from ticker_utils import parse_weather_ticker as parse_ticker
from capital_allocator import PortfolioAllocator
from forecast_verifier import ForecastVerifier, DEFAULT_STATION_MAP
from weather_data import EnsembleCollector, HRRRFetcher, OrderBookDepth, next_model_run, STATION_MAP, NWSForecastFetcher

setup_unbuffered()
log = setup_logging("weather")
setup_signal_handlers()

# === Config ===
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

config = json.loads(CONFIG_PATH.read_text())
CITIES = config["cities"]

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
order_monitor = OrderMonitor(client, log=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": config["maxTradeAmount"],
    "maxTradeAmountPct": config.get("maxTradeAmountPct"),
    "maxDailyTrades": config.get("maxDailyTrades", 10),
    "maxDailyLoss": config.get("maxDailyLoss", 10),
    "maxDailyLossPct": config.get("maxDailyLossPct"),
}, logger=log, order_monitor=order_monitor, cooldown_hours=0.5, bot_name="weather")
trim_trade_log(TRADES_PATH)


# === Days-out-aware dedup cooldown ===
# Local dedup layer on top of TradeManager's RecentTradeTracker.
# Day-0 markets should re-evaluate frequently (NWS updates every 1-2h),
# while day-3+ markets don't need to be re-traded for 12 hours.

_local_trade_times = {}  # ticker -> datetime of last trade


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
        _local_trade_times.clear()
        # In practice this rarely fires since weather markets settle daily

# === Weather Forecast ===

# Ensemble model endpoints for Open-Meteo
ENSEMBLE_MODELS = {
    "gfs": "gfs_seamless",
    "ecmwf": "ecmwf_ifs04",
    "icon": "icon_seamless",
}
ENSEMBLE_ENABLED = config.get("ensemble", {}).get("enabled", False)


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


_open_meteo_limiter = RateLimiter(max_per_second=4, burst=4)


def _rate_limited_request(url, timeout=15, max_retries=2):
    """Open-Meteo request with rate limiting."""
    _open_meteo_limiter.acquire()
    return retry_request("GET", url, timeout=timeout, max_retries=max_retries)



# === Forecast Verification ===
VERIFICATION_ENABLED = config.get("verification", {}).get("enabled", True)
VERIFICATION_CONFIG = config.get("verification", {})
verifier = ForecastVerifier(PROJECT_DIR / "data" / "weather-verification.json", logger=log) if VERIFICATION_ENABLED else None
if verifier:
    verifier.load()

# Ensemble member collector for empirical CDF model
ensemble_collector = EnsembleCollector(logger=log)

# HRRR deterministic forecast fetcher (Phase 3)
hrrr_fetcher = HRRRFetcher(logger=log)

# NWS forecast fetcher (fallback when Open-Meteo fails)
nws_fetcher = NWSForecastFetcher(logger=log)

# Order book depth analyzer (Phase 3)
orderbook = OrderBookDepth(logger=log)


def get_forecast(lat, lon):
    """Single-model GFS forecast (fallback)."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=14"
    r = _rate_limited_request(url, timeout=10)
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

    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lats}&longitude={lons}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&timezone=America%2FNew_York&forecast_days=14"
    )

    try:
        r = _rate_limited_request(url, timeout=30)
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

    Returns {city_code: {date: {model: temp}}} using 1 API call per model
    (3 total instead of 3*N_cities).
    """
    codes = list(cities_dict.keys())
    lats = ",".join(str(cities_dict[c]["lat"]) for c in codes)
    lons = ",".join(str(cities_dict[c]["lon"]) for c in codes)

    # Fetch each model in a single batch request
    model_results = {}  # model_key -> {city_code: {date: temp}}
    for model_key, model_name in ENSEMBLE_MODELS.items():
        if health.is_source_open(f"open-meteo-{model_key}"):
            log.warning(f"Circuit breaker open for {model_key}, skipping")
            continue

        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lats}&longitude={lons}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=America%2FNew_York&forecast_days=14"
            f"&models={model_name}"
        )

        try:
            r = _rate_limited_request(url, timeout=30)
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
            log.warning(f"Batch ensemble {model_key} failed: {e}")
            health.record_source_error(f"open-meteo-{model_key}", str(e))

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
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=America%2FNew_York&forecast_days=14"
            f"&models={model_name}"
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
            health.record_source_error(f"open-meteo-{model_key}",
                                       f"status={getattr(response, 'status_code', 'None')}")
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

    today = datetime.date.today()
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
            days_out = max(0, (market_date - today).days)
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
    now = datetime.datetime.now()
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

    # Get weather markets (5-min cache — markets don't change that fast)
    try:
        markets = client.get_all_markets(prefix="KXHIGH", cache_ttl=300)
        ss.markets_fetched = len(markets)
        log.info(f"Found {len(markets)} KXHIGH markets")
    except Exception as e:
        log.error(f"Market fetch error: {e}")
        ss.finalize()
        return

    if not markets:
        log.info("No weather markets found.")
        ss.finalize()
        return

    # Get forecasts (batch API: 1-3 requests instead of 20-60+)
    # Uses separate circuit breaker keys so batch failures don't block per-model fallback
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
        for code in list(CITIES.keys())[:5]:  # Sample up to 5 cities to limit API calls
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
                    nws_fetcher.cross_validate(code, om_mean, nws_temp)
            elif om_data is not None:
                nws_fetcher.cross_validate(code, om_data, nws_temp)

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
                        verifier.record_forecast(code, date_str, model_data)

    # Get adaptive ensemble data from verification (if available)
    verification_summary = None
    city_bias = {}
    if verifier:
        try:
            lookback = VERIFICATION_CONFIG.get("lookback_days", 30)
            verification_summary = verifier.get_verification_summary(lookback)
            city_bias = verifier.get_city_bias(lookback)
            if verification_summary:
                log.info("Adaptive weights available from %d models",
                        len(verification_summary))
            if city_bias:
                log.info("City bias available for %d cities", len(city_bias))
        except Exception as e:
            log.warning("Verification summary failed (non-blocking): %s", e)

    # Fetch raw ensemble member data for empirical CDF model
    ensemble_members = {}  # {city_code: {date_str: [member_temps]}}
    if not health.is_source_open("open-meteo-ensemble"):
        for code, info in CITIES.items():
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
    if hrrr_cfg.get("enabled", False):
        for code, info in CITIES.items():
            try:
                data = hrrr_fetcher.fetch_hrrr(info["lat"], info["lon"])
                if data:
                    hrrr_data[code] = data
            except Exception as e:
                log.warning("HRRR fetch failed for %s: %s", code, e)
        if hrrr_data:
            log.info("HRRR data available for %d cities", len(hrrr_data))

    # Current hour for intra-day sigma (day-0 markets only)
    current_hour = now.hour

    # Analyze markets
    opportunities = []
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
        try:
            market_date = datetime.date.fromisoformat(date_str)
            days_out = max(0, (market_date - datetime.date.today()).days)
        except (ValueError, TypeError):
            days_out = 0

        # Compute probability — empirical ensemble CDF > parametric ensemble > single-model
        spread_mult = 1.0
        disagreement_score = 0.0
        used_empirical = False

        if ENSEMBLE_ENABLED and isinstance(forecast_data, dict):
            if not forecast_data:
                ss.skip("empty_forecast")
                continue
            valid_temps = [t for t in forecast_data.values() if t is not None]
            forecast_temp = sum(valid_temps) / len(valid_temps) if valid_temps else None  # mean for logging
            if forecast_temp is None:
                ss.skip("null_forecast")
                continue

            # Primary model: empirical ensemble CDF (if member data available)
            if city in ensemble_members and date_str in ensemble_members.get(city, {}):
                members = list(ensemble_members[city][date_str])  # copy to avoid mutation
                if members and len(members) >= 10:
                    # Phase 3: Inject HRRR forecast as additional ensemble members
                    # with day-dependent weighting (60% day-0, 30% day-1)
                    if city in hrrr_data and date_str in hrrr_data.get(city, {}):
                        hrrr_temp = hrrr_data[city][date_str]
                        if days_out == 0:
                            hrrr_weight = hrrr_cfg.get("weight_day0", 0.60)
                        elif days_out == 1:
                            hrrr_weight = hrrr_cfg.get("weight_day1", 0.30)
                        else:
                            hrrr_weight = 0.0

                        if hrrr_weight > 0:
                            # Replicate HRRR temp as fraction of existing member count
                            n_inject = max(1, int(len(members) * hrrr_weight))
                            members.extend([hrrr_temp] * n_inject)
                            log.info("  %s: HRRR temp %.1fF injected %d members (weight=%.0f%%, total=%d)",
                                     ticker, hrrr_temp, n_inject, hrrr_weight * 100, len(members))

                    # Get station bias from verifier if available
                    bias = city_bias.get(city, {}).get("bias_f", 0.0) if city_bias else 0.0
                    our_prob = empirical_ensemble_probability(
                        members, parsed["threshold"], parsed["direction"],
                        bias_offset=bias
                    )
                    if our_prob is not None:
                        log.info("  %s: empirical CDF from %d members (bias=%.1fF) -> P=%.3f",
                                 ticker, len(members), bias, our_prob)
                        used_empirical = True
                        # Empirical CDF already captures model disagreement
                        disagreement_score = 0.0
                    else:
                        log.warning("  %s: empirical CDF returned None, falling back to parametric", ticker)

            # Fallback: parametric ensemble (v2 with adaptive weights)
            if not used_empirical:
                # Phase 3: Blend HRRR into parametric forecast data
                parametric_data = dict(forecast_data)
                if city in hrrr_data and date_str in hrrr_data.get(city, {}) and days_out <= 1:
                    parametric_data["hrrr"] = hrrr_data[city][date_str]
                    log.info("  %s: HRRR temp %.1fF added to parametric ensemble",
                             ticker, hrrr_data[city][date_str])

                # Compute spread multiplier once — widens sigma AND gates edge
                pvalid = [t for t in parametric_data.values() if t is not None]
                if len(pvalid) >= 2:
                    spread = max(pvalid) - min(pvalid)
                    spread_mult = ensemble_spread_sigma_multiplier(spread)
                    if spread_mult > 1.0:
                        log.info(f"  {ticker}: ensemble spread {spread:.1f}F (sigma_mult={spread_mult:.2f})")

                # Use v2 ensemble with adaptive weights, hour-aware sigma, disagreement scoring
                city_skew = city_bias.get(city, {}).get("skew_alpha", 0.0) if city_bias else 0.0
                hour_for_sigma = current_hour if days_out == 0 else None

                result = ensemble_weather_probability_v2(
                    parametric_data, parsed["threshold"], parsed["direction"],
                    days_out, city=city, sigma_multiplier=spread_mult,
                    hour_of_day=hour_for_sigma,
                    verification_data=verification_summary if verification_summary else None,
                    return_details=True,
                )

                if result[0] is not None:
                    our_prob, ensemble_details = result
                    disagreement_score = ensemble_details.get("disagreement_score", 0.0)
                    if disagreement_score > 0.3:
                        log.info(f"  {ticker}: high ensemble disagreement ({disagreement_score:.2f}), doubling edge threshold")
                else:
                    # Ensemble failed (zero weight) — fall back to single-model
                    log.warning("Ensemble returned None for %s, falling back to single-model", ticker)
                    our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"], days_out, city=city)
        else:
            if isinstance(forecast_data, dict):
                if not forecast_data:
                    ss.skip("empty_forecast")
                    continue
                forecast_temp = list(forecast_data.values())[0]
            else:
                forecast_temp = forecast_data
            if forecast_temp is None:
                ss.skip("null_forecast")
                continue
            our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"], days_out, city=city)

        # Skip near-threshold coinflips (|forecast - threshold| < 4°F)
        MIN_FORECAST_DISTANCE_F = 4.0
        distance = abs(forecast_temp - parsed["threshold"])
        if distance < MIN_FORECAST_DISTANCE_F:
            ss.skip("near_threshold")
            trade_manager.log_decision(ticker, "skip", "skipped", "near_threshold",
                                       forecast=forecast_temp, threshold=parsed["threshold"],
                                       distance=round(distance, 1))
            continue

        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)
        last = m.get("last_price", 0)

        # Relaxed liquidity for near-settlement markets (0-1 days out)
        # where forecast accuracy is best and trading interest highest
        if days_out is not None and days_out <= 1:
            liquid = is_market_liquid(m, min_volume=5)
        else:
            liquid = is_market_liquid(m)
        if not liquid:
            ss.skip("illiquid")
            continue

        ss.markets_evaluated += 1

        # Compute edge against the price we'd actually pay (ask for YES, 100-bid for NO)
        # not the midpoint, to avoid false positives from wide spreads
        city_name = CITIES[city]["name"]

        if our_prob > 0.5 and yes_ask and yes_ask < 99:
            edge_yes = our_prob - (yes_ask / 100.0)
        elif our_prob <= 0.5 and no_ask and no_ask < 99:
            edge_yes = (1 - our_prob) - (no_ask / 100.0)  # positive = NO signal
        else:
            ss.skip("no_price")
            continue

        # Guard: never trade on negative edge (model says we'd lose money)
        if edge_yes < 0:
            ss.skip("negative_edge")
            trade_manager.log_decision(ticker, "yes" if our_prob > 0.5 else "no",
                                       "skipped", "negative_edge",
                                       edge=edge_yes,
                                       price_cents=yes_ask if our_prob > 0.5 else no_ask)
            continue

        # Adjust edge threshold for high ensemble spread or disagreement (defense in depth)
        effective_edge_threshold = config["edgeThreshold"]
        disagree_mult = VERIFICATION_CONFIG.get("disagreement_edge_multiplier", 2.0)
        if disagreement_score > 0.3:
            effective_edge_threshold = config["edgeThreshold"] * disagree_mult
        elif spread_mult > 1.5:
            effective_edge_threshold = config["edgeThreshold"] * 2

        if edge_yes >= effective_edge_threshold:
            side = "yes" if our_prob > 0.5 else "no"
            opportunities.append({
                "ticker": ticker, "market": m, "parsed": parsed,
                "forecast": forecast_temp, "our_prob": our_prob,
                "market_price": (yes_ask / 100.0) if side == "yes" else (no_ask / 100.0),
                "edge": edge_yes,
                "side": side,
                "city_name": city_name,
                "yes_ask": yes_ask, "no_ask": no_ask,
                "days_out": days_out, "city": city,
            })
        else:
            ss.skip("low_edge")
            trade_manager.log_decision(
                ticker, "yes" if our_prob > 0.5 else "no", "skipped",
                "edge below threshold", edge=edge_yes,
                price_cents=yes_ask if our_prob > 0.5 else no_ask,
            )

    # Sort by edge magnitude
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    log.info(f"Found {len(opportunities)} opportunities with edge >= {config['edgeThreshold']*100:.0f}%")

    for opp in opportunities:
        ticker = opp["ticker"]
        edge = opp["edge"]
        forecast = opp["forecast"]
        threshold = opp["parsed"]["threshold"]
        direction = opp["parsed"]["direction"]  # T=threshold, B=bracket
        city_name = opp["city_name"]
        yes_ask = opp["yes_ask"]
        no_ask = opp["no_ask"]
        yes_bid = opp["market"].get("yes_bid", 0)
        is_bracket = (direction == "B")

        # Days-out-aware dedup: shorter cooldown for near-settlement markets
        days_out_val = opp.get("days_out", 0)
        if is_locally_deduped(ticker, days_out_val):
            cooldown_secs = get_dedup_cooldown(days_out_val)
            log.info(f"  Skipping {ticker}: local dedup cooldown ({cooldown_secs}s for day-{days_out_val})")
            ss.skip("dedup_cooldown")
            trade_manager.log_decision(ticker, "yes" if opp["our_prob"] > 0.5 else "no",
                                       "skipped", f"dedup_cooldown ({cooldown_secs}s for day-{days_out_val})",
                                       edge=edge, price_cents=yes_ask if opp["our_prob"] > 0.5 else no_ask)
            continue

        # Rec 1: Brackets require 2x edge threshold (higher model uncertainty)
        if is_bracket and edge < config["edgeThreshold"] * 2:
            log.info(f"  Skipping bracket {ticker}: edge {edge*100:.1f}% < {config['edgeThreshold']*200:.0f}% (2x threshold)")
            ss.skip("bracket_low_edge")
            trade_manager.log_decision(ticker, opp["side"], "skipped",
                                       f"bracket edge {edge*100:.1f}% < 2x threshold",
                                       edge=edge, price_cents=yes_ask if opp["side"] == "yes" else no_ask)
            continue

        # Edge is always positive (computed against the ask for the side we'd trade)
        side = opp["side"]

        # Phase 3: Order book depth gating — skip thin books, improve limit pricing
        ob_cfg = config.get("orderbookDepth", {})
        depth_data = None
        if ob_cfg.get("enabled", False):
            depth_data = orderbook.fetch_depth(client, ticker)
            if depth_data:
                min_depth = ob_cfg.get("minDepthContracts", 5)
                side_depth = depth_data["total_ask_depth"] if side == "yes" else depth_data["total_bid_depth"]
                if side_depth < min_depth:
                    log.info(f"  Skipping {ticker}: insufficient depth ({side_depth} < {min_depth})")
                    ss.skip("low_depth")
                    trade_manager.log_decision(ticker, side, "skipped", f"insufficient depth ({side_depth} < {min_depth})",
                                               edge=edge, price_cents=yes_ask if side == "yes" else no_ask)
                    continue

        if side == "yes" and yes_ask and yes_ask < 99:
            # Config-level YES disable — if set, skip all weather YES trades
            if config.get("disableWeatherYes", False):
                trade_manager.log_decision(ticker, "yes", "skipped", "weather YES disabled by config",
                                            edge=edge, price_cents=yes_ask)
                continue
            # Rec 2: NO-only weather constraint — skip YES unless edge >= 15%
            # YES side has 0% historical win rate; only trade with very high conviction
            if edge < 0.15:
                trade_manager.log_decision(ticker, "yes", "skipped", f"YES edge {edge*100:.1f}% < 15% minimum",
                                            edge=edge, price_cents=yes_ask)
                continue
            order_type, price = choose_order_type(yes_bid, yes_ask, "yes", edge, opp["our_prob"], depth_data)
            if not price or price <= 0:
                price = yes_ask
            reasoning = f"{city_name} forecast: {forecast}F, {ticker} YES at {price}c ({order_type}) -> our prob {opp['our_prob']*100:.0f}%, edge +{edge*100:.1f}%, buying YES"
        elif side == "no" and no_ask and no_ask < 99:
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
                                       edge=edge, price_cents=price)
            continue

        # Position sizing based on market type, conviction, and calibration status
        fee = kalshi_fee_cents(price)
        cal = _load_calibration()
        city_code = opp["parsed"]["city"]
        is_calibrated = bool(cal.get("weather", {}).get("per_city", {}).get(city_code))

        sigma_val = weather_sigma(opp["days_out"], opp["city"])
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

        # Phase 3: Improve limit price using orderbook depth (after Kelly sizing for accurate qty)
        if depth_data and count > 0:
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
                                       edge=edge, price_cents=price)
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
            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
            model_prob=round(opp["our_prob"], 4),
            raw_edge=round(edge, 4),
            fee_cents=round(kalshi_fee_cents(price), 2),
            sizing_method=sizing_label,
            market_close_time=opp["market"].get("close_time"),
            kelly_fraction=kelly_details.get("kelly_fraction"),
            bankroll_used=kelly_details.get("bankroll_used"),
            ensemble_forecasts=ensemble_data,
            ensemble_models=list(ensemble_data.keys()) if ensemble_data else None,
            sigma_used=round(weather_sigma(opp["days_out"], opp["city"]), 2),
            is_calibrated=is_calibrated,
            days_out=opp["days_out"],
            city=opp["city"],
            market_type="bracket" if is_bracket else "threshold",
        )
        if result:
            ss.trades_placed += 1
            allocator.record_trade("weather", ticker, risk, edge=edge)
            record_local_trade(ticker)

    # Save verification state
    if verifier:
        try:
            verifier.cleanup()
            verifier.save()
        except Exception as e:
            log.warning("Verification save failed (non-blocking): %s", e)

    ss.finalize()
    return markets

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weather temperature trading bot")
    parser.add_argument("--once", action="store_true", help="Run single scan then exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Weather Trading Bot (DEMO)")
    log.info(f"Mode: {os.environ.get('KALSHI_MODE', 'demo')} | Max: ${config['maxTradeAmount']}/trade | Edge: {config['edgeThreshold']*100:.0f}%")
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
