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
from zoneinfo import ZoneInfo
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, TradeManager, trim_trade_log, CITY_TIMEZONES, _local_today,
    round_half_up, retry_request, fetch_parallel, HealthCheckMonitor,
)
from probability import weather_probability, nws_probability, half_kelly, kalshi_fee_cents
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
ORDER_TTL_MINUTES = pm_config.get("orderTtlMinutes", 120)

# Load NWS station config for model-shift evaluation
MONITOR_CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-monitor-config.json"
try:
    _monitor_cfg = json.loads(MONITOR_CONFIG_PATH.read_text())
    NWS_STATIONS = _monitor_cfg.get("sources", {}).get("nws", {}).get("stations", {})
except (FileNotFoundError, json.JSONDecodeError):
    NWS_STATIONS = {}

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
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

    # Check YES position take-profit (fee-aware: net proceeds must exceed threshold)
    if yes_count > 0 and yes_bid > 0:
        fee = kalshi_fee_cents(yes_bid)
        net_proceeds = yes_bid - fee
        if net_proceeds >= take_profit_cents:
            return {
                "action": "take_profit",
                "side": "yes",
                "count": yes_count,
                "price": yes_bid,
                "reasoning": f"Take profit: YES bid {yes_bid}c - fee {fee:.1f}c = net {net_proceeds:.0f}c >= {take_profit_cents}c threshold",
            }

    # Check NO position take-profit (fee-aware)
    if no_count > 0 and no_bid > 0:
        fee = kalshi_fee_cents(no_bid)
        net_proceeds = no_bid - fee
        if net_proceeds >= take_profit_cents:
            return {
                "action": "take_profit",
                "side": "no",
                "count": no_count,
                "price": no_bid,
                "reasoning": f"Take profit: NO bid {no_bid}c - fee {fee:.1f}c = net {net_proceeds:.0f}c >= {take_profit_cents}c threshold",
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


def _fetch_nws_running_high(city_code):
    """Fetch today's running high temperature from NWS for a city.

    Uses CITY_TIMEZONES for timezone-correct observation window.
    Returns running high in Fahrenheit (int), or None on failure.
    """
    station_id = NWS_STATIONS.get(city_code)
    if not station_id:
        return None

    try:
        tz = ZoneInfo(CITY_TIMEZONES.get(city_code, "America/New_York"))
        local_now = datetime.datetime.now(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_start = local_midnight.astimezone(datetime.timezone.utc)
        start = utc_start.strftime("%Y-%m-%dT%H:%M:%SZ")

        url = f"https://api.weather.gov/stations/{station_id}/observations?start={start}&limit=100"
        headers = {
            "User-Agent": "(KalshiPositionMonitor, contact@example.com)",
            "Accept": "application/geo+json",
        }
        r = retry_request("GET", url, headers=headers, timeout=15)
        data = r.json()
        features = data.get("features", [])
        temps = []
        for f in features:
            t = f.get("properties", {}).get("temperature", {}).get("value")
            if t is not None:
                temps.append(round_half_up(t * 9/5 + 32))
        if temps:
            return max(temps)
    except Exception as e:
        log.error(f"  NWS fetch failed for {city_code}: {e}")
    return None


def evaluate_model_shift(position, market):
    """Check if our probability model now disagrees with our position.

    For weather positions, fetches NWS running high and recomputes probability.
    Exits when:
      1. Fair value (model prob * 100) < bid - FEE_BUFFER_CENTS, AND
      2. Model probability on our side < 0.35
    """
    ticker = position.get("ticker", "")
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)

    parsed = parse_temp_ticker(ticker)
    if not parsed:
        return None  # Can only model-shift weather markets for now

    city = parsed["city"]
    city_today = _local_today(city)

    # Only evaluate model shift for today's markets
    if parsed["date"] != city_today:
        return None

    # Fetch NWS running high
    running_high = _fetch_nws_running_high(city)
    if running_high is None:
        return None

    threshold = parsed["threshold"]
    direction = parsed["direction"]
    now = datetime.datetime.now()

    prob = nws_probability(running_high, threshold, direction, now.hour)
    yes_bid = market.get("yes_bid", 0)
    no_bid = market.get("no_bid", 0) if market.get("no_bid") else (100 - market.get("yes_ask", 100))

    # Check YES position: model now says prob < 0.35 (against us)
    if yes_count > 0 and prob < 0.35:
        fair_value_cents = int(prob * 100)
        fee = kalshi_fee_cents(yes_bid)
        if fair_value_cents < yes_bid - fee:
            return {
                "action": "model_shift",
                "side": "yes",
                "count": yes_count,
                "price": yes_bid,
                "reasoning": (
                    f"Model shift: NWS {city} high {running_high}F, "
                    f"prob={prob*100:.0f}% < 35%, fair={fair_value_cents}c < bid={yes_bid}c - fee {fee:.1f}c"
                ),
            }

    # Check NO position: model now says prob > 0.65 (YES prob > 65%, against our NO)
    if no_count > 0 and prob > 0.65:
        no_prob = 1.0 - prob
        fair_no_cents = int(no_prob * 100)
        fee = kalshi_fee_cents(no_bid)
        if fair_no_cents < no_bid - fee:
            return {
                "action": "model_shift",
                "side": "no",
                "count": no_count,
                "price": no_bid,
                "reasoning": (
                    f"Model shift: NWS {city} high {running_high}F, "
                    f"NO prob={no_prob*100:.0f}% < 35%, fair={fair_no_cents}c < bid={no_bid}c - fee {fee:.1f}c"
                ),
            }

    return None


def cancel_stale_orders():
    """Cancel resting orders that are near settlement or too old.

    Settlement-aware logic (mirrors beatrelease-scanner approach):
      1. If market close_time is within 2 hours → cancel (about to settle, won't fill)
      2. If order age > 12 hours → cancel (stale capital)
      3. Otherwise → keep the order
    """
    try:
        data = client.get("/portfolio/orders?status=resting")
        orders = data.get("orders", [])
        if not orders:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        canceled = 0

        for order in orders:
            ticker = order.get("ticker", "")
            order_id = order.get("order_id", "")
            if not order_id:
                continue

            should_cancel = False
            reason = ""

            # Check 1: Market close time — cancel if settling within 2 hours
            close_time_str = order.get("expiration_time", "") or order.get("close_time", "")
            if close_time_str:
                try:
                    close_dt = datetime.datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                    hours_to_close = (close_dt - now).total_seconds() / 3600
                    if hours_to_close < 2:
                        should_cancel = True
                        reason = f"market closes in {hours_to_close:.1f}h"
                except (ValueError, TypeError):
                    pass

            # Check 2: Order TTL — cancel if older than configured TTL (default 120 min)
            if not should_cancel:
                created = order.get("created_time", "")
                if created:
                    try:
                        created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_minutes = (now - created_dt).total_seconds() / 60
                        if age_minutes > ORDER_TTL_MINUTES:
                            should_cancel = True
                            reason = f"age {age_minutes:.0f}min > {ORDER_TTL_MINUTES}min TTL"
                    except (ValueError, TypeError):
                        pass

            # Check 3: Fallback — cancel if older than 12 hours (safety net)
            if not should_cancel:
                created = order.get("created_time", "")
                if created:
                    try:
                        created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_hours = (now - created_dt).total_seconds() / 3600
                        if age_hours > 12:
                            should_cancel = True
                            reason = f"age {age_hours:.0f}h > 12h"
                    except (ValueError, TypeError):
                        pass

            if should_cancel:
                try:
                    client.delete(f"/portfolio/orders/{order_id}")
                    log.info(f"  Canceled stale order {order_id} on {ticker} ({reason})")
                    canceled += 1
                except Exception as e:
                    log.error(f"  Failed to cancel {order_id}: {e}")

        if canceled:
            log.info(f"  Canceled {canceled} stale resting orders")

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
                sizing_method="position_exit",
            )
            if result:
                exits_today += 1

    # Process allocator pending exits (superseded by better signals)
    pending = allocator.get_pending_exits()
    if pending:
        log.info(f"  Processing {len(pending)} pending exits from allocator supersede...")
        for pending_ticker in pending:
            # Find this ticker in our positions
            for pos in positions:
                if pos.get("ticker") == pending_ticker and exits_today < MAX_DAILY_EXITS:
                    yes_count = pos.get("yes", 0)
                    no_count = pos.get("no", 0)
                    market = get_market_data(pending_ticker)
                    if not market:
                        continue
                    if yes_count > 0:
                        bid = market.get("yes_bid", 0)
                        if bid > 0:
                            result = trade_manager.sell_position(
                                pending_ticker, "yes", bid, yes_count,
                                f"Allocator supersede exit: better signal available",
                                exit_type="allocator_supersede",
                            )
                            if result:
                                exits_today += 1
                    elif no_count > 0:
                        no_bid = market.get("no_bid", 0) or (100 - market.get("yes_ask", 100))
                        if no_bid > 0:
                            result = trade_manager.sell_position(
                                pending_ticker, "no", no_bid, no_count,
                                f"Allocator supersede exit: better signal available",
                                exit_type="allocator_supersede",
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
            health.record_bot_heartbeat("position-monitor")
            scan_positions()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
