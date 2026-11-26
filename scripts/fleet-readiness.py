#!/usr/bin/env python3
"""Node installer adapter; shared readiness remains owned by ProjectFlow."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_mcp.fleet_readiness import main
if __name__ == '__main__':
    raise SystemExit(main())
