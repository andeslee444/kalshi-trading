"""Regression tests for Oracle research scripts."""

from __future__ import annotations

import importlib.util
import json
import datetime as dt
from pathlib import Path

from domain.oracle.alpha_capture import OracleAlphaCapture


def _load_script_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_h8_analyze_policy_uses_zero_ev_for_unfilled_orders():
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "oracle-h8-maker-analysis.py"
    h8 = _load_script_module("oracle_h8_script", script_path)

    opps = [
        {
            "game_id": "G1",
            "passive_yes_attempt_ev": 0.0,
            "passive_yes_filled": False,
            "passive_yes_markout": 2.0,
        },
        {
            "game_id": "G2",
            "passive_yes_attempt_ev": 1.5,
            "passive_yes_filled": True,
            "passive_yes_markout": 1.5,
        },
    ]

    result = h8.analyze_policy(
        opps,
        label="passive_yes",
        attempt_key="passive_yes_attempt_ev",
        fill_key="passive_yes_filled",
        markout_key="passive_yes_markout",
    )

    assert result["fill_rate"] == 0.5
    assert result["attempt_ev_mean"] == 0.75
    assert result["filled_markout_mean"] == 1.5


def test_h6_signal_loader_handles_wrapped_payload_and_kills_catastrophic_no_side(tmp_path):
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "oracle-h6-prop-analysis.py"
    h6 = _load_script_module("oracle_h6_script", script_path)

    signals_path = tmp_path / "signals.json"
    results_dir = tmp_path / "signal_results"
    results_dir.mkdir()
    results_path = results_dir / "results_20260402.json"
    results_tsv = tmp_path / "results.tsv"

    signals_path.write_text(json.dumps({"signals": [{"id": 1}, {"id": 2}, {"id": 3}], "count": 3}))
    results_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "stat": "points",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.30,
                        "model_prob": 0.90,
                    },
                    {
                        "stat": "points",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.25,
                        "model_prob": 0.88,
                    },
                    {
                        "stat": "rebounds",
                        "side": "YES",
                        "won": True,
                        "net_profit": 0.4,
                        "cost": 0.6,
                        "edge": 0.08,
                        "model_prob": 0.58,
                    },
                    {
                        "stat": "assists",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.20,
                        "model_prob": 0.80,
                    },
                    {
                        "stat": "blocks",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.18,
                        "model_prob": 0.78,
                    },
                    {
                        "stat": "steals",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.16,
                        "model_prob": 0.76,
                    },
                    {
                        "stat": "three_pointers",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.15,
                        "model_prob": 0.74,
                    },
                    {
                        "stat": "points",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.22,
                        "model_prob": 0.82,
                    },
                    {
                        "stat": "points",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.24,
                        "model_prob": 0.84,
                    },
                    {
                        "stat": "points",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.26,
                        "model_prob": 0.86,
                    },
                    {
                        "stat": "points",
                        "side": "NO",
                        "won": False,
                        "net_profit": -1.0,
                        "cost": 1.0,
                        "edge": 0.28,
                        "model_prob": 0.89,
                    },
                ]
            }
        )
    )
    results_tsv.write_text("commit\tbrier_score\tcalibration_error\texpected_profit_pct\tsample_size\ttrades_taken\tdescription\n")

    h6.SIGNALS_PATH = signals_path
    h6.RESULTS_DIR = results_dir
    h6.RESULTS_TSV = results_tsv

    result = h6.run_analysis()

    assert result["summary"]["total_signals"] == 3
    assert result["critical_findings"]["no_side_catastrophe"] is True
    assert result["verdict"]["h6_status"] == "KILLED"


def test_h2_load_ledger_divergence_pairs_pregame_crowd_and_quotes(tmp_path):
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "oracle-h2-crowd-divergence.py"
    h2 = _load_script_module("oracle_h2_script", script_path)

    ledger_path = tmp_path / "oracle-alpha-ledger.sqlite3"
    capture = OracleAlphaCapture(path=ledger_path)
    observed_at = dt.datetime(2026, 4, 2, 18, 30, tzinfo=dt.timezone.utc)
    cycle_id = "oracle-h2-pregame-1"

    capture.record_probe_snapshot(
        snapshot_name="crowd_probability",
        observed_at=observed_at,
        hypothesis_id="H2_crowd_divergence",
        payload={
            "collector_mode": "pregame",
            "collector_cycle_id": cycle_id,
            "game_id": "game-1",
            "game_status": "scheduled",
            "team_a": "ATL",
            "team_b": "GSW",
            "team_a_prob": 0.57,
            "team_b_prob": 0.43,
            "start_time": "2026-04-02T23:00:00+00:00",
            "hours_to_tip": 4.5,
        },
    )
    snapshot = capture.record_probe_snapshot(
        snapshot_name="market_index_snapshot",
        observed_at=observed_at,
        hypothesis_id="H2_crowd_divergence",
        payload={
            "collector_mode": "pregame",
            "collector_cycle_id": cycle_id,
            "mapped_games": 1,
            "mapped_tickers": ["KXNBAGAME-26APR02ATLGSW-ATL"],
            "market_index": {"game-1": ["KXNBAGAME-26APR02ATLGSW-ATL"]},
        },
    )
    capture.record_quote_snapshot(
        ticker="KXNBAGAME-26APR02ATLGSW-ATL",
        yes_bid_cents=52,
        yes_ask_cents=54,
        yes_bid_depth=18,
        yes_ask_depth=20,
        quote_timestamp=observed_at.isoformat(),
        source_event_id=snapshot["event_id"],
        source_event_type="market_index_snapshot",
        source_event_timestamp_utc=snapshot["observed_at"],
        game_id="game-1",
        hypothesis_id="H2_crowd_divergence",
        extra={
            "capture_mode": "baseline",
            "collector_mode": "pregame",
            "collector_cycle_id": cycle_id,
            "hours_to_tip": 4.5,
            "game_status": "scheduled",
            "start_time": "2026-04-02T23:00:00+00:00",
        },
    )

    rows = h2.load_ledger_divergence(ledger_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "KXNBAGAME-26APR02ATLGSW-ATL"
    assert row["team"] == "ATL"
    assert row["crowd_prob"] == 0.57
    assert row["kalshi_implied"] == 0.53
    assert row["spread_cents"] == 2
    assert row["hours_to_tip_bucket"] == "4-8h"
    assert row["survives_spread_and_fees"] is True
