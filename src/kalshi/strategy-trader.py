#!/usr/bin/env python3
"""Kalshi Strategy Trader — Applies research-backed strategies to find and place demo trades.
Strategies: Longshot bias selling, maker-only limit orders, info arbitrage near settlement.
"""

import json, time, base64, datetime, os, sys, math
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
MAX_BET = 500  # cents ($5)
BANKROLL = 49600  # cents (~$496 from logs)

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

def get_all_markets():
    all_m = []
    cursor = None
    for _ in range(50):
        path = "/markets?status=open&limit=1000"
        if cursor: path += f"&cursor={cursor}"
        try:
            data = api("GET", path)
        except Exception as e:
            print(f"  Page error: {e}")
            break
        batch = data.get("markets", [])
        all_m.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch: break
    return all_m

def half_kelly(edge, price_cents):
    """Half-Kelly sizing. Returns number of contracts (capped at MAX_BET risk)."""
    if edge <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p_true = (price_cents / 100.0) - edge  # true prob of event (we're selling, so lower)
    # For selling YES at price p: we risk (100-p) to win p
    # Kelly f = (edge * 100) / (100 - price_cents) ... simplified
    # Actually: selling YES at price p means we get p cents now, risk paying 100 if event happens
    # EV of sell YES = p * (1 - p_event) - (100 - p) * p_event ... but we want kelly fraction
    # Simpler: treat as a bet where we win `price_cents` with prob (1-p_true) and lose (100-price_cents) with prob p_true
    win_prob = 1 - p_true
    b = price_cents / (100 - price_cents)  # odds ratio
    kelly_f = (b * win_prob - (1 - win_prob)) / b
    half_f = kelly_f / 2
    if half_f <= 0:
        return 0
    # Max risk per contract when selling YES = (100 - price_cents) cents
    risk_per = 100 - price_cents
    max_contracts_kelly = max(1, int((half_f * BANKROLL) / risk_per))
    max_contracts_cap = max(1, MAX_BET // risk_per)
    return min(max_contracts_kelly, max_contracts_cap)

def find_longshot_sells(markets):
    """Find contracts priced <10¢ YES to SELL (exploit longshot bias)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for m in markets:
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        volume = m.get("volume", 0)
        ticker = m.get("ticker", "")
        
        # We want to SELL YES on longshots. We need yes_bid > 0 to sell into, or place a limit sell.
        # Longshot = yes_ask < 10 cents (market thinks <10% chance)
        # We'll place a limit order to sell YES at the ask or slightly above bid
        if yes_ask <= 0 or yes_ask > 15:
            continue
        
        # Calculate hours to close
        close_str = m.get("close_time", "")
        try:
            close_time = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            hours = (close_time - now).total_seconds() / 3600
        except:
            hours = 999
        
        if hours < 0.5:  # too close to settlement, risky
            continue
        
        # Estimated edge: from Becker 2025, 1¢ contracts have ~57% mispricing,
        # scaling down: at 5¢ ~30%, at 10¢ ~15%
        est_edge = max(0, 0.57 * math.exp(-0.15 * yes_ask))
        
        if est_edge < 0.03:
            continue
            
        sell_price = max(yes_bid, yes_ask - 1) if yes_bid > 0 else yes_ask
        if sell_price <= 1:
            continue
            
        contracts = half_kelly(est_edge, sell_price)
        if contracts <= 0:
            continue
        
        risk = contracts * (100 - sell_price)
        
        candidates.append({
            "ticker": ticker,
            "title": m.get("title", "")[:80],
            "subtitle": m.get("subtitle", "")[:60],
            "strategy": "longshot_sell",
            "side": "no",  # selling YES = buying NO
            "action": "buy",
            "price": 100 - sell_price,  # NO price = 100 - YES price
            "yes_price": sell_price,
            "contracts": contracts,
            "est_edge": est_edge,
            "risk_cents": risk,
            "hours_to_close": hours,
            "volume": volume,
            "reasoning": f"Longshot bias: YES@{sell_price}¢ implies {sell_price}% prob, Becker model est true prob ~{sell_price - est_edge*100:.1f}%. Sell YES (buy NO@{100-sell_price}¢) for ~{est_edge*100:.1f}% edge."
        })
    
    # Sort by edge * volume (prefer liquid + high edge)
    candidates.sort(key=lambda x: -x["est_edge"] * max(1, x["volume"]))
    return candidates

def find_near_settlement(markets):
    """Find markets settling within 6 hours where we might have info edge."""
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for m in markets:
        close_str = m.get("close_time", "")
        try:
            close_time = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            hours = (close_time - now).total_seconds() / 3600
        except:
            continue
        
        if hours < 0.5 or hours > 6:
            continue
        
        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        if yes_ask <= 0:
            continue
        
        spread = yes_ask - yes_bid if yes_bid > 0 else 100
        if spread > 20:  # too wide, no real price discovery
            continue
        
        # Near settlement with decent spread = potential info arb
        # We can't evaluate the actual info here, but flag them
        candidates.append({
            "ticker": m.get("ticker", ""),
            "title": m.get("title", "")[:80],
            "subtitle": m.get("subtitle", "")[:60],
            "yes_ask": yes_ask,
            "yes_bid": yes_bid,
            "spread": spread,
            "hours_to_close": hours,
            "volume": m.get("volume", 0),
        })
    
    candidates.sort(key=lambda x: x["hours_to_close"])
    return candidates

def check_settled_trades():
    """Check if any previous trades have settled."""
    log_path = DATA_DIR / "kalshi-trade-performance.md"
    settled = []
    try:
        positions = api("GET", "/portfolio/positions")
        for p in positions.get("market_positions", []):
            if p.get("settlement_status") == "settled":
                settled.append(p)
        
        # Also check portfolio settlements
        try:
            settlements = api("GET", "/portfolio/settlements")
            return settlements.get("settlements", [])
        except:
            pass
    except Exception as e:
        print(f"  Error checking settlements: {e}")
    return settled

def main():
    print("=" * 70)
    print("🎯 KALSHI STRATEGY TRADER")
    print(f"   {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Balance
    bal = api("GET", "/portfolio/balance")
    balance = bal.get("balance", 0)
    avail = bal.get("available_balance", balance)
    print(f"\n💰 Balance: ${balance/100:.2f} | Available: ${avail/100:.2f}")
    
    if avail < 100:
        print("⚠️  Low available balance — existing positions may be tying up capital")
    
    # Existing positions
    print("\n📊 Current Positions:")
    try:
        pos = api("GET", "/portfolio/positions")
        positions = pos.get("market_positions", [])
        if positions:
            for p in positions[:10]:
                t = p.get("ticker", "")
                yes_q = p.get("position", 0)
                cost = p.get("market_exposure", 0)
                print(f"  • {t}: {yes_q} contracts, exposure: {cost}¢")
        else:
            print("  (none)")
    except Exception as e:
        print(f"  Error: {e}")
    
    # Check settlements
    print("\n📜 Checking Settled Trades:")
    settled = check_settled_trades()
    if settled:
        for s in settled[:5]:
            print(f"  • {s}")
    else:
        print("  No settled trades found")
    
    # Fetch markets
    print("\n🔍 Scanning all open markets...")
    markets = get_all_markets()
    print(f"  Found {len(markets)} open markets")
    
    # Strategy 1: Longshot bias selling
    print("\n" + "=" * 70)
    print("📈 STRATEGY 1: Longshot Bias Exploitation (Sell YES on low-prob events)")
    print("=" * 70)
    longshots = find_longshot_sells(markets)
    print(f"  Found {len(longshots)} longshot sell candidates")
    for i, c in enumerate(longshots[:10]):
        print(f"\n  {i+1}. {c['ticker']}")
        print(f"     {c['title']}")
        if c['subtitle']: print(f"     {c['subtitle']}")
        print(f"     YES@{c['yes_price']}¢ | Edge: {c['est_edge']*100:.1f}% | Contracts: {c['contracts']} | Risk: ${c['risk_cents']/100:.2f}")
        print(f"     Closes in {c['hours_to_close']:.1f}h | Vol: {c['volume']}")
    
    # Strategy 2: Near-settlement info arb candidates
    print("\n" + "=" * 70)
    print("📈 STRATEGY 2: Near-Settlement Markets (Info Arbitrage Candidates)")
    print("=" * 70)
    near_settle = find_near_settlement(markets)
    print(f"  Found {len(near_settle)} markets settling within 6h with reasonable spreads")
    for i, c in enumerate(near_settle[:10]):
        print(f"  {i+1}. {c['ticker']} — {c['title']}")
        print(f"     Bid: {c['yes_bid']}¢ / Ask: {c['yes_ask']}¢ | Spread: {c['spread']}¢ | Close: {c['hours_to_close']:.1f}h")
    
    # Place trades — top 5 longshot sells
    print("\n" + "=" * 70)
    print("💰 PLACING TRADES (Top 5 Longshot Sells)")
    print("=" * 70)
    
    trades_executed = []
    for c in longshots[:5]:
        ticker = c["ticker"]
        # Buy NO = equivalent to selling YES
        # NO price = 100 - YES price
        no_price = 100 - c["yes_price"]
        contracts = c["contracts"]
        
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": "no",
            "type": "limit",
            "count": contracts,
            "no_price": no_price,
        }
        
        print(f"\n  📤 BUY {contracts}x NO @ {no_price}¢ on {ticker}")
        print(f"     ({c['reasoning']})")
        
        try:
            result = api("POST", "/portfolio/orders", body)
            order = result.get("order", {})
            status = order.get("status", "unknown")
            oid = order.get("order_id", "?")
            print(f"  ✅ Order {oid}: {status}")
            
            trades_executed.append({
                "timestamp": datetime.datetime.now().isoformat(),
                "ticker": ticker,
                "title": c["title"],
                "subtitle": c.get("subtitle", ""),
                "strategy": "longshot_sell",
                "direction": f"BUY NO (= SELL YES)",
                "no_price": no_price,
                "yes_price_at_entry": c["yes_price"],
                "contracts": contracts,
                "risk_cents": c["risk_cents"],
                "est_edge": f"{c['est_edge']*100:.1f}%",
                "reasoning": c["reasoning"],
                "order_id": oid,
                "status": status,
            })
        except requests.exceptions.HTTPError as e:
            err = e.response.text[:300] if hasattr(e, 'response') else str(e)
            print(f"  ❌ Failed: {err}")
            trades_executed.append({
                "timestamp": datetime.datetime.now().isoformat(),
                "ticker": ticker,
                "title": c["title"],
                "strategy": "longshot_sell",
                "direction": f"BUY NO @ {no_price}¢",
                "status": f"FAILED: {err[:100]}",
            })
        except Exception as e:
            print(f"  ❌ Failed: {e}")
    
    # Final balance
    bal = api("GET", "/portfolio/balance")
    print(f"\n💰 Final Balance: ${bal.get('balance', 0)/100:.2f} | Available: ${bal.get('available_balance', 0)/100:.2f}")
    
    # Write performance log
    print("\n📝 Writing trade log...")
    log_path = DATA_DIR / "kalshi-trade-performance.md"
    
    existing = ""
    if log_path.exists():
        existing = log_path.read_text()
    
    new_section = f"\n\n## Trade Session: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    new_section += f"**Balance**: ${bal.get('balance', 0)/100:.2f} | **Available**: ${bal.get('available_balance', 0)/100:.2f}\n\n"
    new_section += f"**Markets Scanned**: {len(markets)} | **Longshot Candidates**: {len(longshots)} | **Near-Settlement**: {len(near_settle)}\n\n"
    
    if trades_executed:
        new_section += "### Trades Placed\n\n"
        new_section += "| # | Ticker | Direction | Price | Qty | Edge | Risk | Status | Reasoning |\n"
        new_section += "|---|--------|-----------|-------|-----|------|------|--------|----------|\n"
        for i, t in enumerate(trades_executed):
            new_section += f"| {i+1} | `{t['ticker'][:25]}` | {t['direction'][:20]} | {t.get('no_price', '?')}¢ | {t.get('contracts', '?')} | {t.get('est_edge', '?')} | ${t.get('risk_cents', 0)/100:.2f} | {t['status']} | {t.get('reasoning', '')[:60]} |\n"
    else:
        new_section += "### No trades placed this session\n"
    
    if settled:
        new_section += "\n### Settled Trades\n\n"
        for s in settled:
            new_section += f"- {s}\n"
    
    if not existing:
        existing = "# Kalshi Trade Performance Log\n\nAutomated trading performance tracking.\n"
    
    log_path.write_text(existing + new_section)
    print(f"  ✅ Logged to {log_path}")
    
    # Also save raw JSON
    json_path = DATA_DIR / "kalshi-strategy-trades.json"
    json_data = []
    if json_path.exists():
        try: json_data = json.loads(json_path.read_text())
        except: pass
    json_data.extend(trades_executed)
    json_path.write_text(json.dumps(json_data, indent=2))
    
    print(f"\n{'='*70}")
    print(f"✅ STRATEGY TRADER COMPLETE — {len(trades_executed)} trades placed")
    print(f"{'='*70}")

if __name__ == "__main__":
    main()
