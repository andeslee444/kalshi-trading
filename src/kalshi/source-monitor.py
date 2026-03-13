#!/usr/bin/env python3
"""Kalshi Settlement Source Monitor — Information Arbitrage Trading Bot
Monitors official data sources that Kalshi uses to settle markets.
When a source publishes data revealing the outcome BEFORE Kalshi settles, auto-trades on mispricing.

Sources:
  1. HITS Daily Double (album sales) — every 15 min
  2. Box Office Mojo / The Numbers — every 30 min (Fri-Mon)
  3. NWS actual temperatures — every 10 min
"""

import json, time, datetime, os, sys, re, hashlib
from pathlib import Path
from zoneinfo import ZoneInfo
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, fetch_parallel, retry_request, TradeManager, trim_trade_log, build_market_snapshot, CITY_TIMEZONES, _local_today, round_half_up, HealthCheckMonitor, OrderMonitor, ScanSummary, is_shutdown_requested
from probability import info_arb_probability, album_data_sigma, boxoffice_data_sigma, nws_probability, quarter_kelly, compute_limit_price, kalshi_fee_cents, is_market_liquid, nws_sigma_for_hour
from ticker_utils import parse_weather_ticker as parse_temp_ticker
from hdd_parser import get_album_sales, compute_data_age_hours, parse_album_threshold, check_sanity_health
from capital_allocator import PortfolioAllocator
from singleton_lock import acquire_process_singleton

setup_unbuffered()
log = setup_logging("source-monitor")
setup_signal_handlers()

# === Paths ===
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-monitor-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-monitor-trades.json"
SNAPSHOTS_DIR = PROJECT_DIR / "data" / "kalshi-source-snapshots"
METRICS_PATH = PROJECT_DIR / "data" / "source-monitor-metrics.json"

# Ensure dirs
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Load config
config = json.loads(CONFIG_PATH.read_text())

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
order_monitor = OrderMonitor(client, log=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": config["maxTradeAmount"],
    "maxTradeAmountPct": config.get("maxTradeAmountPct"),
    "maxDailyTrades": config["maxDailyTrades"],
    "maxDailyLoss": config["maxDailyLoss"],
    "maxDailyLossPct": config.get("maxDailyLossPct"),
}, logger=log, order_monitor=order_monitor, bot_name="source-monitor")
trim_trade_log(TRADES_PATH)

def save_snapshot(source_name, content, ext="html"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{source_name}_{ts}.{ext}"
    (SNAPSHOTS_DIR / fname).write_text(content[:500000] if isinstance(content, str) else json.dumps(content, indent=2)[:500000])
    # Cleanup: delete snapshots older than 7 days
    try:
        cutoff = time.time() - 7 * 86400
        for old_file in SNAPSHOTS_DIR.iterdir():
            if old_file.is_file() and old_file.stat().st_mtime < cutoff:
                old_file.unlink()
    except Exception:
        pass
    return fname

MAX_DATA_AGE_HOURS = 168  # 7 days — same as entertainment-bot
NWS_MAX_OBS_AGE_MINUTES = 90  # Skip NWS trades if observation is older than this

_compute_data_age_hours = compute_data_age_hours  # backward compat alias


def _compute_nws_obs_age_minutes(obs_ts):
    """Compute age in minutes of an NWS observation timestamp.

    Returns age in minutes, or None if timestamp is missing/unparseable.
    """
    if not obs_ts:
        return None
    try:
        obs_dt = datetime.datetime.fromisoformat(obs_ts.replace("Z", "+00:00"))
        age_seconds = (datetime.datetime.now(datetime.timezone.utc) - obs_dt).total_seconds()
        return age_seconds / 60
    except (ValueError, TypeError):
        return None

MAX_METRICS_ENTRIES = 1000  # Keep last 1000 scan metrics (rolling)


def _build_scan_metrics(ss, sources_checked=None, nws_freshness=None):
    """Build a metrics dict from a completed scan cycle.

    Args:
        ss: ScanSummary instance (after finalize)
        sources_checked: list of source names checked this cycle
        nws_freshness: dict of city -> obs_age_minutes (from check_nws)
    """
    metrics = {
        "sources_checked": sources_checked or [],
        "markets_fetched": ss.markets_fetched if ss else 0,
        "markets_evaluated": ss.markets_evaluated if ss else 0,
        "trades_placed": ss.trades_placed if ss else 0,
        "skips": dict(ss.skips) if ss else {},
        "data_sources": dict(ss.data_sources) if ss else {},
    }
    if nws_freshness:
        metrics["nws_freshness"] = nws_freshness
    return metrics


def _log_scan_metrics(scan_data):
    """Append per-scan metrics to the rolling metrics log.

    Each entry captures: timestamp, NWS data freshness per city,
    HDD data availability, trades placed, sigma values, and source status.
    Keeps at most MAX_METRICS_ENTRIES entries (FIFO).
    """
    try:
        existing = []
        if METRICS_PATH.exists():
            try:
                existing = json.loads(METRICS_PATH.read_text())
            except (json.JSONDecodeError, ValueError):
                existing = []

        scan_data["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        existing.append(scan_data)

        # Trim to max entries
        if len(existing) > MAX_METRICS_ENTRIES:
            existing = existing[-MAX_METRICS_ENTRIES:]

        METRICS_PATH.write_text(json.dumps(existing, indent=2, default=str))
    except Exception as e:
        log.warning(f"Failed to write scan metrics: {e}")


def _check_with_retry(check_fn, source_name, prefetched, ss, max_retries=2):
    """Retry a source check with exponential backoff on transient failures."""
    for attempt in range(max_retries + 1):
        try:
            check_fn(prefetched_markets=prefetched, ss=ss)
            health.record_source_success(source_name)
            if ss:
                ss.source_ok(source_name)
            return
        except Exception as e:
            if attempt < max_retries:
                delay = 2 ** attempt
                log.warning(f"{source_name} attempt {attempt+1} failed: {e}, retrying in {delay}s")
                time.sleep(delay)
            else:
                log.error("%s failed after %d attempts: %s", source_name, max_retries+1, e, exc_info=True)
                health.record_source_error(source_name, str(e))
                if ss:
                    ss.source_fail(source_name, str(e))

def _validate_market_cluster(entity_key, market_signals):
    """Check that markets for the same entity have monotonically decreasing
    probabilities as thresholds increase.

    market_signals: list of (market, threshold, probability) tuples
    Returns filtered consistent list, or empty list if contradictory.
    """
    if len(market_signals) < 2:
        return market_signals
    sorted_signals = sorted(market_signals, key=lambda s: s[1])
    for i in range(len(sorted_signals) - 1):
        lower_prob = sorted_signals[i][2]
        higher_prob = sorted_signals[i + 1][2]
        if lower_prob < higher_prob - 0.05:  # 5% tolerance
            log.warning(
                f"Inconsistent {entity_key}: P(>{sorted_signals[i][1]})={lower_prob:.0%} "
                f"< P(>{sorted_signals[i+1][1]})={higher_prob:.0%} — skipping all"
            )
            return []
    return sorted_signals

def _nws_min_edge(running_high, threshold, hour, is_bracket):
    """Compute minimum edge threshold for NWS trades based on CI confidence.

    Uses 99% CI of NWS observation error:
      very confident (margin > ci_99):    5% min edge
      moderate (margin > ci_99 * 0.5):   10% min edge
      uncertain:                         15% min edge
      brackets:                          20% min edge (always)
    """
    if is_bracket:
        return 0.20
    sigma = nws_sigma_for_hour(hour)
    ci_margin = abs(running_high - threshold)
    ci_99 = 2.576 * sigma
    if ci_margin > ci_99:
        return 0.05
    elif ci_margin > ci_99 * 0.5:
        return 0.10
    return 0.15

# === Kalshi Market Helpers ===
def get_markets_by_prefix(prefix, status="open"):
    """Get all open markets matching a ticker prefix."""
    return client.get_all_markets(prefix=prefix, status=status)

# ============================================================
# SOURCE 1: HITS Daily Double (Album Sales)
# ============================================================

def check_hdd(prefetched_markets=None, ss=None):
    """Fetch album sales data from HDD via Sanity CMS API."""
    log.info(f"\n[HDD] Checking HITS Daily Double (Sanity CMS)...")

    try:
        found_data = get_album_sales(logger=log)
        if found_data:
            log.info(f"  Found {len(found_data)} album sales entries")
            save_snapshot("hdd", json.dumps(found_data, default=str), ext="json")
            match_hdd_to_markets(found_data, prefetched_markets=prefetched_markets, ss=ss)
        else:
            log.info(f"  No album sales data found")
    except Exception as e:
        log.error(f"  HDD check failed: {e}")

def should_retry_hdd():
    """Check if HDD scanning should be re-enabled after previous errors.

    Calls the Sanity CMS health check. If healthy, returns True.
    """
    hdd_config = config.get("sources", {}).get("hdd", {})
    project_id = hdd_config.get("sanityProject", "8aky18h3")
    return check_sanity_health(project_id)

def match_hdd_to_markets(sales_data, prefetched_markets=None, ss=None):
    """Match parsed album sales data to open Kalshi markets."""
    try:
        if prefetched_markets is not None:
            markets = prefetched_markets.get("album", [])
        else:
            markets = get_markets_by_prefix("KXALBUMSALES")
            if not markets:
                markets = get_markets_by_prefix("KXALBUM")

        if not markets:
            log.info(f"  No open album sales markets found on Kalshi")
            return

        if ss:
            ss.markets_fetched += len(markets)
        log.info(f"  Found {len(markets)} album sales markets")

        for sale in sales_data:
            artist = sale["artist"].lower()
            units = sale["units"]

            # Group all matching markets for consistency check
            artist_markets = []
            for m in markets:
                title = m.get("title", "").lower()
                subtitle = m.get("subtitle", "").lower()
                if re.search(r'\b' + re.escape(artist) + r'\b', title) or re.search(r'\b' + re.escape(artist) + r'\b', subtitle):
                    if not is_market_liquid(m, min_volume=5, max_spread=40):
                        if ss:
                            ss.skip("illiquid")
                        continue
                    # Parse threshold for consistency check
                    threshold = parse_album_threshold(m.get("title", ""), m.get("ticker", ""))
                    if threshold:
                        data_age_hours = compute_data_age_hours(sale.get("chart_date"))
                        sigma = album_data_sigma(datetime.datetime.now().weekday(), hours_since_publication=data_age_hours, source=sale.get("source", ""))
                        prob = info_arb_probability(units, threshold, sigma)
                        artist_markets.append((m, threshold, prob))

            # Validate consistency, then evaluate each
            consistent = _validate_market_cluster(artist, artist_markets)
            for m, _threshold, _prob in consistent:
                if ss:
                    ss.markets_evaluated += 1
                evaluate_album_trade(m, sale, ss=ss)

    except Exception as e:
        log.error(f"  Market matching failed: {e}")

def evaluate_album_trade(market, sale, ss=None):
    """Evaluate and potentially execute a trade based on album sales data."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    units = sale["units"]
    artist = sale["artist"]

    threshold = parse_album_threshold(title, ticker)
    if not threshold:
        log.info(f"  Could not parse threshold from market: {title}")
        if ss:
            ss.skip("threshold_parse_fail")
        trade_manager.log_decision(ticker, "skip", "skipped", "threshold_parse_fail",
                                   price_cents=market.get("yes_ask", 0))
        return

    data_age_hours = compute_data_age_hours(sale.get("chart_date"))
    if data_age_hours > MAX_DATA_AGE_HOURS:
        log.info(f"  {artist}: data {data_age_hours:.0f}h stale (>{MAX_DATA_AGE_HOURS}h), skipping")
        if ss:
            ss.skip("stale_data")
        trade_manager.log_decision(
            ticker, "skip", "skipped", f"data {data_age_hours:.0f}h stale",
            edge=0, price_cents=market.get("yes_ask", 0),
        )
        return
    sigma = album_data_sigma(datetime.datetime.now().weekday(), hours_since_publication=data_age_hours, source=sale.get("source", ""))
    confidence = info_arb_probability(units, threshold, sigma)

    if confidence > 0.5:
        outcome = "yes"
    else:
        outcome = "no"
        confidence = 1.0 - confidence

    if confidence < 0.60:
        log.info(f"  {artist}: {units} units vs {threshold} threshold, confidence {confidence*100:.0f}% too low")
        if ss:
            ss.skip("low_confidence")
        trade_manager.log_decision(
            ticker, outcome, "skipped", "confidence below 60%",
            edge=confidence - 0.5, price_cents=market.get("yes_ask", 0),
            confidence=round(confidence, 4), sigma=round(sigma, 4),
            units=units, threshold=threshold, source=sale.get("source", ""),
        )
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    yes_bid = market.get("yes_bid", 0)

    # Lower edge threshold for confirmed data (sigma <= 5%)
    min_edge = 0.04 if sigma <= 0.05 else 0.10

    if outcome == "yes" and yes_ask and yes_ask < 99:
        edge = confidence - yes_ask / 100
        if edge <= min_edge:
            if ss:
                ss.skip("low_edge")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "edge_below_min",
                edge=round(edge, 4), price_cents=yes_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4), min_edge=min_edge,
                units=units, threshold=threshold, source=sale.get("source", ""),
            )
            return
        budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence, source_type="info_arb")
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            if ss:
                ss.skip("allocator_denied")
            trade_manager.log_decision(
                ticker, outcome, "skipped", f"allocator_denied: {budget.reason}",
                edge=round(edge, 4), price_cents=yes_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        price = compute_limit_price(yes_bid, yes_ask, "yes", edge=edge) or yes_ask
        fee = kalshi_fee_cents(price)
        count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
        if count <= 0:
            if ss:
                ss.skip("kelly_zero")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "kelly_zero",
                edge=round(edge, 4), price_cents=price,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units vs {threshold/1000:.0f}K threshold. YES at {price}c, confidence {confidence*100:.0f}%"
        log.info(f"\nARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
        log.info(f"    Market: {ticker} YES at {price}c -> buying YES (confirmed outcome)")
        log.info(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {price}c = ${count*price/100:.2f}")
        result = trade_manager.place_order(ticker, "yes", price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(confidence, 4), raw_edge=round(edge, 4),
                                            fee_cents=round(fee, 2), sizing_method="quarter_kelly",
                                            market_close_time=market.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"),
                                            artist=artist, units=units, threshold=threshold,
                                            data_sigma=round(sigma, 4), source_type="album")
        if result:
            if ss:
                ss.trades_placed += 1
            trade_manager.log_decision(
                ticker, outcome, "placed", "info_arb",
                edge=round(edge, 4), price_cents=price, count=count,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
                units=units, threshold=threshold, source=sale.get("source", ""),
            )
            allocator.record_trade("source-monitor", ticker, risk, edge=edge)

    elif outcome == "no" and no_ask and no_ask < 99:
        edge = confidence - no_ask / 100
        if edge <= min_edge:
            if ss:
                ss.skip("low_edge")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "edge_below_min",
                edge=round(edge, 4), price_cents=no_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4), min_edge=min_edge,
                units=units, threshold=threshold, source=sale.get("source", ""),
            )
            return
        budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence, source_type="info_arb")
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            if ss:
                ss.skip("allocator_denied")
            trade_manager.log_decision(
                ticker, outcome, "skipped", f"allocator_denied: {budget.reason}",
                edge=round(edge, 4), price_cents=no_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        price = compute_limit_price(yes_bid, yes_ask, "no", edge=edge) or no_ask
        fee = kalshi_fee_cents(price)
        count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
        if count <= 0:
            if ss:
                ss.skip("kelly_zero")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "kelly_zero",
                edge=round(edge, 4), price_cents=price,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units vs {threshold/1000:.0f}K threshold. NO at {price}c, confidence {confidence*100:.0f}%"
        log.info(f"\nARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
        log.info(f"    Market: {ticker} NO at {price}c -> buying NO (confirmed under threshold)")
        log.info(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {price}c = ${count*price/100:.2f}")
        result = trade_manager.place_order(ticker, "no", price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(1.0 - confidence, 4), raw_edge=round(edge, 4),
                                            fee_cents=round(fee, 2), sizing_method="quarter_kelly",
                                            market_close_time=market.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"),
                                            artist=artist, units=units, threshold=threshold,
                                            data_sigma=round(sigma, 4), source_type="album")
        if result:
            if ss:
                ss.trades_placed += 1
            trade_manager.log_decision(
                ticker, outcome, "placed", "info_arb",
                edge=round(edge, 4), price_cents=price, count=count,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
                units=units, threshold=threshold, source=sale.get("source", ""),
            )
            allocator.record_trade("source-monitor", ticker, risk, edge=edge)


# ============================================================
# SOURCE 2: Box Office Data
# ============================================================

def _load_tmdb_api_key():
    """Load TMDb API key from env var or config/keys/tmdb.txt."""
    key = os.environ.get("TMDB_API_KEY", "")
    if not key:
        key_path = PROJECT_DIR / "config" / "keys" / "tmdb.txt"
        if key_path.exists():
            key = key_path.read_text().strip()
    return key

def _fetch_tmdb_boxoffice():
    """Fetch current box office data from TMDb API. Returns list of {title, gross, source}.

    NOTE: TMDb 'revenue' is lifetime worldwide gross, NOT current weekend domestic box office.
    This function is disabled by default (tmdbEnabled: false) because the data does not match
    what Kalshi box office markets settle on (weekend domestic gross). Preserved for future use
    if a proper weekend box office API source is found.
    """
    api_key = _load_tmdb_api_key()
    if not api_key:
        return None  # No key configured, fall back to HTML scraping

    auth_headers = {"Authorization": f"Bearer {api_key}", "accept": "application/json"}

    try:
        url = "https://api.themoviedb.org/3/movie/now_playing?region=US&page=1"
        r = retry_request("GET", url, headers=auth_headers, timeout=15)
        data = r.json()
        movies = data.get("results", [])

        # Build detail URLs and fetch in parallel
        movie_map = {}  # detail_url -> movie dict
        detail_urls = []
        for movie in movies[:15]:
            movie_id = movie.get("id")
            if not movie_id:
                continue
            detail_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
            detail_urls.append(detail_url)
            movie_map[detail_url] = movie

        detail_responses = fetch_parallel(detail_urls, headers=auth_headers, timeout=10)

        box_office_data = []
        for detail_url, detail_r in detail_responses.items():
            if detail_r is None or detail_r.status_code != 200:
                continue
            detail = detail_r.json()
            revenue = detail.get("revenue", 0)
            title = detail.get("title", movie_map[detail_url].get("title", ""))
            if revenue and revenue > 100000:
                box_office_data.append({
                    "title": title,
                    "gross": revenue,
                    "source": "tmdb"
                })

        if box_office_data:
            log.info(f"  TMDb: {len(box_office_data)} movies with revenue data")
            for d in box_office_data[:5]:
                log.info(f"    -> {d['title']}: ${d['gross']:,}")
        return box_office_data

    except Exception as e:
        log.warning(f"  TMDb fetch failed: {e}, falling back to HTML scraping")
        return None

# ─── Box office web scraping ───

def parse_the_numbers_html(html):
    """Parse weekend domestic box office data from The Numbers HTML.

    Extracts movie titles and weekend gross revenue from the weekend
    box office chart table. Returns list of {"title": str, "gross": float}.
    """
    results = []
    # Pattern: movie title in <a> tag, followed by money cell with $X,XXX,XXX
    pattern = r'<a[^>]*>([^<]{3,60})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)'
    for match in re.finditer(pattern, html):
        title = match.group(1).strip()
        gross_str = match.group(2).replace(",", "")
        try:
            gross = float(gross_str)
            if gross > 100_000:  # Filter noise — real movies gross > $100K
                results.append({"title": title, "gross": gross})
        except ValueError:
            continue
    return results


def parse_mojo_html(html):
    """Parse weekend domestic box office data from Box Office Mojo HTML.

    Handles both raw numbers ($163,830,000) and abbreviated ($163.8M).
    Returns list of {"title": str, "gross": float}.
    """
    results = []
    pattern = r'<a[^>]*>([^<]{3,60})</a>(?:[^$]{0,200})\$([\d,.]+)\s*([MmBb])?'
    for match in re.finditer(pattern, html):
        title = match.group(1).strip()
        amount_str = match.group(2).replace(",", "")
        suffix = match.group(3)
        try:
            amount = float(amount_str)
            if suffix and suffix.upper() == "M":
                amount *= 1_000_000
            elif suffix and suffix.upper() == "B":
                amount *= 1_000_000_000
            if amount > 100_000:
                results.append({"title": title, "gross": amount})
        except ValueError:
            continue
    return results


def fetch_boxoffice_data():
    """Fetch weekend domestic box office data with fallback.

    Tries The Numbers first (more structured HTML), falls back to
    Box Office Mojo. Deduplicates by title across sources.
    Returns list of {"title": str, "gross": float, "source": str}.
    """
    sources = [
        ("the_numbers", "https://www.the-numbers.com/market/",
         parse_the_numbers_html),
        ("mojo", "https://www.boxofficemojo.com/weekend/",
         parse_mojo_html),
    ]

    all_movies = []
    seen_titles = set()

    for source_name, url, parser in sources:
        try:
            resp = retry_request("GET", url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (compatible; KalshiBot/1.0)"
            })
            if resp.status_code != 200:
                log.warning(f"  Box office {source_name} returned {resp.status_code}")
                continue
            movies = parser(resp.text)
            for m in movies:
                title_key = m["title"].lower().strip()
                if title_key not in seen_titles:
                    seen_titles.add(title_key)
                    m["source"] = source_name
                    all_movies.append(m)
            if movies:
                log.info(f"  Box office: got {len(movies)} movies from {source_name}")
                health.record_source_success("boxoffice")
                break  # Got data from primary source, skip fallback
        except Exception as e:
            log.warning(f"  Box office {source_name} fetch failed: {e}")
            health.record_source_error("boxoffice", str(e))

    return all_movies


def scan_boxoffice(prefetched_markets=None, ss=None):
    """Run a box office scan cycle: fetch data, match to markets, evaluate trades."""
    try:
        box_data = fetch_boxoffice_data()
        if not box_data:
            log.info("  No box office data available")
            return
        log.info(f"  Box office: {len(box_data)} movies fetched")
        match_boxoffice_to_markets(box_data, prefetched_markets=prefetched_markets, ss=ss)
    except Exception as e:
        log.error(f"  Box office scan failed: {e}")


def match_boxoffice_to_markets(box_data, prefetched_markets=None, ss=None):
    """Match box office data to Kalshi markets."""
    try:
        if prefetched_markets is not None:
            markets = prefetched_markets.get("boxoffice", [])
        else:
            markets = []
            for prefix in ["KXBOXOFFICE", "KXBOX", "KXMOVIE", "KXFILM"]:
                markets.extend(get_markets_by_prefix(prefix))

        if not markets:
            log.info(f"  No open box office markets found on Kalshi")
            return

        log.info(f"  Found {len(markets)} box office markets")

        for movie in box_data:
            title_lower = movie["title"].lower()
            movie_markets = []
            for m in markets:
                market_title = m.get("title", "").lower()
                title_words = [w for w in title_lower.split() if len(w) > 3]
                if title_words and all(re.search(r'\b' + re.escape(w) + r'\b', market_title) for w in title_words):
                    if not is_market_liquid(m, min_volume=5, max_spread=40):
                        continue
                    # Parse threshold for consistency
                    threshold_match = re.search(r'\$(\d+(?:\.\d+)?)\s*[MmBb](?:illion)?', m.get("title", ""))
                    if threshold_match:
                        threshold = float(threshold_match.group(1)) * 1_000_000
                        dow = datetime.datetime.now().weekday()
                        box_age_hours = {4: 0, 5: 0, 6: 24, 0: 48, 1: 72, 2: 96, 3: 120}.get(dow, 0)
                        sigma = boxoffice_data_sigma(dow, hours_since_publication=box_age_hours)
                        prob = info_arb_probability(movie["gross"], threshold, sigma)
                        movie_markets.append((m, threshold, prob))

            consistent = _validate_market_cluster(movie["title"], movie_markets)
            for m, _threshold, _prob in consistent:
                if ss:
                    ss.markets_evaluated += 1
                evaluate_boxoffice_trade(m, movie, ss=ss)

    except Exception as e:
        log.error(f"  Box office market matching failed: {e}")

def evaluate_boxoffice_trade(market, movie, ss=None):
    """Evaluate box office trade opportunity."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    gross = movie["gross"]
    movie_title = movie["title"]

    threshold_match = re.search(r'\$(\d+(?:\.\d+)?)\s*[MmBb](?:illion)?', title)
    if not threshold_match:
        if ss:
            ss.skip("threshold_parse_fail")
        trade_manager.log_decision(ticker, "skip", "skipped", "threshold_parse_fail",
                                   price_cents=market.get("yes_ask", 0))
        return

    threshold = float(threshold_match.group(1)) * 1_000_000

    # Box office: estimate hours since data publication based on day of week
    dow = datetime.datetime.now().weekday()
    box_age_hours = {4: 0, 5: 0, 6: 24, 0: 48, 1: 72, 2: 96, 3: 120}.get(dow, 0)
    sigma = boxoffice_data_sigma(dow, hours_since_publication=box_age_hours)
    confidence = info_arb_probability(gross, threshold, sigma)

    if confidence > 0.5:
        outcome = "yes"
    else:
        outcome = "no"
        confidence = 1.0 - confidence

    if confidence < 0.60:
        if ss:
            ss.skip("low_confidence")
        trade_manager.log_decision(
            ticker, outcome, "skipped", "confidence below 60%",
            edge=confidence - 0.5, price_cents=market.get("yes_ask", 0),
            confidence=round(confidence, 4), sigma=round(sigma, 4),
            gross=gross, threshold=threshold, source=movie.get("source", ""),
        )
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    yes_bid = market.get("yes_bid", 0)

    # Lower edge threshold for confirmed data (sigma <= 5%)
    min_edge = 0.04 if sigma <= 0.05 else 0.10

    if outcome == "yes" and yes_ask and yes_ask < 99:
        edge = confidence - yes_ask / 100
        if edge <= min_edge:
            if ss:
                ss.skip("low_edge")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "edge_below_min",
                edge=round(edge, 4), price_cents=yes_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4), min_edge=min_edge,
                gross=gross, threshold=threshold, source=movie.get("source", ""),
            )
            return
        budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence, source_type="info_arb")
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            if ss:
                ss.skip("allocator_denied")
            trade_manager.log_decision(
                ticker, outcome, "skipped", f"allocator_denied: {budget.reason}",
                edge=round(edge, 4), price_cents=yes_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        price = compute_limit_price(yes_bid, yes_ask, "yes", edge=edge) or yes_ask
        fee = kalshi_fee_cents(price)
        count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
        if count <= 0:
            if ss:
                ss.skip("kelly_zero")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "kelly_zero",
                edge=round(edge, 4), price_cents=price,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        reasoning = f"Box office data shows {movie_title} at ${gross/1e6:.1f}M vs ${threshold/1e6:.0f}M threshold (conf {confidence*100:.0f}%)"
        log.info(f"\nARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M")
        log.info(f"    Market: {ticker} YES at {price}c | conf={confidence*100:.0f}%")
        result = trade_manager.place_order(ticker, "yes", price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(confidence, 4), raw_edge=round(edge, 4),
                                            fee_cents=round(fee, 2), sizing_method="quarter_kelly",
                                            market_close_time=market.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"),
                                            movie_title=movie_title, gross=gross, threshold=threshold,
                                            data_sigma=round(sigma, 4), source_type="boxoffice")
        if result:
            if ss:
                ss.trades_placed += 1
            trade_manager.log_decision(
                ticker, outcome, "placed", "info_arb",
                edge=round(edge, 4), price_cents=price, count=count,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
                gross=gross, threshold=threshold, source=movie.get("source", ""),
            )
            allocator.record_trade("source-monitor", ticker, risk, edge=edge)

    elif outcome == "no" and no_ask and no_ask < 99:
        edge = confidence - no_ask / 100
        if edge <= min_edge:
            if ss:
                ss.skip("low_edge")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "edge_below_min",
                edge=round(edge, 4), price_cents=no_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4), min_edge=min_edge,
                gross=gross, threshold=threshold, source=movie.get("source", ""),
            )
            return
        budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence, source_type="info_arb")
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            if ss:
                ss.skip("allocator_denied")
            trade_manager.log_decision(
                ticker, outcome, "skipped", f"allocator_denied: {budget.reason}",
                edge=round(edge, 4), price_cents=no_ask,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        price = compute_limit_price(yes_bid, yes_ask, "no", edge=edge) or no_ask
        fee = kalshi_fee_cents(price)
        count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
        if count <= 0:
            if ss:
                ss.skip("kelly_zero")
            trade_manager.log_decision(
                ticker, outcome, "skipped", "kelly_zero",
                edge=round(edge, 4), price_cents=price,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
            )
            return
        reasoning = f"Box office data shows {movie_title} at ${gross/1e6:.1f}M vs ${threshold/1e6:.0f}M threshold (conf {confidence*100:.0f}%)"
        log.info(f"\nARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M")
        log.info(f"    Market: {ticker} NO at {price}c | conf={confidence*100:.0f}%")
        result = trade_manager.place_order(ticker, "no", price, count, reasoning,
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(1.0 - confidence, 4), raw_edge=round(edge, 4),
                                            fee_cents=round(fee, 2), sizing_method="quarter_kelly",
                                            market_close_time=market.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"),
                                            movie_title=movie_title, gross=gross, threshold=threshold,
                                            data_sigma=round(sigma, 4), source_type="boxoffice")
        if result:
            if ss:
                ss.trades_placed += 1
            trade_manager.log_decision(
                ticker, outcome, "placed", "info_arb",
                edge=round(edge, 4), price_cents=price, count=count,
                confidence=round(confidence, 4), sigma=round(sigma, 4),
                gross=gross, threshold=threshold, source=movie.get("source", ""),
            )
            allocator.record_trade("source-monitor", ticker, risk, edge=edge)


# ============================================================
# SOURCE 3: NWS Actual Temperature
# ============================================================

def check_nws(prefetched_markets=None, ss=None):
    """Check NWS actual temperature observations for all stations (parallel fetch)."""
    log.info(f"\n[NWS] Checking actual temperatures...")

    stations = config["sources"]["nws"]["stations"]

    actual_temps = {}

    # Build all URLs for parallel fetch (latest observation per station)
    nws_headers = {
        "User-Agent": "(KalshiMonitor, contact@example.com)",
        "Accept": "application/geo+json"
    }
    url_to_city = {}
    urls = []
    for city_code, station_id in stations.items():
        url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
        urls.append(url)
        url_to_city[url] = (city_code, station_id)

    responses = fetch_parallel(urls, headers=nws_headers, timeout=15)

    for url, r in responses.items():
        city_code, station_id = url_to_city[url]
        if r is None or r.status_code != 200:
            log.error(f"  NWS check failed for {city_code} ({station_id}): HTTP {r.status_code if r else 'no response'}")
            continue
        try:
            data = r.json()
            save_snapshot(f"nws_{station_id}", json.dumps(data), ext="json")

            props = data.get("properties", {})
            temp_c = props.get("temperature", {}).get("value")

            if temp_c is not None:
                temp_f = temp_c * 9/5 + 32
                obs_ts = props.get("timestamp", "")
                actual_temps[city_code] = {
                    "temp_f": round_half_up(temp_f),
                    "temp_c": round(temp_c, 1),
                    "station": station_id,
                    "timestamp": obs_ts,
                }
                log.info(f"  {city_code} ({station_id}): {temp_f:.1f}F ({temp_c:.1f}C) @ {obs_ts or '?'}")
                # Check observation staleness — reject data older than NWS_MAX_OBS_AGE_MINUTES
                obs_age_minutes = _compute_nws_obs_age_minutes(obs_ts)
                if obs_age_minutes is not None:
                    actual_temps[city_code]["obs_age_minutes"] = round(obs_age_minutes, 1)
                    if obs_age_minutes > NWS_MAX_OBS_AGE_MINUTES:
                        log.warning(f"  {city_code}: NWS observation is {obs_age_minutes:.0f}min stale (>{NWS_MAX_OBS_AGE_MINUTES}min) — skipping trades")
                        del actual_temps[city_code]
                        continue
                elif not obs_ts:
                    # No timestamp at all — warn but allow (fail-open for robustness)
                    log.warning(f"  {city_code}: NWS observation has no timestamp — proceeding with caution")
            else:
                log.info(f"  {city_code} ({station_id}): No temperature data available")
        except Exception as e:
            log.error(f"  NWS parse failed for {city_code} ({station_id}): {e}")

    if actual_temps:
        check_nws_daily_highs(actual_temps)
        match_nws_to_markets(actual_temps, prefetched_markets=prefetched_markets, ss=ss)

def check_nws_daily_highs(current_temps):
    """Check for daily high temperature observations (parallel fetch).

    Uses per-city local midnight (via CITY_TIMEZONES) to compute the UTC
    start time for each station's observation window. This fixes a bug
    where using server-local date.today() + "T00:00:00Z" could include
    yesterday's late-afternoon temps for western cities (e.g. LAX at UTC-8).
    """
    stations = config["sources"]["nws"]["stations"]
    nws_headers = {
        "User-Agent": "(KalshiMonitor, contact@example.com)",
        "Accept": "application/geo+json"
    }

    url_to_city = {}
    urls = []
    for city_code, station_id in stations.items():
        tz = ZoneInfo(CITY_TIMEZONES.get(city_code, "America/New_York"))
        local_now = datetime.datetime.now(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_start = local_midnight.astimezone(datetime.timezone.utc)
        start = utc_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"https://api.weather.gov/stations/{station_id}/observations?start={start}&limit=100"
        urls.append(url)
        url_to_city[url] = (city_code, station_id)

    responses = fetch_parallel(urls, headers=nws_headers, timeout=15)

    for url, r in responses.items():
        city_code, station_id = url_to_city[url]
        try:
            if r is None or r.status_code != 200:
                continue
            data = r.json()
            features = data.get("features", [])
            temps = []
            for f in features:
                t = f.get("properties", {}).get("temperature", {}).get("value")
                if t is not None:
                    temp_f = t * 9/5 + 32
                    temps.append(round(temp_f, 1))

            if temps:
                running_high = max(temps)
                if city_code in current_temps:
                    current_temps[city_code]["running_high_f"] = running_high
                    current_temps[city_code]["obs_count"] = len(temps)
                log.info(f"  {city_code} running high today: {running_high:.1f}F ({len(temps)} observations)")
        except Exception as e:
            log.error(f"  Daily high check failed for {city_code}: {e}")

def match_nws_to_markets(temp_data, prefetched_markets=None, ss=None):
    """Match actual NWS temperature data to open Kalshi temperature markets."""
    try:
        if prefetched_markets is not None:
            markets = prefetched_markets.get("weather", [])
        else:
            markets = get_markets_by_prefix("KXHIGH")
        if not markets:
            log.info(f"  No open KXHIGH markets found")
            return

        today_markets = []

        for m in markets:
            parsed = parse_temp_ticker(m.get("ticker", ""))
            if not parsed:
                continue
            # Use per-city local date to determine "today"
            city_today = _local_today(parsed["city"])
            if parsed["date"] == city_today:
                today_markets.append((m, parsed))

        if not today_markets:
            log.info(f"  No KXHIGH markets settling today ({datetime.date.today().isoformat()})")
            return

        log.info(f"  Found {len(today_markets)} temperature markets settling today")

        # Group by city for consistency validation
        by_city = {}
        for m, parsed in today_markets:
            city = parsed["city"]
            if city not in by_city:
                by_city[city] = []
            by_city[city].append((m, parsed))

        for city, city_markets in by_city.items():
            if city not in temp_data or "running_high_f" not in temp_data[city]:
                continue

            # Use city-local hour for pre-dawn gate, sigma model, and edge thresholds
            city_tz = ZoneInfo(CITY_TIMEZONES.get(city, "America/New_York"))
            city_now = datetime.datetime.now(city_tz)
            city_hour = city_now.hour

            # Pre-dawn gate: running_high is meaningless before 8 AM local time
            # The daily high hasn't started building yet
            if city_hour < 8:
                log.info(f"  NWS: skipping {city} — pre-dawn ({city_hour}:00 local), running_high unreliable")
                if ss:
                    ss.skip("pre_dawn")
                continue

            running_high = temp_data[city]["running_high_f"]

            # Validate threshold market consistency for this city
            threshold_signals = []
            for m, parsed in city_markets:
                if parsed["direction"] == "T":
                    threshold = parsed["threshold"]
                    prob = nws_probability(running_high, threshold, "T", city_hour)
                    threshold_signals.append((m, threshold, prob))

            consistent_tickers = set()
            if threshold_signals:
                consistent = _validate_market_cluster(f"NWS-{city}", threshold_signals)
                consistent_tickers = {m.get("ticker") for m, _, _ in consistent}

            # Evaluate all markets (brackets skip consistency, thresholds must be consistent)
            for m, parsed in city_markets:
                ticker = m.get("ticker", "")
                if parsed["direction"] == "T" and ticker not in consistent_tickers:
                    continue  # Skip inconsistent threshold market

                if ss:
                    ss.markets_evaluated += 1

                threshold = parsed["threshold"]
                direction = parsed["direction"]
                max_cost = config["maxTradeAmount"] * 100
                is_bracket = (direction == "B")

                prob = nws_probability(running_high, threshold, direction, city_hour)

                yes_ask = m.get("yes_ask", 0)
                no_ask = m.get("no_ask", 0)
                yes_bid = m.get("yes_bid", 0)

                if not is_market_liquid(m):
                    if ss:
                        ss.skip("illiquid")
                    trade_manager.log_decision(ticker, "skip", "skipped", "illiquid",
                                               price_cents=yes_ask, yes_bid=yes_bid, volume=m.get("volume", 0))
                    continue

                # Determine trade side and edge
                if direction == "T":
                    margin = running_high - threshold
                else:
                    margin = 0  # bracket

                if prob > 0.5 and yes_ask and yes_ask < 99:
                    # Buy YES (raw edge, fees handled in Kelly)
                    edge = prob - yes_ask / 100
                    min_edge = _nws_min_edge(running_high, threshold, city_hour, is_bracket)
                    if edge <= min_edge:
                        if ss:
                            ss.skip("low_edge")
                        trade_manager.log_decision(
                            ticker, "yes", "skipped", "edge_below_min",
                            edge=round(edge, 4), price_cents=yes_ask, min_edge=min_edge,
                            confidence=round(prob, 4), running_high=round(running_high, 1),
                            city=city, threshold=threshold, hour=city_hour,
                        )
                        continue
                    budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=prob, source_type="nws")
                    if not budget.approved:
                        if ss:
                            ss.skip("allocator_denied")
                        trade_manager.log_decision(
                            ticker, "yes", "skipped", f"allocator_denied: {budget.reason}",
                            edge=round(edge, 4), price_cents=yes_ask, confidence=round(prob, 4),
                        )
                        continue
                    price = compute_limit_price(yes_bid, yes_ask, "yes", edge=edge) or yes_ask
                    fee = kalshi_fee_cents(price)
                    count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
                    if count <= 0:
                        if ss:
                            ss.skip("kelly_zero")
                        trade_manager.log_decision(
                            ticker, "yes", "skipped", "kelly_zero",
                            edge=round(edge, 4), price_cents=price, confidence=round(prob, 4),
                        )
                        continue
                    if direction == "T":
                        reasoning = f"NWS {city} running high {running_high:.1f}F > {threshold}F by {margin:.1f}F, prob {prob*100:.0f}% (hour {city_hour})"
                    else:
                        reasoning = f"NWS {city} running high {running_high:.1f}F in bracket [{threshold}, {threshold+1})F, prob {prob*100:.0f}%"
                    log.info(f"\nARBITRAGE FOUND: NWS {city} high {running_high:.1f}F -> YES on {ticker}")
                    log.info(f"    YES at {price}c | Edge: ~{edge*100:.0f}% | Prob: {prob*100:.0f}%")
                    result = trade_manager.place_order(ticker, "yes", price, count, reasoning,
                                                        market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                                        model_prob=round(prob, 4), raw_edge=round(edge, 4),
                                                        fee_cents=round(fee, 2), sizing_method="quarter_kelly",
                                                        market_close_time=m.get("close_time"),
                                                        kelly_fraction=kelly_details.get("kelly_fraction"),
                                                        bankroll_used=kelly_details.get("bankroll_used"),
                                                        running_high=round(running_high, 1),
                                                        hour_of_day=city_hour,
                                                        city=city, direction=direction, threshold=threshold,
                                                        source_type="nws")
                    if result:
                        if ss:
                            ss.trades_placed += 1
                        trade_manager.log_decision(
                            ticker, "yes", "placed", "nws_arb",
                            edge=round(edge, 4), price_cents=price, count=count,
                            confidence=round(prob, 4), running_high=round(running_high, 1),
                            city=city, threshold=threshold, hour=city_hour,
                        )
                        allocator.record_trade("source-monitor", ticker, risk, edge=edge)

                elif prob <= 0.5 and no_ask and no_ask < 99:
                    # Buy NO (raw edge, fees handled in Kelly)
                    no_prob = 1.0 - prob
                    edge = no_prob - no_ask / 100
                    min_edge = _nws_min_edge(running_high, threshold, city_hour, is_bracket)
                    if edge <= min_edge:
                        if ss:
                            ss.skip("low_edge")
                        trade_manager.log_decision(
                            ticker, "no", "skipped", "edge_below_min",
                            edge=round(edge, 4), price_cents=no_ask, min_edge=min_edge,
                            confidence=round(no_prob, 4), running_high=round(running_high, 1),
                            city=city, threshold=threshold, hour=city_hour,
                        )
                        continue
                    budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=no_prob, source_type="nws")
                    if not budget.approved:
                        if ss:
                            ss.skip("allocator_denied")
                        trade_manager.log_decision(
                            ticker, "no", "skipped", f"allocator_denied: {budget.reason}",
                            edge=round(edge, 4), price_cents=no_ask, confidence=round(no_prob, 4),
                        )
                        continue
                    price = compute_limit_price(yes_bid, yes_ask, "no", edge=edge) or no_ask
                    fee = kalshi_fee_cents(price)
                    count, risk, kelly_details = quarter_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True)
                    if count <= 0:
                        if ss:
                            ss.skip("kelly_zero")
                        trade_manager.log_decision(
                            ticker, "no", "skipped", "kelly_zero",
                            edge=round(edge, 4), price_cents=price, confidence=round(no_prob, 4),
                        )
                        continue
                    if direction == "T":
                        reasoning = f"NWS {city} running high {running_high:.1f}F < {threshold}F by {abs(margin):.1f}F, prob NO {no_prob*100:.0f}% (hour {city_hour})"
                    else:
                        reasoning = f"NWS {city} running high {running_high:.1f}F outside bracket [{threshold}, {threshold+1})F, prob NO {no_prob*100:.0f}%"
                    log.info(f"\nARBITRAGE FOUND: NWS {city} high {running_high:.1f}F -> NO on {ticker}")
                    log.info(f"    NO at {price}c | Edge: ~{edge*100:.0f}% | Prob NO: {no_prob*100:.0f}%")
                    result = trade_manager.place_order(ticker, "no", price, count, reasoning,
                                                        market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                                        model_prob=round(prob, 4), raw_edge=round(edge, 4),
                                                        fee_cents=round(fee, 2), sizing_method="quarter_kelly",
                                                        market_close_time=m.get("close_time"),
                                                        kelly_fraction=kelly_details.get("kelly_fraction"),
                                                        bankroll_used=kelly_details.get("bankroll_used"),
                                                        running_high=round(running_high, 1),
                                                        hour_of_day=city_hour,
                                                        city=city, direction=direction, threshold=threshold,
                                                        source_type="nws")
                    if result:
                        if ss:
                            ss.trades_placed += 1
                        trade_manager.log_decision(
                            ticker, "no", "placed", "nws_arb",
                            edge=round(edge, 4), price_cents=price, count=count,
                            confidence=round(no_prob, 4), running_high=round(running_high, 1),
                            city=city, threshold=threshold, hour=city_hour,
                        )
                        allocator.record_trade("source-monitor", ticker, risk, edge=edge)

    except Exception as e:
        log.error("  NWS market matching failed: %s", e, exc_info=True)


# ============================================================
# ADAPTIVE POLLING
# ============================================================

def _nws_interval_seconds(config):
    """Adaptive NWS polling: 5 min during peak hours (10am-4pm ET), config interval otherwise.

    Peak hours are when temperature observations are most likely to create
    info-arb opportunities (running highs still developing).
    """
    et_now = datetime.datetime.now(ZoneInfo("America/New_York"))
    if 10 <= et_now.hour < 16:
        return 5 * 60  # 5 minutes during peak
    return config["sources"]["nws"]["intervalMinutes"] * 60  # config default off-peak


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Settlement source monitor (info arbitrage)")
    parser.add_argument("--once", action="store_true", help="Run single scan of all sources then exit")
    args = parser.parse_args()

    if not acquire_process_singleton("monitor", PROJECT_DIR, log, display_name="source-monitor"):
        log.warning("Duplicate source-monitor launch blocked; exiting.")
        return

    log.info("=" * 70)
    log.info("Kalshi Settlement Source Monitor -- Information Arbitrage Bot")
    log.info(f"   Mode: {os.environ.get('KALSHI_MODE', 'demo')} | Max: ${config['maxTradeAmount']}/trade | Daily limit: {config['maxDailyTrades']} trades")
    log.info(f"   Sources: HDD={config['sources']['hdd']['enabled']} | BoxOffice={config['sources']['boxoffice']['enabled']} | NWS={config['sources']['nws']['enabled']}")
    log.info("=" * 70)

    log.info("\nVerifying Kalshi authentication...")
    try:
        balance, _ = client.get_balance()
        log.info(f"Auth OK! Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Auth failed: {e}")
        sys.exit(1)

    last_hdd = 0
    last_boxoffice = 0
    last_nws = 0

    if args.once:
        # Run one full cycle of all enabled sources
        ss = ScanSummary("source-monitor", log)
        prefetched = {}
        sources_checked = []
        if config["sources"]["hdd"]["enabled"]:
            album_markets = get_markets_by_prefix("KXALBUMSALES")
            if not album_markets:
                album_markets = get_markets_by_prefix("KXALBUM")
            prefetched["album"] = album_markets
            _check_with_retry(check_hdd, "hdd", prefetched, ss)
            sources_checked.append("hdd")
        if config["sources"]["boxoffice"]["enabled"]:
            box_markets = []
            for prefix in ["KXBOXOFFICE", "KXBOX", "KXMOVIE", "KXFILM"]:
                box_markets.extend(get_markets_by_prefix(prefix))
            prefetched["boxoffice"] = box_markets
            _check_with_retry(scan_boxoffice, "boxoffice", prefetched, ss)
            sources_checked.append("boxoffice")
        if config["sources"]["nws"]["enabled"]:
            prefetched["weather"] = get_markets_by_prefix("KXHIGH")
            _check_with_retry(check_nws, "nws", prefetched, ss)
            sources_checked.append("nws")
        ss.finalize()
        _log_scan_metrics(_build_scan_metrics(ss, sources_checked=sources_checked))
        return

    hdd_interval = config["sources"]["hdd"]["intervalMinutes"] * 60
    box_interval = config["sources"]["boxoffice"]["intervalMinutes"] * 60

    log.info(f"\nStarting monitoring loop...")
    log.info(f"   HDD: every {config['sources']['hdd']['intervalMinutes']}min")
    log.info(f"   Box Office: every {config['sources']['boxoffice']['intervalMinutes']}min (Fri-Mon)")
    log.info(f"   NWS: adaptive (5min peak / {config['sources']['nws']['intervalMinutes']}min off-peak)\n")

    while True:
        now = time.time()
        issues = health.check_health()
        if issues:
            log.warning("Health issues: %s", "; ".join(issues))
        order_monitor.check_orders()

        try:
            # Determine which sources need checking this cycle
            need_hdd = config["sources"]["hdd"]["enabled"] and (now - last_hdd) >= hdd_interval
            need_box = config["sources"]["boxoffice"]["enabled"] and (now - last_boxoffice) >= box_interval
            need_nws = config["sources"]["nws"]["enabled"] and (now - last_nws) >= _nws_interval_seconds(config)

            # Create scan summary for this iteration
            ss = ScanSummary("source-monitor", log) if (need_hdd or need_box or need_nws) else None

            # Prefetch all needed markets once (with 5-min cache) instead of
            # fetching per-source which made 3 separate paginated API calls
            prefetched = None
            if need_hdd or need_box or need_nws:
                prefetched = {}
                if need_hdd:
                    album_markets = get_markets_by_prefix("KXALBUMSALES")
                    if not album_markets:
                        album_markets = get_markets_by_prefix("KXALBUM")
                    prefetched["album"] = album_markets
                if need_box:
                    box_markets = []
                    for prefix in ["KXBOXOFFICE", "KXBOX", "KXMOVIE", "KXFILM"]:
                        box_markets.extend(get_markets_by_prefix(prefix))
                    prefetched["boxoffice"] = box_markets
                if need_nws:
                    prefetched["weather"] = get_markets_by_prefix("KXHIGH")

            sources_this_cycle = []

            if need_hdd:
                _check_with_retry(check_hdd, "hdd", prefetched, ss)
                sources_this_cycle.append("hdd")
                last_hdd = now

            if need_box:
                # Box office scan — only on active days when weekend data is available
                boxoffice_config = config["sources"]["boxoffice"]
                dow = datetime.datetime.now().weekday()
                active_days = boxoffice_config.get("activeDays", ["Friday", "Saturday", "Sunday", "Monday"])
                day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                if day_names[dow] in active_days:
                    _check_with_retry(scan_boxoffice, "boxoffice", prefetched, ss)
                    sources_this_cycle.append("boxoffice")
                else:
                    log.info(f"  Box office: skipping (not an active day)")
                last_boxoffice = now

            if need_nws:
                _check_with_retry(check_nws, "nws", prefetched, ss)
                sources_this_cycle.append("nws")
                last_nws = now

            if ss:
                ss.finalize()
                _log_scan_metrics(_build_scan_metrics(ss, sources_checked=sources_this_cycle))

            # Record heartbeat AFTER successful cycle (not before)
            health.record_bot_heartbeat("source-monitor")

        except Exception as e:
            log.error("Main loop error: %s", e, exc_info=True)

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
