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

import json, time, datetime, os, sys, re, math, argparse
from pathlib import Path
from app_bootstrap import AppContext, install_app_context
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, TradeManager, trim_trade_log, build_market_snapshot,
    is_shutdown_requested, HealthCheckMonitor,
)
from probability import weather_probability, weather_sigma, is_market_liquid, _probit, _norm_pdf
from capital_allocator import PortfolioAllocator

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-mm-trades.json"
_APP_CONTEXT = None
log = None
health = None
bots_config = {}
mm_config = {}
ENABLED = False
SCAN_INTERVAL = 5
MAX_INVENTORY = 20
GAMMA = 0.3
K_PARAM = 1.5
MAX_TRADE = 5
MAX_DAILY_TRADES = 50
MAX_DAILY_LOSS = 25
TARGET_MARKETS = ["KXHIGH"]

# Load calibrated per-market params (from orderbook simulation)
MM_CALIBRATION_PATH = PROJECT_DIR / "config" / "mm-calibration.json"

def _load_mm_calibration(path=None):
    """Load per-prefix calibrated MM params."""
    calibration_path = Path(path or MM_CALIBRATION_PATH)
    try:
        if calibration_path.exists():
            return json.loads(calibration_path.read_text())
    except (json.JSONDecodeError, ValueError):
        pass
    return {}

_mm_calibration = {}
client = None
allocator = None
trade_manager = None

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

    # delta is in fraction space; convert back to cents as half-spread
    # Result is half-spread in cents: 1c minimum = 2c total spread (covers exchange fees)
    return max(1, round(delta * 100 / 2))


def estimate_order_arrival_rate(market):
    """Estimate order arrival rate from market volume and time.

    Returns estimated orders per hour, or None if insufficient data.
    Uses volume / hours_since_open as a rough proxy.
    """
    volume = market.get("volume", 0) or 0
    if volume < 5:
        return None

    # Estimate hours the market has been active
    open_time = market.get("open_time")
    if open_time:
        try:
            open_dt = datetime.datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            now = datetime.datetime.now(datetime.timezone.utc)
            hours_open = max(1, (now - open_dt).total_seconds() / 3600)
            return volume / hours_open
        except (ValueError, TypeError):
            pass

    # Fallback: assume 8 trading hours if no open_time
    return volume / 8


def estimate_market_sigma(market, mid_price=None):
    """Estimate price volatility independently of current spread.

    Uses weather model sigma for KXHIGH markets (converted to cents),
    fixed defaults for crypto/other. This avoids circular logic where
    sigma was derived FROM the spread and then used to compute the spread.

    For KXHIGH markets with a valid mid_price, uses the CDF derivative
    formula for nonlinear price-space conversion:
        price_sigma = temp_sigma_F * phi(probit(mid/100)) * 100

    This correctly captures that price sensitivity varies with mid:
    - At mid=50c: max sensitivity (phi(0)=0.399)
    - At mid=10c/90c: lower sensitivity (correct for deep OTM/ITM)

    Args:
        market: market dict from API.
        mid_price: midpoint price in cents (0-100). Enables CDF derivative
            conversion for KXHIGH markets.

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
                    weather_sigma_f = weather_sigma(days_out, city=city)

                    # CDF derivative conversion when mid_price is available
                    if mid_price is not None and 1 < mid_price < 99:
                        p = mid_price / 100.0
                        z = _probit(p)
                        price_sigma = weather_sigma_f * _norm_pdf(z) * 100
                        return max(2, round(price_sigma))

                    # Fallback: linear approximation
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
        sigma = estimate_market_sigma(m, mid_price=mid)
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

        # Dynamic k: use max of config value and fill-rate estimate
        arrival_rate = estimate_order_arrival_rate(m)
        if arrival_rate is not None:
            estimated_k = max(base_k, arrival_rate * 0.8)
            if estimated_k != base_k:
                log.info(f"  {ticker}: dynamic k={estimated_k:.1f} (arrival={arrival_rate:.1f}/h, config={base_k:.1f})")
            effective_k = estimated_k
        else:
            effective_k = base_k

        # Compute reservation price and optimal spread
        reservation = compute_reservation_price(mid, inventory, sigma, hours_to_settle, gamma)
        half_spread = compute_optimal_spread(sigma, hours_to_settle, gamma, effective_k)

        bid_price = max(1, reservation - half_spread)
        ask_price = min(99, reservation + half_spread)

        # Minimum spread: 2 cents total to prevent crossing
        if ask_price - bid_price < 2:
            mid_int = round(mid)
            bid_price = max(1, mid_int - 1)
            ask_price = min(99, mid_int + 1)

        # Only quote if our prices improve on existing bid/ask
        if bid_price >= yes_bid and ask_price <= yes_ask:
            # Check allocator budget before quoting
            budget = allocator.request_budget("market-maker", ticker, edge=0.0)
            if not budget.approved:
                log.info(f"  {ticker}: allocator denied: {budget.reason}")
                trade_manager.log_decision(ticker, "yes", "skipped", f"allocator denied: {budget.reason}",
                                           price_cents=round(mid))
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
            trade_manager.log_decision(ticker, "yes", "skipped",
                                       f"spread doesn't improve market ({bid_price}-{ask_price} vs {yes_bid}-{yes_ask})",
                                       price_cents=round(mid))

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

def load_config(project_dir=None):
    project_dir = Path(project_dir or PROJECT_DIR)
    return json.loads((project_dir / "config" / "bots-config.json").read_text())


def build_app(project_dir=None):
    project_dir = Path(project_dir or PROJECT_DIR)
    setup_unbuffered()
    logger = setup_logging("market-maker")
    setup_signal_handlers()
    health_monitor = HealthCheckMonitor(logger=logger)
    bots_config_path = project_dir / "config" / "bots-config.json"
    trades_path = project_dir / "data" / "kalshi-mm-trades.json"
    mm_calibration_path = project_dir / "config" / "mm-calibration.json"
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    loaded_bots_config = load_config(project_dir)
    loaded_mm_config = loaded_bots_config.get("market_maker", {})
    max_trade = loaded_mm_config.get("maxTradeAmount", 5)
    max_daily_trades = loaded_mm_config.get("maxDailyTrades", 50)
    max_daily_loss = loaded_mm_config.get("maxDailyLoss", 25)
    client_obj = KalshiClient()
    allocator_obj = PortfolioAllocator(client_obj, logger=logger)
    trade_manager_obj = TradeManager(client_obj, trades_path, {
        "maxTradeAmount": max_trade,
        "maxTradeAmountPct": loaded_mm_config.get("maxTradeAmountPct"),
        "maxDailyTrades": max_daily_trades,
        "maxDailyLoss": max_daily_loss,
        "maxDailyLossPct": loaded_mm_config.get("maxDailyLossPct"),
    }, logger=logger, cooldown_hours=0, bot_name="mm")
    trim_trade_log(trades_path)
    mm_calibration = _load_mm_calibration(mm_calibration_path)
    return install_app_context(globals(), AppContext({
        "PROJECT_DIR": project_dir,
        "BOTS_CONFIG_PATH": bots_config_path,
        "TRADES_PATH": trades_path,
        "MM_CALIBRATION_PATH": mm_calibration_path,
        "log": logger,
        "health": health_monitor,
        "bots_config": loaded_bots_config,
        "mm_config": loaded_mm_config,
        "ENABLED": loaded_mm_config.get("enabled", False),
        "SCAN_INTERVAL": loaded_mm_config.get("scanIntervalMinutes", 5),
        "MAX_INVENTORY": loaded_mm_config.get("maxInventoryPerMarket", 20),
        "GAMMA": loaded_mm_config.get("gamma", 0.3),
        "K_PARAM": loaded_mm_config.get("kParam", 1.5),
        "MAX_TRADE": max_trade,
        "MAX_DAILY_TRADES": max_daily_trades,
        "MAX_DAILY_LOSS": max_daily_loss,
        "TARGET_MARKETS": loaded_mm_config.get("targetMarkets", ["KXHIGH"]),
        "_mm_calibration": mm_calibration,
        "client": client_obj,
        "allocator": allocator_obj,
        "trade_manager": trade_manager_obj,
    }))


def main():
    parser = argparse.ArgumentParser(description="Kalshi Market Maker")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()
    build_app()

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
            health.record_bot_heartbeat("market-maker")
            scan_and_quote()
        except Exception as e:
            log.error("Scan error: %s", e, exc_info=True)

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break
        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
