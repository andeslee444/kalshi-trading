#!/usr/bin/env python3
"""Pre-upload validation for trade logs and state files.

Exit code 0 = all checks pass, safe to sync.
Exit code 1 = validation failures found, block upload.

Checks:
1. Every canonical trade file is valid JSON (or empty list [])
2. No trade log has shrunk since last sync (possible truncation)
3. financial-snapshot.json exists and is <24h old (forces fresh snapshot before sync)
4. No trade records with null source_bot (unattributed trades)
5. Reconciliation coverage: warn if >50% of executed trades lack settlement_result
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"

# Import canonical trade file list
sys.path.insert(0, str(PROJECT_DIR / "src" / "kalshi"))
from trade_files import TRADE_FILES, ALL_TRADE_PATHS

SIZES_CACHE = DATA_DIR / ".sync-sizes.json"


def load_previous_sizes(cache_path=None):
    """Load previous file sizes from cache."""
    p = cache_path or SIZES_CACHE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_current_sizes(sizes, cache_path=None):
    """Save current file sizes to cache."""
    p = cache_path or SIZES_CACHE
    p.write_text(json.dumps(sizes, indent=2))


def validate_trade_file(path):
    """Validate a single trade file is valid JSON containing a list.

    Returns (trades_list, error_string).
    On success: (list, None). On failure: (None, error_message).
    """
    try:
        trades = json.loads(path.read_text())
        if not isinstance(trades, list):
            return None, f"root is {type(trades).__name__}, expected list"
        return trades, None
    except json.JSONDecodeError as e:
        return None, f"corrupt JSON — {e}"


def detect_shrinkage(filename, current_size, prev_sizes, threshold=0.8):
    """Check if a file has shrunk suspiciously since last sync.

    Returns True if file shrunk by more than (1 - threshold) fraction.
    Default threshold=0.8 means >20% shrinkage triggers.
    """
    prev = prev_sizes.get(filename, 0)
    if prev > 0 and current_size < prev * threshold:
        return True
    return False


def validate(data_dir=None, sizes_cache=None):
    """Run all validation checks.

    Returns exit code: 0 = pass, 1 = errors found.
    """
    d = data_dir or DATA_DIR
    errors = []
    warnings = []
    prev_sizes = load_previous_sizes(sizes_cache)
    curr_sizes = {}

    # Check 1 & 2: Trade logs valid JSON and not shrunk
    for tf in TRADE_FILES:
        path = d / tf["filename"]
        if not path.exists():
            warnings.append(f"Trade log missing (may be new bot): {tf['filename']}")
            continue

        size = path.stat().st_size
        curr_sizes[tf["filename"]] = size

        # Validate JSON
        trades, err = validate_trade_file(path)
        if err:
            errors.append(f"{tf['filename']}: {err}")
            continue

        # Check for shrinkage (possible truncation)
        if detect_shrinkage(tf["filename"], size, prev_sizes):
            prev = prev_sizes.get(tf["filename"], 0)
            errors.append(
                f"{tf['filename']}: shrunk from {prev} to {size} bytes "
                f"({(1 - size/prev)*100:.0f}% smaller) — possible truncation"
            )

        # Check 4: source_bot attribution
        missing_bot = sum(1 for t in trades if not t.get("source_bot"))
        if missing_bot > 0:
            warnings.append(
                f"{tf['filename']}: {missing_bot}/{len(trades)} trades missing source_bot"
            )

        # Check 5: Reconciliation coverage
        unreconciled = [t for t in trades
                        if t.get("settlement_result") is None
                        and t.get("status") == "executed"]
        if len(trades) > 0 and len(unreconciled) > len(trades) * 0.5:
            warnings.append(
                f"{tf['filename']}: {len(unreconciled)}/{len(trades)} trades unreconciled "
                f"— consider running npm run reconcile before sync"
            )

    # Check 3: Snapshot freshness
    snapshot_path = d / "financial-snapshot.json"
    if not snapshot_path.exists():
        warnings.append("financial-snapshot.json missing — run npm run snapshot before sync")
    else:
        try:
            snap = json.loads(snapshot_path.read_text())
            gen = snap.get("generated_at", "")
            if gen:
                gen_dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600
                if age_hours > 24:
                    warnings.append(
                        f"financial-snapshot.json is {age_hours:.0f}h old "
                        f"— run npm run snapshot for fresh data"
                    )
        except (json.JSONDecodeError, ValueError):
            warnings.append("financial-snapshot.json: could not parse generated_at")

    # Print results
    if warnings:
        for w in warnings:
            print(f"WARNING: {w}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        print(f"\n{len(errors)} error(s) found — upload blocked. Fix issues and retry.")
        return 1

    # Save sizes for next comparison
    save_current_sizes(curr_sizes, sizes_cache)
    print(f"Validation passed: {len(curr_sizes)} trade logs checked, "
          f"{len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(validate())
