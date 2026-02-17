"""Tests for load_trades() and save_trade() in kalshi_auth.py."""

import json
import types
import sys
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Import the functions under test.
#
# kalshi_auth.py imports ``cryptography``, which is a real dependency.
# The two utility functions we want to test (load_trades, save_trade) are
# pure-Python helpers that don't touch the crypto layer, so we import the
# module directly -- conftest.py already added src/kalshi to sys.path.
#
# If the ``cryptography`` package is not installed in the test environment
# we fall back to a minimal stub so the tests can still run.
# ---------------------------------------------------------------------------

try:
    from kalshi_auth import load_trades, save_trade
except ImportError:
    # Provide a stub cryptography module so kalshi_auth can be imported
    # even when cryptography is missing.
    _crypto_pkg = types.ModuleType("cryptography")
    _haz = types.ModuleType("cryptography.hazmat")
    _prim = types.ModuleType("cryptography.hazmat.primitives")
    _hash = types.ModuleType("cryptography.hazmat.primitives.hashes")
    _ser = types.ModuleType("cryptography.hazmat.primitives.serialization")
    _asym = types.ModuleType("cryptography.hazmat.primitives.asymmetric")
    _pad = types.ModuleType("cryptography.hazmat.primitives.asymmetric.padding")
    _back = types.ModuleType("cryptography.hazmat.backends")
    for name, mod in [
        ("cryptography", _crypto_pkg),
        ("cryptography.hazmat", _haz),
        ("cryptography.hazmat.primitives", _prim),
        ("cryptography.hazmat.primitives.hashes", _hash),
        ("cryptography.hazmat.primitives.serialization", _ser),
        ("cryptography.hazmat.primitives.asymmetric", _asym),
        ("cryptography.hazmat.primitives.asymmetric.padding", _pad),
        ("cryptography.hazmat.backends", _back),
    ]:
        sys.modules.setdefault(name, mod)
    # Now retry the import
    from kalshi_auth import load_trades, save_trade


# ===================================================================
# load_trades tests
# ===================================================================

class TestLoadTrades:

    def test_missing_file_returns_empty_list(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        assert load_trades(missing) == []

    def test_valid_json_file(self, tmp_path):
        trades_file = tmp_path / "trades.json"
        data = [{"ticker": "ABC", "side": "yes", "price": 42}]
        trades_file.write_text(json.dumps(data))
        result = load_trades(trades_file)
        assert result == data

    def test_empty_json_array(self, tmp_path):
        trades_file = tmp_path / "trades.json"
        trades_file.write_text("[]")
        assert load_trades(trades_file) == []

    def test_corrupt_json_returns_empty_list(self, tmp_path):
        trades_file = tmp_path / "trades.json"
        trades_file.write_text("{{{not valid json!!!")
        assert load_trades(trades_file) == []

    def test_empty_file_returns_empty_list(self, tmp_path):
        trades_file = tmp_path / "trades.json"
        trades_file.write_text("")
        assert load_trades(trades_file) == []


# ===================================================================
# save_trade tests
# ===================================================================

class TestSaveTrade:

    def test_appends_to_existing_file(self, tmp_path):
        trades_file = tmp_path / "trades.json"
        existing = [{"ticker": "OLD", "price": 10}]
        trades_file.write_text(json.dumps(existing))

        new_trade = {"ticker": "NEW", "price": 20}
        save_trade(trades_file, new_trade)

        result = json.loads(trades_file.read_text())
        assert len(result) == 2
        assert result[0]["ticker"] == "OLD"
        assert result[1]["ticker"] == "NEW"

    def test_creates_new_file_if_missing(self, tmp_path):
        trades_file = tmp_path / "subdir" / "trades.json"
        assert not trades_file.exists()

        trade = {"ticker": "FIRST", "price": 99}
        save_trade(trades_file, trade)

        assert trades_file.exists()
        result = json.loads(trades_file.read_text())
        assert len(result) == 1
        assert result[0]["ticker"] == "FIRST"

    def test_creates_parent_directories(self, tmp_path):
        trades_file = tmp_path / "a" / "b" / "c" / "trades.json"
        save_trade(trades_file, {"ticker": "DEEP"})
        assert trades_file.exists()

    def test_persists_multiple_appends(self, tmp_path):
        trades_file = tmp_path / "trades.json"
        for i in range(5):
            save_trade(trades_file, {"index": i})
        result = json.loads(trades_file.read_text())
        assert len(result) == 5
        assert [t["index"] for t in result] == [0, 1, 2, 3, 4]

    def test_overwrites_corrupt_file_gracefully(self, tmp_path):
        """If the existing file is corrupt, save_trade should still write
        the new trade (load_trades returns [] for corrupt files)."""
        trades_file = tmp_path / "trades.json"
        trades_file.write_text("NOT JSON")
        save_trade(trades_file, {"ticker": "RECOVER"})
        result = json.loads(trades_file.read_text())
        assert len(result) == 1
        assert result[0]["ticker"] == "RECOVER"
