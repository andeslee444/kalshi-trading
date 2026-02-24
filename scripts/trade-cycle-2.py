#!/usr/bin/env python3
"""Kalshi Demo Trade Cycle #2 — Check settlements, scan opportunities, place trades."""

import json, datetime, sys
import requests
from pathlib import Path

# ─── Shared auth module ───
# The project is not an installable package, so we add src/kalshi/ to
# sys.path directly so that ``from kalshi_auth import ...`` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))
from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered, setup_logging, PROJECT_DIR, TradeManager, trim_trade_log
from probability import half_kelly_sell, longshot_edge, kalshi_fee_cents

setup_unbuffered()
log = setup_logging("trade-cycle")

TRADES_JSON = PROJECT_DIR / "data" / "kalshi-strategy-trades.json"
PERF_MD = PROJECT_DIR / "data" / "kalshi-trade-performance.md"

client = KalshiClient()
trade_manager = TradeManager(client, TRADES_JSON, {
    "maxTradeAmount": 10,
    "maxDailyTrades": 10,
    "maxDailyLoss": 25,
}, logger=log)
trim_trade_log(TRADES_JSON)

# ─── 1. Check balance ───
now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M EST")
print("=" * 60)
print(f"KALSHI DEMO TRADE CYCLE #2 — {now_str}")
print("=" * 60)

bal = client.get("/portfolio/balance")
balance_cents = bal.get("balance", 0)
print(f"\n💰 Balance: ${balance_cents/100:.2f}")

# ─── 2. Check positions ───
print("\n📊 Current Positions:")
try:
    pos = client.get("/portfolio/positions")
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
    sett = client.get("/portfolio/settlements?limit=20")
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

# Dynamic date check: today and tomorrow
today = datetime.date.today()
tomorrow = today + datetime.timedelta(days=1)
near_dates = {today.isoformat(), tomorrow.isoformat()}

# Scan all open markets for longshots (YES <= 5c) and near-settlement
all_longshots = []
near_settlement = []
cursor = None
total_scanned = 0

for page in range(80):
    path = "/markets?status=open&limit=1000"
    if cursor:
        path += f"&cursor={cursor}"
    try:
        data = client.get(path)
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

        # Longshots: YES <= 5c with asks available
        if yes_ask and 1 <= yes_ask <= 5:
            all_longshots.append(m)

        # Near settlement: closes today or tomorrow
        if close_time and close_time[:10] in near_dates:
            if yes_ask and yes_ask > 0:
                near_settlement.append(m)

    cursor = data.get("cursor")
    if not cursor or not batch:
        break

print(f"  Scanned {total_scanned} markets across {page+1} pages")
print(f"  Found {len(all_longshots)} longshot opportunities (YES <= 5c)")
print(f"  Found {len(near_settlement)} near-settlement markets")

# ─── 5. Score and select best trades ───
# Strategy: sell longshots (buy NO) — exploit favourite-longshot bias
# For near-settlement, look for weather/entertainment with info edge

# Existing tickers to avoid duplicates
existing_tickers = set()
try:
    existing = load_trades(TRADES_JSON)
    for t in existing:
        existing_tickers.add(t.get("ticker", ""))
except (json.JSONDecodeError, ValueError):
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

    # Rec 5: Only sell longshots when NO ≤ 96c (profit/risk ratio floor)
    if not no_ask or no_ask > 96:
        continue

    # Category-adjusted Becker model: returns additive edge (implied - true)
    est_edge = longshot_edge(yes_ask, ticker=ticker)
    fee_per_contract = kalshi_fee_cents(yes_ask)
    fee_as_edge = fee_per_contract / 100
    min_edge = fee_as_edge + 0.005
    if est_edge < min_edge:
        continue

    # Half-Kelly sizing with actual bankroll
    contracts, risk_c = half_kelly_sell(est_edge, yes_ask, 500, bankroll_cents=balance_cents)
    if contracts < 1:
        contracts = min(500 // no_ask, 5)
    if contracts < 1:
        continue

    implied_prob = yes_ask / 100
    est_true_prob = max(0.001, implied_prob - est_edge)
    edge = est_edge

    reasoning = f"Longshot bias: YES@{yes_ask}c implies {implied_prob*100:.0f}% prob, Becker model est true prob ~{est_true_prob*100:.1f}%. Sell YES (buy NO@{no_ask}c) for ~{edge*100:.1f}% edge."

    try:
        result = trade_manager.place_order(ticker, "no", no_ask, contracts, reasoning,
                                            strategy="longshot_sell", raw_edge=round(edge, 4))
        if result:
            print(f"  #{trade_count+1} SELL LONGSHOT: {ticker} — BUY {contracts}x NO@{no_ask}c — {title}")
            new_trades.append({
                "timestamp": datetime.datetime.now().isoformat(),
                "ticker": ticker, "title": title, "subtitle": subtitle,
                "strategy": "longshot_sell", "direction": "BUY NO (= SELL YES)",
                "no_price": no_ask, "yes_price_at_entry": yes_ask,
                "contracts": contracts, "risk_cents": contracts * no_ask,
                "est_edge": f"{edge*100:.1f}%", "reasoning": reasoning,
            })
            existing_tickers.add(ticker)
            trade_count += 1
        else:
            print(f"  TradeManager rejected {ticker} (check risk limits)")
    except Exception as e:
        print(f"  Failed {ticker}: {e}")

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
        reasoning = f"Near settlement: YES@{yes_ask}c suggests likely NO. Buy NO@{no_ask}c for quick resolution."
        try:
            result = trade_manager.place_order(ticker, "no", no_ask, contracts, reasoning,
                                                strategy="near_settlement")
            if result:
                print(f"  #{trade_count+1} NEAR-SETTLE: {ticker} — BUY {contracts}x NO@{no_ask}c — {title}")
                new_trades.append({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "ticker": ticker, "title": title, "subtitle": subtitle,
                    "strategy": "near_settlement", "direction": "BUY NO",
                    "no_price": no_ask, "yes_price_at_entry": yes_ask,
                    "contracts": contracts, "risk_cents": contracts * no_ask,
                    "est_edge": "near-settlement lean", "reasoning": reasoning,
                })
                existing_tickers.add(ticker)
                trade_count += 1
        except Exception as e:
            print(f"  Failed {ticker}: {e}")

    elif yes_ask and 90 <= yes_ask <= 99 and no_ask and 1 <= no_ask <= 10:
        # Likely YES outcome — buy YES
        contracts = min(500 // yes_ask, 5)
        if contracts < 1:
            continue
        reasoning = f"Near settlement: YES@{yes_ask}c suggests likely YES. Buy YES@{yes_ask}c for quick resolution."
        try:
            result = trade_manager.place_order(ticker, "yes", yes_ask, contracts, reasoning,
                                                strategy="near_settlement")
            if result:
                print(f"  #{trade_count+1} NEAR-SETTLE: {ticker} — BUY {contracts}x YES@{yes_ask}c — {title}")
                new_trades.append({
                    "timestamp": datetime.datetime.now().isoformat(),
                    "ticker": ticker, "title": title, "subtitle": subtitle,
                    "strategy": "near_settlement", "direction": "BUY YES",
                    "yes_price": yes_ask, "no_price_at_entry": no_ask,
                    "contracts": contracts, "risk_cents": contracts * yes_ask,
                    "est_edge": "near-settlement lean", "reasoning": reasoning,
                })
                existing_tickers.add(ticker)
                trade_count += 1
        except Exception as e:
            print(f"  Failed {ticker}: {e}")

print(f"\n📊 Summary: Placed {trade_count} new trades")

# ─── 7. Trade log ───
# TradeManager already writes trades to TRADES_JSON atomically.
# No manual file write needed.
print(f"Trades logged to {TRADES_JSON} via TradeManager")

# ─── 8. Update performance markdown ───
# Re-check balance after trades
bal2 = client.get("/portfolio/balance")
new_balance = bal2.get("balance", 0)

perf_entry = f"""

## Trade Session: {now_str} (Cycle #2)

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
    perf_entry += f"| {i+1} | `{t['ticker']}` | {direction} | {price}c | {t['contracts']} | {t['est_edge']} | ${t['risk_cents']/100:.2f} | placed | {t['reasoning'][:60]}... |\n"

if not new_trades:
    perf_entry += "| — | No new trades placed | — | — | — | — | — | — | — |\n"

# Append to performance file
PERF_MD.parent.mkdir(parents=True, exist_ok=True)
with open(PERF_MD, "a") as f:
    f.write(perf_entry)
print(f"✅ Updated {PERF_MD}")

print("\n✅ Trade cycle #2 complete!")
