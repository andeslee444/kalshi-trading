#!/usr/bin/env python3
"""Kalshi Demo Trader — Tests trading flow across all market categories.
Authenticates, lists markets, places test trades, verifies positions.
"""

import json, time, datetime, os, sys, uuid
import requests
from pathlib import Path
from app_bootstrap import AppContext, install_app_context
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, TradeManager, trim_trade_log

# === Config ===
DATA_DIR = PROJECT_DIR / "data"
TRADES_PATH = DATA_DIR / "demo-trades.json"

_APP_CONTEXT = None
log = None
client = None
trade_manager = None

# === Market Discovery ===
def get_all_markets(limit=200):
    """Get all open markets, paginating."""
    all_markets = []
    cursor = None
    for _ in range(10):
        path = f"/markets?status=open&limit={min(limit, 1000)}"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        markets = data.get("markets", [])
        all_markets.extend(markets)
        cursor = data.get("cursor")
        if not cursor or not markets or len(all_markets) >= limit:
            break
    return all_markets

def categorize_markets(markets):
    """Group markets by category/series."""
    categories = {}
    for m in markets:
        ticker = m.get("ticker", "")
        cat = m.get("category", "unknown")
        series = m.get("series_ticker", "")

        # Infer category from ticker prefix
        if "KXHIGH" in ticker or "KXLOW" in ticker:
            cat = "weather"
        elif "KXNCAA" in ticker or "KXNFL" in ticker or "KXNBA" in ticker or "KXNHL" in ticker:
            cat = "sports"
        elif "KXBTC" in ticker or "KXETH" in ticker or "KXCRYPTO" in ticker:
            cat = "crypto"
        elif "KXINX" in ticker or "KXINXY" in ticker or "KXWTI" in ticker:
            cat = "financials"
        elif "KXCPI" in ticker or "KXGDP" in ticker or "KXFED" in ticker or "KXJOBS" in ticker:
            cat = "economics"
        elif "KXGOVSHUT" in ticker:
            cat = "politics"
        elif "KXIPO" in ticker:
            cat = "companies"

        if cat not in categories:
            categories[cat] = []
        categories[cat].append(m)

    return categories

def find_tradeable_markets(markets, max_results=10):
    """Find markets with liquidity that are settling soon."""
    tradeable = []
    now = datetime.datetime.now(datetime.timezone.utc)

    for m in markets:
        ticker = m.get("ticker", "")
        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)
        no_bid = m.get("no_bid", 0)
        volume = m.get("volume", 0)

        # Need at least one side with prices
        has_liquidity = (yes_ask > 0 and yes_ask < 99) or (no_ask > 0 and no_ask < 99)
        if not has_liquidity:
            continue

        # Parse close time
        close_time_str = m.get("close_time", "")
        if close_time_str:
            try:
                close_time = datetime.datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                hours_to_close = (close_time - now).total_seconds() / 3600
            except (ValueError, TypeError):
                hours_to_close = 999
        else:
            hours_to_close = 999

        spread = (yes_ask - yes_bid) if (yes_ask and yes_bid) else 100

        tradeable.append({
            "ticker": ticker,
            "title": m.get("title", ""),
            "subtitle": m.get("subtitle", ""),
            "yes_ask": yes_ask,
            "yes_bid": yes_bid,
            "no_ask": no_ask,
            "no_bid": no_bid,
            "volume": volume,
            "hours_to_close": hours_to_close,
            "spread": spread,
            "category": m.get("category", ""),
            "market": m,
        })

    # Sort by hours to close, prefer ones closing soon with decent volume
    tradeable.sort(key=lambda x: (x["hours_to_close"], -x["volume"]))
    return tradeable[:max_results]

# === Trading ===
def place_trade(ticker, side="yes", action="buy", count=1, price=None):
    """Place a limit order via TradeManager (logged + risk-checked)."""
    reasoning = f"Demo trade: {action} {count}x {side} @ {price}c on {ticker}"
    log.info("Placing order: %s %dx %s @ %sc on %s", action, count, side, price, ticker)
    result = trade_manager.place_order(ticker, side, price, count, reasoning)
    if result:
        log.info("Order placed successfully via TradeManager")
    else:
        log.warning("TradeManager rejected order (check risk limits)")
    return result or {}

def get_positions():
    """Get current positions."""
    data = client.get("/portfolio/positions")
    return data.get("market_positions", [])

def get_orders():
    """Get current orders."""
    data = client.get("/portfolio/orders")
    return data.get("orders", [])

def get_balance():
    """Get account balance."""
    return client.get("/portfolio/balance")

# === Main ===
def build_app(project_dir=None):
    project_dir = Path(project_dir or PROJECT_DIR)
    setup_unbuffered()
    setup_signal_handlers()
    logger = setup_logging("demo-trader")
    data_dir = project_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    trades_path = data_dir / "demo-trades.json"
    client_obj = KalshiClient()
    trade_manager_obj = TradeManager(client_obj, trades_path, {
        "maxTradeAmount": 5,
        "maxDailyTrades": 10,
        "maxDailyLoss": 10,
    }, logger=logger)
    trim_trade_log(trades_path)
    return install_app_context(globals(), AppContext({
        "PROJECT_DIR": project_dir,
        "DATA_DIR": data_dir,
        "TRADES_PATH": trades_path,
        "log": logger,
        "client": client_obj,
        "trade_manager": trade_manager_obj,
    }))


def main():
    build_app()
    log.info("=" * 70)
    log.info("KALSHI DEMO TRADER — Testing Full Trading Flow")
    log.info("Time: %s", datetime.datetime.now().isoformat())
    log.info("API: %s", client.base_url)
    log.info("=" * 70)

    # Step 1: Auth & Balance
    log.info("Step 1: Verify Authentication & Balance")
    try:
        bal = get_balance()
        balance_cents = bal.get("balance", 0)
        log.info("Auth OK! Balance: $%.2f", balance_cents / 100)
    except Exception as e:
        log.error("Auth failed: %s", e)
        sys.exit(1)

    # Step 2: List all markets
    log.info("Step 2: Fetching All Open Markets...")
    try:
        markets = get_all_markets(limit=2000)
        log.info("Found %d open markets total", len(markets))
    except Exception as e:
        log.error("Market fetch failed: %s", e, exc_info=True)
        sys.exit(1)

    # Step 3: Categorize
    log.info("Step 3: Categorizing Markets...")
    categories = categorize_markets(markets)
    for cat, cat_markets in sorted(categories.items(), key=lambda x: -len(x[1])):
        log.info("  %s: %d markets", cat, len(cat_markets))
        # Show a few examples
        for m in cat_markets[:3]:
            ticker = m.get("ticker", "")
            title = m.get("title", "")[:60]
            yes_ask = m.get("yes_ask", 0)
            vol = m.get("volume", 0)
            log.info("    %s — %s (ask:%sc, vol:%s)", ticker, title, yes_ask, vol)

    # Save full market data
    market_data_path = DATA_DIR / "all-open-markets.json"
    with open(market_data_path, "w") as f:
        json.dump({
            "timestamp": datetime.datetime.now().isoformat(),
            "total_markets": len(markets),
            "categories": {k: len(v) for k, v in categories.items()},
            "markets": markets,
        }, f, indent=2)
    log.info("Saved market data to %s", market_data_path)

    # Step 4: Find tradeable markets
    log.info("Step 4: Finding Tradeable Markets (closing soon, has liquidity)...")
    tradeable = find_tradeable_markets(markets, max_results=20)
    log.info("Found %d tradeable markets:", len(tradeable))
    for i, t in enumerate(tradeable):
        hrs = t["hours_to_close"]
        hrs_str = f"{hrs:.1f}h" if hrs < 999 else "n/a"
        log.info("  %d. [%s] %s", i + 1, t['category'], t['ticker'])
        log.info("     %s", t['title'][:70])
        log.info("     Ask: %sc YES / %sc NO | Vol: %s | Close: %s | Spread: %sc",
                 t['yes_ask'], t['no_ask'], t['volume'], hrs_str, t['spread'])

    # Step 5: Place demo trades
    log.info("Step 5: Placing Demo Trades...")
    trades_placed = []

    if not tradeable:
        log.warning("No tradeable markets found! Trying to place on any market with an ask...")
        # Fall back to any market with prices
        for m in markets:
            if m.get("yes_ask", 0) > 0 and m.get("yes_ask", 0) < 95:
                tradeable = [{"ticker": m["ticker"], "yes_ask": m["yes_ask"], "no_ask": m.get("no_ask", 0), "title": m.get("title", ""), "market": m}]
                break

    for t in tradeable[:3]:  # Place up to 3 test trades
        ticker = t["ticker"]
        yes_ask = t.get("yes_ask", 0)
        no_ask = t.get("no_ask", 0)

        # Choose the cheaper side (more upside)
        if yes_ask > 0 and yes_ask <= 50:
            side = "yes"
            price = yes_ask
        elif no_ask > 0 and no_ask <= 50:
            side = "no"
            price = no_ask
        elif yes_ask > 0 and yes_ask < 95:
            side = "yes"
            price = yes_ask
        elif no_ask > 0 and no_ask < 95:
            side = "no"
            price = no_ask
        else:
            log.info("Skipping %s — no good price", ticker)
            continue

        try:
            result = place_trade(ticker, side=side, action="buy", count=1, price=price)
            trades_placed.append({
                "ticker": ticker,
                "side": side,
                "price": price,
                "result": result,
                "title": t.get("title", ""),
            })
        except requests.exceptions.HTTPError as e:
            log.error("Trade failed: %s — %s", e.response.status_code, e.response.text[:200])
        except Exception as e:
            log.error("Trade failed: %s", e)

    # Step 6: Check positions and orders
    log.info("Step 6: Checking Positions & Orders...")
    try:
        positions = get_positions()
        log.info("Positions: %d", len(positions))
        for p in positions[:10]:
            ticker = p.get("ticker", "")
            qty = p.get("total_traded", 0)
            log.info("    %s: %d contracts", ticker, qty)
    except Exception as e:
        log.error("Position fetch error: %s", e)

    try:
        orders = get_orders()
        log.info("Open Orders: %d", len(orders))
        for o in orders[:10]:
            log.info("    %s: %s %sx @ %sc — %s",
                     o.get('ticker', ''), o.get('side', ''),
                     o.get('remaining_count', 0),
                     o.get('yes_price', o.get('no_price', '?')),
                     o.get('status', '?'))
    except Exception as e:
        log.error("Orders fetch error: %s", e)

    # Step 7: Final balance
    log.info("Step 7: Final Balance Check...")
    try:
        bal = get_balance()
        log.info("Balance: $%.2f", bal.get('balance', 0) / 100)
    except Exception as e:
        log.error("Balance error: %s", e)

    # Save trade log
    trade_log = {
        "timestamp": datetime.datetime.now().isoformat(),
        "balance_before": balance_cents,
        "trades": [{
            "ticker": t["ticker"],
            "side": t["side"],
            "price": t["price"],
            "title": t["title"],
            "order_id": t["result"].get("order", {}).get("order_id", ""),
            "status": t["result"].get("order", {}).get("status", ""),
        } for t in trades_placed],
    }
    log_path = DATA_DIR / "demo-trades-log.json"
    with open(log_path, "w") as f:
        json.dump(trade_log, f, indent=2)
    log.info("Trade log saved to %s", log_path)

    log.info("=" * 70)
    log.info("DEMO TRADING COMPLETE — %d trades placed", len(trades_placed))
    log.info("=" * 70)

if __name__ == "__main__":
    main()
