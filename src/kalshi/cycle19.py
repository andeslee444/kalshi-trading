#!/usr/bin/env python3
"""Kalshi Trade Cycle #19 — Feb 17 2026 ~10:30am ET"""
import json, time, base64, datetime, re, sys
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
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
    r = (requests.get if method == "GET" else requests.post)(url, headers=h, json=body if method != "GET" else None, timeout=15)
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
print("KALSHI TRADE CYCLE #19 — Feb 17, 2026 ~10:30am ET")
print("=" * 60)

# 1. Balance
bal = api("GET", "/portfolio/balance")
print(f"\n## Balance: ${bal.get('balance',0)/100:.2f} | Portfolio: ${bal.get('portfolio_value',0)/100:.2f}")

# 2. Check all positions — focus on Feb 16 settlements
print("\n## Positions & Feb 16 Settlements")
positions = []
cursor = None
for _ in range(20):
    path = "/portfolio/positions?limit=200"
    if cursor: path += f"&cursor={cursor}"
    data = api("GET", path)
    positions.extend(data.get("market_positions", []))
    cursor = data.get("cursor")
    if not cursor: break

feb16_positions = []
feb17_positions = []
other_positions = []
for p in positions:
    ticker = p.get("ticker", "")
    parsed = parse_ticker(ticker)
    if parsed and parsed["date"] == "2026-02-16":
        feb16_positions.append(p)
    elif parsed and parsed["date"] == "2026-02-17":
        feb17_positions.append(p)
    elif p.get("position", 0) != 0:
        other_positions.append(p)

print(f"\nFeb 16 positions ({len(feb16_positions)}):")
settled_count = 0
total_pnl = 0
for p in feb16_positions:
    pos = p.get("position", 0)
    realized = p.get("realized_pnl", 0)
    result = p.get("settlement_result", p.get("result", ""))
    status = p.get("market_status", p.get("status", ""))
    total_pnl += realized
    if result or status == "settled":
        settled_count += 1
    print(f"  {p['ticker']}: pos={pos}, realized_pnl=${realized/100:.2f}, status={status}, result={result}")

print(f"\nFeb 16 settled: {settled_count}/{len(feb16_positions)}, total realized P&L: ${total_pnl/100:.2f}")

print(f"\nFeb 17 positions ({len(feb17_positions)}):")
for p in feb17_positions:
    pos = p.get("position", 0)
    if pos == 0: continue
    print(f"  {p['ticker']}: pos={pos}, exposure=${abs(p.get('market_exposure',0))/100:.2f}")

# 3. Get forecasts and scan Feb 17+ markets for trades
print("\n## Market Scan for New Trades")
forecasts = {}
for code, info in CITIES.items():
    try:
        forecasts[code] = get_forecast(info["lat"], info["lon"])
        print(f"  {info['name']}: {forecasts[code].get('2026-02-17', '?')}°F (Feb 17), {forecasts[code].get('2026-02-18', '?')}°F (Feb 18)")
    except Exception as e:
        print(f"  {info['name']}: forecast error: {e}")

# Get open markets
weather_markets = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor: path += f"&cursor={cursor}"
    data = api("GET", path)
    for m in data.get("markets", []):
        if "KXHIGH" in m.get("ticker", ""):
            weather_markets.append(m)
    cursor = data.get("cursor")
    if not cursor or not data.get("markets"): break

print(f"\nOpen KXHIGH markets: {len(weather_markets)}")

# Existing ticker set
existing_tickers = {p["ticker"] for p in feb17_positions if p.get("position", 0) != 0}
print(f"Already have positions in: {len(existing_tickers)} tickers")

# Find opportunities
opps = []
for m in weather_markets:
    ticker = m.get("ticker", "")
    parsed = parse_ticker(ticker)
    if not parsed or parsed["city"] not in forecasts: continue
    if parsed["date"] not in forecasts[parsed["city"]]: continue
    if ticker in existing_tickers: continue  # skip existing

    forecast_temp = forecasts[parsed["city"]][parsed["date"]]
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
    if abs(edge) >= 0.08:
        opps.append({"ticker": ticker, "market": m, "parsed": parsed, "forecast": forecast_temp,
                      "our_prob": our_prob, "market_price": market_price, "edge": edge,
                      "city": CITIES[parsed["city"]]["name"], "yes_ask": yes_ask, "no_ask": no_ask})

opps.sort(key=lambda x: abs(x["edge"]), reverse=True)
print(f"Opportunities with edge >= 8%: {len(opps)}")

# Place up to 3 trades
trades_placed = []
for opp in opps[:10]:  # consider top 10, place up to 3
    if len(trades_placed) >= 3: break
    
    edge = opp["edge"]
    if edge > 0 and opp["yes_ask"] and opp["yes_ask"] < 99:
        side, price = "yes", opp["yes_ask"]
    elif edge < 0 and opp["no_ask"] and opp["no_ask"] < 99:
        side, price = "no", opp["no_ask"]
    else:
        continue

    count = max(1, min(500 // price, 10))
    reasoning = f"{opp['city']} {opp['parsed']['date']}: forecast {opp['forecast']}°F, {opp['ticker']} {side.upper()} @ {price}¢, our_prob={opp['our_prob']*100:.0f}%, edge={abs(edge)*100:.1f}%"
    print(f"\n→ TRADE: {reasoning}")
    
    try:
        order_body = {"ticker": opp["ticker"], "action": "buy", "side": side, "type": "limit", "count": count}
        if side == "yes": order_body["yes_price"] = price
        else: order_body["no_price"] = price
        
        result = api("POST", "/portfolio/orders", order_body)
        oid = result.get("order", {}).get("order_id", "?")
        status = result.get("order", {}).get("status", "?")
        print(f"  ✓ Order {oid}: {status}")
        trades_placed.append({"ticker": opp["ticker"], "side": side, "price": price, "count": count,
                              "reasoning": reasoning, "edge": round(edge, 4), "order_id": oid, "status": status,
                              "forecast": opp["forecast"], "threshold": opp["parsed"]["threshold"]})
    except requests.exceptions.HTTPError as e:
        print(f"  ✗ Failed: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"  ✗ Failed: {e}")

if not trades_placed:
    print("\nNo trades placed this cycle.")

# Save trades
if trades_placed:
    trades_path = PROJECT_DIR / "data" / "kalshi-trades.json"
    existing = json.loads(trades_path.read_text()) if trades_path.exists() else []
    for t in trades_placed:
        t["timestamp"] = datetime.datetime.now().isoformat()
        t["cycle"] = 19
    existing.extend(trades_placed)
    trades_path.write_text(json.dumps(existing, indent=2))

# Generate report
report = f"""# Kalshi Trade Cycle #19 — Feb 17, 2026 ~10:30am ET

## Balance
- **Balance:** ${bal.get('balance',0)/100:.2f}
- **Portfolio Value:** ${bal.get('portfolio_value',0)/100:.2f}

## Feb 16 Settlements
- **Settled:** {settled_count}/{len(feb16_positions)}
- **Total Realized P&L:** ${total_pnl/100:.2f}
"""
for p in feb16_positions:
    report += f"- {p['ticker']}: pos={p.get('position',0)}, pnl=${p.get('realized_pnl',0)/100:.2f}, status={p.get('market_status','')}\n"

report += f"\n## Forecasts (Feb 17)\n"
for code, info in CITIES.items():
    if code in forecasts:
        report += f"- {info['name']}: {forecasts[code].get('2026-02-17', '?')}°F\n"

report += f"\n## Existing Feb 17 Positions ({len(existing_tickers)})\n"
for p in feb17_positions:
    if p.get("position", 0) != 0:
        report += f"- {p['ticker']}: {p.get('position',0)} contracts\n"

report += f"\n## New Trades ({len(trades_placed)})\n"
if trades_placed:
    for t in trades_placed:
        report += f"- {t['reasoning']}\n"
else:
    report += "No new trades placed.\n"

report += f"\n## Market Opportunities Scanned\n- Open KXHIGH markets: {len(weather_markets)}\n- Opportunities >= 8% edge: {len(opps)}\n"
if opps[:5]:
    report += "Top opportunities:\n"
    for o in opps[:5]:
        report += f"  - {o['ticker']}: edge={o['edge']*100:.1f}%, forecast={o['forecast']}°F, market={o['market_price']*100:.0f}¢\n"

(PROJECT_DIR / "data" / "cycle-19-report.md").write_text(report)
print(f"\nReport saved to data/cycle-19-report.md")
print("Done!")
