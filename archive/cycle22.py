#!/usr/bin/env python3
"""Kalshi Trade Cycle #22 — Feb 17, 2026 ~1pm ET"""

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

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_ticker(ticker):
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m: return None
    city, yr, mon, day = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    month = MONTHS.get(mon)
    if not month: return None
    return {"city": city, "date": f"{2000+yr}-{month:02d}-{day:02d}", "direction": m.group(5), "threshold": float(m.group(6))}

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
        bracket_center = threshold + 0.5
        dist = abs(forecast_temp - bracket_center)
        if dist < 1: return 0.30
        elif dist < 2: return 0.20
        elif dist < 3: return 0.12
        elif dist < 5: return 0.06
        else: return 0.02

report = []
def log(msg):
    print(msg)
    report.append(msg)

now = datetime.datetime.now()
log(f"# Kalshi Trade Cycle #22 — {now.strftime('%b %d, %Y ~%I%p ET')}\n")

# 1. Balance & positions
log("## Balance")
bal = api("GET", "/portfolio/balance")
log(json.dumps(bal, indent=2))
log("")

# 2. Check positions
log("## Positions")
try:
    pos = api("GET", "/portfolio/positions")
    positions = pos.get("market_positions", []) or pos.get("positions", [])
    if positions:
        for p in positions:
            ticker = p.get("ticker", p.get("market_ticker", "?"))
            qty = p.get("position", p.get("total_traded", "?"))
            log(f"- {ticker}: position={qty}")
    else:
        log("No open positions.")
except Exception as e:
    log(f"Positions error: {e}")
log("")

# 3. Check settlements on prior trades
log("## Settlement Check")
prior_tickers = ["KXHIGHMIA-26FEB16-T79", "KXHIGHLAX-26FEB16-T65", "KXHIGHAUS-26FEB17-B86.5",
                 "KXHIGHHOU-26FEB17-T80", "KXHIGHHOU-26FEB17-B77.5"]
for t in prior_tickers:
    try:
        m = api("GET", f"/markets/{t}")
        market = m.get("market", m)
        log(f"- {t}: status={market.get('status')}, result={market.get('result','')}")
    except Exception as e:
        log(f"- {t}: error={e}")
log("")

# 4. Forecasts
log("## Forecasts (Feb 17 & 18)")
forecasts = {}
for code, info in CITIES.items():
    try:
        forecasts[code] = get_forecast(info["lat"], info["lon"])
        for d in ["2026-02-17", "2026-02-18"]:
            if d in forecasts[code]:
                log(f"- {info['name']} {d}: {forecasts[code][d]}°F")
    except Exception as e:
        log(f"- {info['name']}: forecast error {e}")
log("")

# 5. Find and place trades
log("## Market Scan & Trades")
# Get KXHIGH markets
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

log(f"Found {len(weather_markets)} KXHIGH markets open\n")

# Find opportunities
opportunities = []
for m in weather_markets:
    parsed = parse_ticker(m["ticker"])
    if not parsed: continue
    city = parsed["city"]
    if city not in CITIES or city not in forecasts: continue
    if parsed["date"] not in forecasts[city]: continue
    # Only Feb 17 & 18
    if parsed["date"] not in ["2026-02-17", "2026-02-18"]: continue
    
    forecast_temp = forecasts[city][parsed["date"]]
    our_prob = compute_probability(forecast_temp, parsed["threshold"], parsed["direction"])
    
    yes_ask = m.get("yes_ask", 0)
    yes_bid = m.get("yes_bid", 0)
    no_ask = m.get("no_ask", 0)
    
    if yes_bid and yes_ask and yes_ask < 100:
        market_price = (yes_bid + yes_ask) / 2 / 100
    elif yes_ask and yes_ask < 100:
        market_price = yes_ask / 100
    elif m.get("last_price", 0) and m["last_price"] < 100:
        market_price = m["last_price"] / 100
    else:
        continue
    
    edge = our_prob - market_price
    opportunities.append({
        "ticker": m["ticker"], "market": m, "parsed": parsed,
        "forecast": forecast_temp, "our_prob": our_prob,
        "market_price": market_price, "edge": edge,
        "city_name": CITIES[city]["name"],
        "yes_ask": yes_ask, "no_ask": no_ask,
    })

opportunities.sort(key=lambda x: abs(x["edge"]), reverse=True)
log(f"Opportunities with edge >= 8%: {sum(1 for o in opportunities if abs(o['edge']) >= 0.08)}")

# Show top 5
log("\nTop opportunities:")
for o in opportunities[:8]:
    side_str = "YES" if o["edge"] > 0 else "NO"
    log(f"  {o['ticker']}: forecast={o['forecast']}°F, prob={o['our_prob']*100:.0f}%, mkt={o['market_price']*100:.0f}¢, edge={o['edge']*100:+.1f}% → {side_str}")

# Place up to 3 trades
trades_placed = []
def load_trades():
    if TRADES_PATH.exists():
        try: return json.loads(TRADES_PATH.read_text())
        except: return []
    return []

log("\n## Trades Placed")
count_placed = 0
for opp in opportunities:
    if count_placed >= 3: break
    if abs(opp["edge"]) < 0.08: continue
    
    edge = opp["edge"]
    if edge > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
        side, price = "yes", opp["yes_ask"]
    elif edge < 0 and opp["no_ask"] and opp["no_ask"] < 99:
        side, price = "no", opp["no_ask"]
    else:
        continue
    
    qty = max(1, min(500 // price, 10))
    reasoning = f"{opp['city_name']} forecast {opp['forecast']}°F, {opp['ticker']} {side.upper()} @ {price}¢, our_prob={opp['our_prob']*100:.0f}%, edge={abs(edge)*100:.1f}%"
    
    try:
        order_body = {"ticker": opp["ticker"], "action": "buy", "side": side, "type": "limit", "count": qty}
        if side == "yes": order_body["yes_price"] = price
        else: order_body["no_price"] = price
        
        result = api("POST", "/portfolio/orders", order_body)
        oid = result.get("order", {}).get("order_id", "?")
        status = result.get("order", {}).get("status", "?")
        log(f"- {side.upper()} {qty}x {opp['ticker']} @ {price}¢ (edge={edge*100:+.1f}%) → {status} [{oid}]")
        count_placed += 1
        
        trades = load_trades()
        trades.append({"timestamp": now.isoformat(), "cycle": 22, "ticker": opp["ticker"], "side": side, "price": price, "count": qty, "edge": round(edge, 4), "reasoning": reasoning, "order_id": oid, "status": status})
        TRADES_PATH.write_text(json.dumps(trades, indent=2))
        trades_placed.append(reasoning)
    except requests.exceptions.HTTPError as e:
        log(f"- FAILED {opp['ticker']}: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        log(f"- FAILED {opp['ticker']}: {e}")

if count_placed == 0:
    log("No trades placed (no sufficient edge or liquidity).")

# Save report
report_path = PROJECT_DIR / "data" / "cycle-22-report.md"
report_path.write_text("\n".join(report))
print(f"\nReport saved to {report_path}")
