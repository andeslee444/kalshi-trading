#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.demo_trader``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.demo_trader")


if __name__ == "__main__":
    main()
