"""Reference path constants from project root config.py."""
from __future__ import annotations
import sys
from pathlib import Path

# Add project root to sys.path to import project modules
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    BASE_DIR,
    MASTER_DB,
    SECRET_KEY,
)
