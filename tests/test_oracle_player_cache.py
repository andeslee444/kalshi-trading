"""Tests for Oracle player cache."""

import json
import tempfile
from pathlib import Path

from domain.oracle.player_cache import PlayerCache


def test_put_and_get():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"
        cache = PlayerCache(path)

        cache.put("LeBron James", "LAL", 2544)
        assert cache.get("LeBron James", "LAL") == 2544


def test_get_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"
        cache = PlayerCache(path)
        assert cache.get("Nobody", "XXX") is None


def test_case_insensitive():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"
        cache = PlayerCache(path)

        cache.put("LeBron James", "LAL", 2544)
        assert cache.get("lebron james", "lal") == 2544


def test_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"

        cache1 = PlayerCache(path)
        cache1.put("LeBron James", "LAL", 2544)
        cache1.put("Stephen Curry", "GSW", 201939)

        # Load fresh from disk
        cache2 = PlayerCache(path)
        assert cache2.get("LeBron James", "LAL") == 2544
        assert cache2.get("Stephen Curry", "GSW") == 201939
        assert cache2.size() == 2


def test_size():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"
        cache = PlayerCache(path)
        assert cache.size() == 0

        cache.put("LeBron James", "LAL", 2544)
        assert cache.size() == 1


def test_corrupt_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"
        path.write_text("not json")

        cache = PlayerCache(path)
        assert cache.size() == 0  # graceful recovery


def test_missing_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "nonexistent" / "cache.json"
        cache = PlayerCache(path)
        assert cache.size() == 0

        # Can still write (creates parent dirs)
        cache.put("Test Player", "BOS", 999)
        assert cache.size() == 1


def test_update_existing():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.json"
        cache = PlayerCache(path)

        cache.put("LeBron James", "LAL", 2544)
        cache.put("LeBron James", "LAL", 9999)  # updated ID
        assert cache.get("LeBron James", "LAL") == 9999
        assert cache.size() == 1
