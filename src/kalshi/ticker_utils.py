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

    Handles both old and new Kalshi formats:
        Old: KXHIGH<CITY>-<DD><MON><YY>  (pre-2026-02-22)
        New: KXHIGH[T]<CITY>-<YY><MON><DD>  (current, T prefix on newer cities)

    Examples:
        KXHIGHMIA-26FEB28-T86   -> {"city": "MIA", "date": "2026-02-28", "direction": "T", "threshold": 86.0}
        KXHIGHTATL-26MAR01-B70.5 -> {"city": "ATL", "date": "2026-03-01", "direction": "B", "threshold": 70.5}
    """
    m = re.match(r"KXHIGHT?([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
    if not m:
        return None
    city = m.group(1)
    g2, mon, g4 = int(m.group(2)), m.group(3), int(m.group(4))
    direction = m.group(5)
    threshold = float(m.group(6))
    month = MONTHS.get(mon)
    if not month:
        return None
    # Detect format: if first number >= 25 it's a year (YYMONDD), otherwise a day (DDMONYY)
    if g2 >= 25:
        yr, day = g2, g4
    else:
        day, yr = g2, g4
    return {
        "city": city,
        "date": f"{2000+yr}-{month:02d}-{day:02d}",
        "direction": direction,
        "threshold": threshold,
    }


def parse_crypto_ticker(ticker):
    """Parse crypto market tickers (BTC, ETH, SOL, DOGE, XRP).

    Handles both old and new Kalshi date formats, including hourly settlement:
        Old: KX<ASSET>-<DD><MON><YY>-<TB><threshold>
        New: KX<ASSET>-<YY><MON><DD>-<TB><threshold>
        Hourly: KX<ASSET>-<YY><MON><DDHR>-<TB><threshold>

    Asset suffixes (D=daily, E=expiry) are stripped.

    Examples:
        KXBTC-26MAR0117-T72999.99 -> {"asset": "BTC", "date": "2026-03-01", ..., "settlement_hour": 17}
        KXDOGE-26MAR0117-T0.175   -> {"asset": "DOGE", "date": "2026-03-01", ..., "settlement_hour": 17}
        KXETH-26MAR01-B3500       -> {"asset": "ETH", "date": "2026-03-01", ...}

    Falls back to a simpler pattern if the full date format doesn't match.
    """
    m = re.match(r"KX(BTC|ETH|SOL|DOGE|XRP|CRYPTO)(\w*)-(\d{2})([A-Z]{3})(\d{2,4})-([TB])([\d.]+)", ticker)
    if not m:
        # Try simpler format without date
        m2 = re.match(r"KX(BTC|ETH|SOL|DOGE|XRP).*-([TB])([\d.]+)$", ticker)
        if m2:
            return {
                "asset": m2.group(1),
                "direction": m2.group(2),
                "threshold": float(m2.group(3)),
            }
        return None

    asset = m.group(1)
    g2 = int(m.group(3))
    mon = m.group(4)
    g4_str = m.group(5)
    direction = m.group(6)
    threshold = float(m.group(7))
    month = MONTHS.get(mon)
    if not month:
        return None

    # Detect format: if first number >= 25 it's a year (YYMONDD[HR]), otherwise a day (DDMONYY)
    settlement_hour = None
    if g2 >= 25:
        yr = g2
        if len(g4_str) == 4:
            day = int(g4_str[:2])
            settlement_hour = int(g4_str[2:])
        else:
            day = int(g4_str)
    else:
        day = g2
        yr = int(g4_str)

    result = {
        "asset": asset,
        "date": f"{2000+yr}-{month:02d}-{day:02d}",
        "direction": direction,
        "threshold": threshold,
    }
    if settlement_hour is not None:
        result["settlement_hour"] = settlement_hour
    return result
