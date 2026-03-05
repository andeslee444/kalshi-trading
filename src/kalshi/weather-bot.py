#!/usr/bin/env python3
"""Kalshi Weather Trading Bot - Demo Paper Trading
Scans KXHIGH temperature markets, compares to Open-Meteo forecasts, and places trades on edge.
"""

import json, time, datetime, os, sys, re
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot, HealthCheckMonitor, OrderMonitor, ScanSummary, is_shutdown_requested
from probability import weather_probability, weather_sigma, ensemble_weather_probability, ensemble_spread_sigma_multiplier, half_kelly, quarter_kelly, high_conviction_kelly, compute_limit_price, kalshi_fee_cents, is_market_liquid, _load_calibration
from ticker_utils import parse_weather_ticker as parse_ticker
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
order_monitor = OrderMonitor(client, log=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": config["maxTradeAmount"],
    "maxTradeAmountPct": config.get("maxTradeAmountPct"),
    "maxDailyTrades": config.get("maxDailyTrades", 10),
    "maxDailyLoss": config.get("maxDailyLoss", 10),
    "maxDailyLossPct": config.get("maxDailyLossPct"),
}, logger=log, order_monitor=order_monitor, cooldown_hours=12, bot_name="weather")
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
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=14"
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
            f"&timezone=America%2FNew_York&forecast_days=14"
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
        day_data = {}
        for model_key, forecasts in model_forecasts.items():
            if date in forecasts:
                day_data[model_key] = forecasts[date]
        if day_data:  # Only include dates with at least one model's data
            combined[date] = day_data

    return combined

# === Trading ===

def compute_probability(forecast_temp, threshold, direction, days_out=0, city=None):
    """Estimate probability that YES resolves true.

    Delegates to weather_probability() with calibrated sigma from probability module.
    If city is provided and calibration data exists, uses calibrated sigma.
    """
    return weather_probability(forecast_temp, threshold, direction, days_out, city=city)

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
    for m in markets:
        ticker = m.get("ticker", "")
        parsed = parse_ticker(ticker)
        if not parsed:
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

        # Compute probability — ensemble or single-model
        spread_mult = 1.0
        if ENSEMBLE_ENABLED and isinstance(forecast_data, dict):
            if not forecast_data:
                ss.skip("empty_forecast")
                continue
            valid_temps = [t for t in forecast_data.values() if t is not None]
            forecast_temp = sum(valid_temps) / len(valid_temps) if valid_temps else None  # mean for logging
            if forecast_temp is None:
                ss.skip("null_forecast")
                continue
            # Compute spread multiplier once — widens sigma AND gates edge
            if len(valid_temps) >= 2:
                spread = max(valid_temps) - min(valid_temps)
                spread_mult = ensemble_spread_sigma_multiplier(spread)
                if spread_mult > 1.0:
                    log.info(f"  {ticker}: ensemble spread {spread:.1f}F (sigma_mult={spread_mult:.2f})")
            our_prob = ensemble_weather_probability(forecast_data, parsed["threshold"], parsed["direction"], days_out, city=city, sigma_multiplier=spread_mult)
            if our_prob is None:
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

        # Skip near-threshold coinflips (|forecast - threshold| < 2°F)
        MIN_FORECAST_DISTANCE_F = 2.0
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

        if not is_market_liquid(m):
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

        # Adjust edge threshold for high ensemble spread (defense in depth)
        effective_edge_threshold = config["edgeThreshold"]
        if spread_mult > 1.5:
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

        # Rec 1: Brackets require 2x edge threshold (higher model uncertainty)
        if is_bracket and edge < config["edgeThreshold"] * 2:
            log.info(f"  Skipping bracket {ticker}: edge {edge*100:.1f}% < {config['edgeThreshold']*200:.0f}% (2x threshold)")
            continue

        # Edge is always positive (computed against the ask for the side we'd trade)
        side = opp["side"]

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
            price = compute_limit_price(yes_bid, yes_ask, "yes", edge=edge)
            if not price or price <= 0:
                price = yes_ask
            reasoning = f"{city_name} forecast: {forecast}F, {ticker} YES at {price}c -> our prob {opp['our_prob']*100:.0f}%, edge +{edge*100:.1f}%, buying YES"
        elif side == "no" and no_ask and no_ask < 99:
            price = compute_limit_price(yes_bid, yes_ask, "no", edge=edge)
            if not price or price <= 0:
                price = no_ask
            reasoning = f"{city_name} forecast: {forecast}F, {ticker} NO at {price}c -> our prob {(1-opp['our_prob'])*100:.0f}%, edge +{edge*100:.1f}%, buying NO"
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

    ss.finalize()

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
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()
            scan_and_trade()
        except Exception as e:
            log.error(f"Scan error: {e}")
            import traceback; traceback.print_exc()

        interval = config["scanIntervalMinutes"]
        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break
        log.info(f"\nNext scan in {interval} minutes...")
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
