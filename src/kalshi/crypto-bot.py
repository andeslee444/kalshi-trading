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
    HealthCheckMonitor, OrderMonitor,
)
from probability import (
    crypto_price_probability, quarter_kelly, half_kelly, compute_limit_price,
    kalshi_fee_cents,
)
from ticker_utils import parse_crypto_ticker
from capital_allocator import PortfolioAllocator

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
SETTLEMENT_BUFFER_MINUTES = crypto_config.get("settlementBufferMinutes", 2)
USE_OU = crypto_config.get("useOrnsteinUhlenbeck", False)
OU_HALF_LIFE = crypto_config.get("ouHalfLifeMinutes", 120)

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
order_monitor = OrderMonitor(client, log=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE,
    "maxDailyTrades": MAX_DAILY_TRADES,
    "maxDailyLoss": MAX_DAILY_LOSS,
}, logger=log, cooldown_hours=0.5, order_monitor=order_monitor)  # short cooldown for fast markets
trim_trade_log(TRADES_PATH)

# === Market ticker prefixes ===
CRYPTO_PREFIXES = ["KXBTC", "KXETH", "KXCRYPTO", "KXSOL"]

# Default annualized volatilities (post-ETF era, updated 2026)
DEFAULT_VOLS = {
    "BTC": 0.50,  # post-ETF BTC vol is 40-55%
    "ETH": 0.65,  # ETH vol tracks BTC more closely now
    "SOL": 0.80,
}

# Recent price cache for realized vol computation
_price_history = {}  # asset -> [(timestamp, price), ...]


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


def compute_realized_vol(asset, current_price):
    """Compute realized volatility from recent price observations.

    Returns annualized vol as decimal, or None if insufficient data.
    """
    now = time.time()

    # Record current observation
    if asset not in _price_history:
        _price_history[asset] = []
    _price_history[asset].append((now, current_price))

    # Keep only last 24h of observations
    cutoff = now - 86400
    _price_history[asset] = [(t, p) for t, p in _price_history[asset] if t > cutoff]

    history = _price_history[asset]
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


# === Scanning ===

def scan_and_trade():
    """Scan crypto markets and trade on model edge."""
    now = datetime.datetime.now()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Crypto scan starting...")

    # Balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")
        return

    # Fetch spot prices
    log.info("\nFetching crypto prices...")
    spot_prices = {}
    for asset in ["BTC", "ETH"]:
        price = fetch_coinbase_spot(asset)
        if price:
            spot_prices[asset] = price
            health.record_source_success("coinbase")
        else:
            health.record_source_error("coinbase", f"{asset} spot unavailable")

    if not spot_prices:
        log.info("No spot prices available, skipping scan.")
        return

    # Fetch IV (optional, fall back to realized vol)
    iv_data = {}
    for asset in spot_prices:
        iv = fetch_deribit_iv(asset)
        if iv:
            iv_data[asset] = iv
            health.record_source_success("deribit")

    # Compute realized vol
    realized_vols = {}
    for asset, price in spot_prices.items():
        rv = compute_realized_vol(asset, price)
        if rv:
            realized_vols[asset] = rv
            log.info(f"  {asset} realized vol: {rv*100:.1f}%")

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
        return

    log.info(f"Found {len(all_markets)} crypto markets")

    # Evaluate each market
    opportunities = []
    for m in all_markets:
        ticker = m.get("ticker", "")
        parsed = parse_crypto_ticker(ticker)
        if not parsed:
            continue

        asset = parsed["asset"]
        if asset not in spot_prices:
            continue

        current_price = spot_prices[asset]
        threshold = parsed["threshold"]
        direction = parsed.get("direction", "T")

        # Estimate time to settlement
        minutes_to_settle = estimate_time_to_settlement(m)

        # Skip markets about to settle (avoid last-minute noise)
        if minutes_to_settle < SETTLEMENT_BUFFER_MINUTES:
            continue

        # Get volatility — IV is forward-looking so gets more weight
        iv = iv_data.get(asset)
        rv = realized_vols.get(asset)
        default_vol = DEFAULT_VOLS.get(asset, 0.50)
        if iv is not None and rv is not None:
            vol_to_use = 0.6 * iv + 0.4 * rv
        elif iv is not None:
            vol_to_use = iv
        elif rv is not None:
            vol_to_use = 0.3 * default_vol + 0.7 * rv
        else:
            vol_to_use = default_vol
        if direction == "T":
            prob = crypto_price_probability(
                current_price, threshold, "above",
                time_horizon_minutes=minutes_to_settle,
                realized_vol_pct=vol_to_use, iv_pct=None,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
            )
        else:
            # Bracket: probability price lands in [threshold, threshold+range)
            # For crypto, brackets are typically $1000 wide for BTC
            range_size = 1000 if asset == "BTC" else 100
            prob_above_low = crypto_price_probability(
                current_price, threshold, "above",
                time_horizon_minutes=minutes_to_settle,
                realized_vol_pct=vol_to_use, iv_pct=None,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
            )
            prob_above_high = crypto_price_probability(
                current_price, threshold + range_size, "above",
                time_horizon_minutes=minutes_to_settle,
                realized_vol_pct=vol_to_use, iv_pct=None,
                use_ou=USE_OU, ou_half_life_minutes=OU_HALF_LIFE,
            )
            prob = prob_above_low - prob_above_high

        yes_ask = m.get("yes_ask", 0)
        no_ask = m.get("no_ask", 0)
        yes_bid = m.get("yes_bid", 0)

        if not yes_ask or yes_ask >= 99:
            continue

        # Determine trade direction and edge (raw edge, fees handled in Kelly)
        if prob > 0.5 and yes_ask:
            edge = prob - yes_ask / 100
            if edge > EDGE_THRESHOLD:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "yes",
                    "prob": prob, "edge": edge, "asset": asset,
                    "current_price": current_price, "threshold": threshold,
                    "minutes_to_settle": minutes_to_settle,
                    "vol_used": vol_to_use,
                })
            else:
                trade_manager.log_decision(
                    ticker, "yes", "skipped", "edge below threshold",
                    edge=edge, price_cents=yes_ask,
                )
        elif prob <= 0.5 and no_ask:
            no_prob = 1.0 - prob
            edge = no_prob - no_ask / 100
            if edge > EDGE_THRESHOLD:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "no",
                    "prob": no_prob, "edge": edge, "asset": asset,
                    "current_price": current_price, "threshold": threshold,
                    "minutes_to_settle": minutes_to_settle,
                    "vol_used": vol_to_use,
                })
            else:
                trade_manager.log_decision(
                    ticker, "no", "skipped", "edge below threshold",
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

        budget = allocator.request_budget("crypto", ticker, edge=edge, confidence=opp["prob"])
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            continue

        price = compute_limit_price(yes_bid, yes_ask, side, edge=edge) or (yes_ask if side == "yes" else no_ask)
        if not price or price <= 0:
            continue

        # Use quarter-Kelly for crypto (high volatility uncertainty)
        fee = kalshi_fee_cents(price)
        count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
        if count <= 0:
            continue

        reasoning = (
            f"Crypto {opp['asset']}: spot ${opp['current_price']:,.0f} vs threshold ${opp['threshold']:,.0f}, "
            f"vol={opp['vol_used']*100:.0f}%, T={opp['minutes_to_settle']}min, "
            f"prob={opp['prob']*100:.0f}%, edge={edge*100:.1f}%"
        )

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c on {ticker} (quarter-Kelly)")

        result = trade_manager.place_order(ticker, side, price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(opp["prob"], 4), raw_edge=round(edge, 4),
                                            fee_cents=round(kalshi_fee_cents(price), 2), sizing_method="quarter_kelly",
                                            market_close_time=m.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"))
        if result:
            allocator.record_trade("crypto", ticker, risk, edge=edge)


# === Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Kalshi Crypto Bot")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Crypto Bot (BTC/ETH)")
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
            order_monitor.check_orders()
            scan_and_trade()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
