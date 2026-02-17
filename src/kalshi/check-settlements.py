#!/usr/bin/env python3
"""Check settlements and market statuses for Feb 16 weather trades."""
import json, sys
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR

setup_unbuffered()
setup_signal_handlers()
log = setup_logging("check-settlements")

TRADES_PATH = PROJECT_DIR / "data" / "kalshi-trades.json"

client = KalshiClient()

log.info("=" * 60)
log.info("KALSHI SETTLEMENT CHECK - Feb 16/17 2026")
log.info("=" * 60)

# 1. Check settlements
log.info("--- PORTFOLIO SETTLEMENTS ---")
try:
    data = client.get("/portfolio/settlements")
    log.info(json.dumps(data, indent=2))
except Exception as e:
    log.error("Error: %s", e)

# 2. Check portfolio balance
log.info("--- PORTFOLIO BALANCE ---")
try:
    data = client.get("/portfolio/balance")
    log.info(json.dumps(data, indent=2))
except Exception as e:
    log.error("Error: %s", e)

# 3. Check positions
log.info("--- PORTFOLIO POSITIONS ---")
try:
    data = client.get("/portfolio/positions")
    positions = data.get("market_positions", []) or data.get("positions", [])
    if positions:
        for p in positions:
            log.info(json.dumps(p, indent=2))
    else:
        log.info("Raw: %s", json.dumps(data, indent=2)[:500])
except Exception as e:
    log.error("Error: %s", e)

# 4. Check fills (actual executed trades)
log.info("--- FILLS ---")
try:
    data = client.get("/portfolio/fills?limit=100")
    fills = data.get("fills", [])
    log.info("Total fills: %d", len(fills))
    for f in fills:
        log.info("  %s %s %sx @ %sc | created: %s",
                 f.get('ticker'), f.get('side'), f.get('count'),
                 f.get('yes_price', f.get('no_price', '?')),
                 f.get('created_time', '?'))
except Exception as e:
    log.error("Error: %s", e)

# 5. Check market statuses for our traded tickers
log.info("--- MARKET STATUSES ---")
try:
    trades = load_trades(TRADES_PATH)
    if not trades:
        log.info("No trades found (trades file missing or empty)")
    else:
        tickers = list(set(t["ticker"] for t in trades))
        for ticker in sorted(tickers):
            try:
                data = client.get(f"/markets/{ticker}")
                m = data.get("market", data)
                status = m.get("status", "?")
                result = m.get("result", "?")
                close_time = m.get("close_time", "?")
                log.info("  %s: status=%s, result=%s, close=%s", ticker, status, result, close_time)
            except Exception as e:
                log.error("  %s: ERROR %s", ticker, e)
except Exception as e:
    log.error("Error loading trades file: %s", e)

# 6. Check orders
log.info("--- ORDERS ---")
try:
    data = client.get("/portfolio/orders?limit=100")
    orders = data.get("orders", [])
    log.info("Total orders: %d", len(orders))
    for o in orders:
        log.info("  %s %s %s %s/%s @ %sc",
                 o.get('ticker'), o.get('side'), o.get('status'),
                 o.get('remaining_count'), o.get('count'),
                 o.get('yes_price', o.get('no_price', '?')))
except Exception as e:
    log.error("Error: %s", e)
