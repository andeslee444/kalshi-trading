"""Direct tests for the research.missed_trade_analysis module."""

import json

from research.missed_trade_analysis import MissedTradeAnalyzer, _load_opportunities_safe


def _sample_records():
    return [
        {
            "timestamp": "2026-03-16T05:00:00+00:00",
            "ticker": "KXHIGHMIA-26MAR16-T86",
            "source_bot": "weather",
            "action": "skipped",
            "reason": "edge_below_min",
            "side": "yes",
            "edge": 0.11,
            "price_cents": 42,
            "opportunity_stage": "decision",
            "model_name": "weather_parametric_ensemble",
            "feature_snapshot_id": "weather:abc123",
        },
        {
            "timestamp": "2026-03-16T05:01:00+00:00",
            "ticker": "KXBTC-26MAR16-T85000",
            "source_bot": "crypto",
            "action": "rejected",
            "reason": "allocator_denied",
            "side": "yes",
            "edge": 0.08,
            "price_cents": 25,
            "opportunity_stage": "decision",
        },
        {
            "timestamp": "2026-03-16T05:02:00+00:00",
            "ticker": "KXCPI-26APR-T3.0",
            "source_bot": "economics",
            "action": "pruned",
            "reason": "selection_pruned",
            "side": "yes",
            "edge": 0.09,
            "price_cents": 18,
            "opportunity_stage": "selection",
        },
        {
            "timestamp": "2026-03-16T05:03:00+00:00",
            "ticker": "KXALBUM-DRAKE-120K",
            "source_bot": "entertainment",
            "action": "skipped",
            "reason": "illiquid_no_ask",
            "side": "yes",
            "edge": 0.15,
            "price_cents": None,
            "opportunity_stage": "decision",
        },
        {
            "timestamp": "2026-03-16T05:04:00+00:00",
            "ticker": "KXHIGHHOU-26MAR16-T88",
            "source_bot": "source-monitor",
            "action": "skipped",
            "reason": "low_edge",
            "side": "no",
            "edge": -0.02,
            "price_cents": 37,
            "opportunity_stage": "decision",
        },
        {
            "timestamp": "2026-03-16T05:05:00+00:00",
            "ticker": "KXETH-26MAR16-T3200",
            "source_bot": "crypto",
            "action": "placed",
            "reason": "executed",
            "side": "yes",
            "edge": 0.21,
            "price_cents": 41,
            "opportunity_stage": "decision",
        },
    ]


def test_load_opportunities_safe_handles_missing_and_valid_files(tmp_path):
    path = tmp_path / "opportunity-log.json"
    path.write_text(json.dumps(_sample_records()))

    assert _load_opportunities_safe(tmp_path / "missing.json") == []
    assert len(_load_opportunities_safe(path)) == 6


def test_reason_distribution_groups_by_bot_and_reason():
    analyzer = MissedTradeAnalyzer()
    analyzer.load_opportunities(records=_sample_records())

    dist = analyzer.reason_distribution()

    assert dist["weather"]["edge_below_min"] == 1
    assert dist["crypto"]["allocator_denied"] == 1
    assert dist["economics"]["selection_pruned"] == 1
    assert "placed" not in dist.get("crypto", {})


def test_stage_distribution_counts_decision_and_selection():
    analyzer = MissedTradeAnalyzer()
    analyzer.load_opportunities(records=_sample_records())

    stages = analyzer.stage_distribution()

    assert stages["decision"] == 4
    assert stages["selection"] == 1


def test_top_missed_trades_sorts_by_estimated_pnl_and_excludes_unpriced_or_nonpositive():
    analyzer = MissedTradeAnalyzer()
    analyzer.load_opportunities(records=_sample_records())

    rows = analyzer.top_missed_trades()

    assert [row["ticker"] for row in rows] == [
        "KXCPI-26APR-T3.0",
        "KXHIGHMIA-26MAR16-T86",
        "KXBTC-26MAR16-T85000",
    ]
    assert rows[0]["estimated_pnl_per_contract_cents"] == 7.38
    assert rows[1]["model_name"] == "weather_parametric_ensemble"
    assert "KXALBUM-DRAKE-120K" not in [row["ticker"] for row in rows]
    assert "KXHIGHHOU-26MAR16-T88" not in [row["ticker"] for row in rows]


def test_full_report_and_summary_report_include_expected_sections():
    analyzer = MissedTradeAnalyzer()
    analyzer.load_opportunities(records=_sample_records())

    report = analyzer.full_report(limit=2, min_edge=0.05)
    summary = analyzer.summary_report(limit=2, min_edge=0.05)

    assert report["summary"]["total_missed_opportunities"] == 5
    assert report["summary"]["positive_edge_opportunities"] == 4
    assert len(report["top_missed_trades"]) == 2
    assert "MISSED TRADE REPORT" in summary
    assert "TOP MISSED TRADES" in summary
    assert "weather" in summary
