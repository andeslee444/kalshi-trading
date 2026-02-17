#!/usr/bin/env python3
"""Check settlements and market statuses for Feb 16 weather trades."""
import json, time, base64, sys, os
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

def api(method, path):
    url = BASE_URL + path
    headers = get_headers(method, "/trade-api/v2" + path)
    r = requests.request(method, url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

print("=" * 60)
print("KALSHI SETTLEMENT CHECK - Feb 16/17 2026")
print("=" * 60)

# 1. Check settlements
print("\n--- PORTFOLIO SETTLEMENTS ---")
try:
    data = api("GET", "/portfolio/settlements")
    print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")

# 2. Check portfolio balance
print("\n--- PORTFOLIO BALANCE ---")
try:
    data = api("GET", "/portfolio/balance")
    print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")

# 3. Check positions
print("\n--- PORTFOLIO POSITIONS ---")
try:
    data = api("GET", "/portfolio/positions")
    positions = data.get("market_positions", []) or data.get("positions", [])
    if positions:
        for p in positions:
            print(json.dumps(p, indent=2))
    else:
        print(f"Raw: {json.dumps(data, indent=2)[:500]}")
except Exception as e:
    print(f"Error: {e}")

# 4. Check fills (actual executed trades)
print("\n--- FILLS ---")
try:
    data = api("GET", "/portfolio/fills?limit=100")
    fills = data.get("fills", [])
    print(f"Total fills: {len(fills)}")
    for f in fills:
        print(f"  {f.get('ticker')} {f.get('side')} {f.get('count')}x @ {f.get('yes_price', f.get('no_price', '?'))}c | created: {f.get('created_time', '?')}")
except Exception as e:
    print(f"Error: {e}")

# 5. Check market statuses for our traded tickers
print("\n--- MARKET STATUSES ---")
trades = json.loads(TRADES_PATH.read_text())
tickers = list(set(t["ticker"] for t in trades))
for ticker in sorted(tickers):
    try:
        data = api("GET", f"/markets/{ticker}")
        m = data.get("market", data)
        status = m.get("status", "?")
        result = m.get("result", "?")
        close_time = m.get("close_time", "?")
        print(f"  {ticker}: status={status}, result={result}, close={close_time}")
    except Exception as e:
        print(f"  {ticker}: ERROR {e}")

# 6. Check orders
print("\n--- ORDERS ---")
try:
    data = api("GET", "/portfolio/orders?limit=100")
    orders = data.get("orders", [])
    print(f"Total orders: {len(orders)}")
    for o in orders:
        print(f"  {o.get('ticker')} {o.get('side')} {o.get('status')} {o.get('remaining_count')}/{o.get('count')} @ {o.get('yes_price', o.get('no_price', '?'))}c")
except Exception as e:
    print(f"Error: {e}")
