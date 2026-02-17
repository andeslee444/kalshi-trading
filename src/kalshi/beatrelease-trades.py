#!/usr/bin/env python3
"""Place BeatRelease.com copy-trades on Kalshi demo API."""

import json, time, base64, datetime, sys, os
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
    headers = get_headers(method, "/trade-api/v2" + path)
    r = requests.request(method, url, headers=headers, json=body if method == "POST" else None, timeout=15)
    r.raise_for_status()
    return r.json()

def find_markets(ticker_prefix):
    """Find all open markets matching a ticker prefix."""
    markets = []
    cursor = None
    for _ in range(20):
        path = f"/markets?status=open&limit=1000&ticker={ticker_prefix}"
        if cursor:
            path += f"&cursor={cursor}"
        data = api("GET", path)
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break
    return markets

def get_market(ticker):
    """Get a single market by ticker."""
    try:
        data = api("GET", f"/markets/{ticker}")
        return data.get("market", data)
    except:
        return None

def place_order(ticker, side, price_cents, count):
    """Place a limit order."""
    body = {"ticker": ticker, "action": "buy", "side": side, "type": "limit", "count": count}
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents
    result = api("POST", "/portfolio/orders", body)
    return result.get("order", {})

# Define trades based on blog posts
# Each: (post_slug, ticker, side, price_cents, count, reasoning)
TRADES_TO_PLACE = [
    # Kanye West - Bully: YES ≥100K pure sales at ~60¢
    {
        "post": "kanye-west-pure-album-sales-history-what-it-means-for-the-kalshi-prediction",
        "ticker": "KXALBUMSALES-BUL-100000",
        "side": "yes",
        "price_cents": 60,
        "max_spend_cents": 500,
        "reasoning": "Kanye YES ≥100K pure sales. Blog position at $59.78. Modern Kanye debuts: JIK 109K, Ye 85K, TLOP 94K. Market ~60% implied probability."
    },
    # Bruno Mars - The Romantic: YES ≥100K at ~77¢
    {
        "post": "kalshi-album-sales-prediction-actively-updating-bruno-mars-the-romantic-debut-sales-outlook",
        "ticker": "KXALBUMSALES-ROM-100000",
        "side": "yes",
        "price_cents": 77,
        "max_spend_cents": 300,
        "reasoning": "Bruno Mars YES ≥100K. Blog recommends as statistically safe bet. ~75-80% probability. Strong crossover appeal."
    },
    # Bruno Mars hedge: NO ≥175K
    {
        "post": "kalshi-album-sales-prediction-actively-updating-bruno-mars-the-romantic-debut-sales-outlook",
        "ticker": "KXALBUMSALES-ROM-175000",
        "side": "no",
        "price_cents": 70,
        "max_spend_cents": 200,
        "reasoning": "Bruno Mars NO ≥175K hedge. Blog recommends YES 100K + NO 175K spread. If sales 100K-174K both win."
    },
    # J. Cole - The Fall Off: CLOSED (already resolved) - skip
    # Bruno Mars additional: YES ≥125K for higher upside
    {
        "post": "kalshi-album-sales-prediction-actively-updating-bruno-mars-the-romantic-debut-sales-outlook",
        "ticker": "KXALBUMSALES-ROM-125000",
        "side": "yes",
        "price_cents": 70,
        "max_spend_cents": 200,
        "reasoning": "Bruno Mars YES ≥125K. Blog says debate is whether he lands 125K or pushes 150K+. ~70% implied at this level."
    },
]

def main():
    now = datetime.datetime.now().isoformat()
    print("=" * 60)
    print("BeatRelease Copy-Trade Bot")
    print("=" * 60)
    
    # Check balance
    bal = api("GET", "/portfolio/balance")
    print(f"Balance: ${bal.get('balance', 0)/100:.2f}")
    
    # Load existing trades
    existing_trades = []
    if TRADES_PATH.exists():
        existing_trades = json.loads(TRADES_PATH.read_text())
    
    # Load state
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    existing_posts = {p["url"].split("/")[-1] for p in state.get("posts_scanned", [])}
    
    placed = []
    errors = []
    
    for trade in TRADES_TO_PLACE:
        ticker = trade["ticker"]
        print(f"\n--- {ticker} ---")
        
        # Check if market exists
        market = get_market(ticker)
        if not market or not market.get("ticker"):
            # Try alternate ticker patterns
            print(f"  Market {ticker} not found, trying to search...")
            # Try WUT prefix for some
            alt_prefixes = []
            if "CLO" in ticker:
                alt_prefixes = ["WUT", "FAL"]  # Fall Off
            elif "BUL" in ticker:
                alt_prefixes = ["BUL", "WUT", "KAN"]
            
            found = False
            for prefix in alt_prefixes:
                alt_ticker = ticker.replace(ticker.split("-")[1], prefix)
                market = get_market(alt_ticker)
                if market and market.get("ticker"):
                    ticker = alt_ticker
                    print(f"  Found alternate: {ticker}")
                    found = True
                    break
            
            if not found:
                print(f"  ✗ Market not found for {ticker} (or alternates)")
                errors.append({"ticker": trade["ticker"], "error": "market not found"})
                continue
        
        print(f"  Market: {market.get('title', ticker)}")
        print(f"  Status: {market.get('status')}")
        
        if market.get("status") not in ("open", "active"):
            print(f"  ✗ Market not tradeable (status: {market.get('status')})")
            errors.append({"ticker": ticker, "error": f"not tradeable: {market.get('status')}"})
            continue
        
        # Calculate count based on max spend
        price = trade["price_cents"]
        count = max(1, trade["max_spend_cents"] // price)
        actual_cost = count * price
        
        # Cap at $5 total
        while count > 1 and count * price > 500:
            count -= 1
        
        print(f"  Placing: {count}x {trade['side']} @ {price}¢ (cost: ${count * price / 100:.2f})")
        print(f"  Reason: {trade['reasoning']}")
        
        try:
            order = place_order(ticker, trade["side"], price, count)
            order_id = order.get("order_id", "unknown")
            status = order.get("status", "?")
            print(f"  ✓ Order placed! ID: {order_id}, status: {status}")
            
            trade_record = {
                "timestamp": now,
                "source": "beatrelease.com",
                "post": trade["post"],
                "ticker": ticker,
                "side": trade["side"],
                "price_cents": price,
                "count": count,
                "reasoning": trade["reasoning"],
                "order_id": order_id,
                "status": status,
            }
            existing_trades.append(trade_record)
            placed.append(trade_record)
        except requests.exceptions.HTTPError as e:
            err_text = e.response.text[:300] if e.response else str(e)
            print(f"  ✗ Order failed: {e.response.status_code if e.response else '?'} {err_text}")
            errors.append({"ticker": ticker, "error": err_text})
        except Exception as e:
            print(f"  ✗ Order failed: {e}")
            errors.append({"ticker": ticker, "error": str(e)})
    
    # Save trades
    TRADES_PATH.write_text(json.dumps(existing_trades, indent=2))
    
    # Update state
    new_posts = [
        {
            "url": "https://www.beatrelease.com/post/kanye-west-pure-album-sales-history-what-it-means-for-the-kalshi-prediction",
            "title": "Kanye West Pure Album Sales History - Kalshi Prediction",
            "scanned_at": now,
            "strategy": "YES ≥100K pure album sales"
        },
        {
            "url": "https://www.beatrelease.com/post/kalshi-album-sales-prediction-actively-updating-bruno-mars-the-romantic-debut-sales-outlook",
            "title": "Bruno Mars The Romantic Debut Sales Outlook",
            "scanned_at": now,
            "strategy": "YES ≥100K + NO ≥175K spread"
        },
        {
            "url": "https://www.beatrelease.com/post/kalshi-album-sales-prediction-why-60k-pure-sales-looks-like-the-smart-floor-with-bigger-upsi",
            "title": "J. Cole The Fall Off - 60K Pure Sales Floor",
            "scanned_at": now,
            "strategy": "YES ≥80K core + YES ≥30K floor + NO ≥150K hedge"
        },
    ]
    state["posts_scanned"].extend(new_posts)
    state["last_scan"] = now
    state["trades_placed"] = state.get("trades_placed", 0) + len(placed)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    
    # Summary
    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(placed)} trades placed, {len(errors)} errors")
    for t in placed:
        print(f"  ✓ {t['ticker']} {t['side']} {t['count']}x @ {t['price_cents']}¢ → {t['order_id'][:12]}...")
    for e in errors:
        print(f"  ✗ {e['ticker']}: {e['error'][:80]}")
    print("=" * 60)

if __name__ == "__main__":
    main()
