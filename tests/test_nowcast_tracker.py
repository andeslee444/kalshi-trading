"""Tests for nowcast history tracker and empirical sigma calibration."""
import json
import math
import random
import pytest
from pathlib import Path


class TestNowcastTracker:

    def test_record_snapshot(self, tmp_path):
        """Recording a snapshot appends to history file."""
        from nowcast_tracker import NowcastTracker
        tracker = NowcastTracker(history_path=tmp_path / "nowcast-history.json")
        tracker.record_snapshot(
            nowcast_value=2.41,
            measure="cpi_yoy",
            target_month="2026-05",
            days_to_release=107,
        )
        history = tracker.load_history()
        assert len(history) == 1
        assert history[0]["nowcast_value"] == 2.41
        assert history[0]["days_to_release"] == 107

    def test_multiple_snapshots_append(self, tmp_path):
        """Multiple snapshots accumulate."""
        from nowcast_tracker import NowcastTracker
        tracker = NowcastTracker(history_path=tmp_path / "nowcast-history.json")
        tracker.record_snapshot(2.41, "cpi_yoy", "2026-05", 107)
        tracker.record_snapshot(2.43, "cpi_yoy", "2026-05", 100)
        history = tracker.load_history()
        assert len(history) == 2

    def test_compute_empirical_sigma(self, tmp_path):
        """With actual values recorded, compute empirical sigma per horizon bucket."""
        from nowcast_tracker import NowcastTracker
        tracker = NowcastTracker(history_path=tmp_path / "nowcast-history.json")
        # Simulate 10 nowcast-vs-actual pairs at ~7 days
        random.seed(42)
        for _ in range(10):
            nowcast = 2.5 + random.gauss(0, 0.10)
            actual = 2.5 + random.gauss(0, 0.02)
            tracker.record_snapshot(nowcast, "cpi_yoy", "2026-01", days_to_release=7)
            tracker.record_actual("cpi_yoy", "2026-01", actual)

        sigmas = tracker.compute_empirical_sigmas()
        # Should have an entry for the 7-day bucket
        assert "7" in sigmas or any(int(k) <= 14 for k in sigmas)

    def test_empty_history_returns_empty_sigmas(self, tmp_path):
        """No history -> no empirical sigmas."""
        from nowcast_tracker import NowcastTracker
        tracker = NowcastTracker(history_path=tmp_path / "nowcast-history.json")
        sigmas = tracker.compute_empirical_sigmas()
        assert sigmas == {}

    def test_record_actual_backfills(self, tmp_path):
        """Recording actual backfills matching snapshots."""
        from nowcast_tracker import NowcastTracker
        tracker = NowcastTracker(history_path=tmp_path / "nowcast-history.json")
        tracker.record_snapshot(2.41, "cpi_yoy", "2026-01", 7)
        tracker.record_snapshot(2.43, "cpi_yoy", "2026-01", 3)
        tracker.record_actual("cpi_yoy", "2026-01", 2.40)
        history = tracker.load_history()
        assert all(e["actual_value"] == 2.40 for e in history)

    def test_insufficient_samples_excluded(self, tmp_path):
        """Fewer than 3 samples in a bucket should not produce a sigma."""
        from nowcast_tracker import NowcastTracker
        tracker = NowcastTracker(history_path=tmp_path / "nowcast-history.json")
        tracker.record_snapshot(2.50, "cpi_yoy", "2026-01", days_to_release=7)
        tracker.record_actual("cpi_yoy", "2026-01", 2.45)
        sigmas = tracker.compute_empirical_sigmas()
        assert sigmas == {}  # Only 1 sample, need >= 3
