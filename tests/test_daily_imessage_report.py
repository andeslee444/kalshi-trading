"""Tests for the daily iMessage report formatting."""

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "daily_imessage_report",
    str(_SCRIPTS_DIR / "daily-imessage-report.py"),
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["daily_imessage_report"] = _mod
_spec.loader.exec_module(_mod)


def test_build_report_includes_basis_and_unattributed_weather_label():
    snap = {
        "account": {"nav_cents": 100000, "balance_cents": 70000, "portfolio_value_cents": 30000},
        "balance_check": {
            "true_total_pnl_cents": 5000,
            "realized_net_cents": 3000,
            "implied_unrealized_cents": 2000,
        },
        "deposits": {"net_funded_cents": 95000, "roi_pct": 5.26},
        "realized_pnl": {
            "by_day": {"2026-04-02": 300},
            "by_bot": {
                "weather": {"pnl_cents": 16876, "wins": 99, "losses": 69, "win_rate": 0.589, "fees_cents": 146},
                "unattributed-weather": {"pnl_cents": 9000, "wins": 2, "losses": 1, "win_rate": 0.667, "fees_cents": 0},
            },
            "by_bot_basis_map": {
                "weather": "local_buy_orders_joined_to_api_fills_and_settlement_outcomes",
                "unattributed-weather": "kalshi_api_settlements",
            },
        },
        "unrealized_pnl": {"positions": []},
        "verification": {"status": "ok"},
    }

    report = _mod.build_report(snap)

    assert "weather: +$168.76" in report
    assert "basis=local" in report
    assert "unattributed-weather history: +$90.00" in report
    assert "basis=api" in report
    assert "excludes canonical weather-family bots" in report
