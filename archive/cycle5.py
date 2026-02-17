#!/usr/bin/env python3
"""Kalshi Trade Cycle #5 - Check settlements and place new trades."""

import json, time, base64, datetime, sys, re
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
STRATEGY_PATH = PROJECT_DIR / "data" / "kalshi-strategy-trades.json"
PERF_PATH = PROJECT_DIR / "data" / "kalshi-trade-performance.md"
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
    if method == "GET":
        r = requests.get(url, headers=h, timeout=15)
    else:
        r = requests.post(url, headers=h, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def api_safe(method, path, body=None):
    try:
        return api(method, path, body)
    except Exception as e:
        print(f"  API error {method} {path}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Response: {e.response.text[:300]}")
        return None

# ===== 1. Check settlements =====
print("=" * 60)
print("KALSHI TRADE CYCLE #5 — 2026-02-16 19:58 EST")
print("=" * 60)

print("\n--- SETTLEMENTS ---")
settlements = api_safe("GET", "/portfolio/settlements")
if settlements:
    items = settlements.get("settlements", [])
    print(f"Found {len(items)} settlements")
    total_pnl = 0
    for s in items:
        ticker = s.get("ticker", "?")
        pnl = s.get("revenue", 0) - s.get("cost", 0)  # cents
        total_pnl += pnl
        print(f"  {ticker}: revenue={s.get('revenue',0)}¢ cost={s.get('cost',0)}¢ pnl={pnl}¢")
    print(f"Total settlement P&L: {total_pnl}¢ (${total_pnl/100:.2f})")
else:
    items = []
    total_pnl = 0
    print("No settlements data")

# ===== 2. Balance =====
print("\n--- BALANCE ---")
bal = api_safe("GET", "/portfolio/balance")
if bal:
    balance = bal.get("balance", 0)
    print(f"Balance: ${balance/100:.2f}")
    print(f"Raw: {json.dumps(bal)}")
else:
    balance = 0

# ===== 3. Open positions =====
print("\n--- OPEN POSITIONS ---")
positions = api_safe("GET", "/portfolio/positions?limit=200")
open_pos = []
if positions:
    for p in positions.get("market_positions", positions.get("positions", [])):
        qty = p.get("position", 0) or p.get("total_traded", 0)
        if qty != 0:
            open_pos.append(p)
            print(f"  {p.get('ticker','?')}: pos={qty} side={p.get('side','?')}")
    print(f"Total open positions: {len(open_pos)}")
else:
    # Try alternative endpoint
    positions = api_safe("GET", "/portfolio/positions")
    if positions:
        for p in positions.get("market_positions", positions.get("positions", [])):
            qty = p.get("position", 0)
            if qty != 0:
                open_pos.append(p)
                print(f"  {p.get('ticker','?')}: pos={qty}")
        print(f"Total open positions: {len(open_pos)}")

# ===== 4. Scan for weather markets settling Feb 17 =====
print("\n--- SCANNING MARKETS ---")
weather_markets = []
all_opportunities = []
cursor = None

total_scanned = 0
for page in range(30):
    path = "/markets?status=open&limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    data = api_safe("GET", path)
    if not data:
        break
    batch = data.get("markets", [])
    if not batch:
        break
    total_scanned += len(batch)
    print(f"  Page {page+1}: {len(batch)} markets (total: {total_scanned})", flush=True)
    
    for m in batch:
        ticker = m.get("ticker", "")
        yes_ask = m.get("yes_ask", 0)
        no_ask = m.get("no_ask", 0)
        
        # Weather markets for Feb 17
        if "KXHIGH" in ticker and "FEB17" in ticker:
            weather_markets.append(m)
        
        # Near-settlement opportunities (any market with extreme prices)
        if yes_ask and yes_ask <= 5 and no_ask and no_ask <= 99:
            all_opportunities.append({"market": m, "strategy": "longshot_sell", "score": (5 - yes_ask) * 3})
    
    cursor = data.get("cursor")
    if not cursor:
        break

print(f"Scanned {page+1} pages")
print(f"Weather Feb 17 markets: {len(weather_markets)}")
print(f"Total opportunities: {len(all_opportunities)}")

# Show weather markets
print("\n--- WEATHER FEB 17 MARKETS ---")
for m in weather_markets[:20]:
    print(f"  {m['ticker']}: yes_ask={m.get('yes_ask')} no_ask={m.get('no_ask')} title={m.get('title','')[:60]}")

# ===== 5. Get weather forecasts for Feb 17 =====
print("\n--- WEATHER FORECASTS ---")
cities = {
    "NY": {"lat": 40.71, "lon": -74.01, "name": "New York"},
    "CHI": {"lat": 41.88, "lon": -87.63, "name": "Chicago"},
    "MIA": {"lat": 25.76, "lon": -80.19, "name": "Miami"},
    "LAX": {"lat": 33.94, "lon": -118.41, "name": "Los Angeles"},
    "AUS": {"lat": 30.27, "lon": -97.74, "name": "Austin"},
    "PHIL": {"lat": 39.95, "lon": -75.17, "name": "Philadelphia"},
    "ATL": {"lat": 33.75, "lon": -84.39, "name": "Atlanta"},
    "DEN": {"lat": 39.74, "lon": -104.98, "name": "Denver"},
}

forecasts = {}
for code, info in cities.items():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={info['lat']}&longitude={info['lon']}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=3"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        d = r.json()["daily"]
        fc = dict(zip(d["time"], d["temperature_2m_max"]))
        forecasts[code] = fc
        feb17 = fc.get("2026-02-17", "N/A")
        print(f"  {info['name']}: Feb 17 high = {feb17}°F")
    except Exception as e:
        print(f"  {info['name']}: forecast error: {e}")

# ===== 6. Select and place trades =====
print("\n--- TRADE SELECTION ---")
trades_to_place = []

# Priority 1: Weather Feb 17 markets with forecast edge
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

for m in weather_markets:
    ticker = m["ticker"]
    # Parse: KXHIGH{CITY}-26FEB17-{T|B}{threshold}
    match = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not match:
        continue
    city_code = match.group(1)
    direction = match.group(5)  # T=above, B=bracket
    threshold = float(match.group(6))
    
    if city_code not in forecasts:
        continue
    fc_temp = forecasts[city_code].get("2026-02-17")
    if fc_temp is None:
        continue
    
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    
    if direction == "T":
        # YES = temp > threshold
        diff = fc_temp - threshold
        if diff > 5 and yes_ask and yes_ask <= 50:
            # Forecast well above, YES is cheap -> buy YES
            trades_to_place.append({
                "ticker": ticker, "title": m.get("title",""), "side": "yes",
                "price": yes_ask, "strategy": "weather_forecast",
                "reasoning": f"Forecast {fc_temp}°F >> threshold {threshold}°F, YES@{yes_ask}¢ underpriced"
            })
        elif diff < -5 and no_ask and no_ask <= 50:
            # Forecast well below -> buy NO
            trades_to_place.append({
                "ticker": ticker, "title": m.get("title",""), "side": "no",
                "price": no_ask, "strategy": "weather_forecast",
                "reasoning": f"Forecast {fc_temp}°F << threshold {threshold}°F, NO@{no_ask}¢ underpriced"
            })
        elif diff > 3 and yes_ask and yes_ask <= 97 and yes_ask >= 85:
            # Likely YES, buy at high price for quick settlement
            trades_to_place.append({
                "ticker": ticker, "title": m.get("title",""), "side": "yes",
                "price": yes_ask, "strategy": "weather_tomorrow",
                "reasoning": f"Forecast {fc_temp}°F > {threshold}°F by {diff:.1f}°, YES@{yes_ask}¢ for quick settlement"
            })
        elif diff < -3 and no_ask and no_ask <= 97 and no_ask >= 85:
            trades_to_place.append({
                "ticker": ticker, "title": m.get("title",""), "side": "no",
                "price": no_ask, "strategy": "weather_tomorrow",
                "reasoning": f"Forecast {fc_temp}°F < {threshold}°F by {abs(diff):.1f}°, NO@{no_ask}¢ for quick settlement"
            })

# Priority 2: Near-settlement with extreme prices (non-weather)
existing_tickers = set()
strat_trades = json.loads(STRATEGY_PATH.read_text())
for t in strat_trades:
    existing_tickers.add(t.get("ticker"))

# Sort opportunities by score
all_opportunities.sort(key=lambda x: x["score"], reverse=True)

for opp in all_opportunities:
    if len(trades_to_place) >= 5:
        break
    m = opp["market"]
    ticker = m["ticker"]
    if ticker in existing_tickers:
        continue
    if any(t["ticker"] == ticker for t in trades_to_place):
        continue
    
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    
    if opp["strategy"] == "longshot_sell" and no_ask and no_ask >= 95:
        trades_to_place.append({
            "ticker": ticker, "title": m.get("title",""), "side": "no",
            "price": no_ask, "strategy": "longshot_sell",
            "reasoning": f"Longshot bias: YES@{yes_ask}¢ → sell YES (buy NO@{no_ask}¢)"
        })

# Limit to 5 trades, prefer weather
trades_to_place = trades_to_place[:5]

print(f"\nSelected {len(trades_to_place)} trades:")
for t in trades_to_place:
    print(f"  {t['ticker']}: {t['side']} @ {t['price']}¢ — {t['reasoning'][:80]}")

# ===== Place trades =====
print("\n--- PLACING TRADES ---")
new_trades = []
for t in trades_to_place:
    order_body = {
        "ticker": t["ticker"],
        "action": "buy",
        "side": t["side"],
        "type": "limit",
        "count": min(5, 500 // t["price"]),  # max $5
    }
    if t["side"] == "yes":
        order_body["yes_price"] = t["price"]
    else:
        order_body["no_price"] = t["price"]
    
    result = api_safe("POST", "/portfolio/orders", order_body)
    if result:
        oi = result.get("order", {})
        status = oi.get("status", "unknown")
        oid = oi.get("order_id", "unknown")
        print(f"  ✓ {t['ticker']}: {status} (id={oid})")
        
        trade_record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": t["ticker"],
            "title": t["title"],
            "strategy": t["strategy"],
            "direction": f"BUY {t['side'].upper()}",
            "order_id": oid,
            "status": status,
            "contracts": order_body["count"],
            "reasoning": t["reasoning"],
        }
        if t["side"] == "yes":
            trade_record["yes_price"] = t["price"]
            trade_record["risk_cents"] = t["price"] * order_body["count"]
        else:
            trade_record["no_price"] = t["price"]
            trade_record["yes_price_at_entry"] = 100 - t["price"]
            trade_record["risk_cents"] = t["price"] * order_body["count"]
        new_trades.append(trade_record)
    else:
        print(f"  ✗ {t['ticker']}: FAILED")

# ===== 7. Update files =====
print("\n--- UPDATING FILES ---")

# Update strategy trades JSON
strat_trades.extend(new_trades)
STRATEGY_PATH.write_text(json.dumps(strat_trades, indent=2))
print(f"Updated {STRATEGY_PATH.name} ({len(strat_trades)} total trades)")

# Check balance after trades
bal_after = api_safe("GET", "/portfolio/balance")
balance_after = bal_after.get("balance", 0) if bal_after else balance

# Update performance markdown
perf_entry = f"""

## Trade Session: 2026-02-16 19:58 (Cycle #5)

**Balance Before**: ${balance/100:.2f} | **Balance After**: ${balance_after/100:.2f}

**Settlements Found**: {len(items if settlements else [])} | **Settlement P&L**: ${total_pnl/100:.2f}

**Weather Feb 17 Markets Found**: {len(weather_markets)} | **Open Positions**: {len(open_pos)}

### Settlements Detail
"""

if items:
    perf_entry += "\n| Ticker | Revenue | Cost | P&L |\n|--------|---------|------|-----|\n"
    for s in items:
        t = s.get("ticker","?")
        rev = s.get("revenue",0)
        cost = s.get("cost",0)
        perf_entry += f"| `{t}` | {rev}¢ | {cost}¢ | {rev-cost}¢ |\n"
else:
    perf_entry += "\nNo settlements yet — Feb 16 weather markets may settle overnight.\n"

perf_entry += "\n### New Trades Placed\n\n"
perf_entry += "| # | Ticker | Direction | Price | Qty | Risk | Status | Strategy | Reasoning |\n"
perf_entry += "|---|--------|-----------|-------|-----|------|--------|----------|----------|\n"

for i, t in enumerate(new_trades, 1):
    price = t.get("yes_price", t.get("no_price", "?"))
    perf_entry += f"| {i} | `{t['ticker']}` | {t['direction']} | {price}¢ | {t['contracts']} | ${t['risk_cents']/100:.2f} | {t['status']} | {t['strategy']} | {t['reasoning'][:60]}... |\n"

if not new_trades:
    perf_entry += "| - | No trades placed | - | - | - | - | - | - | - |\n"

perf_entry += f"""
### Cumulative Stats
- **Total trades**: {len(strat_trades)}
- **Total capital at risk**: ${sum(t.get('risk_cents',0) for t in strat_trades)/100:.2f}
- **Settlements collected**: {len(items if settlements else [])}
- **Settlement P&L**: ${total_pnl/100:.2f}
"""

existing_perf = PERF_PATH.read_text()
PERF_PATH.write_text(existing_perf + perf_entry)
print(f"Updated {PERF_PATH.name}")

# ===== Summary =====
print("\n" + "=" * 60)
print("CYCLE #5 SUMMARY")
print("=" * 60)
print(f"Balance: ${balance/100:.2f} → ${balance_after/100:.2f}")
print(f"Settlements: {len(items if settlements else [])} (P&L: ${total_pnl/100:.2f})")
print(f"Open positions: {len(open_pos)}")
print(f"New trades placed: {len(new_trades)}")
for t in new_trades:
    price = t.get("yes_price", t.get("no_price", "?"))
    print(f"  • {t['ticker']}: {t['direction']} @ {price}¢ x{t['contracts']} [{t['status']}]")
print(f"Weather Feb 17 markets available: {len(weather_markets)}")
