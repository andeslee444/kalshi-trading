#!/usr/bin/env python3
"""Trade cycle #15 - Feb 17 morning scan"""
import json, time, base64, datetime, os, sys, re, requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
config = json.loads((PROJECT_DIR / "config" / "kalshi-config.json").read_text())
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
    r = requests.request(method, url, headers=h, json=body if method=="POST" else None, timeout=15)
    r.raise_for_status()
    return r.json()

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m: return None
    return {"city": m.group(1), "date": f"{2000+int(m.group(2))}-{MONTHS[m.group(3)]:02d}-{int(m.group(4)):02d}", "direction": m.group(5), "threshold": float(m.group(6))}

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

def get_forecast(lat, lon):
    r = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7", timeout=10)
    r.raise_for_status()
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

print("="*60)
print("TRADE CYCLE #15 - Feb 17 Morning")
print("="*60)

# 1. Balance
bal = api("GET", "/portfolio/balance")
print(f"\n💰 Balance: ${bal.get('balance',0)/100:.2f}")

# 2. Check positions (settlements)
print("\n📊 Current Positions:")
try:
    pos = api("GET", "/portfolio/positions?limit=100")
    positions = pos.get("market_positions", [])
    if positions:
        for p in positions:
            ticker = p.get("ticker","")
            yes_qty = p.get("position",0)
            cost = p.get("market_exposure",0)
            print(f"  {ticker}: pos={yes_qty}, exposure={cost/100:.2f}")
    else:
        print("  No open positions (Feb 16 trades may have settled!)")
except Exception as e:
    print(f"  Error: {e}")

# 3. Check settled orders
print("\n📋 Recent Orders (check settlements):")
try:
    orders = api("GET", "/portfolio/orders?limit=20")
    for o in orders.get("orders", [])[:10]:
        status = o.get("status","")
        ticker = o.get("ticker","")
        side = o.get("side","")
        print(f"  {ticker} {side} → {status}")
except Exception as e:
    print(f"  Error: {e}")

# 4. Fetch forecasts for Feb 17
print("\n🌡️ Feb 17 Forecasts:")
forecasts = {}
for code, info in CITIES.items():
    try:
        fc = get_forecast(info["lat"], info["lon"])
        forecasts[code] = fc
        t = fc.get("2026-02-17")
        if t: print(f"  {info['name']}: {t}°F")
    except Exception as e:
        print(f"  {info['name']}: error {e}")

# 5. Scan Feb 17 markets and trade
print("\n🔍 Scanning KXHIGH markets for Feb 17...")
weather = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except: break
    for m in data.get("markets", []):
        if "KXHIGH" in m.get("ticker", ""):
            weather.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"): break

print(f"Found {len(weather)} KXHIGH markets total")

# Filter to Feb 17 and find opportunities
opps = []
for m in weather:
    parsed = parse_ticker(m.get("ticker",""))
    if not parsed or parsed["date"] != "2026-02-17": continue
    city = parsed["city"]
    if city not in forecasts or "2026-02-17" not in forecasts.get(city, {}): continue
    
    fc_temp = forecasts[city]["2026-02-17"]
    our_prob = compute_probability(fc_temp, parsed["threshold"], parsed["direction"])
    
    yes_ask = m.get("yes_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    no_ask = m.get("no_ask", 0)
    last = m.get("last_price", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        mp = (yes_bid + yes_ask) / 2 / 100
    elif yes_ask and yes_ask < 100:
        mp = yes_ask / 100
    elif last and last < 100:
        mp = last / 100
    else:
        continue
    
    edge = our_prob - mp
    opps.append({"ticker": m["ticker"], "market": m, "parsed": parsed, "forecast": fc_temp, "our_prob": our_prob, "market_price": mp, "edge": edge, "city": city, "yes_ask": yes_ask, "no_ask": no_ask})

opps.sort(key=lambda x: abs(x["edge"]), reverse=True)
print(f"Feb 17 opportunities (edge >= 8%): {sum(1 for o in opps if abs(o['edge']) >= 0.08)}")

trades_placed = 0
now = datetime.datetime.now()
for opp in opps:
    if trades_placed >= 3: break
    if abs(opp["edge"]) < 0.08: continue
    
    ticker = opp["ticker"]
    edge = opp["edge"]
    city_name = CITIES[opp["city"]]["name"]
    
    if edge > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
        side, price = "yes", opp["yes_ask"]
        reasoning = f"{city_name} forecast: {opp['forecast']}°F, {ticker} YES@{price}¢, prob {opp['our_prob']*100:.0f}%, edge +{edge*100:.1f}%"
    elif edge < 0 and opp["no_ask"] and opp["no_ask"] < 99:
        side, price = "no", opp["no_ask"]
        reasoning = f"{city_name} forecast: {opp['forecast']}°F, {ticker} NO@{price}¢, prob {(1-opp['our_prob'])*100:.0f}%, edge +{abs(edge)*100:.1f}%"
    else:
        continue
    
    count = max(1, min(500 // price, 10))
    print(f"\n→ TRADE: {reasoning} | {count}x")
    
    try:
        body = {"ticker": ticker, "action": "buy", "side": side, "type": "limit", "count": count}
        if side == "yes": body["yes_price"] = price
        else: body["no_price"] = price
        result = api("POST", "/portfolio/orders", body)
        oi = result.get("order", {})
        print(f"  ✅ Order {oi.get('order_id','?')[:12]}... status: {oi.get('status','?')}")
        trades_placed += 1
        
        # Save trade
        trades = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
        trades.append({"timestamp": now.isoformat(), "cycle": 15, "ticker": ticker, "side": side, "price": price, "count": count, "reasoning": reasoning, "forecast_temp": opp["forecast"], "threshold": opp["parsed"]["threshold"], "edge": round(edge, 4), "order_id": oi.get("order_id"), "status": oi.get("status")})
        TRADES_PATH.write_text(json.dumps(trades, indent=2))
    except requests.exceptions.HTTPError as e:
        print(f"  ❌ Failed: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")

if trades_placed == 0:
    print("\nNo trades placed (no good opportunities or markets not yet open)")

# Final balance
bal2 = api("GET", "/portfolio/balance")
print(f"\n💰 Final Balance: ${bal2.get('balance',0)/100:.2f}")
print(f"Trades placed this cycle: {trades_placed}")
