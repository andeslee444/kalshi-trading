#!/usr/bin/env python3
"""Kalshi Strategy Trader — Applies research-backed strategies to find and place demo trades.
Strategies: Longshot bias selling, maker-only limit orders, info arbitrage near settlement.
"""

import json, time, datetime, os, sys, math
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, TradeManager, trim_trade_log, _atomic_write_json
from probability import half_kelly_sell

setup_unbuffered()
log = setup_logging("strategy")
setup_signal_handlers()

DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
_bots_cfg = json.loads(BOTS_CONFIG_PATH.read_text())["strategy"]
MAX_BET = _bots_cfg["maxBetCents"]

client = KalshiClient()

TRADES_JSON_PATH = DATA_DIR / "kalshi-strategy-trades.json"
trade_manager = TradeManager(client, TRADES_JSON_PATH, {
    "maxTradeAmount": MAX_BET / 100,
    "maxDailyTrades": _bots_cfg.get("maxDailyTrades", 20),
    "maxDailyLoss": _bots_cfg.get("maxDailyLoss", 50),
}, logger=log)
trim_trade_log(TRADES_JSON_PATH)

def find_longshot_sells(markets, bankroll):
    """Find contracts priced <10c YES to SELL (exploit longshot bias)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    for m in markets:
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        volume = m.get("volume", 0)
        ticker = m.get("ticker", "")

        if yes_ask <= 0 or yes_ask > 15:
            continue

        close_str = m.get("close_time", "")
        try:
            close_time = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            hours = (close_time - now).total_seconds() / 3600
        except (ValueError, TypeError):
            hours = 999

        if hours < 0.5:
            continue

        # Becker (2025) "Favourite-Longshot Bias in Prediction Markets":
        #   1-cent contracts are overpriced by ~57% (win rate 0.43% vs 1% implied).
        #   Mispricing decays exponentially with price: edge ≈ 0.57 * e^(-0.15 * price).
        #   At 5c, edge ≈ 27%; at 10c, edge ≈ 13%; at 15c, edge ≈ 6%.
        # Time-decay: full edge only if >24h to close, decay to 50% at 1h
        time_factor = min(1.0, 0.5 + 0.5 * min(hours, 24) / 24)
        est_edge = max(0, 0.57 * math.exp(-0.15 * yes_ask) * time_factor)

        if est_edge < 0.03:
            continue

        sell_price = max(yes_bid, yes_ask - 1) if yes_bid > 0 else yes_ask
        if sell_price <= 1:
            continue

        contracts, risk = half_kelly_sell(est_edge, sell_price, MAX_BET, bankroll_cents=bankroll)
        if contracts <= 0:
            continue

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
            "reasoning": f"Longshot bias: YES@{sell_price}c implies {sell_price}% prob, Becker model est true prob ~{sell_price - est_edge*100:.1f}%. Sell YES (buy NO@{100-sell_price}c) for ~{est_edge*100:.1f}% edge."
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

def main():
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
    log.info(f"  Found {len(markets)} open markets")

    # Strategy 1: Longshot bias selling
    log.info("\n" + "=" * 70)
    log.info("STRATEGY 1: Longshot Bias Exploitation (Sell YES on low-prob events)")
    log.info("=" * 70)
    longshots = find_longshot_sells(markets, avail)
    log.info(f"  Found {len(longshots)} longshot sell candidates")
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

    # Place trades — top 5 longshot sells
    log.info("\n" + "=" * 70)
    log.info("PLACING TRADES (Top 5 Longshot Sells)")
    log.info("=" * 70)

    trades_executed = []
    for c in longshots[:5]:
        ticker = c["ticker"]
        no_price = 100 - c["yes_price"]
        contracts = c["contracts"]

        log.info(f"\n  BUY {contracts}x NO @ {no_price}c on {ticker}")
        log.info(f"     ({c['reasoning']})")

        result = trade_manager.place_order(
            ticker, "no", no_price, contracts, c["reasoning"],
            strategy="longshot_sell", est_edge=f"{c['est_edge']*100:.1f}%",
            risk_cents=c["risk_cents"], title=c["title"],
            subtitle=c.get("subtitle", ""),
            yes_price_at_entry=c["yes_price"],
        )
        if result:
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

    log_path.write_text(existing + new_section)
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
    _atomic_write_json(perf_json_path, perf_data)

    log.info(f"\n{'='*70}")
    log.info(f"STRATEGY TRADER COMPLETE -- {len(trades_executed)} trades placed")
    log.info(f"{'='*70}")

if __name__ == "__main__":
    main()
