#!/usr/bin/env python3
"""Cycle 13: Check settlements, market statuses, portfolio, P&L"""
import json, time, base64, sys, os
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

with open(KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def get_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}"
    sig = private_key.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": API_KEY, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}

def api(method, path):
    url = BASE_URL + path
    headers = get_headers(method, "/trade-api/v2" + path)
    r = requests.request(method, url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

# 1. Get balance
print("=== BALANCE ===")
bal = api("GET", "/portfolio/balance")
print(json.dumps(bal, indent=2))

# 2. Get all positions
print("\n=== POSITIONS ===")
positions = []
cursor = None
for _ in range(20):
    path = "/portfolio/positions?limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    data = api("GET", path)
    batch = data.get("market_positions", [])
    positions.extend(batch)
    cursor = data.get("cursor")
    if not cursor or not batch:
        break
print(f"Total positions: {len(positions)}")

# Separate by ticker pattern
feb16_weather = []
feb17_weather = []
other = []
for p in positions:
    t = p.get("ticker", "")
    if "KXHIGH" in t and "FEB16" in t:
        feb16_weather.append(p)
    elif "KXHIGH" in t and "FEB17" in t:
        feb17_weather.append(p)
    else:
        other.append(p)

print(f"\nFeb 16 weather: {len(feb16_weather)}")
print(f"Feb 17 weather: {len(feb17_weather)}")
print(f"Other: {len(other)}")

# 3. Check Feb 16 market details
print("\n=== FEB 16 WEATHER MARKETS ===")
for p in feb16_weather:
    ticker = p["ticker"]
    print(f"\n--- {ticker} ---")
    print(f"  Position: {p.get('position', 0)} contracts, side: YES={p.get('total_traded', 'n/a')}")
    print(f"  Market value: {p.get('market_exposure', 'n/a')}")
    print(f"  Realized P&L: {p.get('realized_pnl', 'n/a')}")
    print(f"  Resting orders: {p.get('resting_orders_count', 0)}")
    print(f"  Full: {json.dumps(p, indent=4)}")
    # Get market details
    try:
        mkt = api("GET", f"/markets/{ticker}")
        m = mkt.get("market", {})
        print(f"  Market status: {m.get('status')}")
        print(f"  Result: {m.get('result')}")
        print(f"  Settlement: close={m.get('close_time')}, exp={m.get('expiration_time')}")
        print(f"  Settlement value: {m.get('settlement_value')}")
    except Exception as e:
        print(f"  Market lookup error: {e}")

print("\n=== FEB 17 WEATHER MARKETS ===")
for p in feb17_weather:
    ticker = p["ticker"]
    print(f"\n--- {ticker} ---")
    print(f"  Full: {json.dumps(p, indent=4)}")
    try:
        mkt = api("GET", f"/markets/{ticker}")
        m = mkt.get("market", {})
        print(f"  Market status: {m.get('status')}")
        print(f"  Result: {m.get('result')}")
    except Exception as e:
        print(f"  Market lookup error: {e}")

# 4. Get settlements/fills
print("\n=== RECENT FILLS ===")
try:
    fills = api("GET", "/portfolio/fills?limit=100")
    for f in fills.get("fills", [])[:30]:
        print(f"  {f.get('ticker')} | {f.get('side')} | {f.get('count')}x @ {f.get('price')}¢ | {f.get('created_time')} | type={f.get('type')}")
except Exception as e:
    print(f"  Error: {e}")

# 5. Get settlements specifically
print("\n=== SETTLEMENTS ===")
try:
    settlements = api("GET", "/portfolio/settlements?limit=50")
    print(json.dumps(settlements, indent=2))
except Exception as e:
    print(f"  Error: {e}")

# 6. Check orders
print("\n=== RESTING ORDERS ===")
try:
    orders = api("GET", "/portfolio/orders?status=resting&limit=50")
    for o in orders.get("orders", []):
        print(f"  {o.get('ticker')} | {o.get('side')} | {o.get('remaining_count')}x @ {o.get('price')}¢ | status={o.get('status')}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== OTHER POSITIONS (non-weather) ===")
for p in other[:10]:
    print(f"  {p['ticker']}: pos={p.get('position')}, exposure={p.get('market_exposure')}")
if len(other) > 10:
    print(f"  ... and {len(other)-10} more")
