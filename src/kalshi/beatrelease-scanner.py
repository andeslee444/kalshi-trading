#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.beatrelease_scanner``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.beatrelease_scanner")


if __name__ == "__main__":
    main()
