#!/usr/bin/env python3
"""BeatRelease.com Kalshi Copy-Trade Scanner — Self-scheduling daemon.

Checks BeatRelease blog for new/updated Kalshi prediction posts,
uses DeepSeek LLM to extract trade recommendations, and places demo trades.

Usage:
    python3 beatrelease-scanner.py          # Run as daemon (loop every 1h)
    python3 beatrelease-scanner.py --once   # Single scan, no loop
"""

import json, time, datetime, os, sys, re, signal, hashlib
import requests
from pathlib import Path
from bs4 import BeautifulSoup

from kalshi_auth import KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging, PROJECT_DIR, fetch_parallel, retry_request, TradeManager, trim_trade_log, notify_whatsapp, _atomic_write_json, HealthCheckMonitor, ScanSummary, load_trades, is_shutdown_requested
from capital_allocator import PortfolioAllocator
from singleton_lock import acquire_process_singleton

# Unbuffered output
setup_unbuffered()
log = setup_logging("beatrelease")

# === Config ===
DEEPSEEK_KEY_PATH = PROJECT_DIR / "config" / "keys" / "deepseek.txt"
STATE_PATH = PROJECT_DIR / "data" / "beatrelease-state.json"
TRADES_PATH = PROJECT_DIR / "data" / "beatrelease-trades.json"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
LLM_LOG_PATH = PROJECT_DIR / "data" / "beatrelease-llm-log.json"

BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
_bots_cfg = json.loads(BOTS_CONFIG_PATH.read_text())["beatrelease"]
CHECK_INTERVAL_HOURS = _bots_cfg["checkIntervalHours"]
MAX_TRADE_CENTS = _bots_cfg.get("maxTradeAmount", 15) * 100  # dollars → cents
BLOG_URLS = _bots_cfg["blogUrls"]

STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# === Kalshi Client ===
client = KalshiClient()
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE_CENTS / 100,
    "maxTradeAmountPct": _bots_cfg.get("maxTradeAmountPct"),
    "maxDailyTrades": _bots_cfg.get("maxDailyTrades", 10),
    "maxDailyLoss": _bots_cfg.get("maxDailyLoss", 25),
    "maxDailyLossPct": _bots_cfg.get("maxDailyLossPct"),
}, logger=log, bot_name="beatrelease")
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
trim_trade_log(TRADES_PATH)


# === State ===
def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            # Migrate old formats to seen_posts
            if "seen_posts" not in data:
                seen_posts = {}
                # Migrate from seen_urls list
                old_urls = data.get("seen_urls", [])
                if not old_urls and "posts_scanned" in data:
                    old_urls = list(set(p.get("url", "") for p in data["posts_scanned"] if p.get("url")))
                for url in old_urls:
                    if url:
                        seen_posts[url] = {"hash": "", "last_processed": data.get("last_check", "")}
                data["seen_posts"] = seen_posts
                # Remove old keys
                data.pop("seen_urls", None)
                data.pop("posts_scanned", None)
            return data
        except (json.JSONDecodeError, ValueError):
            pass
    return {"seen_posts": {}, "last_check": None}

def save_state(state):
    state["last_check"] = datetime.datetime.now().isoformat()
    _atomic_write_json(STATE_PATH, state)


def content_hash(text):
    """Compute a short hash of article text for change detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _append_llm_log(record):
    """Append a record to the LLM log file (capped at 500 entries)."""
    try:
        entries = json.loads(LLM_LOG_PATH.read_text()) if LLM_LOG_PATH.exists() else []
    except (json.JSONDecodeError, ValueError):
        entries = []
    entries.append(record)
    entries = entries[-500:]  # Keep last 500
    _atomic_write_json(LLM_LOG_PATH, entries)


# === Ticker Map ===
def build_ticker_map(kalshi_client):
    """Fetch open KXALBUMSALES markets and build artist-name-to-ticker mapping.

    Returns a dict like:
        {
            "Charli XCX / Wuthering Heights": ["KXALBUMSALES-WUT-15000", ...],
            "Megan Moroney / Cloud 9": ["KXALBUMSALES-CLO-60000", ...],
        }
    """
    ticker_map = {}
    try:
        markets = kalshi_client.get_all_markets(prefix="KXALBUMSALES", cache_ttl=300)
        for m in markets:
            ticker = m.get("ticker", "")
            title = m.get("title", "")
            subtitle = m.get("subtitle", "")
            # Build a descriptive key from title + subtitle
            # Kalshi titles are like "Will Charli XCX's Wuthering Heights sell 55,000+ copies?"
            # Subtitles often have the artist/album name
            key = subtitle.strip() if subtitle.strip() else title.strip()
            if not key or not ticker:
                continue
            if key not in ticker_map:
                ticker_map[key] = []
            ticker_map[key].append(ticker)
        log.info(f"  Ticker map: {len(ticker_map)} albums, {sum(len(v) for v in ticker_map.values())} tickers")
    except Exception as e:
        log.error(f"  Failed to build ticker map: {e}")
    return ticker_map


def format_ticker_map_for_prompt(ticker_map):
    """Format the ticker map as a string for the DeepSeek prompt."""
    if not ticker_map:
        return "No KXALBUMSALES markets currently open."
    lines = []
    for album, tickers in sorted(ticker_map.items()):
        lines.append(f"  {album}: {', '.join(sorted(tickers))}")
    return "\n".join(lines)


# === DeepSeek ===
def get_deepseek_key():
    key = DEEPSEEK_KEY_PATH.read_text().strip()
    if key == "PASTE_YOUR_DEEPSEEK_API_KEY_HERE" or not key:
        return None
    return key

def extract_trades_with_deepseek(post_text, post_url, ticker_map):
    """Send blog post to DeepSeek and extract trade recommendations.

    Args:
        post_text: Full article text.
        post_url: Source URL for context.
        ticker_map: Dict of album names to valid ticker lists from Kalshi API.
    """
    api_key = get_deepseek_key()
    if not api_key:
        log.warning("DeepSeek API key not configured — skipping LLM extraction")
        return []

    ticker_map_str = format_ticker_map_for_prompt(ticker_map)
    valid_tickers = set()
    for tickers in ticker_map.values():
        valid_tickers.update(tickers)

    prompt = f"""Analyze this BeatRelease.com blog post about Kalshi prediction markets.
Extract ALL specific trade recommendations mentioned, including both new entries AND exit/profit-taking signals.

VALID KALSHI TICKERS (you MUST only use tickers from this list — do NOT invent or guess tickers):
{ticker_map_str}

For each recommendation, return a JSON object with:
- "ticker": exact ticker from the list above (e.g., "KXALBUMSALES-WUT-55000")
- "action": "enter" for new positions, "exit" for profit-taking / position closures
- "direction": "YES" or "NO"
- "price_cents": the blog's recommended limit price in cents (1-99). Use the exact price mentioned in the article.
- "quantity": number of contracts (max cost ${MAX_TRADE_CENTS / 100:.0f} per trade)
- "reasoning": brief explanation of why this trade is recommended

Rules:
1. ONLY use tickers from the VALID KALSHI TICKERS list above. If the article mentions an album/artist not in the list, skip it.
2. Extract the blog's recommended entry price — do NOT substitute your own price estimate.
3. For exit signals (e.g., "take profits", "sell your position", "close out"), use action "exit".
4. For new trade entries (e.g., "buy YES", "we're buying NO"), use action "enter".
5. If no clear trades are mentioned, return [].

Return ONLY a JSON array of trade objects. No other text.

Blog post from {post_url}:
---
{post_text[:60000]}
"""

    try:
        start_time = time.time()
        r = retry_request("POST", DEEPSEEK_URL, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, json={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4000,
        }, timeout=90)
        elapsed = time.time() - start_time

        resp_json = r.json()
        content = resp_json["choices"][0]["message"]["content"].strip()
        usage = resp_json.get("usage", {})
        log.info(f"  DeepSeek: {usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')} tokens, "
                 f"{elapsed:.1f}s")

        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            trades = json.loads(json_match.group())
            # Validate
            valid = []
            rejected_count = 0
            for t in trades:
                if not isinstance(t, dict):
                    rejected_count += 1
                    continue
                ticker = t.get("ticker", "")
                direction = t.get("direction", "").upper()
                price = t.get("price_cents", 0)
                qty = t.get("quantity", 1)
                action = t.get("action", "enter").lower()

                # Reject tickers not in our valid set
                if ticker not in valid_tickers:
                    log.warning(f"  Rejecting invalid ticker from LLM: {ticker}")
                    rejected_count += 1
                    continue

                if direction not in ("YES", "NO"):
                    log.info(f"  LLM trade rejected: invalid direction '{direction}' for {ticker}")
                    rejected_count += 1
                    continue

                if not (1 <= price <= 99) or qty < 1:
                    log.info(f"  LLM trade rejected: invalid price={price} or qty={qty} for {ticker}")
                    rejected_count += 1
                    continue

                # Enforce max cost: price * qty <= MAX_TRADE_CENTS
                max_qty = MAX_TRADE_CENTS // price
                qty = min(qty, max_qty, 10)  # Also cap at 10 contracts
                if qty < 1:
                    rejected_count += 1
                    continue

                t["direction"] = direction
                t["price_cents"] = int(price)
                t["quantity"] = int(qty)
                t["action"] = action if action in ("enter", "exit") else "enter"
                valid.append(t)

            entries = [t for t in valid if t["action"] == "enter"]
            exits = [t for t in valid if t["action"] == "exit"]
            log.info(f"  DeepSeek: {len(entries)} entries + {len(exits)} exits ({rejected_count} rejected) from {len(trades)} raw")

            # Archive LLM response
            _append_llm_log({
                "timestamp": datetime.datetime.now().isoformat(),
                "post_url": post_url,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "latency_seconds": round(elapsed, 1),
                "raw_trade_count": len(trades),
                "valid_entries": len(entries),
                "valid_exits": len(exits),
                "rejected": rejected_count,
            })

            return valid
        else:
            log.info(f"  DeepSeek returned no parseable JSON: {content[:200]}")
            _append_llm_log({
                "timestamp": datetime.datetime.now().isoformat(),
                "post_url": post_url,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "latency_seconds": round(elapsed, 1),
                "raw_trade_count": 0,
                "valid_entries": 0,
                "valid_exits": 0,
                "rejected": 0,
                "error": "no_parseable_json",
            })
            return []

    except Exception as e:
        log.error(f"  DeepSeek error: {e}")
        return []


# === Blog Scraping ===
def fetch_blog_posts():
    """Fetch both BeatRelease URLs and return list of (url, title) tuples (parallel fetch)."""
    posts = []
    seen = set()

    blog_headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    responses = fetch_parallel(BLOG_URLS, headers=blog_headers, timeout=20)

    for blog_url in BLOG_URLS:
        r = responses.get(blog_url)
        if r is None or r.status_code != 200:
            log.error(f"  Error fetching {blog_url}: HTTP {r.status_code if r else 'no response'}")
            continue
        try:
            soup = BeautifulSoup(r.text, "html.parser")

            # Find blog post links — BeatRelease uses various patterns
            for a in soup.find_all("a", href=True):
                href = a["href"]
                # Normalize
                if href.startswith("/"):
                    href = "https://www.beatrelease.com" + href

                # Only blog posts
                if not any(seg in href for seg in ["/post/", "/blog/", "/article/", "/p/"]):
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
            log.error(f"  Error parsing {blog_url}: {e}")

    if not posts:
        log.warning("No blog post links found via HTML parsing — BeatRelease may have changed URL structure")
    return posts


# === Position Management (Exit Trades) ===
def execute_exits(exit_trades):
    """Execute exit/profit-taking signals by selling existing positions.

    Args:
        exit_trades: List of trade dicts with action=="exit" from DeepSeek.

    Returns:
        List of successfully placed exit orders.
    """
    if not exit_trades:
        return []

    # Fetch current positions
    try:
        positions_data = client.get("/portfolio/positions")
        positions = positions_data.get("market_positions", [])
    except Exception as e:
        log.error(f"  Failed to fetch positions for exits: {e}")
        return []

    # Build ticker -> position map
    pos_map = {}
    for p in positions:
        t = p.get("ticker", "")
        pos_val = p.get("position", 0)
        yes_qty = pos_val if pos_val > 0 else 0
        no_qty = abs(pos_val) if pos_val < 0 else 0
        if t and (yes_qty or no_qty):
            pos_map[t] = {"yes": yes_qty, "no": no_qty}

    placed = []
    for t in exit_trades:
        ticker = t["ticker"]
        if ticker not in pos_map:
            log.info(f"  Exit signal for {ticker} — no position held, skipping")
            continue

        pos = pos_map[ticker]
        side = "yes" if t["direction"].upper() == "YES" else "no"
        held = pos.get(side, 0)
        if held <= 0:
            log.info(f"  Exit signal for {side.upper()} {ticker} — no {side} position held, skipping")
            continue

        sell_qty = min(t["quantity"], held)
        sell_price = t["price_cents"]

        # Place sell order via TradeManager (enforces kill switch, circuit breaker, logging)
        reasoning = f"BeatRelease exit: {t.get('reasoning', 'signal reversed')}"
        result = trade_manager.sell_position(ticker, side, sell_price, sell_qty, reasoning)
        if result:
            log.info(f"  EXIT: Sell {sell_qty}x {side.upper()} {ticker} @ {sell_price}c (ID: {result.get('order_id')})")
            placed.append({
                "ticker": ticker,
                "side": side,
                "price": sell_price,
                "quantity": sell_qty,
                "action": "exit",
            })
            time.sleep(0.5)

    return placed


# === Notification ===
# notify_whatsapp imported from kalshi_auth (shared implementation)


# === Stale Order Cleanup ===
def cancel_stale_orders():
    """Cancel resting orders whose market has already closed.

    Copy-trading intentionally places orders in thin markets, so we don't
    cancel based on age. We only cancel when the market is past its close
    time (order can no longer fill and is just locking capital).
    """
    try:
        data = client.get("/portfolio/orders?status=resting")
        orders = data.get("orders", [])
        if not orders:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        canceled = 0
        for order in orders:
            close_time_str = order.get("expiration_time", "") or order.get("close_time", "")
            if not close_time_str:
                # No close time available — fall back to 72h age limit
                created = order.get("created_time", "")
                if not created:
                    continue
                try:
                    created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if (now - created_dt).total_seconds() / 3600 < 72:
                        continue
                except (ValueError, TypeError):
                    continue
            else:
                try:
                    close_dt = datetime.datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                    if now < close_dt:
                        continue  # Market still open, let the order ride
                except (ValueError, TypeError):
                    continue

            order_id = order.get("order_id", "")
            ticker = order.get("ticker", "?")
            if order_id:
                client.delete(f"/portfolio/orders/{order_id}")
                log.info(f"  Canceled expired order {order_id} on {ticker}")
                canceled += 1

        if canceled:
            log.info(f"  Canceled {canceled} expired resting orders")
    except Exception as e:
        log.error(f"  Stale order cleanup error: {e}")


# === Main Scan Cycle ===
def scan_cycle():
    """One full scan cycle."""
    ss = ScanSummary("beatrelease", log)
    log.info("=" * 60)
    log.info("BeatRelease scan starting...")

    # Cancel resting orders on markets that have already closed
    cancel_stale_orders()

    # Build ticker map from live Kalshi markets
    ticker_map = build_ticker_map(client)

    state = load_state()
    seen_posts = state.get("seen_posts", {})
    log.info(f"  {len(seen_posts)} previously seen posts")

    # 1. Fetch blog posts
    posts = fetch_blog_posts()
    if not posts:
        log.info("  No posts found (fetch error?)")
        ss.source_fail("beatrelease_blog", "no posts found")
        save_state(state)
        ss.finalize()
        return
    ss.source_ok("beatrelease_blog")

    # 2. Prefetch all post texts in parallel to check for new/updated content
    all_post_urls = [url for url, title in posts]
    blog_headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    post_responses = fetch_parallel(all_post_urls, headers=blog_headers, timeout=20)

    # 3. Determine which posts are new or updated (content hash changed)
    posts_to_process = []
    for url, title in posts:
        r = post_responses.get(url)
        if r is None or r.status_code != 200:
            continue

        try:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            content_el = soup.find("article") or soup.find("main") or soup.find(class_=re.compile(r"post|content|blog|article", re.I))
            text = content_el.get_text(separator="\n", strip=True) if content_el else soup.get_text(separator="\n", strip=True)
        except Exception as e:
            log.warning(f"  Error parsing post {url}: {e}")
            continue

        if not text:
            continue

        new_hash = content_hash(text)
        prev = seen_posts.get(url)

        if prev is None:
            # Brand new post
            posts_to_process.append((url, title, text, new_hash, "new"))
        elif prev.get("hash", "") != new_hash:
            # Content has changed since last scan
            posts_to_process.append((url, title, text, new_hash, "updated"))
        # else: unchanged, skip

    log.info(f"  {len(posts)} total posts, {len(posts_to_process)} to process (new/updated)")

    if not posts_to_process:
        log.info("  No new or updated posts — sleeping")
        save_state(state)
        ss.markets_fetched = len(posts)
        ss.finalize()
        return

    all_new_trades = []
    all_exit_trades = []
    notification_lines = [f"BeatRelease: {len(posts_to_process)} post(s) to process\n"]

    for url, title, text, new_hash, change_type in posts_to_process:
        log.info(f"\n{'Updated' if change_type == 'updated' else 'New'} post: {title}")
        log.info(f"   {url}")
        log.info(f"  Fetched {len(text)} chars (hash: {new_hash})")

        # Only process Kalshi-relevant posts
        text_lower = text.lower()
        if "kalshi" not in text_lower and "prediction market" not in text_lower:
            log.info("  Not Kalshi-related, skipping trade extraction")
            ss.skip("not_kalshi_related")
            trade_manager.log_decision("N/A", "skip", "skipped", "not_kalshi_related",
                                       post_title=title[:80], url=url)
            seen_posts[url] = {"hash": new_hash, "last_processed": datetime.datetime.now().isoformat()}
            continue

        # Extract trades via DeepSeek (with ticker map)
        trades = extract_trades_with_deepseek(text, url, ticker_map)

        if not trades:
            log.info("  No trades extracted")
            ss.skip("no_trades_extracted")
            trade_manager.log_decision("N/A", "skip", "skipped", "no_trades_extracted",
                                       post_title=title[:80], url=url)
            notification_lines.append(f"* {title} [{change_type}] — no trades extracted")
            seen_posts[url] = {"hash": new_hash, "last_processed": datetime.datetime.now().isoformat()}
            continue

        # Separate entries from exits
        entry_trades = [t for t in trades if t.get("action") == "enter"]
        exit_trades = [t for t in trades if t.get("action") == "exit"]

        # Execute exit signals first
        if exit_trades:
            exit_placed = execute_exits(exit_trades)
            all_exit_trades.extend(exit_placed)

        # Place entry trades
        # NOTE: No liquidity filter here — copy-trading intentionally places
        # limit orders in thin markets. The edge IS being early. Capital locked
        # in resting orders that never fill is returned at no loss.
        placed = []
        for t in entry_trades:
            side = "yes" if t["direction"].upper() == "YES" else "no"
            ticker = t["ticker"]

            # Validate ticker exists on Kalshi (with API v2 field normalization)
            market = client.get_market(ticker)
            if not market:
                log.warning(f"  Ticker {ticker} not found on Kalshi — skipping")
                ss.skip("ticker_not_found")
                trade_manager.log_decision(ticker, side, "skipped", "ticker_not_found",
                                           price_cents=t["price_cents"])
                continue

            # Use the blog's recommended price as the limit price
            limit_price = t["price_cents"]

            # Compute real edge from blog confidence vs market price
            yes_ask = market.get("yes_ask", 0)
            yes_bid = market.get("yes_bid", 0)
            no_ask = market.get("no_ask", 0) or (100 - yes_ask if yes_ask else 0)

            if side == "yes":
                actual_price = yes_ask if yes_ask and yes_ask > 0 else limit_price
                if actual_price > limit_price + 10:
                    log.info(f"  Skipping {ticker}: market ask {actual_price}c >> blog entry {limit_price}c")
                    ss.skip("stale_blog_price")
                    trade_manager.log_decision(ticker, side, "skipped", "stale_blog_price",
                                               price_cents=actual_price, blog_price=limit_price)
                    continue
                # Blog confidence: use LLM-extracted reasoning strength if available,
                # otherwise estimate from the edge between limit price and current market
                llm_confidence = t.get("confidence_pct")
                if llm_confidence and 0 < llm_confidence <= 100:
                    blog_confidence = min(0.95, llm_confidence / 100.0)
                else:
                    # Estimate confidence from how far the blog's limit price is from market
                    # A blog willing to pay 40c for a market at 30c implies 10c of conviction
                    price_edge = abs(limit_price - actual_price) / 100.0
                    blog_confidence = min(0.95, max(0.51, 0.50 + price_edge * 1.5))
                edge = blog_confidence - actual_price / 100.0
            else:  # no
                actual_price = no_ask if no_ask and no_ask > 0 else limit_price
                if actual_price > limit_price + 10:
                    log.info(f"  Skipping {ticker}: market no-ask {actual_price}c >> blog entry {limit_price}c")
                    ss.skip("stale_blog_price")
                    trade_manager.log_decision(ticker, side, "skipped", "stale_blog_price",
                                               price_cents=actual_price, blog_price=limit_price)
                    continue
                # Blog confidence: use LLM-extracted reasoning strength if available,
                # otherwise estimate from the edge between limit price and current market
                llm_confidence = t.get("confidence_pct")
                if llm_confidence and 0 < llm_confidence <= 100:
                    blog_confidence = min(0.95, llm_confidence / 100.0)
                else:
                    # Estimate confidence from how far the blog's limit price is from market
                    # A blog willing to pay 40c for a market at 30c implies 10c of conviction
                    price_edge = abs(limit_price - actual_price) / 100.0
                    blog_confidence = min(0.95, max(0.51, 0.50 + price_edge * 1.5))
                edge = blog_confidence - actual_price / 100.0

            # Discount LLM-stated confidence — blog posts are systematically overconfident
            LLM_CALIBRATION_DISCOUNT = 0.15
            calibrated_confidence = max(0.50, blog_confidence - LLM_CALIBRATION_DISCOUNT)
            edge = calibrated_confidence - actual_price / 100.0

            # Require calibrated confidence > 60% before trading
            if calibrated_confidence < 0.60:
                log.info(f"  Skipping {ticker}: calibrated confidence {calibrated_confidence*100:.0f}% < 60%")
                ss.skip("low_calibrated_confidence")
                trade_manager.log_decision(ticker, side, "skipped", "low_calibrated_confidence",
                                           raw_confidence=round(blog_confidence, 4),
                                           calibrated_confidence=round(calibrated_confidence, 4),
                                           price_cents=actual_price)
                continue

            if edge <= 0.02:
                log.info(f"  Skipping {ticker}: computed edge {edge*100:.1f}% too small")
                ss.skip("edge_too_small")
                trade_manager.log_decision(ticker, side, "skipped", "edge_too_small",
                                           edge=round(edge, 4), price_cents=actual_price)
                continue

            # Check allocator for global dedup (prevents cross-bot double exposure)
            budget = allocator.request_budget("beatrelease", ticker, edge=edge, confidence=blog_confidence)
            if not budget.approved:
                log.info(f"  Allocator denied {ticker}: {budget.reason}")
                ss.skip("allocator_denied")
                trade_manager.log_decision(ticker, side, "skipped", f"allocator denied: {budget.reason}",
                                           edge=round(edge, 4), price_cents=actual_price)
                continue

            # Apply Kelly-bounded sizing (don't blindly use LLM-recommended quantity)
            from probability import quarter_kelly, kalshi_fee_cents
            fee = kalshi_fee_cents(limit_price)
            kelly_count, kelly_risk, _ = quarter_kelly(
                edge, limit_price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee, return_details=True,
            )
            # Use the lesser of LLM-recommended and Kelly-bounded quantity
            if kelly_count <= 0:
                log.info(f"  Skipping {ticker}: Kelly sizing returned 0 (edge {edge*100:.1f}% insufficient at {limit_price}c)")
                ss.skip("kelly_zero")
                trade_manager.log_decision(ticker, side, "skipped", "kelly_zero",
                                           edge=round(edge, 4), price_cents=limit_price,
                                           kelly_count=kelly_count)
                continue
            bounded_qty = min(t["quantity"], kelly_count)

            result = trade_manager.place_order(
                ticker, side, limit_price, bounded_qty,
                t.get("reasoning", ""), source_url=url,
                sizing_method="llm_recommended",
                confidence=round(blog_confidence, 4),
                raw_edge=round(edge, 4),
            )
            if result:
                trade_manager.log_decision(
                    ticker, side, "placed", "llm_recommended",
                    edge=round(edge, 4), price_cents=limit_price,
                    confidence=round(blog_confidence, 4), source_url=url,
                )
                allocator.record_trade("beatrelease", ticker,
                                       limit_price * t["quantity"], edge=edge)
                placed.append({
                    "ticker": ticker,
                    "side": side,
                    "price": limit_price,
                    "quantity": t["quantity"],
                })
            time.sleep(0.5)  # Rate limit

        all_new_trades.extend(placed)
        notification_lines.append(f"* {title} [{change_type}] — {len(placed)}/{len(entry_trades)} entries, {len(exit_trades)} exits")
        for p in placed:
            notification_lines.append(f"  ENTER {p['side'].upper()} {p['ticker']} @ {p['price']}c x{p.get('quantity', '?')}")

        seen_posts[url] = {"hash": new_hash, "last_processed": datetime.datetime.now().isoformat()}

    # 4. Update state (trade saving handled by TradeManager)
    state["seen_posts"] = seen_posts
    save_state(state)

    # 5. Notify
    if all_new_trades or all_exit_trades:
        msg = "\n".join(notification_lines)
        log.info(f"\nNotification:\n{msg}")
        notify_whatsapp(msg, logger=log)

    ss.markets_fetched = len(posts)
    ss.markets_evaluated = len(posts_to_process)
    ss.trades_placed = len(all_new_trades) + len(all_exit_trades)
    ss.finalize()
    log.info(f"\nScan complete — {len(posts_to_process)} posts processed, {len(all_new_trades)} entries + {len(all_exit_trades)} exits placed")


# === Daemon ===
def run_daemon():
    setup_signal_handlers()

    log.info(f"BeatRelease scanner daemon started (PID {os.getpid()})")
    log.info(f"   Check interval: {CHECK_INTERVAL_HOURS}h")
    log.info(f"   State: {STATE_PATH}")
    log.info(f"   Trades: {TRADES_PATH}")

    while True:
        try:
            health.record_bot_heartbeat("beatrelease")
            scan_cycle()
        except Exception as e:
            log.error("Scan cycle error: %s", e, exc_info=True)

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break
        log.info(f"\nSleeping {CHECK_INTERVAL_HOURS}h until next check...")
        time.sleep(CHECK_INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    if not acquire_process_singleton("beatrelease", PROJECT_DIR, log):
        log.warning("Duplicate beatrelease launch blocked; exiting.")
        sys.exit(0)

    if "--once" in sys.argv:
        log.info("Running single scan (--once mode)")
        try:
            scan_cycle()
        except Exception as e:
            log.error("Error: %s", e, exc_info=True)
            sys.exit(1)
    else:
        run_daemon()
