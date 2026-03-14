#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.cross_platform_arb``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.cross_platform_arb")


if __name__ == "__main__":
    main()
