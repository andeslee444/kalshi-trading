#!/usr/bin/env python3
"""Kalshi Crypto Bot — Trades BTC/ETH price markets using spot + volatility data.

Data sources (all public REST, no auth):
  1. Coinbase spot prices — BTC-USD, ETH-USD
  2. Deribit implied volatility — BTC/ETH option-derived IV
  3. Realized volatility — computed from recent price history

Uses log-normal / geometric Brownian motion model for probability estimation.

Usage:
    python3 src/kalshi/crypto-bot.py          # daemon mode
    python3 src/kalshi/crypto-bot.py --once    # single scan
"""

import json, time, datetime, os, sys, re, argparse, math, hashlib
import requests
from pathlib import Path
from app_bootstrap import AppContext, install_app_context
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot,
    HealthCheckMonitor, OrderMonitor, _atomic_write_json, ScanSummary,
    is_shutdown_requested,
)
from probability import (
    half_kelly, compute_limit_price,
    kalshi_fee_cents, is_market_liquid,
    apply_kelly_multipliers as _shared_apply_kelly_multipliers,
)
from ticker_utils import parse_crypto_ticker
from capital_allocator import PortfolioAllocator
from particle_filter import FilterManager, FilterConfig, ci_kelly_multiplier
from regime_detector import RegimeDetector, regime_kelly_multiplier
from crypto_models import EnsembleModel, smooth_edge_threshold, horizon_kelly_fraction, horizon_vol_weights, AR1VolForecast, vol_skew_multiplier
from vol_forecaster import GARCHForecaster, DCCCorrelation, intraday_vol_multiplier, correct_bid_ask_bounce
from singleton_lock import acquire_process_singleton
from research.opportunity_log import OpportunityLog

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-crypto-trades.json"
_APP_CONTEXT = None
log = None
bots_config = {}
crypto_config = {}
MAX_TRADE = 10
MAX_DAILY_TRADES = 30
MAX_DAILY_LOSS = 25
SCAN_INTERVAL = 5
EDGE_THRESHOLD = 0.06
SETTLEMENT_BUFFER_MINUTES = 3
USE_OU = False
OU_HALF_LIFE = 120
DRIFT_PCT = 0.0
client = None
allocator = None
health = None
order_monitor = None
trade_manager = None
opportunity_log = None
pf_config = None
filter_mgr = None
pf_staleness_seconds = 24 * 3600
regime_detector = None
regime_state_path = PROJECT_DIR / "data" / "regime-state.json"
_calibration_path = PROJECT_DIR / "config" / "crypto-calibration.json"
_calibration = {}
ensemble_model = None

# Default Heston parameters
DEFAULT_HESTON_PARAMS = {"v0": 0.25, "kappa": 2.0, "theta": 0.25, "xi": 0.3, "rho": -0.7}

# GARCH vol forecasters per asset
garch_forecasters = {}  # populated after DEFAULT_VOLS is defined

# AR(1) vol forecasters per asset
ar1_forecasters = {}  # populated after DEFAULT_VOLS is defined

# DCC correlation tracker
dcc_tracker = None  # populated after DEFAULT_VOLS is defined

# === Market ticker prefixes ===
CRYPTO_PREFIXES = ["KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP", "KXCRYPTO"]
INVALID_MARKETS_PATH = PROJECT_DIR / "data" / "crypto-invalid-markets.json"
INVALID_MARKET_TTL_SECONDS = 6 * 3600


def _load_invalid_market_cache():
    if not INVALID_MARKETS_PATH.exists():
        return {}
    try:
        data = json.loads(INVALID_MARKETS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _prune_invalid_market_cache(cache):
    if not cache:
        return {}
    now = datetime.datetime.now(datetime.timezone.utc)
    fresh = {}
    for ticker, info in cache.items():
        marked_at = info.get("marked_at") if isinstance(info, dict) else None
        if not marked_at:
            continue
        try:
            marked_dt = datetime.datetime.fromisoformat(marked_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - marked_dt).total_seconds() < INVALID_MARKET_TTL_SECONDS:
            fresh[ticker] = info
    return fresh


def _save_invalid_market_cache(cache):
    _atomic_write_json(INVALID_MARKETS_PATH, _prune_invalid_market_cache(cache))


_invalid_market_cache = {}


def _refresh_invalid_market_cache():
    _invalid_market_cache.clear()
    _invalid_market_cache.update(_prune_invalid_market_cache(_load_invalid_market_cache()))
    _save_invalid_market_cache(_invalid_market_cache)


def _mark_invalid_market(ticker, reason):
    _invalid_market_cache[ticker] = {
        "marked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": reason,
    }
    _save_invalid_market_cache(_invalid_market_cache)


def _is_invalid_market(ticker):
    fresh_cache = _prune_invalid_market_cache(_invalid_market_cache)
    if len(fresh_cache) != len(_invalid_market_cache):
        _invalid_market_cache.clear()
        _invalid_market_cache.update(fresh_cache)
        _save_invalid_market_cache(_invalid_market_cache)
    return ticker in _invalid_market_cache


def _validate_tradeable_market(ticker):
    market = client.get_market(ticker)
    status = str((market or {}).get("status") or "open").lower()
    if not market or status != "open":
        _mark_invalid_market(ticker, f"lookup status={status}")
        return None
    return market

# Default annualized volatilities (post-ETF era, updated 2026)
DEFAULT_VOLS = {
    "BTC": 0.50,  # post-ETF BTC vol is 40-55%
    "ETH": 0.65,  # ETH vol tracks BTC more closely now
    "SOL": 0.80,
    "DOGE": 0.90,  # meme coin, high vol
    "XRP": 0.75,   # mid-cap alt, moderate-high vol
}

# Initialize forecasters in build_app()
garch_forecasters = {}
ar1_forecasters = {}
dcc_tracker = None

# Recent price cache for realized vol computation
_price_history = {}  # asset -> [(timestamp, price), ...]
_PRICE_HISTORY_PATH = PROJECT_DIR / "data" / "crypto-price-history.json"


def _load_price_history():
    """Load price history from disk, keeping only last 7 days."""
    global _price_history
    try:
        if _PRICE_HISTORY_PATH.exists():
            data = json.loads(_PRICE_HISTORY_PATH.read_text())
            cutoff = time.time() - 604800
            for asset, entries in data.items():
                _price_history[asset] = [(t, p) for t, p in entries if t > cutoff]
    except (json.JSONDecodeError, OSError, KeyError):
        pass


def _save_price_history():
    """Persist price history to disk."""
    try:
        _atomic_write_json(_PRICE_HISTORY_PATH, _price_history)
    except Exception:
        pass  # best-effort





def compute_ou_target(asset, lookback_hours=24):
    """Compute OU mean-reversion target from trailing VWAP (volume-weighted average price).

    Uses simple average of recent price observations as a proxy for VWAP
    (true VWAP requires volume data we don't have at this frequency).

    The OU target should be INDEPENDENT of the strike price being evaluated.
    Using the strike price as the OU target (the prior bug in probability.py)
    is mathematically nonsensical — it pulls the price toward every strike
    simultaneously.

    NOTE: OU is currently disabled (useOrnsteinUhlenbeck: false). This function
    is ready for when probability.py is updated to accept an ou_target parameter.

    Args:
        asset: Asset symbol (e.g., "BTC", "ETH").
        lookback_hours: Hours of price history to use (default 24h).

    Returns:
        float: Mean price over the lookback period, or None if insufficient data.
    """
    history = _price_history.get(asset, [])
    if len(history) < 5:
        return None
    cutoff = time.time() - lookback_hours * 3600
    recent = [p for t, p in history if t > cutoff]
    if len(recent) < 3:
        return None
    return sum(recent) / len(recent)


def compute_trailing_drift(asset):
    """Compute annualized drift from trailing 24h price change.

    Returns drift as decimal (e.g., -1.2 = -120% annualized), or 0.0 if insufficient data.
    Clamped to [-2.0, 2.0] to prevent extreme values.
    """
    history = _price_history.get(asset, [])
    if len(history) < 2:
        return 0.0
    oldest_price = history[0][1]
    newest_price = history[-1][1]
    dt_seconds = history[-1][0] - history[0][0]
    if dt_seconds < 3600 or oldest_price <= 0:  # need at least 1h of data
        return 0.0
    log_return = math.log(newest_price / oldest_price)
    annualized = log_return * (365.25 * 86400 / dt_seconds)
    capped = max(-2.0, min(2.0, annualized))
    if abs(annualized) > 2.0:
        log.debug(f"  Drift capped: raw={annualized*100:.0f}% -> {capped*100:.0f}%")
    return capped


def _crypto_model_name(parsed, *, use_ou=False, filtered=True):
    market_type = parsed.get("market_type") or ("bracket" if parsed.get("direction") == "B" else "standard")
    normalized_type = re.sub(r"[^a-z0-9]+", "_", str(market_type).lower()).strip("_")
    parts = ["crypto", "ensemble", normalized_type or "standard", "ou" if use_ou else "gbm"]
    if filtered:
        parts.append("particle_filter")
    return "_".join(parts)


def _crypto_research_fields(parsed, *, current_price=None, minutes_to_settle=None, vol_used=None,
                            raw_prob=None, filtered_est=None, current_regime=None, drift_pct=None,
                            iv=None, rv=None, garch_forecast=None, season_mult=None, skew_mult=None,
                            use_ou=False, ou_half_life_minutes=None, ou_target=None,
                            ou_shadow_prob=None, heston_params=None):
    filtered_prob = getattr(filtered_est, "prob", None)
    market_type = parsed.get("market_type") or ("bracket" if parsed.get("direction") == "B" else "standard")
    descriptor = {
        "model_family": "crypto",
        "model_type": "probability",
        "asset": parsed.get("asset"),
        "market_type": market_type,
        "direction": parsed.get("direction"),
        "filtered": filtered_est is not None,
        "use_ou": bool(use_ou),
    }
    if current_regime is not None:
        descriptor["regime"] = current_regime
    if use_ou and ou_half_life_minutes is not None:
        descriptor["ou_half_life_minutes"] = ou_half_life_minutes

    model_inputs = {
        "asset": parsed.get("asset"),
        "date": parsed.get("date"),
        "threshold": round(float(parsed.get("threshold")), 4) if isinstance(parsed.get("threshold"), (int, float)) else parsed.get("threshold"),
        "direction": parsed.get("direction"),
        "market_type": market_type,
        "settlement_hour": parsed.get("settlement_hour"),
        "current_price": round(float(current_price), 4) if isinstance(current_price, (int, float)) else current_price,
        "minutes_to_settle": minutes_to_settle,
        "vol_used": round(float(vol_used), 6) if isinstance(vol_used, (int, float)) else vol_used,
        "raw_prob": round(float(raw_prob), 6) if isinstance(raw_prob, (int, float)) else raw_prob,
        "filtered_prob": round(float(filtered_prob), 6) if isinstance(filtered_prob, (int, float)) else filtered_prob,
        "filtered_ci_low": round(float(getattr(filtered_est, "ci_low", None)), 6) if isinstance(getattr(filtered_est, "ci_low", None), (int, float)) else getattr(filtered_est, "ci_low", None),
        "filtered_ci_high": round(float(getattr(filtered_est, "ci_high", None)), 6) if isinstance(getattr(filtered_est, "ci_high", None), (int, float)) else getattr(filtered_est, "ci_high", None),
        "filtered_trend": getattr(filtered_est, "trend", None),
        "filtered_updates": getattr(filtered_est, "n_updates", None),
        "regime": current_regime,
        "drift_pct": round(float(drift_pct), 6) if isinstance(drift_pct, (int, float)) else drift_pct,
        "iv": round(float(iv), 6) if isinstance(iv, (int, float)) else iv,
        "rv": round(float(rv), 6) if isinstance(rv, (int, float)) else rv,
        "garch_forecast": round(float(garch_forecast), 6) if isinstance(garch_forecast, (int, float)) else garch_forecast,
        "season_mult": round(float(season_mult), 6) if isinstance(season_mult, (int, float)) else season_mult,
        "skew_mult": round(float(skew_mult), 6) if isinstance(skew_mult, (int, float)) else skew_mult,
        "ou_target": round(float(ou_target), 6) if isinstance(ou_target, (int, float)) else ou_target,
        "ou_shadow_prob": round(float(ou_shadow_prob), 6) if isinstance(ou_shadow_prob, (int, float)) else ou_shadow_prob,
        "heston_params": {
            key: round(float(value), 6) if isinstance(value, (int, float)) else value
            for key, value in (heston_params or {}).items()
        } if isinstance(heston_params, dict) else heston_params,
    }
    snapshot_payload = json.dumps(model_inputs, sort_keys=True, separators=(",", ":"))
    feature_snapshot_id = f"crypto:{hashlib.sha256(snapshot_payload.encode('utf-8')).hexdigest()[:16]}"
    return {
        "model_name": _crypto_model_name(parsed, use_ou=use_ou, filtered=filtered_est is not None),
        "model_family": "crypto",
        "model_type": "probability",
        "model_descriptor": descriptor,
        "feature_snapshot_id": feature_snapshot_id,
        "inline_model_inputs": model_inputs,
    }


def _record_crypto_opportunity(ticker, side, action, reason, *, opportunity_stage,
                               opportunity_log_obj=None, edge=None, price_cents=None, **extra):
    log_obj = opportunity_log_obj or opportunity_log
    if log_obj is None:
        return None

    record = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "reason": reason,
        "opportunity_stage": opportunity_stage,
    }
    if edge is not None:
        record["edge"] = round(edge, 4)
    if price_cents is not None:
        record["price_cents"] = price_cents
    record.update(extra)
    return log_obj.record(record)


def _log_crypto_decision(ticker, side, action, reason, *, edge=None, price_cents=None,
                         opportunity_log_obj=None, **extra):
    trade_manager.log_decision(
        ticker,
        side,
        action,
        reason,
        edge=edge,
        price_cents=price_cents,
        **extra,
    )
    return _record_crypto_opportunity(
        ticker,
        side,
        action,
        reason,
        opportunity_stage="decision",
        opportunity_log_obj=opportunity_log_obj,
        edge=edge,
        price_cents=price_cents,
        **extra,
    )


# === Data Sources ===

def fetch_coinbase_spot(asset="BTC"):
    """Fetch spot price from Coinbase public API.

    Returns price in USD (float), or None on failure.
    """
    try:
        url = f"https://api.coinbase.com/v2/prices/{asset}-USD/spot"
        r = retry_request("GET", url, timeout=10)
        data = r.json()
        price = float(data["data"]["amount"])
        log.info(f"  Coinbase {asset}: ${price:,.2f}")
        return price
    except Exception as e:
        log.error(f"  Coinbase {asset} fetch failed: {e}")
        return None


# Binance symbol mapping (asset -> Binance trading pair)
_BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT",
    "XRP": "XRPUSDT",
}


def fetch_binance_spot(asset="BTC"):
    """Fetch spot price from Binance public API (no auth required).

    Returns price in USD (float), or None on failure.
    """
    try:
        symbol = _BINANCE_SYMBOLS.get(asset.upper())
        if not symbol:
            return None
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        r = retry_request("GET", url, timeout=10)
        data = r.json()
        price = float(data["price"])
        log.info(f"  Binance {asset}: ${price:,.2f}")
        return price
    except Exception as e:
        log.error(f"  Binance {asset} fetch failed: {e}")
        return None


# Ordered list of price sources for fallback
PRICE_SOURCES = [
    ("coinbase", fetch_coinbase_spot),
    ("binance", fetch_binance_spot),
]

# Maximum acceptable price divergence between sources (0.5%)
MAX_PRICE_DIVERGENCE = 0.005


def get_spot_price(asset, health_monitor=None):
    """Fetch spot price with source fallback and cross-validation.

    Tries each price source in order. When multiple sources succeed,
    flags divergence > 0.5% as potential stale data.

    Args:
        asset: Asset symbol (e.g., "BTC", "ETH").
        health_monitor: Optional HealthCheckMonitor for source tracking.

    Returns:
        tuple: (price, source_name) or raises RuntimeError if all fail.
    """
    prices = {}
    for name, fetcher in PRICE_SOURCES:
        try:
            price = fetcher(asset)
            if price and price > 0:
                prices[name] = price
                if health_monitor:
                    health_monitor.record_source_success(name)
        except Exception as e:
            log.warning(f"Price source {name} failed for {asset}: {e}")
            if health_monitor:
                health_monitor.record_source_error(name, str(e))

    if not prices:
        raise RuntimeError(f"All price sources failed for {asset}")

    # Cross-validate: flag divergence
    if len(prices) >= 2:
        price_list = list(prices.values())
        mid = sum(price_list) / len(price_list)
        for name, p in prices.items():
            if mid > 0 and abs(p - mid) / mid > MAX_PRICE_DIVERGENCE:
                log.warning(f"  Price divergence: {name} {asset}=${p:,.2f} vs avg=${mid:,.2f} "
                           f"({abs(p-mid)/mid*100:.2f}% off)")

    # Return the first successful source (priority order)
    for name, _ in PRICE_SOURCES:
        if name in prices:
            return prices[name], name

    # Shouldn't reach here, but safety
    name = next(iter(prices))
    return prices[name], name


def fetch_deribit_iv(asset="BTC"):
    """Fetch implied volatility from Deribit DVOL index.

    Uses the volatility_index_data endpoint which returns the DVOL
    (Deribit Volatility Index) — a 30-day forward-looking IV measure.

    Returns annualized IV as decimal (e.g. 0.55 = 55%), or None on failure.
    """
    try:
        currency = asset.upper()
        if currency not in ("BTC", "ETH"):
            return None
        now_ms = int(time.time() * 1000)
        # Fetch last 2 hours of hourly DVOL data
        start_ms = now_ms - 2 * 3600 * 1000
        url = (
            f"https://deribit.com/api/v2/public/get_volatility_index_data"
            f"?currency={currency}&start_timestamp={start_ms}"
            f"&end_timestamp={now_ms}&resolution=3600"
        )
        r = retry_request("GET", url, timeout=10)
        data = r.json()
        iv = parse_dvol_response(data)
        if iv is None:
            log.info(f"  Deribit DVOL: no valid data for {asset}")
            return None
        log.info(f"  Deribit DVOL {asset}: {iv*100:.1f}%")
        return iv
    except Exception as e:
        log.error(f"  Deribit IV fetch failed for {asset}: {e}")
        return None


def yang_zhang_vol(price_series, bar_seconds=3600):
    """Yang-Zhang volatility estimator using synthetic OHLC bars.

    Yang-Zhang is more sample-efficient than close-to-close and less
    sensitive to microstructure noise (bid-ask bounce, discrete price levels).

    Aggregates (timestamp, price) observations into OHLC bars, then computes:
        sigma_YZ^2 = sigma_O^2 + k * sigma_C^2 + (1-k) * sigma_RS^2

    where:
        sigma_O^2 = overnight variance (open-to-open log returns)
        sigma_C^2 = close-to-close variance
        sigma_RS^2 = Rogers-Satchell intraday variance
        k = 0.34 / (1.34 + (n+1)/(n-1))

    Args:
        price_series: List of (timestamp, price) tuples, sorted by time.
        bar_seconds: Bar duration in seconds (default 1 hour).

    Returns:
        float: Annualized volatility as decimal, or None if insufficient data.
    """
    if len(price_series) < 6:
        return None

    # Aggregate into OHLC bars
    bars = []  # list of (open, high, low, close) dicts
    current_bar_start = None
    bar_open = bar_high = bar_low = bar_close = None

    for ts, price in price_series:
        bar_idx = int(ts // bar_seconds)
        if current_bar_start is None or bar_idx != current_bar_start:
            # Save previous bar
            if current_bar_start is not None and bar_open is not None:
                bars.append({"open": bar_open, "high": bar_high,
                            "low": bar_low, "close": bar_close})
            # Start new bar
            current_bar_start = bar_idx
            bar_open = price
            bar_high = price
            bar_low = price
            bar_close = price
        else:
            bar_high = max(bar_high, price)
            bar_low = min(bar_low, price)
            bar_close = price

    # Save last bar
    if bar_open is not None:
        bars.append({"open": bar_open, "high": bar_high,
                    "low": bar_low, "close": bar_close})

    n = len(bars)
    if n < 3:
        return None

    # Compute log returns
    log_open = [math.log(bars[i]["open"]) for i in range(n)]
    log_close = [math.log(bars[i]["close"]) for i in range(n)]
    log_high = [math.log(bars[i]["high"]) for i in range(n)]
    log_low = [math.log(bars[i]["low"]) for i in range(n)]

    # Overnight returns (open-to-open)
    o_returns = [log_open[i] - log_open[i-1] for i in range(1, n)]
    # Close-to-close returns
    c_returns = [log_close[i] - log_close[i-1] for i in range(1, n)]

    m = len(o_returns)
    if m < 2:
        return None

    # Overnight variance
    o_mean = sum(o_returns) / m
    sigma_o2 = sum((r - o_mean) ** 2 for r in o_returns) / (m - 1)

    # Close variance
    c_mean = sum(c_returns) / m
    sigma_c2 = sum((r - c_mean) ** 2 for r in c_returns) / (m - 1)

    # Rogers-Satchell intraday variance
    rs_terms = []
    for i in range(n):
        h = log_high[i] - log_open[i]
        l = log_low[i] - log_open[i]
        c = log_close[i] - log_open[i]
        rs = h * (h - c) + l * (l - c)
        rs_terms.append(rs)
    sigma_rs2 = sum(rs_terms) / n if n > 0 else 0.0

    # Yang-Zhang optimal k
    k = 0.34 / (1.34 + (m + 1) / max(1, m - 1))

    # Combined Yang-Zhang variance (per bar)
    sigma_yz2 = sigma_o2 + k * sigma_c2 + (1 - k) * max(0.0, sigma_rs2)

    if sigma_yz2 <= 0:
        return None

    # Annualize
    bars_per_year = 365.25 * 86400 / bar_seconds
    annualized = math.sqrt(sigma_yz2 * bars_per_year)
    return max(0.10, min(3.0, annualized))


def compute_realized_vol(asset, current_price=None, lookback_seconds=86400):
    """Compute realized volatility from recent price observations.

    Args:
        asset: Asset symbol (e.g., "BTC", "ETH").
        current_price: If provided, records a new observation before computing.
        lookback_seconds: Window for vol computation (default 24h).

    Returns annualized vol as decimal, or None if insufficient data.
    """
    now = time.time()

    # Record current observation (only if price provided)
    if current_price is not None:
        if asset not in _price_history:
            _price_history[asset] = []
        _price_history[asset].append((now, current_price))
        # Prune to 7 days regardless of lookback (keep full history for flexibility)
        cutoff_7d = now - 604800
        _price_history[asset] = [(t, p) for t, p in _price_history[asset] if t > cutoff_7d]
        _save_price_history()

    # Compute vol with requested lookback
    cutoff = now - lookback_seconds
    history = [(t, p) for t, p in _price_history.get(asset, []) if t > cutoff]
    if len(history) < 5:
        return None

    # Compute raw log returns
    log_returns = []
    for i in range(1, len(history)):
        dt = history[i][0] - history[i-1][0]
        if dt > 0 and history[i-1][1] > 0:
            lr = math.log(history[i][1] / history[i-1][1])
            log_returns.append((lr, dt))

    if len(log_returns) < 3:
        return None

    # Correct approach: normalize returns to common interval, then annualize std dev.
    # Old approach (annualize each return then take std) has Jensen's inequality bias.
    median_dt = sorted(dt for _, dt in log_returns)[len(log_returns) // 2]
    normalized = [lr * math.sqrt(median_dt / dt) for lr, dt in log_returns]
    mean = sum(normalized) / len(normalized)
    variance = sum((r - mean) ** 2 for r in normalized) / (len(normalized) - 1)
    intervals_per_year = 365.25 * 86400 / median_dt
    vol = math.sqrt(variance * intervals_per_year)

    return max(0.10, min(3.0, vol))  # clamp to reasonable range


def estimate_time_to_settlement(market):
    """Estimate minutes until market settlement.

    Returns minutes (int), or 1440 (1 day) as default.
    """
    close_time = market.get("close_time") or market.get("expected_expiration_time")
    if close_time:
        try:
            close_dt = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = close_dt - now
            return max(0, int(delta.total_seconds() / 60))
        except (ValueError, TypeError):
            pass
    return 1440


def _parse_bracket_range(ticker, asset, markets):
    """Parse bracket range from market data. Falls back to defaults."""
    for m in markets:
        if m.get("ticker") == ticker:
            title = m.get("title", "") + " " + m.get("subtitle", "")
            nums = re.findall(r'\$([\d,]+)', title)
            if len(nums) >= 2:
                try:
                    low = int(nums[0].replace(',', ''))
                    high = int(nums[1].replace(',', ''))
                    if high > low > 0:
                        return high - low
                except ValueError:
                    pass
            break
    return 1000 if asset == "BTC" else 100  # fallback defaults



# === Helper functions (extracted from scan_and_trade for testability) ===

def parse_dvol_response(data):
    """Parse a Deribit DVOL API response into an IV decimal.

    Returns annualized IV as decimal (e.g. 0.55), or None if data is
    empty/missing or IV is out of the 10%-300% sanity range.
    """
    result = data.get("result", {})
    points = result.get("data", [])
    if not points:
        return None
    latest_close = points[-1][4]
    iv = latest_close / 100.0
    if iv < 0.10 or iv > 3.0:
        return None
    return iv


def blend_vol(iv, rv, default_vol=0.50, garch_forecast=None, w_iv=0.6, w_rv=0.4):
    """Blend available volatility estimates into a single vol.

    Priority: (iv+rv) weighted blend > iv only > garch > rv-heavy blend > default.
    """
    if iv is not None and rv is not None:
        return w_iv * iv + w_rv * rv
    elif iv is not None:
        return iv
    elif garch_forecast is not None:
        return garch_forecast
    elif rv is not None:
        return 0.3 * default_vol + 0.7 * rv
    else:
        return default_vol


def select_rv_lookback(minutes_to_settle):
    """Select RV lookback window based on market horizon.

    Returns lookback in seconds: 1h for <=30min, 6h for <=2h, 24h for longer.
    """
    if minutes_to_settle <= 30:
        return 3600
    elif minutes_to_settle <= 120:
        return 6 * 3600
    else:
        return 86400


def bracket_eligible(market, max_spread=15, min_volume=5):
    """Check if a bracket market has sufficient liquidity.

    Returns True if EITHER side has a tight spread, or if the market
    has recent trades with sufficient volume.
    """
    yes_bid = market.get("yes_bid", 0) or 0
    yes_ask = market.get("yes_ask", 0) or 0
    no_bid = market.get("no_bid", 0) or 0
    no_ask = market.get("no_ask", 0) or 0
    volume = market.get("volume", 0) or 0

    yes_spread = (yes_ask - yes_bid) if (yes_bid and yes_ask) else 999
    no_spread = (no_ask - no_bid) if (no_bid and no_ask) else 999
    b_spread = min(yes_spread, no_spread)

    has_recent_trade = bool(market.get("last_price"))
    if b_spread > max_spread and not (has_recent_trade and volume >= 10):
        return False
    if volume < min_volume:
        return False
    return True


def get_market_price(market):
    """Extract usable price from market data with fallback chain.

    Returns price in cents, or None if no usable price found.
    Priority: yes_ask -> implied from no_ask -> last_price.
    """
    yes_ask = market.get("yes_ask", 0) or 0
    no_ask = market.get("no_ask", 0) or 0
    last_price = market.get("last_price", 0) or 0

    if yes_ask and 1 <= yes_ask < 99:
        return yes_ask
    elif no_ask and 1 <= no_ask < 99:
        return 100 - no_ask
    elif last_price and 1 <= last_price < 99:
        return last_price
    else:
        return None


def apply_drift(drift, minutes_to_settle):
    """Zero out drift for sub-daily markets where it's negligible noise."""
    if minutes_to_settle < 1440:
        return 0.0
    return drift


def get_regime_position_limit(regime, max_position=10):
    """Compute regime-aware maximum position size.

    Reduces position limits in high-vol and trending regimes where
    the model is less reliable.

    Args:
        regime: Current regime string from RegimeDetector.
        max_position: Base maximum position (contracts).

    Returns:
        int: Adjusted position limit.
    """
    regime_limits = {
        "low_vol": 1.0,      # Full position in calm markets
        "normal": 1.0,       # Full position in normal conditions
        "high_vol": 0.50,    # Half position in high vol
        "crisis": 0.25,      # Quarter position in crisis
    }
    mult = regime_limits.get(regime, 1.0)
    return max(1, int(max_position * mult))


def compute_model_shift(entry_prob, current_prob, shift_threshold=0.20):
    """Detect significant model probability shift since trade entry.

    A large probability shift suggests the model's assessment has changed
    materially, potentially invalidating the original trade thesis.

    Args:
        entry_prob: Model probability at time of entry (0-1).
        current_prob: Current model probability (0-1).
        shift_threshold: Minimum shift (in probability points) to flag (default 20pp).

    Returns:
        dict with keys:
            'shifted': bool - True if shift exceeds threshold
            'shift_pp': float - Shift in probability points
            'direction': str - "favorable" or "adverse" relative to the trade
    """
    shift = current_prob - entry_prob
    abs_shift = abs(shift)
    # A positive shift is favorable for yes-side trades, adverse for no-side
    return {
        "shifted": abs_shift > shift_threshold,
        "shift_pp": round(shift * 100, 1),
        "abs_shift_pp": round(abs_shift * 100, 1),
        "direction": "favorable" if shift > 0 else "adverse" if shift < 0 else "flat",
    }


def select_order_price(side, yes_bid, yes_ask, no_ask, edge, model_fair_value_cents,
                       max_spread=10):
    """Select order price based on spread width for better execution.

    For narrow spreads (<=max_spread): use ask price (maximize fill rate).
    For wide spreads: use model fair value as limit price (avoid overpaying).

    Args:
        side: "yes" or "no".
        yes_bid: Current yes bid in cents.
        yes_ask: Current yes ask in cents.
        no_ask: Current no ask in cents.
        edge: Model edge (decimal).
        model_fair_value_cents: Model's estimate of fair value in cents.
        max_spread: Maximum spread (cents) for market orders.

    Returns:
        int: Order price in cents, or None if no valid price.
    """
    yes_bid = yes_bid or 0
    yes_ask = yes_ask or 0
    no_ask = no_ask or 0

    if side == "yes":
        if not yes_ask or yes_ask <= 0:
            return None
        spread = yes_ask - yes_bid if yes_bid > 0 else 999
        if spread <= max_spread:
            # Narrow spread: take the ask for fill rate
            return yes_ask
        else:
            # Wide spread: use limit price at model fair value
            # Ensure we don't bid above the ask (would be market order)
            limit = min(model_fair_value_cents, yes_ask)
            # And not below the bid (would never fill)
            if yes_bid > 0:
                limit = max(limit, yes_bid + 1)
            return max(1, min(99, limit))
    else:
        if not no_ask or no_ask <= 0:
            # Derive from yes side
            if yes_bid > 0:
                no_ask = 100 - yes_bid
            else:
                return None
        no_bid = 100 - yes_ask if yes_ask > 0 else 0
        spread = no_ask - no_bid if no_bid > 0 else 999
        if spread <= max_spread:
            return no_ask
        else:
            no_fair = 100 - model_fair_value_cents
            limit = min(no_fair, no_ask)
            if no_bid > 0:
                limit = max(limit, no_bid + 1)
            return max(1, min(99, limit))


def apply_kelly_multipliers(base_count, multipliers, floor_pct=0.25):
    """Apply Kelly multipliers — delegates to shared probability.py implementation."""
    return int(_shared_apply_kelly_multipliers(base_count, multipliers, floor_pct))


def compute_correlation_multiplier(max_positive_corr):
    """Compute Kelly multiplier based on maximum positive cross-asset correlation.

    Only POSITIVE correlation (concentration risk) should reduce position size.
    Negative correlation is a diversification benefit and should not be penalized.

    Args:
        max_positive_corr: Maximum positive pairwise correlation with other assets.
            Should be >= 0 (negative correlations are filtered out before calling).

    Returns:
        float: Multiplier in [0.1, 1.0]. Applied to Kelly fraction.
            corr <= 0.5 -> 1.0 (no reduction)
            corr = 0.75 -> 0.85
            corr = 1.0  -> 0.70
    """
    if max_positive_corr <= 0.5:
        return 1.0
    raw = 1.0 - 0.3 * (max_positive_corr - 0.5) / 0.5
    return max(0.1, min(1.0, raw))


def finalize_position_size(count, price_cents, bankroll_cents, multipliers=None, max_cost_cents=None):
    """Apply final caps/multipliers and return synchronized count+risk."""
    if count <= 0 or price_cents <= 0:
        return 0, 0

    max_exposure = max(500, int((bankroll_cents or 50000) * 0.02))
    hard_cap = min(max_exposure, max_cost_cents) if max_cost_cents else max_exposure

    if count * price_cents > hard_cap:
        count = max(1, hard_cap // price_cents)

    if multipliers:
        count = apply_kelly_multipliers(count, multipliers, floor_pct=0.25)
        if count * price_cents > hard_cap:
            count = max(1, hard_cap // price_cents)

    return count, count * price_cents


# === Scanning ===

def scan_and_trade():
    """Scan crypto markets and trade on model edge."""
    now = datetime.datetime.now()
    ss = ScanSummary("crypto", log)
    _refresh_invalid_market_cache()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Crypto scan starting...")

    # Balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")
        ss.finalize()
        return

    # Fetch spot prices (with fallback to Binance if Coinbase fails)
    log.info("\nFetching crypto prices...")
    spot_prices = {}
    for asset in ["BTC", "ETH", "SOL", "DOGE", "XRP"]:
        try:
            price, source = get_spot_price(asset, health_monitor=health)
            spot_prices[asset] = price
            log.debug(f"  {asset} spot from {source}: ${price:,.2f}")
        except RuntimeError:
            health.record_source_error("coinbase", f"{asset} spot unavailable")
            health.record_source_error("binance", f"{asset} spot unavailable")

    if not spot_prices:
        log.info("No spot prices available, skipping scan.")
        ss.source_fail("all_price_sources", "no spot prices")
        ss.finalize()
        return

    # Fetch IV (optional, fall back to realized vol)
    iv_data = {}
    for asset in spot_prices:
        iv = fetch_deribit_iv(asset)
        if iv:
            iv_data[asset] = iv
            health.record_source_success("deribit")

    # Compute realized vol (records observation + 24h vol)
    realized_vols = {}
    for asset, price in spot_prices.items():
        rv = compute_realized_vol(asset, price)
        if rv:
            realized_vols[asset] = rv
            log.info(f"  {asset} realized vol: {rv*100:.1f}%")

    # Update regime detector with latest vol observation
    realized_vol = realized_vols.get("BTC") or realized_vols.get("ETH")
    if realized_vol is not None:
        regime_detector.update(realized_vol)
        regime_detector.save(str(regime_state_path))
        log.info("  Regime: %s (conf=%.2f, mult=%.2f)",
                 regime_detector.current_regime(),
                 regime_detector.regime_confidence(),
                 regime_kelly_multiplier(regime_detector))

    # Update GARCH forecasters with new returns
    for asset, price in spot_prices.items():
        history = _price_history.get(asset, [])
        if len(history) >= 2:
            prev_price = history[-2][1]
            prev_time = history[-2][0]
            if prev_price > 0:
                log_ret = math.log(price / prev_price)
                interval_sec = time.time() - prev_time
                if asset in garch_forecasters:
                    garch_forecasters[asset].update(log_ret, interval_seconds=interval_sec)

    # Update AR(1) vol forecasters
    for asset in spot_prices:
        rv = realized_vols.get(asset)
        if rv is not None and asset in ar1_forecasters:
            ar1_forecasters[asset].update(rv)

    # Update DCC correlation tracker
    returns_for_dcc = {}
    for asset, price in spot_prices.items():
        history = _price_history.get(asset, [])
        if len(history) >= 2:
            prev_price = history[-2][1]
            if prev_price > 0:
                returns_for_dcc[asset] = math.log(price / prev_price)
    if returns_for_dcc:
        dcc_tracker.update(returns_for_dcc)

    # Compute trailing drift per asset
    drift_by_asset = {}
    for asset in spot_prices:
        drift_by_asset[asset] = compute_trailing_drift(asset)
        if drift_by_asset[asset] != 0.0:
            log.info(f"  {asset} trailing drift: {drift_by_asset[asset]*100:.0f}% ann")

    # Fetch crypto markets
    all_markets = []
    for prefix in CRYPTO_PREFIXES:
        try:
            markets = client.get_all_markets(prefix=prefix, cache_ttl=60)
            all_markets.extend(markets)
        except Exception as e:
            log.error(f"Market fetch error for {prefix}: {e}")

    if not all_markets:
        log.info("No open crypto markets found.")
        ss.finalize()
        return

    ss.markets_fetched = len(all_markets)
    log.info(f"Found {len(all_markets)} crypto markets")

    # Evaluate each market
    opportunities = []
    for m in all_markets:
        ticker = m.get("ticker", "")
        if _is_invalid_market(ticker):
            ss.skip("invalid_market_cache")
            continue
        parsed = parse_crypto_ticker(ticker)
        if not parsed:
            ss.skip("unparseable")
            if ss.skips.get("unparseable", 0) <= 5:
                log.info(f"  Unparseable ticker: {ticker} repr={repr(ticker)}")
            continue

        asset = parsed["asset"]
        if asset not in spot_prices:
            ss.skip("no_spot")
            continue

        current_price = spot_prices[asset]
        threshold = parsed["threshold"]
        direction = parsed.get("direction", "T")

        # Bracket markets: gate on liquidity (checks BOTH yes and no sides)
        if direction == "B":
            if not crypto_config.get("enableBrackets", True):
                ss.skip("brackets_disabled")
                continue
            if not bracket_eligible(m):
                ss.skip("bracket_illiquid")
                _log_crypto_decision(
                    ticker,
                    "yes",
                    "skipped",
                    "bracket_illiquid",
                    volume=m.get("volume", 0),
                    asset=asset,
                )
                continue

        # Estimate time to settlement
        minutes_to_settle = estimate_time_to_settlement(m)

        # Skip markets about to settle (avoid last-minute noise)
        if minutes_to_settle < SETTLEMENT_BUFFER_MINUTES:
            ss.skip("settlement_buffer")
            continue

        # Horizon-matched vol lookback
        rv_lookback = select_rv_lookback(minutes_to_settle)

        # Horizon-dependent vol weighting
        w_iv, w_rv = horizon_vol_weights(minutes_to_settle)

        iv = iv_data.get(asset)
        if rv_lookback < 86400:
            rv = compute_realized_vol(asset, lookback_seconds=rv_lookback)
        else:
            rv = realized_vols.get(asset)
        default_vol = DEFAULT_VOLS.get(asset, 0.50)

        # GARCH forecast (if available)
        garch_vol = garch_forecasters.get(asset)
        garch_forecast = garch_vol.forecast_vol(use_actual_interval=True) if garch_vol else None

        # Use best available vol estimate
        vol_to_use = blend_vol(iv, rv, default_vol, garch_forecast, w_iv, w_rv)

        # Intraday seasonality adjustment
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        season_mult = intraday_vol_multiplier(now_utc.hour, now_utc.weekday())
        vol_to_use *= season_mult

        # Vol skew adjustment: OTM options have higher IV than ATM DVOL
        moneyness = current_price / threshold if threshold > 0 else 1.0
        skew_mult = vol_skew_multiplier(moneyness)
        vol_to_use *= skew_mult

        drift = apply_drift(drift_by_asset.get(asset, DRIFT_PCT), minutes_to_settle)
        # Get current regime
        current_regime = regime_detector.current_regime()

        # Heston params from GARCH (dynamic v0, vol-of-vol)
        garch = garch_forecasters.get(asset)
        heston_params = dict(DEFAULT_HESTON_PARAMS)
        if garch:
            v0 = garch.heston_v0()
            if v0 is not None:
                heston_params["v0"] = v0
            vov = garch.vol_of_vol()
            if vov is not None:
                heston_params["xi"] = max(0.1, min(2.0, vov))

        ou_tgt = compute_ou_target(asset)
        ou_shadow_prob = None  # shadow OU probability for dual logging

        if direction == "T":
            prob = ensemble_model.estimate_prob(
                current_price=current_price, threshold=threshold,
                direction="above", time_horizon_minutes=minutes_to_settle,
                vol=vol_to_use, regime=current_regime,
                drift_pct=drift, heston_params=heston_params,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
                ou_target=ou_tgt,
            )
            # Shadow OU computation: when OU is disabled, also compute OU prob for comparison
            if not USE_OU and ou_tgt is not None:
                ou_shadow_prob = ensemble_model.estimate_prob(
                    current_price=current_price, threshold=threshold,
                    direction="above", time_horizon_minutes=minutes_to_settle,
                    vol=vol_to_use, regime=current_regime,
                    drift_pct=drift, heston_params=heston_params,
                    use_ou=True, ou_half_life_minutes=OU_HALF_LIFE,
                    ou_target=ou_tgt,
                )
                log.debug(f"  OU shadow: {ticker} GBM={prob:.3f} OU={ou_shadow_prob:.3f} delta={ou_shadow_prob-prob:+.3f}")
        else:
            # Bracket
            range_size = _parse_bracket_range(ticker, asset, all_markets)
            prob = ensemble_model.estimate_bracket_prob(
                current_price=current_price,
                low_threshold=threshold,
                high_threshold=threshold + range_size,
                time_horizon_minutes=minutes_to_settle,
                vol=vol_to_use, regime=current_regime,
                drift_pct=drift, heston_params=heston_params,
            )

        # Particle filter: update belief state and use filtered prob
        pf = filter_mgr.get_filter(ticker)
        pf.update(prob)
        filtered_est = pf.estimate()
        raw_prob = prob
        prob = filtered_est.prob  # use filtered probability for edge computation
        research_fields = _crypto_research_fields(
            parsed,
            current_price=current_price,
            minutes_to_settle=minutes_to_settle,
            vol_used=vol_to_use,
            raw_prob=raw_prob,
            filtered_est=filtered_est,
            current_regime=current_regime,
            drift_pct=drift,
            iv=iv,
            rv=rv,
            garch_forecast=garch_forecast,
            season_mult=season_mult,
            skew_mult=skew_mult,
            use_ou=USE_OU,
            ou_half_life_minutes=OU_HALF_LIFE,
            ou_target=ou_tgt,
            ou_shadow_prob=ou_shadow_prob,
            heston_params=heston_params,
        )

        yes_ask = m.get("yes_ask", 0) or 0
        no_ask = m.get("no_ask", 0) or 0
        yes_bid = m.get("yes_bid", 0) or 0

        market_price = get_market_price(m)
        if market_price is None:
            ss.skip("no_price")
            continue

        if not is_market_liquid(m):
            ss.skip("illiquid")
            continue

        ss.markets_evaluated += 1

        # Determine trade direction and edge
        # Compare FEE-ADJUSTED edge against threshold to avoid entering with negative net edge
        if prob > 0.5:
            eff_threshold = smooth_edge_threshold(prob, base=EDGE_THRESHOLD)
            edge = prob - market_price / 100
            fee_pp = kalshi_fee_cents(market_price) / 100  # fee as probability points
            net_edge = edge - fee_pp
            if net_edge > eff_threshold:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "yes",
                    "prob": prob, "edge": edge, "asset": asset,
                    "current_price": current_price, "threshold": threshold,
                    "minutes_to_settle": minutes_to_settle,
                    "vol_used": vol_to_use,
                    "is_bracket": direction == "B",
                    "raw_prob": raw_prob, "filtered_est": filtered_est,
                    "ou_shadow_prob": ou_shadow_prob,
                    "research_fields": research_fields,
                })
            else:
                reason = "net edge below threshold" if net_edge <= eff_threshold else "edge below threshold"
                _log_crypto_decision(
                    ticker,
                    "yes",
                    "skipped",
                    reason,
                    edge=edge,
                    net_edge=round(net_edge, 4),
                    fee_pp=round(fee_pp, 4),
                    price_cents=market_price,
                    asset=asset,
                    vol_used=round(vol_to_use, 4),
                    ou_shadow_prob=round(ou_shadow_prob, 4) if ou_shadow_prob is not None else None,
                    **research_fields,
                )
        elif prob <= 0.5:
            no_prob = 1.0 - prob
            eff_threshold = smooth_edge_threshold(no_prob, base=EDGE_THRESHOLD)
            # Use no_ask directly if available, else derive from market_price
            no_price = no_ask if no_ask else (100 - market_price)
            edge = no_prob - no_price / 100
            fee_pp = kalshi_fee_cents(no_price) / 100
            net_edge = edge - fee_pp
            if net_edge > eff_threshold:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "no",
                    "prob": prob, "edge": edge, "asset": asset,
                    "current_price": current_price, "threshold": threshold,
                    "minutes_to_settle": minutes_to_settle,
                    "vol_used": vol_to_use,
                    "is_bracket": direction == "B",
                    "raw_prob": raw_prob, "filtered_est": filtered_est,
                    "ou_shadow_prob": ou_shadow_prob,
                    "research_fields": research_fields,
                })
            else:
                reason = "net edge below threshold" if net_edge <= eff_threshold else "edge below threshold"
                _log_crypto_decision(
                    ticker,
                    "no",
                    "skipped",
                    reason,
                    edge=edge,
                    net_edge=round(net_edge, 4),
                    fee_pp=round(fee_pp, 4),
                    price_cents=no_ask,
                    asset=asset,
                    vol_used=round(vol_to_use, 4),
                    ou_shadow_prob=round(ou_shadow_prob, 4) if ou_shadow_prob is not None else None,
                    **research_fields,
                )

    # Sort by edge
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    log.info(f"Found {len(opportunities)} opportunities with edge >= {EDGE_THRESHOLD*100:.0f}%")

    # Clean up expired particle filters
    active_tickers = {m.get("ticker", "") for m in all_markets}
    filter_mgr.cleanup(active_tickers)

    for opp in opportunities:
        ticker = opp["ticker"]
        side = opp["side"]
        edge = opp["edge"]
        m = opp["market"]
        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)

        # opp["prob"] is always P(YES); compute side-appropriate confidence
        side_confidence = opp["prob"] if side == "yes" else 1.0 - opp["prob"]
        budget = allocator.request_budget("crypto", ticker, edge=edge, confidence=side_confidence)
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            ss.skip("allocator_denied")
            _ou_sp = opp.get("ou_shadow_prob")
            _log_crypto_decision(
                ticker,
                side,
                "skipped",
                f"allocator denied: {budget.reason}",
                edge=edge,
                price_cents=yes_ask if side == "yes" else no_ask,
                asset=opp["asset"],
                vol_used=round(opp["vol_used"], 4),
                ou_shadow_prob=round(_ou_sp, 4) if _ou_sp is not None else None,
                **opp.get("research_fields", {}),
            )
            continue

        if opp.get("is_bracket"):
            price = yes_ask  # Bracket: always use ask for fill rate
        else:
            price = compute_limit_price(yes_bid, yes_ask, side, edge=edge) or (yes_ask if side == "yes" else no_ask)
        if not price or price <= 0:
            continue

        # Horizon-scaled Kelly fraction
        kelly_frac = horizon_kelly_fraction(opp["minutes_to_settle"])
        fee = kalshi_fee_cents(price)

        # Use half_kelly with manual fraction scaling
        count, risk, kelly_details = half_kelly(
            edge, price, budget.max_cost_cents,
            bankroll_cents=budget.bankroll_cents, fee_cents=fee,
            return_details=True,
        )
        # Scale from half-Kelly to horizon-appropriate fraction
        count = max(0, int(count * kelly_frac / 0.5))

        # CI-aware sizing: reduce position when filter is uncertain
        filtered_est = opp["filtered_est"]
        kelly_mult = ci_kelly_multiplier(filtered_est)
        regime_mult = regime_kelly_multiplier(regime_detector)

        # Correlation adjustment: reduce if heavily correlated with existing positions
        # Only penalize POSITIVE correlation (concentration risk).
        # Negative correlation = diversification benefit, not penalized.
        corr_mult = 1.0
        corr_matrix = dcc_tracker.correlation_matrix()
        if corr_matrix is not None:
            max_pos_corr = 0.0
            for other_asset in spot_prices:
                if other_asset != opp["asset"]:
                    rho = dcc_tracker.pair_correlation(opp["asset"], other_asset)
                    if rho is not None and rho > 0:  # Only positive correlation = risk
                        max_pos_corr = max(max_pos_corr, rho)
            corr_mult = compute_correlation_multiplier(max_pos_corr)

        count, risk = finalize_position_size(
            count,
            price,
            budget.bankroll_cents,
            multipliers=[kelly_mult, regime_mult, corr_mult],
            max_cost_cents=trade_manager._effective_max_trade_cents(),
        )

        if count <= 0:
            ss.skip("kelly_zero")
            _ou_sp = opp.get("ou_shadow_prob")
            _log_crypto_decision(
                ticker,
                side,
                "skipped",
                "kelly_zero: edge too small for price",
                edge=edge,
                price_cents=price,
                asset=opp["asset"],
                vol_used=round(opp["vol_used"], 4),
                ou_shadow_prob=round(_ou_sp, 4) if _ou_sp is not None else None,
                **opp.get("research_fields", {}),
            )
            continue

        # Determine vol_source for trade record
        iv = iv_data.get(opp["asset"])
        rv = realized_vols.get(opp["asset"])
        if iv is not None and rv is not None:
            vol_source = "iv+rv"
        elif iv is not None:
            vol_source = "iv_only"
        elif rv is not None:
            vol_source = "rv_only"
        else:
            vol_source = "default"

        reasoning = (
            f"Crypto {opp['asset']}: spot ${opp['current_price']:,.0f} vs threshold ${opp['threshold']:,.0f}, "
            f"vol={opp['vol_used']*100:.0f}%, T={opp['minutes_to_settle']}min, "
            f"P(YES)={opp['prob']*100:.0f}%, {side} edge={edge*100:.1f}%"
        )

        validated_market = _validate_tradeable_market(ticker)
        if not validated_market:
            ss.skip("invalid_market")
            _log_crypto_decision(
                ticker,
                side,
                "skipped",
                "market unavailable before order",
                edge=edge,
                price_cents=price,
                asset=opp["asset"],
                **opp.get("research_fields", {}),
            )
            continue

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c on {ticker} ({kelly_frac:.0%}-Kelly)")

        result = trade_manager.place_order(ticker, side, price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(opp["prob"] if side == "yes" else 1.0 - opp["prob"], 4), raw_edge=round(edge, 4),
                                            fee_cents=round(kalshi_fee_cents(price), 2), sizing_method=f"horizon_{kelly_frac:.0%}_kelly",
                                            market_close_time=m.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"),
                                            vol_source=vol_source,
                                            current_price=opp["current_price"],
                                            threshold=opp["threshold"],
                                            minutes_to_settle=opp["minutes_to_settle"],
                                            asset=opp["asset"],
                                            vol_used=round(opp["vol_used"], 4),
                                            bracket=opp.get("is_bracket", False),
                                            pf_prob=round(filtered_est.prob, 4),
                                            pf_ci_low=round(filtered_est.ci_low, 4),
                                            pf_ci_high=round(filtered_est.ci_high, 4),
                                            pf_trend=filtered_est.trend,
                                            pf_updates=filtered_est.n_updates,
                                            pf_kelly_mult=round(kelly_mult, 4),
                                            regime_mult=round(regime_mult, 4),
                                            regime=regime_detector.current_regime(),
                                            ou_shadow_prob=round(opp["ou_shadow_prob"], 4) if opp.get("ou_shadow_prob") is not None else None,
                                            **opp.get("research_fields", {}))
        if result:
            ss.trades_placed += 1
            allocator.record_trade("crypto", ticker, result.get("cost_cents", risk), edge=edge)
        elif trade_manager.last_error_code == "market_not_found":
            _mark_invalid_market(ticker, trade_manager.last_error_message or "market_not_found")
            ss.skip("market_not_found")

    # Save particle filter state
    filter_mgr.save_all()

    ss.finalize()


def _check_short_horizon_markets():
    """Check if any active crypto markets settle within 2 hours."""
    try:
        for prefix in CRYPTO_PREFIXES:
            markets = client.get_all_markets(prefix=prefix, cache_ttl=60)
            for m in markets:
                mins = estimate_time_to_settlement(m)
                if SETTLEMENT_BUFFER_MINUTES < mins < 120:
                    return True
    except Exception:
        pass
    return False


# === Entry Point ===

def load_config(project_dir=None):
    project_dir = Path(project_dir or PROJECT_DIR)
    return json.loads((project_dir / "config" / "bots-config.json").read_text())


def build_app(project_dir=None):
    project_dir = Path(project_dir or PROJECT_DIR)
    setup_unbuffered()
    logger = setup_logging("crypto")
    setup_signal_handlers()

    bots_config_path = project_dir / "config" / "bots-config.json"
    trades_path = project_dir / "data" / "kalshi-crypto-trades.json"
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    loaded_bots_config = load_config(project_dir)
    loaded_crypto_config = loaded_bots_config.get("crypto", {})

    max_trade = loaded_crypto_config.get("maxTradeAmount", 10)
    max_daily_trades = loaded_crypto_config.get("maxDailyTrades", 30)
    max_daily_loss = loaded_crypto_config.get("maxDailyLoss", 25)
    scan_interval = loaded_crypto_config.get("scanIntervalMinutes", 5)
    edge_threshold = loaded_crypto_config.get("edgeThreshold", 0.06)
    settlement_buffer_minutes = max(3, loaded_crypto_config.get("settlementBufferMinutes", 3))
    use_ou = loaded_crypto_config.get("useOrnsteinUhlenbeck", False)
    ou_half_life = loaded_crypto_config.get("ouHalfLifeMinutes", 120)
    drift_pct = loaded_crypto_config.get("driftPct", 0.0)

    client_obj = KalshiClient()
    allocator_obj = PortfolioAllocator(client_obj, logger=logger)
    health_monitor = HealthCheckMonitor(logger=logger)
    order_monitor_obj = OrderMonitor(client_obj, log=logger)
    trade_manager_obj = TradeManager(client_obj, trades_path, {
        "maxTradeAmount": max_trade,
        "maxTradeAmountPct": loaded_crypto_config.get("maxTradeAmountPct"),
        "maxDailyTrades": max_daily_trades,
        "maxDailyLoss": max_daily_loss,
        "maxDailyLossPct": loaded_crypto_config.get("maxDailyLossPct"),
    }, logger=logger, cooldown_hours=0.5, order_monitor=order_monitor_obj, bot_name="crypto")
    opportunity_log_obj = OpportunityLog(
        project_dir / "data" / "opportunity-log.json",
        logger=logger,
        strategy_id=trade_manager_obj.strategy_id,
        config_version=getattr(trade_manager_obj, "_config_version", None),
        model_registry=getattr(trade_manager_obj, "_model_registry", None),
        source_bot=getattr(logger, "name", None),
        source_path=trades_path,
    )
    trim_trade_log(trades_path)

    particle_filter_config = FilterConfig(
        n_particles=loaded_crypto_config.get("pfParticles", 200),
        process_noise=loaded_crypto_config.get("pfProcessNoise", 0.02),
        observation_noise=loaded_crypto_config.get("pfObservationNoise", 0.05),
    )
    filter_manager = FilterManager(
        bot_name="crypto",
        state_dir=project_dir / "data",
        default_config=particle_filter_config,
    )
    pf_max_age = int(loaded_crypto_config.get("pfStalenessHours", 24) * 3600)
    filter_manager.load_all(max_age_seconds=pf_max_age)

    regime_state = project_dir / "data" / "regime-state.json"
    regime_detector_obj = RegimeDetector()
    regime_detector_obj.load(str(regime_state))

    calibration_path = project_dir / "config" / "crypto-calibration.json"
    calibration = {}
    if calibration_path.exists():
        try:
            calibration = json.loads(calibration_path.read_text())
        except (json.JSONDecodeError, OSError):
            calibration = {}

    if calibration.get("assets"):
        ensemble_model_obj = EnsembleModel.from_calibration(calibration, asset="BTC")
        logger.info("Loaded calibrated ensemble model from crypto-calibration.json")
    else:
        ensemble_model_obj = EnsembleModel()

    invalid_markets_path = project_dir / "data" / "crypto-invalid-markets.json"
    invalid_market_ttl_seconds = int(loaded_crypto_config.get("invalidMarketTtlHours", 6) * 3600)
    price_history_path = project_dir / "data" / "crypto-price-history.json"

    context = AppContext({
        "PROJECT_DIR": project_dir,
        "BOTS_CONFIG_PATH": bots_config_path,
        "TRADES_PATH": trades_path,
        "bots_config": loaded_bots_config,
        "crypto_config": loaded_crypto_config,
        "MAX_TRADE": max_trade,
        "MAX_DAILY_TRADES": max_daily_trades,
        "MAX_DAILY_LOSS": max_daily_loss,
        "SCAN_INTERVAL": scan_interval,
        "EDGE_THRESHOLD": edge_threshold,
        "SETTLEMENT_BUFFER_MINUTES": settlement_buffer_minutes,
        "USE_OU": use_ou,
        "OU_HALF_LIFE": ou_half_life,
        "DRIFT_PCT": drift_pct,
        "log": logger,
        "client": client_obj,
        "allocator": allocator_obj,
        "health": health_monitor,
        "order_monitor": order_monitor_obj,
        "trade_manager": trade_manager_obj,
        "opportunity_log": opportunity_log_obj,
        "pf_config": particle_filter_config,
        "filter_mgr": filter_manager,
        "pf_staleness_seconds": pf_max_age,
        "regime_detector": regime_detector_obj,
        "regime_state_path": regime_state,
        "_calibration_path": calibration_path,
        "_calibration": calibration,
        "ensemble_model": ensemble_model_obj,
        "INVALID_MARKETS_PATH": invalid_markets_path,
        "INVALID_MARKET_TTL_SECONDS": invalid_market_ttl_seconds,
        "_invalid_market_cache": {},
        "garch_forecasters": {asset: GARCHForecaster() for asset in DEFAULT_VOLS},
        "ar1_forecasters": {asset: AR1VolForecast() for asset in DEFAULT_VOLS},
        "dcc_tracker": DCCCorrelation(assets=list(DEFAULT_VOLS.keys())),
        "_price_history": {},
        "_PRICE_HISTORY_PATH": price_history_path,
    })
    install_app_context(globals(), context)
    _refresh_invalid_market_cache()
    _load_price_history()
    return _APP_CONTEXT


def main():
    parser = argparse.ArgumentParser(description="Kalshi Crypto Bot")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()
    build_app()

    if not acquire_process_singleton("crypto", PROJECT_DIR, log):
        log.warning("Duplicate crypto launch blocked; exiting.")
        return

    log.info("=" * 60)
    log.info("Kalshi Crypto Bot (BTC/ETH/SOL/DOGE/XRP)")
    log.info(f"  Max: ${MAX_TRADE}/trade | Edge: {EDGE_THRESHOLD*100:.0f}%")
    log.info(f"  Scan interval: {SCAN_INTERVAL} minutes")
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

    # Daemon loop
    while True:
        try:
            health.record_bot_heartbeat("crypto")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()
            scan_and_trade()
        except Exception as e:
            log.error("Scan error: %s", e, exc_info=True)

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break

        # Adaptive interval: faster when short-horizon markets exist
        has_short_horizon = _check_short_horizon_markets()
        if has_short_horizon:
            interval = max(1, SCAN_INTERVAL // 3)  # ~1.5 min for default 5-min
        else:
            interval = SCAN_INTERVAL
        log.info(f"\nNext scan in {interval} minutes{'  (short-horizon mode)' if has_short_horizon else ''}...")
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
