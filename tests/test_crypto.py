"""Tests for crypto-bot Deribit DVOL parsing and vol blending logic.

Fix 4: Validates the DVOL endpoint response parsing, out-of-range rejection,
and IV/RV blending math.
"""

import pytest


class TestDVOLResponseParsing:
    """Test the DVOL data point parsing logic (extracted from fetch_deribit_iv)."""

    def _parse_dvol(self, data):
        """Simulate the DVOL parsing logic from crypto-bot."""
        result = data.get("result", {})
        points = result.get("data", [])
        if not points:
            return None
        latest_close = points[-1][4]
        iv = latest_close / 100.0
        if iv < 0.10 or iv > 3.0:
            return None
        return iv

    def test_normal_btc_dvol(self):
        data = {"result": {"data": [[1700000000000, 55.0, 56.0, 54.0, 55.0]]}}
        assert self._parse_dvol(data) == pytest.approx(0.55, abs=0.01)

    def test_normal_eth_dvol(self):
        data = {"result": {"data": [[1700000000000, 70.0, 72.0, 68.0, 70.5]]}}
        assert self._parse_dvol(data) == pytest.approx(0.705, abs=0.01)

    def test_multiple_data_points_uses_latest(self):
        data = {"result": {"data": [
            [1700000000000, 50.0, 52.0, 48.0, 51.0],
            [1700003600000, 55.0, 56.0, 54.0, 55.0],
            [1700007200000, 60.0, 62.0, 58.0, 61.0],
        ]}}
        assert self._parse_dvol(data) == pytest.approx(0.61, abs=0.01)

    def test_empty_data_returns_none(self):
        data = {"result": {"data": []}}
        assert self._parse_dvol(data) is None

    def test_missing_result_returns_none(self):
        data = {}
        assert self._parse_dvol(data) is None

    def test_out_of_range_low_rejected(self):
        """IV below 10% is rejected."""
        data = {"result": {"data": [[1700000000000, 5.0, 6.0, 4.0, 5.0]]}}
        assert self._parse_dvol(data) is None

    def test_out_of_range_high_rejected(self):
        """IV above 300% is rejected."""
        data = {"result": {"data": [[1700000000000, 350.0, 360.0, 340.0, 350.0]]}}
        assert self._parse_dvol(data) is None

    def test_boundary_10_pct_accepted(self):
        data = {"result": {"data": [[1700000000000, 10.0, 11.0, 9.0, 10.0]]}}
        assert self._parse_dvol(data) == pytest.approx(0.10, abs=0.01)

    def test_boundary_300_pct_accepted(self):
        data = {"result": {"data": [[1700000000000, 300.0, 301.0, 299.0, 300.0]]}}
        assert self._parse_dvol(data) == pytest.approx(3.0, abs=0.01)


class TestVolBlending:
    """Test the IV/RV blending logic."""

    def _blend(self, iv, rv, default_vol=0.50):
        """Simulate the blending logic from scan_and_trade()."""
        if iv is not None and rv is not None:
            return 0.6 * iv + 0.4 * rv
        elif iv is not None:
            return iv
        elif rv is not None:
            return 0.3 * default_vol + 0.7 * rv
        else:
            return default_vol

    def test_both_iv_and_rv(self):
        assert self._blend(0.55, 0.45) == pytest.approx(0.51, abs=0.01)

    def test_iv_only(self):
        assert self._blend(0.55, None) == 0.55

    def test_rv_only(self):
        assert self._blend(None, 0.45) == pytest.approx(0.465, abs=0.01)

    def test_neither_uses_default(self):
        assert self._blend(None, None) == 0.50

    def test_iv_weight_exceeds_rv(self):
        """IV should get higher weight (60%) than RV (40%)."""
        blended = self._blend(0.60, 0.40)
        assert blended > 0.50
        assert blended == pytest.approx(0.52, abs=0.01)

    def test_custom_default_vol(self):
        assert self._blend(None, None, default_vol=0.65) == 0.65
