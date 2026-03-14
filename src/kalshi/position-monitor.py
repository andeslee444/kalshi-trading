#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.position_monitor``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.position_monitor")


if __name__ == "__main__":
    main()
