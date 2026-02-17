#!/usr/bin/env python3
"""Kalshi Trade Cycle #21 - Feb 17, 2026 ~11am ET"""

import json, time, base64, datetime, os, sys, re
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"
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
    r = requests.request(method, url, headers=h, json=body if method=="POST" else None, timeout=15)
    r.raise_for_status()
    return r.json()

# === 1. Balance & Positions ===
print("=" * 60)
print("KALSHI TRADE CYCLE #21 — Feb 17, 2026 ~11am ET")
print("=" * 60)

bal = api("GET", "/portfolio/balance")
print(f"\n## Balance\n{json.dumps(bal, indent=2)}")

# Positions
try:
    pos = api("GET", "/portfolio/positions?limit=100")
    positions = [p for p in pos.get("market_positions", []) if p.get("total_traded", 0) > 0]
    print(f"\n## Positions: {len(positions)} active")
    for p in positions[:10]:
        print(f"  {p.get('ticker','?')}: {p.get('position',0)} contracts, cost={p.get('market_exposure',0)}")
except Exception as e:
    print(f"Positions error: {e}")
    positions = []

# === 2. Settlement check on recent trades ===
print("\n## Settlement Check")
# Check a few recent Feb 16 tickers
check_tickers = ["KXHIGHMIA-26FEB16-T79", "KXHIGHLAX-26FEB16-T65", "KXHIGHAUS-26FEB17-B86.5"]
for t in check_tickers:
    try:
        m = api("GET", f"/markets/{t}")
        market = m.get("market", m)
        status = market.get("status", "?")
        result = market.get("result", "?")
        print(f"  {t}: status={status}, result={result}")
    except Exception as e:
        print(f"  {t}: error - {e}")

# === 3. Forecasts ===
print("\n## Forecasts")
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def get_forecast(lat, lon):
    r = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7", timeout=10)
    r.raise_for_status()
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

forecasts = {}
for code, info in CITIES.items():
    try:
        forecasts[code] = get_forecast(info["lat"], info["lon"])
        # Show Feb 17 and 18
        for dt in ["2026-02-17", "2026-02-18"]:
            if dt in forecasts[code]:
                print(f"  {info['name']} {dt}: {forecasts[code][dt]}°F")
    except Exception as e:
        print(f"  {info['name']}: error - {e}")

# === 4. Scan markets ===
print("\n## Market Scan")
weather_markets = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except: break
    for m in data.get("markets", []):
        tk = m.get("ticker", "")
        if "KXHIGH" in tk and ("FEB17" in tk or "FEB18" in tk):
            weather_markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"): break

print(f"Found {len(weather_markets)} KXHIGH Feb 17/18 markets")

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

# Find opportunities
opps = []
for m in weather_markets:
    ticker = m.get("ticker", "")
    parsed = parse_ticker(ticker)
    if not parsed or parsed["city"] not in CITIES or parsed["city"] not in forecasts:
        continue
    fc = forecasts[parsed["city"]]
    if parsed["date"] not in fc:
        continue
    temp = fc[parsed["date"]]
    prob = compute_probability(temp, parsed["threshold"], parsed["direction"])
    
    yes_ask = m.get("yes_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    no_ask = m.get("no_ask", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        mp = (yes_bid + yes_ask) / 2 / 100
    elif yes_ask and yes_ask < 100:
        mp = yes_ask / 100
    else:
        continue
    
    edge = prob - mp
    if abs(edge) >= config["edgeThreshold"]:
        opps.append({"ticker": ticker, "market": m, "parsed": parsed, "forecast": temp, "prob": prob, "mp": mp, "edge": edge, "yes_ask": yes_ask, "no_ask": no_ask, "city": CITIES[parsed["city"]]["name"]})

opps.sort(key=lambda x: abs(x["edge"]), reverse=True)
print(f"Opportunities with edge >= {config['edgeThreshold']*100:.0f}%: {len(opps)}")
for o in opps[:10]:
    side = "YES" if o["edge"] > 0 else "NO"
    print(f"  {o['ticker']}: {o['city']} forecast={o['forecast']}°F, prob={o['prob']*100:.0f}%, market={o['mp']*100:.0f}%, edge={abs(o['edge'])*100:.1f}% → {side}")

# === 5. Place up to 3 trades ===
print("\n## Trades")
trades_placed = []
now = datetime.datetime.now()

for opp in opps[:3]:
    ticker = opp["ticker"]
    edge = opp["edge"]
    
    if edge > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
        side, price = "yes", opp["yes_ask"]
    elif edge < 0 and opp["no_ask"] and opp["no_ask"] < 99:
        side, price = "no", opp["no_ask"]
    else:
        continue
    
    max_cost = config["maxTradeAmount"] * 100
    count = max(1, min(max_cost // price, 10))
    reasoning = f"{opp['city']} forecast: {opp['forecast']}°F, {ticker} {side.upper()} at {price}¢ → our prob {opp['prob']*100 if side=='yes' else (1-opp['prob'])*100:.0f}%, edge +{abs(edge)*100:.1f}%"
    
    print(f"  → {side.upper()} {count}x {ticker} @ {price}¢ | {reasoning}")
    
    try:
        body = {"ticker": ticker, "action": "buy", "side": side, "type": "limit", "count": count}
        if side == "yes": body["yes_price"] = price
        else: body["no_price"] = price
        
        result = api("POST", "/portfolio/orders", body)
        oi = result.get("order", {})
        print(f"    ✓ Order {oi.get('order_id','?')[:8]}... status={oi.get('status','?')}")
        
        trade = {"timestamp": now.isoformat(), "ticker": ticker, "side": side, "price": price, "count": count,
                 "reasoning": reasoning, "forecast_temp": opp["forecast"], "threshold": opp["parsed"]["threshold"],
                 "edge": round(edge, 4), "order_id": oi.get("order_id"), "status": oi.get("status"), "cycle": 21}
        trades_placed.append(trade)
    except requests.exceptions.HTTPError as e:
        print(f"    ✗ Failed: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"    ✗ Failed: {e}")

# Save trades
if trades_placed:
    existing = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
    existing.extend(trades_placed)
    TRADES_PATH.write_text(json.dumps(existing, indent=2))
    print(f"\nSaved {len(trades_placed)} trades to data file")

# === 6. Write cycle report ===
report = f"""# Kalshi Trade Cycle #21 — Feb 17, 2026 ~11am ET

## Balance
{json.dumps(bal, indent=2)}

## Settlement Check
"""
for t in check_tickers:
    try:
        m = api("GET", f"/markets/{t}")
        market = m.get("market", m)
        report += f"- {t}: status={market.get('status','?')}, result={market.get('result','?')}\n"
    except:
        report += f"- {t}: check failed\n"

report += f"\nNote: Andes confirms settlements can take 1-2 days. Keep checking but don't worry.\n"

report += "\n## Forecasts (Feb 17 & 18)\n"
for code, info in CITIES.items():
    if code in forecasts:
        for dt in ["2026-02-17", "2026-02-18"]:
            if dt in forecasts[code]:
                report += f"- {info['name']} {dt}: {forecasts[code][dt]}°F\n"

report += f"\n## Trades Placed ({len(trades_placed)})\n"
for t in trades_placed:
    report += f"- {t['side'].upper()} {t['count']}x {t['ticker']} @ {t['price']}¢ (edge={t['edge']*100:+.1f}%)\n"

if not trades_placed:
    report += "- No trades placed this cycle\n"

(PROJECT_DIR / "data" / "cycle-21-report.md").write_text(report)
print(f"\nReport saved to data/cycle-21-report.md")
print("\nDone!")
