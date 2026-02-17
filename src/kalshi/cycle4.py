#!/usr/bin/env python3
"""Kalshi Trade Cycle #4 - Check settlements, place new trades."""

import json, time, base64, datetime, sys, re
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-strategy-trades.json"
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
    r = requests.request(method, url, headers=h, json=body if method != "GET" else None, timeout=15)
    r.raise_for_status()
    return r.json()

# ─── 1. Check settlements ───
print("=" * 60)
print("KALSHI TRADE CYCLE #4 — 2026-02-16 18:10 EST")
print("=" * 60)

print("\n📋 CHECKING SETTLEMENTS...")
try:
    settlements = api("GET", "/portfolio/settlements")
    s_list = settlements.get("settlements", [])
    print(f"  Found {len(s_list)} settlements")
    settlement_pnl = 0
    for s in s_list:
        ticker = s.get("ticker", "")
        revenue = s.get("revenue", 0)
        settlement_pnl += revenue
        yes_price = s.get("yes_price", "?")
        no_price = s.get("no_price", "?")
        print(f"  • {ticker}: revenue={revenue}¢, result={s.get('result','?')}")
    print(f"  Total settlement revenue: {settlement_pnl}¢ (${settlement_pnl/100:.2f})")
except Exception as e:
    print(f"  Settlement check error: {e}")
    s_list = []
    settlement_pnl = 0

# ─── 2. Balance & positions ───
print("\n💰 BALANCE & POSITIONS...")
bal = api("GET", "/portfolio/balance")
balance = bal.get("balance", 0)
print(f"  Balance: ${balance/100:.2f}")

# Check positions
print("\n📊 OPEN POSITIONS...")
try:
    positions = api("GET", "/portfolio/positions?limit=100")
    pos_list = positions.get("market_positions", positions.get("positions", []))
    open_pos = [p for p in pos_list if p.get("position", 0) != 0 or p.get("total_traded", 0) > 0]
    print(f"  Total positions: {len(pos_list)}, with activity: {len(open_pos)}")
    for p in open_pos[:20]:
        ticker = p.get("ticker", "?")
        pos = p.get("position", 0)
        cost = p.get("market_exposure", p.get("total_traded", 0))
        print(f"  • {ticker}: pos={pos}, exposure={cost}¢")
except Exception as e:
    print(f"  Positions error: {e}")
    open_pos = []

# ─── 3. Scan for new opportunities ───
print("\n🔍 SCANNING MARKETS...")

# Scan weather markets for tomorrow (Feb 17)
weather_tomorrow = []
longshot_candidates = []
near_settlement = []
cursor = None

for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except Exception as e:
        print(f"  Page {page} error: {e}")
        break
    batch = data.get("markets", [])
    if not batch:
        break
    
    for m in batch:
        ticker = m.get("ticker", "")
        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)
        close_time = m.get("close_time", "")
        
        # Weather markets for Feb 17
        if "KXHIGH" in ticker and "FEB17" in ticker:
            weather_tomorrow.append(m)
        
        # Near-settlement: closing tonight or tomorrow
        if close_time and close_time[:10] in ["2026-02-16", "2026-02-17"]:
            if yes_ask and yes_ask <= 8:  # likely NO
                near_settlement.append({"market": m, "lean": "NO", "confidence": 100 - yes_ask})
            elif yes_bid and yes_bid >= 92:  # likely YES
                near_settlement.append({"market": m, "lean": "YES", "confidence": yes_bid})
        
        # Longshot bias: YES <= 5¢ with NO available
        if yes_ask and yes_ask <= 5 and no_ask and no_ask <= 99:
            longshot_candidates.append(m)
    
    cursor = data.get("cursor")
    if not cursor:
        break

print(f"  Pages scanned: {page+1}")
print(f"  Weather tomorrow (Feb 17): {len(weather_tomorrow)}")
print(f"  Near-settlement: {len(near_settlement)}")
print(f"  Longshot candidates (YES≤5¢): {len(longshot_candidates)}")

# ─── 4. Select and place trades ───
print("\n🎯 SELECTING TRADES...")

# Load existing trades to avoid duplicates
existing = json.loads(TRADES_PATH.read_text())
existing_tickers = {t["ticker"] for t in existing}

new_trades = []
MAX_TRADES = 5

# Priority 1: Weather tomorrow
for m in weather_tomorrow[:3]:
    if len(new_trades) >= MAX_TRADES:
        break
    ticker = m["ticker"]
    if ticker in existing_tickers:
        continue
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    
    # If YES is cheap (<15¢), lean NO; if YES is expensive (>85¢), lean YES
    if yes_ask and yes_ask <= 15 and no_ask and no_ask <= 99:
        side = "no"
        price = no_ask
        contracts = min(5, 500 // price)
        new_trades.append({"ticker": ticker, "title": m.get("title",""), "subtitle": m.get("subtitle",""), "side": side, "price": price, "contracts": contracts, "strategy": "weather_tomorrow", "reasoning": f"Weather Feb 17: YES@{yes_ask}¢ suggests NO. Buy NO@{price}¢."})
    elif yes_ask and yes_ask >= 85 and yes_ask <= 99:
        side = "yes"
        price = yes_ask
        contracts = min(5, 500 // price)
        new_trades.append({"ticker": ticker, "title": m.get("title",""), "subtitle": m.get("subtitle",""), "side": side, "price": price, "contracts": contracts, "strategy": "weather_tomorrow", "reasoning": f"Weather Feb 17: YES@{yes_ask}¢ suggests YES. Buy YES@{price}¢."})

# Priority 2: Near-settlement (quick resolution)
near_settlement.sort(key=lambda x: x["confidence"], reverse=True)
for ns in near_settlement[:5]:
    if len(new_trades) >= MAX_TRADES:
        break
    m = ns["market"]
    ticker = m["ticker"]
    if ticker in existing_tickers:
        continue
    
    if ns["lean"] == "NO":
        no_ask = m.get("no_ask", 0)
        if no_ask and no_ask <= 99:
            contracts = min(5, 500 // no_ask)
            new_trades.append({"ticker": ticker, "title": m.get("title",""), "subtitle": m.get("subtitle",""), "side": "no", "price": no_ask, "contracts": contracts, "strategy": "near_settlement", "reasoning": f"Near settlement: YES@{m.get('yes_ask',0)}¢ → buy NO@{no_ask}¢."})
    else:
        yes_ask = m.get("yes_ask", 0)
        if yes_ask and yes_ask <= 99:
            contracts = min(5, 500 // yes_ask)
            new_trades.append({"ticker": ticker, "title": m.get("title",""), "subtitle": m.get("subtitle",""), "side": "yes", "price": yes_ask, "contracts": contracts, "strategy": "near_settlement", "reasoning": f"Near settlement: YES@{yes_ask}¢ → buy YES@{yes_ask}¢."})

# Priority 3: Longshot sells
import random
random.shuffle(longshot_candidates)
for m in longshot_candidates:
    if len(new_trades) >= MAX_TRADES:
        break
    ticker = m["ticker"]
    if ticker in existing_tickers:
        continue
    no_ask = m.get("no_ask", 0)
    yes_ask = m.get("yes_ask", 0)
    if no_ask and no_ask >= 95 and no_ask <= 99:
        contracts = min(5, 500 // no_ask)
        new_trades.append({"ticker": ticker, "title": m.get("title",""), "subtitle": m.get("subtitle",""), "side": "no", "price": no_ask, "contracts": contracts, "strategy": "longshot_sell", "reasoning": f"Longshot bias: YES@{yes_ask}¢ → sell YES (buy NO@{no_ask}¢)."})

print(f"  Selected {len(new_trades)} trades to place")

# ─── 5. Execute trades ───
print("\n📝 PLACING ORDERS...")
placed = []
for t in new_trades:
    order = {
        "ticker": t["ticker"],
        "action": "buy",
        "side": t["side"],
        "type": "limit",
        "count": t["contracts"],
    }
    if t["side"] == "yes":
        order["yes_price"] = t["price"]
    else:
        order["no_price"] = t["price"]
    
    try:
        result = api("POST", "/portfolio/orders", order)
        oi = result.get("order", {})
        status = oi.get("status", "?")
        oid = oi.get("order_id", "?")
        print(f"  ✓ {t['ticker']}: {t['side'].upper()}@{t['price']}¢ x{t['contracts']} → {status} ({oid[:8]}...)")
        
        trade_record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": t["ticker"],
            "title": t["title"],
            "subtitle": t.get("subtitle", ""),
            "strategy": t["strategy"],
            "direction": f"BUY {t['side'].upper()}",
            "order_id": oid,
            "status": status,
            "contracts": t["contracts"],
            "reasoning": t["reasoning"],
        }
        if t["side"] == "no":
            trade_record["no_price"] = t["price"]
            trade_record["yes_price_at_entry"] = 100 - t["price"]
        else:
            trade_record["yes_price"] = t["price"]
            trade_record["no_price_at_entry"] = 100 - t["price"]
        trade_record["risk_cents"] = t["price"] * t["contracts"]
        
        placed.append(trade_record)
    except requests.exceptions.HTTPError as e:
        print(f"  ✗ {t['ticker']}: {e.response.status_code} {e.response.text[:100]}")
    except Exception as e:
        print(f"  ✗ {t['ticker']}: {e}")

# ─── 6. Update files ───
print("\n💾 UPDATING FILES...")

# Update trades JSON
all_trades = existing + placed
TRADES_PATH.write_text(json.dumps(all_trades, indent=2))
print(f"  kalshi-strategy-trades.json: {len(all_trades)} total trades")

# Calculate stats
total_trades = len(all_trades)
executed = [t for t in all_trades if t.get("status") == "executed"]
total_risk = sum(t.get("risk_cents", 0) for t in all_trades)

# Update performance markdown
perf_entry = f"""

## Trade Session: 2026-02-16 18:10 (Cycle #4)

**Balance**: ${balance/100:.2f}

**Settlements Found**: {len(s_list)} | **Settlement P&L**: ${settlement_pnl/100:.2f}

**Markets Scanned**: ~{(page+1)*1000} | **Weather Tomorrow**: {len(weather_tomorrow)} | **Near-Settlement**: {len(near_settlement)} | **Longshot Candidates**: {len(longshot_candidates)}

**Open Positions**: {len(open_pos)}

### Trades Placed

| # | Ticker | Direction | Price | Qty | Risk | Status | Strategy | Reasoning |
|---|--------|-----------|-------|-----|------|--------|----------|----------|
"""
for i, t in enumerate(placed, 1):
    price = t.get("no_price", t.get("yes_price", "?"))
    perf_entry += f"| {i} | `{t['ticker']}` | {t['direction']} | {price}¢ | {t['contracts']} | ${t['risk_cents']/100:.2f} | {t['status']} | {t['strategy']} | {t['reasoning'][:60]}... |\n"

if not placed:
    perf_entry += "| - | No new trades placed | - | - | - | - | - | - |\n"

perf_entry += f"""
### Cumulative Stats
- **Total trades**: {total_trades}
- **Executed**: {len(executed)}
- **Total capital at risk**: ${total_risk/100:.2f}
- **Settlements collected**: {len(s_list)}
- **Settlement P&L**: ${settlement_pnl/100:.2f}
"""

old_perf = PERF_PATH.read_text()
PERF_PATH.write_text(old_perf + perf_entry)
print(f"  kalshi-trade-performance.md updated")

# ─── 7. Summary ───
print("\n" + "=" * 60)
print("📊 CYCLE #4 SUMMARY")
print("=" * 60)
print(f"  Balance:          ${balance/100:.2f}")
print(f"  Settlements:      {len(s_list)} (P&L: ${settlement_pnl/100:.2f})")
print(f"  Open positions:   {len(open_pos)}")
print(f"  New trades:       {len(placed)}")
print(f"  Total trades:     {total_trades}")
print(f"  Capital at risk:  ${total_risk/100:.2f}")
for t in placed:
    price = t.get("no_price", t.get("yes_price", "?"))
    print(f"    • {t['ticker']}: {t['direction']} @{price}¢ x{t['contracts']} [{t['status']}]")
print("=" * 60)
