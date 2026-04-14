"""Direct tests for the extracted execution.trade_manager module."""

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock

from execution.trade_manager import (
    RecentTradeTracker,
    TradeManager,
    trim_trade_log,
    validate_trade_config,
)
def _make_manager(tmp_path, config=None, **kwargs):
    client = MagicMock()
    client.post.return_value = {
        "order": {"order_id": "test-123", "status": "resting"}
    }
    logger = MagicMock()
    logger.name = "execution-trade-manager-test"
    manager = TradeManager(
        client,
        tmp_path / "trades.json",
        config or {"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": 25},
        logger=logger,
        kill_switch_path=tmp_path / "HALT",
        breaker_state_path=None,
        **kwargs,
    )
    return manager, client


def test_validate_trade_config_accepts_valid_values():
    validate_trade_config({"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": 25})


def test_validate_trade_config_accepts_count_caps():
    validate_trade_config({
        "maxTradeAmount": 5,
        "maxDailyTrades": 10,
        "maxDailyLoss": 25,
        "maxContractsPerTrade": 100,
        "maxGrossPayoutCents": 10000,
    })


def test_recent_trade_tracker_loads_recent_entries(tmp_path):
    trades_path = tmp_path / "trades.json"
    recent_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    trades_path.write_text(json.dumps([{"timestamp": recent_ts, "ticker": "TICK-1"}]))

    tracker = RecentTradeTracker(trades_path, cooldown_hours=24)

    assert tracker.is_recent("TICK-1") is True


def test_trim_trade_log_removes_old_entries(tmp_path):
    trades_path = tmp_path / "trades.json"
    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=120)).isoformat()
    new_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    trades_path.write_text(json.dumps([
        {"timestamp": old_ts, "ticker": "OLD"},
        {"timestamp": new_ts, "ticker": "NEW"},
    ]))

    trim_trade_log(trades_path, max_age_days=90)

    records = json.loads(trades_path.read_text())
    assert [record["ticker"] for record in records] == ["NEW"]


def test_trade_manager_places_order_and_persists_trade(tmp_path):
    manager, client = _make_manager(tmp_path)

    result = manager.place_order("TICK-1", "yes", 50, 2, "test reason")

    assert result["order_id"] == "test-123"
    client.post.assert_called_once()
    records = json.loads((tmp_path / "trades.json").read_text())
    assert records[-1]["ticker"] == "TICK-1"
    assert records[-1]["source_bot"] == "execution-trade-manager-test"
    assert records[-1]["strategy_id"] == "execution-trade-manager-test"
    assert records[-1]["config_version"]


def test_trade_manager_applies_gross_payout_cap(tmp_path):
    manager, client = _make_manager(tmp_path, {
        "maxTradeAmount": 5,
        "maxDailyTrades": 10,
        "maxDailyLoss": 25,
        "maxGrossPayoutCents": 300,
    })

    result = manager.place_order("TICK-1", "yes", 1, 25, "test reason")

    assert result["count"] == 3
    assert "gross_payout_cap" in result["caps_applied"]
    records = json.loads((tmp_path / "trades.json").read_text())
    assert "gross_payout_cap" in records[-1]["caps_applied"]


def test_trade_manager_log_decision_persists_record(tmp_path):
    manager, _ = _make_manager(tmp_path)

    manager.log_decision(
        "TICK-2",
        "no",
        "skipped",
        "edge below threshold",
        edge=0.123456,
        model_prob=0.31,
        model_name="threshold_model",
    )

    decisions_path = tmp_path / "trades-decisions.json"
    records = json.loads(decisions_path.read_text())
    assert records[-1]["ticker"] == "TICK-2"
    assert records[-1]["edge"] == 0.1235
    assert records[-1]["model_prob"] == 0.31
    assert records[-1]["strategy_id"] == "execution-trade-manager-test"
    assert records[-1]["config_version"]
    assert records[-1]["model_version"]


def test_strategy_config_registry_is_written_on_manager_init(tmp_path):
    manager, _ = _make_manager(
        tmp_path,
        {"maxTradeAmount": 5, "maxDailyTrades": 10, "maxDailyLoss": 25},
    )

    registry_path = tmp_path / "strategy-config-registry.json"
    state = json.loads(registry_path.read_text())
    assert manager._config_version
    assert any(
        entry["config_version"] == manager._config_version
        for entry in state["entries"].values()
    )
