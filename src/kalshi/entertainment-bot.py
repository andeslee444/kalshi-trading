#!/usr/bin/env python3
"""Kalshi Entertainment Markets Bot — Album Sales & Box Office Info Arbitrage
Monitors HITS Daily Double and Box Office Mojo for settlement data before markets adjust.
DEMO API ONLY — $5 max per trade.
"""

import json, time, datetime, os, sys, re, traceback
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, load_trades, save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR

setup_unbuffered()
setup_signal_handlers()

TRADES_PATH = PROJECT_DIR / "data" / "kalshi-entertainment-trades.json"
LOG_PATH = PROJECT_DIR / "data" / "logs" / "kalshi-entertainment.log"
PID_PATH = PROJECT_DIR / "data" / "pids" / "kalshi-entertainment.pid"

TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
Path(PID_PATH).parent.mkdir(parents=True, exist_ok=True)

# Write PID
Path(PID_PATH).write_text(str(os.getpid()))

# Logging — shared setup
log = setup_logging("entertainment", log_file=str(LOG_PATH))

# === Config from file ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
_bots_cfg = json.loads(BOTS_CONFIG_PATH.read_text())["entertainment"]
MAX_TRADE_AMOUNT = _bots_cfg["maxTradeAmount"]
MAX_DAILY_TRADES = _bots_cfg["maxDailyTrades"]
CONFIDENCE_THRESHOLD = _bots_cfg["confidenceThreshold"]
SCAN_INTERVAL_MINUTES = _bots_cfg["scanIntervalMinutes"]
ENTERTAINMENT_TICKERS = _bots_cfg["tickers"]

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

client = KalshiClient()

# === Trade Management ===
daily_trades = 0
daily_date = None

def reset_daily():
    global daily_trades, daily_date
    today = datetime.date.today().isoformat()
    if daily_date != today:
        daily_trades = 0
        daily_date = today

def place_trade(ticker, side, price_cents, count, reasoning, confidence):
    global daily_trades
    reset_daily()
    if daily_trades >= MAX_DAILY_TRADES:
        log.warning(f"Daily trade limit ({MAX_DAILY_TRADES}) reached")
        return None

    cost = price_cents * count
    if cost > MAX_TRADE_AMOUNT * 100:
        count = max(1, (MAX_TRADE_AMOUNT * 100) // price_cents)

    order_body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": count,
    }
    if side == "yes":
        order_body["yes_price"] = price_cents
    else:
        order_body["no_price"] = price_cents

    try:
        result = client.post("/portfolio/orders", body=order_body)
        order_info = result.get("order", {})
        daily_trades += 1
        trade_record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "ticker": ticker, "side": side, "price_cents": price_cents,
            "count": count, "cost_cents": price_cents * count,
            "reasoning": reasoning, "confidence": confidence,
            "order_id": order_info.get("order_id"),
            "status": order_info.get("status"),
        }
        save_trade(TRADES_PATH, trade_record)
        log.info(f"Order placed: {count}x {side} @ {price_cents}c on {ticker}")
        log.info(f"   Order ID: {order_info.get('order_id')}, Status: {order_info.get('status')}")
        return order_info
    except requests.exceptions.HTTPError as e:
        log.error(f"Order failed: {e.response.status_code} {e.response.text[:300]}")
    except Exception as e:
        log.error(f"Order failed: {e}")
    return None

# === Market Discovery ===
def find_entertainment_markets():
    """Find all open entertainment-related markets."""
    try:
        all_markets = client.get_all_markets()
    except Exception as e:
        log.error(f"Market fetch error: {e}")
        return []

    markets = []
    seen_tickers = set()
    for m in all_markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "").lower()
        subtitle = m.get("subtitle", "").lower()
        combined = f"{ticker} {title} {subtitle}"

        if any(kw.lower() in combined.lower() for kw in ENTERTAINMENT_TICKERS):
            if ticker not in seen_tickers:
                markets.append(m)
                seen_tickers.add(ticker)

    return markets

# === Source: HITS Daily Double ===
def scrape_hdd():
    """Scrape HITS Daily Double for album sales data."""
    log.info("Checking HITS Daily Double...")

    album_data = []
    urls = [
        "https://hitsdailydouble.com/charts/hits-top-50",
        "https://hitsdailydouble.com/news?id=1",
        "https://hitsdailydouble.com/news/charts",
        "https://hitsdailydouble.com/",
    ]

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
            if r.status_code != 200:
                log.warning(f"  HDD {url}: HTTP {r.status_code}")
                continue

            html = r.text
            log.info(f"  HDD {url}: {len(html)} bytes fetched")

            parsed = parse_album_sales(html)
            if parsed:
                album_data.extend(parsed)
                log.info(f"  Found {len(parsed)} album entries from {url}")

        except requests.exceptions.ConnectionError:
            log.warning(f"  HDD connection refused: {url}")
        except Exception as e:
            log.warning(f"  HDD error for {url}: {e}")

    # Try building chart alternatives
    building_urls = [
        "https://hitsdailydouble.com/building_album_chart",
        "https://hitsdd.section101.com/building_album_chart",
    ]
    for url in building_urls:
        try:
            r = requests.get(url, headers=headers, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                parsed = parse_album_sales(r.text)
                if parsed:
                    album_data.extend(parsed)
                    log.info(f"  Building chart: {len(parsed)} entries from {url}")
        except Exception as e:
            log.debug(f"  Building chart failed ({url}): {e}")

    return album_data

def parse_album_sales(html):
    """Extract album sales data from HTML."""
    results = []

    patterns = [
        r'(?i)([A-Z][a-zA-Z\s\'.]+?)\s+[-\u2013\u2014]\s+.*?(\d{2,3}(?:,\d{3})*)\s*[Kk]\s*(?:units|copies|sales|albums|total)?',
        r'(?i)([A-Z][a-zA-Z\s\'.]+?)\s+.*?(\d{1,3}(?:,\d{3})+)\s*(?:units|copies|sales|albums)',
        r'(?i)([A-Z][a-zA-Z\s\'.]+?)\s+.*?(?:projected|expected|tracking|building)\s+.*?(\d{2,3}(?:,\d{3})*)\s*[Kk]',
        r'(?i)(?:^|\n)\s*\d+\.\s+([A-Z][a-zA-Z\s\'.]+?)\s+.*?(\d{2,3}(?:,\d{3})*)',
        r'"artist"\s*:\s*"([^"]+)".*?"(?:sales|units|total)"\s*:\s*(\d+)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, html)
        for match in matches:
            artist = match[0].strip()
            units_str = match[1].replace(",", "")
            try:
                units = int(float(units_str))
                if units < 1000:
                    units *= 1000
                if 5000 < units < 5_000_000:
                    results.append({"artist": artist, "units": units})
            except (ValueError, TypeError):
                pass

    # Deduplicate by artist
    seen = set()
    deduped = []
    for r in results:
        key = r["artist"].lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped

# === Source: Box Office Mojo ===
def scrape_box_office():
    """Scrape Box Office Mojo and The Numbers for weekend estimates."""
    log.info("Checking box office data...")

    box_data = []

    # Box Office Mojo
    try:
        r = requests.get("https://www.boxofficemojo.com/", headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 200:
            html = r.text
            log.info(f"  Box Office Mojo: {len(html)} bytes")

            movies = re.findall(r'>([^<]{3,60})</a>.*?\$([\d,.]+)', html, re.DOTALL)
            for title, gross in movies[:15]:
                title = title.strip()
                gross_clean = gross.replace(",", "")
                try:
                    val = float(gross_clean)
                    if val < 1000:
                        val *= 1_000_000
                    if val > 50_000:
                        box_data.append({"title": title, "gross": int(val), "source": "boxofficemojo"})
                except (ValueError, TypeError):
                    pass

            if box_data:
                log.info(f"  Box Office Mojo: {len(box_data)} movies found")
    except Exception as e:
        log.warning(f"  Box Office Mojo error: {e}")

    # The Numbers
    try:
        r = requests.get("https://www.the-numbers.com/market/", headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 200:
            html = r.text
            matches = re.findall(r'>([^<]{3,60})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)', html)
            for title, gross in matches[:15]:
                title = title.strip()
                gross_val = int(gross.replace(",", ""))
                if gross_val > 50_000:
                    box_data.append({"title": title, "gross": gross_val, "source": "the-numbers"})

            if matches:
                log.info(f"  The Numbers: {len(matches)} entries parsed")
    except Exception as e:
        log.warning(f"  The Numbers error: {e}")

    # Weekend estimates
    try:
        r = requests.get("https://www.boxofficemojo.com/weekend/", headers={"User-Agent": USER_AGENT}, timeout=20)
        if r.status_code == 200:
            html = r.text
            movies = re.findall(r'>([^<]{3,60})</a>.*?\$([\d,.]+)', html, re.DOTALL)
            for title, gross in movies[:15]:
                title = title.strip()
                gross_clean = gross.replace(",", "")
                try:
                    val = float(gross_clean)
                    if val < 1000:
                        val *= 1_000_000
                    if val > 50_000 and not any(d["title"] == title for d in box_data):
                        box_data.append({"title": title, "gross": int(val), "source": "boxofficemojo-weekend"})
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        log.debug(f"  Weekend page error: {e}")

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
    threshold = None
    for pattern in [
        r'(\d{1,3}(?:,\d{3})*)\s*(?:K|thousand|copies|units)',
        r'more than\s+(\d{1,3}(?:,\d{3})*)',
        r'over\s+(\d{1,3}(?:,\d{3})*)',
        r'T(\d+)',
    ]:
        match = re.search(pattern, title, re.I)
        if match:
            threshold = int(match.group(1).replace(",", ""))
            if threshold < 1000:
                threshold *= 1000
            break

    if not threshold:
        match = re.search(r'T(\d+)', ticker)
        if match:
            threshold = int(match.group(1))
            if threshold < 1000:
                threshold *= 1000

    if not threshold:
        log.info(f"     Could not parse threshold from: {title}")
        return

    ratio = units / threshold
    if ratio > 1.10:
        side = "yes"
        confidence = min(0.95, 0.75 + (ratio - 1.0) * 0.5)
    elif ratio < 0.90:
        side = "no"
        confidence = min(0.95, 0.75 + (1.0 - ratio) * 0.5)
    else:
        log.info(f"     Too close to threshold ({units/1000:.0f}K vs {threshold/1000:.0f}K), skipping")
        return

    if confidence < CONFIDENCE_THRESHOLD:
        log.info(f"     Confidence {confidence*100:.0f}% < {CONFIDENCE_THRESHOLD*100:.0f}% threshold, skipping")
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)

    if side == "yes" and yes_ask and yes_ask < confidence * 100:
        edge = confidence - yes_ask / 100
        if edge > 0.05:
            count = max(1, min((MAX_TRADE_AMOUNT * 100) // yes_ask, 20))
            reasoning = f"HDD: {artist} at {units/1000:.0f}K > {threshold/1000:.0f}K threshold. YES@{yes_ask}c, conf={confidence*100:.0f}%"
            log.info(f"\nALBUM ARBITRAGE: {artist} {units/1000:.0f}K units > {threshold/1000:.0f}K")
            log.info(f"    {ticker} YES@{yes_ask}c | edge={edge*100:.1f}% | conf={confidence*100:.0f}%")
            place_trade(ticker, "yes", yes_ask, count, reasoning, confidence)

    elif side == "no" and no_ask and no_ask < confidence * 100:
        edge = confidence - no_ask / 100
        if edge > 0.05:
            count = max(1, min((MAX_TRADE_AMOUNT * 100) // no_ask, 20))
            reasoning = f"HDD: {artist} at {units/1000:.0f}K < {threshold/1000:.0f}K threshold. NO@{no_ask}c, conf={confidence*100:.0f}%"
            log.info(f"\nALBUM ARBITRAGE: {artist} {units/1000:.0f}K units < {threshold/1000:.0f}K")
            log.info(f"    {ticker} NO@{no_ask}c | edge={edge*100:.1f}% | conf={confidence*100:.0f}%")
            place_trade(ticker, "no", no_ask, count, reasoning, confidence)

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

    ratio = gross / threshold
    if ratio > 1.15:
        side = "yes"
        confidence = min(0.95, 0.80 + (ratio - 1.0) * 0.3)
    elif ratio < 0.85:
        side = "no"
        confidence = min(0.95, 0.80 + (1.0 - ratio) * 0.3)
    else:
        return

    if confidence < CONFIDENCE_THRESHOLD:
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)

    if side == "yes" and yes_ask and yes_ask < confidence * 100:
        edge = confidence - yes_ask / 100
        if edge > 0.05:
            count = max(1, min((MAX_TRADE_AMOUNT * 100) // yes_ask, 20))
            reasoning = f"Box office: {movie_title} ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M. YES@{yes_ask}c, conf={confidence*100:.0f}%"
            log.info(f"\nBOX OFFICE ARBITRAGE: {movie_title} ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M")
            place_trade(ticker, "yes", yes_ask, count, reasoning, confidence)

    elif side == "no" and no_ask and no_ask < confidence * 100:
        edge = confidence - no_ask / 100
        if edge > 0.05:
            count = max(1, min((MAX_TRADE_AMOUNT * 100) // no_ask, 20))
            reasoning = f"Box office: {movie_title} ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M. NO@{no_ask}c, conf={confidence*100:.0f}%"
            log.info(f"\nBOX OFFICE ARBITRAGE: {movie_title} ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M")
            place_trade(ticker, "no", no_ask, count, reasoning, confidence)

# === Main Loop ===
def scan():
    """Single scan cycle."""
    log.info(f"\n{'='*60}")
    log.info(f"Entertainment market scan starting...")

    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance check failed: {e}")
        return

    markets = find_entertainment_markets()
    log.info(f"Found {len(markets)} entertainment markets")

    for m in markets[:20]:
        log.info(f"  {m.get('ticker')}: {m.get('title', '')[:80]}")

    album_data = []
    box_data = []

    try:
        album_data = scrape_hdd()
    except Exception as e:
        log.error(f"HDD scrape error: {e}")
        traceback.print_exc()

    try:
        box_data = scrape_box_office()
    except Exception as e:
        log.error(f"Box office scrape error: {e}")
        traceback.print_exc()

    log.info(f"Source data: {len(album_data)} album entries, {len(box_data)} box office entries")

    if markets and (album_data or box_data):
        match_and_trade(markets, album_data, box_data)
    elif markets:
        log.info("Markets found but no source data to compare -- will retry next cycle")

    log.info(f"Scan complete.")

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
            reset_daily()
            scan()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        log.info(f"Next scan in {SCAN_INTERVAL_MINUTES} minutes...")
        sys.stdout.flush()
        time.sleep(SCAN_INTERVAL_MINUTES * 60)

if __name__ == "__main__":
    main()
