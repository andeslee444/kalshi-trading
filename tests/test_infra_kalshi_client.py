"""Direct tests for the extracted infra.kalshi_client module."""

import json
import logging
from unittest.mock import MagicMock

import pytest

from infra.kalshi_client import (
    KalshiClient,
    normalize_market,
    read_market_cache,
    write_market_cache,
)


def test_normalize_market_converts_v2_fields():
    market = {
        "yes_bid_dollars": "0.8600",
        "yes_ask_dollars": "0.8700",
        "volume_fp": "100.00",
    }

    normalize_market(market)

    assert market["yes_bid"] == 86
    assert market["yes_ask"] == 87
    assert market["volume"] == 100


def test_write_and_read_market_cache_round_trip(tmp_path):
    cache_path = tmp_path / "market-cache.json"

    write_market_cache({"KXHIGH": [{"ticker": "T1"}]}, cache_path=cache_path)
    result = read_market_cache(prefix="KXHIGH", cache_path=cache_path, max_age=60)

    assert result == [{"ticker": "T1"}]


def test_request_raises_clear_error_on_non_json_response():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = json.JSONDecodeError("msg", "doc", 0)
    mock_response.text = "<html>503 Service Unavailable</html>"
    mock_response.raise_for_status.return_value = None

    client = KalshiClient.__new__(KalshiClient)
    client.base_url = "https://api.kalshi.com/trade-api/v2"
    client.session = MagicMock()
    client.session.request.return_value = mock_response
    client.log = logging.getLogger("test")
    client._sign = MagicMock(return_value={})
    client.requests_module = pytest.importorskip("requests")

    with pytest.raises(ValueError, match="Non-JSON response"):
        KalshiClient._request(client, "GET", "/portfolio/balance")


def test_get_market_normalizes_success_response():
    client = MagicMock()
    client._normalize_market = normalize_market
    client.get.return_value = {
        "market": {
            "ticker": "KXHIGHLAX-26MAR12-T85",
            "yes_bid_dollars": "0.8600",
            "yes_ask_dollars": "0.8700",
            "volume_fp": "100.00",
        }
    }

    result = KalshiClient.get_market(client, "KXHIGHLAX-26MAR12-T85")

    assert result["yes_bid"] == 86
    assert result["yes_ask"] == 87
    assert result["volume"] == 100
