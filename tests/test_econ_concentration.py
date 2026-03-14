"""Tests for economics bot concentration limit logic."""
import pytest
import json
import types
import sys
import math
from unittest.mock import MagicMock
from pathlib import Path
from conftest import make_fake_auth, load_bot_module


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


# ---------------------------------------------------------------------------
# Import economics-bot to test production constants directly
# ---------------------------------------------------------------------------

def _fake_atomic_write(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))

_fake_auth = make_fake_auth(
    retry_request=lambda *a, **kw: MagicMock(),
    _atomic_write_json=_fake_atomic_write,
)

_fake_prob = types.ModuleType("probability")
_fake_prob.econ_nowcast_probability = lambda *a, **kw: 0.5
_fake_prob.cpi_nowcast_sigma = lambda *a, **kw: 0.05
_fake_prob.gdp_nowcast_sigma = lambda *a, **kw: 0.10
_fake_prob.quarter_kelly = lambda *a, **kw: (0, 0)
_fake_prob.uncertainty_kelly = lambda *a, **kw: (0, 0, {})
_fake_prob.compute_limit_price = lambda *a, **kw: 50
_fake_prob.kalshi_fee_cents = lambda *a, **kw: 1.0
_fake_prob.gas_price_probability = lambda *a, **kw: 0.5
_fake_prob.is_market_liquid = lambda *a, **kw: True
_fake_prob._norm_cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))

_fake_alloc = types.ModuleType("capital_allocator")
_fake_alloc.PortfolioAllocator = lambda *a, **kw: MagicMock()

_fake_belief = types.ModuleType("cpi_belief_filter")
_fake_belief.CPIBeliefFilter = type("CPIBeliefFilter", (), {
    "__init__": lambda self, *a, **kw: None,
    "update": lambda self, *a, **kw: None,
    "posterior": property(lambda self: (2.8, 0.10)),
})

_fake_scenario = types.ModuleType("scenario_engine")
_fake_scenario.compute_scenario_weights = lambda *a, **kw: {}
_fake_scenario.scenario_probability = lambda *a, **kw: MagicMock(
    probability=0.5, agreement=0.8, per_scenario={}, weights_used={})

_fake_macro = types.ModuleType("macro_engine")
_fake_macro.MacroEngine = None

_econ = load_bot_module("economics-bot.py", _fake_auth, extra_stubs={
    "probability": _fake_prob,
    "capital_allocator": _fake_alloc,
    "cpi_belief_filter": _fake_belief,
    "scenario_engine": _fake_scenario,
    "macro_engine": _fake_macro,
})


class TestProductionConcentrationConstants:
    """Verify production constants match PM-corrected values (15%/40%)."""

    def test_family_exposure_pct_is_fifteen(self):
        """FAMILY_EXPOSURE_PCT should be 0.15 (15% per PM correction)."""
        assert _econ.FAMILY_EXPOSURE_PCT == 0.15, \
            f"FAMILY_EXPOSURE_PCT should be 0.15 (15%), got {_econ.FAMILY_EXPOSURE_PCT}"

    def test_total_econ_pct_is_forty(self):
        """TOTAL_ECON_PCT should be 0.40 (40% per PM correction)."""
        assert _econ.TOTAL_ECON_PCT == 0.40, \
            f"TOTAL_ECON_PCT should be 0.40 (40%), got {_econ.TOTAL_ECON_PCT}"

    def test_check_concentration_blocks_over_family_limit(self):
        """_check_concentration should block when family exposure >= 15%."""
        bankroll = 100000  # $1000
        # 15% of $1000 = $150 = 15000 cents
        trades = [
            {"ticker": "KXECON-26MAY-T2.0", "cost_cents": 16000},  # $160 > $150
        ]
        allowed, reason = _econ._check_concentration("KXECON-26MAY-T3.0", bankroll, trades)
        assert not allowed, "Should block: $160 >= 15% of $1000"
        assert "family_cap" in reason

    def test_check_concentration_allows_under_family_limit(self):
        """_check_concentration should allow when family exposure < 15%."""
        bankroll = 100000  # $1000
        trades = [
            {"ticker": "KXECON-26MAY-T2.0", "cost_cents": 10000},  # $100 < $150
        ]
        allowed, _ = _econ._check_concentration("KXECON-26MAY-T3.0", bankroll, trades)
        assert allowed, "Should allow: $100 < 15% of $1000"

    def test_check_concentration_blocks_over_total_limit(self):
        """_check_concentration should block when total econ exposure >= 40%."""
        bankroll = 100000  # $1000
        # 40% of $1000 = $400 = 40000 cents
        trades = [
            {"ticker": "KXECON-26MAY-T2.0", "cost_cents": 10000},
            {"ticker": "KXECON-26JUN-T2.0", "cost_cents": 10000},
            {"ticker": "KXCPI-26MAY-T3.0", "cost_cents": 10000},
            {"ticker": "KXGDP-26Q1-T2.0", "cost_cents": 12000},
        ]
        # Total = 42000 > 40000
        allowed, reason = _econ._check_concentration("KXECON-26JUL-T2.0", bankroll, trades)
        assert not allowed, "Should block: $420 >= 40% of $1000"
        assert "total_econ_cap" in reason

    def test_max_contracts_per_order(self):
        """MAX_CONTRACTS_PER_ORDER should be 200 (penny contract cap)."""
        assert _econ.MAX_CONTRACTS_PER_ORDER == 200


class TestLiveExposureRecords:
    """Economics concentration should prefer live positions over stale trade logs."""

    def test_compute_exposure_accepts_market_exposure_records(self):
        records = [
            {"ticker": "KXECONSTATCPIYOY-26MAY-T2.0", "market_exposure": 12000},
            {"ticker": "KXECONSTATCPIYOY-26MAY-T2.5", "market_exposure": 5400},
        ]
        exposure = _econ._compute_exposure(records, "KXECONSTATCPIYOY-26MAY", "family")
        assert exposure == 17400

    def test_load_active_econ_exposure_prefers_live_positions(self):
        _econ.client.get.return_value = {
            "market_positions": [
                {"ticker": "KXECONSTATCPIYOY-26MAY-T2.0", "position": 10, "market_exposure": 12000},
                {"ticker": "KXBTCY-27JAN0100-T149999.99", "position": 2, "market_exposure": 9999},
                {"ticker": "KXGDP-26APR30-T4.5", "position": 0, "market_exposure": 5000},
            ]
        }

        records = _econ._load_active_econ_exposure_records()
        assert records == [{"ticker": "KXECONSTATCPIYOY-26MAY-T2.0", "cost_cents": 12000}]

    def test_load_active_econ_exposure_falls_back_to_open_trade_log(self):
        _econ.client.get.side_effect = RuntimeError("api unavailable")
        _econ.load_trades.return_value = [
            {
                "ticker": "KXECONSTATCPIYOY-26MAY-T2.0",
                "action": "buy",
                "status": "executed",
                "settlement_result": None,
                "cost_cents": 12000,
            },
            {
                "ticker": "KXECONSTATCPIYOY-26MAY-T2.5",
                "action": "sell",
                "status": "executed",
                "settlement_result": None,
                "cost_cents": 2000,
            },
            {
                "ticker": "KXECONSTATCPIYOY-26APR-T2.0",
                "action": "buy",
                "status": "canceled",
                "settlement_result": None,
                "cost_cents": 8000,
            },
        ]

        records = _econ._load_active_econ_exposure_records()
        assert records == [{"ticker": "KXECONSTATCPIYOY-26MAY-T2.0", "cost_cents": 12000}]
