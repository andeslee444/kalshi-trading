#!/usr/bin/env python3
"""Kalshi Weather Trading Bot - Demo Paper Trading
Scans KXHIGH temperature markets, compares to Open-Meteo forecasts, and places trades on edge.
"""

import json, time, datetime, os, sys, re
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot, HealthCheckMonitor
from probability import weather_probability, ensemble_weather_probability, half_kelly, quarter_kelly, high_conviction_kelly, compute_limit_price, kalshi_fee_cents
from capital_allocator import PortfolioAllocator

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
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": config["maxTradeAmount"],
    "maxDailyTrades": config.get("maxDailyTrades", 10),
    "maxDailyLoss": config.get("maxDailyLoss", 10),
}, logger=log)
trim_trade_log(TRADES_PATH)

# === Weather Forecast ===

# Ensemble model endpoints for Open-Meteo
ENSEMBLE_MODELS = {
    "gfs": "gfs_seamless",
    "ecmwf": "ecmwf_ifs04",
    "icon": "icon_seamless",
}
ENSEMBLE_ENABLED = config.get("ensemble", {}).get("enabled", False)


def get_forecast(lat, lon):
    """Single-model GFS forecast (fallback)."""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
    r = retry_request("GET", url, timeout=10)
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))


def get_ensemble_forecast(lat, lon):
    """Fetch GFS, ECMWF, and ICON forecasts in parallel.

    Returns dict: {date_str: {"gfs": temp, "ecmwf": temp, "icon": temp}}
    Falls back to single-model GFS if any API fails.
    """
    from kalshi_auth import fetch_parallel

    urls = {}
    for model_key, model_name in ENSEMBLE_MODELS.items():
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=America%2FNew_York&forecast_days=7"
            f"&models={model_name}"
        )
        urls[url] = model_key

    responses = fetch_parallel(list(urls.keys()), timeout=15)

    # Parse each model's response
    model_forecasts = {}  # model_key -> {date: temp}
    for url, response in responses.items():
        model_key = urls[url]
        if response is None or response.status_code != 200:
            log.warning(f"Ensemble model {model_key} failed, will use single-model fallback")
            continue
        try:
            d = response.json()["daily"]
            model_forecasts[model_key] = dict(zip(d["time"], d["temperature_2m_max"]))
        except (KeyError, ValueError) as e:
            log.warning(f"Ensemble model {model_key} parse error: {e}")

    if not model_forecasts:
        log.warning("All ensemble models failed, falling back to single GFS")
        single = get_forecast(lat, lon)
        return {date: {"gfs": temp} for date, temp in single.items()}

    # Combine into {date: {model: temp}} structure
    all_dates = set()
    for forecasts in model_forecasts.values():
        all_dates.update(forecasts.keys())

    combined = {}
    for date in sorted(all_dates):
        combined[date] = {}
        for model_key, forecasts in model_forecasts.items():
            if date in forecasts:
                combined[date][model_key] = forecasts[date]

    return combined

# === Ticker Parsing ===
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    """Parse KXHIGHMIA-26FEB16-T86 or KXHIGHMIA-26FEB16-B85.5"""
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m:
        return None
    city = m.group(1)
    yr, mon, day = int(m.group(2)), m.group(3), int(m.group(4))
    direction = m.group(5)
    threshold = float(m.group(6))
    month = MONTHS.get(mon)
    if not month:
        return None
    return {
        "city": city,
        "date": f"{2000+yr}-{month:02d}-{day:02d}",
        "direction": direction,  # T=above, B=bracket
        "threshold": threshold,
    }

# === Trading ===

def compute_probability(forecast_temp, threshold, direction, days_out=0, city=None):
    """Estimate probability that YES resolves true.

    CDF-based model using weather_probability() from shared probability module.
    sigma scales with forecast horizon: sigma = 2.5 + 0.5 * days_out.
    If city is provided and calibration data exists, uses calibrated sigma.
    """
    return weather_probability(forecast_temp, threshold, direction, days_out, city=city)

def scan_and_trade():
    now = datetime.datetime.now()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Market scan starting...")

    # Balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")
        return

    # Get weather markets (5-min cache — markets don't change that fast)
    try:
        markets = client.get_all_markets(prefix="KXHIGH", cache_ttl=300)
        log.info(f"Found {len(markets)} KXHIGH markets")
    except Exception as e:
        log.error(f"Market fetch error: {e}")
        return

    if not markets:
        log.info("No weather markets found.")
        return

    # Get forecasts (ensemble or single-model)
    forecasts = {}
    for code, info in CITIES.items():
        try:
            if ENSEMBLE_ENABLED:
                forecasts[code] = get_ensemble_forecast(info["lat"], info["lon"])
            else:
                forecasts[code] = get_forecast(info["lat"], info["lon"])
            health.record_source_success("open-meteo")
        except Exception as e:
            log.error(f"Forecast error for {info['name']}: {e}")
            health.record_source_error("open-meteo", str(e))

    # Analyze markets
    opportunities = []
    skipped = {"no_parse": 0, "no_city": 0, "no_date": 0, "no_price": 0, "low_edge": 0}
    for m in markets:
        ticker = m.get("ticker", "")
        parsed = parse_ticker(ticker)
        if not parsed:
            skipped["no_parse"] += 1
            continue

        city = parsed["city"]
        if city not in CITIES or city not in forecasts:
            skipped["no_city"] += 1
            continue

        date_str = parsed["date"]
        if date_str not in forecasts[city]:
            skipped["no_date"] += 1
            continue

        forecast_data = forecasts[city][date_str]
        try:
            market_date = datetime.date.fromisoformat(date_str)
            days_out = max(0, (market_date - datetime.date.today()).days)
        except (ValueError, TypeError):
            days_out = 0

        # Compute probability — ensemble or single-model
        if ENSEMBLE_ENABLED and isinstance(forecast_data, dict):
            our_prob = ensemble_weather_probability(forecast_data, parsed["threshold"], parsed["direction"], days_out, city=city)
            forecast_temp = sum(forecast_data.values()) / len(forecast_data)  # mean for logging
        else:
            forecast_temp = forecast_data if not isinstance(forecast_data, dict) else list(forecast_data.values())[0]
            our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"], days_out, city=city)

        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)
        last = m.get("last_price", 0)

        # Compute edge against the price we'd actually pay (ask for YES, 100-bid for NO)
        # not the midpoint, to avoid false positives from wide spreads
        city_name = CITIES[city]["name"]

        if our_prob > 0.5 and yes_ask and yes_ask < 99:
            edge_yes = our_prob - (yes_ask / 100.0)
        elif our_prob <= 0.5 and no_ask and no_ask < 99:
            edge_yes = -((1 - our_prob) - (no_ask / 100.0))  # negative = NO signal
        else:
            skipped["no_price"] += 1
            continue

        if abs(edge_yes) >= config["edgeThreshold"]:
            opportunities.append({
                "ticker": ticker, "market": m, "parsed": parsed,
                "forecast": forecast_temp, "our_prob": our_prob,
                "market_price": (yes_ask / 100.0) if edge_yes > 0 else (no_ask / 100.0),
                "edge": edge_yes,
                "city_name": city_name,
                "yes_ask": yes_ask, "no_ask": no_ask,
            })
        else:
            skipped["low_edge"] += 1
            trade_manager.log_decision(
                ticker, "yes" if edge_yes > 0 else "no", "skipped",
                "edge below threshold", edge=abs(edge_yes),
                price_cents=yes_ask if edge_yes > 0 else no_ask,
            )

    log.info(f"Skipped: {skipped}")

    # Sort by edge magnitude
    opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)
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

        # Rec 1: Brackets require 2x edge threshold (higher model uncertainty)
        if is_bracket and abs(edge) < config["edgeThreshold"] * 2:
            log.info(f"  Skipping bracket {ticker}: edge {abs(edge)*100:.1f}% < {config['edgeThreshold']*200:.0f}% (2x threshold)")
            continue

        # Edge already computed against actual ask price in the filter above
        actual_edge = abs(edge)

        if edge > 0 and yes_ask and yes_ask < 99:
            # Config-level YES disable — if set, skip all weather YES trades
            if config.get("disableWeatherYes", False):
                trade_manager.log_decision(ticker, "yes", "skipped", "weather YES disabled by config",
                                            edge=actual_edge, price_cents=yes_ask)
                continue
            # Rec 2: NO-only weather constraint — skip YES unless edge >= 15%
            # YES side has 0% historical win rate; only trade with very high conviction
            if actual_edge < 0.15:
                log.info(f"  Skipping YES on {ticker}: edge {actual_edge*100:.1f}% < 15% minimum for YES side")
                continue
            side = "yes"
            price = compute_limit_price(yes_bid, yes_ask, "yes", edge=actual_edge)
            if not price or price <= 0:
                price = yes_ask
            reasoning = f"{city_name} forecast: {forecast}F, {ticker} YES at {price}c -> our prob {opp['our_prob']*100:.0f}%, edge +{actual_edge*100:.1f}%, buying YES"
        elif edge < 0 and no_ask and no_ask < 99:
            side = "no"
            price = compute_limit_price(yes_bid, yes_ask, "no", edge=actual_edge)
            if not price or price <= 0:
                price = no_ask
            reasoning = f"{city_name} forecast: {forecast}F, {ticker} NO at {price}c -> our prob {(1-opp['our_prob'])*100:.0f}%, edge +{actual_edge*100:.1f}%, buying NO"
        else:
            continue

        # Request budget from portfolio allocator (includes Rec 6 dedup via global ticker check)
        budget = allocator.request_budget("weather", ticker, edge=abs(actual_edge))
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            continue

        # Position sizing based on market type and conviction
        fee = kalshi_fee_cents(price)
        if is_bracket:
            # Rec 3+10: Quarter-Kelly for brackets (cap scales with bankroll)
            count, risk, kelly_details = quarter_kelly(
                abs(actual_edge), price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            sizing_label = "quarter-Kelly"
        elif side == "no" and (1 - opp["our_prob"]) > 0.80:
            # Rec 4: High-conviction threshold-NO → 60% Kelly
            count, risk, kelly_details = high_conviction_kelly(
                abs(actual_edge), price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            sizing_label = "60%-Kelly (high-conviction)"
        else:
            # Standard half-Kelly
            count, risk, kelly_details = half_kelly(
                abs(actual_edge), price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            sizing_label = "half-Kelly"

        if count <= 0:
            log.info(f"  Kelly says 0 contracts for {ticker} (edge too small for price), skipping")
            continue

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c ({sizing_label}, bankroll=${budget.bankroll_cents/100:.2f})")

        result = trade_manager.place_order(
            ticker, side, price, count, reasoning,
            forecast_temp=forecast, threshold=threshold, edge=round(actual_edge, 4),
            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
            model_prob=round(opp["our_prob"], 4),
            raw_edge=round(actual_edge, 4),
            fee_cents=round(kalshi_fee_cents(price), 2),
            sizing_method=sizing_label,
            market_close_time=opp["market"].get("close_time"),
            kelly_fraction=kelly_details.get("kelly_fraction"),
            bankroll_used=kelly_details.get("bankroll_used"),
        )
        if result:
            allocator.record_trade("weather", ticker, risk, edge=actual_edge)

def main():
    log.info("=" * 60)
    log.info("Kalshi Weather Trading Bot (DEMO)")
    log.info(f"Mode: {config['mode']} | Max: ${config['maxTradeAmount']}/trade | Edge: {config['edgeThreshold']*100:.0f}%")
    log.info("=" * 60)

    # Verify auth
    log.info("\nVerifying authentication...")
    try:
        balance, _ = client.get_balance()
        log.info(f"Auth OK! Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Auth failed: {e}")
        sys.exit(1)

    # Main loop
    while True:
        try:
            health.record_bot_heartbeat("weather")
            scan_and_trade()
        except Exception as e:
            log.error(f"Scan error: {e}")
            import traceback; traceback.print_exc()

        interval = config["scanIntervalMinutes"]
        log.info(f"\nNext scan in {interval} minutes...")
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
