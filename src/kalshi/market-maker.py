#!/usr/bin/env python3
"""Kalshi Market Maker — Avellaneda-Stoikov inventory-managed limit orders.

Places bid/ask limit orders on liquid weather markets where we have
informational edge. Uses the Avellaneda-Stoikov reservation price model
to adjust quotes based on inventory and time to settlement.

Reservation price: r = mid - inventory * gamma * sigma^2 * T
Spread: delta = gamma * sigma^2 * T + (2/gamma) * ln(1 + gamma/k)

Starts with enabled: false in config — monitoring mode only.

Usage:
    python3 src/kalshi/market-maker.py          # daemon mode
    python3 src/kalshi/market-maker.py --once    # single scan
"""

import json, time, datetime, os, sys, re, math, argparse, traceback
from pathlib import Path
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, TradeManager, trim_trade_log, build_market_snapshot,
)
from probability import weather_probability, is_market_liquid
from capital_allocator import PortfolioAllocator

setup_unbuffered()
log = setup_logging("market-maker")
setup_signal_handlers()

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-mm-trades.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load config
bots_config = json.loads(BOTS_CONFIG_PATH.read_text())
mm_config = bots_config.get("market_maker", {})

ENABLED = mm_config.get("enabled", False)
SCAN_INTERVAL = mm_config.get("scanIntervalMinutes", 5)
MAX_INVENTORY = mm_config.get("maxInventoryPerMarket", 20)
GAMMA = mm_config.get("gamma", 0.3)  # risk aversion parameter (prediction markets need higher)
K_PARAM = mm_config.get("kParam", 1.5)  # order arrival intensity
MAX_TRADE = mm_config.get("maxTradeAmount", 5)
MAX_DAILY_TRADES = mm_config.get("maxDailyTrades", 50)
MAX_DAILY_LOSS = mm_config.get("maxDailyLoss", 25)
TARGET_MARKETS = mm_config.get("targetMarkets", ["KXHIGH"])

# Load calibrated per-market params (from orderbook simulation)
MM_CALIBRATION_PATH = PROJECT_DIR / "config" / "mm-calibration.json"

def _load_mm_calibration():
    """Load per-prefix calibrated MM params."""
    try:
        if MM_CALIBRATION_PATH.exists():
            return json.loads(MM_CALIBRATION_PATH.read_text())
    except (json.JSONDecodeError, ValueError):
        pass
    return {}

_mm_calibration = _load_mm_calibration()

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE,
    "maxTradeAmountPct": mm_config.get("maxTradeAmountPct"),
    "maxDailyTrades": MAX_DAILY_TRADES,
    "maxDailyLoss": MAX_DAILY_LOSS,
    "maxDailyLossPct": mm_config.get("maxDailyLossPct"),
}, logger=log, cooldown_hours=0, bot_name="mm")  # MM must re-quote every cycle
trim_trade_log(TRADES_PATH)

# === Inventory Tracking ===
_inventory = {}  # ticker -> net position (positive = long YES, negative = short YES)


def get_current_inventory(ticker):
    """Get current net inventory for a ticker."""
    return _inventory.get(ticker, 0)


def update_inventory():
    """Sync inventory from API positions."""
    try:
        data = client.get("/portfolio/positions")
        positions = data.get("market_positions", [])
        for p in positions:
            ticker = p.get("ticker", "")
            _inventory[ticker] = p.get("position", 0)
    except Exception as e:
        log.error(f"Failed to sync inventory: {e}")


# === Avellaneda-Stoikov Model ===

def compute_reservation_price(mid_price, inventory, sigma, time_to_settlement_hours, gamma=None):
    """Compute Avellaneda-Stoikov reservation price.

    r = mid - inventory * gamma * sigma^2 * T

    Args:
        mid_price: current midpoint price (0-100 cents).
        inventory: net position (positive = long).
        sigma: price volatility (in cents, converted to 0-1 fraction internally).
        time_to_settlement_hours: hours until settlement.
        gamma: risk aversion parameter (higher = more conservative).

    Returns:
        Reservation price in cents.
    """
    if gamma is None:
        gamma = GAMMA

    T = max(0.01, time_to_settlement_hours / 24)  # in days
    sigma_frac = sigma / 100.0  # convert cents to 0-1 fraction for A-S formula
    # A-S reservation price: mid - inventory * gamma * sigma^2 * T (all in fraction space)
    r = mid_price / 100.0 - inventory * gamma * (sigma_frac ** 2) * T
    # Convert back to cents
    return max(1, min(99, round(r * 100)))


def compute_optimal_spread(sigma, time_to_settlement_hours, gamma=None, k=None):
    """Compute Avellaneda-Stoikov optimal spread.

    delta = gamma * sigma^2 * T + (2/gamma) * ln(1 + gamma/k)

    Returns half-spread in cents (distance from reservation price to bid/ask).
    """
    if gamma is None:
        gamma = GAMMA
    if k is None:
        k = K_PARAM

    T = max(0.01, time_to_settlement_hours / 24)
    sigma_frac = sigma / 100.0  # convert cents to 0-1 fraction for A-S formula
    delta = gamma * (sigma_frac ** 2) * T + (2 / gamma) * math.log(1 + gamma / k)

    # delta is in fraction space; convert back to cents
    # Minimum spread of 2c (1c each side) to cover exchange fees
    return max(1, round(delta * 100 / 2))


def estimate_market_sigma(market):
    """Estimate price volatility independently of current spread.

    Uses weather model sigma for KXHIGH markets (converted to cents),
    fixed defaults for crypto/other. This avoids circular logic where
    sigma was derived FROM the spread and then used to compute the spread.

    Returns sigma in cents.
    """
    ticker = market.get("ticker", "")

    # Weather markets: use calibrated sigma from probability model
    if "KXHIGH" in ticker:
        m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-", ticker)
        if m:
            MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                      "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
            city = m.group(1)
            yr, mon_str, day = int(m.group(2)), m.group(3), int(m.group(4))
            month = MONTHS.get(mon_str)
            if month:
                try:
                    market_date = datetime.date(2000 + yr, month, day)
                    days_out = max(0, (market_date - datetime.date.today()).days)
                    # Weather sigma: intercept + slope * sqrt(days_out) (matching probability.py)
                    weather_sigma_f = 2.0 + 0.5 * math.sqrt(max(1, days_out))
                    # Convert F uncertainty to price-cents uncertainty (~4 cents per degree F)
                    return max(2, round(weather_sigma_f * 4))
                except (ValueError, TypeError):
                    pass
        return 5  # weather fallback

    # Crypto: higher default volatility
    if any(x in ticker for x in ["KXBTC", "KXETH", "KXSOL", "KXCRYPTO"]):
        return 8

    return 5  # generic default


def estimate_hours_to_settlement(market):
    """Estimate hours until market settlement."""
    close_time = market.get("close_time") or market.get("expected_expiration_time")
    if close_time:
        try:
            close_dt = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            delta = close_dt - now
            return max(0.1, delta.total_seconds() / 3600)
        except (ValueError, TypeError):
            pass
    return 24  # default


# === Order Management ===

def cancel_existing_orders(ticker):
    """Cancel all resting orders on a ticker before placing new quotes."""
    try:
        data = client.get("/portfolio/orders?status=resting")
        orders = data.get("orders", [])
        for order in orders:
            if order.get("ticker") == ticker:
                order_id = order.get("order_id")
                try:
                    client.delete(f"/portfolio/orders/{order_id}")
                    log.info(f"  Cancelled order {order_id} on {ticker}")
                except Exception as e:
                    log.error(f"  Failed to cancel {order_id}: {e}")
    except Exception as e:
        log.error(f"  Failed to fetch orders for {ticker}: {e}")


def place_quotes(ticker, bid_price, ask_price, size=1, yes_bid=None, yes_ask=None,
                 mm_params=None):
    """Place bid and ask limit orders for market making."""
    results = []
    snapshot = build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask)
    extra = mm_params or {}

    # Place bid (buy YES)
    if bid_price > 0 and bid_price < 99:
        reasoning = f"MM bid: {ticker} YES@{bid_price}c (size={size})"
        result = trade_manager.place_order(ticker, "yes", bid_price, size, reasoning,
                                            market_snapshot=snapshot, sizing_method="fixed_mm",
                                            **extra)
        if result:
            results.append(("bid", result))

    # Place ask (sell YES = buy NO at 100-ask_price)
    if ask_price > 1 and ask_price <= 99:
        no_price = 100 - ask_price
        if no_price > 0:
            reasoning = f"MM ask: {ticker} NO@{no_price}c (equiv YES ask@{ask_price}c, size={size})"
            result = trade_manager.place_order(ticker, "no", no_price, size, reasoning,
                                                market_snapshot=snapshot, sizing_method="fixed_mm",
                                                **extra)
            if result:
                results.append(("ask", result))

    return results


# === Main Scan ===

def scan_and_quote():
    """Scan markets and place/update market-making quotes."""
    now = datetime.datetime.now()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Market maker scan starting...")

    if not ENABLED:
        log.info("  Market maker is DISABLED in config. Set enabled: true to activate.")
        # Still log analysis for monitoring
        analyze_opportunities()
        return

    # Sync inventory
    update_inventory()

    # Fetch target markets
    all_markets = []
    for prefix in TARGET_MARKETS:
        try:
            markets = client.get_all_markets(prefix=prefix, cache_ttl=60)
            all_markets.extend(markets)
        except Exception as e:
            log.error(f"Market fetch error for {prefix}: {e}")

    if not all_markets:
        log.info("No markets found.")
        return

    # Filter to liquid markets only
    liquid_markets = [m for m in all_markets if is_market_liquid(m)]
    log.info(f"Found {len(liquid_markets)} liquid markets (of {len(all_markets)} total)")

    quoted = 0
    for m in liquid_markets:
        ticker = m.get("ticker", "")
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)

        if not yes_bid or not yes_ask:
            continue

        mid = (yes_bid + yes_ask) / 2
        inventory = get_current_inventory(ticker)
        sigma = estimate_market_sigma(m)
        hours_to_settle = estimate_hours_to_settlement(m)

        # Skip markets too close to settlement (spread widens, risk increases)
        if hours_to_settle < 1:
            continue

        # Check inventory limits
        if abs(inventory) >= MAX_INVENTORY:
            log.info(f"  {ticker}: inventory limit reached ({inventory}), skipping")
            continue

        # Use calibrated params if available, else default
        cal = None
        for prefix in TARGET_MARKETS:
            if ticker.startswith(prefix):
                cal = _mm_calibration.get(prefix)
                break

        if cal and cal.get("activated"):
            base_gamma = cal.get("gamma", GAMMA)
            base_k = cal.get("k", K_PARAM)
        else:
            base_gamma = GAMMA
            base_k = K_PARAM

        # Adaptive gamma: increase near settlement and with inventory
        gamma = base_gamma * max(1.0, 24 / hours_to_settle) * (1 + abs(inventory) / MAX_INVENTORY)

        # Compute reservation price and optimal spread
        reservation = compute_reservation_price(mid, inventory, sigma, hours_to_settle, gamma)
        half_spread = compute_optimal_spread(sigma, hours_to_settle, gamma, base_k)

        bid_price = max(1, reservation - half_spread)
        ask_price = min(99, reservation + half_spread)

        # Only quote if our prices improve on existing bid/ask
        if bid_price >= yes_bid and ask_price <= yes_ask:
            # Check allocator budget before quoting
            budget = allocator.request_budget("market-maker", ticker, edge=0.0)
            if not budget.approved:
                log.info(f"  {ticker}: allocator denied: {budget.reason}")
                continue

            log.info(f"  {ticker}: mid={mid:.0f}c inv={inventory} r={reservation}c spread={half_spread*2}c -> bid={bid_price} ask={ask_price}")

            # Cancel existing orders and place new quotes
            cancel_existing_orders(ticker)
            mm_params = {
                "inventory": inventory,
                "gamma": round(gamma, 4),
                "mm_sigma": sigma,
                "reservation_price": round(reservation, 1),
                "half_spread": round(half_spread, 1),
                "mid_price": round(mid, 1),
                "hours_to_settle": round(hours_to_settle, 2),
            }
            results = place_quotes(ticker, bid_price, ask_price, yes_bid=yes_bid, yes_ask=yes_ask,
                                   mm_params=mm_params)
            if results:
                quoted += 1
                for side, result in results:
                    allocator.record_trade("market-maker", ticker, 0, edge=0.0)  # quotes don't consume allocation
        else:
            log.info(f"  {ticker}: our spread ({bid_price}-{ask_price}) doesn't improve market ({yes_bid}-{yes_ask}), skipping")

    log.info(f"Scan complete. Quoted {quoted} markets.")


def analyze_opportunities():
    """Analyze potential market making opportunities (monitoring mode)."""
    all_markets = []
    for prefix in TARGET_MARKETS:
        try:
            markets = client.get_all_markets(prefix=prefix, cache_ttl=300)
            all_markets.extend(markets)
        except Exception as e:
            log.error(f"Market fetch error for {prefix}: {e}")

    liquid = [m for m in all_markets if is_market_liquid(m)]
    log.info(f"Analysis: {len(liquid)} liquid markets of {len(all_markets)} total")

    wide_spread = 0
    for m in liquid:
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        spread = yes_ask - yes_bid
        if spread >= 5:  # 5c+ spread = MM opportunity
            wide_spread += 1
            ticker = m.get("ticker", "")
            log.info(f"  {ticker}: spread={spread}c bid={yes_bid}c ask={yes_ask}c vol={m.get('volume',0)}")

    log.info(f"  {wide_spread} markets with spread >= 5c (MM opportunities)")


# === Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Kalshi Market Maker")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Market Maker (Avellaneda-Stoikov)")
    log.info(f"  Enabled: {ENABLED}")
    log.info(f"  Gamma: {GAMMA} | Max inventory: {MAX_INVENTORY}")
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
        scan_and_quote()
        return

    # Daemon loop
    while True:
        try:
            scan_and_quote()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
