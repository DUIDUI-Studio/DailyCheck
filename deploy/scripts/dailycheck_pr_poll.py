#!/usr/bin/env python3
"""Repository entry point for the canonical provider-aware PR poller."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.prod_agent.pr_poll import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
