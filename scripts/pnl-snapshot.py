#!/usr/bin/env python3
"""P&L Snapshot — Dual-source verified financial summary.

Fetches authoritative data from Kalshi API, loads local trade logs,
cross-references both sources, writes data/financial-snapshot.json.

Usage:
    python3 scripts/pnl-snapshot.py              # generate snapshot
    python3 scripts/pnl-snapshot.py --print       # print to stdout instead of file
    python3 scripts/pnl-snapshot.py --pull-s3     # pull trade logs from S3 first
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ─── Project paths ───
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

from artifact_contracts import normalize_financial_snapshot
from trade_files import TRADE_FILES as _CANONICAL_FILES

DATA_DIR = PROJECT_DIR / "data"
DEPOSITS_PATH = DATA_DIR / "deposits.json"
SNAPSHOT_PATH = DATA_DIR / "financial-snapshot.json"


# ─── Pure computation functions (no I/O, fully testable) ───

def compute_realized_pnl(settlements):
    """Compute realized P&L from Kalshi API settlement records.

    Uses the authoritative formula: profit = revenue - yes_total_cost - no_total_cost
    (Kalshi's 'revenue' is gross payout, NOT net profit.)

    Returns dict with total_cents, fees, wins/losses, by_day, by_bot breakdown.
    """
    total_cents = 0
    total_fees_cents = 0
    wins = 0
    losses = 0
    by_day = defaultdict(int)

    for s in settlements:
        revenue = _safe_int(s.get("revenue", 0))
        yes_cost = _cost_cents(s, "yes")
        no_cost = _cost_cents(s, "no")
        profit = revenue - yes_cost - no_cost

        # Fee: Kalshi returns dollars as string (e.g. "0.04")
        try:
            fee_cents = round(float(s.get("fee_cost", "0") or "0") * 100)
        except (TypeError, ValueError):
            fee_cents = 0

        total_cents += profit
        total_fees_cents += fee_cents

        if profit > 0:
            wins += 1
        elif profit < 0:
            losses += 1

        # Daily breakdown by settlement date
        settled_time = s.get("settled_time", "")
        if settled_time and isinstance(settled_time, str) and len(settled_time) >= 10:
            day = settled_time[:10]
            by_day[day] += profit

    count = wins + losses
    return {
        "total_cents": total_cents,
        "total_fees_cents": total_fees_cents,
        "net_after_fees_cents": total_cents - total_fees_cents,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / count, 4) if count > 0 else 0.0,
        "by_day": dict(sorted(by_day.items())),
        "source": "kalshi_api_settlements",
    }


def compute_unrealized_pnl(positions, fills):
    """Compute unrealized P&L from open positions and historical fills.

    WARNING: This calculation is unreliable. Kalshi's market_exposure field
    returns cost basis, not current market value, so most positions show
    unrealized = $0. Use balance_check.implied_unrealized_cents instead.

    Returns dict with total unrealized and per-position breakdown.
    """
    # Build ticker → cost basis from fills
    ticker_cost = defaultdict(int)
    for f in fills:
        ticker = f.get("ticker", "")
        side = (f.get("side", "") or "").lower()
        if side == "yes":
            price_d = float(f.get("yes_price_dollars", "0") or f.get("yes_price", 0) or "0")
        else:
            price_d = float(f.get("no_price_dollars", "0") or f.get("no_price", 0) or "0")
        count = float(f.get("count_fp", "0") or f.get("count", 0) or "0")
        price_cents = round(price_d * 100)
        action = (f.get("action", "") or "").lower()
        if action == "buy":
            ticker_cost[ticker] += price_cents * int(count)
        elif action == "sell":
            ticker_cost[ticker] -= price_cents * int(count)

    result_positions = []
    total_unrealized = 0

    for p in positions:
        pos_count = float(p.get("position_fp", "0") or p.get("position", 0) or "0")
        if pos_count == 0:
            continue
        ticker = p.get("ticker", "")
        exposure_d = float(p.get("market_exposure_dollars", "0") or p.get("market_exposure", 0) or "0")
        current_value = round(exposure_d * 100)
        cost = ticker_cost.get(ticker, 0)
        unrealized = current_value - cost

        result_positions.append({
            "ticker": ticker,
            "position": int(pos_count),
            "cost_cents": cost,
            "current_value_cents": current_value,
            "unrealized_cents": unrealized,
        })
        total_unrealized += unrealized

    return {
        "total_cents": total_unrealized,
        "positions": result_positions,
        "source": "kalshi_api_positions_and_fills",
    }


def verify_settlements(api_settlements, local_trades):
    """Cross-verify API settlements against local trade logs.

    Runs multiple checks and returns structured verification report.
    """
    checks = []

    # Build ticker sets (exclude sell/exit trades from local)
    api_tickers = {s.get("ticker", "") or s.get("market_ticker", "")
                   for s in api_settlements}
    local_buy_trades = [t for t in local_trades
                        if t.get("action", "buy") == "buy"]
    local_tickers = {t.get("ticker", "") for t in local_buy_trades
                     if t.get("ticker")}

    # Check 1: Settlement count match
    api_count = len(api_tickers)
    local_settled_tickers = local_tickers & api_tickers
    local_count = len(local_settled_tickers)
    count_match = api_count == local_count
    checks.append({
        "check": "settlement_count_match",
        "api": api_count,
        "local": local_count,
        "status": "ok" if count_match else "warning",
        "detail": (f"{api_count - local_count} API settlements have no matching "
                   f"local trade" if not count_match else ""),
    })

    # Check 2: P&L agreement per ticker
    # Build API P&L by ticker
    api_pnl_by_ticker = {}
    for s in api_settlements:
        ticker = s.get("ticker", "") or s.get("market_ticker", "")
        revenue = _safe_int(s.get("revenue", 0))
        cost = _cost_cents(s, "yes") + _cost_cents(s, "no")
        api_pnl_by_ticker[ticker] = revenue - cost

    # Build local P&L by ticker from settlement_result + cost_cents + count.
    # NOTE: Do NOT use settlement_revenue_cents — it has inconsistent semantics
    # (reconcile-trades.py stores gross payout, backfill-settlements.py stores
    # net profit). Instead, derive P&L from settlement outcome directly.
    local_pnl_by_ticker = defaultdict(int)
    for t in local_buy_trades:
        ticker = t.get("ticker", "")
        result = t.get("settlement_result")
        if result is not None and ticker:
            count = t.get("count", 1) or 1
            cost = t.get("cost_cents", 0) or 0
            if result == "won":
                local_pnl_by_ticker[ticker] += 100 * count - cost
            elif result == "lost":
                local_pnl_by_ticker[ticker] += -cost

    # Compare where both exist
    common_tickers = set(api_pnl_by_ticker) & set(local_pnl_by_ticker)
    total_api_pnl = sum(api_pnl_by_ticker.get(t, 0) for t in common_tickers)
    total_local_pnl = sum(local_pnl_by_ticker.get(t, 0) for t in common_tickers)
    delta = abs(total_api_pnl - total_local_pnl)
    checks.append({
        "check": "pnl_agreement",
        "api_cents": total_api_pnl,
        "local_cents": total_local_pnl,
        "delta_cents": delta,
        "tickers_compared": len(common_tickers),
        "status": "ok" if delta == 0 else "warning",
    })

    # Check 3: Orphan detection
    unmatched_api = sorted(api_tickers - local_tickers)
    unmatched_local = sorted(local_tickers - api_tickers)

    checks.append({
        "check": "orphan_settlements",
        "count": len(unmatched_api),
        "status": "ok" if not unmatched_api else "warning",
    })
    checks.append({
        "check": "orphan_local_trades",
        "count": len(unmatched_local),
        "status": "ok" if not unmatched_local else "info",
        "detail": "Local trades not yet settled" if unmatched_local else "",
    })

    # Overall status
    statuses = [c["status"] for c in checks]
    if "error" in statuses:
        overall = "errors"
    elif "warning" in statuses:
        overall = "warnings"
    else:
        overall = "ok"

    return {
        "status": overall,
        "checks": checks,
        "unmatched_api_settlements": unmatched_api,
        "unmatched_local_trades": unmatched_local,
    }


def load_deposits(deposits_path):
    """Load manually-tracked deposit/withdrawal ledger.

    Returns dict with tracked, totals, and optional ROI.
    File format: JSON array of {date, type, amount_cents, note}.
    """
    path = Path(deposits_path) if deposits_path else None
    if not path or not path.exists():
        return {"tracked": False, "source": "data/deposits.json"}

    try:
        data = json.loads(path.read_text().strip())
        if not isinstance(data, list):
            return {"tracked": False, "source": str(path)}
    except (json.JSONDecodeError, ValueError, OSError):
        return {"tracked": False, "source": str(path)}

    total_deposited = 0
    total_withdrawn = 0
    for entry in data:
        amount = entry.get("amount_cents", 0)
        if entry.get("type") == "deposit":
            total_deposited += amount
        elif entry.get("type") == "withdrawal":
            total_withdrawn += amount

    return {
        "tracked": True,
        "total_deposited_cents": total_deposited,
        "total_withdrawn_cents": total_withdrawn,
        "net_funded_cents": total_deposited - total_withdrawn,
        "entries": len(data),
        "source": str(path),
    }


def build_snapshot(balance_cents, portfolio_value_cents, settlements, fills,
                   positions, local_trades, deposits_path):
    """Assemble the complete financial snapshot from all data sources."""
    realized = compute_realized_pnl(settlements)
    unrealized = compute_unrealized_pnl(positions, fills)
    verification = verify_settlements(settlements, local_trades)
    deposits = load_deposits(deposits_path)

    # Bot attribution from API settlements + local trade ticker→bot mapping
    ticker_to_bot = {}
    for t in local_trades:
        ticker = t.get("ticker", "")
        bot = t.get("source_bot", "")
        if ticker and bot:
            ticker_to_bot[ticker] = bot

    by_bot = defaultdict(lambda: {"pnl_cents": 0, "wins": 0, "losses": 0, "fees_cents": 0})
    for s in settlements:
        ticker = s.get("ticker", "") or s.get("market_ticker", "")
        revenue = _safe_int(s.get("revenue", 0))
        cost = _cost_cents(s, "yes") + _cost_cents(s, "no")
        profit = revenue - cost
        try:
            fee = round(float(s.get("fee_cost", "0") or "0") * 100)
        except (TypeError, ValueError):
            fee = 0

        bot = ticker_to_bot.get(ticker, _infer_bot(ticker))
        by_bot[bot]["pnl_cents"] += profit
        by_bot[bot]["fees_cents"] += fee
        if profit > 0:
            by_bot[bot]["wins"] += 1
        elif profit < 0:
            by_bot[bot]["losses"] += 1

    # Add win_rate to each bot
    for stats in by_bot.values():
        total = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(stats["wins"] / total, 4) if total > 0 else 0.0

    realized["by_bot"] = dict(by_bot)

    nav_cents = balance_cents + portfolio_value_cents

    # Balance check: derive true P&L from NAV vs deposits (ground truth).
    # Kalshi's market_exposure field returns cost basis, not current value,
    # so compute_unrealized_pnl is unreliable. The balance equation is authoritative.
    balance_check = _build_balance_check(nav_cents, realized, deposits)

    # ROI: use true total P&L (NAV - deposits) when available, not just realized
    if deposits.get("tracked") and deposits.get("net_funded_cents", 0) > 0:
        net_funded = deposits["net_funded_cents"]
        true_total_pnl = nav_cents - net_funded
        deposits["roi_pct"] = round(true_total_pnl / net_funded * 100, 2)

    return normalize_financial_snapshot({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_used": ["kalshi_api", "local_trade_logs"],
        "account": {
            "balance_cents": balance_cents,
            "portfolio_value_cents": portfolio_value_cents,
            "nav_cents": nav_cents,
        },
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "balance_check": balance_check,
        "verification": verification,
        "deposits": deposits,
    })


def _build_balance_check(nav_cents, realized, deposits):
    """Cross-check P&L using the balance equation: true_pnl = NAV - deposits.

    Kalshi's market_exposure field returns cost basis, not current market value,
    so compute_unrealized_pnl is unreliable. This function derives the true
    unrealized P&L from the authoritative NAV and deposit data.
    """
    if not deposits.get("tracked"):
        return {"available": False, "reason": "deposits not tracked"}

    net_funded = deposits.get("net_funded_cents", 0)
    realized_net = realized.get("net_after_fees_cents", 0)

    true_total_pnl = nav_cents - net_funded
    implied_unrealized = true_total_pnl - realized_net

    return {
        "available": True,
        "nav_cents": nav_cents,
        "net_funded_cents": net_funded,
        "true_total_pnl_cents": true_total_pnl,
        "realized_net_cents": realized_net,
        "implied_unrealized_cents": implied_unrealized,
    }


def _infer_bot(ticker):
    """Best-effort bot attribution from ticker prefix."""
    t = (ticker or "").upper()
    if t.startswith("KXHIGH"):
        return "weather"
    if t.startswith(("KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP")):
        return "crypto"
    if t.startswith(("KXCPI", "KXGDP", "KXJOBS", "KXFED", "KXECONSTAT")):
        return "economics"
    if t.startswith(("KXALBUM", "KX1ALBUM")):
        return "entertainment"
    return "other"


def _safe_int(val):
    """Convert to int safely."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _cost_cents(settlement, side):
    """Extract cost in cents from a settlement record.

    Kalshi API v2 uses dollar-string fields (yes_total_cost_dollars,
    no_total_cost_dollars) instead of cent-integer fields.  Fall back to
    the legacy cent-integer field if the dollar field is missing.
    """
    dollar_key = f"{side}_total_cost_dollars"
    cent_key = f"{side}_total_cost"
    if dollar_key in settlement:
        try:
            return round(float(settlement[dollar_key]) * 100)
        except (TypeError, ValueError):
            return 0
    return _safe_int(settlement.get(cent_key, 0))


# ─── I/O functions (not tested in unit tests) ───

def _load_local_trades():
    """Load all local trade logs using canonical trade_files list."""
    all_trades = []
    for tf in _CANONICAL_FILES:
        filepath = DATA_DIR / tf["filename"]
        if not filepath.exists():
            continue
        try:
            text = filepath.read_text().strip()
            if not text:
                continue
            trades = json.loads(text)
            if isinstance(trades, list):
                for t in trades:
                    if not t.get("source_bot"):
                        t["source_bot"] = tf["bot"]
                    all_trades.append(t)
        except (json.JSONDecodeError, ValueError, OSError):
            continue
    return all_trades


def _fetch_api_data():
    """Fetch all required data from Kalshi API.

    Raises RuntimeError if balance fetch fails (cannot produce valid snapshot).
    Settlement/fill/position fetches log warnings on failure but continue.
    """
    from kalshi_auth import KalshiClient, _atomic_write_json
    client = KalshiClient()

    # Balance (required — cannot produce snapshot without it)
    balance_data = client.get("/portfolio/balance")
    balance_cents = balance_data.get("balance", 0)
    portfolio_value_cents = balance_data.get("portfolio_value", 0)

    # Settlements (paginated)
    settlements = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/settlements?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = client.get(path)
        except Exception as e:
            print(f"  WARNING: settlement fetch failed: {e}")
            break
        batch = data.get("settlements", [])
        settlements.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    # Fills (paginated)
    fills = []
    cursor = None
    for _ in range(50):
        path = "/portfolio/fills?limit=100"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            data = client.get(path)
        except Exception as e:
            print(f"  WARNING: fills fetch failed: {e}")
            break
        batch = data.get("fills", [])
        fills.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break

    # Positions
    try:
        pos_data = client.get("/portfolio/positions")
        positions = [p for p in pos_data.get("market_positions", [])
                     if float(p.get("position_fp", "0") or p.get("position", 0) or "0") != 0]
    except Exception as e:
        print(f"  WARNING: positions fetch failed: {e}")
        positions = []

    return balance_cents, portfolio_value_cents, settlements, fills, positions


def main():
    parser = argparse.ArgumentParser(
        description="Generate dual-source verified P&L snapshot."
    )
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="Print to stdout instead of writing file")
    parser.add_argument("--pull-s3", action="store_true",
                        help="Pull trade logs from S3 before generating snapshot")
    args = parser.parse_args()

    if args.pull_s3:
        print("Pulling trade logs from S3...")
        subprocess.run(
            ["bash", str(PROJECT_DIR / "scripts" / "s3-sync.sh"), "download"],
            check=True,
        )

    print("Fetching data from Kalshi API...")
    balance, portfolio_value, settlements, fills, positions = _fetch_api_data()
    print(f"  {len(settlements)} settlements, {len(fills)} fills, {len(positions)} open positions")

    print("Loading local trade logs...")
    local_trades = _load_local_trades()
    print(f"  {len(local_trades)} local trade records")

    deposits_path = DEPOSITS_PATH if DEPOSITS_PATH.exists() else None

    snapshot = build_snapshot(
        balance_cents=balance,
        portfolio_value_cents=portfolio_value,
        settlements=settlements,
        fills=fills,
        positions=positions,
        local_trades=local_trades,
        deposits_path=deposits_path,
    )

    if args.print_only:
        print(json.dumps(snapshot, indent=2))
    else:
        from kalshi_auth import _atomic_write_json
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(SNAPSHOT_PATH, snapshot)
        print(f"Snapshot written to {SNAPSHOT_PATH}")

        # Print verification summary
        v = snapshot["verification"]
        status = v["status"].upper()
        print(f"\nVerification: {status}")
        for c in v["checks"]:
            icon = "+" if c["status"] == "ok" else "!" if c["status"] == "warning" else "x"
            detail = f" -- {c.get('detail', '')}" if c.get("detail") else ""
            print(f"  {icon} {c['check']}{detail}")

        pnl = snapshot["realized_pnl"]
        print(f"\nRealized P&L: ${pnl['total_cents'] / 100:+.2f} "
              f"({pnl['wins']}W/{pnl['losses']}L, "
              f"{pnl['win_rate'] * 100:.1f}% WR)")
        print(f"Fees: ${pnl['total_fees_cents'] / 100:.2f}")
        print(f"NAV: ${snapshot['account']['nav_cents'] / 100:.2f}")

        bc = snapshot.get("balance_check", {})
        if bc.get("available"):
            print(f"\nBalance Check (ground truth):")
            print(f"  Deposits: ${bc['net_funded_cents'] / 100:.2f}")
            print(f"  True Total P&L: ${bc['true_total_pnl_cents'] / 100:+.2f}")
            print(f"  Realized (net): ${bc['realized_net_cents'] / 100:+.2f}")
            print(f"  Implied Unrealized: ${bc['implied_unrealized_cents'] / 100:+.2f}")
            if snapshot["deposits"].get("roi_pct") is not None:
                print(f"  ROI: {snapshot['deposits']['roi_pct']:+.2f}%")


if __name__ == "__main__":
    main()
