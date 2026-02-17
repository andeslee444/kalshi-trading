#!/usr/bin/env python3
"""Cycle 20 — check settlements, balance, positions, trade.

ARCHIVED: This is a historical iteration kept for reference.
Refactored to use shared KalshiClient instead of hardcoded credentials.
"""
import json, datetime, re, sys, requests
from pathlib import Path

# Use shared auth module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
from kalshi_auth import KalshiClient, PROJECT_DIR

client = KalshiClient()

def api(method, path, body=None):
    if method == "GET":
        return client.get(path)
    return client.post(path, body=body)

print("=" * 60)
print("CYCLE 20 — Feb 17 2026 ~noon ET")
print("=" * 60)

# 1. Balance
print("\n--- BALANCE ---")
bal = api("GET", "/portfolio/balance")
print(json.dumps(bal, indent=2))

# 2. Check specific Feb 16 markets for settlement/result
print("\n--- SETTLEMENT CHECK: Individual Feb 16 markets ---")
feb16_tickers = [
    "KXHIGHLAX-26FEB16-B58.5", "KXHIGHLAX-26FEB16-T65",
    "KXHIGHMIA-26FEB16-T79", "KXHIGHMIA-26FEB16-B79.5",
    "KXHIGHMIA-26FEB16-B83.5", "KXHIGHMIA-26FEB16-T86",
    "KXHIGHPHIL-26FEB16-T45", "KXHIGHNY-26FEB16-B43.5",
]

settled_count = 0
for ticker in feb16_tickers:
    try:
        data = api("GET", f"/markets/{ticker}")
        m = data.get("market", data)
        status = m.get("status", "?")
        result = m.get("result", "")
        close_time = m.get("close_time", "")
        settle_time = m.get("settlement_timer_seconds", "")
        print(f"  {ticker}: status={status}, result='{result}', close={close_time}")
        if result:
            settled_count += 1
    except Exception as e:
        print(f"  {ticker}: ERROR {e}")

print(f"\n  Settled: {settled_count}/{len(feb16_tickers)}")

# 3. Also check via /markets?status=settled
print("\n--- Checking /markets?status=settled for KXHIGH ---")
try:
    data = api("GET", "/markets?status=settled&limit=200")
    settled_markets = [m for m in data.get("markets", []) if "KXHIGH" in m.get("ticker", "")]
    print(f"  Found {len(settled_markets)} settled KXHIGH markets")
    for m in settled_markets[:10]:
        print(f"    {m['ticker']}: result={m.get('result','')}")
except Exception as e:
    print(f"  Error: {e}")

# Also try finalized/closed
for st in ["finalized", "closed"]:
    print(f"\n--- Checking /markets?status={st} for KXHIGH ---")
    try:
        data = api("GET", f"/markets?status={st}&limit=200")
        found = [m for m in data.get("markets", []) if "KXHIGH" in m.get("ticker", "")]
        print(f"  Found {len(found)} {st} KXHIGH markets")
        for m in found[:5]:
            print(f"    {m['ticker']}: result={m.get('result','')}, status={m.get('status','')}")
    except Exception as e:
        print(f"  Error: {e}")

# 4. Current positions
print("\n--- POSITIONS ---")
try:
    data = api("GET", "/portfolio/positions?limit=200")
    positions = data.get("market_positions", [])
    feb16_pos = [p for p in positions if "FEB16" in p.get("ticker", "")]
    feb17_pos = [p for p in positions if "FEB17" in p.get("ticker", "")]
    other_pos = [p for p in positions if "FEB16" not in p.get("ticker", "") and "FEB17" not in p.get("ticker", "")]
    
    print(f"  Total positions: {len(positions)}")
    print(f"  Feb 16: {len(feb16_pos)}, Feb 17: {len(feb17_pos)}, Other: {len(other_pos)}")
    
    if feb16_pos:
        print("\n  Feb 16 (should be settled):")
        for p in feb16_pos:
            print(f"    {p['ticker']}: pos={p.get('position',0)}, pnl={p.get('realized_pnl',0)}")
    
    print("\n  Feb 17 positions:")
    for p in feb17_pos:
        print(f"    {p['ticker']}: pos={p.get('position',0)}, market_exposure={p.get('market_exposure',0)}")
except Exception as e:
    print(f"  Error: {e}")

# 5. Get forecasts for Feb 17
print("\n--- FORECASTS (Feb 17) ---")
cities = {"Miami": (25.76,-80.19), "Los Angeles": (34.05,-118.24), "Philadelphia": (39.95,-75.17),
          "New York": (40.71,-74.01), "Chicago": (41.88,-87.63), "Austin": (30.27,-97.74),
          "Denver": (39.74,-104.98), "Houston": (29.76,-95.37)}
forecasts = {}
for city, (lat, lon) in cities.items():
    try:
        r = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America/New_York&forecast_days=3", timeout=10)
        data = r.json()
        dates = data["daily"]["time"]
        temps = data["daily"]["temperature_2m_max"]
        for i, d in enumerate(dates):
            if d == "2026-02-17":
                forecasts[city] = temps[i]
                print(f"  {city}: {temps[i]:.1f}°F")
                break
    except Exception as e:
        print(f"  {city}: error {e}")

# 6. Scan open Feb 17 markets for trading opportunities
print("\n--- SCANNING Feb 17 MARKETS ---")
try:
    data = api("GET", "/markets?status=open&limit=1000")
    feb17_markets = [m for m in data.get("markets", []) if "KXHIGH" in m.get("ticker", "") and "FEB17" in m.get("ticker", "")]
    print(f"  Found {len(feb17_markets)} open KXHIGH Feb 17 markets")
    
    city_codes = {"MIA": "Miami", "LAX": "Los Angeles", "PHIL": "Philadelphia", 
                  "NY": "New York", "CHI": "Chicago", "AUS": "Austin", "DEN": "Denver", "HOU": "Houston"}
    
    opportunities = []
    for m in feb17_markets:
        ticker = m["ticker"]
        yes_bid = m.get("yes_bid", 0) or 0
        yes_ask = m.get("yes_ask", 0) or 0
        no_bid = m.get("no_bid", 0) or 0
        no_ask = m.get("no_ask", 0) or 0
        
        # Parse city and threshold
        city_name = None
        for code, name in city_codes.items():
            if code in ticker:
                city_name = name
                break
        if not city_name or city_name not in forecasts:
            continue
        
        forecast = forecasts[city_name]
        
        # Parse threshold from ticker
        match = re.search(r'[BT](\d+\.?\d*)', ticker.split("FEB17-")[1]) if "FEB17-" in ticker else None
        if not match:
            continue
        threshold = float(match.group(1))
        is_above = ticker.split("FEB17-")[1].startswith("T")  # T = temp above threshold
        is_below = ticker.split("FEB17-")[1].startswith("B")  # B = temp below threshold
        
        # Estimate probability
        diff = forecast - threshold
        if is_above:
            # YES wins if temp >= threshold
            our_prob = max(0.02, min(0.98, 0.5 + diff * 0.08))
        else:
            # B = below: YES wins if temp < threshold
            our_prob = max(0.02, min(0.98, 0.5 - diff * 0.08))
        
        # Check for edge
        if yes_ask and yes_ask > 0:
            edge_yes = our_prob - yes_ask / 100
            if edge_yes > 0.15:
                opportunities.append(("yes", ticker, yes_ask, our_prob, edge_yes, forecast, threshold))
        if no_ask and no_ask > 0:
            edge_no = (1 - our_prob) - no_ask / 100
            if edge_no > 0.15:
                opportunities.append(("no", ticker, no_ask, 1-our_prob, edge_no, forecast, threshold))
    
    opportunities.sort(key=lambda x: -x[4])
    print(f"\n  Opportunities with >15% edge: {len(opportunities)}")
    for side, ticker, price, prob, edge, fc, thresh in opportunities[:10]:
        print(f"    {side.upper()} {ticker} @ {price}¢, prob={prob:.0%}, edge={edge:+.0%}, forecast={fc:.1f}°F vs {thresh}°F")
    
    # Place up to 2 trades
    trades_placed = []
    existing_tickers = set()
    try:
        pos_data = api("GET", "/portfolio/positions?limit=200")
        existing_tickers = {p["ticker"] for p in pos_data.get("market_positions", [])}
    except:
        pass
    
    for side, ticker, price, prob, edge, fc, thresh in opportunities[:5]:
        if len(trades_placed) >= 2:
            break
        if ticker in existing_tickers:
            continue
        
        count = min(10, max(3, int(edge * 30)))
        try:
            order = api("POST", "/portfolio/orders", {
                "ticker": ticker, "action": "buy", "side": side,
                "type": "limit", "count": count, "yes_price": price if side == "yes" else None,
                "no_price": price if side == "no" else None,
            })
            oid = order.get("order", {}).get("order_id", "?")
            print(f"\n  TRADE: {side.upper()} {count}x {ticker} @ {price}¢ → order {oid}")
            trades_placed.append({"ticker": ticker, "side": side, "price": price, "count": count, 
                                  "order_id": oid, "edge": edge, "forecast": fc, "threshold": thresh,
                                  "timestamp": datetime.datetime.now().isoformat()})
        except Exception as e:
            print(f"\n  Trade failed {ticker}: {e}")
    
    if not trades_placed:
        print("\n  No new trades placed (existing positions or no clear opportunities)")

except Exception as e:
    print(f"  Error: {e}")

# 7. Save report
print("\n--- SAVING REPORT ---")
report = f"""# Kalshi Trade Cycle #20 — Feb 17, 2026 ~noon ET

## Balance
{json.dumps(bal, indent=2)}

## Settlement Check (Feb 16 Markets)
- Checked {len(feb16_tickers)} individual Feb 16 markets via GET /markets/{{ticker}}
- Settled: {settled_count}/{len(feb16_tickers)}
- **Conclusion:** {"Demo API does settle markets" if settled_count > 0 else "Demo API does NOT appear to settle markets. After 20 cycles, 0 settlements observed. P&L must be tracked manually using actual weather data."}

## Forecasts (Feb 17)
"""
for city, temp in sorted(forecasts.items()):
    report += f"- {city}: {temp:.1f}°F\n"

report += f"\n## Trades Placed\n"
if trades_placed:
    for t in trades_placed:
        report += f"- {t['side'].upper()} {t['count']}x {t['ticker']} @ {t['price']}¢ (edge={t['edge']:+.0%})\n"
else:
    report += "- None this cycle\n"

Path(PROJECT_DIR / "data" / "cycle-20-report.md").write_text(report)

# Append trades to log
if trades_placed:
    log_path = PROJECT_DIR / "data" / "kalshi-trades.json"
    existing = json.loads(log_path.read_text()) if log_path.exists() else []
    existing.extend(trades_placed)
    log_path.write_text(json.dumps(existing, indent=2))

print("Done. Report saved to data/cycle-20-report.md")
