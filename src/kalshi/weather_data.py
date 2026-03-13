"""Weather data infrastructure for ensemble collection, settlement actuals, and training storage.

Provides:
- STATION_MAP: Kalshi city code -> IEM ASOS station ID mapping
- NWS_GRID_MAP: Kalshi city code -> NWS API grid point mapping
- NWSForecastFetcher: Fetches NWS 7-day forecast as fallback data source
- EnsembleCollector: Fetches raw ensemble member temperatures from Open-Meteo
- HRRRFetcher: Fetches HRRR deterministic forecast data from Open-Meteo
- NAMFetcher: Fetches NAM 3km deterministic forecast from Open-Meteo
- PreviousRunsFetcher: Forecast convergence analysis from previous model runs
- BiasCorrector: Per-city per-model systematic forecast bias correction
- NWSClimateReportFetcher: Fetches final NWS Daily Climate Report actual highs
- SettlementTemperatureFetcher: Uses NWS climate reports first, IEM as fallback
- IEMFetcher: Fetches actual daily high temperatures from Iowa Environmental Mesonet
- OrderBookDepth: Fetches and analyzes Kalshi order book depth
- MODEL_RUN_SCHEDULE / next_model_run(): Model run timing awareness
- TrainingStore: SQLite storage for forecast-vs-actual training pairs
"""

import datetime
import json
import logging
import math
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

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

MODEL_NAME_ALIASES = {
    "gfs": "gfs",
    "gfs_seamless": "gfs",
    "ecmwf": "ecmwf",
    "ecmwf_ifs025": "ecmwf",
    "ecmwf_ifs04": "ecmwf",
    "icon": "icon",
    "icon_seamless": "icon",
    "graphcast": "graphcast",
    "gfs_graphcast025": "graphcast",
    "aifs": "aifs",
    "ecmwf_aifs025": "aifs",
    "nbm": "nbm",
    "nbm_conus": "nbm",
    "hrrr": "hrrr",
    "hrrr_conus": "hrrr",
    "ncep_hrrr_conus": "hrrr",
    "nam": "nam",
    "nam_conus": "nam",
    "nws": "nws",
}

STATION_TO_CITY = {station: city for city, station in STATION_MAP.items()}


def _city_timezone_name(city_code=None, station_id=None):
    try:
        from kalshi_auth import CITY_TIMEZONES
        if city_code:
            return CITY_TIMEZONES.get(city_code, "America/New_York")
        if station_id:
            city = STATION_TO_CITY.get(station_id)
            if city:
                return CITY_TIMEZONES.get(city, "America/New_York")
    except ImportError:
        pass
    return "America/New_York"


def canonical_model_name(model_name):
    """Normalize API-specific model ids to internal short names."""
    if not model_name:
        return model_name
    return MODEL_NAME_ALIASES.get(model_name, model_name)


def _is_iem_header_row(parts):
    """Return True for CSV header rows that sometimes repeat in IEM responses."""
    if not parts:
        return False
    first = parts[0].strip().lower() if len(parts) >= 1 else ""
    third = parts[2].strip().lower() if len(parts) >= 3 else ""
    return first == "station" or third in {"max_tmpf", "tmpf"}


# NWS API grid point mapping for 7-day forecast fallback.
# Grid points determined by NWS /points/{lat},{lon} endpoint.
# office/gridX/gridY identify the NWS forecast grid cell for each station.
NWS_GRID_MAP = {
    "MIA": {"office": "MFL", "gridX": 76, "gridY": 50},
    "LAX": {"office": "LOX", "gridX": 152, "gridY": 44},
    "PHIL": {"office": "PHI", "gridX": 57, "gridY": 97},
    "NY": {"office": "OKX", "gridX": 33, "gridY": 37},
    "CHI": {"office": "LOT", "gridX": 65, "gridY": 76},
    "AUS": {"office": "EWX", "gridX": 156, "gridY": 93},
    "DEN": {"office": "BOU", "gridX": 62, "gridY": 60},
    "HOU": {"office": "HGX", "gridX": 65, "gridY": 97},
    "ATL": {"office": "FFC", "gridX": 52, "gridY": 88},
    "BOS": {"office": "BOX", "gridX": 71, "gridY": 90},
    "SFO": {"office": "MTR", "gridX": 85, "gridY": 105},
    "SEA": {"office": "SEW", "gridX": 124, "gridY": 67},
    "LV": {"office": "VEF", "gridX": 126, "gridY": 97},
    "DAL": {"office": "FWD", "gridX": 80, "gridY": 108},
    "MIN": {"office": "MPX", "gridX": 107, "gridY": 71},
    "PHX": {"office": "PSR", "gridX": 159, "gridY": 57},
    "DC": {"office": "LWX", "gridX": 97, "gridY": 71},
    "NOLA": {"office": "LIX", "gridX": 76, "gridY": 72},
    "OKC": {"office": "OUN", "gridX": 39, "gridY": 44},
    "SATX": {"office": "EWX", "gridX": 131, "gridY": 68},
}


class NWSForecastFetcher:
    """Fetches NWS 7-day forecast as fallback when Open-Meteo is unavailable.

    Uses the NWS API (api.weather.gov) gridpoint forecast endpoint.
    Returns daily high temperature forecasts in Fahrenheit.
    """

    NWS_CROSS_VALIDATE_THRESHOLD_F = 3.0  # Flag if sources disagree by >3F

    def __init__(self, logger=None):
        self.log = logger or _log

    def fetch_forecast(self, city_code):
        """Fetch NWS 7-day forecast for a city.

        Args:
            city_code: Kalshi city code (e.g. "MIA", "NY")

        Returns:
            dict of {date_str: temp_f} with daily high temps,
            or None on failure.
        """
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return None

        grid = NWS_GRID_MAP.get(city_code)
        if not grid:
            self.log.debug("No NWS grid mapping for city %s", city_code)
            return None

        url = (
            f"https://api.weather.gov/gridpoints/"
            f"{grid['office']}/{grid['gridX']},{grid['gridY']}/forecast"
        )

        try:
            resp = _retry_request("GET", url, timeout=10, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.log.warning(
                    "NWS API returned status %s for %s",
                    getattr(resp, "status_code", "None"),
                    city_code,
                )
                return None

            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])
            if not periods:
                self.log.warning("NWS API returned no forecast periods for %s", city_code)
                return None

            return self._parse_periods(periods)
        except Exception as e:
            self.log.warning("NWS API error for %s: %s", city_code, e)
            return None

    def _parse_periods(self, periods):
        """Parse NWS forecast periods into {date_str: high_temp_f}.

        NWS returns periods alternating between daytime and nighttime.
        We extract only daytime periods (isDaytime=True) with temperature.
        """
        result = {}
        for period in periods:
            if not period.get("isDaytime", False):
                continue
            temp = period.get("temperature")
            temp_unit = period.get("temperatureUnit", "F")
            start_time = period.get("startTime", "")

            if temp is None or not start_time:
                continue

            # Convert to Fahrenheit if needed
            if temp_unit == "C":
                temp = temp * 9.0 / 5.0 + 32.0

            # Extract date from ISO format "2026-03-07T06:00:00-05:00"
            date_str = start_time[:10]
            result[date_str] = float(temp)

        return result if result else None

    def cross_validate(self, city_code, open_meteo_temp, nws_temp):
        """Check if Open-Meteo and NWS forecasts agree within threshold.

        Args:
            city_code: city code for logging
            open_meteo_temp: temperature from Open-Meteo (F)
            nws_temp: temperature from NWS (F)

        Returns:
            True if sources agree (difference <= threshold), False if they diverge.
        """
        diff = abs(open_meteo_temp - nws_temp)
        if diff > self.NWS_CROSS_VALIDATE_THRESHOLD_F:
            self.log.warning(
                "%s: Open-Meteo (%.1fF) and NWS (%.1fF) disagree by %.1fF (>%.1fF threshold) "
                "-- one source may be stale",
                city_code, open_meteo_temp, nws_temp, diff,
                self.NWS_CROSS_VALIDATE_THRESHOLD_F,
            )
            return False
        return True


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

        api_key = os.environ.get("OPEN_METEO_API_KEY", "")
        base = "https://customer-ensemble-api.open-meteo.com/v1/ensemble" if api_key else "https://ensemble-api.open-meteo.com/v1/ensemble"
        url = (
            f"{base}?"
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=auto&forecast_days={forecast_days}"
            f"&models=gfs_seamless,ecmwf_ifs025,icon_global,gem_global"
            + (f"&apikey={api_key}" if api_key else "")
        )

        try:
            resp = _retry_request("GET", url, timeout=20, max_retries=2)
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

    IEM provides ASOS station observations used here as a proxy for Kalshi settlement.
    """

    def __init__(self, logger=None):
        self.log = logger or _log

    def fetch_daily_high(self, station_id, date_str, city_code=None):
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

        tz_name = _city_timezone_name(city_code=city_code, station_id=station_id)
        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
            f"station={station_id}&data=max_tmpf&tz={tz_name}"
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

    def fetch_daily_highs(self, station_id, start_date, end_date, city_code=None):
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

        tz_name = _city_timezone_name(city_code=city_code, station_id=station_id)
        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
            f"station={station_id}&data=max_tmpf&tz={tz_name}"
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
            parts = line.split(",")
            if _is_iem_header_row(parts):
                continue
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
            parts = line.split(",")
            if _is_iem_header_row(parts):
                continue
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


class NWSClimateReportFetcher:
    """Fetches actual daily highs from NWS Daily Climate Report (CLI) products."""

    MAX_RECENT_PRODUCTS = 40
    SUMMARY_DATE_RE = re.compile(
        r"\.\.\.THE .*? CLIMATE SUMMARY FOR ([A-Z]+ \d{1,2} \d{4})\.\.\."
    )
    TEMP_SECTION_RE = re.compile(
        r"TEMPERATURE \(F\)(.*?)(?:PRECIPITATION|SNOWFALL|DEGREE DAYS|WIND \(MPH\)|SKY COVER|WEATHER CONDITIONS)",
        re.DOTALL,
    )
    MAXIMUM_RE = re.compile(r"\bMAXIMUM\s+(-?\d+|MM)\b")

    def __init__(self, logger=None):
        self.log = logger or _log

    def fetch_daily_high(self, station_id, date_str, city_code=None):
        """Fetch actual high temperature from the final NWS climate report."""
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return None

        try:
            target_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            self.log.warning("Invalid date format: %s", date_str)
            return None

        location_id = self._location_id(station_id, city_code)
        products = self._fetch_recent_products(location_id)
        if not products:
            return None

        for product in products[:self.MAX_RECENT_PRODUCTS]:
            issue_time = self._parse_issue_time(product.get("issuanceTime"))
            if issue_time and issue_time.date() < target_date:
                break

            product_id = product.get("id")
            if not product_id:
                continue

            product_text = self._fetch_product_text(product_id)
            if not product_text:
                continue

            report_date, maximum = self._parse_product(product_text)
            if report_date == target_date:
                return maximum

        self.log.debug(
            "No NWS climate report match for %s/%s (%s)",
            station_id,
            date_str,
            location_id,
        )
        return None

    def _location_id(self, station_id, city_code=None):
        if station_id and station_id.startswith("K") and len(station_id) == 4:
            return station_id[1:]
        if station_id:
            return station_id
        return city_code or ""

    def _fetch_recent_products(self, location_id):
        url = f"https://api.weather.gov/products/types/CLI/locations/{location_id}"
        try:
            resp = _retry_request("GET", url, timeout=10, max_retries=2)
            if resp is None:
                return []
            data = resp.json()
            products = data.get("@graph", [])
            return sorted(
                products,
                key=lambda p: p.get("issuanceTime", ""),
                reverse=True,
            )
        except Exception as e:
            self.log.warning("NWS CLI product list error for %s: %s", location_id, e)
            return []

    def _fetch_product_text(self, product_id):
        url = f"https://api.weather.gov/products/{product_id}"
        try:
            resp = _retry_request("GET", url, timeout=10, max_retries=2)
            if resp is None:
                return None
            return resp.json().get("productText")
        except Exception as e:
            self.log.warning("NWS CLI product fetch error for %s: %s", product_id, e)
            return None

    def _parse_issue_time(self, value):
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None

    def _parse_product(self, product_text):
        if not product_text:
            return None, None

        summary_match = self.SUMMARY_DATE_RE.search(product_text)
        if not summary_match:
            return None, None

        try:
            report_date = datetime.datetime.strptime(
                summary_match.group(1).title(),
                "%B %d %Y",
            ).date()
        except ValueError:
            return None, None

        temp_section_match = self.TEMP_SECTION_RE.search(product_text)
        if not temp_section_match:
            return report_date, None

        maximum_match = self.MAXIMUM_RE.search(temp_section_match.group(1))
        if not maximum_match:
            return report_date, None

        maximum = maximum_match.group(1)
        if maximum == "MM":
            return report_date, None

        try:
            return report_date, float(maximum)
        except ValueError:
            return report_date, None


class SettlementTemperatureFetcher:
    """Fetch settlement temperatures from NWS climate reports with IEM fallback."""

    NWS_LOOKBACK_DAYS = 14

    def __init__(self, logger=None):
        self.log = logger or _log
        self.nws = NWSClimateReportFetcher(logger=self.log)
        self.iem = IEMFetcher(logger=self.log)

    def fetch_daily_high(self, station_id, date_str, city_code=None):
        """Fetch actual high, preferring the final NWS climate report."""
        value, _ = self.fetch_daily_high_with_source(
            station_id,
            date_str,
            city_code=city_code,
        )
        return value

    def fetch_daily_high_with_source(self, station_id, date_str, city_code=None):
        """Fetch actual high and the source used."""
        nws_value = self.nws.fetch_daily_high(station_id, date_str, city_code=city_code)
        if nws_value is not None:
            return nws_value, "nws_cli"
        iem_value = self.iem.fetch_daily_high(station_id, date_str, city_code=city_code)
        if iem_value is not None:
            return iem_value, "iem_fallback"
        return None, None

    def fetch_daily_highs(self, station_id, start_date, end_date, city_code=None):
        """Fetch actual highs for a range, overlaying recent NWS report values."""
        actuals = self.iem.fetch_daily_highs(
            station_id,
            start_date,
            end_date,
            city_code=city_code,
        )

        try:
            start = datetime.date.fromisoformat(start_date)
            end = datetime.date.fromisoformat(end_date)
        except ValueError:
            return actuals

        recent_cutoff = datetime.date.today() - datetime.timedelta(days=self.NWS_LOOKBACK_DAYS)
        current = max(start, recent_cutoff)
        while current <= end:
            date_key = current.isoformat()
            nws_value = self.nws.fetch_daily_high(station_id, date_key, city_code=city_code)
            if nws_value is not None:
                actuals[date_key] = nws_value
            current += datetime.timedelta(days=1)

        return actuals


class HRRRFetcher:
    """Fetches HRRR deterministic forecast from Open-Meteo (hrrr_conus model).

    HRRR (High-Resolution Rapid Refresh) provides 3km resolution hourly forecasts
    updated every hour, with ~45 minute processing delay. Dramatically improves
    day-0 and day-1 temperature forecasts.
    """

    def __init__(self, logger=None, rate_limiter=None):
        self.log = logger or _log
        self._rate_limiter = rate_limiter
        self.last_status_code = None
        self.last_error = None

    def fetch_hrrr(self, lat, lon):
        """Fetch HRRR hourly temps and compute daily max temperatures.

        HRRR only provides hourly data (not daily max), so we fetch hourly
        temperature_2m and group by calendar day to find daily maxima.

        Args:
            lat: latitude
            lon: longitude

        Returns:
            dict of {date_str: max_temp_f} for the next ~48 hours (2-3 calendar days),
            or None on API failure.
        """
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return None
        self.last_status_code = None
        self.last_error = None

        # Use premium endpoint if API key is configured
        api_key = os.environ.get("OPEN_METEO_API_KEY", "")
        base = "https://customer-api.open-meteo.com/v1/forecast" if api_key else "https://api.open-meteo.com/v1/forecast"
        model = "ncep_hrrr_conus" if api_key else "hrrr_conus"
        params = (
            f"latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m&temperature_unit=fahrenheit"
            f"&timezone=auto&forecast_days=2"
            f"&models={model}"
        )
        url = f"{base}?{params}" + (f"&apikey={api_key}" if api_key else "")

        try:
            if self._rate_limiter is not None:
                self._rate_limiter()
            resp = _retry_request("GET", url, timeout=15, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.last_status_code = getattr(resp, "status_code", None)
                self.last_error = f"status={self.last_status_code}"
                self.log.warning(
                    "HRRR API returned status %s",
                    getattr(resp, "status_code", "None"),
                )
                return None

            data = resp.json()
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])

            if not times or not temps:
                self.log.warning("HRRR API returned no hourly data")
                return None

            # Group hourly temps by calendar day and compute daily max
            day_temps = defaultdict(list)
            for i, time_str in enumerate(times):
                if i < len(temps) and temps[i] is not None:
                    # Extract date from "2026-03-05T14:00" format
                    date_str = time_str[:10]
                    day_temps[date_str].append(temps[i])

            if not day_temps:
                self.log.warning("HRRR: no valid temperature data after filtering nulls")
                return None

            result = {date: max(t_list) for date, t_list in day_temps.items()}

            self.log.debug(
                "HRRR: %d hours fetched, %d dates computed (max temps: %s)",
                len(times), len(result),
                {d: f"{t:.1f}F" for d, t in result.items()},
            )
            return result

        except Exception as e:
            self.last_status_code = getattr(getattr(e, "response", None), "status_code", None)
            self.last_error = str(e)
            self.log.warning("HRRR API error: %s", e)
            return None


class NAMFetcher:
    """Fetches NAM 3km deterministic forecast (nam_conus model).

    NAM (North American Mesoscale) provides 3km resolution forecasts up to 60 hours.
    Updates 4x/day (00, 06, 12, 18 UTC). Unlike HRRR, NAM provides native daily
    aggregation (temperature_2m_max), so no hourly->daily conversion needed.
    """

    def __init__(self, logger=None, rate_limiter=None):
        self.log = logger or _log
        self._rate_limiter = rate_limiter
        self.last_status_code = None
        self.last_error = None

    def fetch_nam(self, lat, lon):
        """Fetch NAM daily max temperatures.

        Args:
            lat: latitude
            lon: longitude

        Returns:
            dict of {date_str: max_temp_f} for next ~2.5 days (60h horizon),
            or None on API failure.
        """
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return None
        self.last_status_code = None
        self.last_error = None

        api_key = os.environ.get("OPEN_METEO_API_KEY", "")
        base = "https://customer-api.open-meteo.com/v1/forecast" if api_key else "https://api.open-meteo.com/v1/forecast"
        params = (
            f"latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
            f"&timezone=auto&forecast_days=3"
            f"&models=nam_conus"
        )
        url = f"{base}?{params}" + (f"&apikey={api_key}" if api_key else "")

        try:
            if self._rate_limiter is not None:
                self._rate_limiter()
            resp = _retry_request("GET", url, timeout=15, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.last_status_code = getattr(resp, "status_code", None)
                self.last_error = f"status={self.last_status_code}"
                self.log.warning(
                    "NAM API returned status %s",
                    getattr(resp, "status_code", "None"),
                )
                return None

            data = resp.json()
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            temps = daily.get("temperature_2m_max", [])

            if not dates or not temps:
                self.log.warning("NAM API returned no daily data")
                return None

            result = {d: t for d, t in zip(dates, temps) if t is not None}
            self.log.debug("NAM: %d dates (max temps: %s)", len(result),
                          {d: f"{t:.1f}F" for d, t in result.items()})
            return result

        except Exception as e:
            self.last_status_code = getattr(getattr(e, "response", None), "status_code", None)
            self.last_error = str(e)
            self.log.warning("NAM API error: %s", e)
            return None


class PreviousRunsFetcher:
    """Compares current vs previous model run forecasts for convergence analysis.

    When successive model runs converge on the same temperature, confidence is
    higher. When they diverge, the atmosphere is in a chaotic regime and
    forecast skill is lower.

    Uses Open-Meteo Previous Runs API: the response includes both the current
    forecast and the previous day's forecast for the same target dates.
    """

    def __init__(self, logger=None, rate_limiter=None):
        self.log = logger or _log
        self._rate_limiter = rate_limiter

    def fetch_convergence_batch(self, cities_dict, model="gfs_seamless"):
        """Batch fetch current + previous-day forecasts for all cities.

        Args:
            cities_dict: dict of {city_code: {"lat": float, "lon": float, ...}}
            model: Open-Meteo model identifier

        Returns:
            dict of {city_code: {date_str: {"current": temp_f, "previous": temp_f, "delta": float}}}
            Empty dict on failure.
        """
        if _retry_request is None:
            self.log.warning("retry_request not available")
            return {}

        api_key = os.environ.get("OPEN_METEO_API_KEY", "")
        base = ("https://customer-previous-runs-api.open-meteo.com/v1/forecast"
                if api_key else "https://previous-runs-api.open-meteo.com/v1/forecast")

        codes = list(cities_dict.keys())
        lats = ",".join(str(cities_dict[c]["lat"]) for c in codes)
        lons = ",".join(str(cities_dict[c]["lon"]) for c in codes)

        params = (
            f"latitude={lats}&longitude={lons}"
            f"&daily=temperature_2m_max,temperature_2m_max_previous_day1"
            f"&temperature_unit=fahrenheit"
            f"&timezone=auto"
            f"&forecast_days=7"
            f"&models={model}"
        )
        url = f"{base}?{params}" + (f"&apikey={api_key}" if api_key else "")

        try:
            if self._rate_limiter is not None:
                self._rate_limiter()
            resp = _retry_request("GET", url, timeout=20, max_retries=2)
            if resp is None or resp.status_code != 200:
                self.log.warning("Previous Runs API returned status %s",
                                getattr(resp, "status_code", "None"))
                return {}

            data = resp.json()

            # Handle multi-location (list) vs single-location (dict) response
            results = {}
            if isinstance(data, list):
                for i, city_data in enumerate(data):
                    if i >= len(codes):
                        break
                    parsed = self._parse_convergence(city_data)
                    if parsed:
                        results[codes[i]] = parsed
            else:
                parsed = self._parse_convergence(data)
                if parsed:
                    results[codes[0]] = parsed

            self.log.debug("Previous runs: convergence data for %d cities", len(results))
            return results

        except Exception as e:
            self.log.warning("Previous Runs API error: %s", e)
            return {}

    def _parse_convergence(self, city_data):
        """Parse a single city's previous runs response.

        Returns:
            dict of {date_str: {"current": float, "previous": float|None, "delta": float|None}}
        """
        daily = city_data.get("daily", {})
        dates = daily.get("time", [])
        current = daily.get("temperature_2m_max", [])
        previous = daily.get("temperature_2m_max_previous_day1", [])

        if not dates or not current:
            return None

        result = {}
        for i, date_str in enumerate(dates):
            cur = current[i] if i < len(current) else None
            prev = previous[i] if i < len(previous) else None
            if cur is not None:
                entry = {"current": cur, "previous": prev, "delta": None}
                if prev is not None:
                    entry["delta"] = round(cur - prev, 2)
                result[date_str] = entry

        return result if result else None

    @staticmethod
    def convergence_multiplier(delta_f):
        """Kelly sizing multiplier based on forecast run-to-run stability.

        Args:
            delta_f: temperature change between current and previous model run (F).

        Returns:
            float multiplier:
            - 1.2: stable forecast (|delta| <= 1F), increase confidence
            - 0.6: unstable forecast (|delta| >= 3F), reduce exposure
            - Linear interpolation for 1F < |delta| < 3F
        """
        d = abs(delta_f)
        if d <= 1.0:
            return 1.2
        elif d >= 3.0:
            return 0.6
        else:
            return 1.2 - (d - 1.0) * 0.3


class BiasCorrector:
    """Corrects systematic forecast bias using historical calibration data.

    Loads per-city, per-model bias from config/historical-calibration.json
    and applies correction: corrected_temp = raw_temp - bias.

    Fallback chain for direct model correction: per-city per-model > global model > raw temp.
    City-average bias is reserved for mixed-member ensembles where model attribution
    is unavailable.
    """

    EXEMPT_MODELS = {"hrrr", "nam", "nws"}

    def __init__(self, calibration_path=None, logger=None):
        self.log = logger or _log
        self._data = {}
        self._load(calibration_path)

    def _load(self, path=None):
        config_dir = Path(__file__).resolve().parent.parent.parent / "config"
        if path is not None:
            candidates = [Path(path)]
        else:
            candidates = [
                config_dir / "historical-calibration.json",
                config_dir / "calibration.json",
            ]

        for candidate in candidates:
            try:
                if not candidate.exists():
                    continue
                raw = json.loads(candidate.read_text())
                data = self._extract_bias_data(raw)
                if not data:
                    continue
                self._data = data
                n = self._data.get("n_forecasts", 0)
                self.log.info("BiasCorrector loaded from %s: %d forecast pairs", candidate.name, n)
                return
            except Exception as e:
                self.log.warning("BiasCorrector: failed to load %s: %s", candidate, e)

    def correct(self, city, model, temp):
        """Apply per-city, per-model bias correction.

        Returns corrected_temp = raw_temp - bias.
        Falls back to city average bias if model not calibrated.
        Returns raw temp if no calibration data available.
        """
        if temp is None:
            return None
        bias = self._get_model_bias(city, model)
        if bias is None:
            return temp
        return temp - bias

    def correct_forecast_dict(self, city, forecasts):
        """Correct all model temps in a {model: temp} dict.

        Returns new dict with corrected temperatures.
        """
        return {
            model: self.correct(city, model, temp)
            for model, temp in forecasts.items()
        }

    def city_average_bias(self, city):
        """Average bias across all calibrated models for a city.

        Used for ensemble members where individual model attribution
        is not possible (GEFS + ECMWF EPS mixed members).
        """
        per_city = self._data.get("per_city", {}).get(city, {})
        if not per_city:
            return self._global_average_bias()
        biases = [m["bias"] for m in per_city.values() if "bias" in m]
        return sum(biases) / len(biases) if biases else 0.0

    def blend_live_bias(self, city, live_bias=None, live_n=0, ramp_n=20):
        """Blend historical city bias with live verification bias."""
        hist_bias = self.city_average_bias(city)
        if live_bias is None or live_n <= 0:
            return hist_bias, hist_bias, 0.0
        alpha = min(1.0, float(live_n) / max(1.0, float(ramp_n)))
        return alpha * live_bias + (1.0 - alpha) * hist_bias, hist_bias, alpha

    def residual_std(self, city, model=None):
        """Compute residual std: sqrt(RMSE^2 - bias^2).

        This is the forecast uncertainty AFTER bias removal.
        If model is None, returns average across models for the city.
        """
        per_city = self._data.get("per_city", {}).get(city, {})
        if model and model in per_city:
            return self._compute_residual(per_city[model])

        residuals = []
        for m_stats in per_city.values():
            r = self._compute_residual(m_stats)
            if r is not None:
                residuals.append(r)
        if residuals:
            return sum(residuals) / len(residuals)

        return self._global_residual_std()

    def _extract_bias_data(self, raw):
        if not isinstance(raw, dict):
            return {}
        if "per_city" in raw and "global" in raw:
            return raw
        weather = raw.get("weather", {})
        bias_data = weather.get("bias_correction", {})
        if isinstance(bias_data, dict) and "per_city" in bias_data and "global" in bias_data:
            return bias_data
        return {}

    def _get_model_bias(self, city, model):
        model = canonical_model_name(model)
        if model in self.EXEMPT_MODELS:
            return 0.0
        per_city = self._data.get("per_city", {}).get(city, {})
        if model in per_city:
            return per_city[model].get("bias", 0.0)
        global_stats = self._data.get("global", {}).get(model, {})
        if global_stats:
            return global_stats.get("bias", 0.0)
        return None

    def _global_average_bias(self):
        global_stats = self._data.get("global", {})
        biases = [m["bias"] for m in global_stats.values() if "bias" in m]
        return sum(biases) / len(biases) if biases else 0.0

    def _compute_residual(self, stats):
        if not stats or "rmse" not in stats or "bias" not in stats:
            return None
        sq = stats["rmse"] ** 2 - stats["bias"] ** 2
        return max(0.5, math.sqrt(max(0, sq)))

    def _global_residual_std(self):
        global_stats = self._data.get("global", {})
        residuals = [self._compute_residual(s) for s in global_stats.values()]
        residuals = [r for r in residuals if r is not None]
        return sum(residuals) / len(residuals) if residuals else 4.7


class OrderBookDepth:
    """Fetches and analyzes Kalshi order book depth for improved limit pricing.

    Provides depth analysis to:
    1. Skip trades into thin books (insufficient liquidity)
    2. Estimate realistic fill prices considering book depth
    """

    def __init__(self, logger=None):
        self.log = logger or _log

    def fetch_depth(self, client, ticker):
        """Fetch order book depth for a given ticker.

        Args:
            client: KalshiClient instance
            ticker: market ticker string

        Returns:
            dict with keys: yes_bids, yes_asks, total_bid_depth, total_ask_depth
            or None on failure.

            yes_bids: list of (price, qty) sorted descending by price
            yes_asks: list of (price, qty) sorted ascending by price
            (NO bids are converted to YES asks at 100-price)
        """
        try:
            data = client.get(f"/markets/{ticker}/orderbook")
            orderbook = data.get("orderbook", {})
            yes_entries = orderbook.get("yes", [])
            no_entries = orderbook.get("no", [])

            # YES bids sorted descending by price
            yes_bids = sorted(
                [(price, qty) for price, qty in yes_entries],
                key=lambda x: x[0], reverse=True,
            )

            # NO bids become YES asks at 100-price, sorted ascending
            yes_asks = sorted(
                [(100 - price, qty) for price, qty in no_entries],
                key=lambda x: x[0],
            )

            total_bid = sum(qty for _, qty in yes_bids)
            total_ask = sum(qty for _, qty in yes_asks)

            return {
                "yes_bids": yes_bids,
                "yes_asks": yes_asks,
                "total_bid_depth": total_bid,
                "total_ask_depth": total_ask,
            }
        except Exception as e:
            self.log.warning("Orderbook fetch failed for %s: %s", ticker, e)
            return None

    def estimate_fill_price(self, depth, side, quantity):
        """Estimate volume-weighted average fill price for a given quantity.

        Args:
            depth: dict from fetch_depth()
            side: "yes" (buy, walk asks) or "no" (sell, walk bids)
            quantity: number of contracts

        Returns:
            Estimated average fill price in cents, or None if insufficient liquidity.
        """
        if side == "yes":
            book = depth.get("yes_asks", [])
        else:
            book = depth.get("yes_bids", [])

        if not book:
            return None

        total_filled = 0
        total_cost = 0

        for price, qty in book:
            fill = min(qty, quantity - total_filled)
            total_cost += price * fill
            total_filled += fill
            if total_filled >= quantity:
                break

        if total_filled < quantity:
            return None

        return total_cost / total_filled


# === Model Run Timing ===

MODEL_RUN_SCHEDULE = {
    "gfs": {"hours_utc": [0, 6, 12, 18], "delay_minutes": 210},      # ~3.5h processing
    "ecmwf": {"hours_utc": [0, 12], "delay_minutes": 360},            # ~6h processing
    "hrrr": {"hours_utc": list(range(24)), "delay_minutes": 45},      # hourly, ~45min delay
    "nbm": {"hours_utc": list(range(24)), "delay_minutes": 90},       # hourly, ~90min delay
    "nam": {"hours_utc": [0, 6, 12, 18], "delay_minutes": 120},      # 4x/day, ~2h processing
}


def latest_available_model_run(model_name, now_utc=None):
    """Return the most recent run initialization time available right now."""
    model_key = canonical_model_name(model_name)
    schedule = MODEL_RUN_SCHEDULE.get(model_key)
    if schedule is None:
        return None

    now = now_utc or datetime.datetime.utcnow()
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)

    latest = None
    delay = datetime.timedelta(minutes=schedule["delay_minutes"])
    for day_offset in (-1, 0):
        run_date = now.date() + datetime.timedelta(days=day_offset)
        for run_hour in schedule["hours_utc"]:
            run_start = datetime.datetime.combine(run_date, datetime.time(hour=run_hour))
            if run_start + delay <= now and (latest is None or run_start > latest):
                latest = run_start
    return latest


def next_model_run(now_utc=None):
    """Find the next model run that will become available in the future.

    Only reports future runs (minutes > 0). Already-available runs are skipped
    so callers only trigger pre-scans when fresh data is truly imminent.

    Args:
        now_utc: datetime.datetime in UTC. If None, uses current UTC time.

    Returns:
        Tuple of (model_name, minutes_until_available) where minutes > 0.
    """
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    best_model = None
    best_minutes = float("inf")
    now_minutes = now_utc.hour * 60 + now_utc.minute

    for model_name, schedule in MODEL_RUN_SCHEDULE.items():
        delay = schedule["delay_minutes"]
        for run_hour in schedule["hours_utc"]:
            available_at = run_hour * 60 + delay
            minutes_until = available_at - now_minutes
            if minutes_until <= 0:
                continue  # already available, skip
            if minutes_until < best_minutes:
                best_minutes = minutes_until
                best_model = model_name

    # Wrap-around: if all today's runs are past, find earliest tomorrow
    if best_model is None:
        for model_name, schedule in MODEL_RUN_SCHEDULE.items():
            delay = schedule["delay_minutes"]
            first_hour = schedule["hours_utc"][0]
            available_at = first_hour * 60 + delay + 1440
            minutes_until = available_at - now_minutes
            if minutes_until < best_minutes:
                best_minutes = minutes_until
                best_model = model_name

    return (best_model, best_minutes)


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
