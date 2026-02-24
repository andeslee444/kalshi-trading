#!/usr/bin/env python3
"""Kalshi Entertainment Markets Bot — Album Sales & Box Office Info Arbitrage
Monitors HITS Daily Double and Box Office Mojo for settlement data before markets adjust.
DEMO API ONLY — $5 max per trade.
"""

import json, time, datetime, os, sys, re, traceback
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, fetch_parallel, retry_request, TradeManager, trim_trade_log, build_market_snapshot, HealthCheckMonitor, OrderMonitor, ScanSummary
from probability import info_arb_probability, album_data_sigma, boxoffice_data_sigma, half_kelly, compute_limit_price, is_market_liquid, kalshi_fee_cents
from hdd_parser import get_album_sales, compute_data_age_hours, parse_album_threshold
from capital_allocator import PortfolioAllocator

setup_unbuffered()
setup_signal_handlers()

TRADES_PATH = PROJECT_DIR / "data" / "kalshi-entertainment-trades.json"
PID_PATH = PROJECT_DIR / "data" / "pids" / "kalshi-entertainment.pid"

TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
Path(PID_PATH).parent.mkdir(parents=True, exist_ok=True)

# Write PID
Path(PID_PATH).write_text(str(os.getpid()))

# Logging — auto file logging via setup_logging (writes to data/logs/entertainment.log)
log = setup_logging("entertainment")

# === Config from file ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
_bots_cfg = json.loads(BOTS_CONFIG_PATH.read_text())["entertainment"]
MAX_TRADE_AMOUNT = _bots_cfg["maxTradeAmount"]
MAX_DAILY_TRADES = _bots_cfg["maxDailyTrades"]
CONFIDENCE_THRESHOLD = _bots_cfg["confidenceThreshold"]
SCAN_INTERVAL_MINUTES = _bots_cfg["scanIntervalMinutes"]
ENTERTAINMENT_TICKERS = _bots_cfg["tickers"]

MIN_EDGE_CONFIRMED = 0.04  # 4% when sigma <= 5% (confirmed data)
MIN_EDGE_UNCERTAIN = 0.10  # 10% when sigma > 5% (projections/articles)
MAX_DATA_AGE_HOURS = 168  # HDD charts publish weekly; keep data fresh for 7 days

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
order_monitor = OrderMonitor(client, log=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE_AMOUNT,
    "maxDailyTrades": MAX_DAILY_TRADES,
    "maxDailyLoss": _bots_cfg.get("maxDailyLoss", 25),
}, logger=log, order_monitor=order_monitor)
trim_trade_log(TRADES_PATH)

# === Data Freshness ===
def _check_hdd_staleness(album_data):
    """Remove stale HDD entries (older than MAX_DATA_AGE_HOURS).

    Fail-open: entries with missing or unparseable chart_date pass through.
    Returns filtered list.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    fresh = []
    stale_count = 0
    for entry in album_data:
        chart_date = entry.get("chart_date", "")
        if not chart_date:
            fresh.append(entry)
            continue
        try:
            dt = datetime.datetime.fromisoformat(chart_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            age_hours = (now - dt).total_seconds() / 3600
            if age_hours <= MAX_DATA_AGE_HOURS:
                fresh.append(entry)
            else:
                stale_count += 1
        except (ValueError, TypeError):
            fresh.append(entry)  # fail-open on unparseable dates
    if stale_count:
        log.warning("Removed %d stale HDD entries (older than %dh)", stale_count, MAX_DATA_AGE_HOURS)
    return fresh


# === Market Discovery ===
def find_entertainment_markets():
    """Find all open entertainment-related markets using prefix-filtered fetches."""
    # Build unique ticker prefixes from keywords (e.g. "KXALBUMSALES" -> prefix "KXALBUMSALES")
    prefixes = set()
    keyword_list = []
    for kw in ENTERTAINMENT_TICKERS:
        kw_upper = kw.upper()
        # Use the keyword as a prefix if it looks like a ticker prefix, else add "KX" + keyword
        if kw_upper.startswith("KX"):
            prefixes.add(kw_upper)
        else:
            prefixes.add(f"KX{kw_upper}")
        keyword_list.append(kw.lower())

    markets = []
    seen_tickers = set()
    for prefix in prefixes:
        try:
            batch = client.get_all_markets(prefix=prefix, cache_ttl=300)
            for m in batch:
                ticker = m.get("ticker", "")
                if ticker not in seen_tickers:
                    markets.append(m)
                    seen_tickers.add(ticker)
        except Exception as e:
            log.error(f"Market fetch error for prefix {prefix}: {e}")

    # Also do a keyword-text match on markets we already fetched (no extra API call)
    # This catches markets whose ticker doesn't start with "KX+keyword" but whose
    # title/subtitle contains the keyword.  For that we'd need the full list, but
    # to avoid the 50K fetch we skip this — prefix matching covers the vast majority.

    return markets

# === Source: HITS Daily Double ===
def scrape_hdd():
    """Fetch album sales data from HDD via Sanity CMS API."""
    log.info("Checking HITS Daily Double (Sanity CMS)...")
    album_data = get_album_sales(logger=log)
    log.info(f"  Found {len(album_data)} album entries from HDD")
    return album_data

# === Source: Box Office Mojo ===
def scrape_box_office():
    """Scrape Box Office Mojo and The Numbers for weekend estimates (parallel fetch)."""
    log.info("Checking box office data...")

    box_data = []

    box_urls = [
        "https://www.boxofficemojo.com/",
        "https://www.the-numbers.com/market/",
        "https://www.boxofficemojo.com/weekend/",
    ]
    responses = fetch_parallel(box_urls, headers={"User-Agent": USER_AGENT}, timeout=20)

    # Box Office Mojo
    r = responses.get("https://www.boxofficemojo.com/")
    if r and r.status_code == 200:
        html = r.text
        log.info(f"  Box Office Mojo: {len(html)} bytes")
        movies = re.findall(r'>([^<]{3,60})</a>.*?\$([\d,.]+)\s*([MmBb])?', html, re.DOTALL)
        for match in movies[:15]:
            title = match[0].strip()
            gross_clean = match[1].replace(",", "")
            suffix = match[2].upper() if match[2] else ""
            try:
                val = float(gross_clean)
                if suffix == "B":
                    val *= 1_000_000_000
                elif suffix == "M":
                    val *= 1_000_000
                if val > 50_000:
                    box_data.append({"title": title, "gross": int(val), "source": "boxofficemojo"})
            except (ValueError, TypeError):
                pass
        if box_data:
            log.info(f"  Box Office Mojo: {len(box_data)} movies found")

    # The Numbers
    r = responses.get("https://www.the-numbers.com/market/")
    if r and r.status_code == 200:
        html = r.text
        matches = re.findall(r'>([^<]{3,60})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)', html)
        for title, gross in matches[:15]:
            title = title.strip()
            gross_val = int(gross.replace(",", ""))
            if gross_val > 50_000:
                box_data.append({"title": title, "gross": gross_val, "source": "the-numbers"})
        if matches:
            log.info(f"  The Numbers: {len(matches)} entries parsed")

    # Weekend estimates
    r = responses.get("https://www.boxofficemojo.com/weekend/")
    if r and r.status_code == 200:
        html = r.text
        movies = re.findall(r'>([^<]{3,60})</a>.*?\$([\d,.]+)\s*([MmBb])?', html, re.DOTALL)
        for match in movies[:15]:
            title = match[0].strip()
            gross_clean = match[1].replace(",", "")
            suffix = match[2].upper() if match[2] else ""
            try:
                val = float(gross_clean)
                if suffix == "B":
                    val *= 1_000_000_000
                elif suffix == "M":
                    val *= 1_000_000
                if val > 50_000 and not any(d["title"] == title for d in box_data):
                    box_data.append({"title": title, "gross": int(val), "source": "boxofficemojo-weekend"})
            except (ValueError, TypeError):
                pass

    return box_data

# === Market Matching & Trading ===
def match_and_trade(markets, album_data, box_data):
    """Match source data to markets and trade on high-confidence signals."""
    if not markets:
        log.info("No entertainment markets found on Kalshi")
        return

    log.info(f"Analyzing {len(markets)} entertainment markets against {len(album_data)} album entries and {len(box_data)} box office entries")

    for m in markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")
        subtitle = m.get("subtitle", "")
        title_lower = f"{title} {subtitle}".lower()

        yes_ask = m.get("yes_ask", 0)
        no_ask = m.get("no_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        last = m.get("last_price", 0)

        # Rec 7: Skip illiquid markets (prevents dead resting orders)
        if not is_market_liquid(m):
            continue

        if yes_bid and yes_ask:
            market_price = (yes_bid + yes_ask) / 2 / 100
        elif last:
            market_price = last / 100
        else:
            market_price = 0.5

        log.info(f"\n  {ticker}: {title}")
        log.info(f"     YES ask: {yes_ask}c | NO ask: {no_ask}c | Last: {last}c")

        # Try album matching — use word boundary matching to avoid false positives
        for album in album_data:
            artist_lower = album["artist"].lower()
            artist_words = [w for w in artist_lower.split() if len(w) > 2]

            if artist_words and all(re.search(r'\b' + re.escape(w) + r'\b', title_lower) for w in artist_words):
                log.info(f"     Potential match: {album['artist']} ({album['units']/1000:.0f}K units)")
                evaluate_album_opportunity(m, album, market_price)

        # Try box office matching — use word boundary matching
        for movie in box_data:
            movie_lower = movie["title"].lower()
            movie_words = [w for w in movie_lower.split() if len(w) > 3]

            if movie_words and all(re.search(r'\b' + re.escape(w) + r'\b', title_lower) for w in movie_words):
                log.info(f"     Potential match: {movie['title']} (${movie['gross']:,})")
                evaluate_boxoffice_opportunity(m, movie, market_price)

def evaluate_album_opportunity(market, album, market_price):
    """Evaluate album sales trade opportunity."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    units = album["units"]
    artist = album["artist"]

    # Parse threshold from market title
    threshold = parse_album_threshold(title, ticker)
    if not threshold:
        log.info(f"     Could not parse threshold from: {title}")
        return

    data_age_hours = compute_data_age_hours(album.get("chart_date", ""))
    sigma = album_data_sigma(datetime.datetime.now().weekday(),
                             hours_since_publication=data_age_hours,
                             source=album.get("source", ""))
    confidence = info_arb_probability(units, threshold, sigma)

    # Determine side: confidence > 0.5 means above threshold (YES), < 0.5 means below (NO)
    if confidence > 0.5:
        side = "yes"
    else:
        side = "no"
        confidence = 1.0 - confidence  # flip to confidence in NO direction

    if confidence < CONFIDENCE_THRESHOLD:
        log.info(f"     Confidence {confidence*100:.0f}% < {CONFIDENCE_THRESHOLD*100:.0f}% threshold, skipping")
        trade_manager.log_decision(
            ticker, side, "skipped", "confidence below threshold",
            edge=confidence - 0.5, price_cents=market.get("yes_ask", 0),
        )
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    max_cost = MAX_TRADE_AMOUNT * 100

    yes_bid = market.get("yes_bid", 0)

    if side == "yes" and yes_ask and yes_ask < 99:
        edge = confidence - yes_ask / 100
        min_edge = MIN_EDGE_CONFIRMED if sigma <= 0.05 else MIN_EDGE_UNCERTAIN
        if edge >= min_edge:
            # Request budget — info-arb with high confidence gets larger allocation
            budget = allocator.request_budget("entertainment", ticker, edge=edge, confidence=confidence, source_type="info_arb")
            if not budget.approved:
                log.info(f"     Allocator denied {ticker}: {budget.reason}")
                return
            price = compute_limit_price(yes_bid, yes_ask, "yes", edge=edge) or yes_ask
            fee = kalshi_fee_cents(price)
            count, risk, kelly_details = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
            if count <= 0:
                return
            reasoning = f"HDD: {artist} at {units/1000:.0f}K vs {threshold/1000:.0f}K threshold. YES@{price}c, conf={confidence*100:.0f}%"
            log.info(f"\nALBUM ARBITRAGE: {artist} {units/1000:.0f}K units > {threshold/1000:.0f}K")
            log.info(f"    {ticker} YES@{price}c | edge={edge*100:.1f}% | conf={confidence*100:.0f}%")
            result = trade_manager.place_order(ticker, "yes", price, count, reasoning, confidence=confidence,
                                                market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                                model_prob=round(confidence, 4), raw_edge=round(edge, 4),
                                                fee_cents=round(fee, 2), sizing_method="half_kelly",
                                                market_close_time=market.get("close_time"),
                                                kelly_fraction=kelly_details.get("kelly_fraction"),
                                                bankroll_used=kelly_details.get("bankroll_used"),
                                                artist=artist, units=units, threshold=threshold,
                                                data_sigma=round(sigma, 4), source_type="album")
            if result:
                allocator.record_trade("entertainment", ticker, risk, edge=edge)

    elif side == "no" and no_ask and no_ask < 99:
        edge = confidence - no_ask / 100
        min_edge = MIN_EDGE_CONFIRMED if sigma <= 0.05 else MIN_EDGE_UNCERTAIN
        if edge >= min_edge:
            budget = allocator.request_budget("entertainment", ticker, edge=edge, confidence=confidence, source_type="info_arb")
            if not budget.approved:
                log.info(f"     Allocator denied {ticker}: {budget.reason}")
                return
            price = compute_limit_price(yes_bid, yes_ask, "no", edge=edge) or no_ask
            fee = kalshi_fee_cents(price)
            count, risk, kelly_details = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
            if count <= 0:
                return
            reasoning = f"HDD: {artist} at {units/1000:.0f}K vs {threshold/1000:.0f}K threshold. NO@{price}c, conf={confidence*100:.0f}%"
            log.info(f"\nALBUM ARBITRAGE: {artist} {units/1000:.0f}K units < {threshold/1000:.0f}K")
            log.info(f"    {ticker} NO@{price}c | edge={edge*100:.1f}% | conf={confidence*100:.0f}%")
            result = trade_manager.place_order(ticker, "no", price, count, reasoning, confidence=confidence,
                                                market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                                model_prob=round(1.0 - confidence, 4), raw_edge=round(edge, 4),
                                                fee_cents=round(fee, 2), sizing_method="half_kelly",
                                                market_close_time=market.get("close_time"),
                                                kelly_fraction=kelly_details.get("kelly_fraction"),
                                                bankroll_used=kelly_details.get("bankroll_used"),
                                                artist=artist, units=units, threshold=threshold,
                                                data_sigma=round(sigma, 4), source_type="album")
            if result:
                allocator.record_trade("entertainment", ticker, risk, edge=edge)

def evaluate_boxoffice_opportunity(market, movie, market_price):
    """Evaluate box office trade opportunity."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    gross = movie["gross"]
    movie_title = movie["title"]

    threshold = None
    match = re.search(r'\$(\d+(?:\.\d+)?)\s*[MmBb](?:illion)?', title)
    if match:
        threshold = float(match.group(1)) * 1_000_000
    else:
        match = re.search(r'(\d+(?:\.\d+)?)\s*million', title, re.I)
        if match:
            threshold = float(match.group(1)) * 1_000_000

    if not threshold:
        return

    sigma = boxoffice_data_sigma(datetime.datetime.now().weekday())
    confidence = info_arb_probability(gross, threshold, sigma)

    if confidence > 0.5:
        side = "yes"
    else:
        side = "no"
        confidence = 1.0 - confidence

    if confidence < CONFIDENCE_THRESHOLD:
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    max_cost = MAX_TRADE_AMOUNT * 100

    yes_bid = market.get("yes_bid", 0)

    if side == "yes" and yes_ask and yes_ask < 99:
        edge = confidence - yes_ask / 100
        min_edge = MIN_EDGE_CONFIRMED if sigma <= 0.05 else MIN_EDGE_UNCERTAIN
        if edge >= min_edge:
            budget = allocator.request_budget("entertainment", ticker, edge=edge, confidence=confidence, source_type="info_arb")
            if not budget.approved:
                return
            price = compute_limit_price(yes_bid, yes_ask, "yes", edge=edge) or yes_ask
            fee = kalshi_fee_cents(price)
            count, risk, kelly_details = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
            if count <= 0:
                return
            reasoning = f"Box office: {movie_title} ${gross/1e6:.1f}M vs ${threshold/1e6:.0f}M. YES@{price}c, conf={confidence*100:.0f}%"
            log.info(f"\nBOX OFFICE ARBITRAGE: {movie_title} ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M")
            result = trade_manager.place_order(ticker, "yes", price, count, reasoning, confidence=confidence,
                                                market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                                model_prob=round(confidence, 4), raw_edge=round(edge, 4),
                                                fee_cents=round(fee, 2), sizing_method="half_kelly",
                                                market_close_time=market.get("close_time"),
                                                kelly_fraction=kelly_details.get("kelly_fraction"),
                                                bankroll_used=kelly_details.get("bankroll_used"),
                                                movie_title=movie_title, gross=gross, threshold=threshold,
                                                data_sigma=round(sigma, 4), source_type="boxoffice")
            if result:
                allocator.record_trade("entertainment", ticker, risk, edge=edge)

    elif side == "no" and no_ask and no_ask < 99:
        edge = confidence - no_ask / 100
        min_edge = MIN_EDGE_CONFIRMED if sigma <= 0.05 else MIN_EDGE_UNCERTAIN
        if edge >= min_edge:
            budget = allocator.request_budget("entertainment", ticker, edge=edge, confidence=confidence, source_type="info_arb")
            if not budget.approved:
                return
            price = compute_limit_price(yes_bid, yes_ask, "no", edge=edge) or no_ask
            fee = kalshi_fee_cents(price)
            count, risk, kelly_details = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
            if count <= 0:
                return
            reasoning = f"Box office: {movie_title} ${gross/1e6:.1f}M vs ${threshold/1e6:.0f}M. NO@{price}c, conf={confidence*100:.0f}%"
            log.info(f"\nBOX OFFICE ARBITRAGE: {movie_title} ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M")
            result = trade_manager.place_order(ticker, "no", price, count, reasoning, confidence=confidence,
                                                market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                                model_prob=round(1.0 - confidence, 4), raw_edge=round(edge, 4),
                                                fee_cents=round(fee, 2), sizing_method="half_kelly",
                                                market_close_time=market.get("close_time"),
                                                kelly_fraction=kelly_details.get("kelly_fraction"),
                                                bankroll_used=kelly_details.get("bankroll_used"),
                                                movie_title=movie_title, gross=gross, threshold=threshold,
                                                data_sigma=round(sigma, 4), source_type="boxoffice")
            if result:
                allocator.record_trade("entertainment", ticker, risk, edge=edge)

# === Main Loop ===
def scan():
    """Single scan cycle."""
    ss = ScanSummary("entertainment", log)
    log.info(f"\n{'='*60}")
    log.info(f"Entertainment market scan starting...")

    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance check failed: {e}")
        ss.finalize()
        return

    markets = find_entertainment_markets()
    ss.markets_fetched = len(markets)
    log.info(f"Found {len(markets)} entertainment markets")

    for m in markets[:20]:
        log.info(f"  {m.get('ticker')}: {m.get('title', '')[:80]}")

    album_data = []
    box_data = []

    try:
        album_data = scrape_hdd()
        album_data = _check_hdd_staleness(album_data)
        health.record_source_success("hdd")
    except Exception as e:
        log.error(f"HDD scrape error: {e}")
        health.record_source_error("hdd", str(e))
        traceback.print_exc()

    try:
        box_data = scrape_box_office()
        health.record_source_success("boxoffice")
    except Exception as e:
        log.error(f"Box office scrape error: {e}")
        health.record_source_error("boxoffice", str(e))
        traceback.print_exc()

    log.info(f"Source data: {len(album_data)} album entries, {len(box_data)} box office entries")

    if markets and (album_data or box_data):
        match_and_trade(markets, album_data, box_data)
    elif markets:
        log.info("Markets found but no source data to compare -- will retry next cycle")

    log.info(f"Scan complete.")
    ss.finalize()

def main():
    log.info("=" * 60)
    log.info("Kalshi Entertainment Markets Bot (DEMO)")
    log.info(f"   Max trade: ${MAX_TRADE_AMOUNT} | Confidence threshold: {CONFIDENCE_THRESHOLD*100:.0f}%")
    log.info(f"   Scan interval: {SCAN_INTERVAL_MINUTES} min | PID: {os.getpid()}")
    log.info("=" * 60)

    log.info("Verifying Kalshi authentication...")
    try:
        balance, _ = client.get_balance()
        log.info(f"Auth OK! Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Auth failed: {e}")
        sys.exit(1)

    while True:
        try:
            health.record_bot_heartbeat("entertainment")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()
            scan()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"Next scan in {SCAN_INTERVAL_MINUTES} minutes...")
        sys.stdout.flush()
        time.sleep(SCAN_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
