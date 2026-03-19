"""Tests for results.tsv integrity chain — creation, verification, tamper detection."""

import hashlib
from pathlib import Path

from run_experiment import (
    _chain_hash,
    _get_last_chain_hash,
    verify_results_integrity,
    append_results,
    RESULTS_HEADER,
)


def test_chain_hash_deterministic():
    h = _chain_hash("seed", "some line\n")
    assert isinstance(h, str)
    assert len(h) == 64
    assert h == _chain_hash("seed", "some line\n")


def test_chain_hash_changes_with_input():
    h1 = _chain_hash("seed", "line A\n")
    h2 = _chain_hash("seed", "line B\n")
    assert h1 != h2


def test_chain_seed_from_header():
    seed = _get_last_chain_hash(Path("/nonexistent"), RESULTS_HEADER)
    assert seed == hashlib.sha256(RESULTS_HEADER.encode()).hexdigest()


def test_verify_no_results_file(tmp_path):
    valid, n, msg = verify_results_integrity(tmp_path / "nope.tsv")
    assert valid is True
    assert n == 0


def test_verify_no_integrity_file(tmp_path):
    tsv = tmp_path / "results.tsv"
    tsv.write_text(RESULTS_HEADER + "abc\t0.25\t0.05\t3.0\t0.5\t100\t10\t0.24-0.26\trun\ttest\n")
    valid, n, msg = verify_results_integrity(tsv)
    assert valid is True
    assert "pre-chain" in msg.lower() or "no integrity" in msg.lower()


def _append_to_files(tmp_path, commit, brier, status="run", desc="test"):
    """Directly write to results.tsv + integrity using the chain logic, scoped to tmp_path."""
    import hashlib as _hl
    from run_experiment import _chain_hash, RESULTS_HEADER

    tsv = tmp_path / "results.tsv"
    integrity = tmp_path / "results.tsv.integrity"
    if not tsv.exists():
        tsv.write_text(RESULTS_HEADER)

    line = f"{commit}\t{brier:.4f}\t0.0500\t0.0\t0.500\t8000\t100\t0.24-0.26\t{status}\t{desc}\n"

    # Get previous hash
    seed = _hl.sha256(RESULTS_HEADER.encode()).hexdigest()
    if integrity.exists():
        text = integrity.read_text().strip()
        prev = text.split("\n")[-1].strip() if text else seed
    else:
        prev = seed

    new_hash = _chain_hash(prev, line)
    with open(integrity, "a") as f:
        f.write(new_hash + "\n")
    with open(tsv, "a") as f:
        f.write(line)


def test_append_and_verify_round_trip(tmp_path):
    tsv = tmp_path / "results.tsv"
    for i in range(3):
        _append_to_files(tmp_path, f"abc{i:04d}", 0.25 - i * 0.01)
    valid, n, msg = verify_results_integrity(tsv)
    assert valid is True
    assert n >= 2


def test_modified_row_breaks_chain(tmp_path):
    tsv = tmp_path / "results.tsv"
    for i in range(3):
        _append_to_files(tmp_path, f"abc{i:04d}", 0.25 - i * 0.01)
    content = tsv.read_text()
    content = content.replace("0.2500", "0.1500", 1)
    tsv.write_text(content)
    valid, n, msg = verify_results_integrity(tsv)
    assert valid is False
    assert "broken" in msg.lower()


def test_orphan_hash_tolerated(tmp_path):
    """Simulate a kill between integrity write and TSV write."""
    tsv = tmp_path / "results.tsv"
    integrity = tmp_path / "results.tsv.integrity"
    for i in range(2):
        _append_to_files(tmp_path, f"abc{i:04d}", 0.25)
    # Add an orphan hash
    with open(integrity, "a") as f:
        f.write("deadbeef" * 8 + "\n")
    valid, n, msg = verify_results_integrity(tsv)
    assert valid is True
