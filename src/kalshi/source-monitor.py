#!/usr/bin/env python3
"""Kalshi Settlement Source Monitor — Information Arbitrage Trading Bot
Monitors official data sources that Kalshi uses to settle markets.
When a source publishes data revealing the outcome BEFORE Kalshi settles, auto-trades on mispricing.

Sources:
  1. HITS Daily Double (album sales) — every 15 min
  2. Box Office Mojo / The Numbers — every 30 min (Fri-Mon)
  3. NWS actual temperatures — every 10 min
"""

import json, time, base64, datetime, os, sys, re, hashlib, traceback
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# Unbuffered output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
os.environ['PYTHONUNBUFFERED'] = '1'

# === Paths ===
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-monitor-config.json"
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-monitor-trades.json"
SNAPSHOTS_DIR = PROJECT_DIR / "data" / "kalshi-source-snapshots"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

# Ensure dirs
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Load config
config = json.loads(CONFIG_PATH.read_text())

# === Kalshi Auth (copied from weather-bot.py) ===
with open(KEY_PATH, "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

def get_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    path_clean = path.split("?")[0]
    msg = f"{ts}{method}{path_clean}"
    sig = private_key.sign(
        msg.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": API_KEY,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }

def api(method, path, body=None):
    url = BASE_URL + path
    full_path = "/trade-api/v2" + path
    headers = get_headers(method, full_path)
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

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

def load_trades():
    if TRADES_PATH.exists():
        try: return json.loads(TRADES_PATH.read_text())
        except: return []
    return []

def save_trade(trade):
    trades = load_trades()
    trades.append(trade)
    TRADES_PATH.write_text(json.dumps(trades, indent=2))

def save_snapshot(source_name, content, ext="html"):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{source_name}_{ts}.{ext}"
    (SNAPSHOTS_DIR / fname).write_text(content[:500000] if isinstance(content, str) else json.dumps(content, indent=2)[:500000])
    return fname

# === Kalshi Market Helpers ===
def get_markets_by_prefix(prefix, status="open"):
    """Get all open markets matching a ticker prefix."""
    markets = []
    cursor = None
    for _ in range(20):
        path = f"/markets?status={status}&limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = api("GET", path)
        for m in data.get("markets", []):
            if m.get("ticker", "").startswith(prefix):
                markets.append(m)
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break
    return markets

def get_orderbook(ticker):
    try:
        return api("GET", f"/markets/{ticker}/orderbook")
    except:
        return None

def place_trade(ticker, side, price_cents, count, reasoning):
    """Place a limit order. Returns order info or None."""
    global daily_trades, daily_loss
    reset_daily_if_needed()

    if daily_trades >= config["maxDailyTrades"]:
        print(f"  ⚠️ Daily trade limit ({config['maxDailyTrades']}) reached, skipping")
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
        result = api("POST", "/portfolio/orders", order_body)
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
        
        print(f"  ✅ Order placed! {count}x {side} @ {price_cents}¢ = ${cost/100:.2f}")
        print(f"     Order ID: {order_info.get('order_id', '?')}, Status: {order_info.get('status', '?')}")
        return order_info
    except requests.exceptions.HTTPError as e:
        print(f"  ❌ Order failed: {e.response.status_code} {e.response.text[:300]}")
        return None
    except Exception as e:
        print(f"  ❌ Order failed: {e}")
        return None


# ============================================================
# SOURCE 1: HITS Daily Double (Album Sales)
# ============================================================

# Track seen articles to avoid re-processing
seen_hdd_hashes = set()

def check_hdd():
    """Scrape HITS Daily Double for album sales data."""
    print(f"\n📀 [HDD] Checking HITS Daily Double...")
    
    for url in config["sources"]["hdd"]["urls"]:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            html = r.text
            save_snapshot("hdd", html)
            
            # Check for keywords indicating album sales data
            keywords = config["sources"]["hdd"]["keywords"]
            html_lower = html.lower()
            
            found_keywords = [kw for kw in keywords if kw.lower() in html_lower]
            if not found_keywords:
                print(f"  No relevant keywords found on {url}")
                continue
            
            print(f"  Found keywords: {found_keywords}")
            
            # Parse for album sales data patterns
            # Look for patterns like "Artist - 150K" or "Artist sold 150,000 units"
            sales_patterns = [
                # "Artist Name ... 150K" or "150,000 units"
                r'(?i)(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+.*?(\d{2,3}[,.]?\d{0,3})\s*[Kk]\s*(?:units|copies|sales|albums)',
                r'(?i)(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+.*?(\d{1,3}(?:,\d{3})+)\s*(?:units|copies|sales|albums)',
                # "projected to sell X"
                r'(?i)(\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+.*?projected\s+.*?(\d{2,3}[,.]?\d{0,3})\s*[Kk]',
                # "building toward X"
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
                        if units < 1000:  # Likely in K already
                            units = units * 1000
                        found_data.append({"artist": artist, "units": units, "source_url": url})
                    except:
                        pass
            
            if found_data:
                print(f"  📊 Found album sales data: {found_data}")
                match_hdd_to_markets(found_data)
            else:
                print(f"  Keywords found but no structured sales data parsed")
                # Still useful — log the content hash to detect changes
                
        except Exception as e:
            print(f"  ⚠️ HDD check failed for {url}: {e}")
    
    # Also try the building chart
    try:
        building_url = "https://hitsdd.section101.com/building_album_chart"
        r = requests.get(building_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if r.status_code == 200:
            save_snapshot("hdd_building", r.text)
            print(f"  📊 Building chart fetched ({len(r.text)} bytes)")
            # Parse building chart similarly
            parse_building_chart(r.text)
    except Exception as e:
        print(f"  ⚠️ Building chart fetch failed: {e}")

def parse_building_chart(html):
    """Parse the HDD building album chart for mid-week estimates."""
    # Look for table rows with artist names and numbers
    # Common pattern: artist name followed by numbers
    rows = re.findall(r'(?i)<tr[^>]*>.*?</tr>', html, re.DOTALL)
    found = []
    for row in rows[:50]:  # Limit to first 50 rows
        # Extract text content
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) >= 2:
            text = " ".join(re.sub(r'<[^>]+>', '', c).strip() for c in cells)
            # Look for number patterns (sales figures)
            nums = re.findall(r'(\d{2,3}(?:,\d{3})*)', text)
            if nums:
                found.append(text[:200])
    
    if found:
        print(f"  Building chart entries: {len(found)}")
        for f in found[:5]:
            print(f"    → {f}")

def match_hdd_to_markets(sales_data):
    """Match parsed album sales data to open Kalshi markets."""
    try:
        markets = get_markets_by_prefix("KXALBUMSALES")
        if not markets:
            # Also try other potential ticker patterns
            markets = get_markets_by_prefix("KXALBUM")
        
        if not markets:
            print(f"  No open album sales markets found on Kalshi")
            return
        
        print(f"  Found {len(markets)} album sales markets")
        
        for sale in sales_data:
            artist = sale["artist"].lower()
            units = sale["units"]
            
            for m in markets:
                title = m.get("title", "").lower()
                subtitle = m.get("subtitle", "").lower()
                
                # Check if artist name appears in market title
                if artist in title or artist in subtitle:
                    evaluate_album_trade(m, sale)
                    
    except Exception as e:
        print(f"  ⚠️ Market matching failed: {e}")

def evaluate_album_trade(market, sale):
    """Evaluate and potentially execute a trade based on album sales data."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    units = sale["units"]
    artist = sale["artist"]
    
    # Parse the market's threshold from title or ticker
    # Typical: "Will [Artist] sell more than X copies?"
    threshold_match = re.search(r'(\d{1,3}(?:,\d{3})*)\s*(?:K|thousand|copies|units)', title, re.I)
    if not threshold_match:
        threshold_match = re.search(r'T(\d+)', ticker)
    
    if not threshold_match:
        print(f"  Could not parse threshold from market: {title}")
        return
    
    threshold = int(threshold_match.group(1).replace(",", ""))
    if threshold < 1000:
        threshold *= 1000
    
    # Determine outcome
    if units > threshold * 1.05:  # 5% buffer for revisions
        outcome = "yes"
        confidence = min(0.95, 0.7 + (units - threshold) / threshold * 0.5)
    elif units < threshold * 0.95:
        outcome = "no"
        confidence = min(0.95, 0.7 + (threshold - units) / threshold * 0.5)
    else:
        print(f"  ⚠️ {artist}: {units} units too close to threshold {threshold}, skipping")
        return
    
    # Check market price
    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    
    if outcome == "yes" and yes_ask and yes_ask < confidence * 100:
        edge = confidence - yes_ask / 100
        if edge > 0.10:
            count = max(1, min((config["maxTradeAmount"] * 100) // yes_ask, 20))
            reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units > {threshold/1000:.0f}K threshold. YES at {yes_ask}¢, confidence {confidence*100:.0f}%"
            
            print(f"\n🐙💰 ARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
            print(f"    Market: {ticker} YES at {yes_ask}¢ → buying YES (confirmed outcome)")
            print(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {yes_ask}¢ = ${count*yes_ask/100:.2f}")
            
            place_trade(ticker, "yes", yes_ask, count, reasoning)
    
    elif outcome == "no" and no_ask and no_ask < confidence * 100:
        edge = confidence - no_ask / 100
        if edge > 0.10:
            count = max(1, min((config["maxTradeAmount"] * 100) // no_ask, 20))
            reasoning = f"HDD confirms {artist} sold {units/1000:.0f}K units < {threshold/1000:.0f}K threshold. NO at {no_ask}¢, confidence {confidence*100:.0f}%"
            
            print(f"\n🐙💰 ARBITRAGE FOUND: HITS Daily Double confirms {artist} sold {units/1000:.0f}K units")
            print(f"    Market: {ticker} NO at {no_ask}¢ → buying NO (confirmed under threshold)")
            print(f"    Edge: ~{edge*100:.0f}% | Trade: {count} contracts @ {no_ask}¢ = ${count*no_ask/100:.2f}")
            
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
        print(f"\n🎬 [BOX OFFICE] Skipping — {day_name} not in active days {active_days}")
        return
    
    print(f"\n🎬 [BOX OFFICE] Checking box office data ({day_name})...")
    
    box_office_data = []
    
    # Check The Numbers
    try:
        url = "https://www.the-numbers.com/market/"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}, timeout=20)
        r.raise_for_status()
        save_snapshot("boxoffice_thenumbers", r.text)
        
        # Parse weekend box office table
        # Look for movie titles and gross amounts
        # Pattern: movie name ... $XX,XXX,XXX
        gross_pattern = r'(?:>)([^<]{3,50})</a>\s*</td>\s*<td[^>]*>\s*\$?([\d,]+)'
        matches = re.findall(gross_pattern, r.text)
        
        for title, gross in matches[:10]:
            title = title.strip()
            gross_val = int(gross.replace(",", ""))
            if gross_val > 100000:  # At least $100K
                box_office_data.append({
                    "title": title,
                    "gross": gross_val,
                    "source": "the-numbers.com"
                })
        
        if box_office_data:
            print(f"  📊 The Numbers: {len(box_office_data)} movies found")
            for d in box_office_data[:5]:
                print(f"    → {d['title']}: ${d['gross']:,}")
    except Exception as e:
        print(f"  ⚠️ The Numbers check failed: {e}")
    
    # Check Box Office Mojo
    try:
        url = "https://www.boxofficemojo.com/"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}, timeout=20)
        r.raise_for_status()
        save_snapshot("boxoffice_mojo", r.text)
        
        # Parse for box office numbers
        money_pattern = r'\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*[MmBb]?'
        movies = re.findall(r'(?:>)([^<]{3,50})</a>.*?\$([\d,.]+)\s*[MmBb]?', r.text, re.DOTALL)
        
        mojo_data = []
        for title, gross in movies[:10]:
            title = title.strip()
            gross_clean = gross.replace(",", "")
            try:
                gross_val = float(gross_clean)
                if gross_val < 1000:  # Likely in millions
                    gross_val *= 1_000_000
                mojo_data.append({"title": title, "gross": int(gross_val), "source": "boxofficemojo.com"})
            except:
                pass
        
        if mojo_data:
            print(f"  📊 Box Office Mojo: {len(mojo_data)} movies found")
            box_office_data.extend(mojo_data)
    except Exception as e:
        print(f"  ⚠️ Box Office Mojo check failed: {e}")
    
    if box_office_data:
        match_boxoffice_to_markets(box_office_data)

def match_boxoffice_to_markets(box_data):
    """Match box office data to Kalshi markets."""
    try:
        # Try various ticker prefixes for box office
        markets = []
        for prefix in ["KXBOXOFFICE", "KXBOX", "KXMOVIE", "KXFILM"]:
            markets.extend(get_markets_by_prefix(prefix))
        
        if not markets:
            print(f"  No open box office markets found on Kalshi")
            return
        
        print(f"  Found {len(markets)} box office markets")
        
        for movie in box_data:
            title_lower = movie["title"].lower()
            for m in markets:
                market_title = m.get("title", "").lower()
                # Fuzzy match — check if key words from movie title appear in market
                title_words = [w for w in title_lower.split() if len(w) > 3]
                if any(w in market_title for w in title_words):
                    evaluate_boxoffice_trade(m, movie)
                    
    except Exception as e:
        print(f"  ⚠️ Box office market matching failed: {e}")

def evaluate_boxoffice_trade(market, movie):
    """Evaluate box office trade opportunity."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    gross = movie["gross"]
    movie_title = movie["title"]
    
    # Parse threshold from market title (e.g., "Will X gross more than $50M?")
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
            print(f"\n🐙💰 ARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M > ${threshold/1e6:.0f}M")
            print(f"    Market: {ticker} YES at {yes_ask}¢")
            place_trade(ticker, "yes", yes_ask, count, reasoning)
    
    elif outcome == "no" and no_ask and no_ask < 85:
        edge = 0.90 - no_ask / 100
        if edge > 0.10:
            count = max(1, (config["maxTradeAmount"] * 100) // no_ask)
            reasoning = f"Box office data shows {movie_title} at ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M threshold"
            print(f"\n🐙💰 ARBITRAGE FOUND: {movie_title} box office ${gross/1e6:.1f}M < ${threshold/1e6:.0f}M")
            print(f"    Market: {ticker} NO at {no_ask}¢")
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
    print(f"\n🌡️ [NWS] Checking actual temperatures...")
    
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
                print(f"  {city_code} ({station_id}): {temp_f:.1f}°F ({temp_c:.1f}°C) @ {props.get('timestamp', '?')}")
            else:
                print(f"  {city_code} ({station_id}): No temperature data available")
                
        except Exception as e:
            print(f"  ⚠️ NWS check failed for {city_code} ({station_id}): {e}")
    
    if actual_temps:
        # Also get daily max observations for today
        check_nws_daily_highs(actual_temps)
        match_nws_to_markets(actual_temps)

def check_nws_daily_highs(current_temps):
    """Check for daily high temperature observations — these are what Kalshi settles on."""
    stations = config["sources"]["nws"]["stations"]
    
    for city_code, station_id in stations.items():
        try:
            # Get today's observations to find the running max
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
                    print(f"  {city_code} running high today: {running_high:.1f}°F ({len(temps)} observations)")
        except Exception as e:
            print(f"  ⚠️ Daily high check failed for {city_code}: {e}")

def match_nws_to_markets(temp_data):
    """Match actual NWS temperature data to open Kalshi temperature markets."""
    try:
        markets = get_markets_by_prefix("KXHIGH")
        if not markets:
            print(f"  No open KXHIGH markets found")
            return
        
        today = datetime.date.today().isoformat()
        today_markets = []
        
        for m in markets:
            parsed = parse_temp_ticker(m.get("ticker", ""))
            if parsed and parsed["date"] == today:
                today_markets.append((m, parsed))
        
        if not today_markets:
            print(f"  No KXHIGH markets settling today ({today})")
            return
        
        print(f"  Found {len(today_markets)} temperature markets settling today")
        
        now = datetime.datetime.now()
        # Only trade on temperature after 3 PM local (most of the day's heat recorded)
        if now.hour < 15:
            print(f"  ⏰ Before 3 PM — waiting for more temperature data before trading")
            # Still log the data, just don't trade yet
            for m, parsed in today_markets:
                city = parsed["city"]
                if city in temp_data and "running_high_f" in temp_data[city]:
                    high = temp_data[city]["running_high_f"]
                    thresh = parsed["threshold"]
                    direction = "above" if parsed["direction"] == "T" else "bracket"
                    print(f"    {m['ticker']}: running high {high:.1f}°F vs threshold {thresh}°F ({direction})")
            return
        
        # After 3 PM — the running high is likely close to the final high
        for m, parsed in today_markets:
            city = parsed["city"]
            if city not in temp_data or "running_high_f" not in temp_data[city]:
                continue
            
            running_high = temp_data[city]["running_high_f"]
            threshold = parsed["threshold"]
            direction = parsed["direction"]
            ticker = m.get("ticker", "")
            
            if direction == "T":
                # YES = temp > threshold
                margin = running_high - threshold
                
                if margin > 3:
                    # Clearly above threshold — buy YES
                    yes_ask = m.get("yes_ask", 0)
                    if yes_ask and yes_ask < 85:
                        edge = 0.92 - yes_ask / 100
                        if edge > 0.10:
                            count = max(1, (config["maxTradeAmount"] * 100) // yes_ask)
                            reasoning = f"NWS {city} running high {running_high:.1f}°F > {threshold}°F threshold by {margin:.1f}°F (after 3PM)"
                            print(f"\n🐙💰 ARBITRAGE FOUND: NWS actual temp confirms {city} high {running_high:.1f}°F > {threshold}°F")
                            print(f"    Market: {ticker} YES at {yes_ask}¢ → buying YES")
                            print(f"    Edge: ~{edge*100:.0f}% | Margin: {margin:.1f}°F")
                            place_trade(ticker, "yes", yes_ask, count, reasoning)
                
                elif margin < -3:
                    # Clearly below threshold — buy NO
                    no_ask = m.get("no_ask", 0)
                    if no_ask and no_ask < 85:
                        edge = 0.92 - no_ask / 100
                        if edge > 0.10:
                            count = max(1, (config["maxTradeAmount"] * 100) // no_ask)
                            reasoning = f"NWS {city} running high {running_high:.1f}°F < {threshold}°F threshold by {abs(margin):.1f}°F (after 3PM)"
                            print(f"\n🐙💰 ARBITRAGE FOUND: NWS actual temp confirms {city} high {running_high:.1f}°F < {threshold}°F")
                            print(f"    Market: {ticker} NO at {no_ask}¢ → buying NO")
                            print(f"    Edge: ~{edge*100:.0f}% | Margin: {abs(margin):.1f}°F")
                            place_trade(ticker, "no", no_ask, count, reasoning)
                
                else:
                    print(f"  {ticker}: running high {running_high:.1f}°F vs {threshold}°F — too close (margin {margin:.1f}°F)")
                    
    except Exception as e:
        print(f"  ⚠️ NWS market matching failed: {e}")
        traceback.print_exc()


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("=" * 70)
    print("🐙 Kalshi Settlement Source Monitor — Information Arbitrage Bot")
    print(f"   Mode: {config['mode']} | Max: ${config['maxTradeAmount']}/trade | Daily limit: {config['maxDailyTrades']} trades")
    print(f"   Sources: HDD={config['sources']['hdd']['enabled']} | BoxOffice={config['sources']['boxoffice']['enabled']} | NWS={config['sources']['nws']['enabled']}")
    print("=" * 70)
    
    # Verify Kalshi auth
    print("\nVerifying Kalshi authentication...")
    try:
        bal = api("GET", "/portfolio/balance")
        print(f"✅ Auth OK! Balance: ${bal.get('balance', 0)/100:.2f}")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        sys.exit(1)
    
    # Track last check times
    last_hdd = 0
    last_boxoffice = 0
    last_nws = 0
    
    hdd_interval = config["sources"]["hdd"]["intervalMinutes"] * 60
    box_interval = config["sources"]["boxoffice"]["intervalMinutes"] * 60
    nws_interval = config["sources"]["nws"]["intervalMinutes"] * 60
    
    print(f"\n🔄 Starting monitoring loop...")
    print(f"   HDD: every {config['sources']['hdd']['intervalMinutes']}min")
    print(f"   Box Office: every {config['sources']['boxoffice']['intervalMinutes']}min (Fri-Mon)")
    print(f"   NWS: every {config['sources']['nws']['intervalMinutes']}min\n")
    
    while True:
        now = time.time()
        reset_daily_if_needed()
        
        try:
            # Check HDD
            if config["sources"]["hdd"]["enabled"] and (now - last_hdd) >= hdd_interval:
                try:
                    check_hdd()
                except Exception as e:
                    print(f"⚠️ HDD source error: {e}")
                    traceback.print_exc()
                last_hdd = now
            
            # Check Box Office
            if config["sources"]["boxoffice"]["enabled"] and (now - last_boxoffice) >= box_interval:
                try:
                    check_boxoffice()
                except Exception as e:
                    print(f"⚠️ Box office source error: {e}")
                    traceback.print_exc()
                last_boxoffice = now
            
            # Check NWS
            if config["sources"]["nws"]["enabled"] and (now - last_nws) >= nws_interval:
                try:
                    check_nws()
                except Exception as e:
                    print(f"⚠️ NWS source error: {e}")
                    traceback.print_exc()
                last_nws = now
                
        except Exception as e:
            print(f"⚠️ Main loop error: {e}")
            traceback.print_exc()
        
        # Sleep 30 seconds between checks (the individual intervals control per-source timing)
        sys.stdout.flush()
        time.sleep(30)


if __name__ == "__main__":
    main()
