#!/usr/bin/env python3
"""Kalshi Strategy Trader — Applies research-backed strategies to find and place demo trades.
Strategies: Longshot bias selling, maker-only limit orders, info arbitrage near settlement.
"""

import json, time, datetime, os, sys, math, argparse, traceback
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, TradeManager, trim_trade_log, _atomic_write_json, build_market_snapshot, HealthCheckMonitor, OrderMonitor, ScanSummary
from probability import quarter_kelly_sell, longshot_edge, compute_limit_price, kalshi_fee_cents, classify_ticker_category
from capital_allocator import PortfolioAllocator

setup_unbuffered()
log = setup_logging("strategy")
setup_signal_handlers()

DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
_bots_cfg = json.loads(BOTS_CONFIG_PATH.read_text())["strategy"]
MAX_BET = _bots_cfg["maxBetCents"]

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
order_monitor = OrderMonitor(client, log=log)

SCAN_INTERVAL = _bots_cfg.get("scanIntervalMinutes", 15)

SPORTS_PREFIXES = ["KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXUFC", "KXNCAA", "KXSPORT", "KXSOCCER", "KXMARMAD"]

TRADES_JSON_PATH = DATA_DIR / "kalshi-strategy-trades.json"
trade_manager = TradeManager(client, TRADES_JSON_PATH, {
    "maxTradeAmount": MAX_BET / 100,
    "maxTradeAmountPct": _bots_cfg.get("maxBetPct"),
    "maxDailyTrades": _bots_cfg.get("maxDailyTrades", 20),
    "maxDailyLoss": _bots_cfg.get("maxDailyLoss", 50),
    "maxDailyLossPct": _bots_cfg.get("maxDailyLossPct"),
}, logger=log, order_monitor=order_monitor)
trim_trade_log(TRADES_JSON_PATH)

def find_longshot_sells(markets, bankroll):
    """Find contracts priced <15c YES to SELL (exploit longshot bias).

    Uses category-adjusted Becker model via longshot_edge() which returns
    a proper additive probability edge (implied_prob - true_prob).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for m in markets:
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        volume = m.get("volume", 0)
        ticker = m.get("ticker", "")

        if yes_ask <= 0 or yes_ask > 15:
            trade_manager.log_decision(ticker, "no", "skipped", "price_out_of_range",
                                       yes_ask=yes_ask)
            continue

        close_str = m.get("close_time", "")
        try:
            close_time = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            hours = (close_time - now).total_seconds() / 3600
        except (ValueError, TypeError):
            hours = 999

        if hours < 0.5:
            trade_manager.log_decision(ticker, "no", "skipped", "too_close_to_settlement",
                                       hours_to_close=round(hours, 2))
            continue

        # Category-adjusted Becker model: returns additive edge
        # (implied_prob - true_prob), correctly accounting for category-specific
        # bias strength and time decay
        est_edge_prelim = longshot_edge(yes_ask, ticker=ticker, hours_to_close=hours)

        # Minimum edge filter — quarter_kelly_sell already deducts fees from
        # win_amount, so no need to subtract fees here (avoids double-counting)
        fee_per_contract = kalshi_fee_cents(yes_ask)
        min_edge = 0.005  # 0.5% minimum edge (fees handled in Kelly sizing)
        if est_edge_prelim < min_edge:
            trade_manager.log_decision(ticker, "no", "skipped", "low_edge_prelim",
                                       edge=round(est_edge_prelim, 4), min_edge=min_edge,
                                       yes_ask=yes_ask)
            continue

        # Place limit within the spread instead of at full ask
        sell_price = compute_limit_price(yes_bid, yes_ask, "yes", edge=est_edge_prelim) if yes_bid else yes_ask
        # Recompute edge at the actual entry price (limit may differ from ask)
        est_edge = longshot_edge(sell_price, ticker=ticker, hours_to_close=hours)
        if est_edge < min_edge:
            trade_manager.log_decision(ticker, "no", "skipped", "low_edge_limit",
                                       edge=round(est_edge, 4), min_edge=min_edge,
                                       sell_price=sell_price)
            continue
        if sell_price <= 1:
            sell_price = max(yes_bid, yes_ask - 1) if yes_bid > 0 else yes_ask
        if sell_price <= 1:
            trade_manager.log_decision(ticker, "no", "skipped", "sell_price_too_low",
                                       sell_price=sell_price, yes_bid=yes_bid, yes_ask=yes_ask)
            continue

        # Rec 5: Only sell longshots when NO ≤ 96c (profit/risk ratio floor)
        # At NO=99c, profit:risk = 1:99. At NO=96c, ratio = 4:96 ≈ 4.2%
        no_price = 100 - sell_price
        if no_price > 96:
            trade_manager.log_decision(ticker, "no", "skipped", "profit_risk_ratio",
                                       no_price=no_price, sell_price=sell_price)
            continue

        # Request budget from portfolio allocator
        budget = allocator.request_budget("strategy", ticker, edge=est_edge)
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            trade_manager.log_decision(ticker, "no", "skipped", f"allocator denied: {budget.reason}",
                                       edge=est_edge, price_cents=sell_price)
            continue

        contracts, risk, kelly_details = quarter_kelly_sell(
            est_edge, sell_price, budget.max_cost_cents,
            bankroll_cents=budget.bankroll_cents, fee_cents=fee_per_contract,
            return_details=True,
        )
        if contracts <= 0:
            trade_manager.log_decision(ticker, "no", "skipped", "kelly_zero",
                                       edge=est_edge, price_cents=sell_price)
            continue

        implied_prob = yes_ask / 100.0
        true_prob = implied_prob - est_edge

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
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "close_time": m.get("close_time"),
            "kelly_fraction": kelly_details.get("kelly_fraction"),
            "bankroll_used": kelly_details.get("bankroll_used"),
            "reasoning": f"Longshot bias: YES@{sell_price}c implies {implied_prob*100:.1f}% prob, Becker model est true prob ~{true_prob*100:.2f}%. Sell YES (buy NO@{100-sell_price}c) for ~{est_edge*100:.2f}% edge."
        })

    candidates.sort(key=lambda x: -x["est_edge"] * math.log1p(x["volume"]))
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
        except (ValueError, TypeError):
            continue

        if hours < 0.5 or hours > 6:
            continue

        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        if yes_ask <= 0:
            continue

        spread = yes_ask - yes_bid if yes_bid > 0 else 100
        if spread > 20:
            continue

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
    settled = []
    try:
        positions = client.get("/portfolio/positions")
        for p in positions.get("market_positions", []):
            if p.get("settlement_status") == "settled":
                settled.append(p)

        try:
            settlements = client.get("/portfolio/settlements")
            return settlements.get("settlements", [])
        except Exception:
            pass
    except Exception as e:
        log.error(f"  Error checking settlements: {e}")
    return settled

def run_scan():
    """Run a single strategy scan cycle."""
    ss = ScanSummary("strategy", log)
    log.info("=" * 70)
    log.info("KALSHI STRATEGY TRADER")
    log.info(f"   {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 70)

    # Balance
    balance, avail = client.get_balance()
    log.info(f"\nBalance: ${balance/100:.2f} | Available: ${avail/100:.2f}")

    if avail < 100:
        log.warning("Warning: Low available balance -- existing positions may be tying up capital")

    # Existing positions
    log.info("\nCurrent Positions:")
    try:
        pos = client.get("/portfolio/positions")
        positions = pos.get("market_positions", [])
        if positions:
            for p in positions[:10]:
                t = p.get("ticker", "")
                yes_q = p.get("position", 0)
                cost = p.get("market_exposure", 0)
                log.info(f"  {t}: {yes_q} contracts, exposure: {cost}c")
        else:
            log.info("  (none)")
    except Exception as e:
        log.error(f"  Error: {e}")

    # Check settlements
    log.info("\nChecking Settled Trades:")
    settled = check_settled_trades()
    if settled:
        for s in settled[:5]:
            log.info(f"  {s}")
    else:
        log.info("  No settled trades found")

    # Fetch markets
    log.info("\nScanning all open markets...")
    markets = client.get_all_markets()
    ss.markets_fetched = len(markets)
    log.info(f"  Found {len(markets)} open markets")

    # Strategy 1: Longshot bias selling
    log.info("\n" + "=" * 70)
    log.info("STRATEGY 1: Longshot Bias Exploitation (Sell YES on low-prob events)")
    log.info("=" * 70)
    longshots = find_longshot_sells(markets, avail)
    log.info(f"  Found {len(longshots)} longshot sell candidates")
    sports_candidates = [c for c in longshots if any(
        c["ticker"].upper().startswith(p) for p in SPORTS_PREFIXES
    )]
    if sports_candidates:
        log.info(f"  Sports candidates: {len(sports_candidates)}")
        for sc in sports_candidates[:5]:
            log.info(f"    {sc['ticker']} | Edge: {sc['est_edge']*100:.1f}% | YES@{sc['yes_price']}c")
    for i, c in enumerate(longshots[:10]):
        log.info(f"\n  {i+1}. {c['ticker']}")
        log.info(f"     {c['title']}")
        if c['subtitle']: log.info(f"     {c['subtitle']}")
        log.info(f"     YES@{c['yes_price']}c | Edge: {c['est_edge']*100:.1f}% | Contracts: {c['contracts']} | Risk: ${c['risk_cents']/100:.2f}")
        log.info(f"     Closes in {c['hours_to_close']:.1f}h | Vol: {c['volume']}")

    # Strategy 2: Near-settlement info arb candidates
    log.info("\n" + "=" * 70)
    log.info("STRATEGY 2: Near-Settlement Markets (Info Arbitrage Candidates)")
    log.info("=" * 70)
    near_settle = find_near_settlement(markets)
    log.info(f"  Found {len(near_settle)} markets settling within 6h with reasonable spreads")
    for i, c in enumerate(near_settle[:10]):
        log.info(f"  {i+1}. {c['ticker']} -- {c['title']}")
        log.info(f"     Bid: {c['yes_bid']}c / Ask: {c['yes_ask']}c | Spread: {c['spread']}c | Close: {c['hours_to_close']:.1f}h")

    # Place trades — top 10 longshot sells
    log.info("\n" + "=" * 70)
    log.info("PLACING TRADES (Top 10 Longshot Sells)")
    log.info("=" * 70)

    trades_executed = []
    for c in longshots[:10]:
        ticker = c["ticker"]
        no_price = 100 - c["yes_price"]
        contracts = c["contracts"]

        log.info(f"\n  BUY {contracts}x NO @ {no_price}c on {ticker}")
        log.info(f"     ({c['reasoning']})")

        result = trade_manager.place_order(
            ticker, "no", no_price, contracts, c["reasoning"],
            strategy="longshot_sell", est_edge=f"{c['est_edge']*100:.2f}%",
            risk_cents=c["risk_cents"], title=c["title"],
            subtitle=c.get("subtitle", ""),
            yes_price_at_entry=c["yes_price"],
            market_snapshot=build_market_snapshot(yes_bid=c.get("yes_bid", 0), yes_ask=c.get("yes_ask", 0)),
            model_prob=round(c["yes_price"] / 100.0 - c["est_edge"], 4),
            raw_edge=round(c["est_edge"], 4),
            fee_cents=round(kalshi_fee_cents(c["yes_price"]), 2),
            sizing_method="quarter_kelly_sell",
            market_close_time=c.get("close_time"),
            kelly_fraction=c.get("kelly_fraction"),
            bankroll_used=c.get("bankroll_used"),
            hours_to_close=round(c.get("hours_to_close", 0), 2),
            ticker_category=classify_ticker_category(ticker),
        )
        if result:
            allocator.record_trade("strategy", ticker, c["risk_cents"], edge=c.get("est_edge", 0))
            trades_executed.append({
                "ticker": ticker,
                "title": c["title"],
                "subtitle": c.get("subtitle", ""),
                "strategy": "longshot_sell",
                "direction": "BUY NO (= SELL YES)",
                "no_price": no_price,
                "contracts": contracts,
                "risk_cents": c["risk_cents"],
                "est_edge": f"{c['est_edge']*100:.1f}%",
                "reasoning": c["reasoning"],
                "order_id": result.get("order_id", "?"),
                "status": result.get("status", "?"),
            })
        else:
            trades_executed.append({
                "ticker": ticker,
                "title": c["title"],
                "strategy": "longshot_sell",
                "direction": f"BUY NO @ {no_price}c",
                "status": "BLOCKED/FAILED",
            })

    # Final balance
    balance, avail = client.get_balance()
    log.info(f"\nFinal Balance: ${balance/100:.2f} | Available: ${avail/100:.2f}")

    # Write performance log
    log.info("\nWriting trade log...")
    log_path = DATA_DIR / "kalshi-trade-performance.md"

    existing = ""
    if log_path.exists():
        existing = log_path.read_text()

    new_section = f"\n\n## Trade Session: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    new_section += f"**Balance**: ${balance/100:.2f} | **Available**: ${avail/100:.2f}\n\n"
    new_section += f"**Markets Scanned**: {len(markets)} | **Longshot Candidates**: {len(longshots)} | **Near-Settlement**: {len(near_settle)}\n\n"

    if trades_executed:
        new_section += "### Trades Placed\n\n"
        new_section += "| # | Ticker | Direction | Price | Qty | Edge | Risk | Status | Reasoning |\n"
        new_section += "|---|--------|-----------|-------|-----|------|------|--------|----------|\n"
        for i, t in enumerate(trades_executed):
            new_section += f"| {i+1} | `{t['ticker'][:25]}` | {t['direction'][:20]} | {t.get('no_price', '?')}c | {t.get('contracts', '?')} | {t.get('est_edge', '?')} | ${t.get('risk_cents', 0)/100:.2f} | {t['status']} | {t.get('reasoning', '')[:60]} |\n"
    else:
        new_section += "### No trades placed this session\n"

    if settled:
        new_section += "\n### Settled Trades\n\n"
        for s in settled:
            new_section += f"- {s}\n"

    if not existing:
        existing = "# Kalshi Trade Performance Log\n\nAutomated trading performance tracking.\n"

    full_text = existing + new_section
    # Keep last 50KB to prevent unbounded growth
    if len(full_text) > 50000:
        full_text = full_text[-50000:]
    log_path.write_text(full_text)
    log.info(f"  Logged to {log_path}")

    # Trade data already saved by TradeManager; save performance summary
    perf_json_path = DATA_DIR / "kalshi-strategy-performance.json"
    perf_data = []
    if perf_json_path.exists():
        try:
            perf_data = json.loads(perf_json_path.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    perf_data.extend(trades_executed)
    # Keep last 500 entries to prevent unbounded growth
    if len(perf_data) > 500:
        perf_data = perf_data[-500:]
    _atomic_write_json(perf_json_path, perf_data)

    ss.trades_placed = len([t for t in trades_executed if t.get("status") not in ("BLOCKED/FAILED",)])
    ss.finalize()

    log.info(f"\n{'='*70}")
    log.info(f"STRATEGY TRADER COMPLETE -- {len(trades_executed)} trades placed")
    log.info(f"{'='*70}")

def main():
    parser = argparse.ArgumentParser(description="Kalshi Strategy Trader")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Strategy Trader (Longshot Bias + Near-Settlement)")
    log.info(f"  Max bet: ${MAX_BET/100:.0f} | Scan interval: {SCAN_INTERVAL} min")
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
        trades_placed = 0
        try:
            trades_placed = run_scan() or 0
            _atomic_write_json(PROJECT_DIR / "data" / "strategy-last-run.json", {
                "timestamp": datetime.datetime.now().isoformat(),
                "status": "ok",
                "trades_placed": trades_placed,
            })
        except Exception as e:
            _atomic_write_json(PROJECT_DIR / "data" / "strategy-last-run.json", {
                "timestamp": datetime.datetime.now().isoformat(),
                "status": "error",
                "error": str(e),
            })
            raise
        return

    # Daemon loop
    while True:
        try:
            health.record_bot_heartbeat("strategy")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()
            run_scan()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
