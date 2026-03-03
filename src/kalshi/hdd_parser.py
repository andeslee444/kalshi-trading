"""Shared HITS Daily Double (HDD) parsing and data fetching.

Provides Sanity CMS access and chart/article parsing for album sales data.
Used by entertainment-bot, source-monitor, and hdd-scraper.

Kalshi KXALBUMSALES markets settle directly on the HDD Hits Top 50
"Albums" column (pure album sales). When this chart publishes, the
settlement value is known. The "Activity" column (total equivalent units
including streaming) is NOT what Kalshi settles on — it's only used as
a fallback when the Albums column is unavailable.

Key discovery: HDD uses Sanity.io CMS (project: 8aky18h3).
We can query their API directly for structured chart data.
"""

import re
import logging
import datetime
import requests

_log = logging.getLogger("hdd_parser")

# === Sanity CMS Config ===
SANITY_PROJECT = "8aky18h3"
SANITY_DATASET = "production"
SANITY_API_VERSION = "2021-10-21"
SANITY_BASE = f"https://{SANITY_PROJECT}.api.sanity.io/v{SANITY_API_VERSION}/data/query/{SANITY_DATASET}"


# ============================================================
# SANITY CMS QUERIES
# ============================================================

def sanity_query(groq_query):
    """Execute a GROQ query against HDD's Sanity CMS."""
    r = requests.get(SANITY_BASE, params={"query": groq_query}, timeout=20)
    r.raise_for_status()
    return r.json().get("result")


def fetch_latest_chart(chart_slug):
    """Fetch the latest chart by slug (e.g., 'hits-top-50', 'midweek-20')."""
    query = f'*[_type=="chart" && chart_type->slug.current=="{chart_slug}"]{{date,chart_data,chart_type->{{title,slug}}}}|order(date desc)[0]'
    return sanity_query(query)


def fetch_recent_articles(limit=20):
    """Fetch recent public articles from HDD."""
    query = f'*[_type=="post" && isPublic==true]{{title,slug,publishedAt,description,"category":category->title,"categorySlug":category->slug.current}}|order(publishedAt desc)[0..{limit-1}]'
    return sanity_query(query) or []


def fetch_articles_with_sales_keywords(limit=30):
    """Search for articles containing sales-related keywords."""
    query = f'*[_type=="post" && isPublic==true && (description match "sales*" || description match "units*" || description match "first week*" || description match "projected*" || title match "Chart Final*" || title match "Forecast*")]{{title,slug,publishedAt,description,"category":category->title}}|order(publishedAt desc)[0..{limit-1}]'
    return sanity_query(query) or []


# ============================================================
# CHART PARSING
# ============================================================

def parse_chart_data(raw_data):
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
        if 'MARKETSHARE' in line or (not line[0].isdigit() and not line.startswith('--')):
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

        except Exception:
            continue

    return entries


def clean_number(s):
    """Clean a number string like '290,861' or '175,345' into an int."""
    s = s.strip().replace(',', '').replace(' ', '')
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except (ValueError, OverflowError):
            return 0


# ============================================================
# ARTICLE PARSING — Extract sales numbers from text
# ============================================================

def extract_sales_from_text(text):
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
# HIGH-LEVEL DATA FUNCTIONS
# ============================================================

def parse_album_threshold(title, ticker=""):
    """Parse album sales threshold from market title/ticker.

    Returns threshold in units (e.g. 150000), or None if unparseable.
    """
    for pattern in [
        r'(\d{1,3}(?:,\d{3})*)\s*(?:K|thousand|copies|units)',
        r'more than\s+(\d{1,3}(?:,\d{3})*)',
        r'over\s+(\d{1,3}(?:,\d{3})*)',
    ]:
        match = re.search(pattern, title, re.I)
        if match:
            threshold = int(match.group(1).replace(",", ""))
            if threshold < 1000:
                threshold *= 1000
            return threshold
    # Fallback: ticker pattern like T200
    match = re.search(r'T(\d+)', ticker)
    if match:
        threshold = int(match.group(1))
        if threshold < 1000:
            threshold *= 1000
        return threshold
    return None


def compute_data_age_hours(chart_date_str):
    """Compute hours since chart data was published. Returns 0 if unparseable (fail-open)."""
    if not chart_date_str:
        return 0
    try:
        dt = datetime.datetime.fromisoformat(chart_date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        _log.warning("Unparseable chart_date: %r, treating as fresh (fail-open)", chart_date_str)
        return 0


def check_sanity_health(project_id="8aky18h3"):
    """Check if the Sanity CMS endpoint is reachable.

    Makes a minimal GROQ query to verify connectivity.
    Returns True if healthy, False otherwise.
    """
    url = f"https://{project_id}.api.sanity.io/v2023-05-03/data/query/production"
    params = {"query": '*[_type == "chart"][0]{_id}'}
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def get_album_sales(logger=None):
    """Fetch album sales data from HDD charts and articles.

    Returns a list of dicts: [{"artist": str, "units": int, "source": str}]
    Combines data from Hits Top 50, Midweek 20 charts, and articles.

    Units come from the "Albums" column (pure album sales) which is what
    Kalshi KXALBUMSALES markets settle on. Falls back to "Activity" (total
    equivalent units) only when Albums is unavailable.
    """
    log = logger or _log
    results = []
    seen_artists = set()

    # Fetch charts
    for chart_slug in ["hits-top-50", "midweek-20"]:
        try:
            chart = fetch_latest_chart(chart_slug)
            if not chart:
                continue

            chart_date = chart.get("date", "")
            entries = parse_chart_data(chart.get("chart_data", ""))
            for entry in entries:
                artist = entry.get("artist", "")
                # Kalshi settles on Albums column (pure sales), Activity is fallback
                units = entry.get("albums", 0) or entry.get("activity", 0)
                if artist and units > 0:
                    key = artist.lower()
                    if key not in seen_artists:
                        seen_artists.add(key)
                        results.append({
                            "artist": artist,
                            "units": units,
                            "source": f"hdd-{chart_slug}",
                            "chart_date": chart_date,
                        })
        except Exception as e:
            log.warning(f"HDD chart {chart_slug} fetch failed: {e}")

    # Fetch articles with sales mentions
    try:
        articles = fetch_articles_with_sales_keywords(20)
        for article in articles:
            desc = article.get("description", "") or ""
            title = article.get("title", "") or ""
            text = f"{title} {desc}"
            sales = extract_sales_from_text(text)
            if sales:
                # Try to extract artist name from title
                # Common pattern: "Artist Name First-Week Forecast: 200K"
                artist_match = re.match(r'^([A-Z][a-zA-Z\s\'.]+?)(?:\s+[-\u2013]|\s+First|\s+Chart|\s+Opens|\s+Sells)', title)
                if artist_match:
                    artist = artist_match.group(1).strip()
                    key = artist.lower()
                    if key not in seen_artists:
                        seen_artists.add(key)
                        results.append({
                            "artist": artist,
                            "units": max(sales),
                            "source": "hdd-article",
                            "chart_date": article.get("publishedAt", ""),
                        })
    except Exception as e:
        log.warning(f"HDD article fetch failed: {e}")

    return results
