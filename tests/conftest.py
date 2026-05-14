from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# Allow `from scripts.<name> import ...` in tests (live_paper_tail etc).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
