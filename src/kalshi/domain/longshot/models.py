"""Longshot bias helpers extracted from probability."""

from __future__ import annotations

import math


# Category-specific Becker (2025) parameters: (amplitude, decay_rate)
# Amplitude: fraction of overpricing at 1 cent
# Decay rate: exponential decay per cent of price
# Becker studied aggregate bias; these category adjustments reflect
# that retail-heavy categories (sports, entertainment) show stronger bias.
LONGSHOT_BIAS_PARAMS = {
    "sports": (0.65, 0.12),       # strongest bias, slowest decay
    "entertainment": (0.60, 0.14),
    "politics": (0.45, 0.18),
    "weather": (0.30, 0.20),      # weakest bias, fastest decay
    "economics": (0.35, 0.18),
    "crypto": (0.50, 0.15),
    "default": (0.57, 0.15),      # original Becker aggregate
}


def classify_ticker_category(ticker):
    """Classify a Kalshi ticker into a longshot bias category."""
    t = ticker.upper()
    if any(x in t for x in ["KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXMARMAD",
                             "KXSOCCER", "KXUFC", "KXNCAA", "KXSPORT"]):
        return "sports"
    if any(x in t for x in ["KXOSCARS", "KXGRAMMYS", "KXBILLBOARD", "KXALBUM",
                             "KXMOVIE", "KXBOX", "KXFILM", "KXMUSIC", "KXENTERTAIN",
                             "KXSTREAM", "KXSPOTIFY"]):
        return "entertainment"
    if any(x in t for x in ["KXPRES", "KXSEN", "KXGOV", "KXELECT", "KXHOUSE",
                             "KXCONGRESS", "KXSCOTUS"]):
        return "politics"
    if any(x in t for x in ["KXHIGH", "KXLOW", "KXTEMP", "KXRAIN", "KXSNOW",
                             "KXWEATHER"]):
        return "weather"
    if any(x in t for x in ["KXCPI", "KXGDP", "KXFED", "KXJOBS", "KXRATE",
                             "KXECON", "KXINFLATION"]):
        return "economics"
    if any(x in t for x in ["KXBTC", "KXETH", "KXCRYPTO", "KXSOL", "KXDOGE", "KXXRP"]):
        return "crypto"
    return "default"


def longshot_edge(yes_price_cents, ticker="", hours_to_close=999):
    """Estimate longshot overpricing edge using category-adjusted Becker model."""
    if yes_price_cents <= 0 or yes_price_cents > 99:
        return 0.0

    category = classify_ticker_category(ticker)
    amplitude, decay_rate = LONGSHOT_BIAS_PARAMS.get(category, LONGSHOT_BIAS_PARAMS["default"])

    # Full edge applies beyond 24h; near expiry it decays toward a 50% floor.
    time_factor = min(1.0, 0.5 + 0.5 * min(hours_to_close, 24) / 24)
    overpricing_ratio = amplitude * math.exp(-decay_rate * yes_price_cents) * time_factor

    implied_prob = yes_price_cents / 100.0
    true_prob = implied_prob * (1.0 - overpricing_ratio)
    additive_edge = implied_prob - true_prob
    return max(0.0, additive_edge)


__all__ = [
    "LONGSHOT_BIAS_PARAMS",
    "classify_ticker_category",
    "longshot_edge",
]
