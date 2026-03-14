"""Kalshi trading bot package.

Phase 1 keeps the legacy flat-module imports working while executable
entrypoints move under ``kalshi.apps``.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys
from pathlib import Path


def _compat_alias_targets() -> dict[str, str]:
    """Map legacy flat module names to the packaged ``kalshi.*`` modules."""
    pkg_dir = Path(__file__).resolve().parent
    aliases = {}
    for path in pkg_dir.glob("*.py"):
        stem = path.stem
        if stem == "__init__" or not stem.isidentifier():
            continue
        aliases[stem] = f"{__name__}.{stem}"
    for path in pkg_dir.iterdir():
        if path.is_dir() and (path / "__init__.py").exists():
            stem = path.name
            if stem.isidentifier() and stem != "apps":
                aliases[stem] = f"{__name__}.{stem}"
    return aliases


_COMPAT_ALIAS_TARGETS = _compat_alias_targets()


class _CompatAliasLoader(importlib.abc.Loader):
    def __init__(self, alias: str, target: str):
        self.alias = alias
        self.target = target

    def create_module(self, spec):
        module = importlib.import_module(self.target)
        sys.modules[self.alias] = module
        return module

    def exec_module(self, module):
        return None


class _CompatAliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if "." in fullname:
            return None
        target_name = _COMPAT_ALIAS_TARGETS.get(fullname)
        if not target_name:
            return None
        loader = _CompatAliasLoader(fullname, target_name)
        return importlib.util.spec_from_loader(fullname, loader, origin=f"kalshi-alias:{target_name}")


if not any(isinstance(finder, _CompatAliasFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _CompatAliasFinder())
