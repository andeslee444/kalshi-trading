"""Trade manager helpers extracted from kalshi_auth."""

from __future__ import annotations

import datetime
import fcntl
import inspect
import json
import logging
import time
from pathlib import Path

import requests

from research.registry import ModelRegistry, StrategyConfigRegistry, annotate_research_record
from risk.circuit_breaker import CircuitBreaker, SHARED_BREAKER_PATH
from risk.kill_switch import KILL_SWITCH_PATH, check_kill_switch, per_bot_halt_path
from storage import TradeStore, atomic_write_json, load_trades, save_decision, save_trade

_log = logging.getLogger("trade-manager")


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class RecentTradeTracker:
    """Track recently traded tickers to prevent duplicate trades across scan cycles."""

    def __init__(self, trades_path: Path, cooldown_hours=6, load_trades_func=load_trades):
        self.trades_path = Path(trades_path)
        self.cooldown_hours = cooldown_hours
        self._load_trades = load_trades_func
        self._recent = {}
        self._load()

    def _load(self):
        trades = self._load_trades(self.trades_path)
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=self.cooldown_hours)
        for trade in trades:
            ts_str = trade.get("timestamp", "")
            ticker = trade.get("ticker", "")
            if not ts_str or not ticker:
                continue
            try:
                ts = datetime.datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
                if ts > cutoff:
                    existing = self._recent.get(ticker)
                    if not existing or ts > existing:
                        self._recent[ticker] = ts
            except (ValueError, TypeError):
                pass

    def is_recent(self, ticker):
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=self.cooldown_hours)
        if not hasattr(self, "_prune_counter"):
            self._prune_counter = 0
        self._prune_counter += 1
        if self._prune_counter >= 100:
            self._prune_counter = 0
            self._recent = {key: value for key, value in self._recent.items() if value > cutoff}

        ts = self._recent.get(ticker)
        if not ts:
            return False
        return ts > cutoff

    def record(self, ticker):
        self._recent[ticker] = datetime.datetime.now(datetime.timezone.utc)


def validate_trade_config(config, bot_name=""):
    """Validate trade-related config values. Raises ValueError on bad config."""
    prefix = f"[{bot_name}] " if bot_name else ""

    amt = config.get("maxTradeAmount")
    if amt is None or amt <= 0:
        raise ValueError(f"{prefix}maxTradeAmount must be positive, got {amt}")
    if amt > 100:
        raise ValueError(f"{prefix}maxTradeAmount={amt} exceeds $100 safety cap")

    trades = config.get("maxDailyTrades")
    if trades is None or not isinstance(trades, int) or trades <= 0:
        raise ValueError(f"{prefix}maxDailyTrades must be a positive integer, got {trades}")
    if trades > 100:
        raise ValueError(f"{prefix}maxDailyTrades={trades} exceeds 100 safety cap")

    loss = config.get("maxDailyLoss")
    if loss is None or loss <= 0:
        raise ValueError(f"{prefix}maxDailyLoss must be positive, got {loss}")
    if loss > 500:
        raise ValueError(f"{prefix}maxDailyLoss={loss} exceeds $500 safety cap")

    for pct_key, label in [("maxTradeAmountPct", "maxTradeAmountPct"), ("maxDailyLossPct", "maxDailyLossPct")]:
        pct = config.get(pct_key)
        if pct is not None:
            if not isinstance(pct, (int, float)) or pct < 0:
                raise ValueError(f"{prefix}{label} must be non-negative, got {pct}")
            if pct > 0.25:
                raise ValueError(f"{prefix}{label}={pct} exceeds 25% safety cap")

    contracts_cap = config.get("maxContractsPerTrade")
    if contracts_cap is not None:
        if not isinstance(contracts_cap, int) or contracts_cap <= 0:
            raise ValueError(f"{prefix}maxContractsPerTrade must be a positive integer, got {contracts_cap}")
        if contracts_cap > 10000:
            raise ValueError(f"{prefix}maxContractsPerTrade={contracts_cap} exceeds 10000 safety cap")

    gross_payout_cap = config.get("maxGrossPayoutCents")
    if gross_payout_cap is not None:
        if not isinstance(gross_payout_cap, (int, float)) or gross_payout_cap < 100:
            raise ValueError(f"{prefix}maxGrossPayoutCents must be at least 100, got {gross_payout_cap}")
        if gross_payout_cap > 100000:
            raise ValueError(f"{prefix}maxGrossPayoutCents={gross_payout_cap} exceeds $1000 safety cap")


def trim_trade_log(
    trades_path,
    max_age_days=90,
    max_entries=5000,
    *,
    load_trades_func=load_trades,
    atomic_write_json_func=atomic_write_json,
    logger=None,
):
    """Remove old entries from a trade log file."""
    trades_path = Path(trades_path)
    log = logger or _log
    lock_path = trades_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            trades = load_trades_func(trades_path)
            if not trades:
                return

            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)
            filtered = []
            for trade in trades:
                ts_str = trade.get("timestamp", "")
                if ts_str:
                    try:
                        ts = datetime.datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=datetime.timezone.utc)
                        if ts < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass
                filtered.append(trade)

            if len(filtered) > max_entries:
                filtered = filtered[-max_entries:]

            if len(filtered) != len(trades):
                log.info(
                    "Trimmed trade log %s: %d -> %d entries",
                    trades_path.name,
                    len(trades),
                    len(filtered),
                )
                atomic_write_json_func(trades_path, filtered)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


class TradeManager:
    """Consolidated trade placement with all safety guardrails."""

    def __init__(
        self,
        client,
        trades_path,
        config,
        logger=None,
        kill_switch_path=None,
        cooldown_hours=6,
        order_monitor=None,
        breaker_state_path=SHARED_BREAKER_PATH,
        bot_name=None,
        *,
        breaker_factory=None,
        load_trades_func=load_trades,
        save_trade_func=save_trade,
        save_decision_func=save_decision,
        notify_func=None,
        atomic_write_json_func=atomic_write_json,
        utc_now_iso_func=_utc_now_iso,
        trade_store_cls=TradeStore,
        requests_module=requests,
        tracker_cls=RecentTradeTracker,
        check_kill_switch_func=check_kill_switch,
        per_bot_halt_path_func=per_bot_halt_path,
    ):
        validate_trade_config(config)
        self.client = client
        self.trades_path = Path(trades_path)
        self.config = config
        self.log = logger or _log
        self.kill_switch_path = kill_switch_path or KILL_SWITCH_PATH
        self.bot_name = bot_name
        self._per_bot_halt_path = per_bot_halt_path_func(bot_name) if bot_name else None
        self._load_trades = load_trades_func
        self._save_trade = save_trade_func
        self._save_decision = save_decision_func
        self._notify = notify_func or (lambda *args, **kwargs: None)
        self._atomic_write_json = atomic_write_json_func
        self._utc_now_iso = utc_now_iso_func
        self._requests = requests_module
        self._check_kill_switch = check_kill_switch_func
        tracker_kwargs = {"cooldown_hours": cooldown_hours}
        try:
            tracker_params = inspect.signature(tracker_cls).parameters
        except (TypeError, ValueError):
            tracker_params = {}
        if "load_trades_func" in tracker_params:
            tracker_kwargs["load_trades_func"] = load_trades_func
        self.tracker = tracker_cls(self.trades_path, **tracker_kwargs)
        factory = breaker_factory or (lambda state_path: CircuitBreaker(state_path=state_path))
        self.breaker = factory(breaker_state_path)
        self.order_monitor = order_monitor
        self.strategy_id = bot_name or getattr(self.log, "name", None)
        self._config_version = None
        self._strategy_config_registry = None
        self._model_registry = None

        self._sell_cooldown = {}
        self._sell_cooldown_seconds = 600
        self._daily_trades = 0
        self._daily_spend_cents = 0
        self._daily_date = None
        self._daily_loss_alerted = False
        self._daily_loss_block_count = 0
        self._daily_loss_first_blocked_at = None
        self._daily_loss_escalated = False
        self.last_error_code = None
        self.last_error_message = ""
        self._local_alerts = {}
        self._cached_balance_cents = None
        self._balance_fetched_at = 0

        self._wal_path = self.trades_path.with_suffix(".wal.json")
        self._wal_store = trade_store_cls(self._wal_path, logger=self.log)
        self._recover_wal()
        self._init_research_registries()

        if config.get("maxTradeAmountPct") or config.get("maxDailyLossPct"):
            self.log.info(
                "Bankroll-proportional limits active: trade=%.1f%%, daily=%.1f%%",
                config.get("maxTradeAmountPct", 0) * 100,
                config.get("maxDailyLossPct", 0) * 100,
            )

    def _init_research_registries(self):
        registry_dir = self.trades_path.parent
        try:
            self._strategy_config_registry = StrategyConfigRegistry(
                registry_dir / "strategy-config-registry.json",
                logger=self.log,
            )
            entry = self._strategy_config_registry.register(
                self.strategy_id,
                self.config,
                source_bot=getattr(self.log, "name", None),
            )
            self._config_version = entry.get("config_version")
        except Exception as e:
            self.log.warning("Failed to initialize strategy config registry: %s", e)
            self._config_version = None

        try:
            self._model_registry = ModelRegistry(
                registry_dir / "model-registry.json",
                logger=self.log,
            )
        except Exception as e:
            self.log.warning("Failed to initialize model registry: %s", e)
            self._model_registry = None

    def _resolve_research_context(self):
        strategy_id = getattr(self, "strategy_id", None) or getattr(self, "bot_name", None)
        if not strategy_id:
            log = getattr(self, "log", None)
            strategy_id = getattr(log, "name", None)
        return strategy_id, getattr(self, "_config_version", None)

    def _apply_research_metadata(self, record):
        strategy_id, config_version = self._resolve_research_context()
        record.update(
            annotate_research_record(
                record,
                strategy_id=strategy_id,
                config_version=config_version,
                model_registry=getattr(self, "_model_registry", None),
                source_bot=record.get("source_bot"),
                source_path=self.trades_path,
                logger=self.log,
            )
        )

    def _reset_daily_if_needed(self):
        today = datetime.date.today().isoformat()
        if self._daily_date != today:
            self._daily_trades = 0
            self._daily_spend_cents = 0
            self._daily_date = today
            self._daily_loss_alerted = False
            self._daily_loss_block_count = 0
            self._daily_loss_first_blocked_at = None
            self._daily_loss_escalated = False
            self._rebuild_daily_counters_from_log(today)

    def remaining_daily_trade_slots(self):
        self._reset_daily_if_needed()
        return max(0, int(self.config["maxDailyTrades"]) - self._daily_trades)

    def _clear_last_error(self):
        self.last_error_code = None
        self.last_error_message = ""

    def _set_last_error(self, code, message):
        self.last_error_code = code
        self.last_error_message = message

    def _should_send_local_alert(self, key, cooldown_seconds=1800):
        now = time.time()
        last_sent = self._local_alerts.get(key, 0)
        if (now - last_sent) < cooldown_seconds:
            return False
        self._local_alerts[key] = now
        return True

    def _rebuild_daily_counters_from_log(self, today_str):
        trades = self._load_trades(self.trades_path)
        for trade in trades:
            ts = trade.get("timestamp", "")
            if ts.startswith(today_str) and trade.get("action", "buy") != "sell":
                self._daily_trades += 1
                self._daily_spend_cents += trade.get("cost_cents", 0)
        if self._daily_trades > 0:
            self.log.info(
                "Daily counters rebuilt from log: %d trades, $%.2f risk",
                self._daily_trades,
                self._daily_spend_cents / 100,
            )

    def _get_available_balance(self):
        now = time.time()
        if self._cached_balance_cents is not None and (now - self._balance_fetched_at) < 30:
            return self._cached_balance_cents
        if self.client:
            try:
                _, available = self.client.get_balance()
                self._cached_balance_cents = available
                self._balance_fetched_at = now
                return available
            except Exception as e:
                self.log.warning("Failed to fetch balance, using cached: %s", e)
        return self._cached_balance_cents or 0

    def _effective_max_trade_cents(self):
        static_cents = int(self.config["maxTradeAmount"] * 100)
        pct = self.config.get("maxTradeAmountPct")
        if pct and pct > 0:
            balance = self._get_available_balance()
            if balance > 0:
                dynamic_cents = int(balance * pct)
                return min(static_cents, dynamic_cents)
        return static_cents

    def _effective_max_daily_loss_cents(self):
        static_cents = int(self.config["maxDailyLoss"] * 100)
        pct = self.config.get("maxDailyLossPct")
        if pct and pct > 0:
            balance = self._get_available_balance()
            if balance > 0:
                dynamic_cents = int(balance * pct)
                return min(static_cents, dynamic_cents)
        return static_cents

    def _write_wal(self, entry):
        self._wal_store.append(entry)

    def _read_wal(self):
        return self._wal_store.load()

    def _clear_wal(self, order_id):
        with self._wal_store.lock():
            entries = [
                entry for entry in self._wal_store.load_unlocked()
                if entry.get("order_id") != order_id
            ]
            if entries:
                self._wal_store.save_unlocked(entries)
            elif self._wal_path.exists():
                self._wal_path.unlink()

    def _recover_wal(self):
        entries = self._read_wal()
        if not entries:
            return
        self.log.warning("WAL recovery: found %d pending entries", len(entries))
        failed_entries = []
        for entry in entries:
            ticker = entry.get("ticker", "?")
            order_id = entry.get("order_id")
            if not order_id:
                self.log.warning("WAL recovery: entry for %s has no order_id, clearing", ticker)
                continue
            try:
                result = self.client.get(f"/portfolio/orders/{order_id}")
                order = result.get("order", {})
                status = (order.get("status") or "").lower()
                if status in ("filled", "complete", "resting"):
                    self.log.warning(
                        "WAL recovery: order %s for %s was %s, writing to trade log",
                        order_id,
                        ticker,
                        status,
                    )
                    record = entry.get("record", {})
                    record["status"] = status
                    record["wal_recovered"] = True
                    self._save_trade(self.trades_path, record)
                else:
                    self.log.warning(
                        "WAL recovery: order %s for %s status=%s, discarding",
                        order_id,
                        ticker,
                        status,
                    )
            except Exception as e:
                self.log.warning("WAL recovery: failed to check order %s: %s", order_id, e)
                failed_entries.append(entry)
        if failed_entries:
            self.log.warning(
                "WAL recovery: %d entries unverified, retaining for next startup",
                len(failed_entries),
            )
            self._wal_store.save(failed_entries)
        elif self._wal_path.exists():
            self._wal_path.unlink()

    @staticmethod
    def _classify_limit_tier(edge):
        if edge is None or edge >= 0.15:
            return "urgent"
        if edge >= 0.08:
            return "balanced"
        return "patient"

    def _build_golden_record(self, ticker, side, price_cents, count, cost_cents, reasoning, order_info, **extra_fields):
        utc_now_iso = getattr(self, "_utc_now_iso", _utc_now_iso)
        record = {
            "timestamp": utc_now_iso(),
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "price_cents": price_cents,
            "count": count,
            "cost_cents": cost_cents,
            "reasoning": reasoning,
            "order_id": order_info.get("order_id"),
            "status": order_info.get("status"),
            "source_bot": self.log.name,
        }
        protected = {"timestamp", "ticker", "action", "side", "source_bot"}
        record.update({key: value for key, value in extra_fields.items() if key not in protected})

        snapshot = extra_fields.get("market_snapshot", {})
        if snapshot:
            record["best_bid"] = snapshot.get("yes_bid")
            record["best_ask"] = snapshot.get("yes_ask")
            bid = snapshot.get("yes_bid", 0) or 0
            ask = snapshot.get("yes_ask", 0) or 0
            record["spread"] = ask - bid if bid and ask else None
            if "volume" in snapshot:
                record["volume"] = snapshot["volume"]

        close_time = extra_fields.get("market_close_time")
        if close_time:
            try:
                close_dt = datetime.datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                now = datetime.datetime.now(datetime.timezone.utc)
                delta = close_dt - now
                record["time_to_settle_minutes"] = max(0, int(delta.total_seconds() / 60))
            except (ValueError, TypeError):
                pass

        edge = extra_fields.get("raw_edge")
        record["limit_price_rule"] = self._classify_limit_tier(edge)
        record["caps_applied"] = extra_fields.get("caps_applied", [])
        record["edge_at_entry"] = extra_fields.get("raw_edge")
        model_prob = extra_fields.get("model_prob")
        record["model_fair_value_cents"] = round(model_prob * 100, 1) if model_prob is not None else None
        record["model_name"] = extra_fields.get("model_name") or extra_fields.get("sizing_method")
        self._apply_research_metadata(record)
        record.setdefault("settlement_result", None)
        record.setdefault("settlement_revenue_cents", None)
        record.setdefault("fill_price_cents", None)
        record.setdefault("fill_count", None)
        return record

    def place_order(self, ticker, side, price_cents, count, reasoning, available_balance_cents=None, market_data_age_seconds=None, **extra_fields):
        self._reset_daily_if_needed()
        self._clear_last_error()
        caps_applied = []

        if side not in ("yes", "no"):
            self.log.error("Invalid side '%s' — must be 'yes' or 'no'", side)
            self._set_last_error("invalid_side", side)
            return None
        if price_cents < 1 or price_cents > 99:
            self.log.error("Invalid price_cents=%d — must be 1-99. Skipping %s", price_cents, ticker)
            self._set_last_error("invalid_price", str(price_cents))
            return None
        if count < 1:
            self.log.error("Invalid count=%d — must be >= 1. Skipping %s", count, ticker)
            self._set_last_error("invalid_count", str(count))
            return None

        if self._check_kill_switch(self.kill_switch_path):
            self.log.warning("KILL SWITCH ACTIVE — refusing trade on %s", ticker)
            self._notify("Kill switch ACTIVE — trades blocked", level="critical")
            self._set_last_error("kill_switch", "kill switch active")
            return None

        if self._per_bot_halt_path and self._per_bot_halt_path.exists():
            self.log.warning("PER-BOT HALT active for %s — refusing trade on %s", self.bot_name, ticker)
            self._set_last_error("per_bot_halt", f"{self.bot_name} halted")
            return None

        if self.breaker.is_open():
            self.log.warning("Circuit breaker OPEN — skipping trade on %s", ticker)
            self._set_last_error("circuit_breaker", "circuit breaker open")
            return None

        max_daily = self.config["maxDailyTrades"]
        if self._daily_trades >= max_daily:
            self.log.warning("Daily trade limit (%d) reached — skipping %s", max_daily, ticker)
            self._set_last_error("daily_trade_limit", f"maxDailyTrades={max_daily}")
            return None

        if self.tracker.is_recent(ticker):
            self.log.info("Skipping %s — traded recently (dedup)", ticker)
            self._set_last_error("dedup", "traded recently")
            return None

        max_cost_cents = self._effective_max_trade_cents()
        cost_per_contract = price_cents

        max_contracts = self.config.get("maxContractsPerTrade")
        if max_contracts is not None and count > max_contracts:
            original_count = count
            count = max_contracts
            caps_applied.append("contracts_cap")
            self.log.info("Contracts cap: %dx → %dx on %s", original_count, count, ticker)

        max_gross_payout_cents = self.config.get("maxGrossPayoutCents")
        if max_gross_payout_cents is not None:
            payout_contract_cap = max(1, int(max_gross_payout_cents // 100))
            if count > payout_contract_cap:
                original_count = count
                count = payout_contract_cap
                caps_applied.append("gross_payout_cap")
                self.log.info(
                    "Gross payout cap: %dx → %dx on %s (max $%.2f payout)",
                    original_count,
                    count,
                    ticker,
                    max_gross_payout_cents / 100,
                )

        if cost_per_contract * count > max_cost_cents:
            original_count = count
            count = max(1, max_cost_cents // cost_per_contract)
            caps_applied.append("cost_cap")
            self.log.info("Cost cap: %dx → %dx on %s (max $%.2f)", original_count, count, ticker, max_cost_cents / 100)

        max_loss_cents = self._effective_max_daily_loss_cents()
        risk_cents = price_cents * count
        if self._daily_spend_cents + risk_cents > max_loss_cents:
            self.log.warning(
                "Daily loss limit ($%.2f) would be exceeded — risked $%.2f + $%.2f > $%.2f. Skipping %s",
                max_loss_cents / 100,
                self._daily_spend_cents / 100,
                risk_cents / 100,
                max_loss_cents / 100,
                ticker,
            )
            self._set_last_error("daily_loss_limit", f"daily loss limit ${max_loss_cents/100:.0f} reached")
            if self._daily_loss_first_blocked_at is None:
                self._daily_loss_first_blocked_at = time.time()
            self._daily_loss_block_count += 1
            if not self._daily_loss_alerted:
                self._notify(f"Daily loss limit (${max_loss_cents/100:.0f}) reached — trades blocked", level="warning")
                self._daily_loss_alerted = True
            blocked_for = time.time() - self._daily_loss_first_blocked_at
            if not self._daily_loss_escalated and self._daily_loss_block_count >= 3 and blocked_for >= 900:
                label = self.bot_name or self.log.name
                self._notify(
                    f"{label}: daily loss limit still blocking trades after {int(blocked_for // 60)}m "
                    f"({self._daily_loss_block_count} blocked attempts)",
                    level="warning",
                )
                self._daily_loss_escalated = True
            return None

        cost_cents = cost_per_contract * count
        if available_balance_cents is not None and cost_cents > available_balance_cents:
            self.log.warning(
                "Insufficient balance: need %dc but only %dc available. Skipping %s",
                cost_cents,
                available_balance_cents,
                ticker,
            )
            self._set_last_error("balance", f"need={cost_cents} available={available_balance_cents}")
            return None

        if market_data_age_seconds is not None and market_data_age_seconds > 600:
            self.log.warning("Market data is %.0fs old (>600s stale threshold). Skipping %s", market_data_age_seconds, ticker)
            self._set_last_error("stale_data", f"age={market_data_age_seconds}")
            return None

        if self._check_kill_switch(self.kill_switch_path):
            self.log.warning("KILL SWITCH ACTIVE (late check) — refusing trade on %s", ticker)
            self._set_last_error("kill_switch", "kill switch active")
            return None

        order_body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
        }
        if side == "yes":
            order_body["yes_price"] = price_cents
        else:
            order_body["no_price"] = price_cents

        try:
            result = self.client.post("/portfolio/orders", body=order_body)
            order_info = result.get("order", {})
            self.breaker.record_success()
        except self._requests.exceptions.HTTPError as e:
            self.breaker.record_failure()
            error_code = "api_error"
            error_message = e.response.text[:300] if e.response is not None else str(e)
            if e.response is not None:
                try:
                    payload = e.response.json()
                    err = payload.get("error", {})
                    error_code = err.get("code", error_code)
                    error_message = err.get("message", error_message)
                except ValueError:
                    pass
            self._set_last_error(error_code, error_message)
            self.log.error("Order failed for %s: %s %s", ticker, getattr(e.response, "status_code", "?"), error_message)
            if error_code == "market_not_found":
                alert_key = f"market_not_found:{self.bot_name or self.log.name}:{ticker}"
                if self._should_send_local_alert(alert_key):
                    self._notify(f"{self.bot_name or self.log.name}: market_not_found on {ticker}", level="warning", logger=self.log)
            return None
        except Exception as e:
            self.breaker.record_failure()
            self._set_last_error("api_error", str(e))
            self.log.error("Order failed for %s: %s", ticker, e)
            return None

        order_id = order_info.get("order_id")
        extra_fields["caps_applied"] = caps_applied
        trade_record = self._build_golden_record(ticker, side, price_cents, count, cost_cents, reasoning, order_info, **extra_fields)
        order_info["count"] = count
        order_info["cost_cents"] = cost_cents
        order_info["caps_applied"] = list(caps_applied)
        if order_id:
            try:
                self._write_wal({"order_id": order_id, "ticker": ticker, "record": trade_record})
            except Exception:
                pass

        self._daily_trades += 1
        if side == "no":
            self._daily_spend_cents += price_cents * count
        else:
            self._daily_spend_cents += cost_cents
        self._daily_loss_block_count = 0
        self._daily_loss_first_blocked_at = None
        self._daily_loss_escalated = False

        if self.order_monitor and order_id:
            self.order_monitor.track(order_id, ticker, side, price_cents, count)

        self._save_trade(self.trades_path, trade_record)
        self.tracker.record(ticker)

        if order_id:
            try:
                self._clear_wal(order_id)
            except Exception:
                pass

        self.log.info(
            "Order placed: %dx %s @ %dc on %s (ID: %s, Status: %s)",
            count,
            side,
            price_cents,
            ticker,
            order_info.get("order_id"),
            order_info.get("status"),
        )
        return order_info

    def sell_position(self, ticker, side, price_cents, count, reasoning, order_type="limit", **extra_fields):
        if side not in ("yes", "no"):
            self.log.error("Invalid side '%s' — must be 'yes' or 'no'", side)
            return None

        if self._check_kill_switch(self.kill_switch_path):
            self.log.warning("KILL SWITCH ACTIVE — refusing exit on %s", ticker)
            return None
        if self.breaker.is_open():
            self.log.warning("Circuit breaker OPEN — skipping exit on %s", ticker)
            return None

        cooldown_key = (ticker, side)
        last_sell = self._sell_cooldown.get(cooldown_key)
        if last_sell and (time.time() - last_sell) < self._sell_cooldown_seconds:
            remaining = self._sell_cooldown_seconds - (time.time() - last_sell)
            self.log.warning("Sell cooldown: %s %s — %.0fs remaining", ticker, side, remaining)
            return None

        order_body = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "type": order_type,
            "count": count,
        }
        if side == "yes":
            order_body["yes_price"] = price_cents
        else:
            order_body["no_price"] = price_cents

        try:
            result = self.client.post("/portfolio/orders", body=order_body)
            order_info = result.get("order", {})
            self.breaker.record_success()
        except self._requests.exceptions.HTTPError as e:
            self.breaker.record_failure()
            self.log.error("Sell order failed for %s: %s %s", ticker, e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:
            self.breaker.record_failure()
            self.log.error("Sell order failed for %s: %s", ticker, e)
            return None

        cost_cents = count * price_cents
        trade_record = self._build_golden_record(
            ticker,
            side,
            price_cents,
            count,
            cost_cents,
            reasoning,
            {"order_id": order_info.get("order_id"), "status": order_info.get("status")},
            **extra_fields,
        )
        trade_record["action"] = "sell"
        self._save_trade(self.trades_path, trade_record)
        self._sell_cooldown[cooldown_key] = time.time()

        self.log.info(
            "EXIT placed: sell %dx %s @ %dc on %s (ID: %s, Status: %s)",
            count,
            side,
            price_cents,
            ticker,
            order_info.get("order_id"),
            order_info.get("status"),
        )
        return order_info

    def log_decision(self, ticker, side, action, reason, edge=None, price_cents=None, **extra):
        utc_now_iso = getattr(self, "_utc_now_iso", _utc_now_iso)
        save_decision_func = getattr(self, "_save_decision", save_decision)
        decisions_path = self.trades_path.parent / f"{self.trades_path.stem}-decisions.json"
        record = {
            "timestamp": utc_now_iso(),
            "ticker": ticker,
            "side": side,
            "action": action,
            "reason": reason,
            "source_bot": self.log.name,
        }
        if edge is not None:
            record["edge"] = round(edge, 4)
        if price_cents is not None:
            record["price_cents"] = price_cents
        record.update(extra)
        self._apply_research_metadata(record)
        save_decision_func(decisions_path, record)


__all__ = [
    "RecentTradeTracker",
    "TradeManager",
    "trim_trade_log",
    "validate_trade_config",
]
