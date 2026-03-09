"""Tests for scripts/check-sync-health.py — post-download integrity report."""
import json
import sys
import importlib.util
from pathlib import Path
from datetime import datetime, timezone

import pytest

# ---------------------------------------------------------------------------
# Import the check module from scripts/
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "kalshi"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_spec = importlib.util.spec_from_file_location(
    "check_sync_health",
    str(SCRIPTS_DIR / "check-sync-health.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check = _mod.check


class TestCheckSyncHealth:
    def _setup_data(self, tmp_path, trades_per_file=2, include_snapshot=False):
        """Create minimal data dir with trade files."""
        from trade_files import TRADE_FILES
        for tf in TRADE_FILES:
            p = tmp_path / tf["filename"]
            trades = [
                {
                    "ticker": f"TEST-{i}",
                    "status": "executed",
                    "source_bot": tf["bot"],
                    "timestamp": "2026-03-07T10:00:00Z",
                }
                for i in range(trades_per_file)
            ]
            p.write_text(json.dumps(trades))

        if include_snapshot:
            snap = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "account": {"nav_cents": 10000},
                "realized_pnl": {"net_after_fees_cents": 500},
                "verification": {"orphan_settlements": []},
            }
            (tmp_path / "financial-snapshot.json").write_text(json.dumps(snap))

    def test_basic_report_returns_stats(self, tmp_path, capsys):
        self._setup_data(tmp_path, trades_per_file=3)
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        assert result["total_trades"] == 30  # 10 files * 3 trades
        assert result["missing_source_bot"] == 0

    def test_missing_files_still_works(self, tmp_path, capsys):
        # Empty data dir — all files missing
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        assert result["total_trades"] == 0
        output = capsys.readouterr().out
        assert "MISSING" in output

    def test_corrupt_file_reported(self, tmp_path, capsys):
        self._setup_data(tmp_path)
        (tmp_path / "kalshi-trades.json").write_text("{bad json")
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        output = capsys.readouterr().out
        assert "CORRUPT" in output

    def test_unreconciled_trades_counted(self, tmp_path, capsys):
        from trade_files import TRADE_FILES
        # Write trades with status=executed but no settlement_result
        first = TRADE_FILES[0]
        trades = [
            {"ticker": "T1", "status": "executed", "source_bot": "test",
             "timestamp": "2026-03-07T10:00:00Z"},
            {"ticker": "T2", "status": "executed", "source_bot": "test",
             "timestamp": "2026-03-07T11:00:00Z", "settlement_result": "won"},
        ]
        (tmp_path / first["filename"]).write_text(json.dumps(trades))
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        assert result["total_unreconciled"] == 1
        assert result["total_reconciled"] == 1

    def test_missing_source_bot_counted(self, tmp_path, capsys):
        from trade_files import TRADE_FILES
        first = TRADE_FILES[0]
        trades = [
            {"ticker": "T1", "status": "executed",
             "timestamp": "2026-03-07T10:00:00Z"},  # no source_bot
        ]
        (tmp_path / first["filename"]).write_text(json.dumps(trades))
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        assert result["missing_source_bot"] == 1
        output = capsys.readouterr().out
        assert "WARNING" in output

    def test_snapshot_reported(self, tmp_path, capsys):
        self._setup_data(tmp_path, include_snapshot=True)
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        output = capsys.readouterr().out
        assert "Snapshot:" in output
        assert "NAV:" in output

    def test_missing_snapshot_noted(self, tmp_path, capsys):
        self._setup_data(tmp_path, include_snapshot=False)
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        output = capsys.readouterr().out
        assert "MISSING" in output
        assert "npm run snapshot" in output

    def test_recommendations_generated(self, tmp_path, capsys):
        from trade_files import TRADE_FILES
        # Create many unreconciled trades to trigger recommendation
        first = TRADE_FILES[0]
        trades = [
            {"ticker": f"T{i}", "status": "executed", "source_bot": "test",
             "timestamp": "2026-03-07T10:00:00Z"}
            for i in range(15)
        ]
        (tmp_path / first["filename"]).write_text(json.dumps(trades))
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        assert any("reconcile" in r for r in result["recommendations"])

    def test_empty_trade_file_handled(self, tmp_path, capsys):
        from trade_files import TRADE_FILES
        first = TRADE_FILES[0]
        (tmp_path / first["filename"]).write_text("[]")
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        output = capsys.readouterr().out
        assert "empty" in output

    def test_metrics_files_reported(self, tmp_path, capsys):
        self._setup_data(tmp_path)
        # Create a metrics file
        metrics = [{"timestamp": "2026-03-07T10:00:00Z", "value": 1}]
        (tmp_path / "weather-metrics.json").write_text(json.dumps(metrics))
        result = check(data_dir=tmp_path, project_dir=tmp_path)
        output = capsys.readouterr().out
        assert "Bot metrics files: 1" in output
        assert "weather-metrics.json" in output
