#!/usr/bin/env python3
"""HITS Daily Double Scraper — Album Sales Monitor for Kalshi Info Arbitrage

Key discovery: HDD uses Sanity.io CMS (project: 8aky18h3).
We can query their API directly for:
  - Hits Top 50 chart (weekly final numbers) 
  - Midweek 20 chart (mid-week building estimates)
  - Articles with sales forecasts

Chart data format (tab-separated):
  LW  TW  ARTIST | ALBUM  LABEL  Activity  Albums  TEA/Songs  SEA/Audio  Video

Publication schedule:
  - Midweek 20: published mid-week (usually Wed) with building estimates
  - Hits Top 50: published weekly (usually Thu/Fri) with final numbers
  - Articles mentioning sales appear throughout the week

The arbitrage window: HDD publishes chart finals BEFORE Billboard/Luminate
official numbers, which is what Kalshi uses to settle markets.
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
DATA_DIR = PROJECT_DIR / "data"
SNAPSHOTS_DIR = DATA_DIR / "kalshi-source-snapshots" / "hdd"
ARTICLES_PATH = DATA_DIR / "hdd-articles.json"
KEY_PATH = PROJECT_DIR / "config" / "keys" / "kalshi-demo.pem"
API_KEY = "64b1b6ff-eac2-4977-919a-fd1b9865f0aa"
BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

# Ensure dirs
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# === Sanity CMS Config ===
SANITY_PROJECT = "8aky18h3"
SANITY_DATASET = "production"
SANITY_API_VERSION = "2021-10-21"
SANITY_BASE = f"https://{SANITY_PROJECT}.api.sanity.io/v{SANITY_API_VERSION}/data/query/{SANITY_DATASET}"

# === Kalshi Auth ===
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

def kalshi_api(method, path, body=None):
    url = BASE_URL + path
    full_path = "/trade-api/v2" + path
    headers = get_headers(method, full_path)
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ============================================================
# SANITY CMS QUERIES
# ============================================================

def sanity_query(groq_query: str) -> dict:
    """Execute a GROQ query against HDD's Sanity CMS."""
    r = requests.get(SANITY_BASE, params={"query": groq_query}, timeout=20)
    r.raise_for_status()
    return r.json().get("result")


def fetch_latest_chart(chart_slug: str) -> dict:
    """Fetch the latest chart by slug (e.g., 'hits-top-50', 'midweek-20')."""
    query = f'*[_type=="chart" && chart_type->slug.current=="{chart_slug}"]{{date,chart_data,chart_type->{{title,slug}}}}|order(date desc)[0]'
    return sanity_query(query)


def fetch_recent_articles(limit: int = 20) -> list:
    """Fetch recent public articles from HDD."""
    query = f'*[_type=="post" && isPublic==true]{{title,slug,publishedAt,description,"category":category->title,"categorySlug":category->slug.current}}|order(publishedAt desc)[0..{limit-1}]'
    return sanity_query(query) or []


def fetch_articles_with_sales_keywords(limit: int = 30) -> list:
    """Search for articles containing sales-related keywords."""
    # Sanity GROQ doesn't support full-text search well, but we can filter by description
    query = f'*[_type=="post" && isPublic==true && (description match "sales*" || description match "units*" || description match "first week*" || description match "projected*" || title match "Chart Final*" || title match "Forecast*")]{{title,slug,publishedAt,description,"category":category->title}}|order(publishedAt desc)[0..{limit-1}]'
    return sanity_query(query) or []


# ============================================================
# CHART PARSING
# ============================================================

def parse_chart_data(raw_data: str) -> list:
    """Parse HDD tab-separated chart data into structured entries.
    
    Format: LW TW ARTIST | ALBUM  LABEL  Numbers...
    The numbers vary by chart type:
      Hits Top 50: Activity, Albums, TEA, SEA, (sometimes Video)  
      Midweek 20: Projected Activity, Projected Albums
    """
    entries = []
    if not raw_data:
        return entries
    
    lines = raw_data.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Skip header/market share line
        if line.startswith('MARKETSHARE') or line.startswith('--\t') is False and not line[0].isdigit() and line[:2] != '--':
            if 'MARKETSHARE' in line:
                continue
        
        # Split by tabs
        parts = [p.strip() for p in line.split('\t') if p.strip()]
        if len(parts) < 4:
            continue
        
        try:
            # Parse rank positions
            lw = parts[0] if parts[0] != '--' else None
            tw = parts[1] if len(parts) > 1 else None
            
            # Find the artist|album field
            artist_album = None
            label = None
            numbers = []
            
            for i, p in enumerate(parts[2:], 2):
                if '|' in p:
                    artist_album = p
                    # Everything after this: label, then numbers
                    remaining = parts[i+1:]
                    if remaining:
                        label = remaining[0]
                        numbers = [clean_number(n) for n in remaining[1:]]
                    break
            
            if not artist_album:
                continue
            
            # Split artist | album
            aa_parts = artist_album.split('|', 1)
            artist = aa_parts[0].strip()
            album = aa_parts[1].strip() if len(aa_parts) > 1 else ""
            
            entry = {
                "rank": int(tw) if tw and tw.isdigit() else None,
                "last_week": int(lw) if lw and lw.isdigit() else None,
                "artist": artist,
                "album": album,
                "label": label,
                "numbers": numbers,
            }
            
            # Assign named fields based on number count
            if len(numbers) >= 1:
                entry["activity"] = numbers[0]
            if len(numbers) >= 2:
                entry["albums"] = numbers[1]
            if len(numbers) >= 3:
                entry["tea_songs"] = numbers[2]
            if len(numbers) >= 4:
                entry["sea_audio"] = numbers[3]
            if len(numbers) >= 5:
                entry["video"] = numbers[4]
            
            entries.append(entry)
            
        except Exception as e:
            # Skip unparseable lines
            continue
    
    return entries


def clean_number(s: str) -> int:
    """Clean a number string like '290,861' or '175,345' into an int."""
    s = s.strip().replace(',', '').replace(' ', '')
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except:
            return 0


# ============================================================
# ARTICLE PARSING — Extract sales numbers from descriptions
# ============================================================

def extract_sales_from_text(text: str) -> list:
    """Extract album sales figures from article text.
    
    Common patterns in HDD articles:
    - "Artist sold 150K units"
    - "projected at 150-175K"
    - "building toward 200K"
    - "first-week number: 290K"
    - "opening with 300,000 units"
    """
    if not text:
        return []
    
    found = []
    patterns = [
        # "290K" or "290,000 units/copies"  
        r'(\d{2,3}(?:,\d{3})*)\s*(?:K|k)\s*(?:units|copies|sales|album equiv|equivalent)?',
        r'(\d{1,3}(?:,\d{3})+)\s*(?:units|copies|sales)',
        # "projected at/to X"
        r'projected\s+(?:at|to|for)\s+(\d{2,3}(?:,\d{3})*)\s*[Kk]?',
        # "building toward X"  
        r'building\s+(?:toward|to|at)\s+(\d{2,3}(?:,\d{3})*)\s*[Kk]?',
        # "opening with X"
        r'opening\s+(?:with|at)\s+(\d{2,3}(?:,\d{3})*)\s*[Kk]?',
        # "first.week.*X"
        r'first[\s-]week.*?(\d{2,3}(?:,\d{3})*)\s*[Kk]?',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for m in matches:
            num = clean_number(m)
            if num < 1000:
                num *= 1000  # Was in K
            if 5000 <= num <= 5_000_000:  # Reasonable album sales range
                found.append(num)
    
    return found


# ============================================================
# SNAPSHOT & ARTICLE LOGGING
# ============================================================

def save_snapshot(name: str, data, ext: str = "json"):
    """Save a timestamped snapshot."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{name}_{ts}.{ext}"
    path = SNAPSHOTS_DIR / fname
    if isinstance(data, (dict, list)):
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False)[:500000])
    else:
        path.write_text(str(data)[:500000])
    return fname


def load_articles_log() -> list:
    if ARTICLES_PATH.exists():
        try:
            return json.loads(ARTICLES_PATH.read_text())
        except:
            return []
    return []


def save_articles_log(articles: list):
    ARTICLES_PATH.write_text(json.dumps(articles, indent=2, ensure_ascii=False))


def log_article(article: dict) -> bool:
    """Log a new article. Returns True if it was new."""
    articles = load_articles_log()
    slug = article.get("slug", {})
    if isinstance(slug, dict):
        slug = slug.get("current", "")
    
    # Check if already logged
    for existing in articles:
        if existing.get("slug") == slug:
            return False
    
    entry = {
        "slug": slug,
        "title": article.get("title", ""),
        "description": article.get("description", ""),
        "category": article.get("category", ""),
        "publishedAt": article.get("publishedAt", ""),
        "logged_at": datetime.datetime.now().isoformat(),
        "sales_data": extract_sales_from_text(article.get("description", "")),
    }
    articles.insert(0, entry)
    save_articles_log(articles)
    return True


# ============================================================
# KALSHI MARKET MATCHING
# ============================================================

def find_album_markets() -> list:
    """Search for open album/music sales markets on Kalshi."""
    markets = []
    # Try various ticker prefixes
    for prefix in ["KXALBUMSALES", "KXALBUM", "KXMUSIC", "KXBILLBOARD", "BILLBOARD"]:
        cursor = None
        for _ in range(5):
            path = f"/markets?status=open&limit=200"
            if cursor:
                path += f"&cursor={cursor}"
            try:
                data = kalshi_api("GET", path)
                for m in data.get("markets", []):
                    ticker = m.get("ticker", "")
                    title = m.get("title", "").lower()
                    # Match by ticker prefix or title keywords
                    if ticker.startswith(prefix) or any(kw in title for kw in ["album", "billboard", "first week", "sales", "chart"]):
                        markets.append(m)
                cursor = data.get("cursor")
                if not cursor or not data.get("markets"):
                    break
            except Exception as e:
                break
    
    # Deduplicate
    seen = set()
    unique = []
    for m in markets:
        t = m.get("ticker")
        if t not in seen:
            seen.add(t)
            unique.append(m)
    return unique


def match_chart_to_markets(chart_entries: list, markets: list):
    """Match chart data to Kalshi markets and identify arbitrage."""
    if not markets:
        print("  No album/music markets found on Kalshi")
        return
    
    print(f"  Found {len(markets)} potential music markets")
    
    for entry in chart_entries[:20]:  # Top 20
        artist = entry["artist"].lower()
        album = entry.get("album", "").lower()
        activity = entry.get("activity", 0)
        albums_sold = entry.get("albums", 0)
        
        for m in markets:
            title_lower = m.get("title", "").lower()
            subtitle = m.get("subtitle", "").lower()
            
            # Check if artist appears in market
            artist_words = [w for w in artist.split() if len(w) > 2]
            if any(w in title_lower or w in subtitle for w in artist_words):
                evaluate_trade(m, entry)


def evaluate_trade(market: dict, chart_entry: dict):
    """Evaluate if chart data creates a trading opportunity."""
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    artist = chart_entry["artist"]
    activity = chart_entry.get("activity", 0)
    albums_sold = chart_entry.get("albums", 0)
    
    # Parse threshold from market title
    threshold_match = re.search(r'(\d{1,3}(?:,\d{3})*)\s*(?:K|thousand|copies|units)', title, re.I)
    if not threshold_match:
        threshold_match = re.search(r'T(\d+)', ticker)
    
    if not threshold_match:
        print(f"    ⚠️ Cannot parse threshold: {title[:80]}")
        return
    
    threshold = clean_number(threshold_match.group(1))
    if threshold < 1000:
        threshold *= 1000
    
    # Use activity (total equiv units) as primary metric
    units = activity if activity > 0 else albums_sold
    if units == 0:
        return
    
    margin_pct = (units - threshold) / threshold if threshold > 0 else 0
    
    if margin_pct > 0.05:
        outcome = "yes"
        confidence = min(0.95, 0.75 + margin_pct * 0.4)
    elif margin_pct < -0.05:
        outcome = "no"
        confidence = min(0.95, 0.75 + abs(margin_pct) * 0.4)
    else:
        print(f"    ⚠️ {artist}: {units:,} units too close to threshold {threshold:,}")
        return
    
    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    
    price = yes_ask if outcome == "yes" else no_ask
    if price and price < confidence * 100:
        edge = confidence - price / 100
        if edge > 0.10:
            print(f"\n🐙💰 ARBITRAGE SIGNAL: HDD chart confirms {artist}")
            print(f"    Units: {units:,} vs threshold: {threshold:,} ({margin_pct*100:+.0f}%)")
            print(f"    Market: {ticker} {outcome.upper()} at {price}¢ (confidence: {confidence*100:.0f}%)")
            print(f"    Edge: ~{edge*100:.0f}%")


# ============================================================
# MAIN SCAN FUNCTIONS  
# ============================================================

def scan_charts():
    """Fetch and analyze current HDD charts."""
    print("\n📀 [HDD] Scanning charts...")
    
    for chart_slug in ["hits-top-50", "midweek-20"]:
        try:
            chart = fetch_latest_chart(chart_slug)
            if not chart:
                print(f"  No data for {chart_slug}")
                continue
            
            date = chart.get("date", "?")
            raw = chart.get("chart_data", "")
            chart_title = chart.get("chart_type", {}).get("title", chart_slug)
            
            print(f"\n  📊 {chart_title} ({date})")
            save_snapshot(f"chart_{chart_slug}", {"date": date, "raw": raw})
            
            entries = parse_chart_data(raw)
            if entries:
                print(f"  Parsed {len(entries)} entries:")
                for e in entries[:10]:
                    rank = e.get("rank", "?")
                    activity = e.get("activity", 0)
                    albums = e.get("albums", 0)
                    print(f"    #{rank} {e['artist']} - {e['album']}")
                    print(f"       Activity: {activity:,} | Albums: {albums:,} | Label: {e.get('label', '?')}")
            else:
                print(f"  ⚠️ Could not parse chart entries")
                print(f"  Raw (first 300): {raw[:300]}")
                
        except Exception as e:
            print(f"  ⚠️ Chart {chart_slug} failed: {e}")
            traceback.print_exc()


def scan_articles():
    """Fetch and log recent HDD articles, looking for sales mentions."""
    print("\n📰 [HDD] Scanning articles...")
    
    try:
        articles = fetch_recent_articles(30)
        new_count = 0
        sales_articles = []
        
        for article in articles:
            is_new = log_article(article)
            if is_new:
                new_count += 1
            
            # Check for sales-related content
            desc = article.get("description", "") or ""
            title = article.get("title", "") or ""
            text = f"{title} {desc}"
            
            sales_keywords = ["first week", "sales", "units", "projected", "building", 
                            "chart final", "forecast", "sold", "opening"]
            if any(kw in text.lower() for kw in sales_keywords):
                sales = extract_sales_from_text(text)
                sales_articles.append({
                    "title": title,
                    "description": desc,
                    "sales_numbers": sales,
                    "slug": article.get("slug", {}).get("current", ""),
                })
        
        print(f"  Found {len(articles)} articles ({new_count} new)")
        
        if sales_articles:
            print(f"\n  🎯 Articles with sales mentions:")
            for sa in sales_articles:
                print(f"    → {sa['title']}")
                if sa['description']:
                    print(f"      {sa['description'][:120]}")
                if sa['sales_numbers']:
                    print(f"      💰 Extracted numbers: {[f'{n:,}' for n in sa['sales_numbers']]}")
        else:
            print("  No articles with sales data found in recent batch")
            
    except Exception as e:
        print(f"  ⚠️ Article scan failed: {e}")
        traceback.print_exc()
    
    # Also try keyword search
    try:
        keyword_articles = fetch_articles_with_sales_keywords(20)
        if keyword_articles:
            print(f"\n  🔍 Keyword search found {len(keyword_articles)} sales-related articles")
            for ka in keyword_articles[:5]:
                title = ka.get("title", "")
                desc = ka.get("description", "")
                pub = ka.get("publishedAt", "")[:10]
                print(f"    [{pub}] {title}")
                if desc:
                    print(f"      {desc[:120]}")
    except Exception as e:
        print(f"  ⚠️ Keyword search failed: {e}")


def scan_kalshi_markets():
    """Search for matching Kalshi markets."""
    print("\n🏛️ [KALSHI] Searching for music/album markets...")
    
    try:
        markets = find_album_markets()
        if markets:
            print(f"  Found {len(markets)} music-related markets:")
            for m in markets[:20]:
                ticker = m.get("ticker", "")
                title = m.get("title", "")
                yes_ask = m.get("yes_ask", "?")
                no_ask = m.get("no_ask", "?")
                print(f"    {ticker}: {title[:80]} | Yes:{yes_ask}¢ No:{no_ask}¢")
        else:
            print("  No open music/album markets found")
            print("  (This is expected — these markets appear around major album releases)")
        
        return markets
    except Exception as e:
        print(f"  ⚠️ Kalshi search failed: {e}")
        traceback.print_exc()
        return []


def run_full_scan():
    """Run a complete scan: charts, articles, market matching."""
    print("=" * 70)
    print("📀 HITS Daily Double Scraper — Kalshi Info Arbitrage")
    print(f"   Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Verify Kalshi auth
    try:
        bal = kalshi_api("GET", "/portfolio/balance")
        print(f"✅ Kalshi auth OK! Balance: ${bal.get('balance', 0)/100:.2f}")
    except Exception as e:
        print(f"⚠️ Kalshi auth failed: {e}")
    
    # Scan charts
    scan_charts()
    
    # Scan articles
    scan_articles()
    
    # Search Kalshi markets
    markets = scan_kalshi_markets()
    
    # If we have both chart data and markets, try matching
    if markets:
        print("\n🔗 [MATCHING] Connecting HDD data to Kalshi markets...")
        for chart_slug in ["hits-top-50", "midweek-20"]:
            try:
                chart = fetch_latest_chart(chart_slug)
                if chart:
                    entries = parse_chart_data(chart.get("chart_data", ""))
                    if entries:
                        match_chart_to_markets(entries, markets)
            except:
                pass
    
    print("\n" + "=" * 70)
    print("✅ Scan complete")
    print("=" * 70)


def monitor_loop(interval_minutes: int = 15):
    """Run continuous monitoring."""
    print(f"🔄 Starting HDD monitor (checking every {interval_minutes} min)...")
    
    while True:
        try:
            run_full_scan()
        except Exception as e:
            print(f"⚠️ Scan error: {e}")
            traceback.print_exc()
        
        print(f"\n⏳ Next scan in {interval_minutes} minutes...")
        sys.stdout.flush()
        time.sleep(interval_minutes * 60)


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="HDD Scraper for Kalshi Arbitrage")
    parser.add_argument("command", nargs="?", default="scan", 
                       choices=["scan", "monitor", "charts", "articles", "markets"],
                       help="Command to run")
    parser.add_argument("--interval", type=int, default=15, help="Monitor interval (minutes)")
    args = parser.parse_args()
    
    if args.command == "scan":
        run_full_scan()
    elif args.command == "monitor":
        monitor_loop(args.interval)
    elif args.command == "charts":
        scan_charts()
    elif args.command == "articles":
        scan_articles()
    elif args.command == "markets":
        scan_kalshi_markets()
