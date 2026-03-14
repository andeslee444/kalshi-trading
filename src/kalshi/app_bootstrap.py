"""Shared bootstrap helpers for import-safe app entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppContext:
    """Mutable runtime state installed into a bot module at startup."""

    state: dict[str, Any] = field(default_factory=dict)


def install_app_context(module_globals: dict[str, Any], context: AppContext) -> AppContext:
    """Install a bootstrapped runtime state into a module namespace."""
    module_globals.update(context.state)
    module_globals["_APP_CONTEXT"] = context
    return context
