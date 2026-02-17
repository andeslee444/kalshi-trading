#!/usr/bin/env python3
"""Kalshi Weather Trading Bot - Demo Paper Trading
Scans KXHIGH temperature markets, compares to Open-Meteo forecasts, and places trades on edge.
"""

import json, time, base64, datetime, os, sys, re
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# Unbuffered output
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
os.environ['PYTHONUNBUFFERED'] = '1'

# === Config ===
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

config = json.loads(CONFIG_PATH.read_text())
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

CITIES = config["cities"]

# === Auth ===
with open(KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def get_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    path_clean = path.split("?")[0]
    msg = f"{ts}{method}{path_clean}"
    sig = private_key.sign(
        msg.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": API_KEY,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

def api(method, path, body=None):
    url = BASE_URL + path
    full_path = "/trade-api/v2" + path
    headers = get_headers(method, full_path)
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def api_get_all_markets():
    """Paginate through all open markets and return KXHIGH ones."""
    weather = []
    cursor = None
    for page in range(50):
        path = "/markets?status=open&limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = api("GET", path)
        except Exception as e:
            print(f"  Market page {page} error: {e}")
            break
        batch = data.get("markets", [])
        for m in batch:
            if "KXHIGH" in m.get("ticker", ""):
                weather.append(m)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    print(f"  Scanned {page+1} pages, found {len(weather)} KXHIGH markets")
    return weather

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
def load_trades():
    if TRADES_PATH.exists():
        try: return json.loads(TRADES_PATH.read_text())
        except: return []
    return []

def save_trade(trade):
    trades = load_trades()
    trades.append(trade)
    TRADES_PATH.write_text(json.dumps(trades, indent=2))

session_trades = 0

def compute_probability(forecast_temp, threshold, direction):
    """Estimate probability that YES resolves true."""
    diff = forecast_temp - threshold
    if direction == "T":
        # YES = temp > threshold
        if diff > 6: return 0.95
        elif diff > 3: return 0.85
        elif diff > 1: return 0.65
        elif diff > -1: return 0.45
        elif diff > -3: return 0.25
        elif diff > -6: return 0.10
        else: return 0.03
    else:  # B = bracket (temp in range)
        # Bracket markets: YES = temp is in a 2-degree range around threshold
        # Approximate: if forecast is within 1° of bracket center, ~30% chance
        # Further away, lower chance
        bracket_center = threshold + 0.5  # e.g., B85.5 means 85-86, center ~86
        dist = abs(forecast_temp - bracket_center)
        if dist < 1: return 0.30
        elif dist < 2: return 0.20
        elif dist < 3: return 0.12
        elif dist < 5: return 0.06
        else: return 0.02

def scan_and_trade():
    global session_trades
    now = datetime.datetime.now()
    print(f"\n{'='*60}")
    print(f"[{now.isoformat()}] Market scan starting...")
    sys.stdout.flush()

    # Balance
    try:
        bal = api("GET", "/portfolio/balance")
        print(f"Balance: ${bal.get('balance',0)/100:.2f}")
    except Exception as e:
        print(f"Balance error: {e}")
        return

    # Get weather markets
    try:
        markets = api_get_all_markets()
        print(f"Found {len(markets)} KXHIGH markets")
    except Exception as e:
        print(f"Market fetch error: {e}")
        import traceback; traceback.print_exc()
        return

    if not markets:
        print("No weather markets found.")
        return

    # Get forecasts
    forecasts = {}
    for code, info in CITIES.items():
        try:
            forecasts[code] = get_forecast(info["lat"], info["lon"])
        except Exception as e:
            print(f"Forecast error for {info['name']}: {e}")

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

        # Market price - use yes_ask for buying YES, yes_bid for selling
        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)
        last = m.get("last_price", 0)
        
        # Use last_price or midpoint as reference; for thin markets, use ask price directly
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
        edge_no = (1 - our_prob) - (1 - market_price)  # same as -(edge_yes)

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
    
    print(f"Skipped: {skipped}")

    # Sort by edge magnitude
    opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)
    print(f"Found {len(opportunities)} opportunities with edge >= {config['edgeThreshold']*100:.0f}%")

    for opp in opportunities:
        if session_trades >= 3:
            print("Session trade limit (3) reached.")
            break

        ticker = opp["ticker"]
        edge = opp["edge"]
        forecast = opp["forecast"]
        threshold = opp["parsed"]["threshold"]
        direction = opp["parsed"]["direction"]
        city_name = opp["city_name"]

        if edge > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
            side = "yes"
            price = opp["yes_ask"]
            reasoning = f"{city_name} forecast: {forecast}°F, {ticker} YES at {price}¢ → our prob {opp['our_prob']*100:.0f}%, edge +{edge*100:.1f}%, buying YES"
        elif edge < 0 and opp["no_ask"] and opp["no_ask"] < 99:
            side = "no"
            price = opp["no_ask"]
            reasoning = f"{city_name} forecast: {forecast}°F, {ticker} NO at {price}¢ → our prob {(1-opp['our_prob'])*100:.0f}%, edge +{abs(edge)*100:.1f}%, buying NO"
        else:
            continue

        max_cost = config["maxTradeAmount"] * 100
        count = max(1, min(max_cost // price, 10))

        print(f"\n→ TRADE: {reasoning}")
        print(f"  Placing: {count}x {side} @ {price}¢")
        sys.stdout.flush()

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

            result = api("POST", "/portfolio/orders", order_body)
            order_info = result.get("order", {})
            print(f"  ✓ Order placed! ID: {order_info.get('order_id', 'unknown')}, status: {order_info.get('status', '?')}")
            session_trades += 1

            save_trade({
                "timestamp": now.isoformat(),
                "ticker": ticker, "side": side, "price": price,
                "count": count, "reasoning": reasoning,
                "forecast_temp": forecast, "threshold": threshold,
                "edge": round(edge, 4),
                "order_id": order_info.get("order_id"),
                "status": order_info.get("status"),
            })
        except requests.exceptions.HTTPError as e:
            print(f"  ✗ Order failed: {e.response.status_code} {e.response.text[:200]}")
        except Exception as e:
            print(f"  ✗ Order failed: {e}")

    sys.stdout.flush()

def main():
    print("=" * 60, flush=True)
    print("Kalshi Weather Trading Bot (DEMO)", flush=True)
    print(f"Mode: {config['mode']} | Max: ${config['maxTradeAmount']}/trade | Edge: {config['edgeThreshold']*100:.0f}%", flush=True)
    print("=" * 60, flush=True)

    # Verify auth
    print("\nVerifying authentication...", flush=True)
    try:
        bal = api("GET", "/portfolio/balance")
        print(f"✓ Auth OK! Balance: {json.dumps(bal)}", flush=True)
    except Exception as e:
        print(f"✗ Auth failed: {e}", flush=True)
        sys.exit(1)

    # Main loop
    while True:
        try:
            scan_and_trade()
        except Exception as e:
            print(f"Scan error: {e}", flush=True)
            import traceback; traceback.print_exc()

        interval = config["scanIntervalMinutes"]
        print(f"\nNext scan in {interval} minutes...", flush=True)
        sys.stdout.flush()
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
