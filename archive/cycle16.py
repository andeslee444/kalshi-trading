#!/usr/bin/env python3
"""Trade Cycle #16 - Morning Feb 17, 2026"""
import json, time, base64, datetime, sys, re, requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
CONFIG = json.loads((PROJECT_DIR / "config" / "kalshi-config.json").read_text())
CITIES = CONFIG["cities"]

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

# 1. Balance
print("=" * 60)
print("TRADE CYCLE #16 - Morning Feb 17, 2026")
print("=" * 60)

bal = api("GET", "/portfolio/balance")
print(f"\n💰 Balance: ${bal.get('balance',0)/100:.2f}")

# 2. Check positions/settlements
print("\n📊 Current Positions:")
try:
    pos = api("GET", "/portfolio/positions?limit=100")
    positions = pos.get("market_positions", [])
    if not positions:
        print("  No open positions")
    for p in positions:
        ticker = p.get("ticker","")
        yes_count = p.get("total_traded",0)
        print(f"  {ticker}: {json.dumps({k:v for k,v in p.items() if v and k != 'ticker'})}")
except Exception as e:
    print(f"  Error: {e}")

# 3. Check settlements - look for Feb 16 resolved markets
print("\n🔍 Checking Feb 16 settlements:")
try:
    data = api("GET", "/markets?status=settled&limit=200")
    feb16_settled = [m for m in data.get("markets",[]) if "KXHIGH" in m.get("ticker","") and "26FEB16" in m.get("ticker","")]
    if feb16_settled:
        for m in feb16_settled[:10]:
            result = m.get("result","?")
            print(f"  {m['ticker']}: result={result}")
    else:
        print("  No Feb 16 KXHIGH settlements found yet")
except Exception as e:
    print(f"  Error: {e}")

# 4. Get forecasts for Feb 17
print("\n🌡️ Forecasts for Feb 17:")
forecasts = {}
for code, info in CITIES.items():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={info['lat']}&longitude={info['lon']}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=3"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        d = r.json()["daily"]
        fc = dict(zip(d["time"], d["temperature_2m_max"]))
        forecasts[code] = fc
        t17 = fc.get("2026-02-17", "N/A")
        print(f"  {info['name']}: {t17}°F")
    except Exception as e:
        print(f"  {info['name']}: error {e}")

# 5. Scan Feb 17 markets and trade
print("\n🔎 Scanning Feb 17 KXHIGH markets...")
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m: return None
    city, yr, mon, day = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    month = MONTHS.get(mon)
    if not month: return None
    return {"city": city, "date": f"{2000+yr}-{month:02d}-{day:02d}", "direction": m.group(5), "threshold": float(m.group(6))}

def compute_prob(forecast_temp, threshold, direction):
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

# Get all open KXHIGH markets
markets = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except: break
    for m in data.get("markets", []):
        if "KXHIGH" in m.get("ticker","") and "26FEB17" in m.get("ticker",""):
            markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"): break

print(f"Found {len(markets)} Feb 17 KXHIGH markets")

opps = []
for m in markets:
    parsed = parse_ticker(m["ticker"])
    if not parsed or parsed["city"] not in forecasts: continue
    fc = forecasts[parsed["city"]].get("2026-02-17")
    if not fc: continue
    
    prob = compute_prob(fc, parsed["threshold"], parsed["direction"])
    yes_ask = m.get("yes_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    no_ask = m.get("no_ask", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        mp = (yes_bid + yes_ask) / 200
    elif yes_ask and yes_ask < 100:
        mp = yes_ask / 100
    elif m.get("last_price", 0) and m["last_price"] < 100:
        mp = m["last_price"] / 100
    else: continue
    
    edge = prob - mp
    city_name = CITIES.get(parsed["city"], {}).get("name", parsed["city"])
    opps.append({"ticker": m["ticker"], "m": m, "parsed": parsed, "fc": fc, "prob": prob, "mp": mp, "edge": edge, "city": city_name, "yes_ask": yes_ask, "no_ask": no_ask})

opps.sort(key=lambda x: abs(x["edge"]), reverse=True)
print(f"\nTop opportunities (edge >= 8%):")
for o in opps[:15]:
    e = o["edge"]
    side = "YES" if e > 0 else "NO"
    print(f"  {o['ticker']}: {o['city']} fc={o['fc']}°F, prob={o['prob']*100:.0f}%, mkt={o['mp']*100:.0f}%, edge={e*100:+.1f}% → {side}")

# Place up to 3 trades
trades_placed = []
for o in opps:
    if len(trades_placed) >= 3: break
    if abs(o["edge"]) < 0.08: continue
    
    if o["edge"] > 0 and o["yes_ask"] and o["yes_ask"] < 99:
        side, price = "yes", o["yes_ask"]
    elif o["edge"] < 0 and o["no_ask"] and o["no_ask"] < 99:
        side, price = "no", o["no_ask"]
    else: continue
    
    count = max(1, min(500 // price, 10))
    print(f"\n→ TRADE: {o['ticker']} {side.upper()} x{count} @ {price}¢ (edge={o['edge']*100:+.1f}%)")
    
    try:
        body = {"ticker": o["ticker"], "action": "buy", "side": side, "type": "limit", "count": count}
        if side == "yes": body["yes_price"] = price
        else: body["no_price"] = price
        result = api("POST", "/portfolio/orders", body)
        oi = result.get("order", {})
        status = oi.get("status", "?")
        print(f"  ✅ Order {oi.get('order_id','?')}: {status}")
        trades_placed.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": o["ticker"], "side": side, "price": price, "count": count,
            "edge": round(o["edge"], 4), "forecast_temp": o["fc"],
            "threshold": o["parsed"]["threshold"], "city": o["city"],
            "order_id": oi.get("order_id"), "status": status,
            "reasoning": f"{o['city']} fc={o['fc']}°F, threshold={o['parsed']['threshold']}, edge={o['edge']*100:+.1f}%"
        })
    except requests.exceptions.HTTPError as e:
        print(f"  ❌ Failed: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")

# Save trades
if trades_placed:
    existing = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
    existing.extend(trades_placed)
    TRADES_PATH.write_text(json.dumps(existing, indent=2))
    print(f"\n💾 Saved {len(trades_placed)} trades to data file")

# Final balance
bal2 = api("GET", "/portfolio/balance")
print(f"\n💰 Final Balance: ${bal2.get('balance',0)/100:.2f}")
print("\nDone! ✓")
