# HANDOFF: owned by Claude (risk/). Edit only via HANDOFF.md Open Request.
"""Anti-overtrade cooldown state machine.

Enforces the blueprint's selectivity rules that ``RiskGuard`` does not cover:

* min time between new entries on the same side
* cooldown after a stop / scratch exit
* cooldown while a data-degradation flag is hot
* no-flip-flop: lock the market after N side changes
* max cancel/replace burst within a sliding window

State is per-process. The decision orchestrator owns one ``CooldownGuard``;
record entry/exit/cancel events as they happen, then ``check_entry`` gates
the next attempted trade.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal

Side = Literal["yes", "no"]
ExitKind = Literal["stop", "scratch", "profit", "time", "settlement", "manual", "degraded"]


@dataclass(frozen=True, slots=True)
class CooldownConfig:
    same_side_min_gap_ms: int = 20_000
    stop_exit_cooldown_ms: int = 90_000
    scratch_exit_cooldown_ms: int = 30_000
    degraded_clear_required_ms: int = 60_000
    cancel_replace_max_in_window: int = 3
    cancel_replace_window_ms: int = 10_000
    max_side_changes_per_market: int = 2


@dataclass(slots=True)
class CooldownDecision:
    allowed: bool
    code: str
    reason: str

    @classmethod
    def allow(cls) -> CooldownDecision:
        return cls(True, "ALLOW", "")

    @classmethod
    def block(cls, code: str, reason: str) -> CooldownDecision:
        return cls(False, code, reason)


@dataclass(slots=True)
class CooldownGuard:
    config: CooldownConfig = field(default_factory=CooldownConfig)
    _last_entry_ms_per_side: dict[tuple[str, Side], int] = field(default_factory=dict)
    _exit_unlock_ms_per_market: dict[str, int] = field(default_factory=dict)
    _side_changes_per_market: dict[str, int] = field(default_factory=dict)
    _last_side_per_market: dict[str, Side] = field(default_factory=dict)
    _cancel_replace_log: deque[int] = field(default_factory=deque)
    _data_degraded_until_ms: int = 0

    def check_entry(
        self,
        *,
        market_ticker: str,
        side: Side,
        now_ms: int,
    ) -> CooldownDecision:
        if self._data_degraded_until_ms > now_ms:
            return CooldownDecision.block(
                "DATA_DEGRADED",
                f"data degraded until ts={self._data_degraded_until_ms}",
            )
        unlock = self._exit_unlock_ms_per_market.get(market_ticker, 0)
        if unlock > now_ms:
            return CooldownDecision.block(
                "EXIT_COOLDOWN",
                f"locked until ts={unlock}",
            )
        last_entry = self._last_entry_ms_per_side.get((market_ticker, side))
        if last_entry is not None and now_ms - last_entry < self.config.same_side_min_gap_ms:
            return CooldownDecision.block(
                "SAME_SIDE_TOO_SOON",
                f"{now_ms - last_entry}ms since last {side} entry",
            )
        if (
            self._side_changes_per_market.get(market_ticker, 0)
            >= self.config.max_side_changes_per_market
        ):
            return CooldownDecision.block(
                "FLIP_FLOP_LOCK",
                f"side changes={self._side_changes_per_market[market_ticker]}",
            )
        if self._cancel_replace_burst_blocked(now_ms):
            return CooldownDecision.block(
                "CANCEL_REPLACE_BURST",
                f"{self.config.cancel_replace_max_in_window} cancel/replace in "
                f"{self.config.cancel_replace_window_ms}ms",
            )
        return CooldownDecision.allow()

    def record_entry(
        self,
        *,
        market_ticker: str,
        side: Side,
        now_ms: int,
    ) -> None:
        self._last_entry_ms_per_side[(market_ticker, side)] = now_ms
        previous = self._last_side_per_market.get(market_ticker)
        if previous is not None and previous != side:
            self._side_changes_per_market[market_ticker] = (
                self._side_changes_per_market.get(market_ticker, 0) + 1
            )
        self._last_side_per_market[market_ticker] = side

    def record_exit(
        self,
        *,
        market_ticker: str,
        kind: ExitKind,
        now_ms: int,
    ) -> None:
        if kind == "stop":
            self._exit_unlock_ms_per_market[market_ticker] = (
                now_ms + self.config.stop_exit_cooldown_ms
            )
        elif kind == "scratch":
            self._exit_unlock_ms_per_market[market_ticker] = (
                now_ms + self.config.scratch_exit_cooldown_ms
            )
        elif kind == "degraded":
            self._data_degraded_until_ms = max(
                self._data_degraded_until_ms,
                now_ms + self.config.degraded_clear_required_ms,
            )

    def record_cancel_replace(self, *, now_ms: int) -> None:
        self._cancel_replace_log.append(now_ms)
        # Trim entries older than the window.
        while (
            self._cancel_replace_log
            and self._cancel_replace_log[0] < now_ms - self.config.cancel_replace_window_ms
        ):
            self._cancel_replace_log.popleft()

    def mark_data_degraded(self, *, now_ms: int) -> None:
        self._data_degraded_until_ms = max(
            self._data_degraded_until_ms,
            now_ms + self.config.degraded_clear_required_ms,
        )

    def reset_market(self, market_ticker: str) -> None:
        self._side_changes_per_market.pop(market_ticker, None)
        self._last_side_per_market.pop(market_ticker, None)
        self._exit_unlock_ms_per_market.pop(market_ticker, None)

    def _cancel_replace_burst_blocked(self, now_ms: int) -> bool:
        threshold_ms = now_ms - self.config.cancel_replace_window_ms
        recent = sum(1 for ts in self._cancel_replace_log if ts >= threshold_ms)
        return recent >= self.config.cancel_replace_max_in_window
