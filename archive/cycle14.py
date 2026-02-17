#!/usr/bin/env python3
"""Trade Cycle #14 - Feb 17 early morning scan"""
import json, time, base64, datetime, re, sys
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

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
    headers = get_headers(method, "/trade-api/v2" + path)
    r = requests.request(method, url, headers=headers, json=body if method=="POST" else None, timeout=15)
    r.raise_for_status()
    return r.json()

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m: return None
    return {"city": m.group(1), "date": f"{2000+int(m.group(2))}-{MONTHS[m.group(3)]:02d}-{int(m.group(4)):02d}", "direction": m.group(5), "threshold": float(m.group(6))}

def get_forecast(lat, lon):
    r = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7", timeout=10)
    r.raise_for_status()
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

def compute_probability(forecast_temp, threshold, direction):
    diff = forecast_temp - threshold
    if direction == "T":
        if diff > 6: return 0.95
        elif diff > 3: return 0.85
        elif diff > 1: return 0.65
        elif diff > -1: return 0.45
        elif diff > -3: return 0.25
        elif diff > -6: return 0.10
        else: return 0.03
    else:
        dist = abs(forecast_temp - (threshold + 0.5))
        if dist < 1: return 0.30
        elif dist < 2: return 0.20
        elif dist < 3: return 0.12
        elif dist < 5: return 0.06
        else: return 0.02

print("=" * 60)
print("CYCLE #14 - Feb 17 Early Morning Scan")
print("=" * 60)

# 1. Balance
bal = api("GET", "/portfolio/balance")
print(f"\n💰 Balance: ${bal.get('balance',0)/100:.2f}")

# 2. Positions (check Feb 16 settlements)
print("\n📊 Current Positions:")
try:
    pos = api("GET", "/portfolio/positions?limit=100")
    positions = pos.get("market_positions", [])
    feb16_pos = [p for p in positions if "FEB16" in p.get("ticker","")]
    feb17_pos = [p for p in positions if "FEB17" in p.get("ticker","")]
    other_pos = [p for p in positions if p not in feb16_pos and p not in feb17_pos]
    
    if feb16_pos:
        print(f"\n  Feb 16 positions ({len(feb16_pos)}):")
        for p in feb16_pos:
            print(f"    {p['ticker']}: {p.get('total_traded',0)} contracts, resting={p.get('resting_orders_count',0)}")
    else:
        print("  No Feb 16 positions found (may have settled)")
    
    if feb17_pos:
        print(f"\n  Feb 17 positions ({len(feb17_pos)}):")
        for p in feb17_pos:
            print(f"    {p['ticker']}: {p.get('total_traded',0)} contracts")
    
    if other_pos:
        print(f"\n  Other positions ({len(other_pos)}):")
        for p in other_pos[:5]:
            print(f"    {p['ticker']}: {p.get('total_traded',0)} contracts")
except Exception as e:
    print(f"  Positions error: {e}")

# 3. Check settlements via fills
print("\n🔍 Recent fills/settlements:")
try:
    fills = api("GET", "/portfolio/fills?limit=20")
    for f in fills.get("fills", [])[:10]:
        print(f"  {f.get('ticker','')} | {f.get('action','')} {f.get('side','')} | {f.get('count',0)}x @ {f.get('yes_price',0)}¢ | {f.get('created_time','')[:19]}")
except Exception as e:
    print(f"  Fills error: {e}")

# 4. Scan Feb 17 weather markets
print("\n🌡️ Scanning Feb 17 weather markets...")
weather_markets = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except: break
    for m in data.get("markets", []):
        t = m.get("ticker", "")
        if "KXHIGH" in t and "FEB17" in t:
            weather_markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"): break

print(f"Found {len(weather_markets)} Feb 17 KXHIGH markets")

# Get forecasts
forecasts = {}
for code, info in CITIES.items():
    try:
        forecasts[code] = get_forecast(info["lat"], info["lon"])
        fc_val = forecasts[code].get("2026-02-17", "N/A")
        print(f"  {info['name']}: Feb 17 forecast = {fc_val}°F")
    except Exception as e:
        print(f"  {info['name']}: forecast error: {e}")

# Find opportunities
opportunities = []
for m in weather_markets:
    parsed = parse_ticker(m["ticker"])
    if not parsed or parsed["city"] not in forecasts: continue
    fc = forecasts[parsed["city"]].get(parsed["date"])
    if not fc: continue
    
    our_prob = compute_probability(fc, parsed["threshold"], parsed["direction"])
    yes_ask = m.get("yes_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    no_ask = m.get("no_ask", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        market_price = (yes_bid + yes_ask) / 2 / 100
    elif yes_ask and yes_ask < 100:
        market_price = yes_ask / 100
    else:
        continue
    
    edge = our_prob - market_price
    opportunities.append({
        "ticker": m["ticker"], "market": m, "parsed": parsed,
        "forecast": fc, "our_prob": our_prob, "market_price": market_price,
        "edge": edge, "yes_ask": yes_ask, "no_ask": no_ask,
        "city_name": CITIES[parsed["city"]]["name"]
    })

opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)
print(f"\n📈 Top opportunities (edge >= 8%):")
for o in opportunities[:15]:
    e = o["edge"]
    direction = "YES" if e > 0 else "NO"
    print(f"  {o['ticker']}: forecast={o['forecast']:.1f}°F, prob={o['our_prob']*100:.0f}%, mkt={o['market_price']*100:.0f}%, edge={abs(e)*100:.1f}% → {direction}")

# 5. Place trades (top 3)
trades_placed = 0
print("\n🎯 Placing trades:")
for o in opportunities:
    if trades_placed >= 3: break
    if abs(o["edge"]) < 0.08: continue
    
    e = o["edge"]
    if e > 0 and o["yes_ask"] and o["yes_ask"] < 99:
        side, price = "yes", o["yes_ask"]
    elif e < 0 and o["no_ask"] and o["no_ask"] < 99:
        side, price = "no", o["no_ask"]
    else:
        continue
    
    count = max(1, min(500 // price, 10))
    reasoning = f"{o['city_name']} forecast: {o['forecast']:.1f}°F, {o['ticker']} {side.upper()} at {price}¢, our_prob={o['our_prob']*100:.0f}%, edge={abs(e)*100:.1f}%"
    print(f"\n  → {reasoning}")
    
    try:
        order_body = {"ticker": o["ticker"], "action": "buy", "side": side, "type": "limit", "count": count}
        if side == "yes": order_body["yes_price"] = price
        else: order_body["no_price"] = price
        
        result = api("POST", "/portfolio/orders", order_body)
        oi = result.get("order", {})
        print(f"    ✅ Order {oi.get('order_id','?')[:12]}... status={oi.get('status','?')}")
        trades_placed += 1
        
        # Save trade
        trades = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
        trades.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "cycle": 14,
            "ticker": o["ticker"], "side": side, "price": price,
            "count": count, "reasoning": reasoning,
            "forecast_temp": o["forecast"], "threshold": o["parsed"]["threshold"],
            "edge": round(e, 4),
            "order_id": oi.get("order_id"), "status": oi.get("status"),
        })
        TRADES_PATH.write_text(json.dumps(trades, indent=2))
    except requests.exceptions.HTTPError as e:
        print(f"    ❌ Failed: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"    ❌ Failed: {e}")

print(f"\n✅ Cycle #14 complete. {trades_placed} trades placed.")
