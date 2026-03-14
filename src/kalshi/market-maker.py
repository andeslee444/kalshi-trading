#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.market_maker``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.market_maker")


if __name__ == "__main__":
    main()
