"""Portable plugin launcher that resolves the enclosing trading-system checkout."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PLUGIN_ROOT.parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("TRADING_SYSTEM_ROOT", str(PROJECT_ROOT))

from agent_gateway.kernel_server import main  # noqa: E402

if __name__ == "__main__":
    main()
