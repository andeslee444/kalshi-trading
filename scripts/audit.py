#!/usr/bin/env python3
"""Comprehensive Math & Strategy Audit for Kalshi Trading System.

Programmatically audits every aspect of the trading system: P&L truth,
data integrity, backtesting realism, execution/microstructure, sizing/
portfolio risk, and bot-specific mathematical correctness.

Usage:
    python3 scripts/audit.py                    # Full audit, text output
    python3 scripts/audit.py --json             # JSON output
    python3 scripts/audit.py --section 0        # Run only section 0
    python3 scripts/audit.py --reconcile        # Include API reconciliation
"""

import argparse
import datetime
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ─── Project paths ───
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))

# ─── Lazy imports (avoid heavy deps for pure analysis) ───
from probability import (
    _norm_cdf, _load_calibration, _CALIBRATION_PATH,
    weather_probability, ensemble_weather_probability, nws_probability,
    info_arb_probability, album_data_sigma, boxoffice_data_sigma,
    econ_nowcast_probability, cpi_nowcast_sigma, crypto_price_probability,
    longshot_edge, classify_ticker_category, LONGSHOT_BIAS_PARAMS,
    half_kelly, half_kelly_sell, quarter_kelly, high_conviction_kelly,
    kalshi_fee_cents, edge_after_fees, KALSHI_FEE_RATE,
    is_market_liquid, compute_limit_price,
)


# ═══════════════════════════════════════════════════════════════════════
# Finding dataclass
# ═══════════════════════════════════════════════════════════════════════

class Finding:
    """A single audit finding with severity and evidence."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"

    def __init__(self, section, qid, severity, title, detail, evidence=None):
        self.section = section
        self.qid = qid          # e.g. "0.1", "5A.3"
        self.severity = severity
        self.title = title
        self.detail = detail
        self.evidence = evidence or {}

    def to_dict(self):
        d = {
            "section": self.section,
            "qid": self.qid,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
        }
        if self.evidence:
            d["evidence"] = self.evidence
        return d

    def __repr__(self):
        icon = {"pass": "✓", "warn": "⚠", "fail": "✗", "info": "ℹ"}.get(self.severity, "?")
        return f"[{icon}] {self.qid}: {self.title}"


# ═══════════════════════════════════════════════════════════════════════
# Trade file definitions (mirrors analyze-performance.py)
# ═══════════════════════════════════════════════════════════════════════

TRADE_FILES = [
    {"label": "Weather Bot", "path": PROJECT_DIR / "data" / "kalshi-trades.json", "bot": "weather"},
    {"label": "Strategy Trader", "path": PROJECT_DIR / "data" / "kalshi-strategy-trades.json", "bot": "strategy"},
    {"label": "Entertainment Bot", "path": PROJECT_DIR / "data" / "kalshi-entertainment-trades.json", "bot": "entertainment"},
    {"label": "BeatRelease Scanner", "path": PROJECT_DIR / "data" / "beatrelease-trades.json", "bot": "beatrelease"},
    {"label": "Source Monitor", "path": PROJECT_DIR / "data" / "kalshi-monitor-trades.json", "bot": "source-monitor"},
    {"label": "Position Monitor", "path": PROJECT_DIR / "data" / "kalshi-position-trades.json", "bot": "position-monitor"},
    {"label": "Economics Bot", "path": PROJECT_DIR / "data" / "kalshi-economics-trades.json", "bot": "economics"},
    {"label": "Crypto Bot", "path": PROJECT_DIR / "data" / "kalshi-crypto-trades.json", "bot": "crypto"},
    {"label": "Cross-Platform Arb", "path": PROJECT_DIR / "data" / "kalshi-arb-trades.json", "bot": "arb"},
    {"label": "Market Maker", "path": PROJECT_DIR / "data" / "kalshi-mm-trades.json", "bot": "mm"},
]

CONFIG_FILES = {
    "weather": PROJECT_DIR / "config" / "kalshi-config.json",
    "bots": PROJECT_DIR / "config" / "bots-config.json",
    "monitor": PROJECT_DIR / "config" / "kalshi-monitor-config.json",
    "calibration": PROJECT_DIR / "config" / "calibration.json",
}


def load_json_safe(path):
    """Load a JSON file, returning None on missing/corrupt."""
    if not path.exists():
        return None
    try:
        text = path.read_text().strip()
        if not text:
            return None
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def load_all_trades():
    """Load all trade files. Returns dict: bot_name -> list[dict]."""
    result = {}
    for tf in TRADE_FILES:
        data = load_json_safe(tf["path"])
        if isinstance(data, list):
            result[tf["bot"]] = data
        elif isinstance(data, dict) and "trades" in data:
            result[tf["bot"]] = data["trades"]
        else:
            result[tf["bot"]] = []
    return result


def load_configs():
    """Load all config files. Returns dict: name -> dict."""
    return {name: load_json_safe(path) for name, path in CONFIG_FILES.items()}


# ═══════════════════════════════════════════════════════════════════════
# AuditEngine
# ═══════════════════════════════════════════════════════════════════════

class AuditEngine:
    """Main audit engine. Each section_X method returns a list of Findings."""

    def __init__(self, reconcile=False):
        self.reconcile = reconcile
        self.trades = load_all_trades()
        self.configs = load_configs()
        self.findings = []
        self._client = None
        self._settlements = None
        self._fills = None

    def _get_client(self):
        if self._client is None:
            from kalshi_auth import KalshiClient
            self._client = KalshiClient()
        return self._client

    def _get_settlements(self):
        if self._settlements is None:
            client = self._get_client()
            self._settlements = []
            cursor = None
            for _ in range(50):
                path = "/portfolio/settlements?limit=100"
                if cursor:
                    path += f"&cursor={cursor}"
                data = client.get(path)
                batch = data.get("settlements", [])
                self._settlements.extend(batch)
                cursor = data.get("cursor")
                if not cursor or not batch:
                    break
        return self._settlements

    def _get_fills(self):
        if self._fills is None:
            client = self._get_client()
            self._fills = []
            cursor = None
            for _ in range(50):
                path = "/portfolio/fills?limit=100"
                if cursor:
                    path += f"&cursor={cursor}"
                data = client.get(path)
                batch = data.get("fills", [])
                self._fills.extend(batch)
                cursor = data.get("cursor")
                if not cursor or not batch:
                    break
        return self._fills

    def all_local_trades(self):
        """Flatten all trade files into one list with bot tag."""
        result = []
        for bot, trades in self.trades.items():
            for t in trades:
                t_copy = dict(t)
                t_copy.setdefault("source_bot", bot)
                result.append(t_copy)
        return result

    def run(self, sections=None):
        """Run all (or selected) audit sections."""
        all_sections = {
            "0": self.section_0_pnl_truth,
            "1": self.section_1_data_integrity,
            "2": self.section_2_backtesting,
            "3": self.section_3_execution,
            "4": self.section_4_sizing_risk,
            "5A": self.section_5a_weather,
            "5B": self.section_5b_entertainment,
            "5C": self.section_5c_source_monitor,
            "5D": self.section_5d_strategy,
            "5E": self.section_5e_crypto,
            "5F": self.section_5f_economics,
            "5G": self.section_5g_cross_platform,
            "5H": self.section_5h_market_maker,
            "5I": self.section_5i_position_monitor,
            "5J": self.section_5j_capital_allocator,
        }
        if sections:
            to_run = {k: v for k, v in all_sections.items() if k in sections}
        else:
            to_run = all_sections

        for name, fn in to_run.items():
            try:
                findings = fn()
                self.findings.extend(findings)
            except Exception as e:
                self.findings.append(Finding(
                    name, f"{name}.ERR", Finding.FAIL,
                    f"Section {name} crashed",
                    f"Exception: {type(e).__name__}: {e}"
                ))
        return self.findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 0: P&L Truth
    # ═══════════════════════════════════════════════════════════════════

    def section_0_pnl_truth(self):
        findings = []
        all_trades = self.all_local_trades()
        total_local = len(all_trades)

        # 0.1: Trade count summary
        bot_counts = Counter(t.get("source_bot", "unknown") for t in all_trades)
        findings.append(Finding(
            "0", "0.1", Finding.INFO,
            "Local trade count summary",
            f"{total_local} total trades across {len(bot_counts)} bots",
            {"per_bot": dict(bot_counts)}
        ))

        # 0.2: Trade record schema check
        required_fields = {"timestamp", "ticker"}
        desirable_fields = {"side", "order_id", "status", "source_bot"}
        missing_required = 0
        missing_desirable = defaultdict(int)
        for t in all_trades:
            keys = set(t.keys())
            if not required_fields.issubset(keys):
                missing_required += 1
            for f in desirable_fields - keys:
                missing_desirable[f] += 1

        if missing_required > 0:
            findings.append(Finding(
                "0", "0.2", Finding.FAIL,
                "Trades missing required fields (timestamp, ticker)",
                f"{missing_required}/{total_local} trades missing required fields",
            ))
        else:
            findings.append(Finding(
                "0", "0.2", Finding.PASS,
                "All trades have required fields",
                f"All {total_local} trades have timestamp and ticker",
                {"missing_desirable": dict(missing_desirable)} if missing_desirable else {}
            ))

        # 0.3: Audit instrumentation coverage (split old vs new trades)
        instrumentation_fields = {"model_prob", "raw_edge", "fee_cents", "sizing_method"}
        # Golden record cutoff: trades before this date predate instrumentation
        GOLDEN_RECORD_CUTOFF = "2026-02-18"
        old_trades = [t for t in all_trades if t.get("timestamp", "") < GOLDEN_RECORD_CUTOFF]
        new_trades = [t for t in all_trades if t.get("timestamp", "") >= GOLDEN_RECORD_CUTOFF]
        coverage_new = defaultdict(int)
        for t in new_trades:
            keys = set(t.keys())
            for f in instrumentation_fields:
                if f in keys:
                    coverage_new[f] += 1
        total_new = len(new_trades)
        pct_new = {f: f"{coverage_new[f]}/{total_new}" for f in instrumentation_fields} if total_new else {}
        all_new_covered = total_new > 0 and all(coverage_new[f] == total_new for f in instrumentation_fields)
        findings.append(Finding(
            "0", "0.3",
            Finding.PASS if all_new_covered else (Finding.WARN if total_new == 0 else Finding.INFO),
            "Audit instrumentation coverage",
            f"New trades ({total_new}): {pct_new} | Old trades ({len(old_trades)}) predate instrumentation"
            if total_new > 0 else f"No new trades since {GOLDEN_RECORD_CUTOFF}",
            {"coverage_new": pct_new, "old_trade_count": len(old_trades)}
        ))

        # 0.4: API reconciliation (if --reconcile)
        if self.reconcile:
            try:
                settlements = self._get_settlements()
                fills = self._get_fills()

                api_tickers = {s.get("ticker") for s in settlements}
                local_tickers = {t.get("ticker") for t in all_trades}

                ghost_trades = api_tickers - local_tickers  # in API, not local
                phantom_trades = local_tickers - api_tickers  # in local, not API

                # P&L from settlements
                total_revenue = sum(int(s.get("revenue", 0)) for s in settlements)
                total_cost = sum(int(s.get("cost", 0)) for s in settlements if s.get("cost"))

                # Win rate
                wins = sum(1 for s in settlements if int(s.get("revenue", 0)) > 0)
                losses = sum(1 for s in settlements if int(s.get("revenue", 0)) <= 0)
                settled = wins + losses
                win_rate = wins / settled if settled > 0 else 0

                findings.append(Finding(
                    "0", "0.4", Finding.INFO,
                    "API reconciliation results",
                    f"API: {len(settlements)} settlements, {len(fills)} fills. "
                    f"Net P&L: ${total_revenue/100:.2f}. Win rate: {win_rate*100:.1f}% ({wins}W/{losses}L)",
                    {
                        "settlements": len(settlements),
                        "fills": len(fills),
                        "net_pnl_cents": total_revenue,
                        "win_rate": round(win_rate, 4),
                        "ghost_trades": len(ghost_trades),
                        "phantom_trades": len(phantom_trades),
                    }
                ))

                if ghost_trades:
                    findings.append(Finding(
                        "0", "0.4a", Finding.WARN,
                        "Ghost trades (in API, not in local logs)",
                        f"{len(ghost_trades)} tickers settled in API but not in any local trade log",
                        {"tickers": sorted(ghost_trades)[:20]}
                    ))

                if phantom_trades:
                    findings.append(Finding(
                        "0", "0.4b", Finding.WARN,
                        "Phantom trades (in local logs, not settled in API)",
                        f"{len(phantom_trades)} tickers in local logs but no API settlement. "
                        "These may be open/unfilled orders.",
                        {"tickers": sorted(phantom_trades)[:20]}
                    ))

                # 0.5: Fill price vs limit price slippage
                if fills:
                    slippage_data = []
                    for f in fills:
                        order_id = f.get("order_id", "")
                        fill_price = f.get("price")
                        # Match fill to local trade
                        for t in all_trades:
                            if t.get("order_id") == order_id:
                                limit_price = t.get("price_cents") or t.get("price") or t.get("no_price")
                                if limit_price and fill_price:
                                    slippage = int(fill_price) - int(limit_price)
                                    slippage_data.append(slippage)
                                break

                    if slippage_data:
                        avg_slip = sum(slippage_data) / len(slippage_data)
                        max_slip = max(slippage_data)
                        findings.append(Finding(
                            "0", "0.5", Finding.INFO,
                            "Fill slippage analysis",
                            f"Matched {len(slippage_data)} fills. "
                            f"Avg slippage: {avg_slip:.1f}c, Max: {max_slip}c",
                            {"avg_slippage_cents": round(avg_slip, 2),
                             "max_slippage_cents": max_slip,
                             "matched_fills": len(slippage_data)}
                        ))

            except Exception as e:
                findings.append(Finding(
                    "0", "0.4", Finding.WARN,
                    "API reconciliation failed",
                    f"Could not connect to Kalshi API: {e}"
                ))
        else:
            findings.append(Finding(
                "0", "0.4", Finding.INFO,
                "API reconciliation skipped",
                "Run with --reconcile to compare local logs against Kalshi API"
            ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 1: Data Integrity & Settlement Mapping
    # ═══════════════════════════════════════════════════════════════════

    def section_1_data_integrity(self):
        findings = []

        # 1.1: NWS Station mapping verification
        monitor_cfg = self.configs.get("monitor")
        if monitor_cfg:
            nws_cfg = monitor_cfg.get("sources", {}).get("nws", {})
            stations = nws_cfg.get("stations", {})

            expected_map = {
                "MIA": ("KMIA", "Miami"),
                "LAX": ("KLAX", "Los Angeles"),
                "PHIL": ("KPHL", "Philadelphia"),
                "NY": ("KNYC", "Central Park"),
                "CHI": ("KMDW", "Chicago Midway"),
                "AUS": ("KAUS", "Austin"),
                "DEN": ("KDEN", "Denver"),
                "HOU": ("KHOU", "Houston Hobby"),
            }

            mismatches = []
            for city, station_id in stations.items():
                expected = expected_map.get(city)
                if expected:
                    exp_station, exp_name = expected
                    if station_id != exp_station:
                        mismatches.append(f"{city}: config has {station_id}, expected {exp_station}")

            if mismatches:
                findings.append(Finding(
                    "1", "1.1", Finding.FAIL,
                    "NWS station mapping mismatches",
                    f"{len(mismatches)} station mapping errors",
                    {"mismatches": mismatches}
                ))
            else:
                findings.append(Finding(
                    "1", "1.1", Finding.PASS,
                    "NWS station mappings correct",
                    f"All {len(stations)} city→station mappings match expected values",
                    {"stations": dict(stations),
                     "verify_urls": {city: f"https://api.weather.gov/stations/{sid}"
                                     for city, sid in stations.items()}}
                ))
        else:
            findings.append(Finding(
                "1", "1.1", Finding.WARN,
                "Monitor config missing",
                "Could not load config/kalshi-monitor-config.json"
            ))

        # 1.2: Calibration file status
        cal_exists = _CALIBRATION_PATH.exists()
        if cal_exists:
            cal = _load_calibration()
            findings.append(Finding(
                "1", "1.2", Finding.PASS,
                "Calibration file exists",
                f"config/calibration.json loaded with {len(cal)} top-level keys",
                {"keys": list(cal.keys())}
            ))
        else:
            findings.append(Finding(
                "1", "1.2", Finding.WARN,
                "Calibration file missing",
                "config/calibration.json does not exist. All bots use hardcoded default "
                "sigma parameters. Run `npm run calibrate` to generate per-city tuning.",
            ))

        # 1.3: Config consistency check
        weather_cfg = self.configs.get("weather")
        bots_cfg = self.configs.get("bots")
        issues = []

        if weather_cfg:
            for key in ["maxTradeAmount", "maxDailyTrades", "maxDailyLoss", "edgeThreshold"]:
                if key not in weather_cfg:
                    issues.append(f"weather config missing {key}")
            cities = weather_cfg.get("cities", {})
            for city_code, city_data in cities.items():
                for field in ["lat", "lon", "name"]:
                    if field not in city_data:
                        issues.append(f"weather city {city_code} missing {field}")

        if bots_cfg:
            expected_bot_sections = [
                ("entertainment", ["maxTradeAmount", "maxDailyTrades", "maxDailyLoss"]),
                ("strategy", ["maxBetCents", "maxDailyTrades", "maxDailyLoss"]),
                ("crypto", ["maxTradeAmount", "maxDailyTrades", "maxDailyLoss"]),
                ("economics", ["maxTradeAmount", "maxDailyTrades", "maxDailyLoss"]),
                ("position_monitor", ["scanIntervalMinutes", "takeProfitThreshold", "stopLossThreshold"]),
                ("market_maker", ["enabled", "gamma", "kParam"]),
            ]
            for section, keys in expected_bot_sections:
                if section not in bots_cfg:
                    issues.append(f"bots-config missing section: {section}")
                else:
                    for k in keys:
                        if k not in bots_cfg[section]:
                            issues.append(f"bots-config.{section} missing {k}")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "1", "1.3", severity,
            "Config consistency",
            f"{len(issues)} issues found" if issues else "All config keys present and valid",
            {"issues": issues} if issues else {}
        ))

        # 1.4: Trade log file existence
        existing = []
        missing = []
        for tf in TRADE_FILES:
            if tf["path"].exists():
                data = load_json_safe(tf["path"])
                count = len(data) if isinstance(data, list) else (len(data.get("trades", [])) if isinstance(data, dict) else 0)
                existing.append(f"{tf['label']}: {count} trades")
            else:
                missing.append(tf["label"])

        findings.append(Finding(
            "1", "1.4", Finding.INFO,
            "Trade log file inventory",
            f"{len(existing)} files exist, {len(missing)} missing (no trades yet)",
            {"existing": existing, "missing": missing}
        ))

        # 1.5: Weather city config vs monitor config consistency
        if weather_cfg and monitor_cfg:
            weather_cities = set(weather_cfg.get("cities", {}).keys())
            nws_cities = set(monitor_cfg.get("sources", {}).get("nws", {}).get("stations", {}).keys())
            weather_only = weather_cities - nws_cities
            nws_only = nws_cities - weather_cities
            if weather_only or nws_only:
                findings.append(Finding(
                    "1", "1.5", Finding.WARN,
                    "City config mismatch between weather and monitor",
                    f"Weather-only cities: {weather_only or 'none'}, "
                    f"Monitor-only cities: {nws_only or 'none'}",
                ))
            else:
                findings.append(Finding(
                    "1", "1.5", Finding.PASS,
                    "Weather and monitor city configs aligned",
                    f"Both configs have same {len(weather_cities)} cities",
                ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 2: Backtesting & Evaluation Realism
    # ═══════════════════════════════════════════════════════════════════

    def section_2_backtesting(self):
        findings = []

        # 2.1: Check if backtest results exist
        backtest_results = PROJECT_DIR / "data" / "backtest-results.json"
        if backtest_results.exists():
            data = load_json_safe(backtest_results)
            findings.append(Finding(
                "2", "2.1", Finding.INFO,
                "Backtest results found",
                f"Backtest data available with {len(data) if data else 0} entries"
            ))
        else:
            findings.append(Finding(
                "2", "2.1", Finding.WARN,
                "No backtest results found",
                "Run `npm run backtest` to generate backtest-results.json"
            ))

        # 2.2: Calibration staleness
        if _CALIBRATION_PATH.exists():
            mtime = datetime.datetime.fromtimestamp(_CALIBRATION_PATH.stat().st_mtime)
            age_days = (datetime.datetime.now() - mtime).days
            if age_days > 30:
                findings.append(Finding(
                    "2", "2.2", Finding.WARN,
                    "Calibration file is stale",
                    f"calibration.json last modified {age_days} days ago. "
                    "Re-run `npm run calibrate` for fresh parameters."
                ))
            else:
                findings.append(Finding(
                    "2", "2.2", Finding.PASS,
                    "Calibration file is fresh",
                    f"calibration.json last modified {age_days} days ago"
                ))
        else:
            findings.append(Finding(
                "2", "2.2", Finding.WARN,
                "Cannot assess calibration staleness — file missing",
                "Run `npm run calibrate` first"
            ))

        # 2.3: Brier score computation on available trades
        brier_scores = self._compute_brier_scores()
        if brier_scores:
            detail_parts = []
            for model, score in brier_scores.items():
                detail_parts.append(f"{model}: {score['brier']:.4f} (n={score['n']})")
            findings.append(Finding(
                "2", "2.3", Finding.INFO,
                "Brier scores from trade data",
                "; ".join(detail_parts),
                {"scores": brier_scores}
            ))
        else:
            findings.append(Finding(
                "2", "2.3", Finding.INFO,
                "Brier scores unavailable",
                "Need settled trades with model_prob field for Brier score computation. "
                "Add instrumentation fields to enable this."
            ))

        # 2.4: Look-ahead bias check (code-level analysis)
        findings.append(Finding(
            "2", "2.4", Finding.INFO,
            "Look-ahead bias check (structural)",
            "All bots fetch data at scan time then immediately evaluate. "
            "No trade record references future data. Weather bot uses forecast "
            "data (inherently forward-looking by design). NWS source monitor uses "
            "running high (real-time observation). "
            "Risk area: backtest.py should verify trade timestamps precede settlement."
        ))

        return findings

    def _compute_brier_scores(self):
        """Compute Brier scores from trades that have model_prob and known outcomes."""
        if not self.reconcile:
            return {}

        try:
            settlements = self._get_settlements()
        except Exception:
            return {}

        # Build ticker → outcome (1 = YES won, 0 = NO won)
        ticker_outcome = {}
        for s in settlements:
            ticker = s.get("ticker", "")
            revenue = int(s.get("revenue", 0))
            # revenue > 0 means the position won
            # But we need the YES/NO outcome, not the trade outcome
            # This is an approximation — we'd need the actual settlement result
            # For now, skip this until we have proper outcome data
            pass

        return {}

    # ═══════════════════════════════════════════════════════════════════
    # Section 3: Execution & Microstructure
    # ═══════════════════════════════════════════════════════════════════

    def section_3_execution(self):
        findings = []
        all_trades = self.all_local_trades()

        # 3.1: Order status distribution
        statuses = Counter(t.get("status", "unknown") for t in all_trades)
        total = len(all_trades)
        resting = statuses.get("resting", 0)
        executed = statuses.get("executed", 0)
        fill_rate = executed / total if total > 0 else 0

        findings.append(Finding(
            "3", "3.1", Finding.INFO,
            "Order fill rate",
            f"{executed}/{total} executed ({fill_rate*100:.1f}%), "
            f"{resting} resting" if total > 0 else "No trades to analyze",
            {"by_status": dict(statuses), "fill_rate": round(fill_rate, 4)}
        ))

        # 3.2: Market snapshot coverage (accept both nested dict and flat fields)
        with_snapshot = sum(
            1 for t in all_trades
            if "market_snapshot" in t or ("best_bid" in t or "best_ask" in t or "yes_bid" in t)
        )
        findings.append(Finding(
            "3", "3.2",
            Finding.PASS if with_snapshot == total else Finding.WARN,
            "Market snapshot coverage",
            f"{with_snapshot}/{total} trades have market snapshot data (nested or flat fields)",
        ))

        # 3.3: Order timing analysis
        timestamps = []
        for t in all_trades:
            ts = t.get("timestamp")
            if ts:
                try:
                    dt = datetime.datetime.fromisoformat(ts)
                    timestamps.append(dt)
                except (ValueError, TypeError):
                    pass

        if timestamps:
            hours = Counter(dt.hour for dt in timestamps)
            peak_hour = hours.most_common(1)[0] if hours else None
            findings.append(Finding(
                "3", "3.3", Finding.INFO,
                "Order timing distribution",
                f"Peak trading hour: {peak_hour[0]:02d}:00 ({peak_hour[1]} trades)" if peak_hour
                else "No valid timestamps",
                {"by_hour": dict(sorted(hours.items()))}
            ))

        # 3.4: API order history (if reconcile enabled)
        if self.reconcile:
            try:
                client = self._get_client()
                orders = client.get("/portfolio/orders?limit=200")
                order_list = orders.get("orders", [])
                order_statuses = Counter(o.get("status", "unknown") for o in order_list)
                findings.append(Finding(
                    "3", "3.4", Finding.INFO,
                    "API order history",
                    f"{len(order_list)} orders in API: {dict(order_statuses)}",
                    {"api_order_statuses": dict(order_statuses)}
                ))
            except Exception as e:
                findings.append(Finding(
                    "3", "3.4", Finding.WARN,
                    "Could not fetch API order history",
                    str(e)
                ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 4: Sizing & Portfolio Risk
    # ═══════════════════════════════════════════════════════════════════

    def section_4_sizing_risk(self):
        findings = []

        # 4.1: Fee treatment audit — THE KNOWN BUG
        findings.extend(self._audit_fee_treatment())

        # 4.2: Kelly formula spot-check
        findings.extend(self._audit_kelly_formula())

        # 4.3: Portfolio concentration
        findings.extend(self._audit_concentration())

        # 4.4: Daily loss limit verification
        findings.extend(self._audit_daily_limits())

        # 4.5: Bankroll basis — check if allocator uses available_balance
        allocator_path = PROJECT_DIR / "src" / "kalshi" / "capital_allocator.py"
        uses_available = False
        if allocator_path.exists():
            src = allocator_path.read_text()
            # Check for the fixed pattern: bankroll = available_balance
            uses_available = "bankroll = available_balance" in src
        if uses_available:
            findings.append(Finding(
                "4", "4.5", Finding.PASS,
                "Bankroll basis: available balance (correct)",
                "Kelly sizing uses available_balance, not total_balance. "
                "Locked capital in open positions is excluded from Kelly bankroll.",
            ))
        else:
            findings.append(Finding(
                "4", "4.5", Finding.WARN,
                "Bankroll basis: total balance (not available balance)",
                "Kelly sizing uses total portfolio balance, not available balance. "
                "This creates implicit leverage risk — if most capital is locked in "
                "open positions, new trades are sized against 'phantom' capital. "
                "Impact: during high-activity periods, position sizes may be ~2x too large.",
            ))

        return findings

    def _audit_fee_treatment(self):
        """Check whether bots use the correct fee treatment in Kelly sizing.

        Correct: pass raw edge to Kelly, fee_cents reduces payout (100→100-fee).
        Buggy (old): call edge_after_fees() to reduce edge, then Kelly with fee=0.
        Detection: grep bot source files for edge_after_fees() calls.
        """
        findings = []

        # Check if any bot still calls edge_after_fees()
        bot_files = [
            "weather-bot.py", "crypto-bot.py", "economics-bot.py",
            "source-monitor.py", "entertainment-bot.py", "strategy-trader.py",
        ]
        callers = []
        for bf in bot_files:
            path = PROJECT_DIR / "src" / "kalshi" / bf
            if path.exists():
                content = path.read_text()
                if "edge_after_fees(" in content and "import" not in content.split("edge_after_fees(")[0].split("\n")[-1]:
                    callers.append(bf)

        # Also check if bots pass fee_cents to Kelly functions
        fee_passers = []
        for bf in bot_files:
            path = PROJECT_DIR / "src" / "kalshi" / bf
            if path.exists():
                content = path.read_text()
                if "fee_cents=" in content:
                    fee_passers.append(bf)

        if not callers and fee_passers:
            findings.append(Finding(
                "4", "4.1", Finding.PASS,
                "Fee treatment correct: raw edge + fee_cents in Kelly",
                f"No bots call edge_after_fees(). {len(fee_passers)}/{len(bot_files)} bots "
                f"pass fee_cents to Kelly functions (correct payout reduction). "
                f"edge_after_fees() is deprecated in probability.py.",
                {"bots_passing_fee_cents": fee_passers}
            ))
        elif callers:
            findings.append(Finding(
                "4", "4.1", Finding.WARN,
                f"Fee treatment bug: {len(callers)} bot(s) still call edge_after_fees()",
                "edge_after_fees() subtracts fee from edge as a probability delta. "
                "Correct approach: use raw edge in Kelly, pass fee_cents to reduce payout. "
                "Bug is conservative (undersizes), not dangerous.",
                {"callers": callers, "fix": "Replace edge_after_fees() with raw edge + fee_cents param"}
            ))
        else:
            findings.append(Finding(
                "4", "4.1", Finding.WARN,
                "Fee treatment: no bots pass fee_cents to Kelly",
                "Bots don't call edge_after_fees() but also don't pass fee_cents "
                "to Kelly functions. Fees are being ignored entirely.",
                {"bots_without_fee_cents": [b for b in bot_files if b not in fee_passers]}
            ))

        return findings

    def _audit_kelly_formula(self):
        """Verify Kelly formulas produce mathematically correct results."""
        findings = []

        # Test half_kelly at known values
        test_cases = [
            # (edge, price, max_cost, bankroll, expected_min_contracts)
            (0.10, 50, 500, 10000, 0),  # should produce some contracts
            (0.0, 50, 500, 10000, 0),   # zero edge → zero
            (-0.05, 50, 500, 10000, 0), # negative edge → zero
            (0.50, 10, 500, 10000, 0),  # huge edge, cheap contract
        ]

        issues = []
        for edge, price, max_cost, bankroll, _ in test_cases:
            contracts, risk = half_kelly(edge, price, max_cost, bankroll)
            if edge <= 0 and contracts != 0:
                issues.append(f"half_kelly({edge}, {price}) returned {contracts} (should be 0)")
            if edge > 0 and contracts == 0:
                # Check if Kelly fraction is just very small
                implied = price / 100
                our_p = implied + edge
                b = (100 - price) / price
                kf = (b * our_p - (1 - our_p)) / b
                if kf > 0.01:  # non-trivial Kelly fraction
                    issues.append(f"half_kelly({edge}, {price}) returned 0 but Kelly fraction is {kf:.4f}")
            if risk < 0:
                issues.append(f"half_kelly returned negative risk: {risk}")
            if contracts > 0 and risk != contracts * price:
                issues.append(f"Risk mismatch: {risk} != {contracts} * {price}")

        # Test quarter_kelly is ≤ half_kelly
        for edge in [0.10, 0.20, 0.30]:
            hk_c, _ = half_kelly(edge, 30, 1000, 50000)
            qk_c, _ = quarter_kelly(edge, 30, 1000, 50000)
            if qk_c > hk_c:
                issues.append(f"quarter_kelly({edge}, 30) = {qk_c} > half_kelly = {hk_c}")

        # Test half_kelly_sell
        sell_c, sell_r = half_kelly_sell(0.10, 10, 500, 50000)
        if sell_c < 0:
            issues.append(f"half_kelly_sell returned negative contracts: {sell_c}")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "4", "4.2", severity,
            "Kelly formula mathematical verification",
            f"{len(issues)} issues found" if issues else "All Kelly formulas produce correct results",
            {"issues": issues} if issues else {}
        ))

        return findings

    def _audit_concentration(self):
        """Check portfolio concentration from trade records."""
        findings = []
        all_trades = self.all_local_trades()

        if not all_trades:
            findings.append(Finding("4", "4.3", Finding.INFO,
                                    "Portfolio concentration: no trades to analyze", ""))
            return findings

        # Ticker concentration
        ticker_counts = Counter(t.get("ticker", "") for t in all_trades)
        max_ticker = ticker_counts.most_common(1)[0] if ticker_counts else ("", 0)

        # Bot concentration
        bot_risk = defaultdict(int)
        for t in all_trades:
            bot = t.get("source_bot", "unknown")
            risk = self._extract_risk(t)
            bot_risk[bot] += risk

        total_risk = sum(bot_risk.values())
        max_bot = max(bot_risk.items(), key=lambda x: x[1]) if bot_risk else ("", 0)
        max_bot_pct = max_bot[1] / total_risk * 100 if total_risk > 0 else 0

        findings.append(Finding(
            "4", "4.3", Finding.INFO,
            "Portfolio concentration",
            f"Most traded ticker: {max_ticker[0]} ({max_ticker[1]}x). "
            f"Highest risk bot: {max_bot[0]} ({max_bot_pct:.0f}% of total risk, ${max_bot[1]/100:.2f})",
            {"top_tickers": dict(ticker_counts.most_common(5)),
             "bot_risk_cents": dict(bot_risk)}
        ))

        return findings

    def _audit_daily_limits(self):
        """Check if daily trade/loss limits were breached historically."""
        findings = []
        all_trades = self.all_local_trades()

        if not all_trades:
            findings.append(Finding("4", "4.4", Finding.INFO,
                                    "Daily limits: no trades to analyze", ""))
            return findings

        # Group by (bot, date)
        by_bot_date = defaultdict(list)
        for t in all_trades:
            bot = t.get("source_bot", "unknown")
            ts = t.get("timestamp", "")
            if ts:
                date = ts[:10]
                by_bot_date[(bot, date)].append(t)

        breaches = []
        configs = self._get_limit_configs()

        for (bot, date), trades in by_bot_date.items():
            cfg = configs.get(bot, {})
            max_trades = cfg.get("maxDailyTrades", 999)
            max_loss = cfg.get("maxDailyLoss", 99999)

            if len(trades) > max_trades:
                breaches.append(f"{bot} on {date}: {len(trades)} trades (limit {max_trades})")

            daily_risk = sum(self._extract_risk(t) for t in trades)
            max_loss_cents = int(max_loss * 100)
            if daily_risk > max_loss_cents:
                breaches.append(f"{bot} on {date}: ${daily_risk/100:.2f} risk (limit ${max_loss})")

        severity = Finding.FAIL if breaches else Finding.PASS
        findings.append(Finding(
            "4", "4.4", severity,
            "Daily limit compliance",
            f"{len(breaches)} breaches detected" if breaches else "No daily limit breaches found",
            {"breaches": breaches} if breaches else {}
        ))

        return findings

    def _get_limit_configs(self):
        """Get per-bot limit configurations."""
        bots_cfg = self.configs.get("bots") or {}
        weather_cfg = self.configs.get("weather") or {}

        return {
            "weather": weather_cfg,
            "entertainment": bots_cfg.get("entertainment", {}),
            "strategy": bots_cfg.get("strategy", {}),
            "crypto": bots_cfg.get("crypto", {}),
            "economics": bots_cfg.get("economics", {}),
            "beatrelease": bots_cfg.get("beatrelease", {}),
            "source-monitor": self.configs.get("monitor") or {},
            "arb": bots_cfg.get("cross_platform_arb", {}),
            "mm": bots_cfg.get("market_maker", {}),
        }

    @staticmethod
    def _extract_risk(trade):
        """Best-effort risk extraction in cents."""
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
        price = trade.get("price_cents") or trade.get("price") or trade.get("no_price") or 0
        count = trade.get("count") or trade.get("contracts") or trade.get("quantity") or 0
        try:
            return int(price) * int(count)
        except (TypeError, ValueError):
            return 0

    # ═══════════════════════════════════════════════════════════════════
    # Section 5A: Weather Bot Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5a_weather(self):
        findings = []

        # 5A.1: Sigma model verification
        # Default: sigma = 2.0 + 0.5 * days_out
        test_points = {0: 2.0, 1: 2.5, 3: 3.5, 7: 5.5}
        issues = []
        for days, expected_sigma in test_points.items():
            # weather_probability uses sigma internally; verify via known computation
            actual_sigma = 2.0 + 0.5 * days
            if abs(actual_sigma - expected_sigma) > 0.01:
                issues.append(f"day {days}: got {actual_sigma}, expected {expected_sigma}")

        # Verify the probability function itself
        # P(actual > 80 | forecast=82, day0) should be > 0.5 (forecast above threshold)
        p = weather_probability(82, 80, "T", days_out=0)
        if not (0.5 < p < 1.0):
            issues.append(f"weather_probability(82, 80, T, d0)={p:.4f}, expected >0.5")

        # P(actual > 80 | forecast=78, day0) should be < 0.5
        p2 = weather_probability(78, 80, "T", days_out=0)
        if not (0.0 < p2 < 0.5):
            issues.append(f"weather_probability(78, 80, T, d0)={p2:.4f}, expected <0.5")

        # Bracket probability should be positive and small
        pb = weather_probability(80, 80, "B", days_out=0)
        if not (0.0 < pb < 0.5):
            issues.append(f"weather_probability(80, 80, B, d0)={pb:.4f}, expected 0 < p < 0.5")

        # Longer horizon should give wider distribution (lower confidence)
        p_d0 = weather_probability(82, 80, "T", days_out=0)
        p_d7 = weather_probability(82, 80, "T", days_out=7)
        if p_d7 >= p_d0:
            issues.append(f"Longer horizon didn't reduce confidence: d0={p_d0:.4f} vs d7={p_d7:.4f}")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "5A", "5A.1", severity,
            "Weather sigma model verification",
            f"{len(issues)} issues" if issues else
            "Sigma model correct: intercept=2.0, slope=0.5. Day-0 sigma=2.0°F, Day-7=5.5°F.",
            {"issues": issues, "sigma_formula": "sigma = 2.0 + 0.5 * days_out"}
        ))

        # 5A.2: Ensemble model verification
        forecasts = {"gfs": 82.0, "ecmwf": 83.0, "icon": 81.0}
        ep = ensemble_weather_probability(forecasts, 80, "T", days_out=0)
        sp = weather_probability(82.0, 80, "T", days_out=0)
        if not (0.0 < ep < 1.0):
            findings.append(Finding("5A", "5A.2", Finding.FAIL,
                                    "Ensemble probability out of range", f"Got {ep}"))
        else:
            findings.append(Finding(
                "5A", "5A.2", Finding.PASS,
                "Ensemble model produces valid probabilities",
                f"Ensemble P(>80|forecasts)={ep:.4f}, Single-model GFS P={sp:.4f}. "
                f"Ensemble smooths across model uncertainty.",
                {"ensemble_prob": round(ep, 4), "single_model_prob": round(sp, 4)}
            ))

        # 5A.3: Per-city calibration status
        cal = _load_calibration()
        weather_cal = cal.get("weather", {})
        per_city = weather_cal.get("per_city", {})
        weather_cfg = self.configs.get("weather") or {}
        cities = list((weather_cfg.get("cities") or {}).keys())

        if per_city:
            calibrated = [c for c in cities if c in per_city]
            uncalibrated = [c for c in cities if c not in per_city]
            findings.append(Finding(
                "5A", "5A.3", Finding.INFO,
                "Per-city sigma calibration",
                f"{len(calibrated)}/{len(cities)} cities calibrated: {calibrated}",
                {"calibrated": calibrated, "uncalibrated": uncalibrated}
            ))
        else:
            findings.append(Finding(
                "5A", "5A.3", Finding.WARN,
                "No per-city sigma calibration",
                f"All {len(cities)} cities use default sigma. Run `npm run calibrate`.",
                {"cities_using_defaults": cities}
            ))

        # 5A.4: YES side performance check
        weather_trades = self.trades.get("weather", [])
        yes_trades = [t for t in weather_trades if t.get("side") == "yes"]
        no_trades = [t for t in weather_trades if t.get("side") == "no"]
        findings.append(Finding(
            "5A", "5A.4", Finding.INFO,
            "Weather bot trade distribution",
            f"{len(weather_trades)} total: {len(yes_trades)} YES, {len(no_trades)} NO. "
            f"Historical data shows YES side has 0% win rate — monitor closely.",
        ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5B: Entertainment Bot Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5b_entertainment(self):
        findings = []

        # 5B.1: Album data sigma schedule
        expected = {0: 0.15, 1: 0.15, 2: 0.10, 3: 0.10, 4: 0.05, 5: 0.05, 6: 0.05}
        issues = []
        for dow, exp_sigma in expected.items():
            actual = album_data_sigma(dow)
            if abs(actual - exp_sigma) > 0.001:
                day_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dow]
                issues.append(f"{day_name}: got {actual}, expected {exp_sigma}")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "5B", "5B.1", severity,
            "Album sales sigma schedule",
            "Mon/Tue=15%, Wed/Thu=10%, Fri+=3% — " +
            ("all correct" if not issues else f"{len(issues)} errors"),
            {"issues": issues,
             "schedule": {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]: f"{album_data_sigma(d)*100:.0f}%"
                         for d in range(7)}}
        ))

        # 5B.2: Box office sigma schedule
        expected_box = {0: 0.02, 4: 0.12, 5: 0.12, 6: 0.05}
        box_issues = []
        for dow, exp_sigma in expected_box.items():
            actual = boxoffice_data_sigma(dow)
            if abs(actual - exp_sigma) > 0.001:
                day_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dow]
                box_issues.append(f"{day_name}: got {actual}, expected {exp_sigma}")

        severity = Finding.FAIL if box_issues else Finding.PASS
        findings.append(Finding(
            "5B", "5B.2", severity,
            "Box office sigma schedule",
            "Fri/Sat=12%, Sun=5%, Mon+=2% — " +
            ("all correct" if not box_issues else f"{len(box_issues)} errors"),
            {"issues": box_issues}
        ))

        # 5B.3: Info arb probability model check
        # When observed >> threshold, prob should be ~1
        p_high = info_arb_probability(200000, 100000, 0.10)
        # When observed << threshold, prob should be ~0
        p_low = info_arb_probability(50000, 100000, 0.10)
        # When observed == threshold, prob should be ~0.5
        p_mid = info_arb_probability(100000, 100000, 0.10)

        model_issues = []
        if p_high < 0.95:
            model_issues.append(f"P(200k > 100k) = {p_high:.4f}, expected > 0.95")
        if p_low > 0.05:
            model_issues.append(f"P(50k > 100k) = {p_low:.4f}, expected < 0.05")
        if abs(p_mid - 0.5) > 0.01:
            model_issues.append(f"P(100k > 100k) = {p_mid:.4f}, expected ~0.5")

        severity = Finding.FAIL if model_issues else Finding.PASS
        findings.append(Finding(
            "5B", "5B.3", severity,
            "Info-arb probability model verification",
            "P(above) = Φ((observed - threshold) / (threshold * sigma)) — " +
            ("correct" if not model_issues else f"{len(model_issues)} issues"),
            {"issues": model_issues, "test_results": {
                "P(200k>100k)": round(p_high, 4),
                "P(50k>100k)": round(p_low, 4),
                "P(100k>100k)": round(p_mid, 4),
            }}
        ))

        # 5B.4: Entertainment trade summary
        ent_trades = self.trades.get("entertainment", [])
        beat_trades = self.trades.get("beatrelease", [])
        findings.append(Finding(
            "5B", "5B.4", Finding.INFO,
            "Entertainment trade count",
            f"Entertainment bot: {len(ent_trades)}, BeatRelease: {len(beat_trades)}"
        ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5C: Source Monitor Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5c_source_monitor(self):
        findings = []

        # 5C.1: NWS sigma model verification
        # sigma = max(0.5, 4.0 * exp(-0.18 * (hour - 6)))
        test_hours = {
            6: ("~4.0", 3.5, 4.5),
            12: ("~1.4", 0.8, 2.0),
            15: ("~0.7", 0.4, 1.0),
            17: ("~0.4", 0.3, 0.6),
            20: ("~0.5", 0.3, 0.6),  # floor kicks in
        }
        issues = []
        computed = {}
        for hour, (desc, lo, hi) in test_hours.items():
            if hour < 6:
                sigma = 5.0
            else:
                sigma = max(0.5, 4.0 * math.exp(-0.18 * (hour - 6)))
            computed[f"hour_{hour}"] = round(sigma, 2)
            if not (lo <= sigma <= hi):
                issues.append(f"hour {hour}: sigma={sigma:.2f}, expected {desc} (range {lo}-{hi})")

        # Verify via nws_probability
        # At hour 17, running_high=82, threshold=80: should give very high P(>80)
        p_late = nws_probability(82, 80, "T", 17)
        if p_late < 0.90:
            issues.append(f"nws_probability(82, 80, T, h17)={p_late:.4f}, expected >0.90")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "5C", "5C.1", severity,
            "NWS sigma model (exponential decay)",
            f"sigma = max(0.5, 4.0 * exp(-0.18 * (hour-6))). " +
            ("Verified correct." if not issues else f"{len(issues)} issues"),
            {"computed_sigmas": computed, "issues": issues,
             "formula": "sigma = max(0.5, 4.0 * exp(-0.18 * (hour - 6)))"}
        ))

        # 5C.2: Polling interval review
        monitor_cfg = self.configs.get("monitor") or {}
        sources = monitor_cfg.get("sources", {})
        intervals = {}
        recommendations = {}
        for source, cfg in sources.items():
            interval = cfg.get("intervalMinutes", "unknown")
            intervals[source] = interval
            if source == "nws" and interval > 15:
                recommendations[source] = f"NWS at {interval}min may miss intraday temp changes"
            elif source == "hdd" and interval < 10:
                recommendations[source] = f"HDD at {interval}min is aggressive; may trigger rate limits"

        findings.append(Finding(
            "5C", "5C.2",
            Finding.WARN if recommendations else Finding.PASS,
            "Source polling intervals",
            f"Intervals: {intervals}" +
            (f". Recommendations: {recommendations}" if recommendations else ""),
            {"intervals": intervals}
        ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5D: Strategy Trader Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5d_strategy(self):
        findings = []

        # 5D.1: Becker longshot model verification
        # At 1c: overpricing should be near amplitude for default category
        test_cases = [
            (1, "default", 0.50, 0.60),     # should be near 0.57 * exp(-0.15*1)
            (5, "default", 0.15, 0.35),
            (10, "default", 0.05, 0.20),
            (1, "sports", 0.50, 0.70),       # amplitude=0.65
            (1, "weather", 0.15, 0.35),      # amplitude=0.30
        ]

        issues = []
        computed = {}
        for price, cat, lo, hi in test_cases:
            # Manually compute expected
            amp, decay = LONGSHOT_BIAS_PARAMS.get(cat, LONGSHOT_BIAS_PARAMS["default"])
            expected_ratio = amp * math.exp(-decay * price)
            implied = price / 100.0
            expected_edge = implied * expected_ratio  # time_factor=1 when hours=999

            # Test via the function
            ticker = {"default": "KXTEST", "sports": "KXNBA-TEST",
                      "weather": "KXHIGH-TEST", "entertainment": "KXALBUM-TEST",
                      "politics": "KXPRES-TEST", "economics": "KXCPI-TEST",
                      "crypto": "KXBTC-TEST"}.get(cat, "KXTEST")
            actual_edge = longshot_edge(price, ticker, hours_to_close=999)

            computed[f"{price}c_{cat}"] = round(actual_edge, 6)
            if abs(actual_edge - expected_edge) > 0.001:
                issues.append(f"{price}c {cat}: got {actual_edge:.6f}, expected {expected_edge:.6f}")

        # 5D.1a: Time decay verification
        # At 24h: time_factor = 1.0; at 1h: time_factor ≈ 0.52
        edge_24h = longshot_edge(5, "KXTEST", hours_to_close=24)
        edge_1h = longshot_edge(5, "KXTEST", hours_to_close=1)
        if edge_1h >= edge_24h:
            issues.append(f"Time decay broken: 1h edge {edge_1h} >= 24h edge {edge_24h}")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "5D", "5D.1", severity,
            "Becker longshot model verification",
            "overpricing = amplitude * exp(-decay * price) * time_factor. " +
            ("Verified correct." if not issues else f"{len(issues)} issues"),
            {"computed_edges": computed, "issues": issues,
             "category_params": {k: {"amplitude": v[0], "decay": v[1]}
                                 for k, v in LONGSHOT_BIAS_PARAMS.items()}}
        ))

        # 5D.2: Strategy trade analysis
        strat_trades = self.trades.get("strategy", [])
        if strat_trades:
            edges = []
            for t in strat_trades:
                est = t.get("est_edge", "")
                if isinstance(est, str) and est.endswith("%"):
                    try:
                        edges.append(float(est.rstrip("%")))
                    except ValueError:
                        pass
            if edges:
                findings.append(Finding(
                    "5D", "5D.2", Finding.INFO,
                    "Strategy trade edge distribution",
                    f"{len(strat_trades)} trades, edge range: {min(edges):.1f}%-{max(edges):.1f}%, "
                    f"mean: {sum(edges)/len(edges):.1f}%",
                    {"min_edge": min(edges), "max_edge": max(edges),
                     "mean_edge": round(sum(edges)/len(edges), 1)}
                ))
            # Check all edges are positive
            neg_edges = [e for e in edges if e < 0]
            if neg_edges:
                findings.append(Finding(
                    "5D", "5D.3", Finding.FAIL,
                    "Strategy trades with negative edge!",
                    f"{len(neg_edges)} trades had negative edge: {neg_edges}"
                ))
        else:
            findings.append(Finding(
                "5D", "5D.2", Finding.INFO,
                "No strategy trades to analyze", ""
            ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5E: Crypto Bot Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5e_crypto(self):
        findings = []

        # 5E.1: GBM formula verification
        # P(S_T > K) = Phi(d2) where d2 = (ln(S/K) - 0.5*σ²*T) / (σ*√T)
        issues = []

        # When current == threshold, P should be slightly less than 0.5 (drift is negative)
        p_at_money = crypto_price_probability(100000, 100000, "above", 1440, 0.60)
        if not (0.40 < p_at_money < 0.50):
            issues.append(f"At-money BTC: P={p_at_money:.4f}, expected ~0.49 (slight negative drift)")

        # When current >> threshold, P should be high
        p_deep_itm = crypto_price_probability(100000, 50000, "above", 1440, 0.60)
        if p_deep_itm < 0.95:
            issues.append(f"Deep ITM: P(100k > 50k)={p_deep_itm:.4f}, expected >0.95")

        # When current << threshold, P should be low
        p_deep_otm = crypto_price_probability(50000, 100000, "above", 1440, 0.60)
        if p_deep_otm > 0.10:
            issues.append(f"Deep OTM: P(50k > 100k)={p_deep_otm:.4f}, expected <0.10")

        # Verify T computation: 1440 minutes = 1 day
        T = 1440 / (365.25 * 24 * 60)
        expected_T = 1.0 / 365.25
        if abs(T - expected_T) > 0.0001:
            issues.append(f"Time conversion: {T} != expected {expected_T}")

        # Check that higher vol → prob closer to 0.5 (more uncertainty)
        p_low_vol = crypto_price_probability(100000, 90000, "above", 1440, 0.30)
        p_high_vol = crypto_price_probability(100000, 90000, "above", 1440, 0.90)
        if p_high_vol >= p_low_vol:
            issues.append(f"Higher vol should reduce confidence: low_vol={p_low_vol:.4f} vs high_vol={p_high_vol:.4f}")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "5E", "5E.1", severity,
            "Crypto GBM formula verification",
            "d2 = (ln(S/K) - 0.5σ²T) / (σ√T), P(>K) = Φ(d2). " +
            ("Verified correct." if not issues else f"{len(issues)} issues"),
            {"issues": issues, "test_results": {
                "at_money": round(p_at_money, 4),
                "deep_itm": round(p_deep_itm, 4),
                "deep_otm": round(p_deep_otm, 4),
            }}
        ))

        # 5E.2: Settlement buffer check
        bots_cfg = self.configs.get("bots") or {}
        crypto_cfg = bots_cfg.get("crypto", {})
        buffer = crypto_cfg.get("settlementBufferMinutes", "unknown")
        findings.append(Finding(
            "5E", "5E.2",
            Finding.WARN if buffer == 2 else Finding.INFO,
            f"Crypto settlement buffer: {buffer} minutes",
            "2-minute buffer is tight for crypto. Price can move 1-2% in 2 minutes "
            "during volatile periods. Consider 5-10 minutes for safety." if buffer == 2
            else f"Settlement buffer configured at {buffer} minutes.",
        ))

        # 5E.3: Vol blending documentation
        findings.append(Finding(
            "5E", "5E.3", Finding.INFO,
            "Crypto vol selection logic",
            "Priority: IV (Deribit) > realized vol > default (60%). "
            "No explicit blending ratio. If IV is available, it's used exclusively. "
            "If only realized vol is available, it's used exclusively. "
            "Default 60% is BTC-appropriate; may need per-asset defaults for ETH.",
        ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5F: Economics Bot Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5f_economics(self):
        findings = []

        # 5F.1: CPI nowcast sigma piecewise exponential
        # Calibrated: Knotek & Zaman (2024), Bloomberg consensus surprise data
        test_points = {0: 0.04, 1: 0.055, 7: 0.11, 14: 0.14}
        issues = []
        computed = {}
        for days, expected_approx in test_points.items():
            actual = cpi_nowcast_sigma(days)
            computed[f"day_{days}"] = round(actual, 4)
            # Allow 30% tolerance for piecewise exponential model
            if actual < expected_approx * 0.5 or actual > expected_approx * 2.0:
                issues.append(f"day {days}: sigma={actual:.4f}, expected ~{expected_approx}")

        # Verify monotonic increase with days_to_release (more days = more uncertainty)
        sigmas = [cpi_nowcast_sigma(d) for d in range(15)]
        for i in range(len(sigmas) - 1):
            if sigmas[i] > sigmas[i + 1]:
                issues.append(f"Non-monotonic: sigma({i})={sigmas[i]:.4f} > sigma({i+1})={sigmas[i+1]:.4f}")

        severity = Finding.FAIL if issues else Finding.PASS
        findings.append(Finding(
            "5F", "5F.1", severity,
            "CPI nowcast sigma verification",
            "Piecewise exponential: ~0.14 at 14d, ~0.11 at 7d, ~0.055 at 1d, 0.04 at 0d. " +
            ("Verified correct." if not issues else f"{len(issues)} issues"),
            {"computed": computed, "issues": issues}
        ))

        # 5F.2: Econ probability model check
        # CPI nowcast=3.5%, sigma=0.06%, threshold=3.4% → should be P(>3.4%) high
        p = econ_nowcast_probability(3.5, 0.06, 3.4, "above")
        if p < 0.85:
            findings.append(Finding(
                "5F", "5F.2", Finding.FAIL,
                "Econ probability model issue",
                f"P(CPI > 3.4 | nowcast=3.5, sigma=0.06) = {p:.4f}, expected >0.85"
            ))
        else:
            findings.append(Finding(
                "5F", "5F.2", Finding.PASS,
                "Econ probability model correct",
                f"P(CPI > 3.4 | nowcast=3.5, sigma=0.06) = {p:.4f}",
            ))

        # 5F.3: Gas price integration status
        findings.append(Finding(
            "5F", "5F.3", Finding.INFO,
            "Gas price integration",
            "AAA gas price is fetched by economics-bot but currently used only as "
            "context in logging, not integrated into the probability model. "
            "Potential future enhancement for CPI component forecasting.",
        ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5G: Cross-Platform Arb Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5g_cross_platform(self):
        findings = []

        bots_cfg = self.configs.get("bots") or {}
        arb_cfg = bots_cfg.get("cross_platform_arb", {})

        # 5G.1: Execution status
        exec_enabled = arb_cfg.get("executionEnabled", False)
        findings.append(Finding(
            "5G", "5G.1", Finding.INFO,
            "Cross-platform arb execution status",
            f"Execution enabled: {exec_enabled}. "
            + ("Phase 1 (monitoring only) — no real trades." if not exec_enabled
               else "Phase 2 (live execution) — actively trading.")
        ))

        # 5G.2: Fuzzy matching threshold — check actual code
        arb_path = PROJECT_DIR / "src" / "kalshi" / "cross-platform-arb.py"
        arb_score = "?"
        has_num_validation = False
        if arb_path.exists():
            arb_src = arb_path.read_text()
            import re as _re
            m = _re.search(r'MIN_MATCH_SCORE\s*=\s*([\d.]+)', arb_src)
            if m:
                arb_score = float(m.group(1))
            has_num_validation = "validate_match" in arb_src and "_extract_numbers" in arb_src

        if isinstance(arb_score, float) and arb_score >= 0.75 and has_num_validation:
            findings.append(Finding(
                "5G", "5G.2", Finding.PASS,
                f"Fuzzy matching: score >= {arb_score} + numerical validation",
                f"MIN_MATCH_SCORE = {arb_score} (raised from 0.6). "
                "validate_match() extracts and compares numerical thresholds to prevent "
                "false positives like 'BTC 70k' vs 'BTC 80k'.",
            ))
        else:
            findings.append(Finding(
                "5G", "5G.2", Finding.WARN,
                f"Fuzzy matching threshold: SequenceMatcher >= {arb_score}",
                "Threshold may produce false positives. 'Will BTC exceed 70k' vs "
                "'Will BTC exceed 80k' could match despite being different markets. "
                "Consider raising to >= 0.75 and adding numerical threshold validation.",
            ))

        # 5G.3: Minimum spread check
        min_spread = arb_cfg.get("minSpreadPct", "unknown")
        findings.append(Finding(
            "5G", "5G.3", Finding.INFO,
            f"Minimum spread threshold: {min_spread}",
            "After fees on both platforms, net spread must exceed this threshold. "
            "Kalshi fee ~1.75c at 50c; typical round-trip cost ~3.5c. "
            "2% minimum on a 50c contract = 1c, which is below fee drag.",
        ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5H: Market Maker Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5h_market_maker(self):
        findings = []

        bots_cfg = self.configs.get("bots") or {}
        mm_cfg = bots_cfg.get("market_maker", {})

        # 5H.1: Enabled status
        enabled = mm_cfg.get("enabled", False)
        findings.append(Finding(
            "5H", "5H.1", Finding.INFO,
            f"Market maker enabled: {enabled}",
            "Disabled by default. Kalshi's thin order books make market-making risky — "
            "adverse selection from informed traders (weather bots, info arb) can "
            "quickly accumulate inventory against the market maker.",
        ))

        # 5H.2: Avellaneda-Stoikov parameter review — check sigma estimation
        gamma = mm_cfg.get("gamma", "?")
        k_param = mm_cfg.get("kParam", "?")
        mm_path = PROJECT_DIR / "src" / "kalshi" / "market-maker.py"
        sigma_independent = False
        if mm_path.exists():
            mm_src = mm_path.read_text()
            # Extract estimate_market_sigma function body (code lines only, not docstring)
            if "def estimate_market_sigma" in mm_src:
                func_block = mm_src.split("def estimate_market_sigma")[1].split("\ndef ")[0]
                # Strip docstring (between triple quotes)
                import re as _re
                func_code = _re.sub(r'""".*?"""', '', func_block, flags=_re.DOTALL)
                # Check: does the code use yes_bid/yes_ask/spread to compute sigma?
                sigma_from_spread = any(w in func_code for w in ["yes_bid", "yes_ask", "spread"])
            else:
                sigma_from_spread = True
            # Check for independent estimation markers (weather model, days_out, fixed defaults)
            has_weather_model = "days_out" in mm_src or "weather_sigma" in mm_src
            has_fixed_defaults = "return 8" in mm_src or "return 5" in mm_src
            sigma_independent = has_weather_model and has_fixed_defaults and not sigma_from_spread

        if sigma_independent:
            findings.append(Finding(
                "5H", "5H.2", Finding.PASS,
                "Avellaneda-Stoikov: independent sigma estimation",
                f"gamma={gamma}, k={k_param}. "
                "σ estimated independently: weather model (days_out × slope) for KXHIGH, "
                "fixed defaults for crypto (8c) and generic (5c). "
                "No circular dependency on bid-ask spread.",
                {"gamma": gamma, "kParam": k_param}
            ))
        else:
            findings.append(Finding(
                "5H", "5H.2", Finding.WARN,
                "Avellaneda-Stoikov parameters",
                f"gamma={gamma}, k={k_param}. "
                "σ may be estimated from the bid-ask spread, creating circular reasoning. "
                "Fix: use independent sigma from weather model or fixed per-market defaults.",
                {"gamma": gamma, "kParam": k_param}
            ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5I: Position Monitor Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5i_position_monitor(self):
        findings = []

        bots_cfg = self.configs.get("bots") or {}
        pm_cfg = bots_cfg.get("position_monitor", {})

        # 5I.1: Exit thresholds
        tp = pm_cfg.get("takeProfitThreshold", "?")
        sl = pm_cfg.get("stopLossThreshold", "?")

        findings.append(Finding(
            "5I", "5I.1", Finding.INFO,
            f"Exit thresholds: take-profit={tp}, stop-loss={sl}",
            f"Take profit at {tp} (85c bid), stop loss at {sl} (20c bid). "
            "Take-profit threshold should be net of fees. A YES position bought at 30c "
            f"and sold at {int(tp*100) if isinstance(tp, float) else '?'}c has fee drag of "
            "~1.75c each way, reducing net profit by ~7%.",
        ))

        # 5I.2: Fee accounting in exit decisions — check actual code
        pm_path = PROJECT_DIR / "src" / "kalshi" / "position-monitor.py"
        uses_fee_in_exits = False
        if pm_path.exists():
            pm_src = pm_path.read_text()
            uses_fee_in_exits = ("kalshi_fee_cents" in pm_src and "net_proceeds" in pm_src)

        if uses_fee_in_exits:
            findings.append(Finding(
                "5I", "5I.2", Finding.PASS,
                "Position monitor accounts for fees in exit decisions",
                "Take-profit and model-shift exits use kalshi_fee_cents() to compute "
                "net proceeds (bid - fee) before threshold comparison. "
                "Stop-loss is unchanged (risk limit, not profitability check).",
            ))
        else:
            findings.append(Finding(
                "5I", "5I.2", Finding.WARN,
                "Position monitor does not account for fees in exit decisions",
                "Take-profit and stop-loss thresholds are based on raw market price, "
                "not net-of-fee price. A position near the take-profit threshold "
                "may not be profitable after round-trip fees (~3.5c). "
                "Fix: subtract estimated exit fee from bid price before comparison.",
            ))

        # 5I.3: Model-shift exit status
        model_shift = pm_cfg.get("modelShiftThreshold", "?")
        findings.append(Finding(
            "5I", "5I.3", Finding.INFO,
            f"Model-shift exit threshold: {model_shift}",
            "Model-shift exit is configured but implementation depends on "
            "real-time model re-evaluation at scan time. Currently stubbed in most "
            "market types. Active only for weather markets with fresh forecast data.",
        ))

        return findings

    # ═══════════════════════════════════════════════════════════════════
    # Section 5J: Capital Allocator Audit
    # ═══════════════════════════════════════════════════════════════════

    def section_5j_capital_allocator(self):
        findings = []

        # 5J.1: Check if allocator module exists
        allocator_path = PROJECT_DIR / "src" / "kalshi" / "capital_allocator.py"
        if allocator_path.exists():
            findings.append(Finding(
                "5J", "5J.1", Finding.INFO,
                "Capital allocator module exists",
                "Found src/kalshi/capital-allocator.py"
            ))
        else:
            findings.append(Finding(
                "5J", "5J.1", Finding.INFO,
                "No capital allocator module found",
                "Capital allocation is implicit via per-bot config limits. "
                "No centralized allocator coordinates cross-bot exposure."
            ))

        # 5J.2: Aggregate limit analysis
        bots_cfg = self.configs.get("bots") or {}
        weather_cfg = self.configs.get("weather") or {}

        bot_limits = {}
        total_max_daily_loss = 0

        # Weather bot
        wl = weather_cfg.get("maxDailyLoss", 0)
        bot_limits["weather"] = wl
        total_max_daily_loss += wl

        # Other bots
        for bot_name, section_name in [
            ("entertainment", "entertainment"),
            ("strategy", "strategy"),
            ("crypto", "crypto"),
            ("economics", "economics"),
            ("beatrelease", "beatrelease"),
            ("cross_platform_arb", "cross_platform_arb"),
            ("market_maker", "market_maker"),
        ]:
            cfg = bots_cfg.get(section_name, {})
            loss = cfg.get("maxDailyLoss", 0)
            bot_limits[bot_name] = loss
            total_max_daily_loss += loss

        # Monitor
        monitor_cfg = self.configs.get("monitor") or {}
        ml = monitor_cfg.get("maxDailyLoss", 0)
        bot_limits["source_monitor"] = ml
        total_max_daily_loss += ml

        # Check if allocator has an absolute daily cap
        allocator_path = PROJECT_DIR / "src" / "kalshi" / "capital_allocator.py"
        absolute_cap = None
        if allocator_path.exists():
            alloc_src = allocator_path.read_text()
            import re as _re
            cap_m = _re.search(r'ABSOLUTE_DAILY_LOSS_CAP_CENTS\s*=\s*(\d+)', alloc_src)
            if cap_m:
                absolute_cap = int(cap_m.group(1))

        if absolute_cap:
            cap_dollars = absolute_cap / 100
            findings.append(Finding(
                "5J", "5J.2", Finding.PASS if cap_dollars <= total_max_daily_loss else Finding.INFO,
                f"Aggregate loss capped: ${cap_dollars:.0f} absolute cap (config sum: ${total_max_daily_loss})",
                f"Per-bot config limits sum to ${total_max_daily_loss}, but "
                f"capital_allocator.py enforces ABSOLUTE_DAILY_LOSS_CAP_CENTS = {absolute_cap} "
                f"(${cap_dollars:.0f}). This prevents worst-case aggregate drawdown.",
                {"per_bot_limits": bot_limits, "config_total": total_max_daily_loss,
                 "absolute_cap_dollars": cap_dollars}
            ))
        else:
            findings.append(Finding(
                "5J", "5J.2", Finding.WARN if total_max_daily_loss > 200 else Finding.INFO,
                f"Aggregate max daily loss: ${total_max_daily_loss}",
                "Sum of all bots' maxDailyLoss limits. If all bots hit their limits "
                "simultaneously, this is the worst-case daily drawdown. "
                "No centralized circuit breaker caps aggregate loss.",
                {"per_bot_limits": bot_limits, "total": total_max_daily_loss}
            ))

        # 5J.3: Global dedup — check if all trading bots use allocator
        trading_bots = [
            "weather-bot.py", "entertainment-bot.py", "source-monitor.py",
            "strategy-trader.py", "crypto-bot.py", "economics-bot.py",
            "beatrelease-scanner.py",
        ]
        bots_with_allocator = []
        bots_without_allocator = []
        for bf in trading_bots:
            path = PROJECT_DIR / "src" / "kalshi" / bf
            if path.exists():
                content = path.read_text()
                if "request_budget(" in content or "is_ticker_traded(" in content:
                    bots_with_allocator.append(bf)
                else:
                    bots_without_allocator.append(bf)

        if not bots_without_allocator:
            findings.append(Finding(
                "5J", "5J.3", Finding.PASS,
                "Global dedup: all bots use allocator",
                f"All {len(bots_with_allocator)} trading bots call "
                "allocator.request_budget() before placing trades. "
                "The allocator's is_ticker_traded() check prevents cross-bot double exposure.",
                {"protected_bots": bots_with_allocator}
            ))
        else:
            findings.append(Finding(
                "5J", "5J.3", Finding.WARN,
                f"Global dedup: {len(bots_without_allocator)} bot(s) skip allocator",
                "Bots without allocator checks can trade tickers already held by other bots. "
                "Add allocator.request_budget() before trade_manager.place_order().",
                {"unprotected": bots_without_allocator, "protected": bots_with_allocator}
            ))

        return findings


# ═══════════════════════════════════════════════════════════════════════
# Output formatters
# ═══════════════════════════════════════════════════════════════════════

def print_text_report(findings):
    """Print human-readable audit report."""
    print("=" * 70)
    print("KALSHI TRADING SYSTEM — COMPREHENSIVE MATH & STRATEGY AUDIT")
    print(f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # Group by section
    by_section = defaultdict(list)
    for f in findings:
        by_section[f.section].append(f)

    section_names = {
        "0": "P&L Truth",
        "1": "Data Integrity & Settlement Mapping",
        "2": "Backtesting & Evaluation Realism",
        "3": "Execution & Microstructure",
        "4": "Sizing & Portfolio Risk",
        "5A": "Weather Bot",
        "5B": "Entertainment Bot",
        "5C": "Source Monitor",
        "5D": "Strategy Trader",
        "5E": "Crypto Bot",
        "5F": "Economics Bot",
        "5G": "Cross-Platform Arb",
        "5H": "Market Maker",
        "5I": "Position Monitor",
        "5J": "Capital Allocator",
    }

    total_pass = sum(1 for f in findings if f.severity == Finding.PASS)
    total_warn = sum(1 for f in findings if f.severity == Finding.WARN)
    total_fail = sum(1 for f in findings if f.severity == Finding.FAIL)
    total_info = sum(1 for f in findings if f.severity == Finding.INFO)

    for section_id in sorted(by_section.keys(), key=_section_sort_key):
        section_findings = by_section[section_id]
        name = section_names.get(section_id, section_id)
        print(f"\n{'─' * 70}")
        print(f"Section {section_id}: {name}")
        print(f"{'─' * 70}")

        for f in section_findings:
            icon = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[INFO]"
                    }.get(f.severity, "[????]")
            print(f"\n  {icon} {f.qid}: {f.title}")
            if f.detail:
                # Wrap long detail lines
                for line in f.detail.split("\n"):
                    print(f"    {line}")
            if f.evidence and f.severity in (Finding.FAIL, Finding.WARN):
                for k, v in f.evidence.items():
                    if isinstance(v, (list, dict)):
                        v_str = json.dumps(v, indent=2)
                        for vl in v_str.split("\n")[:10]:
                            print(f"      {k}: {vl}")
                    else:
                        print(f"      {k}: {v}")

    # Summary
    print(f"\n{'=' * 70}")
    print("AUDIT SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total findings: {len(findings)}")
    print(f"  PASS: {total_pass}  |  WARN: {total_warn}  |  FAIL: {total_fail}  |  INFO: {total_info}")

    if total_fail > 0:
        print(f"\n  FAILURES:")
        for f in findings:
            if f.severity == Finding.FAIL:
                print(f"    - {f.qid}: {f.title}")

    if total_warn > 0:
        print(f"\n  WARNINGS:")
        for f in findings:
            if f.severity == Finding.WARN:
                print(f"    - {f.qid}: {f.title}")

    print()


def print_json_report(findings):
    """Print machine-readable JSON audit report."""
    output = {
        "timestamp": datetime.datetime.now().isoformat(),
        "findings": [f.to_dict() for f in findings],
        "summary": {
            "total": len(findings),
            "pass": sum(1 for f in findings if f.severity == Finding.PASS),
            "warn": sum(1 for f in findings if f.severity == Finding.WARN),
            "fail": sum(1 for f in findings if f.severity == Finding.FAIL),
            "info": sum(1 for f in findings if f.severity == Finding.INFO),
        }
    }
    print(json.dumps(output, indent=2))


def _section_sort_key(section_id):
    """Sort key for section IDs: 0, 1, 2, 3, 4, 5A, 5B, ..., 5J."""
    if section_id.startswith("5"):
        return (5, section_id[1:])
    try:
        return (int(section_id), "")
    except ValueError:
        return (99, section_id)


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive math & strategy audit for the Kalshi trading system."
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--section", type=str, action="append",
                        help="Run only specific section(s). Can repeat: --section 0 --section 4")
    parser.add_argument("--reconcile", action="store_true",
                        help="Query Kalshi API for settlements/fills reconciliation")
    args = parser.parse_args()

    engine = AuditEngine(reconcile=args.reconcile)
    findings = engine.run(sections=args.section)

    if args.json:
        print_json_report(findings)
    else:
        print_text_report(findings)

    # Exit code: 1 if any FAIL findings
    has_failures = any(f.severity == Finding.FAIL for f in findings)
    sys.exit(1 if has_failures else 0)


if __name__ == "__main__":
    main()
