# Multi-Agent Prompt Template

Use this shape when handing work between Codex, Claude, or any other agent.

```text
Read C:\Trading\kalshi-btc-engine-v2\HANDOFF.md first.

Task:
<specific outcome>

Lane:
<directories/files you may touch>

Forbidden:
<directories/files you must not touch>

Stop after:
<concrete deliverable and stop point>

Run before returning:
- $env:PYTHONPATH='src'; py -m pytest <scope> -q
- py -m ruff check <scope>
- py -m black --check <scope>

Return:
- Files changed
- Verification commands and results
- One-paragraph summary
- Any out-of-lane touches, or "none"

Before stopping:
- Update C:\Trading\kalshi-btc-engine-v2\HANDOFF.md with current status.
```

## Example

```text
Read C:\Trading\kalshi-btc-engine-v2\HANDOFF.md first.

Task:
Build M8-lite replay/backtest harness for captured burn-in data. It should run
against SQLite event streams and produce basic fill/edge diagnostics. No live
orders.

Lane:
src/kalshi_btc_engine_v2/backtest/**
tests/test_backtest*.py
docs/backtest.md
HANDOFF.md

Forbidden:
C:\Trading\btc-bias-engine\
src/kalshi_btc_engine_v2/capture/**
src/kalshi_btc_engine_v2/adapters/**
src/kalshi_btc_engine_v2/execution/**

Stop after:
Synthetic replay tests and a CLI-free backtest API are complete. Do not wire
policy or execution.

Run before returning:
- $env:PYTHONPATH='src'; py -m pytest tests/test_backtest*.py -q
- py -m ruff check src/kalshi_btc_engine_v2/backtest tests/test_backtest*.py
- py -m black --check src/kalshi_btc_engine_v2/backtest tests/test_backtest*.py

Return:
- Files changed
- Verification commands and results
- One-paragraph summary
- Any out-of-lane touches, or "none"

Before stopping:
- Update C:\Trading\kalshi-btc-engine-v2\HANDOFF.md with current status.
```

## Ownership Header

For contested files, add a short header comment:

```python
# HANDOFF: owned by Claude (models/). Edit only via HANDOFF.md coordination.
```

Only add this where ownership has already caused friction or where concurrent
edits are likely. Do not blanket every file.

