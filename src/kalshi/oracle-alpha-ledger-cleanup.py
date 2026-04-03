#!/usr/bin/env python3
"""Backward-compatible wrapper for ``kalshi.apps.oracle_alpha_ledger_cleanup``."""

from legacy_wrapper import bootstrap_legacy_wrapper

bootstrap_legacy_wrapper(globals(), "kalshi.apps.oracle_alpha_ledger_cleanup")


if __name__ == "__main__":
    main()
