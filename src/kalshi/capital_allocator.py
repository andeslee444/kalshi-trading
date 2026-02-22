"""Cross-bot capital allocator for Kalshi trading system.

Provides portfolio-level capital coordination across all bots:
  - Global dedup: prevents multiple bots from doubling up on the same ticker
  - Dynamic allocation: bots with higher-edge opportunities get more capital
  - Portfolio risk limits: single daily loss cap across the entire system
  - Concentration limits: prevents over-exposure to a single market type
  - City-level exposure limits: caps total risk on correlated weather brackets
  - File-backed shared state: cross-process coordination via fcntl locking

Usage:
    from capital_allocator import PortfolioAllocator

    allocator = PortfolioAllocator(client)
    budget = allocator.request_budget("weather-bot", ticker, edge, confidence)
    if budget.approved:
        count, risk = half_kelly(edge, price, budget.max_cost_cents, budget.bankroll_cents)
"""

import fcntl
import json
import os
import re
import tempfile
import time
import datetime
import logging
from pathlib import Path

_log = logging.getLogger("capital_allocator")

# Default path for shared state file (all bots converge here)
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "allocator-state.json"

# ─── Budget priority tiers ───
# Higher priority = larger share of available capital
# Info-arb gets highest because edge is information-based (near-certain)
BOT_PRIORITY = {
    "source-monitor": 1.0,    # info-arb: highest edge quality
    "position-monitor": 0.9,  # exits free capital, not consuming allocation
    "economics": 0.9,         # nowcast-based: high edge quality (like info-arb)
    "entertainment": 0.8,     # info-arb: high edge quality
    "weather": 0.5,           # model-based: moderate edge quality
    "crypto": 0.4,            # model-based: high vol, lower confidence
    "strategy": 0.3,          # statistical: lower per-trade edge
    "beatrelease": 0.3,       # copy-trading: variable quality
    "trade-cycle": 0.2,       # one-shot: lowest priority
}

# Max fraction of bankroll any single bot can consume per day
MAX_BOT_FRACTION = 0.40

# Max fraction of bankroll in any single ticker
# Reduced from 0.15 to 0.05 — prevents single-position blowups like
# the Houston B77.5 position (66 contracts, $29 exposure on one bracket)
MAX_TICKER_FRACTION = 0.05

# Max fraction of bankroll in any single city (weather markets)
# Multiple brackets on the same city (e.g., Houston B77, B78, B79) are
# correlated — capping at 10% prevents over-concentration
MAX_CITY_FRACTION = 0.10

# Portfolio-wide daily loss cap as fraction of bankroll
PORTFOLIO_DAILY_LOSS_FRACTION = 0.25

# Absolute daily risk cap regardless of balance ($100 hard cap)
ABSOLUTE_DAILY_LOSS_CAP_CENTS = 10000


# ─── City key extraction ───

# ─── Signal quality factors ───
# Higher factor = more reliable signal. Multiplied by edge to get quality score.
MODEL_QUALITY_FACTOR = {
    "source-monitor": 1.0,    # direct data observation
    "economics": 0.9,         # nowcast-based
    "entertainment": 0.8,     # info-arb from HDD/box office
    "weather": 0.5,           # model-based forecasting
    "crypto": 0.4,            # high-vol model
    "strategy": 0.3,          # statistical bias
    "beatrelease": 0.3,       # copy-trading
    "market-maker": 0.2,      # inventory management
    "trade-cycle": 0.2,       # one-shot
}


def compute_signal_quality(bot_name, edge):
    """Compute a signal quality score for dedup/supersede decisions.

    Returns edge * quality_factor, so a 20% edge from source-monitor (1.0)
    beats a 20% edge from weather (0.5).
    """
    factor = MODEL_QUALITY_FACTOR.get(bot_name, 0.2)
    return abs(edge) * factor


# ─── Region-level correlation grouping ───
# Cities in the same climate region are correlated — a heat wave hits both.
CITY_REGIONS = {
    "SOUTH_TX": ["HOU", "AUS"],
    "NORTHEAST": ["NY", "PHIL"],
}
# Reverse lookup: city -> region
_CITY_TO_REGION = {}
for _region, _cities in CITY_REGIONS.items():
    for _city in _cities:
        _CITY_TO_REGION[_city] = _region

MAX_REGION_FRACTION = 0.15  # 15% of bankroll per region

_CITY_KEY_RE = re.compile(r"KXHIGH([A-Z]+)-(\d{2}[A-Z]{3}\d{2})")


def _extract_city_key(ticker):
    """Extract a city+date key from a KXHIGH ticker for exposure grouping.

    Returns "CITY:DATE" (e.g. "HOU:26FEB16") for weather tickers,
    or None for non-weather tickers.
    """
    m = _CITY_KEY_RE.match(ticker)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return None


class BudgetResponse:
    """Response from the allocator for a trade request."""

    __slots__ = ("approved", "max_cost_cents", "bankroll_cents", "reason", "binding_constraint")

    def __init__(self, approved, max_cost_cents=0, bankroll_cents=0, reason="", binding_constraint=""):
        self.approved = approved
        self.max_cost_cents = max_cost_cents
        self.bankroll_cents = bankroll_cents
        self.reason = reason
        self.binding_constraint = binding_constraint

    def __repr__(self):
        if self.approved:
            return f"BudgetResponse(approved=True, max_cost=${self.max_cost_cents/100:.2f}, bankroll=${self.bankroll_cents/100:.2f})"
        return f"BudgetResponse(approved=False, reason={self.reason!r})"


class PortfolioAllocator:
    """Portfolio-level capital allocator across all bots.

    Tracks global state: which tickers have been traded today, how much
    each bot has spent, and total portfolio risk. Uses file-backed shared
    state with fcntl advisory locking for cross-process coordination.

    Args:
        client: KalshiClient for balance queries.
        state_path: Path to shared state file (for multi-process coordination).
            Defaults to data/allocator-state.json.
        logger: Optional logger instance.
    """

    def __init__(self, client=None, state_path=None, logger=None, max_positions=None):
        self.client = client
        self.log = logger or _log
        if state_path is not None:
            self.state_path = Path(state_path)
        else:
            self.state_path = DEFAULT_STATE_PATH

        # In-memory state (loaded from / saved to file)
        # ticker -> {"bot": str, "timestamp": str, "signal_quality": float,
        #            "edge": float, "risk_cents": int}
        self._traded_tickers = {}
        self._bot_spend = {}            # bot_name -> cents risked today
        self._city_risk = {}            # city_key -> cents risked today
        self._region_risk = {}          # region -> cents risked today
        self._total_risk_cents = 0      # portfolio-wide risk today
        self._daily_date = None
        self._cached_balance = None
        self._cached_available = None
        self._balance_fetched_at = 0
        self._pending_exits = []        # tickers that should be exited (superseded)
        self._max_positions = max_positions
        self._position_count = None
        self._position_count_fetched_at = 0

    def _load_state(self):
        """Load shared state from disk with advisory file locking."""
        if not self.state_path or not self.state_path.exists():
            return
        try:
            with open(self.state_path, "r") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            raw_tickers = data.get("traded_tickers", {})
            # Backward compat: convert old tuple/list format to new dict format
            self._traded_tickers = {}
            for k, v in raw_tickers.items():
                if isinstance(v, dict):
                    self._traded_tickers[k] = v
                elif isinstance(v, (list, tuple)):
                    # Old format: (bot_name, timestamp)
                    self._traded_tickers[k] = {
                        "bot": v[0] if len(v) > 0 else "",
                        "timestamp": v[1] if len(v) > 1 else "",
                        "signal_quality": 0.0,
                        "edge": 0.0,
                        "risk_cents": 0,
                    }
            self._bot_spend = data.get("bot_spend", {})
            self._city_risk = data.get("city_risk", {})
            self._region_risk = data.get("region_risk", {})
            self._total_risk_cents = data.get("total_risk_cents", 0)
            self._daily_date = data.get("daily_date")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self.log.warning("Corrupt state file, resetting: %s", e)
            self._traded_tickers = {}
            self._bot_spend = {}
            self._city_risk = {}
            self._region_risk = {}
            self._total_risk_cents = 0
            self._daily_date = None

    def _save_state(self):
        """Save shared state to disk atomically with exclusive locking."""
        if not self.state_path:
            return
        data = {
            "traded_tickers": self._traded_tickers,
            "bot_spend": self._bot_spend,
            "city_risk": self._city_risk,
            "region_risk": self._region_risk,
            "total_risk_cents": self._total_risk_cents,
            "daily_date": self._daily_date,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.state_path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(data, f, indent=2)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                os.replace(tmp_path, str(self.state_path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            self.log.warning("Failed to save allocator state: %s", e)

    def _reset_daily_if_needed(self):
        self._load_state()
        today = datetime.date.today().isoformat()
        if self._daily_date != today:
            if self._daily_date is not None:
                self.log.info("Allocator daily reset: %d tickers, $%.2f risk cleared",
                              len(self._traded_tickers), self._total_risk_cents / 100)
            self._traded_tickers = {}
            self._bot_spend = {}
            self._city_risk = {}
            self._region_risk = {}
            self._total_risk_cents = 0
            self._daily_date = today
            self._pending_exits = []
            self._save_state()

    def _get_balance(self):
        """Get total and available balance, cached for 5 seconds.

        Returns (total_balance, available_balance) in cents.
        Available balance is used for both Kelly sizing and risk limit checks.
        """
        now = time.time()
        if self._cached_balance is not None and (now - self._balance_fetched_at) < 5:
            return self._cached_balance, self._cached_available
        if self.client:
            try:
                total, avail = self.client.get_balance()
                self._cached_balance = total
                self._cached_available = avail
                self._balance_fetched_at = now
                return total, avail
            except Exception as e:
                self.log.warning("Balance fetch failed: %s", e)
        return self._cached_balance or 0, self._cached_available or 0

    def _get_position_count(self):
        """Get open position count, cached for 30 seconds."""
        now = time.time()
        if self._position_count is not None and (now - self._position_count_fetched_at) < 30:
            return self._position_count
        if self.client:
            try:
                data = self.client.get("/portfolio/positions")
                positions = data.get("market_positions", [])
                count = sum(1 for p in positions if p.get("total_traded", 0) > 0)
                self._position_count = count
                self._position_count_fetched_at = now
                return count
            except Exception as e:
                self.log.warning("Position count fetch failed: %s", e)
        return self._position_count or 0

    def is_ticker_traded(self, ticker):
        """Check if any bot has already traded this ticker today."""
        self._reset_daily_if_needed()
        return ticker in self._traded_tickers

    def record_trade(self, bot_name, ticker, risk_cents, edge=0.0):
        """Record that a trade was executed.

        Args:
            bot_name: Name of the bot that placed the trade.
            ticker: Market ticker.
            risk_cents: Risk in cents for this trade.
            edge: The edge that was used for this trade (for signal quality).
        """
        self._reset_daily_if_needed()
        quality = compute_signal_quality(bot_name, edge)
        self._traded_tickers[ticker] = {
            "bot": bot_name,
            "timestamp": datetime.datetime.now().isoformat(),
            "signal_quality": round(quality, 4),
            "edge": round(abs(edge), 4),
            "risk_cents": risk_cents,
        }
        self._bot_spend[bot_name] = self._bot_spend.get(bot_name, 0) + risk_cents
        self._total_risk_cents += risk_cents
        # Track city-level and region-level exposure for weather tickers
        city_key = _extract_city_key(ticker)
        if city_key:
            self._city_risk[city_key] = self._city_risk.get(city_key, 0) + risk_cents
            city_code = city_key.split(":")[0]
            region = _CITY_TO_REGION.get(city_code)
            if region:
                self._region_risk[region] = self._region_risk.get(region, 0) + risk_cents
        self._save_state()

    def _risk_today_cents(self):
        """Sum risk_cents from all today's entries in traded tickers."""
        today = datetime.date.today().isoformat()
        return sum(
            v.get("risk_cents", 0) for v in self._traded_tickers.values()
            if isinstance(v, dict) and v.get("timestamp", "")[:10] == today
        )

    def get_pending_exits(self):
        """Return and clear the list of tickers that should be exited.

        These are tickers where a better signal superseded a previous trade.
        """
        exits = list(self._pending_exits)
        self._pending_exits.clear()
        return exits

    def request_budget(self, bot_name, ticker, edge=0.0, confidence=0.0,
                       bot_max_cost_cents=500):
        """Request a capital allocation for a trade.

        Args:
            bot_name: Identifier for the requesting bot.
            ticker: Market ticker to trade.
            edge: Estimated edge (probability difference).
            confidence: Model confidence (0-1).
            bot_max_cost_cents: Bot's own per-trade cost cap from config.

        Returns:
            BudgetResponse with approved flag, allocated max_cost, and bankroll.
        """
        self._reset_daily_if_needed()

        # 1. Global dedup with "best signal wins" supersede logic
        if ticker in self._traded_tickers:
            existing = self._traded_tickers[ticker]
            existing_quality = existing.get("signal_quality", 0.0)
            new_quality = compute_signal_quality(bot_name, edge)
            # Only supersede if new signal is 1.5x better
            if new_quality > existing_quality * 1.5 and new_quality > 0:
                self.log.info(
                    "Signal supersede: %s (quality %.3f) replaces %s (quality %.3f) on %s",
                    bot_name, new_quality, existing.get("bot", "?"), existing_quality, ticker
                )
                self._pending_exits.append(ticker)
                # Allow the new trade to proceed (don't return denied)
            else:
                other_bot = existing.get("bot", "unknown")
                return BudgetResponse(False, reason=f"already traded by {other_bot}")

        # 1b. Max concurrent positions check
        if self._max_positions and self._max_positions > 0:
            count = self._get_position_count()
            if count >= self._max_positions:
                return BudgetResponse(False, reason=f"max concurrent positions ({self._max_positions}) reached")

        # 2. Get balance — available for both Kelly sizing and risk checks
        total_balance, available_balance = self._get_balance()
        if available_balance <= 0:
            return BudgetResponse(False, reason="no balance available")

        # Use available balance for Kelly bankroll — can't size based on locked capital
        bankroll = available_balance

        # 3. Portfolio-level daily loss check (based on available)
        max_portfolio_risk = int(available_balance * PORTFOLIO_DAILY_LOSS_FRACTION)
        remaining_portfolio = max_portfolio_risk - self._total_risk_cents
        if remaining_portfolio <= 0:
            return BudgetResponse(False, reason="portfolio daily loss limit reached")

        # 3b. Absolute daily risk cap ($100 hard cap regardless of balance)
        if self._risk_today_cents() >= ABSOLUTE_DAILY_LOSS_CAP_CENTS:
            return BudgetResponse(False, reason="absolute daily risk cap ($100) reached")

        # 4. Per-bot daily spending check (based on available)
        priority = BOT_PRIORITY.get(bot_name, 0.2)
        max_bot_risk = int(available_balance * MAX_BOT_FRACTION * priority)
        bot_spent = self._bot_spend.get(bot_name, 0)
        remaining_bot = max_bot_risk - bot_spent
        if remaining_bot <= 0:
            return BudgetResponse(False, reason=f"{bot_name} daily allocation exhausted")

        # 5. Per-ticker concentration limit (based on available)
        max_ticker_risk = int(available_balance * MAX_TICKER_FRACTION)

        # 5b. City-level concentration limit (weather markets)
        city_key = _extract_city_key(ticker)
        max_city_risk = int(available_balance * MAX_CITY_FRACTION)
        remaining_city = max_city_risk
        if city_key:
            city_spent = self._city_risk.get(city_key, 0)
            remaining_city = max_city_risk - city_spent
            if remaining_city <= 0:
                return BudgetResponse(False, reason=f"city exposure limit reached for {city_key}")

        # 5c. Region-level exposure check (correlated cities)
        if city_key:
            city_code = city_key.split(":")[0]
            region = _CITY_TO_REGION.get(city_code)
            if region:
                max_region_risk = int(available_balance * MAX_REGION_FRACTION)
                region_spent = self._region_risk.get(region, 0)
                if region_spent >= max_region_risk:
                    return BudgetResponse(False, reason=f"region exposure limit reached for {region}")
                remaining_city = min(remaining_city, max_region_risk - region_spent)

        # 6. Compute allocated budget
        # The allocation is the minimum of all constraints
        constraints = {
            "bot_config_cap": bot_max_cost_cents,
            "portfolio_daily_limit": remaining_portfolio,
            "bot_daily_limit": remaining_bot,
            "ticker_concentration": max_ticker_risk,
            "city_concentration": remaining_city,
        }
        allocated = min(constraints.values())
        binding = min(constraints, key=constraints.get)

        # 7. Scale up for high-confidence info-arb trades
        # When confidence > 90%, allow up to 25% of bankroll per trade
        if confidence > 0.90 and edge > 0.15:
            high_conf_max = int(available_balance * 0.25)
            allocated = min(
                high_conf_max,
                remaining_portfolio,
                remaining_bot * 2,  # relax bot cap for high-confidence
                max_ticker_risk * 2,
                remaining_city,     # NEVER bypass city limit
            )
            self.log.info(
                "High-confidence trade: %s edge=%.1f%% conf=%.0f%% -> budget $%.2f",
                ticker, edge * 100, confidence * 100, allocated / 100
            )

        if allocated <= 0:
            return BudgetResponse(False, reason="computed allocation is zero")

        return BudgetResponse(
            approved=True,
            max_cost_cents=allocated,
            bankroll_cents=bankroll,  # total equity for Kelly sizing
            binding_constraint=binding,
        )

    def get_status(self):
        """Return current allocation status for logging/monitoring."""
        self._reset_daily_if_needed()
        total, available = self._get_balance()
        return {
            "bankroll_cents": total,
            "available_cents": available,
            "total_risk_today_cents": self._total_risk_cents,
            "portfolio_risk_limit_cents": int(available * PORTFOLIO_DAILY_LOSS_FRACTION) if available else 0,
            "tickers_traded_today": len(self._traded_tickers),
            "bot_spend": dict(self._bot_spend),
        }
