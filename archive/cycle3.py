#!/usr/bin/env python3
"""Kalshi Trade Cycle #3 — Check settlements, scan markets, place new trades."""

import json, time, base64, datetime, os, sys, re
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_JSON = PROJECT_DIR / "data" / "kalshi-strategy-trades.json"
PERF_MD = PROJECT_DIR / "data" / "kalshi-trade-performance.md"
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
    headers = get_headers(method, "/trade-api/v2" + path)
    r = requests.request(method, url, headers=headers, json=body if method != "GET" else None, timeout=30)
    r.raise_for_status()
    return r.json()

def api_paginate(path, key="markets", max_pages=50):
    """Paginate through results."""
    all_items = []
    cursor = None
    for _ in range(max_pages):
        p = path + ("&" if "?" in path else "?") + "limit=1000"
        if cursor:
            p += f"&cursor={cursor}"
        try:
            data = api("GET", p)
        except Exception as e:
            print(f"  Pagination error: {e}")
            break
        items = data.get(key, [])
        all_items.extend(items)
        cursor = data.get("cursor")
        if not cursor or not items:
            break
    return all_items

# ---- STEP 1: Check balance & positions ----
print("=" * 60)
print("KALSHI TRADE CYCLE #3")
print("=" * 60)

bal = api("GET", "/portfolio/balance")
balance = bal.get("balance", 0)
print(f"\n💰 Balance: ${balance/100:.2f}")

# Check positions
try:
    positions = api("GET", "/portfolio/positions")
    pos_list = positions.get("market_positions", [])
    print(f"📊 Open positions: {len(pos_list)}")
    for p in pos_list[:20]:
        ticker = p.get("ticker", "?")
        yes_q = p.get("market_exposure", p.get("position", "?"))
        print(f"   {ticker}: {json.dumps({k:v for k,v in p.items() if v and k != 'ticker'})}")
except Exception as e:
    print(f"Positions error: {e}")
    pos_list = []

# ---- STEP 2: Check settlements ----
print(f"\n{'='*60}")
print("CHECKING SETTLEMENTS")
try:
    settlements = api("GET", "/portfolio/settlements")
    settl_list = settlements.get("settlements", [])
    print(f"Found {len(settl_list)} settlements")
    for s in settl_list[:10]:
        print(f"  {s.get('ticker','?')}: revenue=${s.get('revenue',0)/100:.2f}, settled={s.get('settled_time','?')}")
except Exception as e:
    print(f"Settlements error: {e}")
    settl_list = []

# ---- STEP 3: Scan markets for opportunities ----
print(f"\n{'='*60}")
print("SCANNING MARKETS FOR CYCLE 3 TRADES")

# Strategy 1: Near-settlement weather markets (today's date)
today = datetime.date.today().strftime("%y%b%d").upper()  # e.g., 26FEB16
print(f"\n📍 Looking for near-settlement weather (today={today})...")

weather_markets = []
all_markets_count = 0
longshot_candidates = []
near_settlement = []
crypto_markets = []

# Scan all open markets
cursor = None
for page in range(60):
    path = "/markets?status=open&limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except Exception as e:
        print(f"  Page {page} error: {e}")
        break
    batch = data.get("markets", [])
    all_markets_count += len(batch)
    
    for m in batch:
        ticker = m.get("ticker", "")
        yes_ask = m.get("yes_ask", 0) or 0
        no_ask = m.get("no_ask", 0) or 0
        
        # Weather today
        if "KXHIGH" in ticker and today in ticker:
            weather_markets.append(m)
        
        # Crypto 15-min
        if any(x in ticker for x in ["KXBTC", "KXETH", "BTC", "ETH"]) and "15" in ticker:
            crypto_markets.append(m)
        
        # Longshot candidates: YES ask <= 5 cents
        if yes_ask > 0 and yes_ask <= 5 and no_ask > 0:
            longshot_candidates.append(m)
        
        # Near-settlement: markets closing today with strong lean
        close_time = m.get("close_time", "") or m.get("expiration_time", "")
        if close_time and "2026-02-16" in close_time:
            if (yes_ask <= 10 and yes_ask > 0) or (no_ask <= 10 and no_ask > 0):
                near_settlement.append(m)
    
    cursor = data.get("cursor")
    if not cursor or not batch:
        break

print(f"  Total markets scanned: {all_markets_count}")
print(f"  Today's weather: {len(weather_markets)}")
print(f"  15-min crypto: {len(crypto_markets)}")
print(f"  Longshot candidates (YES<=5¢): {len(longshot_candidates)}")
print(f"  Near-settlement today (strong lean): {len(near_settlement)}")

# ---- STEP 4: Select and place trades ----
print(f"\n{'='*60}")
print("PLACING TRADES")

existing_trades = json.loads(TRADES_JSON.read_text())
existing_tickers = {t["ticker"] for t in existing_trades}
new_trades = []

def place_order(ticker, side, price, count, strategy, reasoning, title="", subtitle=""):
    """Place a limit order and record it."""
    if ticker in existing_tickers:
        print(f"  ⏭️  Already have position in {ticker}, skipping")
        return None
    
    body = {"ticker": ticker, "action": "buy", "side": side, "type": "limit", "count": count}
    if side == "yes":
        body["yes_price"] = price
    else:
        body["no_price"] = price
    
    try:
        result = api("POST", "/portfolio/orders", body)
        order = result.get("order", {})
        oid = order.get("order_id", "unknown")
        status = order.get("status", "?")
        print(f"  ✅ {ticker} — {side.upper()} {count}x @ {price}¢ — {status} (ID: {oid})")
        
        trade_record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": ticker,
            "title": title,
            "subtitle": subtitle,
            "strategy": strategy,
            "direction": f"BUY {side.upper()}",
            "order_id": oid,
            "status": status,
        }
        if side == "no":
            trade_record["no_price"] = price
            trade_record["yes_price_at_entry"] = 100 - price
            trade_record["risk_cents"] = price * count
        else:
            trade_record["yes_price"] = price
            trade_record["risk_cents"] = price * count
        trade_record["contracts"] = count
        trade_record["est_edge"] = reasoning[:60]
        trade_record["reasoning"] = reasoning
        
        new_trades.append(trade_record)
        existing_tickers.add(ticker)
        return trade_record
    except requests.exceptions.HTTPError as e:
        print(f"  ❌ {ticker} failed: {e.response.status_code} {e.response.text[:200]}")
        return None
    except Exception as e:
        print(f"  ❌ {ticker} failed: {e}")
        return None

trades_placed = 0
MAX_TRADES = 5

# Priority 1: Near-settlement weather markets settling tonight
print(f"\n🌡️  Near-settlement weather markets:")
for m in weather_markets[:5]:
    if trades_placed >= MAX_TRADES:
        break
    ticker = m["ticker"]
    yes_ask = m.get("yes_ask", 0) or 0
    no_ask = m.get("no_ask", 0) or 0
    title = m.get("title", "")
    subtitle = m.get("subtitle", "")
    
    if ticker in existing_tickers:
        print(f"  ⏭️  {ticker} — already have position")
        continue
    
    # Strong lean: buy the likely side
    if yes_ask > 0 and yes_ask <= 10:
        # Likely NO outcome, buy NO
        if no_ask > 0 and no_ask <= 99:
            count = min(5, 500 // no_ask)
            t = place_order(ticker, "no", no_ask, count, "near_settlement", 
                f"Near settlement: YES@{yes_ask}¢ suggests likely NO. Buy NO@{no_ask}¢.", title, subtitle)
            if t: trades_placed += 1
    elif no_ask > 0 and no_ask <= 10:
        # Likely YES outcome, buy YES
        if yes_ask > 0 and yes_ask <= 99:
            count = min(5, 500 // yes_ask)
            t = place_order(ticker, "yes", yes_ask, count, "near_settlement",
                f"Near settlement: NO@{no_ask}¢ suggests likely YES. Buy YES@{yes_ask}¢.", title, subtitle)
            if t: trades_placed += 1

# Priority 2: Near-settlement non-weather markets
print(f"\n⏰ Other near-settlement markets:")
for m in near_settlement[:10]:
    if trades_placed >= MAX_TRADES:
        break
    ticker = m["ticker"]
    if ticker in existing_tickers or "KXHIGH" in ticker:
        continue
    yes_ask = m.get("yes_ask", 0) or 0
    no_ask = m.get("no_ask", 0) or 0
    title = m.get("title", "")
    
    if yes_ask > 0 and yes_ask <= 5:
        count = min(5, 500 // max(1, 100 - yes_ask))
        t = place_order(ticker, "no", 100 - yes_ask, count, "near_settlement_longshot",
            f"Near settlement longshot sell: YES@{yes_ask}¢, buy NO@{100-yes_ask}¢.", title)
        if t: trades_placed += 1
    elif no_ask > 0 and no_ask <= 5:
        count = min(5, 500 // max(1, 100 - no_ask))
        t = place_order(ticker, "yes", 100 - no_ask, count, "near_settlement_longshot",
            f"Near settlement longshot sell: NO@{no_ask}¢, buy YES@{100-no_ask}¢.", title)
        if t: trades_placed += 1

# Priority 3: Longshot bias — sell YES on cheap longshots (buy NO at high prices)
print(f"\n🎯 Longshot bias opportunities:")
# Sort by lowest yes_ask (most extreme longshots)
longshot_candidates.sort(key=lambda m: m.get("yes_ask", 100))
for m in longshot_candidates[:20]:
    if trades_placed >= MAX_TRADES:
        break
    ticker = m["ticker"]
    if ticker in existing_tickers:
        continue
    yes_ask = m.get("yes_ask", 0) or 0
    no_ask = m.get("no_ask", 0) or 0
    title = m.get("title", "")
    
    # Prefer sports/entertainment (strongest bias per Becker)
    is_sports = any(x in ticker for x in ["NBA", "NFL", "MLB", "NHL", "NCAA", "MARMAD", "SOCCER"])
    is_entertainment = any(x in ticker for x in ["OSCAR", "GRAMMY", "BILLBOARD", "CELEBRITY"])
    
    if no_ask > 0 and no_ask <= 99 and yes_ask <= 3:
        count = min(5, 500 // no_ask)
        edge_label = "sports longshot" if is_sports else "longshot bias"
        t = place_order(ticker, "no", no_ask, count, "longshot_sell",
            f"Longshot bias: YES@{yes_ask}¢. Sell YES (buy NO@{no_ask}¢). {edge_label}.", title)
        if t: trades_placed += 1

# Priority 4: Crypto 15-min extreme strikes
print(f"\n₿ 15-min crypto markets:")
for m in crypto_markets[:10]:
    if trades_placed >= MAX_TRADES:
        break
    ticker = m["ticker"]
    if ticker in existing_tickers:
        continue
    yes_ask = m.get("yes_ask", 0) or 0
    no_ask = m.get("no_ask", 0) or 0
    title = m.get("title", "")
    
    # Sell extreme OTM: YES <= 5¢
    if yes_ask > 0 and yes_ask <= 5 and no_ask > 0:
        count = min(5, 500 // no_ask)
        t = place_order(ticker, "no", no_ask, count, "crypto_15min_otm",
            f"15-min crypto OTM: YES@{yes_ask}¢, sell YES (buy NO@{no_ask}¢).", title)
        if t: trades_placed += 1

print(f"\n{'='*60}")
print(f"CYCLE 3 SUMMARY")
print(f"  Trades placed: {trades_placed}")
print(f"  Markets scanned: {all_markets_count}")
print(f"  Balance: ${balance/100:.2f}")

# ---- STEP 5: Update files ----
# Update trades JSON
all_trades = existing_trades + new_trades
TRADES_JSON.write_text(json.dumps(all_trades, indent=2))
print(f"\n📝 Updated {TRADES_JSON}")

# Update performance MD
cycle3_md = f"""

## Trade Session: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} (Cycle #3)

**Balance**: ${balance/100:.2f}

**Markets Scanned**: {all_markets_count} | **Longshot Candidates**: {len(longshot_candidates)} | **Near-Settlement**: {len(near_settlement)} | **Weather Today**: {len(weather_markets)} | **Crypto 15-min**: {len(crypto_markets)}

**Settlements Found**: {len(settl_list)}
"""

if settl_list:
    cycle3_md += "\n### Settlements\n\n"
    cycle3_md += "| Ticker | Revenue | Settled |\n|--------|---------|--------|\n"
    for s in settl_list:
        cycle3_md += f"| `{s.get('ticker','?')}` | ${s.get('revenue',0)/100:.2f} | {s.get('settled_time','?')[:19]} |\n"

if new_trades:
    cycle3_md += "\n### New Trades Placed\n\n"
    cycle3_md += "| # | Ticker | Direction | Price | Qty | Strategy | Risk | Status | Reasoning |\n"
    cycle3_md += "|---|--------|-----------|-------|-----|----------|------|--------|----------|\n"
    for i, t in enumerate(new_trades, 1):
        price = t.get("no_price", t.get("yes_price", "?"))
        cycle3_md += f"| {i} | `{t['ticker']}` | {t['direction']} | {price}¢ | {t['contracts']} | {t['strategy']} | ${t['risk_cents']/100:.2f} | {t['status']} | {t['reasoning'][:60]}... |\n"
else:
    cycle3_md += "\n### No new trades placed this cycle.\n"

# Append to performance file
with open(PERF_MD, "a") as f:
    f.write(cycle3_md)
print(f"📝 Updated {PERF_MD}")

print(f"\n✅ Cycle #3 complete!")
