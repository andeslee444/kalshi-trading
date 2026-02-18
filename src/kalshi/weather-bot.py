#!/usr/bin/env python3
"""Kalshi Weather Trading Bot - Demo Paper Trading
Scans KXHIGH temperature markets, compares to Open-Meteo forecasts, and places trades on edge.
"""

import json, time, datetime, os, sys, re
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, retry_request, TradeManager, trim_trade_log
from probability import weather_probability, half_kelly

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
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": config["maxTradeAmount"],
    "maxDailyTrades": config.get("maxDailyTrades", 10),
    "maxDailyLoss": config.get("maxDailyLoss", 10),
}, logger=log)
trim_trade_log(TRADES_PATH)

# === Weather Forecast ===
def get_forecast(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
    r = retry_request("GET", url, timeout=10)
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

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

    # Get forecasts
    forecasts = {}
    for code, info in CITIES.items():
        try:
            forecasts[code] = get_forecast(info["lat"], info["lon"])
        except Exception as e:
            log.error(f"Forecast error for {info['name']}: {e}")

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

        forecast_temp = forecasts[city][date_str]
        try:
            market_date = datetime.date.fromisoformat(date_str)
            days_out = max(0, (market_date - datetime.date.today()).days)
        except (ValueError, TypeError):
            days_out = 0
        our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"], days_out, city=city)

        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)
        last = m.get("last_price", 0)

        if yes_bid and yes_ask and yes_ask < 100:
            market_price = (yes_bid + yes_ask) / 2 / 100
        elif yes_ask and yes_ask < 100:
            market_price = yes_ask / 100
        elif last and last < 100:
            market_price = last / 100
        else:
            skipped["no_price"] += 1
            continue

        edge_yes = our_prob - market_price

        city_name = CITIES[city]["name"]

        if abs(edge_yes) >= config["edgeThreshold"]:
            opportunities.append({
                "ticker": ticker, "market": m, "parsed": parsed,
                "forecast": forecast_temp, "our_prob": our_prob,
                "market_price": market_price, "edge": edge_yes,
                "city_name": city_name,
                "yes_ask": yes_ask, "no_ask": no_ask,
            })
        else:
            skipped["low_edge"] += 1

    log.info(f"Skipped: {skipped}")

    # Sort by edge magnitude
    opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)
    log.info(f"Found {len(opportunities)} opportunities with edge >= {config['edgeThreshold']*100:.0f}%")

    for opp in opportunities:
        ticker = opp["ticker"]
        edge = opp["edge"]
        forecast = opp["forecast"]
        threshold = opp["parsed"]["threshold"]
        city_name = opp["city_name"]

        if edge > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
            side = "yes"
            price = opp["yes_ask"]
            reasoning = f"{city_name} forecast: {forecast}F, {ticker} YES at {price}c -> our prob {opp['our_prob']*100:.0f}%, edge +{edge*100:.1f}%, buying YES"
        elif edge < 0 and opp["no_ask"] and opp["no_ask"] < 99:
            side = "no"
            price = opp["no_ask"]
            reasoning = f"{city_name} forecast: {forecast}F, {ticker} NO at {price}c -> our prob {(1-opp['our_prob'])*100:.0f}%, edge +{abs(edge)*100:.1f}%, buying NO"
        else:
            continue

        max_cost = config["maxTradeAmount"] * 100
        count, risk = half_kelly(abs(edge), price, max_cost)
        if count < 1:
            count = 1

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c")

        trade_manager.place_order(
            ticker, side, price, count, reasoning,
            forecast_temp=forecast, threshold=threshold, edge=round(edge, 4)
        )

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
            scan_and_trade()
        except Exception as e:
            log.error(f"Scan error: {e}")
            import traceback; traceback.print_exc()

        interval = config["scanIntervalMinutes"]
        log.info(f"\nNext scan in {interval} minutes...")
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
