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
)
from probability import (
    crypto_price_probability, quarter_kelly, half_kelly, compute_limit_price,
    kalshi_fee_cents, is_market_liquid,
)
from ticker_utils import parse_crypto_ticker
from capital_allocator import PortfolioAllocator
from particle_filter import FilterManager, FilterConfig, ci_kelly_multiplier
from regime_detector import RegimeDetector, regime_kelly_multiplier

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
# 1 min buffer: Kalshi closes 15s before settlement + ~30s clock/network margin
SETTLEMENT_BUFFER_MINUTES = crypto_config.get("settlementBufferMinutes", 1)
USE_OU = crypto_config.get("useOrnsteinUhlenbeck", False)
OU_HALF_LIFE = crypto_config.get("ouHalfLifeMinutes", 120)
DRIFT_PCT = crypto_config.get("driftPct", 0.0)
MID_RANGE_EDGE_THRESHOLD = crypto_config.get("midRangeEdgeThreshold", 0.15)
MID_RANGE_BAND = (
    crypto_config.get("midRangeLow", 0.25),
    crypto_config.get("midRangeHigh", 0.75),
)

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
filter_mgr.load_all()

# Regime detector
regime_detector = RegimeDetector()
regime_state_path = PROJECT_DIR / "data" / "regime-state.json"
regime_detector.load(str(regime_state_path))

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


def _effective_edge_threshold(model_prob):
    """Higher edge required when model probability is in the uncertain mid-range."""
    if MID_RANGE_BAND[0] < model_prob < MID_RANGE_BAND[1]:
        return MID_RANGE_EDGE_THRESHOLD
    return EDGE_THRESHOLD


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
    return max(-2.0, min(2.0, annualized))


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

        # Bracket markets: gate on tighter liquidity (spread < 15c, volume > 10)
        if direction == "B":
            if not crypto_config.get("enableBrackets", True):
                ss.skip("brackets_disabled")
                continue
            b_spread = (m.get("yes_ask", 0) - m.get("yes_bid", 0)) if m.get("yes_bid") else 999
            b_volume = m.get("volume", 0) or 0
            if b_spread > 15 or b_volume < 10:
                ss.skip("bracket_illiquid")
                trade_manager.log_decision(ticker, "yes", "skipped", "bracket_illiquid",
                                           spread=b_spread, volume=b_volume)
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

        # Get volatility — IV is forward-looking so gets more weight
        iv = iv_data.get(asset)
        # Use horizon-matched RV if shorter lookback needed, else use pre-computed 24h RV
        if rv_lookback < 86400:
            rv = compute_realized_vol(asset, lookback_seconds=rv_lookback)
        else:
            rv = realized_vols.get(asset)
        default_vol = DEFAULT_VOLS.get(asset, 0.50)
        if iv is not None and rv is not None:
            vol_to_use = 0.6 * iv + 0.4 * rv  # IV more predictive for short-term crypto
        elif iv is not None:
            vol_to_use = iv
        elif rv is not None:
            vol_to_use = 0.3 * default_vol + 0.7 * rv
        else:
            vol_to_use = default_vol

        drift = drift_by_asset.get(asset, DRIFT_PCT)
        if direction == "T":
            prob = crypto_price_probability(
                current_price, threshold, "above",
                time_horizon_minutes=minutes_to_settle,
                realized_vol_pct=vol_to_use, iv_pct=None,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
                drift_pct=drift,
            )
        else:
            # Bracket: probability price lands in [threshold, threshold+range)
            range_size = _parse_bracket_range(ticker, asset, all_markets)
            prob_above_low = crypto_price_probability(
                current_price, threshold, "above",
                time_horizon_minutes=minutes_to_settle,
                realized_vol_pct=vol_to_use, iv_pct=None,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
                drift_pct=drift,
            )
            prob_above_high = crypto_price_probability(
                current_price, threshold + range_size, "above",
                time_horizon_minutes=minutes_to_settle,
                realized_vol_pct=vol_to_use, iv_pct=None,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
                drift_pct=drift,
            )
            prob = prob_above_low - prob_above_high

        # Particle filter: update belief state and use filtered prob
        pf = filter_mgr.get_filter(ticker)
        pf.update(prob)
        filtered_est = pf.estimate()
        raw_prob = prob
        prob = filtered_est.prob  # use filtered probability for edge computation

        yes_ask = m.get("yes_ask", 0)
        no_ask = m.get("no_ask", 0)
        yes_bid = m.get("yes_bid", 0)

        if not yes_ask or yes_ask >= 99:
            ss.skip("no_price")
            continue

        if not is_market_liquid(m):
            ss.skip("illiquid")
            continue

        ss.markets_evaluated += 1

        # Determine trade direction and edge (raw edge, fees handled in Kelly)
        if prob > 0.5 and yes_ask:
            eff_threshold = _effective_edge_threshold(prob)
            edge = prob - yes_ask / 100
            if edge > eff_threshold:
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
                reason = "edge below mid-range threshold" if eff_threshold > EDGE_THRESHOLD else "edge below threshold"
                trade_manager.log_decision(
                    ticker, "yes", "skipped", reason,
                    edge=edge, price_cents=yes_ask,
                )
        elif prob <= 0.5 and no_ask:
            no_prob = 1.0 - prob
            eff_threshold = _effective_edge_threshold(no_prob)
            edge = no_prob - no_ask / 100
            if edge > eff_threshold:
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
                reason = "edge below mid-range threshold" if eff_threshold > EDGE_THRESHOLD else "edge below threshold"
                trade_manager.log_decision(
                    ticker, "no", "skipped", reason,
                    edge=edge, price_cents=no_ask,
                )

    # Sort by edge
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    log.info(f"Found {len(opportunities)} opportunities with edge >= {EDGE_THRESHOLD*100:.0f}%")

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
                                       edge=edge, price_cents=yes_ask if side == "yes" else no_ask)
            continue

        if opp.get("is_bracket"):
            price = yes_ask  # Bracket: always use ask for fill rate
        else:
            price = compute_limit_price(yes_bid, yes_ask, side, edge=edge) or (yes_ask if side == "yes" else no_ask)
        if not price or price <= 0:
            continue

        # Use quarter-Kelly for crypto (high volatility uncertainty)
        fee = kalshi_fee_cents(price)
        count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)

        # CI-aware sizing: reduce position when filter is uncertain
        filtered_est = opp["filtered_est"]
        kelly_mult = ci_kelly_multiplier(filtered_est)
        count = max(0, int(count * kelly_mult))

        if count <= 0:
            ss.skip("kelly_zero")
            trade_manager.log_decision(ticker, side, "skipped", "kelly_zero: edge too small for price",
                                       edge=edge, price_cents=price)
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
        log.info(f"  Placing: {count}x {side} @ {price}c on {ticker} (quarter-Kelly)")

        result = trade_manager.place_order(ticker, side, price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(opp["prob"] if side == "yes" else 1.0 - opp["prob"], 4), raw_edge=round(edge, 4),
                                            fee_cents=round(kalshi_fee_cents(price), 2), sizing_method="quarter_kelly",
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
                                            pf_kelly_mult=round(kelly_mult, 4))
        if result:
            ss.trades_placed += 1
            allocator.record_trade("crypto", ticker, risk, edge=edge)

    # Save particle filter state
    filter_mgr.save_all()

    ss.finalize()


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

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
