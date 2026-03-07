#!/usr/bin/env python3
"""Kalshi Position Monitor — Manages exits for open positions.

Scans open positions and evaluates:
  1. Take-profit: sell when bid reaches threshold (e.g. 85c+)
  2. Stop-loss: cut when bid drops below threshold (e.g. 20c)
  3. Model-shift: exit when updated model disagrees with position
  4. Stale orders: cancel resting orders older than settlement

Usage:
    python3 src/kalshi/position-monitor.py          # daemon mode
    python3 src/kalshi/position-monitor.py --once    # single scan
"""

import json, time, datetime, os, sys, re, argparse
from pathlib import Path
from zoneinfo import ZoneInfo
from kalshi_auth import (
    KalshiClient, setup_unbuffered, setup_signal_handlers, setup_logging,
    PROJECT_DIR, TradeManager, trim_trade_log, CITY_TIMEZONES, _local_today,
    round_half_up, retry_request, fetch_parallel, HealthCheckMonitor,
    load_trades, _atomic_write_json, ScanSummary,
    notify_whatsapp, is_shutdown_requested,
)
from probability import weather_probability, nws_probability, half_kelly, kalshi_fee_cents, crypto_price_probability
from ticker_utils import parse_weather_ticker as parse_temp_ticker, parse_crypto_ticker
from capital_allocator import PortfolioAllocator

setup_unbuffered()
log = setup_logging("position-monitor")
setup_signal_handlers()

# === Paths ===
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
WEATHER_CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
TRADES_PATH = PROJECT_DIR / "data" / "kalshi-position-trades.json"
TRADES_PATH.parent.mkdir(parents=True, exist_ok=True)

# Load config
bots_config = json.loads(BOTS_CONFIG_PATH.read_text())
pm_config = bots_config.get("position_monitor", {})

TAKE_PROFIT_THRESHOLD = pm_config.get("takeProfitThreshold", 0.85)
STOP_LOSS_THRESHOLD = pm_config.get("stopLossThreshold", 0.20)
MODEL_SHIFT_THRESHOLD = pm_config.get("modelShiftThreshold", 0.25)
MAX_DAILY_EXITS = pm_config.get("maxDailyExits", 20)
SCAN_INTERVAL = pm_config.get("scanIntervalMinutes", 15)
ORDER_TTL_MINUTES = pm_config.get("orderTtlMinutes", 120)

# Source bot name -> bots-config.json key mapping
BOT_CONFIG_MAP = {
    "weather": "weather",
    "source-monitor": "weather",  # NWS trades use weather config
    "entertainment": "entertainment",
    "crypto": "crypto",
    "economics": "economics",
    "strategy": "strategy",
    "beatrelease": "beatrelease",
}


def _get_exit_config(source_bot):
    """Look up per-bot exit thresholds from bots-config.json.

    Falls back to position_monitor defaults if bot has no exit config.
    """
    config_key = BOT_CONFIG_MAP.get(source_bot, "position_monitor")
    bot_cfg = bots_config.get(config_key, {})
    exit_cfg = bot_cfg.get("exit", {})
    return {
        "take_profit_cents": exit_cfg.get("takeProfitCents", int(TAKE_PROFIT_THRESHOLD * 100)),
        "stop_loss_cents": exit_cfg.get("stopLossCents", int(STOP_LOSS_THRESHOLD * 100)),
        "model_shift_pp": exit_cfg.get("modelShiftPp", int(MODEL_SHIFT_THRESHOLD * 100)),
        "trailing_drop_cents": exit_cfg.get("trailingDropCents", pm_config.get("trailingDropCents", 10)),
        "trailing_min_profit_cents": exit_cfg.get("trailingMinProfitCents", pm_config.get("trailingMinProfitCents", 10)),
        "take_profit_fraction": exit_cfg.get("takeProfitFraction", 0.50),
    }


# === Entry Record Lookup (for 4.1 entry-price stop, 4.3 info-arb gate) ===

from trade_files import ALL_TRADE_PATHS
ALL_TRADE_LOGS = ALL_TRADE_PATHS


def _load_entry_records():
    """Load most recent BUY trade record per ticker across all bot logs.

    Returns {ticker: trade_record_dict}. Used by stop-loss (entry price)
    and info-arb gate (source_bot + model_prob).
    """
    entries = {}
    for log_path in ALL_TRADE_LOGS:
        trades = load_trades(log_path)
        for t in sorted(trades, key=lambda x: x.get("timestamp", "")):
            if t.get("action") != "sell":  # buy records have no "action" key
                entries[t.get("ticker", "")] = t  # last write wins (most recent)
    return entries


# === Trailing Stop Peak State ===

TRAILING_STATE_PATH = PROJECT_DIR / "data" / "trailing-state.json"
_OLD_PEAKS_PATH = PROJECT_DIR / "data" / "position-peaks.json"


def _load_peaks():
    """Load trailing stop peak state from disk.

    Handles migration from old position-peaks.json to trailing-state.json.
    """
    if TRAILING_STATE_PATH.exists():
        try:
            return json.loads(TRAILING_STATE_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    # Migration: read from old path if new doesn't exist
    if _OLD_PEAKS_PATH.exists():
        try:
            data = json.loads(_OLD_PEAKS_PATH.read_text())
            # Write to new path and remove old
            _atomic_write_json(TRAILING_STATE_PATH, data)
            _OLD_PEAKS_PATH.unlink(missing_ok=True)
            log.info("Migrated trailing state from position-peaks.json to trailing-state.json")
            return data
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_peaks(peaks):
    """Save trailing stop peak state to disk atomically."""
    _atomic_write_json(TRAILING_STATE_PATH, peaks)


# Load NWS station config for model-shift evaluation
MONITOR_CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-monitor-config.json"
try:
    _monitor_cfg = json.loads(MONITOR_CONFIG_PATH.read_text())
    NWS_STATIONS = _monitor_cfg.get("sources", {}).get("nws", {}).get("stations", {})
except (FileNotFoundError, json.JSONDecodeError):
    NWS_STATIONS = {}

client = KalshiClient()
allocator = PortfolioAllocator(client, logger=log)
health = HealthCheckMonitor(logger=log)
trade_manager = TradeManager(client, TRADES_PATH, {
    "maxTradeAmount": pm_config.get("maxTradeAmount", 50),  # exits can be larger
    "maxDailyTrades": MAX_DAILY_EXITS,
    "maxDailyLoss": pm_config.get("maxDailyLoss", 100),
}, logger=log, bot_name="positions")
trim_trade_log(TRADES_PATH)

# === Position Fetching ===

def get_open_positions():
    """Fetch all open positions from the Kalshi API.

    The API returns a signed 'position' field (positive=YES, negative=NO).
    We normalize to 'yes' and 'no' count fields for downstream evaluation.
    """
    try:
        data = client.get("/portfolio/positions")
        positions = data.get("market_positions", [])
        result = []
        for p in positions:
            pos_val = p.get("position", 0)
            if pos_val == 0:
                continue
            p["yes"] = pos_val if pos_val > 0 else 0
            p["no"] = abs(pos_val) if pos_val < 0 else 0
            result.append(p)
        return result
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        return []


def get_market_data(ticker):
    """Fetch current market data for a ticker."""
    try:
        data = client.get(f"/markets/{ticker}")
        return data.get("market", data)
    except Exception as e:
        log.error(f"Failed to fetch market {ticker}: {e}")
        return None


# === Exit Evaluation ===

def evaluate_take_profit(position, market, exit_config):
    """Check if position should be partially exited for profit.

    Sells a fraction (exit_config['take_profit_fraction']) at current bid
    using limit order. The remainder rides to settlement or trailing stop.
    """
    ticker = position.get("ticker", "")
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)
    take_profit_cents = exit_config["take_profit_cents"]
    take_profit_fraction = exit_config["take_profit_fraction"]

    yes_bid = market.get("yes_bid", 0)
    no_bid = market.get("no_bid", 0) if market.get("no_bid") else (100 - market.get("yes_ask", 100))

    # Check YES position take-profit (fee-aware)
    if yes_count > 0 and yes_bid > 0:
        fee = kalshi_fee_cents(yes_bid)
        net_proceeds = yes_bid - fee
        if net_proceeds >= take_profit_cents:
            exit_count = max(1, int(yes_count * take_profit_fraction))
            return {
                "action": "take_profit",
                "side": "yes",
                "count": exit_count,
                "price": yes_bid,
                "order_type": "limit",
                "reasoning": (f"Take profit: YES bid {yes_bid}c - fee {fee:.1f}c = "
                              f"net {net_proceeds:.0f}c >= {take_profit_cents}c threshold "
                              f"(selling {exit_count}/{yes_count} contracts)"),
            }

    # Check NO position take-profit (fee-aware)
    if no_count > 0 and no_bid > 0:
        fee = kalshi_fee_cents(no_bid)
        net_proceeds = no_bid - fee
        if net_proceeds >= take_profit_cents:
            exit_count = max(1, int(no_count * take_profit_fraction))
            return {
                "action": "take_profit",
                "side": "no",
                "count": exit_count,
                "price": no_bid,
                "order_type": "limit",
                "reasoning": (f"Take profit: NO bid {no_bid}c - fee {fee:.1f}c = "
                              f"net {net_proceeds:.0f}c >= {take_profit_cents}c threshold "
                              f"(selling {exit_count}/{no_count} contracts)"),
            }

    return None


def evaluate_stop_loss(position, market, exit_config, entry_price_cents=None):
    """Check if position should be cut to limit losses.

    Uses absolute threshold from exit_config. Sends market order (urgent exit).
    Closes full position.
    """
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)
    stop_loss_cents = exit_config["stop_loss_cents"]

    yes_bid = market.get("yes_bid", 0)
    no_bid = market.get("no_bid", 0) if market.get("no_bid") else (100 - market.get("yes_ask", 100))

    # Check YES position stop-loss
    if yes_count > 0 and yes_bid > 0:
        if yes_bid <= stop_loss_cents:
            return {
                "action": "stop_loss",
                "side": "yes",
                "count": yes_count,
                "price": yes_bid,
                "order_type": "market",
                "reasoning": (f"Stop loss: YES bid {yes_bid}c <= {stop_loss_cents}c threshold "
                              f"(entry={entry_price_cents or '?'}c) — MARKET ORDER"),
            }

    # Check NO position stop-loss
    if no_count > 0 and no_bid > 0:
        if no_bid <= stop_loss_cents:
            return {
                "action": "stop_loss",
                "side": "no",
                "count": no_count,
                "price": no_bid,
                "order_type": "market",
                "reasoning": (f"Stop loss: NO bid {no_bid}c <= {stop_loss_cents}c threshold "
                              f"(entry={entry_price_cents or '?'}c) — MARKET ORDER"),
            }

    return None


def evaluate_trailing_stop(position, market, peak_info, exit_config):
    """Exit if bid dropped significantly from observed peak, locking in gains.

    Only triggers when:
      1. Peak bid was profitable (peak >= entry + trailing_min_profit_cents)
      2. Current bid dropped >= trailing_drop_cents from peak
      3. Market is liquid (bid > 0 and spread <= 20c)

    Uses market order for urgent exit.
    Returns (exit_signal_or_None, updated_peak_info).
    """
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)

    if yes_count > 0:
        current_bid = market.get("yes_bid", 0)
        current_ask = market.get("yes_ask", 0)
        side = "yes"
        count = yes_count
    elif no_count > 0:
        current_bid = market.get("no_bid", 0) or (100 - market.get("yes_ask", 100))
        current_ask = market.get("no_ask", 0) if market.get("no_ask") else (100 - market.get("yes_bid", 0))
        side = "no"
        count = no_count
    else:
        return None, peak_info

    # Illiquidity check: skip if no bids or spread > 20c
    if current_bid <= 0:
        log.warning(f"  Trailing stop: skipping {position.get('ticker', '')} — no bids (illiquid)")
        return None, peak_info

    spread = abs(current_ask - current_bid) if current_ask > 0 else 0
    if spread > 20:
        log.warning(f"  Trailing stop: skipping {position.get('ticker', '')} — wide spread {spread}c (illiquid)")
        return None, peak_info

    entry_price = peak_info.get("entry_price", 0)
    peak_bid = peak_info.get("peak_bid", current_bid)

    # Update peak
    if current_bid > peak_bid:
        peak_info["peak_bid"] = current_bid
        peak_bid = current_bid
    peak_info["last_updated"] = datetime.datetime.now().isoformat()

    trailing_drop = exit_config["trailing_drop_cents"]
    trailing_min_profit = exit_config["trailing_min_profit_cents"]

    # Arming condition: peak must be >= entry + min profit
    if peak_bid < entry_price + trailing_min_profit:
        return None, peak_info

    # Trigger: dropped trailing_drop from peak
    if peak_bid - current_bid >= trailing_drop:
        return {
            "action": "trailing_stop",
            "side": side,
            "count": count,
            "price": current_bid,
            "order_type": "market",
            "reasoning": (
                f"Trailing stop: {side} bid {current_bid}c, peak was {peak_bid}c "
                f"(entry {entry_price}c), dropped {peak_bid - current_bid}c >= {trailing_drop}c — MARKET ORDER"
            ),
        }, peak_info

    return None, peak_info


def _fetch_nws_running_high(city_code):
    """Fetch today's running high temperature from NWS for a city.

    Uses CITY_TIMEZONES for timezone-correct observation window.
    Returns running high in Fahrenheit (int), or None on failure.
    """
    station_id = NWS_STATIONS.get(city_code)
    if not station_id:
        return None

    try:
        tz = ZoneInfo(CITY_TIMEZONES.get(city_code, "America/New_York"))
        local_now = datetime.datetime.now(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_start = local_midnight.astimezone(datetime.timezone.utc)
        start = utc_start.strftime("%Y-%m-%dT%H:%M:%SZ")

        url = f"https://api.weather.gov/stations/{station_id}/observations?start={start}&limit=100"
        headers = {
            "User-Agent": "(KalshiPositionMonitor, contact@example.com)",
            "Accept": "application/geo+json",
        }
        r = retry_request("GET", url, headers=headers, timeout=15)
        data = r.json()
        features = data.get("features", [])
        temps = []
        for f in features:
            t = f.get("properties", {}).get("temperature", {}).get("value")
            if t is not None:
                temps.append(round_half_up(t * 9/5 + 32))
        if temps:
            return max(temps)
    except Exception as e:
        log.error(f"  NWS fetch failed for {city_code}: {e}")
    return None


def _get_latest_crypto_vol(asset):
    """Read the latest realized vol for an asset from crypto-bot's decision log.

    Returns vol as a decimal (e.g. 0.55) or None if data is missing/stale (>4h).
    """
    decisions_path = PROJECT_DIR / "data" / "kalshi-crypto-trades-decisions.json"
    try:
        if not decisions_path.exists():
            return None
        decisions = json.loads(decisions_path.read_text())
        # Find most recent decision for this asset with vol_used
        for d in reversed(decisions[-100:]):  # check last 100 decisions
            if d.get("asset", "").upper() == asset.upper() and "vol_used" in d:
                # Check staleness
                ts = d.get("timestamp") or d.get("time")
                if ts:
                    try:
                        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        age_hours = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600
                        if age_hours > 4:
                            log.debug(f"  Crypto vol for {asset} is {age_hours:.1f}h stale, ignoring")
                            return None
                    except (ValueError, TypeError):
                        pass
                vol = d["vol_used"]
                if isinstance(vol, (int, float)) and 0 < vol < 5:
                    return vol
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def _compute_current_probability(ticker, source_bot, entry_side):
    """Recompute probability using the model that opened this position.

    Routes to the correct probability model by source_bot name.
    Returns (probability_for_our_side, reasoning_str) or (None, None) on failure.
    """
    # Weather: parse ticker, fetch NWS running high
    if source_bot in ("weather", "source-monitor"):
        parsed = parse_temp_ticker(ticker)
        if not parsed:
            return None, None
        city = parsed["city"]
        city_today = _local_today(city)
        if parsed["date"] != city_today:
            return None, None  # only model-shift for today's markets
        running_high = _fetch_nws_running_high(city)
        if running_high is None:
            return None, None
        prob = nws_probability(running_high, parsed["threshold"], parsed["direction"],
                               datetime.datetime.now().hour)
        return (prob if entry_side == "yes" else 1.0 - prob,
                f"NWS {city} high {running_high}F, model prob={prob*100:.0f}%")

    # Crypto: fetch current spot price, compute model probability
    if source_bot == "crypto":
        parsed = parse_crypto_ticker(ticker)
        if not parsed:
            return None, None
        asset = parsed["asset"]
        # Fetch current spot price from Coinbase (lightweight public API)
        try:
            url = f"https://api.coinbase.com/v2/prices/{asset}-USD/spot"
            r = retry_request("GET", url, timeout=10)
            price = float(r.json()["data"]["amount"])
        except Exception as e:
            log.error(f"  Crypto spot fetch failed for {asset}: {e}")
            return None, None
        # Estimate time to settlement
        market_data = get_market_data(ticker)
        if not market_data:
            return None, None
        close_time = market_data.get("close_time") or market_data.get("expected_expiration_time")
        minutes_to_settle = 1440
        if close_time:
            try:
                import datetime as dt_mod
                close_dt = dt_mod.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                now = dt_mod.datetime.now(dt_mod.timezone.utc)
                minutes_to_settle = max(1, int((close_dt - now).total_seconds() / 60))
            except (ValueError, TypeError):
                pass
        # Use crypto-bot's latest realized vol if available and fresh
        vol = _get_latest_crypto_vol(asset) or 0.50
        prob = crypto_price_probability(
            price, parsed["threshold"], "above",
            time_horizon_minutes=minutes_to_settle,
            realized_vol_pct=vol,
        )
        side_prob = prob if entry_side == "yes" else 1.0 - prob
        return (side_prob,
                f"Crypto {asset} spot ${price:,.0f}, vol={vol*100:.0f}%, model P(YES)={prob*100:.0f}%, T={minutes_to_settle}min")

    # Entertainment/beatrelease: no live data source to recompute
    if source_bot in ("entertainment", "beatrelease"):
        return None, None  # Hold to settlement

    # Economics: read latest model probability from economics bot decision log
    if source_bot == "economics":
        decisions_path = PROJECT_DIR / "data" / "kalshi-economics-trades-decisions.json"
        try:
            if not decisions_path.exists():
                return None, None
            decisions = json.loads(decisions_path.read_text())
            # Find most recent decision for this ticker
            for d in reversed(decisions):
                if d.get("ticker") == ticker and d.get("model_prob") is not None:
                    current_prob = d["model_prob"]
                    side_prob = current_prob if entry_side == "yes" else 1.0 - current_prob
                    age_min = 0
                    ts = d.get("timestamp", "")
                    if ts:
                        try:
                            d_dt = datetime.datetime.fromisoformat(ts)
                            age_min = (datetime.datetime.now() - d_dt).total_seconds() / 60
                        except (ValueError, TypeError):
                            pass
                    # Only use if decision is less than 12 hours old
                    if age_min < 720:
                        return (side_prob,
                                f"Econ decision log: model_prob={current_prob*100:.0f}% (age={age_min:.0f}min)")
                    break
        except (json.JSONDecodeError, KeyError, OSError):
            pass
        return None, None

    return None, None  # Unknown bot


def evaluate_model_shift(position, market, exit_config, entry_rec=None):
    """Check if our probability model now disagrees with our position.

    Uses _compute_current_probability to route to the correct model.
    Exits when current model probability on our side diverges from entry
    probability by more than model_shift_pp percentage points.
    Uses limit order at current bid (patient exit).
    """
    ticker = position.get("ticker", "")
    yes_count = position.get("yes", 0)
    no_count = position.get("no", 0)
    model_shift_pp = exit_config["model_shift_pp"]

    if not entry_rec:
        return None

    source_bot = entry_rec.get("source_bot", "")
    entry_prob = entry_rec.get("model_prob")
    if entry_prob is None:
        return None

    # Determine our side and bid
    if yes_count > 0:
        side = "yes"
        count = yes_count
        bid = market.get("yes_bid", 0)
        entry_side = "yes"
    elif no_count > 0:
        side = "no"
        count = no_count
        bid = market.get("no_bid", 0) or (100 - market.get("yes_ask", 100))
        entry_side = "no"
    else:
        return None

    if bid <= 0:
        return None

    # Compute current probability using the correct model
    current_prob, reasoning = _compute_current_probability(ticker, source_bot, entry_side)
    if current_prob is None:
        return None  # Can't evaluate model-shift, skip

    # Check if divergence exceeds threshold
    divergence_pp = abs(current_prob * 100 - entry_prob * 100)
    if divergence_pp >= model_shift_pp and current_prob < 0.50:
        return {
            "action": "model_shift",
            "side": side,
            "count": count,  # full position exit
            "price": bid,
            "order_type": "limit",
            "reasoning": (f"Model shift: {reasoning}, current={current_prob*100:.0f}% "
                          f"vs entry={entry_prob*100:.0f}% "
                          f"(divergence {divergence_pp:.0f}pp >= {model_shift_pp}pp) — limit at {bid}c"),
        }

    return None


def cancel_stale_orders():
    """Cancel resting orders that are near settlement or too old.

    Settlement-aware logic (mirrors beatrelease-scanner approach):
      1. If market close_time is within 2 hours → cancel (about to settle, won't fill)
      2. If order age > 12 hours → cancel (stale capital)
      3. Otherwise → keep the order
    """
    try:
        data = client.get("/portfolio/orders?status=resting")
        orders = data.get("orders", [])
        if not orders:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        canceled = 0

        for order in orders:
            ticker = order.get("ticker", "")
            order_id = order.get("order_id", "")
            if not order_id:
                continue

            should_cancel = False
            reason = ""

            # Check 1: Market close time — cancel if settling within 2 hours
            close_time_str = order.get("expiration_time", "") or order.get("close_time", "")
            if close_time_str:
                try:
                    close_dt = datetime.datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                    hours_to_close = (close_dt - now).total_seconds() / 3600
                    if hours_to_close < 2:
                        should_cancel = True
                        reason = f"market closes in {hours_to_close:.1f}h"
                except (ValueError, TypeError):
                    pass

            # Check 2: Order TTL — cancel if older than configured TTL (default 120 min)
            if not should_cancel:
                created = order.get("created_time", "")
                if created:
                    try:
                        created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_minutes = (now - created_dt).total_seconds() / 60
                        if age_minutes > ORDER_TTL_MINUTES:
                            should_cancel = True
                            reason = f"age {age_minutes:.0f}min > {ORDER_TTL_MINUTES}min TTL"
                    except (ValueError, TypeError):
                        pass

            # Check 3: Fallback — cancel if older than 12 hours (safety net)
            if not should_cancel:
                created = order.get("created_time", "")
                if created:
                    try:
                        created_dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                        age_hours = (now - created_dt).total_seconds() / 3600
                        if age_hours > 12:
                            should_cancel = True
                            reason = f"age {age_hours:.0f}h > 12h"
                    except (ValueError, TypeError):
                        pass

            if should_cancel:
                try:
                    client.delete(f"/portfolio/orders/{order_id}")
                    log.info(f"  Canceled stale order {order_id} on {ticker} ({reason})")
                    canceled += 1
                except Exception as e:
                    log.error(f"  Failed to cancel {order_id}: {e}")

        if canceled:
            log.info(f"  Canceled {canceled} stale resting orders")

    except Exception as e:
        log.error(f"Failed to fetch resting orders: {e}")


def _count_exits_today():
    """Count exit trades placed today from the trade log."""
    trades = load_trades(TRADES_PATH)
    today = datetime.date.today().isoformat()
    return sum(1 for t in trades if t.get("action") == "sell" and t.get("timestamp", "").startswith(today))


# === Main Scan ===

def scan_positions():
    """Scan all open positions and evaluate exit opportunities."""
    # Record heartbeat at scan START (not just end) to prevent supervisor staleness kills
    health.record_bot_heartbeat("position-monitor")

    ss = ScanSummary("position-monitor", log)
    now = datetime.datetime.now()
    log.info(f"\n{'='*60}")
    log.info(f"[{now.isoformat()}] Position scan starting...")

    # Get balance
    try:
        balance, _ = client.get_balance()
        log.info(f"Balance: ${balance/100:.2f}")
    except Exception as e:
        log.error(f"Balance error: {e}")

    # Cancel stale orders first
    cancel_stale_orders()

    # Get open positions
    positions = get_open_positions()
    if not positions:
        log.info("No open positions found.")
        ss.finalize()
        return

    log.info(f"Found {len(positions)} positions to evaluate")
    exits_today = _count_exits_today()

    # Load entry records for entry-price stop and info-arb gate
    entry_records = _load_entry_records()
    log.info(f"Loaded {len(entry_records)} entry records from {len(ALL_TRADE_LOGS)} trade logs")
    peaks = _load_peaks()
    # Grace period: skip trailing stop evaluation for first scan after restart
    # to prevent stale peak data from triggering immediate exits.
    # Persisted in trailing-state.json _metadata instead of function attribute.
    _metadata = peaks.pop("_metadata", {})
    _current_pid = str(os.getpid())
    _stored_pid = _metadata.get("pid")
    grace_expires = _metadata.get("grace_period_expires")
    _first_scan = False
    if _stored_pid != _current_pid:
        # New process detected — set fresh grace period
        _first_scan = True
        _metadata["pid"] = _current_pid
        _metadata["grace_period_expires"] = (datetime.datetime.now() + datetime.timedelta(minutes=5)).isoformat()
        peaks["_metadata"] = _metadata
        _save_peaks(peaks)
        log.info("  New process (pid=%s) — trailing stop grace period active (5 min)", _current_pid)
    elif grace_expires:
        try:
            expires_dt = datetime.datetime.fromisoformat(grace_expires)
            if datetime.datetime.now() < expires_dt:
                _first_scan = True
                log.info("  Grace period still active (expires %s)", grace_expires)
        except (ValueError, TypeError):
            pass
    # Restore _metadata into peaks for persistence
    peaks["_metadata"] = _metadata
    open_tickers = set()

    for pos in positions:
        ticker = pos.get("ticker", "")
        yes_count = pos.get("yes", 0)
        no_count = pos.get("no", 0)

        if yes_count == 0 and no_count == 0:
            continue

        open_tickers.add(ticker)

        # Fetch market data
        market = get_market_data(ticker)
        if not market:
            continue

        log.info(f"  {ticker}: YES={yes_count} NO={no_count} | bid={market.get('yes_bid',0)}c ask={market.get('yes_ask',0)}c")

        # Look up entry record for this position
        entry_rec = entry_records.get(ticker, {})
        entry_price = entry_rec.get("price_cents")
        source_bot = entry_rec.get("source_bot", "")
        exit_config = _get_exit_config(source_bot)

        # Evaluate exit conditions in priority order
        exit_signal = None

        # 1. Take profit (per-bot thresholds handle info-arb naturally)
        exit_signal = evaluate_take_profit(pos, market, exit_config)

        # 2. Stop loss (per-bot threshold, market order)
        if not exit_signal:
            exit_signal = evaluate_stop_loss(pos, market, exit_config, entry_price_cents=entry_price)

        # 3. Trailing stop (per-bot config, illiquidity protection, market orders)
        if not exit_signal:
            if ticker not in peaks:
                peaks[ticker] = {
                    "entry_price": entry_price or 0,
                    "peak_bid": 0,
                    "side": "yes" if yes_count > 0 else "no",
                    "source_bot": source_bot,
                    "first_seen": datetime.datetime.now().isoformat(),
                    "last_updated": datetime.datetime.now().isoformat(),
                }
            elif entry_price:
                # Refresh entry price in case position was averaged up/down
                peaks[ticker]["entry_price"] = entry_price

            if not _first_scan:
                exit_signal, peaks[ticker] = evaluate_trailing_stop(pos, market, peaks[ticker], exit_config)
            else:
                # Grace period: still update peaks, just don't trigger exits
                current_bid = market.get("yes_bid", 0) if yes_count > 0 else (market.get("no_bid", 0) or (100 - market.get("yes_ask", 100)))
                if current_bid > peaks[ticker].get("peak_bid", 0):
                    peaks[ticker]["peak_bid"] = current_bid
                peaks[ticker]["last_updated"] = datetime.datetime.now().isoformat()

        # 4. Model shift (per-bot threshold, multi-model routing)
        if not exit_signal:
            exit_signal = evaluate_model_shift(pos, market, exit_config, entry_rec=entry_rec)

        if exit_signal and exits_today < MAX_DAILY_EXITS:
            log.info(f"  -> EXIT SIGNAL: {exit_signal['action']} on {ticker}")
            log.info(f"     {exit_signal['reasoning']}")

            result = trade_manager.sell_position(
                ticker,
                exit_signal["side"],
                exit_signal["price"],
                exit_signal["count"],
                exit_signal["reasoning"],
                order_type=exit_signal.get("order_type", "limit"),
                exit_type=exit_signal["action"],
                sizing_method="position_exit",
                entry_price_cents=entry_price,
                peak_bid=peaks.get(ticker, {}).get("peak_bid"),
            )
            if result:
                exits_today += 1
                ss.trades_placed += 1
                allocator.record_trade("position-monitor", ticker, risk=0, edge=0)
                trade_manager.log_decision(ticker, exit_signal["side"], "placed", exit_signal["action"],
                                           price_cents=exit_signal["price"])

                # WhatsApp notification on successful exit
                entry_cost_cents = (entry_price or 0) * exit_signal["count"]
                exit_proceeds_cents = exit_signal["price"] * exit_signal["count"]
                pnl_cents = exit_proceeds_cents - entry_cost_cents
                pnl_str = f"+${pnl_cents/100:.2f}" if pnl_cents >= 0 else f"-${abs(pnl_cents)/100:.2f}"
                notify_whatsapp(
                    f"EXIT [{exit_signal['action']}] {ticker}: "
                    f"bought {entry_price or '?'}c, sold {exit_signal['price']}c, {pnl_str}",
                    logger=log,
                )
            else:
                trade_manager.log_decision(ticker, exit_signal["side"], "rejected", "sell_failed",
                                           price_cents=exit_signal["price"])
        elif exit_signal and exits_today >= MAX_DAILY_EXITS:
            trade_manager.log_decision(ticker, exit_signal["side"], "skipped", "daily_exit_limit",
                                       price_cents=exit_signal["price"])
        else:
            # No exit signal — position held
            ss.skip("no_exit_signal")

    # Mid-scan heartbeat to prevent supervisor staleness detection on long scans
    health.record_bot_heartbeat("position-monitor")

    # Clean up peaks for closed positions and save
    stale_tickers = [t for t in peaks.keys() if t not in open_tickers and t != "_metadata"]
    for stale_ticker in stale_tickers:
        del peaks[stale_ticker]
    if stale_tickers:
        log.info(f"  Cleaned up trailing state for {len(stale_tickers)} closed positions")
    _save_peaks(peaks)

    # Process allocator pending exits (superseded by better signals)
    pending = allocator.get_pending_exits()
    if pending:
        log.info(f"  Processing {len(pending)} pending exits from allocator supersede...")
        for pending_ticker in pending:
            # Find this ticker in our positions
            for pos in positions:
                if pos.get("ticker") == pending_ticker and exits_today < MAX_DAILY_EXITS:
                    yes_count = pos.get("yes", 0)
                    no_count = pos.get("no", 0)
                    market = get_market_data(pending_ticker)
                    if not market:
                        continue
                    if yes_count > 0:
                        bid = market.get("yes_bid", 0)
                        if bid > 0:
                            result = trade_manager.sell_position(
                                pending_ticker, "yes", bid, yes_count,
                                f"Allocator supersede exit: better signal available",
                                exit_type="allocator_supersede",
                            )
                            if result:
                                exits_today += 1
                                allocator.record_trade("position-monitor", pending_ticker, risk=0, edge=0)
                    elif no_count > 0:
                        no_bid = market.get("no_bid", 0) or (100 - market.get("yes_ask", 100))
                        if no_bid > 0:
                            result = trade_manager.sell_position(
                                pending_ticker, "no", no_bid, no_count,
                                f"Allocator supersede exit: better signal available",
                                exit_type="allocator_supersede",
                            )
                            if result:
                                exits_today += 1
                                allocator.record_trade("position-monitor", pending_ticker, risk=0, edge=0)

    ss.markets_fetched = len(positions)
    ss.markets_evaluated = len(open_tickers)
    ss.finalize()
    log.info(f"Scan complete. {exits_today} exit orders placed.")


# === Entry Point ===

def main():
    parser = argparse.ArgumentParser(description="Kalshi Position Monitor")
    parser.add_argument("--once", action="store_true", help="Run single scan and exit")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Kalshi Position Monitor")
    log.info(f"  Default take-profit: {pm_config.get('takeProfitThreshold', 0.80)*100:.0f}c")
    log.info(f"  Default stop-loss: {pm_config.get('stopLossThreshold', 0.30)*100:.0f}c")
    log.info(f"  Default model-shift: {MODEL_SHIFT_THRESHOLD*100:.0f}pp  Max exits/day: {MAX_DAILY_EXITS}")
    log.info(f"  Scan interval: {SCAN_INTERVAL} minutes")
    log.info(f"  Per-bot exit config enabled ({len([k for k in bots_config if 'exit' in bots_config.get(k, {})])} bots configured)")
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
        scan_positions()
        return

    # Daemon loop
    while True:
        try:
            health.record_bot_heartbeat("position-monitor")
            issues = health.check_health()
            if issues:
                log.warning("Health issues: %s", "; ".join(issues))
            scan_positions()
        except Exception as e:
            log.error("Scan error: %s", e, exc_info=True)

        if is_shutdown_requested():
            log.info("Graceful shutdown requested, exiting.")
            break
        log.info(f"\nNext scan in {SCAN_INTERVAL} minutes...")
        time.sleep(SCAN_INTERVAL * 60)


if __name__ == "__main__":
    main()
