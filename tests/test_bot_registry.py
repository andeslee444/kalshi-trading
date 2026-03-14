from pathlib import Path

from bot_registry import (
    ALWAYS_DISABLED_BOT_IDS,
    BOT_DEFINITIONS,
    BOT_DISPLAY_NAMES,
    DASHBOARD_BOT_IDS,
    DECISION_FILE_SPECS,
    SUPERVISOR_BOT_DEFINITIONS,
    TRADE_FILE_SPECS,
)
from trade_files import TRADE_FILES


def test_bot_ids_are_unique():
    bot_ids = [bot.bot_id for bot in BOT_DEFINITIONS]
    assert len(bot_ids) == len(set(bot_ids))


def test_trade_files_follow_registry():
    assert TRADE_FILES == [dict(spec) for spec in TRADE_FILE_SPECS]


def test_decision_files_derive_from_trade_stems():
    expected = {
        spec["bot"]: f"{Path(spec['filename']).stem}-decisions.json"
        for spec in TRADE_FILE_SPECS
    }
    actual = {spec["bot"]: spec["filename"] for spec in DECISION_FILE_SPECS}
    assert actual == expected


def test_display_aliases_cover_runtime_names():
    assert BOT_DISPLAY_NAMES["monitor"] == "Data Monitor"
    assert BOT_DISPLAY_NAMES["source-monitor"] == "Data Monitor"
    assert BOT_DISPLAY_NAMES["positions"] == "Position Mgmt"
    assert BOT_DISPLAY_NAMES["position-monitor"] == "Position Mgmt"
    assert BOT_DISPLAY_NAMES["arb"] == "Cross-Platform"
    assert BOT_DISPLAY_NAMES["cross-platform-arb"] == "Cross-Platform"


def test_dashboard_and_supervisor_visibility_are_explicit():
    assert "weather" in DASHBOARD_BOT_IDS
    assert "hdd" in DASHBOARD_BOT_IDS
    assert "demo" not in DASHBOARD_BOT_IDS
    assert "weather" in {bot.bot_id for bot in SUPERVISOR_BOT_DEFINITIONS}
    assert "demo" not in {bot.bot_id for bot in SUPERVISOR_BOT_DEFINITIONS}
    assert {"hdd", "mm", "demo"} <= ALWAYS_DISABLED_BOT_IDS


def test_supervisor_commands_target_real_scripts():
    project_dir = Path(__file__).resolve().parent.parent
    for bot in SUPERVISOR_BOT_DEFINITIONS:
        if len(bot.command) < 2:
            continue
        script = bot.command[1]
        if script.endswith(".py"):
            assert (project_dir / script).exists(), bot.bot_id
