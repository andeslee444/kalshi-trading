"""Tests for offline Book C feed analysis."""

import json

from domain.oracle.book_c_research import (
    evaluate_clutch_comeback_buckets,
    load_processed_game_feeds,
    summarize_book_c_dataset,
    summarize_book_c_parameter_sweep,
)


def test_load_processed_game_feeds_ignores_collection_summary(tmp_path):
    feeds_dir = tmp_path / "game_feeds"
    feeds_dir.mkdir()
    (feeds_dir / "collection_summary.json").write_text(json.dumps({"games_collected": 2}))
    (feeds_dir / "1.json").write_text(json.dumps({"game_id": 1, "game_state_snapshots": []}))
    (feeds_dir / "2.json").write_text(json.dumps({"game_id": 2, "game_state_snapshots": []}))

    feeds = load_processed_game_feeds(feeds_dir)

    assert [feed["game_id"] for feed in feeds] == [1, 2]


def test_summarize_book_c_dataset_ranks_and_flags_data_gaps():
    feeds = [
        {
            "game_id": 1,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 1, "time_remaining": 90},
                {"event": "technical_foul", "margin": 4, "time_remaining": 500},
            ],
        },
        {
            "game_id": 2,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 5, "time_remaining": 60},
                {"event": "blowout_moment", "margin": 24, "time_remaining": 420},
            ],
        },
        {
            "game_id": 3,
            "game_state_snapshots": [
                {"event": "technical_foul", "margin": 2, "time_remaining": 300},
            ],
        },
    ]

    summary = summarize_book_c_dataset(feeds)

    assert summary["total_games"] == 3
    assert summary["top_recommendation"] == "clutch_comeback"
    assert summary["ranked_event_classes"][0]["event_class"] == "clutch_comeback"
    assert summary["ranked_event_classes"][1]["event_class"] == "ot_likely"

    ranked = {row["event_class"]: row for row in summary["ranked_event_classes"]}
    assert ranked["clutch_comeback"]["games_with_signal"] == 2
    assert ranked["clutch_comeback"]["opportunities"] == 2
    assert ranked["ot_likely"]["games_with_signal"] == 1
    assert ranked["blowout"]["games_with_signal"] == 1
    assert ranked["technical_foul"]["games_with_signal"] == 2

    unsupported = {row["event_class"]: row for row in summary["unsupported_event_classes"]}
    assert unsupported["foul_trouble"]["data_ready"] is False
    assert unsupported["scoring_run"]["recommendation"] == "data gap"


def test_evaluate_clutch_comeback_buckets_uses_final_result():
    feeds = [
        {
            "game_id": 1,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 2, "time_remaining": 70, "home_score": 96, "away_score": 98},
                {"event": "period_end", "period": 4, "home_score": 104, "away_score": 101, "margin": 3},
            ],
        },
        {
            "game_id": 2,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 2, "time_remaining": 68, "home_score": 101, "away_score": 103},
                {"event": "period_end", "period": 4, "home_score": 109, "away_score": 111, "margin": 2},
            ],
        },
        {
            "game_id": 3,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 5, "time_remaining": 28, "home_score": 90, "away_score": 95},
                {"event": "period_end", "period": 4, "home_score": 102, "away_score": 108, "margin": 6},
            ],
        },
    ]

    rows = evaluate_clutch_comeback_buckets(feeds)
    keyed = {(row["time_bucket"], row["margin_bucket"]): row for row in rows}

    assert keyed[("2:00-1:00", "2")]["opportunities"] == 2
    assert keyed[("2:00-1:00", "2")]["trailing_wins"] == 1
    assert keyed[("2:00-1:00", "2")]["leader_holds"] == 1
    assert keyed[("2:00-1:00", "2")]["trailing_win_rate"] == 0.5
    assert keyed[("0:30-0:10", "3-5")]["leader_hold_rate"] == 1.0


def test_summarize_book_c_parameter_sweep_ranks_all_three_signals():
    feeds = [
        {
            "game_id": 1,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 1, "time_remaining": 30, "home_score": 98, "away_score": 96},
                {"event": "clutch_moment", "margin": 2, "time_remaining": 60, "home_score": 101, "away_score": 99},
                {"event": "blowout_moment", "margin": 21, "period": 3, "time_remaining": 420},
                {"event": "period_end", "period": 4, "home_score": 110, "away_score": 104},
            ],
        },
        {
            "game_id": 2,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 4, "time_remaining": 60, "home_score": 88, "away_score": 92},
                {"event": "blowout_moment", "margin": 24, "period": 4, "time_remaining": 300},
                {"event": "period_end", "period": 4, "home_score": 96, "away_score": 108},
            ],
        },
        {
            "game_id": 3,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 2, "time_remaining": 20, "home_score": 91, "away_score": 89},
                {"event": "period_end", "period": 4, "home_score": 97, "away_score": 94},
            ],
        },
    ]

    sweep = summarize_book_c_parameter_sweep(feeds)

    assert sweep["total_games"] == 3
    assert set(sweep["sweeps"]) == {"clutch_comeback", "ot_likely", "blowout"}

    clutch_best = sweep["best_parameters"]["clutch_comeback"]
    ot_best = sweep["best_parameters"]["ot_likely"]
    blowout_best = sweep["best_parameters"]["blowout"]

    assert clutch_best["parameters"] == {"max_margin": 4, "max_time_remaining": 60}
    assert ot_best["parameters"] == {"max_margin": 2, "max_time_remaining": 60}
    assert blowout_best["parameters"] == {"min_margin": 20, "min_period": 3}

    assert clutch_best["games_with_signal"] == 3
    assert clutch_best["opportunities"] == 4
    assert ot_best["games_with_signal"] == 2
    assert ot_best["opportunities"] == 3
    assert blowout_best["games_with_signal"] == 2
    assert blowout_best["opportunities"] == 2

    clutch_rows = sweep["sweeps"]["clutch_comeback"]["rows"]
    assert clutch_rows[0]["rank"] == 1
    assert clutch_rows[0]["window"] == "<= 4 pts, <= 60s"
    assert clutch_rows[0]["recommendation"] == "build now"

    blowout_rows = sweep["sweeps"]["blowout"]["rows"]
    assert blowout_rows[0]["window"] == ">= 20 pts, >= Q3"
    assert blowout_rows[0]["rank"] == 1


def test_oracle_book_c_sweep_cli_prints_best_rows(monkeypatch, capsys):
    import sys

    import apps.oracle_book_c_sweep as oracle_book_c_sweep

    feeds = [
        {
            "game_id": 1,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 1, "time_remaining": 30, "home_score": 98, "away_score": 96},
                {"event": "clutch_moment", "margin": 2, "time_remaining": 60, "home_score": 101, "away_score": 99},
                {"event": "blowout_moment", "margin": 21, "period": 3, "time_remaining": 420},
                {"event": "period_end", "period": 4, "home_score": 110, "away_score": 104},
            ],
        },
        {
            "game_id": 2,
            "game_state_snapshots": [
                {"event": "clutch_moment", "margin": 4, "time_remaining": 60, "home_score": 88, "away_score": 92},
                {"event": "blowout_moment", "margin": 24, "period": 4, "time_remaining": 300},
                {"event": "period_end", "period": 4, "home_score": 96, "away_score": 108},
            ],
        },
    ]

    monkeypatch.setattr(oracle_book_c_sweep, "load_processed_game_feeds", lambda _path: feeds)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "oracle-book-c-sweep.py",
            "--format",
            "table",
            "--limit",
            "2",
        ],
    )

    exit_code = oracle_book_c_sweep.main()
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "clutch_comeback: best=max_margin=4, max_time_remaining=60" in output
    assert "ot_likely: best=max_margin=2, max_time_remaining=60" in output
    assert "blowout: best=min_margin=20, min_period=Q3" in output
    assert "<= 4 pts, <= 60s" in output
