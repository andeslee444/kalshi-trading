import json
import types
from pathlib import Path
from unittest.mock import MagicMock

from conftest import load_bot_module, make_fake_auth


def _real_atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _load_strategy_module(tmp_path):
    project_dir = Path(tmp_path)
    (project_dir / "config").mkdir(parents=True, exist_ok=True)
    (project_dir / "data").mkdir(parents=True, exist_ok=True)
    (project_dir / "config" / "bots-config.json").write_text(json.dumps({"strategy": {}}))

    fake_auth = make_fake_auth(PROJECT_DIR=project_dir)
    fake_alloc = types.ModuleType("capital_allocator")
    fake_alloc.PortfolioAllocator = lambda *a, **kw: MagicMock()

    mod = load_bot_module(
        "strategy-trader.py",
        fake_auth,
        extra_stubs={"capital_allocator": fake_alloc},
        initialize=False,
    )
    mod.log = MagicMock()
    mod.client = MagicMock()
    mod.edge_estimator = MagicMock()
    mod._bayesian_edge_enabled = True
    mod.TRADES_JSON_PATH = project_dir / "data" / "kalshi-strategy-trades.json"
    mod.BAYES_PARAMS_PATH = project_dir / "config" / "bayes-params.json"
    mod.BAYES_PROCESSED_SETTLEMENTS_PATH = project_dir / "data" / "strategy-bayes-processed-settlements.json"
    mod._atomic_write_json = _real_atomic_write
    mod.classify_ticker_category = lambda ticker: "sports"
    return mod


def test_check_settled_trades_bootstraps_existing_settled_orders_without_replay(tmp_path):
    mod = _load_strategy_module(tmp_path)
    local_trades = [
        {
            "order_id": "ord-1",
            "ticker": "TICKER-1",
            "timestamp": "2026-03-01T00:00:00Z",
            "strategy": "longshot_sell",
            "yes_price_at_entry": 8,
            "settlement_result": "won",
        },
        {
            "order_id": "ord-2",
            "ticker": "TICKER-2",
            "timestamp": "2026-03-02T00:00:00Z",
            "strategy": "longshot_sell",
            "yes_price_at_entry": 5,
            "settlement_result": "lost",
        },
    ]
    mod._load_strategy_trade_history = lambda: local_trades
    mod.client.get.side_effect = [
        {"market_positions": []},
        {"settlements": [{"ticker": "TICKER-1"}]},
    ]

    mod.check_settled_trades()

    state = json.loads(mod.BAYES_PROCESSED_SETTLEMENTS_PATH.read_text())
    assert set(state["processed_settlement_keys"]) == {"order:ord-1", "order:ord-2"}
    assert state["bootstrapped"] is True
    mod.edge_estimator.update_posterior.assert_not_called()
    mod.edge_estimator.save_params.assert_not_called()


def test_check_settled_trades_processes_new_settlement_once(tmp_path):
    mod = _load_strategy_module(tmp_path)
    local_trades = [
        {
            "order_id": "ord-3",
            "ticker": "TICKER-3",
            "timestamp": "2026-03-03T00:00:00Z",
            "strategy": "longshot_sell",
            "yes_price_at_entry": 8,
            "settlement_result": "won",
        },
    ]
    mod._load_strategy_trade_history = lambda: local_trades
    _real_atomic_write(mod.BAYES_PROCESSED_SETTLEMENTS_PATH, {"processed_settlement_keys": []})
    mod.client.get.side_effect = [
        {"market_positions": []},
        {"settlements": [{"ticker": "TICKER-3"}]},
        {"market_positions": []},
        {"settlements": [{"ticker": "TICKER-3"}]},
    ]

    mod.check_settled_trades()
    mod.check_settled_trades()

    mod.edge_estimator.update_posterior.assert_called_once_with("sports", 8, True)
    mod.edge_estimator.save_params.assert_called_once_with(mod.BAYES_PARAMS_PATH)
    state = json.loads(mod.BAYES_PROCESSED_SETTLEMENTS_PATH.read_text())
    assert state["processed_settlement_keys"] == ["order:ord-3"]
    assert state["bootstrapped"] is False


def test_check_settled_trades_flips_buy_side_outcome_before_bucketing(tmp_path):
    mod = _load_strategy_module(tmp_path)
    local_trades = [
        {
            "order_id": "ord-4",
            "ticker": "TICKER-4",
            "timestamp": "2026-03-04T00:00:00Z",
            "strategy": "longshot_buy",
            "yes_price_at_entry": 92,
            "settlement_result": "won",
        },
    ]
    mod._load_strategy_trade_history = lambda: local_trades
    _real_atomic_write(mod.BAYES_PROCESSED_SETTLEMENTS_PATH, {"processed_settlement_keys": []})
    mod.client.get.side_effect = [
        {"market_positions": []},
        {"settlements": [{"ticker": "TICKER-4"}]},
    ]

    mod.check_settled_trades()

    mod.edge_estimator.update_posterior.assert_called_once_with("sports", 8, False)
