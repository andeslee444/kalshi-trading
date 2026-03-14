"""Helpers for legacy hyphenated wrapper entrypoints."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from pathlib import Path


def _copy_module_exports(wrapper_globals: dict, module: ModuleType):
    """Mirror a preloaded module into the legacy wrapper namespace."""
    for name, value in module.__dict__.items():
        if name in {"__name__", "__file__", "__package__", "__loader__", "__spec__", "__builtins__"}:
            continue
        wrapper_globals[name] = value


def bootstrap_legacy_wrapper(wrapper_globals: dict, app_module_name: str):
    """Execute an app module's source in the wrapper module namespace.

    This preserves the legacy module surface, including mutable globals used by
    older tests and scripts, while the real source lives under ``kalshi.apps``.
    """
    wrapper_file = Path(wrapper_globals["__file__"]).resolve()
    src_root = wrapper_file.parent.parent
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

    existing_module = sys.modules.get(app_module_name)
    if existing_module is not None and getattr(existing_module, "__spec__", None) is None:
        _copy_module_exports(wrapper_globals, existing_module)
        return

    spec = importlib.util.find_spec(app_module_name)
    if spec is None or not spec.origin:
        raise ImportError(f"Cannot locate app module {app_module_name}")

    app_path = Path(spec.origin)
    original_name = wrapper_globals.get("__name__", "")
    original_file = wrapper_globals.get("__file__", str(wrapper_file))
    original_package = wrapper_globals.get("__package__")

    try:
        wrapper_globals["__name__"] = f"{app_module_name}.__legacy__"
        wrapper_globals["__file__"] = str(app_path)
        wrapper_globals["__package__"] = None
        exec(compile(app_path.read_text(encoding="utf-8"), str(app_path), "exec"), wrapper_globals)
    finally:
        wrapper_globals["__name__"] = original_name
        wrapper_globals["__file__"] = original_file
        wrapper_globals["__package__"] = original_package
