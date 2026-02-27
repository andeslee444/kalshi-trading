#!/usr/bin/env python3
"""Kalshi Cross-Platform Arbitrage — Monitors price differences between Kalshi and Polymarket.

Phase 1 (current): Monitoring only. Reads Polymarket CLOB prices, fuzzy-matches
events to Kalshi markets, and logs spread opportunities. No execution.

Phase 2 (future): Soft arb — when Kalshi is cheap vs Polymarket consensus, buy Kalshi side only.
Phase 3 (future): Full arb — both legs (requires Polygon wallet for Polymarket).

Minimum viable spread after fees: ~2% (Kalshi ~0.7%, Polymarket ~0.01%).

Usage:
    python3 src/kalshi/cross-platform-arb.py          # daemon mode
    python3 src/kalshi/cross-platform-arb.py --once    # single scan
"""

import json, time, datetime, os, sys, re, argparse, traceback
from pathlib import Path
from difflib import SequenceMatcher
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, TradeManager, trim_trade_log, build_market_snapshot,
    _atomic_write_json, HealthCheckMonitor, ScanSummary,
)
from polymarket_client import PolymarketClient
from capital_allocator import PortfolioAllocator
from probability import quarter_kelly, compute_limit_price, kalshi_fee_cents

setup_unbuffered()
log = setup_logging("cross-platform-arb")
setup_signal_handlers()

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-arb-trades.json"
SPREADS_LOG = PROJECT_DIR / "data" / "arb-spread-log.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load config
bots_config = json.loads(BOTS_CONFIG_PATH.read_text())
arb_config = bots_config.get("cross_platform_arb", {})

SCAN_INTERVAL = arb_config.get("scanIntervalMinutes", 30)
MIN_SPREAD_PCT = arb_config.get("minSpreadPct", 0.02)  # 2% minimum spread
EXECUTION_ENABLED = arb_config.get("executionEnabled", False)  # Phase 1: monitoring only
MAX_TRADE = arb_config.get("maxTradeAmount", 10)
MAX_DAILY_TRADES = arb_config.get("maxDailyTrades", 10)
MAX_DAILY_LOSS = arb_config.get("maxDailyLoss", 25)

# Polymarket fee ~2% (taker fee on CLOB); Kalshi fee is price-dependent via kalshi_fee_cents()
POLYMARKET_FEE = 0.02

client = KalshiClient()
pm_client = PolymarketClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE,
    "maxDailyTrades": MAX_DAILY_TRADES,
    "maxDailyLoss": MAX_DAILY_LOSS,
}, logger=log)
trim_trade_log(TRADES_PATH)


# === Fuzzy Matching ===

def normalize_event_text(text):
    """Normalize event text for fuzzy matching."""
    text = text.lower().strip()
    # Remove common prefixes/suffixes
    for prefix in ["will ", "is ", "will the ", "the "]:
        if text.startswith(prefix):
            text = text[len(prefix):]
    # Remove punctuation
    text = re.sub(r'[^\w\s]', '', text)
    return text


def fuzzy_match_score(text1, text2):
    """Compute similarity score between two event descriptions."""
    norm1 = normalize_event_text(text1)
    norm2 = normalize_event_text(text2)
    return SequenceMatcher(None, norm1, norm2).ratio()


MIN_MATCH_SCORE = 0.75


def _extract_numbers(text):
    """Extract all numbers from text for secondary validation."""
    return set(re.findall(r'\d+\.?\d*', text))


def validate_match(k_text, p_text, score):
    """Require fuzzy score >= 0.75 AND matching numerical thresholds."""
    if score < MIN_MATCH_SCORE:
        return False
    k_nums = _extract_numbers(k_text)
    p_nums = _extract_numbers(p_text)
    # If both have numbers, at least one must overlap
    if k_nums and p_nums and not k_nums & p_nums:
        return False
    return True


def match_markets(kalshi_markets, polymarket_markets):
    """Find matching markets between Kalshi and Polymarket.

    Returns list of (kalshi_market, polymarket_market, score) tuples
    with score >= 0.75 AND matching numerical thresholds.
    """
    matches = []

    for km in kalshi_markets:
        k_title = km.get("title", "")
        k_subtitle = km.get("subtitle", "")
        k_text = f"{k_title} {k_subtitle}".strip()

        if not k_text:
            continue

        best_match = None
        best_score = 0

        for pm in polymarket_markets:
            p_question = pm.get("question", "")
            if not p_question:
                continue

            score = fuzzy_match_score(k_text, p_question)
            if score > best_score and validate_match(k_text, p_question, score):
                best_score = score
                best_match = pm

        if best_match:
            matches.append((km, best_match, best_score))

    return matches


# === Spread Analysis ===

def compute_spread(kalshi_market, pm_market):
    """Compute the price spread between Kalshi and Polymarket.

    Returns dict with spread info, or None if prices unavailable.
    """
    k_yes_ask = kalshi_market.get("yes_ask", 0)
    k_no_ask = kalshi_market.get("no_ask", 0)
    k_yes_bid = kalshi_market.get("yes_bid", 0)

    if not k_yes_ask or k_yes_ask >= 99:
        return None

    # Get Polymarket best bid (executable sell price, not midpoint)
    tokens = pm_market.get("tokens", [])
    pm_yes_bid = None
    for token in tokens:
        outcome = token.get("outcome", "").lower()
        if outcome == "yes":
            token_id = token.get("token_id")
            if token_id:
                pm_yes_bid = pm_client.get_best_bid(token_id)
            break

    # Fallback to market-level price (less accurate but better than nothing)
    if pm_yes_bid is None:
        pm_yes_bid = pm_market.get("outcomePrices")
        if isinstance(pm_yes_bid, list) and len(pm_yes_bid) > 0:
            try:
                pm_yes_bid = float(pm_yes_bid[0])
            except (ValueError, TypeError):
                pm_yes_bid = None

    if pm_yes_bid is None:
        return None

    k_yes_price = k_yes_ask / 100  # convert cents to decimal

    # Spread = Polymarket YES bid - Kalshi YES ask (positive = Kalshi is cheap)
    spread = pm_yes_bid - k_yes_price
    kalshi_buy_fee = kalshi_fee_cents(k_yes_ask) / 100 if k_yes_ask > 0 else 0.007
    kalshi_sell_fee = kalshi_fee_cents(k_yes_bid) / 100 if k_yes_bid > 0 else 0.007
    net_spread = spread - (kalshi_buy_fee + kalshi_sell_fee + POLYMARKET_FEE)

    return {
        "kalshi_yes_ask": k_yes_ask,
        "kalshi_yes_bid": k_yes_bid,
        "kalshi_no_ask": k_no_ask,
        "polymarket_yes_bid": round(pm_yes_bid, 4),
        "raw_spread": round(spread, 4),
        "net_spread": round(net_spread, 4),
        "is_tradeable": net_spread > MIN_SPREAD_PCT,
    }


def log_spread(kalshi_ticker, pm_question, spread_info):
    """Log spread opportunity to file for analysis."""
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "kalshi_ticker": kalshi_ticker,
        "polymarket_question": pm_question[:100],
        **spread_info,
    }

    try:
        existing = []
        if SPREADS_LOG.exists():
            existing = json.loads(SPREADS_LOG.read_text())
        existing.append(entry)
        # Keep last 1000 entries
        if len(existing) > 1000:
            existing = existing[-1000:]
        _atomic_write_json(SPREADS_LOG, existing)
    except Exception as e:
        log.error(f"Failed to log spread: {e}")


# === Main Scan ===

def scan_spreads():
    """Scan for cross-platform arbitrage opportunities."""
    ss = ScanSummary("cross-platform-arb", log)
    now = datetime.datetime.now()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Cross-platform arb scan starting...")

    if EXECUTION_ENABLED:
        log.info("  MODE: EXECUTION ENABLED (Phase 2)")
    else:
        log.info("  MODE: MONITORING ONLY (Phase 1)")

    # Fetch Kalshi markets (focus on liquid categories)
    kalshi_markets = []
    for prefix in ["KXCPI", "KXBTC", "KXETH", "KXPRES", "KXGOV",
                     "KXFED", "KXJOBS", "KXGDP"]:
        try:
            markets = client.get_all_markets(prefix=prefix, cache_ttl=300)
            kalshi_markets.extend(markets)
        except Exception as e:
            log.error(f"Kalshi market fetch error for {prefix}: {e}")

    if not kalshi_markets:
        log.info("No Kalshi markets found.")
        ss.finalize()
        return

    log.info(f"Fetched {len(kalshi_markets)} Kalshi markets")

    # Fetch Polymarket markets (search for matching categories)
    pm_markets = []
    for query in ["CPI", "Bitcoin", "Ethereum", "President", "Federal Reserve", "GDP", "jobs"]:
        try:
            results = pm_client.get_markets(query=query, limit=50)
            if isinstance(results, list):
                pm_markets.extend(results)
            time.sleep(0.5)  # rate limiting
        except Exception as e:
            log.error(f"Polymarket fetch error for '{query}': {e}")

    if not pm_markets:
        log.info("No Polymarket markets found.")
        ss.markets_fetched = len(kalshi_markets)
        ss.finalize()
        return

    log.info(f"Fetched {len(pm_markets)} Polymarket markets")

    # Match markets
    matches = match_markets(kalshi_markets, pm_markets)
    log.info(f"Found {len(matches)} matched market pairs")

    # Analyze spreads
    tradeable = 0
    for km, pm, score in matches:
        k_ticker = km.get("ticker", "")
        p_question = pm.get("question", "")

        spread = compute_spread(km, pm)
        if not spread:
            continue

        log_spread(k_ticker, p_question, spread)

        if spread["is_tradeable"]:
            tradeable += 1
            log.info(f"\n  SPREAD OPPORTUNITY:")
            log.info(f"    Kalshi: {k_ticker} YES@{spread['kalshi_yes_ask']}c")
            log.info(f"    Polymarket: {p_question[:60]}... YES bid@{spread['polymarket_yes_bid']*100:.0f}c")
            log.info(f"    Raw spread: {spread['raw_spread']*100:.1f}% | Net: {spread['net_spread']*100:.1f}%")
            log.info(f"    Match score: {score:.2f}")

            # Phase 2: Execute soft arb (Kalshi side only)
            if EXECUTION_ENABLED and spread["net_spread"] > MIN_SPREAD_PCT:
                edge = spread["net_spread"]
                budget = allocator.request_budget("cross-platform-arb", k_ticker,
                                                   edge=edge, confidence=0.5 + edge)
                if not budget.approved:
                    log.info(f"    Allocator denied: {budget.reason}")
                    trade_manager.log_decision(k_ticker, "yes", "skipped", f"allocator denied: {budget.reason}",
                                               edge=edge, price_cents=spread["kalshi_yes_ask"])
                    continue

                yes_bid = spread["kalshi_yes_bid"]
                yes_ask = spread["kalshi_yes_ask"]
                price = compute_limit_price(yes_bid, yes_ask, "yes", edge=edge) or yes_ask
                fee = kalshi_fee_cents(price)
                count, risk = quarter_kelly(edge, price, budget.max_cost_cents,
                                          bankroll_cents=budget.bankroll_cents, fee_cents=fee)
                if count > 0:
                    reasoning = (
                        f"Cross-platform arb: Kalshi {k_ticker} YES@{price}c vs "
                        f"Polymarket YES@{spread['polymarket_yes_bid']*100:.0f}c, "
                        f"net spread={spread['net_spread']*100:.1f}%"
                    )
                    result = trade_manager.place_order(k_ticker, "yes", price, count, reasoning,
                                                        market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                                        model_prob=round(0.5 + edge, 4), raw_edge=round(edge, 4),
                                                        fee_cents=round(kalshi_fee_cents(price), 2), sizing_method="quarter_kelly")
                    if result:
                        ss.trades_placed += 1
                        allocator.record_trade("cross-platform-arb", k_ticker, risk, edge=edge)
                else:
                    trade_manager.log_decision(k_ticker, "yes", "skipped", "kelly_zero",
                                               edge=edge, price_cents=spread["kalshi_yes_ask"])
            elif spread["is_tradeable"] and not EXECUTION_ENABLED:
                trade_manager.log_decision(k_ticker, "yes", "skipped", "execution_disabled",
                                           edge=spread["net_spread"], price_cents=spread["kalshi_yes_ask"])
        else:
            if abs(spread["raw_spread"]) > 0.01:
                log.info(f"  {k_ticker} vs PM: raw spread {spread['raw_spread']*100:.1f}% (below threshold after fees)")

    ss.markets_fetched = len(kalshi_markets)
    ss.markets_evaluated = len(matches)
    ss.finalize()
    log.info(f"\nScan complete. {tradeable} tradeable spreads found (of {len(matches)} matched pairs).")


# === Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Kalshi Cross-Platform Arbitrage Monitor")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Cross-Platform Arbitrage")
    log.info(f"  Mode: {'EXECUTION' if EXECUTION_ENABLED else 'MONITORING ONLY'}")
    log.info(f"  Min spread: {MIN_SPREAD_PCT*100:.1f}%  Scan interval: {SCAN_INTERVAL}min")
    log.info("=" * 60)

    # Verify auth
    log.info("\nVerifying Kalshi authentication...")
    try:
        balance, _ = client.get_balance()
        log.info(f"Kalshi auth OK! Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Kalshi auth failed: {e}")
        sys.exit(1)

    # Verify Polymarket connectivity
    log.info("Testing Polymarket API...")
    test = pm_client.get_markets(query="Bitcoin", limit=1)
    if test:
        log.info("Polymarket API OK!")
    else:
        log.warning("Polymarket API may be unavailable — will retry during scans")

    if args.once:
        scan_spreads()
        return

    # Daemon loop
    while True:
        try:
            health.record_bot_heartbeat("cross-platform-arb")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            scan_spreads()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
