"""Backwards-compat shim. Real implementation lives in ``master.metrics``.

Kept so any existing imports (e.g. ad-hoc scripts) keep working. Prefer
importing from ``master.metrics`` directly in new code.
"""

from master.metrics import (  # noqa: F401
    CalibrationBin,
    ScoredPrediction,
    brier_score,
    calibration_bins,
    format_metrics,
    log_loss,
)
