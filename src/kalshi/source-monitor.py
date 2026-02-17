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
from kalshi_auth import KalshiClient, load_trades, save_trade as _save_trade, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR

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

# === Trade tracking ===
daily_trades = 0
daily_loss = 0
daily_date = None

def reset_daily_if_needed():
    global daily_trades, daily_loss, daily_date
    today = datetime.date.today().isoformat()
    if daily_date != today:
        daily_trades = 0
        daily_loss = 0
        daily_date = today

def save_trade(trade):
    _save_trade(TRADES_PATH, trade)

def save_snapshot(source_name, content, ext="html"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{source_name}_{ts}.{ext}"
    (SNAPSHOTS_DIR / fname).write_text(content[:500000] if isinstance(content, str) else json.dumps(content, indent=2)[:500000])
    return fname

# === Kalshi Market Helpers ===
def get_markets_by_prefix(prefix, status="open"):
    """Get all open markets matching a ticker prefix."""
    return client.get_all_markets(prefix=prefix, status=status)

def place_trade(ticker, side, price_cents, count, reasoning):
    """Place a limit order. Returns order info or None."""
    global daily_trades, daily_loss
    reset_daily_if_needed()

    if daily_trades >= config["maxDailyTrades"]:
        log.info(f"  Daily trade limit ({config['maxDailyTrades']}) reached, skipping")
        return None

    cost = price_cents * count
    if cost > config["maxTradeAmount"] * 100:
        count = max(1, (config["maxTradeAmount"] * 100) // price_cents)
        cost = price_cents * count

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
            "ticker": ticker,
            "side": side,
            "price_cents": price_cents,
            "count": count,
            "cost_cents": cost,
            "reasoning": reasoning,
            "order_id": order_info.get("order_id"),
            "status": order_info.get("status"),
        }
        save_trade(trade_record)

        log.info(f"  Order placed! {count}x {side} @ {price_cents}c = ${cost/100:.2f}")
        log.info(f"     Order ID: {order_info.get('order_id', '?')}, Status: {order_info.get('status', '?')}")
        return order_info
    except requests.exceptions.HTTPError as e:
        log.error(f"  Order failed: {e.response.status_code} {e.response.text[:300]}")
        return None
    except Exception as e:
        log.error(f"  Order failed: {e}")
        return None


# ============================================================
# SOURCE 1: HITS Daily Double (Album Sales)
# ============================================================

def check_hdd():
    """Scrape HITS Daily Double for album sales data."""
    log.info(f"\n[HDD] Checking HITS Daily Double...")

    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    for url in config["sources"]["hdd"]["urls"]:
        try:
            headers = {"User-Agent": user_agent}
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
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
                match_hdd_to_markets(found_data)
            else:
                log.info(f"  Keywords found but no structured sales data parsed")

        except Exception as e:
            log.error(f"  HDD check failed for {url}: {e}")

    # Also try the building chart
    try:
        building_url = "https://hitsdd.section101.com/building_album_chart"
        r = requests.get(building_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
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

def match_hdd_to_markets(sales_data):
    """Match parsed album sales data to open Kalshi markets."""
    try:
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

    if units > threshold * 1.05:
        outcome = "yes"
        confidence = min(0.95, 0.7 + (units - threshold) / threshold * 0.5)
    elif units < threshold * 0.95:
        outcome = "no"
        confidence = min(0.95, 0.7 + (threshold - units) / threshold * 0.5)
    else:
        log.info(f"  {artist}: {units} units too close to threshold {threshold}, skipping")
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)

    if outcome == "yes" and yes_ask and yes_ask < confidence * 100:
        edge = confidence - yes_ask / 100
        if edge > 0.10:
            count = max(1, min((config["maxTradeAmount"] * 100) // yes_ask, 20))
            reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units > {threshold/1000:.0f}K threshold. YES at {yes_ask}c, confidence {confidence*100:.0f}%"
            log.info(f"\nARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
            log.info(f"    Market: {ticker} YES at {yes_ask}c -> buying YES (confirmed outcome)")
            log.info(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {yes_ask}c = ${count*yes_ask/100:.2f}")
            place_trade(ticker, "yes", yes_ask, count, reasoning)

    elif outcome == "no" and no_ask and no_ask < confidence * 100:
        edge = confidence - no_ask / 100
        if edge > 0.10:
            count = max(1, min((config["maxTradeAmount"] * 100) // no_ask, 20))
            reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units < {threshold/1000:.0f}K threshold. NO at {no_ask}c, confidence {confidence*100:.0f}%"
            log.info(f"\nARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
            log.info(f"    Market: {ticker} NO at {no_ask}c -> buying NO (confirmed under threshold)")
            log.info(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {no_ask}c = ${count*no_ask/100:.2f}")
            place_trade(ticker, "no", no_ask, count, reasoning)


# ============================================================
# SOURCE 2: Box Office Data
# ============================================================

def check_boxoffice():
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
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
        r.raise_for_status()
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
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
        r.raise_for_status()
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
        match_boxoffice_to_markets(box_office_data)

def match_boxoffice_to_markets(box_data):
    """Match box office data to Kalshi markets."""
    try:
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

    if gross > threshold * 1.10:
        outcome = "yes"
    elif gross < threshold * 0.90:
        outcome = "no"
    else:
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)

    if outcome == "yes" and yes_ask and yes_ask < 85:
        edge = 0.90 - yes_ask / 100
        if edge > 0.10:
            count = max(1, (config["maxTradeAmount"] * 100) // yes_ask)
            reasoning = f"Box office data shows {movie_title} at ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M threshold"
            log.info(f"\nARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M")
            log.info(f"    Market: {ticker} YES at {yes_ask}c")
            place_trade(ticker, "yes", yes_ask, count, reasoning)

    elif outcome == "no" and no_ask and no_ask < 85:
        edge = 0.90 - no_ask / 100
        if edge > 0.10:
            count = max(1, (config["maxTradeAmount"] * 100) // no_ask)
            reasoning = f"Box office data shows {movie_title} at ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M threshold"
            log.info(f"\nARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M")
            log.info(f"    Market: {ticker} NO at {no_ask}c")
            place_trade(ticker, "no", no_ask, count, reasoning)


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

def check_nws():
    """Check NWS actual temperature observations for all stations."""
    log.info(f"\n[NWS] Checking actual temperatures...")

    stations = config["sources"]["nws"]["stations"]
    today = datetime.date.today().isoformat()

    actual_temps = {}

    for city_code, station_id in stations.items():
        try:
            url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
            r = requests.get(url, headers={
                "User-Agent": "(KalshiMonitor, contact@example.com)",
                "Accept": "application/geo+json"
            }, timeout=15)
            r.raise_for_status()
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
            log.error(f"  NWS check failed for {city_code} ({station_id}): {e}")

    if actual_temps:
        check_nws_daily_highs(actual_temps)
        match_nws_to_markets(actual_temps)

def check_nws_daily_highs(current_temps):
    """Check for daily high temperature observations."""
    stations = config["sources"]["nws"]["stations"]

    for city_code, station_id in stations.items():
        try:
            today = datetime.date.today()
            start = today.isoformat() + "T00:00:00Z"
            url = f"https://api.weather.gov/stations/{station_id}/observations?start={start}&limit=100"
            r = requests.get(url, headers={
                "User-Agent": "(KalshiMonitor, contact@example.com)",
                "Accept": "application/geo+json"
            }, timeout=15)

            if r.status_code == 200:
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

def match_nws_to_markets(temp_data):
    """Match actual NWS temperature data to open Kalshi temperature markets."""
    try:
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
        if now.hour < 15:
            log.info(f"  Before 3 PM -- waiting for more temperature data before trading")
            for m, parsed in today_markets:
                city = parsed["city"]
                if city in temp_data and "running_high_f" in temp_data[city]:
                    high = temp_data[city]["running_high_f"]
                    thresh = parsed["threshold"]
                    direction = "above" if parsed["direction"] == "T" else "bracket"
                    log.info(f"    {m['ticker']}: running high {high:.1f}F vs threshold {thresh}F ({direction})")
            return

        for m, parsed in today_markets:
            city = parsed["city"]
            if city not in temp_data or "running_high_f" not in temp_data[city]:
                continue

            running_high = temp_data[city]["running_high_f"]
            threshold = parsed["threshold"]
            direction = parsed["direction"]
            ticker = m.get("ticker", "")

            if direction == "T":
                margin = running_high - threshold

                if margin > 3:
                    yes_ask = m.get("yes_ask", 0)
                    if yes_ask and yes_ask < 85:
                        edge = 0.92 - yes_ask / 100
                        if edge > 0.10:
                            count = max(1, (config["maxTradeAmount"] * 100) // yes_ask)
                            reasoning = f"NWS {city} running high {running_high:.1f}F > {threshold}F threshold by {margin:.1f}F (after 3PM)"
                            log.info(f"\nARBITRAGE FOUND: NWS actual temp confirms {city} high {running_high:.1f}F > {threshold}F")
                            log.info(f"    Market: {ticker} YES at {yes_ask}c -> buying YES")
                            log.info(f"    Edge: ~{edge*100:.0f}% | Margin: {margin:.1f}F")
                            place_trade(ticker, "yes", yes_ask, count, reasoning)

                elif margin < -3:
                    no_ask = m.get("no_ask", 0)
                    if no_ask and no_ask < 85:
                        edge = 0.92 - no_ask / 100
                        if edge > 0.10:
                            count = max(1, (config["maxTradeAmount"] * 100) // no_ask)
                            reasoning = f"NWS {city} running high {running_high:.1f}F < {threshold}F threshold by {abs(margin):.1f}F (after 3PM)"
                            log.info(f"\nARBITRAGE FOUND: NWS actual temp confirms {city} high {running_high:.1f}F < {threshold}F")
                            log.info(f"    Market: {ticker} NO at {no_ask}c -> buying NO")
                            log.info(f"    Edge: ~{edge*100:.0f}% | Margin: {abs(margin):.1f}F")
                            place_trade(ticker, "no", no_ask, count, reasoning)

                else:
                    log.info(f"  {ticker}: running high {running_high:.1f}F vs {threshold}F -- too close (margin {margin:.1f}F)")

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
        reset_daily_if_needed()

        try:
            if config["sources"]["hdd"]["enabled"] and (now - last_hdd) >= hdd_interval:
                try:
                    check_hdd()
                except Exception as e:
                    log.error(f"HDD source error: {e}")
                    traceback.print_exc()
                last_hdd = now

            if config["sources"]["boxoffice"]["enabled"] and (now - last_boxoffice) >= box_interval:
                try:
                    check_boxoffice()
                except Exception as e:
                    log.error(f"Box office source error: {e}")
                    traceback.print_exc()
                last_boxoffice = now

            if config["sources"]["nws"]["enabled"] and (now - last_nws) >= nws_interval:
                try:
                    check_nws()
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
