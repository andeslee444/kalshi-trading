#!/usr/bin/env python3
"""Trade Cycle #7 - Check settlements, balance, positions, place new trades."""
import json, time, base64, datetime, os, sys, re
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
    h = get_headers(method, "/trade-api/v2" + path)
    if method == "GET":
        r = requests.get(url, headers=h, timeout=15)
    else:
        r = requests.post(url, headers=h, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def api_safe(method, path, body=None):
    try:
        return api(method, path, body)
    except requests.exceptions.HTTPError as e:
        return {"error": f"{e.response.status_code}: {e.response.text[:300]}"}
    except Exception as e:
        return {"error": str(e)}

# ── 1. Check settlements ──
print("=" * 60)
print("TRADE CYCLE #7 — Settlement Check & New Trades")
print(f"Time: {datetime.datetime.now().isoformat()}")
print("=" * 60)

print("\n── SETTLEMENTS ──")
settlements = api_safe("GET", "/portfolio/settlements")
print(json.dumps(settlements, indent=2)[:2000])

# ── 2. Check our Feb 16 weather market statuses ──
print("\n── MARKET STATUS FOR OUR POSITIONS ──")
our_tickers = ["KXHIGHMIA-26FEB16-T79", "KXHIGHLAX-26FEB16-T65", "KXHIGHLAX-26FEB16-B58.5"]
for ticker in our_tickers:
    data = api_safe("GET", f"/markets/{ticker}")
    m = data.get("market", data)
    status = m.get("status", "?")
    result = m.get("result", "?")
    close_time = m.get("close_time", "?")
    settle = m.get("settlement_value", "?")
    print(f"  {ticker}: status={status}, result={result}, close={close_time}, settlement_value={settle}")

# ── 3. Balance ──
print("\n── BALANCE ──")
bal = api_safe("GET", "/portfolio/balance")
print(f"  {json.dumps(bal)}")

# ── 4. Positions ──
print("\n── POSITIONS ──")
positions = api_safe("GET", "/portfolio/positions")
pos_list = positions.get("market_positions", positions.get("positions", []))
if isinstance(pos_list, list):
    if not pos_list:
        print("  No open positions")
    for p in pos_list:
        print(f"  {p.get('ticker','?')}: {p.get('market_exposure',0)/100:.2f}$ exposure, yes={p.get('total_traded',0)}")
else:
    print(f"  {json.dumps(positions)[:500]}")

# ── 5. Find and place new trades ──
print("\n── SCANNING FOR NEW TRADES ──")

# Get forecasts
def get_forecast(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

forecasts = {}
for code, info in CITIES.items():
    try:
        forecasts[code] = get_forecast(info["lat"], info["lon"])
        dates = list(forecasts[code].items())[:3]
        print(f"  {info['name']}: {', '.join(f'{d}={t}°F' for d,t in dates)}")
    except Exception as e:
        print(f"  {info['name']}: forecast error: {e}")

# Get open weather markets
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m: return None
    city, yr, mon, day = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    month = MONTHS.get(mon)
    if not month: return None
    return {"city": city, "date": f"{2000+yr}-{month:02d}-{day:02d}", "direction": m.group(5), "threshold": float(m.group(6))}

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
        bracket_center = threshold + 0.5
        dist = abs(forecast_temp - bracket_center)
        if dist < 1: return 0.30
        elif dist < 2: return 0.20
        elif dist < 3: return 0.12
        elif dist < 5: return 0.06
        else: return 0.02

# Fetch open markets
weather_markets = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except: break
    for m in data.get("markets", []):
        if "KXHIGH" in m.get("ticker", ""):
            weather_markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"): break

print(f"  Found {len(weather_markets)} open KXHIGH markets")

# Find opportunities
opportunities = []
for m in weather_markets:
    ticker = m["ticker"]
    parsed = parse_ticker(ticker)
    if not parsed or parsed["city"] not in CITIES or parsed["city"] not in forecasts:
        continue
    fc = forecasts[parsed["city"]].get(parsed["date"])
    if fc is None: continue
    
    prob = compute_probability(fc, parsed["threshold"], parsed["direction"])
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        mp = (yes_bid + yes_ask) / 200
    elif yes_ask and yes_ask < 100:
        mp = yes_ask / 100
    elif m.get("last_price", 0) and m["last_price"] < 100:
        mp = m["last_price"] / 100
    else:
        continue
    
    edge = prob - mp
    if abs(edge) >= 0.08:
        opportunities.append({"ticker": ticker, "m": m, "parsed": parsed, "fc": fc, "prob": prob, "mp": mp, "edge": edge, "yes_ask": yes_ask, "no_ask": no_ask})

opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)
print(f"  {len(opportunities)} opportunities with edge >= 8%")

# Place up to 5 trades
trades_placed = 0
new_trades = []
for opp in opportunities[:5]:
    t = opp["ticker"]
    edge = opp["edge"]
    city_name = CITIES[opp["parsed"]["city"]]["name"]
    
    if edge > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
        side, price = "yes", opp["yes_ask"]
    elif edge < 0 and opp["no_ask"] and opp["no_ask"] < 99:
        side, price = "no", opp["no_ask"]
    else:
        continue
    
    count = max(1, min(500 // price, 10))
    
    print(f"\n  → {t}: {side.upper()} {count}x@{price}¢ | {city_name} fc={opp['fc']}°F prob={opp['prob']*100:.0f}% mkt={opp['mp']*100:.0f}% edge={abs(edge)*100:.1f}%")
    
    order = {"ticker": t, "action": "buy", "side": side, "type": "limit", "count": count}
    if side == "yes": order["yes_price"] = price
    else: order["no_price"] = price
    
    result = api_safe("POST", "/portfolio/orders", order)
    oi = result.get("order", result)
    status = oi.get("status", oi.get("error", "?"))
    oid = oi.get("order_id", "?")
    print(f"    Result: {status} (id={oid})")
    
    new_trades.append({
        "timestamp": datetime.datetime.now().isoformat(),
        "cycle": 7, "ticker": t, "side": side, "price": price,
        "count": count, "edge": round(edge, 4), "forecast_temp": opp["fc"],
        "threshold": opp["parsed"]["threshold"],
        "order_id": oid, "status": status,
        "reasoning": f"{city_name} fc={opp['fc']}°F vs threshold={opp['parsed']['threshold']}°F"
    })
    trades_placed += 1

print(f"\n  Placed {trades_placed} new trades")

# ── 6. Update data files ──
if new_trades:
    existing = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
    existing.extend(new_trades)
    TRADES_PATH.write_text(json.dumps(existing, indent=2))
    print(f"  Updated {TRADES_PATH} ({len(existing)} total trades)")

print("\n── DONE ──")
