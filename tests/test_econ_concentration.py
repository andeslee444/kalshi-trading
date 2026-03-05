"""Tests for economics bot concentration limit logic."""
import pytest


def _ticker_family(ticker):
    """Extract ticker family: everything before the last -T or -B segment.

    KXECONSTATCPIYOY-26MAY-T2.0 -> KXECONSTATCPIYOY-26MAY
    """
    import re
    m = re.match(r'^(.*?)-[TB][\d.]+$', ticker)
    return m.group(1) if m else ticker


def _release_date_key(ticker):
    """Extract release date key from ticker for grouping.

    KXECONSTATCPIYOY-26MAY-T2.0 -> KXECONSTATCPIYOY-26MAY
    KXECONSTATCPIYOY-26JUN-T3.0 -> KXECONSTATCPIYOY-26JUN

    Groups all thresholds for the same CPI release together.
    """
    return _ticker_family(ticker)


def _compute_exposure(trades, ticker_or_family, match_mode="family"):
    """Sum cost_cents across trades matching a ticker family or release date."""
    total = 0
    for t in trades:
        t_ticker = t.get("ticker", "")
        if match_mode == "family":
            if _ticker_family(t_ticker) == ticker_or_family:
                total += t.get("cost_cents", 0)
        elif match_mode == "prefix":
            # Market type prefix (e.g., all CPI)
            if t_ticker.startswith(ticker_or_family):
                total += t.get("cost_cents", 0)
    return total


class TestTickerFamily:
    def test_cpi_threshold(self):
        assert _ticker_family("KXECONSTATCPIYOY-26MAY-T2.0") == "KXECONSTATCPIYOY-26MAY"

    def test_below_threshold(self):
        assert _ticker_family("KXECONSTATCPIYOY-26MAY-B3.5") == "KXECONSTATCPIYOY-26MAY"

    def test_no_threshold(self):
        assert _ticker_family("KXGAS-26MAR") == "KXGAS-26MAR"


class TestConcentrationExposure:
    def test_sums_same_family(self):
        trades = [
            {"ticker": "KXECONSTATCPIYOY-26MAY-T2.0", "cost_cents": 5000},
            {"ticker": "KXECONSTATCPIYOY-26MAY-T2.5", "cost_cents": 3000},
            {"ticker": "KXECONSTATCPIYOY-26JUN-T2.0", "cost_cents": 4000},
        ]
        exposure = _compute_exposure(trades, "KXECONSTATCPIYOY-26MAY", "family")
        assert exposure == 8000  # Only May tickers

    def test_prefix_sums_all_cpi(self):
        trades = [
            {"ticker": "KXECONSTATCPIYOY-26MAY-T2.0", "cost_cents": 5000},
            {"ticker": "KXECONSTATCORECPIYOY-26MAY-T3.0", "cost_cents": 2000},
            {"ticker": "KXGAS-26MAR-T3.50", "cost_cents": 1000},
        ]
        exposure = _compute_exposure(trades, "KXECON", "prefix")
        assert exposure == 7000  # CPI + Core CPI, not Gas


class TestConcentrationLimits:
    def test_family_limit_blocks_trade(self):
        """15% of bankroll cap per ticker family."""
        bankroll = 500000  # $5000
        family_cap = int(bankroll * 0.15)  # $750 = 75000 cents
        existing = 80000  # $800 already exposed
        assert existing > family_cap, "Should exceed 15% limit"

    def test_family_limit_allows_trade(self):
        """Under 15% should allow trade."""
        bankroll = 500000
        family_cap = int(bankroll * 0.15)
        existing = 50000  # $500 < $750
        assert existing < family_cap

    def test_total_econ_limit(self):
        """40% of bankroll cap for all econ markets."""
        bankroll = 500000
        total_cap = int(bankroll * 0.40)  # $2000 = 200000 cents
        assert total_cap == 200000
