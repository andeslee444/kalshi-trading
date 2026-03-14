#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.hdd_scraper``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.hdd_scraper")


if __name__ == "__main__":
    main()
