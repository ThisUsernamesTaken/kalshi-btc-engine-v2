$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $repo "src"
python -m kalshi_btc_engine_v2.cli smoke-replay --db (Join-Path $repo "data\smoke.sqlite")
python -m kalshi_btc_engine_v2.cli continuity-report --db (Join-Path $repo "data\smoke.sqlite")
