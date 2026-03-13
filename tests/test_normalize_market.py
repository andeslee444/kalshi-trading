"""Tests for Kalshi API v2 market field normalization.

The Kalshi API v2 returns prices as dollar-amount strings (e.g., "0.86")
in _dollars/_fp fields. The normalize_market() function converts these to
integer-cent fields (e.g., yes_bid=86) that all bot code expects.
"""

import pytest
from kalshi_auth import normalize_market, normalize_markets


# --- Sample API v2 response (mirrors real Kalshi data) ---

def _make_api_v2_market(**overrides):
    """Build a realistic Kalshi API v2 market dict."""
    m = {
        "ticker": "KXHIGHLAX-26MAR12-T85",
        "status": "active",
        "yes_bid_dollars": "0.8600",
        "yes_ask_dollars": "0.8700",
        "no_bid_dollars": "0.1300",
        "no_ask_dollars": "0.1400",
        "last_price_dollars": "0.8700",
        "volume_fp": "5622.00",
        "open_interest_fp": "4580.00",
        "volume_24h_fp": "120.00",
        "yes_bid_size_fp": "453.00",
        "yes_ask_size_fp": "449.00",
        "response_price_units": "usd_cent",
    }
    m.update(overrides)
    return m


class TestNormalizeMarket:
    """Core normalization tests."""

    def test_converts_dollar_strings_to_cents(self):
        m = _make_api_v2_market()
        normalize_market(m)
        assert m["yes_bid"] == 86
        assert m["yes_ask"] == 87
        assert m["no_bid"] == 13
        assert m["no_ask"] == 14
        assert m["last_price"] == 87

    def test_converts_volume_and_open_interest(self):
        m = _make_api_v2_market()
        normalize_market(m)
        assert m["volume"] == 5622
        assert m["open_interest"] == 4580

    def test_preserves_original_fields(self):
        """Normalization adds legacy fields without removing the API v2 fields."""
        m = _make_api_v2_market()
        normalize_market(m)
        assert m["yes_bid_dollars"] == "0.8600"
        assert m["volume_fp"] == "5622.00"

    def test_preserves_non_price_fields(self):
        m = _make_api_v2_market()
        normalize_market(m)
        assert m["ticker"] == "KXHIGHLAX-26MAR12-T85"
        assert m["status"] == "active"

    def test_returns_same_dict(self):
        m = _make_api_v2_market()
        result = normalize_market(m)
        assert result is m


class TestIdempotency:
    """Calling normalize_market() multiple times must not corrupt data."""

    def test_double_normalize_unchanged(self):
        m = _make_api_v2_market()
        normalize_market(m)
        first_bid = m["yes_bid"]
        first_ask = m["yes_ask"]
        normalize_market(m)
        assert m["yes_bid"] == first_bid
        assert m["yes_ask"] == first_ask

    def test_already_has_legacy_fields(self):
        """If legacy fields are present (e.g., from test fixtures), don't overwrite."""
        m = {
            "ticker": "KXHIGHNY-26MAR13-T45",
            "yes_bid": 40,
            "yes_ask": 44,
            "no_bid": 56,
            "no_ask": 60,
            "volume": 38,
            "open_interest": 36,
            "last_price": 42,
            # Also has new-style fields with different values
            "yes_bid_dollars": "0.9900",
            "yes_ask_dollars": "0.9900",
        }
        normalize_market(m)
        # Legacy fields must NOT be overwritten
        assert m["yes_bid"] == 40
        assert m["yes_ask"] == 44


class TestEdgeCases:
    """Edge cases: missing fields, zeros, empty strings, bad data."""

    def test_zero_prices(self):
        """Empty orderbook: bid=0, ask=$1.00."""
        m = _make_api_v2_market(
            yes_bid_dollars="0.0000",
            yes_ask_dollars="1.0000",
            no_bid_dollars="0.0000",
            no_ask_dollars="1.0000",
            volume_fp="0.00",
            open_interest_fp="0.00",
        )
        normalize_market(m)
        assert m["yes_bid"] == 0
        assert m["yes_ask"] == 100
        assert m["volume"] == 0

    def test_missing_dollar_fields(self):
        """Market dict with no _dollars fields at all (shouldn't crash)."""
        m = {"ticker": "KXTEST", "status": "active"}
        normalize_market(m)
        # No legacy fields added since source fields don't exist
        assert m.get("yes_bid") is None
        assert m.get("yes_ask") is None

    def test_none_dollar_values(self):
        """Dollar fields present but None (API sometimes returns null)."""
        m = _make_api_v2_market(
            yes_bid_dollars=None,
            yes_ask_dollars=None,
        )
        normalize_market(m)
        assert m.get("yes_bid") is None
        assert m.get("yes_ask") is None
        # Other fields should still convert
        assert m["no_bid"] == 13
        assert m["volume"] == 5622

    def test_empty_string_dollars(self):
        """Empty string in dollar field → converted to 0 (fail-safe)."""
        m = _make_api_v2_market(yes_bid_dollars="")
        normalize_market(m)
        assert m["yes_bid"] == 0

    def test_non_numeric_string(self):
        """Garbage in dollar field → converted to 0 (fail-safe)."""
        m = _make_api_v2_market(yes_bid_dollars="N/A")
        normalize_market(m)
        assert m["yes_bid"] == 0

    def test_subpenny_rounding_half_up(self):
        """Subpenny prices use arithmetic rounding (0.5 rounds UP, not banker's)."""
        m = _make_api_v2_market(
            yes_bid_dollars="0.0350",   # 3.5 cents → 4
            yes_ask_dollars="0.9650",   # 96.5 cents → 97 (arithmetic), NOT 96 (banker's)
        )
        normalize_market(m)
        assert m["yes_bid"] == 4    # round_half_up(3.5) = 4
        assert m["yes_ask"] == 97   # round_half_up(96.5) = 97 (NOT 96 from banker's)

    def test_subpenny_045_rounds_up(self):
        """0.045 → 4.5 cents → 5 (arithmetic), NOT 4 (banker's)."""
        m = _make_api_v2_market(yes_bid_dollars="0.0450")
        normalize_market(m)
        assert m["yes_bid"] == 5  # round_half_up(4.5) = 5

    def test_subpenny_065_rounds_up(self):
        """0.065 → 6.5 cents → 7 (arithmetic), NOT 6 (banker's)."""
        m = _make_api_v2_market(yes_bid_dollars="0.0650")
        normalize_market(m)
        assert m["yes_bid"] == 7  # round_half_up(6.5) = 7

    def test_subpenny_085_rounds_up(self):
        """0.085 → 8.5 cents → 9 (arithmetic), NOT 8 (banker's)."""
        m = _make_api_v2_market(yes_bid_dollars="0.0850")
        normalize_market(m)
        assert m["yes_bid"] == 9  # round_half_up(8.5) = 9

    def test_high_precision_dollars(self):
        """Extra decimal places handled correctly."""
        m = _make_api_v2_market(yes_bid_dollars="0.12345678")
        normalize_market(m)
        assert m["yes_bid"] == 12  # round(12.345678) = 12

    def test_fractional_volume(self):
        """Fractional volume truncated to int."""
        m = _make_api_v2_market(volume_fp="99.99")
        normalize_market(m)
        assert m["volume"] == 99


class TestNormalizeMarkets:
    """Batch normalization of market lists."""

    def test_normalizes_all_in_list(self):
        markets = [
            _make_api_v2_market(ticker="KXHIGHLAX-26MAR12-T85", yes_bid_dollars="0.8600"),
            _make_api_v2_market(ticker="KXHIGHNY-26MAR13-T45", yes_bid_dollars="0.4000"),
            _make_api_v2_market(ticker="KXHIGHCHI-26MAR12-T51", yes_bid_dollars="0.5000"),
        ]
        result = normalize_markets(markets)
        assert result is markets  # same list object
        assert markets[0]["yes_bid"] == 86
        assert markets[1]["yes_bid"] == 40
        assert markets[2]["yes_bid"] == 50

    def test_empty_list(self):
        result = normalize_markets([])
        assert result == []

    def test_mixed_normalized_and_raw(self):
        """List with both already-normalized and raw API dicts."""
        markets = [
            {"ticker": "A", "yes_bid": 50, "yes_ask": 55},  # already normalized
            _make_api_v2_market(ticker="B", yes_bid_dollars="0.3000"),  # raw API
        ]
        normalize_markets(markets)
        assert markets[0]["yes_bid"] == 50  # unchanged
        assert markets[1]["yes_bid"] == 30  # converted


class TestLiquidityFilterIntegration:
    """Verify that normalized data passes through is_market_liquid() correctly."""

    def test_liquid_market_passes_filter(self):
        from probability import is_market_liquid
        m = _make_api_v2_market(
            yes_bid_dollars="0.8600",
            yes_ask_dollars="0.8700",
            volume_fp="100.00",
        )
        normalize_market(m)
        assert is_market_liquid(m) is True

    def test_empty_book_fails_filter(self):
        from probability import is_market_liquid
        m = _make_api_v2_market(
            yes_bid_dollars="0.0000",
            yes_ask_dollars="1.0000",
            volume_fp="0.00",
        )
        normalize_market(m)
        assert is_market_liquid(m) is False

    def test_wide_spread_fails_filter(self):
        from probability import is_market_liquid
        m = _make_api_v2_market(
            yes_bid_dollars="0.0100",
            yes_ask_dollars="0.5000",
            volume_fp="50.00",
        )
        normalize_market(m)
        # spread = 50 - 1 = 49c > 20c threshold
        assert is_market_liquid(m) is False

    def test_low_volume_fails_filter(self):
        from probability import is_market_liquid
        m = _make_api_v2_market(
            yes_bid_dollars="0.4000",
            yes_ask_dollars="0.4400",
            volume_fp="3.00",
        )
        normalize_market(m)
        # volume=3 < 10 threshold
        assert is_market_liquid(m) is False

    def test_near_settlement_relaxed_volume(self):
        from probability import is_market_liquid
        m = _make_api_v2_market(
            yes_bid_dollars="0.4000",
            yes_ask_dollars="0.4400",
            volume_fp="7.00",
        )
        normalize_market(m)
        # volume=7 >= 5 (near-settlement threshold)
        assert is_market_liquid(m, min_volume=5) is True


class TestGetMarketErrorHandling:
    """Verify get_market() logs errors instead of silently swallowing them."""

    def test_get_market_logs_exception(self):
        """get_market() must log the exception before returning None."""
        import logging
        from unittest.mock import patch, MagicMock

        client = MagicMock()
        # Make .get() raise a connection error
        client.get.side_effect = ConnectionError("API unreachable")

        # Attach a real KalshiClient.get_market to our mock
        from kalshi_auth import KalshiClient
        bound_method = KalshiClient.get_market.__get__(client, KalshiClient)

        with patch("kalshi_auth._log") as mock_log:
            result = bound_method("KXTEST-FAKE")
            assert result is None
            mock_log.warning.assert_called_once()
            log_msg = mock_log.warning.call_args[0][0]
            assert "KXTEST-FAKE" in log_msg % mock_log.warning.call_args[0][1:]

    def test_get_market_returns_none_on_error(self):
        """get_market() returns None on exception (not crash)."""
        from unittest.mock import MagicMock
        from kalshi_auth import KalshiClient

        client = MagicMock()
        client.get.side_effect = Exception("500 Internal Server Error")

        bound_method = KalshiClient.get_market.__get__(client, KalshiClient)
        result = bound_method("KXTEST-FAKE")
        assert result is None

    def test_get_market_normalizes_on_success(self):
        """get_market() normalizes API v2 fields on success."""
        from unittest.mock import MagicMock
        from kalshi_auth import KalshiClient

        client = MagicMock()
        client.get.return_value = {
            "market": {
                "ticker": "KXHIGHLAX-26MAR12-T85",
                "yes_bid_dollars": "0.8600",
                "yes_ask_dollars": "0.8700",
                "volume_fp": "100.00",
            }
        }

        bound_method = KalshiClient.get_market.__get__(client, KalshiClient)
        result = bound_method("KXHIGHLAX-26MAR12-T85")
        assert result["yes_bid"] == 86
        assert result["yes_ask"] == 87
        assert result["volume"] == 100
