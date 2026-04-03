"""Tests for daily ops loop explanation helpers."""

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "daily_ops_loop",
    str(_SCRIPTS_DIR / "daily-ops-loop.py"),
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["daily_ops_loop"] = _mod
_spec.loader.exec_module(_mod)


def test_select_trading_outcome_by_bot_prefers_financial_snapshot():
    snapshot = {
        "realized_pnl": {
            "by_bot": {"weather": {"pnl_cents": 100}},
            "by_bot_basis_map": {"weather": "local_buy_orders_joined_to_api_fills_and_settlement_outcomes"},
        }
    }
    attribution = {"by_bot": {"weather": {"pnl_cents": 999}}}

    by_bot, basis_map, source = _mod._select_trading_outcome_by_bot(snapshot, attribution)

    assert by_bot == {"weather": {"pnl_cents": 100}}
    assert basis_map == {"weather": "local_buy_orders_joined_to_api_fills_and_settlement_outcomes"}
    assert source == "financial_snapshot.by_bot"


def test_select_trading_outcome_by_bot_falls_back_to_attribution_when_needed():
    snapshot = {"realized_pnl": {}}
    attribution = {"by_bot": {"weather": {"pnl_cents": 999}}}

    by_bot, basis_map, source = _mod._select_trading_outcome_by_bot(snapshot, attribution)

    assert by_bot == {"weather": {"pnl_cents": 999}}
    assert basis_map == {}
    assert source == "attribution_report.by_bot_fallback"
