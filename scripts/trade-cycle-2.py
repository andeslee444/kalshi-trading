#!/usr/bin/env python3
"""Kalshi Demo Trade Cycle #2 — Check settlements, scan opportunities, place trades."""

import json, time, base64, datetime, os, sys, re
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path("/Users/andeslee/Documents/cursor-projects/class-sniper")
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
    h = get_headers(method, "/trade-api/v2" + path)
    r = requests.request(method, url, headers=h, json=body if method != "GET" else None, timeout=15)
    r.raise_for_status()
    return r.json()

# ─── 1. Check balance ───
print("=" * 60)
print("KALSHI DEMO TRADE CYCLE #2 — 2026-02-16 15:51 EST")
print("=" * 60)

bal = api("GET", "/portfolio/balance")
balance_cents = bal.get("balance", 0)
print(f"\n💰 Balance: ${balance_cents/100:.2f}")

# ─── 2. Check positions ───
print("\n📊 Current Positions:")
try:
    pos = api("GET", "/portfolio/positions")
    positions = pos.get("market_positions", [])
    for p in positions:
        ticker = p.get("ticker", "?")
        yes_q = p.get("position", 0)
        no_q = p.get("total_traded", 0)
        market_exposure = p.get("market_exposure", 0)
        print(f"  {ticker}: position={yes_q}, exposure={market_exposure}")
    if not positions:
        print("  (no open positions)")
except Exception as e:
    print(f"  Error: {e}")
    positions = []

# ─── 3. Check settlements ───
print("\n🏁 Recent Settlements:")
try:
    sett = api("GET", "/portfolio/settlements?limit=20")
    settlements = sett.get("settlements", [])
    if settlements:
        for s in settlements:
            ticker = s.get("ticker", "?")
            revenue = s.get("revenue", 0)
            yes_price = s.get("yes_price", 0)
            print(f"  {ticker}: revenue={revenue}¢, settled YES@{yes_price}¢")
    else:
        print("  (no settlements yet)")
except Exception as e:
    print(f"  Settlement check error: {e}")
    settlements = []

# ─── 4. Scan for longshot opportunities ───
print("\n🔍 Scanning markets for opportunities...")

# Categories to scan for longshots
SEARCH_CATEGORIES = [
    ("weather", "KXHIGH"),
    ("weather", "KXLOW"),
    ("sports", "KXNBA"),
    ("sports", "KXNFL"),
    ("sports", "KXMARMAD"),
    ("politics", "KXPRES"),
    ("entertainment", "KXOSCARS"),
    ("entertainment", "KXGRAMMYS"),
    ("crypto", "KXBTC"),
    ("economics", "KXCPI"),
]

# Scan all open markets for longshots (YES ≤ 5¢) and near-settlement
all_longshots = []
near_settlement = []
cursor = None
total_scanned = 0

for page in range(80):
    path = "/markets?status=open&limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except Exception as e:
        print(f"  Page {page} error: {e}")
        break
    batch = data.get("markets", [])
    total_scanned += len(batch)
    
    for m in batch:
        ticker = m.get("ticker", "")
        yes_ask = m.get("yes_ask", 0)
        no_ask = m.get("no_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        close_time = m.get("close_time", "")
        title = m.get("title", "")
        subtitle = m.get("subtitle", "")
        
        # Longshots: YES ≤ 5¢ with asks available
        if yes_ask and 1 <= yes_ask <= 5:
            all_longshots.append(m)
        
        # Near settlement: closes today (2026-02-16) or tomorrow
        if close_time and close_time[:10] in ("2026-02-16", "2026-02-17"):
            if yes_ask and yes_ask > 0:
                near_settlement.append(m)
    
    cursor = data.get("cursor")
    if not cursor or not batch:
        break

print(f"  Scanned {total_scanned} markets across {page+1} pages")
print(f"  Found {len(all_longshots)} longshot opportunities (YES ≤ 5¢)")
print(f"  Found {len(near_settlement)} near-settlement markets")

# ─── 5. Score and select best trades ───
# Strategy: sell longshots (buy NO) — exploit favourite-longshot bias
# For near-settlement, look for weather/entertainment with info edge

# Existing tickers to avoid duplicates
existing_tickers = set()
try:
    existing = json.loads(TRADES_JSON.read_text())
    for t in existing:
        existing_tickers.add(t.get("ticker", ""))
except:
    existing = []

# Score longshots — prefer sports/entertainment (strongest bias per Becker)
def score_longshot(m):
    ticker = m.get("ticker", "")
    yes_ask = m.get("yes_ask", 1)
    score = 0
    # Lower price = more overpriced (stronger longshot bias)
    score += (6 - yes_ask) * 10
    # Category bonus
    if any(x in ticker for x in ["KXNBA", "KXNFL", "KXMARMAD", "KXNHL", "KXMLB"]):
        score += 30  # Sports = strongest bias
    elif any(x in ticker for x in ["KXOSCARS", "KXGRAMMYS", "KXBILLBOARD"]):
        score += 25
    elif any(x in ticker for x in ["KXPRES", "KXSEN", "KXGOV"]):
        score += 15
    # Skip already traded
    if ticker in existing_tickers:
        score -= 1000
    return score

all_longshots.sort(key=score_longshot, reverse=True)

# Print top candidates
print("\n📋 Top Longshot Candidates:")
for m in all_longshots[:15]:
    ticker = m.get("ticker", "")
    title = m.get("title", "")
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    skip = " ⚠️ ALREADY TRADED" if ticker in existing_tickers else ""
    print(f"  {ticker}: YES@{yes_ask}¢ NO@{no_ask}¢ — {title}{skip}")

print("\n📋 Near-Settlement Candidates:")
for m in near_settlement[:10]:
    ticker = m.get("ticker", "")
    title = m.get("title", "")
    yes_ask = m.get("yes_ask", 0)
    close_time = m.get("close_time", "")[:16]
    print(f"  {ticker}: YES@{yes_ask}¢ closes {close_time} — {title}")

# ─── 6. Place trades ───
print("\n🎯 Placing Trades...")
new_trades = []
trade_count = 0

# Strategy A: Sell longshots (buy NO) — up to 3 trades
for m in all_longshots:
    if trade_count >= 3:
        break
    ticker = m.get("ticker", "")
    if ticker in existing_tickers:
        continue
    
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    title = m.get("title", "")
    subtitle = m.get("subtitle", "")
    
    if not no_ask or no_ask > 99 or no_ask < 90:
        continue
    
    # Buy NO at no_ask — risk = no_ask per contract, profit = 100 - no_ask if longshot loses
    # Max $5 → contracts = min(floor(500/no_ask), 5)
    contracts = min(500 // no_ask, 5)
    if contracts < 1:
        continue
    
    # Becker model edge estimate
    implied_prob = yes_ask / 100
    # Longshots at ≤5¢ historically win ~40-60% less than implied
    est_true_prob = max(0.001, implied_prob * 0.43)  # Becker: 1¢ contracts win 0.43% vs 1% implied
    edge = (1 - est_true_prob) - (no_ask / 100)
    
    order = {
        "ticker": ticker,
        "action": "buy",
        "side": "no",
        "type": "limit",
        "count": contracts,
        "no_price": no_ask,
    }
    
    try:
        result = api("POST", "/portfolio/orders", order)
        oi = result.get("order", {})
        status = oi.get("status", "unknown")
        oid = oi.get("order_id", "unknown")
        print(f"  ✅ #{trade_count+1} SELL LONGSHOT: {ticker} — BUY {contracts}x NO@{no_ask}¢ — {title}")
        print(f"     Order {oid}: {status} | Risk: ${contracts*no_ask/100:.2f} | Edge: {edge*100:.1f}%")
        
        new_trades.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": ticker,
            "title": title,
            "subtitle": subtitle,
            "strategy": "longshot_sell",
            "direction": "BUY NO (= SELL YES)",
            "no_price": no_ask,
            "yes_price_at_entry": yes_ask,
            "contracts": contracts,
            "risk_cents": contracts * no_ask,
            "est_edge": f"{edge*100:.1f}%",
            "reasoning": f"Longshot bias: YES@{yes_ask}¢ implies {implied_prob*100:.0f}% prob, Becker model est true prob ~{est_true_prob*100:.1f}%. Sell YES (buy NO@{no_ask}¢) for ~{edge*100:.1f}% edge.",
            "order_id": oid,
            "status": status,
        })
        existing_tickers.add(ticker)
        trade_count += 1
    except requests.exceptions.HTTPError as e:
        print(f"  ❌ Failed {ticker}: {e.response.status_code} {e.response.text[:200]}")
    except Exception as e:
        print(f"  ❌ Failed {ticker}: {e}")

# Strategy B: Near-settlement opportunities — up to 2 trades
# Look for weather markets settling today where we can check forecast
for m in near_settlement:
    if trade_count >= 5:
        break
    ticker = m.get("ticker", "")
    if ticker in existing_tickers:
        continue
    
    yes_ask = m.get("yes_ask", 0)
    no_ask = m.get("no_ask", 0)
    title = m.get("title", "")
    subtitle = m.get("subtitle", "")
    
    # Only trade if there's a clear lean (YES very cheap or very expensive)
    if yes_ask and 1 <= yes_ask <= 10 and no_ask and no_ask >= 90:
        # Likely NO outcome — buy NO
        contracts = min(500 // no_ask, 5)
        if contracts < 1:
            continue
        order = {"ticker": ticker, "action": "buy", "side": "no", "type": "limit", "count": contracts, "no_price": no_ask}
        try:
            result = api("POST", "/portfolio/orders", order)
            oi = result.get("order", {})
            status = oi.get("status", "unknown")
            oid = oi.get("order_id", "unknown")
            print(f"  ✅ #{trade_count+1} NEAR-SETTLE: {ticker} — BUY {contracts}x NO@{no_ask}¢ — {title}")
            print(f"     Order {oid}: {status}")
            new_trades.append({
                "timestamp": datetime.datetime.now().isoformat(),
                "ticker": ticker, "title": title, "subtitle": subtitle,
                "strategy": "near_settlement",
                "direction": "BUY NO",
                "no_price": no_ask, "yes_price_at_entry": yes_ask,
                "contracts": contracts, "risk_cents": contracts * no_ask,
                "est_edge": "near-settlement lean",
                "reasoning": f"Near settlement: YES@{yes_ask}¢ suggests likely NO. Buy NO@{no_ask}¢ for quick resolution.",
                "order_id": oid, "status": status,
            })
            existing_tickers.add(ticker)
            trade_count += 1
        except requests.exceptions.HTTPError as e:
            print(f"  ❌ Failed {ticker}: {e.response.status_code} {e.response.text[:200]}")
        except Exception as e:
            print(f"  ❌ Failed {ticker}: {e}")
    
    elif yes_ask and 90 <= yes_ask <= 99 and no_ask and 1 <= no_ask <= 10:
        # Likely YES outcome — buy YES
        contracts = min(500 // yes_ask, 5)
        if contracts < 1:
            continue
        order = {"ticker": ticker, "action": "buy", "side": "yes", "type": "limit", "count": contracts, "yes_price": yes_ask}
        try:
            result = api("POST", "/portfolio/orders", order)
            oi = result.get("order", {})
            status = oi.get("status", "unknown")
            oid = oi.get("order_id", "unknown")
            print(f"  ✅ #{trade_count+1} NEAR-SETTLE: {ticker} — BUY {contracts}x YES@{yes_ask}¢ — {title}")
            print(f"     Order {oid}: {status}")
            new_trades.append({
                "timestamp": datetime.datetime.now().isoformat(),
                "ticker": ticker, "title": title, "subtitle": subtitle,
                "strategy": "near_settlement",
                "direction": "BUY YES",
                "yes_price": yes_ask, "no_price_at_entry": no_ask,
                "contracts": contracts, "risk_cents": contracts * yes_ask,
                "est_edge": "near-settlement lean",
                "reasoning": f"Near settlement: YES@{yes_ask}¢ suggests likely YES. Buy YES@{yes_ask}¢ for quick resolution.",
                "order_id": oid, "status": status,
            })
            existing_tickers.add(ticker)
            trade_count += 1
        except requests.exceptions.HTTPError as e:
            print(f"  ❌ Failed {ticker}: {e.response.status_code} {e.response.text[:200]}")
        except Exception as e:
            print(f"  ❌ Failed {ticker}: {e}")

print(f"\n📊 Summary: Placed {trade_count} new trades")

# ─── 7. Update trades JSON ───
all_trades = existing + new_trades
TRADES_JSON.write_text(json.dumps(all_trades, indent=2))
print(f"✅ Updated {TRADES_JSON} ({len(all_trades)} total trades)")

# ─── 8. Update performance markdown ───
# Re-check balance after trades
bal2 = api("GET", "/portfolio/balance")
new_balance = bal2.get("balance", 0)

perf_entry = f"""

## Trade Session: 2026-02-16 15:51 (Cycle #2)

**Balance Before**: ${balance_cents/100:.2f} | **Balance After**: ${new_balance/100:.2f}

**Markets Scanned**: {total_scanned} | **Longshot Candidates**: {len(all_longshots)} | **Near-Settlement**: {len(near_settlement)}

**Settlements**: {len(settlements)} found
"""

if settlements:
    perf_entry += "\n### Settled Positions\n\n"
    perf_entry += "| Ticker | Revenue | Settlement |\n|--------|---------|------------|\n"
    for s in settlements:
        perf_entry += f"| `{s.get('ticker','')}` | {s.get('revenue',0)}¢ | YES@{s.get('yes_price',0)}¢ |\n"

perf_entry += "\n### New Trades Placed\n\n"
perf_entry += "| # | Ticker | Direction | Price | Qty | Edge | Risk | Status | Reasoning |\n"
perf_entry += "|---|--------|-----------|-------|-----|------|------|--------|----------|\n"

for i, t in enumerate(new_trades):
    direction = t.get("direction", "?")
    price = t.get("no_price", t.get("yes_price", "?"))
    perf_entry += f"| {i+1} | `{t['ticker']}` | {direction} | {price}¢ | {t['contracts']} | {t['est_edge']} | ${t['risk_cents']/100:.2f} | {t['status']} | {t['reasoning'][:60]}... |\n"

if not new_trades:
    perf_entry += "| — | No new trades placed | — | — | — | — | — | — | — |\n"

# Append to performance file
with open(PERF_MD, "a") as f:
    f.write(perf_entry)
print(f"✅ Updated {PERF_MD}")

print("\n✅ Trade cycle #2 complete!")
