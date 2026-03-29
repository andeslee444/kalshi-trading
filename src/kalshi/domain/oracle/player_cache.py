"""JSON-persisted player lookup cache.

Maps (player_name, team) -> Real Sports player_id. Populated on first scan,
persisted across restarts. Avoids O(n) player matching on every cycle.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

_log = logging.getLogger("oracle.player_cache")


class PlayerCache:
    """Maps Real Sports player names to player IDs."""

    def __init__(self, cache_path: Path):
        self._cache: dict[str, int] = {}  # "name|team" -> player_id
        self._path = cache_path
        self._load()

    def _key(self, name: str, team: str) -> str:
        return f"{name.strip().lower()}|{team.strip().lower()}"

    def get(self, name: str, team: str) -> Optional[int]:
        return self._cache.get(self._key(name, team))

    def put(self, name: str, team: str, player_id: int) -> None:
        key = self._key(name, team)
        if self._cache.get(key) != player_id:
            self._cache[key] = player_id
            self._save()

    def size(self) -> int:
        return len(self._cache)

    def all_entries(self) -> dict[str, int]:
        return dict(self._cache)

    def _load(self) -> None:
        if not self._path.exists():
            self._cache = {}
            return
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, dict):
                self._cache = {k: int(v) for k, v in data.items()}
            else:
                _log.warning("Player cache has unexpected format, resetting")
                self._cache = {}
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Failed to load player cache: %s", exc)
            self._cache = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._cache, indent=2))
            tmp.replace(self._path)
        except OSError as exc:
            _log.warning("Failed to save player cache: %s", exc)
