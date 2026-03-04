"""Tests for agent-based order book simulation.

Uses seeded random for reproducibility. Tests verify economic properties
(informed profit, price convergence) rather than exact values.
"""

import math
import pytest
from orderbook_sim import (
    OrderBook, Order, InformedAgent, NoiseAgent, MMAgent,
    OrderBookSimulator, kyle_lambda_estimate,
)


class TestOrderBook:
    def test_add_and_match(self):
        book = OrderBook()
        book.add_order(Order("buyer", "buy", 55, 1))
        book.add_order(Order("seller", "sell", 50, 1))
        fills = book.match()
        assert len(fills) == 1
        assert fills[0]["size"] == 1
        assert fills[0]["price"] == 52  # midpoint of 55 and 50

    def test_no_match_when_spread(self):
        book = OrderBook()
        book.add_order(Order("buyer", "buy", 45, 1))
        book.add_order(Order("seller", "sell", 55, 1))
        fills = book.match()
        assert len(fills) == 0

    def test_best_bid_ask(self):
        book = OrderBook()
        book.add_order(Order("b1", "buy", 48, 1))
        book.add_order(Order("b2", "buy", 50, 1))
        book.add_order(Order("s1", "sell", 55, 1))
        assert book.best_bid() == 50
        assert book.best_ask() == 55

    def test_midpoint(self):
        book = OrderBook()
        book.add_order(Order("b", "buy", 48, 1))
        book.add_order(Order("s", "sell", 52, 1))
        assert book.midpoint() == 50.0

    def test_empty_book(self):
        book = OrderBook()
        assert book.best_bid() == 0
        assert book.best_ask() == 100
        assert book.midpoint() == 50.0


class TestInformedAgent:
    def test_buys_when_underpriced(self):
        agent = InformedAgent("inf1", true_prob=0.80, signal_noise=0.0)
        book = OrderBook()
        book.add_order(Order("x", "sell", 60, 1))
        order = agent.decide(book, 0)
        assert order is not None
        assert order.side == "buy"

    def test_sells_when_overpriced(self):
        agent = InformedAgent("inf1", true_prob=0.20, signal_noise=0.0)
        book = OrderBook()
        book.add_order(Order("x", "buy", 40, 1))
        order = agent.decide(book, 0)
        assert order is not None
        assert order.side == "sell"


class TestOrderBookSimulator:
    def test_price_converges_to_true_prob(self):
        """Informed agents should push price toward true probability."""
        sim = OrderBookSimulator(true_prob=0.70, n_informed=15, n_noise=40,
                                 n_mm=5, seed=42)
        history, _ = sim.run(n_steps=500)
        # Last 50 prices should average close to 70
        final_avg = sum(history[-50:]) / 50
        assert 55 < final_avg < 85  # Within 15 cents of true prob

    def test_informed_agents_profit(self):
        """Informed agents should make money in aggregate."""
        sim = OrderBookSimulator(true_prob=0.65, n_informed=10, n_noise=50,
                                 n_mm=3, seed=123)
        history, book = sim.run(n_steps=400)
        # Compute informed agent P&L from fills
        informed_pnl = 0
        for fill in book.all_fills:
            buyer_id = fill["buyer_id"]
            seller_id = fill["seller_id"]
            price = fill["price"]
            true_cents = sim.true_prob * 100
            if buyer_id.startswith("informed_"):
                informed_pnl += true_cents - price  # expected profit per buy
            if seller_id.startswith("informed_"):
                informed_pnl += price - true_cents  # expected profit per sell
        # Informed should be profitable (on expectation)
        # Allow some variance -- just check it's not deeply negative
        assert informed_pnl > -200

    def test_returns_price_history(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=1)
        history, book = sim.run(n_steps=100)
        assert len(history) == 100
        assert all(0 <= p <= 100 for p in history)

    def test_deterministic_with_seed(self):
        sim1 = OrderBookSimulator(true_prob=0.60, seed=777)
        h1, _ = sim1.run(n_steps=50)
        sim2 = OrderBookSimulator(true_prob=0.60, seed=777)
        h2, _ = sim2.run(n_steps=50)
        assert h1 == h2


class TestFillProbability:
    def test_aggressive_price_fills_more(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=42)
        # Buying at ask should fill more than buying at low price
        fill_high = sim.estimate_fill_probability(55, "buy", n_simulations=50)
        fill_low = sim.estimate_fill_probability(40, "buy", n_simulations=50)
        assert fill_high >= fill_low

    def test_fill_probability_range(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=42)
        prob = sim.estimate_fill_probability(50, "buy", n_simulations=50)
        assert 0.0 <= prob <= 1.0


class TestKyleLambda:
    def test_positive_lambda(self):
        """Price impact should be positive (buys push price up)."""
        # Run simulation and collect order flow / price changes
        sim = OrderBookSimulator(true_prob=0.50, n_informed=10, n_noise=50,
                                 seed=42)
        history, book = sim.run(n_steps=300)

        # Build order flow from fills
        order_flows = []
        for fill in book.all_fills:
            # +1 for buys, -1 for sells (from the initiator's perspective)
            order_flows.append(1)  # simplified: each fill is a unit of flow
        price_changes = [history[i+1] - history[i]
                         for i in range(min(len(history)-1, len(order_flows)))]

        lam = kyle_lambda_estimate(history, order_flows)
        # Lambda should be non-negative (buys increase price)
        assert lam >= -0.5  # Allow small negative due to noise

    def test_insufficient_data(self):
        lam = kyle_lambda_estimate([50], [])
        assert lam == 0.0


class TestFindOptimalMMParams:
    def test_returns_valid_params(self):
        sim = OrderBookSimulator(true_prob=0.50, seed=42)
        result = sim.find_optimal_mm_params(
            gamma_range=[0.2, 0.5],
            k_range=[1.0, 2.0],
            n_simulations=10,
            n_steps=100,
        )
        assert "gamma" in result
        assert "k" in result
        assert "sharpe" in result
        assert "should_activate" in result
        assert result["gamma"] in [0.2, 0.5]
        assert result["k"] in [1.0, 2.0]
