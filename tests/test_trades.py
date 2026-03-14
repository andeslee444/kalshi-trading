"""Tests for trade JSON helpers in storage.py."""

import json
from storage import load_trades, save_trade


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
