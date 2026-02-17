#!/usr/bin/env python3
"""BeatRelease.com Kalshi Copy-Trade Scanner — Self-scheduling daemon.

Checks BeatRelease blog every 4 hours for new Kalshi prediction posts,
uses DeepSeek LLM to extract trade recommendations, and places demo trades.

Usage:
    python3 beatrelease-scanner.py          # Run as daemon (loop every 4h)
    python3 beatrelease-scanner.py --once   # Single scan, no loop
"""

import json, time, datetime, os, sys, re, signal, atexit
import requests
from pathlib import Path
from bs4 import BeautifulSoup

from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR

# Unbuffered output
setup_unbuffered()
log = setup_logging("beatrelease")

# === Config ===
DEEPSEEK_KEY_PATH = PROJECT_DIR / "config" / "keys" / "deepseek.txt"
STATE_PATH = PROJECT_DIR / "data" / "beatrelease-state.json"
TRADES_PATH = PROJECT_DIR / "data" / "beatrelease-trades.json"
PID_FILE = PROJECT_DIR / "data" / "pids" / "beatrelease-scanner.pid"
PID_FILE.parent.mkdir(parents=True, exist_ok=True)

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
_bots_cfg = json.loads(BOTS_CONFIG_PATH.read_text())["beatrelease"]
CHECK_INTERVAL_HOURS = _bots_cfg["checkIntervalHours"]
MAX_TRADE_CENTS = _bots_cfg["maxTradeCents"]
BLOG_URLS = _bots_cfg["blogUrls"]

STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# === Kalshi Client ===
client = KalshiClient()


# === PID Management ===
def write_pid():
    PID_FILE.write_text(str(os.getpid()))

def remove_pid():
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

def check_existing():
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if running
            log.info(f"Another instance running (PID {pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # Stale PID file


# === State ===
def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            # Migrate old format: extract seen_urls from posts_scanned
            if "seen_urls" not in data and "posts_scanned" in data:
                data["seen_urls"] = list(set(p.get("url", "") for p in data["posts_scanned"] if p.get("url")))
            return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {"seen_urls": [], "last_check": None}

def save_state(state):
    state["last_check"] = datetime.datetime.now().isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2))


# === DeepSeek ===
def get_deepseek_key():
    key = DEEPSEEK_KEY_PATH.read_text().strip()
    if key == "PASTE_YOUR_DEEPSEEK_API_KEY_HERE" or not key:
        return None
    return key

def extract_trades_with_deepseek(post_text, post_url):
    """Send blog post to DeepSeek and extract trade recommendations."""
    api_key = get_deepseek_key()
    if not api_key:
        log.warning("DeepSeek API key not configured — skipping LLM extraction")
        return []

    prompt = f"""Analyze this BeatRelease.com blog post about Kalshi prediction markets.
Extract ALL specific trade recommendations mentioned.

For each trade, return a JSON object with:
- "ticker": the Kalshi market ticker (e.g., "KXALBUMSALES-WUT-55000") or best guess from context
- "direction": "YES" or "NO"
- "price_cents": recommended entry price in cents (1-99)
- "quantity": number of contracts (keep total cost under $5 per trade)
- "reasoning": brief explanation

Return a JSON array of trade objects. If no clear trades, return [].
Only return the JSON array, no other text.

Blog post from {post_url}:
---
{post_text[:8000]}
"""

    try:
        r = requests.post(DEEPSEEK_URL, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2000,
        }, timeout=60)
        r.raise_for_status()

        content = r.json()["choices"][0]["message"]["content"].strip()
        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            trades = json.loads(json_match.group())
            # Validate
            valid = []
            for t in trades:
                if not isinstance(t, dict):
                    continue
                ticker = t.get("ticker", "")
                direction = t.get("direction", "").upper()
                price = t.get("price_cents", 0)
                qty = t.get("quantity", 1)
                if ticker and direction in ("YES", "NO") and 1 <= price <= 99 and qty >= 1:
                    # Enforce $5 max: price * qty <= 500 cents
                    max_qty = MAX_TRADE_CENTS // price
                    qty = min(qty, max_qty, 10)  # Also cap at 10 contracts
                    if qty < 1:
                        continue
                    t["direction"] = direction
                    t["price_cents"] = int(price)
                    t["quantity"] = int(qty)
                    valid.append(t)
            log.info(f"  DeepSeek extracted {len(valid)} valid trades from {len(trades)} raw")
            return valid
        else:
            log.info(f"  DeepSeek returned no parseable JSON: {content[:200]}")
            return []

    except Exception as e:
        log.error(f"  DeepSeek error: {e}")
        return []


# === Blog Scraping ===
def fetch_blog_posts():
    """Fetch both BeatRelease URLs and return list of (url, title) tuples."""
    posts = []
    seen = set()

    for blog_url in BLOG_URLS:
        try:
            r = requests.get(blog_url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            # Find blog post links — BeatRelease uses various patterns
            for a in soup.find_all("a", href=True):
                href = a["href"]
                # Normalize
                if href.startswith("/"):
                    href = "https://www.beatrelease.com" + href

                # Only blog posts
                if "/post/" not in href and "/blog/" not in href:
                    continue
                # Skip category/tag pages
                if "/categories/" in href or "/tags/" in href:
                    continue
                # Must be a beatrelease.com URL
                if "beatrelease.com" not in href:
                    continue

                if href in seen:
                    continue
                seen.add(href)

                title = a.get_text(strip=True) or href.split("/")[-1]
                posts.append((href, title))

            log.info(f"  Fetched {blog_url} — found {len(seen)} unique posts so far")

        except Exception as e:
            log.error(f"  Error fetching {blog_url}: {e}")

    return posts


def fetch_post_text(url):
    """Fetch full text of a blog post."""
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Remove scripts, styles, nav
        for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
            tag.decompose()

        # Try to find main content
        content = soup.find("article") or soup.find("main") or soup.find(class_=re.compile(r"post|content|blog|article", re.I))
        if content:
            return content.get_text(separator="\n", strip=True)
        return soup.get_text(separator="\n", strip=True)

    except Exception as e:
        log.error(f"  Error fetching post {url}: {e}")
        return ""


# === Trade Placement ===
def place_trade(ticker, direction, price_cents, quantity, reasoning=""):
    """Place a limit order on Kalshi demo."""
    side = "yes" if direction.upper() == "YES" else "no"
    price_key = "yes_price" if side == "yes" else "no_price"

    body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": quantity,
        price_key: price_cents,
    }

    try:
        result = client.post("/portfolio/orders", body=body)
        order = result.get("order", {})
        log.info(f"  Order {order.get('order_id','?')}: {quantity}x {direction} {ticker} @ {price_cents}c — {order.get('status','?')}")
        return {
            "timestamp": datetime.datetime.now().isoformat(),
            "source_url": "",
            "ticker": ticker,
            "side": side,
            "price": price_cents,
            "quantity": quantity,
            "reasoning": reasoning,
            "order_id": order.get("order_id"),
            "status": order.get("status"),
        }
    except requests.exceptions.HTTPError as e:
        log.error(f"  Order failed {ticker}: {e.response.status_code} {e.response.text[:200]}")
        return None
    except Exception as e:
        log.error(f"  Order failed {ticker}: {e}")
        return None


# === Notification ===
def notify_whatsapp(message):
    """Try to send WhatsApp notification via openclaw CLI."""
    try:
        import subprocess
        # Write message to temp file to handle special chars
        tmp = PROJECT_DIR / "data" / "beatrelease-msg.txt"
        tmp.write_text(message)
        result = subprocess.run(
            ["openclaw", "message", "send", "--to", "+14255336828", "--message", message, "--channel", "whatsapp"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log.info("  WhatsApp notification sent")
        else:
            log.warning(f"  WhatsApp send failed: {result.stderr[:200]}")
    except FileNotFoundError:
        log.warning("  openclaw CLI not found — notification logged only")
    except Exception as e:
        log.warning(f"  WhatsApp error: {e}")


# === Main Scan Cycle ===
def scan_cycle():
    """One full scan cycle."""
    log.info("=" * 60)
    log.info("BeatRelease scan starting...")

    state = load_state()
    seen_urls = set(state.get("seen_urls", []))
    log.info(f"  {len(seen_urls)} previously seen URLs")

    # 1. Fetch blog posts
    posts = fetch_blog_posts()
    if not posts:
        log.info("  No posts found (fetch error?)")
        save_state(state)
        return

    # 2. Find new posts
    new_posts = [(url, title) for url, title in posts if url not in seen_urls]
    log.info(f"  {len(posts)} total posts, {len(new_posts)} new")

    if not new_posts:
        log.info("  No new posts — sleeping")
        save_state(state)
        return

    # 3. Process each new post
    all_new_trades = []
    notification_lines = [f"BeatRelease: {len(new_posts)} new post(s) found\n"]

    for url, title in new_posts:
        log.info(f"\nNew post: {title}")
        log.info(f"   {url}")

        # Fetch full text
        text = fetch_post_text(url)
        if not text:
            log.warning("  Could not fetch post text")
            seen_urls.add(url)
            continue

        log.info(f"  Fetched {len(text)} chars")

        # Only process Kalshi-relevant posts
        text_lower = text.lower()
        if "kalshi" not in text_lower and "prediction market" not in text_lower:
            log.info("  Not Kalshi-related, skipping trade extraction")
            seen_urls.add(url)
            continue

        # Extract trades via DeepSeek
        trades = extract_trades_with_deepseek(text, url)

        if not trades:
            log.info("  No trades extracted")
            notification_lines.append(f"* {title} — no trades extracted")
            seen_urls.add(url)
            continue

        # Place trades
        placed = []
        for t in trades:
            result = place_trade(
                t["ticker"], t["direction"], t["price_cents"], t["quantity"],
                t.get("reasoning", "")
            )
            if result:
                result["source_url"] = url
                placed.append(result)
            time.sleep(0.5)  # Rate limit

        all_new_trades.extend(placed)
        notification_lines.append(f"* {title} — {len(placed)}/{len(trades)} trades placed")
        for p in placed:
            notification_lines.append(f"  {p['side'].upper()} {p['ticker']} @ {p['price']}c x{p['quantity']}")

        seen_urls.add(url)

    # 4. Save trades
    if all_new_trades:
        existing = []
        if TRADES_PATH.exists():
            try:
                existing = json.loads(TRADES_PATH.read_text())
            except (json.JSONDecodeError, ValueError):
                pass
        existing.extend(all_new_trades)
        TRADES_PATH.write_text(json.dumps(existing, indent=2))
        log.info(f"\n{len(all_new_trades)} trades saved ({len(existing)} total)")

    # 5. Update state
    state["seen_urls"] = list(seen_urls)
    save_state(state)

    # 6. Notify
    if all_new_trades:
        msg = "\n".join(notification_lines)
        log.info(f"\nNotification:\n{msg}")
        notify_whatsapp(msg)

    log.info(f"\nScan complete — {len(new_posts)} new posts, {len(all_new_trades)} trades placed")


# === Daemon ===
def run_daemon():
    check_existing()
    write_pid()
    atexit.register(remove_pid)
    setup_signal_handlers()

    log.info(f"BeatRelease scanner daemon started (PID {os.getpid()})")
    log.info(f"   Check interval: {CHECK_INTERVAL_HOURS}h")
    log.info(f"   State: {STATE_PATH}")
    log.info(f"   Trades: {TRADES_PATH}")

    while True:
        try:
            scan_cycle()
        except Exception as e:
            log.error(f"Scan cycle error: {e}")
            import traceback
            traceback.print_exc()

        log.info(f"\nSleeping {CHECK_INTERVAL_HOURS}h until next check...")
        time.sleep(CHECK_INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    if "--once" in sys.argv:
        log.info("Running single scan (--once mode)")
        try:
            scan_cycle()
        except Exception as e:
            log.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        run_daemon()
