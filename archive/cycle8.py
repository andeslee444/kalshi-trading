#!/usr/bin/env python3
"""Cycle 8: Check settlements, positions, find late-night weather trades."""

import json, time, base64, datetime, sys, re
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
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
config = json.loads(CONFIG_PATH.read_text())
CITIES = config["cities"]

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

def get_forecast(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m: return None
    return {"city": m.group(1), "date": f"{2000+int(m.group(2))}-{MONTHS[m.group(3)]:02d}-{int(m.group(4)):02d}", "direction": m.group(5), "threshold": float(m.group(6))}

print("=" * 60)
print("CYCLE 8 — Settlement Check & Late-Night Trades")
print(f"Time: {datetime.datetime.now().isoformat()}")
print("=" * 60)

# 1. Balance
bal = api("GET", "/portfolio/balance")
print(f"\n💰 Balance: ${bal.get('balance',0)/100:.2f}")
print(f"   Raw: {json.dumps(bal)}")

# 2. Positions
print("\n📊 POSITIONS:")
try:
    pos = api("GET", "/portfolio/positions?limit=200")
    positions = pos.get("market_positions", [])
    if not positions:
        print("   No open positions")
    for p in positions:
        ticker = p.get("ticker","")
        yes_qty = p.get("position", 0)  
        cost = p.get("total_traded", 0)
        print(f"   {ticker}: qty={yes_qty}, cost={cost/100:.2f}")
except Exception as e:
    print(f"   Error: {e}")
    positions = []

# 3. Settlements - check fills/orders history
print("\n📋 RECENT ORDERS:")
try:
    orders = api("GET", "/portfolio/orders?limit=50")
    for o in orders.get("orders", [])[:20]:
        status = o.get("status","")
        ticker = o.get("ticker","")
        side = o.get("side","")
        price = o.get("yes_price") or o.get("no_price") or 0
        filled = o.get("place_count",0)
        print(f"   {status:10s} {ticker} {side} @ {price}¢ qty={filled}")
except Exception as e:
    print(f"   Error: {e}")

# 4. Check settlements
print("\n🏁 SETTLEMENTS:")
try:
    settlements = api("GET", "/portfolio/settlements?limit=50")
    slist = settlements.get("settlements", [])
    if not slist:
        print("   No settlements yet")
    for s in slist[:20]:
        ticker = s.get("ticker","")
        revenue = s.get("revenue", 0)
        settled = s.get("settled_at","")
        print(f"   {ticker}: revenue=${revenue/100:.2f} settled={settled}")
except Exception as e:
    print(f"   Settlements endpoint error: {e}")

# 5. Find weather markets expiring tonight
print("\n🔍 SCANNING WEATHER MARKETS EXPIRING SOON...")
weather_markets = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except Exception as e:
        print(f"   Page {page} error: {e}")
        break
    batch = data.get("markets", [])
    for m in batch:
        t = m.get("ticker", "")
        if "KXHIGH" in t:
            weather_markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not batch: break

print(f"   Found {len(weather_markets)} KXHIGH markets")

# Group by date
today = datetime.date.today().isoformat()  # 2026-02-16
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
tonight_markets = []
future_markets = []

for m in weather_markets:
    parsed = parse_ticker(m["ticker"])
    if not parsed: continue
    m["_parsed"] = parsed
    if parsed["date"] <= today:
        tonight_markets.append(m)
    elif parsed["date"] == tomorrow:
        future_markets.append(m)

print(f"   Expiring today/past: {len(tonight_markets)}")
print(f"   Tomorrow (Feb 17): {len(future_markets)}")

# 6. Get forecasts and find best trades on tomorrow's markets
print("\n🌡️  FORECASTS:")
forecasts = {}
for code, info in CITIES.items():
    try:
        forecasts[code] = get_forecast(info["lat"], info["lon"])
        tomorrow_temp = forecasts[code].get(tomorrow, "N/A")
        print(f"   {info['name']}: tomorrow high = {tomorrow_temp}°F")
    except Exception as e:
        print(f"   {info['name']}: error {e}")

# 7. Find best opportunities
print("\n🎯 BEST OPPORTUNITIES (tomorrow's markets):")
opps = []
for m in future_markets:
    p = m["_parsed"]
    city = p["city"]
    if city not in forecasts or tomorrow not in forecasts.get(city, {}):
        continue
    temp = forecasts[city][tomorrow]
    
    # Compute our probability
    diff = temp - p["threshold"]
    if p["direction"] == "T":
        if diff > 6: prob = 0.95
        elif diff > 3: prob = 0.85
        elif diff > 1: prob = 0.65
        elif diff > -1: prob = 0.45
        elif diff > -3: prob = 0.25
        elif diff > -6: prob = 0.10
        else: prob = 0.03
    else:
        center = p["threshold"] + 0.5
        dist = abs(temp - center)
        if dist < 1: prob = 0.30
        elif dist < 2: prob = 0.20
        elif dist < 3: prob = 0.12
        elif dist < 5: prob = 0.06
        else: prob = 0.02

    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        mkt = (yes_bid + yes_ask) / 200
    elif yes_ask and yes_ask < 100:
        mkt = yes_ask / 100
    else:
        continue
    
    edge = prob - mkt
    opps.append({"ticker": m["ticker"], "temp": temp, "threshold": p["threshold"], "dir": p["direction"], 
                 "prob": prob, "mkt": mkt, "edge": edge, "yes_ask": yes_ask, "no_ask": no_ask, "city": CITIES.get(city, {}).get("name", city)})

opps.sort(key=lambda x: abs(x["edge"]), reverse=True)

for o in opps[:15]:
    arrow = "BUY YES" if o["edge"] > 0 else "BUY NO"
    print(f"   {o['ticker']:40s} forecast={o['temp']:.0f}°F thresh={o['threshold']} prob={o['prob']*100:.0f}% mkt={o['mkt']*100:.0f}% edge={o['edge']*100:+.1f}% → {arrow}")

# 8. Place top 2-3 trades
print("\n💸 PLACING TRADES:")
trades_placed = 0
for o in opps:
    if trades_placed >= 3:
        break
    if abs(o["edge"]) < 0.10:  # Need at least 10% edge
        continue
    
    ticker = o["ticker"]
    if o["edge"] > 0 and o["yes_ask"] and o["yes_ask"] < 99:
        side, price = "yes", o["yes_ask"]
        reason = f"{o['city']} {o['temp']:.0f}°F vs {o['threshold']}°F, YES at {price}¢, our prob {o['prob']*100:.0f}%, edge +{o['edge']*100:.1f}%"
    elif o["edge"] < 0 and o["no_ask"] and o["no_ask"] < 99:
        side, price = "no", o["no_ask"]
        reason = f"{o['city']} {o['temp']:.0f}°F vs {o['threshold']}°F, NO at {price}¢, our prob {(1-o['prob'])*100:.0f}%, edge +{abs(o['edge'])*100:.1f}%"
    else:
        continue
    
    count = max(1, min(500 // price, 10))
    print(f"\n   → {ticker}: {count}x {side} @ {price}¢")
    print(f"     {reason}")
    
    order = {"ticker": ticker, "action": "buy", "side": side, "type": "limit", "count": count}
    if side == "yes": order["yes_price"] = price
    else: order["no_price"] = price
    
    try:
        result = api("POST", "/portfolio/orders", order)
        oi = result.get("order", {})
        print(f"     ✓ Order {oi.get('order_id','?')}: {oi.get('status','?')}")
        trades_placed += 1
        
        # Save trade
        trades = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
        trades.append({
            "timestamp": datetime.datetime.now().isoformat(), "cycle": 8,
            "ticker": ticker, "side": side, "price": price, "count": count,
            "reasoning": reason, "edge": round(o["edge"], 4),
            "order_id": oi.get("order_id"), "status": oi.get("status"),
        })
        TRADES_PATH.write_text(json.dumps(trades, indent=2))
    except requests.exceptions.HTTPError as e:
        print(f"     ✗ Failed: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"     ✗ Failed: {e}")

print(f"\n✅ Cycle 8 complete. {trades_placed} trades placed.")
