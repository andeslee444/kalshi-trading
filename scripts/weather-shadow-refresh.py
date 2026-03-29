#!/usr/bin/env python3
"""Refresh Phase-4-safe weather artifacts into a local shadow tree.

This orchestrator only writes non-canonical outputs by default. It can refresh:
- shadow observation and audit artifacts
- shadow backtest and calibration artifacts
- shadow promotion candidates
- optional shadow prior artifacts under a separate shadow DB/bias path
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "data" / "shadow" / "weather-refresh"
DEFAULT_SHADOW_ROOT = PROJECT_DIR / "data" / "shadow"

sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))
from kalshi_auth import atomic_write_json  # noqa: E402


@dataclass
class StepResult:
    name: str
    status: str
    command: list
    output_path: str | None
    duration_s: float
    returncode: int
    stderr: str = ""


def _resolve_output_dir(output_dir):
    path = Path(output_dir)
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def _validate_shadow_output_dir(path):
    resolved = Path(path).resolve()
    shadow_root = DEFAULT_SHADOW_ROOT.resolve()
    if not resolved.is_relative_to(shadow_root):
        raise ValueError(f"shadow refresh output must stay under {shadow_root}")
    return resolved


def _run_process(command, runner=subprocess.run):
    start = time.monotonic()
    proc = runner(command, capture_output=True, text=True)
    duration = round(time.monotonic() - start, 3)
    return proc, duration


def _load_json(text, step_name):
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{step_name}: could not parse JSON output: {exc}") from exc


def _persist_json(output_path, payload):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, payload)
    return output_path


def _count_training_pairs(db_path):
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM training_pairs")
        return int(cursor.fetchone()[0])
    finally:
        conn.close()


def _run_json_step(step_name, command, output_path=None, *, runner=subprocess.run, persist_output=False, compare_saved=False):
    proc, duration = _run_process(command, runner=runner)
    if proc.returncode != 0:
        return StepResult(
            name=step_name,
            status="failed",
            command=command,
            output_path=str(output_path) if output_path else None,
            duration_s=duration,
            returncode=proc.returncode,
            stderr=proc.stderr.strip(),
        )

    payload = _load_json(proc.stdout, step_name)
    saved_path = None
    if output_path is not None and persist_output:
        saved_path = _persist_json(output_path, payload)
    elif output_path is not None:
        saved_path = Path(output_path)

    if output_path is not None:
        if not Path(output_path).exists():
            return StepResult(
                name=step_name,
                status="failed",
                command=command,
                output_path=str(output_path),
                duration_s=duration,
                returncode=proc.returncode,
                stderr=f"{step_name}: expected output path was not written",
            )
        if compare_saved:
            saved_payload = _load_json(Path(output_path).read_text(), step_name)
            if saved_payload != payload:
                return StepResult(
                    name=step_name,
                    status="failed",
                    command=command,
                    output_path=str(output_path),
                    duration_s=duration,
                    returncode=proc.returncode,
                    stderr=f"{step_name}: stdout JSON did not match saved output",
                )

    return StepResult(
        name=step_name,
        status="ok",
        command=command,
        output_path=str(saved_path) if saved_path else None,
        duration_s=duration,
        returncode=proc.returncode,
    )


def _run_backfill_step(step_name, command, output_db, *, runner=subprocess.run):
    proc, duration = _run_process(command, runner=runner)
    if proc.returncode != 0:
        return StepResult(
            name=step_name,
            status="failed",
            command=command,
            output_path=str(output_db),
            duration_s=duration,
            returncode=proc.returncode,
            stderr=proc.stderr.strip(),
        )

    if not Path(output_db).exists():
        return StepResult(
            name=step_name,
            status="failed",
            command=command,
            output_path=str(output_db),
            duration_s=duration,
            returncode=proc.returncode,
            stderr=f"{step_name}: expected shadow DB was not written",
        )

    try:
        pair_count = _count_training_pairs(output_db)
    except sqlite3.Error as exc:
        return StepResult(
            name=step_name,
            status="failed",
            command=command,
            output_path=str(output_db),
            duration_s=duration,
            returncode=proc.returncode,
            stderr=f"{step_name}: could not inspect shadow DB: {exc}",
        )
    if pair_count <= 0:
        return StepResult(
            name=step_name,
            status="failed",
            command=command,
            output_path=str(output_db),
            duration_s=duration,
            returncode=proc.returncode,
            stderr=f"{step_name}: shadow DB contains zero training_pairs",
        )

    return StepResult(
        name=step_name,
        status="ok",
        command=command,
        output_path=str(output_db),
        duration_s=duration,
        returncode=proc.returncode,
    )


def refresh_weather_shadow_artifacts(
    output_dir=DEFAULT_OUTPUT_DIR,
    *,
    refresh_shadow_prior=False,
    backfill_days=14,
    backfill_models="gfs,ecmwf,icon,gem,graphcast",
    runner=subprocess.run,
):
    output_dir = _resolve_output_dir(output_dir)
    _validate_shadow_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    obs_path = output_dir / "weather-observation-pack.json"
    verification_summary_path = output_dir / "weather-verification-summary.json"
    city_audit_path = output_dir / "weather-city-audit.json"
    nws_crosscheck_path = output_dir / "weather-nws-crosscheck-audit.json"
    execution_audit_path = output_dir / "weather-execution-audit.json"
    backtest_path = output_dir / "weather-backtest-results.json"
    calibration_path = output_dir / "weather-calibration.json"
    promotion_candidates_path = output_dir / "weather-promotion-candidates.json"
    shadow_db_path = output_dir / "weather-training.db"
    shadow_bias_path = output_dir / "weather-live-bias.json"
    shadow_prior_ready = False

    steps = []

    steps.append(_run_json_step(
        "verification_summary",
        [sys.executable, str(SCRIPTS_DIR / "weather-verification-summary.py"), "--json"],
        verification_summary_path,
        runner=runner,
        persist_output=True,
    ))
    steps.append(_run_json_step(
        "city_audit",
        [
            sys.executable,
            str(SCRIPTS_DIR / "weather-city-audit.py"),
            "--lookback-days",
            "30",
            "--json",
        ],
        city_audit_path,
        runner=runner,
        persist_output=True,
    ))
    steps.append(_run_json_step(
        "nws_crosscheck_audit",
        [
            sys.executable,
            str(SCRIPTS_DIR / "weather-nws-crosscheck-audit.py"),
            "--lookback-days",
            "30",
            "--json",
        ],
        nws_crosscheck_path,
        runner=runner,
        persist_output=True,
    ))
    steps.append(_run_json_step(
        "execution_audit",
        [sys.executable, str(SCRIPTS_DIR / "weather-execution-audit.py"), "--json"],
        execution_audit_path,
        runner=runner,
        persist_output=True,
    ))
    steps.append(_run_json_step(
        "weather_backtest",
        [
            sys.executable,
            str(SCRIPTS_DIR / "backtest.py"),
            "--bot",
            "weather",
            "--no-api",
            "--save",
            "--output",
            str(backtest_path),
            "--json",
        ],
        backtest_path,
        runner=runner,
        persist_output=False,
        compare_saved=True,
    ))
    steps.append(_run_json_step(
        "weather_calibration",
        [
            sys.executable,
            str(SCRIPTS_DIR / "calibrate-sigma.py"),
            "--no-api",
            "--save",
            "--output",
            str(calibration_path),
            "--json",
        ],
        calibration_path,
        runner=runner,
        persist_output=False,
        compare_saved=True,
    ))

    if refresh_shadow_prior:
        backfill_command = [
            sys.executable,
            str(SCRIPTS_DIR / "backfill-weather-data.py"),
            "--days",
            str(backfill_days),
            "--models",
            backfill_models,
            "--db-path",
            str(shadow_db_path),
        ]
        steps.append(_run_backfill_step("shadow_prior_backfill", backfill_command, shadow_db_path, runner=runner))
        if steps[-1].status == "ok":
            steps.append(_run_json_step(
                "shadow_prior_calibration",
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "calibrate-weather-bias.py"),
                    "--db-path",
                    str(shadow_db_path),
                    "--output",
                    str(shadow_bias_path),
                    "--json",
                ],
                shadow_bias_path,
                runner=runner,
                persist_output=False,
                compare_saved=True,
            ))
            shadow_prior_ready = steps[-1].status == "ok"

    observation_command = [
        sys.executable,
        str(SCRIPTS_DIR / "weather-observation-pack.py"),
        "--backtest-results-path",
        str(backtest_path),
        "--calibration-path",
        str(calibration_path),
        "--save",
        "--output",
        str(obs_path),
        "--json",
    ]
    if shadow_prior_ready:
        observation_command.extend([
            "--weather-bias-path",
            str(shadow_bias_path),
        ])
    steps.append(_run_json_step(
        "observation_pack",
        observation_command,
        obs_path,
        runner=runner,
        persist_output=False,
        compare_saved=True,
    ))
    steps.append(_run_json_step(
        "promotion_candidates",
        [
            sys.executable,
            str(SCRIPTS_DIR / "weather-promotion-candidates.py"),
            "--observation-pack-path",
            str(obs_path),
            "--city-audit-path",
            str(city_audit_path),
            "--save",
            "--output",
            str(promotion_candidates_path),
            "--json",
        ],
        promotion_candidates_path,
        runner=runner,
        persist_output=False,
        compare_saved=True,
    ))

    failed_required = any(step.status == "failed" for step in steps if not step.name.startswith("shadow_prior"))
    if refresh_shadow_prior:
        failed_required = failed_required or any(
            step.status == "failed" for step in steps if step.name.startswith("shadow_prior")
        )
    status = "ok" if not failed_required else "failed"
    summary = {
        "artifact_type": "weather_shadow_refresh",
        "schema_version": 1,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "status": status,
        "refresh_shadow_prior": bool(refresh_shadow_prior),
        "steps": [asdict(step) for step in steps],
    }
    return summary


def _print_summary(summary):
    print(f"Weather shadow refresh: {summary['status']} -> {summary['output_dir']}")
    for step in summary.get("steps", []):
        print(f"  {step['name']}: {step['status']} ({step['duration_s']}s)")
    if summary.get("refresh_shadow_prior"):
        print("  shadow prior refresh: enabled")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh Phase-4-safe weather shadow artifacts")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Shadow output directory for refreshed artifacts",
    )
    parser.add_argument(
        "--refresh-shadow-prior",
        action="store_true",
        help="Also refresh a shadow training DB and lead-time-matched bias artifact",
    )
    parser.add_argument(
        "--backfill-days",
        type=int,
        default=14,
        help="History window for shadow prior backfill (default: 14)",
    )
    parser.add_argument(
        "--backfill-models",
        default="gfs,ecmwf,icon,gem,graphcast",
        help="Comma-separated models for shadow prior backfill (default: gfs,ecmwf,icon,gem,graphcast)",
    )
    parser.add_argument("--json", action="store_true", help="Print the refresh summary as JSON")
    parser.add_argument("--dry-run", action="store_true", help="Describe the refresh without running it")
    args = parser.parse_args(argv)

    output_dir = _resolve_output_dir(args.output_dir)
    _validate_shadow_output_dir(output_dir)
    if args.dry_run:
        print(f"Would refresh shadow weather artifacts into {output_dir}")
        if args.refresh_shadow_prior:
            print("Would also refresh shadow prior artifacts")
        return

    summary = refresh_weather_shadow_artifacts(
        output_dir=output_dir,
        refresh_shadow_prior=args.refresh_shadow_prior,
        backfill_days=args.backfill_days,
        backfill_models=args.backfill_models,
    )

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        _print_summary(summary)

    if summary["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
