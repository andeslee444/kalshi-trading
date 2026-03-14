#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.economics_bot``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.economics_bot")


if __name__ == "__main__":
    main()
