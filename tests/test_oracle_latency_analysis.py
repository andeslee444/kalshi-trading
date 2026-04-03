"""Tests for Oracle latency event classification and summarization."""

from domain.oracle.latency_analysis import (
    classify_live_event,
    summarize_alpha_execution_capture,
    summarize_h1_daily_activity,
    summarize_h1_decision,
    summarize_latency_capture,
)
from domain.oracle.real_sports_client import LiveEvent


def test_classify_live_event_blowout_entry():
    event = LiveEvent(
        event_type="GameUpdated",
        game_id=23454,
        data={"homeScore": 120, "awayScore": 95, "period": "Q3", "clock": "4:30"},
    )

    classified = classify_live_event(
        event,
        previous_game_state={
            "game_state": "competitive",
            "home_score": 108,
            "away_score": 98,
            "period": "Q3",
            "clock_seconds": 360,
        },
    )

    assert classified["derived_event_class"] == "blowout_entry"
    assert classified["game_state"] == "blowout"
    assert classified["state_transition"] == "competitive->blowout"
    assert classified["score_margin"] == 25
    assert classified["clock_seconds"] == 270


def test_classify_live_event_foul_trouble_entry():
    event = LiveEvent(
        event_type="LiveFeedSocketPlayersUpdated",
        game_id=23454,
        player_id=77,
        data={"pf": 4, "period": "Q3", "clock": "5:00"},
    )

    classified = classify_live_event(event, previous_player_fouls=3)

    assert classified["derived_event_class"] == "foul_trouble_entry"
    assert classified["player_fouls"] == 4
    assert classified["period"] == "Q3"


def test_classify_live_event_technical_foul_from_play_text():
    event = LiveEvent(
        event_type="LiveFeedSocketPlaysAdded",
        game_id=23454,
        data={"description": "Double technical foul assessed after dead ball", "period": "Q2", "clock": "0:18"},
    )

    classified = classify_live_event(event)

    assert classified["derived_event_class"] == "technical_foul"


def test_classify_live_event_injury_player_out_from_play_text():
    event = LiveEvent(
        event_type="LiveFeedSocketPlaysAdded",
        game_id=23454,
        player_id=77,
        data={
            "description": "LeBron James left the game and will not return due to injury",
            "period": "Q4",
            "clock": "2:10",
        },
    )

    classified = classify_live_event(event)

    assert classified["derived_event_class"] == "injury_player_out"


def test_classify_live_event_injury_player_out_from_status_field():
    event = LiveEvent(
        event_type="PlayerBoxScoreUpdated",
        game_id=23454,
        player_id=77,
        data={"injuryStatus": "Out", "period": "Q3", "clock": "5:00"},
    )

    classified = classify_live_event(event)

    assert classified["derived_event_class"] == "injury_player_out"


def test_classify_live_event_reads_real_play_payload_shape():
    event = LiveEvent(
        event_type="LiveFeedSocketPlaysUpdated",
        game_id=23547,
        player_id=20002880,
        data={
            "sport": "nba",
            "period": 4,
            "homeTeamScore": 102,
            "awayTeamScore": 100,
            "timeRemainingMinutes": 0,
            "timeRemainingSeconds": 45,
            "type": "FieldGoalMade",
        },
    )

    classified = classify_live_event(
        event,
        previous_game_state={
            "game_state": "competitive",
            "home_score": 98,
            "away_score": 97,
            "period": "Q4",
            "clock_seconds": 75,
        },
    )

    assert classified["derived_event_class"] == "ot_likely_entry"
    assert classified["home_score"] == 102
    assert classified["away_score"] == 100
    assert classified["clock_seconds"] == 45
    assert classified["period"] == "Q4"


def test_summarize_latency_capture_groups_by_derived_event_class():
    source_rows = [
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-1",
            "derived_event_class": "foul_trouble_entry",
            "game_id": 23454,
            "player_id": 77,
        },
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-2",
            "derived_event_class": "technical_foul",
            "game_id": 23454,
        },
    ]
    quote_rows = [
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "derived_event_class": "foul_trouble_entry",
            "ticker": "T1",
            "source_to_quote_ms": 1800,
            "midpoint_change_cents": -4,
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "derived_event_class": "foul_trouble_entry",
            "ticker": "T2",
            "source_to_quote_ms": 2300,
            "midpoint_change_cents": 0,
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-2",
            "derived_event_class": "technical_foul",
            "ticker": "T3",
            "source_to_quote_ms": 900,
            "midpoint_change_cents": 2,
        },
    ]

    summary = summarize_latency_capture(
        source_rows,
        quote_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    foul_summary = summary["by_event_class"]["foul_trouble_entry"]
    technical_summary = summary["by_event_class"]["technical_foul"]

    assert summary["source_events"] == 2
    assert summary["quote_snapshots"] == 3
    assert foul_summary["source_events"] == 1
    assert foul_summary["quote_snapshots"] == 2
    assert foul_summary["median_latency_ms"] == 2050
    assert foul_summary["moved_quote_snapshots"] == 1
    assert foul_summary["moved_quote_rate"] == 0.5
    assert technical_summary["median_abs_midpoint_change_cents"] == 2


def test_summarize_latency_capture_pairs_followup_markouts_by_horizon():
    source_rows = [
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-1",
            "derived_event_class": "clutch_entry",
            "game_id": 23454,
        },
    ]
    quote_rows = [
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "derived_event_class": "clutch_entry",
            "ticker": "T1",
            "horizon_seconds": 0.0,
            "source_to_quote_ms": 700,
            "midpoint_change_cents": 0,
            "midpoint_cents": 62,
            "spread_cents": 4,
            "yes_bid_cents": 60,
            "yes_ask_cents": 64,
            "yes_bid_depth": 6,
            "yes_ask_depth": 8,
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "derived_event_class": "clutch_entry",
            "ticker": "T1",
            "horizon_seconds": 3.0,
            "source_to_quote_ms": 3700,
            "midpoint_change_cents": 5,
            "midpoint_cents": 67,
            "spread_cents": 4,
            "yes_bid_cents": 65,
            "yes_ask_cents": 69,
            "yes_bid_depth": 7,
            "yes_ask_depth": 8,
        },
    ]

    summary = summarize_latency_capture(
        source_rows,
        quote_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
        max_spread_cents=8,
        min_depth_contracts=5,
    )

    clutch_summary = summary["by_event_class"]["clutch_entry"]
    immediate = clutch_summary["by_horizon"][0.0]
    later = clutch_summary["by_horizon"][3.0]

    assert summary["quoted_source_events"] == 1
    assert summary["capture_rate"] == 1.0
    assert clutch_summary["capture_rate"] == 1.0
    assert immediate["displayed_any_fillable_rate"] == 1.0
    assert later["paired_snapshots"] == 1
    assert later["paired_fillable_opportunities"] == 1
    assert later["median_abs_midpoint_change_from_initial_cents"] == 5
    assert later["median_best_markout_cents"] == 1
    assert later["mean_best_markout_cents"] == 1.0
    assert later["positive_best_markout_rate"] == 1.0
    assert later["bootstrap_mean_best_markout_cents"] == {
        "mean": 1.0,
        "ci_low": 1.0,
        "ci_high": 1.0,
        "sample_size": 1,
        "cluster_count": 1,
        "bootstrap_samples": 500,
    }
    assert later["bootstrap_positive_best_markout_rate"] == {
        "rate": 1.0,
        "ci_low": 1.0,
        "ci_high": 1.0,
        "sample_size": 1,
        "cluster_count": 1,
        "bootstrap_samples": 500,
    }
    assert later["proof_checks"] == {
        "stage1_research_signal_target_met": False,
        "stage2_shadow_trade_target_met_proxy": False,
        "bootstrap_mean_best_markout_ci_above_zero": True,
    }
    assert summary["paired_fillable_opportunities"] == 1
    assert summary["bootstrap_mean_best_markout_cents"] == {
        "mean": 1.0,
        "ci_low": 1.0,
        "ci_high": 1.0,
        "sample_size": 1,
        "cluster_count": 1,
        "bootstrap_samples": 500,
    }
    assert summary["proof_checks"] == {
        "stage1_research_signal_target_met": False,
        "stage2_shadow_trade_target_met_proxy": False,
        "bootstrap_mean_best_markout_ci_above_zero": True,
    }


def test_summarize_latency_capture_tracks_game_vs_prop_event_capture():
    source_rows = [
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-1",
            "derived_event_class": "foul_trouble_entry",
            "game_id": 23454,
            "player_id": 30,
            "mapped_game_tickers": ["KXNBAGAME-26MAR21GSWATL-ATL"],
            "mapped_prop_tickers": ["KXNBAPTS-26MAR21GSWATL-GSWCURRY30-30"],
        },
    ]
    quote_rows = [
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "ticker": "KXNBAGAME-26MAR21GSWATL-ATL",
            "capture_mode": "event_immediate",
            "horizon_seconds": 0.0,
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "ticker": "KXNBAPTS-26MAR21GSWATL-GSWCURRY30-30",
            "capture_mode": "event_immediate",
            "horizon_seconds": 0.0,
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "ticker": "KXNBAPTS-26MAR21GSWATL-GSWCURRY30-30",
            "capture_mode": "event_followup",
            "horizon_seconds": 3.0,
        },
    ]

    summary = summarize_latency_capture(
        source_rows,
        quote_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    game_capture = summary["by_market_type"]["game"]
    prop_capture = summary["by_market_type"]["prop"]

    assert game_capture["source_events"] == 1
    assert game_capture["quoted_source_events"] == 1
    assert game_capture["quote_snapshots"] == 1
    assert game_capture["event_immediate_quote_snapshots"] == 1
    assert game_capture["event_followup_quote_snapshots"] == 0
    assert prop_capture["source_events"] == 1
    assert prop_capture["quoted_source_events"] == 1
    assert prop_capture["quote_snapshots"] == 2
    assert prop_capture["event_immediate_quote_snapshots"] == 1
    assert prop_capture["event_followup_quote_snapshots"] == 1


def test_summarize_latency_capture_proof_checks_reflect_sample_and_ci_strength():
    source_rows = [
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": f"source-{index}",
            "derived_event_class": "ot_likely_entry",
            "game_id": 30000 + index,
        }
        for index in range(200)
    ]
    quote_rows = []
    for index in range(200):
        source_event_id = f"source-{index}"
        ticker = f"T{index}"
        quote_rows.append(
            {
                "record_kind": "quote_snapshot",
                "hypothesis_id": "H1_real_to_kalshi_latency",
                "source_event_id": source_event_id,
                "derived_event_class": "ot_likely_entry",
                "ticker": ticker,
                "horizon_seconds": 0.0,
                "source_to_quote_ms": 600 + index,
                "midpoint_change_cents": 0,
                "midpoint_cents": 50,
                "spread_cents": 4,
                "yes_bid_cents": 48,
                "yes_ask_cents": 52,
                "yes_bid_depth": 7,
                "yes_ask_depth": 7,
            }
        )
        quote_rows.append(
            {
                "record_kind": "quote_snapshot",
                "hypothesis_id": "H1_real_to_kalshi_latency",
                "source_event_id": source_event_id,
                "derived_event_class": "ot_likely_entry",
                "ticker": ticker,
                "horizon_seconds": 3.0,
                "source_to_quote_ms": 3600 + index,
                "midpoint_change_cents": 4,
                "midpoint_cents": 54,
                "spread_cents": 4,
                "yes_bid_cents": 54,
                "yes_ask_cents": 56,
                "yes_bid_depth": 6,
                "yes_ask_depth": 6,
            }
        )

    summary = summarize_latency_capture(
        source_rows,
        quote_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
        max_spread_cents=8,
        min_depth_contracts=5,
    )

    ot_summary = summary["by_event_class"]["ot_likely_entry"]["by_horizon"][3.0]

    assert summary["source_events"] == 200
    assert summary["paired_fillable_opportunities"] == 200
    assert summary["proof_checks"] == {
        "stage1_research_signal_target_met": True,
        "stage2_shadow_trade_target_met_proxy": True,
        "bootstrap_mean_best_markout_ci_above_zero": True,
    }
    assert ot_summary["paired_fillable_opportunities"] == 200
    assert ot_summary["mean_best_markout_cents"] == 2.0
    assert ot_summary["positive_best_markout_rate"] == 1.0
    assert ot_summary["bootstrap_mean_best_markout_cents"]["mean"] == 2.0
    assert ot_summary["bootstrap_mean_best_markout_cents"]["ci_low"] == 2.0
    assert ot_summary["bootstrap_mean_best_markout_cents"]["ci_high"] == 2.0
    assert ot_summary["bootstrap_mean_best_markout_cents"]["cluster_count"] == 200
    assert ot_summary["bootstrap_positive_best_markout_rate"]["rate"] == 1.0
    assert ot_summary["bootstrap_positive_best_markout_rate"]["cluster_count"] == 200
    assert ot_summary["proof_checks"] == {
        "stage1_research_signal_target_met": True,
        "stage2_shadow_trade_target_met_proxy": True,
        "bootstrap_mean_best_markout_ci_above_zero": True,
    }
    assert summary["ranked_horizon_rows"][0]["event_class"] == "ot_likely_entry"
    assert summary["ranked_horizon_rows"][0]["horizon_seconds"] == 3.0


def test_summarize_latency_capture_uses_clustered_bootstrap_for_repeated_game_observations():
    source_rows = [
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-1",
            "derived_event_class": "technical_foul",
            "game_id": 7001,
        },
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-2",
            "derived_event_class": "technical_foul",
            "game_id": 7001,
        },
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-3",
            "derived_event_class": "technical_foul",
            "game_id": 7002,
        },
    ]
    quote_rows = []
    for source_event_id, ticker, current_yes_bid in (
        ("source-1", "T1", 62),
        ("source-2", "T2", 62),
        ("source-3", "T3", 48),
    ):
        quote_rows.append(
            {
                "record_kind": "quote_snapshot",
                "hypothesis_id": "H1_real_to_kalshi_latency",
                "source_event_id": source_event_id,
                "derived_event_class": "technical_foul",
                "ticker": ticker,
                "horizon_seconds": 0.0,
                "source_to_quote_ms": 500,
                "midpoint_change_cents": 0,
                "midpoint_cents": 60,
                "spread_cents": 4,
                "yes_bid_cents": 58,
                "yes_ask_cents": 60,
                "yes_bid_depth": 8,
                "yes_ask_depth": 8,
            }
        )
        quote_rows.append(
            {
                "record_kind": "quote_snapshot",
                "hypothesis_id": "H1_real_to_kalshi_latency",
                "source_event_id": source_event_id,
                "derived_event_class": "technical_foul",
                "ticker": ticker,
                "horizon_seconds": 3.0,
                "source_to_quote_ms": 3500,
                "midpoint_change_cents": 2,
                "midpoint_cents": current_yes_bid + 1,
                "spread_cents": 4,
                "yes_bid_cents": current_yes_bid,
                "yes_ask_cents": current_yes_bid + 2,
                "yes_bid_depth": 8,
                "yes_ask_depth": 8,
            }
        )

    summary = summarize_latency_capture(
        source_rows,
        quote_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
        max_spread_cents=8,
        min_depth_contracts=5,
    )

    horizon = summary["by_event_class"]["technical_foul"]["by_horizon"][3.0]

    assert horizon["paired_fillable_opportunities"] == 3
    assert horizon["bootstrap_mean_best_markout_cents"]["sample_size"] == 3
    assert horizon["bootstrap_mean_best_markout_cents"]["cluster_count"] == 2
    assert horizon["bootstrap_positive_best_markout_rate"]["sample_size"] == 3
    assert horizon["bootstrap_positive_best_markout_rate"]["cluster_count"] == 2


def test_summarize_latency_capture_does_not_double_count_overall_fillable_opportunities_across_horizons():
    source_rows = [
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "event_id": "source-1",
            "derived_event_class": "clutch_entry",
            "game_id": 8801,
        },
    ]
    quote_rows = [
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "derived_event_class": "clutch_entry",
            "ticker": "T1",
            "horizon_seconds": 0.0,
            "source_to_quote_ms": 500,
            "midpoint_change_cents": 0,
            "midpoint_cents": 60,
            "spread_cents": 4,
            "yes_bid_cents": 58,
            "yes_ask_cents": 60,
            "yes_bid_depth": 8,
            "yes_ask_depth": 8,
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "derived_event_class": "clutch_entry",
            "ticker": "T1",
            "horizon_seconds": 1.0,
            "source_to_quote_ms": 1500,
            "midpoint_change_cents": 2,
            "midpoint_cents": 62,
            "spread_cents": 4,
            "yes_bid_cents": 61,
            "yes_ask_cents": 63,
            "yes_bid_depth": 8,
            "yes_ask_depth": 8,
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "source_event_id": "source-1",
            "derived_event_class": "clutch_entry",
            "ticker": "T1",
            "horizon_seconds": 3.0,
            "source_to_quote_ms": 3500,
            "midpoint_change_cents": 4,
            "midpoint_cents": 64,
            "spread_cents": 4,
            "yes_bid_cents": 63,
            "yes_ask_cents": 65,
            "yes_bid_depth": 8,
            "yes_ask_depth": 8,
        },
    ]

    summary = summarize_latency_capture(
        source_rows,
        quote_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
        max_spread_cents=8,
        min_depth_contracts=5,
    )

    horizons = summary["by_event_class"]["clutch_entry"]["by_horizon"]

    assert horizons[1.0]["paired_fillable_opportunities"] == 1
    assert horizons[3.0]["paired_fillable_opportunities"] == 1
    assert summary["paired_fillable_opportunities"] == 1
    assert summary["bootstrap_mean_best_markout_cents"]["sample_size"] == 1
    assert summary["bootstrap_mean_best_markout_cents"]["cluster_count"] == 1


def test_summarize_alpha_execution_capture_handles_no_fills_yet():
    signal_rows = [
        {
            "record_kind": "signal",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "signal_timestamp_utc": "2026-03-19T12:00:00+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "model_prob": 0.63,
            "market_prob": 0.54,
            "entry_price": 0.61,
            "expected_fill_probability": 0.42,
        }
    ]

    summary = summarize_alpha_execution_capture(
        signal_rows,
        [],
        [],
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    clutch = summary["by_signal_type"]["clutch_comeback"]

    assert summary["signal_rows"] == 1
    assert summary["order_rows"] == 0
    assert summary["fill_rows"] == 0
    assert summary["no_fills_yet"] is True
    assert summary["order_fill_rate"] is None
    assert summary["median_time_to_fill_seconds"] is None
    assert clutch["signal_rows"] == 1
    assert clutch["order_fill_rate"] is None
    assert clutch["expected_fill_probability_mean"] == 0.42
    assert summary["execution_summary"]["pass_fail_status"] == "insufficient_close_data"
    assert summary["execution_summary"]["no_settlement_yet"] is True
    assert summary["execution_summary"]["no_close_yet"] is True


def test_summarize_alpha_execution_capture_links_signal_order_and_fill():
    signal_rows = [
        {
            "record_kind": "signal",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "signal_timestamp_utc": "2026-03-19T12:00:00+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "model_prob": 0.63,
            "market_prob": 0.54,
            "entry_price": 0.61,
            "expected_fill_probability": 0.42,
        }
    ]
    order_rows = [
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "timestamp": "2026-03-19T12:00:02+00:00",
            "order_timestamp_utc": "2026-03-19T12:00:02+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "price_cents": 61,
            "count": 2,
        }
    ]
    fill_rows = [
        {
            "record_kind": "fill",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "fill_timestamp_utc": "2026-03-19T12:00:08+00:00",
            "timestamp": "2026-03-19T12:00:08+00:00",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 62,
            "fill_count": 2,
        }
    ]

    summary = summarize_alpha_execution_capture(
        signal_rows,
        order_rows,
        fill_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    clutch = summary["by_signal_type"]["clutch_comeback"]

    assert summary["signal_rows"] == 1
    assert summary["order_rows"] == 1
    assert summary["fill_rows"] == 1
    assert summary["signals_with_order"] == 1
    assert summary["signals_with_filled_order"] == 1
    assert summary["order_fill_rate"] == 1.0
    assert summary["signal_fill_rate"] == 1.0
    assert summary["mean_time_to_fill_seconds"] == 6.0
    assert summary["median_time_to_fill_seconds"] == 6.0
    assert summary["p75_time_to_fill_seconds"] == 6.0
    assert summary["expected_fill_probability_mean"] == 0.42
    assert clutch["order_fill_rate"] == 1.0
    assert clutch["median_time_to_fill_seconds"] == 6.0


def test_summarize_alpha_execution_capture_includes_settlement_summary_and_missing_fee_data():
    signal_rows = [
        {
            "record_kind": "signal",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "signal_timestamp_utc": "2026-03-19T12:00:00+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "model_prob": 0.63,
            "market_prob": 0.54,
            "entry_price": 0.61,
        }
    ]
    order_rows = [
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "timestamp": "2026-03-19T12:00:02+00:00",
            "order_timestamp_utc": "2026-03-19T12:00:02+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "price_cents": 61,
            "count": 2,
        }
    ]
    fill_rows = [
        {
            "record_kind": "fill",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "fill_timestamp_utc": "2026-03-19T12:00:08+00:00",
            "timestamp": "2026-03-19T12:00:08+00:00",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 61,
            "fill_count": 2,
        }
    ]
    settlement_rows = [
        {
            "record_kind": "settlement",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 61,
            "fill_count": 2,
            "settlement_revenue_cents": 200,
            "close_price_cents": 64,
            "settlement_result": "won",
        }
    ]

    summary = summarize_alpha_execution_capture(
        signal_rows,
        order_rows,
        fill_rows,
        settlement_rows=settlement_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    execution = summary["execution_summary"]
    clutch = summary["by_signal_type"]["clutch_comeback"]

    assert execution["settlement_rows"] == 1
    assert execution["has_settlement_data"] is True
    assert execution["has_close_data"] is True
    assert execution["realized_gross_pnl_cents"] == 78
    assert execution["realized_clv_cents"] == 6
    assert execution["realized_gross_ev_per_contract_cents"] == 39.0
    assert execution["pass_fail_status"] == "insufficient_fee_data"
    assert execution["pass_fail_reason"] == "no_fee_data"
    assert clutch["settlement_rows"] == 1
    assert clutch["realized_gross_pnl_cents"] == 78
    assert clutch["realized_clv_cents"] == 6
    assert clutch["pass_fail_status"] == "insufficient_fee_data"


def test_summarize_alpha_execution_capture_can_ignore_unlinked_execution_rows():
    signal_rows = [
        {
            "record_kind": "signal",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "signal_timestamp_utc": "2026-03-19T12:00:00+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "model_prob": 0.63,
            "market_prob": 0.54,
            "entry_price": 0.61,
        }
    ]
    order_rows = [
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "timestamp": "2026-03-19T12:00:02+00:00",
            "order_timestamp_utc": "2026-03-19T12:00:02+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "price_cents": 61,
            "count": 2,
        },
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": None,
            "order_id": "order-2",
            "timestamp": "2026-03-19T12:10:02+00:00",
            "order_timestamp_utc": "2026-03-19T12:10:02+00:00",
            "book": "C",
            "signal_type": "unclassified",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "price_cents": 60,
            "count": 1,
        },
    ]
    fill_rows = [
        {
            "record_kind": "fill",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "fill_timestamp_utc": "2026-03-19T12:00:08+00:00",
            "timestamp": "2026-03-19T12:00:08+00:00",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 62,
            "fill_count": 2,
        },
        {
            "record_kind": "fill",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": None,
            "order_id": "order-2",
            "fill_timestamp_utc": "2026-03-19T12:10:10+00:00",
            "timestamp": "2026-03-19T12:10:10+00:00",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 60,
            "fill_count": 1,
        },
    ]
    settlement_rows = [
        {
            "record_kind": "settlement",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 62,
            "fill_count": 2,
            "settlement_revenue_cents": 200,
            "fee_cents": 5,
            "close_price_cents": 100,
            "settlement_result": "win",
        },
        {
            "record_kind": "settlement",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": None,
            "order_id": "order-2",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 60,
            "fill_count": 1,
            "settlement_revenue_cents": 0,
            "fee_cents": 2,
            "close_price_cents": 0,
            "settlement_result": "loss",
        },
    ]

    summary = summarize_alpha_execution_capture(
        signal_rows,
        order_rows,
        fill_rows,
        settlement_rows=settlement_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
        require_signal_link=True,
    )

    assert summary["signal_rows"] == 1
    assert summary["order_rows"] == 1
    assert summary["fill_rows"] == 1
    assert summary["execution_summary"]["settlement_rows"] == 1
    assert summary["execution_summary"]["realized_net_pnl_cents"] == 71


def test_summarize_alpha_execution_capture_handles_multiple_orders_per_signal():
    signal_rows = [
        {
            "record_kind": "signal",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "signal_timestamp_utc": "2026-03-19T12:00:00+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "model_prob": 0.63,
            "market_prob": 0.54,
            "entry_price": 0.61,
        }
    ]
    order_rows = [
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "timestamp": "2026-03-19T12:00:02+00:00",
            "order_timestamp_utc": "2026-03-19T12:00:02+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "price_cents": 61,
            "count": 2,
            "expected_fill_probability": 0.40,
        },
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-2",
            "timestamp": "2026-03-19T12:00:05+00:00",
            "order_timestamp_utc": "2026-03-19T12:00:05+00:00",
            "book": "C",
            "signal_type": "clutch_comeback",
            "ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "price_cents": 62,
            "count": 2,
            "expected_fill_probability": 0.20,
        },
    ]
    fill_rows = [
        {
            "record_kind": "fill",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_id": "signal-1",
            "order_id": "order-1",
            "fill_timestamp_utc": "2026-03-19T12:00:07+00:00",
            "timestamp": "2026-03-19T12:00:07+00:00",
            "market_ticker": "KXNBA-18MAR26-LALHOU-LAL",
            "side": "yes",
            "fill_price_cents": 62,
            "fill_count": 1,
        }
    ]

    summary = summarize_alpha_execution_capture(
        signal_rows,
        order_rows,
        fill_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    clutch = summary["by_signal_type"]["clutch_comeback"]

    assert summary["signals_with_order"] == 1
    assert summary["signals_with_filled_order"] == 1
    assert summary["order_fill_rate"] == 0.5
    assert summary["signal_fill_rate"] == 1.0
    assert summary["expected_fill_probability_mean"] == 0.3
    assert clutch["signal_to_order_rate"] == 1.0
    assert clutch["signal_fill_rate"] == 1.0
    assert clutch["order_fill_rate"] == 0.5
    assert clutch["expected_fill_probability_mean"] == 0.3


def test_summarize_h1_daily_activity_uses_oracle_payload_timestamps_and_realized_outcomes():
    source_rows = [
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "observed_at": "2026-03-19T12:00:00+00:00",
        },
        {
            "record_kind": "source_event",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "observed_at": "2026-03-20T12:00:00+00:00",
        },
    ]
    quote_rows = [
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "quote_timestamp_utc": "2026-03-19T12:00:01+00:00",
        },
        {
            "record_kind": "quote_snapshot",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "quote_timestamp_utc": "2026-03-20T12:00:01+00:00",
        },
    ]
    signal_rows = [
        {
            "record_kind": "signal",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_timestamp_utc": "2026-03-19T12:00:02+00:00",
        },
        {
            "record_kind": "signal",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "signal_timestamp_utc": "2026-03-20T12:00:02+00:00",
        },
    ]
    order_rows = [
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "order_timestamp_utc": "2026-03-19T12:00:03+00:00",
        },
        {
            "record_kind": "order_submission",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "order_timestamp_utc": "2026-03-20T12:00:03+00:00",
        },
    ]
    fill_rows = [
        {
            "record_kind": "fill",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "fill_timestamp_utc": "2026-03-19T12:00:05+00:00",
        },
        {
            "record_kind": "fill",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "fill_timestamp_utc": "2026-03-20T12:00:05+00:00",
        },
    ]
    settlement_rows = [
        {
            "record_kind": "settlement",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "settlement_timestamp_utc": "2026-03-19T12:01:00+00:00",
            "side": "yes",
            "fill_price_cents": 60,
            "fill_count": 2,
            "settlement_revenue_cents": 200,
            "fee_cents": 4,
            "close_price_cents": 100,
        },
        {
            "record_kind": "settlement",
            "hypothesis_id": "H1_real_to_kalshi_latency",
            "settlement_timestamp_utc": "2026-03-20T12:01:00+00:00",
            "side": "yes",
            "fill_price_cents": 70,
            "fill_count": 1,
            "settlement_revenue_cents": 0,
            "fee_cents": 2,
            "close_price_cents": 0,
        },
    ]

    summary = summarize_h1_daily_activity(
        source_rows,
        quote_rows,
        signal_rows,
        order_rows,
        fill_rows,
        settlement_rows,
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    assert summary["days"] == 2
    assert summary["settled_days"] == 2
    assert summary["positive_net_pnl_days"] == 1
    assert summary["positive_clv_days"] == 1
    assert summary["positive_net_pnl_day_share"] == 0.5
    assert summary["positive_clv_day_share"] == 0.5
    assert summary["rows"][0] == {
        "date": "2026-03-19",
        "source_events": 1,
        "quote_snapshots": 1,
        "signals": 1,
        "orders": 1,
        "fills": 1,
        "settlements": 1,
        "settled_contracts": 2,
        "realized_net_pnl_cents": 76,
        "realized_clv_cents": 80,
        "capture_rate": 1.0,
        "order_fill_rate_proxy": 1.0,
    }
    assert summary["rows"][1]["date"] == "2026-03-20"
    assert summary["rows"][1]["realized_net_pnl_cents"] == -72
    assert summary["rows"][1]["realized_clv_cents"] == -70


def test_summarize_h1_daily_activity_excludes_source_failures_from_source_event_counts():
    summary = summarize_h1_daily_activity(
        source_rows=[
            {
                "record_kind": "source_event",
                "hypothesis_id": "H1_real_to_kalshi_latency",
                "observed_at": "2026-03-29T12:00:00+00:00",
            },
            {
                "record_kind": "source_failure",
                "hypothesis_id": "H1_real_to_kalshi_latency",
                "observed_at": "2026-03-29T12:00:30+00:00",
                "failure_code": "empty_crowd_price_response",
            },
        ],
        quote_rows=[],
        signal_rows=[],
        order_rows=[],
        fill_rows=[],
        settlement_rows=[],
        hypothesis_id="H1_real_to_kalshi_latency",
    )

    assert summary["days"] == 1
    assert summary["rows"][0]["date"] == "2026-03-29"
    assert summary["rows"][0]["source_events"] == 1


def test_summarize_h1_decision_reports_pass_when_required_gates_are_met():
    summary = summarize_h1_decision(
        latency_summary={
            "source_events": 240,
            "paired_fillable_opportunities": 140,
            "ranked_event_classes": [
                {
                    "event_class": "technical_foul",
                    "median_latency_ms": 4100,
                    "p75_latency_ms": 6200,
                    "source_events": 80,
                }
            ],
        },
        execution_summary={
            "order_fill_rate": 0.31,
            "has_settlement_data": True,
            "has_close_data": True,
            "realized_net_pnl_cents": 420,
            "realized_clv_cents": 390,
            "bootstrap_net_ev_per_signal_cents": {
                "mean": 3.5,
                "ci_low": 1.1,
                "ci_high": 5.7,
                "sample_size": 110,
            },
            "bootstrap_clv_per_signal_cents": {
                "mean": 2.8,
                "ci_low": 0.9,
                "ci_high": 4.4,
                "sample_size": 110,
            },
        },
        daily_summary={
            "positive_clv_day_share": 0.667,
        },
    )

    assert summary["status"] == "pass"
    assert summary["reasons"] == ["all_required_criteria_met"]
    assert summary["best_latency_event_class"] == "technical_foul"
    assert summary["criteria"] == {
        "latency_gate_any_event_class": True,
        "labeled_opportunities_gte_200": True,
        "fillable_pairs_gte_100": True,
        "fill_rate_gte_20pct": True,
        "net_ev_bootstrap_ci_above_zero": True,
        "clv_bootstrap_ci_above_zero": True,
        "positive_clv_day_share_gte_60pct": True,
    }
