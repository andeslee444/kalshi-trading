#!/usr/bin/env python3
"""Kalshi Economics Bot — Trades CPI, GDP, and Jobs markets using nowcast data.

Data sources (all public HTTP, no auth):
  1. Cleveland Fed CPI Nowcast — daily point estimate + distribution
  2. AAA Gasoline Prices — daily national average (~8% CPI weight)
  3. BLS Release Schedule — exact release dates

Usage:
    python3 src/kalshi/economics-bot.py          # daemon mode
    python3 src/kalshi/economics-bot.py --once    # single scan
"""

import json, time, datetime, os, sys, re, argparse, traceback, math
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, retry_request, TradeManager, trim_trade_log, build_market_snapshot,
    HealthCheckMonitor, OrderMonitor, ScanSummary,
    is_shutdown_requested,
)
from probability import (
    econ_nowcast_probability, cpi_nowcast_sigma, gdp_nowcast_sigma, quarter_kelly,
    uncertainty_kelly, compute_limit_price, kalshi_fee_cents, gas_price_probability,
)
from capital_allocator import PortfolioAllocator
from cpi_belief_filter import CPIBeliefFilter
from scenario_engine import compute_scenario_weights, scenario_probability
try:
    from macro_engine import MacroEngine
except ImportError:
    MacroEngine = None

setup_unbuffered()
log = setup_logging("economics")
setup_signal_handlers()

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-economics-trades.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load config
bots_config = json.loads(BOTS_CONFIG_PATH.read_text())
econ_config = bots_config.get("economics", {})

MAX_TRADE = econ_config.get("maxTradeAmount", 15)
MAX_DAILY_TRADES = econ_config.get("maxDailyTrades", 5)
MAX_DAILY_LOSS = econ_config.get("maxDailyLoss", 30)
SCAN_INTERVAL = econ_config.get("scanIntervalMinutes", 360)
EDGE_THRESHOLD = econ_config.get("edgeThreshold", 0.08)
GAS_EDGE_THRESHOLD = econ_config.get("gasEdgeThreshold", 0.04)

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
macro = MacroEngine(config=econ_config.get("macro", {})) if MacroEngine else None
order_monitor = OrderMonitor(client, log=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": MAX_TRADE,
    "maxTradeAmountPct": econ_config.get("maxTradeAmountPct"),
    "maxDailyTrades": MAX_DAILY_TRADES,
    "maxDailyLoss": MAX_DAILY_LOSS,
    "maxDailyLossPct": econ_config.get("maxDailyLossPct"),
}, logger=log, order_monitor=order_monitor, bot_name="economics")
trim_trade_log(TRADES_PATH)

def _classify_econ_market(ticker):
    """Classify economics market type from ticker."""
    t = ticker.upper()
    if "CPI" in t or "INFLATION" in t:
        return "CPI"
    elif "GDP" in t:
        return "GDP"
    elif "JOBS" in t or "NFP" in t or "EMPLOYMENT" in t:
        return "JOBS"
    elif "GAS" in t:
        return "GAS"
    elif "FED" in t or "FOMC" in t:
        return "FED"
    return "other"

# === Concentration Limits ===
FAMILY_EXPOSURE_PCT = 0.15   # 15% of bankroll per ticker family
TOTAL_ECON_PCT = 0.40        # 40% total economics exposure

def _ticker_family(ticker):
    """Extract ticker family (everything before -T/-B threshold suffix)."""
    m = re.match(r'^(.*?)-[TB][\d.]+$', ticker)
    return m.group(1) if m else ticker

def _compute_exposure(trades, match_value, match_mode="family"):
    """Sum cost_cents across trades matching a ticker family or prefix."""
    total = 0
    for t in trades:
        t_ticker = t.get("ticker", "")
        if match_mode == "family":
            if _ticker_family(t_ticker) == match_value:
                total += t.get("cost_cents", 0)
        elif match_mode == "prefix":
            if t_ticker.startswith(match_value):
                total += t.get("cost_cents", 0)
    return total

def _check_concentration(ticker, bankroll_cents, trades):
    """Check concentration limits. Returns (allowed, reason) tuple."""
    family = _ticker_family(ticker)

    # Level 2: Per-ticker-family (15%)
    family_cap = int(bankroll_cents * FAMILY_EXPOSURE_PCT)
    family_exposure = _compute_exposure(trades, family, "family")
    if family_exposure >= family_cap:
        return False, f"family_cap: ${family_exposure/100:.0f} >= ${family_cap/100:.0f} (15%)"

    # Level 3: Total econ exposure (40%)
    total_cap = int(bankroll_cents * TOTAL_ECON_PCT)
    total_exposure = _compute_exposure(trades, "KXECON", "prefix")
    total_exposure += _compute_exposure(trades, "KXCPI", "prefix")
    total_exposure += _compute_exposure(trades, "KXGDP", "prefix")
    total_exposure += _compute_exposure(trades, "KXJOBS", "prefix")
    total_exposure += _compute_exposure(trades, "KXGAS", "prefix")
    total_exposure += _compute_exposure(trades, "KXINFLATION", "prefix")
    if total_exposure >= total_cap:
        return False, f"total_econ_cap: ${total_exposure/100:.0f} >= ${total_cap/100:.0f} (40%)"

    return True, ""

class EdgeScaler:
    """Auto-scale exposure limits based on settlement track record.

    Starts conservative (20% of bankroll), scales up as settlements prove
    the model works. Halves on losing streaks.
    """
    TIERS = [
        {"min_wins": 0,  "max_exposure_pct": 0.20},
        {"min_wins": 5,  "max_exposure_pct": 0.40},
        {"min_wins": 10, "max_exposure_pct": 0.60},
        {"min_wins": 20, "max_exposure_pct": 1.00},
    ]

    def current_limit(self, settlement_record):
        """Compute current max exposure as fraction of bankroll.

        Args:
            settlement_record: List of dicts with 'profitable' boolean key.

        Returns:
            Float in (0, 1] -- max fraction of bankroll to deploy.
        """
        wins = sum(1 for s in settlement_record if s.get("profitable"))
        recent = settlement_record[-10:] if settlement_record else []
        loss_streak = 0
        for s in reversed(recent):
            if not s.get("profitable"):
                loss_streak += 1
            else:
                break
        loss_penalty = 0.5 if loss_streak >= 3 else 1.0

        tier = self.TIERS[0]
        for t in self.TIERS:
            if wins >= t["min_wins"]:
                tier = t

        return tier["max_exposure_pct"] * loss_penalty

# === Market ticker prefixes ===
# Note: KXFED removed — CME FedWatch is a JavaScript SPA, HTML scraper returns garbage.
# Re-enable when a proper FedWatch data source (JSON API or FRED SOFR futures) is wired up.
# KXJOBS kept — will skip gracefully when no nowcast source is connected.
ECON_PREFIXES = ["KXCPI", "KXGDP", "KXJOBS", "KXINFLATION", "KXECON", "KXGAS"]

# === Nowcast cache ===
NOWCAST_CACHE_PATH = PROJECT_DIR / "data" / "econ-nowcast-cache.json"
NOWCAST_CACHE_TTL = 24 * 3600  # 24 hours


def _load_nowcast_cache():
    """Load cached nowcast values if fresh (within TTL)."""
    if not NOWCAST_CACHE_PATH.exists():
        return None
    try:
        cache = json.loads(NOWCAST_CACHE_PATH.read_text())
        ts = cache.get("cached_at", 0)
        if time.time() - ts < NOWCAST_CACHE_TTL:
            return cache.get("data", {})
    except Exception:
        pass
    return None


def _save_nowcast_cache(data):
    """Save nowcast values to cache with timestamp."""
    from kalshi_auth import _atomic_write_json
    _atomic_write_json(NOWCAST_CACHE_PATH, {"cached_at": time.time(), "data": data})


def _nowcast_cache_age_hours():
    """Return age of nowcast cache in hours, or float('inf') if missing."""
    try:
        if NOWCAST_CACHE_PATH.exists():
            cache = json.loads(NOWCAST_CACHE_PATH.read_text())
            return (time.time() - cache.get("cached_at", 0)) / 3600
    except Exception:
        pass
    return float("inf")


def _compute_cross_measure_dispersion(nowcast):
    """Compute cross-measure dispersion from available Fed YoY measures.

    Uses the spread across CPI/Core CPI/PCE/Core PCE as a proxy for model
    uncertainty. When 3+ measures are available, their standard deviation
    provides a dynamic sigma floor that auto-calibrates.

    Args:
        nowcast: Dict with keys like cpi_yoy, core_cpi_yoy, pce_yoy, core_pce_yoy.
            Modified in-place to add "cross_measure_dispersion" key.

    Returns:
        CI width (dispersion * 1.645) if 2+ measures available, else None.
    """
    measure_keys = ["cpi_yoy", "core_cpi_yoy", "pce_yoy", "core_pce_yoy"]
    values = [nowcast[k] for k in measure_keys if k in nowcast and nowcast[k] is not None]

    if len(values) < 2:
        return None

    if len(values) >= 3:
        # Standard deviation as dispersion
        mean_val = sum(values) / len(values)
        variance = sum((v - mean_val) ** 2 for v in values) / len(values)
        dispersion = math.sqrt(variance)
    else:
        # 2 values: use abs(diff)/2 as rough dispersion
        dispersion = abs(values[0] - values[1]) / 2

    ci_width = dispersion * 1.645  # 1-sigma to 90% CI approximation
    nowcast["cross_measure_dispersion"] = ci_width
    return ci_width


# === Data Sources ===

def _parse_nowcast_bs4(html):
    """Parse Cleveland Fed nowcast page using BeautifulSoup.

    The page has tables identified by <caption> text. Column headers (CPI,
    Core CPI, PCE, Core PCE) are in <th> elements; values are bare decimals
    in <td> cells (no % sign). We target the "year-over-year" table.
    """
    soup = BeautifulSoup(html, "html.parser")
    nowcast = {}

    # Strategy 1: find the year-over-year table by caption text
    for table in soup.find_all("table"):
        caption = table.find("caption")
        if not caption:
            continue
        caption_text = caption.get_text(" ", strip=True).lower()
        if "year" not in caption_text:
            continue

        # Map column headers to indices
        header_row = table.find("thead")
        if not header_row:
            continue
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
        col_map = {}
        for i, h in enumerate(headers):
            if h == "core cpi":
                col_map["core_cpi_yoy"] = i
            elif h == "cpi":
                col_map["cpi_yoy"] = i
            elif h == "core pce":
                col_map["core_pce_yoy"] = i
            elif h == "pce":
                col_map["pce_yoy"] = i

        if not col_map:
            continue

        # Read the first data row (most recent nowcast)
        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
        for row in rows:
            cells = row.find_all("td")
            for key, idx in col_map.items():
                if idx < len(cells):
                    cell_text = cells[idx].get_text(strip=True)
                    m = re.match(r'^(\d+\.?\d*)$', cell_text)
                    if m:
                        val = float(m.group(1))
                        if 0.0 < val < 20.0 and key not in nowcast:
                            nowcast[key] = val
            if nowcast:
                break  # first row with data is enough

    if nowcast:
        return nowcast, "yoy_table"

    # Strategy 2: broader table search — look for rows with CPI/PCE labels and values
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            row_text = row.get_text(" ", strip=True)
            for cell in cells:
                cell_text = cell.get_text(strip=True)
                m = re.match(r'^(\d+\.?\d*)\s*%?$', cell_text)
                if not m:
                    continue
                val = float(m.group(1))
                if not (0.0 < val < 20.0):
                    continue
                row_lower = row_text.lower()
                if "core cpi" in row_lower and "core_cpi_yoy" not in nowcast:
                    nowcast["core_cpi_yoy"] = val
                elif "cpi" in row_lower and "core" not in row_lower and "cpi_yoy" not in nowcast:
                    nowcast["cpi_yoy"] = val
                elif "pce" in row_lower and "pce_yoy" not in nowcast:
                    nowcast["pce_yoy"] = val

    if nowcast:
        return nowcast, "table_row_labels"

    # Strategy 3: look for labeled spans/divs with percentages
    text_blocks = soup.find_all(["span", "div", "p", "strong"])
    for el in text_blocks:
        txt = el.get_text(" ", strip=True)
        if len(txt) > 200:
            continue
        lower = txt.lower()
        pct_match = re.search(r'(\d+\.?\d*)\s*%', txt)
        if not pct_match:
            continue
        val = float(pct_match.group(1))
        if not (0.0 < val < 20.0):
            continue
        if "core cpi" in lower and "core_cpi_yoy" not in nowcast:
            nowcast["core_cpi_yoy"] = val
        elif "cpi" in lower and "core" not in lower and "cpi_yoy" not in nowcast:
            nowcast["cpi_yoy"] = val
        elif "pce" in lower and "pce_yoy" not in nowcast:
            nowcast["pce_yoy"] = val

    if nowcast:
        return nowcast, "span_text"

    return nowcast, None


def _parse_nowcast_regex(html):
    """Fallback regex parser for Cleveland Fed nowcast page.

    The table structure has headers (CPI, Core CPI, PCE, Core PCE) in <th>
    and values as bare decimals in <td> cells. Match the year-over-year
    table by its caption and extract values from the first data row.
    """
    nowcast = {}

    # Strategy 1: match the year-over-year table structure
    # Caption identifies the table, then first <tr> in <tbody> has the data
    yoy_match = re.search(
        r'year-over-year.*?<tbody>(.*?)</tbody>',
        html, re.IGNORECASE | re.DOTALL
    )
    if yoy_match:
        first_row = re.search(r'<tr>(.*?)</tr>', yoy_match.group(1), re.DOTALL)
        if first_row:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', first_row.group(1), re.DOTALL)
            # Expected columns: Month, CPI, Core CPI, PCE, Core PCE, Updated
            keys = [None, "cpi_yoy", "core_cpi_yoy", "pce_yoy", "core_pce_yoy"]
            for i, key in enumerate(keys):
                if key and i < len(cells):
                    val_match = re.match(r'^\s*(\d+\.?\d*)\s*$', cells[i].strip())
                    if val_match:
                        val = float(val_match.group(1))
                        if 0.0 < val < 20.0:
                            nowcast[key] = val

    if nowcast:
        return nowcast, "regex_yoy_tbody"

    # Strategy 2: broader patterns (CPI label near percentage value)
    cpi_pattern = r'(?:CPI|Consumer Price Index)[^<]{0,200}?(\d+\.?\d*)\s*%'
    cpi_matches = re.findall(cpi_pattern, html, re.IGNORECASE)
    if cpi_matches:
        try:
            nowcast["cpi_yoy"] = float(cpi_matches[0])
        except (ValueError, IndexError):
            pass

    core_pattern = r'(?:Core CPI|core.*?CPI)[^<]{0,200}?(\d+\.?\d*)\s*%'
    core_matches = re.findall(core_pattern, html, re.IGNORECASE)
    if core_matches:
        try:
            nowcast["core_cpi_yoy"] = float(core_matches[0])
        except (ValueError, IndexError):
            pass

    pce_pattern = r'(?:PCE)[^<]{0,200}?(\d+\.?\d*)\s*%'
    pce_matches = re.findall(pce_pattern, html, re.IGNORECASE)
    if pce_matches:
        try:
            nowcast["pce_yoy"] = float(pce_matches[0])
        except (ValueError, IndexError):
            pass

    return nowcast, "regex_patterns" if nowcast else None


def fetch_cleveland_fed_nowcast():
    """Fetch Cleveland Fed inflation nowcast.

    Uses layered parsing: BS4 structured parsing -> regex fallback -> cache fallback.
    Returns dict with 'cpi_yoy', 'core_cpi_yoy', 'pce_yoy' or empty dict.
    """
    try:
        url = "https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        r = retry_request("GET", url, headers=headers, timeout=20)
        html = r.text

        # Primary: BeautifulSoup structured parsing
        nowcast, strategy = _parse_nowcast_bs4(html)

        # Fallback: regex parsing
        if not nowcast:
            log.warning("  Cleveland Fed: BS4 parser found no data, falling back to regex")
            nowcast, strategy = _parse_nowcast_regex(html)

        if nowcast and strategy:
            log.info(f"  Cleveland Fed parsed via: {strategy}")
        elif not nowcast:
            log.warning("  Cleveland Fed: regex parser also found no data (page structure may have changed)")
            log.info(f"  Cleveland Fed page length: {len(html)} chars, tables found: {html.count('<table')}")

        if nowcast:
            for key, val in nowcast.items():
                log.info(f"  Cleveland Fed {key}: {val}%")
            _save_nowcast_cache(nowcast)
            health.record_source_success("cleveland-fed")
            return nowcast

        # No values found from live page — try cache
        log.warning("  Cleveland Fed: no values parsed from live page, trying cache")
        cached = _load_nowcast_cache()
        if cached:
            cache_age = _nowcast_cache_age_hours()
            log.info(f"  Using cached nowcast (age: {cache_age:.1f}h): {cached}")
            if cache_age > 24:
                log.error(f"  Nowcast cache is {cache_age:.0f}h stale — will skip trading on stale data")
                cached["_stale"] = True
            return cached

        health.record_source_error("cleveland-fed", "no values parsed")
        return {}

    except Exception as e:
        log.error(f"  Cleveland Fed fetch failed: {e}")
        # Try cache on HTTP/network failure
        cached = _load_nowcast_cache()
        if cached:
            cache_age = _nowcast_cache_age_hours()
            log.info(f"  Using cached nowcast after error (age: {cache_age:.1f}h): {cached}")
            if cache_age > 24:
                log.error(f"  Nowcast cache is {cache_age:.0f}h stale — will skip trading on stale data")
                cached["_stale"] = True
            return cached
        health.record_source_error("cleveland-fed", str(e))
        return {}


def fetch_gas_prices():
    """Fetch current national average gas price from AAA.

    Returns price in dollars (e.g. 3.45) or None on failure.
    """
    try:
        url = "https://gasprices.aaa.com/"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        r = retry_request("GET", url, headers=headers, timeout=20)
        html = r.text

        # Look for national average gas price
        price_pattern = r'\$(\d+\.\d{2,3})'
        prices = re.findall(price_pattern, html)
        if prices:
            price = float(prices[0])
            if 1.0 < price < 10.0:  # sanity check
                log.info(f"  AAA gas price: ${price}")
                return price

        return None
    except Exception as e:
        log.error(f"  AAA gas price fetch failed: {e}")
        return None


# === Market Matching ===

def parse_econ_threshold(market):
    """Extract threshold value from an economics market title/ticker.

    Examples:
      "Will CPI YoY be above 3.0%?" -> 3.0
      "KXCPI-JAN-T3.0" -> 3.0
    """
    title = market.get("title", "")
    ticker = market.get("ticker", "")

    # Try ticker first: KXCPI-...-T3.0 or KXCPI-...-B3.0
    m = re.search(r'-([TB])([\d.]+)$', ticker)
    if m:
        return float(m.group(2)), m.group(1)

    # Try title: "above 3.0%" or "between 3.0% and 3.5%"
    above_match = re.search(r'(?:above|over|more than)\s+(\d+\.?\d*)\s*%', title, re.I)
    if above_match:
        return float(above_match.group(1)), "T"

    below_match = re.search(r'(?:below|under|less than)\s+(\d+\.?\d*)\s*%', title, re.I)
    if below_match:
        return float(below_match.group(1)), "B_below"

    between_match = re.search(r'(?:between)\s+(\d+\.?\d*)\s*%?\s+and\s+(\d+\.?\d*)\s*%', title, re.I)
    if between_match:
        return float(between_match.group(1)), "B_range"

    return None, None


def estimate_days_to_release(market):
    """Estimate days until the economic data release this market tracks.

    Prefers market close_time (which tracks the actual data release) over
    month-parsing heuristics. CPI/GDP/Jobs markets on Kalshi close on or
    near the BLS release date.
    """
    # Primary: use market close_time — most accurate, tracks actual release
    close_time = market.get("close_time", "")
    if close_time:
        try:
            close_dt = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            days = max(0, (close_dt.date() - datetime.date.today()).days)
            return days
        except (ValueError, TypeError):
            pass

    # Fallback: parse month from ticker and estimate ~13th of next month
    ticker = market.get("ticker", "")
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}

    for abbr, num in months.items():
        if abbr in ticker.upper():
            now = datetime.date.today()
            target_year = now.year
            target_month = num + 1
            if target_month > 12:
                target_month = 1
                target_year += 1
            # Handle year boundary: if target is in the past, bump year
            try:
                target_date = datetime.date(target_year, target_month, 13)
                days = (target_date - now).days
                if days < -30:
                    # Target is far in the past — likely need next year
                    target_date = datetime.date(target_year + 1, target_month, 13)
                    days = (target_date - now).days
                return max(0, days)
            except ValueError:
                pass

    # Conservative default
    return 7


def parse_gas_threshold(market):
    """Extract price threshold from gas market ticker/title.

    KXGAS-...-T3.50 -> (3.50, "T")
    KXGAS-...-B3.50 -> (3.50, "B_below")
    """
    ticker = market.get("ticker", "")
    m = re.search(r'-([TB])([\d.]+)$', ticker)
    if m:
        direction_type = m.group(1) if m.group(1) == "T" else "B_below"
        return float(m.group(2)), direction_type

    title = market.get("title", "")
    above = re.search(r'(?:above|over)\s+\$?([\d.]+)', title, re.I)
    if above:
        return float(above.group(1)), "T"
    below = re.search(r'(?:below|under)\s+\$?([\d.]+)', title, re.I)
    if below:
        return float(below.group(1)), "B_below"
    return None, None


# === FedWatch Data ===

def fetch_fedwatch_probabilities():
    """Fetch CME FedWatch implied probabilities for upcoming FOMC meetings.

    Scrapes CME FedWatch page for rate decision probabilities.
    Returns dict: {target_rate: probability} or empty dict on failure.

    Example: {4.25: 0.05, 4.50: 0.85, 4.75: 0.10}
    """
    try:
        url = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        r = retry_request("GET", url, headers=headers, timeout=20)
        html = r.text

        probs = {}
        # CME FedWatch shows rate ranges and probabilities
        # Pattern: "4.25-4.50" ... "85.0%"
        rate_pattern = r'(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*.*?(\d+\.?\d*)%'
        matches = re.findall(rate_pattern, html)
        for low, high, prob_str in matches:
            low_f, high_f = float(low), float(high)
            prob_f = float(prob_str) / 100
            if 0 < low_f < 20 and 0 < prob_f <= 1.0:
                rate = (low_f + high_f) / 2 / 100  # midpoint as decimal (e.g. 0.0438)
                probs[rate] = prob_f

        if probs:
            log.info(f"  FedWatch probabilities: {len(probs)} rate buckets loaded")
            health.record_source_success("cme-fedwatch")
        return probs
    except Exception as e:
        log.error(f"  CME FedWatch fetch failed: {e}")
        health.record_source_error("cme-fedwatch", str(e))
        return {}


def match_fed_market_to_fedwatch(market, fedwatch_probs):
    """Match a KXFED market to its CME FedWatch probability.

    Parses market title for rate action (cut/hold/hike) and maps to
    the appropriate FedWatch probability sum.

    Returns probability (0-1) or None if no match.
    """
    if not fedwatch_probs:
        return None

    title = market.get("title", "").lower()
    ticker = market.get("ticker", "").upper()

    # Estimate current target rate from FedWatch: rate with highest probability
    current_rate = max(fedwatch_probs, key=fedwatch_probs.get)

    if "cut" in title or "lower" in title or "decrease" in title:
        # P(cut) = sum of probabilities for rates below current
        prob = sum(p for r, p in fedwatch_probs.items() if r < current_rate)
        return prob if prob > 0 else None
    elif "hold" in title or "unchanged" in title or "maintain" in title or "no change" in title:
        return fedwatch_probs.get(current_rate)
    elif "raise" in title or "hike" in title or "increase" in title or "higher" in title:
        # P(hike) = sum of probabilities for rates above current
        prob = sum(p for r, p in fedwatch_probs.items() if r > current_rate)
        return prob if prob > 0 else None

    # Try to extract a specific rate from title/ticker
    # e.g. "Fed funds rate above 4.5%" or "KXFED-...-T4.50"
    rate_match = re.search(r'(\d+\.?\d*)\s*%', title)
    if rate_match:
        target = float(rate_match.group(1)) / 100
        # Find closest FedWatch bucket
        closest = min(fedwatch_probs.keys(), key=lambda r: abs(r - target))
        if abs(closest - target) < 0.005:  # within 0.5%
            return fedwatch_probs[closest]

    return None


# === Scanning ===

def scan_and_trade():
    """Scan economics markets and trade on nowcast edge."""
    now = datetime.datetime.now()
    ss = ScanSummary("economics", log)
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Economics scan starting...")

    # Balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")
        ss.finalize()
        return

    # Fetch nowcast data (health recording handled inside fetch_cleveland_fed_nowcast)
    log.info("\nFetching economic data sources...")
    nowcast = fetch_cleveland_fed_nowcast()
    gas_price = fetch_gas_prices()
    if gas_price:
        health.record_source_success("aaa-gas")

    nowcast_stale = nowcast.pop("_stale", False) if nowcast else False
    if nowcast_stale:
        log.warning("Nowcast data is stale (>24h) — skipping CPI/GDP/Jobs trading this cycle")

    if nowcast:
        ss.source_ok("cleveland-fed")
    else:
        ss.source_fail("cleveland-fed", "no data")
    if gas_price:
        ss.source_ok("aaa-gas")

    # Fetch Bayesian filter data sources
    truflation_cpi = None
    tips_breakeven = None
    if macro is not None:
        try:
            truflation_cpi = macro._truflation.fetch() if hasattr(macro, '_truflation') else None
        except Exception as e:
            log.warning(f"  Truflation fetch failed (non-fatal): {e}")
        try:
            fred_data = macro._fred.fetch_all() if hasattr(macro, '_fred') else {}
            tips_breakeven = fred_data.get("tips_breakeven_10y")
        except Exception as e:
            log.warning(f"  FRED fetch failed (non-fatal): {e}")

    # Macro adjustment (if available)
    macro_signal = None
    if macro is not None:
        try:
            macro_signal = macro.compute_signal(cleveland_nowcast=nowcast.get("cpi_yoy") if nowcast else None)
        except Exception as e:
            log.warning(f"  Macro engine error (non-fatal): {e}")
            # Fallback to cached signal
            try:
                macro_signal = macro.load_cached_signal()
                if macro_signal:
                    log.info(f"  Using cached macro signal (bias={macro_signal.cpi_bias:+.3f}%)")
            except Exception:
                pass

    if macro_signal and macro_signal.confidence > 0.2 and nowcast:
        if "cpi_yoy" in nowcast:
            adjusted_cpi = nowcast["cpi_yoy"] + macro_signal.cpi_bias
            nowcast["cpi_yoy"] = adjusted_cpi
            log.info(f"  Macro-adjusted CPI nowcast: {adjusted_cpi:.3f}% "
                     f"(bias={macro_signal.cpi_bias:+.3f}%, conf={macro_signal.confidence:.2f})")

    # Bayesian belief filter (replaces heuristic macro bias)
    # Sources: Cleveland Fed (prior) + Truflation + TIPS breakeven
    if truflation_cpi is not None:
        log.info(f"  Truflation CPI: {truflation_cpi:.2f}%")
        ss.source_ok("truflation")
    if tips_breakeven is not None:
        log.info(f"  TIPS 10Y breakeven: {tips_breakeven:.2f}%")
        ss.source_ok("tips-breakeven")

    # Fetch scenario weight data
    fred_scenario_data = {}
    polymarket_scenario_data = {}
    if macro is not None:
        try:
            fred_all = macro._fred.fetch_all() if hasattr(macro, '_fred') else {}
            fred_scenario_data = {
                "crude_oil": fred_all.get("crude_oil"),
                "crude_oil_90d_ma": fred_all.get("crude_oil"),  # TODO: compute actual 90d MA
                "T10Y2Y": fred_all.get("yield_curve"),
                "gdpnow": fred_all.get("gdpnow"),
            }
        except Exception as e:
            log.warning(f"  FRED scenario data fetch failed (non-fatal): {e}")

    # Compute scenario weights
    scenario_weights = compute_scenario_weights(polymarket_scenario_data, fred_scenario_data)
    log.info(f"  Scenario weights: { {k: f'{v:.2f}' for k, v in scenario_weights.items()} }")

    if not nowcast and not gas_price:
        log.info("No data sources available (nowcast + gas), skipping scan.")
        ss.finalize()
        return

    # Fetch economics markets
    all_markets = []
    for prefix in ECON_PREFIXES:
        try:
            markets = client.get_all_markets(prefix=prefix, cache_ttl=300)
            all_markets.extend(markets)
        except Exception as e:
            log.error(f"Market fetch error for {prefix}: {e}")

    if not all_markets:
        log.info("No open economics markets found.")
        ss.finalize()
        return

    ss.markets_fetched = len(all_markets)
    log.info(f"Found {len(all_markets)} economics markets")

    # Compute cross-measure dispersion for dynamic sigma
    dispersion_ci = None
    if nowcast:
        dispersion_ci = _compute_cross_measure_dispersion(nowcast)
        if dispersion_ci is not None:
            log.info(f"  Cross-measure dispersion CI width: {dispersion_ci:.4f}")

    # Evaluate each market
    opportunities = []
    for m in all_markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")

        market_type = _classify_econ_market(ticker)
        threshold, direction_type = parse_econ_threshold(m)
        if threshold is None:
            ss.skip("no_threshold")
            trade_manager.log_decision(ticker, "skip", "skipped", "no_threshold",
                                       market_type=market_type, title=title[:80])
            continue

        # Determine which nowcast value to use
        nowcast_value = None
        if "CPI" in ticker.upper() or "INFLATION" in ticker.upper():
            nowcast_value = nowcast.get("cpi_yoy") or nowcast.get("core_cpi_yoy")
        elif "GDP" in ticker.upper():
            # Primary: Cleveland Fed GDP nowcast. Fallback: Atlanta Fed GDPNow via macro engine
            nowcast_value = nowcast.get("gdp_growth")
            if nowcast_value is None and macro_signal and hasattr(macro_signal, 'gdpnow') and macro_signal.gdpnow:
                nowcast_value = macro_signal.gdpnow
                log.info(f"  Using GDPNow from macro engine for {ticker}: {nowcast_value:.2f}%")
        elif "JOBS" in ticker.upper() or "EMPLOYMENT" in ticker.upper():
            # Jobs nowcast not yet available — skip with clear log message
            log.info(f"  Skipping {ticker}: no Jobs/NFP nowcast data source connected")
            nowcast_value = None

        if nowcast_value is None:
            ss.skip("no_nowcast")
            trade_manager.log_decision(ticker, "skip", "skipped", "no_nowcast",
                                       market_type=market_type, title=title[:80])
            continue

        # Skip nowcast-based trades when data is stale
        if nowcast_stale:
            ss.skip("stale_nowcast")
            trade_manager.log_decision(ticker, "skip", "skipped", "stale_nowcast",
                                       market_type=market_type, cache_age_hours=round(_nowcast_cache_age_hours(), 1))
            continue

        ss.markets_evaluated += 1

        # Estimate uncertainty — use market-type-specific sigma
        days_to_release = estimate_days_to_release(m)
        ticker_upper = ticker.upper()
        if "CPI" in ticker_upper:
            sigma = cpi_nowcast_sigma(days_to_release, fed_ci_width=dispersion_ci)
        elif "GAS" in ticker_upper:
            # Gas handled separately via gas_price_probability path
            continue
        elif "GDP" in ticker_upper or "JOBS" in ticker_upper or "NFP" in ticker_upper:
            # GDP/Jobs: use calibrated GDP sigma (wider than CPI)
            sigma = gdp_nowcast_sigma(days_to_release)
        elif "FED" in ticker_upper or "FOMC" in ticker_upper:
            # Fed markets handled via FedWatch path
            continue
        else:
            sigma = cpi_nowcast_sigma(days_to_release, fed_ci_width=dispersion_ci)  # default fallback

        # Macro-adjusted sigma tightening
        if macro is not None and macro_signal and macro_signal.confidence > 0.3:
            sigma *= macro.compute_sigma_multiplier(macro_signal.confidence)

        # Bayesian belief fusion
        belief = CPIBeliefFilter(nowcast_value, sigma)
        if truflation_cpi is not None:
            belief.update(truflation_cpi, obs_sigma=0.15)
        if tips_breakeven is not None:
            belief.update(tips_breakeven, obs_sigma=0.25)
        fused_nowcast, posterior_sigma = belief.posterior

        # Compute probability using scenario-weighted mixture
        result = scenario_probability(
            fused_nowcast=fused_nowcast,
            posterior_sigma=posterior_sigma,
            threshold=threshold,
            direction="above" if direction_type == "T" else "below",
            scenario_weights=scenario_weights,
        )
        prob = result.probability

        yes_ask = m.get("yes_ask", 0)
        no_ask = m.get("no_ask", 0)
        yes_bid = m.get("yes_bid", 0)

        if not yes_ask or yes_ask >= 99:
            continue

        # Determine trade direction (raw edge, fees handled in Kelly)
        if prob > 0.5:
            edge = prob - yes_ask / 100
            if edge > EDGE_THRESHOLD:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "yes",
                    "prob": prob, "edge": edge, "threshold": threshold,
                    "nowcast_value": fused_nowcast, "sigma": posterior_sigma,
                    "days_to_release": days_to_release,
                    "raw_nowcast": nowcast_value,
                    "posterior_sigma": posterior_sigma,
                    "scenario_agreement": result.agreement,
                    "per_scenario": result.per_scenario,
                })
            else:
                trade_manager.log_decision(
                    ticker, "yes", "skipped", "edge below threshold",
                    edge=edge, price_cents=yes_ask,
                )
        else:
            no_prob = 1.0 - prob
            if not no_ask:
                trade_manager.log_decision(ticker, "no", "skipped", "no_no_ask", price_cents=0)
                continue
            edge = no_prob - no_ask / 100
            if edge > EDGE_THRESHOLD:
                opportunities.append({
                    "ticker": ticker, "market": m, "side": "no",
                    "prob": no_prob, "edge": edge, "threshold": threshold,
                    "nowcast_value": fused_nowcast, "sigma": posterior_sigma,
                    "days_to_release": days_to_release,
                    "raw_nowcast": nowcast_value,
                    "posterior_sigma": posterior_sigma,
                    "scenario_agreement": result.agreement,
                    "per_scenario": result.per_scenario,
                })
            else:
                trade_manager.log_decision(
                    ticker, "no", "skipped", "edge below threshold",
                    edge=edge, price_cents=no_ask,
                )

    # Gas price markets
    if gas_price:
        gas_markets = [m for m in all_markets if m.get("ticker", "").startswith("KXGAS")]
        if gas_markets:
            log.info(f"  Evaluating {len(gas_markets)} gas price markets (AAA avg: ${gas_price:.2f})")
        for gm in gas_markets:
            ticker = gm.get("ticker", "")
            threshold, direction_type = parse_gas_threshold(gm)
            if threshold is None:
                continue
            direction = "above" if direction_type == "T" else "below"
            prob = gas_price_probability(gas_price, threshold, direction)

            yes_ask = gm.get("yes_ask", 0)
            no_ask = gm.get("no_ask", 0)
            yes_bid = gm.get("yes_bid", 0)
            if not yes_ask or yes_ask >= 99:
                continue

            if prob > 0.5:
                edge = prob - yes_ask / 100
                if edge > GAS_EDGE_THRESHOLD:
                    opportunities.append({
                        "ticker": ticker, "market": gm, "side": "yes",
                        "prob": prob, "edge": edge, "threshold": threshold,
                        "nowcast_value": gas_price, "sigma": gas_price * 0.02,
                        "days_to_release": 0,
                    })
            else:
                no_prob = 1.0 - prob
                if not no_ask:
                    trade_manager.log_decision(ticker, "no", "skipped", "no_no_ask", price_cents=0)
                    continue
                edge = no_prob - no_ask / 100
                if edge > GAS_EDGE_THRESHOLD:
                    opportunities.append({
                        "ticker": ticker, "market": gm, "side": "no",
                        "prob": no_prob, "edge": edge, "threshold": threshold,
                        "nowcast_value": gas_price, "sigma": gas_price * 0.02,
                        "days_to_release": 0,
                    })

    # Fed rate decision markets
    fed_markets = [m for m in all_markets if m.get("ticker", "").startswith("KXFED")]
    if fed_markets:
        fedwatch = fetch_fedwatch_probabilities()
        if fedwatch:
            log.info(f"  Evaluating {len(fed_markets)} Fed rate markets")
            for fm in fed_markets:
                ticker = fm.get("ticker", "")
                cme_prob = match_fed_market_to_fedwatch(fm, fedwatch)
                if cme_prob is None:
                    continue

                yes_ask = fm.get("yes_ask", 0)
                no_ask = fm.get("no_ask", 0)
                yes_bid = fm.get("yes_bid", 0)
                if not yes_ask or yes_ask >= 99:
                    continue

                kalshi_price = yes_ask / 100.0
                edge = cme_prob - kalshi_price

                if edge > EDGE_THRESHOLD:
                    opportunities.append({
                        "ticker": ticker, "market": fm, "side": "yes",
                        "prob": cme_prob, "edge": edge, "threshold": 0,
                        "nowcast_value": cme_prob, "sigma": 0,
                        "days_to_release": 0,
                    })
                elif (-edge) > EDGE_THRESHOLD:
                    # Kalshi overpriced YES -> buy NO
                    no_prob = 1.0 - cme_prob
                    no_edge = no_prob - (no_ask / 100 if no_ask else 1.0)
                    if no_edge > EDGE_THRESHOLD:
                        opportunities.append({
                            "ticker": ticker, "market": fm, "side": "no",
                            "prob": no_prob, "edge": no_edge, "threshold": 0,
                            "nowcast_value": cme_prob, "sigma": 0,
                            "days_to_release": 0,
                        })

    # Sort by edge
    opportunities.sort(key=lambda x: x["edge"], reverse=True)
    log.info(f"Found {len(opportunities)} opportunities with edge >= {EDGE_THRESHOLD*100:.0f}%")

    # Edge scaler: limit total exposure based on settlement track record
    edge_scaler = EdgeScaler()
    settled = [t for t in trade_manager.load_trades() if t.get("settlement_result") is not None]
    max_exposure_pct = edge_scaler.current_limit(settled)
    max_econ_exposure = int(balance * max_exposure_pct)
    log.info(f"  Edge scaler: {len([s for s in settled if s.get('profitable')])} wins -> "
             f"max {max_exposure_pct*100:.0f}% exposure (${max_econ_exposure/100:.0f})")

    for opp in opportunities:
        ticker = opp["ticker"]
        side = opp["side"]
        edge = opp["edge"]
        m = opp["market"]
        yes_ask = m.get("yes_ask", 0)
        yes_bid = m.get("yes_bid", 0)
        no_ask = m.get("no_ask", 0)

        budget = allocator.request_budget("economics", ticker, edge=edge, confidence=opp["prob"])
        if not budget.approved:
            log.info(f"  Allocator denied {ticker}: {budget.reason}")
            ss.skip("allocator_denied")
            trade_manager.log_decision(ticker, side, "skipped", f"allocator denied: {budget.reason}",
                                       edge=edge, price_cents=yes_ask if side == "yes" else no_ask)
            continue

        # Concentration check
        existing_trades = trade_manager.load_trades()
        allowed, conc_reason = _check_concentration(ticker, budget.bankroll_cents, existing_trades)
        if not allowed:
            log.info(f"  Concentration limit hit for {ticker}: {conc_reason}")
            ss.skip("concentration_limit")
            trade_manager.log_decision(ticker, side, "skipped", f"concentration: {conc_reason}",
                                       edge=edge, price_cents=yes_ask if side == "yes" else no_ask)
            continue

        price = compute_limit_price(yes_bid, yes_ask, side, edge=edge) or (yes_ask if side == "yes" else no_ask)
        if not price or price <= 0:
            continue

        fee = kalshi_fee_cents(price)
        # Use uncertainty_kelly for CPI/GDP markets (scenario-aware sizing)
        # Fall back to quarter_kelly for gas/fed markets (no scenario model)
        is_scenario_market = not (ticker.startswith("KXGAS") or ticker.startswith("KXFED"))
        if is_scenario_market and "scenario_agreement" in opp:
            count, risk, kelly_details = uncertainty_kelly(
                edge, price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents,
                scenario_agreement=opp.get("scenario_agreement", 0.5),
                posterior_sigma=opp.get("posterior_sigma", 0.20),
                fee_cents=fee,
            )
        else:
            count, risk, kelly_details = quarter_kelly(
                edge, price, budget.max_cost_cents,
                bankroll_cents=budget.bankroll_cents, fee_cents=fee,
                return_details=True,
            )
        if count <= 0:
            ss.skip("kelly_zero")
            trade_manager.log_decision(ticker, side, "skipped", "kelly_zero: edge too small for price",
                                       edge=edge, price_cents=price)
            continue

        # Format gas price markets differently (dollars, not percentages)
        is_gas = ticker.startswith("KXGAS")
        if is_gas:
            reasoning = (
                f"Gas price: ${opp['nowcast_value']:.2f} vs threshold ${opp['threshold']:.2f}, "
                f"prob={opp['prob']*100:.0f}%, edge={edge*100:.1f}%"
            )
        else:
            reasoning = (
                f"Econ nowcast: {opp['nowcast_value']:.2f}% vs threshold {opp['threshold']:.1f}%, "
                f"sigma={opp['sigma']:.3f}, days_to_release={opp['days_to_release']}, "
                f"prob={opp['prob']*100:.0f}%, edge={edge*100:.1f}%"
            )

        log.info(f"\n-> TRADE: {reasoning}")
        log.info(f"  Placing: {count}x {side} @ {price}c on {ticker}")

        result = trade_manager.place_order(ticker, side, price, count, reasoning,
                                            edge=round(edge, 4),
                                            market_snapshot=build_market_snapshot(yes_bid=yes_bid, yes_ask=yes_ask),
                                            model_prob=round(opp["prob"], 4), raw_edge=round(edge, 4),
                                            fee_cents=round(kalshi_fee_cents(price), 2),
                                            sizing_method="uncertainty_kelly" if is_scenario_market else "quarter_kelly",
                                            market_close_time=m.get("close_time"),
                                            kelly_fraction=kelly_details.get("kelly_fraction"),
                                            bankroll_used=kelly_details.get("bankroll_used"),
                                            sigma_used=round(opp.get("sigma", 0), 4),
                                            nowcast_value=opp.get("nowcast_value"),
                                            days_to_release=opp.get("days_to_release"),
                                            market_type=_classify_econ_market(ticker),
                                            fused_nowcast=round(opp.get("nowcast_value", 0), 4) if opp.get("raw_nowcast") else None,
                                            posterior_sigma=round(opp.get("posterior_sigma", 0), 4) if opp.get("posterior_sigma") else None,
                                            sources_fused=sum([
                                                1,  # Cleveland Fed (always)
                                                1 if truflation_cpi is not None else 0,
                                                1 if tips_breakeven is not None else 0,
                                            ]),
                                            scenario_agreement=round(opp.get("scenario_agreement", 0), 4))
        if result:
            ss.trades_placed += 1
            allocator.record_trade("economics", ticker, risk, edge=edge)

    ss.finalize()


# === Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Kalshi Economics Bot")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Economics Bot (CPI/GDP/Jobs)")
    log.info(f"  Max: ${MAX_TRADE}/trade | Edge: {EDGE_THRESHOLD*100:.0f}%")
    log.info(f"  Scan interval: {SCAN_INTERVAL} minutes")
    log.info("=" * 60)

    # Verify auth
    log.info("\nVerifying authentication...")
    try:
        balance, _ = client.get_balance()
        log.info(f"Auth OK! Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Auth failed: {e}")
        sys.exit(1)

    if args.once:
        scan_and_trade()
        return

    # Daemon loop
    while True:
        try:
            health.record_bot_heartbeat("economics")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            order_monitor.check_orders()
            scan_and_trade()
        except Exception as e:
            log.error(f"Scan error: {e}")
            traceback.print_exc()

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break
        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
