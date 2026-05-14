# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Divergence-logit distribution analyzer.

Reads a decision log and extracts ``divergence_logit`` values from each
decision's diagnostics (where the regime classifier recorded them). Reports the
distribution and what percentile each candidate ``mean_revert_min_divergence``
threshold would correspond to.

Designed to calibrate the regime classifier's thresholds against observed
data rather than blueprint defaults. The default 0.5 threshold proved to be
below the median magnitude in v2's first 4h burn-in (median ≈ 5.0), labeling
~100% of decisions as ``mean_revert_dislocation``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_DIVERGENCE_RE = re.compile(r"divergence_logit=([-+]?\d*\.?\d+)")


@dataclass(slots=True)
class DivergenceStats:
    sample_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    abs_percentiles: dict[str, float] = field(default_factory=dict)
    threshold_crossings: dict[str, dict[str, float]] = field(default_factory=dict)
    max_abs: float = 0.0
    median_signed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "sample_count": self.sample_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "median_signed": self.median_signed,
            "max_abs": self.max_abs,
            "abs_percentiles": self.abs_percentiles,
            "threshold_crossings": self.threshold_crossings,
        }


def _extract_divergences(decision_log_path: str | Path) -> list[float]:
    out: list[float] = []
    with Path(decision_log_path).open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            diag = record.get("diag", {})
            reason = diag.get("regime_reason") or ""
            match = _DIVERGENCE_RE.search(reason)
            if match:
                try:
                    out.append(float(match.group(1)))
                except ValueError:
                    continue
    return out


def divergence_stats(
    decision_log_path: str | Path,
    *,
    candidate_thresholds: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 7.0, 10.0, 12.0),
) -> DivergenceStats:
    samples = _extract_divergences(decision_log_path)
    stats = DivergenceStats()
    if not samples:
        return stats
    n = len(samples)
    samples_sorted = sorted(samples)
    abs_sorted = sorted(abs(x) for x in samples)
    stats.sample_count = n
    stats.positive_count = sum(1 for x in samples if x > 0)
    stats.negative_count = sum(1 for x in samples if x < 0)
    stats.median_signed = samples_sorted[n // 2]
    stats.max_abs = abs_sorted[-1]
    for pct in (50, 75, 90, 95, 99):
        idx = min(n - 1, int(n * pct / 100))
        stats.abs_percentiles[f"p{pct}"] = round(abs_sorted[idx], 4)
    for thresh in candidate_thresholds:
        crossings = sum(1 for x in abs_sorted if x >= thresh)
        stats.threshold_crossings[str(thresh)] = {
            "count": crossings,
            "fraction": round(crossings / n, 4),
        }
    return stats
