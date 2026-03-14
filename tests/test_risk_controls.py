"""Direct tests for the extracted risk helpers."""

import time

from risk.circuit_breaker import CircuitBreaker, SHARED_BREAKER_PATH
from risk.kill_switch import (
    KILL_SWITCH_PATH,
    PER_BOT_HALT_PREFIX,
    check_kill_switch,
    per_bot_halt_path,
)


def test_check_kill_switch_returns_false_when_absent(tmp_path):
    assert check_kill_switch(tmp_path / "HALT_TRADING") is False


def test_check_kill_switch_returns_true_when_present(tmp_path):
    halt_path = tmp_path / "HALT_TRADING"
    halt_path.touch()

    assert check_kill_switch(halt_path) is True


def test_per_bot_halt_path_uses_prefix_and_project_dir(tmp_path):
    halt_path = per_bot_halt_path("weather", project_dir=tmp_path)

    assert halt_path == tmp_path / "data" / "HALT_bot_weather"
    assert PER_BOT_HALT_PREFIX in halt_path.name


def test_kill_switch_constant_name_is_stable():
    assert KILL_SWITCH_PATH.name == "HALT_TRADING"


def test_shared_breaker_constant_name_is_stable():
    assert SHARED_BREAKER_PATH.name == "circuit-breaker-state.json"


def test_circuit_breaker_opens_after_threshold_and_notifies():
    notifications = []
    breaker = CircuitBreaker(
        max_failures=3,
        notifier=lambda message, level=None: notifications.append((message, level)),
    )

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.is_open() is True
    assert notifications == [("Circuit breaker OPEN after 3 consecutive failures", "critical")]


def test_circuit_breaker_shared_state_persists_across_instances(tmp_path):
    state_path = tmp_path / "breaker.json"
    cb1 = CircuitBreaker(max_failures=2, state_path=state_path)
    cb1.record_failure()
    cb1.record_failure()

    cb2 = CircuitBreaker(max_failures=2, state_path=state_path)

    assert cb2.is_open() is True


def test_circuit_breaker_auto_resets_after_timeout(tmp_path):
    breaker = CircuitBreaker(max_failures=2, reset_seconds=0.1, state_path=tmp_path / "breaker.json")
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.is_open() is True
    time.sleep(0.15)
    assert breaker.is_open() is False
