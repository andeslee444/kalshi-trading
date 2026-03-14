#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.weather_bot``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.weather_bot")


if __name__ == "__main__":
    main()
