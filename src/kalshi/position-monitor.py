#!/usr/bin/env python3
"""Kalshi Position Monitor — Manages exits for open positions.

Scans open positions and evaluates:
  1. Take-profit: sell when bid reaches threshold (e.g. 85c+)
  2. Stop-loss: cut when bid drops below threshold (e.g. 20c)
  3. Model-shift: exit when updated model disagrees with position
  4. Stale orders: cancel resting orders older than settlement

Usage:
    python3 src/kalshi/position-monitor.py          # daemon mode
    python3 src/kalshi/position-monitor.py --once    # single scan
"""

import json, time, datetime, os, sys, re, argparse, traceback
from pathlib import Path
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, TradeManager, trim_trade_log,
)
from probability import weather_probability, nws_probability, half_kelly
from capital_allocator import PortfolioAllocator

setup_unbuffered()
log = setup_logging("position-monitor")
setup_signal_handlers()

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
WEATHER_CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-position-trades.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load config
bots_config = json.loads(BOTS_CONFIG_PATH.read_text())
pm_config = bots_config.get("position_monitor", {})

TAKE_PROFIT_THRESHOLD = pm_config.get("takeProfitThreshold", 0.85)
STOP_LOSS_THRESHOLD = pm_config.get("stopLossThreshold", 0.20)
MODEL_SHIFT_THRESHOLD = pm_config.get("modelShiftThreshold", 0.25)
MAX_DAILY_EXITS = pm_config.get("maxDailyExits", 20)
SCAN_INTERVAL = pm_config.get("scanIntervalMinutes", 15)

client = KalshiClient()
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": 50,  # exits can be larger
    "maxDailyTrades": MAX_DAILY_EXITS,
    "maxDailyLoss": 100,
}, logger=log)
trim_trade_log(TRADES_PATH)

# === Ticker Parsing ===
MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

def parse_temp_ticker(ticker):
    """Parse KXHIGHMIA-26FEB16-T86 or KXHIGHMIA-26FEB16-B85.5"""
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m:
        return None
    city = m.group(1)
    day, mon, yr = int(m.group(2)), m.group(3), int(m.group(4))
    direction = m.group(5)
    threshold = float(m.group(6))
    month = MONTHS.get(mon)
    if not month:
        return None
    return {
        "city": city,
        "date": f"{2000+yr}-{month:02d}-{day:02d}",
        "direction": direction,
        "threshold": threshold,
    }


# === Position Fetching ===

def get_open_positions():
    """Fetch all open positions from the Kalshi API."""
    try:
        data = client.get("/portfolio/positions")
        positions = data.get("market_positions", [])
        return [p for p in positions if p.get("total_traded", 0) > 0]
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        return []


def get_market_data(ticker):
    """Fetch current market data for a ticker."""
    try:
        data = client.get(f"/markets/{ticker}")
        return data.get("market", data)
    except Exception as e:
        log.error(f"Failed to fetch market {ticker}: {e}")
        return None


# === Exit Evaluation ===

def evaluate_take_profit(position, market):
    """Check if position should be exited for profit.

    If we bought YES and the bid is now >= take-profit threshold,
    sell to lock in gains rather than waiting for settlement.
    """
    ticker = position.get("ticker", "")
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)

    yes_bid = market.get("yes_bid", 0)
    no_bid = market.get("no_bid", 0) if market.get("no_bid") else (100 - market.get("yes_ask", 100))

    take_profit_cents = int(TAKE_PROFIT_THRESHOLD * 100)

    # Check YES position take-profit
    if yes_count > 0 and yes_bid >= take_profit_cents:
        return {
            "action": "take_profit",
            "side": "yes",
            "count": yes_count,
            "price": yes_bid,
            "reasoning": f"Take profit: YES bid {yes_bid}c >= {take_profit_cents}c threshold",
        }

    # Check NO position take-profit
    if no_count > 0 and no_bid >= take_profit_cents:
        return {
            "action": "take_profit",
            "side": "no",
            "count": no_count,
            "price": no_bid,
            "reasoning": f"Take profit: NO bid {no_bid}c >= {take_profit_cents}c threshold",
        }

    return None


def evaluate_stop_loss(position, market):
    """Check if position should be cut to limit losses.

    If we bought YES and the bid drops below the stop-loss threshold,
    sell to prevent further losses.
    """
    ticker = position.get("ticker", "")
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)

    yes_bid = market.get("yes_bid", 0)
    no_bid = market.get("no_bid", 0) if market.get("no_bid") else (100 - market.get("yes_ask", 100))

    stop_loss_cents = int(STOP_LOSS_THRESHOLD * 100)

    # Check YES position stop-loss
    if yes_count > 0 and yes_bid > 0 and yes_bid <= stop_loss_cents:
        return {
            "action": "stop_loss",
            "side": "yes",
            "count": yes_count,
            "price": yes_bid,
            "reasoning": f"Stop loss: YES bid {yes_bid}c <= {stop_loss_cents}c threshold",
        }

    # Check NO position stop-loss
    if no_count > 0 and no_bid > 0 and no_bid <= stop_loss_cents:
        return {
            "action": "stop_loss",
            "side": "no",
            "count": no_count,
            "price": no_bid,
            "reasoning": f"Stop loss: NO bid {no_bid}c <= {stop_loss_cents}c threshold",
        }

    return None


def evaluate_model_shift(position, market):
    """Check if our probability model now disagrees with our position.

    For weather positions, recompute probability with latest forecast.
    If model now gives <25% in our favor (i.e. we'd be on the wrong side),
    exit the position.
    """
    ticker = position.get("ticker", "")
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)

    parsed = parse_temp_ticker(ticker)
    if not parsed:
        return None  # Can only model-shift weather markets for now

    now = datetime.datetime.now()
    today = datetime.date.today().isoformat()

    # Only evaluate model shift for today's markets
    if parsed["date"] != today:
        return None

    # Would need NWS data to evaluate — skip if we don't have it
    # (The source-monitor handles NWS-based trading; here we just check
    # if the model strongly disagrees with our position)
    return None


def cancel_stale_orders():
    """Cancel resting orders on markets that are close to settlement."""
    try:
        data = client.get("/portfolio/orders?status=resting")
        orders = data.get("orders", [])

        for order in orders:
            ticker = order.get("ticker", "")
            order_id = order.get("order_id", "")
            created = order.get("created_time", "")

            # Cancel orders older than 2 hours
            if created:
                try:
                    created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age = datetime.datetime.now(datetime.timezone.utc) - created_dt
                    if age.total_seconds() > 7200:  # 2 hours
                        log.info(f"Cancelling stale order {order_id} on {ticker} (age: {age})")
                        try:
                            client.delete(f"/portfolio/orders/{order_id}")
                            log.info(f"  Cancelled {order_id}")
                        except Exception as e:
                            log.error(f"  Failed to cancel {order_id}: {e}")
                except (ValueError, TypeError):
                    pass

    except Exception as e:
        log.error(f"Failed to fetch resting orders: {e}")


# === Main Scan ===

def scan_positions():
    """Scan all open positions and evaluate exit opportunities."""
    now = datetime.datetime.now()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Position scan starting...")

    # Get balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")

    # Cancel stale orders first
    cancel_stale_orders()

    # Get open positions
    positions = get_open_positions()
    if not positions:
        log.info("No open positions found.")
        return

    log.info(f"Found {len(positions)} positions to evaluate")
    exits_today = 0

    for pos in positions:
        ticker = pos.get("ticker", "")
        yes_count = pos.get("yes", 0)
        no_count = pos.get("no", 0)

        if yes_count == 0 and no_count == 0:
            continue

        # Fetch market data
        market = get_market_data(ticker)
        if not market:
            continue

        log.info(f"  {ticker}: YES={yes_count} NO={no_count} | bid={market.get('yes_bid',0)}c ask={market.get('yes_ask',0)}c")

        # Evaluate exit conditions in priority order
        exit_signal = None

        # 1. Take profit (highest priority — lock in gains)
        exit_signal = evaluate_take_profit(pos, market)

        # 2. Stop loss
        if not exit_signal:
            exit_signal = evaluate_stop_loss(pos, market)

        # 3. Model shift
        if not exit_signal:
            exit_signal = evaluate_model_shift(pos, market)

        if exit_signal and exits_today < MAX_DAILY_EXITS:
            log.info(f"  -> EXIT SIGNAL: {exit_signal['action']} on {ticker}")
            log.info(f"     {exit_signal['reasoning']}")

            result = trade_manager.sell_position(
                ticker,
                exit_signal["side"],
                exit_signal["price"],
                exit_signal["count"],
                exit_signal["reasoning"],
                exit_type=exit_signal["action"],
            )
            if result:
                exits_today += 1

    log.info(f"Scan complete. {exits_today} exit orders placed.")


# === Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Kalshi Position Monitor")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Position Monitor")
    log.info(f"  Take-profit: {TAKE_PROFIT_THRESHOLD*100:.0f}%  Stop-loss: {STOP_LOSS_THRESHOLD*100:.0f}%")
    log.info(f"  Model-shift: {MODEL_SHIFT_THRESHOLD*100:.0f}%  Max exits/day: {MAX_DAILY_EXITS}")
    log.info(f"  Scan interval: {SCAN_INTERVAL} minutes")
    log.info("=" * 60)

    # Verify auth
    log.info("\nVerifying authentication...")
    try:
        balance, _ = client.get_balance()
        log.info(f"Auth OK! Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Auth failed: {e}")
        sys.exit(1)

    if args.once:
        scan_positions()
        return

    # Daemon loop
    while True:
        try:
            scan_positions()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
