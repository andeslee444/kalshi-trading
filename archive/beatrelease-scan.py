#!/usr/bin/env python3
"""BeatRelease.com Kalshi copy-trader — first scan for Charli XCX album sales."""

import json, time, base64, datetime, os, sys
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
STATE_PATH = PROJECT_DIR / "data" / "beatrelease-state.json"
TRADES_PATH = PROJECT_DIR / "data" / "beatrelease-trades.json"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Auth
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
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

# Verify auth
print("Verifying auth...")
bal = api("GET", "/portfolio/balance")
print(f"Balance: ${bal.get('balance',0)/100:.2f}")

# Search for Charli XCX markets
print("\nSearching for Charli XCX album sales markets...")
# Try different search approaches
markets_found = []
cursor = None
for page in range(50):
    path = "/markets?status=open&limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    data = api("GET", path)
    batch = data.get("markets", [])
    for m in batch:
        t = m.get("ticker", "").upper()
        title = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
        if "charli" in title or "xcx" in title or "wuthering" in title or "album" in title:
            markets_found.append(m)
            print(f"  Found: {m['ticker']} — {m.get('title','')} | yes_ask={m.get('yes_ask')} no_ask={m.get('no_ask')}")
    cursor = data.get("cursor")
    if not cursor or not batch:
        print(f"  Scanned {page+1} pages, {len(batch)} in last batch")
        break

print(f"\nTotal matching markets: {len(markets_found)}")

# Also try event search
print("\nSearching events...")
try:
    events = api("GET", "/events?status=open&limit=200")
    for ev in events.get("events", []):
        title = (ev.get("title", "") + " " + ev.get("sub_title", "")).lower()
        if any(k in title for k in ["charli", "xcx", "wuthering", "album sale"]):
            print(f"  Event: {ev.get('event_ticker')} — {ev.get('title')}")
            # Get markets for this event
            evt_markets = api("GET", f"/events/{ev['event_ticker']}")
            for m in evt_markets.get("markets", []):
                markets_found.append(m)
                print(f"    Market: {m['ticker']} — {m.get('title','')} yes_ask={m.get('yes_ask')} no_ask={m.get('no_ask')}")
except Exception as e:
    print(f"  Event search error: {e}")

# Print all found markets for debugging
if markets_found:
    print(f"\n=== {len(markets_found)} markets found ===")
    for m in markets_found:
        print(json.dumps({k: m.get(k) for k in ['ticker','title','subtitle','yes_ask','no_ask','yes_bid','no_bid','last_price','volume']}, indent=2))
else:
    print("\nNo markets found. Trying broader search with series...")
    # Try series/collections
    try:
        series = api("GET", "/series?limit=200")
        for s in series.get("series", []):
            title = s.get("title", "").lower()
            if any(k in title for k in ["charli", "xcx", "album", "music", "sales"]):
                print(f"  Series: {s}")
    except Exception as e:
        print(f"  Series error: {e}")

# BeatRelease recommended allocation (from blog post)
RECOMMENDED_TRADES = [
    {"threshold": 15000, "price_cents": 72, "contracts": 4},
    {"threshold": 20000, "price_cents": 52, "contracts": 7},
    {"threshold": 30000, "price_cents": 37, "contracts": 3},
    {"threshold": 35000, "price_cents": 35, "contracts": 1},
    {"threshold": 40000, "price_cents": 20, "contracts": 3},
    {"threshold": 45000, "price_cents": 17, "contracts": 2},
    {"threshold": 50000, "price_cents": 20, "contracts": 2},
    {"threshold": 55000, "price_cents": 5, "contracts": 15},
]

# Try to match markets to thresholds and place trades
trades_placed = []
now = datetime.datetime.now().isoformat()

# Build threshold->market mapping
threshold_map = {}
for m in markets_found:
    title = m.get("title", "") + " " + m.get("subtitle", "")
    # Try to extract threshold from title (e.g., "15,000" or "15000" or "15k")
    import re
    numbers = re.findall(r'(\d{1,3},?\d{3})', title)
    for n in numbers:
        val = int(n.replace(",", ""))
        if val in [t["threshold"] for t in RECOMMENDED_TRADES]:
            threshold_map[val] = m
    # Also check for "Xk" patterns
    k_numbers = re.findall(r'(\d+)k', title.lower())
    for n in k_numbers:
        val = int(n) * 1000
        if val in [t["threshold"] for t in RECOMMENDED_TRADES]:
            threshold_map[val] = m

print(f"\nMatched thresholds: {list(threshold_map.keys())}")

for rec in RECOMMENDED_TRADES:
    threshold = rec["threshold"]
    if threshold not in threshold_map:
        print(f"  ⚠ No market found for ≥{threshold:,} threshold, skipping")
        continue
    
    m = threshold_map[threshold]
    ticker = m["ticker"]
    yes_ask = m.get("yes_ask", 99)
    
    # Use market ask price, fall back to recommended price
    price = min(yes_ask, rec["price_cents"]) if yes_ask and yes_ask < 99 else rec["price_cents"]
    count = rec["contracts"]
    
    print(f"\n→ Placing: {count}x YES {ticker} @ {price}¢ (≥{threshold:,} albums)")
    try:
        result = api("POST", "/portfolio/orders", {
            "ticker": ticker,
            "action": "buy",
            "side": "yes",
            "type": "limit",
            "count": count,
            "yes_price": price,
        })
        order = result.get("order", {})
        print(f"  ✓ Order {order.get('order_id','?')} — status: {order.get('status','?')}")
        trades_placed.append({
            "timestamp": now,
            "source": "beatrelease.com",
            "post": "kalshi-charli-xcx-sales-prediction-20k-55k-pure-sales-spread",
            "ticker": ticker,
            "side": "yes",
            "price_cents": price,
            "count": count,
            "threshold": threshold,
            "order_id": order.get("order_id"),
            "status": order.get("status"),
        })
    except requests.exceptions.HTTPError as e:
        print(f"  ✗ Failed: {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        print(f"  ✗ Failed: {e}")

# Save state
state = {
    "last_scan": now,
    "source": "https://www.beatrelease.com/blog/categories/kalshi-predictions",
    "posts_scanned": [
        {
            "url": "https://www.beatrelease.com/post/kalshi-charli-xcx-sales-prediction-20k-55k-pure-sales-spread",
            "title": "Kalshi: Charli XCX Sales Prediction 20k-55k Pure Sales Spread",
            "scanned_at": now,
            "strategy": "YES ladder across 15k-55k pure album sales thresholds",
            "allocation": RECOMMENDED_TRADES,
        }
    ],
    "markets_found": [m.get("ticker") for m in markets_found],
    "trades_placed": len(trades_placed),
}
STATE_PATH.write_text(json.dumps(state, indent=2))
print(f"\n✓ State saved to {STATE_PATH}")

# Save trades
existing = []
if TRADES_PATH.exists():
    try: existing = json.loads(TRADES_PATH.read_text())
    except: pass
existing.extend(trades_placed)
TRADES_PATH.write_text(json.dumps(existing, indent=2))
print(f"✓ Trades saved to {TRADES_PATH} ({len(trades_placed)} new, {len(existing)} total)")

print(f"\nDone! {len(trades_placed)} orders placed.")
