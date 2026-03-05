"""Weather data infrastructure for ensemble collection, IEM station actuals, and training storage.

Provides:
- STATION_MAP: Kalshi city code -> IEM ASOS station ID mapping
- EnsembleCollector: Fetches raw ensemble member temperatures from Open-Meteo
- IEMFetcher: Fetches actual daily high temperatures from Iowa Environmental Mesonet
- TrainingStore: SQLite storage for forecast-vs-actual training pairs
"""

import logging
import sqlite3

_log = logging.getLogger("weather_data")

# Import retry_request at module level for easier mocking in tests.
# Falls back to None if kalshi_auth is not available (e.g. standalone usage).
try:
    from kalshi_auth import retry_request as _retry_request
except ImportError:
    _retry_request = None

# Canonical mapping of Kalshi city codes to IEM ASOS station IDs.
# These are the official weather stations that Kalshi uses for settlement.
STATION_MAP = {
    "MIA": "KMIA",
    "LAX": "KLAX",
    "PHIL": "KPHL",
    "NY": "KNYC",
    "CHI": "KMDW",
    "AUS": "KAUS",
    "DEN": "KDEN",
    "HOU": "KHOU",
    "ATL": "KATL",
    "BOS": "KBOS",
    "SFO": "KSFO",
    "SEA": "KSEA",
    "LV": "KLAS",
    "DAL": "KDFW",
    "MIN": "KMSP",
    "PHX": "KPHX",
    "DC": "KDCA",
    "NOLA": "KMSY",
    "OKC": "KOKC",
    "SATX": "KSAT",
}


class EnsembleCollector:
    """Fetches raw ensemble member temperatures from Open-Meteo Ensemble API.

    Returns per-date lists of all ensemble member forecasts (typically 82 members:
    31 GEFS + 51 ECMWF ENS when both models requested).
    """

    def __init__(self, logger=None):
        self.log = logger or _log

    def fetch_ensemble(self, lat, lon, forecast_days=14):
        """Fetch ensemble member temperatures from Open-Meteo.

        Args:
            lat: latitude
            lon: longitude
            forecast_days: number of forecast days (default 14)

        Returns:
            dict of {date_str: [temp1, temp2, ..., tempN]} where N is total
            ensemble members found, or None on API failure.
        """
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return None

        url = (
            f"https://ensemble-api.open-meteo.com/v1/ensemble?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=America%2FNew_York&forecast_days={forecast_days}"
            f"&models=gfs_seamless,ecmwf_ifs025"
        )

        try:
            resp = _retry_request("GET", url, timeout=15, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.log.warning(
                    "Ensemble API returned status %s",
                    getattr(resp, "status_code", "None"),
                )
                return None

            data = resp.json()
            daily = data.get("daily", {})

            # Extract dates
            dates = daily.get("time", [])
            if not dates:
                self.log.warning("Ensemble API returned no dates")
                return None

            # Find all member columns: temperature_2m_max_memberNN
            member_keys = sorted(
                k for k in daily.keys()
                if k.startswith("temperature_2m_max_member")
            )

            if not member_keys:
                self.log.warning("Ensemble API returned no member columns")
                return None

            # Build result: {date_str: [temps from all members]}
            result = {}
            for i, date_str in enumerate(dates):
                temps = []
                for key in member_keys:
                    values = daily[key]
                    if i < len(values) and values[i] is not None:
                        temps.append(values[i])
                if temps:
                    result[date_str] = temps

            self.log.debug(
                "Ensemble: %d dates, %d members per date",
                len(result),
                len(member_keys),
            )
            return result

        except Exception as e:
            self.log.warning("Ensemble API error: %s", e)
            return None


class IEMFetcher:
    """Fetches actual daily high temperatures from Iowa Environmental Mesonet (IEM) ASOS.

    IEM provides official ASOS station observations that match Kalshi settlement data.
    """

    def __init__(self, logger=None):
        self.log = logger or _log

    def fetch_daily_high(self, station_id, date_str):
        """Fetch actual daily high temperature for a single date.

        Args:
            station_id: IEM ASOS station ID (e.g. "KNYC", "KMIA")
            date_str: date in YYYY-MM-DD format

        Returns:
            Temperature in Fahrenheit (float), or None if missing/failed.
        """
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return None

        try:
            parts = date_str.split("-")
            year, month, day = parts[0], parts[1], parts[2]
        except (IndexError, ValueError):
            self.log.warning("Invalid date format: %s", date_str)
            return None

        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
            f"station={station_id}&data=max_tmpf&tz=America/New_York"
            f"&format=comma&year1={year}&month1={month}&day1={day}"
            f"&year2={year}&month2={month}&day2={day}"
        )

        try:
            resp = _retry_request("GET", url, timeout=10, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.log.warning(
                    "IEM ASOS returned status %s for %s/%s",
                    getattr(resp, "status_code", "None"),
                    station_id,
                    date_str,
                )
                return None

            return self._parse_csv_single(resp.text)
        except Exception as e:
            self.log.warning("IEM ASOS error for %s/%s: %s", station_id, date_str, e)
            return None

    def fetch_daily_highs(self, station_id, start_date, end_date):
        """Fetch actual daily high temperatures for a date range.

        Args:
            station_id: IEM ASOS station ID
            start_date: start date in YYYY-MM-DD format
            end_date: end date in YYYY-MM-DD format

        Returns:
            dict of {date_str: float} with daily highs, or empty dict on failure.
        """
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return {}

        try:
            s = start_date.split("-")
            e = end_date.split("-")
        except (IndexError, ValueError):
            self.log.warning("Invalid date format: %s to %s", start_date, end_date)
            return {}

        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
            f"station={station_id}&data=max_tmpf&tz=America/New_York"
            f"&format=comma&year1={s[0]}&month1={s[1]}&day1={s[2]}"
            f"&year2={e[0]}&month2={e[1]}&day2={e[2]}"
        )

        try:
            resp = _retry_request("GET", url, timeout=15, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.log.warning(
                    "IEM ASOS returned status %s for %s range",
                    getattr(resp, "status_code", "None"),
                    station_id,
                )
                return {}

            return self._parse_csv_range(resp.text)
        except Exception as e:
            self.log.warning("IEM ASOS range error for %s: %s", station_id, e)
            return {}

    def _parse_csv_single(self, text):
        """Parse IEM CSV response for a single date, returning float or None."""
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "station" in line.lower() and "max_tmpf" in line.lower():
                continue
            parts = line.split(",")
            if len(parts) >= 3:
                max_tmpf = parts[2].strip()
                if max_tmpf == "M" or max_tmpf == "":
                    return None
                try:
                    return float(max_tmpf)
                except ValueError:
                    return None
        return None

    def _parse_csv_range(self, text):
        """Parse IEM CSV response for a date range, returning {date: float}."""
        result = {}
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "station" in line.lower() and "max_tmpf" in line.lower():
                continue
            parts = line.split(",")
            if len(parts) >= 3:
                # valid column is date string like "2026-03-01"
                date_str = parts[1].strip()
                # IEM sometimes returns datetime format; extract date part
                if " " in date_str:
                    date_str = date_str.split(" ")[0]
                max_tmpf = parts[2].strip()
                if max_tmpf == "M" or max_tmpf == "":
                    continue
                try:
                    result[date_str] = float(max_tmpf)
                except ValueError:
                    continue
        return result


class TrainingStore:
    """SQLite storage for forecast-vs-actual training pairs.

    Stores ensemble member forecasts alongside actual observed temperatures
    and market outcomes for model training and calibration.
    """

    def __init__(self, db_path="data/weather-training.db", logger=None):
        self.log = logger or _log
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        """Create training_pairs table if it doesn't exist."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS training_pairs (
                city TEXT,
                date TEXT,
                model TEXT,
                lead_days INTEGER,
                member_id INTEGER,
                forecast_temp REAL,
                actual_temp_cli REAL,
                market_outcome TEXT,
                PRIMARY KEY (city, date, model, lead_days, member_id)
            )
        """)
        self.conn.commit()

    def insert_pair(self, city, date, model, lead_days, member_id,
                    forecast_temp, actual_temp_cli=None, market_outcome=None):
        """Insert or replace a single training pair."""
        self.conn.execute(
            """INSERT OR REPLACE INTO training_pairs
               (city, date, model, lead_days, member_id, forecast_temp,
                actual_temp_cli, market_outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (city, date, model, lead_days, member_id, forecast_temp,
             actual_temp_cli, market_outcome),
        )
        self.conn.commit()

    def insert_batch(self, rows):
        """Bulk insert training pairs.

        Args:
            rows: list of tuples (city, date, model, lead_days, member_id,
                  forecast_temp, actual_temp_cli, market_outcome)
        """
        self.conn.executemany(
            """INSERT OR REPLACE INTO training_pairs
               (city, date, model, lead_days, member_id, forecast_temp,
                actual_temp_cli, market_outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self.conn.commit()

    def get_pairs(self, city=None, model=None, min_lead_days=None, max_lead_days=None):
        """Query training pairs with optional filters.

        Returns:
            list of dicts with keys: city, date, model, lead_days, member_id,
            forecast_temp, actual_temp_cli, market_outcome
        """
        query = "SELECT * FROM training_pairs WHERE 1=1"
        params = []

        if city is not None:
            query += " AND city = ?"
            params.append(city)
        if model is not None:
            query += " AND model = ?"
            params.append(model)
        if min_lead_days is not None:
            query += " AND lead_days >= ?"
            params.append(min_lead_days)
        if max_lead_days is not None:
            query += " AND lead_days <= ?"
            params.append(max_lead_days)

        cursor = self.conn.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def count(self):
        """Return total row count."""
        cursor = self.conn.execute("SELECT COUNT(*) FROM training_pairs")
        return cursor.fetchone()[0]

    def close(self):
        """Close the database connection."""
        self.conn.close()
