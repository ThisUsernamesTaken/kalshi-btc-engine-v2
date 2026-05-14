"""Execution gateway: paper simulator and gated live executor."""

from kalshi_btc_engine_v2.execution.live import LiveExecutor
from kalshi_btc_engine_v2.execution.paper import PaperExecutor
from kalshi_btc_engine_v2.execution.types import (
    BookSide,
    ExecutionFill,
    ExecutionMode,
    ExecutionResult,
    Position,
)

__all__ = [
    "BookSide",
    "ExecutionFill",
    "ExecutionMode",
    "ExecutionResult",
    "LiveExecutor",
    "PaperExecutor",
    "Position",
]
