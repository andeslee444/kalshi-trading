"""Direct tests for the research.trade_attribution module."""

import json

from artifact_contracts import TRADE_ATTRIBUTION_ARTIFACT, TRADE_ATTRIBUTION_SCHEMA_VERSION
from research.trade_attribution import TradeAttributionArtifact, _load_trade_attribution_safe


def _sample_report():
    return {
        "generated_at": "2026-03-17T01:10:00+00:00",
        "by_bot": {
            "weather": {
                "pnl_cents": 80,
                "trades": 2,
                "wins": 1,
                "losses": 1,
                "win_rate": 0.5,
            }
        },
        "summary": {
            "total_trades_settled": 2,
            "total_trades_all": 3,
            "total_pnl_cents": 80,
        },
    }


def test_load_trade_attribution_safe_handles_missing_and_valid_files(tmp_path):
    path = tmp_path / "attribution-report.json"
    path.write_text(json.dumps(_sample_report()))

    missing = _load_trade_attribution_safe(tmp_path / "missing.json")
    loaded = _load_trade_attribution_safe(path)

    assert missing["artifact_type"] == TRADE_ATTRIBUTION_ARTIFACT
    assert missing["summary"] == {}
    assert loaded["artifact_type"] == TRADE_ATTRIBUTION_ARTIFACT
    assert loaded["summary"]["total_trades_settled"] == 2


def test_trade_attribution_artifact_save_normalizes_and_roundtrips(tmp_path):
    fixed_now = "2026-03-17T01:15:00+00:00"
    artifact = TradeAttributionArtifact(
        path=tmp_path / "attribution-report.json",
        utc_now_iso_func=lambda: fixed_now,
    )

    saved = artifact.save({"summary": {"total_trades_settled": 1}})
    loaded = artifact.load()

    assert saved["artifact_type"] == TRADE_ATTRIBUTION_ARTIFACT
    assert saved["schema_version"] == TRADE_ATTRIBUTION_SCHEMA_VERSION
    assert saved["generated_at"] == fixed_now
    assert saved["report_name"] == "daily_attribution"
    assert saved["by_bot"] == {}
    assert loaded == saved
