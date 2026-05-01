"""Scoring metrics for probabilistic forecasts — shared across strategies.

Brier score, log-loss, and reliability bins. Pure math, asset-agnostic;
both weather (KXHIGH bracket) and ETH (KXETHD greater-than) consume
this same module from master.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ScoredPrediction:
    """A single (predicted probability, binary outcome) pair."""

    prob: float
    outcome: int  # 0 or 1
    label: str = ""  # optional free-form context, e.g. ticker + cutoff


def brier_score(predictions: Sequence[ScoredPrediction]) -> float:
    """Mean Brier score: lower is better, range [0, 1]."""
    if not predictions:
        return float("nan")
    return sum((p.prob - p.outcome) ** 2 for p in predictions) / len(predictions)


def log_loss(predictions: Sequence[ScoredPrediction], eps: float = 1e-9) -> float:
    """Mean log-loss: lower is better."""
    if not predictions:
        return float("nan")
    total = 0.0
    for p in predictions:
        clipped = min(1.0 - eps, max(eps, p.prob))
        total += -p.outcome * math.log(clipped) - (1 - p.outcome) * math.log(1.0 - clipped)
    return total / len(predictions)


@dataclass(frozen=True)
class CalibrationBin:
    """Reliability stats for one probability bucket."""

    lo: float
    hi: float
    n: int
    mean_prob: float
    empirical_rate: float


def calibration_bins(
    predictions: Sequence[ScoredPrediction],
    n_bins: int = 10,
) -> list[CalibrationBin]:
    """Equal-width reliability bins.

    For a perfectly calibrated model, ``mean_prob`` ≈ ``empirical_rate``
    in every bin.
    """
    bins: list[list[ScoredPrediction]] = [[] for _ in range(n_bins)]
    for pred in predictions:
        idx = min(n_bins - 1, max(0, int(pred.prob * n_bins)))
        bins[idx].append(pred)
    out: list[CalibrationBin] = []
    for i, contents in enumerate(bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        if not contents:
            out.append(CalibrationBin(lo=lo, hi=hi, n=0, mean_prob=float("nan"), empirical_rate=float("nan")))
            continue
        mean_p = sum(p.prob for p in contents) / len(contents)
        emp = sum(p.outcome for p in contents) / len(contents)
        out.append(CalibrationBin(lo=lo, hi=hi, n=len(contents), mean_prob=mean_p, empirical_rate=emp))
    return out


def format_metrics(predictions: Sequence[ScoredPrediction]) -> str:
    """Human-readable summary string with calibration table."""
    n = len(predictions)
    if n == 0:
        return "(no predictions)"
    bs = brier_score(predictions)
    ll = log_loss(predictions)
    out = [
        f"n={n}",
        f"brier={bs:.4f}",
        f"log_loss={ll:.4f}",
        "",
        "calibration (equal-width 10 bins):",
        "  bin       n    mean_p   empirical",
    ]
    for b in calibration_bins(predictions):
        if b.n == 0:
            out.append(f"  [{b.lo:.1f},{b.hi:.1f})    0       -          -")
        else:
            out.append(f"  [{b.lo:.1f},{b.hi:.1f}) {b.n:5d}   {b.mean_prob:.3f}      {b.empirical_rate:.3f}")
    return "\n".join(out)
