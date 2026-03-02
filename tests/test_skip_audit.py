"""Tests for the skip-audit tool (scripts/skip-audit.py).

Validates decision log loading, skip distribution counting,
money-left-on-table analysis, and summary report generation.

Uses importlib to load the script since it has a hyphen in its filename.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load the skip-audit module via importlib (hyphenated filename)
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_SKIP_AUDIT_PATH = _SCRIPTS_DIR / "skip-audit.py"


def _load_skip_audit():
    """Import scripts/skip-audit.py as a module."""
    spec = importlib.util.spec_from_file_location("skip_audit", str(_SKIP_AUDIT_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


skip_audit = _load_skip_audit()

# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURE_FILE = FIXTURE_DIR / "skip-audit-decisions.json"


# ---------------------------------------------------------------------------
# TestLoadDecisions
# ---------------------------------------------------------------------------
class TestLoadDecisions:
    """Tests for load_decisions()."""

    def test_loads_single_file(self):
        """Loading the fixture file returns 10 records."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        assert len(decisions) == 10

    def test_filters_by_bot(self):
        """Can filter decisions by source_bot after loading."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        crypto_only = [d for d in decisions if d.get("source_bot") == "crypto"]
        assert len(crypto_only) == 5  # 3 skipped + 1 placed + 1 no_price

    def test_handles_missing_file(self):
        """Missing file paths are silently skipped."""
        decisions = skip_audit.load_decisions(["/nonexistent/path/fake.json"])
        assert decisions == []

    def test_handles_corrupt_file(self):
        """Corrupt JSON files are silently skipped."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json!!")
            tmp_path = f.name
        try:
            decisions = skip_audit.load_decisions([tmp_path])
            assert decisions == []
        finally:
            os.unlink(tmp_path)

    def test_merges_multiple_files(self):
        """Loading the same fixture twice doubles the count."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE), str(FIXTURE_FILE)])
        assert len(decisions) == 20


# ---------------------------------------------------------------------------
# TestSkipDistribution
# ---------------------------------------------------------------------------
class TestSkipDistribution:
    """Tests for skip_distribution()."""

    def test_per_bot_distribution(self):
        """Distribution returns counts grouped by bot and reason."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        dist = skip_audit.skip_distribution(decisions)
        # crypto has 3 skipped entries (edge below threshold, illiquid, edge below mid-range)
        assert dist["crypto"]["edge below threshold"] == 1
        assert dist["crypto"]["illiquid"] == 1

    def test_excludes_placed_trades(self):
        """Placed trades should not appear in the distribution."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        dist = skip_audit.skip_distribution(decisions)
        # The "placed" action (crypto, "edge above threshold") should not appear
        for bot_reasons in dist.values():
            assert "edge above threshold" not in bot_reasons

    def test_all_bots_present(self):
        """All bots with skips should appear in the distribution."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        dist = skip_audit.skip_distribution(decisions)
        assert "crypto" in dist
        assert "weather" in dist
        assert "entertainment" in dist
        assert "cross-platform-arb" in dist

    def test_total_skip_count(self):
        """Total skips across all bots should equal 9 (10 decisions minus 1 placed)."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        dist = skip_audit.skip_distribution(decisions)
        total = sum(count for bot in dist.values() for count in bot.values())
        assert total == 9


# ---------------------------------------------------------------------------
# TestMoneyLeftOnTable
# ---------------------------------------------------------------------------
class TestMoneyLeftOnTable:
    """Tests for money_left_on_table()."""

    def test_skips_with_edge_have_estimated_pnl(self):
        """Every returned item should have an estimated_pnl field."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        results = skip_audit.money_left_on_table(decisions)
        for item in results:
            assert "estimated_pnl" in item

    def test_no_price_excluded(self):
        """Decisions without price_cents should be excluded."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        results = skip_audit.money_left_on_table(decisions)
        tickers = [r["ticker"] for r in results]
        # no_price entry (KXBTCD-26MAR0112-T95000) should not appear
        assert "KXBTCD-26MAR0112-T95000" not in tickers
        # illiquid_no_ask entry (price_cents=null) should not appear
        assert "ALBUM-26MAR07-SALES2" not in tickers

    def test_sorted_by_edge_descending(self):
        """Results should be sorted by edge in descending order."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        results = skip_audit.money_left_on_table(decisions)
        edges = [r["edge"] for r in results]
        assert edges == sorted(edges, reverse=True)

    def test_excludes_placed_trades(self):
        """Placed trades should not appear in money_left_on_table."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        results = skip_audit.money_left_on_table(decisions)
        tickers = [r["ticker"] for r in results]
        assert "KXBTCD-26MAR0112-T80000" not in tickers

    def test_estimated_pnl_positive(self):
        """All entries have positive edge, so estimated_pnl should be positive."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        results = skip_audit.money_left_on_table(decisions)
        for item in results:
            assert item["estimated_pnl"] > 0


# ---------------------------------------------------------------------------
# TestSummaryReport
# ---------------------------------------------------------------------------
class TestSummaryReport:
    """Tests for summary_report()."""

    def test_produces_non_empty_string(self):
        """Summary report should return a non-empty string."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        report = skip_audit.summary_report(decisions)
        assert isinstance(report, str)
        assert len(report) > 0

    def test_contains_bot_names(self):
        """Report should mention all bots that have skipped decisions."""
        decisions = skip_audit.load_decisions([str(FIXTURE_FILE)])
        report = skip_audit.summary_report(decisions)
        assert "crypto" in report
        assert "weather" in report
        assert "entertainment" in report
        assert "cross-platform-arb" in report

    def test_empty_decisions_returns_string(self):
        """Even with no decisions, should return a valid string."""
        report = skip_audit.summary_report([])
        assert isinstance(report, str)
