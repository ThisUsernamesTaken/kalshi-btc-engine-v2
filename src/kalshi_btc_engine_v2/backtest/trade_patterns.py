# HANDOFF: owned by Claude (backtest/). Edit only via HANDOFF.md Open Request.
"""Trade-pattern detector.

Scans a decision log for failure-mode patterns from the SCALP catastrophe
history and the new v2 empirical evidence:

* **quick_flip** — BUY then EXIT within ``quick_flip_max_s`` seconds. v2
  empirical default 9s avg hold suggests anything <30s on a 15-min market is
  symptomatic.
* **chase** — multiple BUY entries on the same side within a short window
  while price keeps moving away. The signature of "fade-the-rising-market"
  losses in the ungated counterfactual.
* **flip_flop** — BUY YES followed by BUY NO (or vice versa) on the same
  market within ``flip_flop_window_s``. Indicates the engine is whipsawing
  between sides.

Counts each pattern overall and per market. Useful for tuning cooldowns and
detecting model-vs-market structural mispricing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TradePatternConfig:
    quick_flip_max_s: float = 30.0
    chase_window_s: float = 60.0
    chase_min_entries: int = 2
    flip_flop_window_s: float = 60.0


@dataclass(slots=True)
class TradePatternReport:
    config: TradePatternConfig
    quick_flips: int = 0
    chases: int = 0
    flip_flops: int = 0
    per_market: dict[str, dict[str, int]] = field(default_factory=dict)
    quick_flip_samples: list[dict] = field(default_factory=list)
    chase_samples: list[dict] = field(default_factory=list)
    flip_flop_samples: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "config": {
                "quick_flip_max_s": self.config.quick_flip_max_s,
                "chase_window_s": self.config.chase_window_s,
                "chase_min_entries": self.config.chase_min_entries,
                "flip_flop_window_s": self.config.flip_flop_window_s,
            },
            "totals": {
                "quick_flips": self.quick_flips,
                "chases": self.chases,
                "flip_flops": self.flip_flops,
            },
            "per_market": self.per_market,
            "quick_flip_samples": self.quick_flip_samples[:10],
            "chase_samples": self.chase_samples[:10],
            "flip_flop_samples": self.flip_flop_samples[:10],
        }


def detect_patterns(
    decision_log_path: str | Path,
    *,
    config: TradePatternConfig | None = None,
) -> TradePatternReport:
    cfg = config or TradePatternConfig()
    decisions: list[dict] = []
    with Path(decision_log_path).open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    decisions.sort(key=lambda d: int(d.get("ts_ms", 0)))
    report = TradePatternReport(config=cfg)

    by_market: dict[str, list[dict]] = {}
    for d in decisions:
        ticker = d.get("market_ticker") or ""
        by_market.setdefault(ticker, []).append(d)

    for ticker, market_decisions in by_market.items():
        counts = {"quick_flip": 0, "chase": 0, "flip_flop": 0}
        report.per_market[ticker] = counts

        # quick_flip: pair each entry to next EXIT (any side); flag if delta < threshold
        open_entry: dict | None = None
        for d in market_decisions:
            action = d.get("action")
            if action in {"BUY_YES", "BUY_NO"}:
                open_entry = d
            elif action == "EXIT" and open_entry is not None:
                delta_s = (int(d.get("ts_ms", 0)) - int(open_entry.get("ts_ms", 0))) / 1000.0
                if delta_s <= cfg.quick_flip_max_s:
                    counts["quick_flip"] += 1
                    report.quick_flips += 1
                    if len(report.quick_flip_samples) < 50:
                        report.quick_flip_samples.append(
                            {
                                "market": ticker,
                                "side": open_entry.get("side"),
                                "hold_s": round(delta_s, 2),
                                "entry_q": open_entry.get("q_cal"),
                            }
                        )
                open_entry = None

        # chase: window of N same-side entries
        entries_only = [d for d in market_decisions if d.get("action") in {"BUY_YES", "BUY_NO"}]
        for i, entry in enumerate(entries_only):
            same_side_in_window = 1
            for j in range(i + 1, len(entries_only)):
                later = entries_only[j]
                gap_s = (int(later.get("ts_ms", 0)) - int(entry.get("ts_ms", 0))) / 1000.0
                if gap_s > cfg.chase_window_s:
                    break
                if later.get("side") == entry.get("side"):
                    same_side_in_window += 1
            if same_side_in_window >= cfg.chase_min_entries:
                counts["chase"] += 1
                report.chases += 1
                if len(report.chase_samples) < 50:
                    report.chase_samples.append(
                        {
                            "market": ticker,
                            "side": entry.get("side"),
                            "entries_in_window": same_side_in_window,
                            "first_q": entry.get("q_cal"),
                        }
                    )

        # flip_flop: opposite-side entry within window
        for i, entry in enumerate(entries_only):
            for j in range(i + 1, len(entries_only)):
                later = entries_only[j]
                gap_s = (int(later.get("ts_ms", 0)) - int(entry.get("ts_ms", 0))) / 1000.0
                if gap_s > cfg.flip_flop_window_s:
                    break
                if later.get("side") != entry.get("side"):
                    counts["flip_flop"] += 1
                    report.flip_flops += 1
                    if len(report.flip_flop_samples) < 50:
                        report.flip_flop_samples.append(
                            {
                                "market": ticker,
                                "from_side": entry.get("side"),
                                "to_side": later.get("side"),
                                "gap_s": round(gap_s, 2),
                            }
                        )
                    break  # only count one flip per entry

    return report
