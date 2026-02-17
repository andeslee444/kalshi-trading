#!/usr/bin/env python3
"""Kalshi Trade Cycle #6 - Check settlements & place new trades"""

import json, time, base64, datetime, sys, re
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
STRATEGY_TRADES_PATH = PROJECT_DIR / "data" / "kalshi-strategy-trades.json"
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
    r = requests.get(url, headers=h, timeout=15) if method == "GET" else requests.post(url, headers=h, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def api_delete(path):
    url = BASE_URL + path
    h = get_headers("DELETE", "/trade-api/v2" + path)
    r = requests.delete(url, headers=h, timeout=15)
    r.raise_for_status()
    return r.json()

print("=" * 60)
print("KALSHI TRADE CYCLE #6")
print("=" * 60)

# 1. Balance
bal = api("GET", "/portfolio/balance")
balance_cents = bal.get("balance", 0)
print(f"\n💰 Balance: ${balance_cents/100:.2f}")

# 2. Check all positions
print("\n📊 Current Positions:")
try:
    positions = api("GET", "/portfolio/positions?limit=200")
    pos_list = positions.get("market_positions", []) or positions.get("positions", [])
    if not pos_list:
        # Try event_positions
        pos_list = positions.get("event_positions", [])
    settled_positions = []
    open_positions = []
    for p in pos_list:
        ticker = p.get("ticker", p.get("market_ticker", ""))
        yes_count = p.get("market_exposure", p.get("yes", 0))
        no_count = p.get("no", 0)
        # Try different field names
        print(f"  Position data: {json.dumps(p)[:200]}")
except Exception as e:
    print(f"  Error fetching positions: {e}")
    pos_list = []

# 3. Check settlements for our trades
print("\n📋 Checking settlements for recorded trades:")
trades = json.loads(STRATEGY_TRADES_PATH.read_text())

# Weather trades that should be settled (Feb 16 markets)
weather_feb16 = [t for t in trades if "FEB16" in t.get("ticker", "") and "KXHIGH" in t.get("ticker", "")]
print(f"\nFeb 16 weather trades to check: {len(weather_feb16)}")

total_pnl = 0
settled_count = 0
win_count = 0

for t in weather_feb16:
    ticker = t["ticker"]
    order_id = t.get("order_id", "")
    direction = t.get("direction", "")
    
    # Check order status
    try:
        order = api("GET", f"/portfolio/orders/{order_id}")
        o = order.get("order", order)
        status = o.get("status", "unknown")
        
        # Check if market settled
        try:
            market = api("GET", f"/markets/{ticker}")
            m = market.get("market", market)
            market_result = m.get("result", "unknown")
            market_status = m.get("status", "unknown")
            
            print(f"\n  {ticker}:")
            print(f"    Order status: {status}, Market status: {market_status}, Result: {market_result}")
            print(f"    Our direction: {direction}")
            
            if market_status in ("settled", "closed", "finalized"):
                settled_count += 1
                # Calculate P&L
                if "BUY NO" in direction:
                    no_price = t.get("no_price", 0)
                    contracts = t.get("contracts", 5)
                    cost = no_price * contracts
                    if market_result == "no":
                        payout = 100 * contracts
                        pnl = payout - cost
                        win_count += 1
                        print(f"    ✅ WIN: paid {cost}¢, received {payout}¢, P&L: +{pnl}¢")
                    elif market_result == "yes":
                        pnl = -cost
                        print(f"    ❌ LOSS: paid {cost}¢, received 0¢, P&L: {pnl}¢")
                    else:
                        pnl = 0
                        print(f"    ⏳ Result: {market_result}")
                    total_pnl += pnl
                elif "BUY YES" in direction:
                    yes_price = t.get("yes_price", 0)
                    contracts = t.get("contracts", 5)
                    cost = yes_price * contracts
                    if market_result == "yes":
                        payout = 100 * contracts
                        pnl = payout - cost
                        win_count += 1
                        print(f"    ✅ WIN: paid {cost}¢, received {payout}¢, P&L: +{pnl}¢")
                    elif market_result == "no":
                        pnl = -cost
                        print(f"    ❌ LOSS: paid {cost}¢, received 0¢, P&L: {pnl}¢")
                    else:
                        pnl = 0
                        print(f"    ⏳ Result: {market_result}")
                    total_pnl += pnl
            else:
                print(f"    ⏳ Not yet settled")
        except Exception as e:
            print(f"    Market check error: {e}")
    except Exception as e:
        print(f"  Order {order_id[:8]}... error: {e}")

print(f"\n{'='*40}")
print(f"Feb 16 Weather Settlement Summary:")
print(f"  Settled: {settled_count}/{len(weather_feb16)}")
print(f"  Wins: {win_count}, Losses: {settled_count - win_count}")
print(f"  Total P&L: {'+' if total_pnl >= 0 else ''}{total_pnl}¢ (${total_pnl/100:.2f})")

# Also check ALL trades for settlements
print(f"\n📋 Checking ALL {len(trades)} recorded trades:")
all_pnl = 0
all_settled = 0
all_wins = 0
all_still_open = 0

for t in trades:
    ticker = t["ticker"]
    order_id = t.get("order_id", "")
    direction = t.get("direction", "")
    
    try:
        market = api("GET", f"/markets/{ticker}")
        m = market.get("market", market)
        market_result = m.get("result", "unknown")
        market_status = m.get("status", "unknown")
        
        if market_status in ("settled", "closed", "finalized"):
            all_settled += 1
            if "BUY NO" in direction:
                no_price = t.get("no_price", 0)
                contracts = t.get("contracts", 5)
                cost = no_price * contracts
                if market_result == "no":
                    pnl = (100 * contracts) - cost
                    all_wins += 1
                elif market_result == "yes":
                    pnl = -cost
                else:
                    pnl = 0
                all_pnl += pnl
            elif "BUY YES" in direction:
                yes_price = t.get("yes_price", 0)
                contracts = t.get("contracts", 5)
                cost = yes_price * contracts
                if market_result == "yes":
                    pnl = (100 * contracts) - cost
                    all_wins += 1
                elif market_result == "no":
                    pnl = -cost
                else:
                    pnl = 0
                all_pnl += pnl
            print(f"  ✓ {ticker}: {market_status} → {market_result}")
        else:
            all_still_open += 1
            print(f"  ○ {ticker}: {market_status}")
    except Exception as e:
        print(f"  ? {ticker}: error {e}")

print(f"\nOverall Settlement Summary:")
print(f"  Settled: {all_settled}/{len(trades)}, Still open: {all_still_open}")
print(f"  Wins: {all_wins}, Losses: {all_settled - all_wins}")
print(f"  Win rate: {all_wins/all_settled*100:.1f}%" if all_settled > 0 else "  Win rate: N/A")
print(f"  Total P&L: {'+' if all_pnl >= 0 else ''}{all_pnl}¢ (${all_pnl/100:.2f})")

# 4. Find new markets and place trades
print(f"\n{'='*60}")
print("🔍 Finding new trade opportunities...")

# Get weather forecasts
def get_forecast(lat, lon):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=7"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    d = r.json()["daily"]
    return dict(zip(d["time"], d["temperature_2m_max"]))

# Scan markets for opportunities
existing_tickers = {t["ticker"] for t in trades}
cursor = None
all_markets = []
for page in range(20):
    path = "/markets?status=open&limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    try:
        data = api("GET", path)
    except:
        break
    batch = data.get("markets", [])
    all_markets.extend(batch)
    cursor = data.get("cursor")
    if not cursor or not batch:
        break

print(f"  Found {len(all_markets)} open markets total")

# Strategy: find good limit order opportunities
# Look for weather markets, longshots, near-settlement
opportunities = []

for m in all_markets:
    ticker = m.get("ticker", "")
    title = m.get("title", "")
    yes_ask = m.get("yes_ask", 0) or 0
    yes_bid = m.get("yes_bid", 0) or 0
    no_ask = m.get("no_ask", 0) or 0
    no_bid = m.get("no_bid", 0) or 0
    
    if ticker in existing_tickers:
        continue
    
    # Strategy 1: Longshot sell (YES <= 3¢, buy NO)
    if yes_ask and 1 <= yes_ask <= 3 and no_ask and no_ask <= 99:
        score = 20 - yes_ask  # lower YES = better
        opportunities.append({
            "ticker": ticker, "title": title,
            "strategy": "longshot_sell", "direction": "BUY NO",
            "no_price": no_ask, "yes_price_at_entry": yes_ask,
            "score": score, "reasoning": f"Longshot bias: YES@{yes_ask}¢ → sell YES (buy NO@{no_ask}¢)"
        })
    
    # Strategy 2: Near-settlement weather (KXHIGH with extreme prices)
    if "KXHIGH" in ticker:
        if yes_ask and yes_ask >= 95 and yes_ask <= 99:
            opportunities.append({
                "ticker": ticker, "title": title,
                "strategy": "weather_lean", "direction": "BUY YES",
                "yes_price": yes_ask, "no_price_at_entry": 100 - yes_ask,
                "score": 15, "reasoning": f"Weather lean YES: YES@{yes_ask}¢"
            })
        elif yes_ask and 1 <= yes_ask <= 5 and no_ask:
            opportunities.append({
                "ticker": ticker, "title": title,
                "strategy": "weather_lean", "direction": "BUY NO",
                "no_price": no_ask, "yes_price_at_entry": yes_ask,
                "score": 15, "reasoning": f"Weather lean NO: YES@{yes_ask}¢ → buy NO@{no_ask}¢"
            })

# Sort by score, pick top 5
opportunities.sort(key=lambda x: x["score"], reverse=True)
to_trade = opportunities[:5]

print(f"  Found {len(opportunities)} opportunities, placing top {len(to_trade)}")

new_trades = []
for opp in to_trade:
    ticker = opp["ticker"]
    direction = opp["direction"]
    contracts = 5
    
    if direction == "BUY NO":
        price = opp["no_price"]
        cost = price * contracts
        if cost > 500:  # $5 max
            contracts = 500 // price
        if contracts < 1:
            continue
        order_body = {"ticker": ticker, "action": "buy", "side": "no", "type": "limit", "count": contracts, "no_price": price}
    else:
        price = opp["yes_price"]
        cost = price * contracts
        if cost > 500:
            contracts = 500 // price
        if contracts < 1:
            continue
        order_body = {"ticker": ticker, "action": "buy", "side": "yes", "type": "limit", "count": contracts, "yes_price": price}
    
    try:
        result = api("POST", "/portfolio/orders", order_body)
        o = result.get("order", {})
        status = o.get("status", "unknown")
        oid = o.get("order_id", "unknown")
        print(f"\n  ✅ {ticker}: {direction} {contracts}x @ {price}¢ → {status}")
        
        trade_record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": ticker,
            "title": opp["title"],
            "strategy": opp["strategy"],
            "direction": direction,
            "order_id": oid,
            "status": status,
            "contracts": contracts,
            "reasoning": opp["reasoning"],
        }
        if direction == "BUY NO":
            trade_record["no_price"] = price
            trade_record["yes_price_at_entry"] = opp.get("yes_price_at_entry", 0)
            trade_record["risk_cents"] = price * contracts
        else:
            trade_record["yes_price"] = price
            trade_record["no_price_at_entry"] = opp.get("no_price_at_entry", 0)
            trade_record["risk_cents"] = price * contracts
        
        new_trades.append(trade_record)
    except Exception as e:
        print(f"  ❌ {ticker}: {e}")

# 5. Update data file
if new_trades:
    trades.extend(new_trades)
    STRATEGY_TRADES_PATH.write_text(json.dumps(trades, indent=2))
    print(f"\n✅ Saved {len(new_trades)} new trades to strategy file")

# 6. Final balance
bal2 = api("GET", "/portfolio/balance")
print(f"\n{'='*60}")
print(f"CYCLE #6 SUMMARY")
print(f"{'='*60}")
print(f"Balance: ${bal2.get('balance',0)/100:.2f}")
print(f"Settlements: {all_settled} settled, {all_still_open} open")
print(f"Win rate: {all_wins}/{all_settled} ({all_wins/all_settled*100:.1f}%)" if all_settled > 0 else "Win rate: N/A")
print(f"Total realized P&L: ${all_pnl/100:.2f}")
print(f"New trades placed: {len(new_trades)}")
for nt in new_trades:
    print(f"  • {nt['ticker']}: {nt['direction']} {nt['contracts']}x @ {nt.get('no_price', nt.get('yes_price', 0))}¢ [{nt['status']}]")
