"""Canonical bot registry and artifact metadata.

This module centralizes control-plane bot ids, runtime/log aliases, and
artifact filenames so dashboards, supervisors, and analytics don't drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BotDefinition:
    """Stable metadata for a bot or closely related process."""

    bot_id: str
    display_name: str
    command: tuple[str, ...]
    process_kind: str
    config_key: str | None = None
    health_key: str | None = None
    log_name: str | None = None
    default_scan_interval_min: int | None = None
    trade_label: str | None = None
    trade_filename: str | None = None
    decision_filename: str | None = None
    source_bot: str | None = None
    always_disabled: bool = False
    dashboard_visible: bool = True
    supervisor_managed: bool = True

    @property
    def resolved_config_key(self) -> str:
        return self.config_key or self.bot_id

    @property
    def resolved_health_key(self) -> str | None:
        return self.health_key

    @property
    def resolved_log_name(self) -> str:
        return self.log_name or self.bot_id

    @property
    def resolved_source_bot(self) -> str:
        return self.source_bot or self.resolved_log_name

    @property
    def resolved_trade_label(self) -> str:
        return self.trade_label or self.display_name

    @property
    def resolved_decision_filename(self) -> str | None:
        if self.decision_filename is not None:
            return self.decision_filename
        if self.trade_filename:
            return f"{Path(self.trade_filename).stem}-decisions.json"
        return None


BOT_DEFINITIONS: tuple[BotDefinition, ...] = (
    BotDefinition(
        bot_id="weather",
        display_name="Weather",
        command=("python3", "src/kalshi/weather-bot.py"),
        process_kind="daemon",
        config_key="weather",
        health_key="weather",
        log_name="weather",
        default_scan_interval_min=30,
        trade_label="Weather Bot",
        trade_filename="kalshi-trades.json",
    ),
    BotDefinition(
        bot_id="entertainment",
        display_name="Entertainment",
        command=("python3", "src/kalshi/entertainment-bot.py"),
        process_kind="daemon",
        health_key="entertainment",
        log_name="entertainment",
        default_scan_interval_min=15,
        trade_label="Entertainment Bot",
        trade_filename="kalshi-entertainment-trades.json",
    ),
    BotDefinition(
        bot_id="crypto",
        display_name="Crypto",
        command=("python3", "src/kalshi/crypto-bot.py"),
        process_kind="daemon",
        health_key="crypto",
        log_name="crypto",
        default_scan_interval_min=5,
        trade_label="Crypto Bot",
        trade_filename="kalshi-crypto-trades.json",
    ),
    BotDefinition(
        bot_id="economics",
        display_name="Economics",
        command=("python3", "src/kalshi/economics-bot.py"),
        process_kind="daemon",
        health_key="economics",
        log_name="economics",
        default_scan_interval_min=360,
        trade_label="Economics Bot",
        trade_filename="kalshi-economics-trades.json",
    ),
    BotDefinition(
        bot_id="positions",
        display_name="Position Mgmt",
        command=("python3", "src/kalshi/position-monitor.py"),
        process_kind="daemon",
        config_key="position_monitor",
        health_key="position-monitor",
        log_name="position-monitor",
        source_bot="position-monitor",
        default_scan_interval_min=15,
        trade_label="Position Monitor",
        trade_filename="kalshi-position-trades.json",
    ),
    BotDefinition(
        bot_id="monitor",
        display_name="Data Monitor",
        command=("python3", "src/kalshi/source-monitor.py"),
        process_kind="daemon",
        health_key="source-monitor",
        log_name="source-monitor",
        source_bot="source-monitor",
        default_scan_interval_min=10,
        trade_label="Source Monitor",
        trade_filename="kalshi-monitor-trades.json",
    ),
    BotDefinition(
        bot_id="strategy",
        display_name="Opportunistic",
        command=("python3", "src/kalshi/strategy-trader.py"),
        process_kind="daemon",
        health_key="strategy",
        log_name="strategy",
        default_scan_interval_min=15,
        trade_label="Strategy Trader",
        trade_filename="kalshi-strategy-trades.json",
    ),
    BotDefinition(
        bot_id="oracle",
        display_name="Oracle NBA",
        command=("python3", "src/kalshi/oracle-bot.py"),
        process_kind="daemon",
        config_key="oracle",
        health_key="oracle",
        log_name="oracle",
        default_scan_interval_min=1,
        trade_label="Oracle NBA",
        trade_filename="kalshi-oracle-trades.json",
    ),
    BotDefinition(
        bot_id="hdd",
        display_name="Data Scraper",
        command=("python3", "src/kalshi/hdd-scraper.py"),
        process_kind="oneshot",
        log_name="hdd-scraper",
        always_disabled=True,
    ),
    BotDefinition(
        bot_id="hdd-monitor",
        display_name="HDD Monitor",
        command=("python3", "src/kalshi/hdd-scraper.py", "monitor"),
        process_kind="daemon",
        config_key="hdd_monitor",
        health_key="hdd-monitor",
        log_name="hdd-scraper",
        default_scan_interval_min=15,
    ),
    BotDefinition(
        bot_id="arb",
        display_name="Cross-Platform",
        command=("python3", "src/kalshi/cross-platform-arb.py"),
        process_kind="daemon",
        config_key="cross_platform_arb",
        health_key="cross-platform-arb",
        log_name="cross-platform-arb",
        source_bot="cross-platform-arb",
        default_scan_interval_min=10,
        trade_label="Cross-Platform Arb",
        trade_filename="kalshi-arb-trades.json",
    ),
    BotDefinition(
        bot_id="mm",
        display_name="Market Making",
        command=("python3", "src/kalshi/market-maker.py"),
        process_kind="daemon",
        config_key="market_maker",
        health_key="market-maker",
        log_name="market-maker",
        source_bot="market-maker",
        default_scan_interval_min=5,
        trade_label="Market Maker",
        trade_filename="kalshi-mm-trades.json",
        always_disabled=True,
    ),
    BotDefinition(
        bot_id="beatrelease",
        display_name="Beat Release",
        command=("python3", "src/kalshi/beatrelease-scanner.py"),
        process_kind="daemon",
        health_key="beatrelease",
        log_name="beatrelease",
        default_scan_interval_min=60,
        trade_label="BeatRelease Scanner",
        trade_filename="beatrelease-trades.json",
    ),
    BotDefinition(
        bot_id="demo",
        display_name="Demo",
        command=("python3", "src/kalshi/demo-trader.py"),
        process_kind="oneshot",
        log_name="demo",
        always_disabled=True,
        dashboard_visible=False,
        supervisor_managed=False,
    ),
)


BOT_BY_ID = {bot.bot_id: bot for bot in BOT_DEFINITIONS}
DASHBOARD_BOT_IDS = tuple(bot.bot_id for bot in BOT_DEFINITIONS if bot.dashboard_visible)
SUPERVISOR_BOT_DEFINITIONS = tuple(bot for bot in BOT_DEFINITIONS if bot.supervisor_managed)
TRADE_BOT_DEFINITIONS = tuple(bot for bot in BOT_DEFINITIONS if bot.trade_filename)
DECISION_BOT_DEFINITIONS = tuple(
    bot for bot in BOT_DEFINITIONS if bot.resolved_decision_filename is not None
)

DAEMON_BOT_IDS = frozenset(
    bot.bot_id for bot in SUPERVISOR_BOT_DEFINITIONS if bot.process_kind == "daemon"
)
ONESHOT_BOT_IDS = frozenset(
    bot.bot_id for bot in SUPERVISOR_BOT_DEFINITIONS if bot.process_kind == "oneshot"
)
ALWAYS_DISABLED_BOT_IDS = frozenset(
    bot.bot_id for bot in BOT_DEFINITIONS if bot.always_disabled
)

BOT_COMMANDS = {
    bot.bot_id: list(bot.command) for bot in SUPERVISOR_BOT_DEFINITIONS
}
BOT_CONFIG_KEY_MAP = {
    bot.bot_id: bot.resolved_config_key for bot in BOT_DEFINITIONS
}
BOT_HEALTH_KEY_MAP = {
    bot.bot_id: bot.resolved_health_key
    for bot in BOT_DEFINITIONS
    if bot.resolved_health_key is not None
}
BOT_LOG_NAME_MAP = {
    bot.bot_id: bot.resolved_log_name
    for bot in BOT_DEFINITIONS
    if bot.resolved_log_name != bot.bot_id
}
BOT_DEFAULT_SCAN_INTERVAL_MAP = {
    bot.bot_id: bot.default_scan_interval_min
    for bot in BOT_DEFINITIONS
    if bot.default_scan_interval_min is not None
}
BOT_DISPLAY_NAMES: dict[str, str] = {}
for bot in BOT_DEFINITIONS:
    aliases = {
        bot.bot_id,
        bot.resolved_log_name,
        bot.resolved_source_bot,
        bot.resolved_health_key,
    }
    for alias in aliases:
        if alias:
            BOT_DISPLAY_NAMES[alias] = bot.display_name

TRADE_FILE_SPECS = tuple(
    {
        "label": bot.resolved_trade_label,
        "bot": bot.bot_id,
        "filename": bot.trade_filename,
    }
    for bot in TRADE_BOT_DEFINITIONS
)
DECISION_FILE_SPECS = tuple(
    {
        "bot": bot.bot_id,
        "filename": bot.resolved_decision_filename,
    }
    for bot in DECISION_BOT_DEFINITIONS
    if bot.resolved_decision_filename is not None
)
