#!/usr/bin/env python3
"""Kalshi Trading Dashboard — Read-only web UI for monitoring bots, trades, and risk.

Usage:
    python3 scripts/dashboard.py              # Start on port 3456
    python3 scripts/dashboard.py --port 8080  # Custom port

Visit http://localhost:3456 in a browser.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ─── Project paths ───
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from pnl_attribution import PnLAttributor, _classify_market_type
from edge_monitor import EdgeMonitor
from execution_quality import ExecutionAnalyzer
from ticker_utils import format_ticker_human

DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard.html"
DATA_DIR = PROJECT_DIR / "data"
PID_DIR = DATA_DIR / "pids"
LOG_DIR = DATA_DIR / "logs"
HEALTH_STATE_PATH = DATA_DIR / "health-state.json"
ALLOCATOR_STATE_PATH = DATA_DIR / "allocator-state.json"
KILL_SWITCH_PATH = DATA_DIR / "HALT_TRADING"
BOTS_CONFIG_PATH = PROJECT_DIR / "config" / "bots-config.json"
WEATHER_CONFIG_PATH = PROJECT_DIR / "config" / "kalshi-config.json"
STARTING_BALANCE_CENTS = int(os.environ.get("STARTING_BALANCE_CENTS", "50000"))  # $500 default

# ─── Strategy display names ───
STRATEGY_DISPLAY = {
    "weather": "Weather",
    "entertainment": "Entertainment",
    "crypto": "Crypto",
    "economics": "Economics",
    "positions": "Position Mgmt",
    "monitor": "Data Monitor",
    "strategy": "Opportunistic",
    "hdd": "Data Scraper",
    "arb": "Cross-Platform",
    "mm": "Market Making",
    "beatrelease": "Beat Release",
}

# ─── Human-readable decision skip reasons ───
SKIP_REASON_MAP = [
    (["edge", "threshold"], "Advantage too small"),
    (["cooldown", "dedup", "recent"], "Recently traded"),
    (["daily", "limit"], "Daily budget used"),
    (["stale"], "Data too old"),
    (["kill", "halt"], "Trading paused"),
    (["balance", "cost"], "Insufficient funds"),
]


def _human_reason(raw_reason: str) -> str:
    """Convert raw skip reason to human-readable text."""
    if not raw_reason:
        return ""
    lower = raw_reason.lower()
    for keywords, human in SKIP_REASON_MAP:
        if any(k in lower for k in keywords):
            return human
    return raw_reason


# ─── Config key mapping: bot name → bots-config.json key ───
BOT_CONFIG_KEY = {
    "positions": "position_monitor",
    "mm": "market_maker",
    "arb": "cross_platform_arb",
}

# ─── Trade file definitions (from analyze-performance.py) ───
TRADE_FILES = [
    {"label": "Weather Bot",        "bot": "weather",       "path": DATA_DIR / "kalshi-trades.json"},
    {"label": "Strategy Trader",    "bot": "strategy",      "path": DATA_DIR / "kalshi-strategy-trades.json"},
    {"label": "Entertainment Bot",  "bot": "entertainment", "path": DATA_DIR / "kalshi-entertainment-trades.json"},
    {"label": "BeatRelease Scanner","bot": "beatrelease",   "path": DATA_DIR / "beatrelease-trades.json"},
    {"label": "Source Monitor",     "bot": "monitor",       "path": DATA_DIR / "kalshi-monitor-trades.json"},
    {"label": "Position Monitor",   "bot": "positions",     "path": DATA_DIR / "kalshi-position-trades.json"},
    {"label": "Economics Bot",      "bot": "economics",     "path": DATA_DIR / "kalshi-economics-trades.json"},
    {"label": "Crypto Bot",         "bot": "crypto",        "path": DATA_DIR / "kalshi-crypto-trades.json"},
    {"label": "Cross-Platform Arb", "bot": "arb",           "path": DATA_DIR / "kalshi-arb-trades.json"},
    {"label": "Market Maker",       "bot": "mm",            "path": DATA_DIR / "kalshi-mm-trades.json"},
]

# ─── Bot definitions (from supervisor.py) ───
BOT_NAMES = [
    "weather", "entertainment", "crypto", "economics",
    "positions", "monitor", "strategy", "hdd", "arb", "mm",
]

# Decision log files — bots that write decision logs
DECISION_FILES = [
    {"bot": bot, "path": DATA_DIR / f"{bot}-decisions.json"}
    for bot in BOT_NAMES
]


# ─── Helpers (from analyze-performance.py) ───

def load_trades_safe(filepath: Path) -> list | None:
    if not filepath.exists():
        return None
    try:
        text = filepath.read_text().strip()
        if not text:
            return None
        data = json.loads(text)
        if not isinstance(data, list):
            return None
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def extract_side(trade: dict) -> str:
    side = trade.get("side", "")
    if side:
        return side.upper()
    direction = trade.get("direction", "")
    if direction:
        d = direction.upper()
        if "NO" in d:
            return "NO"
        if "YES" in d:
            return "YES"
    return "unknown"


def extract_risk_cents(trade: dict) -> int:
    if "risk_cents" in trade:
        try:
            return int(trade["risk_cents"])
        except (TypeError, ValueError):
            pass
    if "cost_cents" in trade:
        try:
            return int(trade["cost_cents"])
        except (TypeError, ValueError):
            pass
    price = trade.get("price", 0) or 0
    count = trade.get("count", 0) or 0
    if price and count:
        try:
            return int(price) * int(count)
        except (TypeError, ValueError):
            pass
    quantity = trade.get("quantity", 0) or 0
    if price and quantity:
        try:
            return int(price) * int(quantity)
        except (TypeError, ValueError):
            pass
    no_price = trade.get("no_price", 0) or 0
    contracts = trade.get("contracts", 0) or 0
    if no_price and contracts:
        try:
            return int(no_price) * int(contracts)
        except (TypeError, ValueError):
            pass
    price_cents = trade.get("price_cents", 0) or 0
    if price_cents and count:
        try:
            return int(price_cents) * int(count)
        except (TypeError, ValueError):
            pass
    return 0


def extract_timestamp(trade: dict) -> str | None:
    ts = trade.get("timestamp")
    if ts and isinstance(ts, str):
        return ts
    return None


def load_json_safe(filepath: Path) -> dict | list | None:
    if not filepath.exists():
        return None
    try:
        text = filepath.read_text().strip()
        if not text:
            return None
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, OSError):
        return None


# ─── PID check (from supervisor.py) ───

def is_bot_running(name: str) -> tuple[bool, int | None]:
    pid_file = PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        return False, None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return False, None
    try:
        os.kill(pid, 0)
        return True, pid
    except (ProcessLookupError, PermissionError):
        return False, pid


# ─── API cache (per-key TTL) ───

class Cache:
    def __init__(self, default_ttl=30):
        self.default_ttl = default_ttl
        self._store: dict[str, tuple[float, float, object]] = {}  # key → (timestamp, ttl, value)

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and time.time() - entry[0] < entry[1]:
            return entry[2]
        return None

    def set(self, key: str, value, ttl: float | None = None):
        self._store[key] = (time.time(), ttl or self.default_ttl, value)


cache = Cache(default_ttl=30)

# ─── Thread pool for concurrent market fetches ───
_market_pool = ThreadPoolExecutor(max_workers=5)


# ─── Kalshi client (lazy, optional) ───

_kalshi_client = None
_kalshi_available = None


def get_kalshi_client():
    global _kalshi_client, _kalshi_available
    if _kalshi_available is False:
        return None
    if _kalshi_client is not None:
        return _kalshi_client
    try:
        from kalshi_auth import KalshiClient
        _kalshi_client = KalshiClient()
        _kalshi_available = True
        return _kalshi_client
    except Exception:
        _kalshi_available = False
        return None


# ─── FastAPI app ───

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI(title="Kalshi Trading Dashboard")


@app.get("/", response_class=HTMLResponse)
async def index():
    if not DASHBOARD_HTML.exists():
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=500)
    return HTMLResponse(DASHBOARD_HTML.read_text())


def _get_bot_config(name: str, bots_config: dict, weather_config: dict | None = None) -> dict:
    """Resolve the config dict for a bot, checking bots-config.json and kalshi-config.json."""
    if name == "weather" and weather_config:
        return weather_config
    config_key = BOT_CONFIG_KEY.get(name, name)
    return bots_config.get(config_key, {})


@app.get("/api/bots")
async def api_bots():
    health_data = load_json_safe(HEALTH_STATE_PATH) or {}
    bots_config = load_json_safe(BOTS_CONFIG_PATH) or {}
    weather_config = load_json_safe(WEATHER_CONFIG_PATH) or {}
    bot_health = health_data.get("bots", {})

    result = []
    for name in BOT_NAMES:
        running, pid = is_bot_running(name)
        h = bot_health.get(name, {})
        last_heartbeat = h.get("last_heartbeat")

        # Stale check: if heartbeat > 2x scan interval, mark stale
        status = "stopped"
        bot_cfg = _get_bot_config(name, bots_config, weather_config)
        if running:
            status = "running"
            if last_heartbeat:
                try:
                    from datetime import datetime, timezone
                    hb_time = datetime.fromisoformat(last_heartbeat.replace("Z", "+00:00"))
                    age_s = (datetime.now(timezone.utc) - hb_time).total_seconds()
                    interval_min = bot_cfg.get("scanIntervalMinutes", 30)
                    if age_s > interval_min * 60 * 2.5:
                        status = "stale"
                except Exception:
                    pass

        result.append({
            "name": name,
            "display_name": STRATEGY_DISPLAY.get(name, name),
            "status": status,
            "pid": pid if running else None,
            "last_heartbeat": last_heartbeat,
            "scan_interval_min": bot_cfg.get("scanIntervalMinutes"),
            "error_count": h.get("error_count", 0),
        })

    return result


@app.get("/api/account")
async def api_account():
    cached = cache.get("account")
    if cached is not None:
        return cached

    client = get_kalshi_client()
    if client is None:
        return {"error": "API unavailable"}

    try:
        data = client.get("/portfolio/balance")
        balance = data.get("balance", 0)
        portfolio_value = data.get("portfolio_value", 0)
        result = {
            "balance": balance,
            "portfolio_value": portfolio_value,
            "total_value": balance + portfolio_value,
        }

        # Add P&L from settlements (reuse cache if available)
        try:
            settlement_data = cache.get("settlements")
            if not settlement_data:
                s_resp = await api_settlements(limit=200)
                if "summary" in s_resp:
                    settlement_data = s_resp

            if settlement_data and "summary" in settlement_data:
                s = settlement_data["summary"]
                nav = balance + portfolio_value

                result["pnl"] = {
                    "nav_cents": nav,
                    "realized_cents": s["total_pnl_cents"],
                    "open_position_cents": portfolio_value,
                    "today_pnl_cents": s["today_pnl_cents"],
                    "total_fees_cents": s.get("total_fees_cents", 0),
                    "wins": s["wins"],
                    "losses": s["losses"],
                    "win_rate": s["win_rate"],
                }
        except Exception:
            pass  # Account still returns balance data if P&L fails

        cache.set("account", result)
        return result
    except Exception as e:
        return {"error": str(e)}


def _build_local_index() -> tuple[dict[str, dict], dict[str, str]]:
    """Build order_id → local trade info and ticker → bot name indexes."""
    order_index: dict[str, dict] = {}
    ticker_bot: dict[str, str] = {}
    for tf in TRADE_FILES:
        trades = load_trades_safe(tf["path"])
        if not trades:
            continue
        for t in trades:
            oid = t.get("order_id", "")
            if oid:
                order_index[oid] = {
                    "bot": tf["bot"],
                    "bot_label": tf["label"],
                    "edge": t.get("edge") or t.get("edge_pct"),
                    "status": t.get("status", ""),
                }
            ticker = t.get("ticker", "")
            if ticker:
                ticker_bot[ticker] = tf["bot"]
    return order_index, ticker_bot


@app.get("/api/trades")
async def api_trades(
    limit: int = Query(50, ge=1, le=500),
    source: str = Query("auto"),
):
    """Return recent trades. source=api uses Kalshi fills, source=local uses log files,
    source=auto tries API first and falls back to local."""

    # Try API fills first (authoritative)
    if source in ("api", "auto"):
        cached = cache.get("fills")
        if cached is not None:
            return cached[:limit]

        client = get_kalshi_client()
        if client is not None:
            try:
                all_fills = []
                cursor = None
                # Fetch up to 500 recent fills (enough for dashboard)
                for _ in range(3):
                    path = "/portfolio/fills?limit=200"
                    if cursor:
                        path += f"&cursor={cursor}"
                    data = client.get(path)
                    batch = data.get("fills", [])
                    all_fills.extend(batch)
                    cursor = data.get("cursor")
                    if not cursor or not batch:
                        break

                # Build local index for bot attribution
                order_index, ticker_bot = _build_local_index()

                result = []
                for f in all_fills:
                    oid = f.get("order_id", "")
                    ticker = f.get("ticker", "")
                    local = order_index.get(oid, {})

                    # Infer bot from local logs or ticker prefix
                    bot = local.get("bot", "")
                    if not bot:
                        bot = ticker_bot.get(ticker, "")
                    if not bot:
                        bot = _infer_bot_from_ticker(ticker)

                    side = f.get("side", "").upper()
                    price = f.get("yes_price") if side == "YES" else f.get("no_price")
                    count = f.get("count", 0)
                    risk_cents = (price or 0) * count

                    result.append({
                        "bot": bot,
                        "bot_label": local.get("bot_label", ""),
                        "ticker": ticker,
                        "human_ticker": format_ticker_human(ticker),
                        "strategy": STRATEGY_DISPLAY.get(bot, bot),
                        "side": side,
                        "risk_cents": risk_cents,
                        "status": "filled",
                        "timestamp": f.get("created_time", ""),
                        "edge": local.get("edge"),
                        "price": price,
                        "count": count,
                        "source": "api",
                        "order_id": oid,
                    })

                result.sort(key=lambda x: x["timestamp"] or "", reverse=True)
                cache.set("fills", result)
                return result[:limit]
            except Exception:
                if source == "api":
                    return {"error": "API unavailable"}

    # Fallback: local trade logs
    all_trades = []
    for tf in TRADE_FILES:
        trades = load_trades_safe(tf["path"])
        if not trades:
            continue
        for t in trades:
            ts = extract_timestamp(t)
            ticker = t.get("ticker", "")
            all_trades.append({
                "bot": tf["bot"],
                "bot_label": tf["label"],
                "ticker": ticker,
                "human_ticker": format_ticker_human(ticker),
                "strategy": STRATEGY_DISPLAY.get(tf["bot"], tf["bot"]),
                "side": extract_side(t),
                "risk_cents": extract_risk_cents(t),
                "status": t.get("status", ""),
                "timestamp": ts,
                "edge": t.get("edge") or t.get("edge_pct"),
                "price": t.get("price") or t.get("price_cents") or t.get("no_price"),
                "count": t.get("count") or t.get("quantity") or t.get("contracts"),
                "source": "local",
            })

    all_trades.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    return all_trades[:limit]


def _infer_bot_from_ticker(ticker: str) -> str:
    """Best-effort bot attribution from ticker prefix."""
    t = ticker.upper()
    if t.startswith("KXHIGH"):
        return "weather"
    if t.startswith(("KXBTC", "KXETH")):
        return "crypto"
    if t.startswith(("KXCPI", "KXGDP", "KXJOBS")):
        return "economics"
    if t.startswith(("KXALBUM", "KX1ALBUM")):
        return "entertainment"
    # Sports, politics, quicksettle, etc. are typically strategy-trader longshot bets
    if t.startswith("KX"):
        return "strategy"
    return ""


@app.get("/api/decisions")
async def api_decisions(limit: int = Query(50, ge=1, le=500)):
    all_decisions = []
    for df in DECISION_FILES:
        data = load_trades_safe(df["path"])
        if not data:
            continue
        for d in data:
            d["bot"] = df["bot"]
            d["strategy"] = STRATEGY_DISPLAY.get(df["bot"], df["bot"])
            ticker = d.get("ticker", "")
            if ticker:
                d["human_ticker"] = format_ticker_human(ticker)
            reason = d.get("reason", "") or d.get("skip_reason", "")
            if reason:
                d["human_reason"] = _human_reason(reason)
            all_decisions.append(d)

    all_decisions.sort(key=lambda x: x.get("timestamp", "") or "", reverse=True)
    return all_decisions[:limit]


@app.get("/api/risk")
async def api_risk():
    cached = cache.get("risk")
    if cached is not None:
        return cached

    bots_config = load_json_safe(BOTS_CONFIG_PATH) or {}
    weather_config = load_json_safe(WEATHER_CONFIG_PATH) or {}
    allocator_state = load_json_safe(ALLOCATOR_STATE_PATH) or {}
    kill_switch = KILL_SWITCH_PATH.exists()

    # Circuit breaker state
    cb = allocator_state.get("circuit_breaker", {})
    breaker_open = cb.get("failures", 0) >= cb.get("max_failures", 5) if cb else False

    from datetime import date
    today = date.today().isoformat()

    # Bot label lookup
    bot_labels = {tf["bot"]: tf["label"] for tf in TRADE_FILES}

    # Try Kalshi API fills first (authoritative, works from any machine)
    api_counts: dict[str, dict] = {}
    client = get_kalshi_client()
    if client is not None:
        try:
            # Reuse cached fills from /api/trades, or fetch fresh
            fills_data = cache.get("fills")
            if fills_data is None:
                all_fills = []
                cursor = None
                for _ in range(3):
                    path = "/portfolio/fills?limit=200"
                    if cursor:
                        path += f"&cursor={cursor}"
                    data = client.get(path)
                    batch = data.get("fills", [])
                    all_fills.extend(batch)
                    cursor = data.get("cursor")
                    if not cursor or not batch:
                        break
                fills_data = all_fills
            else:
                # Cache hit — reuse cached fills for risk computation too
                all_fills = fills_data if isinstance(fills_data, list) else []

            _, ticker_bot = _build_local_index()

            for f in fills_data:
                # fills_data may be raw API dicts or processed dashboard dicts
                ts = f.get("created_time") or f.get("timestamp", "")
                if not ts or ts[:10] != today:
                    continue

                ticker = f.get("ticker") or f.get("market_ticker", "")
                bot = ticker_bot.get(ticker, "") or _infer_bot_from_ticker(ticker)

                side = (f.get("side") or "").upper()
                count = f.get("count", 0)
                # Raw API fills have yes_price/no_price in cents
                price = f.get("yes_price") if side == "YES" else f.get("no_price")
                risk = (price or 0) * count

                if bot not in api_counts:
                    api_counts[bot] = {"trades": 0, "risk": 0}
                api_counts[bot]["trades"] += 1
                api_counts[bot]["risk"] += risk
        except Exception:
            pass  # Fall through to local logs

    # Build per-bot risk: merge API data with config limits
    per_bot_risk = []
    for tf in TRADE_FILES:
        bot = tf["bot"]
        bot_cfg = _get_bot_config(bot, bots_config, weather_config)

        # Use API data if available, otherwise fall back to local logs
        if api_counts:
            ac = api_counts.get(bot, {"trades": 0, "risk": 0})
            today_trades = ac["trades"]
            today_risk = ac["risk"]
        else:
            trades = load_trades_safe(tf["path"])
            today_trades = 0
            today_risk = 0
            if trades:
                for t in trades:
                    ts = extract_timestamp(t)
                    if ts and ts[:10] == today:
                        today_trades += 1
                        today_risk += extract_risk_cents(t)

        per_bot_risk.append({
            "bot": bot,
            "label": tf["label"],
            "today_trades": today_trades,
            "today_risk_cents": today_risk,
            "max_daily_loss": bot_cfg.get("maxDailyLoss"),
            "max_daily_trades": bot_cfg.get("maxDailyTrades"),
        })

    result = {
        "kill_switch": kill_switch,
        "circuit_breaker_open": breaker_open,
        "circuit_breaker": cb,
        "per_bot": per_bot_risk,
        "source": "api" if api_counts else "local",
    }
    cache.set("risk", result)
    return result


@app.get("/api/orders")
async def api_orders():
    cached = cache.get("orders")
    if cached is not None:
        return cached

    client = get_kalshi_client()
    if client is None:
        return {"error": "API unavailable"}

    try:
        data = client.get("/portfolio/orders?status=resting")
        orders = data.get("orders", [])
        result = []
        for o in orders:
            ticker = o.get("ticker", "")
            bot = _infer_bot_from_ticker(ticker)
            result.append({
                "ticker": ticker,
                "human_ticker": format_ticker_human(ticker),
                "strategy": STRATEGY_DISPLAY.get(bot, bot),
                "side": o.get("side", ""),
                "type": o.get("type", ""),
                "price": o.get("yes_price") or o.get("no_price"),
                "remaining_count": o.get("remaining_count", 0),
                "created_time": o.get("created_time", ""),
                "order_id": o.get("order_id", ""),
            })
        cache.set("orders", result)
        return result
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/settlements")
async def api_settlements(limit: int = Query(50, ge=1, le=200)):
    cached = cache.get("settlements")
    if cached is not None:
        settlements = cached["settlements"][:limit]
        return {"settlements": settlements, "summary": cached["summary"]}

    client = get_kalshi_client()
    if client is None:
        return {"error": "API unavailable"}

    try:
        all_settlements = []
        cursor = None
        for _ in range(3):
            path = "/portfolio/settlements?limit=200"
            if cursor:
                path += f"&cursor={cursor}"
            data = client.get(path)
            batch = data.get("settlements", [])
            all_settlements.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break

        # Build ticker→bot index for attribution
        _, ticker_bot = _build_local_index()

        from datetime import date, datetime, timezone
        today = date.today().isoformat()

        result = []
        total_pnl = 0
        today_pnl = 0
        total_fees = 0
        wins = 0
        losses = 0
        for s in all_settlements:
            revenue = s.get("revenue", 0)
            yes_cost = s.get("yes_total_cost", 0)
            no_cost = s.get("no_total_cost", 0)
            profit = revenue - yes_cost - no_cost
            ticker = s.get("market_ticker", "") or s.get("ticker", "")
            settled_time = s.get("settled_time", "")

            # fee_cost is dollars as string
            try:
                fee_cents = round(float(s.get("fee_cost", "0")) * 100)
            except (TypeError, ValueError):
                fee_cents = 0
            total_fees += fee_cents

            bot = ticker_bot.get(ticker, "")
            if not bot:
                bot = _infer_bot_from_ticker(ticker)

            # W/L based on net profit (not gross revenue)
            if profit > 0:
                wins += 1
            elif profit < 0:
                losses += 1

            total_pnl += profit
            if settled_time and settled_time[:10] == today:
                today_pnl += profit

            result.append({
                "ticker": ticker,
                "human_ticker": format_ticker_human(ticker),
                "strategy": STRATEGY_DISPLAY.get(bot, bot),
                "revenue": revenue,
                "cost": yes_cost + no_cost,
                "profit": profit,
                "fees": fee_cents,
                "settled_time": settled_time,
                "bot": bot,
            })

        count = wins + losses
        summary = {
            "total_pnl_cents": total_pnl,
            "today_pnl_cents": today_pnl,
            "total_fees_cents": total_fees,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / count * 100, 1) if count > 0 else 0,
            "count": len(result),
        }

        full = {"settlements": result, "summary": summary}
        cache.set("settlements", full, ttl=60)
        return {"settlements": result[:limit], "summary": summary}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/health")
async def api_health():
    health_data = load_json_safe(HEALTH_STATE_PATH)
    if health_data is None:
        health_data = {"sources": {}, "bots": {}}

    # Add regime detector state
    regime_path = PROJECT_DIR / "data" / "regime-state.json"
    regime_info = {"regime": "unknown", "confidence": 0.0, "n_updates": 0}
    try:
        with open(regime_path) as f:
            regime_data = json.load(f)
        belief = regime_data.get("belief", [0.25] * 4)
        states = ["low_vol", "normal", "high_vol", "crisis"]
        max_idx = belief.index(max(belief))
        regime_info = {
            "regime": states[max_idx],
            "confidence": belief[max_idx],
            "n_updates": regime_data.get("n_updates", 0),
            "belief": dict(zip(states, belief)),
        }
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    health_data["regime"] = regime_info
    return health_data


BACKTEST_RESULTS_PATH = DATA_DIR / "backtest-results.json"
PERFORMANCE_METRICS_PATH = DATA_DIR / "performance-metrics.json"


@app.get("/api/backtest")
async def api_backtest():
    """Return full backtest results (Brier scores + calibration curves)."""
    data = load_json_safe(BACKTEST_RESULTS_PATH)
    if data is None:
        return {"error": "No backtest results found. Run: python3 scripts/backtest.py --save"}
    return data


@app.get("/api/calibration-curve")
async def api_calibration_curve():
    """Return just the calibration curves section from backtest results."""
    data = load_json_safe(BACKTEST_RESULTS_PATH)
    if data is None:
        return {"error": "No backtest results found. Run: python3 scripts/backtest.py --save"}
    curves = data.get("calibration_curves", {})
    return {"calibration_curves": curves, "timestamp": data.get("timestamp", data.get("generated_at"))}


@app.get("/api/performance")
async def api_performance():
    """Return full P&L performance metrics."""
    data = load_json_safe(PERFORMANCE_METRICS_PATH)
    if data is None:
        return {"error": "No performance metrics found. Run: python3 scripts/analyze-performance.py --reconcile --save"}
    return data


@app.get("/api/logs")
async def api_logs(
    bot: str = Query("weather"),
    lines: int = Query(100, ge=1, le=1000),
):
    log_file = LOG_DIR / f"{bot}.log"
    if not log_file.exists():
        return {"bot": bot, "lines": [], "error": "Log file not found"}

    try:
        text = log_file.read_text()
        all_lines = text.splitlines()
        return {"bot": bot, "lines": all_lines[-lines:]}
    except OSError as e:
        return {"bot": bot, "lines": [], "error": str(e)}


def _fetch_market(client, ticker: str) -> dict | None:
    """Fetch a single market's data, using per-ticker cache."""
    cache_key = f"market:{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        data = client.get(f"/markets/{ticker}")
        market = data.get("market", data)
        cache.set(cache_key, market, ttl=60)
        return market
    except Exception:
        return None


@app.get("/api/positions")
async def api_positions():
    cached = cache.get("positions")
    if cached is not None:
        return cached

    client = get_kalshi_client()
    if client is None:
        return {"error": "API unavailable"}

    try:
        data = client.get("/portfolio/positions")
        positions = data.get("market_positions", [])
        active = [p for p in positions if p.get("position", 0) != 0]

        # Batch-fetch market data concurrently
        tickers = [p.get("ticker", "") for p in active]
        market_map = {}
        if client and tickers:
            futures = {
                ticker: _market_pool.submit(_fetch_market, client, ticker)
                for ticker in tickers if ticker
            }
            for ticker, fut in futures.items():
                try:
                    market_map[ticker] = fut.result(timeout=10)
                except Exception:
                    market_map[ticker] = None

        result = []
        for p in active:
            ticker = p.get("ticker", "")
            market = market_map.get(ticker)
            bot = _infer_bot_from_ticker(ticker)
            entry = {
                "ticker": ticker,
                "human_ticker": format_ticker_human(ticker),
                "strategy": STRATEGY_DISPLAY.get(bot, bot),
                "position": p.get("position", 0),
                "market_exposure": p.get("market_exposure", 0),
                "realized_pnl": p.get("realized_pnl", 0),
                "total_traded": p.get("total_traded", 0),
                "fees_paid": p.get("fees_paid", 0),
                "resting_orders_count": p.get("resting_orders_count", 0),
            }
            if market:
                entry["close_time"] = market.get("close_time") or market.get("expiration_time", "")
                entry["market_status"] = market.get("status", "")
                entry["yes_bid"] = market.get("yes_bid")
                entry["yes_ask"] = market.get("yes_ask")
                entry["result"] = market.get("result", "")
            result.append(entry)

        cache.set("positions", result)
        return result
    except Exception as e:
        return {"error": str(e)}


# ─── Exit State (Active Exits Panel) ───

# Bot source name → bots-config.json key (mirrors position-monitor.py BOT_CONFIG_MAP)
_EXIT_BOT_CONFIG_MAP = {
    "weather": "weather",
    "source-monitor": "weather",
    "entertainment": "entertainment",
    "crypto": "crypto",
    "economics": "economics",
    "strategy": "strategy",
    "beatrelease": "beatrelease",
}


@app.get("/api/exit-state")
async def api_exit_state():
    """Return active exit state for all open positions.

    Combines:
    - Trailing state from data/trailing-state.json (peak bids, entry prices)
    - Per-bot exit thresholds from bots-config.json
    """
    # Load trailing state
    trailing_path = DATA_DIR / "trailing-state.json"
    trailing = {}
    if trailing_path.exists():
        try:
            trailing = json.loads(trailing_path.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    # Load bots config for exit thresholds
    bots_config = load_json_safe(BOTS_CONFIG_PATH) or {}
    pm_cfg = bots_config.get("position_monitor", {})

    # Build per-position exit state
    result = []
    for ticker, state in trailing.items():
        entry_price = state.get("entry_price", 0)
        peak_bid = state.get("peak_bid", 0)
        source_bot = state.get("source_bot", "")
        side = state.get("side", "yes")

        # Look up exit thresholds for this bot
        config_key = _EXIT_BOT_CONFIG_MAP.get(source_bot, "position_monitor")
        bot_cfg = bots_config.get(config_key, {})
        exit_cfg = bot_cfg.get("exit", {})

        take_profit = exit_cfg.get("takeProfitCents", int(pm_cfg.get("takeProfitThreshold", 0.80) * 100))
        stop_loss = exit_cfg.get("stopLossCents", int(pm_cfg.get("stopLossThreshold", 0.30) * 100))
        trailing_drop = exit_cfg.get("trailingDropCents", pm_cfg.get("trailingDropCents", 10))
        trailing_min_profit = exit_cfg.get("trailingMinProfitCents", pm_cfg.get("trailingMinProfitCents", 10))

        # Determine nearest threshold
        nearest = "hold"
        if peak_bid > 0 and entry_price > 0:
            if peak_bid >= entry_price + trailing_min_profit:
                nearest = f"trailing ({peak_bid - trailing_drop}c)"

        result.append({
            "ticker": ticker,
            "human_ticker": format_ticker_human(ticker),
            "strategy": STRATEGY_DISPLAY.get(source_bot, source_bot),
            "entry_price": entry_price,
            "peak_bid": peak_bid,
            "side": side,
            "source_bot": source_bot,
            "take_profit_threshold": take_profit,
            "stop_loss_threshold": stop_loss,
            "trailing_drop": trailing_drop,
            "nearest_threshold": nearest,
            "first_seen": state.get("first_seen", ""),
            "last_updated": state.get("last_updated", ""),
        })

    return result


# ─── Analytics endpoints ───

# Build paths for analytics modules
_ANALYTICS_TRADE_FILES = [
    {"path": str(tf["path"]), "bot": tf["bot"]} for tf in TRADE_FILES
]


@app.get("/api/attribution")
async def api_attribution():
    """P&L attribution by bot, edge bucket, regime, sizing, market type."""
    cached = cache.get("attribution")
    if cached is not None:
        return cached

    attr = PnLAttributor(
        trade_file_paths=_ANALYTICS_TRADE_FILES,
        regime_state_path=str(PROJECT_DIR / "data" / "regime-state.json"),
    )
    attr.load_trades()
    report = attr.full_report()
    cache.set("attribution", report, ttl=120)
    return report


@app.get("/api/edge-decay")
async def api_edge_decay():
    """Edge decay metrics per market type."""
    cached = cache.get("edge_decay")
    if cached is not None:
        return cached

    em = EdgeMonitor(state_path=str(DATA_DIR / "edge-monitor-state.json"))
    em.load_state()

    # If no persisted state, build observations from settled trades
    if not em._observations:
        for tf in TRADE_FILES:
            trades = load_trades_safe(tf["path"])
            if not trades:
                continue
            for t in trades:
                if t.get("settlement_result") is None:
                    continue
                ticker = t.get("ticker", "")
                obs = {
                    "timestamp": t.get("timestamp", ""),
                    "market_type": _classify_market_type(ticker),
                    "model_prob": t.get("model_prob", 0.5),
                    "market_price_cents": t.get("best_ask") or t.get("price_cents", 50),
                    "raw_edge": t.get("raw_edge", 0.0),
                    "settled_won": t.get("settlement_result") in ("won", "yes", True, 1),
                }
                em._observations.append(obs)

    report = em.json_report()
    cache.set("edge_decay", report, ttl=300)
    return report


@app.get("/api/execution-quality")
async def api_execution_quality():
    """Execution quality metrics: fill rate, slippage, shortfall."""
    cached = cache.get("exec_quality")
    if cached is not None:
        return cached

    ea = ExecutionAnalyzer(trade_file_paths=_ANALYTICS_TRADE_FILES)
    ea.load_trades()
    report = ea.json_report()
    cache.set("exec_quality", report, ttl=120)
    return report


# ─── Main ───

@app.get("/api/correlation")
async def api_correlation():
    """Return correlation engine state for monitoring."""
    state_path = DATA_DIR / "correlation-state.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {"cluster_risk": {}, "portfolio_var": 0, "last_updated": ""}


def main():
    parser = argparse.ArgumentParser(description="Kalshi Trading Dashboard")
    parser.add_argument("--port", type=int, default=3456, help="Port (default: 3456)")
    parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    args = parser.parse_args()

    print(f"Dashboard starting at http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
