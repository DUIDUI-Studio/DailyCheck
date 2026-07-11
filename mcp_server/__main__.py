"""MCP Server CLI entry point — allows running as: python -m mcp_server"""
from __future__ import annotations

import sys
from pathlib import Path

# Compute project root (parent of mcp_server/) and add to sys.path
# so that 'from mcp_server import ...' works regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_server.main import run

if __name__ == "__main__":
    run()
