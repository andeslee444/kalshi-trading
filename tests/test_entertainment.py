"""Tests for entertainment bot: HDD staleness, liquidity, pricing, and Kelly sizing."""

import datetime
import importlib.util
import sys
from unittest.mock import MagicMock

import pytest

# Import real Kelly functions for tiered sizing tests
from probability import half_kelly, quarter_kelly


# Stub kalshi_auth before importing the bot module
_mock_auth = MagicMock()
_mock_auth.KalshiClient = MagicMock
_mock_auth.setup_unbuffered = MagicMock()
_mock_auth.setup_signal_handlers = MagicMock()
_mock_auth.setup_logging = MagicMock(return_value=MagicMock())
_mock_auth.PROJECT_DIR = MagicMock()
_mock_auth.load_trades = MagicMock(return_value=[])
_mock_auth.save_trade = MagicMock()
_mock_auth.fetch_parallel = MagicMock(return_value={})
_mock_auth.retry_request = MagicMock()
_mock_auth.TradeManager = MagicMock()
_mock_auth.trim_trade_log = MagicMock()
_mock_auth.build_market_snapshot = MagicMock()
_mock_auth.HealthCheckMonitor = MagicMock()
_mock_auth.OrderMonitor = MagicMock()
_mock_auth.ScanSummary = MagicMock()


def _make_entry(artist, units, chart_date, source="hdd-hits-top-50"):
    """Helper to create album data entries."""
    return {
        "artist": artist,
        "units": units,
        "source": source,
        "chart_date": chart_date,
    }


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _hours_ago_iso(hours):
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return dt.isoformat()


# Import the staleness function directly — it's a pure function
# We need to load the module, but entertainment-bot does module-level init
# So we define our own version matching the logic
def _check_hdd_staleness(album_data, max_age_hours=48):
    """Mirror of entertainment-bot._check_hdd_staleness for testing."""
    now = datetime.datetime.now(datetime.timezone.utc)
    fresh = []
    stale_count = 0
    for entry in album_data:
        chart_date = entry.get("chart_date", "")
        if not chart_date:
            fresh.append(entry)
            continue
        try:
            dt = datetime.datetime.fromisoformat(chart_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            age_hours = (now - dt).total_seconds() / 3600
            if age_hours <= max_age_hours:
                fresh.append(entry)
            else:
                stale_count += 1
        except (ValueError, TypeError):
            fresh.append(entry)  # fail-open
    return fresh


class TestHddStaleness:

    def test_fresh_data_passes(self):
        """Data from 1 hour ago should pass through."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(1))]
        result = _check_hdd_staleness(data)
        assert len(result) == 1
        assert result[0]["artist"] == "Drake"

    def test_stale_data_rejected(self):
        """Data from 5 days ago should be removed."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(120))]
        result = _check_hdd_staleness(data)
        assert len(result) == 0

    def test_47h_passes(self):
        """Data at 47 hours (under 48h limit) should pass."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(47))]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_49h_fails(self):
        """Data at 49 hours (over 48h limit) should be removed."""
        data = [_make_entry("Drake", 200000, _hours_ago_iso(49))]
        result = _check_hdd_staleness(data)
        assert len(result) == 0

    def test_missing_chart_date_passes(self):
        """Entries without chart_date should pass (fail-open)."""
        data = [{"artist": "Drake", "units": 200000, "source": "hdd-hits-top-50"}]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_empty_chart_date_passes(self):
        """Empty string chart_date should pass (fail-open)."""
        data = [_make_entry("Drake", 200000, "")]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_malformed_date_passes(self):
        """Malformed date string should pass (fail-open)."""
        data = [_make_entry("Drake", 200000, "not-a-date")]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_mixed_fresh_stale_filtering(self):
        """Mix of fresh and stale entries should filter correctly."""
        data = [
            _make_entry("Drake", 200000, _hours_ago_iso(1)),     # fresh
            _make_entry("Taylor", 300000, _hours_ago_iso(72)),   # stale
            _make_entry("Kendrick", 150000, _hours_ago_iso(24)), # fresh
            _make_entry("Beyonce", 250000, _hours_ago_iso(96)),  # stale
        ]
        result = _check_hdd_staleness(data)
        assert len(result) == 2
        artists = [e["artist"] for e in result]
        assert "Drake" in artists
        assert "Kendrick" in artists
        assert "Taylor" not in artists
        assert "Beyonce" not in artists

    def test_z_suffix_date_passes(self):
        """ISO date with Z suffix should parse correctly."""
        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=5)
        data = [_make_entry("Drake", 200000, dt.strftime("%Y-%m-%dT%H:%M:%SZ"))]
        result = _check_hdd_staleness(data)
        assert len(result) == 1

    def test_empty_list(self):
        """Empty input should return empty output."""
        result = _check_hdd_staleness([])
        assert result == []


def _make_market(yes_bid=0, yes_ask=0, no_ask=0, volume=0, last_price=0):
    """Helper to create a market dict for liquidity tests."""
    return {
        "ticker": "KXALBUMSALES-TEST",
        "title": "Test Market",
        "subtitle": "",
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "volume": volume,
        "last_price": last_price,
    }


def _is_entertainment_liquid(market):
    """Mirror of the inline liquidity check in entertainment-bot.match_and_trade."""
    yes_ask = market.get("yes_ask", 0)
    no_ask = market.get("no_ask", 0)
    yes_bid = market.get("yes_bid", 0)

    has_yes_side = yes_ask and yes_ask < 99
    has_no_side = no_ask and no_ask < 99

    if not has_yes_side and not has_no_side:
        return False, "no_ask"

    if yes_bid and yes_ask and (yes_ask - yes_bid) > 40:
        return False, "wide_spread"

    return True, "ok"


def _entertainment_market_price(market):
    """Mirror of the price calculation in entertainment-bot.match_and_trade."""
    yes_bid = market.get("yes_bid", 0)
    yes_ask = market.get("yes_ask", 0)
    last = market.get("last_price", 0)

    if yes_bid and yes_ask:
        return (yes_bid + yes_ask) / 2 / 100
    elif yes_ask:
        return yes_ask / 100
    elif last:
        return last / 100
    else:
        return 0.5


class TestEntertainmentLiquidity:

    def test_ask_only_market_passes(self):
        """Market with yes_ask but no bid should pass (common in entertainment)."""
        m = _make_market(yes_ask=65, no_ask=35)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is True

    def test_ask_only_yes_side_passes(self):
        """Market with only yes_ask (no no_ask, no bid) should pass."""
        m = _make_market(yes_ask=50)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is True

    def test_ask_only_no_side_passes(self):
        """Market with only no_ask should pass."""
        m = _make_market(no_ask=40)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is True

    def test_no_ask_on_either_side_rejected(self):
        """Market with no ask on either side should be rejected."""
        m = _make_market(yes_bid=30)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is False
        assert reason == "no_ask"

    def test_empty_market_rejected(self):
        """Market with all zeros should be rejected."""
        m = _make_market()
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is False

    def test_ask_at_99_rejected(self):
        """Ask at 99c is effectively no market — reject."""
        m = _make_market(yes_ask=99)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is False

    def test_both_sides_with_narrow_spread_passes(self):
        """Market with bid+ask and narrow spread should pass."""
        m = _make_market(yes_bid=40, yes_ask=55, no_ask=45)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is True

    def test_wide_spread_rejected(self):
        """Market with bid+ask but spread > 40c should be rejected."""
        m = _make_market(yes_bid=10, yes_ask=60, no_ask=40)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is False
        assert reason == "wide_spread"

    def test_spread_exactly_40_passes(self):
        """Spread of exactly 40c should pass (> 40 is the threshold)."""
        m = _make_market(yes_bid=20, yes_ask=60, no_ask=40)
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is True

    def test_spread_check_skipped_when_no_bid(self):
        """Spread check should not apply when there's no bid (ask-only market)."""
        m = _make_market(yes_ask=95, no_ask=5)  # would be wide spread if bid existed
        liquid, reason = _is_entertainment_liquid(m)
        assert liquid is True


class TestEntertainmentMarketPrice:

    def test_midpoint_when_both_sides(self):
        """Use midpoint when both bid and ask exist."""
        m = _make_market(yes_bid=40, yes_ask=60)
        assert _entertainment_market_price(m) == pytest.approx(0.50)

    def test_ask_price_when_no_bid(self):
        """Use ask directly when no bid exists."""
        m = _make_market(yes_ask=65)
        assert _entertainment_market_price(m) == pytest.approx(0.65)

    def test_last_price_fallback(self):
        """Fall back to last_price when no ask."""
        m = _make_market(last_price=45)
        assert _entertainment_market_price(m) == pytest.approx(0.45)

    def test_default_when_nothing(self):
        """Default to 0.5 when no price info at all."""
        m = _make_market()
        assert _entertainment_market_price(m) == pytest.approx(0.5)


# === Tiered Kelly sizing (mirrors entertainment-bot._eighth_kelly and _select_kelly_sizer) ===

def _eighth_kelly(edge, price_cents, max_cost_cents, bankroll_cents=None,
                  fee_cents=0, return_details=False):
    """Mirror of entertainment-bot._eighth_kelly for testing."""
    result = half_kelly(edge, price_cents, max_cost_cents, bankroll_cents,
                        fee_cents=fee_cents, return_details=True)
    contracts, _risk, details = result
    contracts = max(1, round(contracts / 4)) if contracts >= 1 else 0
    details["kelly_fraction"] = details["kelly_fraction"] / 4
    risk = contracts * price_cents
    if return_details:
        return (contracts, risk, details)
    return (contracts, risk)


def _select_kelly_sizer(sigma):
    """Mirror of entertainment-bot._select_kelly_sizer for testing."""
    if sigma <= 0.05:
        return half_kelly, "half_kelly"
    elif sigma <= 0.10:
        return quarter_kelly, "quarter_kelly"
    else:
        return _eighth_kelly, "eighth_kelly"


class TestSelectKellySizer:
    """Test tiered Kelly selection based on data sigma."""

    def test_confirmed_data_uses_half_kelly(self):
        """Sigma <= 5% (confirmed HDD final chart) -> half_kelly."""
        fn, name = _select_kelly_sizer(0.02)
        assert fn is half_kelly
        assert name == "half_kelly"

    def test_boundary_5pct_uses_half_kelly(self):
        """Sigma at exactly 5% -> half_kelly (inclusive boundary)."""
        fn, name = _select_kelly_sizer(0.05)
        assert fn is half_kelly
        assert name == "half_kelly"

    def test_high_confidence_uses_quarter_kelly(self):
        """Sigma 6-10% (mid-week updates) -> quarter_kelly."""
        fn, name = _select_kelly_sizer(0.08)
        assert fn is quarter_kelly
        assert name == "quarter_kelly"

    def test_boundary_10pct_uses_quarter_kelly(self):
        """Sigma at exactly 10% -> quarter_kelly (inclusive boundary)."""
        fn, name = _select_kelly_sizer(0.10)
        assert fn is quarter_kelly
        assert name == "quarter_kelly"

    def test_low_confidence_uses_eighth_kelly(self):
        """Sigma > 10% (early projections/articles) -> eighth_kelly."""
        fn, name = _select_kelly_sizer(0.15)
        assert name == "eighth_kelly"

    def test_very_high_sigma_uses_eighth_kelly(self):
        """Sigma at 20% (very uncertain) -> eighth_kelly."""
        fn, name = _select_kelly_sizer(0.20)
        assert name == "eighth_kelly"

    def test_zero_sigma_uses_half_kelly(self):
        """Sigma at 0 (perfect data) -> half_kelly."""
        fn, name = _select_kelly_sizer(0.0)
        assert fn is half_kelly
        assert name == "half_kelly"


class TestEighthKelly:
    """Test the eighth_kelly sizing function."""

    def test_returns_smaller_than_quarter_kelly(self):
        """Eighth Kelly should produce fewer contracts than quarter Kelly."""
        edge = 0.15
        price = 50
        max_cost = 1000
        bankroll = 10000

        qk_count, qk_risk = quarter_kelly(edge, price, max_cost, bankroll_cents=bankroll)
        ek_count, ek_risk = _eighth_kelly(edge, price, max_cost, bankroll_cents=bankroll)

        assert ek_count <= qk_count
        assert ek_risk <= qk_risk

    def test_returns_smaller_than_half_kelly(self):
        """Eighth Kelly should produce fewer contracts than half Kelly."""
        edge = 0.15
        price = 50
        max_cost = 1000
        bankroll = 10000

        hk_count, hk_risk = half_kelly(edge, price, max_cost, bankroll_cents=bankroll)
        ek_count, ek_risk = _eighth_kelly(edge, price, max_cost, bankroll_cents=bankroll)

        assert ek_count <= hk_count
        assert ek_risk <= hk_risk

    def test_zero_edge_returns_zero(self):
        """No edge -> no contracts."""
        count, risk = _eighth_kelly(0, 50, 1000, bankroll_cents=10000)
        assert count == 0
        assert risk == 0

    def test_negative_edge_returns_zero(self):
        """Negative edge -> no contracts."""
        count, risk = _eighth_kelly(-0.05, 50, 1000, bankroll_cents=10000)
        assert count == 0
        assert risk == 0

    def test_return_details_flag(self):
        """With return_details=True, returns 3-tuple with kelly_fraction."""
        count, risk, details = _eighth_kelly(0.15, 50, 1000, bankroll_cents=10000, return_details=True)
        assert "kelly_fraction" in details
        assert "bankroll_used" in details
        assert details["kelly_fraction"] >= 0

    def test_kelly_fraction_is_quarter_of_half(self):
        """Eighth kelly_fraction should be ~1/4 of half_kelly fraction."""
        edge = 0.15
        price = 50
        _, _, hk_details = half_kelly(edge, price, 1000, bankroll_cents=10000, return_details=True)
        _, _, ek_details = _eighth_kelly(edge, price, 1000, bankroll_cents=10000, return_details=True)

        assert ek_details["kelly_fraction"] == pytest.approx(hk_details["kelly_fraction"] / 4, rel=0.01)

    def test_minimum_one_contract_when_positive(self):
        """When half_kelly returns >= 1 contract, eighth_kelly should return at least 1."""
        # Small edge, small bankroll -> half_kelly returns 1-4 contracts
        count, risk = _eighth_kelly(0.10, 30, 500, bankroll_cents=5000)
        hk_count, _ = half_kelly(0.10, 30, 500, bankroll_cents=5000)
        if hk_count >= 1:
            assert count >= 1

    def test_tiered_ordering(self):
        """Confirm half > quarter > eighth for same parameters."""
        edge = 0.20
        price = 40
        max_cost = 2000
        bankroll = 20000

        hk_count, _ = half_kelly(edge, price, max_cost, bankroll_cents=bankroll)
        qk_count, _ = quarter_kelly(edge, price, max_cost, bankroll_cents=bankroll)
        ek_count, _ = _eighth_kelly(edge, price, max_cost, bankroll_cents=bankroll)

        assert hk_count >= qk_count >= ek_count
