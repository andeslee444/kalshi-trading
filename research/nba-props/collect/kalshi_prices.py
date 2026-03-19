"""Kalshi historical price collector — fetches settled NBA prop market data.

Collects:
1. Settled NBA player prop markets from Kalshi historical API
2. Candlestick price history for each market
3. Trade history for price discovery analysis

This data replaces the proxy "market price" (uniform-weight hit rate) in the
backtester with actual Kalshi prices, enabling the optimizer to learn where
markets are truly mispriced.

Usage:
    python3 -m collect.kalshi_prices
    python3 -m collect.kalshi_prices --max-markets 100 --dry-run
    python3 -m collect.kalshi_prices --series KXNBAPTS,KXNBAREB,KXNBAAST
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Import Kalshi client from sibling repo
KALSHI_TRADING_ROOT = _PROJECT_ROOT.parent / "kalshi-trading"
if str(KALSHI_TRADING_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(KALSHI_TRADING_ROOT / "src"))

_log = logging.getLogger("collect.kalshi_prices")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
                    datefmt="%H:%M:%S")

# NBA player prop series tickers
NBA_PROP_SERIES = [
    "KXNBAPTS",   # Points
    "KXNBAREB",   # Rebounds
    "KXNBAAST",   # Assists
    "KXNBA3PT",   # 3-Pointers
    "KXNBASTL",   # Steals
    "KXNBABLK",   # Blocks
    "KXNBA2D",    # Double-doubles
]


def _parse_ticker(ticker: str) -> dict | None:
    """Parse a Kalshi NBA prop ticker into components.

    Example: KXNBAPTS-26MAR18OKCBKN-BKNNCLAXTON33-25
    Returns: {series, date_str, player_name, line, teams}
    """
    parts = ticker.split("-")
    if len(parts) < 3:
        return None

    series = parts[0]
    date_teams = parts[1] if len(parts) > 1 else ""
    player_line = "-".join(parts[2:])

    # Extract line number (last segment after final -)
    try:
        line = float(parts[-1])
    except (ValueError, IndexError):
        line = None

    return {
        "series": series,
        "date_teams": date_teams,
        "player_line_raw": player_line,
        "line": line,
        "ticker": ticker,
    }


def collect_kalshi_prices(
    series_list: list[str] | None = None,
    max_markets: int = 500,
    include_candlesticks: bool = True,
    dry_run: bool = False,
) -> dict:
    """Collect settled NBA prop markets and their price history from Kalshi."""
    from dotenv import load_dotenv
    load_dotenv(str(KALSHI_TRADING_ROOT / ".env"))

    from kalshi.infra.kalshi_client import KalshiClient
    client = KalshiClient()

    if series_list is None:
        series_list = NBA_PROP_SERIES

    data_dir = _PROJECT_ROOT / "data" / "processed" / "kalshi_prices"
    data_dir.mkdir(parents=True, exist_ok=True)

    _log.info("Fetching NBA markets from Kalshi (%s mode)...", client.mode)

    # Step 1: Get all NBA markets (both live settled and historical)
    all_markets = []

    # Live/recent settled markets
    _log.info("Fetching live/recent markets...")
    for series in series_list:
        markets = client.get_all_markets(prefix=series, status="settled", cache_ttl=0)
        _log.info("  %s settled: %d markets", series, len(markets))
        all_markets.extend(markets)
        time.sleep(0.2)

    # Also fetch currently open markets (for current price snapshots)
    _log.info("Fetching open markets for current prices...")
    for series in series_list:
        markets = client.get_all_markets(prefix=series, status="open", cache_ttl=0)
        _log.info("  %s open: %d markets", series, len(markets))
        all_markets.extend(markets)
        time.sleep(0.2)

    # Historical settled markets (before cutoff)
    # Stop early if consecutive pages have no NBA markets (they're clustered)
    _log.info("Fetching historical markets...")
    cursor = None
    historical_count = 0
    empty_pages = 0
    for page in range(50):
        path = "/historical/markets?limit=1000"
        if cursor:
            path += f"&cursor={cursor}"
        data = client.get(path)
        markets = data.get("markets", [])
        if not markets:
            break

        nba_markets = [m for m in markets if any(
            m.get("ticker", "").startswith(s) for s in series_list
        )]
        all_markets.extend(nba_markets)
        historical_count += len(nba_markets)

        if len(nba_markets) == 0:
            empty_pages += 1
            if empty_pages >= 3:
                _log.info("  3 consecutive pages with no NBA markets, stopping")
                break
        else:
            empty_pages = 0

        cursor = data.get("cursor")
        if not cursor:
            break
        _log.info("  Historical page %d: %d NBA of %d total (cumulative: %d)",
                  page + 1, len(nba_markets), len(markets), historical_count)
        time.sleep(0.3)

    _log.info("Total NBA prop markets: %d", len(all_markets))

    # Deduplicate by ticker
    seen = set()
    unique_markets = []
    for m in all_markets:
        t = m.get("ticker", "")
        if t not in seen:
            seen.add(t)
            unique_markets.append(m)

    _log.info("Unique markets: %d", len(unique_markets))

    if len(unique_markets) > max_markets:
        unique_markets = unique_markets[:max_markets]
        _log.info("Capped to %d (--max-markets)", max_markets)

    if dry_run:
        # Summarize by series
        by_series = {}
        for m in unique_markets:
            series = m.get("series_ticker", m.get("ticker", "")[:10])
            by_series[series] = by_series.get(series, 0) + 1
        print(f"\nDRY RUN: {len(unique_markets)} unique NBA prop markets")
        for s, c in sorted(by_series.items(), key=lambda x: -x[1]):
            print(f"  {s}: {c}")
        return {"dry_run": True, "markets": len(unique_markets)}

    # Step 2: Save market data with optional candlestick history
    collected = 0
    candlesticks_collected = 0
    start_time = time.time()

    for i, market in enumerate(unique_markets):
        ticker = market.get("ticker", "")
        parsed = _parse_ticker(ticker)

        output = {
            "ticker": ticker,
            "title": market.get("title", ""),
            "series_ticker": market.get("series_ticker", ""),
            "status": market.get("status", ""),
            "result": market.get("result", ""),
            "yes_bid": market.get("yes_bid"),
            "yes_ask": market.get("yes_ask"),
            "last_price": market.get("last_price"),
            "volume": market.get("volume", 0),
            "open_interest": market.get("open_interest", 0),
            "parsed": parsed,
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        # Fetch candlestick history for settled markets
        if include_candlesticks and market.get("status") in ("finalized", "settled", "determined"):
            try:
                # Use 1-hour candles, last 7 days before close
                now_ts = int(time.time())
                week_ago = now_ts - 7 * 86400
                candle_path = f"/markets/{ticker}/candlesticks?start_ts={week_ago}&end_ts={now_ts}&period_interval=60"

                # Try live endpoint first, fall back to historical
                try:
                    candle_data = client.get(candle_path)
                except Exception:
                    candle_path = f"/historical/markets/{ticker}/candlesticks?start_ts={week_ago}&end_ts={now_ts}&period_interval=60"
                    candle_data = client.get(candle_path)

                candles = candle_data.get("candlesticks", [])
                if candles:
                    output["candlesticks"] = candles
                    candlesticks_collected += 1
            except Exception as e:
                _log.debug("  No candlesticks for %s: %s", ticker, e)

        # Save
        safe_ticker = ticker.replace("/", "_")
        with open(data_dir / f"{safe_ticker}.json", "w") as f:
            json.dump(output, f, indent=2)

        collected += 1
        if (i + 1) % 50 == 0:
            elapsed = time.time() - start_time
            _log.info("[%d/%d] Collected %d markets, %d with candlesticks (%.1fs)",
                      i + 1, len(unique_markets), collected, candlesticks_collected, elapsed)

        time.sleep(0.1)  # rate limiting

    elapsed = time.time() - start_time
    _log.info("Complete: %d markets, %d with candlesticks in %.1fs",
              collected, candlesticks_collected, elapsed)

    summary = {
        "markets_collected": collected,
        "candlesticks_collected": candlesticks_collected,
        "elapsed_seconds": round(elapsed, 1),
        "by_series": {},
    }
    for m in unique_markets:
        s = m.get("series_ticker", "")
        summary["by_series"][s] = summary["by_series"].get(s, 0) + 1

    with open(data_dir / "collection_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def main():
    parser = argparse.ArgumentParser(description="Collect Kalshi NBA prop market prices")
    parser.add_argument("--max-markets", type=int, default=500)
    parser.add_argument("--series", type=str, default=",".join(NBA_PROP_SERIES),
                        help="Comma-separated series tickers")
    parser.add_argument("--no-candlesticks", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    series = [s.strip() for s in args.series.split(",")]
    summary = collect_kalshi_prices(
        series_list=series,
        max_markets=args.max_markets,
        include_candlesticks=not args.no_candlesticks,
        dry_run=args.dry_run,
    )

    if not args.dry_run and not summary.get("error"):
        print(f"\n{'='*60}")
        print("Kalshi Price Collection Summary")
        print(f"{'='*60}")
        print(f"  Markets:       {summary.get('markets_collected', 0)}")
        print(f"  Candlesticks:  {summary.get('candlesticks_collected', 0)}")
        print(f"  Elapsed:       {summary.get('elapsed_seconds', 0)}s")
        for series, count in sorted(summary.get("by_series", {}).items(), key=lambda x: -x[1]):
            print(f"    {series}: {count}")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
