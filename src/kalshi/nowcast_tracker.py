"""Track Cleveland Fed nowcast history for empirical sigma calibration.

Records daily nowcast snapshots and actual BLS prints. Over time, builds
an empirical forecast error distribution per horizon bucket, replacing
heuristic sigma curves with measured accuracy.
"""

import json
import math
import time
from pathlib import Path
from kalshi_auth import _atomic_write_json, PROJECT_DIR


HISTORY_PATH = PROJECT_DIR / "data" / "nowcast-history.json"

# Horizon buckets for sigma computation
HORIZON_BUCKETS = [0, 3, 7, 14, 30, 60, 90]


class NowcastTracker:
    """Record and analyze nowcast forecast accuracy over time."""

    def __init__(self, history_path=None):
        self.path = Path(history_path) if history_path else HISTORY_PATH

    def load_history(self):
        """Load nowcast history from disk."""
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    def _save_history(self, history):
        """Save history atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.path, history)

    def record_snapshot(self, nowcast_value, measure, target_month, days_to_release):
        """Record a nowcast observation.

        Args:
            nowcast_value: The nowcast point estimate (e.g., 2.41).
            measure: "cpi_yoy", "core_cpi_yoy", "gdp_growth", etc.
            target_month: "YYYY-MM" of the data being forecast.
            days_to_release: Days until the BLS/BEA release.
        """
        history = self.load_history()
        history.append({
            "timestamp": time.time(),
            "nowcast_value": nowcast_value,
            "measure": measure,
            "target_month": target_month,
            "days_to_release": days_to_release,
            "actual_value": None,
        })
        self._save_history(history)

    def record_actual(self, measure, target_month, actual_value):
        """Record the actual BLS/BEA print for a target month.

        Backfills actual_value on all matching snapshots.
        """
        history = self.load_history()
        for entry in history:
            if (entry["measure"] == measure and
                entry["target_month"] == target_month and
                entry["actual_value"] is None):
                entry["actual_value"] = actual_value
        self._save_history(history)

    def compute_empirical_sigmas(self):
        """Compute empirical sigma per horizon bucket.

        Groups forecast errors by days_to_release bucket, computes
        RMSE as the empirical sigma estimate.

        Returns:
            Dict[str, float]: {"7": 0.10, "30": 0.25, ...}
        """
        history = self.load_history()
        paired = [e for e in history if e.get("actual_value") is not None]

        if not paired:
            return {}

        # Bucket errors by horizon
        buckets = {str(b): [] for b in HORIZON_BUCKETS}
        for entry in paired:
            d = entry["days_to_release"]
            # Find the closest bucket
            closest = min(HORIZON_BUCKETS, key=lambda b: abs(b - d))
            error = entry["nowcast_value"] - entry["actual_value"]
            buckets[str(closest)].append(error)

        # Compute RMSE per bucket
        sigmas = {}
        for bucket, errors in buckets.items():
            if len(errors) >= 3:  # Need minimum samples
                rmse = math.sqrt(sum(e**2 for e in errors) / len(errors))
                sigmas[bucket] = round(rmse, 4)

        return sigmas
