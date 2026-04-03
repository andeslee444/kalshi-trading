#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.oracle_h2_pregame_collector``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.oracle_h2_pregame_collector")


if __name__ == "__main__":
    main()
