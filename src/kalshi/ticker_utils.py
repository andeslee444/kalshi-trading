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
        15-min: KXBTC15M-<YY><MON><DDHHMM>-<threshold>
        Monthly max: KXBTCMAXMON-<ASSET>-<YY><MON><DD>-<threshold>
        Monthly min: KXBTCMINMON-<ASSET>-<YY><MON><DD>-<threshold>

    Asset suffixes (D=daily, E=expiry) are stripped.

    Examples:
        KXBTC-26MAR0117-T72999.99 -> {"asset": "BTC", "date": "2026-03-01", ..., "settlement_hour": 17}
        KXDOGE-26MAR0117-T0.175   -> {"asset": "DOGE", "date": "2026-03-01", ..., "settlement_hour": 17}
        KXETH-26MAR01-B3500       -> {"asset": "ETH", "date": "2026-03-01", ...}
        KXBTC15M-26MAR042230-30   -> {"asset": "BTC", "date": "2026-03-04", ..., "market_type": "15m"}
        KXBTCMAXMON-BTC-26MAR31-8750000 -> {"asset": "BTC", ..., "market_type": "maxmon"}
        KXBTCMINMON-BTC-26MAR31-6500000 -> {"asset": "BTC", ..., "market_type": "minmon"}

    Falls back to a simpler pattern if the full date format doesn't match.
    """
    # --- 15-minute bracket format: KXBTC15M-26MAR042230-30 ---
    m15 = re.match(r"KX(BTC|ETH|SOL|DOGE|XRP)15M-(\d{2})([A-Z]{3})(\d{2})(\d{4})-(\d+\.?\d*)", ticker)
    if m15:
        asset = m15.group(1)
        yr = int(m15.group(2))
        mon = m15.group(3)
        day = int(m15.group(4))
        hhmm = m15.group(5)
        threshold = float(m15.group(6))
        month = MONTHS.get(mon)
        if not month:
            return None
        settlement_hour = int(hhmm[:2])
        settlement_minute = int(hhmm[2:])
        return {
            "asset": asset,
            "date": f"{2000+yr}-{month:02d}-{day:02d}",
            "direction": "B",  # 15-min brackets are bracket markets
            "threshold": threshold,
            "settlement_hour": settlement_hour,
            "settlement_minute": settlement_minute,
            "market_type": "15m",
        }

    # --- Monthly max/min format: KXBTCMAXMON-BTC-26MAR31-8750000 ---
    mmax = re.match(r"KX(BTC|ETH|SOL|DOGE|XRP)(MAX|MIN)MON-\w+-(\d{2})([A-Z]{3})(\d{2})-(\d+\.?\d*)", ticker)
    if mmax:
        asset = mmax.group(1)
        max_or_min = mmax.group(2)
        yr = int(mmax.group(3))
        mon = mmax.group(4)
        day = int(mmax.group(5))
        threshold_raw = float(mmax.group(6))
        month = MONTHS.get(mon)
        if not month:
            return None
        # Threshold is in cents for max/min markets — divide by 100
        threshold = threshold_raw / 100.0
        direction = "T" if max_or_min == "MAX" else "B"
        market_type = "maxmon" if max_or_min == "MAX" else "minmon"
        return {
            "asset": asset,
            "date": f"{2000+yr}-{month:02d}-{day:02d}",
            "direction": direction,
            "threshold": threshold,
            "market_type": market_type,
        }

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


# ─── City code → full name ───
CITY_NAMES = {
    "MIA": "Miami", "NYC": "New York", "NY": "New York", "CHI": "Chicago",
    "LAX": "Los Angeles", "LA": "Los Angeles",
    "DFW": "Dallas", "ATL": "Atlanta", "DEN": "Denver", "PHX": "Phoenix",
    "HOU": "Houston", "SEA": "Seattle", "BOS": "Boston", "MSP": "Minneapolis",
    "DTW": "Detroit", "SFO": "San Francisco", "DCA": "Washington DC",
    "AUS": "Austin", "LAS": "Las Vegas", "ORD": "Chicago", "JFK": "New York",
    "PHL": "Philadelphia", "PHIL": "Philadelphia",
    "CLT": "Charlotte", "TPA": "Tampa", "MCO": "Orlando",
    "SLC": "Salt Lake City", "PDX": "Portland", "STL": "St. Louis",
    "BNA": "Nashville", "IND": "Indianapolis", "MKE": "Milwaukee",
    "BWI": "Baltimore", "RDU": "Raleigh", "CVG": "Cincinnati",
    "PIT": "Pittsburgh", "MCI": "Kansas City", "SAT": "San Antonio",
    "JAX": "Jacksonville", "MEM": "Memphis", "OKC": "Oklahoma City",
    "ABQ": "Albuquerque", "TUS": "Tucson", "ELP": "El Paso",
}

MONTH_ABBR = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
              7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}

CRYPTO_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana",
                "DOGE": "Dogecoin", "XRP": "XRP"}

ECON_NAMES = {"KXCPI": "CPI", "KXGDP": "GDP", "KXJOBS": "Jobs", "KXPCE": "PCE"}


def format_ticker_human(ticker: str, city_names: dict | None = None) -> str:
    """Format a raw Kalshi ticker into a human-readable display string.

    Examples:
        KXHIGHMIA-26FEB28-T86    → "Miami Above 86°F · Feb 28"
        KXBTC-26MAR0117-T72999.99 → "Bitcoin Above $73,000 · Mar 1 5pm"
        KXCPI-26MAR12-T0.3        → "CPI Above 0.3% · Mar 12"
    """
    cities = city_names or CITY_NAMES

    # 1. Weather tickers
    w = parse_weather_ticker(ticker)
    if w:
        city = cities.get(w["city"], w["city"])
        direction = "Above" if w["direction"] == "T" else "Below"
        threshold = w["threshold"]
        th_str = f"{int(threshold)}" if threshold == int(threshold) else f"{threshold}"
        date_parts = w["date"].split("-")
        month = MONTH_ABBR.get(int(date_parts[1]), date_parts[1])
        day = int(date_parts[2])
        return f"{city} {direction} {th_str}°F · {month} {day}"

    # 2. Crypto tickers
    c = parse_crypto_ticker(ticker)
    if c:
        asset = CRYPTO_NAMES.get(c["asset"], c["asset"])
        market_type = c.get("market_type")
        # Determine direction label based on market type
        if market_type == "15m":
            direction = "Bracket"
        elif market_type == "maxmon":
            direction = "Max Above"
        elif market_type == "minmon":
            direction = "Min Below"
        else:
            direction = "Above" if c["direction"] == "T" else "Below"
        threshold = c["threshold"]
        if threshold >= 1000:
            th_str = f"${threshold:,.0f}"
        elif threshold == int(threshold):
            th_str = f"${int(threshold):,}"
        else:
            th_str = f"${threshold:,}"
        date_str = ""
        if "date" in c:
            date_parts = c["date"].split("-")
            month = MONTH_ABBR.get(int(date_parts[1]), date_parts[1])
            day = int(date_parts[2])
            date_str = f" · {month} {day}"
            if c.get("settlement_hour") is not None:
                h = c["settlement_hour"]
                hour_str = f"{h % 12 or 12}{'pm' if h >= 12 else 'am'}"
                date_str += f" {hour_str}"
                if c.get("settlement_minute") is not None and c["settlement_minute"] != 0:
                    date_str = date_str[:-len(hour_str)]  # remove hour_str
                    minute = c["settlement_minute"]
                    hour_str = f"{h % 12 or 12}:{minute:02d}{'pm' if h >= 12 else 'am'}"
                    date_str += f"{hour_str}"
        type_suffix = ""
        if market_type == "15m":
            type_suffix = " (15m)"
        elif market_type in ("maxmon", "minmon"):
            type_suffix = " (Monthly)"
        return f"{asset} {direction} {th_str}{date_str}{type_suffix}"

    # 3. Economics tickers
    for prefix, name in ECON_NAMES.items():
        if ticker.upper().startswith(prefix):
            m = re.match(r"KX\w+-(\d{2})([A-Z]{3})(\d{2})-([TB])([\d.]+)", ticker)
            if m:
                g2, mon, g4 = int(m.group(1)), m.group(2), int(m.group(3))
                direction = "Above" if m.group(4) == "T" else "Below"
                threshold = m.group(5)
                month_num = MONTHS.get(mon)
                if month_num:
                    if g2 >= 25:
                        day = g4
                    else:
                        day = g2
                    month_str = MONTH_ABBR.get(month_num, mon)
                    return f"{name} {direction} {threshold}% · {month_str} {day}"
            return f"{name} · {ticker}"

    # 4. Album/entertainment tickers
    if ticker.upper().startswith(("KXALBUM", "KX1ALBUM")):
        m = re.match(r"KX1?ALBUM\w*-(\d{2})([A-Z]{3})(\d{2})", ticker)
        if m:
            g2, mon, g4 = int(m.group(1)), m.group(2), int(m.group(3))
            month_num = MONTHS.get(mon)
            if month_num:
                if g2 >= 25:
                    day = g4
                else:
                    day = g2
                month_str = MONTH_ABBR.get(month_num, mon)
                return f"Album Sales · {month_str} {day}"
        return "Album Sales"

    # 5. Economic stat tickers: KXECONSTATCPIYOY-26JUN-T2.0, KXECONSTATCORECPIYOY-26JUN-T2.3
    m = re.match(r"KXECONSTAT(CORE)?(CPI|GDP|JOBS|PCE|PPI)(YOY|MOM)?-(\d{2})([A-Z]{3})(?:(\d{2}))?(?:-([TB])([\d.]+))?", ticker)
    if m:
        core = "Core " if m.group(1) else ""
        stat = m.group(2)
        freq = " YoY" if m.group(3) == "YOY" else " MoM" if m.group(3) == "MOM" else ""
        yr = int(m.group(4))
        mon = m.group(5)
        day = m.group(6)
        direction_code = m.group(7)
        threshold = m.group(8)
        month_num = MONTHS.get(mon)
        month_str = MONTH_ABBR.get(month_num, mon) if month_num else mon
        date_str = month_str
        if day:
            date_str += f" {int(day)}"
        if direction_code and threshold:
            direction = "Above" if direction_code == "T" else "Below"
            return f"{core}{stat}{freq} {direction} {threshold}% · {date_str}"
        return f"{core}{stat}{freq} · {date_str}"

    # 6. Generic KX ticker fallback — extract event name and make it readable
    if ticker.upper().startswith("KX"):
        return _humanize_generic_ticker(ticker)

    return ticker


# ─── Known event names for generic ticker humanization ───
_EVENT_NAMES = {
    "FACUPADVANCE": "FA Cup Advance",
    "FACUP": "FA Cup",
    "MARMAD1SEED": "March Madness #1 Seed",
    "MARMAD": "March Madness",
    "MARMADCHAMP": "March Madness Champion",
    "MARMADFINAL4": "March Madness Final 4",
    "MARMADELITE8": "March Madness Elite 8",
    "MARMADSWEET16": "March Madness Sweet 16",
    "NFLMVP": "NFL MVP",
    "NFLPLAYOFFS": "NFL Playoffs",
    "NFLSB": "Super Bowl",
    "NBAMVP": "NBA MVP",
    "NBACHAMP": "NBA Champion",
    "NBA": "NBA",
    "MLBWS": "World Series",
    "MLB": "MLB",
    "NHL": "NHL",
    "NHLSC": "Stanley Cup",
    "OSCARS": "Oscars",
    "OSCAR": "Oscar",
    "GRAMMY": "Grammy Awards",
    "EMMYS": "Emmy Awards",
    "POTUS": "Presidential Election",
    "SENATE": "Senate Race",
    "HOUSE": "House Race",
    "FEDRATE": "Fed Rate Decision",
    "FEDCUT": "Fed Rate Cut",
    "SCOTUS": "Supreme Court",
    "INX": "S&P 500",
    "INXD": "S&P 500 Daily",
    "NASDAQ": "Nasdaq",
    "NDXD": "Nasdaq Daily",
    "TIKTOKBAN": "TikTok Ban",
    "TRUMPTARIFF": "Trump Tariff",
    "GOVSHUTDOWN": "Gov't Shutdown",
    "NASCARCUPSERIES": "NASCAR Cup Series",
    "NASCAR": "NASCAR",
    "PRESNOMD": "Pres. Nomination (D)",
    "PRESNOMR": "Pres. Nomination (R)",
    "PRESNOM": "Pres. Nomination",
    "DEMSNOM": "Dem Nomination",
    "GOPNOM": "GOP Nomination",
    "F1WDC": "F1 World Championship",
    "F1": "Formula 1",
    "PGA": "PGA Tour",
    "UFC": "UFC",
    "BOXING": "Boxing",
    "SOCCER": "Soccer",
    "EPL": "Premier League",
    "EPLRELEGATE": "Premier League Relegation",
    "UCLCHAMP": "Champions League",
    "LALIGA": "La Liga",
    "SERIEA": "Serie A",
    "MLS": "MLS",
    "CFBCHAMP": "College Football Champion",
    "HEISMAN": "Heisman Trophy",
    "WORLDSERIES": "World Series",
    "NBAALLSTAR": "NBA All-Star",
    "NBAROY": "NBA Rookie of the Year",
    "NBADPOY": "NBA DPOY",
    "SUPERBOWL": "Super Bowl",
}

# School / team abbreviations for sports outcomes
_TEAM_NAMES = {
    # College basketball / football
    "HOU": "Houston", "UVA": "Virginia", "ARIZ": "Arizona", "ISU": "Iowa St",
    "CONN": "UConn", "DUK": "Duke", "UNC": "North Carolina", "KU": "Kansas",
    "PUR": "Purdue", "BAYLOR": "Baylor", "GONZ": "Gonzaga", "AUB": "Auburn",
    "TENN": "Tennessee", "FLA": "Florida", "MARQ": "Marquette", "WISC": "Wisconsin",
    "MICH": "Michigan", "UK": "Kentucky", "STAN": "Stanford", "UCLA": "UCLA",
    "MSU": "Michigan St", "OHST": "Ohio St", "TXTECH": "Texas Tech", "CREI": "Creighton",
    "PROV": "Providence", "NOVA": "Villanova", "OREG": "Oregon", "ALA": "Alabama",
    "MIZZ": "Missouri", "ARK": "Arkansas", "ILL": "Illinois", "IUPUI": "IUPUI",
    "TXAM": "Texas A&M", "ND": "Notre Dame", "WAKE": "Wake Forest",
    "CLEMSON": "Clemson", "VT": "Virginia Tech", "LOU": "Louisville",
    # NBA
    "LAL": "Lakers", "LAC": "Clippers", "BOS": "Celtics", "GSW": "Warriors",
    "MIL": "Bucks", "PHI": "76ers", "DEN": "Nuggets", "PHX": "Suns",
    "MIA": "Heat", "DAL": "Mavericks", "NYK": "Knicks", "BKN": "Nets",
    "CLE": "Cavaliers", "MIN": "Timberwolves", "SAC": "Kings", "OKC": "Thunder",
    "IND": "Pacers", "ATL": "Hawks", "CHI": "Bulls", "TOR": "Raptors",
    "NOP": "Pelicans", "POR": "Trail Blazers", "SAS": "Spurs", "WAS": "Wizards",
    "CHA": "Hornets", "DET": "Pistons", "ORL": "Magic", "MEM": "Grizzlies",
    "UTA": "Jazz", "HOU2": "Rockets",
    # Soccer / Premier League
    "BRC": "Brentford", "ARS": "Arsenal", "MCI": "Man City", "MUN": "Man United",
    "LIV": "Liverpool", "CHE": "Chelsea", "TOT": "Tottenham", "NEW": "Newcastle",
    "EVE": "Everton", "WHU": "West Ham", "BHA": "Brighton", "AVL": "Aston Villa",
    "FUL": "Fulham", "WOL": "Wolves", "BOU": "Bournemouth", "CRY": "Crystal Palace",
    "NFO": "Nottm Forest", "LEI": "Leicester", "IPS": "Ipswich", "SOU": "Southampton",
    # NASCAR
    "TRED": "Toyota Racing", "CHEV": "Chevrolet", "FORD": "Ford",
    # Politics (candidates)
    "REMA": "Ramaswamy", "TRUMP": "Trump", "BIDEN": "Biden", "HARRIS": "Harris",
    "DESANTIS": "DeSantis", "HALEY": "Haley", "NEWSOM": "Newsom",
    "PENCE": "Pence", "SCOTT": "Tim Scott", "VANCE": "Vance",
    "RFK": "RFK Jr", "KENNEDY": "Kennedy",
}


def _humanize_generic_ticker(ticker: str) -> str:
    """Best-effort humanization of unknown KX tickers.

    KXFACUPADVANCE-26FEB14PVABRC-BRC → "FA Cup Advance · Brentford"
    KXMARMAD1SEED-26-HOU             → "March Madness #1 Seed · Houston"
    """
    parts = ticker.split("-")
    event = parts[0]

    # Strip KX prefix
    if event.upper().startswith("KX"):
        event = event[2:]

    event_upper = event.upper()

    # Find the best matching event name (longest prefix match)
    best_match = ""
    best_name = ""
    for prefix, name in _EVENT_NAMES.items():
        if event_upper.startswith(prefix) and len(prefix) > len(best_match):
            best_match = prefix
            best_name = name

    # Extract outcome from the last part (team/player name)
    outcome = ""
    if len(parts) >= 2:
        last = parts[-1]
        # Check if it's a known team
        if last.upper() in _TEAM_NAMES:
            outcome = _TEAM_NAMES[last.upper()]
        elif last.upper() in CITY_NAMES:
            outcome = CITY_NAMES[last.upper()]
        elif re.match(r"^[A-Z]{2,}$", last) and not re.match(r"^\d", last):
            # Unknown all-caps code, title-case it
            outcome = last.title()

    if best_name:
        if outcome:
            return f"{best_name} · {outcome}"
        return best_name

    # No known event match — insert spaces in camelCase and title-case
    spaced = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', event)
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', spaced)
    spaced = re.sub(r'(\d+)', r' \1 ', spaced).strip()
    result = spaced.title() if spaced else ticker
    if outcome and outcome.lower() not in result.lower():
        result += f" · {outcome}"
    return result
