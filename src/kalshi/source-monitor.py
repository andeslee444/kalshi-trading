#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.source_monitor``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.source_monitor")


if __name__ == "__main__":
    main()
