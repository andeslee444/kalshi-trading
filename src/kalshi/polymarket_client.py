"""Polymarket CLOB API client — read-only access for cross-platform arbitrage.

Reads public order book data from Polymarket's CLOB API.
No authentication required for reading prices and markets.

Usage:
    from polymarket_client import PolymarketClient

    pm = PolymarketClient()
    markets = pm.get_markets(query="CPI")
    book = pm.get_orderbook(token_id)
"""

import requests
import logging
import time

_log = logging.getLogger("polymarket")

CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


class PolymarketClient:
    """Read-only Polymarket CLOB client."""

    def __init__(self, timeout=15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KalshiArbMonitor/1.0",
            "Accept": "application/json",
        })

    def get_markets(self, query=None, limit=100, offset=0):
        """Search Polymarket events/markets.

        Returns list of market dicts with keys like:
          condition_id, question, outcomes, tokens, active, closed, etc.
        """
        try:
            params = {"limit": limit, "offset": offset, "active": True}
            if query:
                params["_q"] = query
            r = self.session.get(
                f"{GAMMA_BASE_URL}/markets",
                params=params,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            _log.error("Polymarket markets fetch failed: %s", e)
            return []

    def get_market(self, condition_id):
        """Get a single market by condition ID."""
        try:
            r = self.session.get(
                f"{GAMMA_BASE_URL}/markets/{condition_id}",
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            _log.error("Polymarket market fetch failed for %s: %s", condition_id, e)
            return None

    def get_orderbook(self, token_id):
        """Get CLOB order book for a token.

        Returns dict with 'bids' and 'asks' arrays, each entry having
        'price' and 'size' fields.
        """
        try:
            r = self.session.get(
                f"{CLOB_BASE_URL}/book",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            _log.error("Polymarket orderbook fetch failed for %s: %s", token_id, e)
            return None

    def get_midpoint(self, token_id):
        """Get midpoint price for a token.

        Returns midpoint as decimal (0-1), or None on failure.
        """
        try:
            r = self.session.get(
                f"{CLOB_BASE_URL}/midpoint",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            mid = data.get("mid")
            if mid is not None:
                return float(mid)
            return None
        except Exception as e:
            _log.error("Polymarket midpoint fetch failed for %s: %s", token_id, e)
            return None

    def get_best_bid(self, token_id):
        """Get best bid price for a token (what you could sell at).

        Returns best bid as decimal (0-1), or None if no bids.
        """
        book = self.get_orderbook(token_id)
        if book and book.get("bids"):
            try:
                return max(float(b["price"]) for b in book["bids"])
            except (ValueError, KeyError):
                pass
        return None

    def get_price(self, token_id):
        """Get last traded price for a token.

        Returns price as decimal (0-1), or None on failure.
        """
        try:
            r = self.session.get(
                f"{CLOB_BASE_URL}/price",
                params={"token_id": token_id, "side": "buy"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            return float(data.get("price", 0))
        except Exception as e:
            _log.error("Polymarket price fetch failed for %s: %s", token_id, e)
            return None
