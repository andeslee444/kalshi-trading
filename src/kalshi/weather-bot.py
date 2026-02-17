#!/usr/bin/env python3
"""Kalshi Weather Trading Bot - Demo Paper Trading
Scans KXHIGH temperature markets, compares to Open-Meteo forecasts, and places trades on edge.
"""

import json, time, datetime, os, sys, re
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR

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

# === Weather Forecast ===
def get_forecast(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
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
session_trades = 0

def compute_probability(forecast_temp, threshold, direction):
    """Estimate probability that YES resolves true.

    Step-function approximation based on Open-Meteo forecast error distribution.
    NWS forecast error for daily highs is typically ±3°F (68% CI), ±6°F (95% CI).
    The probability bins map forecast-vs-threshold difference to YES probability:
      T (above): >6°F→95%, >3°F→85%, >1°F→65%, >-1°F→45%, etc.
      B (bracket): within 1°F of center→30%, decaying with distance.
    """
    diff = forecast_temp - threshold
    if direction == "T":
        # YES = temp > threshold.
        # Bins derived from NWS daily-high forecast error CDF:
        #   P(error < 3°F) ≈ 68%, P(error < 6°F) ≈ 95% (normal, σ ≈ 3°F).
        # Each bin = P(actual > threshold) given forecast diff.
        if diff > 6: return 0.95    # forecast well above → ~95% CI confirms
        elif diff > 3: return 0.85  # forecast 1σ above → high confidence
        elif diff > 1: return 0.65  # slight edge, within noise
        elif diff > -1: return 0.45 # near coin-flip, forecast ≈ threshold
        elif diff > -3: return 0.25 # forecast 1σ below → unlikely
        elif diff > -6: return 0.10 # forecast well below → ~5th percentile
        else: return 0.03           # forecast >6°F below → extreme tail
    else:  # B = bracket (temp in range, typically 1°F wide)
        bracket_center = threshold + 0.5
        dist = abs(forecast_temp - bracket_center)
        # Bracket P ≈ PDF of error distribution × bracket width.
        # Peak ~30% for 1°F bracket when forecast is centered (σ ≈ 3°F).
        if dist < 1: return 0.30    # forecast centered on bracket
        elif dist < 2: return 0.20  # near edge of bracket
        elif dist < 3: return 0.12  # ~1σ away → density dropping
        elif dist < 5: return 0.06  # ~1.5σ away → low density
        else: return 0.02           # >5°F away → deep tail

def scan_and_trade():
    global session_trades
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

    # Get weather markets
    try:
        markets = client.get_all_markets(prefix="KXHIGH")
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
        our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"])

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
        if session_trades >= config.get("maxDailyTrades", 10):
            log.info(f"Session trade limit ({config.get('maxDailyTrades', 10)}) reached.")
            break

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
        count = max(1, min(max_cost // price, 10))

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c")

        try:
            order_body = {
                "ticker": ticker,
                "action": "buy",
                "side": side,
                "type": "limit",
                "count": count,
            }
            if side == "yes":
                order_body["yes_price"] = price
            else:
                order_body["no_price"] = price

            result = client.post("/portfolio/orders", body=order_body)
            order_info = result.get("order", {})
            log.info(f"  Order placed! ID: {order_info.get('order_id', 'unknown')}, status: {order_info.get('status', '?')}")
            session_trades += 1

            save_trade(TRADES_PATH, {
                "timestamp": now.isoformat(),
                "ticker": ticker, "side": side, "price": price,
                "count": count, "reasoning": reasoning,
                "forecast_temp": forecast, "threshold": threshold,
                "edge": round(edge, 4),
                "order_id": order_info.get("order_id"),
                "status": order_info.get("status"),
            })
        except requests.exceptions.HTTPError as e:
            log.error(f"  Order failed: {e.response.status_code} {e.response.text[:200]}")
        except Exception as e:
            log.error(f"  Order failed: {e}")

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
