#!/usr/bin/env python3
"""Kalshi Economics Bot — Trades CPI, GDP, and Jobs markets using nowcast data.

Data sources (all public HTTP, no auth):
  1. Cleveland Fed CPI Nowcast — daily point estimate + distribution
  2. AAA Gasoline Prices — daily national average (~8% CPI weight)
  3. BLS Release Schedule — exact release dates

Usage:
    python3 src/kalshi/economics-bot.py          # daemon mode
    python3 src/kalshi/economics-bot.py --once    # single scan
"""

import json, time, datetime, os, sys, re, argparse, traceback
import requests
from pathlib import Path
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot,
    HealthCheckMonitor,
)
from probability import (
    econ_nowcast_probability, cpi_nowcast_sigma, half_kelly, compute_limit_price,
    kalshi_fee_cents,
)
from capital_allocator import PortfolioAllocator

setup_unbuffered()
log = setup_logging("economics")
setup_signal_handlers()

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-economics-trades.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load config
bots_config = json.loads(BOTS_CONFIG_PATH.read_text())
econ_config = bots_config.get("economics", {})

MAX_TRADE = econ_config.get("maxTradeAmount", 15)
MAX_DAILY_TRADES = econ_config.get("maxDailyTrades", 5)
MAX_DAILY_LOSS = econ_config.get("maxDailyLoss", 30)
SCAN_INTERVAL = econ_config.get("scanIntervalMinutes", 360)
EDGE_THRESHOLD = econ_config.get("edgeThreshold", 0.08)

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE,
    "maxDailyTrades": MAX_DAILY_TRADES,
    "maxDailyLoss": MAX_DAILY_LOSS,
}, logger=log)
trim_trade_log(TRADES_PATH)

# === Market ticker prefixes ===
ECON_PREFIXES = ["KXCPI", "KXGDP", "KXJOBS", "KXFED", "KXINFLATION", "KXECON"]


# === Data Sources ===

def fetch_cleveland_fed_nowcast():
    """Fetch Cleveland Fed inflation nowcast.

    Returns dict with 'cpi_yoy' (year-over-year %), 'core_cpi_yoy', 'pce_yoy'
    if available, or empty dict on failure.
    """
    try:
        url = "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        r = retry_request("GET", url, headers=headers, timeout=20)
        html = r.text

        nowcast = {}

        # Look for CPI nowcast values in the page
        # Pattern: "CPI" followed by a percentage like "3.2%"
        cpi_pattern = r'(?:CPI|Consumer Price Index)[^%]*?(\d+\.?\d*)\s*%'
        cpi_matches = re.findall(cpi_pattern, html, re.IGNORECASE)
        if cpi_matches:
            try:
                nowcast["cpi_yoy"] = float(cpi_matches[0])
                log.info(f"  Cleveland Fed CPI nowcast: {nowcast['cpi_yoy']}%")
            except (ValueError, IndexError):
                pass

        # Core CPI
        core_pattern = r'(?:Core CPI|core.*?CPI)[^%]*?(\d+\.?\d*)\s*%'
        core_matches = re.findall(core_pattern, html, re.IGNORECASE)
        if core_matches:
            try:
                nowcast["core_cpi_yoy"] = float(core_matches[0])
                log.info(f"  Cleveland Fed Core CPI nowcast: {nowcast['core_cpi_yoy']}%")
            except (ValueError, IndexError):
                pass

        # PCE
        pce_pattern = r'(?:PCE)[^%]*?(\d+\.?\d*)\s*%'
        pce_matches = re.findall(pce_pattern, html, re.IGNORECASE)
        if pce_matches:
            try:
                nowcast["pce_yoy"] = float(pce_matches[0])
            except (ValueError, IndexError):
                pass

        return nowcast

    except Exception as e:
        log.error(f"  Cleveland Fed fetch failed: {e}")
        return {}


def fetch_gas_prices():
    """Fetch current national average gas price from AAA.

    Returns price in dollars (e.g. 3.45) or None on failure.
    """
    try:
        url = "https://gasprices.aaa.com/"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        r = retry_request("GET", url, headers=headers, timeout=20)
        html = r.text

        # Look for national average gas price
        price_pattern = r'\$(\d+\.\d{2,3})'
        prices = re.findall(price_pattern, html)
        if prices:
            price = float(prices[0])
            if 1.0 < price < 10.0:  # sanity check
                log.info(f"  AAA gas price: ${price}")
                return price

        return None
    except Exception as e:
        log.error(f"  AAA gas price fetch failed: {e}")
        return None


# === Market Matching ===

def parse_econ_threshold(market):
    """Extract threshold value from an economics market title/ticker.

    Examples:
      "Will CPI YoY be above 3.0%?" -> 3.0
      "KXCPI-JAN-T3.0" -> 3.0
    """
    title = market.get("title", "")
    ticker = market.get("ticker", "")

    # Try ticker first: KXCPI-...-T3.0 or KXCPI-...-B3.0
    m = re.search(r'-([TB])([\d.]+)$', ticker)
    if m:
        return float(m.group(2)), m.group(1)

    # Try title: "above 3.0%" or "between 3.0% and 3.5%"
    above_match = re.search(r'(?:above|over|more than)\s+(\d+\.?\d*)\s*%', title, re.I)
    if above_match:
        return float(above_match.group(1)), "T"

    below_match = re.search(r'(?:below|under|less than)\s+(\d+\.?\d*)\s*%', title, re.I)
    if below_match:
        return float(below_match.group(1)), "B_below"

    between_match = re.search(r'(?:between)\s+(\d+\.?\d*)\s*%?\s+and\s+(\d+\.?\d*)\s*%', title, re.I)
    if between_match:
        return float(between_match.group(1)), "B_range"

    return None, None


def estimate_days_to_release(market):
    """Estimate days until the economic data release this market tracks."""
    # Parse month/date hints from ticker
    ticker = market.get("ticker", "")
    title = market.get("title", "")

    # Check for month indicators
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

    for abbr, num in months.items():
        if abbr in ticker.upper():
            # BLS typically releases CPI mid-month for prior month
            # Rough estimate: target is ~15th of next month
            now = datetime.date.today()
            target_year = now.year
            target_month = num + 1
            if target_month > 12:
                target_month = 1
                target_year += 1
            try:
                target_date = datetime.date(target_year, target_month, 15)
                days = (target_date - now).days
                return max(0, days)
            except ValueError:
                pass

    # Default: assume mid-range uncertainty
    return 7


# === Scanning ===

def scan_and_trade():
    """Scan economics markets and trade on nowcast edge."""
    now = datetime.datetime.now()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Economics scan starting...")

    # Balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")
        return

    # Fetch nowcast data
    log.info("\nFetching economic data sources...")
    nowcast = fetch_cleveland_fed_nowcast()
    if nowcast:
        health.record_source_success("cleveland-fed")
    else:
        health.record_source_error("cleveland-fed", "empty nowcast")
    gas_price = fetch_gas_prices()
    if gas_price:
        health.record_source_success("aaa-gas")

    if not nowcast:
        log.info("No nowcast data available, skipping scan.")
        return

    # Fetch economics markets
    all_markets = []
    for prefix in ECON_PREFIXES:
        try:
            markets = client.get_all_markets(prefix=prefix, cache_ttl=300)
            all_markets.extend(markets)
        except Exception as e:
            log.error(f"Market fetch error for {prefix}: {e}")

    if not all_markets:
        log.info("No open economics markets found.")
        return

    log.info(f"Found {len(all_markets)} economics markets")

    # Evaluate each market
    opportunities = []
    for m in all_markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")

        threshold, direction_type = parse_econ_threshold(m)
        if threshold is None:
            continue

        # Determine which nowcast value to use
        nowcast_value = None
        if "CPI" in ticker.upper() or "INFLATION" in ticker.upper():
            nowcast_value = nowcast.get("cpi_yoy") or nowcast.get("core_cpi_yoy")
        elif "GDP" in ticker.upper():
            nowcast_value = nowcast.get("gdp_growth")
        elif "JOBS" in ticker.upper() or "EMPLOYMENT" in ticker.upper():
            nowcast_value = nowcast.get("nonfarm_payrolls")

        if nowcast_value is None:
            continue

        # Estimate uncertainty
        days_to_release = estimate_days_to_release(m)
        sigma = cpi_nowcast_sigma(days_to_release)

        # Compute probability
        if direction_type == "T":
            prob = econ_nowcast_probability(nowcast_value, sigma, threshold, "above")
        else:
            prob = econ_nowcast_probability(nowcast_value, sigma, threshold, "below")

        yes_ask = m.get("yes_ask", 0)
        no_ask = m.get("no_ask", 0)
        yes_bid = m.get("yes_bid", 0)

        if not yes_ask or yes_ask >= 99:
            continue

        # Determine trade direction (raw edge, fees handled in Kelly)
        if prob > 0.5:
            edge = prob - yes_ask / 100
            if edge > EDGE_THRESHOLD:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "yes",
                    "prob": prob, "edge": edge, "threshold": threshold,
                    "nowcast_value": nowcast_value, "sigma": sigma,
                    "days_to_release": days_to_release,
                })
            else:
                trade_manager.log_decision(
                    ticker, "yes", "skipped", "edge below threshold",
                    edge=edge, price_cents=yes_ask,
                )
        else:
            no_prob = 1.0 - prob
            edge = no_prob - (no_ask / 100 if no_ask else 1.0)
            if edge > EDGE_THRESHOLD:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "no",
                    "prob": no_prob, "edge": edge, "threshold": threshold,
                    "nowcast_value": nowcast_value, "sigma": sigma,
                    "days_to_release": days_to_release,
                })
            else:
                trade_manager.log_decision(
                    ticker, "no", "skipped", "edge below threshold",
                    edge=edge, price_cents=no_ask,
                )

    # Sort by edge
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    log.info(f"Found {len(opportunities)} opportunities with edge >= {EDGE_THRESHOLD*100:.0f}%")

    for opp in opportunities:
        ticker = opp["ticker"]
        side = opp["side"]
        edge = opp["edge"]
        m = opp["market"]
        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)

        budget = allocator.request_budget("economics", ticker, edge=edge, confidence=opp["prob"])
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            continue

        price = compute_limit_price(yes_bid, yes_ask, side, edge=edge) or (yes_ask if side == "yes" else no_ask)
        if not price or price <= 0:
            continue

        fee = kalshi_fee_cents(price)
        count, risk, kelly_details = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
        if count <= 0:
            continue

        reasoning = (
            f"Econ nowcast: {opp['nowcast_value']:.2f}% vs threshold {opp['threshold']:.1f}%, "
            f"sigma={opp['sigma']:.3f}, days_to_release={opp['days_to_release']}, "
            f"prob={opp['prob']*100:.0f}%, edge={edge*100:.1f}%"
        )

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c on {ticker}")

        result = trade_manager.place_order(ticker, side, price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(opp["prob"], 4), raw_edge=round(edge, 4),
                                            fee_cents=round(kalshi_fee_cents(price), 2), sizing_method="half_kelly",
                                            market_close_time=m.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"))
        if result:
            allocator.record_trade("economics", ticker, risk, edge=edge)


# === Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Kalshi Economics Bot")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Economics Bot (CPI/GDP/Jobs)")
    log.info(f"  Max: ${MAX_TRADE}/trade | Edge: {EDGE_THRESHOLD*100:.0f}%")
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
        scan_and_trade()
        return

    # Daemon loop
    while True:
        try:
            health.record_bot_heartbeat("economics")
            scan_and_trade()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
