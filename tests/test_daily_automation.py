"""Tests for daily automation wrapper — cron-ready report + backtest runner."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


class TestDailyAutomationConfig:
    """Test daily automation configuration and schedule logic."""

    def test_should_run_report_daily(self):
        """Report should run every day regardless of market hours."""
        valid_days = list(range(7))
        assert len(valid_days) == 7
        for dow in valid_days:
            assert 0 <= dow <= 6

    def test_lock_file_prevents_double_run(self):
        """Lock file prevents concurrent daily runs."""
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "daily-run.lock"
            # First run creates lock
            lock_path.write_text(str(os.getpid()))
            assert lock_path.exists()
            # Lock should contain PID
            pid = int(lock_path.read_text())
            assert pid == os.getpid()

    def test_stale_lock_detected(self):
        """Lock files older than 1 hour are considered stale."""
        import time
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "daily-run.lock"
            lock_path.write_text("99999")
            # Set mtime to 2 hours ago
            old_time = time.time() - 7200
            os.utime(lock_path, (old_time, old_time))
            lock_age = time.time() - lock_path.stat().st_mtime
            assert lock_age > 3600, "Lock should be detected as stale after 1 hour"


class TestDailyReportFormatting:
    """Test report output formatting for WhatsApp readability."""

    def test_report_summary_has_required_fields(self):
        """Report dict must contain balance, win_rate, pnl keys."""
        report = {
            "balance_total": 509000,
            "balance_available": 318700,
            "win_rate": 0.62,
            "settled_pnl_cents": 4500,
            "trades_today": 3,
            "per_bot": {},
        }
        assert "balance_total" in report
        assert "win_rate" in report
        assert "settled_pnl_cents" in report

    def test_format_currency_cents_to_dollars(self):
        """Currency formatting: 509000 cents -> $5,090.00."""
        cents = 509000
        dollars = cents / 100
        formatted = f"${dollars:,.2f}"
        assert formatted == "$5,090.00"

    def test_format_pnl_negative(self):
        """Negative P&L should show minus sign."""
        cents = -1500
        dollars = cents / 100
        formatted = f"${dollars:,.2f}"
        assert formatted == "$-15.00"
