"""Agent-Based Order Book Simulation for Market Maker Activation.

Simulates a continuous double auction with four agent types calibrated
to Kalshi prediction market microstructure. Used to:
  1. Estimate fill probability for limit orders at various prices
  2. Find optimal Avellaneda-Stoikov (gamma, k) per market
  3. Measure price impact (Kyle's lambda)
  4. Decide whether to activate the market maker on a given market

Agent types:
  - Informed (5-15%): trade toward true probability with noisy signal
  - Noise (60-80%): random direction, size
  - MarketMaker: A-S quotes, inventory-managed
  - (Future: Bot agents for deterministic strategies)

Usage:
    from orderbook_sim import OrderBookSimulator

    sim = OrderBookSimulator(true_prob=0.65, seed=42)
    history, book = sim.run(n_steps=500)
    fill_prob = sim.estimate_fill_probability(50, "buy")
    params = sim.find_optimal_mm_params()
"""

import math
import random
import logging

log = logging.getLogger("orderbook-sim")


class Order:
    """A limit order in the book."""
    __slots__ = ("agent_id", "side", "price", "size", "timestamp")

    def __init__(self, agent_id, side, price, size, timestamp=0):
        self.agent_id = agent_id
        self.side = side       # "buy" or "sell"
        self.price = int(price)  # cents, 1-99
        self.size = int(size)
        self.timestamp = timestamp


class OrderBook:
    """Continuous double auction order book."""

    def __init__(self):
        self.bids = []       # sorted by price descending
        self.asks = []       # sorted by price ascending
        self.all_fills = []  # list of fill dicts

    def add_order(self, order):
        if order.side == "buy":
            self.bids.append(order)
            self.bids.sort(key=lambda o: (-o.price, o.timestamp))
        else:
            self.asks.append(order)
            self.asks.sort(key=lambda o: (o.price, o.timestamp))

    def match(self, timestamp=0):
        """Match crossing orders. Returns list of fill dicts."""
        fills = []
        while self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            bid = self.bids[0]
            ask = self.asks[0]
            fill_price = (bid.price + ask.price) // 2
            fill_size = min(bid.size, ask.size)
            fills.append({
                "timestamp": timestamp,
                "price": fill_price,
                "size": fill_size,
                "buyer_id": bid.agent_id,
                "seller_id": ask.agent_id,
            })
            bid.size -= fill_size
            ask.size -= fill_size
            if bid.size <= 0:
                self.bids.pop(0)
            if ask.size <= 0:
                self.asks.pop(0)
        self.all_fills.extend(fills)
        return fills

    def best_bid(self):
        return self.bids[0].price if self.bids else 0

    def best_ask(self):
        return self.asks[0].price if self.asks else 100

    def midpoint(self):
        b, a = self.best_bid(), self.best_ask()
        if b > 0 and a < 100:
            return (b + a) / 2.0
        return 50.0

    def clear_stale(self, max_age=50, current_step=0):
        """Remove orders older than max_age steps to prevent book bloat."""
        self.bids = [o for o in self.bids if current_step - o.timestamp < max_age]
        self.asks = [o for o in self.asks if current_step - o.timestamp < max_age]


class InformedAgent:
    """Trades toward true probability with noisy private signal."""

    def __init__(self, agent_id, true_prob, signal_noise=0.03):
        self.agent_id = agent_id
        self.true_prob = true_prob
        self.signal_noise = signal_noise

    def decide(self, book, step):
        signal = self.true_prob + random.gauss(0, self.signal_noise)
        signal = max(0.01, min(0.99, signal))
        signal_cents = round(signal * 100)
        mid = book.midpoint()

        if signal_cents > mid + 2:
            return Order(self.agent_id, "buy", min(signal_cents, 99), 1, step)
        elif signal_cents < mid - 2:
            return Order(self.agent_id, "sell", max(signal_cents, 1), 1, step)
        return None


class NoiseAgent:
    """Random trader -- models retail participants."""

    def __init__(self, agent_id, trade_prob=0.25):
        self.agent_id = agent_id
        self.trade_prob = trade_prob

    def decide(self, book, step):
        if random.random() > self.trade_prob:
            return None
        side = random.choice(["buy", "sell"])
        mid = book.midpoint()
        offset = random.gauss(0, 5)
        price = int(mid + offset)
        price = max(1, min(99, price))
        return Order(self.agent_id, side, price, 1, step)


class MMAgent:
    """Market maker using Avellaneda-Stoikov reservation price model."""

    def __init__(self, agent_id, gamma=0.3, k=1.5, sigma_frac=0.05):
        self.agent_id = agent_id
        self.gamma = gamma
        self.k = k
        self.sigma_frac = sigma_frac
        self.inventory = 0

    def decide(self, book, step):
        mid_frac = book.midpoint() / 100.0
        T = 1.0

        r = mid_frac - self.inventory * self.gamma * (self.sigma_frac ** 2) * T
        r_cents = max(1, min(99, round(r * 100)))

        delta = (self.gamma * (self.sigma_frac ** 2) * T +
                 (2 / self.gamma) * math.log(1 + self.gamma / self.k))
        half_spread = max(1, round(delta * 100 / 2))

        bid = max(1, r_cents - half_spread)
        ask = min(99, r_cents + half_spread)

        orders = []
        if bid < ask:
            orders.append(Order(self.agent_id, "buy", bid, 1, step))
            orders.append(Order(self.agent_id, "sell", ask, 1, step))
        return orders if orders else None


class OrderBookSimulator:
    """Agent-based market simulation for MM evaluation."""

    def __init__(self, true_prob=0.50, n_informed=10, n_noise=60,
                 n_mm=5, informed_noise=0.03, seed=None):
        self._seed = seed
        if seed is not None:
            random.seed(seed)
        self.true_prob = true_prob
        self.agents = []
        self._agent_map = {}
        self._mm_agents = []

        aid = 0
        for _ in range(n_informed):
            a = InformedAgent(f"informed_{aid}", true_prob, informed_noise)
            self.agents.append(a)
            self._agent_map[a.agent_id] = a
            aid += 1
        for _ in range(n_noise):
            a = NoiseAgent(f"noise_{aid}")
            self.agents.append(a)
            self._agent_map[a.agent_id] = a
            aid += 1
        for _ in range(n_mm):
            a = MMAgent(f"mm_{aid}")
            self.agents.append(a)
            self._agent_map[a.agent_id] = a
            self._mm_agents.append(a)
            aid += 1

    def run(self, n_steps=500):
        """Run simulation. Returns (price_history, book)."""
        book = OrderBook()
        price_history = []

        for step in range(n_steps):
            agent = random.choice(self.agents)
            result = agent.decide(book, step)

            if result is None:
                pass
            elif isinstance(result, list):
                for order in result:
                    book.add_order(order)
            else:
                book.add_order(result)

            fills = book.match(step)

            # Update MM inventory from fills
            for fill in fills:
                buyer = self._agent_map.get(fill["buyer_id"])
                seller = self._agent_map.get(fill["seller_id"])
                if isinstance(buyer, MMAgent):
                    buyer.inventory += fill["size"]
                if isinstance(seller, MMAgent):
                    seller.inventory -= fill["size"]

            # Periodic stale order cleanup
            if step % 50 == 0:
                book.clear_stale(max_age=100, current_step=step)

            price_history.append(book.midpoint())

        return price_history, book

    def estimate_fill_probability(self, limit_price, side="buy",
                                   duration_steps=50, n_simulations=100):
        """Estimate probability a limit order fills within duration_steps."""
        fills = 0
        for sim_i in range(n_simulations):
            if self._seed is not None:
                random.seed(self._seed + sim_i + 10000)
            else:
                random.seed(sim_i + 10000)

            book = OrderBook()
            our_order = Order("_test_", side, limit_price, 1, 0)
            book.add_order(our_order)

            filled = False
            for step in range(duration_steps):
                agent = random.choice(self.agents)
                result = agent.decide(book, step)
                if result is None:
                    continue
                if isinstance(result, list):
                    for order in result:
                        book.add_order(order)
                else:
                    book.add_order(result)

                matched = book.match(step)
                for f in matched:
                    if f["buyer_id"] == "_test_" or f["seller_id"] == "_test_":
                        filled = True
                        break
                if filled:
                    break

            if filled:
                fills += 1

        return fills / n_simulations

    def find_optimal_mm_params(self, gamma_range=None, k_range=None,
                                n_simulations=30, n_steps=300):
        """Grid search for optimal MM params.

        Returns dict with best gamma, k, sharpe, and should_activate flag.
        """
        if gamma_range is None:
            gamma_range = [0.1, 0.2, 0.3, 0.5, 0.8]
        if k_range is None:
            k_range = [0.5, 1.0, 1.5, 2.0, 3.0]

        best_sharpe = -float("inf")
        best_params = (0.3, 1.5)

        for gamma in gamma_range:
            for k in k_range:
                pnls = []
                for sim_i in range(n_simulations):
                    if self._seed is not None:
                        random.seed(self._seed + sim_i + 20000)

                    # Sim without our MM, plus one of ours
                    sim = OrderBookSimulator(
                        self.true_prob, n_informed=10, n_noise=60, n_mm=3,
                        seed=(self._seed or 0) + sim_i + 30000,
                    )
                    our_mm = MMAgent("our_mm", gamma=gamma, k=k)
                    sim.agents.append(our_mm)
                    sim._agent_map[our_mm.agent_id] = our_mm
                    sim._mm_agents.append(our_mm)

                    _, book = sim.run(n_steps)

                    # Compute our MM's P&L from fills
                    buy_cost = 0
                    buy_qty = 0
                    sell_rev = 0
                    sell_qty = 0
                    for fill in book.all_fills:
                        if fill["buyer_id"] == "our_mm":
                            buy_cost += fill["price"] * fill["size"]
                            buy_qty += fill["size"]
                        if fill["seller_id"] == "our_mm":
                            sell_rev += fill["price"] * fill["size"]
                            sell_qty += fill["size"]

                    net_inv = buy_qty - sell_qty
                    settlement = net_inv * (self.true_prob * 100)
                    pnl = sell_rev - buy_cost + settlement
                    pnls.append(pnl)

                if len(pnls) >= 2:
                    mean_pnl = sum(pnls) / len(pnls)
                    var = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
                    std_pnl = math.sqrt(var) if var > 0 else 0.001
                    sharpe = mean_pnl / std_pnl

                    if sharpe > best_sharpe:
                        best_sharpe = sharpe
                        best_params = (gamma, k)

        return {
            "gamma": best_params[0],
            "k": best_params[1],
            "sharpe": round(best_sharpe, 4),
            "should_activate": best_sharpe > 1.0,
        }


def kyle_lambda_estimate(price_history, order_flows):
    """Estimate Kyle's lambda from price changes and order flow.

    lambda = price impact per unit signed order flow.
    Uses regression through origin: delta_P = lambda * signed_sqrt_flow.

    Returns estimated lambda.
    """
    if len(price_history) < 2 or len(order_flows) < 1:
        return 0.0

    n = min(len(price_history) - 1, len(order_flows))
    xs, ys = [], []
    for i in range(n):
        of = order_flows[i]
        if of == 0:
            continue
        x = math.copysign(math.sqrt(abs(of)), of)
        y = price_history[i + 1] - price_history[i]
        xs.append(x)
        ys.append(y)

    if len(xs) < 2:
        return 0.0

    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)

    return sum_xy / sum_xx if sum_xx > 0 else 0.0


def load_calibrated_params(config_path, ticker_prefix):
    """Load calibrated MM params for a market prefix.

    Returns dict with gamma, k if activated. Returns None if not
    activated, not found, or config file missing.
    """
    import json
    try:
        with open(config_path) as f:
            data = json.load(f)
        params = data.get(ticker_prefix)
        if params and params.get("activated"):
            return {"gamma": params["gamma"], "k": params["k"]}
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None
