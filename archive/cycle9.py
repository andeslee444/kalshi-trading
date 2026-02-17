#!/usr/bin/env python3
"""Kalshi Trade Cycle #9 - Late night check, settlements, new trades"""
import json, time, base64, datetime, os, sys, re
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
print("KALSHI TRADE CYCLE #9 — Late Night Feb 16")
print("=" * 60)

# 1. Balance
bal = api("GET", "/portfolio/balance")
print(f"\n💰 Balance: ${bal.get('balance',0)/100:.2f}")

# 2. Check settlements
print("\n📋 SETTLEMENTS:")
try:
    settlements = api("GET", "/portfolio/settlements")
    s_list = settlements.get("settlements", [])
    if s_list:
        total_pnl = 0
        for s in s_list:
            print(f"  {s.get('ticker','?')} → settled: revenue={s.get('revenue',0)}¢, cost={s.get('cost',0)}¢")
            total_pnl += s.get('revenue', 0) - s.get('cost', 0)
        print(f"  Total settlement P&L: {total_pnl}¢")
    else:
        print("  No settlements found yet.")
    print(f"  (Raw response keys: {list(settlements.keys())})")
except requests.exceptions.HTTPError as e:
    print(f"  Settlement API: {e.response.status_code} — {e.response.text[:300]}")
except Exception as e:
    print(f"  Settlement error: {e}")

# 3. Check positions
print("\n📊 CURRENT POSITIONS:")
try:
    positions = api("GET", "/portfolio/positions")
    pos_list = positions.get("market_positions", positions.get("positions", []))
    if pos_list:
        for p in pos_list:
            ticker = p.get("ticker", "?")
            yes_qty = p.get("position", p.get("yes_count", 0))
            no_qty = p.get("no_count", 0)
            cost = p.get("market_exposure", p.get("total_cost", "?"))
            print(f"  {ticker}: yes={yes_qty}, no={no_qty}, exposure={cost}")
    else:
        print("  No open positions.")
    print(f"  (Raw keys: {list(positions.keys())})")
except Exception as e:
    print(f"  Positions error: {e}")

# 4. Check our traded tickers' market status
print("\n🔍 MARKET STATUS FOR OUR TICKERS:")
our_tickers = set()
trades = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
for t in trades:
    our_tickers.add(t["ticker"])

for ticker in sorted(our_tickers):
    try:
        m = api("GET", f"/markets/{ticker}")
        market = m.get("market", m)
        status = market.get("status", "?")
        result = market.get("result", "?")
        close_time = market.get("close_time", "?")
        last = market.get("last_price", "?")
        print(f"  {ticker}: status={status}, result={result}, close={close_time}, last={last}¢")
    except requests.exceptions.HTTPError as e:
        print(f"  {ticker}: {e.response.status_code} {e.response.text[:100]}")
    except Exception as e:
        print(f"  {ticker}: error {e}")

# 5. Look for late-night opportunities (Feb 17 markets)
print("\n🎯 SCANNING FOR LATE-NIGHT OPPORTUNITIES:")
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m: return None
    return {"city": m.group(1), "date": f"{2000+int(m.group(2))}-{MONTHS.get(m.group(3),0):02d}-{int(m.group(4)):02d}", "direction": m.group(5), "threshold": float(m.group(6))}

# Get forecasts
CITIES = {"MIA":{"lat":25.76,"lon":-80.19,"name":"Miami"},"LAX":{"lat":34.05,"lon":-118.24,"name":"Los Angeles"},"HOU":{"lat":29.76,"lon":-95.37,"name":"Houston"},"AUS":{"lat":30.27,"lon":-97.74,"name":"Austin"},"CHI":{"lat":41.88,"lon":-87.63,"name":"Chicago"},"NY":{"lat":40.71,"lon":-74.01,"name":"New York"},"DEN":{"lat":39.74,"lon":-104.99,"name":"Denver"},"PHIL":{"lat":39.95,"lon":-75.17,"name":"Philadelphia"}}

forecasts = {}
for code, info in CITIES.items():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={info['lat']}&longitude={info['lon']}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
        r = requests.get(url, timeout=10); r.raise_for_status()
        d = r.json()["daily"]
        forecasts[code] = dict(zip(d["time"], d["temperature_2m_max"]))
        print(f"  {info['name']} forecast: { {k:v for k,v in list(forecasts[code].items())[:3]} }")
    except Exception as e:
        print(f"  {info['name']} forecast error: {e}")

# Scan markets
weather_markets = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except: break
    for m in data.get("markets", []):
        if "KXHIGH" in m.get("ticker", ""): weather_markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"): break

print(f"\n  Found {len(weather_markets)} open KXHIGH markets")

# Find best opportunities
opps = []
for m in weather_markets:
    ticker = m.get("ticker", "")
    p = parse_ticker(ticker)
    if not p or p["city"] not in forecasts: continue
    if p["date"] not in forecasts[p["city"]]: continue
    
    fc = forecasts[p["city"]][p["date"]]
    diff = fc - p["threshold"]
    
    if p["direction"] == "T":
        if diff > 6: prob = 0.95
        elif diff > 3: prob = 0.85
        elif diff > 1: prob = 0.65
        elif diff > -1: prob = 0.45
        elif diff > -3: prob = 0.25
        elif diff > -6: prob = 0.10
        else: prob = 0.03
    else:
        dist = abs(fc - (p["threshold"] + 0.5))
        if dist < 1: prob = 0.30
        elif dist < 2: prob = 0.20
        elif dist < 3: prob = 0.12
        elif dist < 5: prob = 0.06
        else: prob = 0.02
    
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        mp = (yes_bid + yes_ask) / 2 / 100
    elif yes_ask and yes_ask < 100:
        mp = yes_ask / 100
    elif m.get("last_price", 0) and m["last_price"] < 100:
        mp = m["last_price"] / 100
    else: continue
    
    edge = prob - mp
    if abs(edge) >= 0.08:
        opps.append({"ticker": ticker, "edge": edge, "prob": prob, "mp": mp, "fc": fc, "parsed": p, "yes_ask": yes_ask, "no_ask": no_ask, "city": CITIES.get(p["city"], {}).get("name", p["city"])})

opps.sort(key=lambda x: abs(x["edge"]), reverse=True)
print(f"  {len(opps)} opportunities with edge >= 8%")

# Place up to 2 trades
placed = 0
for opp in opps[:5]:
    if placed >= 2: break
    
    if opp["edge"] > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
        side, price = "yes", opp["yes_ask"]
    elif opp["edge"] < 0 and opp["no_ask"] and opp["no_ask"] < 99:
        side, price = "no", opp["no_ask"]
    else: continue
    
    count = max(1, min(500 // price, 10))
    print(f"\n  → TRADE: {opp['city']} {opp['ticker']} {side.upper()} {count}x@{price}¢ (forecast={opp['fc']}°F, prob={opp['prob']*100:.0f}%, edge={abs(opp['edge'])*100:.1f}%)")
    
    try:
        body = {"ticker": opp["ticker"], "action": "buy", "side": side, "type": "limit", "count": count}
        if side == "yes": body["yes_price"] = price
        else: body["no_price"] = price
        
        result = api("POST", "/portfolio/orders", body)
        oi = result.get("order", {})
        print(f"    ✓ Order {oi.get('order_id','?')}: {oi.get('status','?')}")
        placed += 1
        
        trades.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": opp["ticker"], "side": side, "price": price,
            "count": count, "edge": round(opp["edge"], 4),
            "forecast_temp": opp["fc"], "threshold": opp["parsed"]["threshold"],
            "reasoning": f"Cycle9: {opp['city']} fc={opp['fc']}°F, {side}@{price}¢, edge={abs(opp['edge'])*100:.1f}%",
            "order_id": oi.get("order_id"), "status": oi.get("status"),
        })
    except requests.exceptions.HTTPError as e:
        print(f"    ✗ {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        print(f"    ✗ {e}")

# Save trades
TRADES_PATH.write_text(json.dumps(trades, indent=2))
print(f"\n💾 Saved {len(trades)} total trades to {TRADES_PATH.name}")
print("\n✅ Cycle #9 complete.")
