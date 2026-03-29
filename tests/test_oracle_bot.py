"""Integration tests for Oracle NBA bot."""

import json
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import apps.oracle_bot as oracle_bot

from domain.oracle.models import Book, Signal, OraclePosition
from domain.oracle.risk.ledger import OracleRiskLedger
from domain.oracle.risk.internal_limits import check_all_limits
from domain.oracle.risk.sizing import contracts_for_book
from domain.oracle.execution.fill_monitor import FillMonitor


# ── Signal execution pipeline tests ──

def test_signal_to_execution_pipeline():
    """Verify Signal objects carry all fields needed for execution."""
    signal = Signal(
        book=Book.A,
        ticker="KXNBA-18MAR26-LALHOU-LAL",
        side="yes",
        model_prob=0.68,
        kalshi_price=0.49,
        edge=0.19,
        metadata={
            "signal_type": "divergence",
            "game_id": "LAL-HOU-20260318",
            "kalshi_price_cents": 49,
        },
    )
    assert signal.book == Book.A
    assert signal.side == "yes"
    assert signal.edge == 0.19
    assert signal.metadata["kalshi_price_cents"] == 49
    assert signal.metadata["game_id"] == "LAL-HOU-20260318"


def test_oracle_alpha_capture_records_skipped_decision(monkeypatch):
    fake_capture = _FakeOracleAlphaCapture()
    monkeypatch.setattr(oracle_bot, "_get_oracle_alpha_capture", lambda: fake_capture)
    monkeypatch.setattr(oracle_bot, "save_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(oracle_bot, "_demo_mode", False)

    signal = Signal(
        book=Book.C,
        ticker="KXNBAGAME-26MAR22GSWATL-GSW",
        side="no",
        model_prob=0.54,
        kalshi_price=0.60,
        edge=0.06,
        metadata={
            "signal_type": "clutch_comeback",
            "game_id": "ATL-GSW-20260322",
            "player_name": "",
            "kalshi_yes_bid": 59,
            "kalshi_yes_ask": 61,
            "kalshi_yes_bid_depth": 12,
            "kalshi_yes_ask_depth": 14,
        },
    )

    oracle_bot._log_decision(signal, triggered=False, reason="limit: blocked", contracts=0)

    assert len(fake_capture.signals) == 1
    assert fake_capture.orders == []
    record = fake_capture.signals[0]
    assert record["triggered"] is False
    assert record["reason"] == "limit: blocked"
    assert record["mode"] == "shadow"
    assert record["entry_type"] == "passive"
    assert record["yes_bid"] == 59
    assert record["yes_ask"] == 61
    assert record["signal_type"] == "clutch_comeback"
    assert record["market_ticker"] == signal.ticker


def test_oracle_alpha_capture_records_demo_signal(monkeypatch):
    fake_capture = _FakeOracleAlphaCapture()
    monkeypatch.setattr(oracle_bot, "_get_oracle_alpha_capture", lambda: fake_capture)
    monkeypatch.setattr(oracle_bot, "save_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(oracle_bot, "_demo_mode", True)

    signal = Signal(
        book=Book.C,
        ticker="KXNBAPTS-22MAR26-ATLJOHNSONJ-O27",
        side="yes",
        model_prob=0.63,
        kalshi_price=0.24,
        edge=0.39,
        metadata={
            "signal_type": "ot_likely",
            "game_id": "ATL-BOS-20260322",
            "player_id": 42,
            "team": "Atlanta Hawks",
            "stat_type": "points",
            "kalshi_yes_bid": 23,
            "kalshi_yes_ask": 24,
            "kalshi_yes_bid_depth": 11,
            "kalshi_yes_ask_depth": 12,
        },
    )

    oracle_bot._log_decision(signal, triggered=True, reason="demo mode", contracts=3)

    assert len(fake_capture.signals) == 1
    assert len(fake_capture.orders) == 1
    record = fake_capture.signals[0]
    assert record["triggered"] is True
    assert record["reason"] == "demo mode"
    assert record["mode"] == "demo"
    assert record["contracts"] == 3
    assert record["player_id"] == 42
    assert record["stat"] == "points"
    assert record["signal_type"] == "ot_likely"
    order = fake_capture.orders[0]
    assert order["signal_id"] == record["signal_id"]
    assert order["mode"] == "demo"
    assert order["status"] == "shadow"
    assert order["signal_type"] == "ot_likely"


def test_execute_signal_records_order_metadata_without_false_fill(monkeypatch):
    fake_capture = _FakeOracleAlphaCapture()
    monkeypatch.setattr(oracle_bot, "_get_oracle_alpha_capture", lambda: fake_capture)
    monkeypatch.setattr(oracle_bot, "save_decision", lambda *args, **kwargs: None)
    monkeypatch.setattr(oracle_bot, "_demo_mode", False)
    monkeypatch.setattr(oracle_bot, "oracle_config", {"books": {"C": {"enabled": True}}})
    monkeypatch.setattr(oracle_bot, "contracts_for_book", lambda *args, **kwargs: 2)
    monkeypatch.setattr(oracle_bot, "expected_value_cents", lambda *args, **kwargs: 12)
    monkeypatch.setattr(
        oracle_bot,
        "check_all_limits",
        lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""),
    )
    monkeypatch.setattr(oracle_bot, "check_cross_book_sizing", lambda *args, **kwargs: 1)

    class _FakeLedger:
        def __init__(self):
            self.positions = []
            self.order_successes = 0
            self.order_failures = 0

        def add_position(self, position):
            self.positions.append(position)

        def record_order_success(self):
            self.order_successes += 1

        def record_order_failure(self):
            self.order_failures += 1

        def has_position(self, ticker):
            return False

    fake_ledger = _FakeLedger()
    monkeypatch.setattr(oracle_bot, "ledger", fake_ledger)
    monkeypatch.setattr(
        oracle_bot,
        "allocator",
        SimpleNamespace(request_budget=lambda *args, **kwargs: SimpleNamespace(approved=True, reason="ok")),
    )
    monkeypatch.setattr(
        oracle_bot,
        "trade_manager",
        SimpleNamespace(
            place_order=lambda *args, **kwargs: {
                "order_id": "ord_1",
                "status": "resting",
                "count": 2,
                "price_cents": 60,
            }
        ),
    )
    monkeypatch.setattr(oracle_bot, "log", logging.getLogger("test.oracle_bot"))

    signal = Signal(
        book=Book.C,
        ticker="KXNBAGAME-26MAR22GSWATL-GSW",
        side="no",
        model_prob=0.54,
        kalshi_price=0.60,
        edge=0.06,
        metadata={
            "signal_type": "clutch_comeback",
            "game_id": "ATL-GSW-20260322",
            "kalshi_yes_bid": 59,
            "kalshi_yes_ask": 61,
            "kalshi_yes_bid_depth": 12,
            "kalshi_yes_ask_depth": 14,
        },
    )

    result = oracle_bot._execute_signal(signal, bankroll_cents=100000)

    assert result is True
    assert len(fake_capture.signals) == 1
    assert len(fake_capture.orders) == 1
    record = fake_capture.signals[0]
    assert record["triggered"] is True
    assert record["reason"] == "executed"
    assert record["mode"] == "live"
    assert record["order_id"] == "ord_1"
    assert record["order_status"] == "resting"
    assert record["order_count"] == 2
    assert record["signal_type"] == "clutch_comeback"
    order = fake_capture.orders[0]
    assert order["signal_id"] == record["signal_id"]
    assert order["order_id"] == "ord_1"
    assert order["status"] == "resting"
    assert order["mode"] == "live"
    assert fake_capture.fills == []


def test_limit_check_passes_fresh_ledger(tmp_path):
    """Fresh ledger with no positions should pass all limit checks."""
    ledger = OracleRiskLedger(state_path=tmp_path / "state.json")
    config = {
        "risk": {
            "maxTotalExposurePct": 0.18,
            "maxSimultaneousPositions": 10,
            "dailyStopLossPct": 0.05,
            "maxDrawdownPct": 0.15,
            "maxPropsPerPlayer": 2,
            "maxPropsPerGame": 4,
            "noOppositeSidesOnSpread": True,
        },
        "books": {
            "A": {
                "maxPositions": 4,
                "maxExposurePct": 0.08,
                "maxPerGamePct": 0.04,
            },
        },
        "killSwitches": {
            "consecutiveOrderFailures": 3,
            "singleBookDrawdownPct": 0.10,
        },
    }
    result = check_all_limits(
        book=Book.A,
        ledger=ledger,
        bankroll_cents=500000,
        config=config,
        game_id="LAL-HOU-20260318",
    )
    assert result.allowed


def test_limit_check_blocks_over_position_count(tmp_path):
    """Exceeding max positions should block new trades."""
    ledger = OracleRiskLedger(state_path=tmp_path / "state.json")
    # Fill up 4 positions (Book A max)
    for i in range(4):
        ledger.add_position(OraclePosition(
            book=Book.A,
            ticker=f"KXNBA-T{i}",
            side="yes",
            contracts=10,
            entry_price_cents=50,
        ))

    config = {
        "risk": {
            "maxTotalExposurePct": 0.18,
            "maxSimultaneousPositions": 10,
            "dailyStopLossPct": 0.05,
            "maxDrawdownPct": 0.15,
        },
        "books": {
            "A": {
                "maxPositions": 4,
                "maxExposurePct": 0.50,
                "maxPerGamePct": 0.40,
            },
        },
        "killSwitches": {
            "consecutiveOrderFailures": 3,
            "singleBookDrawdownPct": 0.10,
        },
    }
    result = check_all_limits(
        book=Book.A,
        ledger=ledger,
        bankroll_cents=500000,
        config=config,
    )
    assert not result.allowed
    assert "position" in result.reason.lower()


def test_sizing_for_each_book():
    """Verify fixed-fractional sizing produces correct contracts per book."""
    bankroll = 500000  # $5000 = 500000c

    # Book A: 2% of $5000 = $100 = 10000c / 50c = 200, capped at 100
    contracts_a = contracts_for_book("A", bankroll, 50)
    assert contracts_a == 100  # max_contracts default cap

    # With higher cap
    contracts_a_uncapped = contracts_for_book("A", bankroll, 50, max_contracts=500)
    assert contracts_a_uncapped == 200

    # Book B: 1.5% of $5000 = $75 = 7500c / 50c = 150, capped at 100
    contracts_b = contracts_for_book("B", bankroll, 50)
    assert contracts_b == 100

    # Book C: 1% of $5000 = $50 = 5000c / 50c = 100
    contracts_c = contracts_for_book("C", bankroll, 50)
    assert contracts_c == 100


def test_fill_monitor_integration():
    """Fill monitor tracks slippage across multiple fills."""
    fm = FillMonitor(
        fill_timeout_seconds=15,
        max_reprices=2,
        slippage_disable_threshold_cents=3,
        slippage_disable_min_trades=3,
    )
    assert not fm.is_disabled

    # Three fills with acceptable slippage
    for i in range(3):
        attempt = fm.start_fill(f"KXNBA-T{i}", 50)
        fm.record_fill(attempt, 51)  # 1c slippage

    assert not fm.is_disabled

    stats = fm.stats()
    assert stats["fills"] == 3
    assert stats["avg_slippage_cents"] == 1.0


def test_fill_monitor_disables_on_high_slippage():
    """High average slippage auto-disables Book C."""
    fm = FillMonitor(
        slippage_disable_threshold_cents=2,
        slippage_disable_min_trades=3,
    )

    # Three fills with 5c slippage each
    for i in range(3):
        attempt = fm.start_fill(f"KXNBA-T{i}", 50)
        fm.record_fill(attempt, 55)

    assert fm.is_disabled


def test_ledger_roundtrip(tmp_path):
    """Ledger persists and reloads state correctly."""
    state_path = tmp_path / "risk-state.json"

    # Create and populate ledger
    ledger1 = OracleRiskLedger(state_path=state_path)
    ledger1.add_position(OraclePosition(
        book=Book.A,
        ticker="KXNBA-TEST",
        side="yes",
        contracts=10,
        entry_price_cents=50,
        game_id="LAL-HOU-20260318",
    ))
    ledger1.record_pnl(500)  # +$5

    # Reload from disk
    ledger2 = OracleRiskLedger(state_path=state_path)
    assert ledger2.position_count() == 1
    assert ledger2.total_exposure_cents() == 500
    assert ledger2.daily_pnl_cents == 500


def test_kill_switch_on_consecutive_failures(tmp_path):
    """Three consecutive order failures trigger kill switch."""
    ledger = OracleRiskLedger(state_path=tmp_path / "state.json")
    ledger.record_order_failure()
    ledger.record_order_failure()
    ledger.record_order_failure()

    config = {
        "risk": {
            "maxTotalExposurePct": 0.18,
            "maxSimultaneousPositions": 10,
            "dailyStopLossPct": 0.05,
            "maxDrawdownPct": 0.15,
        },
        "books": {"A": {"maxPositions": 4, "maxExposurePct": 0.08}},
        "killSwitches": {
            "consecutiveOrderFailures": 3,
            "singleBookDrawdownPct": 0.10,
        },
    }
    result = check_all_limits(
        book=Book.A, ledger=ledger, bankroll_cents=500000, config=config,
    )
    assert not result.allowed
    assert "consecutive" in result.reason.lower()


def test_daily_stop_loss_halts_trading(tmp_path):
    """Daily P&L exceeding stop loss threshold halts all trading."""
    ledger = OracleRiskLedger(state_path=tmp_path / "state.json")
    # Lose 6% of $5000 = -$300
    ledger.record_pnl(-30000)

    config = {
        "risk": {
            "maxTotalExposurePct": 0.18,
            "maxSimultaneousPositions": 10,
            "dailyStopLossPct": 0.05,
            "maxDrawdownPct": 0.15,
        },
        "books": {"A": {"maxPositions": 4, "maxExposurePct": 0.08}},
        "killSwitches": {
            "consecutiveOrderFailures": 3,
            "singleBookDrawdownPct": 0.10,
        },
    }
    result = check_all_limits(
        book=Book.A, ledger=ledger, bankroll_cents=500000, config=config,
    )
    assert not result.allowed
    assert "stop" in result.reason.lower() or "daily" in result.reason.lower()


# ── Config loading tests ──

def test_oracle_config_structure():
    """Verify the oracle config section has the expected structure."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "bots-config.json"
    with open(config_path) as f:
        full_config = json.load(f)

    oracle = full_config["oracle"]
    assert oracle["enabled"] is False
    assert oracle["maxTradeAmount"] == 25
    assert oracle["maxDailyTrades"] == 30
    # Risk
    risk = oracle["risk"]
    assert risk["maxTotalExposurePct"] == 0.18
    assert risk["maxSimultaneousPositions"] == 10
    assert risk["dailyStopLossPct"] == 0.05
    assert risk["maxDrawdownPct"] == 0.15

    # Books
    books = oracle["books"]
    assert "A" in books
    assert "B" in books
    assert "C" in books
    assert books["A"]["minEdge"] == 0.15
    assert books["B"]["minEdge"] == 0.10
    assert books["C"]["minEdge"] == 0.12

    # Kill switches
    ks = oracle["killSwitches"]
    assert ks["consecutiveOrderFailures"] == 3


def test_load_config_honors_enabled_override(monkeypatch, tmp_path):
    config_path = tmp_path / "bots-config.json"
    config_path.write_text(json.dumps({"oracle": {"enabled": False, "books": {"C": {"enabled": True}}}}))
    monkeypatch.setattr(oracle_bot, "BOTS_CONFIG_PATH", config_path)
    monkeypatch.setenv("ORACLE_ENABLED_OVERRIDE", "yes")

    config = oracle_bot._load_config()

    assert config["enabled"] is True
    assert config["books"]["C"]["enabled"] is True


class _FakeRealClient:
    def __init__(self, home_feed, game_details):
        self._home_feed = home_feed
        self._game_details = game_details
        self.game_detail_calls = []

    async def get_home_feed(self, sport="nba"):
        return self._home_feed

    async def get_game_detail(self, game_id, sport="nba"):
        self.game_detail_calls.append((game_id, sport))
        return self._game_details[game_id]

    async def close(self):
        return None


class _FakeKalshiClient:
    def __init__(self, orderbooks):
        self._orderbooks = orderbooks

    def get_orderbook(self, ticker):
        return self._orderbooks[ticker]


class _FakeFillMonitor:
    is_disabled = False

    def reset_disable(self):
        return None


class _FakeRealWebSocket:
    def __init__(self, *, connected=True, stale=False, disabled=False):
        self.connected = connected
        self.is_stale = stale
        self.is_disabled = disabled

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False


class _FakeOracleAlphaCapture:
    def __init__(self):
        self.signals = []
        self.orders = []
        self.fills = []

    def record_signal(self, **kwargs):
        payload = dict(kwargs)
        extra = payload.pop("extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        payload.setdefault("signal_id", f"signal-{len(self.signals) + 1}")
        self.signals.append(payload)
        return payload

    def record_fill(self, **kwargs):
        payload = dict(kwargs)
        self.fills.append(payload)
        return payload

    def record_order_submission(self, **kwargs):
        payload = dict(kwargs)
        extra = payload.pop("extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        self.orders.append(payload)
        return payload


class _FakeHealthMonitor:
    def __init__(self):
        self._state = {"bots": {}, "sources": {}}
        self._dirty_bots = set()
        self.saved = 0
        self.source_errors = []
        self.source_warnings = []
        self.source_successes = []

    def record_bot_heartbeat(self, bot):
        self._state["bots"].setdefault(bot, {})["last_heartbeat"] = "2026-03-22T00:00:00+00:00"
        self._dirty_bots.add(bot)

    def update_bot_state(self, bot, **fields):
        self._state["bots"].setdefault(bot, {}).update(fields)
        self._dirty_bots.add(bot)
        self._save()

    def record_source_success(self, source):
        self.source_successes.append(source)

    def record_source_warning(self, source, msg=""):
        self.source_warnings.append((source, msg))

    def record_source_error(self, source, msg=""):
        self.source_errors.append((source, msg))

    def _save(self):
        self.saved += 1


def _configure_book_c_runtime(monkeypatch, tmp_path, *, home_feed, game_details, orderbooks):
    monkeypatch.setattr(oracle_bot, "log", logging.getLogger("test.oracle_bot"))
    monkeypatch.setattr(
        oracle_bot,
        "oracle_config",
        {
            "edgeThreshold": 0.10,
            "books": {
                "C": {
                    "enabled": True,
                    "minEdge": 0.12,
                    "maxSpreadCents": 8,
                    "minDepthContracts": 5,
                },
            },
        },
    )
    monkeypatch.setattr(oracle_bot, "fill_monitor", _FakeFillMonitor())
    monkeypatch.setattr(oracle_bot, "ledger", OracleRiskLedger(state_path=tmp_path / "oracle-risk.json"))
    monkeypatch.setattr(oracle_bot, "real_client", _FakeRealClient(home_feed, game_details))
    monkeypatch.setattr(oracle_bot, "client", _FakeKalshiClient(orderbooks))
    monkeypatch.setattr(oracle_bot, "real_ws", None)
    monkeypatch.setattr(oracle_bot, "_book_c_live_state_cache", oracle_bot._BookCLiveStateCache())
    monkeypatch.setattr(oracle_bot, "health", _FakeHealthMonitor())
    monkeypatch.setattr(
        oracle_bot,
        "_BOOK_C_SCAN_DIAGNOSTICS",
        {
            "last_scan_utc": None,
            "live_games": 0,
            "game_opportunities": 0,
            "prop_opportunities": 0,
            "total_opportunities": 0,
            "signals": 0,
            "zero_signal_live_scans": 0,
            "zero_signal_live_streak": 0,
            "zero_signal_alert_active": False,
            "last_nonzero_signal_utc": None,
            "last_zero_signal_warning_utc": None,
        },
    )


def _stat_values(**stats):
    stat_type_map = {
        "points": 1,
        "assists": 2,
        "rebounds": 3,
        "steals": 4,
        "blocks": 5,
        "turnovers": 6,
        "minutes": 8,
        "fouls": 25,
        "three_pointers": 27,
    }
    rows = []
    for key, value in stats.items():
        rows.append({"type": stat_type_map[key], "value": value})
    return rows


def test_scan_book_c_generates_clutch_comeback_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(oracle_bot, "log", logging.getLogger("test.oracle_bot"))
    monkeypatch.setattr(
        oracle_bot,
        "oracle_config",
        {
            "edgeThreshold": 0.10,
            "books": {
                "C": {
                    "enabled": True,
                    "minEdge": 0.12,
                    "maxSpreadCents": 8,
                    "minDepthContracts": 5,
                },
            },
        },
    )
    monkeypatch.setattr(oracle_bot, "fill_monitor", _FakeFillMonitor())
    monkeypatch.setattr(oracle_bot, "ledger", OracleRiskLedger(state_path=tmp_path / "oracle-risk.json"))

    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 23454,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Golden State Warriors"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q4",
                    "homeTeamScore": 101,
                    "awayTeamScore": 99,
                },
            ],
        },
    }
    game_details = {
        23454: {
            "game": {
                "homeScore": 101,
                "awayScore": 99,
                "period": "Q4",
                "clock": "1:15",
            },
        },
    }
    monkeypatch.setattr(oracle_bot, "real_client", _FakeRealClient(home_feed, game_details))

    trailing_ticker = "KXNBAGAME-26MAR22GSWATL-GSW"
    monkeypatch.setattr(
        oracle_bot,
        "client",
        _FakeKalshiClient(
            {
                trailing_ticker: {
                    "orderbook_fp": {
                        "yes_dollars": [["0.4000", "12.00"]],
                        "no_dollars": [["0.5900", "15.00"]],
                    },
                },
            }
        ),
    )

    kalshi_markets = [
        {"ticker": trailing_ticker},
        {"ticker": "KXNBAGAME-26MAR22GSWATL-ATL"},
    ]
    signals = asyncio.run(oracle_bot.scan_book_c(kalshi_markets))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.book == Book.C
    assert signal.ticker == trailing_ticker
    assert signal.side == "no"
    assert signal.metadata["signal_type"] == "clutch_comeback"
    assert signal.metadata["trailing_team"] == "Golden State Warriors"
    assert signal.metadata["leading_team"] == "Atlanta Hawks"
    assert signal.metadata["game_id"] == "ATL-GSW-20260322"
    assert signal.metadata["kalshi_yes_bid"] == 40
    assert signal.metadata["kalshi_yes_ask"] == 41
    assert signal.metadata["kalshi_price_cents"] == 60


def test_scan_book_c_rejects_wide_spread_quote(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 23454,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Golden State Warriors"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q4",
                    "homeTeamScore": 101,
                    "awayTeamScore": 99,
                },
            ],
        },
    }
    game_details = {
        23454: {
            "game": {
                "homeScore": 101,
                "awayScore": 99,
                "period": "Q4",
                "clock": "1:15",
            },
        },
    }
    trailing_ticker = "KXNBAGAME-26MAR22GSWATL-GSW"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            trailing_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.4000", "12.00"]],
                    "no_dollars": [["0.4000", "15.00"]],
                },
            },
        },
    )

    kalshi_markets = [
        {"ticker": trailing_ticker},
        {"ticker": "KXNBAGAME-26MAR22GSWATL-ATL"},
    ]
    signals = asyncio.run(oracle_bot.scan_book_c(kalshi_markets))

    assert signals == []


def test_scan_book_c_records_zero_signal_warning_and_recovers(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 23454,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Golden State Warriors"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q4",
                    "homeTeamScore": 101,
                    "awayTeamScore": 99,
                },
            ],
        },
    }
    game_details = {
        23454: {
            "game": {
                "homeScore": 101,
                "awayScore": 99,
                "period": "Q4",
                "clock": "1:15",
            },
        },
    }
    trailing_ticker = "KXNBAGAME-26MAR22GSWATL-GSW"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            trailing_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.2000", "12.00"]],
                    "no_dollars": [["0.7900", "15.00"]],
                },
            },
        },
    )

    kalshi_markets = [
        {"ticker": trailing_ticker},
        {"ticker": "KXNBAGAME-26MAR22GSWATL-ATL"},
    ]

    for _ in range(3):
        signals = asyncio.run(oracle_bot.scan_book_c(kalshi_markets))
        assert signals == []

    diagnostics = oracle_bot._BOOK_C_SCAN_DIAGNOSTICS
    assert diagnostics["zero_signal_live_streak"] == 3
    assert diagnostics["zero_signal_alert_active"] is True
    assert diagnostics["last_zero_signal_warning_utc"] is not None
    assert oracle_bot.health.source_warnings == [
        (
            "oracle-book-c-zero-signals",
            "Book C produced 0 signals across 3 consecutive scans with 1 live opportunities",
        )
    ]
    assert oracle_bot.health.source_errors == []

    oracle_bot.client._orderbooks[trailing_ticker] = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "12.00"]],
            "no_dollars": [["0.5900", "15.00"]],
        },
    }
    signals = asyncio.run(oracle_bot.scan_book_c(kalshi_markets))

    assert len(signals) == 1
    assert signals[0].metadata["signal_type"] == "clutch_comeback"
    assert oracle_bot.health.source_successes == ["oracle-book-c-zero-signals"]
    assert oracle_bot._BOOK_C_SCAN_DIAGNOSTICS["zero_signal_live_streak"] == 0
    assert oracle_bot._BOOK_C_SCAN_DIAGNOSTICS["zero_signal_alert_active"] is False


def test_record_scan_health_persists_book_c_metrics(tmp_path, monkeypatch):
    fake_health = _FakeHealthMonitor()
    monkeypatch.setattr(oracle_bot, "health", fake_health)
    monkeypatch.setattr(
        oracle_bot,
        "ledger",
        OracleRiskLedger(state_path=tmp_path / "oracle-risk.json"),
    )
    monkeypatch.setattr(
        oracle_bot,
        "_BOOK_C_SCAN_DIAGNOSTICS",
        {
            "last_scan_utc": "2026-03-22T23:59:00+00:00",
            "live_games": 2,
            "game_opportunities": 1,
            "prop_opportunities": 3,
            "total_opportunities": 4,
            "signals": 0,
            "zero_signal_live_scans": 3,
            "zero_signal_live_streak": 3,
            "zero_signal_alert_active": True,
            "last_nonzero_signal_utc": None,
            "last_zero_signal_warning_utc": "2026-03-22T23:58:00+00:00",
        },
    )

    oracle_bot._record_scan_health(12, 1, 2, 3, 4)

    oracle_state = fake_health._state["bots"]["oracle"]
    assert oracle_state["scan_metrics"]["book_c"]["zero_signal_live_streak"] == 3
    assert oracle_state["scan_metrics"]["book_c"]["zero_signal_alert_active"] is True
    assert oracle_state["scan_metrics"]["book_c"]["prop_opportunities"] == 3
    assert fake_health.saved >= 1


def test_scan_book_c_generates_ot_prop_signal_without_game_market(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1001,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q4",
                    "homeTeamScore": 101,
                    "awayTeamScore": 101,
                },
            ],
        },
    }
    game_details = {
        1001: {
            "game": {"homeScore": 101, "awayScore": 101, "period": "Q4", "clock": "1:00"},
            "playerBoxScores": [
                {
                    "playerId": 42,
                    "player": {"name": "Jalen Johnson"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=24, minutes=30, fouls=2),
                },
            ],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLJOHNSONJ-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.2300", "11.00"]],
                    "no_dollars": [["0.7600", "12.00"]],
                },
            },
        },
    )

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.ticker == prop_ticker
    assert signal.side == "yes"
    assert signal.metadata["signal_type"] == "ot_likely"
    assert signal.metadata["stat_type"] == "points"
    assert signal.metadata["player_name"] == "Jalen Johnson"
    assert signal.metadata["kalshi_price_cents"] == 24


def test_scan_book_c_generates_blowout_prop_signal(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1002,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q3",
                    "homeTeamScore": 100,
                    "awayTeamScore": 75,
                },
            ],
        },
    }
    game_details = {
        1002: {
            "game": {"homeScore": 100, "awayScore": 75, "period": "Q3", "clock": "5:00"},
            "playerBoxScores": [
                {
                    "playerId": 11,
                    "player": {"name": "Trae Young"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=15, minutes=24, fouls=1),
                },
            ],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLYOUNGT-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.5000", "10.00"]],
                    "no_dollars": [["0.4500", "12.00"]],
                },
            },
        },
    )

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.ticker == prop_ticker
    assert signal.side == "no"
    assert signal.metadata["signal_type"] == "blowout"
    assert signal.metadata["player_name"] == "Trae Young"


def test_scan_book_c_generates_foul_trouble_prop_signal(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1003,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q3",
                    "homeTeamScore": 88,
                    "awayTeamScore": 84,
                },
            ],
        },
    }
    game_details = {
        1003: {
            "game": {"homeScore": 88, "awayScore": 84, "period": "Q3", "clock": "6:00"},
            "playerBoxScores": [
                {
                    "playerId": 42,
                    "player": {"name": "Jalen Johnson"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=15, minutes=26, fouls=4),
                },
            ],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLJOHNSONJ-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.5500", "10.00"]],
                    "no_dollars": [["0.4400", "12.00"]],
                },
            },
        },
    )

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.ticker == prop_ticker
    assert signal.side == "no"
    assert signal.metadata["signal_type"] == "foul_trouble"
    assert signal.metadata["fouls"] == 4


def test_scan_book_c_uses_websocket_cache_before_rest(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1002,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q3",
                    "homeTeamScore": 100,
                    "awayTeamScore": 75,
                },
            ],
        },
    }
    game_details = {
        1002: {
            "game": {"homeScore": 0, "awayScore": 0, "period": "Q1", "clock": "12:00"},
            "playerBoxScores": [],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLYOUNGT-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.5000", "10.00"]],
                    "no_dollars": [["0.4500", "12.00"]],
                },
            },
        },
    )
    monkeypatch.setattr(
        oracle_bot,
        "real_ws",
        _FakeRealWebSocket(connected=True, stale=False, disabled=False),
    )

    cache = oracle_bot._book_c_live_state_cache
    asyncio.run(
        oracle_bot._handle_real_game_updated(
            SimpleNamespace(
                game_id=1002,
                data={
                    "gameId": 1002,
                    "homeScore": 100,
                    "awayScore": 75,
                    "period": "Q3",
                    "clock": "5:00",
                },
            )
        )
    )
    cache.set_game_context(
        1002,
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics"},
    )
    asyncio.run(
        oracle_bot._handle_real_player_box_score_updated(
            SimpleNamespace(
                game_id=1002,
                data={
                    "gameId": 1002,
                    "data": {
                        "playerBoxScore": {
                            "playerId": 11,
                            "player": {"name": "Trae Young"},
                            "team": {"name": "Atlanta Hawks"},
                            "started": True,
                            "statValues": _stat_values(points=15, minutes=24, fouls=1),
                        },
                    },
                },
            )
        )
    )

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    assert signals[0].metadata["signal_type"] == "blowout"
    assert oracle_bot.real_client.game_detail_calls == []


def test_scan_book_c_uses_nested_data_wrapped_game_update(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1002,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q3",
                    "homeTeamScore": 100,
                    "awayTeamScore": 75,
                },
            ],
        },
    }
    game_details = {
        1002: {
            "game": {"homeScore": 0, "awayScore": 0, "period": "Q1", "clock": "12:00"},
            "playerBoxScores": [],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLYOUNGT-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.5000", "10.00"]],
                    "no_dollars": [["0.4500", "12.00"]],
                },
            },
        },
    )
    monkeypatch.setattr(
        oracle_bot,
        "real_ws",
        _FakeRealWebSocket(connected=True, stale=False, disabled=False),
    )

    cache = oracle_bot._book_c_live_state_cache
    asyncio.run(
        oracle_bot._handle_real_game_updated(
            SimpleNamespace(
                game_id=None,
                data={
                    "data": {
                        "gameId": 1002,
                        "homeScore": 100,
                        "awayScore": 75,
                        "period": "Q3",
                        "clock": "5:00",
                    },
                },
            )
        )
    )
    cache.set_game_context(
        1002,
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics"},
    )
    asyncio.run(
        oracle_bot._handle_real_player_box_score_updated(
            SimpleNamespace(
                game_id=None,
                data={
                    "data": {
                        "gameId": 1002,
                        "playerBoxScore": {
                            "playerId": 11,
                            "player": {"name": "Trae Young"},
                            "team": {"name": "Atlanta Hawks"},
                            "started": True,
                            "statValues": _stat_values(points=15, minutes=24, fouls=1),
                        },
                    },
                },
            )
        )
    )

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    assert signals[0].metadata["signal_type"] == "blowout"
    assert oracle_bot.real_client.game_detail_calls == []


def test_scan_book_c_falls_back_to_rest_when_live_cache_stale(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1003,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q3",
                    "homeTeamScore": 88,
                    "awayTeamScore": 84,
                },
            ],
        },
    }
    game_details = {
        1003: {
            "game": {"homeScore": 88, "awayScore": 84, "period": "Q3", "clock": "6:00"},
            "playerBoxScores": [
                {
                    "playerId": 42,
                    "player": {"name": "Jalen Johnson"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=15, minutes=26, fouls=4),
                },
            ],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLJOHNSONJ-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.5500", "10.00"]],
                    "no_dollars": [["0.4400", "12.00"]],
                },
            },
        },
    )
    monkeypatch.setattr(
        oracle_bot,
        "real_ws",
        _FakeRealWebSocket(connected=True, stale=True, disabled=False),
    )
    oracle_bot._book_c_live_state_cache.hydrate_from_game_detail(
        1003,
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics"},
        {
            "game": {"homeScore": 50, "awayScore": 49, "period": "Q2", "clock": "3:00"},
            "playerBoxScores": [],
        },
    )

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    assert signals[0].metadata["signal_type"] == "foul_trouble"
    assert oracle_bot.real_client.game_detail_calls == [(1003, "nba")]


def test_extract_live_player_states_prefers_nested_nonempty_box_score_list():
    payload = {
        "playerBoxScores": [],
        "data": {
            "playerBoxScores": [
                {
                    "playerId": 11,
                    "player": {"name": "Trae Young"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=15, minutes=24, fouls=1),
                },
            ],
        },
    }
    players = oracle_bot._extract_live_player_states(
        payload,
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics"},
    )

    assert len(players) == 1
    assert players[0]["player_name"] == "Trae Young"
    assert players[0]["points"] == 15


def test_scan_book_c_falls_back_to_rest_when_game_cache_entry_is_too_old(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1002,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q3",
                    "homeTeamScore": 100,
                    "awayTeamScore": 75,
                },
            ],
        },
    }
    game_details = {
        1002: {
            "game": {"homeScore": 100, "awayScore": 75, "period": "Q3", "clock": "5:00"},
            "playerBoxScores": [
                {
                    "playerId": 11,
                    "player": {"name": "Trae Young"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=15, minutes=24, fouls=1),
                },
            ],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLYOUNGT-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.5000", "10.00"]],
                    "no_dollars": [["0.4500", "12.00"]],
                },
            },
        },
    )
    monkeypatch.setattr(
        oracle_bot,
        "real_ws",
        _FakeRealWebSocket(connected=True, stale=False, disabled=False),
    )
    oracle_bot._book_c_live_state_cache.hydrate_from_game_detail(
        1002,
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics"},
        {
            "game": {"homeScore": 50, "awayScore": 49, "period": "Q2", "clock": "3:00"},
            "playerBoxScores": [
                {
                    "playerId": 11,
                    "player": {"name": "Trae Young"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=1, minutes=10, fouls=0),
                },
            ],
        },
    )
    oracle_bot._book_c_live_state_cache._game_state_updated_at[1002] -= 60.0

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    assert signals[0].metadata["signal_type"] == "blowout"
    assert oracle_bot.real_client.game_detail_calls == [(1002, "nba")]


def test_scan_book_c_falls_back_to_rest_when_player_cache_entry_is_too_old(tmp_path, monkeypatch):
    home_feed = {
        "latestDay": "2026-03-22",
        "latestDayContent": {
            "games": [
                {
                    "id": 1003,
                    "homeTeam": {"name": "Atlanta Hawks"},
                    "awayTeam": {"name": "Boston Celtics"},
                    "status": "live",
                    "dateTime": "2026-03-22T23:30:00.000Z",
                    "periodName": "Q3",
                    "homeTeamScore": 88,
                    "awayTeamScore": 84,
                },
            ],
        },
    }
    game_details = {
        1003: {
            "game": {"homeScore": 88, "awayScore": 84, "period": "Q3", "clock": "6:00"},
            "playerBoxScores": [
                {
                    "playerId": 42,
                    "player": {"name": "Jalen Johnson"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=15, minutes=26, fouls=4),
                },
            ],
        },
    }
    prop_ticker = "KXNBAPTS-22MAR26-ATLJOHNSONJ-O27"
    _configure_book_c_runtime(
        monkeypatch,
        tmp_path,
        home_feed=home_feed,
        game_details=game_details,
        orderbooks={
            prop_ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.5500", "10.00"]],
                    "no_dollars": [["0.4400", "12.00"]],
                },
            },
        },
    )
    monkeypatch.setattr(
        oracle_bot,
        "real_ws",
        _FakeRealWebSocket(connected=True, stale=False, disabled=False),
    )
    oracle_bot._book_c_live_state_cache.hydrate_from_game_detail(
        1003,
        {"home_team": "Atlanta Hawks", "away_team": "Boston Celtics"},
        {
            "game": {"homeScore": 88, "awayScore": 84, "period": "Q3", "clock": "6:00"},
            "playerBoxScores": [
                {
                    "playerId": 42,
                    "player": {"name": "Jalen Johnson"},
                    "team": {"name": "Atlanta Hawks"},
                    "started": True,
                    "statValues": _stat_values(points=10, minutes=22, fouls=1),
                },
            ],
        },
    )
    oracle_bot._book_c_live_state_cache._player_state_updated_at[1003] -= 60.0

    signals = asyncio.run(oracle_bot.scan_book_c([{"ticker": prop_ticker}]))

    assert len(signals) == 1
    assert signals[0].metadata["signal_type"] == "foul_trouble"
    assert oracle_bot.real_client.game_detail_calls == [(1003, "nba")]


# ── Registry integration tests ──

def test_oracle_in_bot_registry():
    """Oracle bot appears in the bot registry with correct metadata."""
    from bot_registry import BOT_BY_ID, TRADE_FILE_SPECS, DAEMON_BOT_IDS

    oracle = BOT_BY_ID["oracle"]
    assert oracle.display_name == "Oracle NBA"
    assert oracle.process_kind == "daemon"
    assert oracle.trade_filename == "kalshi-oracle-trades.json"
    assert oracle.config_key == "oracle"
    assert oracle.health_key == "oracle"
    assert oracle.default_scan_interval_min == 1

    # Should appear in daemon bots
    assert "oracle" in DAEMON_BOT_IDS

    # Should appear in trade file specs
    trade_bots = [s["bot"] for s in TRADE_FILE_SPECS]
    assert "oracle" in trade_bots


def test_oracle_in_capital_allocator():
    """Oracle has a priority entry in the capital allocator."""
    from capital_allocator import BOT_PRIORITY

    assert "oracle" in BOT_PRIORITY
    assert BOT_PRIORITY["oracle"] == 0.6


def test_oracle_in_health_monitor():
    """Oracle has a source map entry in the health monitor."""
    from ops.health_monitor import BOT_SOURCE_MAP

    assert "oracle" in BOT_SOURCE_MAP
    assert "real-sports" in BOT_SOURCE_MAP["oracle"]


def test_oracle_decision_filename():
    """Oracle decision filename auto-derives from trade filename."""
    from bot_registry import BOT_BY_ID

    oracle = BOT_BY_ID["oracle"]
    assert oracle.resolved_decision_filename == "kalshi-oracle-trades-decisions.json"
