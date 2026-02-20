#!/usr/bin/env python3
"""Kalshi Settlement Source Monitor — Information Arbitrage Trading Bot
Monitors official data sources that Kalshi uses to settle markets.
When a source publishes data revealing the outcome BEFORE Kalshi settles, auto-trades on mispricing.

Sources:
  1. HITS Daily Double (album sales) — every 15 min
  2. Box Office Mojo / The Numbers — every 30 min (Fri-Mon)
  3. NWS actual temperatures — every 10 min
"""

import json, time, datetime, os, sys, re, hashlib, traceback
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, save_trade as _save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, fetch_parallel, retry_request, TradeManager, trim_trade_log
from probability import info_arb_probability, album_data_sigma, boxoffice_data_sigma, nws_probability, half_kelly, compute_limit_price, edge_after_fees
from capital_allocator import PortfolioAllocator

setup_unbuffered()
log = setup_logging("source-monitor")
setup_signal_handlers()

# === Paths ===
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-monitor-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-monitor-trades.json"
SNAPSHOTS_DIR = PROJECT_DIR / "data" / "kalshi-source-snapshots"

# Ensure dirs
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Load config
config = json.loads(CONFIG_PATH.read_text())

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": config["maxTradeAmount"],
    "maxDailyTrades": config["maxDailyTrades"],
    "maxDailyLoss": config["maxDailyLoss"],
}, logger=log)
trim_trade_log(TRADES_PATH)

def save_snapshot(source_name, content, ext="html"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{source_name}_{ts}.{ext}"
    (SNAPSHOTS_DIR / fname).write_text(content[:500000] if isinstance(content, str) else json.dumps(content, indent=2)[:500000])
    return fname

# === Kalshi Market Helpers ===
def get_markets_by_prefix(prefix, status="open"):
    """Get all open markets matching a ticker prefix."""
    return client.get_all_markets(prefix=prefix, status=status)

# ============================================================
# SOURCE 1: HITS Daily Double (Album Sales)
# ============================================================

def check_hdd(prefetched_markets=None):
    """Scrape HITS Daily Double for album sales data."""
    log.info(f"\n[HDD] Checking HITS Daily Double...")

    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    for url in config["sources"]["hdd"]["urls"]:
        try:
            headers = {"User-Agent": user_agent}
            r = retry_request("GET", url, headers=headers, timeout=20)
            html = r.text
            save_snapshot("hdd", html)

            keywords = config["sources"]["hdd"]["keywords"]
            html_lower = html.lower()

            found_keywords = [kw for kw in keywords if kw.lower() in html_lower]
            if not found_keywords:
                log.info(f"  No relevant keywords found on {url}")
                continue

            log.info(f"  Found keywords: {found_keywords}")

            sales_patterns = [
                r'(?i)(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+.*?(\d{2,3}[,.]?\d{0,3})\s*[Kk]\s*(?:units|copies|sales|albums)',
                r'(?i)(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+.*?(\d{1,3}(?:,\d{3})+)\s*(?:units|copies|sales|albums)',
                r'(?i)(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+.*?projected\s+.*?(\d{2,3}[,.]?\d{0,3})\s*[Kk]',
                r'(?i)(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+.*?building\s+.*?(\d{2,3}[,.]?\d{0,3})\s*[Kk]',
            ]

            found_data = []
            for pattern in sales_patterns:
                matches = re.findall(pattern, html)
                for match in matches:
                    artist = match[0].strip()
                    units_str = match[1].replace(",", "")
                    try:
                        units = int(float(units_str))
                        if units < 1000:
                            units = units * 1000
                        found_data.append({"artist": artist, "units": units, "source_url": url})
                    except (ValueError, TypeError):
                        pass

            if found_data:
                log.info(f"  Found album sales data: {found_data}")
                match_hdd_to_markets(found_data, prefetched_markets=prefetched_markets)
            else:
                log.info(f"  Keywords found but no structured sales data parsed")

        except Exception as e:
            log.error(f"  HDD check failed for {url}: {e}")

    # Also try the building chart
    try:
        building_url = "https://hitsdd.section101.com/building_album_chart"
        r = retry_request("GET", building_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code == 200:
            save_snapshot("hdd_building", r.text)
            log.info(f"  Building chart fetched ({len(r.text)} bytes)")
            parse_building_chart(r.text)
    except Exception as e:
        log.error(f"  Building chart fetch failed: {e}")

def parse_building_chart(html):
    """Parse the HDD building album chart for mid-week estimates."""
    rows = re.findall(r'(?i)<tr[^>]*>.*?</tr>', html, re.DOTALL)
    found = []
    for row in rows[:50]:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) >= 2:
            text = " ".join(re.sub(r'<[^>]+>', '', c).strip() for c in cells)
            nums = re.findall(r'(\d{2,3}(?:,\d{3})*)', text)
            if nums:
                found.append(text[:200])

    if found:
        log.info(f"  Building chart entries: {len(found)}")
        for f in found[:5]:
            log.info(f"    -> {f}")

def match_hdd_to_markets(sales_data, prefetched_markets=None):
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

        log.info(f"  Found {len(markets)} album sales markets")

        for sale in sales_data:
            artist = sale["artist"].lower()
            units = sale["units"]

            for m in markets:
                title = m.get("title", "").lower()
                subtitle = m.get("subtitle", "").lower()

                # Use word boundary matching to avoid false positives
                if re.search(r'\b' + re.escape(artist) + r'\b', title) or re.search(r'\b' + re.escape(artist) + r'\b', subtitle):
                    evaluate_album_trade(m, sale)

    except Exception as e:
        log.error(f"  Market matching failed: {e}")

def evaluate_album_trade(market, sale):
    """Evaluate and potentially execute a trade based on album sales data."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    units = sale["units"]
    artist = sale["artist"]

    threshold_match = re.search(r'(\d{1,3}(?:,\d{3})*)\s*(?:K|thousand|copies|units)', title, re.I)
    if not threshold_match:
        threshold_match = re.search(r'T(\d+)', ticker)

    if not threshold_match:
        log.info(f"  Could not parse threshold from market: {title}")
        return

    threshold = int(threshold_match.group(1).replace(",", ""))
    if threshold < 1000:
        threshold *= 1000

    sigma = album_data_sigma(datetime.datetime.now().weekday())
    confidence = info_arb_probability(units, threshold, sigma)

    if confidence > 0.5:
        outcome = "yes"
    else:
        outcome = "no"
        confidence = 1.0 - confidence

    if confidence < 0.60:
        log.info(f"  {artist}: {units} units vs {threshold} threshold, confidence {confidence*100:.0f}% too low")
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    yes_bid = market.get("yes_bid", 0)

    # Lower edge threshold for confirmed data (sigma <= 5%)
    min_edge = 0.05 if sigma <= 0.05 else 0.10

    if outcome == "yes" and yes_ask and yes_ask < 99:
        edge = edge_after_fees(confidence - yes_ask / 100, yes_ask)
        if edge > min_edge:
            budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence)
            if not budget.approved:
                log.info(f"  Allocator denied {ticker}: {budget.reason}")
                return
            price = compute_limit_price(yes_bid, yes_ask, "yes") or yes_ask
            count, risk = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents)
            if count <= 0:
                return
            reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units vs {threshold/1000:.0f}K threshold. YES at {price}c, confidence {confidence*100:.0f}%"
            log.info(f"\nARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
            log.info(f"    Market: {ticker} YES at {price}c -> buying YES (confirmed outcome)")
            log.info(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {price}c = ${count*price/100:.2f}")
            result = trade_manager.place_order(ticker, "yes", price, count, reasoning)
            if result:
                allocator.record_trade("source-monitor", ticker, risk)

    elif outcome == "no" and no_ask and no_ask < 99:
        edge = edge_after_fees(confidence - no_ask / 100, no_ask)
        if edge > min_edge:
            budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence)
            if not budget.approved:
                log.info(f"  Allocator denied {ticker}: {budget.reason}")
                return
            price = compute_limit_price(yes_bid, yes_ask, "no") or no_ask
            count, risk = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents)
            if count <= 0:
                return
            reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units vs {threshold/1000:.0f}K threshold. NO at {price}c, confidence {confidence*100:.0f}%"
            log.info(f"\nARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
            log.info(f"    Market: {ticker} NO at {price}c -> buying NO (confirmed under threshold)")
            log.info(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {price}c = ${count*price/100:.2f}")
            result = trade_manager.place_order(ticker, "no", price, count, reasoning)
            if result:
                allocator.record_trade("source-monitor", ticker, risk)


# ============================================================
# SOURCE 2: Box Office Data
# ============================================================

def check_boxoffice(prefetched_markets=None):
    """Check box office data from Box Office Mojo and The Numbers."""
    now = datetime.datetime.now()
    day_name = now.strftime("%A")

    active_days = config["sources"]["boxoffice"]["activeDays"]
    if day_name not in active_days:
        log.info(f"\n[BOX OFFICE] Skipping -- {day_name} not in active days {active_days}")
        return

    log.info(f"\n[BOX OFFICE] Checking box office data ({day_name})...")

    box_office_data = []
    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    # Check The Numbers
    try:
        url = "https://www.the-numbers.com/market/"
        r = retry_request("GET", url, headers={"User-Agent": user_agent}, timeout=20)
        save_snapshot("boxoffice_thenumbers", r.text)

        gross_pattern = r'(?:>)([^<]{3,50})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)'
        matches = re.findall(gross_pattern, r.text)

        for title, gross in matches[:10]:
            title = title.strip()
            gross_val = int(gross.replace(",", ""))
            if gross_val > 100000:
                box_office_data.append({"title": title, "gross": gross_val, "source": "the-numbers.com"})

        if box_office_data:
            log.info(f"  The Numbers: {len(box_office_data)} movies found")
            for d in box_office_data[:5]:
                log.info(f"    -> {d['title']}: ${d['gross']:,}")
    except Exception as e:
        log.error(f"  The Numbers check failed: {e}")

    # Check Box Office Mojo
    try:
        url = "https://www.boxofficemojo.com/"
        r = retry_request("GET", url, headers={"User-Agent": user_agent}, timeout=20)
        save_snapshot("boxoffice_mojo", r.text)

        movies = re.findall(r'(?:>)([^<]{3,50})</a>.*?\$([\d,.]+)\s*[MmBb]?', r.text, re.DOTALL)

        mojo_data = []
        for title, gross in movies[:10]:
            title = title.strip()
            gross_clean = gross.replace(",", "")
            try:
                gross_val = float(gross_clean)
                if gross_val < 1000:
                    gross_val *= 1_000_000
                mojo_data.append({"title": title, "gross": int(gross_val), "source": "boxofficemojo.com"})
            except (ValueError, TypeError):
                pass

        if mojo_data:
            log.info(f"  Box Office Mojo: {len(mojo_data)} movies found")
            box_office_data.extend(mojo_data)
    except Exception as e:
        log.error(f"  Box Office Mojo check failed: {e}")

    if box_office_data:
        match_boxoffice_to_markets(box_office_data, prefetched_markets=prefetched_markets)

def match_boxoffice_to_markets(box_data, prefetched_markets=None):
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
            for m in markets:
                market_title = m.get("title", "").lower()
                title_words = [w for w in title_lower.split() if len(w) > 3]
                if title_words and all(re.search(r'\b' + re.escape(w) + r'\b', market_title) for w in title_words):
                    evaluate_boxoffice_trade(m, movie)

    except Exception as e:
        log.error(f"  Box office market matching failed: {e}")

def evaluate_boxoffice_trade(market, movie):
    """Evaluate box office trade opportunity."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    gross = movie["gross"]
    movie_title = movie["title"]

    threshold_match = re.search(r'\$(\d+(?:\.\d+)?)\s*[MmBb](?:illion)?', title)
    if not threshold_match:
        return

    threshold = float(threshold_match.group(1)) * 1_000_000

    sigma = boxoffice_data_sigma(datetime.datetime.now().weekday())
    confidence = info_arb_probability(gross, threshold, sigma)

    if confidence > 0.5:
        outcome = "yes"
    else:
        outcome = "no"
        confidence = 1.0 - confidence

    if confidence < 0.60:
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    yes_bid = market.get("yes_bid", 0)

    # Lower edge threshold for confirmed data (sigma <= 5%)
    min_edge = 0.05 if sigma <= 0.05 else 0.10

    if outcome == "yes" and yes_ask and yes_ask < 99:
        edge = edge_after_fees(confidence - yes_ask / 100, yes_ask)
        if edge > min_edge:
            budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence)
            if not budget.approved:
                return
            price = compute_limit_price(yes_bid, yes_ask, "yes") or yes_ask
            count, risk = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents)
            if count <= 0:
                return
            reasoning = f"Box office data shows {movie_title} at ${gross/1e6:.1f}M vs ${threshold/1e6:.0f}M threshold (conf {confidence*100:.0f}%)"
            log.info(f"\nARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M")
            log.info(f"    Market: {ticker} YES at {price}c | conf={confidence*100:.0f}%")
            result = trade_manager.place_order(ticker, "yes", price, count, reasoning)
            if result:
                allocator.record_trade("source-monitor", ticker, risk)

    elif outcome == "no" and no_ask and no_ask < 99:
        edge = edge_after_fees(confidence - no_ask / 100, no_ask)
        if edge > min_edge:
            budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=confidence)
            if not budget.approved:
                return
            price = compute_limit_price(yes_bid, yes_ask, "no") or no_ask
            count, risk = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents)
            if count <= 0:
                return
            reasoning = f"Box office data shows {movie_title} at ${gross/1e6:.1f}M vs ${threshold/1e6:.0f}M threshold (conf {confidence*100:.0f}%)"
            log.info(f"\nARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M")
            log.info(f"    Market: {ticker} NO at {price}c | conf={confidence*100:.0f}%")
            result = trade_manager.place_order(ticker, "no", price, count, reasoning)
            if result:
                allocator.record_trade("source-monitor", ticker, risk)


# ============================================================
# SOURCE 3: NWS Actual Temperature
# ============================================================

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

def check_nws(prefetched_markets=None):
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
                actual_temps[city_code] = {
                    "temp_f": round(temp_f, 1),
                    "temp_c": round(temp_c, 1),
                    "station": station_id,
                    "timestamp": props.get("timestamp", ""),
                }
                log.info(f"  {city_code} ({station_id}): {temp_f:.1f}F ({temp_c:.1f}C) @ {props.get('timestamp', '?')}")
            else:
                log.info(f"  {city_code} ({station_id}): No temperature data available")
        except Exception as e:
            log.error(f"  NWS parse failed for {city_code} ({station_id}): {e}")

    if actual_temps:
        check_nws_daily_highs(actual_temps)
        match_nws_to_markets(actual_temps, prefetched_markets=prefetched_markets)

def check_nws_daily_highs(current_temps):
    """Check for daily high temperature observations (parallel fetch)."""
    stations = config["sources"]["nws"]["stations"]
    nws_headers = {
        "User-Agent": "(KalshiMonitor, contact@example.com)",
        "Accept": "application/geo+json"
    }

    today = datetime.date.today()
    start = today.isoformat() + "T00:00:00Z"

    url_to_city = {}
    urls = []
    for city_code, station_id in stations.items():
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
                    temps.append(t * 9/5 + 32)

            if temps:
                running_high = max(temps)
                if city_code in current_temps:
                    current_temps[city_code]["running_high_f"] = round(running_high, 1)
                    current_temps[city_code]["obs_count"] = len(temps)
                log.info(f"  {city_code} running high today: {running_high:.1f}F ({len(temps)} observations)")
        except Exception as e:
            log.error(f"  Daily high check failed for {city_code}: {e}")

def match_nws_to_markets(temp_data, prefetched_markets=None):
    """Match actual NWS temperature data to open Kalshi temperature markets."""
    try:
        if prefetched_markets is not None:
            markets = prefetched_markets.get("weather", [])
        else:
            markets = get_markets_by_prefix("KXHIGH")
        if not markets:
            log.info(f"  No open KXHIGH markets found")
            return

        today = datetime.date.today().isoformat()
        today_markets = []

        for m in markets:
            parsed = parse_temp_ticker(m.get("ticker", ""))
            if parsed and parsed["date"] == today:
                today_markets.append((m, parsed))

        if not today_markets:
            log.info(f"  No KXHIGH markets settling today ({today})")
            return

        log.info(f"  Found {len(today_markets)} temperature markets settling today")

        now = datetime.datetime.now()

        for m, parsed in today_markets:
            city = parsed["city"]
            if city not in temp_data or "running_high_f" not in temp_data[city]:
                continue

            running_high = temp_data[city]["running_high_f"]
            threshold = parsed["threshold"]
            direction = parsed["direction"]
            ticker = m.get("ticker", "")
            max_cost = config["maxTradeAmount"] * 100
            is_bracket = (direction == "B")

            prob = nws_probability(running_high, threshold, direction, now.hour)

            yes_ask = m.get("yes_ask", 0)
            no_ask = m.get("no_ask", 0)
            yes_bid = m.get("yes_bid", 0)

            # Determine trade side and edge
            if direction == "T":
                margin = running_high - threshold
            else:
                margin = 0  # bracket

            if prob > 0.5 and yes_ask and yes_ask < 99:
                # Buy YES
                edge = edge_after_fees(prob - yes_ask / 100, yes_ask)
                # Rec 1: Brackets need 2x edge threshold (20% for NWS)
                base_min_edge = 0.20 if is_bracket else 0.10
                # Lower threshold for high-confidence NWS (hour >= 17, non-bracket)
                min_edge = base_min_edge * 0.5 if now.hour >= 17 and not is_bracket else base_min_edge
                # Rec 2: YES side requires 15%+ edge (0% historical win rate)
                min_edge = max(min_edge, 0.15)
                if edge > min_edge:
                    budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=prob)
                    if not budget.approved:
                        continue
                    price = compute_limit_price(yes_bid, yes_ask, "yes") or yes_ask
                    count, risk = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents)
                    if count <= 0:
                        continue
                    if direction == "T":
                        reasoning = f"NWS {city} running high {running_high:.1f}F > {threshold}F by {margin:.1f}F, prob {prob*100:.0f}% (hour {now.hour})"
                    else:
                        reasoning = f"NWS {city} running high {running_high:.1f}F in bracket [{threshold}, {threshold+1})F, prob {prob*100:.0f}%"
                    log.info(f"\nARBITRAGE FOUND: NWS {city} high {running_high:.1f}F -> YES on {ticker}")
                    log.info(f"    YES at {price}c | Edge: ~{edge*100:.0f}% | Prob: {prob*100:.0f}%")
                    result = trade_manager.place_order(ticker, "yes", price, count, reasoning)
                    if result:
                        allocator.record_trade("source-monitor", ticker, risk)

            elif prob <= 0.5 and no_ask and no_ask < 99:
                # Buy NO
                no_prob = 1.0 - prob
                edge = edge_after_fees(no_prob - no_ask / 100, no_ask)
                # Rec 1: Brackets need 2x edge threshold (20% for NWS)
                base_min_edge = 0.20 if is_bracket else 0.10
                # Lower threshold for high-confidence NWS (hour >= 17, non-bracket)
                min_edge = base_min_edge * 0.5 if now.hour >= 17 and not is_bracket else base_min_edge
                if edge > min_edge:
                    budget = allocator.request_budget("source-monitor", ticker, edge=edge, confidence=no_prob)
                    if not budget.approved:
                        continue
                    price = compute_limit_price(yes_bid, yes_ask, "no") or no_ask
                    count, risk = half_kelly(edge, price, budget.max_cost_cents, bankroll_cents=budget.bankroll_cents)
                    if count <= 0:
                        continue
                    if direction == "T":
                        reasoning = f"NWS {city} running high {running_high:.1f}F < {threshold}F by {abs(margin):.1f}F, prob NO {no_prob*100:.0f}% (hour {now.hour})"
                    else:
                        reasoning = f"NWS {city} running high {running_high:.1f}F outside bracket [{threshold}, {threshold+1})F, prob NO {no_prob*100:.0f}%"
                    log.info(f"\nARBITRAGE FOUND: NWS {city} high {running_high:.1f}F -> NO on {ticker}")
                    log.info(f"    NO at {price}c | Edge: ~{edge*100:.0f}% | Prob NO: {no_prob*100:.0f}%")
                    result = trade_manager.place_order(ticker, "no", price, count, reasoning)
                    if result:
                        allocator.record_trade("source-monitor", ticker, risk)

    except Exception as e:
        log.error(f"  NWS market matching failed: {e}")
        traceback.print_exc()


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    log.info("=" * 70)
    log.info("Kalshi Settlement Source Monitor -- Information Arbitrage Bot")
    log.info(f"   Mode: {config['mode']} | Max: ${config['maxTradeAmount']}/trade | Daily limit: {config['maxDailyTrades']} trades")
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

    hdd_interval = config["sources"]["hdd"]["intervalMinutes"] * 60
    box_interval = config["sources"]["boxoffice"]["intervalMinutes"] * 60
    nws_interval = config["sources"]["nws"]["intervalMinutes"] * 60

    log.info(f"\nStarting monitoring loop...")
    log.info(f"   HDD: every {config['sources']['hdd']['intervalMinutes']}min")
    log.info(f"   Box Office: every {config['sources']['boxoffice']['intervalMinutes']}min (Fri-Mon)")
    log.info(f"   NWS: every {config['sources']['nws']['intervalMinutes']}min\n")

    while True:
        now = time.time()

        try:
            # Determine which sources need checking this cycle
            need_hdd = config["sources"]["hdd"]["enabled"] and (now - last_hdd) >= hdd_interval
            need_box = config["sources"]["boxoffice"]["enabled"] and (now - last_boxoffice) >= box_interval
            need_nws = config["sources"]["nws"]["enabled"] and (now - last_nws) >= nws_interval

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

            if need_hdd:
                try:
                    check_hdd(prefetched_markets=prefetched)
                except Exception as e:
                    log.error(f"HDD source error: {e}")
                    traceback.print_exc()
                last_hdd = now

            if need_box:
                try:
                    check_boxoffice(prefetched_markets=prefetched)
                except Exception as e:
                    log.error(f"Box office source error: {e}")
                    traceback.print_exc()
                last_boxoffice = now

            if need_nws:
                try:
                    check_nws(prefetched_markets=prefetched)
                except Exception as e:
                    log.error(f"NWS source error: {e}")
                    traceback.print_exc()
                last_nws = now

        except Exception as e:
            log.error(f"Main loop error: {e}")
            traceback.print_exc()

        time.sleep(30)


if __name__ == "__main__":
    main()
