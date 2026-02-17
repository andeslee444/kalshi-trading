#!/usr/bin/env python3
"""Copy-trade BeatRelease blog posts for Jack Harlow MONICA and BTS ARIRANG."""

import json, time, base64, uuid, datetime
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

with open(KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def get_headers(method, path):
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}"
    sig = private_key.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": API_KEY, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}

def api(method, path, body=None):
    url = BASE_URL + path
    full_path = "/trade-api/v2" + path
    headers = get_headers(method, full_path)
    r = requests.request(method, url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def get_market(ticker):
    """Get market details and orderbook."""
    try:
        market = api("GET", f"/markets/{ticker}")
        return market.get("market", market)
    except Exception as e:
        print(f"  Error fetching {ticker}: {e}")
        return None

def place_order(ticker, side, price_cents, count):
    """Place a limit order. side='yes' or 'no'."""
    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "type": "limit",
        "action": "buy",
        "side": side,
        "count": count,
        "yes_price": price_cents if side == "yes" else None,
        "no_price": price_cents if side == "no" else None,
    }
    # Remove None values
    body = {k: v for k, v in body.items() if v is not None}
    print(f"  Placing: {side.upper()} {ticker} x{count} @ {price_cents}¢")
    try:
        result = api("POST", "/portfolio/orders", body)
        order = result.get("order", result)
        print(f"  → Order ID: {order.get('order_id', 'unknown')}, status: {order.get('status', 'unknown')}")
        return order
    except requests.HTTPError as e:
        print(f"  → ERROR: {e.response.status_code} {e.response.text}")
        return {"error": str(e), "status_code": e.response.status_code}

# === Trade Plan ===
# Jack Harlow MONICA: YES on 5K+, 10K+, 20K+ (blog has positions at 75¢, 54-57¢, ~35¢)
# BTS ARIRANG: YES on 240K+, 280K+ (blog has positions in these tiers)

trades_to_place = [
    # Jack Harlow MONICA - $5 max per trade
    {"ticker": "KXALBUMSALES-MO-5000", "side": "yes", "price_cents": 85, "count": 5,
     "post": "jack-harlow-monica", "reasoning": "Harlow YES ≥5K pure sales. Blog: 85% prob, core position at 75¢. Strong baseline."},
    {"ticker": "KXALBUMSALES-MO-10000", "side": "yes", "price_cents": 56, "count": 5,
     "post": "jack-harlow-monica", "reasoning": "Harlow YES ≥10K pure sales. Blog: 56% prob, large 263-contract position at 57.6¢ avg."},
    {"ticker": "KXALBUMSALES-MO-20000", "side": "yes", "price_cents": 35, "count": 5,
     "post": "jack-harlow-monica", "reasoning": "Harlow YES ≥20K pure sales. Blog: 35% prob upside position, physical editions boost."},
    # BTS ARIRANG - $5 max per trade
    {"ticker": "KXALBUMSALES-ARI-240000", "side": "yes", "price_cents": 80, "count": 5,
     "post": "bts-arirang", "reasoning": "BTS YES ≥240K pure sales. Blog position. Historical floor 150-180K, comeback demand ~300K expected."},
    {"ticker": "KXALBUMSALES-ARI-280000", "side": "yes", "price_cents": 65, "count": 5,
     "post": "bts-arirang", "reasoning": "BTS YES ≥280K pure sales. Blog position. Market forecasting ~300K, near historical peak."},
]

now = datetime.datetime.now().isoformat()
results = []

print("=== BeatRelease Copy-Trade: Jack Harlow MONICA + BTS ARIRANG ===\n")

for trade in trades_to_place:
    # Check market exists
    m = get_market(trade["ticker"])
    if not m:
        print(f"  SKIP: {trade['ticker']} not found\n")
        continue
    
    status = m.get("status", "unknown")
    print(f"  Market: {trade['ticker']} status={status}")
    
    if status not in ("open", "active"):
        print(f"  SKIP: market not open/active (status={status})\n")
        continue
    
    # Get current best prices
    try:
        ob = api("GET", f"/markets/{trade['ticker']}/orderbook")
        yes_ask = ob.get("orderbook", {}).get("yes", [[]])[0][0] if ob.get("orderbook", {}).get("yes") else "N/A"
        print(f"  Current yes ask: {yes_ask}¢")
    except:
        pass
    
    order = place_order(trade["ticker"], trade["side"], trade["price_cents"], trade["count"])
    
    record = {
        "timestamp": now,
        "source": "beatrelease.com",
        "post": trade["post"],
        "ticker": trade["ticker"],
        "side": trade["side"],
        "price_cents": trade["price_cents"],
        "count": trade["count"],
        "reasoning": trade["reasoning"],
        "order_id": order.get("order_id", "error"),
        "status": order.get("status", "error"),
    }
    results.append(record)
    print()

# === Update trades file ===
existing_trades = json.loads(TRADES_PATH.read_text()) if TRADES_PATH.exists() else []
existing_trades.extend(results)
TRADES_PATH.write_text(json.dumps(existing_trades, indent=2))
print(f"Appended {len(results)} trades to {TRADES_PATH}")

# === Update state file ===
state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"seen_urls": [], "posts_scanned": []}
new_urls = [
    "https://www.beatrelease.com/post/why-2026-could-be-jack-harlow-s-biggest-year-yet-early-kalshi-market-odds",
    "https://www.beatrelease.com/post/kalshi-bts-pure-album-sales-history-prediction-market-breakdown-for-arirang",
]
for url in new_urls:
    if url not in [p.get("url") for p in state.get("posts_scanned", [])]:
        state["posts_scanned"].append({
            "url": url,
            "scanned_at": now,
            "strategy": "YES ladder" if "harlow" in url else "YES 240K+ and 280K+",
        })

state["last_scan"] = now
STATE_PATH.write_text(json.dumps(state, indent=2))
print(f"Updated state file with new URLs")

print("\n=== Summary ===")
for r in results:
    status_str = r["status"]
    print(f"  {r['side'].upper()} {r['ticker']} x{r['count']} @ {r['price_cents']}¢ → {status_str} (order: {r['order_id'][:12]}...)")
