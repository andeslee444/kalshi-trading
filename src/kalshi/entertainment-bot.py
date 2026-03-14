#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.entertainment_bot``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.entertainment_bot")


if __name__ == "__main__":
    main()
