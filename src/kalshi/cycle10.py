#!/usr/bin/env python3
"""Cycle 10: Check settlements, positions, P&L, find new trades."""
import json, time, base64, datetime, sys, os
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

with open(KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def get_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}"
    sig = private_key.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": API_KEY, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}

def api(method, path, body=None):
    url = BASE_URL + path
    h = get_headers(method, "/trade-api/v2" + path)
    r = requests.request(method, url, headers=h, json=body if method != "GET" else None, timeout=15)
    r.raise_for_status()
    return r.json()

print("=" * 60)
print("CYCLE 10 — Settlement Check & New Trades")
print(f"Time: {datetime.datetime.now().isoformat()}")
print("=" * 60)

# 1. Check balance
print("\n--- BALANCE ---")
try:
    bal = api("GET", "/portfolio/balance")
    print(f"Balance: ${bal.get('balance', 0)/100:.2f}")
except Exception as e:
    print(f"Balance error: {e}")

# 2. Check positions (settled and open)
print("\n--- POSITIONS ---")
try:
    pos = api("GET", "/portfolio/positions?limit=100")
    positions = pos.get("market_positions", []) or pos.get("positions", [])
    if not positions:
        print("No positions found")
    for p in positions:
        ticker = p.get("ticker", "?")
        qty = p.get("position", p.get("total_traded", 0))
        side = p.get("market_position", {}) if isinstance(p, dict) else {}
        print(f"  {ticker}: {json.dumps(p, default=str)}")
except Exception as e:
    print(f"Positions error: {e}")

# 3. Check settlements - look for our FEB16 markets
print("\n--- FEB16 MARKET STATUS ---")
feb16_tickers = ["KXHIGHMIA-26FEB16-T79", "KXHIGHMIA-26FEB16-B79.5", 
                  "KXHIGHLAX-26FEB16-T65", "KXHIGHLAX-26FEB16-B58.5"]
for ticker in feb16_tickers:
    try:
        m = api("GET", f"/markets/{ticker}")
        market = m.get("market", m)
        status = market.get("status", "?")
        result = market.get("result", "?")
        print(f"  {ticker}: status={status}, result={result}")
    except Exception as e:
        print(f"  {ticker}: {e}")

# 4. Check fills/settlements
print("\n--- RECENT FILLS ---")
try:
    fills = api("GET", "/portfolio/fills?limit=20")
    for f in (fills.get("fills", []) or []):
        print(f"  {f.get('ticker')}: side={f.get('side')} price={f.get('price')} count={f.get('count')} created={f.get('created_time','?')}")
except Exception as e:
    print(f"Fills error: {e}")

# 5. Check settlements
print("\n--- SETTLEMENTS ---")
try:
    settlements = api("GET", "/portfolio/settlements?limit=20")
    for s in (settlements.get("settlements", []) or []):
        print(f"  {json.dumps(s, default=str)}")
    if not settlements.get("settlements"):
        print("  No settlements found")
except Exception as e:
    print(f"Settlements error: {e}")

# 6. Find new trading opportunities - scan open KXHIGH markets
print("\n--- SCANNING OPEN MARKETS ---")
weather_markets = []
cursor = None
for page in range(10):
    path = "/markets?status=open&limit=200"
    if cursor:
        path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except:
        break
    for m in data.get("markets", []):
        t = m.get("ticker", "")
        if "KXHIGH" in t:
            weather_markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"):
        break

print(f"Found {len(weather_markets)} open KXHIGH markets")
for m in weather_markets[:20]:
    ticker = m.get("ticker", "")
    yes_ask = m.get("yes_ask", "?")
    no_ask = m.get("no_ask", "?") 
    print(f"  {ticker}: yes_ask={yes_ask}, no_ask={no_ask}, title={m.get('title','')[:60]}")

# 7. Get forecasts for cities with open markets and place trades
import re

def get_forecast(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America/New_York&forecast_days=3"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

city_coords = {
    "MIA": (25.76, -80.19), "LAX": (34.05, -118.24), "HOU": (29.76, -95.37),
    "NYC": (40.71, -74.01), "CHI": (41.88, -87.63), "DEN": (39.74, -104.98),
    "ATL": (33.75, -84.39), "DFW": (32.78, -96.80), "PHX": (33.45, -112.07),
}

# Extract unique cities from open markets
cities_needed = set()
for m in weather_markets:
    match = re.search(r"KXHIGH(\w+)-", m["ticker"])
    if match:
        cities_needed.add(match.group(1))

print(f"\nCities with open markets: {cities_needed}")

forecasts = {}
for city in cities_needed:
    if city in city_coords:
        try:
            fc = get_forecast(*city_coords[city])
            forecasts[city] = fc
            print(f"  {city} forecast: {fc}")
        except Exception as e:
            print(f"  {city} forecast error: {e}")

# Find edges and trade
print("\n--- TRADE OPPORTUNITIES ---")
trades_placed = []

for m in weather_markets:
    ticker = m["ticker"]
    match = re.search(r"KXHIGH(\w+)-26FEB(\d+)-([TB])([\d.]+)", ticker)
    if not match:
        continue
    city, day, bracket_type, threshold_str = match.groups()
    threshold = float(threshold_str)
    date_str = f"2026-02-{day}"
    
    if city not in forecasts or date_str not in forecasts[city]:
        continue
    
    fc_temp = forecasts[city][date_str]
    yes_ask = m.get("yes_ask", 100)
    no_ask = m.get("no_ask", 100)
    
    if yes_ask is None or no_ask is None:
        continue
    
    # For T (above threshold): YES wins if temp >= threshold
    if bracket_type == "T":
        our_prob = 0.95 if fc_temp >= threshold + 5 else (0.05 if fc_temp <= threshold - 5 else 0.5 + (fc_temp - threshold) * 0.09)
    else:  # B (bracket) - harder to estimate, skip for now
        continue
    
    our_prob = max(0.02, min(0.98, our_prob))
    
    # Check YES buy edge
    yes_price = yes_ask / 100.0
    yes_edge = our_prob - yes_price
    
    # Check NO buy edge  
    no_price = no_ask / 100.0
    no_edge = (1 - our_prob) - no_price
    
    if yes_edge > 0.15 and yes_ask <= 50 and len(trades_placed) < 2:
        print(f"  BUY YES {ticker}: fc={fc_temp}°F, threshold={threshold}, prob={our_prob:.2f}, price={yes_ask}¢, edge={yes_edge:.2f}")
        try:
            order = api("POST", "/portfolio/orders", {
                "ticker": ticker, "action": "buy", "side": "yes",
                "type": "limit", "count": 10, "yes_price": yes_ask
            })
            oid = order.get("order", {}).get("order_id", "?")
            print(f"    ORDER PLACED: {oid}")
            trades_placed.append({"cycle": 10, "ticker": ticker, "side": "yes", "price": yes_ask, "count": 10, "edge": yes_edge, "forecast_temp": fc_temp, "threshold": threshold, "order_id": oid, "status": "executed", "timestamp": datetime.datetime.now().isoformat()})
        except Exception as e:
            print(f"    Order error: {e}")
    
    elif no_edge > 0.15 and no_ask <= 50 and len(trades_placed) < 2:
        print(f"  BUY NO {ticker}: fc={fc_temp}°F, threshold={threshold}, prob={our_prob:.2f}, no_price={no_ask}¢, edge={no_edge:.2f}")
        try:
            order = api("POST", "/portfolio/orders", {
                "ticker": ticker, "action": "buy", "side": "no",
                "type": "limit", "count": 10, "no_price": no_ask
            })
            oid = order.get("order", {}).get("order_id", "?")
            print(f"    ORDER PLACED: {oid}")
            trades_placed.append({"cycle": 10, "ticker": ticker, "side": "no", "price": no_ask, "count": 10, "edge": no_edge, "forecast_temp": fc_temp, "threshold": threshold, "order_id": oid, "status": "executed", "timestamp": datetime.datetime.now().isoformat()})
        except Exception as e:
            print(f"    Order error: {e}")

# Update trades file
if trades_placed:
    existing = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
    existing.extend(trades_placed)
    TRADES_PATH.write_text(json.dumps(existing, indent=2, default=str))
    print(f"\nSaved {len(trades_placed)} new trades to {TRADES_PATH}")

print(f"\n--- CYCLE 10 COMPLETE: {len(trades_placed)} trades placed ---")
