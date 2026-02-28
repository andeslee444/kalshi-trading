"""Tests for shared ticker parsing utilities."""

import pytest
from ticker_utils import parse_weather_ticker, parse_crypto_ticker


# ===================================================================
# Weather Ticker Parser
# ===================================================================

class TestParseWeatherTicker:

    # --- Current YYMONDD format (2026-02-22 onwards) ---

    def test_current_format_threshold(self):
        r = parse_weather_ticker("KXHIGHMIA-26FEB28-T86")
        assert r is not None
        assert r["city"] == "MIA"
        assert r["date"] == "2026-02-28"
        assert r["direction"] == "T"
        assert r["threshold"] == 86.0

    def test_current_format_bracket(self):
        r = parse_weather_ticker("KXHIGHAUS-26MAR01-B87.5")
        assert r is not None
        assert r["city"] == "AUS"
        assert r["date"] == "2026-03-01"
        assert r["direction"] == "B"
        assert r["threshold"] == 87.5

    def test_current_format_different_cities(self):
        for ticker, city, date in [
            ("KXHIGHCHI-26MAR01-T36", "CHI", "2026-03-01"),
            ("KXHIGHDEN-26FEB28-T64", "DEN", "2026-02-28"),
            ("KXHIGHLAX-26MAR01-T78", "LAX", "2026-03-01"),
            ("KXHIGHNY-26FEB28-T50", "NY", "2026-02-28"),
            ("KXHIGHPHIL-26FEB28-B55.5", "PHIL", "2026-02-28"),
        ]:
            r = parse_weather_ticker(ticker)
            assert r is not None, f"Failed to parse: {ticker}"
            assert r["city"] == city
            assert r["date"] == date

    # --- T-prefix cities (KXHIGHT<CITY>) ---

    def test_t_prefix_city_atl(self):
        r = parse_weather_ticker("KXHIGHTATL-26MAR01-T72")
        assert r is not None
        assert r["city"] == "ATL"
        assert r["date"] == "2026-03-01"

    def test_t_prefix_city_bos(self):
        r = parse_weather_ticker("KXHIGHTBOS-26FEB28-B49.5")
        assert r is not None
        assert r["city"] == "BOS"
        assert r["date"] == "2026-02-28"
        assert r["direction"] == "B"

    def test_t_prefix_various_cities(self):
        for ticker, city in [
            ("KXHIGHTDAL-26MAR01-T80", "DAL"),
            ("KXHIGHTDC-26FEB28-T59", "DC"),
            ("KXHIGHTHOU-26FEB28-T73", "HOU"),
            ("KXHIGHTLV-26MAR01-B65.5", "LV"),
            ("KXHIGHTMIN-26FEB28-T30", "MIN"),
            ("KXHIGHTNOLA-26MAR01-T72", "NOLA"),
            ("KXHIGHTOKC-26FEB28-T60", "OKC"),
            ("KXHIGHTPHX-26MAR01-T90", "PHX"),
            ("KXHIGHTSATX-26FEB28-B75.5", "SATX"),
            ("KXHIGHTSEA-26FEB28-T50", "SEA"),
            ("KXHIGHTSFO-26MAR01-T65", "SFO"),
        ]:
            r = parse_weather_ticker(ticker)
            assert r is not None, f"Failed to parse: {ticker}"
            assert r["city"] == city

    # --- Old DDMONYY format (backward compatibility for trade log parsing) ---

    def test_old_format_standard(self):
        r = parse_weather_ticker("KXHIGHMIA-21FEB26-T86")
        assert r is not None
        assert r["city"] == "MIA"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "T"
        assert r["threshold"] == 86.0

    def test_old_format_bracket(self):
        r = parse_weather_ticker("KXHIGHMIA-21FEB26-B85.5")
        assert r is not None
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "B"
        assert r["threshold"] == 85.5

    def test_old_format_different_city(self):
        r = parse_weather_ticker("KXHIGHLAX-05MAR26-T72")
        assert r is not None
        assert r["city"] == "LAX"
        assert r["date"] == "2026-03-05"
        assert r["threshold"] == 72.0

    def test_old_format_chicago(self):
        r = parse_weather_ticker("KXHIGHCHI-15JAN26-T30")
        assert r is not None
        assert r["city"] == "CHI"
        assert r["date"] == "2026-01-15"

    # --- YYMONDD format heuristic ---

    def test_yymondd_year26(self):
        """26FEB16: g2=26 >= 25 -> YYMONDD -> year=26, day=16 -> 2026-02-16."""
        r = parse_weather_ticker("KXHIGHMIA-26FEB16-T86")
        assert r is not None
        assert r["date"] == "2026-02-16"

    def test_yymondd_year27(self):
        r = parse_weather_ticker("KXHIGHMIA-27JAN15-T80")
        assert r is not None
        assert r["date"] == "2027-01-15"

    # --- Invalid tickers ---

    def test_invalid_ticker_returns_none(self):
        assert parse_weather_ticker("KXBTC-21FEB26-T70000") is None

    def test_empty_string_returns_none(self):
        assert parse_weather_ticker("") is None

    def test_invalid_month_returns_none(self):
        assert parse_weather_ticker("KXHIGHMIA-21XYZ26-T86") is None

    def test_inflation_ticker_returns_none(self):
        assert parse_weather_ticker("KXHIGHINFLATION-26DEC-T3.5") is None


# ===================================================================
# Crypto Ticker Parser
# ===================================================================

class TestParseCryptoTicker:

    def test_btc_threshold(self):
        r = parse_crypto_ticker("KXBTC-21FEB26-T70000")
        assert r is not None
        assert r["asset"] == "BTC"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "T"
        assert r["threshold"] == 70000.0

    def test_eth_bracket(self):
        r = parse_crypto_ticker("KXETH-21FEB26-B3500")
        assert r is not None
        assert r["asset"] == "ETH"
        assert r["date"] == "2026-02-21"
        assert r["direction"] == "B"
        assert r["threshold"] == 3500.0

    def test_sol(self):
        r = parse_crypto_ticker("KXSOL-10MAR26-T200")
        assert r is not None
        assert r["asset"] == "SOL"
        assert r["threshold"] == 200.0

    def test_simple_format_fallback(self):
        r = parse_crypto_ticker("KXBTC-T95000")
        assert r is not None
        assert r["asset"] == "BTC"
        assert r["direction"] == "T"
        assert r["threshold"] == 95000.0
        assert "date" not in r

    def test_invalid_ticker_returns_none(self):
        assert parse_crypto_ticker("KXHIGHMIA-21FEB26-T86") is None

    def test_empty_string_returns_none(self):
        assert parse_crypto_ticker("") is None

    def test_invalid_month_returns_none(self):
        assert parse_crypto_ticker("KXBTC-21XYZ26-T70000") is None

    def test_crypto_prefix_variant(self):
        r = parse_crypto_ticker("KXCRYPTO-21FEB26-T50000")
        assert r is not None
        assert r["asset"] == "CRYPTO"

    # --- Hourly settlement format (YYMONDDHR) ---

    def test_btc_hourly_format(self):
        r = parse_crypto_ticker("KXBTC-26MAR0117-T72999.99")
        assert r is not None
        assert r["asset"] == "BTC"
        assert r["date"] == "2026-03-01"
        assert r["direction"] == "T"
        assert r["threshold"] == 72999.99
        assert r["settlement_hour"] == 17

    def test_eth_hourly_format(self):
        r = parse_crypto_ticker("KXETH-26MAR0117-B3099.99")
        assert r is not None
        assert r["asset"] == "ETH"
        assert r["date"] == "2026-03-01"
        assert r["direction"] == "B"
        assert r["threshold"] == 3099.99
        assert r["settlement_hour"] == 17

    def test_sol_hourly_format(self):
        r = parse_crypto_ticker("KXSOLD-26MAR0117-T99.9999")
        assert r is not None
        assert r["asset"] == "SOL"
        assert r["date"] == "2026-03-01"
        assert r["settlement_hour"] == 17

    def test_hourly_no_settlement_hour_in_daily(self):
        r = parse_crypto_ticker("KXBTC-26FEB28-T70000")
        assert r is not None
        assert r["date"] == "2026-02-28"
        assert "settlement_hour" not in r

    # --- DOGE and XRP ---

    def test_doge_hourly(self):
        r = parse_crypto_ticker("KXDOGE-26MAR0117-T0.1749999")
        assert r is not None
        assert r["asset"] == "DOGE"
        assert r["date"] == "2026-03-01"
        assert r["direction"] == "T"
        assert r["threshold"] == 0.1749999

    def test_xrp_hourly(self):
        r = parse_crypto_ticker("KXXRP-26MAR0101-T1.9399")
        assert r is not None
        assert r["asset"] == "XRP"
        assert r["date"] == "2026-03-01"
        assert r["direction"] == "T"
        assert r["threshold"] == 1.9399
        assert r["settlement_hour"] == 1

    def test_doge_bracket(self):
        r = parse_crypto_ticker("KXDOGE-26MAR0117-B0.172")
        assert r is not None
        assert r["asset"] == "DOGE"
        assert r["direction"] == "B"

    def test_xrp_bracket(self):
        r = parse_crypto_ticker("KXXRP-26MAR0101-B1.9299500")
        assert r is not None
        assert r["asset"] == "XRP"
        assert r["direction"] == "B"

    def test_doge_simple_fallback(self):
        r = parse_crypto_ticker("KXDOGE-T0.20")
        assert r is not None
        assert r["asset"] == "DOGE"
        assert r["threshold"] == 0.20
        assert "date" not in r

    def test_xrp_simple_fallback(self):
        r = parse_crypto_ticker("KXXRP-B2.50")
        assert r is not None
        assert r["asset"] == "XRP"
        assert "date" not in r

    # --- Asset suffixes (D=daily, E=expiry) ---

    def test_btcd_suffix(self):
        r = parse_crypto_ticker("KXBTCD-26MAR0706-T9")
        assert r is not None
        assert r["asset"] == "BTC"
        assert r["date"] == "2026-03-07"
        assert r["settlement_hour"] == 6

    def test_btce_suffix(self):
        r = parse_crypto_ticker("KXBTCE-26FEB2610-T10")
        assert r is not None
        assert r["asset"] == "BTC"
        assert r["date"] == "2026-02-26"
        assert r["settlement_hour"] == 10

    def test_doged_suffix(self):
        r = parse_crypto_ticker("KXDOGED-26MAR0117-T0.1749999")
        assert r is not None
        assert r["asset"] == "DOGE"

    # --- Exotic formats should return None ---

    def test_btc15m_returns_none(self):
        assert parse_crypto_ticker("KXBTC15M-26FEB281800-00") is None

    def test_btcmax100_returns_none(self):
        assert parse_crypto_ticker("KXBTCMAX100-26-SEP") is None

    def test_btcminmon_returns_none(self):
        assert parse_crypto_ticker("KXBTCMINMON-BTC-26FEB28-275000") is None
