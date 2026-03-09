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

Kalshi KXALBUMSALES markets settle directly on the HDD Hits Top 50
"Albums" column. When this chart publishes, the settlement value is known.
"""

import json, time, datetime, os, sys, re
import requests
from pathlib import Path
from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR
from probability import info_arb_probability, album_data_sigma
from hdd_parser import (
    sanity_query, fetch_latest_chart, fetch_recent_articles,
    fetch_articles_with_sales_keywords, parse_chart_data, clean_number,
    extract_sales_from_text, parse_album_threshold,
    SANITY_PROJECT, SANITY_DATASET, SANITY_BASE,
)

# Unbuffered output
setup_unbuffered()
log = setup_logging("hdd-scraper")

# === Paths ===
DATA_DIR = PROJECT_DIR / "data"
SNAPSHOTS_DIR = DATA_DIR / "kalshi-source-snapshots" / "hdd"
ARTICLES_PATH = DATA_DIR / "hdd-articles.json"

# Ensure dirs
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# === Kalshi Client ===
client = KalshiClient()


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
        except (json.JSONDecodeError, ValueError):
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
    prefixes = ["KXALBUMSALES", "KXALBUM", "KXMUSIC", "KXBILLBOARD", "BILLBOARD"]
    title_keywords = ["album", "billboard", "first week", "sales", "chart"]

    all_markets = client.get_all_markets()

    markets = []
    for m in all_markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "").lower()
        if any(ticker.startswith(prefix) for prefix in prefixes):
            markets.append(m)
        elif any(kw in title for kw in title_keywords):
            markets.append(m)

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
        log.info("  No album/music markets found on Kalshi")
        return

    log.info(f"  Found {len(markets)} potential music markets")

    for entry in chart_entries[:20]:  # Top 20
        artist = entry["artist"].lower()
        album = entry.get("album", "").lower()
        # Kalshi settles on Albums column; Activity is fallback
        albums_sold = entry.get("albums", 0)
        activity = entry.get("activity", 0)

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
    threshold = parse_album_threshold(title, ticker)
    if not threshold:
        log.warning(f"    Cannot parse threshold: {title[:80]}")
        return

    # Kalshi settles on Albums column; Activity is fallback
    units = albums_sold if albums_sold > 0 else activity
    if units == 0:
        return

    sigma = album_data_sigma(datetime.datetime.now().weekday())
    confidence = info_arb_probability(units, threshold, sigma)

    if confidence > 0.5:
        outcome = "yes"
    else:
        outcome = "no"
        confidence = 1.0 - confidence

    if confidence < 0.60:
        log.warning(f"    {artist}: {units:,} units vs {threshold:,} threshold, confidence {confidence*100:.0f}% too low")
        return

    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)

    price = yes_ask if outcome == "yes" else no_ask
    if price and price < confidence * 100:
        edge = confidence - price / 100
        if edge > 0.10:
            margin_pct = (units - threshold) / threshold if threshold else 0
            log.info(f"\n ARBITRAGE SIGNAL: HDD chart confirms {artist}")
            log.info(f"    Units: {units:,} vs threshold: {threshold:,} ({margin_pct*100:+.0f}%)")
            log.info(f"    Market: {ticker} {outcome.upper()} at {price}c (confidence: {confidence*100:.0f}%)")
            log.info(f"    Edge: ~{edge*100:.0f}%")


# ============================================================
# MAIN SCAN FUNCTIONS
# ============================================================

def scan_charts():
    """Fetch and analyze current HDD charts."""
    log.info("\n[HDD] Scanning charts...")

    for chart_slug in ["hits-top-50", "midweek-20"]:
        try:
            chart = fetch_latest_chart(chart_slug)
            if not chart:
                log.info(f"  No data for {chart_slug}")
                continue

            date = chart.get("date", "?")
            raw = chart.get("chart_data", "")
            chart_title = chart.get("chart_type", {}).get("title", chart_slug)

            log.info(f"\n  {chart_title} ({date})")
            save_snapshot(f"chart_{chart_slug}", {"date": date, "raw": raw})

            entries = parse_chart_data(raw)
            if entries:
                log.info(f"  Parsed {len(entries)} entries:")
                for e in entries[:10]:
                    rank = e.get("rank", "?")
                    activity = e.get("activity", 0)
                    albums = e.get("albums", 0)
                    log.info(f"    #{rank} {e['artist']} - {e['album']}")
                    log.info(f"       Activity: {activity:,} | Albums: {albums:,} | Label: {e.get('label', '?')}")
            else:
                log.warning(f"  Could not parse chart entries")
                log.info(f"  Raw (first 300): {raw[:300]}")

        except Exception as e:
            log.error("  Chart %s failed: %s", chart_slug, e, exc_info=True)


def scan_articles():
    """Fetch and log recent HDD articles, looking for sales mentions."""
    log.info("\n[HDD] Scanning articles...")

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

        log.info(f"  Found {len(articles)} articles ({new_count} new)")

        if sales_articles:
            log.info(f"\n  Articles with sales mentions:")
            for sa in sales_articles:
                log.info(f"    -> {sa['title']}")
                if sa['description']:
                    log.info(f"      {sa['description'][:120]}")
                if sa['sales_numbers']:
                    log.info(f"      Extracted numbers: {[f'{n:,}' for n in sa['sales_numbers']]}")
        else:
            log.info("  No articles with sales data found in recent batch")

    except Exception as e:
        log.error("  Article scan failed: %s", e, exc_info=True)

    # Also try keyword search
    try:
        keyword_articles = fetch_articles_with_sales_keywords(20)
        if keyword_articles:
            log.info(f"\n  Keyword search found {len(keyword_articles)} sales-related articles")
            for ka in keyword_articles[:5]:
                title = ka.get("title", "")
                desc = ka.get("description", "")
                pub = ka.get("publishedAt", "")[:10]
                log.info(f"    [{pub}] {title}")
                if desc:
                    log.info(f"      {desc[:120]}")
    except Exception as e:
        log.error(f"  Keyword search failed: {e}")


def scan_kalshi_markets():
    """Search for matching Kalshi markets."""
    log.info("\n[KALSHI] Searching for music/album markets...")

    try:
        markets = find_album_markets()
        if markets:
            log.info(f"  Found {len(markets)} music-related markets:")
            for m in markets[:20]:
                ticker = m.get("ticker", "")
                title = m.get("title", "")
                yes_ask = m.get("yes_ask", "?")
                no_ask = m.get("no_ask", "?")
                log.info(f"    {ticker}: {title[:80]} | Yes:{yes_ask}c No:{no_ask}c")
        else:
            log.info("  No open music/album markets found")
            log.info("  (This is expected -- these markets appear around major album releases)")

        return markets
    except Exception as e:
        log.error("  Kalshi search failed: %s", e, exc_info=True)
        return []


def run_full_scan():
    """Run a complete scan: charts, articles, market matching."""
    log.info("=" * 70)
    log.info("HITS Daily Double Scraper -- Kalshi Info Arbitrage")
    log.info(f"   Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 70)

    # Verify Kalshi auth
    try:
        bal = client.get("/portfolio/balance")
        log.info(f"Kalshi auth OK! Balance: ${bal.get('balance', 0)/100:.2f}")
    except Exception as e:
        log.error(f"Kalshi auth failed: {e}")

    # Scan charts
    scan_charts()

    # Scan articles
    scan_articles()

    # Search Kalshi markets
    markets = scan_kalshi_markets()

    # If we have both chart data and markets, try matching
    if markets:
        log.info("\n[MATCHING] Connecting HDD data to Kalshi markets...")
        for chart_slug in ["hits-top-50", "midweek-20"]:
            try:
                chart = fetch_latest_chart(chart_slug)
                if chart:
                    entries = parse_chart_data(chart.get("chart_data", ""))
                    if entries:
                        match_chart_to_markets(entries, markets)
            except Exception:
                pass

    log.info("\n" + "=" * 70)
    log.info("Scan complete")
    log.info("=" * 70)


def monitor_loop(interval_minutes: int = 15):
    """Run continuous monitoring."""
    log.info(f"Starting HDD monitor (checking every {interval_minutes} min)...")

    while True:
        try:
            run_full_scan()
        except Exception as e:
            log.error("Scan error: %s", e, exc_info=True)

        log.info(f"\nNext scan in {interval_minutes} minutes...")
        time.sleep(interval_minutes * 60)


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse
    from kalshi_auth import _atomic_write_json
    parser = argparse.ArgumentParser(description="HDD Scraper for Kalshi Arbitrage")
    parser.add_argument("command", nargs="?", default="scan",
                       choices=["scan", "monitor", "charts", "articles", "markets"],
                       help="Command to run")
    parser.add_argument("--interval", type=int, default=15, help="Monitor interval (minutes)")
    args = parser.parse_args()

    if args.command == "scan":
        try:
            run_full_scan()
            _atomic_write_json(PROJECT_DIR / "data" / "hdd-last-run.json", {
                "timestamp": datetime.datetime.now().isoformat(),
                "status": "ok",
            })
        except Exception as e:
            _atomic_write_json(PROJECT_DIR / "data" / "hdd-last-run.json", {
                "timestamp": datetime.datetime.now().isoformat(),
                "status": "error",
                "error": str(e),
            })
            raise
    elif args.command == "monitor":
        monitor_loop(args.interval)
    elif args.command == "charts":
        scan_charts()
    elif args.command == "articles":
        scan_articles()
    elif args.command == "markets":
        scan_kalshi_markets()
