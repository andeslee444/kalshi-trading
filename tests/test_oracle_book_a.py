"""Tests for Book A: game-level price divergence."""

from domain.oracle.books.book_a import (
    scan_divergence,
    divergence_to_signal,
    evaluate_games,
)
from domain.oracle.models import Book


def test_scan_divergence_yes_side():
    """Real=68%, Kalshi=49% -> 19% edge on YES side."""
    opp = scan_divergence(
        real_prob=0.68,
        kalshi_yes_price_cents=49,
        ticker="KXNBA-18MAR26-LALHOU-LAL",
        game_id="LAL-HOU-20260318",
        real_volume=5000000,
        min_edge=0.15,
    )
    assert opp is not None
    assert opp.side == "yes"
    assert abs(opp.edge - 0.19) < 0.01
    assert opp.kalshi_price_cents == 49


def test_scan_divergence_no_side():
    """Real=30%, Kalshi YES=55% -> NO edge = (1-0.30) - (1-0.55) = 0.25."""
    opp = scan_divergence(
        real_prob=0.30,
        kalshi_yes_price_cents=55,
        ticker="KXNBA-18MAR26-LALHOU-LAL",
        game_id="LAL-HOU-20260318",
        real_volume=5000000,
        min_edge=0.15,
    )
    assert opp is not None
    assert opp.side == "no"
    assert abs(opp.edge - 0.25) < 0.01
    assert opp.kalshi_price_cents == 45  # NO price = 100 - 55


def test_scan_divergence_below_threshold():
    """Small divergence below threshold returns None."""
    opp = scan_divergence(
        real_prob=0.55,
        kalshi_yes_price_cents=50,
        ticker="T1",
        game_id="G1",
        real_volume=5000000,
        min_edge=0.15,
    )
    assert opp is None


def test_scan_divergence_low_volume():
    """Below minimum volume returns None."""
    opp = scan_divergence(
        real_prob=0.80,
        kalshi_yes_price_cents=50,
        ticker="T1",
        game_id="G1",
        real_volume=50000,
        min_edge=0.15,
        min_volume=200000,
    )
    assert opp is None


def test_scan_divergence_invalid_price():
    assert scan_divergence(0.5, 0, "T1", "G1", 1000000) is None
    assert scan_divergence(0.5, 100, "T1", "G1", 1000000) is None
    # Extreme prices (<=5, >=95) rejected to avoid illiquid markets
    assert scan_divergence(0.5, 5, "T1", "G1", 1000000) is None
    assert scan_divergence(0.5, 95, "T1", "G1", 1000000) is None


def test_scan_divergence_invalid_prob():
    assert scan_divergence(0.0, 50, "T1", "G1", 1000000) is None
    assert scan_divergence(1.0, 50, "T1", "G1", 1000000) is None


def test_divergence_to_signal_yes():
    from domain.oracle.books.book_a import DivergenceOpportunity
    opp = DivergenceOpportunity(
        ticker="KXNBA-18MAR26-LALHOU-LAL",
        game_id="LAL-HOU-20260318",
        side="yes",
        real_prob=0.68,
        kalshi_price_cents=49,
        edge=0.19,
        volume=5000000,
    )
    signal = divergence_to_signal(opp)
    assert signal.book == Book.A
    assert signal.side == "yes"
    assert signal.model_prob == 0.68
    assert abs(signal.edge - 0.19) < 0.01


def test_divergence_to_signal_no():
    from domain.oracle.books.book_a import DivergenceOpportunity
    opp = DivergenceOpportunity(
        ticker="KXNBA-18MAR26-LALHOU-HOU",
        game_id="LAL-HOU-20260318",
        side="no",
        real_prob=0.30,
        kalshi_price_cents=45,
        edge=0.25,
        volume=5000000,
    )
    signal = divergence_to_signal(opp)
    assert signal.book == Book.A
    assert signal.side == "no"
    assert abs(signal.model_prob - 0.70) < 0.01  # 1 - 0.30


def test_evaluate_games():
    real_markets = [
        {
            "game_id": 23454,
            "home_team": "Lakers",
            "away_team": "Rockets",
            "home_pct": 70,
            "volume": 5000000,
        },
    ]
    kalshi_markets = [
        {"ticker": "KXNBA-18MAR26-LALHOU-LAL", "yes_price": 50},
    ]

    def mock_match(home, away, markets):
        return markets  # return all for simplicity

    opps = evaluate_games(
        real_markets, kalshi_markets, mock_match,
        min_edge=0.15, min_volume=200000,
    )
    assert len(opps) == 1
    assert opps[0].side == "yes"
    assert abs(opps[0].edge - 0.20) < 0.01


# ── Edge convergence tests (spec Section 3) ──

def test_edge_convergence_detection():
    """Edge narrowing over 3 consecutive polls → skip."""
    from domain.oracle.books.book_a import is_edge_converging, reset_edge_history
    reset_edge_history()

    ticker = "KXNBA-TEST-CONVERGENCE"
    # Simulate 3 polls with narrowing edge: 0.20 → 0.18 → 0.16
    assert not is_edge_converging(ticker, 0.20)  # only 1 data point
    assert not is_edge_converging(ticker, 0.18)  # 2 data points
    assert is_edge_converging(ticker, 0.16)      # 3 polls, all narrowing → converging

    reset_edge_history(ticker)


def test_edge_not_converging_when_stable():
    """Stable or widening edge → do NOT skip."""
    from domain.oracle.books.book_a import is_edge_converging, reset_edge_history
    reset_edge_history()

    ticker = "KXNBA-TEST-STABLE"
    assert not is_edge_converging(ticker, 0.20)
    assert not is_edge_converging(ticker, 0.20)  # stable, not narrowing
    assert not is_edge_converging(ticker, 0.22)  # widening

    reset_edge_history(ticker)


def test_convergence_blocks_scan_divergence():
    """scan_divergence returns None when edge is converging."""
    from domain.oracle.books.book_a import reset_edge_history
    reset_edge_history()

    ticker = "KXNBA-26MAR18-CONVERGENCE"
    # 3 polls with narrowing edge (real_prob decreasing toward kalshi)
    scan_divergence(0.85, 50, ticker, "G1", 5000000, min_edge=0.15)  # edge=0.35
    scan_divergence(0.80, 50, ticker, "G1", 5000000, min_edge=0.15)  # edge=0.30
    # Third poll with still-narrowing edge should be blocked
    opp = scan_divergence(0.75, 50, ticker, "G1", 5000000, min_edge=0.15)  # edge=0.25
    assert opp is None  # blocked by convergence check

    reset_edge_history(ticker)
