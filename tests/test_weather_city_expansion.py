"""Tests for weather city coverage verification."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock


# Known Kalshi city codes as of 2026-03 (from KXHIGH ticker prefixes)
KNOWN_KALSHI_CITIES = {
    "MIA", "LAX", "PHIL", "NY", "CHI", "AUS", "DEN", "HOU",
    "ATL", "BOS", "SFO", "SEA", "LV", "DAL", "MIN", "PHX",
    "DC", "NOLA", "OKC", "SATX",
}


class TestWeatherCityConfig:
    """Verify weather city configuration covers Kalshi markets."""

    def setup_method(self):
        config_path = Path(__file__).resolve().parent.parent / "config" / "kalshi-config.json"
        with open(config_path) as f:
            self.config = json.load(f)
        self.config_cities = set(self.config.get("cities", {}).keys())

    def test_all_known_cities_configured(self):
        """Every known Kalshi KXHIGH city must have coords in config."""
        missing = KNOWN_KALSHI_CITIES - self.config_cities
        assert missing == set(), f"Cities missing from config: {missing}"

    def test_all_cities_have_coordinates(self):
        """Every configured city must have lat/lon."""
        for city, coords in self.config.get("cities", {}).items():
            assert "lat" in coords, f"City {city} missing lat"
            assert "lon" in coords, f"City {city} missing lon"
            assert -90 <= coords["lat"] <= 90, f"City {city} lat out of range"
            assert -180 <= coords["lon"] <= 180, f"City {city} lon out of range"

    def test_no_duplicate_coordinates(self):
        """No two cities should share exact same coordinates."""
        seen = {}
        for city, coords in self.config.get("cities", {}).items():
            key = (coords["lat"], coords["lon"])
            assert key not in seen, f"City {city} duplicates coords of {seen[key]}"
            seen[key] = city

    def test_config_has_ensemble_settings(self):
        """Ensemble configuration must be present."""
        assert "ensemble" in self.config
        assert self.config["ensemble"].get("enabled") is False  # disabled on free Open-Meteo tier
        assert "models" in self.config["ensemble"]
        assert "weights" in self.config["ensemble"]
