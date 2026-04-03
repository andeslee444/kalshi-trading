"""Tests for the Oracle H2 pregame collector."""

from __future__ import annotations

import datetime as dt

import apps.oracle_h2_pregame_collector as collector


def test_is_pregame_game_rejects_live_and_final_statuses():
    now = dt.datetime(2026, 4, 2, 18, 0, tzinfo=dt.timezone.utc)

    assert collector._is_pregame_game({"status": "live"}, now=now) is False
    assert collector._is_pregame_game({"status": "final"}, now=now) is False


def test_is_pregame_game_requires_future_tip_when_start_time_exists():
    now = dt.datetime(2026, 4, 2, 18, 0, tzinfo=dt.timezone.utc)

    assert collector._is_pregame_game(
        {"status": "scheduled", "start_time": "2026-04-02T19:00:00+00:00"},
        now=now,
    ) is True
    assert collector._is_pregame_game(
        {"status": "scheduled", "start_time": "2026-04-02T17:30:00+00:00"},
        now=now,
    ) is False
