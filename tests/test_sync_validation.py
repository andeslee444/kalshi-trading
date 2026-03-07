"""Tests for scripts/validate-sync-data.py — pre-upload trade log validation."""
import json
import sys
import types
import importlib.util
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

# ---------------------------------------------------------------------------
# Import the validation module from scripts/
# The file uses hyphens so we need importlib to load it.
# ---------------------------------------------------------------------------
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

# We need trade_files importable
SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "kalshi"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_spec = importlib.util.spec_from_file_location(
    "validate_sync_data",
    str(SCRIPTS_DIR / "validate-sync-data.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

validate_trade_file = _mod.validate_trade_file
detect_shrinkage = _mod.detect_shrinkage
load_previous_sizes = _mod.load_previous_sizes
save_current_sizes = _mod.save_current_sizes
validate = _mod.validate


# ---------------------------------------------------------------------------
# validate_trade_file tests
# ---------------------------------------------------------------------------
class TestValidateTradeFile:
    def test_valid_empty_list(self, tmp_path):
        f = tmp_path / "trades.json"
        f.write_text("[]")
        trades, err = validate_trade_file(f)
        assert err is None
        assert trades == []

    def test_valid_list_with_trades(self, tmp_path):
        f = tmp_path / "trades.json"
        f.write_text(json.dumps([{"ticker": "T1", "status": "executed"}]))
        trades, err = validate_trade_file(f)
        assert err is None
        assert len(trades) == 1

    def test_corrupt_json(self, tmp_path):
        f = tmp_path / "trades.json"
        f.write_text("{invalid json")
        trades, err = validate_trade_file(f)
        assert trades is None
        assert "corrupt JSON" in err

    def test_root_not_list(self, tmp_path):
        f = tmp_path / "trades.json"
        f.write_text('{"key": "value"}')
        trades, err = validate_trade_file(f)
        assert trades is None
        assert "expected list" in err

    def test_empty_file(self, tmp_path):
        f = tmp_path / "trades.json"
        f.write_text("")
        trades, err = validate_trade_file(f)
        assert trades is None
        assert "corrupt JSON" in err


# ---------------------------------------------------------------------------
# detect_shrinkage tests
# ---------------------------------------------------------------------------
class TestDetectShrinkage:
    def test_no_previous_size(self):
        assert detect_shrinkage("f.json", 100, {}) is False

    def test_no_shrinkage(self):
        assert detect_shrinkage("f.json", 10000, {"f.json": 9000}) is False

    def test_minor_shrinkage_ok(self):
        # 15% shrinkage (below 20% threshold)
        assert detect_shrinkage("f.json", 8500, {"f.json": 10000}) is False

    def test_major_shrinkage_detected(self):
        # 80% shrinkage
        assert detect_shrinkage("f.json", 2000, {"f.json": 10000}) is True

    def test_exact_threshold_boundary(self):
        # Exactly 20% shrinkage (at threshold) — size = prev * 0.8, not less
        assert detect_shrinkage("f.json", 8000, {"f.json": 10000}) is False

    def test_just_over_threshold(self):
        # Just over 20% shrinkage
        assert detect_shrinkage("f.json", 7999, {"f.json": 10000}) is True

    def test_empty_file_from_nonempty(self):
        assert detect_shrinkage("f.json", 2, {"f.json": 10000}) is True

    def test_custom_threshold(self):
        # 50% threshold: only flag if shrunk by more than 50%
        assert detect_shrinkage("f.json", 6000, {"f.json": 10000}, threshold=0.5) is False
        assert detect_shrinkage("f.json", 4000, {"f.json": 10000}, threshold=0.5) is True


# ---------------------------------------------------------------------------
# Size cache round-trip
# ---------------------------------------------------------------------------
class TestSizeCache:
    def test_save_and_load(self, tmp_path):
        cache = tmp_path / ".sync-sizes.json"
        sizes = {"a.json": 100, "b.json": 200}
        save_current_sizes(sizes, cache)
        loaded = load_previous_sizes(cache)
        assert loaded == sizes

    def test_load_missing_file(self, tmp_path):
        cache = tmp_path / "nonexistent.json"
        assert load_previous_sizes(cache) == {}

    def test_load_corrupt_cache(self, tmp_path):
        cache = tmp_path / ".sync-sizes.json"
        cache.write_text("{bad json")
        assert load_previous_sizes(cache) == {}


# ---------------------------------------------------------------------------
# Full validate() integration tests
# ---------------------------------------------------------------------------
class TestValidateIntegration:
    def _setup_data_dir(self, tmp_path):
        """Create a minimal data dir with valid trade files."""
        from trade_files import TRADE_FILES
        for tf in TRADE_FILES:
            p = tmp_path / tf["filename"]
            p.write_text(json.dumps([
                {"ticker": "TEST", "status": "executed", "source_bot": "test"}
            ]))
        return tmp_path

    def test_all_valid_passes(self, tmp_path):
        self._setup_data_dir(tmp_path)
        sizes_cache = tmp_path / ".sync-sizes.json"
        result = validate(data_dir=tmp_path, sizes_cache=sizes_cache)
        assert result == 0

    def test_corrupt_file_blocks(self, tmp_path):
        self._setup_data_dir(tmp_path)
        # Corrupt one file
        (tmp_path / "kalshi-trades.json").write_text("{bad")
        sizes_cache = tmp_path / ".sync-sizes.json"
        result = validate(data_dir=tmp_path, sizes_cache=sizes_cache)
        assert result == 1

    def test_shrinkage_blocks(self, tmp_path):
        self._setup_data_dir(tmp_path)
        sizes_cache = tmp_path / ".sync-sizes.json"
        # Pre-populate sizes cache with large file
        save_current_sizes({"kalshi-trades.json": 50000}, sizes_cache)
        # File is now tiny (valid JSON but much smaller)
        (tmp_path / "kalshi-trades.json").write_text("[]")
        result = validate(data_dir=tmp_path, sizes_cache=sizes_cache)
        assert result == 1

    def test_missing_source_bot_warns_not_blocks(self, tmp_path):
        self._setup_data_dir(tmp_path)
        # Write trades without source_bot
        (tmp_path / "kalshi-trades.json").write_text(json.dumps([
            {"ticker": "T1", "status": "executed"}  # no source_bot
        ]))
        sizes_cache = tmp_path / ".sync-sizes.json"
        result = validate(data_dir=tmp_path, sizes_cache=sizes_cache)
        # Warnings don't block
        assert result == 0

    def test_missing_trade_file_warns_not_blocks(self, tmp_path):
        # Only create some files, not all
        from trade_files import TRADE_FILES
        first = TRADE_FILES[0]
        (tmp_path / first["filename"]).write_text(json.dumps([
            {"ticker": "T1", "status": "executed", "source_bot": "test"}
        ]))
        sizes_cache = tmp_path / ".sync-sizes.json"
        result = validate(data_dir=tmp_path, sizes_cache=sizes_cache)
        # Missing files warn but don't block
        assert result == 0

    def test_stale_snapshot_warns(self, tmp_path):
        self._setup_data_dir(tmp_path)
        # Create old snapshot
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        snap = {"generated_at": old_time}
        (tmp_path / "financial-snapshot.json").write_text(json.dumps(snap))
        sizes_cache = tmp_path / ".sync-sizes.json"
        result = validate(data_dir=tmp_path, sizes_cache=sizes_cache)
        # Stale snapshot is a warning, not error
        assert result == 0

    def test_root_not_list_blocks(self, tmp_path):
        self._setup_data_dir(tmp_path)
        (tmp_path / "kalshi-trades.json").write_text('{"not": "a list"}')
        sizes_cache = tmp_path / ".sync-sizes.json"
        result = validate(data_dir=tmp_path, sizes_cache=sizes_cache)
        assert result == 1
