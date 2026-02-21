"""Shared ticker parsing utilities for Kalshi market tickers.

All bots should use these parsers instead of inline implementations.
"""

import re

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_weather_ticker(ticker):
    """Parse KXHIGH weather temperature tickers.

    Examples:
        KXHIGHMIA-21FEB26-T86  -> {"city": "MIA", "date": "2026-02-21", "direction": "T", "threshold": 86.0}
        KXHIGHMIA-21FEB26-B85.5 -> {"city": "MIA", "date": "2026-02-21", "direction": "B", "threshold": 85.5}

    Ticker format: KXHIGH<CITY>-<DD><MON><YY>-<T|B><threshold>
    """
    m = re.match(r"KXHIGH([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m:
        return None
    city = m.group(1)
    day, mon, yr = int(m.group(2)), m.group(3), int(m.group(4))
    direction = m.group(5)
    threshold = float(m.group(6))
    month = MONTHS.get(mon)
    if not month:
        return None
    return {
        "city": city,
        "date": f"{2000+yr}-{month:02d}-{day:02d}",
        "direction": direction,
        "threshold": threshold,
    }


def parse_crypto_ticker(ticker):
    """Parse crypto market tickers (BTC, ETH, SOL).

    Examples:
        KXBTC-21FEB26-T70000  -> {"asset": "BTC", "date": "2026-02-21", "direction": "T", "threshold": 70000.0}
        KXETH-21FEB26-B3500   -> {"asset": "ETH", "date": "2026-02-21", "direction": "B", "threshold": 3500.0}

    Falls back to a simpler pattern if the full date format doesn't match.
    """
    m = re.match(r"KX(BTC|ETH|SOL|CRYPTO)(\w*)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m:
        # Try simpler format without date
        m2 = re.match(r"KX(BTC|ETH|SOL).*-([TB])([\d.]+)$", ticker)
        if m2:
            return {
                "asset": m2.group(1),
                "direction": m2.group(2),
                "threshold": float(m2.group(3)),
            }
        return None

    asset = m.group(1)
    day, mon, yr = int(m.group(3)), m.group(4), int(m.group(5))
    direction = m.group(6)
    threshold = float(m.group(7))
    month = MONTHS.get(mon)
    if not month:
        return None

    return {
        "asset": asset,
        "date": f"{2000+yr}-{month:02d}-{day:02d}",
        "direction": direction,
        "threshold": threshold,
    }
