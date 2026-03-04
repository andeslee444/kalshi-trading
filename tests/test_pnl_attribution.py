"""Tests for P&L attribution engine.

Tests pure analytics functions — no API calls or kalshi_auth dependency.
All trade data is provided as fixture dicts matching the golden record format.
"""

import pytest
from pnl_attribution import (
    PnLAttributor, _compute_pnl_cents, _classify_market_type,
)


# ── Fixtures: known trade records with settlement data ──

def _make_trade(ticker="KXHIGHHOU-26MAR03-T75", source_bot="weather",
                raw_edge=0.12, cost_cents=200, count=4, price_cents=50,
                sizing_method="half_kelly", timestamp="2026-03-01T10:00:00",
                settlement_result=None, settlement_revenue_cents=None):
    """Build a golden-record-style trade dict."""
    return {
        "ticker": ticker,
        "source_bot": source_bot,
        "raw_edge": raw_edge,
        "cost_cents": cost_cents,
        "count": count,
        "price_cents": price_cents,
        "sizing_method": sizing_method,
        "timestamp": timestamp,
        "settlement_result": settlement_result,
        "settlement_revenue_cents": settlement_revenue_cents,
        "best_bid": 48,
        "best_ask": 52,
        "spread": 4,
        "status": "filled",
    }


SETTLED_TRADES = [
    # Weather bot: won, 4 contracts at 50c → revenue 400c, cost 200c, P&L +200c
    _make_trade(source_bot="weather", raw_edge=0.12, cost_cents=200, count=4,
                sizing_method="half_kelly", timestamp="2026-03-01T10:00:00",
                settlement_result="won", settlement_revenue_cents=400),
    # Weather bot: lost, 2 contracts at 60c → revenue 0c, cost 120c, P&L -120c
    _make_trade(source_bot="weather", raw_edge=0.08, cost_cents=120, count=2,
                price_cents=60, sizing_method="half_kelly",
                timestamp="2026-03-01T14:00:00",
                settlement_result="lost", settlement_revenue_cents=0),
    # Crypto bot: won, 3 contracts at 30c → revenue 300c, cost 90c, P&L +210c
    _make_trade(ticker="KXBTC-26MAR03-T95000", source_bot="crypto",
                raw_edge=0.20, cost_cents=90, count=3, price_cents=30,
                sizing_method="quarter_kelly", timestamp="2026-03-01T11:00:00",
                settlement_result="won", settlement_revenue_cents=300),
    # Economics bot: lost, 10 contracts at 3c → revenue 0c, cost 30c, P&L -30c
    _make_trade(ticker="KXCPI-26MAY-T20", source_bot="economics",
                raw_edge=0.05, cost_cents=30, count=10, price_cents=3,
                sizing_method="half_kelly", timestamp="2026-03-02T08:00:00",
                settlement_result="lost", settlement_revenue_cents=0),
    # Strategy bot: won (longshot sell), 5 contracts at 2c
    _make_trade(ticker="KXSPORTS-NCAAM", source_bot="strategy",
                raw_edge=0.03, cost_cents=10, count=5, price_cents=2,
                sizing_method="half_kelly", timestamp="2026-03-02T09:00:00",
                settlement_result="won", settlement_revenue_cents=500),
]

UNSETTLED_TRADE = _make_trade(settlement_result=None, settlement_revenue_cents=None)


# ── Tests: _compute_pnl_cents ──

class TestComputePnlCents:
    def test_won_with_revenue(self):
        t = _make_trade(cost_cents=200, settlement_result="won",
                        settlement_revenue_cents=400)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == 200

    def test_lost_with_revenue(self):
        t = _make_trade(cost_cents=120, settlement_result="lost",
                        settlement_revenue_cents=0)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == -120

    def test_unsettled_returns_zero(self):
        pnl, settled = _compute_pnl_cents(UNSETTLED_TRADE)
        assert settled is False
        assert pnl == 0

    def test_fallback_won_without_revenue(self):
        t = _make_trade(cost_cents=200, count=4,
                        settlement_result="won", settlement_revenue_cents=None)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == 200  # 100*4 - 200

    def test_fallback_lost_without_revenue(self):
        t = _make_trade(cost_cents=120, count=2,
                        settlement_result="lost", settlement_revenue_cents=None)
        pnl, settled = _compute_pnl_cents(t)
        assert settled is True
        assert pnl == -120


# ── Tests: _classify_market_type ──

class TestClassifyMarketType:
    def test_weather(self):
        assert _classify_market_type("KXHIGHHOU-26MAR03-T75") == "weather"

    def test_crypto_btc(self):
        assert _classify_market_type("KXBTC-26MAR03-T95000") == "crypto"

    def test_crypto_eth(self):
        assert _classify_market_type("KXETH-26MAR03-T3500") == "crypto"

    def test_economics_cpi(self):
        assert _classify_market_type("KXCPI-26MAY-T20") == "economics"

    def test_economics_gdp(self):
        assert _classify_market_type("KXGDP-26Q1-T20") == "economics"

    def test_entertainment(self):
        assert _classify_market_type("KXALBUM-DRAKE-100K") == "entertainment"

    def test_box_office(self):
        assert _classify_market_type("KXBOX-MINECRAFT-200M") == "box_office"

    def test_other(self):
        assert _classify_market_type("KXSPORTS-NCAAM") == "other"


# ── Tests: PnLAttributor ──

class TestAttributeByBot:
    def test_groups_by_source_bot(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_bot()
        assert "weather" in result
        assert "crypto" in result
        assert result["weather"]["trades"] == 2
        assert result["crypto"]["trades"] == 1

    def test_correct_pnl_per_bot(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_bot()
        assert result["weather"]["pnl_cents"] == 80  # 200 - 120
        assert result["crypto"]["pnl_cents"] == 210
        assert result["economics"]["pnl_cents"] == -30

    def test_win_rate(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_bot()
        assert result["weather"]["win_rate"] == 0.5  # 1 win, 1 loss
        assert result["crypto"]["win_rate"] == 1.0

    def test_empty_trades(self):
        attr = PnLAttributor()
        attr.load_trades(trades=[])
        result = attr.attribute_by_bot()
        assert result == {}

    def test_unsettled_excluded(self):
        attr = PnLAttributor()
        attr.load_trades(trades=[UNSETTLED_TRADE])
        result = attr.attribute_by_bot()
        assert result == {}


class TestAttributeByEdgeBucket:
    def test_buckets_assigned_correctly(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_edge_bucket()
        # raw_edge=0.03 → "0-4%", raw_edge=0.05 → "4-8%",
        # raw_edge=0.08 → "8-15%", raw_edge=0.12 → "8-15%", raw_edge=0.20 → "15%+"
        assert "0-4%" in result
        assert result["0-4%"]["trades"] == 1
        assert "4-8%" in result
        assert result["4-8%"]["trades"] == 1
        assert "8-15%" in result
        assert result["8-15%"]["trades"] == 2
        assert "15%+" in result
        assert result["15%+"]["trades"] == 1

    def test_pnl_per_bucket(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_edge_bucket()
        assert result["15%+"]["pnl_cents"] == 210  # crypto won
        assert result["0-4%"]["pnl_cents"] == 490   # strategy won

    def test_unknown_edge_grouped(self):
        trade = _make_trade(raw_edge=None, settlement_result="won",
                            settlement_revenue_cents=400, cost_cents=200)
        attr = PnLAttributor()
        attr.load_trades(trades=[trade])
        result = attr.attribute_by_edge_bucket()
        assert "unknown" in result


class TestAttributeByRegime:
    def test_regime_assignment_from_history(self):
        # Regime history: normal until 2026-03-02, then high_vol
        regime_history = [
            ("2026-03-01T00:00:00", 0.45, "normal"),
            ("2026-03-02T00:00:00", 0.80, "high_vol"),
        ]
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_regime(regime_history=regime_history)
        # 3 trades on 03-01 → normal, 2 trades on 03-02 → high_vol
        assert result["normal"]["trades"] == 3
        assert result["high_vol"]["trades"] == 2

    def test_empty_regime_history(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_regime(regime_history=[])
        assert "unknown" in result
        assert result["unknown"]["trades"] == 5


class TestAttributeBySizing:
    def test_groups_by_sizing_method(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_sizing()
        assert "half_kelly" in result
        assert "quarter_kelly" in result
        assert result["half_kelly"]["trades"] == 4
        assert result["quarter_kelly"]["trades"] == 1


class TestAttributeByMarketType:
    def test_groups_by_ticker_prefix(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        result = attr.attribute_by_market_type()
        assert "weather" in result
        assert result["weather"]["trades"] == 2
        assert "crypto" in result
        assert result["crypto"]["trades"] == 1
        assert "economics" in result


class TestFullReport:
    def test_report_has_all_dimensions(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        report = attr.full_report()
        assert "by_bot" in report
        assert "by_edge_bucket" in report
        assert "by_regime" in report
        assert "by_sizing" in report
        assert "by_market_type" in report
        assert "summary" in report

    def test_summary_totals(self):
        attr = PnLAttributor()
        attr.load_trades(trades=SETTLED_TRADES)
        report = attr.full_report()
        assert report["summary"]["total_trades_settled"] == 5
        # Total P&L: 200 - 120 + 210 - 30 + 490 = 750
        assert report["summary"]["total_pnl_cents"] == 750
