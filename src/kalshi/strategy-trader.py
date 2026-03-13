#!/usr/bin/env python3
"""Kalshi Strategy Trader — Applies research-backed strategies to find and place demo trades.
Strategies: Longshot bias selling, maker-only limit orders, info arbitrage near settlement.
"""

import json, time, datetime, os, sys, math, argparse, statistics
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, TradeManager, trim_trade_log, _atomic_write_json, build_market_snapshot, HealthCheckMonitor, OrderMonitor, ScanSummary, is_shutdown_requested
from probability import quarter_kelly_sell, quarter_kelly, half_kelly, longshot_edge, compute_limit_price, kalshi_fee_cents, classify_ticker_category
from capital_allocator import PortfolioAllocator
from strategy_engine import (
    BayesianEdgeEstimator, CorrelationAwareSizer,
    bayesian_kelly_multiplier, longshot_edge_sell, longshot_edge_buy, EdgeEstimate,
    ScheduledScanner, SettlementSourceChecker, FillProbabilityEstimator, InfoEdge,
)
from singleton_lock import acquire_process_singleton

setup_unbuffered()
log = setup_logging("strategy")
setup_signal_handlers()

DATA_DIR = PROJECT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
_bots_cfg = json.loads(BOTS_CONFIG_PATH.read_text())["strategy"]
MAX_BET = _bots_cfg.get("maxTradeAmount", 10) * 100  # dollars → cents

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
order_monitor = OrderMonitor(client, log=log)

SCAN_INTERVAL = _bots_cfg.get("scanIntervalMinutes", 15)

# Bayesian edge estimator (online learning from settlements)
BAYES_PARAMS_PATH = PROJECT_DIR / "config" / "bayes-params.json"
edge_estimator = BayesianEdgeEstimator(params_path=BAYES_PARAMS_PATH)

# Correlation-aware sizer (copula-based Kelly + category caps)
_daily_budget = _bots_cfg.get("maxDailyLoss", 100) * 100  # convert to cents
_category_cap = _bots_cfg.get("categoryCap", 0.30)
_single_trade_cap = _bots_cfg.get("singleTradeCap", 0.05)
correlation_sizer = CorrelationAwareSizer(
    daily_budget_cents=_daily_budget,
    category_cap_pct=_category_cap,
    single_trade_cap_pct=_single_trade_cap,
)

# Intraday wave scheduler
_wave_scheduling_enabled = _bots_cfg.get("waveScheduling", True)
_wave_daily_budget = _bots_cfg.get("dailyBudgetCents", _bots_cfg.get("maxDailyLoss", 100) * 100)
scheduler = ScheduledScanner(daily_budget_cents=_wave_daily_budget) if _wave_scheduling_enabled else None

# Settlement source checker
_settlement_sources_enabled = _bots_cfg.get("settlementSources", True)
settlement_checker = SettlementSourceChecker() if _settlement_sources_enabled else None

# Fill probability model
_fill_model_enabled = _bots_cfg.get("fillModel", True)
FILL_MODEL_PATH = DATA_DIR / _bots_cfg.get("fillModelPath", "strategy-fill-model.json")
fill_estimator = FillProbabilityEstimator(betas_path=FILL_MODEL_PATH) if _fill_model_enabled else None

# Config flags for new features
_bayesian_edge_enabled = _bots_cfg.get("bayesianEdge", True)
_buy_longshots_enabled = _bots_cfg.get("enableBuyLongshots", True)
_sell_max_price = _bots_cfg.get("sellMaxPrice", 30)
_buy_min_price = _bots_cfg.get("buyMinPrice", 70)

SPORTS_PREFIXES = ["KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXUFC", "KXNCAA", "KXSPORT", "KXSOCCER", "KXMARMAD"]

TRADES_JSON_PATH = DATA_DIR / "kalshi-strategy-trades.json"
METRICS_PATH = DATA_DIR / "strategy-metrics.json"
trade_manager = TradeManager(client, TRADES_JSON_PATH, {
    "maxTradeAmount": MAX_BET / 100,
    "maxTradeAmountPct": _bots_cfg.get("maxTradeAmountPct"),
    "maxDailyTrades": _bots_cfg.get("maxDailyTrades", 20),
    "maxDailyLoss": _bots_cfg.get("maxDailyLoss", 50),
    "maxDailyLossPct": _bots_cfg.get("maxDailyLossPct"),
}, logger=log, order_monitor=order_monitor, bot_name="strategy")
trim_trade_log(TRADES_JSON_PATH)

# Multi-outcome futures where longshot bias model doesn't apply
TOURNAMENT_PREFIXES = ("KXMARMAD-",)

def find_longshot_sells(markets, bankroll):
    """Find contracts priced <=30c YES to SELL (exploit longshot bias).

    Uses Bayesian edge model (when enabled) or category-adjusted Becker model
    via longshot_edge(). Expanded range from 15c to sellMaxPrice (default 30c).
    Applies copula-based correlation scaling and confidence-scaled Kelly.

    Returns:
        (sized_candidates, total_candidates_count, n_allocator_calls)
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    # Count concurrent same-category candidates for copula sizing
    category_counts = {}

    for m in markets:
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        volume = m.get("volume", 0)
        ticker = m.get("ticker", "")

        if yes_ask <= 0 or yes_ask > _sell_max_price:
            trade_manager.log_decision(ticker, "no", "skipped", "price_out_of_range",
                                       yes_ask=yes_ask)
            continue

        # Skip tournament/championship futures (multi-outcome, violates longshot bias premise)
        if any(ticker.startswith(p) for p in TOURNAMENT_PREFIXES):
            trade_manager.log_decision(ticker, "no", "skipped", "tournament_market",
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

        # Edge estimation: Bayesian posterior or fallback to point estimate
        if _bayesian_edge_enabled:
            est_edge_prelim = longshot_edge_sell(yes_ask, ticker=ticker,
                                                 hours_to_close=hours, estimator=edge_estimator)
        else:
            est_edge_prelim = longshot_edge(yes_ask, ticker=ticker, hours_to_close=hours)

        # Minimum edge filter
        fee_per_contract = kalshi_fee_cents(yes_ask)
        min_edge = 0.005  # 0.5% minimum edge (fees handled in Kelly sizing)
        if est_edge_prelim < min_edge:
            trade_manager.log_decision(ticker, "no", "skipped", "low_edge_prelim",
                                       edge=round(est_edge_prelim, 4), min_edge=min_edge,
                                       yes_ask=yes_ask)
            continue

        # Place limit within the spread instead of at full ask
        sell_price = compute_limit_price(yes_bid, yes_ask, "yes", edge=est_edge_prelim) if yes_bid else yes_ask

        # Fill probability adjustment
        if fill_estimator and _fill_model_enabled and yes_bid and yes_ask and yes_ask > yes_bid:
            mid = (yes_bid + yes_ask) / 2.0
            spread = yes_ask - yes_bid
            fill_prob = fill_estimator.estimate_fill_prob(sell_price, mid, spread)
            adjusted = fill_estimator.adjust_limit_price(sell_price, est_edge_prelim, fill_prob, yes_bid, yes_ask, "no")
            if adjusted != sell_price:
                log.debug(f"  Fill prob {fill_prob:.2f} -> adjusted limit {sell_price} -> {adjusted}")
                sell_price = adjusted

        # Recompute edge at the actual entry price (limit may differ from ask)
        if _bayesian_edge_enabled:
            est_edge = longshot_edge_sell(sell_price, ticker=ticker,
                                          hours_to_close=hours, estimator=edge_estimator)
        else:
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

        # Rec 5: Only sell longshots when NO <= 96c (profit/risk ratio floor)
        no_price = 100 - sell_price
        if no_price > 96:
            trade_manager.log_decision(ticker, "no", "skipped", "profit_risk_ratio",
                                       no_price=no_price, sell_price=sell_price,
                                       edge=round(est_edge, 4))
            continue

        # Bayesian edge estimate for confidence-scaled sizing
        category = classify_ticker_category(ticker)
        edge_est = None
        kelly_mult = 1.0
        copula_scale = 1.0
        if _bayesian_edge_enabled:
            edge_est = edge_estimator.estimate_edge(sell_price, category, hours)
            # Design doc: "With <10 trades per category: use Becker priors directly"
            if edge_est.n_observations >= 10:
                kelly_mult = bayesian_kelly_multiplier(edge_est.confidence_ratio)
            else:
                kelly_mult = 1.0  # trust Becker prior at full quarter-Kelly
            n_same = category_counts.get(category, 0) + 1
            rho = correlation_sizer.get_intra_category_rho(category)
            copula_scale = correlation_sizer.kelly_scale(n_same, rho)

        # Check category cap before sizing
        if not correlation_sizer.check_category_cap(category, no_price * 1):
            trade_manager.log_decision(ticker, "no", "skipped", "category_cap_exceeded",
                                       category=category)
            continue

        # Track for copula
        category_counts[category] = category_counts.get(category, 0) + 1

        implied_prob = sell_price / 100.0
        true_prob = implied_prob - est_edge

        mu_edge = edge_est.mu_edge if edge_est else est_edge
        sigma_edge = edge_est.sigma_edge if edge_est else 0.0
        conf_ratio = edge_est.confidence_ratio if edge_est else 0.0

        candidates.append({
            "ticker": ticker,
            "title": m.get("title", "")[:80],
            "subtitle": m.get("subtitle", "")[:60],
            "strategy": "longshot_sell",
            "side": "no",  # selling YES = buying NO
            "action": "buy",
            "price": 100 - sell_price,  # NO price = 100 - YES price
            "yes_price": sell_price,
            "est_edge": est_edge,
            "hours_to_close": hours,
            "volume": volume,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "close_time": m.get("close_time"),
            "mu_edge": mu_edge,
            "sigma_edge": sigma_edge,
            "confidence_ratio": conf_ratio,
            "kelly_multiplier": kelly_mult,
            "copula_scale": copula_scale,
            "fee_per_contract": fee_per_contract,
            "category": category,
            "reasoning": f"Longshot bias: YES@{sell_price}c implies {implied_prob*100:.1f}% prob, Becker model est true prob ~{true_prob*100:.2f}%. Sell YES (buy NO@{100-sell_price}c) for ~{est_edge*100:.2f}% edge. Bayesian CR={conf_ratio:.2f}, kelly_mult={kelly_mult:.2f}, copula={copula_scale:.2f}."
        })

    candidates.sort(key=lambda x: -x["est_edge"] * math.log1p(max(x.get("volume", 0), 1)))

    # Defer allocator calls to top-N candidates only (avoids thousands of
    # file-lock + JSON-parse cycles that cause 100% CPU spin).
    TOP_N = 20
    sized_candidates = []
    n_allocator_calls = 0
    for c in candidates[:TOP_N]:
        ticker = c["ticker"]
        est_edge = c["est_edge"]
        sell_price = c["yes_price"]
        kelly_mult = c["kelly_multiplier"]
        copula_scale = c["copula_scale"]
        fee_per_contract = c["fee_per_contract"]

        n_allocator_calls += 1
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

        # Apply Bayesian Kelly multiplier and copula scale
        if contracts > 0 and _bayesian_edge_enabled:
            adjusted = max(1, int(contracts * kelly_mult * copula_scale))
            contracts = adjusted
            risk_per = 100 - int(sell_price)
            risk = contracts * risk_per if risk_per > 0 else 0

        if contracts <= 0:
            trade_manager.log_decision(ticker, "no", "skipped", "kelly_zero",
                                       edge=est_edge, price_cents=sell_price)
            continue

        c["contracts"] = contracts
        c["risk_cents"] = risk
        c["kelly_fraction"] = kelly_details.get("kelly_fraction")
        c["bankroll_used"] = kelly_details.get("bankroll_used")
        sized_candidates.append(c)

    return sized_candidates, len(candidates), n_allocator_calls

def find_longshot_buys(markets, bankroll):
    """Find YES 70-99c contracts to BUY (exploit NO-side longshot bias).

    When YES is priced 70-99c, the NO side (1-30c) is the overpriced longshot.
    Buy YES to profit from NO-side longshot bias.

    Returns:
        (sized_candidates, total_candidates_count, n_allocator_calls)
    """
    if not _buy_longshots_enabled:
        return [], 0, 0

    now = datetime.datetime.now(datetime.timezone.utc)
    candidates = []
    category_counts = {}

    for m in markets:
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        volume = m.get("volume", 0)
        ticker = m.get("ticker", "")

        if yes_bid < _buy_min_price or yes_bid > 99:
            continue

        close_str = m.get("close_time", "")
        try:
            close_time = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            hours = (close_time - now).total_seconds() / 3600
        except (ValueError, TypeError):
            hours = 999

        if hours < 0.5:
            continue

        # Edge estimation: NO-side longshot bias
        if _bayesian_edge_enabled:
            est_edge = longshot_edge_buy(yes_bid, ticker=ticker,
                                          hours_to_close=hours, estimator=edge_estimator)
        else:
            est_edge = longshot_edge_buy(yes_bid, ticker=ticker, hours_to_close=hours)

        min_edge = 0.005
        if est_edge < min_edge:
            trade_manager.log_decision(ticker, "yes", "skipped", "low_buy_edge",
                                       edge=round(est_edge, 4), yes_bid=yes_bid)
            continue

        # Limit price within spread
        buy_price = compute_limit_price(yes_bid, yes_ask, "yes", edge=est_edge) if yes_ask else yes_bid

        # Fill probability adjustment
        if fill_estimator and _fill_model_enabled and yes_bid and yes_ask and yes_ask > yes_bid:
            mid = (yes_bid + yes_ask) / 2.0
            spread = yes_ask - yes_bid
            fill_prob = fill_estimator.estimate_fill_prob(buy_price, mid, spread)
            adjusted = fill_estimator.adjust_limit_price(buy_price, est_edge, fill_prob, yes_bid, yes_ask, "yes")
            if adjusted != buy_price:
                buy_price = adjusted

        if buy_price <= 0 or buy_price >= 100:
            continue

        # Bayesian confidence scaling
        category = classify_ticker_category(ticker)
        edge_est = None
        kelly_mult = 1.0
        copula_scale = 1.0
        if _bayesian_edge_enabled:
            no_price = 100 - yes_bid
            edge_est = edge_estimator.estimate_edge(no_price, category, hours)
            # Design doc: "With <10 trades per category: use Becker priors directly"
            if edge_est.n_observations >= 10:
                kelly_mult = bayesian_kelly_multiplier(edge_est.confidence_ratio)
            else:
                kelly_mult = 1.0  # trust Becker prior at full quarter-Kelly
            n_same = category_counts.get(category, 0) + 1
            rho = correlation_sizer.get_intra_category_rho(category)
            copula_scale = correlation_sizer.kelly_scale(n_same, rho)

        # Category cap check
        if not correlation_sizer.check_category_cap(category, buy_price):
            continue

        fee_per_contract = kalshi_fee_cents(buy_price)

        category_counts[category] = category_counts.get(category, 0) + 1

        no_equiv = 100 - yes_bid
        mu_edge = edge_est.mu_edge if edge_est else est_edge
        conf_ratio = edge_est.confidence_ratio if edge_est else 0.0

        candidates.append({
            "ticker": ticker,
            "title": m.get("title", "")[:80],
            "subtitle": m.get("subtitle", "")[:60],
            "strategy": "longshot_buy",
            "side": "yes",
            "action": "buy",
            "price": buy_price,
            "yes_price": buy_price,
            "est_edge": est_edge,
            "hours_to_close": hours,
            "volume": volume,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "close_time": m.get("close_time"),
            "kelly_multiplier": kelly_mult,
            "copula_scale": copula_scale,
            "fee_per_contract": fee_per_contract,
            "category": category,
            "reasoning": f"Buy-side longshot: YES@{buy_price}c, NO equiv {no_equiv}c longshot. Becker model edge ~{est_edge*100:.2f}%. CR={conf_ratio:.2f}, kelly_mult={kelly_mult:.2f}."
        })

    candidates.sort(key=lambda x: -x["est_edge"] * math.log1p(max(x.get("volume", 0), 1)))

    # Defer allocator calls to top-N candidates only (avoids thousands of
    # file-lock + JSON-parse cycles that cause 100% CPU spin).
    TOP_N = 20
    sized_candidates = []
    n_allocator_calls = 0
    for c in candidates[:TOP_N]:
        ticker = c["ticker"]
        est_edge = c["est_edge"]
        buy_price = c["yes_price"]
        kelly_mult = c["kelly_multiplier"]
        copula_scale = c["copula_scale"]
        fee_per_contract = c["fee_per_contract"]

        n_allocator_calls += 1
        budget = allocator.request_budget("strategy", ticker, edge=est_edge)
        if not budget.approved:
            continue

        contracts, risk, kelly_details = quarter_kelly(
            est_edge, buy_price, budget.max_cost_cents,
            bankroll_cents=budget.bankroll_cents, fee_cents=fee_per_contract,
            return_details=True,
        )

        # Apply Bayesian + copula scaling
        if contracts > 0 and _bayesian_edge_enabled:
            adjusted = max(1, int(contracts * kelly_mult * copula_scale))
            contracts = adjusted
            risk = contracts * buy_price

        if contracts <= 0:
            continue

        c["contracts"] = contracts
        c["risk_cents"] = risk
        c["kelly_fraction"] = kelly_details.get("kelly_fraction")
        c["bankroll_used"] = kelly_details.get("bankroll_used")
        sized_candidates.append(c)

    return sized_candidates, len(candidates), n_allocator_calls


def check_settled_trades():
    """Check if any previous trades have settled and update Bayesian model."""
    settled = []
    try:
        positions = client.get("/portfolio/positions")
        for p in positions.get("market_positions", []):
            if p.get("settlement_status") == "settled":
                settled.append(p)

        try:
            settlements = client.get("/portfolio/settlements")
            settled_list = settlements.get("settlements", [])
            # Update Bayesian edge model with settlement outcomes
            if settled_list and _bayesian_edge_enabled:
                # Load our trade records to determine strategy (buy vs sell side)
                our_trades = {}
                try:
                    from kalshi_auth import load_trades
                    for t in load_trades(TRADES_JSON_PATH):
                        our_trades[t.get("ticker", "")] = t
                except Exception:
                    pass

                for s in settled_list:
                    ticker = s.get("ticker", "")
                    if not ticker:
                        continue
                    # Only process settlements for OUR strategy trades
                    trade_rec = our_trades.get(ticker)
                    if not trade_rec:
                        continue

                    category = classify_ticker_category(ticker)
                    strategy = trade_rec.get("strategy", "")
                    price_cents = trade_rec.get("yes_price_at_entry", 5)

                    # Determine outcome: prefer local settlement_result
                    local_result = trade_rec.get("settlement_result")
                    if local_result == "won":
                        won = True
                    elif local_result == "lost":
                        won = False
                    else:
                        won = s.get("revenue", 0) > 0

                    # Fix side conflation: for buy-side trades (YES 70-99c),
                    # convert to NO-equivalent price (1-30c) before bucketing
                    if strategy == "longshot_buy" and price_cents > 30:
                        price_cents = 100 - price_cents  # NO-equivalent for bucketing
                        # For buy-side: "won" means YES resolved (buyer wins)
                        # For the Becker model, this means the longshot (NO side) LOST
                        won = not won  # flip: buyer winning = seller losing

                    # Clamp to valid bucket range
                    price_cents = max(1, min(30, price_cents))
                    edge_estimator.update_posterior(category, price_cents, won)
                try:
                    edge_estimator.save_params(BAYES_PARAMS_PATH)
                except Exception as e:
                    log.warning(f"Failed to save Bayes params: {e}")
            return settled_list
        except Exception:
            pass
    except Exception as e:
        log.error(f"  Error checking settlements: {e}")
    return settled

def run_scan():
    """Run a single strategy scan cycle."""
    scan_start = time.monotonic()
    allocator_calls = 0  # tracks total allocator.request_budget calls this scan

    # Reset daily caps at start of scan (idempotent — safe to call every scan)
    correlation_sizer.reset_daily()
    if scheduler:
        scheduler.reset_daily()

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

    trades_executed = []

    # Strategy 0: Settlement source info-arb (highest priority)
    info_arb_trades = []
    if settlement_checker and _settlement_sources_enabled:
        log.info("\n" + "=" * 70)
        log.info("STRATEGY 0: Settlement Source Info-Arb (High Conviction)")
        log.info("=" * 70)
        for m in markets:
            ticker = m.get("ticker", "")
            info_edge = settlement_checker.check_info_edge(ticker)
            if info_edge is None:
                continue
            yes_bid = m.get("yes_bid", 0)
            yes_ask = m.get("yes_ask", 0)
            if yes_ask <= 0 or yes_bid <= 0:
                continue

            log.info(f"  INFO-ARB: {ticker} | Source: {info_edge.source} | Edge: {info_edge.edge*100:.0f}% | Confidence: {info_edge.confidence*100:.0f}%")

            allocator_calls += 1
            budget = allocator.request_budget("strategy", ticker, edge=info_edge.edge)
            if not budget.approved:
                log.info(f"    Allocator denied: {budget.reason}")
                continue

            fee = kalshi_fee_cents(yes_ask)
            contracts, risk, kelly_details = half_kelly(
                info_edge.edge, yes_ask, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee,
                return_details=True,
            )
            if contracts <= 0:
                continue

            buy_price = yes_ask
            reasoning = f"Info-arb: {info_edge.source} confirms outcome. Edge {info_edge.edge*100:.0f}%, conf {info_edge.confidence*100:.0f}%. Half-Kelly @ full ask."
            result = trade_manager.place_order(
                ticker, "yes", buy_price, contracts, reasoning,
                strategy="info_arb", est_edge=f"{info_edge.edge*100:.0f}%",
                edge=round(info_edge.edge, 4),
                raw_edge=round(info_edge.edge, 4),
                risk_cents=risk, title=m.get("title", "")[:80],
                market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                sizing_method="half_kelly",
            )
            if result:
                allocator.record_trade("strategy", ticker, risk, edge=info_edge.edge)
                if scheduler:
                    scheduler.record_spend(risk)
                info_arb_trades.append({"ticker": ticker, "source": info_edge.source, "edge": info_edge.edge})
                trades_executed.append({
                    "ticker": ticker, "title": m.get("title", "")[:80],
                    "strategy": "info_arb", "direction": f"BUY YES @ {buy_price}c",
                    "contracts": contracts, "risk_cents": risk,
                    "est_edge": f"{info_edge.edge*100:.0f}%",
                    "reasoning": reasoning,
                    "order_id": result.get("order_id", "?"), "status": result.get("status", "?"),
                })
        log.info(f"  Info-arb trades: {len(info_arb_trades)}")

    # Strategy 1: Longshot bias selling
    log.info("\n" + "=" * 70)
    log.info("STRATEGY 1: Longshot Bias Exploitation (Sell YES on low-prob events)")
    log.info("=" * 70)
    longshots, sell_candidates_total, sell_allocator_calls = find_longshot_sells(markets, avail)
    allocator_calls += sell_allocator_calls
    log.info(f"  Found {len(longshots)} longshot sell candidates (from {sell_candidates_total} pre-filter)")
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

    # Place trades — top 10 longshot sells
    log.info("\n" + "=" * 70)
    log.info("PLACING TRADES (Top 10 Longshot Sells)")
    log.info("=" * 70)
    for c in longshots[:10]:
        ticker = c["ticker"]
        no_price = 100 - c["yes_price"]
        contracts = c["contracts"]

        log.info(f"\n  BUY {contracts}x NO @ {no_price}c on {ticker}")
        log.info(f"     ({c['reasoning']})")

        result = trade_manager.place_order(
            ticker, "no", no_price, contracts, c["reasoning"],
            strategy="longshot_sell", est_edge=f"{c['est_edge']*100:.2f}%",
            edge=round(c["est_edge"], 4),
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
            correlation_sizer.record_trade(classify_ticker_category(ticker), c["risk_cents"])
            if scheduler:
                scheduler.record_spend(c["risk_cents"])
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

    # Strategy 2: Buy-side longshot exploitation
    log.info("\n" + "=" * 70)
    log.info("STRATEGY 2: Buy-Side Longshot Exploitation (Buy YES on high-prob events)")
    log.info("=" * 70)
    buy_longshots, buy_candidates_total, buy_allocator_calls = find_longshot_buys(markets, avail)
    allocator_calls += buy_allocator_calls
    log.info(f"  Found {len(buy_longshots)} buy-side longshot candidates (from {buy_candidates_total} pre-filter)")

    for i, c in enumerate(buy_longshots[:5]):
        log.info(f"\n  {i+1}. {c['ticker']}")
        log.info(f"     {c['title']}")
        log.info(f"     YES@{c['yes_price']}c | Edge: {c['est_edge']*100:.1f}% | Contracts: {c['contracts']} | Risk: ${c['risk_cents']/100:.2f}")

    # Place buy-side trades (top 5)
    if buy_longshots:
        log.info("\n" + "=" * 70)
        log.info("PLACING TRADES (Top 5 Buy-Side Longshots)")
        log.info("=" * 70)

    for c in buy_longshots[:5]:
        ticker = c["ticker"]
        buy_price = c["yes_price"]
        contracts = c["contracts"]

        log.info(f"\n  BUY {contracts}x YES @ {buy_price}c on {ticker}")
        log.info(f"     ({c['reasoning']})")

        result = trade_manager.place_order(
            ticker, "yes", buy_price, contracts, c["reasoning"],
            strategy="longshot_buy", est_edge=f"{c['est_edge']*100:.2f}%",
            edge=round(c["est_edge"], 4),
            risk_cents=c["risk_cents"], title=c["title"],
            subtitle=c.get("subtitle", ""),
            yes_price_at_entry=c["yes_price"],
            market_snapshot=build_market_snapshot(yes_bid=c.get("yes_bid", 0), yes_ask=c.get("yes_ask", 0)),
            model_prob=round(1.0 - (100 - c["yes_price"]) / 100.0 + c["est_edge"], 4),
            raw_edge=round(c["est_edge"], 4),
            fee_cents=round(kalshi_fee_cents(c["yes_price"]), 2),
            sizing_method="quarter_kelly_buy",
            market_close_time=c.get("close_time"),
            kelly_fraction=c.get("kelly_fraction"),
            bankroll_used=c.get("bankroll_used"),
            hours_to_close=round(c.get("hours_to_close", 0), 2),
            ticker_category=classify_ticker_category(ticker),
        )
        if result:
            allocator.record_trade("strategy", ticker, c["risk_cents"], edge=c.get("est_edge", 0))
            correlation_sizer.record_trade(classify_ticker_category(ticker), c["risk_cents"])
            if scheduler:
                scheduler.record_spend(c["risk_cents"])
            trades_executed.append({
                "ticker": ticker,
                "title": c["title"],
                "subtitle": c.get("subtitle", ""),
                "strategy": "longshot_buy",
                "direction": f"BUY YES @ {buy_price}c",
                "yes_price": buy_price,
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
                "strategy": "longshot_buy",
                "direction": f"BUY YES @ {buy_price}c",
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
    new_section += f"**Markets Scanned**: {len(markets)} | **Sell Candidates**: {sell_candidates_total} (sized: {len(longshots)}) | **Buy Candidates**: {buy_candidates_total} (sized: {len(buy_longshots)})\n\n"

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

    # Write per-scan metrics
    scan_duration = time.monotonic() - scan_start
    all_edges = [c["est_edge"] for c in longshots] + [c["est_edge"] for c in buy_longshots]
    edge_distribution = {}
    if all_edges:
        sorted_edges = sorted(all_edges)
        edge_distribution = {
            "min": round(sorted_edges[0], 4),
            "median": round(statistics.median(sorted_edges), 4),
            "max": round(sorted_edges[-1], 4),
        }

    metrics = {
        "markets_scanned": len(markets),
        "longshot_sell_candidates": sell_candidates_total,
        "longshot_buy_candidates": buy_candidates_total,
        "trades_placed": ss.trades_placed,
        "edge_distribution": edge_distribution,
        "scan_duration_seconds": round(scan_duration, 2),
        "allocator_calls": allocator_calls,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    try:
        _atomic_write_json(METRICS_PATH, metrics)
    except Exception as e:
        log.warning(f"Failed to write scan metrics: {e}")

    log.info(f"\n{'='*70}")
    log.info(f"STRATEGY TRADER COMPLETE -- {len(trades_executed)} trades placed")
    log.info(f"  Scan: {scan_duration:.1f}s | Allocator calls: {allocator_calls}")
    log.info(f"{'='*70}")

    return len(trades_executed)

def main():
    parser = argparse.ArgumentParser(description="Kalshi Strategy Trader")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    if not acquire_process_singleton("strategy", PROJECT_DIR, log):
        log.warning("Duplicate strategy launch blocked; exiting.")
        return

    log.info("=" * 60)
    log.info("Kalshi Strategy Trader (Longshot Bias)")
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
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "ok",
                "trades_placed": trades_placed,
            })
        except Exception as e:
            _atomic_write_json(PROJECT_DIR / "data" / "strategy-last-run.json", {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "error",
                "error": str(e),
            })
            raise
        return

    # Daemon loop
    if scheduler:
        scheduler.reset_daily()
    while True:
        try:
            health.record_bot_heartbeat("strategy")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()

            if scheduler and _wave_scheduling_enabled:
                wave = scheduler.current_wave()
                if wave:
                    log.info(f"Wave {wave} scan (budget remaining: ${scheduler.remaining_budget()/100:.2f})")
                else:
                    log.info("Off-wave scan (no budget tracking)")
                run_scan()
            else:
                run_scan()
        except Exception as e:
            log.error("Scan error: %s", e, exc_info=True)

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break

        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
