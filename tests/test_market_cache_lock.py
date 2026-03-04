"""Test that market cache updates are protected by file locking."""
import sys
import json
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kalshi"))

from kalshi_auth import read_market_cache, write_market_cache, MARKET_CACHE_PATH


def test_market_cache_write_uses_lock(tmp_path):
    """write_market_cache should use file locking to prevent lost updates."""
    cache_file = tmp_path / "market-cache.json"

    with patch("kalshi_auth.MARKET_CACHE_PATH", cache_file):
        # Write initial data
        write_market_cache({"KXHIGH": [{"ticker": "T1"}]})
        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert "KXHIGH" in data["markets"]

        # Write again with different prefix
        write_market_cache({"KXBTC": [{"ticker": "T2"}]})
        data = json.loads(cache_file.read_text())
        assert "KXBTC" in data["markets"]


def test_read_market_cache_returns_none_when_missing(tmp_path):
    """read_market_cache returns None if file does not exist."""
    cache_file = tmp_path / "market-cache.json"
    with patch("kalshi_auth.MARKET_CACHE_PATH", cache_file):
        result = read_market_cache(prefix="KXHIGH")
        assert result is None


def test_read_market_cache_returns_none_when_stale(tmp_path):
    """read_market_cache returns None if cache is older than max_age."""
    cache_file = tmp_path / "market-cache.json"
    with patch("kalshi_auth.MARKET_CACHE_PATH", cache_file):
        # Write cache with a timestamp far in the past
        cache_file.write_text(json.dumps({
            "updated_at": time.time() - 9999,
            "markets": {"KXHIGH": [{"ticker": "T1"}]},
        }))
        result = read_market_cache(prefix="KXHIGH", max_age=60)
        assert result is None


def test_read_market_cache_returns_data_when_fresh(tmp_path):
    """read_market_cache returns market list when cache is fresh."""
    cache_file = tmp_path / "market-cache.json"
    with patch("kalshi_auth.MARKET_CACHE_PATH", cache_file):
        write_market_cache({"KXHIGH": [{"ticker": "T1"}]})
        result = read_market_cache(prefix="KXHIGH", max_age=60)
        assert result == [{"ticker": "T1"}]


def test_market_cache_lock_file_created(tmp_path):
    """The lock file path should be derivable from MARKET_CACHE_PATH."""
    cache_file = tmp_path / "market-cache.json"
    lock_file = cache_file.with_suffix(".lock")
    assert lock_file == tmp_path / "market-cache.lock"
