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

import json, time, datetime, os, sys, re, argparse, traceback, math
import requests
from pathlib import Path
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot,
    HealthCheckMonitor, OrderMonitor, _atomic_write_json, ScanSummary,
    is_shutdown_requested,
)
from probability import (
    half_kelly, compute_limit_price,
    kalshi_fee_cents, is_market_liquid,
)
from ticker_utils import parse_crypto_ticker
from capital_allocator import PortfolioAllocator
from particle_filter import FilterManager, FilterConfig, ci_kelly_multiplier
from regime_detector import RegimeDetector, regime_kelly_multiplier
from crypto_models import EnsembleModel, smooth_edge_threshold, horizon_kelly_fraction, horizon_vol_weights, AR1VolForecast, vol_skew_multiplier
from vol_forecaster import GARCHForecaster, DCCCorrelation, intraday_vol_multiplier, correct_bid_ask_bounce

setup_unbuffered()
log = setup_logging("crypto")
setup_signal_handlers()

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-crypto-trades.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load config
bots_config = json.loads(BOTS_CONFIG_PATH.read_text())
crypto_config = bots_config.get("crypto", {})

MAX_TRADE = crypto_config.get("maxTradeAmount", 10)
MAX_DAILY_TRADES = crypto_config.get("maxDailyTrades", 30)
MAX_DAILY_LOSS = crypto_config.get("maxDailyLoss", 25)
SCAN_INTERVAL = crypto_config.get("scanIntervalMinutes", 5)
EDGE_THRESHOLD = crypto_config.get("edgeThreshold", 0.06)
# Buffer must exceed market cache TTL (60s) to prevent race condition
SETTLEMENT_BUFFER_MINUTES = max(3, crypto_config.get("settlementBufferMinutes", 3))
USE_OU = crypto_config.get("useOrnsteinUhlenbeck", False)
OU_HALF_LIFE = crypto_config.get("ouHalfLifeMinutes", 120)
DRIFT_PCT = crypto_config.get("driftPct", 0.0)

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
order_monitor = OrderMonitor(client, log=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE,
    "maxTradeAmountPct": crypto_config.get("maxTradeAmountPct"),
    "maxDailyTrades": MAX_DAILY_TRADES,
    "maxDailyLoss": MAX_DAILY_LOSS,
    "maxDailyLossPct": crypto_config.get("maxDailyLossPct"),
}, logger=log, cooldown_hours=0.5, order_monitor=order_monitor, bot_name="crypto")  # short cooldown for fast markets
trim_trade_log(TRADES_PATH)

# Particle filter for Bayesian belief tracking
pf_config = FilterConfig(
    n_particles=crypto_config.get("pfParticles", 200),
    process_noise=crypto_config.get("pfProcessNoise", 0.02),
    observation_noise=crypto_config.get("pfObservationNoise", 0.05),
)
filter_mgr = FilterManager(bot_name="crypto", state_dir=PROJECT_DIR / "data",
                            default_config=pf_config)
pf_staleness_seconds = int(crypto_config.get("pfStalenessHours", 24) * 3600)
filter_mgr.load_all(max_age_seconds=pf_staleness_seconds)

# Regime detector
regime_detector = RegimeDetector()
regime_state_path = PROJECT_DIR / "data" / "regime-state.json"
regime_detector.load(str(regime_state_path))

# Ensemble model (loads calibration if available)
_calibration_path = PROJECT_DIR / "config" / "crypto-calibration.json"
_calibration = {}
if _calibration_path.exists():
    try:
        _calibration = json.loads(_calibration_path.read_text())
    except (json.JSONDecodeError, OSError):
        pass

if _calibration.get("assets"):
    # Use BTC calibration as the primary (most liquid, best data)
    ensemble_model = EnsembleModel.from_calibration(_calibration, asset="BTC")
    log.info("Loaded calibrated ensemble model from crypto-calibration.json")
else:
    ensemble_model = EnsembleModel()

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

# Default annualized volatilities (post-ETF era, updated 2026)
DEFAULT_VOLS = {
    "BTC": 0.50,  # post-ETF BTC vol is 40-55%
    "ETH": 0.65,  # ETH vol tracks BTC more closely now
    "SOL": 0.80,
    "DOGE": 0.90,  # meme coin, high vol
    "XRP": 0.75,   # mid-cap alt, moderate-high vol
}

# Initialize forecasters now that DEFAULT_VOLS is defined
garch_forecasters = {asset: GARCHForecaster() for asset in DEFAULT_VOLS}
ar1_forecasters = {asset: AR1VolForecast() for asset in DEFAULT_VOLS}
dcc_tracker = DCCCorrelation(assets=list(DEFAULT_VOLS.keys()))

# Recent price cache for realized vol computation
_price_history = {}  # asset -> [(timestamp, price), ...]
_PRICE_HISTORY_PATH = PROJECT_DIR / "data" / "crypto-price-history.json"


def _load_price_history():
    """Load price history from disk, keeping only last 24h."""
    global _price_history
    try:
        if _PRICE_HISTORY_PATH.exists():
            data = json.loads(_PRICE_HISTORY_PATH.read_text())
            cutoff = time.time() - 86400
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


_load_price_history()



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
        result = data.get("result", {})
        points = result.get("data", [])
        if not points:
            log.info(f"  Deribit DVOL: no data points for {asset}")
            return None
        # Each point: [timestamp, open, high, low, close]
        latest_close = points[-1][4]
        iv = latest_close / 100.0  # DVOL is in percentage, convert to decimal
        # Sanity clamp: 10%-300%
        if iv < 0.10 or iv > 3.0:
            log.warning(f"  Deribit DVOL {asset} out of range: {iv*100:.1f}%, ignoring")
            return None
        log.info(f"  Deribit DVOL {asset}: {iv*100:.1f}%")
        return iv
    except Exception as e:
        log.error(f"  Deribit IV fetch failed for {asset}: {e}")
        return None


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
        # Prune to 24h regardless of lookback (keep full history for flexibility)
        cutoff_24h = now - 86400
        _price_history[asset] = [(t, p) for t, p in _price_history[asset] if t > cutoff_24h]
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



# === Scanning ===

def scan_and_trade():
    """Scan crypto markets and trade on model edge."""
    now = datetime.datetime.now()
    ss = ScanSummary("crypto", log)
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

    # Fetch spot prices
    log.info("\nFetching crypto prices...")
    spot_prices = {}
    for asset in ["BTC", "ETH", "SOL", "DOGE", "XRP"]:
        price = fetch_coinbase_spot(asset)
        if price:
            spot_prices[asset] = price
            health.record_source_success("coinbase")
        else:
            health.record_source_error("coinbase", f"{asset} spot unavailable")

    if not spot_prices:
        log.info("No spot prices available, skipping scan.")
        ss.source_fail("coinbase", "no spot prices")
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
            if prev_price > 0:
                log_ret = math.log(price / prev_price)
                if asset in garch_forecasters:
                    garch_forecasters[asset].update(log_ret)

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
        parsed = parse_crypto_ticker(ticker)
        if not parsed:
            ss.skip("unparseable")
            if ss.skips.get("unparseable", 0) <= 10:
                log.info(f"  Unparseable ticker: {ticker}")
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
            yes_bid_b = m.get("yes_bid", 0) or 0
            yes_ask_b = m.get("yes_ask", 0) or 0
            no_bid_b = m.get("no_bid", 0) or 0
            no_ask_b = m.get("no_ask", 0) or 0
            b_volume = m.get("volume", 0) or 0

            # Check YES-side spread
            yes_spread = (yes_ask_b - yes_bid_b) if (yes_bid_b and yes_ask_b) else 999
            # Check NO-side spread
            no_spread = (no_ask_b - no_bid_b) if (no_bid_b and no_ask_b) else 999
            # Market is liquid if EITHER side has a tight spread
            b_spread = min(yes_spread, no_spread)

            # Also accept if market has recent trades even with empty book
            has_recent_trade = bool(m.get("last_price"))
            if b_spread > 15 and not (has_recent_trade and b_volume >= 10):
                ss.skip("bracket_illiquid")
                trade_manager.log_decision(ticker, "yes", "skipped", "bracket_illiquid",
                                           spread=b_spread, volume=b_volume, asset=asset)
                continue
            if b_volume < 5:  # Lower volume floor (was 10) since we have spread confirmation
                ss.skip("bracket_illiquid")
                trade_manager.log_decision(ticker, "yes", "skipped", "bracket_illiquid",
                                           spread=b_spread, volume=b_volume, asset=asset)
                continue

        # Estimate time to settlement
        minutes_to_settle = estimate_time_to_settlement(m)

        # Skip markets about to settle (avoid last-minute noise)
        if minutes_to_settle < SETTLEMENT_BUFFER_MINUTES:
            ss.skip("settlement_buffer")
            continue

        # Horizon-matched vol lookback
        if minutes_to_settle <= 30:
            rv_lookback = 3600       # 1h for <=30-min markets
        elif minutes_to_settle <= 120:
            rv_lookback = 6 * 3600   # 6h for hourly markets
        else:
            rv_lookback = 86400      # 24h for daily/weekly

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
        garch_forecast = garch_vol.forecast_vol(annualize_factor=365.25*24*12) if garch_vol else None

        # Use best available vol estimate
        if iv is not None and rv is not None:
            vol_to_use = w_iv * iv + w_rv * rv
        elif iv is not None:
            vol_to_use = iv
        elif garch_forecast is not None:
            vol_to_use = garch_forecast
        elif rv is not None:
            vol_to_use = 0.3 * default_vol + 0.7 * rv
        else:
            vol_to_use = default_vol

        # Intraday seasonality adjustment
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        season_mult = intraday_vol_multiplier(now_utc.hour, now_utc.weekday())
        vol_to_use *= season_mult

        # Vol skew adjustment: OTM options have higher IV than ATM DVOL
        moneyness = current_price / threshold if threshold > 0 else 1.0
        skew_mult = vol_skew_multiplier(moneyness)
        vol_to_use *= skew_mult

        drift = drift_by_asset.get(asset, DRIFT_PCT)
        # Drift is negligible for sub-daily horizons and introduces noise
        if minutes_to_settle < 1440:
            drift = 0.0
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

        if direction == "T":
            prob = ensemble_model.estimate_prob(
                current_price=current_price, threshold=threshold,
                direction="above", time_horizon_minutes=minutes_to_settle,
                vol=vol_to_use, regime=current_regime,
                drift_pct=drift, heston_params=heston_params,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
            )
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

        yes_ask = m.get("yes_ask", 0) or 0
        no_ask = m.get("no_ask", 0) or 0
        yes_bid = m.get("yes_bid", 0) or 0
        last_price = m.get("last_price", 0) or 0

        # Primary: use yes_ask if available and reasonable
        # Fallback 1: compute from no_ask (yes_ask ~ 100 - no_ask)
        # Fallback 2: use last_price as stale reference
        if yes_ask and 1 <= yes_ask < 99:
            market_price = yes_ask
        elif no_ask and 1 <= no_ask < 99:
            market_price = 100 - no_ask  # implied yes price from NO side
        elif last_price and 1 <= last_price < 99:
            market_price = last_price
        else:
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
                })
            else:
                reason = "net edge below threshold" if net_edge <= eff_threshold else "edge below threshold"
                trade_manager.log_decision(
                    ticker, "yes", "skipped", reason,
                    edge=edge, net_edge=round(net_edge, 4), fee_pp=round(fee_pp, 4),
                    price_cents=market_price, asset=asset, vol_used=round(vol_to_use, 4),
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
                })
            else:
                reason = "net edge below threshold" if net_edge <= eff_threshold else "edge below threshold"
                trade_manager.log_decision(
                    ticker, "no", "skipped", reason,
                    edge=edge, net_edge=round(net_edge, 4), fee_pp=round(fee_pp, 4),
                    price_cents=no_ask, asset=asset, vol_used=round(vol_to_use, 4),
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
            trade_manager.log_decision(ticker, side, "skipped", f"allocator denied: {budget.reason}",
                                       edge=edge, price_cents=yes_ask if side == "yes" else no_ask,
                                       asset=opp["asset"], vol_used=round(opp["vol_used"], 4))
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

        # Bankroll-scaled cap (2% of bankroll, min $5)
        max_exposure = max(500, int((budget.bankroll_cents or 50000) * 0.02))
        if count * price > max_exposure:
            count = max(1, max_exposure // price)

        # CI-aware sizing: reduce position when filter is uncertain
        filtered_est = opp["filtered_est"]
        kelly_mult = ci_kelly_multiplier(filtered_est)
        regime_mult = regime_kelly_multiplier(regime_detector)

        # Correlation adjustment: reduce if heavily correlated with existing positions
        corr_mult = 1.0
        corr_matrix = dcc_tracker.correlation_matrix()
        if corr_matrix is not None:
            max_corr = 0.0
            for other_asset in spot_prices:
                if other_asset != opp["asset"]:
                    rho = dcc_tracker.pair_correlation(opp["asset"], other_asset)
                    if rho is not None:
                        max_corr = max(max_corr, abs(rho))
            if max_corr > 0.5:
                corr_mult = 1.0 - 0.3 * (max_corr - 0.5) / 0.5

        combined_mult = kelly_mult * regime_mult * corr_mult
        count = max(0, int(count * combined_mult))

        if count <= 0:
            ss.skip("kelly_zero")
            trade_manager.log_decision(ticker, side, "skipped", "kelly_zero: edge too small for price",
                                       edge=edge, price_cents=price,
                                       asset=opp["asset"], vol_used=round(opp["vol_used"], 4))
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
                                            regime=regime_detector.current_regime())
        if result:
            ss.trades_placed += 1
            allocator.record_trade("crypto", ticker, risk, edge=edge)

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

def main():
    parser = argparse.ArgumentParser(description="Kalshi Crypto Bot")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

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
            log.error(f"Scan error: {e}")
            traceback.print_exc()

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
